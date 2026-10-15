# Thinking research transport

`src.providers.llm_research_client.ResearchChatSession` provides one sequential,
memory-only OpenAI-chat conversation using the existing `LLMClient` configuration.
It is a transport primitive, not a company-agent runner or an identity authority.

The initial allowlist is `deepseek-flash` on `api.deepseek.com` and
`mimo-v2.6-pro` / `mimo-v2.6-flash` on ordinary `api.xiaomimimo.com` chat endpoints.
The ordinary MiMo API credential is required; the Token Plan host is not allowed.
MiniMax and other protocols require separate qualification and are rejected.

The caller must bind the existing durable StockQA budget and route before each
`step`. An absent budget fails before admission or HTTP. Existing owner generation,
subject dispatch and pack guards remain in force; this API grants no company send
approval and does not bypass an unsupported owner protocol.

Create a session with `system_prefix`, function `tools` and explicit resource
limits. The first `step(user_context=...)` sends the user request. A response with
`tool_calls` contains only the validated call IDs, function names and arguments.
Execute permitted tools through the existing search/runtime ports and pass exactly
those IDs to `step(tool_results={call_id: result_text})`. It never executes tools,
shell commands or a search itself. Tool schemas cannot use remote references.

Thinking is enabled for every request. Provider-produced reasoning is returned to
that provider only in private memory, as required by the multi-round protocol.
Do not inspect private attributes, serialize sessions or enable HTTP body tracing.
Public results contain final content, validated function calls, safe receipt hashes,
usage observations and latency. `close()` clears private message history and the
credential reference. Pickle is deliberately unsupported.

HTTP uses the shared metered pool, with no transport retries or redirects. Every
request passes through `begin_quick_scan_send` and its original accounting API.
Incorrect model identity, malformed tools, duplicate JSON arguments or truncated
output stops that session after retaining the physical response and its fee.
Timeout or uncertain outcome must be inspected through the original attempt;
recreating a session is not authorization to resend the same unresolved work.

Missing or inconsistent cache/reasoning usage remains unknown. It does not become
zero, settle a fee or open the next step. Published price-card estimates and actual
cash billing are separate observations. Output usage already includes reasoning;
do not add reasoning tokens a second time. Price callbacks receive the original
sanitized durable receipt allowlist, which includes `completed_at` but does not
include `started_at`; use a verified bounded tariff window or retain the reservation.

S0 on 2026-10-15 qualified real DeepSeek and MiMo Pro thinking/tool round trips and
observed prefix caching. Flash has protocol support here but was not re-probed in
that run. This does not qualify financial answer accuracy, production owner
integration, the full Lab RuntimePort adapter, checkpoint resume or invoice cost.


Native controlled-generation compatibility (2026-10-15): DeepSeek chat uses
`thinking.type=enabled` and `max_tokens`, distinct from its Responses API dialect.
The owner comparison guard now validates these exact fields for
`deepseek_chat_completions` with provider `deepseek`. Existing provider/model,
cohort, approved target route, policy, pack-size and owner-scope checks still
run before any send. This does not supply owner permission or a production
RuntimePort; S0 and worker frozen inputs are unchanged. Synthetic native SQLite
regressions cover single work and pack caps3/12 without creating HTTP attempts.
