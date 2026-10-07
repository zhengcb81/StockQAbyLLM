# QA-NET-01：总控整改交接（2026-10-07）

状态仍为 **partial**。原交付主体610de6a及证据修订d160d80已由总控接收；用户随后明确指定总控接管StockQA整改。代码提交为84e24ef79901ce7c817054f6354b90a15c44afd3，基线d160d80，分支master。原交付6文档/12日志及36工件索引完整保留在IQS `docs/implementation/intake/QA-NET-01/2026-10-07/owner-delivery/`，不能混作整改后的验证。

## 已闭合的验收阻断

| 发现 | 修复后的公开行为 |
|---|---|
| F1 external已准入却偷偷发送native | external生产dispatcher未实现时一律HTTP前拒绝；准入回执明确external_dispatch_enabled=false及原因 |
| F2备用模型响应与首模型冻结attempt冲突 | 每次真实HTTP分别记录route/model/send_intent/预算/结果；checkpoint只引用最终成功HTTP，严格绑定不放宽 |
| F3增加模块破坏旧题routing绑定 | 旧题继承原不可变routing，仅新题使用本次routing；用新输出文件仍复用旧答案 |
| F4 gen2重启错误回到gen1 | 每题保留实际generation；已完成gen2暖启动不请求HTTP |
| F5改prompt绕过旧uncertain重新收费 | identity/scope相同的未决请求优先对账；改prompt也不盲重发 |
| R1 checkpoint拒绝仍声称成功 | 缺可信绑定或保存拒绝成为公开error/CLI1，原HTTP/费用状态保留 |
| R2/R3异常usage与私有receipt早返回 | 冲突/缺失/负usage不推定免费；私有HTTP回执仍通过搜索/响应/model完整性门 |

HTTP测试仅替换HTTP边界，使用真实runner/provider/client/store和明确标注的合成identity/rate card。首模型已确认失败且明确完整usage才可结算并切备用；普通quota错误缺usage仍暂停，不能声称真实供应商拒绝免费。格式修复正例只覆盖既有legacy单route；configured v2仍禁格式修复。

## 实际验证

隔离导出：冻结194件公开Git文件加13件整改overlay；不复制ignored本地配置、真实key、生产DB或公司文档。统一公开 `python -B scripts/checks.py --full --timeout 180` 在IQS私有root运行：black/isort/mypy/bandit/pytest/smoke六步骤exit0，**982 passed，0 failed，0 skipped**，85.81s（pytest74.95s），见 `logs/remediation-full-green.log`。真实模型/搜索/付费请求0；opt-in live/benchmark不在本次范围。提交静态hooks全部通过。

本批初始6验收case为5fail/1pass；后续42定向绿、完整982初轮15fail/967pass、最终982全绿分别留档。环境和fixture失败未抹去，不能把全部历史GREEN累计为独立测试数量。独立静态增量审查由IQS保存，未独立重跑982。提交前4文件仅CRLF->LF规范化，实际执行字节SHA和最终源码SHA由commit-eol-equivalence.json分别绑定；可按导出manifest从Git精确复原。

## 尚未完成

1. external retrieval→短证据context→LLM生产发送未接通；即使准入配置齐也失败关闭。已有parser/缓存键/规整不等于端到端可执行。
2. DeepSeek Anthropic续写仍未准入；确切协议与可控费用上界尚未闭合。Responses不支持的native route不冒充可用。
3. Brave/Tavily/Z.ai的执行策略、可保存范围和计费需对应确切route确认，不由拥有API key推定。
4. security题须调用方明确security-scope-id；缺权威绑定保持deferred_scope_unbound、HTTP0。
5. 真实StockWiki identity/golden→C06导入/ACK→生产投影/UI及双owner备份恢复尚未集中验收；合成成功不是跨仓正例。
6. 原owner按名称/mtime删除共享TEMP的事故影响未知；本次隔离整改不能证明原删除无损。

## 下一步与回退

先按artifacts.json和IQS CLI校验本交接；再由总控集中处理SW-READY交付及真实公共接口联调。不得自动启动200家L03、改中央schema、复活已退役工程回执或清理共享TEMP。

可在新隔离目录重建：见IQS `reviews/QA-NET-01/export_runtime.py`、归档remediation-export-manifest.json与isolated_checks.py。本包没有生产DB迁移。回退必须由唯一owner在确认无新写入的独立分支上 `git revert 84e24ef`（必要时单独回退原主体610de6a）；`git checkout --`只恢复未提交文件，不能撤销已提交功能。回退不重复请求，不能把未知费用当0或删除历史attempt/outbox。
