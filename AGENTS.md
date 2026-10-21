# Development instructions

## Current scope

The active system follows only authenticated original AlphaAgent messages in the
Dennis source channel, and executes options in **Alpaca Paper**. `alpha_discord/`
contains this system. The original YHS strategy is retained as legacy source;
do not re-enable it or treat `main.py --replay` as validation of the current system.
`stock_quant/common.py` and `notifications.py` are shared infrastructure; their
presence does not authorize new stock strategies or entries.

The separate `ibkr_bridge/` sidecar may observe a user-selected live IBKR
account through local TWS/IB Gateway. It is strictly read-only: exact account
binding, positions, contract matching, data quotes and informational previews.
Keep TWS Read-Only API enabled, client ID nonzero, and the endpoint on literal
loopback. User login and 2FA stay in the official local client. Never implement
an order-submission, modification, cancellation, exercise, what-if order or
execution-enabling switch. Do not infer ownership or adopt existing positions.

`ibkr_paper/` is a separate authorized simulated-trading executor. Only it may
submit/cancel simulated orders through literal loopback, Paper defaults
4002/7497, nonzero client ID, one authenticated DU account and exact SHA256
binding. DU suffixes may contain uppercase letters and digits. A port alone
does not prove Paper mode. Require private Paper-login/execution flags plus
explicit `--execute-paper`; preflight and observation must have no write path.
Never add live ports, live-account writes, arbitrary account selection or
execution support to `ibkr_bridge/`. Keep its Read-Only API workflow intact.
For Paper only, the user must disable Gateway's Read-Only API after verifying
the simulated login. Preserve all intent, execution, ownership and unknown
submission evidence. The SDK's completedOrder omits API orderId/clientId and
has explicit filledQuantity in the Order object: never infer zero fills or
restore identities from prefix/default values. Use exact permId associations
from confirmed executions/orders or the bound durable journal.

## Preserve the authorized policy

- Alpaca execution is Paper endpoint only. The IBKR sidecar permits live-account
  observations only; no live-order calls anywhere.
- OPEN: source creation age at most 30 seconds; maximum buy price is Entry +3%,
  rounded down to supported cents. Use the existing limit-order rules.
- Target entry debit $4,000, allowed $3,000–$5,000 with a flat $2 reserve per
  intent. Choose whole contracts; skip when the range/caps cannot be satisfied.
- Options in-use plus reserved debit cap $10,000. Preserve shared-account
  coordination (stocks $40,000, account $50,000) and one executor per account.
- Current entry cancellation threshold is 30 seconds; cancellation is not proof
  that no fill occurred. CLOSE uses its separate 120-second freshness policy and
  only confirmed, uniquely owned exact OCC+tier quantity.
- Never infer fills from source messages, price touches, notification delivery,
  accepted status or an HTTP timeout. Unknown submissions retain reservations;
  query the original client order ID, never repeat POST automatically.
- Preserve original CREATE IDs/timestamps, tombstones and edit/delete
  invalidation. Verified transport-only Embed changes must not invalidate an
  unchanged trade, while actual changes must remain blocked.
- Do not reset ledgers, re-arm historical messages or backfill missed entries.

## Cloud work boundaries

Read `DEVELOPMENT.md` and `CLOUD_TASK.txt` before editing. Work in a small branch
and leave a draft PR with the change and its limitations. Do not deploy to the
VPS, restart services, open SSH sessions, change SSH identities/configuration,
read credentials, connect to Discord/Alpaca/YHS or send notifications during
ordinary cloud development. Those are separate operational tasks.

Do not add or run tests unless the user explicitly asks for them. Default work
may use static source review, AST parsing, JSON parsing and `git diff --check`.
Do not import/execute application modules as a substitute for a test. Existing
test files and old validation reports are historical material, not evidence for
new changes. State exactly which checks were performed.

Never commit private configurations, account-binding hashes, credentials,
webhooks, session tokens, SSH files, raw Discord messages, trade records or
runtime snapshots. Public target IDs are examples and private account allowlists
are absent. Never remove binding checks to make examples executable. No workflow
in this repository should automatically deploy changes or execute trades.
