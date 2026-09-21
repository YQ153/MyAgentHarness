"""工作区文件服务：列目录与读文件。

职责边界：把基础设施层的文件访问包装成「有权限语义、有审计」的应用能力。
**路径校验不在这里实现**——它只有一份实现，位于 ``runtime.workspace_files``；
在这里再写一遍就会形成第二份口径，而两份口径迟早分叉（分叉的表现是「某一层
放行了逃逸路径」）。

WHY 目录列举不落审计、内容读取落审计：列举是高频动作且不暴露任何内容，把它写进
审计只会把审计表刷成噪声，让真正需要留痕的读取记录淹没在里面；而「谁读了哪个
文件」正是审计要回答的问题，与 ``thread_rename`` / ``memory_delete`` 同级口径。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

from application.audit_context import LOCAL_ACTOR_ID, audit_client_info, audit_trace_id
from application.dto import (
    WorkspaceEntryInfo,
    WorkspaceFileContent,
    WorkspaceListing,
)
from application.errors import NotFoundError
from runtime.workspace_files import (
    IMAGE_SUFFIXES,
    list_directory,
    looks_binary,
    read_bytes_capped,
    resolve_in_workspace,
)

if TYPE_CHECKING:
    from application.ports import AuditLog
    from config import AppConfig, SessionRoot

logger = logging.getLogger(__name__)

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_BINARY = "binary"
KIND_TOO_LARGE = "too_large"
"""``WorkspaceFileContent.kind`` 的四个取值，也就是与前端的渲染约定。

WHY 集中成模块级常量：前端按取值分派四条分支（按文本展示 / 按图片内联 / 提示无法
预览 / 提示文件过大），而它们同时出现在 ``__all__`` 与接口响应里；散落成字面量时，
改一处漏一处只会让某个取值悄悄失去渲染分支，而那种失效不报任何错。
"""

_BYTES_PER_CHAR = 4
"""由字符窗口换算读取字节数的系数（UTF-8 单字符最多 4 字节）。"""


class WorkspaceService:
    """工作区文件的只读访问。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        scope: SessionRoot,
        audit_store: AuditLog | None = None,
    ) -> None:
        """构造服务。

        Args:
            config: 应用配置，提供各项上限。
            scope: 本实例服务的工作区；**必填**。面板展示的必须正是 Agent 读写的那片
                目录，指向别处会让「Agent 写了但面板看不见」与「面板看到的其实不是
                这个项目」同时成立。
            audit_store: 审计存储；``None`` 表示不落审计（测试与无库场景）。

        Raises:
            ValueError: ``config`` 或 ``scope`` 为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if scope is None:
            raise ValueError("scope 不能为 None：面板的根由它决定")

        self._config = config
        self._root = scope.root
        # WHY 带上挂载表：技能库、技能视图与工具输出留存住在工作区之外，由只读挂载暴露在
        # 固定虚拟路径下。面板要能打开「完整输出」（``/_tool_outputs/…``）就得按这张表解析
        # ——否则那条路径会被当成工作区内的相对路径，读到一个不存在的文件（404）。
        self._mounts = scope.mount_table
        self._audit_store = audit_store
        logger.info("WorkspaceService 就绪：root=%s mounts=%d", self._root, len(self._mounts))

    async def list_dir(self, virtual_path: str = "/") -> WorkspaceListing:
        """列出工作区内一级目录。

        Args:
            virtual_path: 虚拟路径，``/`` 表示工作区根。

        Returns:
            目录项清单（目录在前）。

        Raises:
            WorkspacePathError: 路径非法或逃出工作区。
            NotFoundError: 目录不存在或不是目录。
            ValueError: 目录不存在或不是目录（服务层视角的统一入口）。
        """
        try:
            listing = await asyncio.to_thread(
                list_directory,
                self._root,
                virtual_path,
                max_entries=self._config.workspace_list_max_entries,
                mounts=self._mounts,
            )
        except FileNotFoundError as exc:
            raise NotFoundError("目录", virtual_path) from exc
        except NotADirectoryError as exc:
            raise NotFoundError("目录", virtual_path) from exc

        return WorkspaceListing(
            path=listing.path,
            parent=listing.parent,
            entries=[
                WorkspaceEntryInfo(
                    name=item.name,
                    path=item.path,
                    is_dir=item.is_dir,
                    size=item.size,
                    modified_at=item.modified_at,
                    is_symlink=item.is_symlink,
                )
                for item in listing.entries
            ],
            truncated=listing.truncated,
        )

    async def read_file(
        self, virtual_path: str, *, offset: int = 0
    ) -> WorkspaceFileContent:
        """读取一个文件，返回一段文本、图片 data URL 或降级标记。

        读取成功即落审计（含文件名与大小，**不含正文**）。

        WHY 做成按偏移分段而不是「一次给完」：工具输出经留存后可能有几十万字符，
        一次塞进响应会把浏览器拖住；而如果干脆提高预览上限，只是把同一个问题推给
        下一次更大的输出。分段之后每次响应仍有界，而「完整查看」由前端续取拼出来。

        Args:
            virtual_path: 文件虚拟路径。
            offset: 起始字符偏移；由前一次响应的 ``offset + len(text)`` 得到。

        Returns:
            文件内容片段或降级标记。

        Raises:
            WorkspacePathError: 路径非法或逃出工作区。
            NotFoundError: 文件不存在或指向目录。
            ValueError: ``offset`` 为负数。
        """
        if offset < 0:
            raise ValueError(f"offset 不能为负数，实际：{offset}")

        # 路径校验必须在读取之前单独走一次：``read_bytes_capped`` 只认真实路径，
        # 把「解析交给它」会让逃逸路径在打开文件那一刻才被发现。
        target = await asyncio.to_thread(
            resolve_in_workspace, self._root, virtual_path, mounts=self._mounts
        )

        exists = await asyncio.to_thread(target.exists)
        if not exists:
            raise NotFoundError("文件", virtual_path)
        is_dir = await asyncio.to_thread(target.is_dir)
        if is_dir:
            raise NotFoundError("文件", virtual_path)

        size = (await asyncio.to_thread(target.stat)).st_size
        normalized = "/" + Path(virtual_path.strip().replace("\\", "/")).as_posix().lstrip("/")
        limit = self._config.workspace_file_preview_chars
        mime_type = self._mime_of(target)

        if size > self._config.workspace_file_max_bytes:
            # 只回大小不回正文：调用方（界面）要展示「它有多大」，
            # 而把整份内容读进内存再丢弃是纯粹的浪费。
            await self._audit("file_read", actor_id=LOCAL_ACTOR_ID, path=normalized)
            return WorkspaceFileContent(
                path=normalized,
                name=target.name,
                size=size,
                kind=KIND_TOO_LARGE,
                mime_type=mime_type,
            )

        # WHY 字节上限随窗口推进：分页读取不能每次都按整文件上限读，否则翻到第 N 页
        # 仍要付第 1 页的读盘代价。(offset + limit) 字符在最坏情况下占 4 字节/字符。
        window_bytes = min((offset + limit) * _BYTES_PER_CHAR, self._config.workspace_file_max_bytes)
        data, total, _ = await asyncio.to_thread(
            read_bytes_capped, target, max_bytes=window_bytes
        )
        await self._audit("file_read", actor_id=LOCAL_ACTOR_ID, path=normalized)

        if offset == 0:
            # 类型判定只在第一页做：续取时类型早已确定，重复判定没有信息增量
            if mime_type.startswith("image/"):
                # WHY 图片判定必须排在二进制判定之前：PNG/JPEG 的字节流里必然含空字节，
                # 先判「二进制」会让图片永远走不到这一支，界面上就只剩一句「无法预览」。
                # 按 data URL 内联返回：文件面板不需要第二个「原始字节」端点，
                # 而字节上限（workspace_file_max_bytes）已经把响应体大小框住了。
                return WorkspaceFileContent(
                    path=normalized,
                    name=target.name,
                    size=total,
                    kind=KIND_IMAGE,
                    text=f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}",
                    mime_type=mime_type,
                )
            if looks_binary(data):
                # 二进制不尝试解码：解出来是乱码，占满上下文还看不出它是二进制
                return WorkspaceFileContent(
                    path=normalized,
                    name=target.name,
                    size=total,
                    kind=KIND_BINARY,
                    mime_type=mime_type,
                )

        text = data.decode("utf-8", errors="replace")
        chunk = text[offset : offset + limit]
        # 两种「还有更多」：本页窗口没读完文件，或读完了但字符位置还没到末尾
        has_more = total > len(data) or len(text) > offset + limit

        return WorkspaceFileContent(
            path=normalized,
            name=target.name,
            size=total,
            kind=KIND_TEXT,
            text=chunk,
            truncated=has_more,
            offset=offset,
            mime_type=mime_type,
        )

    # ------------------------------------------------------------------ 内部

    def _mime_of(self, target: Path) -> str:
        """按扩展名推断 MIME；命中不到的按文本处理。"""
        guessed, _ = mimetypes.guess_type(target.name)
        if guessed:
            return guessed
        if target.suffix.lower() in IMAGE_SUFFIXES:
            return f"image/{target.suffix.lower().lstrip('.')}"
        return "text/plain"

    async def _audit(self, event_type: str, *, actor_id: str, path: str) -> None:
        """写一条文件读取审计。

        WHY 审计失败不上抛：它是旁路职责，一次成功的读取不应因为审计库抖动
        变成 500——与 ``ThreadService._audit`` 同一取舍。
        """
        if self._audit_store is None:
            return
        ip, ua = audit_client_info()
        try:
            await self._audit_store.log(
                event_type=event_type,
                actor_id=actor_id,
                target_id=path,
                action="read",
                outcome="success",
                ip=ip,
                user_agent=ua,
                # WHY 文件读取也要链路标识：「这次请求读了哪些文件」往往正是排查的
                # 起点，缺了它就只能按时间戳猜，而同秒内的多条记录猜不出来。
                trace_id=audit_trace_id(),
            )
        except Exception:
            logger.exception("写入文件读取审计失败：path=%s", path)

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        return f"WorkspaceService(root={self._root})"


__all__ = [
    "KIND_BINARY",
    "KIND_IMAGE",
    "KIND_TEXT",
    "KIND_TOO_LARGE",
    "WorkspaceService",
]
