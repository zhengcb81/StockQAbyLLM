# StockQA F4 已付结算回告

正式发布提交 `e96b2fa5e223be93480a42f3cb44a9e7394350e7` 已快进到 `origin/codex/qa-content-filter-resolution-01`。独立审查者从该最终提交重复了单题/题包拒绝零发送、单题回写双错误、stderr/日志脱敏，以及只允许预算 owner 稳定码的离线反例，全部通过。合并后复测的 42 项覆盖新工作线收费前置、旧账只读恢复和 F4 日常入口。总控可在本仓隔离工作树 `.worktrees/f4-diagnostics` 直接运行已验版本。主工作区在 a91a164 上有另一个项目持有的未提交改动，默认工作目录还须由其合流后实际切换。总控 F4 对账单独验收。



通过定点代码与现有资料分离验证。实际 IQS 配置有 68 个执行包，`stockqa_root` 指向主要工作目录，`runtime_dir`、`model_policy_path` 与各包的 `admission_config.work_store` 独立指定。同一已验 StockQA 代码对原 `invest-quick-scan/runs/u25-live/work.sqlite` 走公开只读 inspect，读回 33 个工作项。第二次缺输入返回原 rc2 并产生一条 F4 失败。本次 API=0、未创建 reservation，43,581,440 字节原账 SHA 和同目录账本文件集合前后一致，新增业务库 0。固定三例总控探针均在同一版本产生 deviation，另六项原账正控均通过。主工作目录直接执行与 IQS 当前既有配置仍读取外部在途版本，独立代码根验证完成；实际切换由 IQS owner 通过其公开 setup 或等活跃 StockQA 项目并线。仓内无替换现有解释器和账本的业务 console_script。
## 2026-11-21 普通执行失败与结算反馈

当前基线 f69913c5 保留原作业登记、已付恢复和未发送释放的所属接口。中微 IQS_13/14 只有 prepared 状态和外层类型，发送前的底层原因不能从旧记录恢复。

|阶段|动作与可复验输出|
|---|---|
|复现|采用 IQS 交接的 14 项测试，补 CLI 参数、批量读文件、正常主体拒绝、深层原因、抛错的异常属性、冷进程读取和诊断本身失败的反例。|
|实施|普通运行在原边界记录脱敏原因、位置、运行与题目，CLI、问答引擎和只读 inspect 共享同一记录。分开处理业务失败、结算和反馈投递。|
|验收|在隔离测试根以全新进程从本仓重新读取实际观察和诊断；验证 after_question_failed、未知发送和已付原账、0 新请求；协调独审在最终改动上复验。|
|交接|把当前发布代码、原生读取命令、剩余限制及总控导入结果写进本页；只更新 StockQA 的发布线。|


## 2026-11-21 本仓实现、取证与发布交接

|实际运行面|原行为与根因|本仓实现与验收|
|---|---|---|
|普通命令、输入、只读工具|参数退出、读不到批量文件、缺主体或请求坏掉后，正常 stderr/返回码与 F4 分离。|两个程序入口、basic/LLM runner、十个所属 CLI 记录业务阶段、实际异常、参数形状和版本。保留公开返回码和 JSON 错误封装。|
|单题和组包|只保留最外层类型。返回拒绝没有异常，单题失败回写再次抛错会覆盖原原因。|在发生层分别记录返回的拒绝、原始异常、回写错误。保留标准 cause、context 和 from None 的结构，原生命周期回调接收原始异常。已知原终态跳过归作 expected。|
|普通冷读|读本地 JSON 的共享钩子意外载入整套付费 HTTP 客户端，未安装 httpx 时连诊断也打不开。|把同一 JSON 字段处理函数移入无依赖接口，留原兼容名；只有查询结算时才加载只读账本。用不加载 site-packages 的全新 Python 验证。|
|已有收费事实|inspect 要显式设置 OUT 才送观察。未发送、已确认的失败、待确认与读取错误含义各异。|默认生成偏差、未知和已结算失败或未确认发送的原生观察，关联原工作账本。新进程通过同一公开 owner 查询本次失败与原费用。真实金额和状态继续只在原账有证据时确认。|
|反馈故障和文本泄漏|临时挂载点或日志失效可能覆盖原结果。旧普通异常日志和 stderr 可能包含原文。|先持久保存结构化原始诊断，最后原子写 ready。显式投影失败保留原生记录，指定 outbox 失效回退本仓位置。公共错误记录使用类型、代码位置、稳定码、输入引用。|

生产发布线为 `codex/qa-content-filter-resolution-01`。部署在当前正式工作目录时，默认观察目录为 `C:/Users/郑曾波/Projects/StockQAbyLLM/logs/.feedback/observations`。独立测试以 `RESEARCH_FEEDBACK_DIR` 指定临时目录；`RESEARCH_FEEDBACK_OUT` 继续显式投影。每次原子发布 `feedback-<uuid>.jsonl` 后写 `.ready`。普通失败的 `.cause.json` 和原账绑定在所属模块，公开动作 `python -m src.utils.quick_scan_feedback_owner --state <feedback.sqlite> triage|reconcile` 从 stdin 接收既有 F4 action。总控摄取只读公开观察，ACK 后仍能冷读原证据。

已实际复验原失败、归属、费用与重试行为。合成门禁正常返回的两个独审反例和单题原错误被回写替代、普通 stderr 印出私密标记均先红后绿。原基线的三项公开重放退出 2/1/1，彼时 F4 记录数均为零。修后的本仓 112 项定向回归通过，其中涵盖原已付、未知、无新发送、冷读、真实 CLI、收费客户端缺失。随后新增指定 outbox 失效的反例。完整命令如下。

```text
python -B -X utf8 -m pytest -q -o addopts= -p no:cacheprovider tests/unit/test_execution_failure_diagnostics.py tests/unit/test_execution_feedback_intake.py tests/unit/test_quick_scan_feedback_owner.py tests/unit/test_quick_scan_feedback_observation.py tests/unit/test_qa_question_pack.py
```

25efa17d 经独立复验，单题和组包的真实未派发各产生一项观察，回写异常未覆盖原始失败，普通 CLI 的 stderr 和文件均无合成私密标记。按已有 Q09 兼容测试保留本仓预算规则判定的具体代码，包装的任意异常文字仍不投射。最终费用与输入组合 48 项通过。另一在途工作线已将正式基线更新至 a91a164，集成在本仓隔离目录进行；活跃工作区有并行未提交内容，运行版本分别报告。

中微当时的原作业只留下 prepared 和外层错误名，无法补造发送前根因。上述测试使用全新隔离夹具并禁止网络，旧账按字节比对。总控正式宿主摄取、独审回告及发布分支版本分别登记，未发生的公司重跑或真实收费不得记作验证。启动前的强制杀进程或整个状态卷不可写，需要由宿主保留独立观测。


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
