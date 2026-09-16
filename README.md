# MyAgentHarness

一个可直接运行的**通用 Agent 骨架**：以 [deepagents](https://github.com/langchain-ai/deepagents) 为内核，
自带文件读写、待办规划、子任务委派与人工审批（HITL）能力，同时提供 **命令行（CLI）** 与 **Web 服务** 两种运行形态。

两种形态共享同一套内核（`agent/` 装配层 + `application/` 应用服务），差异只在适配器，
因此同一份会话状态、同一套审批语义在两种入口下完全一致。

---

## 一、功能特性

| 能力 | 说明 |
| --- | --- |
| 流式对话 | 模型输出以 SSE / 逐字打印的方式实时呈现，不等待整轮结束 |
| 文件工具 | 读写、编辑、列目录等全部收敛到 `workspace/` 内，越权访问直接拒绝 |
| 待办与规划 | 任务拆解为待办列表并实时回传快照，长任务过程可见 |
| 子任务委派 | 复杂任务可拆给子 Agent 并行处理，避免主上下文被细节撑爆 |
| 人工审批（HITL） | 高危工具调用前暂停并请求确认，支持**批准 / 改写 / 拒绝 / 代答**四种决策 |
| 会话持久化 | 基于 SQLite 异步检查点，进程重启后可续聊；刷新页面不丢上下文 |
| 会话清单 | 独立元数据表登记标题、创建/活动时间与对话轮数；只收录**真正发过消息**的会话，Web 侧栏可切换历史 |
| 长期记忆 | 加载 `workspace/AGENTS.md` 作为跨会话记忆 |
| 模型切换 | 每次请求可指定模型别名，也可走配置里的默认模型 |
| 执行护栏 | 单次运行的模型调用次数、递归深度、shell 超时与输出长度均有上限 |

---

## 二、环境要求

- **Python >= 3.14**
- **uv**（推荐，项目已包含 `uv.lock`）
- 一个 **DeepSeek API Key**

---

## 三、安装

```bash
# 1) 安装依赖（会按 uv.lock 精确复现环境）
uv sync

# 2) 生成配置文件
cp .env.example .env      # Windows: copy .env.example .env
```

编辑 `.env`，**至少**填入密钥：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
```

> 配置读取优先级：`环境变量 > .env 文件 > 字段默认值`。
> 详细配置项见下文「配置参考」。

---

## 四、启动命令

统一入口是 `main.py`，下有 `cli` 与 `web` 两个子命令。

### 1. 命令行交互

```bash
uv run python main.py cli

# 指定本次会话使用的模型（默认取配置中的 default_model）
uv run python main.py cli --model deepseek-flash

# 调整日志级别（--log-level 是全局参数，要写在子命令之前）
uv run python main.py --log-level DEBUG cli
```

> Windows 下也可直接用虚拟环境解释器：`.venv\Scripts\python.exe main.py cli`

交互方式：

```
通用 Agent 已启动，输入 exit 退出。
工作区：...\workspace
执行档位：disabled
当前模型：deepseek-flash
会话 ID：83cb82b40ea04ea7bbf96e81fe872c4a

> 帮我看一下 workspace 里有什么
```

| 操作 | 输入 |
| --- | --- |
| 退出 | `exit` / `quit` / `:q`（也可 `Ctrl+C`） |
| 审批：批准执行 | `a` |
| 审批：改写后执行 | `e`（随后按提示输入新的工具参数） |
| 审批：拒绝 | `r`（可附补充说明） |
| 审批：代为回答 | `s`（不执行工具，直接把答案交给模型） |

> 若传入了不存在的模型别名，CLI 会在**启动时**直接报错并列出可选值，而不是等到第一次提问才失败。

### 2. Web 服务

```bash
uv run python main.py web

# 自定义监听地址与端口（默认 127.0.0.1:8000，可用 .env 的 HOST/PORT 覆盖）
uv run python main.py web --host 0.0.0.0 --port 8080
```

启动后浏览器访问 <http://127.0.0.1:8000/> 即可使用：左侧会话列表，右侧流式对话区，
出现审批请求时会在对话流中给出操作按钮。

会话由地址栏承载（形如 `/#/c/{thread_id}`）：刷新会**恢复**当前会话而不是新建，
「新会话」按钮只清空界面进入草稿态。只有真正发出第一条消息，服务端才会创建会话——
纯浏览、刷新页面、点「新会话」都不会留下任何空会话。

---

## 五、执行档位（安全相关）

`EXECUTION_MODE` 决定 `execute`（shell 命令执行）工具是否可用，**默认是关闭的**：

| 档位 | 含义 | 适用 |
| --- | --- | --- |
| `disabled` | `execute` 工具存在但调用即返回错误，且不开启审批通道 | **默认**，任何环境的起手档位 |
| `local` | 在宿主机直接执行 shell | **仅限本机 CLI 开发**，禁止用于 Web / 多租户 |
| `sandbox` | 在沙箱内执行 shell，隔离强度由 `SANDBOX_TIER` 决定 | 需要命令能力、又不愿裸跑宿主机的场景 |

> 安全边界：把 `local` 档位暴露到公网，等同于把宿主机 shell 开放出去，请务必谨慎。

### 沙箱档位（`SANDBOX_TIER`，仅 `sandbox` 档位生效）

| 档位 | 状态 | 隔离能力 |
| --- | --- | --- |
| `auto` | 默认 | 按 `wsl → process` 顺序探测，选中首个可用的最强档位 |
| `wsl` | **已实现（Tier 1）** | 命令跑在 WSL2 发行版内：utility VM 边界 + Linux rlimit（进程数 / 内存 / CPU 时间），超时由 GNU `timeout` 终止整个进程组 |
| `process` | **已实现（Tier 0）** | Windows Job Object：进程树管控、活动进程数/内存/CPU 上限、超时终止整棵树；零新增依赖 |
| `docker` | 未实现 | 计划中：容器内执行 |

> **Tier 0 与 Tier 1 都不是安全边界。** 它们只解决「命令失控」：fork bomb、
> 无限输出、超时残留进程、环境变量泄漏。挡不住本地提权，也挡不住命令主动
> 读取宿主机上的其他文件——Tier 1 的发行版经 `/mnt` 仍能读写宿主文件，且
> 通过 WSL interop 可反向启动 Windows 程序。
> 因此两者**默认强制开启人工审批**（`SANDBOX_REQUIRE_APPROVAL=true`），
> 审批才是主防线。
>
> 选择未实现的档位会**直接报错**，不会静默降级到更弱的隔离——否则等同于
> 伪造安全边界。`auto` 档位在更强档位探测失败而回落时，会在日志里写明
> 跳过了哪些档位。

回归验证：

```bash
python scripts/smoke_sandbox.py
```

覆盖 Tier 0 与 Tier 1 两档共 21 项：基本执行、环境变量清洗、超时终止、输出截断、
进程数上限、工作目录换算、写入工作区、backend 集成与护栏联动。

---

## 六、配置参考

全部配置集中在 `config.py`，通过 `.env` 或环境变量覆盖：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 密钥，缺失时模型无法就绪 |
| `DEEPSEEK_API_BASE` | `https://api.deepseek.com` | 接口地址，可指向自建网关 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 具体模型 ID |
| `DEFAULT_MODEL` | `deepseek-flash` | 默认模型别名 |
| `LLM_TEMPERATURE` | `0.0` | 工具调用场景建议保持 0，避免参数漂移 |
| `LLM_TIMEOUT` | `60.0` | 单次请求超时（秒） |
| `LLM_MAX_RETRIES` | `2` | 请求重试次数 |
| `EXECUTION_MODE` | `disabled` | 执行档位，见上一节 |
| `SANDBOX_TIER` | `auto` | 沙箱档位：`auto`/`process`/`wsl`/`docker` |
| `SANDBOX_WSL_DISTRO` | 空 | WSL 档位使用的发行版；留空则自动挑选首个满足要求的发行版 |
| `SANDBOX_TIMEOUT` | `120` | 沙箱内单条命令超时（秒） |
| `SANDBOX_MAX_OUTPUT_BYTES` | `100000` | 沙箱 stdout/stderr 各自的截断阈值 |
| `SANDBOX_MAX_PROCESSES` | `64` | 活动进程数上限，防 fork bomb |
| `SANDBOX_MAX_MEMORY_MB` | `2048` | 单进程内存上限（MB） |
| `SANDBOX_CPU_PERCENT` | `50` | CPU 占用硬上限百分比（Windows Job Object） |
| `SANDBOX_ENV_ALLOWLIST` | `PATH,SYSTEMROOT,...` | 传入子进程的环境变量白名单，其余一律不传 |
| `SANDBOX_NETWORK_MODE` | `none` | `none` 仅剔除代理类变量；进程级断网需管理员权限，本期不做 |
| `SANDBOX_REQUIRE_APPROVAL` | `true` | 沙箱档位下是否仍需人工审批 |
| `SHELL_TIMEOUT` | `120` | `local` 档位 shell 执行超时（秒） |
| `SHELL_MAX_OUTPUT_BYTES` | `100000` | `local` 档位 shell 输出截断阈值 |
| `MAX_MODEL_CALLS_PER_RUN` | `60` | 单轮最大模型调用次数，防死循环 |
| `RECURSION_LIMIT` | `100` | 图递归深度上限 |
| `WORKSPACE` | `./workspace` | 文件工具的根目录（活动边界） |
| `MEMORY_FILE` | `./workspace/AGENTS.md` | 长期记忆文件 |
| `DB_PATH` | `./.data/agent.db` | SQLite 数据库（检查点表 + 会话元数据表） |
| `THREAD_TITLE_MAX_CHARS` | `24` | 会话列表标题的字符上限，超出以省略号截断 |
| `AUDIT_RETENTION_DAYS` | `180` | 审计日志保留天数，超期记录由定期任务删除 |
| `AUDIT_RETENTION_INTERVAL_SECONDS` | `86400` | 审计保留清理任务的执行间隔（秒） |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Web 监听地址与端口 |
| `LOG_LEVEL` | `INFO` | 日志级别：DEBUG/INFO/WARNING/ERROR |

---

## 七、HTTP API

Web 形态对外提供以下接口（均以 `/api` 为前缀）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/models` | 列出可切换的模型（不含任何密钥信息） |
| `POST` | `/api/threads` | 申请一个会话 ID；**不落库**，会话在首条消息被接受时才创建 |
| `GET` | `/api/threads` | 列出全部会话（最近活动在前，支持 `limit` / `offset`） |
| `GET` | `/api/threads/{thread_id}` | 读取会话历史，用于刷新后恢复上下文 |
| `DELETE` | `/api/threads/{thread_id}` | 删除会话及其检查点 |
| `POST` | `/api/threads/{thread_id}/runs` | 发起一轮对话，**以 SSE 流式返回** |
| `POST` | `/api/threads/{thread_id}/resume` | 提交人工审批结果，继续被中断的运行（需 `hitl:approve` 权限） |
| `POST` | `/api/threads/{thread_id}/stop` | 请求停止当前运行；幂等返回 200，未运行时 `stopped=false` |

发起对话的请求体：

```json
{ "content": "帮我看一下 workspace 里有什么", "model": null }
```

提交审批的请求体（`type` 取 `approve` / `edit` / `reject` / `respond`）：

```json
{ "decisions": [{ "type": "approve" }], "model": null }
```

SSE 事件类型：

| 事件 | 含义 |
| --- | --- |
| `token` | 增量文本片段 |
| `tool_call` / `tool_result` | 工具调用与执行结果 |
| `todos` | 待办列表快照 |
| `step` | 节点级进度 |
| `interrupt` | 请求人工审批，携带 `action_requests` 与 `review_configs` |
| `error` | 运行期错误 |
| `done` | 本轮运行结束 |

---

## 八、目录结构

```
MyAgentHarness/
├── main.py             # 入口：分发 cli / web 两个子命令
├── config.py           # 统一配置中心（唯一的环境变量解析处）
├── agent/              # 装配层：模型后端、工具、护栏、图构建
├── application/        # 应用服务：会话编排、事件模型、中断编解码
├── interfaces/         # 适配器
│   ├── cli.py          # 命令行交互
│   └── web/            # FastAPI 服务 + 前端页面
├── llm/                # 模型注册表与构造
├── runtime/            # 检查点持久化、会话元数据表与长期存储
└── workspace/          # Agent 的活动边界（文件读写、记忆、技能）
```

分层约定：`interfaces` → `application` → `agent` / `runtime`，上层依赖下层，下层不反向依赖。

---

## 九、已知限制

- 若你在此前的版本下打开或刷新过页面，库里可能残留 `title=''` 且 `turn_count=0` 的空会话，
  需一次性手工清理：`DELETE FROM thread_meta WHERE title = '' AND turn_count = 0;`
- 模型侧目前只接入 **DeepSeek** 一家供应商（其余 provider 未在注册表中注册）。
- `sandbox` 档位已实现 Tier 0（进程沙箱）与 Tier 1（WSL 发行版）：两者都只做资源与
  进程树管控，**不是安全边界**，必须与人工审批配合使用；进程级网络隔离在 Windows 上
  需要管理员权限建防火墙规则，本期未做。Tier 1 另有两条越出沙箱的通路：发行版经
  `/mnt` 可读写宿主文件，且 WSL interop 允许从 Linux 侧启动 Windows 程序。
- `docker` 沙箱档位尚未实现，显式指定会直接报错（不静默降级）。
- 检查点为单机 SQLite，多副本部署需另行替换为共享存储（如 PostgreSQL 检查点）。
