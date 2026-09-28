# 脱离 deepagents 内核迁移方案

> 文档定位：本文件是「以自有内核替换 `deepagents==0.7.17`、同时**保留 langgraph/langchain**」的实施规格。
> 它描述决策点、目标架构、文件级改动清单、里程碑验收与回退策略。
>
> 与既有文档的关系：架构总览仍是 `docs/architecture.html`（叙述型）与
> `docs/architecture-diagrams.html`（图形型）；分层契约见 `.importlinter`。
> 本文件**不覆盖**它们，只补「内核替换」这一专项。
>
> 前置分析结论（本文的事实依据）：
>
> - 生产代码对 deepagents 的依赖只有 **9 个文件**（`agent/` 7 个、`runtime/` 2 个），
>   `application/` 28 个模块**零依赖**；
> - `create_deep_agent` 全应用唯一调用点在 `agent/graph.py:204`；
> - deepagents 的 `HumanInTheLoopMiddleware`（graph.py:921）与 `TodoList` /
>   `ContextEditing` / `ModelCallLimit` 中间件**都来自 langchain**，不在替换范围；
> - deepagents 本身就是 langchain `create_agent` 之上的一层封装（其 graph.py:12），
>   因此"脱 deepagents 保留 langgraph/langchain"在结构上是自然切分。
>
> **v2 修订（2026-09-28）**：应「完整、无妥协适配多场景横向拓展」的要求，本版把
> **场景从附带收益升格为设计主线**：新增决策点 D4（一张图 + 三层运行时裁决）、
> 目标 G5 扩充为七维场景矩阵（新增 G7/G8/G9）、M4/M5 里程碑吸收场景一等任务、
> 原「场景化本体 M6 后另立项」的排除条款撤销。

---

## 一、三个前置决策点（动手前必须拍板）

### D1 功能裁剪清单 —— 不追求 25,000 行等价重写

deepagents 全包约 25,000 行（侦查见附录 A）。其中大量能力本项目**从未使用**。
等价重写全部内容是本方案明确拒绝的做法；裁剪清单见第六节，要点：

- **放弃**：`backends/sandbox.py`（1,989 行，本项目已有自研三档沙箱且更贴合需求）、
  `backends/context_hub.py`、`backends/langsmith.py`、`middleware/rubric.py`、
  `middleware/async_subagents.py`、`profiles/` 里的全部厂商内置 profile（含 1,857 行的 nvidia）；
- **简化**：`composite.py` 的路由本项目只有两条（`/memories/` → Store，其余 → 本地），
  等价实现可从 1,019 行缩到约 200 行；`filesystem.py`（3,697 行）裁掉
  upload/download、视频抽帧、跨沙箱路径换算后约为原 1/3；
- **直接搬运**：协议层**纯数据类型**（`ReadResult` / `GrepResult` / `GlobResult` /
  `FileData` / `FileInfo` 等）连同其一致性校验逻辑原样复制——这些防御性校验
  （如 `ReadResult.__post_init__` 的分页窗口数值一致性检查）是上游踩坑的结晶，
  重写遗漏任何一个都会变成"静默跳行/静默截断"级别的 bug。

### D2 语义保留范围

| 能力 | 决策 | 理由 |
| --- | --- | --- |
| langgraph 检查点 / 时间旅行 / 中断恢复 | **保留**（langgraph 提供，不动） | `run_branch.py` 的分叉依赖它 |
| 同步子 agent（`task` 工具） | **保留**，等价实现 | 通用能力，README 已对用户承诺 |
| 异步子 agent（start_async_task 等 5 个工具） | **放弃** | 未使用 |
| 上下文治理 | **保留 langchain 的 `ContextEditingMiddleware`** | 现状即如此，deepagents 的 Summarization 未在栈中（M1 侦查确认） |
| HITL | **保留 langchain 的 `HumanInTheLoopMiddleware`** | 现状即如此 |

### D3 迁移策略 —— 绞杀者模式，双内核并存

新内核与 deepagents **长期并存**，由装配开关切换；每个里程碑结束时系统都处于
"可运行、可回退"状态，不存在半成品期。禁止"大爆炸式替换"。

### D4 ★ 场景模型 —— 一张图 + 三层运行时裁决（场景升格为设计主线）

多场景的七个维度（技能 / 工具 / 提示词 / 护栏 / 治理参数 / 模型 / 可观测）全部以
`AgentRunContext.scenario` 为轴做**运行时裁决**，而非"每场景编译一张图"：

- **为什么不分图**：缓存键若含 scenario，则缓存 = 工作区 × 模型 × 场景 三维爆炸，
  且每次新增场景都要动装配机制；一张图 + 运行时裁决让场景增减零装配成本。
- **三层裁决（纵深防御）**：
  1. `wrap_model_call` 裁剪 `ModelRequest.tools` / 重写 `system_message` —— 模型
     看不到 schema 就无法发起调用（第一道）；
  2. `wrap_tool_call` 对不可见工具返回结构化错误 —— 拦截提示注入编造的工具名
     （第二道）；
  3. `InterruptOnConfig.when` 谓词按场景裁决审批 —— 编译期取**全场景并集**（默认
     最严），运行时收窄（第三道）。
- **EXECUTION_MODE 语义重定义**：从「全局开关」变为「**部署上限 + 场景收窄**」。
  部署档位是宪法（`sandbox` 部署里任何场景都造不出宿主裸执行），场景是行政法规
  （在宪法内收紧工具、加审批）。场景**不能放宽**部署档位——这是安全模型的设计，
  不是妥协；需要 `execute` 的场景（如报告渲染）要求部署档位至少 `sandbox`。
- **凭证与资源**：场景专属 MCP server 采用**惰性连接 + 空闲回收**（首次出现该场景
  的会话活动才建立，复用 `llm/embed_process.py` 已验证的空闲回收模式）；工具目录
  （`/api/tools`）按场景过滤。对「Agent 越权调用」这一威胁模型，三层裁决提供与
  分图等价的隔离；进程级物理隔离属于部署维度（路线 C），不在本方案。
- **场景配置载体**：`preset.toml` 升级为场景清单（新增可选 `[scenario]` 段），解析
  fail-loud（声明不存在的工具名 / 超出部署档位 / 不存在的模型别名 → 装配期报错），
  解析结果按 `(scenario_id, presets_dir)` 进程级缓存。

---

## 二、目标与范围

### 2.1 可判定目标

| 编号 | 目标 | 判定方式 |
| --- | --- | --- |
| G1 | 依赖收敛 | `grep -r "from deepagents" agent/ runtime/` 仅命中 1 个门面模块（M0）；最终归零（M6） |
| G2 | 行为等价 | 现有 ~1300 个测试全绿；`tests/agent/` 的 133 个不经修改通过（协议类测试除外，逐个列豁免） |
| G3 | 安全模型恢复 | `EXECUTION_MODE=local` 下 `read_file .env` 被**工具级权限**拒绝（而非仅靠审批）；`smoke_execution_modes.py` 断言更新并通过 |
| G4 | 技能即时生效 | `SkillState.set_enabled` 后**无需重建图/视图**，下一次模型调用即按新状态注入技能 |
| G5 | 场景运行时解析 | 同一张图缓存条目下，两个不同 scenario 的会话获得不同的**工具可见性**与**系统提示**（新增契约测试钉住） |
| G7 ★ | 七维场景矩阵 | 场景声明可作用于全部七个维度：技能 / 工具（allowed+hidden）/ 系统提示追加 / 审批矩阵（when 谓词收窄）/ 治理参数（max_model_calls 按场景）/ 默认模型 / 可观测（audit·usage·事件带 scenario 列）；契约测试逐维钉住 |
| G8 ★ | 同根多场景解锁 | 同一工作区的不同会话可绑定不同场景（`_preset_allows_reuse` 放宽）；同一会话内场景仍锁定（首条消息锁定语义不变，`thread_preset_locked` 错误码保留） |
| G9 ★ | 可观测按场景切片 | 审计与 usage 记录带 `scenario` 列；`/api/usage/series`、`/api/audit` 支持按场景过滤；可回答「某场景花了多少钱、审批率多高」 |
| G6 | 私有 API 归零 | 仓库内不存在对 deepagents 下划线符号的引用（现状 1 处：`agent/path_safety.py:79`） |

### 2.2 范围

**做**：

- 新增 `agent/core/` 自有内核包（协议、backend、内置工具、middleware、装配函数）
- `agent/` 既有 7 个文件的依赖改道（改 import，不改逻辑）
- `runtime/skill_view.py` 的退役、`application/event_translator.py` 的**瘦身**（M6，见 M6 说明）
- `.importlinter` 新增契约：`agent.core` 不得依赖 `deepagents`；`deepagents` 仅允许被
  `agent/adapters/` 导入
- 双跑对比工具（`scripts/diff_kernels.py`）与提示词快照测试
- ★ **场景一等能力**（v2 新增）：`Scenario` 值对象与 `preset.toml [scenario]` 段、
  三层裁决中间件（`core/scenario/`）、`when` 谓词接入 `build_interrupt_on`、
  场景化治理参数（自研 limit 中间件替换 langchain 版）、`AgentRunContext.scenario`
  贯通（`RunHandle.preset` → context）、`GET /api/scenarios`、审计/usage 加
  `scenario` 列、同根多场景解锁（`_preset_allows_reuse` 放宽）

**不做**（明确排除）：

- 不替换 langgraph / langchain（检查点、中断、HITL、中间件协议、`create_agent` 均保留；
  langchain 的中间件协议**正是**场景运行时裁决的机制载体）
- 不改对外契约的**既有部分**：REST API 既有端点、SSE 事件、DTO、错误码保持兼容
  （新增 `/api/scenarios` 端点与 audit/usage 的 `scenario` 列属于**加法演进**）
- 不动既有存储 schema 的列语义（`scenario` 列为新增可空列，历史数据兼容）
- 不做异步子 agent、rubric、langsmith、视频抽帧、file upload/download API
- **场景不突破部署档位**（D4）：`EXECUTION_MODE` 是部署上限，场景只能在其下收窄——
  需要 `execute` 的场景要求部署档位至少 `sandbox`，这是安全模型的设计而非妥协

---

## 三、现状依赖清单（迁移的完整工作面）

### 3.1 生产代码（9 个文件）

| 文件 | 依赖符号 | 迁移动作 |
| --- | --- | --- |
| `agent/graph.py:16` | `create_deep_agent` | 装配点改为按开关分发（M5），最终指向自有 `agent.core.graph.assemble_graph` |
| `agent/profiles.py:13` | `HarnessProfile` / `register_harness_profile` / `GeneralPurposeSubagentProfile` | 换自有 profile 机制（`agent/core/profiles.py`），**同时修复 provider 维度缺陷**（openai/anthropic 也获得完整提示词） |
| `agent/backends.py:13,19` | `CompositeBackend` / `StoreBackend` 等 + `BackendProtocol` / `ExecuteResponse` | 改道 `agent.core.backends`；组合逻辑（`build_backend`）不变 |
| `agent/sandbox_backend.py:22` | `LocalShellBackend` / `ExecuteResponse` | 改道；`SandboxRunner`（runtime/sandbox）对接逻辑不变 |
| `agent/readonly_mount.py:43` | `FilesystemBackend` + protocol | 改道（两个 Mount 类本身是自有实现，仅换基类引用） |
| `agent/path_safety.py:39,79` | `FilesystemBackend` + **私有** `_raise_if_symlink_loop` | 符号链接循环检测自研（M2），顺带消灭 G6 |
| `agent/guardrails.py:12` | `FilesystemPermission` / `supports_execution` / `BackendProtocol` | 权限模型自有化（M3），**恢复可执行 backend 下的权限语义**（G3） |
| `runtime/skills.py:27` | `CompositeBackend` / `FilesystemMiddleware`（用于 `inspect_skills`） | 改道；`inspect_skills` 保留为诊断工具 |
| `runtime/skill_store.py:29` | `MAX_SKILL_NAME_LENGTH` | 固化为自有常量（取值不变，兼容既有数据） |

### 3.2 测试与脚本（迁移期改道，不阻碍 M 里程碑）

`tests/agent/`（5 个文件直接 import deepagents）、`scripts/`（5 个 probe/smoke）。
处理原则：验证**行为**的断言保留（改 import 即可）；验证**上游约束**的断言
（`probe_permissions_backend.py`、`smoke_execution_modes.py` 中的上游豁免分支）在 M6 删除。

---

## 四、目标架构

### 4.1 包结构（目标态）

```
agent/
├── core/                          # ★ 自有内核：禁止 import deepagents（新 lint 契约）
│   ├── __init__.py                # 门面：仅导出 assemble_graph / AgentKernelConfig
│   ├── types.py                   # AgentRunContext（自 run_context 迁入）、内核级枚举
│   ├── protocol.py                # BackendProtocol + Result 数据类（自 deepagents 搬运裁剪）
│   ├── backends/
│   │   ├── filesystem.py          # 虚拟文件系统 backend（含 \\?\ 容错、symlink 循环检测）
│   │   ├── composite.py           # 路由：/memories/ → store，其余 → 下级
│   │   ├── store.py               # 长期记忆 backend（对接 langgraph Store）
│   │   ├── shell.py               # 可执行 backend（对接 runtime.sandbox 的三档 Runner）
│   │   └── mounts.py              # ReadOnlyFileMount / ReadOnlyDirectoryMount（自 readonly_mount 迁入）
│   ├── tools/                     # 内置工具：ls/read_file/write_file/edit_file/delete/glob/grep/execute/task/write_todos
│   ├── middleware/
│   │   ├── filesystem.py          # 工具注册 + 权限执行 + 分页格式化（对位上游 3,697 行，裁剪后约 1/3）
│   │   ├── skills.py              # 技能发现 + 渐进披露（运行时查 skill_store，G4）
│   │   ├── memory.py              # AGENTS.md 注入
│   │   ├── subagents.py           # 同步子 agent（task）
│   │   ├── limits.py              # ★ 场景感知的模型调用限额（替换 langchain ModelCallLimit，
│   │   │                          #   run_limit 运行时按场景/全局解析，G7 治理维度）
│   │   └── patch_tool_calls.py    # 工具调用修补语义
│   ├── scenario/                  # ★ 场景子包（v2 升格，G5/G7 的载体）
│   │   ├── policy.py              # Scenario 值对象：七维声明（frozen，fail-loud 校验）
│   │   ├── loader.py              # preset.toml [scenario] 段解析 + (id, dir) 进程级缓存
│   │   ├── middleware.py          # 三层裁决：wrap_model_call（工具+提示）/ wrap_tool_call（硬拦截）
│   │   └── gates.py               # when 谓词工厂：供 build_interrupt_on 编译期并集 + 运行时收窄
│   ├── graph.py                   # assemble_graph()：对位 create_deep_agent，输出同一 AgentGraph 类型
│   └── profiles.py                # 简化版 profile（base/suffix + excluded_tools，三维正交）
├── adapters/
│   └── deepagents_adapter.py      # 旧内核适配器：把 assemble_graph 的入参翻译成 create_deep_agent 调用
├── graph.py                       # 装配点：按 config.kernel 选择 core / adapter（M5）；M6 后只剩 core
└── （其余既有文件不动）
```

### 4.2 依赖方向（新增契约写进 `.importlinter`）

```
agent.core   → langgraph / langchain / config（不得 → deepagents、application、bootstrap）
agent.adapters → agent.core + deepagents（唯一允许 import deepagents 的包）
agent        → agent.core
runtime      → agent.core（skills.py、skill_store.py 的引用改道）
```

### 4.3 刻意保留的等价物

- `AgentRunContext`（`frozen dataclass`）形状**仅做加法扩展**：新增 `scenario: str = ""`
  字段（阶段 1 已定，默认值保证向后兼容）；`user_id`/`workspace` 语义不变——记忆
  命名空间与知识库路由不受影响，且 `scenario` **不参与**记忆命名空间（记忆是人的
  偏好，跨场景有效）；
- `AgentGraph = CompiledStateGraph[Any, AgentRunContext, Any, Any]` 类型别名不变——
  `application/run_service.py` 的驱动代码零改动；
- 内置工具名与参数 schema 逐字保持——模型对工具的"手感"不变，提示词快照测试钉住。

---

## 五、里程碑

> 每个里程碑独立可发布、可回退。回退方式统一为：装配开关拨回 `adapter`，无数据迁移回滚
> （M6 之前不删任何表、不删任何目录）。

### M0 —— 依赖收敛（预置步，独立价值）

**目标**：全仓库对 deepagents 的 import 从 24 处收敛到 1 处门面模块，为后续替换建立唯一改道点。

**改动**：
- 新增 `agent/upstream.py`：re-export 本项目用到的全部 deepagents 符号
  （`create_deep_agent`、`BackendProtocol`、`ExecuteResponse`、`FilesystemPermission`、
  `supports_execution`、`FilesystemBackend`、`CompositeBackend`、`StoreBackend`、
  `LocalShellBackend`、`HarnessProfile` 三件套、`MAX_SKILL_NAME_LENGTH`、
  `_raise_if_symlink_loop` 私有函数单独封装成具名函数 `raise_if_symlink_loop`）；
- 9 个生产文件 + 5 个测试文件 + 5 个脚本的 import 改道；
- `.importlinter` 加契约：deepagents 仅允许被 `agent.upstream` 导入。

**验收**：G1 前半（仅 1 个文件 import deepagents）；`uv run lint-imports` + 全量 pytest 通过。

**回退**：无风险（纯 import 改道，行为零变化）。

**独立价值**：即使迁移中止，私有 API 依赖已被隔离到一个函数、后续 deepagents 升级的
爆炸半径从 9 个文件缩到 1 个。

### M1 —— 侦查确认 + 边界类型 + 双跑骨架

**目标**：确认 deepagents 默认中间件栈的完整清单（含 langchain `create_agent` 的
base stack 是否含 summarization）；冻结自有内核的协议类型；建立双跑对比工具。

**改动**：
- 侦查脚本固化（`scripts/recon_deepagents.py` 扩展：dump 默认栈、dump 最终系统提示词全文）；
  **确认 SummarizationMiddleware 是否在默认链路中**——本方案的裁剪表据此修正；
- 新增 `agent/core/protocol.py`：从 `deepagents/backends/protocol.py` **复制**全部
  Result 数据类与校验逻辑（`ReadResult.__post_init__` 的窗口一致性检查逐行保留），
  裁掉 upload/download 相关类型；
- 新增 `agent/core/types.py`：内核配置值对象（对位 `create_deep_agent` 的 12 个入参，
  外加 `scenario: str`）；
- 新增 `scripts/diff_kernels.py`：同一输入分别经 core 与 adapter 跑完一轮，
  diff 事件流（token/tool_call/tool_result/usage/done）与产物文件 hash；
- 新增提示词快照测试：固定输入下 dump 两内核的最终 SystemMessage，diff 必须为空
  （core 未接管前以 adapter 为基准建立快照）。

**验收**：快照测试建立并绿；`diff_kernels.py` 在 3 条固定场景（纯问答 / 文件编辑 /
execute+审批）上输出"两内核事件流一致"。

### M2 —— backend 层自有化

**目标**：`agent/core/backends/` 达到生产可用，`agent/backends.py` 组合逻辑改道。

**改动**（按依赖序）：
1. `filesystem.py`：虚拟路径 ↔ 宿主路径映射。**必须原样搬运**的三块资产：
   `path_safety.py` 的 `\\?\` 扩展前缀容错（15 个既有用例钉住）、符号链接循环检测
   （自实现 `raise_if_symlink_loop`，G6）、`.harness/` 只读兜底路由语义；
2. `store.py`：对接 `langgraph` Store，命名空间工厂沿用 `agent.run_context.namespace_of_runtime`；
3. `composite.py`：两条路由（`/memories/` 前缀 → store，其余 → 下级），约 200 行；
4. `shell.py`：`execute` 委托 `runtime/sandbox` 的 `SandboxRunner`（三档逻辑零改动，
   只换协议基类）；`ExecuteResponse` 搬运；
5. `mounts.py`：`ReadOnlyFileMount` / `ReadOnlyDirectoryMount` 自 `readonly_mount.py` 迁入
   （这两个类本来就是自有实现，改基类 import 即可）；
6. `agent/backends.py` 的 `build_backend` 改道 core，开关拨向 core 跑回归。

**验收**：`tests/agent/` 中 backend 相关用例（`test_path_safety.py` 15 例、
`test_permissions_backend.py`、`test_memory_namespace.py`、`test_memory_end_to_end.py`）
在 core 内核下全绿；`smoke_sandbox.py` / `smoke_docker_sandbox.py` 通过；
`diff_kernels.py` 文件类场景一致。

**回退**：开关拨回 adapter。

### M3 —— 文件工具与权限（工作量最大的一个里程碑）

**目标**：`agent/core/tools/` + `middleware/filesystem.py` 上线；**G3 达成**。

**改动**：
- 10 个内置工具等价实现（schema 与输出格式逐字对齐既有快照）；
  其中 `execute` 直接复用 `shell.py`；`task` 在 M3 先以"占位拒绝 + 明确错误文案"上线，
  M4 补齐（避免一个里程碑横跨工具与子 agent 两个风险面）；
- 权限模型：`FilesystemPermission` 语义自有化——**规则检查放在工具分发路径上**，
  与 backend 是否具备执行能力**解耦**（这正是被上游 0.7.14 砍掉、本方案要恢复的）；
  `build_permissions` 不再需要 `backend` 参数与"可执行即返回空规则"的裁剪分支；
- `interrupt_on` 语义对齐：`execute` / `delete` 的审批配置沿用 `build_interrupt_on` 现有矩阵；
- 大结果落盘（`runtime/tool_outputs.py`）与 `/.tool_outputs` 回取挂载语义保持不变。

**验收**：G3 判定通过（`local` 档位 `read_file .env` 被权限拒绝，且日志不再是
"权限已停用"WARNING）；`smoke_execution_modes.py` 按新安全口径更新断言并通过；
工具调用事件流与 adapter 双跑 diff 为空。

**风险缓解**：`filesystem.py` 上游 3,697 行是本项目最大的单点等价物。缓解：
只实现被用到的分支（分页读取 + 行号 gutter + 长行拆分 `5.1/5.2` + edit_file 的
字符串替换语义），upload/download/视频/沙箱路径换算一律不实现；
`tests/agent/test_tool_registry.py`（26 例）与 `tests/test_web_tools.py` 的工具
schema 断言作为等价判据。

### M4 —— 技能 / 记忆 / 装配函数 / 子 agent

**目标**：`assemble_graph()` 完整对位 `create_deep_agent`；G4 达成。

**改动**：
- `middleware/skills.py`：技能发现与渐进披露。**运行时查 `skill_store`**（每次
  `wrap_model_call` 决定注入哪些技能），`.skills-active` 物化视图**不再参与图装配**
  （目录与重建逻辑保留到 M6 才删，保证可回退）；
- `middleware/memory.py`：`memory_plan.sources` 注入语义对齐（全局只读挂载 / 工作区回落）；
- `middleware/subagents.py`：同步子 agent（`task`），上下文隔离边界对齐上游
  （子 agent 独立消息列表 + 共享 backend + 独立 tool 预算）；
- `middleware/patch_tool_calls.py`：搬运（52 行，语义简单）；
- `core/profiles.py`：简化 profile（`base_system_prompt` / `suffix` / `excluded_tools`），
  **为全部已注册 provider 注册**，同时消解 `USER/BASE/SUFFIX` 三段重复
  （`_FALLBACK_SYSTEM_PROMPT` 与 `_BASE_SYSTEM_PROMPT` 合并为单一基线）；
- `core/graph.py`：`assemble_graph(config, scope, checkpointer, store, model_name, tools)`
  ——组装顺序：PatchToolCalls → Skills → Filesystem(+权限) → SubAgent → Memory →
  HITL(langchain, when 谓词已注入) → Scenario → 用户 middleware（错误收敛/Todo/ContextEditing）；
- ★ `core/scenario/`（G5/G7 的载体，v2 从"喂空集"升格为完整实现）：
  - `policy.py` + `loader.py`：`Scenario` 七维值对象与解析（fail-loud：工具名不存在、
    模型别名不存在、声明超出部署档位的能力 → 装配期报错）；
  - `middleware.py`：三层裁决的前两层（`wrap_model_call` 裁工具/重写提示 +
    `wrap_tool_call` 硬拦截），提示追加做确定性序列化（R9）；
  - `gates.py`：`when` 谓词工厂——`build_interrupt_on` 编译期取全场景审批并集，
    运行时按 `AgentRunContext.scenario` 收窄（G7 护栏维度）；
  - 场景专属工具注册协议：场景声明的外部工具经既有 `register_tools(registry, config)`
    SPI 进程级注册，MCP 场景专属 server 惰性连接 + 空闲回收（复用
    `embed_process` 的空闲回收模式）；
- ★ `core/middleware/limits.py`：自研模型调用限额中间件替换 langchain
  `ModelCallLimitMiddleware`（行为兼容），`run_limit` 运行时按「场景声明 → 全局」
  解析（G7 治理维度）——langchain 版的 limit 是构造期常量，无法场景化。

**验收**：G4 判定通过（启停技能后**不**调用 `refresh_view`、不重建图，
下一轮对话技能清单即变化——新增契约测试钉住）；
G5 判定通过（同图缓存下两个 scenario 的工具可见性与系统提示不同——契约测试钉住）；
G7 的五维判定通过（工具/提示/护栏/治理/模型——逐维契约测试）；
`tests/agent/` 全量绿；提示词快照更新（基线合并后快照有意变化一次，diff 审查后重录）。

### M5 —— 双内核切流

**目标**：生产流量切到 core；adapter 保留为回退通道。

**改动**：
- `config/settings/execution.py` 新增 `AGENT_KERNEL: Literal["deepagents", "core"]`
  （默认 `deepagents`，灰度期逐环境拨到 `core`）；
- `agent/graph.py:204` 的调用点改为按开关分发（两分支签名一致，返回同一 `AgentGraph`）；
- `bootstrap/core.py` 零改动（装配产物类型不变，这是 M0-M4 约束的兑现）；
- 观测：`usage` 与审计记录加 `kernel` 字段（可空），灰度期对比两内核的
  token 用量偏差与工具错误率；
- ★ `GET /api/scenarios`：列出可用场景及七维能力概览（技能数、工具增删、
  是否需要审批、默认模型、治理参数），前端建会话选场景时展示；
- ★ `GET /api/tools` 支持 `?scenario=` 过滤（场景内可见工具清单）；
- ★ `audit` 与 `usage` 表新增可空列 `scenario`（`AgentRunContext.scenario` 贯通，
  加法演进，历史数据兼容），`/api/audit` 与 `/api/usage/series` 支持 `scenario`
  过滤参数（G9）。

**验收**：`AGENT_KERNEL=core` 下全量 pytest + 全部 smoke 脚本绿；
CLI 与 Web 各跑 3 条真实任务的人工验收清单（对话续聊 / HITL 四种决策 / 会话分叉 /
长期记忆读写）逐条通过；G5/G7 判定通过；
★ G9 判定通过（同场景两条会话的 usage 按场景聚合出正数、audit 可过滤）；
★ `/api/scenarios` 对三场景（常规/coding/新报告场景）返回正确的七维概览。

**回退**：拨回 `deepagents`，即时生效（图缓存键不变，首次切换后按 `drop_workspace` 清一次）。

### M6 —— 退役与清理

**目标**：deepagents 出库；收益兑现。

**改动（删除清单）**：
- `pyproject.toml` 移除 `deepagents` 依赖；
- `agent/adapters/`、`agent/upstream.py` 删除；
- `runtime/skill_view.py`（226 行）、`SessionRoot.skill_view_store`、
  `.skills-active` 目录与重建调用链（`SessionRegistry._assemble` 的 `refresh_view` 调用、
  `SkillService.refresh_view` 及其 11 处测试）删除；
- `application/event_translator.py`（294 行）**瘦身而非删除**。WHY：translator 翻译的是
  **langgraph `astream` 的事件形状**，不是 deepagents 的——deepagents 图同样是 langgraph
  图，事件格式与内核无关，换内核后 `RunService._stream_graph` 的事件来源不变，translator
  的存在理由不消失。core 能做的是把**自己可控的 payload**（todo 快照、子 agent 事件、
  工具结果）由中间件直接产出结构化数据，使 translator 的猜测与修补逻辑缩减
  （预期 294 行 → 100 行上下，DTO 与 SSE 编码不变）。**彻底删除需改造
  `RunService._stream_graph` 的驱动方式**（弃 `astream` 事件、由自建事件通道直出
  `AgentEvent`），涉及 token 流式搭桥，风险独立成篇，另立方案；
- `scripts/probe_permissions_backend.py`（151 行）删除；
  `smoke_execution_modes.py` 移除上游豁免分支；
- `AgentFactory.drop_workspace()` 删除（无生产调用点，技能/场景已运行时解析）；
- `.importlinter` 中 deepagents 相关契约改为"禁止全仓 import"；
- ★ `application/session_registry.py` 的 `_preset_allows_reuse` 放宽：同一工作区的
  不同会话允许绑定不同场景（技能视图已消亡、图缓存键不含场景、场景经运行时裁决，
  原「同根单场景」锁的存在理由消失）；同一会话内中途换场景仍拒绝
  （`thread_preset_locked` 错误码与语义保留，仅范围收窄到会话级）——G8 达成；
- README 更新：执行档位权限矩阵（G3 后 `local`/`sandbox` 恢复工具级权限）、
  技能启停语义（即时生效）、**场景章节重写**（七维矩阵、部署上限语义、
  同根多场景、`/api/scenarios`）、依赖清单。

**验收**：G1 完整达成（仓库内零 deepagents 引用）；`uv run lint-imports` +
全量 pytest + smoke 绿；G3/G4/G5/G7 复验通过；
★ G8 判定通过（同工作区两条会话分别绑定不同场景，各自的工具可见性与提示不同，
  且互不串味——契约测试钉住）；★ G9 复验通过。

---

## 六、功能裁剪决策表

| deepagents 组件 | 行数 | 本项目使用 | 决策 |
| --- | --- | --- | --- |
| `graph.py` create_deep_agent | 980 | 唯一入口 | 等价自研 `core/graph.py` |
| `backends/protocol.py` | 984 | 6 文件依赖 | **搬运**协议 + Result 数据类（裁掉 upload/download） |
| `backends/filesystem.py` | 1576 | 间接 | 等价实现（去掉跨沙箱路径换算） |
| `backends/composite.py` | 1019 | `/memories` 路由 | 等价实现（两路由，约 200 行） |
| `backends/store.py` | 719 | 长期记忆 | 等价实现 |
| `backends/utils.py` | 1231 | 部分 | 按需搬（`normalize_read_bounds` 等） |
| `backends/local_shell.py` | 366 | 已包装使用 | 等价实现（execute 委托 `runtime/sandbox`） |
| `backends/state.py` | 374 | 间接 | 按需搬 |
| `backends/sandbox.py` | 1989 | ✗（自有三档沙箱） | **放弃** |
| `backends/context_hub.py` | 713 | ✗ | **放弃** |
| `backends/langsmith.py` | 353 | ✗ | **放弃** |
| `middleware/filesystem.py` | 3697 | 核心 | 等价实现（裁剪后约 1/3） |
| `middleware/skills.py` | 1074 | 核心（+被迫物化视图） | 等价 + **运行时化**（消灭 skill_view） |
| `middleware/memory.py` | 417 | 核心 | 等价实现 |
| `middleware/subagents.py` | 992 | `task` 工具 | 等价实现（同步） |
| `middleware/async_subagents.py` | 931 | ✗ | **放弃** |
| `middleware/summarization.py` | 2288 | ✗（栈中用的是 langchain 版） | **放弃**（M1 侦查最终确认） |
| `middleware/rubric.py` | 1438 | ✗ | **放弃** |
| `middleware/patch_tool_calls.py` | 52 | 间接 | 搬运 |
| `profiles/`（含 nvidia 1857 行） | ~3500 | 仅 HarnessProfile 三符号 | 自有简化 profile，厂商内置全弃 |
| `_models.py` / `_tools.py` / `_excluded_middleware.py` 等 | ~600 | 间接 | 按需 |

**裁剪后需等价实现/搬运的净规模约 9,000–10,000 行**（上游），对应自有实现预计 5,000–6,000 行。

---

## 七、行为等价性验收策略

| 层 | 判据 | 说明 |
| --- | --- | --- |
| 单元回归 | 现有 ~1300 测试 | `tests/agent/` 133 例为主要等价面；协议类测试逐个列豁免清单 |
| 冒烟 | `smoke_execution_modes` / `smoke_sandbox` / `smoke_docker_sandbox` / `smoke_web_chain` / `smoke_knowledge` | 全部脚本在 core 下复跑 |
| 双跑对比 | `scripts/diff_kernels.py` | 事件流 + 产物 hash 逐项 diff；固定 3+ 场景进 CI |
| 提示词快照 | SystemMessage 快照测试 | 允许**有记录的**一次变化（M4 基线合并），其余必须零 diff |
| 人工验收 | CLI/Web 各 3 条真实任务 | 含 HITL 四种决策、会话分叉、长期记忆读写、附件上传 |
| 观测对比 | usage/审计的 `kernel` 字段 | 灰度期 token 偏差 < 2%、工具错误率无恶化 |

---

## 八、风险登记簿

| # | 风险 | 等级 | 缓解 |
| --- | --- | --- | --- |
| R1 | 防御性逻辑遗漏 → 静默分页/截断 bug | **高** | 协议数据类**逐行搬运**而非重写；`ReadResult.__post_init__` 类校验保留；双跑 diff 钉住 |
| R2 | Windows 特有修复丢失（`\\?\` 前缀、并行写竞态） | **高** | `test_path_safety.py` 15 例先行、迁移中始终全绿；这三块代码**搬运不重写** |
| R3 | 迁移中途放弃留下半成品 | 中 | 绞杀者模式：任一 M 结束系统均可运行，开关一键回退 |
| R4 | 技能名长度规则变化破坏既有数据 | 中 | `MAX_SKILL_NAME_LENGTH` 取值固化不变；`inspect_skills` 的校验差集报告保留 |
| R5 | langchain 版本联动（AgentMiddleware 协议演进） | 中 | 保留范围声明（第二节）；`langchain==1.4.2` 钉住不动；升级另立方案 |
| R6 | 提示词变化引发模型行为漂移 | 中 | 快照测试 + 变化必须显式审查重录；M4 合并基线时人工回归 3 条任务 |
| R7 | filesystem middleware 等价面超预期膨胀 | 中 | 裁剪表是硬边界：未列出的分支一律不实现，遇到需求先加进裁剪表再动手 |
| R8 | 灰度期两内核数据互写（检查点兼容） | 低 | 图状态 schema 不变（`DeepAgentState` 的字段以 langgraph 通道承载，core 沿用同名通道）；灰度期禁止单会话跨内核续跑（`kernel` 字段检测，发现即 409 提示新建会话） |
| R9 | 运行时动态注入破坏 prompt 前缀缓存 | 中 | 技能/场景清单变化会使系统提示前缀失配，DeepSeek 缓存命中率短期下降（原「编译期烧死」对缓存反而更友好，此为运行时化的固有代价）。缓解：注入内容做「清单不变则字节不变」的确定性序列化；启停/场景切换属低频事件；命中率已可观测（`prompt_cache_hit_tokens`），灰度期纳入监控指标 |
| R10 ★ | 场景配置错误在运行期才暴露 | 中 | 解析 fail-loud 且前置：装配期校验工具名存在性、模型别名存在性、能力不超部署档位；`loader` 缓存键含 presets_dir，测试与部署隔离；未知场景 ID（历史会话残留）按「不限定」降级并记 WARNING，与会话根对已删除场景的口径一致 |
| R11 ★ | 三层裁决的口径漂移（可见性 / 拦截 / 审批不一致） | 中 | 单一事实来源：三层共用 `ScenarioPolicy.visible()` 同一判定函数；契约测试以矩阵方式逐场景 × 逐工具断言三层行为一致（可见 ⇒ 可调 ⇒ 审批按声明）；`/api/tools?scenario=` 展示值与裁决值同源 |

---

## 九、明确不做的事

- 不重写 `deepagents` 未被使用的子系统（见裁剪表），也不为"以后可能用到"预留实现；
- **场景不突破部署档位**（D4，v2 由「排除条款」改写为「设计声明」）：`EXECUTION_MODE`
  是部署上限，场景运行时只能收窄不能放宽——这不是未实现的功能，是安全模型；
  需要 `execute` 的场景要求部署档位至少 `sandbox`；
- **场景差异不进图缓存键**（D4）：不做「每场景一张图」，场景专属资源的隔离由
  三层裁决 + MCP 惰性连接承担；进程级物理隔离属部署维度，另立方案；
- 不改对外契约的既有部分：REST API 既有端点、SSE 事件、DTO、错误码保持兼容；
  新增端点与新增可空列属于加法演进，不属于「改契约」；
- 不引入新的三方依赖（langgraph / langchain 既有版本钉死不动）。

---

## 附录 A：deepagents 0.7.17 包规模侦查（`scripts/recon_deepagents.py` 产出）

```
graph.py 980 | backends 合计 9,324（protocol 984 / filesystem 1576 / composite 1019 /
  utils 1231 / sandbox 1989 / store 719 / state 374 / context_hub 713 / langsmith 353 /
  local_shell 366）| middleware 合计 ~12,000（filesystem 3697 / summarization 2288 /
  rubric 1438 / subagents 992 / async_subagents 931 / skills 1074 / memory 417 / 其余 ~600）|
profiles 合计 ~3,500（nvidia nemotron 单文件 1,857）
```

中间件组装顺序（graph.py:685-938）：`PatchToolCalls → Skills(if skills) →
Filesystem → SubAgent → AsyncSubAgent(if) → Memory(if memory) →
HumanInTheLoop(langchain) → ToolExclusion(profile)` → 调用方 middleware。

> 附注：`HumanInTheLoopMiddleware`、`TodoListMiddleware`、`ContextEditingMiddleware`、
> `ModelCallLimitMiddleware` 均来自 `langchain.agents.middleware`——**不在替换范围**。
> 本方案替换的只是"deepagents 自有"的那部分：backend 层、文件工具、技能、记忆、
> 同步子 agent、装配函数与 profile。
