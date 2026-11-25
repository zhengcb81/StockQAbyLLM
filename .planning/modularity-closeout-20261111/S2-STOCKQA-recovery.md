# StockQA S2 已付回复成员解码诊断

## 边界与复现

正式基线为 codex/qa-content-filter-resolution-01@1449d71，包含 e95befd 的独立观察隔离和此前 F4 已付结算。原回复已支付且记录在既有 SQLite。本地补检查点时，一个标准答案成员若被原解析器拒绝，公开结果仍按约定给 partial 与 received_checkpoint_standard_member_invalid。修复前该成员的原异常与原因链没有进入原生 F4，合成坏成员下的公开重放 CLI 退出 0，但本仓观察数为 0。

## 本仓处理

成员解码失败的处理处调用现有 owner 结构诊断。具体记录本地阶段、异常类型、代码帧及该成员的工作、问题和原生 attempt 引用。观察属于本次本地 replay，以本次的外层 run/attempt 关联。原回复内容和校验输入留在原资料，原业务结果、原费用、可恢复兄弟成员及权限均保持原合同。F4 对该本地动作的用量为不适用，继续由原业务账本证明已付用量。

## 离线复验

真实 main_with_llm.py --recover-checkpoints --seal-deliveries 读取本仓原生隔离 SQLite 与冻结回答夹具。使用原生 owner adapter，在登记和本地投递接缝使用合成替身，禁止构造 provider 与连接 socket。两个新进程使用原 quick_scan_feedback_owner reconcile 读回同一观察，不创建动作库，不增加请求、尝试或代际。用原持久表对比原回答、预算终态和已支付用量。公开结果的 JSON 一致性保持。

运行 tests/unit/test_quick_scan_known_outcome.py::test_received_member_decode_failure_reaches_original_f4_without_reasking。此文件使用项目现有的公开 C06 subject_dispatch_contract 夹具。已安装同版纯合同的环境可直接运行。若尚未安装，本地可只读将已发布 IQS 的 scripts 目录加入 PYTHONPATH。测试本身只运行 StockQA，不执行 IQS。Windows 验收应在短隔离 cwd 运行，pytest 使用短 --basetemp，避免宿主深 TEMP 的旧文件打开限制。

## 交接

只改本仓成员恢复路径与运行诊断。独审发现局部故障会令通用观察器误判整个调用的独立终态已经解释，两个反例分别复现后续非零返回和异常终态消失。修复让解析诊断在成员事务结束后单独保存，并显式不代替整个调用的终态；既有调用的默认方式保持不变。

最终本仓三个验收用例通过。除公开真实恢复 CLI 外，tests/unit/test_execution_feedback_intake.py::test_local_step_diagnostic_does_not_explain_independent_invocation_failure 覆盖同一运行的两条不同失败。另一独立 agent 匹配四份最终代码和测试 SHA，独立复跑三项全过。复核确认真实 F4 双冷读、原已付账本与独立终态兼容。
