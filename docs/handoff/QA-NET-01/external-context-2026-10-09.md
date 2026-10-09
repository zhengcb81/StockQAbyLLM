# Phase111 外部证据执行链交接

本文件随本批 StockQA 源码交付。输入基线为
`42a517c4bd6bc8219f926957c6c332944da3278a`；正式版本以包含本文件的
StockQA commit 与 IQS 的实际 publication 回执为准，不把准备中的 Git 意图当发布。

## 范围与版本

复用原 Q08 health、Q09 预算与逐 HTTP 派发、LLM 客户端、工作库及结果 outbox。
不另造账本、通用 MCP client、名单权威或文档采集器。输入搜索执行 policy 为
1.1.0；旧 policy 1.0.0 保持原解释。工作 SQLite schema 从 8 升至 13；升级只添
真实新记录的存储结构，不补造旧检索/native 事件或模型来源。checkpoint1/2 显式
区分；native-only public result 1.0.0 保持，external/hybrid 使用 public result
1.1.0 与独立 use proof。C06 Observation/ExchangePackage/ImportAck 公共契约不变。

REST 路线为 Brave、Tavily、Z.ai；Z.ai Streamable HTTP MCP 的每个 initialize、
initialized 通知、tools/list、tools/call 都是分别准入、预留、派发、结算的真实
HTTP。支持协议 2024-11-05 与 2025-03-26、工具 web_search_prime/webSearchPrime，
按实际发现的 schema 仅发送 search_query，top_k 在本地截取。不能假定协商免费，
不能增加隐含 GET、重连、DELETE、legacy SSE 回退或执行 server request。

DeepSeek Responses 可按显式模型许可 1.1.0 使用 external-only 文本回答与 high
reasoning；这不宣称该接口有原生搜索。其他厂商的实际可用协议仍依据既有配置、
能力许可及真实探针。实际 HTTP model、requested model 分开留存，未知实际模型或
费用不由请求名回填。只提取最终 assistant 正文，不保存思考过程。

## 执行与恢复

公开入口仍为 main_with_llm.py。外部执行需独立确认的 identity snapshot、question
manifest、1.1 搜索 policy、既有模型配置/模型顺位与 spend authorization；完整
standard body/C06 封包还需独立 c06 authority。测试里的合成身份、价格、权限引用
和凭据只能用于测试，不可直接作为生产配置或真实 golden。

已有 CLI 参数为 --company、--entity-id、--provider、--config、--output、
--require-search、--identity-snapshot、--question-manifest、--search-policy、
--spend-authorization、--c06-authority。既有 --seal-deliveries 补封不调用模型。
本批不自动启用 paid route，不导入真实名单或迁移生产数据库。

搜索短证据作为明确标注的不可信 context 提供给 LLM。公司、身份、题库、代次、
查询计划、policy、时间/TTL、域名及发行人路径、价格与存储授权都在发送前冻结。
答案引用不能越过实际 eligible 来源。数据库保存有限短摘要/来源元数据和哈希、
标准答案及账务，不保存网页正文、财报、原 HTTP body、prompt 或 reasoning。

已收到、已结算且 TTL 内的 REST 短证据可由新 lease 在同 generation 重新核验复用，
原 operation/检索时点/费用保持；不同 generation 必须重新完成自己的检索，直接
传旧 operation 或重哈希旧引用也拒绝。旧 worker 无权推进新 lease。MCP 迟到的
控制 session 不能推进新 lease。模型迟到只保留实际响应来源和费用，不得到有效
use/checkpoint，不发布答案。

unknown 发送、费用/usage 不明、超过价格上界或持久化失败保持预留并停机；换模型、
搜索路线、policy 或过期租约都不能洗掉未知态来重复付费。确认且可计价的终态失败
才按各自顺位换路线。已完成 warm 恢复先验证原缓存；可不提供搜索 key，但新 HTTP
必须在预留/发送前验证凭据。用户明确新扫描 generation 才刷新过期证据。

## 验证与交接边界

IQS 留档目录：docs/implementation/intake/QA-NET-01/2026-10-09-external-context。
受审37源码精确 SHA 为 static/whole-phase-static-04/process.json 与
verification/whole-phase-regression-02/process.json；两者相同、执行期间不变。
isort/Black/mypy61源全通过；最后主批861 passed（stdout145.23s，controller
145.863s），另邻接128 passed及 IQS111 passed/216 subtests 的未变源码证据保留，
不跨批机械相加。原 RED、错误码 fixture 失败、controller timeout 均独立保存。

同一次集中独审原报告 final-review.md/json 发现 PH111-R1/R2；修复后的最终结论与
精确 SHA 以 final-review-recheck.md/json 为准。OS 集成使用真正独立 Python CLI，
包括冷暖、11个强杀断点、未知发送与完整大 body/warm/seal；不由测试先替 CLI
恢复 work。所有该批验证使用 HTTP 边界替身、合成身份/价格和独占 guard 环境。

这些证据只支持有限软件实现；不认证当前厂商连通/价格、金融事实或评分准确率，
不伪造 StockWiki owner identity/facts/query golden 或 import ACK，也不关闭
G3/F05/L03/TH-IN 与200家公司 live 门。下一跨仓动作仍需 StockWiki 精确写授权及
实际 owner 交付，再固定两仓新输入运行联合链验收。旧 C01–C07 工程回执已退役。
