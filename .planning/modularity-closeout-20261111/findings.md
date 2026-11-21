## 2026-11-21 日常执行失败为何漏进 F4

脚本输入失败能只留一个退出码或滚动日志。单题和组包错误又可能按最外层异常名压平，inspect 的结算观察依赖显式宿主输出路径。由错误发生层保全脱敏原因链、所属阶段和安全输入形状，以公开读取接口把一次失败和原账核对应起来。中微 IQS_13/14 的旧 prepared 记录只能证明当时未到 HTTP，旧深层异常已丢，不反推具体数据库错误。详见 [本仓 F4 子计划](F4-STOCKQA.md)。

## 2026-11-21 新实物原因不可倒推历史失败

原run第4次冷恢复已真实通过，0收费新请求、原10包绑定不变，说明原paid读取不需新计划授权。第一次受限shell复验的CLI错误已安全定位为eager导入创建默认日志的PermissionError，经fallback误显成权限；正确授权环境原seal通过。这是本次操作环境证据，不是原13:09历史cause，也不能作为删除原paid/输入校验的理由。日志副作用和完整错误揭示交F4另一harness，本线程不扩改。

## 2026-11-21 根因：把不同职责放进同一个前置检查

已付答案读取与新扫描调度是两种操作。原paid记录、context、subject receipt和binding齐全时，新plan提出ttl_expired/新work不能使旧答案失去读取资格。原生产launch3和合法合成ACK隔离反例已共同暴露此问题；修复应删除读取对deferred/reuse的依赖，today anchor/plan仅约束真正reactivated的新派发。

账本普通打开/就绪与维护深审计也应分离。当前schema27临时只读快照结构检查0.005秒、完整历史审计91.037秒均通过，不能把全历史扫描作为每次启动的必要门禁。收敛在store current-schema构造和admission这两个实际入口，保留旧迁移及显式deep audit；当前操作依然读取并验证本操作所需的原生记录。不按公司、题号或异常文本添加特例。

`refresh_admission_ledger_invalid`原历史底层原因仍未知；结构无效与SQLite/IO暂不可读应分别报告，不将不可读账本放行收费。F4细节由另一harness推进，原失败的时间/阶段/影响和未知已在本模块及IQS PWF留档。两组实现已通过作者针对性测试与非作者12项实际组合（ACCEPT）；没有抬协议/版本，也没有新增审批门禁。软件结果不代替原中微完整28题/实际UI验收。

## 2026-11-21 设计收敛结果

原状态owner已有完整原子证明，transport复制证明且依赖prepared对象返回反而漏清理；现只委托原public release_unsent。收费授权只在intent前一次fresh，不以新许可阻已付保存/复用。跨层丢支付未知并非缺一个异常名，而是各层复制只看cause的规则；现既有interfaces提供同一有界cause/context遍历。新local验证是可选能力，不改变旧1.3 required协议。

上述统一设计与针对性测试、非作者对抗审查均通过；F4日志本身未在此实现或签收。最近13:09历史错误的深层cause已丢，仍不补造根因，原详细记录由IQS PWF保留。

## 2026-11-21 日志与授权职责

根源不是缺少一个反馈系统：已有F4只覆盖明确的settlement检查，而模型执行的原异常边界只输出type，cause未落盘；普通UI直接启动main_with_llm，配置host不等于自动采集异常。过度脱敏与粗catch共同让真实底层错误不可追溯。本轮保留脱敏错误码、原因链和代码位置，不导出message/args/locals/提示正文；日志和反馈是伴随诊断，绝非业务门禁。

重复调用同一个.check并不会加强冻结身份/题义绑定，却会把外部owner核查扩散到组装、getter、保存、恢复。两题隔离实测26次CLI已构成可重放反例。固定绑定在加载时核实，实时权限紧邻原子发送intent一次；其后同一个一次性permit不再次授权，避免纯权限变化被误当成未知发送。下一次真实收费仍独立核查，原预算和lease保持原子事实。

# MC-QA 发现记录

## 2026-11-16 回执物理结算与供应商标签

供应商响应状态描述的是回包，逻辑决议取决于原物理回执是否真实收到并结算和原 checkpoint 是否存在。旧白名单在合法新标签和缺结束标签时误拒。有完整回执的中性无答案与具体答案解析诊断分开处理。未知发送、未结算及有效租约沿原规则保留。

旧恢复测试绕过了已演进的 recovery-only 会话构造并假设题包整体抛错。回归按实有会话构造和逐题恢复更新，验证记录前后的同一 attempt、失效成员隔离、写入后提交前撤权回滚和费用不变。

## 接口与兼容

1. **候选即超集**：1.8 候选相对 canonical 工作树在 47 个内容不同文件上几乎全部是
   符号级超集（协议 `1.1.0→1.2.0/1.3.0`、`1.2.0→2.2.0`、裸名→`api_*` 导出、
   新增 `supports_public_api_operations`/`original_semantic_resources` 等）。
   唯一反向是 canonical 在 `quick_scan_shared_company.py` 里的 CA 源路径 task port
   （`COMPANY_SCAN_TASK_PORT_*` + `_require_company_scan_task_port`），正是契约 C1
   要移除的形态；IQS 侧若曾依赖它，需总控在组合时改走 `company_scan_contract`/
   `create_company_scan_task` 公开操作（候选测试已按新形态覆盖）。
2. **facade 1.8 floor**：IQS ports 要求 ≥1.7；本包公开 1.8 且 `supports_public_api`
   同 major 兼容（1.7.0/1.8.2 均过、2.0.0/1.6.9 拒绝），消费者无需抬高下限。
3. **`company_agent_public_api` 返回形状变化**：由"验证后返回整个 module"改为
   `CompanyAgentPublicOperations` 有限投影。StockQA 内部 8 处调用点全部按
   `.属性` 访问，无一依赖 module 身份；`tests/unit/test_quick_scan_company_task_factory.py`
   的 fake 已按投影形态 monkeypatch。若 IQS 有同名调用需同样只按属性访问。
4. **组合根配置新增可选键**：`company_agent_import_root` / `runtime_import_root`
   （显式 companion 包导入根，免源码布局假设）。旧配置继续工作（root/src 探测）。

## 根因

5. **候选的 `self._store` 重构断裂**：为通过新增的 owner-internals 守卫
   （禁止 `session.service`），候选把 store 访问改成 `self._store` 但 LLMRunner
   从未接线——生产 recovery-only 路径必 AttributeError，且其自身测试 double 缺
   `snapshot()` 先行掩盖。修法遵循 port 设计：store 属于组合（composition root），
   不属于 opaque port；测试 fake 不带 service/store 属性以防回归。
6. **EOL 字节夹具**：manifest/authority/ledger 夹具内嵌 `manifest_file_sha256`
   等原字节 hash，只在 autocrlf=true 的 CRLF 落盘下匹配。任何 LF 检出的工作树
   （CI 除外——CI 在 ubuntu 上同样 LF，但那批夹具测试不在日检目标里）会假失败。
   本包把工作树恢复为 owner 磁盘约定；长期修法是夹具内嵌 hash 改为对归一字节计算，
   归 MC-QA 后续或总控裁量，本轮不动夹具语义。
7. **守卫 vs 接线**：静态守卫（禁 `session.service`）没有配套的合法取用路径时，
   会把正确代码逼成错误代码（`self._store`）。收口边界时每删一条访问路径要同时
   给出新的显式接线，否则是守卫绿化不是收口。

## 测试分层

8. **反射扫描收敛**：2289 行 → 域方向 12 检查 + 公开契约 14 检查（两文件共 ~700 行）。
   删除的 10 个别名/taint 追踪器及其 15 组自测（~950 行）按契约第 4 节
   "不造解释器/沙箱"裁决；独审确认的两次覆盖回退（companion facade import、
   owner export 别名属性链）已按"常见直接访问"标准补回并配反例探针。
9. **真包材料分层**：单元层全用 `company_agent_public_fake.py`（协议 2.3.0 +
   detached snapshot + digest 校验，形状对齐真实 `task_snapshot`）；真包组合在
   `tests/integration/test_company_agent_facade_material.py`，包缺失即 skip，
   供总控 I1 场景在安装真包后启用。
10. **外部 IQS 依赖显式化**：`IQS_ORIGINAL_ROOT`（answer_validation 语义资源）与
    `IQS_ROUTE_TEST_CODE_ROOT`（wire schema/route oracle）未配置即 skip；
    候选原来用 `parents[3]` 硬编码"StockQA 物理位于 IQS 树内"的机器布局知识。

## 遗留（不阻塞本包交付）

11. 候选其余 mypy 噪音已在本包触碰的 5 个文件清零；未触碰文件的存量类型噪音
    （如 providers/ 下若干 no-any-return）留待各自域处理。
12. `run_shared_company_scan` 现传 `scan_contract.identity_payload`（entity_id/
    canonical_name/listings 投影）给 `native_company_projection`，较候选的全量
    identity 更窄——有意收窄，真包组合（I1）是最终把关。

## 独审复核结论与已知限制（`72fae07`）

复核 verdict：**ACCEPT**（F1-F4 全部验证修复，检测器反例注入探针全部命中，
真实 src 树零误报）。已知静态限制（契约第 4 节 declared boundary 内，记录不修）：

- R1：`from importlib import import_module as load; load("<companion>.task")`
  别名动态 loader 不被 F1 捕获（候选扫描器捕获；属故意绕过形态）。
- R2：`import src.utils.quick_scan_X` 后以全路径 `src.utils.X.attr` 访问不被
  F2 捕获（`import X as alias` + `alias.attr` 已捕获；grep 确认真实 src 无此形态）。
- 相对导入别名注册（`from . import X as y`）未实现；真实使用不在 EXPORT_BOUND_OWNERS。


## 2026-11-20 StockQA F4

旧固定目录无法代表多个已使用布局，仓根 SQLite 本次只读探测为 schema 5。按实际 inspect 已知 ledger、原工作项、代际和原生 attempt 建索引，外层 attempt 覆盖无影响。仅原生终态和同次收费收据能确证历史；派生修复不会补猜费用。 [F4 端口](F4-STOCKQA.md)
