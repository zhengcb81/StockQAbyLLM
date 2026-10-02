# Quick-scan rate cards

Quick-scan does not embed provider prices in code. Copy
`examples/quick_scan_rate_cards.template.json` to
`quick_scan_rate_cards.json` beside `llm_apis.json`, then add a reviewed card for
each provider/model/billing currency covered by the active policy's
`cost_policy.pricing_ref`. Validate the file against
`src/config/quick_scan_rate_cards.schema.json` before enabling that policy.

Each `pricing_ref` is an immutable pricing release. When a provider changes its
price, account billing mode, cache treatment, or search charge, add a new card
and use a new reference for new runs. Keep `source_ref` and `source_checked_at`
so reviewers can trace the chosen values. Never put API keys, account IDs, or
request payloads in this file.

The five rates are expressed in the policy currency: four are per one million
tokens (`input_per_million_tokens`, cached input, cache-creation input, and
output); `search_tool_call` is the charge per provider-reported or response-bound
search call. The usage receipt stores only validated integer counters, not raw
provider response bodies. The resolver matches the exact provider, actual model,
pricing reference, and currency. If usage or a matching card is absent or
invalid, the cost remains unknown, the reservation is retained, and dispatch
pauses for reconciliation. No generic token-price estimate is used.

The empty template is intentionally non-operational. Do not fill it with
illustrative rates. For subscription/credit plans, use the plan's actual billing
unit as the policy currency and enter the correct cached/input/output/search
rates; if provider receipts cannot establish the billable amount, leave the
cost unknown and reconcile through the provider's usage records.

JSON decimal literals are parsed directly as `Decimal` and priced with exact
integer-coefficient arithmetic; the ledger rounds upward to the next micro-unit
without using binary floating point or the process-wide Decimal context. The
runtime rejects a rate with more than 100 significant digits or an exponent
outside -100 through +100 so malformed/extreme local values cannot trigger
unbounded arithmetic. Keep account-entered rates within those limits.
