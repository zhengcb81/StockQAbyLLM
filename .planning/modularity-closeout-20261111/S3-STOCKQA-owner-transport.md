# S3 StockQA owner 进程环境边界

## 当前步骤

维护线 `codex/qa-content-filter-resolution-01` 基线 `95fdab89`。实现、定向验证和非作者审查通过。其他 harness 的根 PWF、工作区与全局安装保留。

## 问题与范围

既有 owner 保存配置映射，consent client 用同名私有方法构造子进程环境。预算、决策和登记依赖具体内部对象，正常类型组合当前可运行。需要用只提供公开操作的替身、默认隔离、显式路由配置、C06 受控夹具证明实际边界，以及既有权限和登记回归验证。

1. 先记录边界测试 RED。
2. 由无业务依赖的本仓叶子封装两种现存环境策略，路由/登记继续共享原路由规则，consent、预算、决策使用公开 consent 端口。
3. 本仓跑定向测试并交给非作者审查。测试使用临时根和假响应。
4. 单独登记依赖环其余边及后续方向，按总控审查发布。

## RED

4 个边界用例在旧实现下定向失败。决策替身报内部环境属性不存在，登记替身报内部配置不存在，两种独立环境入口尚未公开。49 项同文件旧用例未计入该次运行。测试临时根使用后清理。

另外 4 个替身回归在旧实现报私有 `_record` 不存在。分别覆盖原登记正确、丢失、冻结输入变化和收据变化。隔离导出同一 HEAD 还复现了两条既有恢复报告测试失败，原夹具仍使用已不经由的 `check()`，并缺有效原登记或已付响应。真实执行失败日志在禁止导入业务检查模块时无法生成旁车，也有单独 RED。

## 实现

- route 和登记共享环境构造，允许受信调用方传入完整路由覆盖，默认只继承既有 OS 键。consent 及预算、判断继续用既有 OS 键过滤显式和继承值，保持原解释器参数、C06 专用 guard/temp 和 subprocess 参数。登记会话先执行原校验，再交 owner 发送请求。外层无须读原始环境配置。
- `RefreshAdmissionService.read_registration()` 封装原账本冻结登记读取。执行会话使用一个键值比较实现，仍独立检查原绑定的 confirmation ID。恢复测试夹具现在提供原登记和已付响应，在真实校验通过后进入报告。无新的准入条件或架构扫描门禁。
- 失败日志调用已有 `execution_feedback`。保留原业务投影模块的两个兼容入口和既有旁车、日志不可用时的业务退出行为。结构化日志仍排除异常文案、参数、局部变量。

## GREEN 和验收

用临时 cwd、pytest 临时根及反馈目录运行这些文件，禁用 inherited addopts 和 pytest cache。最后清理本轮临时目录，未调用真实下载、模型或已付重发。

- Owner transport 组合包括 `test_quick_scan_owner_refresh_port.py`、`test_quick_scan_refresh_consent.py`、`test_quick_scan_work_decision_authority.py`、`test_quick_scan_subject_owner_receipt.py`、`test_quick_scan_module_boundaries.py`、`test_quick_scan_owner_public_boundary.py`。
- 原登记与恢复组合包括 `test_quick_scan_refresh_execution.py`、`test_quick_scan_refresh_admission.py`、`test_paid_result_preservation.py`、`test_qa_question_pack.py`、`test_quick_scan_known_outcome.py`。
- 失败发射和原账冷读组合包括 `test_execution_failure_diagnostics.py`、`test_quick_scan_feedback_owner.py`、`test_quick_scan_feedback_observation.py`。真实本仓 CLI 对隔离 SQLite 触发输出丢失后，用 action ID 查回原修复和回告，重放核对原 attempt。API、新 attempt、新 generation 均为零，原账本逐字节相同；日志发射不再触碰业务检查模块。

总计 286 passed、1 个既有 skip，95.71 秒。跳过项需要未配置的 `IQS_ORIGINAL_ROOT`。全 `src/` mypy 125 文件通过，静态注解微调后 6 项登记尾验通过。Black、isort 和 git diff 检查均通过。非作者追加 10 项隔离通过，复核了已付范围、原登记和原日志接口。

## 后续

静态分析 `src/` 中可解析的本地 Python import，排除 TYPE_CHECKING 并分别计算顶层和含函数内导入的强连通组。在同一候选只恢复旧 logger 依赖以对照，顶层均无环，含延迟导入的最大组由 63 文件降为 56 文件。该统计无法证明每条延迟边在一次运行都会被走到。后续围绕账本、恢复会话与组合根现有依赖继续拆分，不一次搬动账户和交易逻辑。发布以正式维护分支的 Git 提交为准。

另外，严格 OS 环境继承历来丢失 StockWiki 子进程的 F4 correlation 字段。它与本轮端口整理分开处理。下一步用本仓 owner 级定向测试验证受信调用的已知相关号能透传、普通凭据继续被过滤，并由组合根验一次跨宿主冷读。
