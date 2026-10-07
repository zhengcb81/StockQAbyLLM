# QA-NET-01 交付摘要

## 1. 问题与最终行为

包指令要求补上 Q10/Q13/Q02 三处**剩余产品行为**，同时不得重做既有底座：

1. **检查点落定后 runner 调用 C06 adapter 并封存/记录阻断（Q10 glue）**
   现状反例：生产激活路径（`--identity-snapshot`）下 `QuickScanWorkLifecycle` 未获得回答模型名，
   `attempt.model_requested != receipt.actual_model` 永远成立 → **检查点永远不落**，因此封存链路在 HEAD 上不可达。
   实现：把首个合格 route 的 model 注入 lifecycle；`after_question` 保存检查点成功后同步调用
   `seal_result_delivery`（权威缺失 → `blocked/c06_authority_unavailable`，权威/字段不全 → `blocked/c06_adapter_missing_fields`）；
   新增 `--seal-deliveries` 重启入口，`model_calls` 恒为 0。

2. **消费冻结模块问卷，只派新增/过期题（Q13）**
   实现：`--question-manifest` 载入 IQS 已发布 manifest，做**结构 + 双向 prompt hash 绑定**校验（重复 ID、截断、改写、
   跨模块替换冲突一律在任何 HTTP 之前拒绝），随后产出可审计的逐题增量计划回执
   （`dispatch` / `expired_dispatch` 是唯一可能消耗模型调用的动作），并把逐题 scope/generation 绑定交给既有 work store
   （新增 1 个只读查询 `find_work_items`，不改状态机）。

3. **外部检索证据路径与已验证原生协议（Q02 增量）**
   实现：统一分层判定边界（传输/搜索执行/证据/最终答案）、四个外部路由的离线 parser（≤3 层 JSON 解包、
   字节上限、业务错误分类）、证据规整（URL 去重、实体与截止日筛选、500/30,000 字符上限、按题选 context）、
   版本化搜索策略导入与**逐路由准入闸门**（凭据/计价/费用上界/存储权缺一即未准入、发送 0）、
   DeepSeek Anthropic 续写协议分类（`tool_use` 不冒充最终答案）并保持该 route 未启用。

## 2. 任务范围

| 项 | 内容 |
|---|---|
| task_ids | Q02、Q10、Q13（与 manifest 一致） |
| 唯一写入仓 | `C:/Users/郑曾波/Projects/StockQAbyLLM` |
| 未触碰 | StockWiki / IQS 中央 PWF / 生产配置 / 凭据 / B01 冻结输入与历史结果 / StockWiki 私有表 / 安装目录 |
| 既有 7 个未跟踪项 | 原样保留（`.codegraph/`、`.workbuddy-ai/`、`nul`、三个 `pilot_runs/*`、`progress_update.txt`） |

## 3. 入口命令（最小复现）

```powershell
# 仓统一门（G2-SQA-CHECKS 新增，唯一 Python 定义点）
python -B -X utf8 scripts/checks.py --full      # 离线全量：black/isort/mypy/bandit/pytest/smoke
python -B -X utf8 scripts/checks.py --static-only

# 定向：本包 27 个新断言
python -B -X utf8 -m pytest -p no:base_url tests/unit/test_qa_net01_c06_seal.py tests/unit/test_qa_net01_question_manifest.py tests/unit/test_qa_net01_search_boundary.py tests/integration/test_qa_net01_cli_e2e.py -q

# 单独静态门
python -B -X utf8 -m mypy src
python -B -X utf8 -m black --check src/ tests/
python -B -X utf8 -m isort --profile black --check-only src/ tests/
python -B -X utf8 -m pylint src/ --fail-under=9.0
python -B -X utf8 -m bandit -r src/ -f screen -ll
```

公开入口用法与冻结 manifest 生成命令见 [interfaces.md](interfaces.md)。

## 4. 版本兼容

* C06 交换包仍为 `schema_version=1.0.0`；**未修改 C06 公共 schema**，未新增顶层字段，
  未把外部检索回执写成原生搜索回执。
* work/outbox 状态机、模型策略、预算与 attempt 语义未改；`find_work_items` 为只读新增。
* 新增 CLI 参数全部可选；不给 `--question-manifest`/`--search-policy` 时行为与 HEAD 一致
  （除本包修复的检查点→封存 glue）。
* 新增两个版本化 schema 文件，加载器**不引入 jsonschema 运行时依赖**（StockQA 依赖清单未变）。

## 5. 验证

仓既有统一入口（G2-SQA-CHECKS 新增）与包内定向命令都已运行；**全部在本包写入完成后的最终代码状态上执行**：

| 批次 | 命令 | 结果 |
|---|---|---|
| 仓全量门（权威） | `python -B -X utf8 scripts/checks.py --full` | **pass**：steps `black,isort,mypy,bandit,pytest,smoke`，`962 passed`（离线 unit+integration，排除 live/benchmarks），101.8s |
| 全量回归（含 live skip / benchmark） | `python -B -X utf8 -m pytest -p no:base_url -q` | **986 passed, 4 skipped**（4 个 skip 全部是需密钥的 live E2E） |
| 本包定向 | 见上 | **27 passed**（`GREEN_batch*.log`） |
| mypy | `mypy src` | 0 error（55 files） |
| black | `black --check <package files>` | 27 files unchanged（black 26.5.1，与 `.pre-commit-config.yaml` rev 同步） |
| isort | `isort --profile black --check-only <package files>` | clean（7.0.0） |
| pylint | `pylint src/ --fail-under=9.0` | **9.34/10**（仅诊断，新门不再设数字通过线） |
| bandit | `bandit -r src/ -f screen -ll` | 0 issues |
| pre-commit | `pre-commit run --files <本包文件>` | black/isort/mypy/detect-secrets/check-json 等全部 Passed（`GREEN_precommit.log`） |
| TDD RED | 4 份 RED 日志 | glue/manifest/CLI 闸门在 HEAD 上确认不存在 |

**基线漂移（已核对，无交集）**：本包开工基线为 `6a9ff13`；实施期间另一 lane（G2-SQA-CHECKS）在**同一仓**提交 5 个 commit（`d989ea7`→`0f8fbfa`，改动 `.github/workflows/*`、`.pre-commit-config.yaml`、`pyproject.toml`、`requirements-lock.txt`、`scripts/checks.py`、`CONTRIBUTING/README`、`.planning/`、`docs/implementation/g2-stockqa-checks/` 及其两个新测试文件），与本包 `changed_paths` **无任何交集**；本包随后的全部验证与提交均在 `0f8fbfa` 之上进行，未改动对方文件。

原始日志在 `logs/`；hash 与字节数见 [artifacts.json](artifacts.json)；逐 case 对照见 [case-map.md](case-map.md)。

## 6. 未解项（partial 的原因）

1. **外部检索 → context → LLM 的生产发送接线未做**：准入闸门、parser、证据规整已离线验收，
   但把 evidence context 注入回答 prompt 的路径留待下一批，避免交付半接线的计费路径。
   当前 `external_context` / `explicit_hybrid` 模式在无已准入路由时直接失败关闭（发送 0）。
2. **DeepSeek Anthropic 续写 route 保持未启用**：分类器已实现，但确切续写协议与可执行费用上界未确认
   （`max_uses` 实测 1→3 次搜索，不构成上界），按包指令保持 `partial`，不编造最终答案或搜索数。
3. **Brave/Tavily/Z.ai 存储权与计价未确认** → 全部未准入；不得据“已买 API”推断 storage rights。
4. **manifest 中 `scope=security` 的题**：本仓身份导出未提供“公司→挂牌”的权威选择规则，
   `--security-scope-id` 由调用方显式给出；缺省时记 `deferred_scope_unbound`（0 发送），不自造 `SEC_` ID。
5. **跨仓真实导入/ACK（StockWiki W05/W10）与 producer→consumer golden** 本包未运行，交总控集中联调。
6. **未做 git commit**：按“仅在用户明确要求时提交”的约束，本地提交待用户确认后补 `result_ref/result_commit`。
7. **独立审查未做** → `review.status=not_run`（不得自填 approved）。

## 7. 回退方式

* 所有新能力都挂在可选参数上：删除 `--question-manifest` / `--search-policy` / `--c06-authority` /
  `--seal-deliveries` 即回到题文件直跑（检查点→封存 glue 仍生效，这是本包的缺陷修复，不建议回退）。
* 需要完全回退本包：`git checkout -- .gitignore main_with_llm.py src/runners/llm_runner.py src/utils/quick_scan_work_store.py`
  并删除下列新文件（清单见 [artifacts.json](artifacts.json) 的 `kind=source` 条目）。
* 运行数据可保留：`quick_scan_work.sqlite` 中本包新增的 `blocked` 投递行可由后续
  `--seal-deliveries` 消化，回退不会丢失已回答问题。
* 未修改任何生产配置、名单或数据库，回退不涉及数据迁移。
