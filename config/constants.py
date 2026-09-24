"""配置层的共享常量：目录名、虚拟挂载前缀与默认白名单。

WHY 从 ``AppConfig`` 里拆出来：这些常量同时被 ``app_config``（派生路径、
目录准备）与 ``session_root``（挂载规划、旧布局迁移）引用，放在任何一个
使用方旁边都会让另一个反向依赖使用者。常量只有一份、落在包内最底层，
包内依赖方向才保持单向（``app_config`` / ``session_root`` / ``settings.*``
→ ``constants``，从不反向）。

包内约定：下划线开头的名字是**包内私有契约**——``app_config`` 与
``session_root`` 会显式导入它们（跨模块共享同一份口径），但对包外仍保持
不可见（门面 ``__init__.py`` 不导出任何下划线名字）。
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "TZ",
    "LANG",
)
"""沙箱环境变量白名单。

刻意不含 ``USERPROFILE`` / ``HOME`` / ``APPDATA``：这些变量会引导 git、
aws-cli 等工具去读用户目录下的凭据文件，属于「合法变量导致的凭据泄漏」。

WHY 定义在配置层而非 runtime 层：``runtime.sandbox`` 需要引用它构造策略，
而它又是配置项的默认值，放在下层会让 config 反向依赖 runtime。
"""

NETWORK_MODE_NONE = "none"
NETWORK_MODE_HOST = "host"
"""沙箱网络模式；``none`` 表示不向子进程传递代理类变量。"""

DEFAULT_ATTACHMENT_MIME_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)
"""默认允许上传的附件类型。

WHY 只放图片：这一项的实质约束是「模型能不能收下这种内容」。四种图片类型是各
provider 的多模态接口共同支持的集合；把 PDF / 文本也放进来会得到一个「上传成功、
但构造消息时才失败」的入口，而那正是本任务要消除的静默失败。
"""

_APP_ROOT = Path(__file__).resolve().parents[1]
"""应用自身的目录（源码树根，容器内为 ``/app``）。

WHY 用 ``__file__`` 定位而不是 ``./``：按进程工作目录解析的默认值会在「从别的目录
启动进程」时指向一个不存在的路径，而表现是内置资产凭空消失，没有任何报错。

WHY 是 ``parents[1]`` 而不是 ``parent``：本文件已下沉到 ``config/`` 包内一层，
应用根是它的上一级目录。
"""

BUILTIN_SKILLS_DIR: Path = _APP_ROOT / "skills" / "builtin"
"""随应用交付的**通用技能**目录（工作区之外，经 ``/skills-builtin`` 只读挂载）。

WHY 收进 ``skills/`` 这一层（2026-09-22 改）：技能现在分三类——通用（本目录）、场景预设
（``skills/presets/<场景>/``）、用户（工作区内 ``.harness/skills/``）。三者同处一棵
``skills/`` 树下，读代码的人一眼能看清「技能从哪来」；而原先那个与预设目录毫无关系的
顶层 ``skills-builtin/`` 做不到这一点。

WHY 不在用户工作区内：工作区由用户显式指定（可能是任意项目目录），把随产品交付的
资产写进别人的项目里既越界，也会在用户换一个工作区之后失效——而失效形态只是几行
WARNING，功能上是「内置技能装了却用不上」。
"""

PRESET_SKILLS_DIR: Path = _APP_ROOT / "skills" / "presets"
"""**场景预设技能**目录：每个一级子目录是一个场景（含 ``preset.toml`` 与可选技能包）。

WHY 与通用技能分开目录：场景是「一组技能 + 一句说明」的打包，而通用技能是每个场景都能
用的底座。混在同一个目录里，「哪些随产品交付、哪些属于某个场景」就只能靠命名约定去猜。
"""

_MEMORY_FILE_NAME = "AGENTS.md"
"""长期记忆文件的默认文件名（与 ``AGENTS.md`` 生态惯例一致）。"""

GLOBAL_MEMORY_PREFIX = "/global/"
"""**全局**长期记忆在虚拟文件系统里的挂载前缀。

WHY 需要独立前缀、不直接挂在虚拟根上：虚拟根就是**本会话的工作区**，``/AGENTS.md``
在那里表示「这个工作区自带的说明文件」。两者来源不同（一个跨全部会话、一个只对落在该
工作区的会话），挤到同一个路径上就只能二选一——而用户要的是「全局那份额外对我生效」。
"""

_ROOTS_STORE_DIR_NAME = "roots"
"""根外存储的父目录名（在数据目录下）：一个会话根一个子目录。

WHY 跟着数据目录、不做成配置项：它必须与数据目录**同卷**（搬迁/备份只搬数据目录这件事
才成立），而多一个配置项只会多一种「配到别的盘」的机会，换不来任何能力。
"""

_SKILLS_STORE_DIR_NAME = "skills"
"""技能库子目录名（位于某个根的存储目录里）：用户放技能包的地方。"""

_SKILL_VIEW_STORE_DIR_NAME = "skills-active"
"""技能视图子目录名（同上）：只把**启用中**的技能物化出来的派生物。"""

_TOOL_OUTPUTS_STORE_DIR_NAME = "tool-outputs"
"""工具输出留存子目录名（同上）。"""

HARNESS_DIR_NAME = ".harness"
"""应用数据在**工作区内**的目录名：技能库 / 技能视图 / 工具留存 / 知识库索引。

WHY 统一收在一个点目录里而不是散成三个名字（2026-09-22 改）：这些内容与工作空间强绑定
（技能是「这个项目常用的套路」、留存是「这个项目的运行记录」），跟着项目走才能让换机器、
换工作空间之后行为一致。收在一个目录下，文件面板只需隐藏一个名字，用户要在版本控制里
忽略它也只需写一行 ``.harness/``。

代价是**有意接受**的：工作区是用户的仓库，应用会往里写这个目录（2026-09-21 曾为此把它
搬到数据目录，本次按「内容应随项目走」的取舍搬回）。因此配套两条约束：面板默认隐藏它
（``WorkspaceService``），Agent 侧经只读路由 ``/.harness/`` 暴露、改不动。
"""

_KNOWLEDGE_DB_NAME = "knowledge.db"
"""知识库索引文件名（位于 ``.harness/`` 下）。

WHY 用固定名字、不再带根标识（2026-09-22 改）：旧布局里几十个根共用同一个数据目录，
只能靠 ``knowledge-<根标识>.db`` 区分；现在每个根各有一个 ``.harness/``，撞名不可能
发生。固定名字还让「重建索引 = 删掉这个文件」成为一句能对用户说清的话。
"""

VIRTUAL_SKILLS = "/skills"
"""技能库在虚拟文件系统里的挂载点（Agent 只读）。"""

VIRTUAL_BUILTIN_SKILLS = "/skills-builtin"
"""**通用技能**在虚拟文件系统里的挂载点（Agent 只读）。

WHY 名字保留 ``builtin`` 而不是跟着目录一起改：它是**图的技能来源与挂载表**共同使用的
契约（缓存下来的图持有这个字符串），改名会让已缓存的图指向不存在的路径；而目录搬家只是
宿主侧的事，不影响对外路径。
"""

VIRTUAL_PRESET_SKILLS = "/skills-presets"
"""**场景预设技能**在虚拟文件系统里的挂载前缀（Agent 只读）。

WHY 是前缀而不是一个固定挂载点：预设技能按场景分目录（``skills/presets/<场景>/``），只有
**当前场景**的那一个目录才是来源——挂整个 ``presets`` 目录会让所有场景的技能一起被发现
（表现为「选了 A 场景，B 场景的技能也在」）。挂载时在其后接场景 ID。
"""

VIRTUAL_SKILL_VIEW = "/.skills-active"
"""技能视图在虚拟文件系统里的挂载点：**建图时的技能来源就是它**。

WHY 名字带点：它是程序产物，不是用户的资料——虽然它已经不在工作区里（面板不会再遍历到
它），但保留这个前缀能让日志与虚拟路径一眼区分「技能包本体」与「它的派生物」。
"""

VIRTUAL_TOOL_OUTPUTS = "/_tool_outputs"
"""工具输出留存在虚拟文件系统里的挂载点。

WHY 名字带下划线：与技能视图同理——它是系统的旁路留存，不是任务成果；消息与日志里出现
``/_tool_outputs/...`` 时，读者应当立刻知道那是「可回取的完整输出」而不是用户文件。
"""

VIRTUAL_HARNESS = "/.harness"
"""``.harness`` 在虚拟文件系统里的挂载点（只读）。

WHY 必须单独挂它：``/skills`` / ``/.skills-active`` / ``/_tool_outputs`` 三条只读路由各自
只覆盖自己的前缀，而 Agent 仍可经工作区内的真实路径 ``/.harness/skills/...`` 走到同一批
文件——那条路走 default backend，是可写的。把整个 ``.harness`` 也挂成只读，才真正堵住
这个绕行口（服务端自己用宿主路径写，不受只读挂载影响）。
"""

_USER_SKILLS_DIR_NAME = "skills"
"""旧版本里「会话根下用户技能目录」的目录名。

WHY 还留着它：根外存储落地后，技能库搬到了 ``<数据目录>/roots/<根标识>/skills``，但
**工作区里可能还留着旧位置**——启动时要能认出它并把技能包搬过去（见
``SessionRoot._migrate_legacy_skills``）。名字另取一个常量会让迁移代码与旧布局对不上。
"""

_SESSIONS_DIR_NAME = "sessions"
"""未绑定工作空间的会话，其专属目录在数据目录下的默认位置。"""
