"""附件存储：把上传的图片落在工作区的 ``.attachments/<thread_id>/`` 下。

WHY 落在工作区内而不是另开一个数据目录：附件与工作区文件必须共用**同一套路径
校验口径**（见 ``runtime.workspace_files.resolve_in_workspace``）。另开一个根目录
意味着第二份「路径是否越界」的实现，而两份口径迟早分叉——分叉的表现是某一层
放行了逃逸路径，且从代码上看不出来。

存储形态：每个附件两个文件，互为约束——
- ``<id>.<ext>``：原始字节，扩展名由 **MIME** 决定（不取用户文件名），
  这样后续用扩展名猜类型的既有代码（文件面板、``mimetypes``）不会因为用户
  上传名为 ``a.png`` 的 JPEG 而给出错误结论；
- ``<id>.json``：元数据（原始文件名、MIME、大小、sha256、创建时间）。

WHY 元数据单独成文件而不是一张清单：清单是「多个并发写者共享的可变状态」，
读改写必然互相覆盖，要保证正确就得引入锁或事务；而每个附件一份元数据是**只写
一次、内容不可变**的，天然并发安全。代价是列举时要扫目录，而单会话附件数由配置
封顶（默认 8），扫目录的代价可以忽略。

WHY 元数据**后写**：先落 blob 再落 meta，于是「meta 存在」即等价于「这个附件是
完整的」。反过来写会让一次中途失败留下一个可被列举、却读不出字节的幽灵条目。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.workspace_files import resolve_in_workspace
from thread_utils import normalize_thread_id

logger = logging.getLogger(__name__)

ATTACHMENTS_DIR = ".attachments"
"""工作区内的附件根目录名。以点开头，与 ``_tool_outputs`` 一样属于内部产物。"""

MAX_DISPLAY_FILENAME_CHARS = 120
"""原始文件名的展示上限；超出部分截断。"""

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
"""附件 ID 形态：``uuid4().hex``。

WHY 必须校验而不是直接拼路径：ID 来自请求（下载 / 引用），若不校验，``../`` 之类
的取值会先一步改变路径结构，把后面那层解析级校验绕过去。
"""

_MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
"""受支持的 MIME 到扩展名的映射。

WHY 用固定映射而不是 ``mimetypes.guess_extension``：后者对 ``image/jpeg`` 可能给出
``.jpe`` / ``.jpeg`` 等不同结果，且依赖宿主机的类型表——同一份上传在不同机器上
会落到不同扩展名，进而让「按扩展名猜类型」的既有代码结果不一致。
"""

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class AttachmentError(ValueError):
    """附件操作的可预期失败（非法输入、MIME 不受支持、元数据损坏等）。

    WHY 继承 ``ValueError``：调用方（服务层与路由）需要把它与「文件不存在」
    区分开——前者是非法输入（400），后者是资源缺失（404）。
    """


@dataclass(frozen=True)
class AttachmentRecord:
    """一个附件的元数据。"""

    id: str
    thread_id: str
    filename: str
    """上传时的原始文件名，仅用于展示。"""
    mime_type: str
    size: int
    sha256: str
    created_at: str
    """创建时刻（ISO8601 UTC）。"""
    path: str
    """虚拟路径，可直接交给工作区文件接口取回内容（用于前端预览）。"""


def _normalize_thread_id(thread_id: str) -> str:
    """校验并返回会话 ID。

    WHY 复用 ``thread_utils.normalize_thread_id`` 再补一条本层独有的约束：会话 ID 的
    通用规则（非空、长度上限）只有一份实现；而「不能含路径分隔符」是**存储布局**的
    要求——本模块把 ``<thread_id>`` 当作 ``.attachments`` 下的一个目录名，含 ``/``
    的取值会把它变成多层目录，从而与其它会话的目录相互嵌套。

    Raises:
        ValueError: 非字符串、为空或超长（由通用规则抛出）。
        AttachmentError: 含路径分隔符或为 ``.`` / ``..``。
    """
    normalized = normalize_thread_id(thread_id)
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise AttachmentError(f"thread_id 不能包含路径分隔符：{thread_id!r}")
    return normalized


def _validate_attachment_id(attachment_id: str) -> str:
    """校验附件 ID 形态。

    Raises:
        AttachmentError: 形态不符（防止把路径结构改掉）。
    """
    if not isinstance(attachment_id, str) or not _ID_RE.match(attachment_id):
        raise AttachmentError(f"附件 ID 非法：{attachment_id!r}")
    return attachment_id


def sanitize_filename(raw: str) -> str:
    """把原始文件名压成「可安全展示」的形态。

    WHY 不直接用它做磁盘名：磁盘名由 ID + MIME 扩展名决定，用户文件名只进元数据
    用于展示。这样即便文件名里带 ``..`` / 路径分隔符 / 控制字符，也不会影响落盘位置。

    Args:
        raw: 上传时携带的文件名。

    Returns:
        去掉目录部分与控制字符、限长后的展示名；无法得到有效名称时返回 ``unnamed``。
    """
    if not isinstance(raw, str):
        return "unnamed"
    # 两种分隔符都要处理：调用方可能在任意平台上构造这个值
    candidate = raw.replace("\\", "/").split("/")[-1]
    candidate = _CONTROL_CHARS_RE.sub("", candidate).strip().strip(".")
    if not candidate:
        return "unnamed"
    if len(candidate) > MAX_DISPLAY_FILENAME_CHARS:
        suffix = Path(candidate).suffix
        keep = max(1, MAX_DISPLAY_FILENAME_CHARS - len(suffix))
        candidate = f"{Path(candidate).stem[:keep]}{suffix}"
    return candidate


def extension_for_mime(mime_type: str) -> str:
    """返回受支持 MIME 对应的扩展名。

    Raises:
        AttachmentError: 该 MIME 不受支持。

    WHY 在这里再判一次而不是只信服务层：本模块是落盘的最后一道关口，
    「扩展名由 MIME 决定」这条不变量必须由它自己保证——否则一旦有调用方绕过
    服务层的白名单，落盘的文件名就会带上不受控的扩展名。
    """
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    ext = _MIME_TO_EXT.get(normalized)
    if ext is None:
        supported = ", ".join(sorted(_MIME_TO_EXT))
        raise AttachmentError(f"不支持的附件类型 {mime_type!r}；支持：{supported}")
    return ext


def attachment_dir(root: Path, thread_id: str) -> Path:
    """返回某会话的附件目录（不要求存在）。

    Raises:
        AttachmentError: ``thread_id`` 非法。
        WorkspacePathError: 解析后逃出工作区。
    """
    normalized = _normalize_thread_id(thread_id)
    # WHY 经由 resolve_in_workspace 而不是直接拼接：会话 ID 来自 URL 路径段，
    # 直接拼会让 ``..`` 这类取值先改变目录结构，绕过后续的解析级校验。
    return resolve_in_workspace(root, f"/{ATTACHMENTS_DIR}/{normalized}")


def _meta_path(directory: Path, attachment_id: str) -> Path:
    return directory / f"{attachment_id}.json"


def _blob_path(directory: Path, attachment_id: str, ext: str) -> Path:
    return directory / f"{attachment_id}.{ext}"


def _record_from_meta(payload: dict[str, Any]) -> AttachmentRecord:
    """把元数据字典还原成记录。

    Raises:
        AttachmentError: 字段缺失或类型不符（元数据被外部改坏的场景）。
    """
    try:
        attachment_id = str(payload["id"])
        thread_id = str(payload["thread_id"])
        filename = str(payload["filename"])
        mime_type = str(payload["mime_type"])
        size = int(payload["size"])
        digest = str(payload["sha256"])
        created_at = str(payload["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AttachmentError(f"附件元数据损坏：{exc}") from exc

    ext = extension_for_mime(mime_type)
    virtual = f"/{ATTACHMENTS_DIR}/{thread_id}/{attachment_id}.{ext}"
    return AttachmentRecord(
        id=attachment_id,
        thread_id=thread_id,
        filename=filename,
        mime_type=mime_type,
        size=size,
        sha256=digest,
        created_at=created_at,
        path=virtual,
    )


def list_attachments(root: Path, thread_id: str) -> tuple[AttachmentRecord, ...]:
    """列出某会话的全部附件，按创建时间升序。

    WHY 元数据坏掉的条目跳过而不是整体失败：一条坏记录不该让用户看不到同一会话
    下其余完好的附件（与文件面板处理断链软链同一取舍）。

    Returns:
        附件记录元组；目录不存在时返回空元组。
    """
    directory = attachment_dir(root, thread_id)
    if not directory.is_dir():
        return ()

    records: list[AttachmentRecord] = []
    for meta_file in directory.glob("*.json"):
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
            records.append(_record_from_meta(payload))
        except (OSError, json.JSONDecodeError, AttachmentError) as exc:
            logger.warning("跳过损坏的附件元数据：%s（%s）", meta_file, exc)

    records.sort(key=lambda item: (item.created_at, item.id))
    return tuple(records)


def count_attachments(root: Path, thread_id: str) -> int:
    """返回某会话已有的附件数。"""
    return len(list_attachments(root, thread_id))


def save_attachment(
    root: Path,
    thread_id: str,
    *,
    filename: str,
    mime_type: str,
    data: bytes,
) -> AttachmentRecord:
    """写入一个附件，返回其记录。

    Args:
        root: 工作区根目录。
        thread_id: 会话 ID。
        filename: 上传时的原始文件名（仅用于展示）。
        mime_type: 上传声明的 MIME；不在支持列表内直接拒绝。
        data: 字节内容；空内容直接拒绝。

    Returns:
        写入后的附件记录。

    Raises:
        AttachmentError: ``data`` 为空、MIME 不受支持、或 ``thread_id`` 非法。
        OSError: 落盘失败（磁盘满、权限不足）。
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AttachmentError("附件内容不能为空")
    blob = bytes(data)

    normalized = _normalize_thread_id(thread_id)
    ext = extension_for_mime(mime_type)

    directory = attachment_dir(root, normalized)
    directory.mkdir(parents=True, exist_ok=True)

    attachment_id = uuid.uuid4().hex
    blob_file = _blob_path(directory, attachment_id, ext)
    meta_file = _meta_path(directory, attachment_id)

    created_at = datetime.now(UTC).isoformat()
    payload = {
        "id": attachment_id,
        "thread_id": normalized,
        "filename": sanitize_filename(filename),
        "mime_type": mime_type.split(";", 1)[0].strip().lower(),
        "size": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "created_at": created_at,
    }

    # 元数据最后写：中途失败只会留下一个孤儿 blob，不会被列举成「可用附件」
    _write_atomic(blob_file, blob)
    _write_atomic(meta_file, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    logger.info(
        "附件已写入：thread=%s id=%s mime=%s size=%d", normalized, attachment_id, payload["mime_type"], len(blob)
    )
    return _record_from_meta(payload)


def load_attachment(root: Path, thread_id: str, attachment_id: str) -> tuple[AttachmentRecord, bytes]:
    """读取一个附件的元数据与字节。

    Raises:
        AttachmentError: ID 非法或元数据损坏。
        FileNotFoundError: 该附件不存在（含「只有 blob 没有 meta」的半成品）。
    """
    normalized = _normalize_thread_id(thread_id)
    safe_id = _validate_attachment_id(attachment_id)

    directory = attachment_dir(root, normalized)
    meta_file = _meta_path(directory, safe_id)
    if not meta_file.is_file():
        raise FileNotFoundError(f"附件不存在：{attachment_id}")

    try:
        payload = json.loads(meta_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AttachmentError(f"附件元数据损坏：{attachment_id}") from exc

    record = _record_from_meta(payload)
    blob_file = _blob_path(directory, safe_id, extension_for_mime(record.mime_type))
    try:
        data = blob_file.read_bytes()
    except FileNotFoundError as exc:
        # 元数据在、内容不在：只能报「不存在」——报「损坏」会让调用方以为重试有意义
        raise FileNotFoundError(f"附件内容缺失：{attachment_id}") from exc

    if len(data) != record.size:
        # 大小对不上说明文件被外部改动过；读取仍返回实际内容，但必须留下痕迹
        logger.warning(
            "附件大小与元数据不一致：id=%s meta=%d actual=%d", safe_id, record.size, len(data)
        )
    return record, data


def index_by_sha256(root: Path, thread_id: str) -> dict[str, AttachmentRecord]:
    """返回「内容 sha256 → 记录」的索引，供历史消息回填附件信息使用。

    WHY 用内容摘要做键：消息里存的是图片内容本身（data URL），而附件目录里存的是
    内容 + 原始文件名。两者之间没有共享的 ID——把 ID 塞进 content block 会被当作
    未知字段透传给模型。摘要由内容算出，不依赖任何一侧的额外字段，是唯一稳定的键。
    """
    return {record.sha256: record for record in list_attachments(root, thread_id)}


def delete_attachment(root: Path, thread_id: str, attachment_id: str) -> bool:
    """删除单个附件（元数据 + 内容）。

    WHY 提供删除入口：单会话附件数有上限，而「上传错了」没有撤销手段时，用户会被
    上一次误操作顶到上限、再也传不进去——那是比「文件残留」更难自解的困境。

    Returns:
        ``True`` 表示确实删掉了；``False`` 表示该附件本就不存在。

    Raises:
        AttachmentError: ``thread_id`` 或 ``attachment_id`` 形态非法。
    """
    normalized = _normalize_thread_id(thread_id)
    safe_id = _validate_attachment_id(attachment_id)

    directory = attachment_dir(root, normalized)
    meta_file = _meta_path(directory, safe_id)
    if not meta_file.is_file():
        return False

    try:
        payload = json.loads(meta_file.read_text(encoding="utf-8"))
        mime_type = str(payload.get("mime_type", ""))
    except (OSError, json.JSONDecodeError) as exc:
        # 元数据已损坏时仍然删掉它：留着只会让列举里继续出现一条坏记录
        logger.warning("附件元数据损坏，按 ID 直接清理：%s（%s）", safe_id, exc)
        mime_type = ""

    ext = _MIME_TO_EXT.get(mime_type.split(";", 1)[0].strip().lower())
    targets = [meta_file]
    if ext:
        targets.append(_blob_path(directory, safe_id, ext))
    for target in targets:
        try:
            target.unlink()
        except FileNotFoundError:
            logger.debug("附件文件已不在：%s", target)
        except OSError:
            logger.exception("删除附件文件失败：%s", target)
            raise

    logger.info("附件已删除：thread=%s id=%s", normalized, safe_id)
    return True


def delete_thread_attachments(root: Path, thread_id: str) -> int:
    """删除某会话的全部附件文件，返回删除的附件数。

    WHY 需要它：会话被删除后附件仍在磁盘上就是纯粹的泄漏——它们不再被任何消息
    引用，界面上也没有入口能看到。

    Returns:
        实际删除的附件数；目录不存在时为 0。
    """
    directory = attachment_dir(root, thread_id)
    if not directory.is_dir():
        return 0

    removed = 0
    for record in list_attachments(root, thread_id):
        ext = _MIME_TO_EXT.get(record.mime_type)
        targets = [_meta_path(directory, record.id)]
        if ext:
            targets.append(_blob_path(directory, record.id, ext))
        for target in targets:
            try:
                target.unlink()
            except FileNotFoundError:
                logger.debug("附件文件已不在：%s", target)
            except OSError:
                logger.exception("删除附件文件失败：%s", target)
        removed += 1

    # 目录可能还留着孤儿 blob（半成品写入）或空目录：一并清理，避免残留累积
    try:
        for leftover in directory.iterdir():
            leftover.unlink()
        directory.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("附件目录未能完全清理：%s", directory)

    logger.info("已删除会话附件：thread=%s count=%d", thread_id, removed)
    return removed


def _write_atomic(target: Path, payload: bytes) -> None:
    """先写临时文件再改名，避免读到写了一半的内容。

    WHY 需要它：附件写入与「读取 / 列举」是并发发生的（用户刚上传完就在另一处刷新），
    直接写目标文件会让读取方看到截断的内容——而截断的图片不会报错，只会显示不全。
    """
    temp = target.with_name(f".{target.name}.tmp")
    try:
        temp.write_bytes(payload)
        os.replace(temp, target)
    except OSError:
        # 失败时清掉临时文件，否则目录里会堆积 .tmp（它们不匹配 *.json，不会被列举）
        temp.unlink(missing_ok=True)
        raise


__all__ = [
    "ATTACHMENTS_DIR",
    "AttachmentError",
    "AttachmentRecord",
    "attachment_dir",
    "count_attachments",
    "delete_attachment",
    "delete_thread_attachments",
    "extension_for_mime",
    "index_by_sha256",
    "list_attachments",
    "load_attachment",
    "sanitize_filename",
    "save_attachment",
]
