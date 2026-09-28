# CLI 能力补齐落地方案

> 文档定位：本文件是 `interfaces/cli` 从「进程内交互适配器」演进为「可被脚本调用的
> Unix 工具」的实施规格。它描述目标、分层、文件组织、接口约定、数据流转与异常/日志
> 策略，落盘后的目录树见附录 A。
>
> 与既有文档的关系：架构总览仍是 `docs/architecture.html`（叙述型）与
> `docs/architecture-diagrams.html`（图形型），本文件**不追加、不覆盖**那两份，
> 只补它们没有覆盖的「CLI 形态的落地规格」。
>
> 命名与风格沿用仓库既有约定（模块 docstring 解释 WHY 而非 WHAT、根级文档无空格中文名）。

---

## 一、目标与范围

### 1.1 整体目标

让同一条 CLI 既能被人交互使用，也能被脚本、CI 与上层编排程序调用，且两种形态共享
同一套内核（`bootstrap` 装配 + `application` 服务），不出现「Web 有的能力 CLI 没有」
的分叉。

拆成三条可判定的目标：

| 编号 | 目标 | 判定方式 |
| --- | --- | --- |
| G1 | **可编排**：CLI 能被非交互调用并产出机器可读结果 | `main.py cli -p "..." --output-format json` 的输出可被 `json.load` 直接解析 |
| G2 | **可续接**：一条 CLI 会话能恢复、能列出、能导出 | `--resume <thread_id>` 能接着既有会话跑；`/threads` 能列出会话 |
| G3 | **可约束**：脚本化调用能自我限制权限与轮次 | `--permission-mode`、`--max-turns` 生效且可被测试断言 |

**现状对照**：`application` 层已具备 G2 所需的全部能力（`ThreadService.list_threads`
`application/thread_service.py:240`、`ThreadService.history` `:299`、
`ThreadService.export_thread` `:536`），CLI 侧一个入口都没接。G2 是「暴露缺口」而非
「能力缺口」，成本最低、收益最高。

### 1.2 范围

**做**：

- `interfaces/cli.py` 拆分为 `interfaces/cli/` 包（11 个模块，见 4.3）
- `main.py` 的 CLI 参数定义下沉，`main.py` 只保留进程级分发与日志装配
- `application` 侧**参数贯通**：为 `RunService.stream` / `RunService.resume` 增加
  每次运行的可选策略参数（写入既有 `application/dto.py`，不新增模块）
- `agent` 侧在最后一个里程碑引入工具白名单（需扩展图缓存键，见 M5）
- 契约测试与端到端冒烟（`tests/interfaces/cli/`、`scripts/smoke_cli_headless.py`）
- README「启动命令」章节与本文档同步

**不做**（明确排除，避免范围蔓延）：

- 全屏 TUI（不引入 `textual` 一类框架；仅做行式 REPL + 状态行）
- Web 适配器与前端（`interfaces/web/**`、`interfaces/web/static/**`）行为不变
- 存储 schema 变更（不改 `runtime/thread_store.py` 等任何表结构）
- 沙箱档位机制改造（`runtime/sandbox/**` 不动）
- 认证鉴权（该子系统已在 `ce965d0` 移除，不回填）
- 新增分层、新增根级模块、新增 `application` 顶层模块（见 3.4 的硬约束）

### 1.3 与上一轮差距分析的追溯

上一轮列出的 P0/P1/P2 与里程碑的对应关系，作为需求覆盖度的自查表：

| 上一轮条目 | 优先级 | 落位里程碑 |
| --- | --- | --- |
| 非交互一次性执行（`-p/--print`） | P0 | M1 |
| 机器可读输出（`--output-format`） | P0 | M1 |
| 会话恢复（`--resume` / `--continue`） | P0 | M2 |
| 权限模式与工具开关 | P0 | M3（审批侧）/ M5（工具侧） |
| REPL 斜杠命令体系 | P1 | M4 |
| 场景预设 `preset` 透传 | P1 | M2 |
| 中断语义分层 + 走 `RunService.stop` | P1 | M3 |
| 审批记忆（会话内 always allow） | P1 | M3 |
| 终端渲染与 `NO_COLOR`、编码 | P1 | M4 |
| readline 历史 / 补全 | P1 | M4 |
| `--max-turns` 等运行控制 | P1 | M2 |
| `--version` 与 `[project.scripts]` | P1 | M4 |
| `@file` 引用 / 附件 / `!shell` | P2 | 不在本次范围（另立方案） |
| `mcp` / `config` 子命令 | P2 | 不在本次范围（另立方案） |
| 成本估算 | P2 | 不在本次范围（需先有价格表数据源） |

---

## 二、关键里程碑

### 2.1 里程碑总览

每个里程碑都是**可独立合并、可独立回滚**的增量；M0 是纯重构（对外行为零变化），
后续里程碑才引入新行为。

| 里程碑 | 主题 | 交付物 | 验收（全部通过才算完成） | 依赖 |
| --- | --- | --- | --- | --- |
| **M0** | 包化重构与结构契约 | `interfaces/cli/` 包；`tests/interfaces/cli/` 骨架；`test_structure_contract.py` | `uv run pytest` 全绿；`uv run lint-imports` 全绿；`main.py cli` 行为与重构前逐字一致（既有 600 行 CLI 测试原样通过） | — |
| **M1** | 可编排：headless + 输出协议 | `headless.py`、`output.py`、`terminal.py`；`options.py` 新增 `-p` / `--output-format` | `-p` 单次执行返回后进程退出；三种输出格式各有契约测试；stdout 只含结果、stderr 只含诊断 | M0 |
| **M2** | 会话生命周期 | `session.py`；`options.py` 新增 `--resume` / `--continue` / `--max-turns` / `--preset`；命令 `/threads` `/export` `/cost` | 能恢复既有 thread_id 继续对话；`--max-turns` 生效；`--preset` 与 Web 行为一致（锁定语义同源） | M1 |
| **M3** | 审批策略与中断语义 | `approval.py` 扩展；`options.py` 新增 `--permission-mode`；`runner.py` 接入 `RunService.stop` | Ctrl+C 第一次中止本轮并保留会话、第二次退出；会话级 always-allow 生效；退出码符合 7.1 表 | M1 |
| **M4** | 交互体验 | `commands.py`、`render.py` 增强；`[project.scripts]` 与 `--version` | 斜杠命令表全部可用；`NO_COLOR` 与非 TTY 降级有测试；`harness --version` 可执行 | M2、M3 |
| **M5** | 工具策略贯通（高风险） | `agent/graph.py` 与 `AgentFactory` 缓存键扩展；`application/dto.py::RunPolicy` 生效 | `--allow-tools` / `--deny-tools` 真正限制图内工具；缓存键含策略维度且命中率有实测记录 | M2 |

**里程碑之间的顺序理由**：M0 必须先做——包化是一切后续拆分的地基，且必须在不改行为的
前提下把既有测试当回归网。M1 先于 M2，因为 `--output-format` 的写出抽象（`EventSink`）
是 headless 与 REPL 的共同下游；先定抽象再挂会话功能，避免两处各写一份输出逻辑。
M5 放最后，因为它是唯一需要动内核装配与缓存键的里程碑，风险与验证成本最高，且
M3 的「审批策略」已能在不改内核的前提下覆盖大部分 `--permission-mode` 诉求（见 3.4）。

### 2.2 任务分解（按层落位）

**M0 — 包化重构**

| 任务 | 落位文件 | 说明 |
| --- | --- | --- |
| 建立包骨架 | `interfaces/cli/__init__.py` | 对外再导出 `run_cli` / `main_sync`，保证 `from interfaces.cli import run_cli` 不破 |
| 迁移事件渲染 | `interfaces/cli/render.py` | 原 `render_event` / `_render_todos` / `_format_args`（`interfaces/cli.py:29-84`） |
| 迁移审批 | `interfaces/cli/approval.py` | 原 `ask_human` / `_allowed_decisions`（`interfaces/cli.py:87-144`） |
| 迁移主循环 | `interfaces/cli/repl.py` | 原主循环体（`interfaces/cli.py:246-267`） |
| 迁移编排 | `interfaces/cli/runner.py` | 原 `run_cli` / `main_sync`（`interfaces/cli.py:196-285`） |
| 迁移参数定义 | `interfaces/cli/options.py` | 原 `_build_parser` 的 `cli` 部分（`main.py:93-102`） |
| 迁移测试 | `tests/interfaces/cli/*.py` | 按被测模块拆，`tests/interfaces/test_cli.py` 删除 |
| 新增结构契约 | `tests/interfaces/cli/test_structure_contract.py` | 把 5.2 的单向依赖变成 CI 断言 |

**M1 — headless 与输出协议**：`options.py` 增加 `-p/--print`、`--output-format`；
`output.py` 实现 4 个 sink；`terminal.py` 实现能力探测；`headless.py` 实现单次执行与
退出码；`main.py` 的 `--log-level` 用 `argparse.SUPPRESS` 双点声明以免覆盖根级取值。

**M2 — 会话生命周期**：`session.py` 实现 `resolve_binding()`；`options.py` 增加
`--resume` / `--continue` / `--max-turns` / `--preset`；`application/dto.py` 增加
`RunPolicy`，`application/run_service.py` 的 `stream`/`resume` 增加 `policy` 形参；
`application/runnable.py::build_runnable_config` 支持按次覆盖 `recursion_limit`。

**M3 — 审批策略与中断语义**：`approval.py` 增加 `ApprovalPolicy`（会话级 always-allow
与 `--permission-mode` 的映射）；`repl.py` 与 `runner.py` 接入 `RunService.stop`
（`application/run_service.py:629`）；两级 Ctrl+C 语义。

**M4 — 交互体验**：`commands.py` 命令注册表；`render.py` 增加 Markdown 轻渲染与耗时；
`pyproject.toml` 补 `[build-system]` 与 `[project.scripts]`；`--version` 由
`importlib.metadata` 读取并回退解析 `pyproject.toml`（`tomllib`，标准库）。

**M5 — 工具策略贯通**：`agent/graph.py::build_agent`（`:81`）接收工具过滤参数并传给
装配；`AgentFactory`（`:222`）缓存键加入策略维度；`application/dto.py` 的 `RunPolicy`
经 `run_service` 透传到图构建。

---

## 三、分层与模块拆分

### 3.1 本仓库既有分层（不可新增层）

六个包 + 八个根级模块已经由 `.importlinter` 与 `tests/test_root_module_contract.py`
固化为可执行规则，**本方案不新增任何一层，也不新增任何根级模块**：

```
interfaces  →  application  →  agent / runtime
interfaces  →  bootstrap（适配器调用组装点，取已装配对象）
llm 不依赖 agent；runtime 是叶子；bootstrap 不被下层依赖
根级：config / text_utils / thread_utils / web_safety（中立叶子）
      knowledge_runtime（服务句柄） / web_tools / knowledge_tools（工具插件）
      main（入口分发）
```

### 3.2 需求层名 → 仓库层的映射

需求里给出的通用层名，在本仓库的落位如下。新代码**只落在前两行**，其余层只做参数贯通。

| 通用层名 | 本仓库对应 | 目录 / 模块 | 本方案是否新增代码 |
| --- | --- | --- | --- |
| 入口层 | 接口层 + 入口分发 | `interfaces/cli/**`、`main.py` | 是（主要工作量） |
| 业务逻辑层 | 应用层 | `application/**` | 是（仅签名与 DTO 扩展） |
| 领域内核层 | 内核层 | `agent/**` | 是（仅 M5） |
| 数据处理层 | 运行时层 | `runtime/**` | 否 |
| 模型访问层 | 模型层 | `llm/**` | 否 |
| 公共 / 工具层 | 根级中立叶子 + 工具插件 | `config.py`、`text_utils.py`、`thread_utils.py`、`web_safety.py` | 否 |
| 配置层 | 配置 | `config.py`、`.env.example` | 否（复用既有键） |
| 测试层 | 测试 | `tests/**` | 是（新增 `tests/interfaces/cli/`） |

### 3.3 各层职责边界与依赖关系

| 层 | 职责边界（做） | 明确不做（越界判定） | 依赖 |
| --- | --- | --- | --- |
| 入口层 `interfaces/cli` | 参数解析、终端读写、会话绑定展示、审批交互、事件渲染与序列化、退出码 | 不持有 Agent 逻辑；不直接 import `runtime`（契约 1）；不自己装配依赖（经 `bootstrap`） | `application`、`bootstrap`、根级 `config` |
| 业务逻辑层 `application` | 运行推进、会话与分支、审批编解码、用量聚合、策略校验 | 不知道"终端"的存在；不 import `interfaces` / `bootstrap`（契约 4） | `agent`、`runtime`、根级中立模块 |
| 领域内核层 `agent` | 图装配、工具集、护栏与中断配置 | 不 import `application` / `interfaces` / `bootstrap`（契约 5） | `runtime`、`llm` |
| 数据处理层 `runtime` | 检查点、各类 store、文件与沙箱 | 不依赖任何上层（契约 3） | 根级中立模块 |
| 装配层 `bootstrap` | 唯一装配点，产出 `AppContext` | 不被业务层依赖（契约 6） | 全部下层 |
| 配置层 `config.py` | 配置模型与派生路径 | 不依赖仓库内任何模块 | 无 |
| 测试层 `tests/` | 按被测层镜像组织 | 不参与运行期装配（契约扫描显式排除） | 被测对象 |

**依赖方向为什么必须是这个方向**：CLI 需要`RunService`、`ThreadService`、`UsageService`
这些业务能力，也需要读到「这条会话的根在哪」这类装配事实。前者走 `application`，后者走
`bootstrap.context.AppContext`。一旦 CLI 为图省事直接 import `runtime.thread_store`，
存储实现的更换就会波及接口层——这正是 2026-09 分层重构（13 处越界）要消灭的耦合。

### 3.4 拆分依据与三条硬约束

**拆分依据**（每一条都对应一个「不拆会出问题」的具体场景）：

1. **按"变化理由"拆，不按"代码位置"拆**。参数定义的变化理由是「命令行契约变了」，
   渲染的变化理由是「终端表现变了」，重试与退出的变化理由是「生命周期规则变了」——
   三者变化频率与责任人不同，混在一个 `cli.py` 里会让每次小改动都触碰同一个文件。
2. **把"可测的纯函数"从"不可测的 IO"里剥出来**。`render.py` / `output.py` / `options.py`
   全部是「输入事件或参数 → 输出字符串或 DTO」的纯逻辑，可零替身测试；
   `repl.py` / `approval.py` / `runner.py` 才需要替身 `input()` 与假事件流。
   现状 `interfaces/cli.py` 一个文件同时承载两者（223 语句、渲染与 IO 交错）。
3. **输出协议独立成模块**，因为它是 headless 与 REPL 的**共同下游**：两种形态都要把
   `AgentEvent` 变成字节。若不抽出，`--output-format` 会在两处各实现一遍，随后
   「REPL 的 json 与 headless 的 json 字段不一致」将是必然。

**三条硬约束**（违反任一条会直接让 CI 变红，或引入架构倒退）：

| 约束 | 原因 | 违反后的症状 |
| --- | --- | --- |
| 不新增 `application` 顶层模块 | `tests/application/test_layer_purity_contract.py:44-83` 的分组表必须与包内实际模块**逐项相等**，且 `application/__init__.py` 的 docstring 必须点名每个模块 | 新增模块即触发 `test_role_table_covers_every_application_module` 与 `test_module_docstring_names_every_module` 双失败 |
| 不新增根级模块 | `tests/test_root_module_contract.py:46-77` 的 `_BOUNDARIES` 要求每个根级模块登记角色，且 `:190` 断言不存在未声明角色的模块 | 新增 `.py` 到仓库根即触发 `test_every_root_module_declares_a_role` 失败 |
| `.importlinter` 保持纯 ASCII | Windows Python 以 locale 编码（cp936）读取该文件，一个非 ASCII 字节就让 `lint-imports` 抛 `UnicodeDecodeError` | 本方案不改 `.importlinter`；若后续需要新增契约，必须英文注释 |

**M3 为何能不动内核就实现 `--permission-mode`**：`interrupt_on` 是在
`agent/graph.py:203` 装配期烧进图里的，运行期无法改。但「审批如何被应答」发生在 CLI
这一侧——`--permission-mode` 的第一阶段语义因此定义为**审批应答策略**（自动 approve /
仅自动 approve 只读类工具 / 全部询问 / 禁止任何 approve），零内核改动。真正的
「工具不出现在图里」是 M5 的事。两者语义必须区别命名，否则会误导用户以为
`--permission-mode` 已经限制了工具可见性——这一点写进 7.1 的退出码与提示文案要求。

---

## 四、目录结构与文件划分

### 4.1 目标目录树（增量视图）

`+` 新增，`~` 改写，`-` 删除。未标注的保持不变。

```
MyAgentHarness/
├── main.py                          ~  只保留进程级分发 + 日志装配；CLI 参数定义下沉
├── config.py                            不变
├── pyproject.toml                   ~  M4 补 [build-system] 与 [project.scripts]
├── .importlinter                        不变（若新增契约：注释必须纯 ASCII）
│
├── interfaces/
│   ├── __init__.py                      不变
│   ├── cli/                         +  原 cli.py 包化（11 个模块，见 4.2）
│   │   ├── __init__.py              +  对外契约面 + 兼容再导出
│   │   ├── options.py               +  参数模型与 argparse 构建/校验
│   │   ├── session.py               +  会话绑定解析（新建 / 恢复 / 续接）
│   │   ├── runner.py                +  生命周期编排与形态分发、退出码
│   │   ├── repl.py                  +  交互循环
│   │   ├── headless.py              +  非交互一次性执行
│   │   ├── commands.py              +  斜杠命令注册表与实现
│   │   ├── approval.py              +  HITL 审批交互与应答策略
│   │   ├── render.py                +  事件 → 人类可读文本
│   │   ├── output.py                +  事件 → 输出协议写出（EventSink 家族）
│   │   └── terminal.py              +  终端能力探测（TTY / 颜色 / 编码 / 宽度）
│   ├── cli.py                       -  由 cli/ 取代
│   └── web/                             不变
│
├── application/
│   ├── dto.py                       ~  M2 新增 RunPolicy；M5 扩展其字段
│   ├── run_service.py               ~  M2 stream/resume 增加 policy 形参
│   └── runnable.py                  ~  M2 build_runnable_config 支持按次覆盖上限
│
├── agent/
│   └── graph.py                     ~  M5 build_agent 接收工具过滤；AgentFactory 缓存键扩展
│
├── scripts/
│   └── smoke_cli_headless.py        +  M1 端到端冒烟（对齐 scripts/probe_* 惯例）
│
└── tests/
    ├── interfaces/
    │   ├── test_cli.py              -  拆入 tests/interfaces/cli/
    │   └── cli/                     +  按被测模块镜像组织
    │       ├── test_options.py      +
    │       ├── test_session.py      +
    │       ├── test_runner.py       +
    │       ├── test_repl.py         +
    │       ├── test_headless.py     +
    │       ├── test_commands.py     +
    │       ├── test_approval.py     +
    │       ├── test_render.py       +
    │       ├── test_output.py       +
    │       ├── test_terminal.py     +
    │       └── test_structure_contract.py  +  把 5.2 的依赖方向变成 CI 断言
    └── ...                          其余不变
```

### 4.2 文件划分原则与命名规范

**划分原则**（三条，按优先级）：

1. **一个文件一个变化理由**。文件名的名词必须能回答「改什么需求会打开它」。
2. **叶子优先**。没有仓库内依赖的模块（`terminal`、`output`、`render`）放最底，
   编排者（`runner`）放最顶。包内依赖必须无环，由结构契约测试守住。
3. **形态隔离**。`repl` 与 `headless` 互不 import——它们共享的是 `output` / `approval` /
   `session` 这些下游，而不是彼此。允许两者互相引用就等于允许「交互形态悄悄影响
   非交互形态」，而后者是要进 CI 的。

**命名规范**：

| 对象 | 规范 | 示例 |
| --- | --- | --- |
| 包内模块 | 小写蛇形、单数、领域名词；**禁止** `utils` / `helpers` / `common` / `misc` | `output.py` 而非 `io_utils.py` |
| 类 | PascalCase；DTO 用语义后缀 | `SessionBinding`、`RunPolicy`、`ToolListResult` |
| 枚举 | PascalCase 类名 + 大写成员工 | `OutputFormat.STREAM_JSON` |
| 协议（Protocol） | 名词或 `*Like`；不叫 `Interface` | `EventSink`、`CommandContext` |
| 私有符号 | 单下划线前缀；仅限包内使用 | `_render_todos`、`_ExitCode` |
| 斜杠命令 | `/` + 小写蛇形，与用户可见文档逐字一致 | `/threads`、`/export`、`/cost` |
| 命令行长选项 | `--kebab-case`；取值用 `choices` 枚举，不接受任意字符串 | `--output-format stream-json` |
| 测试文件 | `test_<被测模块>.py`，与 `interfaces/cli/<模块>.py` 一一对应 | `test_output.py` |
| 测试函数 | `test_<行为>_<预期>`，中文用例名允许（与 `tests/frontend/` 既有做法一致） | `test_stdout_只含结果不含诊断` |

### 4.3 核心文件清单与用途

**入口层（`interfaces/cli/`）**

| 文件 | 用途（单一职责） | 关键符号 | 依赖 |
| --- | --- | --- | --- |
| `__init__.py` | 包的对外契约面：声明 `__all__`，再导出稳定入口，使 `from interfaces.cli import run_cli` 与既有调用点不破 | `run_cli`、`main_sync`、`CliOptions`、`__all__` | `runner`、`options` |
| `options.py` | 命令行契约的唯一载体：`CliOptions` 数据模型、`build_cli_parser()`（供 `main.py` 注册子命令）、`parse_cli_args()` 的取值校验与冲突检测（如 `--continue` 与 `--resume` 互斥） | `CliOptions`、`OutputFormat`、`PermissionMode`、`build_cli_parser()`、`parse_cli_args()` | `config` |
| `session.py` | 会话绑定的唯一解析点：新建 / `--resume` / `--continue` 三种来源收敛成一个 `SessionBinding`，并把「恢复的会话与 `--workspace` 是否冲突」这类判定集中在此 | `SessionBinding`、`resolve_binding()` | `application`（ThreadService 端口）、`config` |
| `runner.py` | 生命周期编排：装配上下文（`bootstrap.core.build_app_context`）、模型别名校验、形态分发（repl / headless）、异常到退出码的映射、资源释放 | `run_cli()`、`main_sync()`、`_ExitCode` | `repl`、`headless`、`options`、`session`、`bootstrap` |
| `repl.py` | 交互循环：读输入 → 交命令路由或运行服务 → 消费事件 → 处理中断 → 回到读输入；两级 Ctrl+C 语义 | `run_repl()` | `commands`、`approval`、`output`、`render` |
| `headless.py` | 非交互一次性执行：一次运行、遇到中断按策略处理、产出唯一退出码；**不读 stdin 做决策** | `run_headless()` | `approval`、`output`、`session` |
| `commands.py` | 斜杠命令注册表：命令元数据（名字、简述、参数提示）+ 实现 + 路由；未知命令不改写用户输入（`/foo` 作为普通文本进入模型需显式约定） | `CommandSpec`、`CommandRouter`、`CommandContext`、`build_default_router()` | `application`（列表/导出/用量服务）、`render` |
| `approval.py` | 审批交互与应答策略：把「允许的决策集」与「策略」映射成一次 `HITLResponse`，负责非法输入重试与会话级 always-allow 记忆 | `ApprovalPolicy`、`prompt_approval()`、`auto_decide()`、`_allowed_decisions()` | `options`、`output`、`terminal` |
| `render.py` | 事件 → 人类可读文本的纯函数集合（含待办清单、参数单行摘要、耗时、轻量 Markdown 强调）；不写任何 IO | `render_event()`、`render_todos()`、`format_args()` | `terminal` |
| `output.py` | 事件 → 输出协议写出：`EventSink` 协议与 `TextSink` / `JsonSink` / `StreamJsonSink` 三个实现；负责 stdout/stderr 分流 | `EventSink`、`TextSink`、`JsonSink`、`StreamJsonSink`、`make_sink()` | `render`、`terminal` |
| `terminal.py` | 终端能力探测：是否 TTY、是否支持颜色（含 `NO_COLOR`）、编码、宽度；所有终端判定的唯一来源 | `TerminalCapabilities`、`detect_capabilities()` | 标准库 |

**业务逻辑层（`application/`，仅扩展）**

| 文件 | 变更 | 用途 |
| --- | --- | --- |
| `dto.py`（契约组） | 新增 `RunPolicy` 数据类 | 承载「本次运行」的可选约束：轮次上限、工具允许/拒绝集合、预设 ID。放契约组是因为它要被 `interfaces` 与 `application` 同时引用，且**不依赖任何服务**（层纯度契约要求） |
| `run_service.py` | `stream`（`:272`）/ `resume`（`:546`）新增 `policy: RunPolicy \| None = None` | 校验策略与既有会话语义是否冲突（如预设锁定），并透传到装配与运行配置 |
| `runnable.py` | `build_runnable_config`（`:17`）新增按次覆盖 `recursion_limit` | 现状 `recursion_limit` 只来自 `config.recursion_limit`（`config.py:775`），无法按次收紧 |

**内核层（`agent/`，仅 M5）**

| 文件 | 变更 | 用途 |
| --- | --- | --- |
| `graph.py` | `build_agent`（`:81`）接收工具过滤；`AgentFactory`（`:222`）缓存键加入策略维度 | 让 `--allow-tools` / `--deny-tools` 真正作用于图内工具集 |

**测试层与脚本层**

| 文件 | 用途 |
| --- | --- |
| `tests/interfaces/cli/test_structure_contract.py` | 包内依赖方向契约（5.2/5.4） |
| `tests/interfaces/cli/test_output.py` | 三种输出协议的字段级契约（含 stdout/stderr 分流） |
| `tests/interfaces/cli/test_options.py` | 参数解析、互斥冲突、默认值、`--log-level` 双点声明的取值优先级 |
| `scripts/smoke_cli_headless.py` | 真进程端到端冒烟：起子进程、喂 `-p`、断言退出码与 JSON 可解析 |

---

## 五、模块间接口约定

### 5.1 层间接口（入口层 ↔ 应用层）

入口层**只允许**通过以下三类通道获取能力。这是 `.importlinter` 契约 1 与
`tests/application/test_runtime_port_contract.py` 已经固化的方向，本方案不新增例外。

| 通道 | 形态 | 本方案的使用 |
| --- | --- | --- |
| 装配事实 | `bootstrap.context.AppContext`（不可变依赖集合，`bootstrap/context.py`） | `runner.py` 持 `context.runs` / `context.threads` / `context.catalog` / `context.usage`（若未在 context 上暴露，先在 `bootstrap/core.py` 补，不得在 CLI 里自行构造服务） |
| 业务能力 | `application` 服务的公开方法 | `RunService.stream/resume/stop`、`ThreadService.list_threads/history/export_thread`、`UsageService.summarize`、`ToolCatalog.list_tools` |
| 契约数据 | `application/dto.py`、`application/events.py`、`application/errors.py` | 事件用 `AgentEvent` / `AgentEventType`；错误用 `application.errors` 的既有类型（如 `ThreadBusyError`、`SessionRootLockedError`） |

**明确禁止**：

- 从 `interfaces/cli/**` import 任何 `runtime.*`（契约 1；`allow_indirect_imports` 允许
  经 `application` 的传递依赖，但禁止直接 import）
- 在 CLI 内直接读写 SQLite 或文件系统以绕过服务（会话导出走 `ThreadService.export_thread`，
  工作区文件走 `WorkspaceService`）
- 在 CLI 内自行 `build_app_context` 之外的第二套装配（契约 6 想避免的「第二套组装」）

### 5.2 包内接口（`interfaces/cli` 子模块之间的依赖方向）

包内依赖**必须无环**，方向固定为自下而上（下层不知道上层存在）：

```
                       runner
                    ↙     ↓     ↘
              repl     headless     （二者互不 import）
            ↙   ↓  ↘        ↓
   commands  approval  session
        ↓        ↓
      render   output
          ↘     ↙
        terminal
```

规则化表述：

| 模块 | 允许依赖（包内） | 禁止依赖（包内） |
| --- | --- | --- |
| `terminal` | 无 | 全部其他模块 |
| `output` | `render`、`terminal` | `options`、`session`、`runner`、`repl`、`headless`、`commands`、`approval` |
| `render` | `terminal` | 其余全部（**尤其不得 import `output`**） |
| `options` | 无（只依赖 `config`） | 包内全部其他模块 |
| `session` | `options` | `runner`、`repl`、`headless`、`commands`、`approval` |
| `commands` | `render`、`options` | `runner`、`repl`、`headless` |
| `approval` | `output`、`options`、`terminal` | `runner`、`repl`、`headless`、`commands` |
| `repl` | `commands`、`approval`、`output`、`render`、`session`、`options` | `runner`、`headless` |
| `headless` | `approval`、`output`、`session`、`options` | `runner`、`repl`、`commands` |
| `runner` | 全部 | 无 |

**`render` 与 `output` 的边界为什么必须划清**：`render` 决定「这句话用中文怎么说」，
`output` 决定「这句话写进哪个流、用什么协议包起来」。合并会立刻产生
「json 输出里混进了中文装饰符」这类问题，而 json 消费方只会报解析失败，
不会告诉你是装饰符进了 payload。

### 5.3 本方案新增/修改的对外签名

```python
# application/dto.py（契约组，新增）
@dataclass(frozen=True)
class RunPolicy:
    """一次运行的可选约束。None 表示不约束（沿用配置）。"""
    max_turns: int | None = None
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] | None = None
    preset: str | None = None


# application/run_service.py（扩展形参，保持向后兼容）
async def stream(
    self,
    thread_id: str,
    user_input: str | list[dict[str, Any]],
    *,
    model_name: str | None = None,
    workspace: str | None = None,
    preset: str | None = None,
    policy: RunPolicy | None = None,      # 新增
) -> AsyncIterator[AgentEvent]: ...

async def resume(
    self,
    thread_id: str,
    decision_payload: dict[str, Any],
    *,
    model_name: str | None = None,
    policy: RunPolicy | None = None,      # 新增
) -> AsyncIterator[AgentEvent]: ...
```

```python
# interfaces/cli/output.py（新增）
class EventSink(Protocol):
    """事件下游：交互与 headless 两种形态共享的唯一出口。"""
    def emit(self, event: AgentEvent) -> None: ...
    def close(self, exit_code: int) -> None: ...
```

**兼容性要求**：`policy` 必须带默认值 `None`，且 `None` 的语义与改动前**逐字一致**。
`interfaces/web/routes.py` 的 `POST /threads/{id}/runs`（`:621`）与
`resume`（`:727`）不传该参数，行为不得有任何变化——这一点由既有 Web 路由测试覆盖。

### 5.4 把约定变成可执行规则

仓库的既有做法是「规则写成测试，而不是写在文档里」（README「规则是可执行的」一节）。
本方案新增的约定同样如此：

| 约定 | 载体 | 怎么跑 |
| --- | --- | --- |
| 包内依赖方向（5.2 的表） | `tests/interfaces/cli/test_structure_contract.py` | `uv run pytest` |
| 输出协议的字段集与流归属（6.3） | `tests/interfaces/cli/test_output.py` | `uv run pytest` |
| 退出码语义（7.1） | `tests/interfaces/cli/test_runner.py` | `uv run pytest` |
| CLI 不直接依赖 runtime | `.importlinter` 契约 1（**既有**，无需新增） | `uv run lint-imports` |

结构契约测试的实现方式沿用既有基础设施：`tests/_ast_imports.py` 已提供
`imported_symbols()` 与 `package_modules()`，静态解析能覆盖
`if TYPE_CHECKING:` 块与函数内延迟导入——这正是历史上被用来藏越界的位置。

---

## 六、数据流转

### 6.1 交互形态（`repl.py`）

```
argv
 └─ options.parse_cli_args()        → CliOptions（含 output_format / permission_mode / max_turns）
     └─ session.resolve_binding()   → SessionBinding（thread_id, workspace, preset, resumed）
         └─ output.make_sink()      → EventSink（TextSink | JsonSink | StreamJsonSink）
             └─ 循环：
                  ① 读一行（TTY：带历史的行式输入；非 TTY：EOF 即结束）
                  ② commands.CommandRouter 命中 → 本地执行 → 回 ①（**不经过模型**）
                  ③ 未命中 → runs.stream(thread_id, text, model_name, workspace, preset, policy)
                  ④ 逐事件：INTERRUPT → 暂存；其余 → sink.emit(event)
                  ⑤ 有 INTERRUPT → approval.prompt_approval() → runs.resume(thread_id, decision, policy)
                     → 回到 ④（可多次，直到无中断）
                  ⑥ 一轮结束 → 回到 ①
```

**关键不变量**：

- `runs.stream(...)` 是普通协程（非异步生成器），参数校验与模型初始化在 `await` 那一刻
  完成，因此**错误发生在事件流开始之前**（`application/run_service.py:283-287` 的注释
  解释了为什么要这样设计）。CLI 必须保留这个顺序：先 `await` 拿到事件流，再进入消费，
  否则「模型别名不存在」会表现为流中途的 ERROR 事件而不是可判定的启动失败。
- 运行期 `policy` 必须在 `stream` 与 `resume` 两侧**传同一个值**：中断与恢复是同一次
  运行的两个半程，两侧策略不同会让成本与行为出现不可预期偏差（`interfaces/cli.py:189`
  已有同型注释解释为什么恢复时仍要带 `model_name`）。

### 6.2 非交互形态（`headless.py`）

```
argv（含 -p）→ options → session.resolve_binding() → runs.stream(...) → 消费到 DONE
                                                     ↓
              若出现 INTERRUPT：
                 permission_mode 允许自动应答 → 自动 resume，继续消费
                 否则 → sink.close(EXIT_NEEDS_APPROVAL=3)，不再等待 stdin
```

差异点只有两条，且必须显式：

1. **绝不读 stdin 做决策**。非交互形态下 `input()` 会读到管道剩余内容或立刻 EOF，
   两种情况都会产出「静默的错误决策」。缺省策略是退出码 3（见 7.1）。
2. **stdout 只承载结果**。诊断（装配信息、用量、工具调用过程）在 `--output-format text`
   下走 stderr；在 `json` / `stream-json` 下进入结构化事件，仍然只写 stdout。

### 6.3 事件 → 三种输出协议的映射

| 事件（`AgentEventType`） | `text` | `json`（单条聚合对象） | `stream-json`（NDJSON 逐条） | 流 |
| --- | --- | --- | --- | --- |
| `TOKEN` | 原样追加（不换行、强制 flush） | 拼进 `result.text` | `{"type":"assistant_delta","text":...}` | stdout |
| `TOOL_CALL` | `[调用] name args` | 追加进 `tool_calls[]` | `{"type":"tool_call","name":...,"args":...}` | stdout(text 形态走 stderr) |
| `TOOL_RESULT` | `[结果] name status (已截断)` + 留存路径 | 追加进 `tool_results[]` | `{"type":"tool_result",...}` | 同上 |
| `TODOS` | `-- 待办 --` 块 | `todos[]` | `{"type":"todos","items":[...]}` | 同上 |
| `STEP` | 不输出 | 不输出 | 不输出 | — |
| `USAGE` | `[用量] ...` | `usage{prompt_tokens,...}` | `{"type":"usage",...}` | 同上 |
| `ERROR` | 走 stderr | `errors[]`（**必须进结构化输出**） | `{"type":"error","message":...}` | stdout |
| `INTERRUPT` | 交互提示 | `interrupt{...}` + 退出码 3 | `{"type":"interrupt",...}` | stdout |
| `DONE` | 换行 | 触发写出聚合对象 | `{"type":"done"}` | stdout |

**`json` 形态的写出时机**：所有事件收集完毕后由 `sink.close()` 一次性写出单个 JSON 对象，
不是逐条 NDJSON——理由是这样消费方可以一次 `json.load` 拿到完整结果，而需要流式的场景
应当选 `stream-json`。两者的差别必须写进 `--help` 文本。

### 6.4 会话解析与状态归属

| 状态 | 归属 | 说明 |
| --- | --- | --- |
| `thread_id` | `SessionBinding`（值对象，不可变） | 新建时由 `ThreadService.new_thread_id()` 发号；恢复时来自 `--resume` |
| 文件根（workspace） | 服务端（`ThreadService` 与会话元数据表） | **只在首条消息生效并永久锁定**。CLI 恢复既有会话时不得再传 `--workspace`，否则服务端会以 `SessionRootLockedError` 拒绝——`session.resolve_binding()` 必须在**本地**就拦下这种组合并给出可读提示，而不是把 409 转成一个栈 |
| 场景预设（preset） | 服务端（同上，与根一一对应） | 恢复时从会话元数据读取；`--preset` 只在新建会话时有效 |
| 审批挂起状态 | `RunService`（`mark_hitl_pending` / `clear_hitl_pending`） | CLI 不得自行记录「有没有待审批」，必须问服务，否则 Web 与 CLI 会各持一份真相 |
| 输出累积缓冲 | `EventSink`（只属于本次运行） | 每次运行新建 sink，避免跨轮串味 |

---

## 七、异常与日志策略

### 7.1 异常分类与退出码

退出码是脚本化调用唯一可靠的判据，必须一次定全。既有约定是 0/1/2
（`interfaces/cli.py:208-209` 的返回值说明），本方案在保持这三个语义不变的前提下补充。

| 退出码 | 含义 | 触发来源 | 日志级别 | 输出位置 |
| --- | --- | --- | --- | --- |
| `0` | 正常结束（含用户输入 `exit`、非 TTY 下 EOF） | 主循环正常退出 | 无 | — |
| `1` | 运行期异常 | 模型调用失败、存储失败、`ThreadBusyError` 等未预期异常 | `ERROR` + `logger.exception` | stderr |
| `2` | 参数 / 配置错误 | argparse 校验失败、模型别名不存在、`AppConfig.load()` 失败 | `WARNING`（**不是 ERROR**：这是用法问题不是故障） | stderr |
| `3` | 非交互形态遇到需要人工审批的调用 | headless + 策略不允许自动应答 | `WARNING` | stderr |
| `124` | 运行超时 | `--max-turns` 或运行超时治理生效 | `WARNING` | stderr |
| `130` | 被 `SIGINT` 中断（128 + 2，POSIX 惯例） | 第二次 Ctrl+C | 无（用户主动行为） | — |

**与现状的两处变化**（须在 README 变更记录里点名）：

- 现状 Ctrl+C 返回 `0`（`interfaces/cli.py:273-275`）。改为：第一次 Ctrl+C 中止**当前
  轮次**并调用 `RunService.stop()`（`:629`）后保留会话继续；第二次才退出并返回 `130`。
- 现状 CLI 从不调用 `RunService.stop()`。改为：任何中止路径（Ctrl+C、`/stop`、
  非交互超时）都必须走 `stop()`，否则审计里会留下「客户端以为停了、服务端仍在跑」的
  悬挂运行——Web 的 `POST /threads/{id}/stop`（`interfaces/web/routes.py:933`）已经在
  走这条路，两条适配器不能分叉。

**错误处理纪律**（对齐仓库既有要求，禁止静默吞错）：

1. 每个 `except` 块必须**同时**做两件事：给人一条可读提示（stderr），给排障一条日志
   （`logger.exception` 或 `logger.warning`，视是否预期而定）。
2. 不允许裸 `except Exception: pass`。允许的例外只有一种：清理路径上的二次异常，且必须
   记录为 `logger.debug` 并注明「清理时失败不应掩盖原始异常」。
3. `application.errors` 里的领域异常（`ThreadBusyError`、`SessionRootLockedError`、
   `SessionPresetLockedError`、`NotFoundError`、`KeyError`（模型别名））需要**逐类映射**
   到上表的退出码与文案，不得统一压成「出错了」。

### 7.2 日志规范

| 项 | 约定 | 理由 |
| --- | --- | --- |
| 通道 | 日志一律写 **stderr**（`main.py:65` 已如此配置） | stdout 留给结果，`cli -p ... \| jq` 才能成立 |
| 格式 | 沿用 `main.py` 的 `_setup_logging`：`text` 带 `[trace_id]`，`json` 走 `_JsonFormatter` | 两种格式都要能按链路串起一次请求 |
| `trace_id` | 每次**运行**建立一个 context（复用 `application/audit_context.py` 的上下文机制），使该轮所有日志带同一个 `trace_id` | 现状 CLI 不绑定上下文，`trace_id` 恒为空串——排障时无法把「MCP 连接日志」与「某次提问」对应起来 |
| 级别 | 启动信息（工作空间/模型/会话 ID）`INFO`；本地问题（模型别名不存在、参数组合冲突）`WARNING`；基础设施故障 `ERROR` + 堆栈 | 与现状一致，不改动（`interfaces/cli.py:235-244` 的启动信息保持逐字不变） |
| `--log-level` 位置 | 根级保留（兼容 `README.md:89-90` 的既有用法），子命令侧新增同名参数，子命令侧用 `default=argparse.SUPPRESS` 声明 | 同名 `dest` 在 argparse 中会被子解析器的默认值覆盖，必须用 `SUPPRESS` 才能让「子命令未给」时落到根级取值 |
| 脱敏 | 日志与审批提示中不打印工具参数里的疑似凭据（沿用 `agent/guardrails.py:59-69` 的敏感路径判定思路）；`--output-format json` 中工具参数原样保留 | 结构化输出是给机器用的，脱敏会破坏可编排性；脱敏只作用于**给人看**的日志 |

### 7.3 资源释放与取消

| 资源 | 释放责任 | 时机 |
| --- | --- | --- |
| `AppContext`（含 SQLite 连接、图缓存） | `runner.py` 的 `async with build_app_context(config)` | 正常退出、异常退出、Ctrl+C 三条路径都要覆盖（现状 `interfaces/cli.py:220` 已如此） |
| 运行槽位 | `RunService` 的 `_acquire_run_slot` / `release_run_slot` | 事件流消费完毕或被中止时；CLI 不得手工操作槽位 |
| 事件流 | `repl.py` / `headless.py` | 中止时必须消费到迭代器结束或显式 `aclose()`，否则 `async with` 退出时可能留下未关闭的生成器 |
| 输出缓冲 | `EventSink.close(exit_code)` | 每次运行结束；`json` 形态的聚合对象在此写出，**任何退出路径都必须调用**，否则「提前退出导致没有任何输出」会让脚本拿到空结果 |

---

## 八、测试层方案

### 8.1 组织与命名

测试按被测层镜像组织（仓库既有约定），CLI 侧从单文件拆为目录：

```
tests/interfaces/cli/
├── test_options.py              # 纯逻辑：解析、互斥、默认值、--log-level 优先级
├── test_session.py              # 纯逻辑 + 服务替身：三种绑定来源、冲突拦截
├── test_runner.py               # 退出码矩阵（7.1 表逐行一条用例）
├── test_repl.py                 # 主循环：斜杠命令分流、中断循环、EOF
├── test_headless.py             # 单次执行、不读 stdin、审批缺省退出码
├── test_commands.py             # 命令注册表：名字唯一、未知命令行为
├── test_approval.py             # 决策集、非法输入重试、会话级 always-allow
├── test_render.py               # 纯函数：三种事件的文本形态
├── test_output.py               # 输出协议：字段集、stdout/stderr 分流、聚合时机
├── test_terminal.py             # TTY / NO_COLOR / 编码降级
└── test_structure_contract.py   # 包内依赖方向（5.2）
```

`tests/interfaces/test_cli.py`（现 600+ 行）在 M0 整体删除，内容按上表迁移——
不保留「旧文件 + 新文件」两份，避免同一个行为有两处断言。

### 8.2 既有测试纪律的延续

`tests/interfaces/test_cli.py` 的 docstring 已经确立了三条纪律，新测试全部沿用：

1. **装配用替身**：真实 `build_app_context` 会建库、连模型、编译图；本层要断言的是
   CLI 自己的分支，装配路径由 `bootstrap` 侧覆盖。
2. **断言落在 stdout 与退出码上**：这是 CLI 与用户的全部契约。
3. **mock `input()` 用「按序喂值」而非重定向 stdin**：`_feed_input` 的队列耗尽即报错，
   避免用例悄悄多读一次输入（`tests/interfaces/test_cli.py:46-60`）。

新增的第四条：**输出协议用冻结样本断言**。`test_output.py` 对同一串事件断言三种格式的
**完整**输出（含键名与顺序），而不是只断言「包含某字段」——字段漂移正是要靠这条拦住。

### 8.3 端到端冒烟

`scripts/smoke_cli_headless.py`（对齐仓库 `scripts/probe_*` / `smoke_*` 惯例）：

- 以子进程启动 `python main.py cli -p "..." --output-format json`
- 断言：进程退出码为 0、stdout 可被 `json.load` 解析、stderr 不含结果正文
- 断言：`--output-format json` 下 `options` 与 `--resume` 组合的退出码符合 7.1 表
- 该脚本默认不跑真模型调用（用最小可用输入或显式跳过），保持「冒烟即可、不烧 token」

---

## 九、遗留与风险

### 9.1 风险登记簿

| 风险 | 影响 | 应对 | 归属里程碑 |
| --- | --- | --- | --- |
| 包化改造破坏既有导入路径 | `main.py` 与 600 行 CLI 测试同时失败 | `interfaces/cli/__init__.py` 保留再导出；M0 一次性切换测试并跑全量回归 | M0 |
| argparse 同名 `dest` 取值被覆盖 | `--log-level` 在子命令后取值失效（静默） | 子命令侧用 `default=argparse.SUPPRESS`；`test_options.py` 专门断言两种位置的优先级 | M1 |
| 非交互形态下 `input()` 读到管道残留 | 产出一个用户从未做过的审批决策 | headless **不读 stdin**，缺省走退出码 3；`test_headless.py` 断言不调用 `input()` | M1 |
| 图缓存键扩展导致内存与装配耗时上升 | `AgentFactory` 缓存条目数按策略维度倍增 | M5 先做实测（缓存条目数与装配耗时）再定策略键的粒度；必要时只把「工具集合的哈希」进键 | M5 |
| `render` 中文文本进入 json payload | 消费方解析失败 | `render` 与 `output` 职责分离（5.2）+ 冻结样本测试 | M1 |
| Windows 与 Linux 的 TTY / 编码差异 | 本地通过、CI 失败 | `terminal.py` 集中探测；CI 双平台跑同一组用例；显式 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` 并记录在 `terminal.py` 的 docstring 里说明为什么需要 | M1 |
| 新增三方依赖（行式输入历史与 Markdown 渲染） | 需核实 Python 3.14 轮子可用性与 CI 双轨成本 | M4 前单独做选型实测，把结论作为决策记录附在本文件之后；**不得以「不许加依赖」为由拒绝**（该约束已废除），但必须有实测依据 | M4 |
| 契约测试的覆盖面在新增模块时静默缩小 | 新文件不受任何约束 | `test_structure_contract.py` 断言「`interfaces/cli/` 下的每个模块都出现在依赖表里」，形态与 `tests/test_root_module_contract.py:190` 一致 | M0 |

### 9.2 需要同步清理的仓库残留

| 位置 | 问题 | 处置 |
| --- | --- | --- |
| `README.md:717` | 指向 `docs/overview/architecture.html`，该路径不存在（HTML 已在重排中移到 `docs/`） | 改为 `docs/architecture.html` |
| `README.md:764` | 指向 `docs/architecture/架构遗留问题治理方案.md`，该文件已在 `ce965d0` 清理 | 删除该句，或改指本文件 |
| `runtime/store.py:49` | docstring 里残留「该包，因此这次替换是零新增依赖」的表述 | 「应用内零新增依赖」的约束已废除，该表述须删除或改写，否则会与 M4 的依赖选型相互矛盾 |
| `interfaces/cli.py:261-267` | `await _run_turn(` 的实参缩进与函数体平级（不触发 ruff 现有规则集，但会掩盖后续真实缩进 bug） | M0 迁移时顺手修正 |
| `interfaces/cli.py:283-285` | `main_sync` 丢弃 `workspace` 形参，与 `main.py:112-118` 实际使用的调用形式不一致，仅被测试引用 | M0 统一为一个入口，或让 `main_sync` 转发 `workspace` |

---

## 附录 A：落盘后的完整目录树

标注同 4.1：`+` 新增、`~` 改写、`-` 删除；未标注为不变。省略与本方案无关的
`interfaces/web/**`、`runtime/**`、`llm/**`、`skills/**`、`docker/**` 内部细节。

```
MyAgentHarness/
├── main.py                              ~  进程级分发 + 日志装配（~60 行）
├── config.py                                配置层（不改）
├── text_utils.py / thread_utils.py /        公共/工具层：中立叶子（不改）
│   web_safety.py
├── knowledge_runtime.py /                   公共/工具层：服务句柄与工具插件（不改）
│   knowledge_tools.py / web_tools.py
├── .importlinter                            分层契约（不改；新增注释须纯 ASCII）
├── .env.example                             配置模板（不改）
├── pyproject.toml                       ~  补 [build-system] 与 [project.scripts]
├── uv.lock                              ~  若 M4 引入依赖则更新
├── README.md                            ~  「启动命令」章节同步新增参数与命令
│
├── bootstrap/                               装配层（不改；若新增服务暴露需动 core.py）
├── agent/                                   领域内核层
│   └── graph.py                         ~  M5：工具过滤参数 + 缓存键扩展
├── application/                             业务逻辑层
│   ├── dto.py                           ~  M2：新增 RunPolicy
│   ├── run_service.py                   ~  M2：stream/resume 增加 policy 形参
│   └── runnable.py                      ~  M2：build_runnable_config 支持按次覆盖上限
├── runtime/                                 数据处理层（不改）
├── llm/                                     模型访问层（不改）
│
├── interfaces/
│   ├── __init__.py
│   ├── cli/                             +  入口层（本方案主体）
│   │   ├── __init__.py                  +  对外契约面 + 兼容再导出
│   │   ├── options.py                   +  参数模型与 argparse 构建
│   │   ├── session.py                   +  会话绑定解析
│   │   ├── runner.py                    +  生命周期编排与退出码
│   │   ├── repl.py                      +  交互循环
│   │   ├── headless.py                  +  非交互一次性执行
│   │   ├── commands.py                  +  斜杠命令
│   │   ├── approval.py                  +  审批交互与应答策略
│   │   ├── render.py                    +  事件 → 文本
│   │   ├── output.py                    +  事件 → 输出协议
│   │   └── terminal.py                  +  终端能力探测
│   ├── cli.py                           -  由 cli/ 取代
│   └── web/                                 接口层（Web 适配器，不改）
│
├── scripts/
│   ├── smoke_cli_headless.py            +  端到端冒烟
│   └── ...                                  既有探针与冒烟脚本
│
├── tests/
│   ├── interfaces/
│   │   ├── test_cli.py                  -  拆入下面目录
│   │   └── cli/                         +  与被测模块一一对应
│   │       ├── test_options.py          +
│   │       ├── test_session.py          +
│   │       ├── test_runner.py           +
│   │       ├── test_repl.py             +
│   │       ├── test_headless.py         +
│   │       ├── test_commands.py         +
│   │       ├── test_approval.py         +
│   │       ├── test_render.py           +
│   │       ├── test_output.py           +
│   │       ├── test_terminal.py         +
│   │       └── test_structure_contract.py  +
│   ├── application/                         层内纯度与端口契约（不改；分组表也不动）
│   ├── test_root_module_contract.py         根级角色契约（不改；不新增根级模块）
│   └── conftest.py                          共享夹具
│
├── docs/
│   ├── architecture.html                    架构总览（不改）
│   ├── architecture-diagrams.html           图形化视图（不改）
│   └── CLI能力补齐落地方案.md           +  本文件
│
├── skills/                                  技能资产（不改）
├── docker/                                  容器定义（不改）
└── workspace/                               本机示例目录（不改）
```

## 附录 B：交付顺序清单（可直接当 PR 拆分的依据）

| 序 | PR 标题 | 包含文件 | 前置 |
| --- | --- | --- | --- |
| 1 | `refactor(cli): 拆分 interfaces/cli.py 为包并补齐结构契约` | `interfaces/cli/**`、`main.py`、`tests/interfaces/cli/**`、删除 `interfaces/cli.py` 与 `tests/interfaces/test_cli.py` | — |
| 2 | `feat(cli): 非交互执行与三种输出协议` | `options.py`、`headless.py`、`output.py`、`terminal.py`、`scripts/smoke_cli_headless.py` | 1 |
| 3 | `feat(cli): 会话恢复、轮次上限与预设透传` | `session.py`、`options.py`、`application/dto.py`、`application/run_service.py`、`application/runnable.py` | 2 |
| 4 | `feat(cli): 审批策略与两级中断语义` | `approval.py`、`repl.py`、`runner.py` | 3 |
| 5 | `feat(cli): 斜杠命令与终端体验` | `commands.py`、`render.py`、`pyproject.toml`、`README.md` | 4 |
| 6 | `feat(cli): 工具白名单贯通内核装配` | `agent/graph.py`、`application/dto.py` | 3 |

每个 PR 的完成定义（DoD）统一为：`uv run pytest` 全绿、`uv run lint-imports` 全绿、
本文件对应章节无待修项、README 相应段落已同步。
