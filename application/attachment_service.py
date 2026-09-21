"""附件服务：上传、列举、删除，并把附件构造成多模态消息内容。

职责边界（三条不变量）：

1. **路径校验不在本层**——它只有一份实现（``runtime.workspace_files``，经
   ``runtime.attachments`` 调用）。在服务层再写一遍，就等于给「附件会不会成为
   第二个目录穿越入口」这个问题留下第二种答案。
2. **模型能力判定必须在构造消息之前**——决定「能不能把图发给这个模型」的依据是
   注册表里的 ``supports_vision``，而不是「上传是否成功」。放在构造前，用户得到的
   是一条可操作的错误（换哪个模型），而不是一次发出去才失败的运行。
3. **审计不落内容**——上传留文件名 / 大小 / MIME / 摘要，字节本身只在工作区里，
   不进审计表。审计表会被归档与检索，把图片字节写进去只会让它无法阅读。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from application.audit_context import LOCAL_ACTOR_ID, audit_client_info, audit_trace_id
from application.dto import AttachmentInfo, AttachmentLimits, AttachmentListResult
from application.errors import NotFoundError, VisionUnsupportedError
from runtime.attachments import (
    AttachmentError,
    AttachmentRecord,
    count_attachments,
    delete_attachment,
    list_attachments,
    load_attachment,
    save_attachment,
)
from thread_utils import normalize_thread_id

if TYPE_CHECKING:
    from application.ports import AuditLog, ThreadMetadataReader
    from config import AppConfig, SessionRoot
    from llm.registry import ModelRegistry

logger = logging.getLogger(__name__)

IMAGE_BLOCK_TYPE = "image_url"
"""多模态内容里图片块的类型名。

WHY 用 ``image_url`` 而不是 provider 私有结构：这是 LangChain / OpenAI 兼容接口的
通用形态，且允许把 ``data:`` URL 直接放进去（Anthropic 与 OpenAI 的适配层都会把它
转成各自的图片字段）；用私有结构会把「换 provider 就不工作」写进消息里。
"""


def attachment_limits(config: AppConfig) -> AttachmentLimits:
    """算出当前生效的附件上限。

    WHY 做成模块级函数而不是只留服务方法：上限只依赖配置，与「哪个文件根」无关。独立
    出来之后，那个纯配置查询的端点（``GET /api/attachments/limits``）可以完全绕过会话根
    解析——否则一个只需要读配置的请求会因为「这条会话还没有根」而变成 409。

    Args:
        config: 应用配置。

    Returns:
        上限 DTO。
    """
    if config is None:
        raise ValueError("config 不能为 None")
    return AttachmentLimits(
        max_bytes=config.attachment_max_bytes,
        max_per_thread=config.attachment_max_per_thread,
        allowed_mime_types=list(config.attachment_allowed_mime_types),
    )


class AttachmentService:
    """会话附件的上传与引用。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        scope: SessionRoot,
        registry: ModelRegistry,
        thread_store: ThreadMetadataReader,
        audit_store: AuditLog | None = None,
    ) -> None:
        """构造服务。

        Args:
            config: 应用配置，提供附件各项上限。
            scope: 本实例服务的工作区；**必填**。附件落在 ``<工作区>/.attachments/``
                下，指向别的工作区会让「上一条消息引用的图」在新会话里找不到。
            registry: 模型注册表，用于判定目标模型是否接受图片。
            thread_store: 会话元数据存储，用于确认会话存在。
            audit_store: 审计存储；``None`` 表示不落审计（测试与无库场景）。

        Raises:
            ValueError: 任一必需依赖为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")
        if scope is None:
            raise ValueError("scope 不能为 None：附件目录建在哪个工作区由它决定")
        if registry is None:
            raise ValueError("registry 不能为 None：模型能力判定依赖它")
        if thread_store is None:
            raise ValueError("thread_store 不能为 None：会话存在性判定依赖它")

        self._config = config
        self._registry = registry
        self._thread_store = thread_store
        self._audit_store = audit_store
        self._root = scope.root
        # WHY 需要一把锁：附件数上限的「检查 + 写入」之间隔着磁盘 I/O（``to_thread``
        # 会让出事件循环），并发上传会各自读到同一个旧计数而一起写入，把上限冲破。
        self._upload_lock = asyncio.Lock()
        logger.info("AttachmentService 就绪：workspace=%s", self._root)

    # ------------------------------------------------------------------ 读

    def limits(self) -> AttachmentLimits:
        """当前生效的附件上限。"""
        return attachment_limits(self._config)

    async def list(self, thread_id: str) -> AttachmentListResult:
        """列出某会话的附件。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话不存在。
        """
        normalized = await self._require_thread(thread_id)
        records = await asyncio.to_thread(list_attachments, self._root, normalized)
        return AttachmentListResult(
            thread_id=normalized,
            items=[attachment_info(record) for record in records],
            limits=self.limits(),
        )

    # ------------------------------------------------------------------ 写

    async def upload(
        self,
        thread_id: str,
        *,
        filename: str,
        mime_type: str,
        data: bytes,
    ) -> AttachmentInfo:
        """保存一个附件。

        Args:
            thread_id: 会话 ID。
            filename: 上传时的原始文件名（仅用于展示）。
            mime_type: 调用方声明的 MIME；不在白名单内直接拒绝。
            data: 字节内容。

        Returns:
            写入后的附件信息。

        Raises:
            ValueError: 内容为空 / 超限、MIME 不受支持、附件数已达上限、会话 ID 非法。
            NotFoundError: 会话不存在。
            RuntimeError: 落盘失败。
        """
        # WHY 允许尚未登记的会话：Web 前端会先 ``POST /api/threads`` 取一个 ID，
        # 元数据要到首轮运行才落库。若这里强制要求已登记，用户就无法在首轮带附件。
        normalized = await self._require_thread(thread_id, allow_claim=True)

        blob = self._validate_payload(mime_type, data)

        async with self._upload_lock:
            existing = await asyncio.to_thread(count_attachments, self._root, normalized)
            limit = self._config.attachment_max_per_thread
            if existing >= limit:
                raise ValueError(
                    f"该会话附件数已达上限（{limit} 个）；请先删除不再需要的附件再上传"
                )
            try:
                record = await asyncio.to_thread(
                    save_attachment,
                    self._root,
                    normalized,
                    filename=filename,
                    mime_type=_normalize_mime(mime_type),
                    data=blob,
                )
            except AttachmentError as exc:
                # 落盘层的形态校验失败（MIME 映射不到扩展名等）：属于输入问题
                raise ValueError(str(exc)) from exc
            except OSError as exc:
                logger.exception("附件落盘失败：thread=%s filename=%s", normalized, filename)
                raise RuntimeError(f"附件保存失败：{exc}") from exc

        await self._audit_upload(record)
        return attachment_info(record)

    async def delete(self, thread_id: str, attachment_id: str) -> bool:
        """删除一个附件。

        Returns:
            ``True`` 表示确实删掉了；``False`` 表示该附件不存在（幂等语义）。

        Raises:
            ValueError: ``thread_id`` / ``attachment_id`` 非法。
            NotFoundError: 会话不存在。
            RuntimeError: 删除失败。
        """
        normalized = await self._require_thread(thread_id)
        try:
            removed = await asyncio.to_thread(
                delete_attachment, self._root, normalized, attachment_id
            )
        except AttachmentError as exc:
            raise ValueError(str(exc)) from exc
        except OSError as exc:
            logger.exception("删除附件失败：thread=%s id=%s", normalized, attachment_id)
            raise RuntimeError(f"附件删除失败：{exc}") from exc
        return removed

    # ------------------------------------------------------------------ 消息内容构造

    async def build_user_content(
        self,
        thread_id: str,
        text: str,
        attachment_ids: list[str],
        *,
        model_name: str | None = None,
    ) -> str | list[dict[str, Any]]:
        """把文本与附件拼成一条用户消息的内容。

        WHY 返回 ``str`` 或 ``list`` 两种形态：没有附件时保持纯字符串——那是既有
        行为，改成统一的列表形态会让所有历史消息的形状发生变化（导出、导入、
        正文提取都依赖它）。

        Args:
            thread_id: 会话 ID。
            text: 用户输入的文本（已由调用方去空白）。
            attachment_ids: 要携带的附件 ID 列表；空列表表示纯文本。
            model_name: 目标模型别名；``None`` 表示默认模型。

        Returns:
            纯文本（无附件）或多模态内容块列表。

        Raises:
            ValueError: 附件 ID 列表非法、或数量超出上限。
            NotFoundError: 会话或某个附件不存在。
            VisionUnsupportedError: 目标模型不接受图片输入。
            KeyError: 模型别名未注册。
        """
        normalized = await self._require_thread(thread_id, allow_claim=True)
        ids = _normalize_attachment_ids(attachment_ids)
        if not ids:
            return text

        self._ensure_model_accepts_images(model_name)

        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for attachment_id in ids:
            try:
                record, data = await asyncio.to_thread(
                    load_attachment, self._root, normalized, attachment_id
                )
            except FileNotFoundError as exc:
                raise NotFoundError("附件", attachment_id) from exc
            except AttachmentError as exc:
                raise ValueError(str(exc)) from exc
            blocks.append(
                {
                    "type": IMAGE_BLOCK_TYPE,
                    "image_url": {"url": _data_url(record, data)},
                }
            )
        logger.info(
            "构造多模态消息：thread=%s images=%d text_chars=%d", normalized, len(ids), len(text)
        )
        return blocks

    # ------------------------------------------------------------------ 内部

    def _validate_payload(self, mime_type: str, data: bytes) -> bytes:
        """校验内容与类型，返回规范化后的字节。

        Raises:
            ValueError: 内容为空 / 超限，或 MIME 不在白名单内。
        """
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("附件内容不能为空")
        blob = bytes(data)

        limit = self._config.attachment_max_bytes
        if len(blob) > limit:
            raise ValueError(
                f"附件过大（{len(blob)} 字节 > 上限 {limit} 字节），请压缩后重试"
            )

        normalized_mime = _normalize_mime(mime_type)
        allowed = list(self._config.attachment_allowed_mime_types)
        if normalized_mime not in allowed:
            supported = "、".join(allowed)
            raise ValueError(
                f"不支持的文件类型 {mime_type!r}；当前允许：{supported}"
            )
        return blob

    def _ensure_model_accepts_images(self, model_name: str | None) -> None:
        """确认目标模型接受图片输入。

        Raises:
            KeyError: 别名未注册（由注册表抛出）。
            VisionUnsupportedError: 该模型不接受图片。
        """
        if self._registry.supports_vision(model_name):
            return
        capable = [
            item["name"]
            for item in self._registry.describe()
            if item.get("supports_vision")
        ]
        key = model_name or self._registry.default_name
        raise VisionUnsupportedError(key, capable)

    async def _require_thread(self, thread_id: str, *, allow_claim: bool = False) -> str:
        """确认会话存在，返回规范化后的会话 ID。

        Args:
            thread_id: 会话 ID。
            allow_claim: 允许会话尚未登记（首轮消息之前上传附件的场景）。

        Raises:
            ValueError: ``thread_id`` 非法。
            NotFoundError: 会话不存在且 ``allow_claim=False``。
        """
        normalized = normalize_thread_id(thread_id)
        record = await self._thread_store.get(normalized)
        if record is None and not allow_claim:
            raise NotFoundError("会话", normalized)
        return normalized

    async def _audit_upload(self, record: AttachmentRecord) -> None:
        """写一条上传审计（含元信息，**不含内容**）。

        WHY 审计失败不上抛：与 ``WorkspaceService._audit`` 同一取舍——一次成功的
        上传不应因为审计库抖动变成 500。
        """
        if self._audit_store is None:
            return
        ip, ua = audit_client_info()
        try:
            await self._audit_store.log(
                event_type="attachment_upload",
                actor_id=LOCAL_ACTOR_ID,
                target_id=record.path,
                action="write",
                outcome="success",
                ip=ip,
                user_agent=ua,
                trace_id=audit_trace_id(),
                # 只落元信息：审计表会被归档与人工检索，把图片字节写进去只会让它无法阅读
                details={
                    "attachment_id": record.id,
                    "filename": record.filename,
                    "mime_type": record.mime_type,
                    "size": record.size,
                    "sha256": record.sha256,
                },
            )
        except Exception:
            logger.exception("写入附件上传审计失败：id=%s", record.id)

    def __repr__(self) -> str:  # pragma: no cover - 仅用于日志排错
        return f"AttachmentService(workspace={self._root})"


def _normalize_mime(mime_type: str) -> str:
    """归一 MIME 写法（去参数、去空白、转小写）。"""
    return (mime_type or "").split(";", 1)[0].strip().lower()


def _data_url(record: AttachmentRecord, data: bytes) -> str:
    """把附件字节编成 data URL。

    WHY 不用工作区文件的 URL：模型接口只接受内联内容（或对象存储的签名地址），
    内网地址它取不到；而附件本就有大小上限，内联的体积可控。
    """
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{record.mime_type};base64,{encoded}"


def _normalize_attachment_ids(attachment_ids: list[str]) -> list[str]:
    """校验并去重附件 ID 列表。

    Raises:
        ValueError: 不是列表、含非字符串或空值。
    """
    if attachment_ids is None:
        return []
    if not isinstance(attachment_ids, (list, tuple)):
        raise ValueError("attachment_ids 必须是列表")

    seen: set[str] = set()
    ordered: list[str] = []
    for item in attachment_ids:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"附件 ID 必须是非空字符串：{item!r}")
        candidate = item.strip()
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def attachment_info(record: AttachmentRecord) -> AttachmentInfo:
    """把存储层记录转成对外 DTO。

    WHY 做成公开函数而不是私有方法：历史消息回填附件时也要用同一份映射，
    两处各写一遍字段对应关系，加字段时必漏一处——而漏了的那处只会「少个字段」，
    不会报错。
    """
    return AttachmentInfo(
        id=record.id,
        thread_id=record.thread_id,
        filename=record.filename,
        mime_type=record.mime_type,
        size=record.size,
        sha256=record.sha256,
        created_at=record.created_at,
        path=record.path,
    )


__all__ = ["IMAGE_BLOCK_TYPE", "AttachmentService", "attachment_info"]
