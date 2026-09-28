"""错误契约测试：每个失败都必须带稳定错误码，且码与状态码自洽。

这次改动的目标不是「多一个字段」，而是让**机器**能分辨同一状态码下的不同失败——
本应用的 409 同时表示「会话正在运行」「文件根已锁定」「场景已锁定」「还没有根」
「根不可用」「审批已过期」「对话框已打开」七种情形，此前调用方只能靠字符串匹配中文文案
去猜该提示什么。因此本文件的断言重点在「不同的失败给出不同的码」，而不只是「有码」。

WHY 手工建 app 而不是走 ``create_app``：``create_app`` 会在 lifespan 里连数据库、连 MCP，
而这里要验证的是**错误契约本身**。代价是必须自己调一次 ``install_error_handlers``——
这正是「处理器挂在 app 上」的必然结果。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.errors import (
    ErrorCode,
    InterruptExpiredError,
    NotFoundError,
    REASON_CONCURRENCY,
    REASON_RATE,
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
from interfaces.web.errors import (
    ApiError,
    DEFAULT_CODE_BY_STATUS,
    STATUS_BY_CODE,
    install_error_handlers,
)
from interfaces.web.routes import router
from interfaces.web.workspace_routes import router as workspace_router


# --------------------------------------------------------------------- 替身


class _ServicesStub:
    """会话根服务集合的替身：只提供被路由真正读到的成员。"""

    attachments = None


class _WorkspacesStub:
    """会话根注册表的替身：要么失败，要么给出一份空的服务集合。

    ``list_directories`` / ``pick_folder`` 是同步方法——路由用 ``asyncio.to_thread``
    调用它们，替身必须与真实签名同形，否则失败路径根本走不到。
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self._error = error

    def _fail_or(self, value: Any) -> Any:
        if self._error is not None:
            raise self._error
        return value

    async def services_for(self, **kwargs: Any) -> _ServicesStub:
        return self._fail_or(_ServicesStub())

    async def describe(self, **kwargs: Any) -> Any:
        if self._error is not None:
            raise self._error
        raise AssertionError("本替身不提供成功的 describe：用例只驱动失败路径")

    def list_directories(self, path: str | None) -> Any:
        if self._error is not None:
            raise self._error
        raise AssertionError("本替身不提供成功的目录列举：用例只驱动失败路径")

    def pick_folder(self, workspace: str | None) -> Any:
        return self._fail_or(None)


class _RunsStub:
    """运行服务替身：每个用例只驱动一条失败路径。"""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    async def resume(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    async def regenerate(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    async def edit(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    def clear_hitl_pending(self, thread_id: str) -> None:
        return None


class _ThreadsStub:
    """会话服务替身：只驱动失败路径。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self._error = error

    async def list_branches(self, thread_id: str) -> Any:
        if self._error is not None:
            raise self._error
        raise AssertionError("本替身不提供成功的分支列举：用例只驱动失败路径")


def _build_client(**state: Any) -> TestClient:
    """构造只挂业务路由、但装了错误处理器的测试客户端。"""
    app = FastAPI()
    for name, value in state.items():
        setattr(app.state, name, value)
    install_error_handlers(app)
    app.include_router(router)
    # 工作区面板的端点在另一个 router 上（它自带 ``/api/workspace`` 前缀）。
    app.include_router(workspace_router)
    return TestClient(app)


# ------------------------------------------------------------- 表自身的完整性


def test_every_code_declares_a_status() -> None:
    """新增错误码却忘了登记状态码，必须在测试里就暴露。"""
    missing = [
        code
        for code in ErrorCode
        if code is not ErrorCode.UNCLASSIFIED and code not in STATUS_BY_CODE
    ]

    assert missing == [], f"以下错误码未登记 HTTP 状态码：{missing}"


def test_unclassified_is_a_sentinel_and_cannot_be_raised() -> None:
    """``unclassified`` 只作兜底标记：显式抛它说明有人想绕过状态码登记。"""
    assert ErrorCode.UNCLASSIFIED not in STATUS_BY_CODE

    with pytest.raises(KeyError):
        ApiError(ErrorCode.UNCLASSIFIED, "不该被显式抛出")


def test_status_values_are_error_statuses() -> None:
    """状态码只应是 4xx / 5xx——把码映射到 2xx 会让「错误」变成成功响应。"""
    invalid = {code: value for code, value in STATUS_BY_CODE.items() if not 400 <= value < 600}

    assert invalid == {}


def test_fallback_codes_are_consistent_with_the_status_table() -> None:
    """兜底表里的「状态 → 码」必须与该码声明的状态一致，否则会出现自相矛盾的响应。"""
    mismatched = {
        status: code
        for status, code in DEFAULT_CODE_BY_STATUS.items()
        if STATUS_BY_CODE[code] != status
    }

    assert mismatched == {}


def test_conflict_codes_share_409_but_stay_distinct() -> None:
    """本次改动的核心：同一个 409 下的七种失败，码必须各不相同。"""
    conflict_codes = {
        ErrorCode.THREAD_BUSY,
        ErrorCode.THREAD_ROOT_LOCKED,
        ErrorCode.THREAD_PRESET_LOCKED,
        ErrorCode.THREAD_ROOT_NOT_READY,
        ErrorCode.THREAD_ROOT_UNAVAILABLE,
        ErrorCode.INTERRUPT_EXPIRED,
        ErrorCode.FOLDER_PICKER_BUSY,
    }

    assert len(conflict_codes) == 7
    assert {STATUS_BY_CODE[code] for code in conflict_codes} == {409}


# --------------------------------------------------------- 框架自己抛出的错误


def test_unmatched_route_reports_not_found() -> None:
    """未匹配路由由框架抛出，调用点改不到——必须由兜底表接住。"""
    client = _build_client()

    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.NOT_FOUND.value
    assert response.json()["detail"]


def test_method_not_allowed_has_its_own_code() -> None:
    client = _build_client()

    response = client.delete("/api/models")

    assert response.status_code == 405
    assert response.json()["code"] == ErrorCode.METHOD_NOT_ALLOWED.value


def test_missing_service_reports_service_unavailable() -> None:
    """未装配的服务是 503 而不是 500，且码要与之匹配。"""
    client = _build_client()

    response = client.get("/api/models")

    assert response.status_code == 503
    assert response.json()["code"] == ErrorCode.SERVICE_UNAVAILABLE.value
    assert "模型目录未初始化" in response.json()["detail"]


def test_validation_error_is_coded_and_stringified() -> None:
    """校验失败的 ``detail`` 恒为字符串（前端 ``new Error(detail)`` 才可读），原始条目另存 ``errors``。"""
    client = _build_client(threads=object())

    response = client.get("/api/threads", params={"limit": 999})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == ErrorCode.REQUEST_VALIDATION_FAILED.value
    assert isinstance(body["detail"], str)
    assert "请求参数校验失败" in body["detail"]
    assert isinstance(body["errors"], list) and body["errors"]


# ------------------------------------------------------------- 会话根的四类冲突


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SessionRootLockedError("t1", "/a", "/b"), ErrorCode.THREAD_ROOT_LOCKED),
        (SessionPresetLockedError("会话 t1", "dev", "doc"), ErrorCode.THREAD_PRESET_LOCKED),
        (SessionRootNotReadyError(), ErrorCode.THREAD_ROOT_NOT_READY),
        (SessionRootUnavailableError("/gone"), ErrorCode.THREAD_ROOT_UNAVAILABLE),
    ],
)
def test_root_conflicts_stay_distinguishable(error: BaseException, expected: ErrorCode) -> None:
    """四种冲突共用 409，但用户下一步动作完全不同——必须能区分。"""
    client = _build_client(workspaces=_WorkspacesStub(error))

    response = client.get("/api/workspace/info")

    assert response.status_code == 409
    assert response.json()["code"] == expected.value


def test_root_conflict_carries_the_original_message() -> None:
    """码是新增的，``detail`` 必须原样保留——既有前端与脚本仍在读它。"""
    message = SessionRootLockedError("t1", "/a", "/b")
    client = _build_client(workspaces=_WorkspacesStub(message))

    response = client.get("/api/workspace/info")

    assert str(message) in response.json()["detail"]


# --------------------------------------------------------------- 会话 ID 与模型


def test_thread_id_invalid_has_its_own_code() -> None:
    """会话 ID 形状不对是 400，但它不是「载荷非法」——客户端该改的是这个 ID。"""
    client = _build_client(threads=_ThreadsStub())

    response = client.get(f"/api/threads/{'x' * 129}/branches")

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.THREAD_ID_INVALID.value


def test_unknown_model_has_its_own_code() -> None:
    """模型别名写错时，客户端该做的是换一个别名，而不是重试。"""
    client = _build_client(
        runs=_RunsStub(KeyError("nope")), workspaces=_WorkspacesStub()
    )

    response = client.post("/api/threads/t1/runs", json={"content": "hi", "model": "nope"})

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.UNKNOWN_MODEL.value


def test_missing_resource_reports_not_found() -> None:
    client = _build_client(threads=_ThreadsStub(NotFoundError("会话", "t1")))

    response = client.get("/api/threads/t1/branches")

    assert response.status_code == 404
    assert response.json()["code"] == ErrorCode.NOT_FOUND.value


# ------------------------------------------------------------------- 运行相关


def test_thread_busy_has_its_own_code() -> None:
    client = _build_client(runs=_RunsStub(ThreadBusyError("t1")), workspaces=_WorkspacesStub())

    response = client.post("/api/threads/t1/runs", json={"content": "hi"})

    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.THREAD_BUSY.value


def test_interrupt_expired_has_its_own_code() -> None:
    """审批过期与「会话占用」同样是 409，但处置是「重新发起对话」而不是「等一会」。"""
    client = _build_client(
        runs=_RunsStub(InterruptExpiredError("t1", 600)),
    )

    response = client.post("/api/threads/t1/resume", json={"decisions": [{"type": "approve"}]})

    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.INTERRUPT_EXPIRED.value


def test_vision_unsupported_has_its_own_code() -> None:
    """失败由替身注入：本用例验的是「这类异常被映射成哪个码」，而不是附件管线本身。"""
    client = _build_client(
        runs=_RunsStub(VisionUnsupportedError("deepseek-flash", ["openai"])),
        workspaces=_WorkspacesStub(),
    )

    response = client.post("/api/threads/t1/runs", json={"content": "看图"})

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.VISION_UNSUPPORTED.value


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (REASON_CONCURRENCY, ErrorCode.RUN_CONCURRENCY_EXCEEDED),
        (REASON_RATE, ErrorCode.RUN_RATE_LIMITED),
    ],
)
def test_run_rejection_reasons_stay_distinguishable(
    reason: str, expected: ErrorCode
) -> None:
    """两种拒绝都要客户端稍后重试，但扩容与排查对应的码不是同一个。"""
    client = _build_client(
        runs=_RunsStub(RunRejectedError(reason, 7)), workspaces=_WorkspacesStub()
    )

    response = client.post("/api/threads/t1/runs", json={"content": "hi"})

    assert response.status_code == 429
    assert response.json()["code"] == expected.value
    # Retry-After 是结构化信息，加码不能顺手把它弄丢
    assert response.headers["Retry-After"] == "7"


# --------------------------------------------------------------- 文件夹选择对话框


@pytest.mark.parametrize(
    ("error", "status_code", "expected"),
    [
        (FolderPickerBusyError(), 409, ErrorCode.FOLDER_PICKER_BUSY),
        (FolderPickerUnavailableError(), 501, ErrorCode.FOLDER_PICKER_UNAVAILABLE),
        (FolderPickerTimeoutError(), 504, ErrorCode.FOLDER_PICKER_TIMEOUT),
    ],
)
def test_folder_picker_failures_stay_distinguishable(
    error: BaseException, status_code: int, expected: ErrorCode
) -> None:
    """「已有弹窗」是等一下，「没有桌面」是换条路，「超时」是重来——三种处置都不同。"""
    client = _build_client(workspaces=_WorkspacesStub(error))

    response = client.post("/api/workspaces/pick")

    assert response.status_code == status_code
    assert response.json()["code"] == expected.value


# ------------------------------------------------------------------- 未捕获异常


def test_unhandled_exception_returns_json_500() -> None:
    """不接 500 时框架回的是**纯文本**——那是最不合规范的一种错误响应。"""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("内置的崩溃")

    # WHY 关掉 raise_server_exceptions：默认的 TestClient 会把异常重新抛给测试，
    # 那样就看不到响应体了；这里要验的正是「响应体长什么样」。
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/boom")

    assert response.status_code == 500
    assert response.json()["code"] == ErrorCode.INTERNAL_ERROR.value
