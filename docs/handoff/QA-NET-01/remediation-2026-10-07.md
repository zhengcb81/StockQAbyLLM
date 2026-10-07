# QA-NET-01整改证据入口

代码：master@84e24ef79901ce7c817054f6354b90a15c44afd3；冻结基线d160d80dc2340c6fb6cef0453b289b5210cf2cb6。

主线实际执行公开六步骤：982 passed/0 failed/0 skipped/85.81s，见logs/remediation-full-green.log；这是离线unit/integration，不含live/benchmark，stub仅HTTP边界，不是模拟整个业务层。独立报告位于IQS docs/implementation/reviews/QA-NET-01/remediation-review-2026-10-07.md，审查范围是本次5阻断及3增量薄弱点，不是全搜索/跨仓上线审批。报告如有未决结论，以IQS最新记录为准。

完整不可变原始交付、初始5fail/1pass、42定向绿、full15fail/967pass、环境/fixture修正、最终982绿、guard及导出/EOL等价manifest，都位于IQS docs/implementation/intake/QA-NET-01/2026-10-07/。实际执行字节与Git源码换行区别明确，export_runtime.py可从精确commit恢复，避免读取私人配置。artifacts.json绑定本仓当前源码/文档/日志。

验收只关闭列明5阻断及R1–R3，整包partial。下一批必须处理真实producer/consumer及ACK/恢复证据；external未接通、DeepSeek续写未启用、security未权威绑定不可执行。未知费用/迟到结果不能清账或重发；缺使用证据的供应商错误不能自动算0成本。
