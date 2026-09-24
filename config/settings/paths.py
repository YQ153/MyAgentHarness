"""路径域配置：全局记忆文件、数据库、会话根、场景预设与技能目录。

字段从原 ``config.AppConfig`` 的「运行时路径」分区（原 L528–591）整体迁入。
原文件唯一涉及本域四个路径字段的跨字段 validator ``_expand_path`` 恰好全部
落在本域内，因此随迁为域内 validator。
"""

import os
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import NoDecode

from config.parsing import parse_list_config


class PathsSettings(BaseModel):
    """运行时路径域的字段。

    WHY 没有「默认工作空间」这一项：会话的工作空间由用户在**创建会话时**指定，也可以
    不指定。配置里再放一个默认值，就等于把「不指定」偷偷变成「指定了配置里那个」——
    而这两种选择在本模型下必须产生不同的结果（后者落到应用管理的会话专属目录里）。
    """

    memory_file: Path | None = None
    """**全局**长期记忆文件（人工维护，Agent 只读）。

    ``None`` 表示不配全局记忆，改用 ``<会话根>/AGENTS.md``（若存在）——那一份是**工作区
    自带**的说明文件，只对落在该工作区的会话生效。

    WHY 显式配置的这一份按「全局」对待：它的用途就是跨会话共享的偏好与约定（称呼、
    语言、项目惯例）。要求它落在会话根之内，等于要求用户为每条会话各维护一份，而那恰好
    是它要消除的重复。它由应用以**只读挂载**的方式进入虚拟文件系统
    （``/global/<文件名>``，见 ``SessionRoot.memory_plan``），因此不需要、也不应该位于
    任何工作区内部。
    """

    db_path: Path = Field(default=Path("./.data/agent.db"))

    sessions_root: Path | None = None
    """**未绑定工作空间的会话**其专属目录的父目录。

    ``None`` 表示按 ``<数据目录>/sessions`` 派生（见 ``resolved_sessions_root``）——
    数据目录就是 ``DB_PATH`` 所在目录。WHY 跟着数据目录走：会话的检查点、审计与用量
    都在那个库里，把「会话专属目录」放在别处会让「备份/搬迁只需要搬一个目录」不再成立。

    WHY 需要它：要求「不绑定工作空间的会话，在应用指定的文件夹下自动创建独立子文件夹」。
    没有这一项，那些会话就没有文件根——而 Agent 的文件工具、沙箱挂载根、附件与知识库
    索引都需要一个根。

    每个会话的子目录名就是它的会话 ID（见 ``AppConfig.session_dir``）。
    """

    presets_dir: Path | None = None
    """**场景预设目录**；``None`` 表示用随应用交付的 ``<应用目录>/skills/presets``。

    WHY 允许覆盖：预设是产品资产，但部署里可能需要换成团队自己的场景集（与 ``SKILL_DIRS``
    同一动机）；而测试也需要一个隔离目录才能验证「按场景过滤」这条路径——不可注入的常量会
    让那段逻辑只能靠集成环境去碰，而它决定"某个场景到底有哪些技能"。
    """

    skill_dirs: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    """技能目录（按顺序查找）。**空列表表示按工作区派生**，见 ``SessionRoot``。

    **顺序有语义：越靠后优先级越高**（上游 ``SkillsMiddleware`` 的规则是同名技能由后面的
    来源覆盖前面的）。因此三类默认来源排成「通用 → 预设 → 用户」：用户在工作空间里放的
    同名技能覆盖场景预设与通用技能；顺序反过来会让产品自带的技能永远赢，而「我改了却不
    生效」不会有任何报错。

    为空时每个根派生三项：``BUILTIN_SKILLS_DIR``（通用技能）、``PRESET_SKILLS_DIR``
    （场景预设技能）与该根的用户技能库 ``SessionRoot.skills_store``（在**工作区内**的
    ``.harness/skills/``）。前两项在应用目录里，因此必须由 ``read_only_mounts`` 挂上虚拟
    路径——backend 只认虚拟路径，不挂载就会「一个都读不到且没有任何告警」。

    WHY 一旦显式配置就**对所有工作区生效**（不再按工作区分叉）：显式给的是绝对路径，
    它表达的是「技能包放在这些固定位置」，与当前是哪个工作区无关。

    环境变量支持两种写法：路径分隔符（Windows ``;`` / POSIX ``:``，与 ``PATH``
    同口径）或 JSON 数组。**不用逗号**——路径本身可能含逗号，按逗号切会把
    一个目录拆成两个不存在的目录，而失败表现为「技能没加载」这种难排查的
    现象。
    """

    @field_validator("memory_file", "db_path", "sessions_root", "presets_dir", mode="after")
    @classmethod
    def _expand_path(cls, value: Path | None) -> Path | None:
        """展开用户目录并转绝对路径；``None`` 表示「未配置，稍后派生」。

        WHY 统一在这里转绝对路径：backend 的 ``root_dir`` 与会话目录若用相对路径，
        进程工作目录一旦变化就会指向不同的物理目录，属于难以复现的隐患。

        WHY 落在本域 mixin：四个字段全部属于路径域，validator 与字段同处一个类，
        「哪些字段会被展开」一眼可查。
        """
        if value is None:
            return None
        return value.expanduser().resolve()

    @field_validator("skill_dirs", mode="before")
    @classmethod
    def _parse_skill_dirs(cls, value: object) -> object:
        """按路径分隔符切分技能目录，口径见 ``parse_list_config``。

        WHY 用 ``os.pathsep`` 而不是逗号：路径本身可能含逗号，按逗号切会把
        一个目录拆成两个不存在的目录，而症状是「技能没加载」——排障时不会
        有人想到去查分隔符。
        """
        return parse_list_config(value, field="skill_dirs", separators=(os.pathsep,))

    @field_validator("skill_dirs", mode="after")
    @classmethod
    def _expand_dirs(cls, value: list[Path]) -> list[Path]:
        """逐个展开用户目录并转绝对路径，与 ``_expand_path`` 同一口径。"""
        return [item.expanduser().resolve() for item in value]
