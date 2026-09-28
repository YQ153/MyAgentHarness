"""HTTP 错误契约：稳定错误码、统一错误体与异常处理器。

WHY 单独成模块：错误体是**跨全部端点**的契约，必须只有一处能定义它。此前每个路由各自
``raise HTTPException(status_code=..., detail=...)``，于是「同一个状态码到底有哪几种含义」
只能靠通读全部路由来回答，而调用方拿到的只有一句会随文案改动而变化的中文。

错误体固定为两个键（外加校验错误额外带的结构化条目）：

- ``detail``：人类可读的失败原因。**保持既有契约**——前端 ``api()`` 与脚本一直在读它，
  所以这里只新增字段、不改造旧字段；
- ``code``：稳定的机器可读错误码（``application.errors.ErrorCode``），只增不改。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from application.errors import (
    ErrorCode,
    InterruptExpiredError,
    NotFoundError,
    REASON_CONCURRENCY,
    RunRejectedError,
    SessionPresetLockedError,
    SessionRootLockedError,
    SessionRootNotReadyError,
    SessionRootUnavailableError,
    ThreadBusyError,
    VisionUnsupportedError,
)
from application.session_registry import (
    FolderPickerBusyError,
    FolderPickerTimeoutError,
    FolderPickerUnavailableError,
)

logger = logging.getLogger(__name__)

STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.REQUEST_VALIDATION_FAILED: 422,
    ErrorCode.THREAD_ID_INVALID: 400,
    ErrorCode.UNKNOWN_MODEL: 400,
    ErrorCode.VISION_UNSUPPORTED: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.THREAD_BUSY: 409,
    ErrorCode.THREAD_ROOT_LOCKED: 409,
    ErrorCode.THREAD_PRESET_LOCKED: 409,
    ErrorCode.THREAD_ROOT_NOT_READY: 409,
    ErrorCode.THREAD_ROOT_UNAVAILABLE: 409,
    ErrorCode.INTERRUPT_EXPIRED: 409,
    ErrorCode.FOLDER_PICKER_BUSY: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.RUN_CONCURRENCY_EXCEEDED: 429,
    ErrorCode.RUN_RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.FOLDER_PICKER_UNAVAILABLE: 501,
    ErrorCode.NOT_IMPLEMENTED: 501,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.FOLDER_PICKER_TIMEOUT: 504,
}
"""每个错误码唯一对应的 HTTP 状态码。

WHY 只保留「码 → 状态」这一个方向：反向映射必然出现「一个状态对应多个码」，写进表里
等于把「409 到底是哪一种」这个问题的答案又留给了调用方。有了这张表，新增一个码时状态码
由定义处一并决定，不会再出现「同一个码在两处回不同状态」。

``ErrorCode.UNCLASSIFIED`` 刻意不在表内：它是哨兵，不允许被显式抛出。
"""

DEFAULT_CODE_BY_STATUS: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.REQUEST_VALIDATION_FAILED,
    429: ErrorCode.RUN_RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    501: ErrorCode.NOT_IMPLEMENTED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}
"""推断不出具体原因时的兜底码，按状态码取值。

WHY 需要兜底：Starlette 与 FastAPI 内部会自己抛 ``HTTPException``（未匹配的路由 404、
方法不允许 405 等），那些调用点我们改不到；没有兜底，「任何错误都有码」就不成立。
"""

_CODE_BY_EXCEPTION_TYPE: dict[type[BaseException], ErrorCode] = {
    NotFoundError: ErrorCode.NOT_FOUND,
    ThreadBusyError: ErrorCode.THREAD_BUSY,
    SessionRootLockedError: ErrorCode.THREAD_ROOT_LOCKED,
    SessionPresetLockedError: ErrorCode.THREAD_PRESET_LOCKED,
    SessionRootNotReadyError: ErrorCode.THREAD_ROOT_NOT_READY,
    SessionRootUnavailableError: ErrorCode.THREAD_ROOT_UNAVAILABLE,
    InterruptExpiredError: ErrorCode.INTERRUPT_EXPIRED,
    VisionUnsupportedError: ErrorCode.VISION_UNSUPPORTED,
    FolderPickerBusyError: ErrorCode.FOLDER_PICKER_BUSY,
    FolderPickerUnavailableError: ErrorCode.FOLDER_PICKER_UNAVAILABLE,
    FolderPickerTimeoutError: ErrorCode.FOLDER_PICKER_TIMEOUT,
}
"""应用异常类型 → 错误码：回答「哪一种失败算哪一种码」的唯一处。

WHY 放在接口层而不是 ``application.errors``：状态码是 HTTP 概念，而这份表要与状态表
一起遵守「码与状态自洽」的约束；放进应用层会让应用层凭空认识 HTTP，也会让
``application.errors`` 反向依赖 ``application.session_registry``（后者已 import 前者）。
"""


def code_for_exception(exc: BaseException | None) -> ErrorCode | None:
    """按异常类型推断错误码；推断不出时返回 ``None``。

    WHY 按类型推断，而不是要求每个路由把自己的码再写一遍：路由里 89 处
    ``raise HTTPException`` 中有 87 处写成 ``raise ... from exc``，而 ``exc``
    （``SessionRootLockedError`` 等）**本身就是失败原因**。在 87 处重复写一遍码，等于把
    同一张映射表抄 87 份——漏一处就会让那条失败悄悄退回通用码，且没有任何检测能发现。

    WHY 用 MRO 而不是 ``type(exc)`` 精确匹配：应用层异常本就有继承关系
    （``UnsupportedDocumentError`` 继承 ``ValueError``、``ThreadBusyError`` 继承
    ``RuntimeError``），将来再派生一层时，精确匹配会让它掉出这张表。

    Args:
        exc: 被 ``raise ... from exc`` 串起来的原始异常；``None`` 表示没有原因。

    Returns:
        推断出的错误码；无法推断时为 ``None``。
    """
    if exc is None:
        return None

    for klass in type(exc).__mro__:
        if klass is RunRejectedError:
            # 同一个异常要表达两个码：它们对客户端的含义相同（稍后重试），但「为什么被拒」
            # 决定了运维该扩容还是该查滥用，所以不能合并成一个码。
            reason = getattr(exc, "reason", "")
            if reason == REASON_CONCURRENCY:
                return ErrorCode.RUN_CONCURRENCY_EXCEEDED
            return ErrorCode.RUN_RATE_LIMITED

        mapped = _CODE_BY_EXCEPTION_TYPE.get(klass)
        if mapped is not None:
            return mapped

    return None


class ApiError(StarletteHTTPException):
    """带稳定错误码的 HTTP 异常。

    WHY 继承 Starlette 的 ``HTTPException`` 而不是另立一个异常基类：这样它既走 FastAPI
    既有的错误处理路径（是它的子类），又由本模块的处理器统一渲染——两条路共用一个出口，
    不会出现「有的错误走处理器、有的不走」。

    只在**推断不出码**的调用点使用它：即那处失败的原因不是某个具名应用异常
    （例如「会话 ID 形状不对」是 ``ValueError``、四个入口把 ``KeyError`` 解释成
    「模型别名未注册」）。
    """

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        """构造一个带错误码的失败响应。

        Args:
            code: 稳定错误码；状态码由它决定，**不接受调用方另行指定**——两处各给一次
                正是「码与状态说不到一起」的来源。
            detail: 人类可读的失败原因。
            headers: 额外响应头，例如 429 的 ``Retry-After``。

        Raises:
            KeyError: ``code`` 未在 ``STATUS_BY_CODE`` 中登记。
        """
        status_code = STATUS_BY_CODE.get(code)
        if status_code is None:
            # WHY 直接抛而不是兜一个状态码：漏登记的码会让这个接口在运行期给出错误的
            # 状态码，而那正是「错误契约不稳定」本身。宁可在这里当场失败。
            raise KeyError(f"错误码未登记状态码：{code!r}")
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code: ErrorCode = code


def error_body(code: ErrorCode, detail: str) -> dict[str, str]:
    """构造统一错误体。

    Args:
        code: 稳定错误码。
        detail: 人类可读的失败原因。

    Returns:
        形如 ``{"detail": ..., "code": ...}`` 的响应体。
    """
    return {"detail": detail, "code": code.value}


def _format_location(loc: Sequence[Any]) -> str:
    """把校验错误的字段路径拼成 ``body.model`` 这样的可读文本。"""
    parts = [str(item) for item in loc]
    return ".".join(parts) if parts else "请求"


def _summarize_validation_errors(
    errors: Sequence[Mapping[str, Any]], limit: int = 3
) -> str:
    """把校验错误压成一句人类可读的话。

    WHY 需要压：一个畸形载荷可能产出几十条错误，全部拼进 ``detail`` 会让前端的提示变成
    一屏噪音；完整条目仍在同一响应的 ``errors`` 里，需要的人拿得到。

    Args:
        errors: pydantic 给出的错误条目。
        limit: 进入摘要的最大条数。

    Returns:
        一句摘要；没有条目时返回通用文案。
    """
    items: list[str] = []
    for error in list(errors)[:limit]:
        location = _format_location(tuple(error.get("loc") or ()))
        message = str(error.get("msg") or "不合法")
        items.append(f"{location}: {message}")
    if not items:
        return "请求参数校验失败"
    text = "；".join(items)
    remaining = len(errors) - len(items)
    return f"请求参数校验失败：{text}（另有 {remaining} 处）" if remaining > 0 else f"请求参数校验失败：{text}"


def _resolve_code(exc: StarletteHTTPException) -> ErrorCode:
    """定出一条 HTTP 异常的错误码，必要时退回兜底值。

    优先级：显式声明的码 → 由原因异常推断出的码（**且必须与状态码自洽**）→
    状态码兜底表 → 哨兵。

    Args:
        exc: 待解析的 HTTP 异常。

    Returns:
        该响应应当携带的错误码。
    """
    explicit = getattr(exc, "code", None)
    if isinstance(explicit, ErrorCode):
        return explicit

    derived = code_for_exception(exc.__cause__)
    if derived is not None:
        if STATUS_BY_CODE.get(derived) == exc.status_code:
            return derived
        # 码与状态码不一致：说明那个站点的 except 分支可能接错了（把 409 的异常兜进了 500），
        # 或本表与新增异常不同步。此时**以状态码为准**——一对自相矛盾的 码/状态 比一个笼统的
        # 码更难用；并留下告警，让这处不一致自己暴露出来。
        logger.warning(
            "错误码与状态码不一致，已退回状态码默认值：code=%s(期望状态 %s) status=%s cause=%r",
            derived.value,
            STATUS_BY_CODE.get(derived),
            exc.status_code,
            exc.__cause__,
        )

    default = DEFAULT_CODE_BY_STATUS.get(exc.status_code)
    if default is not None:
        return default

    logger.warning(
        "状态码没有兜底错误码，已使用哨兵：status=%s detail=%s",
        exc.status_code,
        exc.detail,
    )
    return ErrorCode.UNCLASSIFIED


def install_error_handlers(app: FastAPI) -> None:
    """把统一错误体挂到应用上。

    WHY 必须由应用工厂调用：异常处理器挂在 app 上（不是 router 上），
    ``create_app`` 是唯一的生产装配点。测试里若手工建 app，需要自行调用本函数
    （``tests/interfaces/web/test_error_contract.py`` 就是范例）。

    WHY 只接三类：

    - ``StarletteHTTPException``：所有「有明确状态码的错误」都是它的子类（FastAPI 的
      ``HTTPException``、本模块的 ``ApiError``），接管它等于接管全部；
    - ``RequestValidationError``：FastAPI 单独抛它，**不是**前者的子类，必须单独接；
    - **``500``（而不是 ``Exception``）**：``Exception`` 会被 Starlette 的
      ``ExceptionMiddleware`` 截获，等于把进程内所有未捕获异常静默吞成一个响应、并且
      不再向上抛出（服务端栈日志与测试里的异常传播都会消失）；而注册 ``500`` 是由最外层
      ``ServerErrorMiddleware`` 调用的——它**发完响应后仍会重新抛出**，日志与传播都保住。
    """

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """渲染带错误码的错误体。

        WHY 保留 ``exc.headers``：429 的 ``Retry-After`` 就在这里，丢掉它等于把「多久之后
        可以再来」这个结构化信息降级成一句需要正则解析的中文。
        """
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(_resolve_code(exc), str(exc.detail)),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """渲染校验失败。

        WHY 状态码仍是 422：FastAPI 的既有行为，改变它属于破坏性变更，而本次只新增
        ``code``。校验错误因此有独立的码（``request_validation_failed``）——
        同一份状态表里，一个码仍然只对应一个状态。
        """
        errors = list(exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "detail": _summarize_validation_errors(errors),
                "code": ErrorCode.REQUEST_VALIDATION_FAILED.value,
                # WHY 保留原始条目：detail 被压成一句话是为了可读；但排查需要的字段路径
                # 与错误类型不能因此丢失。
                "errors": jsonable_encoder(errors),
            },
        )

    @app.exception_handler(500)
    async def _handle_server_error(request: Request, exc: Exception) -> JSONResponse:
        """把未捕获的崩溃渲染成 JSON。

        WHY 必须接：不接时 Starlette 返回的是**纯文本** ``Internal Server Error``——
        那是最不合规范的一种错误响应（前端 json 解析失败，只能兜成 ``HTTP 500``）。

        WHY 显式传 ``exc_info=exc``：本函数由 ``ServerErrorMiddleware`` 在 except 块内
        调用，但不依赖「当前线程仍处于 except 上下文」这个前提，显式给出才不会被将来的
        调用方式变更悄悄弄丢栈信息。
        """
        logger.error(
            "未捕获的请求异常：%s %s", request.method, request.url.path, exc_info=exc
        )
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL_ERROR, "服务内部错误"),
        )


__all__ = [
    "ApiError",
    "DEFAULT_CODE_BY_STATUS",
    "STATUS_BY_CODE",
    "code_for_exception",
    "error_body",
    "install_error_handlers",
]
