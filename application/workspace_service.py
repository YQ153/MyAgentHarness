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

from application.audit_context import audit_client_info
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
    from application.principal import Principal
    from config import AppConfig
    from runtime.audit_store import AuditStore

logger = logging.getLogger(__name__)

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_BINARY = "binary"
KIND_TOO_LARGE = "too_large"

_ANONYMOUS_ACTOR = "anonymous"


class WorkspaceService:
    """工作区文件的只读访问。"""

    def __init__(self, config: AppConfig, *, audit_store: AuditStore | None = None) -> None:
        """构造服务。

        Args:
            config: 应用配置，提供工作区根目录与各项上限。
            audit_store: 审计存储；``None`` 表示不落审计（测试与无库场景）。

        Raises:
            ValueError: ``config`` 为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")

        self._config = config
        self._root = Path(config.workspace)
        self._audit_store = audit_store
        logger.info("WorkspaceService 就绪：root=%s", self._root)

    async def list_dir(
        self, virtual_path: str = "/", principal: Principal | None = None
    ) -> WorkspaceListing:
        """列出工作区内一级目录。

        Args:
            virtual_path: 虚拟路径，``/`` 表示工作区根。
            principal: 当前主体；仅用于审计归属（本方法不落审计）。

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
        self, virtual_path: str, principal: Principal | None = None
    ) -> WorkspaceFileContent:
        """读取一个文件并按类型给出预览。

        读取成功即落审计（含文件名与大小，**不含正文**）。

        Args:
            virtual_path: 文件虚拟路径。
            principal: 当前主体，用于审计归因。

        Returns:
            文件内容或降级标记。

        Raises:
            WorkspacePathError: 路径非法或逃出工作区。
            NotFoundError: 文件不存在或指向目录。
        """
        # 路径校验必须在读取之前单独走一次：``read_bytes_capped`` 只认真实路径，
        # 把「解析交给它」会让逃逸路径在打开文件那一刻才被发现。
        target = await asyncio.to_thread(resolve_in_workspace, self._root, virtual_path)

        exists = await asyncio.to_thread(target.exists)
        if not exists:
            raise NotFoundError("文件", virtual_path)
        is_dir = await asyncio.to_thread(target.is_dir)
        if is_dir:
            raise NotFoundError("文件", virtual_path)

        size = (await asyncio.to_thread(target.stat)).st_size
        normalized = "/" + Path(virtual_path.strip().replace("\\", "/")).as_posix().lstrip("/")
        if size > self._config.workspace_file_max_bytes:
            # 只回大小不回正文：调用方（界面）要展示「它有多大」，
            # 而把整份内容读进内存再丢弃是纯粹的浪费。
            await self._audit("file_read", actor_id=self._actor(principal), path=normalized)
            return WorkspaceFileContent(
                path=normalized,
                name=target.name,
                size=size,
                kind=KIND_TOO_LARGE,
                mime_type=self._mime_of(target),
            )

        data, total, hit_cap = await asyncio.to_thread(
            read_bytes_capped, target, max_bytes=self._config.workspace_file_max_bytes
        )
        await self._audit("file_read", actor_id=self._actor(principal), path=normalized)

        mime_type = self._mime_of(target)
        if hit_cap:
            return WorkspaceFileContent(
                path=normalized,
                name=target.name,
                size=total,
                kind=KIND_TOO_LARGE,
                mime_type=mime_type,
            )

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
        limit = self._config.workspace_file_preview_chars
        if len(text) > limit:
            return WorkspaceFileContent(
                path=normalized,
                name=target.name,
                size=total,
                kind=KIND_TEXT,
                text=text[:limit],
                truncated=True,
                mime_type=mime_type,
            )

        return WorkspaceFileContent(
            path=normalized,
            name=target.name,
            size=total,
            kind=KIND_TEXT,
            text=text,
            mime_type=mime_type,
        )

    # ------------------------------------------------------------------ 内部

    def _actor(self, principal: Principal | None) -> str:
        """审计主体标识；认证关闭时与其它服务保持同一口径。"""
        return principal.user_id if principal else _ANONYMOUS_ACTOR

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
