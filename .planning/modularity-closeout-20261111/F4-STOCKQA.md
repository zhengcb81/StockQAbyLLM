# StockQA F4 已付结算回告

## 本仓范围与根因

基线为已有发布线 `codex/qa-content-filter-resolution-01@ee92bc9c640dc8afc3013c5be2f589b8199b5f6f`。F2 的 `settled-answer-replayed` 能识别已收到并结案的题目却被公开投影为可重发。决定从原生终态、已收响应和原尝试分别取证。StockQA 修复的是旧代际的派生投影，消费端可以查回原结算终态。原响应、收费和有效的后续代际继续由现有工作账本管理。

门禁复核结果：同一已收到且已结案的尝试不能再次当新请求发送，现有授权仍负责合法后续动作。将某个默认工作目录强行当作所有历史账本会误报缺资料，本次由产生观察的实际 owner 登记本次使用的账本。仓根 `quick_scan_work.sqlite` 当前是老 schema 5 的两条旧记录。生产路由沿本次公开入口使用的账本，不将该路径写死。另有费用账单未落盘或无法找到原 ledger 时，原账状态需查证。

## 启用

`cwd` 是发布后的 StockQA 代码根。ROOT 的同一 owner 环境为本次真实 `src.utils.quick_scan_refresh_inspect_cli` 设置 `RESEARCH_FEEDBACK_OUT`，并在实际启动者配置 `business_env.STOCKQA_FEEDBACK_STATE` 为同一个绝对反馈 SQLite 路径。由 IQS 或 Dashboard 继承环境启动此 inspect 时一并传入。生产 `stockqa` 宿主命令为以下固定参数。

```text
python -B -X utf8 -m src.utils.quick_scan_feedback_owner --state <owner-feedback.sqlite> triage
python -B -X utf8 -m src.utils.quick_scan_feedback_owner --state <owner-feedback.sqlite> repair
python -B -X utf8 -m src.utils.quick_scan_feedback_owner --state <owner-feedback.sqlite> replay
python -B -X utf8 -m src.utils.quick_scan_feedback_owner --state <owner-feedback.sqlite> report
python -B -X utf8 -m src.utils.quick_scan_feedback_owner --state <owner-feedback.sqlite> reconcile
python -B -X utf8 -m src.utils.quick_scan_feedback_owner --state <owner-feedback.sqlite> get-action <original-action-id>
```

- 四种执行动作接受 `feedback-action/1` 的 `submit`。它们配置同一 `lookup_argv`，末项是 `get-action`，由 ROOT 追加原 action ID。读回返回原请求与原结果。
- `reconcile` 接受 `lookup`。已知费用有原票据来源和权威已结算事件，费用未知继续返回原证据及 `cost_unknown`。修复结果不替代原费用结算。
- 现有 `qa:<work_item_id>/g<generation>` 保留，新增 `details_ref=qa-attempt/1/<attempt_id>` 绑定原生尝试。外层 run/attempt 标识继续用于调度，原生 ID 独立保留。原实际账本位置只存在 StockQA 的本地反馈库目录映射。已有证据文件准备好时，可显式 `--ledger` 复放既有历史观察，旧行的原生 `attempt_id` 仍有效。
- 原生原代际确认结案、原尝试绑定和响应仍可读时，`repair` 发布 `stockqa.feedback_settlement/1` 的工作项 ID、代际、原尝试、稳定终态、原响应 SHA 和原因。重复原 action 用同一收据。`replay` 重新读取原账本验证同一发布版本，`report` 按已有复验和原发生绑定并持久登记。未知/异代/异尝试交本仓问题回路处理。

## 复验记录

```text
python -B -X utf8 tests/unit/test_quick_scan_feedback_owner.py --fixture-root <isolated>
python -B -X utf8 -m pytest -q tests/unit/test_quick_scan_feedback_owner.py tests/unit/test_quick_scan_feedback_observation.py tests/unit/test_quick_scan_refresh_inspect.py
```

- 用 F2 原生账本夹具创建一次已收到后结案的回答。真实公开 inspect 读取正常终态，旧投影重建只改变消费者当时看到的状态以复现既有 F2 失败模式。实现前 RED 为外层 attempt 覆盖后缺原生绑定。新测试覆盖动态账本、显式历史回读、费用未知、带原费用来源的测试账单及误配同文件保护。
- 组合用实际 company-wiki LocalHost 以正式 `--state` 参数启动。1 个偏差 verified，分诊、补发、复验、回告各一条。修复和回告在本仓数据库提交后强行退出而无 stdout，宿主两次均按原动作读回。再次处理状态相同。业务账本逐字节一致，新增 API、尝试、代际和费用均为零。组合临时目录已清。
- 加载已发布 IQS 的公开 Python 合同后扩展本仓回归，198 项通过。既有父子保护夹具需使用仓内 `tests/fixtures/delegated_guard/sitecustomize.py`、UTF-8、独占 `IQS_C06_OWN` 及临时 cwd 启动，避免旧默认 logger 写候选仓。按合同重跑单项通过，保留原先全部保护断言，未改旧测试。最终新增 4 项均通过。Ruff、Black、isort、mypy 检查通过。
- 交 ROOT 独审与分仓部署。从既有已验证发布线更新，原 `master` 分歧按原作业计划处理。本次仅提交隔离候选。
