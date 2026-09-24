"""工作区文件域配置：Web 文件面板限额、附件上传与会话产物留存。

字段从原 ``config.AppConfig`` 的「工作区文件（Web 文件面板）」「附件（上传
与多模态）」「会话」三个分区（原 L593–676）整体迁入。它们共同的特点是
「约束推送给浏览器 / 模型 / 磁盘的数据量」，因此归为一个域。
"""

import re
from typing import Annotated

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import NoDecode

from config.constants import DEFAULT_ATTACHMENT_MIME_TYPES
from config.parsing import parse_list_config


class WorkspaceSettings(BaseModel):
    """工作区文件域的字段：面板限额、附件与会话产物留存。"""

    # ---------------- 工作区文件（Web 文件面板） ----------------
    workspace_list_max_entries: int = Field(default=500, ge=1, le=5000)
    """单次列目录返回的最大条目数。

    WHY 需要上限：工作区里常有 ``node_modules`` 这类上万条的目录，无上限地返回
    会让一次展开变成一次大数据传输，也会让前端渲染卡死。
    """

    workspace_file_preview_chars: int = Field(default=20_000, ge=100, le=500_000)
    """文件面板里文本预览的字符上限（超出截断并在响应里标注）。"""

    workspace_file_max_bytes: int = Field(default=5_000_000, ge=1024)
    """超过此字节数的文件不做文本预览，只回 ``too_large`` 降级标记。

    WHY 用降级标记而不是报错：文件确实存在、也确实读得到，只是不适合整份塞进
    浏览器。当成错误会让界面只能显示一句失败，而用户真正想知道的是「它有多大」。
    """

    # ---------------- 附件（上传与多模态） ----------------
    attachment_max_bytes: int = Field(default=2_000_000, ge=1024, le=50_000_000)
    """单个附件的字节上限。

    WHY 默认只有 2 MB（远小于 ``workspace_file_max_bytes``）：附件的内容会以
    data URL 形式进入**用户消息**，而消息要被写进检查点并在后续每一轮里重新发给
    模型。上限放宽一倍，检查点与每次请求的体积就跟着翻一倍——这个开销是持续的，
    不是一次性的。
    """

    attachment_max_per_thread: int = Field(default=8, ge=1, le=100)
    """单个会话允许保留的附件数上限。

    WHY 需要它：附件目录在工作区里只增不减（会话存续期间），而没有上限时一次
    误操作就能把工作区塞满；上限也顺带把「一次请求塞多少图片给模型」框住了。
    """

    attachment_allowed_mime_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ATTACHMENT_MIME_TYPES)
    )
    """允许上传的 MIME 白名单。

    WHY 白名单而不是黑名单：MIME 是调用方自己声明的，黑名单永远补不全；
    而这里真正的约束是「模型能不能收下这种内容」，只有少数几种图片类型成立。
    """

    # ---------------- 会话 ----------------
    thread_title_max_chars: int = Field(default=24, ge=1, le=200)
    """**自动生成**标题的字符上限（按首条用户输入生成，超出以省略号截断）。

    WHY 做成配置：不同前端宽度能承载的标题长度不同，硬编码会让窄侧栏溢出、
    宽侧栏浪费空间；而这里只约束「截断长度」，不参与任何存储结构。
    """

    thread_rename_max_chars: int = Field(default=120, ge=1, le=200)
    """**手动改名**允许的字符上限。

    WHY 与 ``thread_title_max_chars`` 分开：后者是自动标题的生成口径（很短，
    只为列表可读），而用户手写的标题常常带上下文（「排查登录超时 - 2026Q3」），
    用 24 字符去卡手工输入等于逼用户起一个没信息量的名字。
    WHY 上限不超过 200：存储层的标题硬上限是 200，服务层必须比它更严，
    否则用户输入的标题会在入库时被静默截断——那比直接报错更让人困惑。
    """

    tool_result_preview_chars: int = Field(default=2000, ge=100, le=50_000)
    """推送给前端的工具结果预览长度上限。

    WHY 做成配置：命令输出或大文件读取可达数十万字符，直接推送会占满带宽并
    让界面卡死；而不同部署的前端能承载的预览长度不同，硬编码无法按环境调整。
    """

    tool_output_max_chars: int = Field(default=200_000, ge=1000)
    """单个留存文件的字符上限（被截断的工具输出会完整落盘到工作区）。

    WHY 仍要上限：留存是为「能回取」，不是做无限仓库；一次读到几十 MB 文件的调用
    若原样落盘，磁盘会随对话量无界增长。截断副本配上首行说明已足以回答
    「这次调用产出了什么」。
    """

    tool_output_retention_per_thread: int = Field(default=20, ge=1, le=1000)
    """每个会话保留的最新工具输出份数，超出后按时间清理旧文件。

    WHY 必须清理：留存目录只增不减会变成磁盘黑洞，而真正有用的只有最近若干次
    ——旧输出对应的是已经翻过去的对话。
    """

    @field_validator("attachment_allowed_mime_types", mode="before")
    @classmethod
    def _parse_mime_types(cls, value: object) -> object:
        """按逗号切分 MIME 白名单，口径见 ``parse_list_config``。

        WHY 独立成validator（原 ``_parse_csv_lists`` 按域拆分而来）：分隔符与
        vision 别名相同但语义不同，合并成跨域 validator 会让两个域互相牵连；
        拆开后各自的报错也各说各的字段。
        """
        return parse_list_config(
            value, field="attachment_allowed_mime_types", separators=(",",)
        )

    @field_validator("attachment_allowed_mime_types", mode="after")
    @classmethod
    def _normalize_mime_types(cls, value: list[str]) -> list[str]:
        """归一 MIME 写法（去空白、转小写、去重保序）。

        WHY 必须归一：白名单要与上传请求声明的 MIME 做**精确比较**，而
        ``Image/PNG`` 与 ``image/png`` 在字符串层面不同、在语义上相同——不归一
        就会表现为「明明配了却拒绝上传」。
        """
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            candidate = str(item).split(";", 1)[0].strip().lower()
            if not candidate:
                continue
            if not re.match(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$", candidate):
                raise ValueError(f"attachment_allowed_mime_types 含非法 MIME：{item!r}")
            if candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
        if not normalized:
            raise ValueError("attachment_allowed_mime_types 不能为空，否则任何附件都无法上传")
        return normalized
