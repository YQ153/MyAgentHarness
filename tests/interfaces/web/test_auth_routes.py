"""/auth 端点与权限闸门的 HTTP 契约测试。

WHY 补这一组：``interfaces/web/auth`` 此前没有任何测试（上一阶段删除 OIDC 时，随它
一起失效的用例被整块删掉了），于是「apikey 模式下客户端该把凭据放在哪个请求头」
这件事只能靠人肉在浏览器里点。而它恰恰是浏览器唯一能自行认证的通路——放错了头，
表现只是「界面一直说未认证」，与「key 本身不对」长得一模一样。

WHY 只起一个最小应用而不走 ``create_app``：lifespan 会装配数据库、模型与图，而本组
要覆盖的是「请求头 → 主体 → 权限」这条链路，与那些真实依赖无关（同目录其它用例
也是这个取舍）。

WHY 用自建的 ``/probe`` 而不是某个业务端点来断言闸门：本组验的是闸门本身。挂在业务
端点上会让这些断言随着业务端点的权限调整而假失败，而闸门的语义一个字都没变。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from agent.run_context import ANONYMOUS_USER_ID
from application.principal import Principal
from interfaces.web.auth import require_permission
from interfaces.web.auth import router as auth_router
from tests.conftest import make_config

_DEV_KEY = "dev-secret"


class _StubKeyStore:
    """API Key 存储替身：只回答「这把 key 对应哪些元数据」。

    WHY 不接真库：本组验的是「请求头 → 主体」的翻译，把 SQLite 拉进来只会让失败
    指向存储层。真库的路径由 ``tests/runtime/test_api_key_store.py`` 覆盖。
    """

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self._records = records or {}

    async def validate(self, key: str) -> dict[str, Any] | None:
        """返回该 key 的元数据；不存在返回 ``None``。"""
        return self._records.get(key)


class _StubAuditStore:
    """审计存储替身：只回答「列出事件」，用于验证端点确实取到了装配好的存储。"""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []
        self.calls: list[dict[str, Any]] = []

    async def list(
        self,
        *,
        actor_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """记录调用参数并返回预置事件。"""
        self.calls.append(
            {"actor_id": actor_id, "event_type": event_type, "limit": limit, "offset": offset}
        )
        return self.events


def _build_client(
    config: Any, *, api_key_store: Any = None, audit_store: Any = None
) -> TestClient:
    """构造只挂认证路由 + 一个受权限保护的探针的最小应用。

    注意 ``audit_store`` 为 ``None`` 时**不**挂该项——这正是「lifespan 漏铺」在真机上
    的下场，用来断言此时端点的行为（503 而不是 500）。
    """
    app = FastAPI()
    app.state.config = config
    app.state.api_key_store = api_key_store
    if audit_store is not None:
        app.state.audit_store = audit_store
    app.include_router(auth_router)

    @app.get("/probe")
    async def probe(
        principal: Principal = Depends(require_permission("thread:list")),
    ) -> dict[str, Any]:
        return {"user_id": principal.user_id, "auth_method": principal.auth_method}

    return TestClient(app)


def _apikey_config(tmp_path: Any, **overrides: Any) -> Any:
    """apikey 档位的隔离配置；默认带一把可用的 dev key。"""
    params: dict[str, Any] = {"auth_mode": "apikey", "auth_api_key_dev": _DEV_KEY}
    params.update(overrides)
    return make_config(tmp_path, **params)


# ------------------------------------------------------------------ /auth/config


def test_config_exposes_auth_mode_and_key_header(tmp_path):
    """前端靠这两个字段决定「要不要凭据、放进哪个头」。"""
    client = _build_client(make_config(tmp_path))

    body = client.get("/auth/config").json()

    assert body["auth_mode"] == "disabled"
    assert body["auth_api_key_header"] == "X-API-Key"


def test_config_key_header_follows_configuration(tmp_path):
    """请求头名可配置：前端硬编码 X-API-Key 会在改名后静默失效。"""
    client = _build_client(_apikey_config(tmp_path, auth_api_key_header="X-Harness-Key"))

    assert client.get("/auth/config").json()["auth_api_key_header"] == "X-Harness-Key"


# ------------------------------------------------------------------ disabled 档位


def test_disabled_mode_grants_anonymous_admin(tmp_path):
    """认证关闭时按匿名管理员放行：本地开发形态的全部功能都依赖它。"""
    client = _build_client(make_config(tmp_path))

    probe = client.get("/probe")
    me = client.get("/auth/me")

    assert probe.status_code == 200
    assert probe.json() == {"user_id": ANONYMOUS_USER_ID, "auth_method": "disabled"}
    assert me.status_code == 200
    assert me.json()["role"] == "admin"


# ------------------------------------------------------------------ apikey 档位


def test_apikey_mode_rejects_missing_credential(tmp_path):
    """没有凭据一律 401，并带上 WWW-Authenticate 供客户端识别。"""
    client = _build_client(_apikey_config(tmp_path))

    probe = client.get("/probe")

    assert probe.status_code == 401
    assert probe.headers["www-authenticate"] == "Bearer"
    assert client.get("/auth/me").status_code == 401


def test_apikey_mode_accepts_dev_key_in_configured_header(tmp_path):
    """dev key 走配置的请求头即可通过，且主体是管理员级的 apikey:dev。"""
    client = _build_client(_apikey_config(tmp_path))

    response = client.get("/probe", headers={"X-API-Key": _DEV_KEY})

    assert response.status_code == 200
    assert response.json() == {"user_id": "apikey:dev", "auth_method": "apikey"}


def test_dev_key_also_accepted_as_bearer_token(tmp_path):
    """``Authorization: Bearer`` 是同一条校验路径；CLI 与脚本走的是它。"""
    client = _build_client(_apikey_config(tmp_path))

    response = client.get("/probe", headers={"Authorization": f"Bearer {_DEV_KEY}"})

    assert response.status_code == 200
    assert response.json()["user_id"] == "apikey:dev"


def test_custom_header_name_replaces_default(tmp_path):
    """改名后默认头失效、自定义头生效——前端必须读 /auth/config 的理由。"""
    client = _build_client(_apikey_config(tmp_path, auth_api_key_header="X-Harness-Key"))

    assert client.get("/probe", headers={"X-API-Key": _DEV_KEY}).status_code == 401
    assert client.get("/probe", headers={"X-Harness-Key": _DEV_KEY}).status_code == 200


def test_unknown_key_is_rejected(tmp_path):
    """库里的 key 才生效；错误 key 一律 401，且不透露「是不存在还是已吊销」。"""
    store = _StubKeyStore(
        {
            "harness_good": {
                "key_id": "k1",
                "key_prefix": "harness_",
                "role": "member",
                "scopes": "",
            }
        }
    )
    client = _build_client(
        _apikey_config(tmp_path, auth_api_key_dev=""), api_key_store=store
    )

    assert client.get("/probe", headers={"X-API-Key": "wrong"}).status_code == 401
    accepted = client.get("/probe", headers={"X-API-Key": "harness_good"})
    assert accepted.status_code == 200
    assert accepted.json()["user_id"] == "apikey:k1"


def test_insufficient_role_is_403_not_401(tmp_path):
    """已认证但权限不足是 403：401 会让客户端以为要换凭据，从而陷入重试死循环。"""
    store = _StubKeyStore(
        {
            "harness_viewer": {
                "key_id": "k2",
                "key_prefix": "harness_",
                "role": "viewer",
                "scopes": "",
            }
        }
    )
    client = _build_client(
        _apikey_config(tmp_path, auth_api_key_dev=""), api_key_store=store
    )

    forbidden = client.get("/auth/api-keys", headers={"X-API-Key": "harness_viewer"})
    anonymous = client.get("/auth/api-keys")

    assert forbidden.status_code == 403
    assert "apikey:manage" in forbidden.json()["detail"]
    assert anonymous.status_code == 401


def test_api_key_store_unavailable_yields_401(tmp_path):
    """存储未装配时按未认证处理，而不是 500——凭据无法校验不等于服务不可用。"""
    client = _build_client(_apikey_config(tmp_path, auth_api_key_dev=""))

    assert client.get("/probe", headers={"X-API-Key": "anything"}).status_code == 401


# ------------------------------------------------------------------ /auth/audit


def test_audit_endpoint_reads_events_from_wired_store(tmp_path):
    """审计查询端点必须能取到 lifespan 铺好的存储。

    WHY 单独立一条：``app.state.audit_store`` 曾经漏铺，这个端点在真机上直接 500
    （AttributeError），而单元测试里没人挂过它、也就没人发现。
    """
    store = _StubAuditStore(
        [{"event_type": "apikey_auth_success", "actor_id": "apikey:dev"}]
    )
    client = _build_client(
        _apikey_config(tmp_path), api_key_store=_StubKeyStore(), audit_store=store
    )

    response = client.get("/auth/audit?limit=10", headers={"X-API-Key": _DEV_KEY})

    assert response.status_code == 200
    assert response.json()[0]["event_type"] == "apikey_auth_success"
    # 查询参数必须原样透传：limit 被吞掉的表现是「面板永远只显示 50 条」
    assert store.calls[0]["limit"] == 10


def test_audit_endpoint_reports_503_when_store_missing(tmp_path):
    """存储未装配是 503（暂时不可用），不是 500——后者会把「漏装配」说成「服务有 bug」。"""
    client = _build_client(_apikey_config(tmp_path), api_key_store=_StubKeyStore())

    response = client.get("/auth/audit", headers={"X-API-Key": _DEV_KEY})

    assert response.status_code == 503
    assert "未初始化" in response.json()["detail"]
