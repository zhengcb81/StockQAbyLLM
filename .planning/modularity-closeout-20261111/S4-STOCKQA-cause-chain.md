# StockQA S4 原请求异常链与重入

## 问题与责任边界

正式基线为 `2c96d1e96769fcfafce29f39c9a5647e35301af9`。题包刚进入派发时，外层异常带 24 层包装和一个回指，最内层是 transport 已声明的未知结果。StockQA 的共享 transport 契约最多访问 16 个异常，导致核心不能恢复原请求元数据，运行器将两个仍有效的原租约当作未发送而释放。此时可重新领取。现有 F4 已能冷读全部异常类型，业务处理应读到同一因果事实。

## 修复与复现

- 从真实 QAEngine 题包入口复现。两条单独的原 SQLite 工作在访问类型前已领取。合成 transport 返回深层未知结果，无网络、模型、付费请求或正式库。
- 共享的 `iter_exception_chain()` 依旧按因果、上下文的顺序读取 Python 原生槽，用对象身份去重。改用队列完整遍历，存在回指或两个边指向同一对象仍只访问一次。调用者继续负责原来的未知结果、费用和工作状态。
- 在旧代码上，定向两例均失败。两条原工作状态为 `pending`，30 个不同对象只读到 16 个。修复后两个定向用例通过。复核派发时与尝试重新领取后的完整原工作行，包括原租约 token 与 epoch。原 attempt 和 request 仍来自 transport，结果为 `uncertain`，费用策略和新 attempt 均未产生，transport 调用一次。
- 从新的 `src.utils.quick_scan_feedback_owner` CLI 进程冷读原观察，重新定位原工作和完整 25 层类型事实。异常文案未进入冷读结果。结构化日志、来源数据协议和付费重试策略沿用既有实现。

## 验收和交接

离线运行 `test_owner_refresh_charge_boundaries.py` 的 `deep_uncertainty or public_exception_chain`，最终两项通过。九文件组合为 232 通过、12 跳过、1 个基线失败，Black、isort 的 Black profile、全 `src/` mypy（125 文件）和 git diff 检查通过。各轮使用隔离临时根并清理。

唯一组合失败为 `test_w15_owner_refresh_transport.py::test_capacity_retry_rechecks_owner_before_reservation[True]`。从正式 SHA 直接导出到另一隔离目录重跑，仍得到相同的 `abandoned_unsent` 对 `prepared` 差异，配对的控制项通过。该断言沿用 `95854316`，早于 `f69913c` 确立的 owner 已证实未发送时释放原租约的合同。同仓已有拒绝回归要求 `abandoned_unsent` 且没有发送意图。旧容量用例已证明预算表仍只有其他请求的原记录，因此只需另案更新测试，正反两支分别验证无新增预算请求和允许后的真实第二次预留。

总控的非作者审查通过，核对源码、实际调用回归与原 F4 冷读。候选两文件镜像的 LF 汇总 SHA-256 为 `46c2a5485ea61f78a84621d8c8ef8a5ca0ca9b68437c2c8e69a6bf0b40184381`。正式维护线发布 `dda4d2721aec111949401103331cd48318801103` 后核对五个上游文件均不重叠，只快进 S4 分支。新基线复验 110 项，包含原租约、C06 真实夹具、题包 transport 和 F4 诊断全部通过。官方基线的 Ubuntu CI 运行 45583936047 已通过，包括统一检查步骤。本候选按同一基线提交发布。其他工作树与全局安装保持原样。

## W15 容量复验

深异常修复 `cff6b3d3068049197806ed3dd0e81ad3e9a63783` 已发布，其 Ubuntu CI 45584685741 的统一检查成功。W15 用例独立修正，继续走真实容量等待、重新读当前 owner 和原合同。失效支路核对只有其他请求的 `DISPATCH_busy` 预算记录且实际用量为零，原工作回到 `pending`，原租约清空，唯一 attempt 标为 `abandoned_unsent`，无发送意图。配置未变支路仍要求第二笔容量预留。运行代码保持原样。

两个容量分支经非作者独立复验通过。W15 与 S4 组合运行 46 通过、4 个依赖环境的既有项目跳过，Black、isort 和 diff 检查通过。所有运行使用隔离 SQLite 或反馈目录。
