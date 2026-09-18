"""长期记忆命名空间的归属、兜底与隔离测试。

WHY 这一层必须单独覆盖：记忆隔离不靠接口层的权限判断，而靠命名空间本身。
命名空间算错时，越权发生在「Agent 读文件」这条路径上——没有 HTTP 状态码会
体现出来，日志里也只是一次普通的 ``read_file`` 调用。
"""

from __future__ import annotations

from typing import Any

import pytest
from deepagents.backends.store import _validate_namespace
from langgraph.store.memory import InMemoryStore

import deepagents.backends.store as store_module
from agent.backends import build_backend
from agent.run_context import (
    ANONYMOUS_USER_ID,
    MEMORY_NAMESPACE_ROOT,
    AgentRunContext,
    memory_namespace,
    memory_owner_of,
    namespace_of_runtime,
)
from application.principal import ANONYMOUS_PRINCIPAL
from tests.conftest import make_config


def _config(tmp_path):
    """构造配置并建好工作区目录。

    WHY 必须显式建目录：``build_backend`` 会拒绝不存在的工作区（这是刻意的
    ——指向一个不存在的根会让所有相对路径静默落到别处）。
    """
    config = make_config(tmp_path)
    config.ensure_directories()
    return config


class _Runtime:
    """最小运行时替身：命名空间工厂只读 ``context`` 一个字段。"""

    def __init__(self, context: object | None) -> None:
        self.context = context


def _with_runtime(context: object | None):
    """让 backend 在图外看到指定 context。

    WHY 替换 ``get_runtime`` 而不是伪造后端：真正要验证的是「工厂 + deepagents
    的校验 + 存储命名空间」这条完整链路，把运行时塞进去才能覆盖到它。
    """
    return _Runtime(context)


# ------------------------------------------------------------------ 归属计算


def test_namespace_is_scoped_by_user():
    assert memory_namespace("alice") == (MEMORY_NAMESPACE_ROOT, "alice")
    assert memory_namespace("alice") != memory_namespace("bob")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_namespace_falls_back_to_anonymous(value):
    assert memory_namespace(value) == (MEMORY_NAMESPACE_ROOT, ANONYMOUS_USER_ID)


def test_anonymous_principal_shares_one_pool():
    """认证关闭时，接口层的匿名主体必须与命名空间兜底是同一个池子。

    WHY 断言相等而不是各写一份：两处字面量一旦漂移，会得到「Web 写的记忆
    CLI 读不到」这种现象——功能没坏，但用户会觉得记忆随机丢失。
    """
    assert ANONYMOUS_PRINCIPAL.user_id == ANONYMOUS_USER_ID


@pytest.mark.parametrize(
    "user_id",
    [
        "auth0|abc",
        "https://idp.example.com/users/1",
        "张三",
        "a b",
        "a*b",
        "a?b",
        "a/b",
        "a[b]",
    ],
)
def test_namespace_is_acceptable_to_deepagents(user_id):
    """主体标识可能来自 IdP，含非法字符时必须散列而不是原样透出。

    WHY 直接调用 deepagents 的校验函数：字符集规则由它定义，自己写一份
    「看起来一样」的正则在对方收紧或放宽时会静默失配，而失配的表现是
    工具调用期抛 ValueError——那条路径上没人会想到是命名空间的问题。
    """
    namespace = memory_namespace(user_id)

    assert _validate_namespace(namespace) == namespace
    assert namespace[1] != user_id


def test_hashed_component_is_stable_and_distinct():
    assert memory_namespace("auth0|abc") == memory_namespace("auth0|abc")
    assert memory_namespace("auth0|abc") != memory_namespace("auth0|abd")


@pytest.mark.parametrize("user_id", ["alice", "user.name+tag@example.com", "a:b~c_d-e"])
def test_safe_identifier_stays_readable(user_id):
    """合法标识必须原样保留：命名空间是排障时唯一能人工核对的线索。"""
    assert memory_namespace(user_id)[1] == user_id


# ------------------------------------------------------------------ 上下文取值


def test_owner_reads_context_user_id():
    runtime = _with_runtime(AgentRunContext(user_id="alice"))

    assert memory_owner_of(runtime) == "alice"


@pytest.mark.parametrize("runtime", [None, object(), _with_runtime(None)])
def test_owner_falls_back_when_runtime_missing(runtime):
    """图外调用（管理路径、单元测试）拿不到运行时，必须兜底而不是抛错。

    WHY：``StoreBackend`` 在图外被调用时 deepagents 会把 ``None`` 交给工厂；
    此时抛异常等于让这些路径上的每一次文件操作都失败。
    """
    assert memory_owner_of(runtime) == ANONYMOUS_USER_ID


def test_namespace_of_runtime_uses_context():
    runtime = _with_runtime(AgentRunContext(user_id="alice"))

    assert namespace_of_runtime(runtime) == memory_namespace("alice")


@pytest.mark.parametrize("blank", ["", "   "])
def test_context_rejects_blank_user_id(blank):
    with pytest.raises(ValueError):
        AgentRunContext(user_id=blank)


def test_context_rejects_non_string_user_id():
    with pytest.raises(ValueError):
        AgentRunContext(user_id=123)  # type: ignore[arg-type]


def test_context_strips_whitespace():
    assert AgentRunContext(user_id=" alice ").user_id == "alice"


# ------------------------------------------------------------------ 存储隔离


def test_out_of_graph_write_lands_in_anonymous_pool(tmp_path):
    """图外写入按匿名主体归档：既不报错，也不会凭空造出一个命名空间。"""
    store = InMemoryStore()
    backend = build_backend(_config(tmp_path), store)

    backend.write("/memories/notes.md", "hi")

    assert [item.key for item in store.search(memory_namespace(ANONYMOUS_USER_ID))] == [
        "/notes.md"
    ]


def test_memory_pools_are_isolated_per_user(tmp_path, monkeypatch):
    """同一路径下两个主体互不可见——这是「跨用户记忆泄漏」的回归线。"""
    store = InMemoryStore()
    backend = build_backend(_config(tmp_path), store)

    monkeypatch.setattr(store_module, "get_runtime", lambda: _Runtime(AgentRunContext("alice")))
    backend.write("/memories/notes.md", "alice 的偏好")

    monkeypatch.setattr(store_module, "get_runtime", lambda: _Runtime(AgentRunContext("bob")))
    bob_read = backend.read("/memories/notes.md")

    assert [item.value["content"] for item in store.search(memory_namespace("alice"))] == [
        "alice 的偏好"
    ]
    # Bob 的池子里什么都没有：读不到内容，且不该出现「文件已存在」这类结果
    assert store.search(memory_namespace("bob")) == []
    assert "alice 的偏好" not in str(bob_read)


# ------------------------------------------------------------------ 图装配


class _FakeRegistry:
    """模型注册表替身：装配测试不关心模型从哪来。"""

    default_name = "fake"

    def get(self, name: str | None = None) -> object:
        return object()


def test_build_agent_declares_context_schema(tmp_path, monkeypatch):
    """图必须声明 ``context_schema``，否则主体传不进去、记忆只能落匿名池。

    WHY 用替身替换 ``create_deep_agent``：装配真实图需要可用的模型与密钥，
    而本用例要钉住的只是「参数有没有传对」——那是隔离能否成立的前提。
    """
    from agent import graph as graph_module
    from agent.graph import build_agent

    captured: dict[str, Any] = {}
    monkeypatch.setattr(graph_module, "get_registry", lambda config: _FakeRegistry())
    monkeypatch.setattr(
        graph_module,
        "create_deep_agent",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    build_agent(_config(tmp_path), store=InMemoryStore())

    assert captured["context_schema"] is AgentRunContext
    assert captured["store"] is not None


def test_build_agent_requires_store(tmp_path):
    """漏传 store 必须立刻失败：静默退回内存会让长期记忆在重启后消失。"""
    from agent.graph import AgentFactory, build_agent

    with pytest.raises(ValueError, match="store"):
        build_agent(_config(tmp_path), store=None)
    with pytest.raises(ValueError, match="store"):
        AgentFactory(_config(tmp_path), store=None)


def test_hashed_owner_reads_back_its_own_pool(tmp_path, monkeypatch):
    """散列过的标识也要能稳定读写自己的池子（否则换一种标识来源就会一写就丢）。"""
    store = InMemoryStore()
    backend = build_backend(_config(tmp_path), store)
    runtime = _Runtime(AgentRunContext("auth0|abc"))
    monkeypatch.setattr(store_module, "get_runtime", lambda: runtime)

    backend.write("/memories/prefs.md", "中文")
    read_back = backend.read("/memories/prefs.md")

    namespace = memory_namespace("auth0|abc")
    assert namespace != ("memories", "auth0|abc")
    assert [item.key for item in store.search(namespace)] == ["/prefs.md"]
    assert "中文" in str(read_back)
