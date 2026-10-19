# Development instructions

## Current scope

The active system follows only authenticated original AlphaAgent messages in the
Dennis source channel, and executes options in **Alpaca Paper**. `alpha_discord/`
contains this system. The original YHS strategy is retained as legacy source;
do not re-enable it or treat `main.py --replay` as validation of the current system.
`stock_quant/common.py` and `notifications.py` are shared infrastructure; their
presence does not authorize new stock strategies or entries.

## Preserve the authorized policy

- Paper endpoint only; no live-account support or live-order calls.
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
