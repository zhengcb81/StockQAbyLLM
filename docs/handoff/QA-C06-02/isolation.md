# QA-C06-02 隔离与清理

## 临时根

| 用途 | 路径 | 归属 | 终态 |
|---|---|---|---|
| golden 导出 | `%TEMP%\qa-c06-02-golden-5k1050lz\` | 本包一次性创建 | **已删除**（`shutil.rmtree` 后复核目录不存在） |
| golden 导出脚本 | `%TEMP%\qa_c06_02_golden_export.py` | 本包一次性创建（不入仓） | **已删除** |
| pytest 用例临时根 | `%TEMP%\pytest-of-郑曾波\pytest-*\` | pytest 自管共享 TEMP | **未按编号清理**——共同规范明令“不按 mtime/pytest 编号/目录名清共享 TEMP”；pytest 自身按代数回收，本包只在用例内使用自己的 `tmp_path` |
| 整改 RED 独占副本（含 `--basetemp <root>/tmp/*`） | `%TEMP%\qa-c0602r-58cdae\` | 本批一次性创建（源码副本 + guard + 原字节 controller 9 例） | **已删除**（清单/SHA/CIM 回执见 `logs/cleanup-remediation-receipt.json`） |
| 整改 GREEN 独占副本 | `%TEMP%\qa-c0602g-19576b\` | 同上（工作树字节副本） | **已删除**（同回执） |
| 本批最后一次全量门的 pytest 共享 TEMP | `%TEMP%\pytest-of-郑曾波\pytest-149\` | 可证归属（971 文件均为门内用例 `tmp_path`，mtime 与 `logs/full-gate-remediation-2026-10-08.log` 完成时刻一致） | **已删除**（同回执；仅删除这一可证明归属的编号目录） |
| 共享 TEMP 无法证归属的残余 | `%TEMP%\pytest-of-郑曾波\pytest-147\`、`pytest-148\` | 早于本批任何执行、仅剩编号可循 | **列出不删**——不按编号/mtime/宽 glob 删除；归属判断交总控 |
| 本批 scratch | `%TEMP%\qa-c0602r-58cdae.path`、`%TEMP%\src.diff`、`%TEMP%\qa_c0602_update_handoff.py`、`%TEMP%\qa_c0602_update_artifacts.py`、`%TEMP%\qa-c0602-cim-check.ps1` | 本批创建 | **已删除** |

用例内所有 DB / cache / logs / 子进程都在 pytest `tmp_path` 之下：
`quick_scan_work.sqlite`、`quick_scan_health.sqlite`、`llm_apis.json`、`manifest.json`、
`identity.json`、`quick_scan_c06_authority.json`、`questions.json`、`spend_authorization.json`、
`result.json`、`logs/`。测试根由 pytest 隔离，未落到仓内。

仓内既有 `quick_scan_work.sqlite`（`.gitignore` 精确例外 `/quick_scan_work.sqlite*`）是开工前
就存在的基线文件，本包**未写入、未删除**：所有用例都用 `monkeypatch.chdir(tmp_path)`，store 路径
是 `llm_config.config_file.parent / "quick_scan_work.sqlite"`，即临时根之下。

## 网络 / 收费 / 下载

| 项 | 实际次数 |
|---|---|
| 外部 HTTP 请求（模型） | **0**（HTTP 边界仅 `unittest.mock` 替换 `src.providers.llm_client.http_client_manager`） |
| 搜索调用 | **0**（`web_search_call` 由 stub 响应伪造，不发起网络） |
| 付费 API 调用 | **0** |
| 下载（财报/网页正文） | **0** |
| 环境 key | 未读取、未打印；用例内 `api_key` 为字面量 `offline-fixture-key` |
| `STOCKQA_RUN_LIVE_E22E` / live 开关 | `STOCKQA_RUN_LIVE_E2E=0`（`scripts/checks.py` 自带 OFFLINE_ENV） |

`python -B scripts/checks.py --full --timeout 300` 的 OFFLINE_ENV 同时注入离线守卫；
本包未申请、未使用任何在线授权（旧 B01 费用授权不复用）。

整改批次 2 的**子进程** E2E：guard（`sitecustomize.py`，由用例写入自己的 `tmp_path/guard/`
并注入子进程 `PYTHONPATH`）审计拒绝一切网络事件（连接/DNS/绑定/发送），只放行 Windows
asyncio 内部 socketpair 的**精确** ephemeral loopback 绑定（`127.0.0.1:0` / `::1:0`）及其
对端连接——放行不记账，其余事件先写 `network-attempts.jsonl` 再拒绝；HTTP 边界 stub 只在
`QA100_STUB_HTTP=1` 时替换 `requests.Session.post`（应答取自用例生成的
`stub-responses.json`，逐次记 `http-stub-sends.jsonl`）。**warm/seal 子进程显式撤掉 stub**
（`QA100_STUB_HTTP=0`）仍 exit 0，发送账本字节不变；四个测试结束时网络账本均不存在。
key 读取由 guard 对 `llm_apis.json` 只读打开记账（`key-opens.jsonl`）：拒绝类用例断言其
不存在（key 读取 0），cold 对照用例断言其存在。子进程 `TEMP/TMP/TMPDIR` 指到
`tmp_path/temp`，`*_API_KEY` 环境变量在 spawn 前剔除，`STOCKQA_RUN_LIVE_E2E=0`。

**监听端口**：0。用例不启动任何服务、线程或 server；只对 `requests` 会话做对象级 stub。
`tasklist` 可见的 python.exe 进程为编辑器语言服务/检查器常驻进程，非本包启动的测试进程；
pytest 进程在每次调用返回时已退出（exit 0）。

## 逐文件清理

| 路径 | 动作 |
|---|---|
| `%TEMP%\qa-c06-02-golden-*` | 删除（本包创建） |
| `%TEMP%\qa_c06_02_golden_export.py` | 删除（本包创建） |
| 仓内 `tests/`、`src/` 下无临时产物 | 无 |
| `%TEMP%\qa-c0602r-58cdae\`、`%TEMP%\qa-c0602g-19576b\`、`%TEMP%\qa-c0602r-58cdae.path` | 删除（本批创建；回执 `logs/cleanup-remediation-receipt.json`：逐文件 SHA 清单 + CIM `python*` 匹配进程 0 + 删除后目录不存在） |
| `%TEMP%\pytest-of-郑曾波\pytest-149\` | 删除（唯一可证归属目录；同回执） |
| `%TEMP%\pytest-of-郑曾波\pytest-147\`、`pytest-148\` | **不删**（归属不可证，见上表） |
| 本批交接目录 `docs/handoff/QA-C06-02/` | **保留**（交接物） |
| 九件 overlay 原字节 | 见下方“EOL 事件” |

## 过程异常（如实记录）

1. **pre-commit `mixed-line-ending` 改写了 6 个 overlay 输入的行尾。**
   `tests/fixtures/quick_scan_c06_manifest_v2_fixture.json`、
   `quick_scan_c06_authority_v2_fixture.json`、
   `src/config/quick_scan_observation.schema.json`、`quick_scan_answer_content.schema.json`、
   `quick_scan_metric_registry.json`、`pyproject.toml` 原为 CRLF，被 hook 就地转为 LF，
   导致 8 个用例立刻 RED（`manifest_file_sha256` 与文件字节不符）。
   **处置**：从 `inputs/qa-phase92-overlay/` 按原字节恢复前 5 个（SHA 分别回到
   `a7e80a5b…` / `e5bc782a…` / `6344d2af…` / `a83236c2…` / `790d0a82…`，与输入锁一致），
   复核 55 个相关用例全绿。`pyproject.toml` 由本包新增 mypy 覆写，内容本就变化，未回滚。
   **提交顺序**：`git add` → 第一次 `git commit`（hook 首轮把 5 个文件转 LF 后 fail）
   → 再次 `git add` → 第二次 `git commit`（hook 通过）→ **提交后按 overlay 原字节恢复工作树**。
   `core.autocrlf=true` 下索引存 LF、工作树恢复 CRLF，`git status` 归一化后为 clean。
   这正对应输入锁“Windows 工作树 SHA 与 Git blob SHA 不同口径；不能用 EOL 转换修复原输入”。
2. 早期一次 `black`（line-length 100，读 `pyproject.toml`）对 4 个文件重排；其中
   `src/utils/quick_scan_observation_context.py`、`tests/unit/test_quick_scan_observation_context.py`
   是九件 overlay 中本就不 black-clean 的两件。静态门要求 black 通过，故保留重排；
   与 overlay 原字节做过 AST 对比（`ast.dump` 等价），语义未变。
3. 未发生测试失败遗留：最终门 `result=pass`，失败用例 0。
4. **整改批次 2 的子进程 guard 首跑拦了 Windows asyncio socketpair。** 首轮子进程 RED 里
   `network-attempts.jsonl` 记到 `socket.bind`——实测为 CPython 在本机的 socketpair 绑定
   `('::1', 0)`（IPv6 回环），而总控修正版 guard 只放行 `('127.0.0.1', 0)`。处置：把放行
   条件改为**两个精确 ephemeral 回环**地址（仍先记账任何其它地址），不放行任意 loopback
   服务或模型 HTTP；修正后 cold/warm/seal 与全部拒绝类用例复跑，网络账本不存在。
   该轮 RED 日志如实保留（`logs/red-subprocess-remediation.log`），其中错 metadata /
   重复 authority 的产品级 RED（CLI exit 0 并实发 31）不依赖该环境修正。
5. **证据日志按仓标准 hook 归一化后提交。** `logs/red-boundary-remediation.log` 等原始 pytest
   输出含行尾空格与混合行尾，`pre-commit` 的 `trailing-whitespace`/`mixed-line-ending --fix=lf`
   就地规范化（**无内容行增删**）；IQS intake 内总控原始日志字节不碰。
6. **冻结 CRLF 输入：工作树 SHA vs Git blob SHA 与可复现还原。** 本批未改任何冻结输入；
   5 个 IQS 冻结文件在 `core.autocrlf=true` 下的双口径如下（工作树 SHA = 输入锁口径；
   blob 为对象库 LF 字节）：

   | 文件 | 工作树 SHA256（CRLF 字节） | Git blob SHA256（HEAD，LF 字节） |
   |---|---|---|
   | `src/config/quick_scan_observation.schema.json` | `6344d2afc9e0d937e05baba8aef27ad15c91770ab7ecb2ddbd549e5ab4fb70ec` | `fceea2a0c68e7260d4bdb3c0ac220dda2c1abccc55211fe43ab607856a7375b8` |
   | `src/config/quick_scan_answer_content.schema.json` | `a83236c2aba50115038cd2d404d5810bd50791c2b3f089f252b77b6a6d322547` | `14800653a0690ff64813025b603068dd3325e99308242860b7ce2cfa8aebbad0` |
   | `src/config/quick_scan_metric_registry.json` | `790d0a8295771c372d8266ab1f0883c93b2bb22f4f81ab482dd8d95054bf0e75` | `a18f51cca30e887835c4e136d5e9903dd1c28a2538a060363f9cab3cb50b7ca0` |
   | `tests/fixtures/quick_scan_c06_authority_v2_fixture.json` | `e5bc782afd245374fff3bc224f04cb9dbcc0fb22b76d62faf2a16917db1ded43` | `d337be135560c0c3b96c1e77a41cf596a0929c095e4532702ecd8fcc689d402b` |
   | `tests/fixtures/quick_scan_c06_manifest_v2_fixture.json` | `a7e80a5bcaf2cb2499fa24f091b129d7e1a2ebbf6e91a5666359c5e91c90270c` | `73853233364ea3cb019d02b13cd774f1cbaa3f6e3c671b891b37ae835271977d` |

   已验证：`blob(LF) 经 LF→CRLF 转换 == 工作树字节`（5/5 成立）。可复现还原命令：
   `git -c core.autocrlf=true checkout -- <path>`（本批实测还原后工作树 SHA 回到左列且
   `git status` 干净）；`git cat-file blob HEAD:<path>` 取到的是右列 LF 原字节，
   **不可**用 `git show HEAD:<path> > <path>` 直接当工作树字节写回（会丢 CRLF）。
   本批未偷偷换行、未重签任何 IQS 冻结输入；`.pre-commit-config.yaml` 等其余 20 个
   均匀 CRLF 文件同样未触碰。

## 未清理/未触碰

`.codegraph/`、`.workbuddy-ai/`、`nul`、`pilot_runs/b2a_2026-10-03/`、
`pilot_runs/g2b_alphabet_2026-10-04/`、`pilot_runs/l02_2026-10-04/`、`progress_update.txt`
——输入锁登记的 7 项旧未跟踪，未读内容、未删除、未提交。
其他仓（invest-quick-scan / StockWiki / company-wiki / Theme / Industry）只读，零写入。
整改批次 2 同样：唯一写根仍是本仓；IQS 只读取 intake 原字节（controller 9 例、
guard 模板、v5 源）复制进独占副本执行，未回写一个字节。
