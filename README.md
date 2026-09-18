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
| 会话整理 | 标题搜索、重命名、归档（软删除，可恢复）与「含已归档」开关；归档不影响历史、用量与运行 |
| 长期记忆 | 两层：`workspace/AGENTS.md`（人工维护，随部署走）+ `/memories/`（Agent 自写，**按用户隔离**并落 SQLite，重启不丢）；Web 端「记忆」面板可查看与删除 |
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
| 宿主机 `.env` | `AUTH_SESSION_SECRET`、`AUTH_API_KEY_DEV` 等既有配置 | compose 的 `env_file: .env` |
| 宿主机环境变量 | `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | compose 的 `${VAR:-}` 透传 |
| compose `environment` | `HOST` / `PORT` / `AUTH_MODE` / `DB_PATH` / `WORKSPACE` / `MEMORY_FILE` | 覆盖上面两处的取值 |

两类变量名与 `.env` 里完全一致（配置是扁平的，环境变量名就是字段名大写）。

容器化默认 **`AUTH_MODE=apikey`**，不跟随本机开发用的 `disabled`——端口一旦映射到
宿主机，网络边界比本机进程宽得多。因此 `AUTH_SESSION_SECRET` 必须已设置（≥32 字节，
`auth_mode != disabled` 时必填），否则服务会拒绝启动。

**执行档位按 `.env` 原样生效，compose 不覆盖它。** 本仓库 `.env` 里是
`EXECUTION_MODE=disabled`，容器因此以该档位启动，`execute` 工具调用会直接返回错误
（启动日志里有 `执行档位=disabled：execute 工具调用将返回错误`）；文件类工具不受影响。

**当前不要改用 `local` 档位**：在本仓库钉住的 `deepagents 0.7.14` 下，`local`
（提供命令执行的 backend）与工具级权限配置**互不兼容**，Agent 构建阶段就会失败：

```
NotImplementedError: FilesystemMiddleware does not yet support permissions with backends
that provide command execution (SandboxBackendProtocol).
```

这不是容器特有的——在宿主机上以同样的档位运行 `main.py cli` 会得到同一处报错。
上游要么支持该组合、要么我们放弃工具级权限，二选一之前 `local` 与 `sandbox`
都用不了。这是独立于容器化的既有问题，已知限制见 `docs/planning`。

WHY 编排不替使用者改这个值：镜像里有模型密钥、容器有网络出口，而允许执行命令意味着
一次提示词注入就能在容器里跑命令。这类开关应按各自的威胁模型显式打开，不跟着编排文件默认开启。

#### 卷

| 卷 | 容器内路径 | 内容 |
| --- | --- | --- |
| `agent-data` | `/app/.data` | SQLite 数据库（会话、检查点、用量、审计）与审计归档 |
| `agent-workspace` | `/app/workspace` | Agent 的工作区：产出文件与 `_tool_outputs/` 留存 |

用命名卷而不是绑定挂载，是因为容器以 uid 10001 运行，绑定挂载的目录属主由宿主机决定
（常见结果是「起得来但写不进去」）；命名卷首次挂载会继承镜像里该目录的属主。

想在宿主机上直接翻看 Agent 产出的文件，把 compose 里的 `agent-workspace` 换成绑定挂载，
并把宿主机目录交给同一位用户：

```bash
mkdir -p ./workspace && sudo chown -R 10001:10001 ./workspace
```

#### 容器内跑 CLI（apikey 模式）

CLI 通过 `HARNESS_API_KEY` 取凭据。最省事的入口是 `.env` 里的 `AUTH_API_KEY_DEV`
（单 key 快速通道，角色为 `admin`）；生产环境应把它留空，改用管理面板创建真实 Key：

```bash
docker compose exec -e HARNESS_API_KEY="$AUTH_API_KEY_DEV" agent python main.py cli
```

Web 接口在同一个容器里，无需另起进程。

#### 可选：自带的 Authentik（OIDC 登录）

`oidc` 档位会在同一个编排里再起一套 Authentik（`server` + `worker` + `postgresql`，
版本钉在 `2026.8.2`；实测这套配置**不需要 Redis**）。默认不启动——一个近 2 GB 的 IdP
不该在只想聊两句时被拉起来。

```bash
# 1) .env 里打开 OIDC，并把 issuer 指向本机地址（下面解释为什么必须是地址）
AUTH_MODE=oidc
OIDC_ISSUER=http://<本机局域网地址>:9000/application/o/myagentharness/

# 2) 起 IdP + 应用
docker compose --profile oidc up -d

# 3) 在 IdP 里声明应用与提供方（可重复执行）
docker compose --profile oidc cp scripts/bootstrap_authentik.py server:/tmp/bootstrap_authentik.py
docker compose --profile oidc exec -T server ak shell -c "exec(open('/tmp/bootstrap_authentik.py').read())"
```

然后浏览器打开 `http://localhost:8000` 点登录，用 **`akadmin`** 登录——初始密码在 `.env`
的 `AUTHENTIK_BOOTSTRAP_PASSWORD`（首次启动时生成，请按密钥对待）。

##### issuer 为什么必须是本机地址

OIDC 的 discovery 文档里，`authorization_endpoint` 给**浏览器**用、`token_endpoint` 与
`jwks_uri` 给**容器**用，而它们是同一个 host——所以这个 host 必须两侧都解析得到。
实测三种取法（Authentik 未设品牌域名时，issuer 跟随请求 Host）：

| issuer 里的 host | 浏览器 | 容器 | 结果 |
| --- | --- | --- | --- |
| `127.0.0.1` | ✓（且绕过本机代理） | ✗ 容器里的回环是它自己 | 不可用 |
| `xxx.localtest.me`（解析到回环的公共通配域名） | ✗ 被本机代理接走（实测 502） | ✓ | 不可用 |
| `<本机局域网地址>` | ✓（`192.168.*` 等私有段默认在代理绕过列表里） | ✓ | **可用** |

代价是 IdP 的登录页监听在宿主所有网卡上（这与既有部署一致：原编排同样绑 `0.0.0.0`）。
要收回局域网暴露，给它一个专用名字并同时绕过代理：在 hosts 里加
`127.0.0.1 authentik.local`，在「Internet 选项 → 代理 → 不走代理的地址」里加
`authentik.local`，然后改用 `OIDC_ISSUER=http://authentik.local:9000/...`。

##### 已验证到什么程度

已验证：容器能取到 discovery 且 issuer 与配置逐字符一致；`/auth/login` 会 307 到
Authentik 的授权地址（带 PKCE/nonce/state）；IdP 接受该请求并 302 进入登录流程。
**未验证**：在浏览器里实际输入用户名密码完成整条登录链——那一步需要你的凭据。

过程中一处只有真机才会暴露的坑已写进 `scripts/bootstrap_authentik.py` 的注释：用 ORM 直接
创建的提供方 `grant_types` 是空列表（界面上是勾选项），授权码流程会被 IdP 拒绝，
而调用方只看到一句 `invalid_request`。

#### 一次性维护命令

```bash
# 给历史会话补归属（从单用户升级到多用户时执行一次）
docker compose exec agent python scripts/migrate_thread_owners.py

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
| `WORKSPACE` | `./workspace` | 文件工具的根目录（活动边界） |
| `MEMORY_FILE` | `./workspace/AGENTS.md` | 长期记忆文件 |
| `DB_PATH` | `./.data/agent.db` | SQLite 数据库（检查点 + 会话元数据 + 审计 + 用量 + **长期记忆**） |
| `SKILL_DIRS` | `./workspace/skills` | 技能目录，按顺序查找；多目录用路径分隔符（Windows `;` / POSIX `:`） |
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
| `GET` | `/api/tools` | 列出生效工具（内置 + 自定义 + MCP）及每台 MCP 服务器的加载状态（需 `tool:read`） |
| `GET` | `/api/memories` | 列出**当前主体自己**的长期记忆（需 `memory:read`） |
| `DELETE` | `/api/memories/{path}` | 删除一条长期记忆（需 `memory:delete`）；删除不存在的条目仍返回 200 |
| `POST` | `/api/threads` | 申请一个会话 ID；**不落库**，会话在首条消息被接受时才创建 |
| `GET` | `/api/threads` | 列出会话（最近活动在前，支持 `limit` / `offset` / `query` 标题搜索 / `include_archived`），默认不含已归档 |
| `PATCH` | `/api/threads/{thread_id}` | 重命名（`title`）或归档（`archived`），需 `thread:update`；两项至少要给一项 |
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
- 模型侧**恒定注册 DeepSeek**，OpenAI / Anthropic / Ollama 按「是否提供密钥或地址」
  条件注册（未配置则不出现在下拉框），避免把配置错误转嫁给终端用户；运行时故障
  转移（调用失败自动切换供应商）尚未实现。
- `sandbox` 档位已实现 Tier 0（进程沙箱）与 Tier 1（WSL 发行版）：两者都只做资源与
  进程树管控，**不是安全边界**，必须与人工审批配合使用；进程级网络隔离在 Windows 上
  需要管理员权限建防火墙规则，本期未做。Tier 1 另有两条越出沙箱的通路：发行版经
  `/mnt` 可读写宿主文件，且 WSL interop 允许从 Linux 侧启动 Windows 程序。
- 会话搜索**只匹配标题**，不搜消息正文：正文存在检查点的 msgpack BLOB 里，检索需要
  逐条反序列化，成本与「查一张窄表」不是一个量级。要让正文可搜，需要额外建索引
  （单独一件事，本项未做）。
- 归档只是「从默认清单里收起来」：**不影响**正在运行的任务、历史读取与用量记录。
  删除会话同样不会中断正在进行的运行——运行持有自己的图与检查点句柄，删除后它仍会
  跑到结束（事件照常推给发起方），只是会话元数据已不在清单里。
- 删除会话会**保留**其用量记录：成本台账不应随会话消失，否则「花了多少」会随着
  用户清理清单而变小。代价是删除后无法再按该会话 ID 过滤用量（聚合里仍在）。
- `docker` 沙箱档位尚未实现，显式指定会直接报错（不静默降级）。
- 检查点为单机 SQLite，多副本部署需另行替换为共享存储（如 PostgreSQL 检查点）；
  长期记忆与它共用同一个文件，多副本部署时需一并替换。
- 长期记忆按主体隔离，但**没有**「管理员查看/清理他人记忆」的入口：跨主体读取属于
  另一类授权与审计设计，本期只做「自己看自己删」。主体标识含命名空间不允许的字符
  （例如某些 IdP 的 `sub` 形如 `auth0|abc`）时会改用 sha256 前 32 位作为命名空间
  组件，此时无法从命名空间反推主体，排障需对照日志里的告警。
