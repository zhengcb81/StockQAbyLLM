# StockQA S6 P1-10

## Goal

Run the complete applicable offline StockQA test inventory on Windows and
Ubuntu. Keep owner composition executable separately and the developer command
short. Freeze the architecture scan against the published base.

## Scope

- Base `master@86f633d8f36e1039323ae492e47f8a8067031d5a`.
- Canonical S6 reference tree `33ca2b5d7c77786416e5d436f589948785fb494b`.
- Candidate branch `codex/s6-p1-10`. Only this repository changes. Runtime
  fixtures use synthetic credentials and loopback transport.
- Broad runtime logger, schema settings and module architecture are subsequent
  S6 phases.

## Status

- Complete. Native baseline collection, platform and owner split, and real
  source ratchet.
- Complete. Matching independent discovery and actual call-phase execution
  receipts, Windows and Ubuntu jobs, and a separate developer smoke directory.
- In progress. Candidate freeze, exact compatibility matrix, and independent
  review. Owner composition evidence is tracked separately.

## Next Step

Run the full offline candidate at its exact Git revision on Windows and Ubuntu.
Check that the execution set matches each inventory, verify the owner fixture
split independently, and record outcomes in the S6 parent ledger.
