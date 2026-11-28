# Owner composition tests

These tests use another owner's authentic runtime or an independently
preserved original state fixture. The standalone suite executes the StockQA
consumer tests with portable contract input already stored in this repository,
including the IQS public export and wire schemas. Real providers are excluded
from both suites.

Use the owner launch instructions for each test family. Start pytest in an
isolated StockQA checkout with a short temporary state path. For the synthetic
C06 process, the original StockWiki runtime, IQS code, and explicitly owned
temporary workspace must be configured as described in
`.planning/modularity-closeout-20261111/S5-STOCKQA-lifecycle-output.md`. Keep
the documented owner runtime and artifact revisions. The source files in this
directory retain their prior input contracts and assertions.

```text
python -m pytest tests/composition/<test_module>.py
python -m pytest -m owner_composition tests/unit tests/integration
```

The first command selects an individually provisioned family. The second runs
the four cross-owner cases that share their home module with standalone
StockQA tests. Some archived-native modules need a separately authorized copy of
a frozen earlier operation. Such a case cannot be given execution credit
without its original applicable fixture and a genuine run. Required Windows
tests have explicit platform markers.
