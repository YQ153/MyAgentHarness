# MyAgentHarness

一个可直接运行的**通用 Agent 骨架**：以 [deepagents](https://github.com/langchain-ai/deepagents) 为内核，
自带文件读写、待办规划、子任务委派与人工审批（HITL）能力，同时提供 **命令行（CLI）** 与 **Web 服务** 两种运行形态。

两种形态共享同一套内核（`agent/` + `application/`），差异只在适配器，
装配集中在唯一的组装点 `bootstrap/`；因此同一份会话状态、同一套审批语义在两种入口下完全一致。

---

## 一、功能特性

| 能力 | 说明 |
| --- | --- |
| 流式对话 | 模型输出以 SSE / 逐字打印的方式实时呈现，不等待整轮结束 |
| 文件工具 | 读写、编辑、列目录等全部收敛到**用户工作区**内，越权访问直接拒绝 |
| 待办与规划 | 任务拆解为待办列表并实时回传快照，长任务过程可见 |
| 子任务委派 | 复杂任务可拆给子 Agent 并行处理，避免主上下文被细节撑爆 |
| 人工审批（HITL） | 高危工具调用前暂停并请求确认，支持**批准 / 改写 / 拒绝 / 代答**四种决策 |
| 会话持久化 | 基于 SQLite 异步检查点，进程重启后可续聊；刷新页面不丢上下文 |
| 会话清单 | 独立元数据表登记标题、创建/活动时间与对话轮数；只收录**真正发过消息**的会话，Web 侧栏可切换历史 |
| 会话整理 | 标题搜索、重命名、归档（软删除，可恢复）与「含已归档」开关；归档不影响历史、用量与运行 |
| 长期记忆 | 三层：**全局** `AGENTS.md`（`MEMORY_FILE` 指定，人工维护、跨全部会话共享，以只读挂载 `/global/` 交给 Agent）+ **工作区** `AGENTS.md`（未配全局记忆时生效，随项目走）+ `/memories/`（Agent 自写，落 SQLite，重启不丢）；Web 端「记忆」面板可查看与删除 |
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

编辑 `.env`，**至少**填入一项：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
```

> **工作空间由你在建会话时挑，配置里没有「默认工作区」这一项。** 两条规则：
>
> - 会话可以不绑定工作空间 —— 应用会在 `<数据目录>/sessions/<会话 ID>/` 下给它建一个
>   **专属目录**（可用 `SESSIONS_ROOT` 改父目录）。那条会话的文件、附件与索引都落在那里。
> - 会话也可以绑定**任意一个已存在的目录**（不限制路径范围）。同一个工作空间可以承载
>   多条会话 —— 想在同一个项目里开几个会话就开几个。
>
> 根在**产生第一条交互时永久锁定**：此后给出不同的取值会被拒绝（`409`），换项目请新建会话
> ——同一条会话的产物、附件、技能视图与索引都锚在一个根上，中途换根会把半条会话留在旧目录、
> 半条写到新目录，而两侧都不会报错。
>
> 仓库里的 `workspace/` 只是本机示例目录（含示例记忆模板与示例技能）：配置里**没有**指向它的
> 默认工作空间（那个键已移除，残留的话启动会以 WARNING 点名）。只有 `MEMORY_FILE` 会显式指到
> 里面的 `AGENTS.md`——那是**全局长期记忆**（Agent 只读、对所有会话生效），与会话的文件根是
> 两件事。

> 配置读取优先级：`环境变量 > .env 文件 > 字段默认值`。
> 详细配置项见下文「配置参考」。

---

## 四、启动命令

统一入口是 `main.py`，下有 `cli` 与 `web` 两个子命令。

### 1. 命令行交互

```bash
uv run python main.py cli

# 显式指定本次会话的工作空间（一条 CLI 进程就是一条会话，因此选择发生在启动时）
uv run python main.py cli --workspace D:/projects/my-app

# 指定本次会话使用的模型（默认取配置中的 default_model）
uv run python main.py cli --model deepseek-flash

# 调整日志级别（--log-level 是全局参数，要写在子命令之前）
uv run python main.py --log-level DEBUG cli
```

> Windows 下也可直接用虚拟环境解释器：`.venv\Scripts\python.exe main.py cli`

交互方式：

```
通用 Agent 已启动，输入 exit 退出。
工作空间：D:\projects\my-app          # 不传 --workspace 时这里显示它的专属目录
执行档位：disabled
当前模型：deepseek-flash
会话 ID：83cb82b40ea04ea7bbf96e81fe872c4a

> 帮我看一下这个项目里有什么
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

**新建会话时**可以在左侧挑一个工作空间，也可以什么都不选：

- **挑了**：那条会话的文件、附件与索引都落在你选的项目目录里。同一个工作空间可以承载多条
  会话。
- **没挑**：应用在 `SESSIONS_ROOT`（默认 `<数据目录>/sessions`）下按会话 ID 建一个专属目录，
  只有这条会话用它。界面上会明确写出「未选择 —— 本条会话将使用应用为它创建的专属目录」，
  免得你事后在自己的项目里找不到 Agent 刚产出的文件。**不选就直接发第一条消息也可以**：
  那个目录在解析时按需创建（附件与产物都落在里面），不需要先做别的操作。
- **应用不往你挑的目录里写自己的东西**：技能库、技能视图与工具输出留存都住在数据目录的
  **根外存储**里（`<数据目录>/roots/<根标识>/{skills,skills-active,tool-outputs}`），再以
  只读虚拟路径（`/skills`、`/.skills-active`、`/_tool_outputs`）接回 Agent 的视野。技能包
  请放进 `roots/<根标识>/skills/`（路径会打印在启动日志里）。升级说明：旧版本放在
  `<工作区>/skills/` 里的技能包会在该根第一次被使用时**自动搬过来**；`.skills-active`（派生物）
  与 `_tool_outputs`（最近的输出留存）不迁移——前者会重建，后者是缓存。
- 两个入口：**「选择文件夹…」**弹出操作系统的文件夹选择对话框；**「浏览…」**在网页里逐级
  挑选，也可以直接粘贴路径。任意目录都可以选 —— 没有允许清单，「不绑定」也是一个正常选项。
- **「选择文件夹…」弹的是原生对话框，但它出现在运行服务端的那台机器上**，不是浏览器这边。
  浏览器页面拿不到宿主的绝对路径（`<input webkitdirectory>` 只给相对名，File System
  Access API 只给一个 handle），所以只能由服务端进程在自己的桌面上弹。
  因此：本机自托管 = 正常；容器 / 无桌面的服务器 / 服务端与浏览器不在同一台机器上 =
  返回 `501`，这时用「浏览…」在网页里挑（它不受这个限制）。同一个进程一次只允许一个
  弹窗，重复请求返回 `409`。
- 选择随**首条消息**提交并**永久锁定**；此后再给出不同取值会被拒绝（`409`）。
  换项目请新建会话。
- 会话的根在**第一次用到它时**才落到磁盘上：没选工作空间的会话，专属目录由服务端在解析根的那一刻
  按需创建（因此点开一条还没发过消息的会话不会报错）；而你选定的那个目录不见了时会得到明确的
  `409` 与「恢复该目录，或删除这条会话」的提示，而不是一个悄悄建出来的同名空目录。
- 顶部「工作区」面板显示**当前会话**的根，并写清它属于哪一类
  （`工作空间：…` / `会话专属目录：…`）以及是否已锁定（`GET /api/workspace/info?thread_id=…`）。
  文件面板、附件、技能与知识库接口都按会话解析各自的根。

> **任意目录都可选**（这是产品规则）。所以：能访问这个服务的人，就能把 Agent 的文件根
> 指到宿主机任意位置。因此请只在本机可访问的形态下使用它；若必须对外暴露，请把它放在
> 有访问控制的网络边界之后。

会话由地址栏承载（形如 `/#/c/{thread_id}`）：刷新会**恢复**当前会话而不是新建，
「新会话」按钮只清空界面进入草稿态。只有真正发出第一条消息，服务端才会创建会话——
纯浏览、刷新页面、点「新会话」都不会留下任何空会话。

---

### 3. 容器化运行

```bash
docker compose up -d --build     # 首次构建并启动
docker compose ps                # 等 STATUS 出现 (healthy)
curl -s http://127.0.0.1:8000/ready
```

`/ready` 返回 **200** 表示数据库可访问且默认模型配置自洽（返回 503 时响应体里会写明
哪一项没就绪）。`/health` 是不查任何依赖的存活探针，容器 `HEALTHCHECK` 打的是它——
依赖抖动不该让编排系统反复重启容器。

**它长什么样**：镜像用多阶段构建（依赖装完只拷结果），以 **uid 10001 的非 root 用户**
运行，`Dockerfile` 里带 `HEALTHCHECK`。端口默认只绑 `127.0.0.1:8000`。

#### 环境变量

配置来源有两处，按优先级排列：

| 来源 | 内容 | 怎么进来的 |
| --- | --- | --- |
| 宿主机 `.env` | 模型别名、执行档位、沙箱与知识库等既有配置 | compose 的 `env_file: .env` |
| 宿主机环境变量 | `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | compose 的 `${VAR:-}` 透传 |
| compose `environment` | `HOST` / `PORT` / `DB_PATH` / `SESSIONS_ROOT` / `MEMORY_FILE` | 覆盖上面两处的取值 |

两类变量名与 `.env` 里完全一致（配置是扁平的，环境变量名就是字段名大写）。

**服务不区分调用方**：端口一旦映射到宿主机，网络边界就比本机进程宽得多，因此 compose
默认只绑 `127.0.0.1`。需要别的机器访问时，请自行在其前面提供访问控制。

**执行档位按 `.env` 原样生效，compose 不覆盖它。** 本仓库 `.env` 里是
`EXECUTION_MODE=disabled`，容器因此以该档位启动，`execute` 工具调用会直接返回错误
（启动日志里有 `执行档位=disabled：execute 工具调用将返回错误`）；文件类工具不受影响。

**`local` / `sandbox` 档位下不再应用工具级文件权限。** 上游 `deepagents 0.7.14` 拒绝
「文件权限规则 + 可提供命令执行的 backend」这一组合，并在装配期直接抛：

```
NotImplementedError: FilesystemMiddleware does not yet support permissions with backends
that provide command execution (SandboxBackendProtocol).
```

这不是上游的实现缺漏，而是刻意的口径：权限只作用于 `read_file` / `write_file` 等
**工具**，而 `execute` 走 shell，一条 `cat .env` 就能绕过全部路径规则——上游拒绝放行
一条纸面防线。本项目的处理是**显式裁剪**（不静默降级）：`agent/guardrails.py` 的
`build_permissions(backend)` 在 backend 具备执行能力时返回空规则并打 WARNING
（启动日志可见），凭据防护改由 `execute` 的人工审批与沙箱隔离承担。

因此两档的防护口径不同：

| 档位 | 工具级文件权限 | 敏感路径读取（工作区内的 `.env` / `.git` / 私钥） |
| --- | --- | --- |
| `disabled`（默认） | 生效 | 被**拒绝** |
| `local` / `sandbox` | 停用（有 WARNING） | 规则不再生效，防护由 `execute` 审批承担 |

**推论：不要把密钥放进 `workspace/`。** 仓库根的 `.env` 在虚拟根之外，两种口径下都
不会被 Agent 读到。

WHY 编排不替使用者改这个值：镜像里有模型密钥、容器有网络出口，而允许执行命令意味着
一次提示词注入就能在容器里跑命令。这类开关应按各自的威胁模型显式打开，不跟着编排文件默认开启。

回归验证：`python scripts/smoke_execution_modes.py`（三档装配与真实命令执行）、
`python scripts/probe_permissions_backend.py`（上游约束本身与豁免条件）。

#### 卷

| 卷 | 容器内路径 | 内容 |
| --- | --- | --- |
| `agent-data` | `/app/.data` | SQLite 数据库（会话、检查点、用量、审计、审计归档）**+ 未绑定工作空间的会话专属目录**（`/app/.data/sessions/`） |

用命名卷而不是绑定挂载，是因为容器以 uid 10001 运行，绑定挂载的目录属主由宿主机决定
（常见结果是「起得来但写不进去」）；命名卷首次挂载会继承镜像里该目录的属主。

> **只有一个卷是刻意的**：会话的库与「专属目录」同处一棵树，备份/搬迁只需要搬它。

想在容器里直接读写**宿主上的项目目录**，把那个目录绑定挂载进来（属主交给 uid 10001），
再在界面上新建会话时选它：

```bash
mkdir -p ./projects && sudo chown -R 10001:10001 ./projects
# compose 里加：  - ./projects:/work/projects
```

#### 容器内跑 CLI

CLI 直接使用与 Web 相同的配置（compose 的 `env_file` 会把它带进容器）：

```bash
docker compose exec agent python main.py cli
```

想临时换一个工作空间时用 `--workspace`：

```bash
docker compose exec agent python main.py cli --workspace /app/.data/sessions/demo
```

本机（非容器）同理：直接 `python main.py cli`。

Web 接口在同一个容器里，无需另起进程。

#### 一次性维护命令

```bash
# 查看日志 / 停止
docker compose logs -f agent
docker compose down            # 保留卷；加 -v 会连数据一起删
```

## 五、执行档位（安全相关）

`EXECUTION_MODE` 决定 `execute`（shell 命令执行）工具是否可用，**默认是关闭的**：

| 档位 | 含义 | 适用 |
| --- | --- | --- |
| `disabled` | `execute` 工具存在但调用即返回错误，且不开启审批通道 | **默认**，任何环境的起手档位 |
| `local` | 在宿主机直接执行 shell | **仅限本机 CLI 开发**，禁止用于 Web / 多租户 |
| `sandbox` | 在沙箱内执行 shell，隔离强度由 `SANDBOX_TIER` 决定 | 需要命令能力、又不愿裸跑宿主机的场景 |

> 安全边界：把 `local` 档位暴露到公网，等同于把宿主机 shell 开放出去，请务必谨慎。
>
> 权限口径：工具级文件权限（敏感路径拒绝）只在 `disabled` 档位生效。`local` /
> `sandbox` 档位下 `execute` 可经 shell 绕过路径规则，因此权限被显式停用（启动
> 日志有 WARNING），防护由审批承担——理由见第 四 章「容器化运行」中的说明。

### 沙箱档位（`SANDBOX_TIER`，仅 `sandbox` 档位生效）

| 档位 | 状态 | 隔离能力 |
| --- | --- | --- |
| `auto` | 默认 | 按 `wsl → process` 顺序探测，选中首个可用的最强档位。**不含 `docker`**，理由见下 |
| `docker` | **已实现（Tier 2）** | 一次性容器：**只有镜像与挂载进来的工作区可见**，网络默认切断，进程随容器结束而消失，内存 / CPU / 进程数上限，丢弃全部 capabilities |
| `wsl` | **已实现（Tier 1）** | 命令跑在 WSL2 发行版内：utility VM 边界 + Linux rlimit（进程数 / 内存 / CPU 时间），超时由 GNU `timeout` 终止整个进程组 |
| `process` | **已实现（Tier 0）** | Windows Job Object：进程树管控、活动进程数/内存/CPU 上限、超时终止整棵树 |

> **三个档位都不是「强隔离」，都不能替代安全边界。** 它们解决的是「命令失控」：
> fork bomb、无限输出、超时残留进程、环境变量泄漏。
>
> - Tier 0 / Tier 1 挡不住本地提权，也挡不住命令主动读取宿主上的其他文件——
>   Tier 1 的发行版经 `/mnt` 仍能读写宿主文件，且通过 WSL interop 可反向启动
>   Windows 程序。
> - Tier 2 **是第一档真正限制「命令能看见什么」的**：宿主其余路径在容器内不存在
>   （连仓库根的 `.env` 都读不到）。但它共享宿主内核，因此**挡不住内核漏洞逃逸、
>   侧信道，以及 Docker 守护进程本身被攻破**；容器内一条 `rm` 照样能删掉挂载进来的
>   工作区。
>
> 因此三个档位**都默认强制开启人工审批**（`SANDBOX_REQUIRE_APPROVAL=true`）。
> 审批回答的是「这条命令该不该跑」，隔离回答的是「跑起来失控了会怎样」——
> 两个问题，不能互相替代。

> 档位不可用时（宿主无 Docker、执行镜像未构建、WSL 发行版缺失）会**直接报错**，
> 不会静默降级到更弱的隔离——否则等同于伪造安全边界。`auto` 档位在候选探测失败
> 而回落时，会在日志里写明跳过了哪些档位。

回归验证：

```bash
python scripts/smoke_sandbox.py          # Tier 0 / Tier 1，两档共 21 项
python scripts/smoke_docker_sandbox.py   # Tier 2，19 项（含中止与残留检查）
```

`smoke_sandbox.py` 覆盖基本执行、环境变量清洗、超时终止、输出截断、进程数上限、
工作目录换算、写入工作区、backend 集成与护栏联动；`smoke_docker_sandbox.py` 覆盖
容器内执行、挂载范围越界被拒、断网、超时、中止无残留、失败即报错。

### Docker 沙箱（Tier 2）

**宿主需求**

- **Linux 容器模式**（`docker info` 的 `OSType=linux`）。Windows 宿主需 Docker Desktop +
  WSL2 后端。Windows 容器模式下起 Linux 镜像的报错是 `no matching manifest`，与
  「镜像不存在」长得一模一样——档位探测会先判掉它并给出明确原因。
- 实测可用：Docker Desktop 4.41.2 / Engine 28.1.1（`OSType=linux`）。所需特性
  （`--rm`、`--network none`、`-v ...:ro`、`docker kill`）都是老特性，但**没有实测过更低
  版本**，因此探测失败时给明确错误，而不是宣称「≥ 某版本即可」。
- 磁盘约 190 MB（执行镜像）。

**准备执行镜像**

```bash
python scripts/setup_sandbox_image.py
# 内网或镜像源受限时换基础镜像：
python scripts/setup_sandbox_image.py --base <可用的基础镜像>
```

镜像由 `docker/sandbox.Dockerfile` 定义，与**应用自身**的根 `Dockerfile` 分开：
那份的受众是「跑应用」，这份是「跑命令」，而后者越空越好——一次成功逃逸能碰到的东西，
就等于镜像里有什么。它只带 Python 与 coreutils，不预装编译器与网络工具。

**实际施加的约束**

| 约束 | 说明 |
| --- | --- |
| 挂载 | **仅**这条会话的文件根一个目录，挂到容器内 `/work`。根之外的 `cwd` 直接报错，不额外挂载迁就——多挂一个宿主目录，可见面就当场退回 Tier 0 的形态 |
| 只读挂载 | `SANDBOX_DOCKER_WORKSPACE_READ_ONLY=true` 时以 `:ro` 挂载。**默认读写**：`execute` 的主要用途就是跑脚本与构建，产物本来就落在工作区里，只读会让这个档位不可用 |
| 网络 | 默认 `--network none`；`SANDBOX_NETWORK_MODE=host` 时用 `--network host`。注意 `host` **仅在 Linux 宿主上语义正确**——Docker Desktop 下它指向 Linux VM，而不是 Windows 宿主 |
| 资源 | `--memory` / `--cpus` / `--pids-limit` 由 `SANDBOX_*` 策略换算 |
| 加固 | `--cap-drop ALL` + `--security-opt no-new-privileges` |
| 身份 | `SANDBOX_DOCKER_USER`（形如 `1000:1000`），留空用镜像默认身份。**Linux 宿主建议显式设置**：容器内以 root 写出文件时宿主侧属主是 root，之后宿主进程可能改不动挂载目录。Windows 宿主由 Docker Desktop 代为处理属主，留空即可 |
| 收尾 | `--rm` + 无论成败都显式 `docker rm -f`，保证宿主无残留容器 |
| 超时 / 中止 | 两者都走 `docker kill`（实测 0.32 s），并登记进 `abort_scope`。超时返回 `timed_out=True` + 退出码 124；中止**不会**被误记为超时 |

**为什么 `auto` 不含 `docker`**

`auto` 的既有候选（`wsl` / `process`）之间差异是渐进的，而容器档位一次性改变三件事：
网络被切断、宿主文件系统不可见、shell 从宿主方言变成 POSIX `sh`。把它放进 `auto`，
会让升级到本版本的用户在毫无预期的情况下遇到「我的构建命令突然连不上网」。那属于
**部署决定**，应当显式写下 `SANDBOX_TIER=docker`。

代价说清楚：`auto` 因此在装有 Docker 的机器上可能选到比实际可用的更弱的档位。

**已知的坑（真机验收踩出来的两条）**

- 容器的 `PATH` 只能来自镜像。宿主的 `PATH`（Windows 上是 `C:\...` 一串）被 `-e` 进
  Linux 容器后会覆盖镜像里正确的值，于是 `sleep` / `python` 全部找不到、命令以 127
  立即返回；这个缺陷**只在「Windows 宿主 + Linux 容器」下出现**（正是 Docker Desktop
  的默认形态），不报错也不告警。故本档位只透传平台中立的 `TZ` / `LANG`。
- 判断「镜像在不在」不能用 `docker image inspect <tag>`：本机实测它对**真实存在且能正常
  运行**的镜像返回 `No such image`（`docker image ls --filter` 与 `docker run` 均正常）。
  拿它当判据会把「镜像已就绪」误判成「镜像缺失」，从而把一个能跑的档位判死。

---

## 六、配置参考

全部配置集中在 `config.py`，通过 `.env` 或环境变量覆盖：

列表型变量支持**分隔符**与 **JSON 数组**两种写法：`SKILL_DIRS` 用路径分隔符
（Windows `;`、POSIX `:`，与 `PATH` 同口径——路径本身可能含逗号），
`SANDBOX_ENV_ALLOWLIST` / `CUSTOM_TOOL_MODULES` 用逗号；`MCP_SERVERS` 只接受
JSON 数组（元素是对象，没有分隔符能表达）。

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
| `SANDBOX_DOCKER_IMAGE` | `harness-sandbox:latest` | Tier 2 的执行镜像，由 `scripts/setup_sandbox_image.py` 构建 |
| `SANDBOX_DOCKER_WORKSPACE_READ_ONLY` | `false` | Tier 2 是否只读挂载工作区；默认读写（构建产物要落回工作区） |
| `SANDBOX_DOCKER_USER` | 空 | Tier 2 传给 `--user` 的值；**Linux 宿主建议设为宿主的 `uid:gid`**，避免容器内写出的文件在宿主侧属于 root |
| `SANDBOX_TIMEOUT` | `120` | 沙箱内单条命令超时（秒） |
| `SANDBOX_MAX_OUTPUT_BYTES` | `100000` | 沙箱 stdout/stderr 各自的截断阈值 |
| `SANDBOX_MAX_PROCESSES` | `64` | 活动进程数上限，防 fork bomb |
| `SANDBOX_MAX_MEMORY_MB` | `2048` | 单进程内存上限（MB） |
| `SANDBOX_CPU_PERCENT` | `50` | CPU 占用硬上限百分比（Windows Job Object） |
| `SANDBOX_ENV_ALLOWLIST` | `PATH,SYSTEMROOT,...` | 传入子进程的环境变量白名单（逗号分隔），其余一律不传 |
| `SANDBOX_NETWORK_MODE` | `none` | `none` 仅剔除代理类变量；进程级断网需管理员权限，本期不做 |
| `SANDBOX_REQUIRE_APPROVAL` | `true` | 沙箱档位下是否仍需人工审批 |
| `SHELL_TIMEOUT` | `120` | `local` 档位 shell 执行超时（秒） |
| `SHELL_MAX_OUTPUT_BYTES` | `100000` | `local` 档位 shell 输出截断阈值 |
| `MAX_MODEL_CALLS_PER_RUN` | `60` | 单轮最大模型调用次数，防死循环 |
| `RECURSION_LIMIT` | `100` | 图递归深度上限 |
| `SESSIONS_ROOT` | `<数据目录>/sessions` | **未绑定工作空间**的会话，其专属目录的父目录；每个会话一个子目录（名字就是会话 ID）。WHY 跟着数据目录走：会话的检查点、审计与用量都在那里，备份/搬迁只需要搬一个目录 |
| `MEMORY_FILE` | 空 | **全局**长期记忆文件：人工维护、Agent 只读、对**全部会话**生效。它**不必**位于工作区内——应用把它只读挂载到 `/global/AGENTS.md`（Agent 改不动它）。留空则改用各会话根下的 `AGENTS.md`（工作区自带，只对该会话生效）；文件不存在时只记 WARNING 并跳过加载 |

> 配置里**没有**「默认工作区」这一项：会话的根由它自己决定（用户在建会话时挑的工作空间，
> 或应用为它建的专属目录）。有一个配置级默认值，等于把「用户没选」偷偷变成「用户选了
> 配置里那个」，而这两种选择本该落到不同的根上。
>
> `.env` 里**不被任何字段读取**的键会在启动时以 WARNING 点名列出（多半是旧版本的残留，
> 例如已移除的 `WORKSPACE`）。静默忽略未知键是必要的——同一份 `.env` 常混着别的工具的
> 变量——但一声不响会让「配置写着某个已不存在的项」被当成生效。
| `DB_PATH` | `./.data/agent.db` | SQLite 数据库（检查点 + 会话元数据 + 审计 + 用量 + **长期记忆**） |
| `SKILL_DIRS` | `<应用目录>/skills-builtin` + 本根的技能库 | 技能目录，按顺序查找（越靠后优先级越高）；多目录用路径分隔符（Windows `;` / POSIX `:`）。默认两项是内置技能目录与 `<数据目录>/roots/<根标识>/skills`；目录都在工作区之外，由程序挂只读虚拟路径后读取 |
| `THREAD_TITLE_MAX_CHARS` | `24` | **自动生成**标题的字符上限，超出以省略号截断 |
| `THREAD_RENAME_MAX_CHARS` | `120` | **手动改名**允许的字符上限；超长直接报错（不静默截断用户输入） |
| `AUDIT_RETENTION_DAYS` | `180` | 审计日志保留天数，超期记录由定期任务删除 |
| `AUDIT_RETENTION_INTERVAL_SECONDS` | `86400` | 审计保留清理任务的执行间隔（秒） |
| `CUSTOM_TOOL_MODULES` | 空 | 自定义工具模块（点分路径，逗号分隔） |
| `MCP_ENABLED` | `true` | MCP 总开关；未配置任何 server 时取何值都不建连 |
| `MCP_SERVERS` | 空 | MCP 服务器清单（JSON 数组，见下节） |
| `MCP_TOOL_NAME_PREFIX` | `true` | 为 MCP 工具名加 `服务器名_` 前缀，让同名冲突在注册期显式报错 |
| `MCP_LOAD_TIMEOUT_SECONDS` | `15` | 单台 MCP server 拉取工具清单的超时（秒） |
| `MCP_FAIL_FAST` | `false` | 某台 server 加载失败时是否阻断启动；默认降级并写 ERROR 日志 |
| `TOOL_AUDIT_BUILTIN` | `false` | 是否把内置工具（`read_file` / `execute` 等）的调用也写入审计 |
| `WEB_SEARCH_PROVIDER` | `none` | 检索 provider：`none` / `tavily` / `searxng`；取值决定检索工具是否注册 |
| `WEB_SEARCH_API_KEY` | 空 | `tavily` 的密钥；缺失时检索工具不注册（清单里不会出现「点了才报缺密钥」的条目） |
| `WEB_SEARCH_BASE_URL` | 空 | 检索服务地址；`searxng` 必填（如 `http://127.0.0.1:8888`），`tavily` 留空用官方地址 |
| `WEB_SEARCH_TIMEOUT_SECONDS` | `15` | 单次检索请求超时（秒） |
| `WEB_SEARCH_MAX_RESULTS` | `5` | 检索结果条数上限（结果会整体进入上下文） |
| `WEB_FETCH_TIMEOUT_SECONDS` | `20` | 单次抓取请求超时（秒） |
| `WEB_FETCH_MAX_CHARS` | `20000` | 抓取正文的字符上限，超出部分截断并显式标注 |
| `WEB_FETCH_MAX_REDIRECTS` | `3` | 跟随重定向的跳数上限；**每一跳都复检出站安全** |
| `WEB_USER_AGENT` | 空 | 出站请求的 User-Agent；留空用内置默认值 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Web 监听地址与端口 |
| `LOG_LEVEL` | `INFO` | 日志级别：DEBUG/INFO/WARNING/ERROR |

### 工具扩展（自定义工具 / MCP）

工具集在**启动期**装配完成，装配失败会让进程起不来（除 MCP 降级外）——
带着一个「少了工具」的 Agent 继续服务，只会把失败推迟到某次具体对话。

- **自定义工具**：`CUSTOM_TOOL_MODULES=myapp.tools.weather,myapp.tools.others`
  （也接受 JSON 数组写法）。被导入的模块需提供 `TOOLS`（工具或可调用对象列表）
  或 `register_tools(registry)` 函数，二者之一。普通函数按类型注解生成参数
  schema，无需额外包装。
  钩子也可以声明第二个参数 `register_tools(registry, config)` 从而拿到配置对象——
  需要按配置决定「注册哪些工具、用什么阈值」的模块**必须**用这种写法：`.env` 里的值
  只进配置对象、不进 `os.environ`，模块自己去读环境变量是读不到的。
- **内置联网工具**：仓库自带 `web_tools` 模块（检索 + 抓取），**默认不加载**，
  需要显式列入：`CUSTOM_TOOL_MODULES=web_tools`。
  - `web_search` 只在 `WEB_SEARCH_PROVIDER` 已选且所需配置齐全时注册；
  - `web_fetch` 加载即可用，但**只接受 http/https 的公网地址**：内网、回环、链路本地、
    组播地址与云元数据端点一律拒绝（防 SSRF），重定向逐跳复检且不超过设定跳数；
  - 信任边界：抓取的地址由模型给出，属不可信输入；`WEB_SEARCH_BASE_URL` 由运营方填写，
    属受信配置（自建 SearXNG 跑在 `127.0.0.1` 是正当用法，不会被拦）。
- **MCP 服务器**：`MCP_SERVERS` 是一个 JSON 数组，每项至少要有 `name`
  （仅允许 `[A-Za-z0-9_-]`，且必须唯一）与 `transport`：

```jsonc
// stdio：必须提供 command；env 不继承宿主环境，需要什么就写什么，
// 避免宿主机的 *_API_KEY 被第三方进程读走
[{"name": "filesystem", "transport": "stdio", "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}]

// sse / streamable_http / websocket：必须提供 url，headers 常用于承载令牌
[{"name": "remote", "transport": "streamable_http",
  "url": "https://mcp.example.com/mcp",
  "headers": {"Authorization": "Bearer xxx"}}]
```

- **冲突处理**：自定义工具与 deepagents 内置工具同名，或两台服务器提供同名工具
  时，注册期直接抛错而不是静默覆盖（后者会让内置工具「消失」，表现为
  「Agent 突然不会读文件了」）。
- **失败降级**：每台 MCP server 单独拉取、单独记录状态。任一台挂掉不会牵连
  其余服务器；默认不阻断启动，失败原因通过 `GET /api/tools` 暴露。
- **审计**：工具调用落 `tool_call` 审计，含 `tool` / `source` / `server` /
  `status` / `elapsed_ms` 与截断后的 `args_preview`；未等到结果的调用记为
  `interrupted`，工具报错记为 `error`。内置工具默认不落库。

---

## 七、HTTP API

Web 形态对外提供以下接口（均以 `/api` 为前缀）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/models` | 列出可切换的模型（不含任何密钥信息） |
| `GET` | `/api/workspaces/dirs` | 列出一个目录下的**子目录**（一次一层），供界面逐级挑选工作空间；不传 `path` 返回起点（Windows 盘符列表 / POSIX `/`）。不存在的路径 → `400` |
| `POST` | `/api/workspaces/pick` | 在**服务端**弹出系统的文件夹选择对话框，返回选中的路径。用户取消 → `200 {"cancelled": true}`；环境不支持（无桌面 / 缺 tkinter）→ `501`；已有弹窗在等待 → `409`；超时 → `504` |
| `GET` | `/api/tools` | 列出生效工具（内置 + 自定义 + MCP）及每台 MCP 服务器的加载状态 |
| `GET` | `/api/audit` | 读取审计日志（最近在前，支持 `actor_id` / `event_type` / `limit` / `offset`）；只读 |
| `GET` | `/api/memories` | 列出长期记忆 |
| `DELETE` | `/api/memories/{path}` | 删除一条长期记忆；删除不存在的条目仍返回 200 |
| `POST` | `/api/threads` | 申请一个会话 ID；**不落库**，会话在首条消息被接受时才创建 |
| `GET` | `/api/threads/{thread_id}/export` | 导出会话为可移植 JSON（`format=json` 或 `markdown`）。文件里记有来源工作区供人查看，**不作为**导入后的绑定值 |
| `POST` | `/api/threads/import` | 导入一份导出快照为**新会话**；可用查询参数 `workspace` 指定它绑定到哪个工作空间，缺省表示不绑定（用它的专属目录） |
| `GET` | `/api/threads` | 列出会话（最近活动在前，支持 `limit` / `offset` / `query` 标题搜索 / `include_archived`），默认不含已归档 |
| `PATCH` | `/api/threads/{thread_id}` | 重命名（`title`）/ 归档（`archived`）/ 打标签（`tags`），三项至少要给一项 |
| `GET` | `/api/threads/{thread_id}` | 读取会话历史，用于刷新后恢复上下文 |
| `DELETE` | `/api/threads/{thread_id}` | 删除会话及其检查点 |
| `POST` | `/api/threads/{thread_id}/runs` | 发起一轮对话，**以 SSE 流式返回** |
| `POST` | `/api/threads/{thread_id}/resume` | 提交人工审批结果，继续被中断的运行 |
| `POST` | `/api/threads/{thread_id}/stop` | 请求停止当前运行；幂等返回 200，未运行时 `stopped=false` |
| `GET` | `/api/attachments/limits` | 附件上限（大小 / 张数 / MIME 白名单）；不依赖会话，草稿态也能取 |
| `POST` | `/api/threads/{thread_id}/attachments` | 上传附件（`multipart/form-data`，字段名 `file`） |
| `GET` | `/api/threads/{thread_id}/attachments` | 该会话的附件清单及其上限 |
| `DELETE` | `/api/threads/{thread_id}/attachments/{attachment_id}` | 删除附件；幂等，不存在时 `deleted=false` |

发起对话的请求体：

```json
{ "content": "帮我看一下这个项目里有什么", "model": null }
```

新建会话时用 `workspace` 指定这条会话要用的工作空间（任意已存在的目录）：

```json
{ "content": "跑一下测试", "model": null, "workspace": "D:/projects/my-app" }
```

> `workspace` **只在会话首条消息上生效，且此后永久锁定**。省略表示不绑定 —— 这条会话
> 将使用应用为它创建的专属目录；对已有根的会话给出不同取值会返回 `409`（换项目请新建会话），
> 路径不存在会返回 `400`。
> 上传附件时可以另带 `?workspace=<路径>` 查询参数——附件在首条消息**之前**上传，那一刻
> 服务端还不知道这条会话将绑定到哪，不带的话它只能落进那条会话的专属目录里。

带图片时把上传得到的 `id` 放进 `attachment_ids`（目标模型必须支持图片输入，否则
整体返回 400 并指明可用的多模态模型——**不会静默丢弃图片**）：

```json
{ "content": "这张图里是什么？", "model": "openai", "attachment_ids": ["<上传返回的 id>"] }
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

### 附件与多模态输入

- **存放**：`<工作区>/.attachments/<thread_id>/`，每个附件两个文件（内容 + 元数据）。
  路径校验复用工作区文件面板的同一套口径——附件不是第二个目录穿越入口。
- **上限**：`ATTACHMENT_MAX_BYTES`（默认 2 MB）、`ATTACHMENT_MAX_PER_THREAD`（默认 8）、
  `ATTACHMENT_ALLOWED_MIME_TYPES`（默认四种图片类型），全部走配置，不硬编码。
- **能力判定**：`VISION_MODEL_ALIASES`（默认 `openai,anthropic`）声明哪些**模型别名**
  接受图片输入——能力取决于实际模型名，而模型名是可改的，写死会在换模型后继续放行上传。
  `GET /api/models` 会下发 `supports_vision`，前端据此禁用上传入口。
- **审计**：上传落一条 `attachment_upload`，含文件名 / 大小 / MIME / sha256，
  **不含文件内容**（图片字节只存在于工作区）。
- **前端**：拖拽或点「＋」选图，缩略图贴在输入区上方；上传在**发送时**发生（草稿态还没有
  会话 ID）。上传失败的条目留在原地，可点角标重试，且这条消息不会发出去——
  「发出去了但图没带上」比一次失败更难察觉。

### 长期记忆：Agent 记住了什么，用户说了算

Agent 通过 `write_file` 写进 `/memories/` 的内容会**跨会话保留**，并进入后续每一轮
上下文；这与 `AGENTS.md`（人工维护、随部署分发）是两层不同的东西。

- **存储**：落在 `DB_PATH` 指向的 SQLite（`langgraph-checkpoint-sqlite` 自带的 Store），
  进程重启后仍在。浏览器端的「记忆」按钮打开面板，可查看正文并逐条删除，
  删除会写 `memory_delete` 审计事件。
- **隔离**：命名空间按主体收敛（`("memories", <user_id>)`），认证关闭时统一落在
  `__anonymous__`。因此**跨用户不可见**，管理员在面板里同样只看得到自己的那一份。
- **路径口径**：面板返回的 `path` 形如 `/memories/notes.md`，删除时把它直接拼在
  `/api/memories` 后面即可（服务层同时接受相对挂载点的 `notes.md`）。
- **上限**：单次最多返回 200 条、单条正文最多 4000 字符，超出时响应里的
  `truncated` 为 `true`——截断与「记忆本来就这么少」必须能区分开。

---

## 八、目录结构

```
MyAgentHarness/
├── main.py                   # 进程入口：解析子命令 → 装载配置 → 日志 → 分发
├── config.py                 # 配置中心（唯一的环境变量解析处）
├── thread_utils.py           # 会话 ID 校验规则：接口 / 应用 / 存储三层共用
├── text_utils.py             # 文本规整：应用层与存储层共用
├── web_safety.py             # 出站地址 SSRF 校验：供联网工具复用
├── web_tools.py              # 内置联网工具（默认不加载）
├── knowledge_runtime.py      # 知识库的进程级服务句柄
├── knowledge_tools.py        # 知识库工具（默认不加载）
│
├── Dockerfile                # 应用镜像：多阶段构建，以非 root 用户运行
├── docker-compose.yml        # 编排：端口只绑定回环地址
├── docker/                   # 沙箱执行镜像（受众是"跑命令"，与应用镜像分开）
├── pyproject.toml            # 依赖声明（requires-python >= 3.14）
├── uv.lock                   # 精确锁定的依赖版本
├── .env.example              # 配置模板
├── README.md                 # 本文档
├── .importlinter             # 分层依赖契约（6 条；uv run lint-imports）
├── .github/workflows/ci.yml  # 双平台 CI：pytest + lint-imports
│
├── bootstrap/                # 组装层：唯一的装配点
│   ├── context.py            # ★ AppContext：装配完成的依赖集合（不可变）
│   ├── core.py               # ★ build_app_context：CLI 与 Web 共用
│   └── web.py                # Web 专有资源（审计清理、运行治理）
├── agent/                    # 内核层：模型后端、工具、护栏、图构建
├── application/              # 应用层：会话编排、运行推进、事件模型、中断编解码
├── interfaces/               # 接口层：两种适配器
│   ├── cli.py                # 命令行交互
│   └── web/                  # FastAPI 服务（REST + SSE）与前端页面
├── llm/                      # 模型层：模型注册表与嵌入后端
├── runtime/                  # 运行时层：检查点、各类 store、文件与沙箱
│   └── sandbox/              # 三档沙箱：进程 / WSL / 容器
├── scripts/                  # 运维与回归脚本（probe_* 探针、smoke_* 冒烟）
├── tests/                    # 测试（按被测层镜像组织）
├── docs/                     # 文档（含 docs/overview/architecture.html 结构总览）
├── skills-builtin/           # 随应用交付的内置技能（挂虚拟路径后由 Agent 读取）
└── workspace/                # 本机示例目录（示例记忆模板 + 示例用户技能）
```

> `workspace/` 只是仓库里的一份示例：配置里**没有**指向它的默认值，应用启动时也不会选它。
> 想拿它试跑，就在界面上新建会话时把它选成工作空间，或 `--workspace ./workspace`（仅 CLI）。

### 分层约定

依赖方向只有两条，`interfaces` 是唯一的上层：

- `interfaces` → `application` → `agent` / `runtime`
- `interfaces` → `bootstrap`（适配器调用组装点，拿已装配好的对象）

**`interfaces` 不得直接依赖 `runtime`**：基础设施由 `bootstrap` 装配后经 `AppContext`
注入。这条是 2026-09 分层重构的成果——此前它有 13 处越界，且更换存储实现要改接口层。

根级模块都**不属于任何层**，放在根级各有理由：

| 模块 | 为什么在根级 |
| --- | --- |
| `config.py` | 全应用的最底层，谁都可以依赖它 |
| `thread_utils.py` / `text_utils.py` | 规则被 `application` 与 `runtime` **同时**使用，放进任一层都会立刻构成循环 |
| `web_safety.py` | 被联网工具（一个根级模块）复用，而它本身只依赖标准库 |
| `web_tools.py` / `knowledge_tools.py` | 自定义工具插件，运行期按 `CUSTOM_TOOL_MODULES` 动态加载；只能依赖注册 SPI 与自己的服务句柄 |
| `knowledge_runtime.py` | 知识库需要整进程唯一的一份服务句柄，而工具扩展点没有注入依赖的通道 |
| `main.py` | 只做分发，装配必须经 `interfaces` → `bootstrap` |

### 规则是可执行的，不是文档里的共识

| 约束 | 载体 | 怎么跑 |
| --- | --- | --- |
| 六个包之间的依赖方向（6 条契约） | `.importlinter` | `uv run lint-imports` |
| 根级模块的依赖边界与角色 | `tests/test_root_module_contract.py` | `uv run pytest` |
| `application` 的层内纯度（契约组不依赖服务组） | `tests/application/test_layer_purity_contract.py` | `uv run pytest` |
| `application` 允许直接依赖的 `runtime` 符号（端口化之外的白名单） | `tests/application/test_runtime_port_contract.py` | `uv run pytest` |

> 为什么分成两处：import-linter 只接受「包」作为分析根（`root_packages` 必须是含
> `__init__.py` 的目录），单文件模块既不能列为分析根、也不能被契约引用（实测报
> `'config' is a module, not a package` 与 `Module 'config' does not exist`）。
> 根级模块因此由契约测试承担，两者跑在 CI 的同一个 job 里。

**改动依赖关系前先跑这两条命令**，而不是先猜。越界会在提交时立刻失败，
而不是在某次偶发的导入顺序下以循环导入的形式出现。
方案的来龙去脉见 `docs/architecture/架构遗留问题治理方案.md`。

---

## 九、已知限制

- 若你在此前的版本下打开或刷新过页面，库里可能残留 `title=''` 且 `turn_count=0` 的空会话，
  需一次性手工清理：`DELETE FROM thread_meta WHERE title = '' AND turn_count = 0;`
- 模型侧**恒定注册 DeepSeek**，OpenAI / Anthropic / Ollama 按「是否提供密钥或地址」
  条件注册（未配置则不出现在下拉框），避免把配置错误转嫁给终端用户；运行时故障
  转移（调用失败自动切换供应商）尚未实现。
- `sandbox` 档位已实现 Tier 0（进程沙箱）、Tier 1（WSL 发行版）与 Tier 2（容器）：
  三者都只做资源与进程树管控，**都不是安全边界**，必须与人工审批配合使用；进程级的
  网络隔离在 Windows 宿主上需要管理员权限建防火墙规则，本期未做。Tier 1 另有两条
  越出沙箱的通路：发行版经 `/mnt` 可读写宿主文件，且 WSL interop 允许从 Linux 侧启动
  Windows 程序。Tier 2 会切断网络并限制可见面，但它共享宿主内核——挡不住内核漏洞
  逃逸与侧信道，容器内也照样能改挂载进来的工作区。
- 会话搜索**只匹配标题**，不搜消息正文：正文存在检查点的 msgpack BLOB 里，检索需要
  逐条反序列化，成本与「查一张窄表」不是一个量级。要让正文可搜，需要额外建索引
  （单独一件事，本项未做）。
- 归档只是「从默认清单里收起来」：**不影响**正在运行的任务、历史读取与用量记录。
  删除会话同样不会中断正在进行的运行——运行持有自己的图与检查点句柄，删除后它仍会
  跑到结束（事件照常推给发起方），只是会话元数据已不在清单里。
- 删除会话会**保留**其用量记录：成本台账不应随会话消失，否则「花了多少」会随着
  用户清理清单而变小。代价是删除后无法再按该会话 ID 过滤用量（聚合里仍在）。
- 沙箱档位不可用时（宿主无 Docker、执行镜像未构建、WSL 发行版缺失）会**直接报错**，
  不静默降级到更弱的隔离——那等同于伪造安全边界。`auto` 的候选只含
  `wsl` → `process`，**不含 `docker`**：容器档位须显式写 `SANDBOX_TIER=docker`
  （理由见第五章）。
- 附件的回收挂在「删除会话」上（`<会话根>/.attachments/<thread_id>/`）。若一次上传之后
  运行**没能真正开始**（被限流、因模型不支持图片被 400 拒绝、或发送途中关掉页面），
  而该会话又是**从未登记过的草稿**，那批附件就会留在工作区里无人清理——草稿没有
  元数据行，也就没有可触发的删除入口。每个草稿最多 `ATTACHMENT_MAX_PER_THREAD` 个附件、
  单个不超过 `ATTACHMENT_MAX_BYTES`。前端在模型不支持图片时已禁用上传入口；
  通过 API 直接调用的场景需自行注意。
- 联网检索：**tavily 适配器已对真实服务验证**（`python scripts/smoke_web_chain.py` 可复跑）；
  **SearXNG 适配器仍未经真实实例验证**——本机无自建实例，公网实例全部不可达、Docker 也拉不到
  镜像。其请求构造（含 `format=json`）与响应解析已由用例钉住，接上可达实例后跑同一条脚本即可补齐。
- 检查点为单机 SQLite，多副本部署需另行替换为共享存储（如 PostgreSQL 检查点）；
  长期记忆与它共用同一个文件，多副本部署时需一并替换。
- 长期记忆只有一份（工作区里没有第二份）：记忆命名空间由进程内唯一的主体标识派生，
  因此**换一台机器不会自动带上另一台的记忆**——要迁移请搬数据目录（`DB_PATH` 所在处）。
