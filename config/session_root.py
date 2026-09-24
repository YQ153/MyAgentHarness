"""会话根与虚拟挂载：一个会话的文件根、只读挂载表与旧布局迁移。

从原 ``config.py`` 尾部（原 L219–230、L1551–2265）整体迁入：``SkillSource``、
``MountKind``、``VirtualMount``、``MemoryPlan`` 与 ``SessionRoot``。它们描述的是
「配置在某个会话根上的投影」——派生路径、挂载规划与技能来源——与 ``AppConfig``
的「进程级字段声明」是两个不同的职责，故分文件存放。

WHY ``SessionRoot`` 仍直接引用 ``AppConfig`` 的派生属性
（``skill_presets_dir`` / ``roots_store_root`` / ``skill_dirs``）：这些派生口径
只有一份，散到本文件各写一遍必然分叉。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from config.constants import (
    BUILTIN_SKILLS_DIR,
    GLOBAL_MEMORY_PREFIX,
    HARNESS_DIR_NAME,
    PRESET_SKILLS_DIR,
    VIRTUAL_BUILTIN_SKILLS,
    VIRTUAL_HARNESS,
    VIRTUAL_PRESET_SKILLS,
    VIRTUAL_SKILLS,
    VIRTUAL_SKILL_VIEW,
    VIRTUAL_TOOL_OUTPUTS,
    _KNOWLEDGE_DB_NAME,
    _MEMORY_FILE_NAME,
    _SKILLS_STORE_DIR_NAME,
    _SKILL_VIEW_STORE_DIR_NAME,
    _TOOL_OUTPUTS_STORE_DIR_NAME,
    _USER_SKILLS_DIR_NAME,
)
from config.parsing import _readable_dir_name

if TYPE_CHECKING:
    from config.app_config import AppConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillSource:
    """一个技能来源：宿主机上的真实目录 + 它在虚拟文件系统里的挂载路径。

    WHY 需要这一对：技能包由 ``SkillsMiddleware`` 经 backend 读取，而 backend 只认虚拟
    路径。技能库已经搬到工作区之外的存储目录（``SessionRoot.skills_store``），内置技能
    随应用交付、同样在工作区之外——两者都必须显式挂一个虚拟路径才读得到
    （见 ``SessionRoot.read_only_mounts``），否则**一个都读不到且没有任何告警**。
    """

    host_dir: Path
    virtual: str


MountKind = Literal["file", "dir"]
"""挂载对象的类型：单个文件（父目录不可见），或整棵目录树（写一律拒绝）。"""


@dataclass(frozen=True)
class VirtualMount:
    """一个**只读**挂进虚拟文件系统的宿主路径。

    Attributes:
        prefix: 虚拟路径前缀（如 ``/global/``、``/skills/``）；``CompositeBackend``
            按它路由，并在转发前**剥掉**它。
        host_path: 宿主上的真实路径（文件或目录）。
        label: 错误文案与日志里对它的说法（如「全局长期记忆」「技能库」）。
        kind: ``file`` 只暴露那一个文件（父目录里的其它文件不可见）；``dir`` 暴露整棵
            子树，但写操作一律拒绝。
    """

    prefix: str
    host_path: Path
    label: str
    kind: MountKind = "file"

    def __post_init__(self) -> None:
        """校验取值，避免拼出一个路由不到、或文案说不清的挂载点。

        WHY 校验前缀形态：``CompositeBackend`` 按前缀匹配并**剥掉**它再转发，前缀写错
        （少一个斜杠）会落进默认 backend——于是这个文件连同路径语义都跑到工作区里去了，
        而装配与运行都不会报错。

        Raises:
            ValueError: 前缀不是 ``/`` 开头 ``/`` 结尾、``host_path`` 不是 ``Path``、
                ``kind`` 不是 ``file`` / ``dir``、文件挂载没有文件名、``label`` 为空。
        """
        if not self.prefix.startswith("/") or not self.prefix.endswith("/"):
            raise ValueError(f"挂载前缀必须以 / 开头并以 / 结尾：{self.prefix!r}")
        if not isinstance(self.host_path, Path):
            raise ValueError(f"host_path 必须是 Path，实际：{type(self.host_path).__name__}")
        if self.kind not in ("file", "dir"):
            raise ValueError(f"kind 只能是 file 或 dir，实际：{self.kind!r}")
        if self.kind == "file" and not self.host_path.name:
            raise ValueError(f"host_path 必须指向一个文件（有文件名）：{self.host_path}")
        if not self.label or not str(self.label).strip():
            raise ValueError("label 不能为空：错误文案要靠它说明这是哪一种挂载")

    @property
    def virtual_path(self) -> str:
        """该路径在虚拟文件系统里的位置：文件含文件名，目录就是前缀本身。"""
        if self.kind == "file":
            return f"{self.prefix}{self.host_path.name}"
        return self.prefix.rstrip("/") or "/"


@dataclass(frozen=True)
class MemoryPlan:
    """本会话的长期记忆方案：根内的来源 + 需要挂载的根外文件。

    WHY 把两者放进同一个对象、且 ``sources`` 由挂载**派生**：``sources`` 里的
    ``/global/AGENTS.md`` 只有在 ``mounts`` 里挂了对应文件时才读得到，而 deepagents 对
    读不到的来源是**静默跳过**的（只留一行 WARNING）。分成两个方法返回（一个给来源、
    一个给挂载），调用方漏接一半就会得到「记忆少了一条」，且没有任何错误指向它。

    Attributes:
        in_root_sources: 落在会话根内、无需挂载就能读到的来源（如工作区自带的
            ``/AGENTS.md``）。
        mounts: 需要只读挂进虚拟文件系统的根外文件。
    """

    in_root_sources: list[str]
    mounts: list[VirtualMount]

    @property
    def sources(self) -> list[str]:
        """deepagents ``memory`` 参数所需的来源列表（挂载出来的在前）。"""
        return [mount.virtual_path for mount in self.mounts] + list(self.in_root_sources)

    @property
    def mounted_files(self) -> list[Path]:
        """当前挂载的宿主文件（供日志与排错使用）。"""
        return [mount.host_path for mount in self.mounts]


@dataclass(frozen=True)
class SessionRoot:
    """一条会话的**文件根**：配置 + 具体根目录，附上全部由它派生的路径。

    根只有两种来源，本模型下没有第三种：

    1. **用户绑定的工作空间**——用户在创建会话时选定的目录，可以同时容纳多条会话；
    2. **会话专属目录**——没绑定工作空间的会话，在 ``SESSIONS_ROOT`` 下按会话 ID
       自动创建的子目录，只有它自己用。

    两者在本对象看来完全一样（一个绝对路径），因为文件工具、沙箱挂载根、附件目录与
    知识库索引需要的都只是「一个根」。区分它们的地方只有两处：解析时（谁决定这个根）
    与界面文案（「工作空间」还是「会话专属目录」）。

    WHY 单独成为一个对象，而不是给 ``AppConfig`` 的那些方法各加一个 ``root`` 参数：

    1. **漏传必须炸，而不是静默退回某个默认值**。「用错了根」是这套设计里最危险的
       失败形态（写进别人的项目、把附件的根指错、技能视图建到另一个目录）。参数若带
       默认值，任何一处漏传都会悄悄用另一个目录，而症状（文件跑到别处）与原因相距
       极远；把「哪个根」收进一个必须显式获得的对象里，漏了就是 ``AttributeError``。
    2. **派生逻辑只有一份**。长期记忆路径、技能来源与虚拟路径映射全都依赖根目录，
       把它们挂在这里是唯一能保证「同一个根处处得到同一组派生结果」的形态。
    3. **可独立测试**：它只依赖 ``AppConfig`` 的字段，不需要启动任何服务。

    Attributes:
        config: 应用配置（提供上限、显式覆盖项等与根无关的取值）。
        root: 根目录的绝对路径。
        preset: 本条会话所属的**场景预设 ID**；空串表示不限定（接受全部技能）。
    """

    config: AppConfig
    root: Path
    preset: str = ""
    """本条会话所属的**场景预设 ID**；空串表示「不限定」。

    WHY 放在根对象上，而不是每次建视图时临时传：技能视图按根物化
    （``.harness/skills-active``），而视图内容取决于场景白名单——把场景挂在根上，
    「同一个根对应同一份视图」这条不变量才成立。反过来说，**一个工作空间只属于一个场景**：
    两条不同场景的会话共用一个根时，后者的视图会覆盖前者，而运行中的会话只按自己首轮
    加载的那份技能索引做事，两边都不会报错。

    WHY 允许空串：既有会话与「不选场景」的用法都必须继续可用——空串意味着不做白名单
    过滤（接受全部技能），这正是本次改造前的行为。
    """

    def __post_init__(self) -> None:
        """把根归一到绝对路径，并归一场景 ID。

        WHY 这里**不**校验目录是否存在：会话专属目录在第一次用到它之前并不存在，而
        「存在性」这件事对用户绑定的工作空间必须严格（拼错的路径不能静默变成一个空
        目录）。两者放在同一个入口判定会二选一牺牲一边，因此校验落在**用户输入进
        来的那一刻**——由应用层的解析函数负责（见 ``SessionRegistry.resolve``），
        而本对象只需保证「拿到的是一个绝对路径」，目录按需创建（``ensure_directories``）。

        WHY 不校验「这个场景是否存在」：场景清单由 ``runtime.skill_presets`` 从文件系统
        巡检得出，而根对象不该依赖那层扫描（它还会被知识库、测试等路径构造）。查不到的
        场景由服务层按「不限定」处理并打日志——历史会话里记着已删除的场景 ID 时，让它
        打不开历史才是更糟的选择。

        Raises:
            ValueError: ``config`` 为 ``None``，或 ``preset`` 不是字符串。
        """
        if self.config is None:
            raise ValueError("config 不能为 None")
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())
        if not isinstance(self.preset, str):
            raise ValueError(f"preset 必须是字符串，实际：{type(self.preset).__name__}")
        if self.preset != self.preset.strip():
            object.__setattr__(self, "preset", self.preset.strip())

    # ------------------------------------------------------------------ 基本属性

    @property
    def global_memory_file(self) -> Path | None:
        """**全局**长期记忆文件的绝对路径；未配置时为 ``None``。

        WHY 它不受会话根约束：这份文件按定义就是跨会话的（人工维护的偏好与约定），
        要求它落在工作区内等于要求「每条会话各放一份」。旧实现把它当成「工作区之外 →
        跳过」，于是配了全局记忆的实例里**所有**会话都静默少一条记忆——只有一行
        WARNING，用户看不出自己写的东西没生效。

        Returns:
            ``MEMORY_FILE`` 的绝对路径（字段校验已展开 ``~`` 并转绝对路径）；
            未配置时 ``None``。
        """
        return self.config.memory_file

    @property
    def workspace_memory_file(self) -> Path:
        """本工作区自带的 ``AGENTS.md``（未配置全局记忆时才作为来源）。

        WHY 仍然保留这份回落：工作区里的 ``AGENTS.md`` 是**项目自己的**说明（不与其它
        工作区共享），而「没配全局记忆」的实例正是最需要它的场合。
        """
        return self.root / _MEMORY_FILE_NAME

    @cached_property
    def memory_plan(self) -> MemoryPlan:
        """本会话的长期记忆方案：来源与为它们准备的只读挂载点。

        WHY 缓存（``cached_property``）：它要读磁盘、还会记日志，而同一次装配里 backend
        的路由与 ``memory`` 参数都要取它——算两遍会把同一件事记两遍，也会让「文件恰好
        在这一刻被删」出现两种结果。``SessionRoot`` 是**按请求**构造的，所以缓存期限就是
        一次请求，不会让人工刚改完的内容滞留。

        WHY 配置了全局记忆就不再加载工作区自带的那份：一份记忆只应有一个来源，否则两处
        说法冲突时没有任何优先级依据（deepagents 只是把它们一起塞进提示），用户很难理解
        「我改的是全局那份，Agent 却不照做」。它若存在会记一行 INFO，免得用户以为生效了。

        Returns:
            来源列表与挂载点；两者缺一时读不到对应来源（deepagents 会静默跳过），
            因此它们由同一个对象给出，见 :class:`MemoryPlan`。
        """
        global_file = self.global_memory_file
        if global_file is None:
            # 没配全局记忆：退回到「本工作区自带的 AGENTS.md」。
            if not self.workspace_memory_file.is_file():
                logger.warning("长期记忆文件不存在，跳过加载：%s", self.workspace_memory_file)
                return MemoryPlan(in_root_sources=[], mounts=[])
            return MemoryPlan(
                in_root_sources=[f"/{self.workspace_memory_file.name}"], mounts=[]
            )

        if self.workspace_memory_file.is_file():
            logger.info(
                "已配置全局长期记忆（%s）：工作区自带的 %s 不再作为来源",
                global_file,
                self.workspace_memory_file,
            )
        if not global_file.is_file():
            logger.warning("全局长期记忆文件不存在，跳过加载：%s", global_file)
            return MemoryPlan(in_root_sources=[], mounts=[])

        mount = VirtualMount(
            prefix=GLOBAL_MEMORY_PREFIX, host_path=global_file, label="全局长期记忆"
        )
        logger.info("全局长期记忆将以只读方式挂载：%s ← %s", mount.virtual_path, global_file)
        return MemoryPlan(in_root_sources=[], mounts=[mount])

    # ------------------------------------------------------------------ 根外存储

    @cached_property
    def storage_dir(self) -> Path:
        """本根的**存储目录**：技能库、技能视图、工具留存与知识库索引都住在这里。

        WHY 放回工作区内（2026-09-22 改）：这些内容与**这个工作空间**强绑定——技能是
        「这个项目常用的套路」，留存是「这个项目的运行记录」——跟着项目走，换机器或换
        工作空间之后行为才一致；放在数据目录下则表现为「换个工作空间就什么都要重配」。

        WHY 集中成一个 ``.harness/``：文件面板只需隐藏一个名字；用户要在版本控制里忽略
        它，也只需写一行 ``.gitignore``（应用不替用户改仓库里的文件）。

        代价是明确的：工作区里会多出这个目录（2026-09-21 曾为同样理由把它搬到数据目录，
        本次按「内容应随项目走」的取舍搬回）。这是**有意接受**的取舍，不是疏漏。

        WHY 位置由**根路径**派生、而不是按会话分：同一个工作空间可能承载多条会话，而
        「这个工作空间装了什么技能」本来就该共享（技能启停状态也是全应用一份，见
        ``runtime.skill_store``）。按会话分会让同一工作空间的第二条会话看不到第一条装好
        的技能——那正是「技能库」这个概念要消除的重复。

        Note:
            ``SessionRoot`` 是**按请求**构造的，所以这个缓存只活一次请求——换工作区
            会换对象，不会拿到别人的存储目录。
        """
        return self.root / HARNESS_DIR_NAME

    @property
    def legacy_root_store_dir(self) -> Path:
        """**旧版**的根外存储目录（``<数据目录>/roots/<可读名>-<短哈希>``）。

        WHY 还需要它：2026-09-22 之前技能库 / 技能视图 / 工具留存住在这里，升级时要把
        它们搬回工作区内的 ``.harness/``。位置公式必须与旧版逐字一致——差一个字符就会
        认为「没有旧数据」，而表现是用户的技能库凭空消失。

        WHY 不当成 ``storage_dir`` 的兼容分支：``storage_dir`` 是**现行**布局的唯一出处
        （面板、挂载、迁移都以它为准），在里面塞一条「旧位置」分支会让两个位置各自被一半
        代码引用，而那种分叉不会报错。这里只作为**只读的迁移来源**存在。
        """
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:12]
        return self.config.roots_store_root / f"{_readable_dir_name(self.root)}-{digest}"

    @property
    def skills_store(self) -> Path:
        """技能库目录（用户放技能包的地方）。"""
        return self.storage_dir / _SKILLS_STORE_DIR_NAME

    @property
    def skill_view_store(self) -> Path:
        """技能视图目录（只把启用中的技能物化出来的派生物）。"""
        return self.storage_dir / _SKILL_VIEW_STORE_DIR_NAME

    @property
    def tool_output_store(self) -> Path:
        """工具输出留存目录（``<store>/<会话 ID>/NNNN-<工具>.txt``）。"""
        return self.storage_dir / _TOOL_OUTPUTS_STORE_DIR_NAME

    @property
    def knowledge_db(self) -> Path:
        """本根的知识库索引文件（``.harness/knowledge.db``）。

        WHY 跟着工作区走（2026-09-22 改）：索引的对象就是**这个工作区里的文档**（库里以
        根内虚拟路径为键去重），因此它必须与项目同生共死——放在数据目录下会出现「项目删了、
        索引还在」，而那份索引永远指不回去。

        WHY 这是**唯一**的索引路径来源：``knowledge_runtime`` 与面板都从它取，避免两处各
        拼一遍路径（那样的分叉不会报错，只会表现为「面板说已索引、检索却查不到」）。
        """
        return self.storage_dir / _KNOWLEDGE_DB_NAME

    @cached_property
    def read_only_mounts(self) -> list[VirtualMount]:
        """本根要挂进虚拟文件系统的**全部**只读路径。

        WHY 收成一个列表、且与「技能来源」同源：图的技能来源与文件工具看到的路径都靠
        这些挂载才解析得到。分开给（一个给来源、一个给挂载）时，漏接一半的表现是
        「面板里列得出来、Agent 却读不到」，而 deepagents 对读不到的技能来源只是记一条
        警告——没有任何错误会指向装配漏了一条路由。

        挂载清单：
        - ``/.harness/``：应用数据目录（技能库 / 技能视图 / 工具留存）。2026-09-22 起
          这些内容**物理上就在工作区内**，所以这条路由的作用只剩「只读」——没有它，Agent
          可以经工作区路径直接改写自己的技能库（default backend 是可写的）；
        - ``/skills/``：技能库（人工维护；与 ``/.harness/skills`` 是同一个目录）；
        - ``/.skills-active/``：技能视图（派生物，建图时的来源就是它）；
        - ``/_tool_outputs/``：工具输出留存（消息里只带引用，正文在这里）；
        - 内置技能与 ``SKILL_DIRS`` 指定的目录：它们位于应用目录或任意位置，同样必须
          挂上——``sources_for_graph`` 的兜底（视图缺失 → 退回配置目录）正是靠它们才
          真的读得到，否则那句「等于全部启用」是假的。
        """
        planned: list[VirtualMount] = [
            # 摆第一位只是习惯（前缀互不重叠，顺序不影响匹配）：它覆盖整个应用数据目录，
            # 是「Agent 改不动自己的技能库/留存」这条约束的兜底。
            VirtualMount(
                prefix=f"{VIRTUAL_HARNESS}/",
                host_path=self.storage_dir,
                label="应用数据目录",
                kind="dir",
            ),
            VirtualMount(
                prefix=f"{VIRTUAL_SKILL_VIEW}/",
                host_path=self.skill_view_store,
                label="技能视图",
                kind="dir",
            ),
            VirtualMount(
                prefix=f"{VIRTUAL_TOOL_OUTPUTS}/",
                host_path=self.tool_output_store,
                label="工具输出留存",
                kind="dir",
            ),
        ]
        seen = {mount.prefix for mount in planned}
        # 技能库只在「没显式配置 SKILL_DIRS」时挂我们自己那个存储目录：显式配置时技能库
        # **就是那些目录**（与 ``skill_dir_plan`` 同一个分支）。两个都挂同一前缀会让后来者
        # 被静默跳过——症状是「面板里技能一个都列不出来」，而目录里明明有技能包。
        if not self.config.skill_dirs:
            planned.append(
                VirtualMount(
                    prefix=f"{VIRTUAL_SKILLS}/",
                    host_path=self.skills_store,
                    label="技能库",
                    kind="dir",
                )
            )
            seen.add(f"{VIRTUAL_SKILLS}/")
        for source in self.skill_dir_plan():
            prefix = f"{source.virtual}/"
            if prefix in seen:
                continue
            planned.append(
                VirtualMount(
                    prefix=prefix,
                    host_path=source.host_dir,
                    label=f"技能来源 {source.virtual}",
                    kind="dir",
                )
            )
            seen.add(prefix)
        return planned

    @property
    def mount_table(self) -> dict[str, Path]:
        """``{虚拟前缀: 宿主目录}``：只含**目录**挂载，供巡检与路径解析用。"""
        return {
            mount.prefix: mount.host_path
            for mount in self.read_only_mounts
            if mount.kind == "dir"
        }

    @property
    def skill_view_virtual(self) -> str:
        """技能视图的虚拟路径（建图时的技能来源就是它）。

        WHY 由这里给出而不是让调用方各自 import 常量：虚拟路径是**挂载约定**的一半
        （另一半是 ``read_only_mounts`` 里的前缀），两者必须同源——分开写会在改动其一
        之后留下「来源指向没人挂的路径」这种静默失效。
        """
        return VIRTUAL_SKILL_VIEW

    @property
    def tool_outputs_virtual(self) -> str:
        """工具输出留存的虚拟路径前缀（消息与事件里的引用以它开头）。"""
        return VIRTUAL_TOOL_OUTPUTS

    def ensure_storage(self) -> None:
        """建好工作区内的 ``.harness/``，并把两代旧位置的技能库 / 留存搬进来。

        WHY 要迁移而不是「重新开始」：技能包是**用户放进来的东西**，静默丢掉等于让用户
        的技能凭空消失——而现场表现只是「Agent 忽然不会那些套路了」，没有任何报错指向
        这里。迁移是幂等的：新位置已有内容就不再动旧位置。

        要处理的两代旧布局：
        1. **根外存储**（2026-09-21～09-22）：``<数据目录>/roots/<根标识>/{skills,tool-outputs}``；
        2. **工作区内的 ``skills/``**（更早）：那时候技能库直接躺在项目根下。

        WHY 只迁技能库与工具留存、不迁技能视图：视图是派生物，``rebuild_view`` 会按**当前
        启停状态**重建；搬一份旧视图进来反而可能让「面板说停用、Agent 照用」同时成立。

        Raises:
            OSError: 目录创建失败（磁盘满、权限不足）。
        """
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # WHY 不预建技能视图目录：``sources_for_graph`` 用「视图目录是否存在」判断
        # 「物化视图是否已建过」——预建一个空目录会让那个判据永远为真，于是「视图被清理 /
        # 重建失败」与「所有技能都停用」变得无法区分（前者会让技能静默消失且没有告警）。
        # 视图由 ``runtime.skill_view.rebuild_view`` 自己建（先建临时目录再整体替换）。
        self.tool_output_store.mkdir(parents=True, exist_ok=True)
        # WHY 先搬根外存储、再处理更老的 ``<根>/skills``：前者是**最近一次布局**的数据，
        # 后者更早；先把近的搬到位，老的那份才有机会命中「新位置已非空 → 两边都保留」的
        # 保护，而不是反过来被后一步覆盖。
        self._migrate_root_store_into_workspace()
        if self.config.skill_dirs:
            # 显式配置了技能目录：技能库**就是那些目录**，我们的存储里那份不被使用。
            # 此时既不建它、更不能把 ``<根>/skills`` 当「旧位置」搬走——那可能是用户自己
            # 配进去的路径（搬走会让配置里的路径凭空消失，技能一个都列不出来）。
            return
        self.skills_store.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_skills()

    def _migrate_root_store_into_workspace(self) -> None:
        """把旧版**根外存储**里的技能库与工具留存搬回工作区内的 ``.harness/``。

        WHY 逐个目录判断、而不是整体 rename 旧的根标识目录：旧目录里可能只有
        ``tool-outputs``（用户从没放过技能包），整体搬会把一个「空的技能库」也算进来，
        干扰后续对更老布局（``<根>/skills``）的判定。

        WHY 出错继续搬下一个而不是中断：技能库与工具留存是两类独立数据，一类搬不动不该
        让另一类也留在旧位置——两边都保留、并各打一条 ERROR 比「整体放弃」更容易收尾。
        """
        legacy = self.legacy_root_store_dir
        if not legacy.is_dir():
            return
        if legacy.resolve() == self.storage_dir.resolve():
            # 幂等保护：两个位置被配成同一个目录时，不能自己搬自己。
            logger.debug("旧存储与新存储是同一个目录，跳过迁移：%s", legacy)
            return

        for name, target in (
            (_SKILLS_STORE_DIR_NAME, self.skills_store),
            (_TOOL_OUTPUTS_STORE_DIR_NAME, self.tool_output_store),
        ):
            source = legacy / name
            if not source.is_dir():
                continue
            try:
                target.mkdir(parents=True, exist_ok=True)
                existing = list(target.iterdir())
            except OSError as exc:
                logger.error("读取存储目录失败，旧位置保持原样：%s（%s）", target, exc)
                continue
            if existing:
                logger.warning(
                    "旧存储 %s 里还有内容，而新位置 %s 已非空：两边都保留，请自行合并",
                    source,
                    target,
                )
                continue
            try:
                # WHY 逐子项 move 而不是 move 整个目录：``target`` 已经建出来了（上面
                # mkdir），对已存在的目录做整体 move 会把它搬成子目录，结果是多一层嵌套。
                for child in list(source.iterdir()):
                    shutil.move(str(child), str(target / child.name))
                source.rmdir()
            except OSError as exc:
                logger.error(
                    "迁移旧存储失败，旧位置仍保留（请手工搬到 %s）：%s（%s）",
                    target,
                    source,
                    exc,
                )
                continue
            logger.info("旧存储已搬回工作区：%s → %s", source, target)

        try:
            leftover = list(legacy.iterdir())
        except OSError as exc:
            logger.debug("读取旧存储根目录失败（无害）：%s（%s）", legacy, exc)
            return
        if leftover:
            # 还有没搬的东西（例如 ``skills-active``——派生物，按当前启停状态重建）：
            # 目录原样保留，下一次升级仍有机会继续迁移。
            logger.info("旧存储根目录里仍有未迁移内容，保持原样：%s", legacy)
            return
        try:
            legacy.rmdir()
        except OSError as exc:
            logger.debug("旧存储根目录已空但删除失败（无害）：%s（%s）", legacy, exc)

    def _migrate_legacy_skills(self) -> None:
        """把工作区根下的 ``skills/``（更早的布局）里的技能包搬进 ``.harness/skills``。

        WHY 必须先确认它**确实是个技能库**：``skills`` 这个目录名太常见（前端、数据项目
        都可能有），而历史上出现过「打开项目后应用把用户的目录搬走」——用户找不到自己的
        东西，且全程没有任何提示。因此只有「里面存在含 ``SKILL.md`` 的子目录」时才迁移，
        否则原样不动（它只是一个同名目录）。

        WHY 只搬技能包子目录、不搬整个目录：目录里可能混着用户自己的其它文件；搬走它们
        等于替用户做了一次他不知情的整理。搬完后若目录已空才顺手删掉。
        """
        legacy = self.root / _USER_SKILLS_DIR_NAME
        if not legacy.is_dir():
            return
        try:
            children = list(legacy.iterdir())
        except OSError as exc:
            logger.warning("读取旧技能库失败，保持原样：%s（%s）", legacy, exc)
            return

        if not children:
            # WHY 空目录不动：旧版本确实会建出空目录，但用户也可能自己建了一个空的
            # ``skills/``——「删掉用户自己建的目录」比「留下一个空目录」危险得多。
            logger.debug("工作区里存在空的 skills/，不视为旧技能库，保持原样：%s", legacy)
            return

        packages = [child for child in children if (child / "SKILL.md").is_file()]
        if not packages:
            logger.info(
                "工作区里的 %s 不含技能包（没有含 SKILL.md 的子目录），不视为旧技能库，保持原样",
                legacy,
            )
            return

        if self.skills_store.is_dir() and any(self.skills_store.iterdir()):
            logger.warning(
                "旧技能库 %s 里还有内容，而新位置 %s 已非空：两边都保留，请自行合并",
                legacy,
                self.skills_store,
            )
            return

        try:
            for package in packages:
                shutil.move(str(package), str(self.skills_store / package.name))
        except OSError as exc:
            # WHY 出错就停手、且不动旧位置：搬一半会让技能分散在两处，而调用方无法判断
            # 哪边是全的——宁可让用户看到一个完整的旧目录。
            logger.error(
                "迁移旧技能库失败，旧位置仍保留（请手工搬到 %s）：%s（%s）",
                self.skills_store,
                legacy,
                exc,
            )
            return

        try:
            remaining = list(legacy.iterdir())
        except OSError as exc:
            logger.debug("读取旧技能库残留内容失败（无害）：%s（%s）", legacy, exc)
            remaining = []
        if remaining:
            logger.info("旧技能库里还有非技能包内容，目录原样保留：%s", legacy)
        else:
            try:
                legacy.rmdir()
            except OSError as exc:
                logger.debug("旧技能库目录已空但删除失败（无害）：%s（%s）", legacy, exc)
        logger.info("旧技能包已搬进工作区存储：%s → %s", legacy, self.skills_store)

    @property
    def skill_dirs(self) -> list[Path]:
        """本工作区生效的技能目录（宿主机路径）；技能库在工作区的 ``.harness/`` 下。"""
        return [source.host_dir for source in self.skill_dir_plan()]

    # ------------------------------------------------------------------ 目录准备

    def ensure_directories(self) -> None:
        """创建本工作区运行所需的目录。

        WHY 在工作区**被选中的那一刻**建，而不是启动时替所有候选目录建：后者会往
        用户还没用过的项目里写目录，而那是用户的仓库，不是我们的。

        WHY 只建**根本身**、不在这里建 ``.harness/``：应用数据的落点由
        ``ensure_storage`` 负责，它与「技能视图是否需要重建」这类按需逻辑在同一处；
        而长期记忆由人工维护（应用不替它建目录——那等于往别处悄悄写目录）。
        """
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 技能来源

    def skill_dir_plan(self) -> list[SkillSource]:
        """技能目录 → 虚拟路径的完整规划（三类来源，含工作区内与共享来源）。

        WHY 虚拟路径不能由「是否在工作区内」推导：用户技能库在**工作区内**
        （``.harness/skills``），而通用技能与场景预设技能都在**应用目录**里（工作区之外）
        ——三者只有一处共同口径，即**挂载表**（挂在哪，就按哪读）。按位置推导（旧规则：
        工作区外取目录名）在多种来源共存时会给出与真实挂载点不一致的路径，而症状是
        「面板里列得出来、Agent 却读不到」。

        默认顺序（越靠后优先级越高，同名技能由后者覆盖前者）：

        1. ``/skills-builtin``：通用技能（随应用交付，每个场景都能用）；
        2. ``/skills-presets``：场景预设技能（随应用交付，按场景白名单过滤）；
        3. ``/skills``：用户技能库（工作区内，用户可改）。

        WHY 这样排：用户 > 场景 > 通用。用户在自己的工作空间里放的同名技能理应覆盖产品
        自带的那一份，否则「我改了却不生效」不会有任何报错。

        Returns:
            来源列表，顺序即优先级（越靠后优先级越高）。
        """
        if self.config.skill_dirs:
            # 显式配置优先：用户说了算，虚拟路径取目录名（与 ``read_only_mounts`` 的挂载
            # 前缀一致，见那里的 WHY）。此时**不**叠加三类默认来源——否则「显式指定」只对
            # 了一半：目录列出来了，产品的技能却仍混在里面。
            return [
                SkillSource(host_dir=directory, virtual=f"/{directory.name}")
                for directory in self.config.skill_dirs
            ]
        plan: list[SkillSource] = [
            SkillSource(host_dir=BUILTIN_SKILLS_DIR, virtual=VIRTUAL_BUILTIN_SKILLS)
        ]
        # WHY 只把**当前场景**的目录作为来源，而不是整个 ``presets`` 目录：上游按「来源的
        # 一级子目录」发现技能，因此来源必须正好是"装着技能包的那一层"。指向 ``presets``
        # 会让所有场景的技能一起被发现——表现为「选了 A 场景，B 场景的技能也在」。
        #
        # WHY 取 ``config.skill_presets_dir`` 而不是直接引用常量：预设目录可被配置覆盖
        # （部署换一套场景集、测试指向隔离目录）。来源用常量、过滤用配置值，会让「配置里指了
        # 别的目录，技能却还是从默认目录来的」这种分叉无从解释。
        if self.preset:
            plan.append(
                SkillSource(
                    host_dir=self.config.skill_presets_dir / self.preset,
                    virtual=f"{VIRTUAL_PRESET_SKILLS}/{self.preset}",
                )
            )
        plan.append(SkillSource(host_dir=self.skills_store, virtual=VIRTUAL_SKILLS))
        return plan

    def skill_sources(self) -> list[SkillSource]:
        """把**存在**的技能目录映射成「宿主机目录 + 虚拟路径」的来源列表。

        WHY 必须显式映射：技能由 ``SkillsMiddleware`` 经 backend 读取，而 backend 只认
        虚拟路径——目录挂在哪，就从哪读（见 ``read_only_mounts``）。

        WHY 先 ``ensure_storage()``：技能库是我们的目录，读来源之前确保它就位（幂等），
        否则「刷新页面直接打开一条历史会话」这条路径上（它只装配图、不装配面板服务）
        会看到「技能库不存在」的 WARNING，而那条警告是假的。

        Returns:
            来源列表，顺序即优先级（越靠后优先级越高）。

        Raises:
            ValueError: 两个技能目录映射到同一个虚拟路径——静默的后果是其中一个
                来源整体失效，而界面上看不出任何异常。
        """
        self.ensure_storage()
        sources: list[SkillSource] = []
        claimed: dict[str, Path] = {}
        for source in self.skill_dir_plan():
            if not source.host_dir.is_dir():
                # WHY 过滤而非报错：技能库是可选能力，缺失只应降级而不是让应用无法启动。
                logger.warning(
                    "技能目录不存在，已跳过：%s（虚拟路径 %s）", source.host_dir, source.virtual
                )
                continue
            previous = claimed.get(source.virtual)
            if previous is not None:
                msg = (
                    f"技能目录 {previous} 与 {source.host_dir} 映射到同一个虚拟路径 "
                    f"{source.virtual}；请给其中一个改名，或用 SKILL_DIRS 显式指定不同的目录"
                )
                logger.error("%s", msg)
                raise ValueError(msg)
            claimed[source.virtual] = source.host_dir
            sources.append(source)
        return sources

    def skill_source_paths(self) -> list[str]:
        """返回 deepagents ``skills`` 参数所需的虚拟路径列表（``/skills`` 之类）。

        WHY 必须是虚拟路径而不是宿主机绝对路径：backend 只认虚拟路径，绝对路径（Windows
        下还带盘符与反斜杠）在虚拟模式下会被当成工作区内的子路径，导致技能永远命中不了。
        这些虚拟路径由 ``read_only_mounts`` 挂上，两者必须同源。
        """
        return [source.virtual for source in self.skill_sources()]

    def skill_host_dir(
        self, virtual_directory: str, *, sources: list[SkillSource] | None = None
    ) -> Path | None:
        """把技能（或技能目录）的虚拟路径还原成宿主机路径；无法归属时返回 ``None``。

        WHY 需要反向映射：技能物化视图要从**真实目录**复制内容（见
        ``runtime.skill_view.rebuild_view``），而巡检结果给的是虚拟路径。内置技能在
        工作区之外，直接与工作区拼接会得到一个不存在的路径——表现为「技能在清单里、
        却怎么也复制不进视图」，而视图为空又会让该技能静默失效。

        Args:
            virtual_directory: 技能目录的虚拟路径，如 ``/skills/code-review``。
            sources: 已经算好的来源列表；``None`` 表示就地求一次。批量解析（视图重建）
                必须传入，否则每个技能都要重新扫一遍技能目录。

        Returns:
            宿主机绝对路径；该路径不属于任何已知来源时为 ``None``。
        """
        normalized = "/" + str(virtual_directory).strip().strip("/")
        best_length = -1
        best: Path | None = None
        for source in self.skill_sources() if sources is None else sources:
            if normalized == source.virtual:
                candidate = source.host_dir
            elif normalized.startswith(source.virtual + "/"):
                candidate = source.host_dir / normalized[len(source.virtual) + 1 :]
            else:
                continue
            # WHY 取最长匹配：来源可以嵌套（``/skills`` 与 ``/skills/team``），取短的
            # 那个会把团队技能映射到基础目录下，而它的源目录根本不是那里。
            if len(source.virtual) > best_length:
                best_length = len(source.virtual)
                best = candidate
        return best

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        preset = f", preset={self.preset}" if self.preset else ""
        return f"SessionRoot(root={self.root}{preset})"
