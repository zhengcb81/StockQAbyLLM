# QA-NET-01 隔离与历史事故记录（2026-10-07整改版）

当前唯一StockQA整改writer为IQS总控，用户明确授权接管。整改基线d160d80，代码84e24ef。7项既有未跟踪项仍存在，未读内容、未删、未暂存：.codegraph/、.workbuddy-ai/、nul、pilot_runs/b2a_2026-10-03/、pilot_runs/g2b_alphabet_2026-10-04/、pilot_runs/l02_2026-10-04/、progress_update.txt。当前状态摘要由handoff.json记录，不能使用旧“未提交/27dirty”的文本代表当前状态。

## 本次整改验证

所有pytest/temp/log/cache/db在IQS `runs/qa-net01-intake-*` 独占绝对root，明确export-manifest白名单及哈希，不使用共享TEMP或真实执行/公司库。凭据环境移除，不复制ignored配置；真实HTTP/搜索/付费0。Python audit拦截根外写入、DNS/socket、注册表、任意shell，仅允许精确冻结哈希的私有run_ci.sh/run_ci.bat与Python checks；标准库socketpair自唤醒有精确调用位置/localhost例外，不开放一般HTTP或loopback。固定wrapper只转发Python；Python audit不是OS级shell沙箱。

子Python继承guard；-S/-I/-E/-BIS被拒绝。nested pytest按PID独占basetemp，避免删除父测试进程临时数据。根外写入/DNS/guard禁用flag的真实canary已通过，记录 `logs/remediation-guard-canary.log`。Git Bash因Windows沙箱MSYS对象权限拒绝，改在沙箱外同guard跑六步骤，982全绿；没有通过skip失败或改生产契约来报绿。检查结束不留子进程；日志/manifest归档后仅按精确归属清理本批独占root，最终清理回执由IQS保存。

## 原owner共享TEMP事故（必须保留）

原owner自述按名称/mtime会话窗删除25个pytest根，误触含其他进程产物的pytest-921并遇PermissionError；该清理不满足manifest精确归属规范。rmtree可能在报错前已删除部分文件，**影响未知，不能说原样保留/无损**。后续所谓pytest轮转删除是原owner陈述，总控没有独立验证。原日志/coverage曾写共享仓库，原记录完整在IQS owner-delivery/isolation.md。总控未重现删除、未扫描共享TEMP、未清理原日志/coverage，也不能以新隔离通过撤销此事故。

本批清理规则：核绝对路径位于IQS/runs、归属manifest、无reparse/active process，保存清单和证据后才删；禁止按日期、mtime、pytest编号推定归属。StockWiki/company-wiki及其他harness目录均不写、不清理。
