# Low latency execution controls

These changes affect explicitly opted-in future Dennis direct-source Paper
orders. Defaults preserve the original behavior. No ledger schema, source
binding, activation time or existing immutable order payload is migrated.

## Opt-in configuration

```json
{
  "entry_limit_method": "reference_cap",
  "snapshot_reuse_enabled": true,
  "source_wakeup_enabled": true
}
```

Only `direct_personal_account` with `budget_range_contracts` permits these
opt-ins. The public example remains deliberately non-executable: execution is
false, private bindings/arming/allowlist are missing, and the legacy executor
authorization is absent.

## Initial buy limit

The original `legacy_ask` method sets a buy limit to the observed rounded ask.
`reference_cap` first verifies ask is at or below the authorized reference-price
cap, then uses that cap as the initial limit. The cap remains Entry +3%, rounded
down to cents. Whole-contract quantity and all cash/risk reservations are
calculated at the final limit, including one flat $2 reserve per intent.
Target debit remains $4,000, allowed range $3,000–$5,000; options in use plus
reservations remain capped at $10,000. An infeasible quantity is skipped.

Each new intent records its price method. Old intents without that field retain
`legacy_ask` checks. Existing orders are not replaced or repriced, historical
signals are not replayed, and an ambiguous POST is never resubmitted.
Telemetry distinguishes observed-ask premium from submitted-limit premium.

## Duplicate reconciliation

Opted-in execution holds the same shared account OS lock across one processing
step. A complete broker snapshot can be reused within that step for at most
2 seconds after snapshot completion, provided the complete order/lot risk-state
digest and forced-refresh set remain unchanged. Submit and cancellation attempts
invalidate it; errors, stop and step completion clear it. Expired or changed
evidence requires fresh reconciliation. No snapshot crosses steps.
The final broker account GET and local before-send checks remain in place.

## Source wake-up

While idle and broker-healthy, the consumer checks the existing read-only source
SQLite connection's `PRAGMA data_version` every 0.2 seconds. A change only wakes
normal processing; it does not authenticate a source or authorize an order.
The baseline is captured before processing to avoid losing changes during
broker work. Startup, broker failures and probe failure retain the normal
periodic schedule. The configured 5-second periodic reconciliation remains.
Database commits may cause an extra reconciliation. Broker request limits and
Retry-After handling are unchanged; this is not 0.2-second broker polling.

## Observability and verification limits

`execution_latency_controls` in copier status reports enabled controls, full
snapshot attempts, reuse counts, wake-up counts and probe health. Counters reset
on process restart. Warm-up may initially show a null probe; idle operation may
show zero reuses. These are not failures by themselves.

Source creation/receipt, submission, ACK and confirmed fill timestamps remain
separate. Neither a lower processing delay nor an accepted order proves a fill.
The indicative feed is still an estimate; these changes do not switch feeds or
purchase a market-data subscription. Existing source freshness, quote checks,
source edit/delete invalidation and exact-owned CLOSE controls remain active.

This revision received static source review and AST/JSON parsing only. No tests
were added or run. Actual latency improvement needs subsequent fresh signals;
do not claim a measured speedup from the implementation alone. Deployment uses
a separate private operational workflow; no automatic GitHub deployment exists.
