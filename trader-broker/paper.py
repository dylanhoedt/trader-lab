#!/usr/bin/env python3
"""A local paper broker for trader-broker. Standard library only. No real money, ever.

It stands in for Alpaca's place_stock_order when there is no brokerage
connector (a cloud session, or no account yet). It fills orders against free
delayed quotes (Yahoo, via trader-alerts) and keeps the paper account in
trader/portfolio.json, so trader-risk, trader-tax and trader-rebalance see
the same account as with a real broker.

It keeps every gate of trader-broker: an order is only executed when
ticket.py recorded the user's explicit yes for THAT order today (which itself
needs a trader-risk PASS for that exact order today). One order per call.

  execute   fill one approved order (or queue it until the NYSE opens)
  settle    fill queued orders once the market is open; expire stale day orders
  account   cash, positions at current prices, profit and loss vs the start
  orders    the paper order book (filled, queued, expired)
  reset     start a fresh paper account (needs --confirm)

    python3 trader-broker/paper.py execute --index 1
    python3 trader-broker/paper.py settle
    python3 trader-broker/paper.py account
    python3 trader-broker/paper.py orders
    python3 trader-broker/paper.py reset --confirm

The book is kept in trader/state/paper.json. Fills slip by paper_slippage_bps
(config, default 5 basis points) against you, so paper results are not rosier
than a real market order would be.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(folder, name):
    path = os.path.join(os.path.dirname(HERE), folder, name + ".py")
    spec = importlib.util.spec_from_file_location("paper_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ticket = _load("trader-broker", "ticket")
alerts = _load("trader-alerts", "alerts")


class PaperError(Exception):
    pass


def utc_now(text=None):
    if text:
        return alerts.parse_now(text)
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _money(v):
    return ticket._money(round(float(v), 2))


# ---------------------------------------------------------------- pricing

def quote(sym, a):
    """Current price for sym: --price override, else a live (delayed) quote."""
    if getattr(a, "price", None):
        return float(a.price), "given"
    quotes, errors = alerts.get_quotes([sym], offline=getattr(a, "offline", None), pause=0)
    if sym not in quotes:
        raise PaperError("no quote for %s: %s" % (sym, "; ".join(errors) or "unknown"))
    q = quotes[sym]
    return float(q["price"]), "%s %s" % (q.get("source", "yahoo"), q.get("time", ""))


def fill_terms(order, price, slippage_bps):
    """(fills, qty, fill_price, why). Market orders always fill; a limit only if marketable."""
    side = str(order.get("side")).lower()
    slip = float(slippage_bps) / 10000.0
    px = price * (1 + slip) if side == "buy" else price * (1 - slip)
    typ = str(order.get("type", "market"))
    if typ == "limit":
        lim = float(order["limit_price"])
        if (side == "buy" and px > lim) or (side == "sell" and px < lim):
            return False, None, None, "limit %s not reached (price %s)" % (_money(lim), _money(px))
        px = min(px, lim) if side == "buy" else max(px, lim)
    elif typ != "market":
        raise PaperError("the paper broker fills market and limit orders only, not %s" % typ)
    if order.get("notional") is not None:
        qty = round(float(order["notional"]) / px, 6)
    else:
        qty = float(order["qty"])
    return True, qty, round(px, 4), "filled"


# ---------------------------------------------------------------- the book

def load_book(path):
    return ticket.read_json(path, "paper book", required=False) or {"orders": []}


def next_id(book):
    return "PAPER-%04d" % (len(book.get("orders", [])) + 1)


def do_fill(a, cfg, entry, order, now):
    price, src = quote(entry["symbol"], a)
    ok, qty, px, why = fill_terms(order, price, cfg.get("paper_slippage_bps", 5))
    if not ok:
        return False, why
    day = alerts.to_eastern(now).date().isoformat()
    ticket.apply_fill(a.portfolio, cfg, entry["symbol"], entry["side"], qty, px, day)
    entry.update({"status": "filled", "filled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "filled_qty": qty, "filled_price": px, "quote": price, "quote_source": src,
                  "value": round(qty * px, 2)})
    return True, "%s %s %s at %s (%s)" % ("Bought" if entry["side"] == "buy" else "Sold", qty,
                                         entry["symbol"], _money(px), _money(qty * px))


# ---------------------------------------------------------------- commands

def cmd_execute(a):
    cfg = ticket.read_json(a.config, "config", required=False) or {}
    if not cfg.get("paper", True) or str(cfg.get("broker", "")).lower() not in ("paper", "local"):
        raise PaperError('trader/config.json must say "broker": "paper" and "paper": true to use the paper broker')
    doc = ticket.read_json(a.orders, "orders file")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    order = ticket._pick(orders, a.index)
    now = utc_now(a.now)
    today = a.today or alerts.to_eastern(now).date().isoformat()
    fp = ticket.fingerprint(order)
    state = ticket.load_state(a.state)
    rec = ticket.latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise PaperError("order %d has no yes recorded today for this exact order. Show the ticket "
                         "(ticket.py show --index %d), ask the user, and record it with ticket.py approve"
                         % (a.index, a.index))
    book = load_book(a.book)
    entry = {"id": next_id(book), "date": today, "index": a.index, "fingerprint": fp,
             "symbol": rec["symbol"], "side": rec["side"], "plain": rec["plain"],
             "order": ticket.tool_input(order), "placed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "status": "queued"}
    mkt = alerts.market_status(now)
    if mkt["open"] or a.anytime:
        ok, msg = do_fill(a, cfg, entry, order, now)
        if not ok:
            entry["tried_on"] = today
            msg = "open order, " + msg + "; settle tries again"
    else:
        msg = "NYSE closed (%s). Queued until the open (%s New York); run paper.py settle then." % (
            mkt["reason"], mkt.get("next_open", "?"))
    book["orders"].append(entry)
    ticket.write_json(a.book, book)
    state["orders"].append({"at": ticket.now_iso(), "date": today, "index": a.index, "fingerprint": fp,
                            "symbol": rec["symbol"], "side": rec["side"], "plain": rec["plain"],
                            "status": "placed", "broker_order_id": entry["id"], "note": "paper broker"})
    ticket.write_json(a.state, state)
    print("PAPER %s: %s" % (entry["id"], rec["plain"]))
    print("  " + msg)
    return 0


def cmd_settle(a):
    cfg = ticket.read_json(a.config, "config", required=False) or {}
    book = load_book(a.book)
    now = utc_now(a.now)
    mkt = alerts.market_status(now)
    today = alerts.to_eastern(now).date().isoformat()
    pending = [e for e in book["orders"] if e["status"] == "queued"]
    if not pending:
        print("No queued paper orders.")
        return 1
    if not mkt["open"] and not a.anytime:
        print("NYSE closed (%s). %d order(s) wait for %s New York." % (mkt["reason"], len(pending),
                                                                        mkt.get("next_open", "?")))
        return 1
    for e in pending:
        tif = e["order"].get("time_in_force", "day")
        # a day order placed after the close is good for the next session, then expires
        if tif == "day" and e.get("tried_on") and e["tried_on"] != today:
            e["status"] = "expired"
            print("%s expired (day order, not filled): %s" % (e["id"], e["plain"]))
            continue
        ok, msg = do_fill(a, cfg, e, e["order"], now)
        e["tried_on"] = today
        print("%s %s" % (e["id"], msg))
    ticket.write_json(a.book, book)
    return 0


def cmd_account(a):
    cfg = ticket.read_json(a.config, "config", required=False) or {}
    pf = ticket.read_json(a.portfolio, "portfolio", required=False)
    if pf is None:
        raise PaperError("no %s yet: run paper.py reset --confirm to open a paper account" % a.portfolio)
    start = float(pf.get("start_cash", cfg.get("budget", pf.get("cash", 0))))
    syms = [p["ticker"] for p in pf.get("positions", [])]
    prices = {}
    if syms and not a.no_quotes:
        quotes, errors = alerts.get_quotes(syms, offline=a.offline)
        prices = {s: q["price"] for s, q in quotes.items()}
        for e in errors:
            print("note: " + e)
    rows, value = [], 0.0
    for p in pf.get("positions", []):
        cost = sum(float(l["cost"]) for l in p.get("lots", []))
        px = prices.get(p["ticker"])
        mv = float(p["shares"]) * px if px else cost
        value += mv
        rows.append((p["ticker"], float(p["shares"]), cost, px, mv))
    cash = float(pf.get("cash", 0))
    equity = cash + value
    print("PAPER ACCOUNT (no real money)")
    print("%-6s %12s %12s %10s %12s %10s" % ("ticker", "shares", "cost", "price", "value", "P/L"))
    for t, sh, cost, px, mv in rows:
        pl = mv - cost
        print("%-6s %12.4f %12s %10s %12s %+9.2f%%" % (t, sh, _money(cost), _money(px) if px else "?",
                                                      _money(mv), (pl / cost * 100) if cost else 0))
    print("cash   %s" % _money(cash))
    print("equity %s   start %s   P/L %s (%+.2f%%)" % (_money(equity), _money(start), _money(equity - start),
                                                      (equity / start - 1) * 100 if start else 0))
    return 0


def cmd_orders(a):
    book = load_book(a.book)
    if not book["orders"]:
        print("No paper orders yet.")
        return 1
    for e in book["orders"]:
        extra = ""
        if e["status"] == "filled":
            extra = "  %s @ %s" % (e["filled_qty"], _money(e["filled_price"]))
        print("%s  %s  %-8s %s%s" % (e["id"], e["date"], e["status"], e["plain"], extra))
    return 0


def cmd_reset(a):
    if not a.confirm:
        print("This wipes the paper account (%s and %s). Run again with --confirm." % (a.portfolio, a.book))
        return 1
    cfg = ticket.read_json(a.config, "config", required=False) or {}
    cash = float(a.cash or cfg.get("budget") or 0)
    if cash <= 0:
        raise PaperError("give --cash or set a budget in %s" % a.config)
    ticket.write_json(a.portfolio, {"cash": cash, "start_cash": cash, "positions": []})
    ticket.write_json(a.book, {"orders": []})
    print("Fresh paper account with %s cash." % _money(cash))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local paper broker for trader-broker. No real money.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--book", default="trader/state/paper.json")
        p.add_argument("--offline", help="folder with yahoo-SYM.json quote files (tests)")
        p.add_argument("--now", help="UTC time, YYYY-MM-DDTHH:MM (tests)")

    p = sub.add_parser("execute", help="fill or queue ONE order the user approved")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--orders", default="trader/orders.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--today")
    p.add_argument("--price", help="fill at this price instead of a live quote")
    p.add_argument("--anytime", action="store_true", help="fill even while the NYSE is closed")
    p = sub.add_parser("settle", help="fill queued orders once the market is open")
    common(p)
    p.add_argument("--price")
    p.add_argument("--anytime", action="store_true")
    p = sub.add_parser("account", help="cash, positions and P/L")
    common(p)
    p.add_argument("--no-quotes", action="store_true")
    p = sub.add_parser("orders", help="the paper order book")
    common(p)
    p = sub.add_parser("reset", help="open a fresh paper account")
    common(p)
    p.add_argument("--cash", type=float)
    p.add_argument("--confirm", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"execute": cmd_execute, "settle": cmd_settle, "account": cmd_account,
                "orders": cmd_orders, "reset": cmd_reset}[a.cmd](a)
    except (PaperError, ticket.TicketError, alerts.AlertError) as e:
        print("paper.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
