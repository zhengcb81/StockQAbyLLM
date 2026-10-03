# L01 pilot report — 10-company real search pilot (2026-10-02/03)

Freeze: `invest-quick-scan/docs/implementation/reviews/L01-pilot-freeze-2026-10-02.json`
Task: L01 (执行10家公司真实搜索探针与小样本) · Cases: LIVE-01, LIVE-02, SC-01, LLM-01, LLM-02, BUD-04
Production path: `main_with_llm.py --require-search` (public CLI), provider minimax / MiniMax-M3, sequential, bounded.

## Headline results

| Metric | Value |
|---|---|
| Companies (3 markets: CN ×5 incl. STAR/BJ/GEM boards, US ×3, HK ×2) | 10 |
| CLI invocations (primary requests) | **11 / 20 cap** (HTTP attempts 11 ≤ 40 ceiling) |
| Answers | 20 (IQS_05 + IQS_06 per company) |
| **Executed-search receipts** | **20 / 20** (`search_status=executed`, HTTP 200, `MiniMax-M3`) |
| Real search calls / source URLs | **41 / 388** |
| Scored answers | **18** (scores 4–9: `[4,5,6,7,7,7,8,8,8,8,8,9,9,9,9,9,9,9]`) |
| Fail-closed unknowns | **2** (茅台 phaseA IQS_05, 阿里巴巴 IQS_06) — model emitted balanced-but-invalid JSON; strict parser refused; `format_repair_budget=0` by Q04 dispatch-cap design |
| SC-01 layer checks | **20/20** status↔score coherence; **20/20** `receipt.answer_sha256` matches the canonical answer envelope bytes |
| Mocks used | 0 (every request real; no offline substitution) |

## Per-company detail

| # | Company | entity_id (probe) | IQS_05 | IQS_06 | search receipts | sources (05/06) |
|---|---|---|---|---|---|---|
| 1 | 茅台 | probe:CN-A:600519 | unknown* | 9 | executed/executed | — / 20 |
| 2 | 安集科技 | probe:CN-A:688019 | 7 | 8 | executed/executed | 20 / 20 |
| 3 | 曙光数创 | probe:CN-A:872808 | 8 | 9 | executed/executed | 16 / 16 |
| 4 | 万科 | probe:CN-A:000002 | 6 | 4 | executed/executed | 17 / 16 |
| 5 | 宁德时代 | probe:CN-A:300750 | 9 | 9 | executed/executed | 20 / 20 |
| 6 | Microsoft Corporation | probe:US:NASDAQ:MSFT | 9 | 9 | executed/executed | 19 / 30 |
| 7 | MongoDB | probe:US:NASDAQ:MDB | 7 | 5 | executed/executed | 20 / 19 |
| 8 | Snowflake | probe:US:NYSE:SNOW | 7 | 8 | executed/executed | 20 / 19 |
| 9 | 腾讯控股 (HK, probe-only) | probe:HK:00700 | 9 | 8 | executed/executed | 20 / 19 |
| 10 | 阿里巴巴集团 (HK, probe-only) | probe:HK:09988 | 8 | unknown† | executed/executed | 19 / 18 |

\* phase A attempt (original freeze questions); recorded as-is, never deleted or re-run to fake success (rollback rule). † model JSON invalid → strict fail-closed.

## Case evidence mapping

- **LIVE-01** ✓ — all 11 invocations through the real public CLI; per-company result JSONs carry execution receipts (provider/model/response_id/search receipt/source URLs/call counts/failure types), per-question sources and the 2 failures; no mock anywhere; `membership`/pool writes: none (pilot scope only).
- **LIVE-02** ✓ (negative path armed, never triggered) — the phase-A gate (abort on zero executed search) checked each result and never fired; no batch ever exceeded its cap; no record was deleted or re-run to manufacture success.
- **SC-01** ✓ — 20/20 status↔score coherence inside the formal envelope; 20/20 `receipt.answer_sha256` byte-bindings to the canonical answer serialization (JSON sort_keys+compact and ASCII-escaped variants both checked); cross-layer equality for the parser→AnswerGenerator path additionally rests on S02's prior verified record (SC-01's owner task) — the pilot adds the CLI/exchange envelope layer evidence.
- **LLM-01** ✓ — 20 executed search receipts with request/response correlation (`response_id`, `attempt_id`, `request_id=null` per vendor), per-question citation source URLs (388 total), receipt metadata separate from model text (envelope vs `description`).
- **LLM-02** ✓ (cited, not re-run) — fake-search-claim refusal covered by Q02's executed offline suite (`test_search_request_without_execution_evidence_is_unverified` and siblings) recorded in `contracts/validation-Q02-MiniMax-live-E2E-2026-10-02.md`.
- **BUD-04** ✓ — `L01-pilot-freeze-2026-10-02.json` pins pricing version (`quick_scan_rate_cards/1.0.0`, **empty cards → no fabricated unit prices**), request caps (20 primary/40 attempts), conservative bounds (token usage per receipt — **not persisted by the vendor response, so vendor console is billing truth**; search per-call fee unpriced locally), and the owner budget statement. Actual usage: 11 primary requests, 41 search calls, 388 sources.

## Failures (recorded, not hidden)

1. `company-01-phaseA-IQS_05` (茅台, original freeze questions): model's post-search answer was balanced-but-invalid JSON → strict extraction failed closed → `status=unknown`, `format_repair.status=not_enabled` (`format_repair_budget` forced to 0 for quick-scan routes by Q04 dispatch-cap design). Local parser replay proves prose+valid-JSON parses (the other18 scored answers prove the pipeline); the defect is model-output validity only.
2. `company-10-IQS_05_06` (阿里巴巴, IQS_06): same class, fail-closed.
3. Observability gap (recorded, not fixed here): the strict path's candidate-loads-failure is silent (no WARNING) — cosmetic logging gap only; behavior is correct fail-closed.

## Honest boundaries

- `probe:<listing_key>` entity-ids are probe-local stable bindings, **not issuer resolutions** (preview reported issuer_unresolved for all 216; HK companies are outside the confirmed 216 pool, probe-only).
- Scores are model outputs under real search evidence — not investment advice, not pool membership, no StockWiki writes, no scan queue, no paid Work created beyond these 11 requests.
- Cost precision: no fabricated prices anywhere; exact billing = owner's MiniMax console (≈11 completions + 41 searches in this pilot, plus the ~9 earlier Q02/phase-A diagnostic single calls).

## Evidence files (this directory)

`company-*.json` (11 result/receipt files), `run-log.json` (per-invocation manifest with exit/seconds/answers/receipt summaries + budget counters), `_score-rows.json` (SC-01 verification rows), `questions_q1.json` / `questions_q2.json` / `questions_both.json` (frozen question configs). `llm_apis.json` stays ignored by repo policy (keyless shape documented here: `default_provider=minimax`, model `MiniMax-M3`, `base_url=https://api.minimaxi.com/v1/responses`, `timeout=300`, `max_retries=1`, `format_repair_budget=0`, `api_key=""` → env fallback). `logs/` (run logs) ignored by design.
