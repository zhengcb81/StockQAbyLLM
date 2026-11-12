# MC-QA 发现记录

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
