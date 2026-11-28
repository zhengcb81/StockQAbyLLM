# Findings

- Former CI used the Linux developer list. Running Windows collection against
  the actual release produced 3,634 collected, four live deselections and five
  import errors. Two errors were real private StockWiki compositions. The three
  other errors were consumer tests importing a private IQS test solely to build
  synthetic wire data.
- StockQA now reads the v1.2 synthetic authority produced by IQS's public
  `scripts/c06_authority.py` at
  `b4aee7fb05671b162ef7f6d8e270ef3cb2692e52`, using the repository's existing
  byte-locked manifest, a synthetic identity and version metadata. The
  authority's SHA-256 is
  `ce9e4e6813b9e29d2ae15d0c7a2132c37e0439a53a7c89824b68ada76acdbb75`.
  The producer returned zero network calls and no identity attestation.
  Four public schemas were copied byte-for-byte from that same revision for
  schema-validation tests. The separately configured true owner scenario
  remains in `tests/composition`.
- A second hidden consumer skip only needed the shared canonical JSON digest.
  StockQA now computes its own synthetic receipt with its published canonical
  representation, retaining the actual cross-owner producer tests separately.
- The native corrupt checkpoint test first failed in the eager work-item list,
  before the old error handler. The recovery path now enumerates work metadata
  separately and reports unreadable stock checkpoints with their original
  structured cause and work identifier. No unreadable checkpoint becomes a
  re-dispatch candidate.
- Old fixed selectors and scratch locations slowed and obscured coverage.
  Local smoke now discovers a small folder. Exact CI and review use canonical
  roots, tempfile-only state, and a real test-call receipt. The Windows
  engineering subprocess runner uses explicit no-window flags.
