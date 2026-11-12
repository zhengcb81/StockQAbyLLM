# MC-QA 进度记录

## 2026-11-11

### 归拢

- 3-way 合并脚本（base=HEAD blob, ours=canonical, theirs=candidate）跑 87 个双改路径 + 17 个 untracked：
  53 same / 13 theirs-only / 16 merged-clean / 18 CONFLICT；untracked 中 6 new-same / 7 new-DIFFER。
- 18 个冲突路径逐个做符号级超集验证（`ast` 顶层名对比）：除 `quick_scan_shared_company.py`
  （canonical 独有 `_require_company_scan_task_port`/CA 源路径常量——正是包卡要移除的）外，
  其余 17 个候选均为严格超集 → 全部按候选解决。
- 合并后校验：worktree 104 个改动路径与候选字节一致（CRLF 归一）。
- 工作树 EOL 陷阱：初始用 `core.eol lf` 落盘导致字节 hash 夹具失败（候选磁盘为
  autocrlf=true 的 CRLF）。恢复 `autocrlf=true` 并把 437 个未改动 LF blob 文件重写为 CRLF；
  `git diff` 证实无内容漂移。

### 测试与提交

- 短组（包卡入口 + 新文件）：99 passed → 收敛后 99 passed → 含回归组 336 passed / 41 skipped。
- mypy（5 个触碰文件）：候选 24 errors → 0 errors（净修 4 个 `_store` attr-defined + 候选既有噪音）。
- pre-commit（black/isort/mypy/mixed-line-ending/secrets）：全部通过。
- 提交：`9585431`（实现，110 文件 +9434/−1772）。

### 独审

- 审查 agent（独立会话）读完整 diff `4c24789..9585431`，亲跑短组 + mypy：
  **ACCEPT-WITH-FIXES**。
  - 已验证正确：facade 有限投影（含值导出、module 出境阻断）、组合根 facade 协议校验、
    发送/重放分离未被削弱、owner port 不透明性与幂等 adapt、SQL 迁移逐字保留、
    归拢无缺漏（字节对比）、无秘密。
  - F1(major)：companion facade import 守卫随扫描器收敛而丢失，无替代 → 已补
    （含 `importlib.import_module` 属性调用形态，反例探针验证）。
  - F2(major)：owner export 检查退化为仅 ImportFrom 名 → 已补模块别名属性链检查。
  - F3(minor)：vacuous `undeclared_calls` 断言 → 已删（同属防御由 F2/私有成员检查承担）。
  - F4(minor, 既有)：SQL 匹配需尾空格 → 补 padding。
- 修复提交：`72fae07`；边界测试 12 passed；F1/F2 反例探针通过。
- 复核（同一审查 agent，针对 delta）：**ACCEPT**——F1 五种违规形态 + 对照组合法形态、
  F2 别名属性链绕过均被捕获且定位正确；真实 src 树 33 passed 零误报。
  非阻塞残留 R1（别名动态 loader）/R2（全路径 Attribute 链）记录于 findings，不修关包。

### 真实缺陷修复记录（候选带入，RED→GREEN）

1. `LLMRunner._recover_shared_company_answers` 引用未接线的 `self._store`
   （候选为过守卫把 `session.service.store` 改成 `self._store` 却没接线）→
   组合根在 `adapt_owner_refresh` 前取 `prepared_session.service.store` 显式传参；
   opaque port 不携带 store，测试 fake 无 service/store 属性以钉死回归。
2. 恢复预算路由引用先于定义的 `work_store`（recovery 路径 NameError）→ `recovery_store`。
3. `test_paid_runner_independence` 的 owner double 缺 `snapshot()`（port 化前形态）→
   更新为 detached snapshot 形态。

### 环境事实

- 本机 Python 3.13.9；`iqs_company_agent_lab`/`iqs_agent_runtime_lab` 未安装 → 真包组合测试 skip。
- `IQS_ORIGINAL_ROOT`/`IQS_ROUTE_TEST_CODE_ROOT` 未配置 → 对应测试干净 skip（配置后 13 passed 验证过）。
- 测试临时根统一 `--basetemp` 到 `%TEMP%/iqs-mcqa-merge/bt*`，未触生产库/密钥/收费 API。
