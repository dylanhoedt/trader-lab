# trader-lab: paper trading with Claude

This repo is the Trader skill pack (the `trader-*` folders, linked into
`.claude/skills/`) plus a live **paper** account in `trader/`. No real money.
Talk to the user in Dutch; the tools print English.

## The account

| file | what |
| --- | --- |
| `trader/config.json` | the rules: budget, cash buffer, caps, `"broker": "paper"`, `"paper": true` |
| `trader/portfolio.json` | cash, positions and tax lots of the paper account |
| `trader/targets.json` | target weights (written by trader-copycat or by hand) |
| `trader/orders.json` | proposed orders (trader-budget, trader-rebalance, trader-tax) |
| `trader/prices.json` | latest delayed quotes (trader-alerts) |
| `trader/state/` | risk verdicts, the user's yes/no log, the paper order book, alert state |

The container is ephemeral: after every session that changes `trader/`,
commit and push it, or the account is lost.

## The order path (always, for every order)

```
size / rebalance / harvest  ->  trader/orders.json
python3 trader-alerts/alerts.py quote --holdings --write
python3 trader-risk/check.py                              PASS or BLOCK
python3 trader-broker/ticket.py show --index N            show ONE ticket, ask yes or no
python3 trader-broker/ticket.py approve --index N --reply "<user's reply, word for word>"
python3 trader-broker/paper.py execute --index N          fills, or queues until the NYSE opens
python3 trader-broker/paper.py settle                     fills queued orders once the market is open
python3 trader-broker/paper.py account                    cash, positions, P/L
```

## Hard rules

- Never execute an order without trader-risk PASS today and the user's own
  explicit yes for that one order, recorded with `ticket.py approve`.
  One yes, one order. Never "yes to all", never turn "ok" into a yes.
- Never change a limit in `trader/config.json` yourself; propose it and let the user decide.
- Never switch `"paper"` to false or `"broker"` to a live broker. The user does that.
- Never place or approve orders from a scheduled or background task. Scheduled
  tasks may only read: news, pelosi, alerts, `paper.py settle` of orders the
  user already approved, and `paper.py account`.
- Never ask for, store or repeat broker API keys.
- This is education and tooling, not financial advice. Say so when you
  propose trades, and give the reasoning and the risk, not certainty.
- Free quotes are delayed; paper fills use them plus `paper_slippage_bps`.

## Tests

```bash
python3 -m unittest discover -s tests
```
