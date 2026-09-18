"""技术验证：LangGraph 检查点能否按 ``checkpoint_id`` 从任意历史点分叉（T16 首个动作）。

WHY 用最小图而不是直接跑本项目的 Agent：这里要问的是**上游能力**，不是我们的行为。
本项目那张图带模型、工具、审批中断，任何一个环节出错都会让「分叉到底行不行」这个
结论被噪声淹没；最小图（一个回声节点 + 真实的 AsyncSqliteSaver）把变量降到只剩
「检查点」这一件事，而检查点正是分叉的全部依赖。

四个问题：
1. 能否从任意历史检查点续跑，并产生**新的分支**而不是覆盖旧路径？
2. 分叉后，旧分支的检查点是否仍在、还能否按 id 读回？
3. 分叉后 ``aget_state``（不带 id）指向谁？——这决定界面默认显示哪一支。
4. ``aupdate_state`` 能否在历史点上改写消息（「编辑某轮用户消息」的实现基础）？

结论决定 T16 的实现路径，因此脚本原样保留，可复跑复核。
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

THREAD = "fork-probe"


class ProbeState(TypedDict):
    """只保留一个关键状态键：与真实图同形的消息列表。"""

    messages: Annotated[list[Any], add_messages]


async def _echo(state: ProbeState) -> dict[str, Any]:
    """回声节点：把最后一条消息反射成助手回复，充当一次「模型调用」。"""
    last = state["messages"][-1]
    return {"messages": [AIMessage(content=f"回复<{last.content}>")]}


def _build_graph(saver: AsyncSqliteSaver) -> Any:
    builder = StateGraph(ProbeState)
    builder.add_node("echo", _echo)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile(checkpointer=saver)


def _config(checkpoint_id: str | None = None) -> dict[str, Any]:
    """构造运行配置；``checkpoint_id`` 缺省表示「取该会话当前分支的头」。"""
    configurable: dict[str, Any] = {"thread_id": THREAD}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


async def _dump(graph: Any, title: str, checkpoint_id: str | None = None) -> list[Any]:
    """打印某条分支的完整检查点链，并返回它。"""
    chain = [item async for item in graph.aget_state_history(_config(checkpoint_id))]
    print(f"\n--- {title} ---")
    print(f"检查点数量：{len(chain)}")
    for index, item in enumerate(reversed(chain), start=1):
        cid = item.config["configurable"]["checkpoint_id"]
        contents = [getattr(msg, "content", "") for msg in item.values.get("messages", [])]
        print(f"  {index}. {cid}  next={item.next}  消息={contents}")
    return chain


async def main() -> int:
    """跑完四个问题并把结论打出来。"""
    import langgraph

    print("langgraph 版本：", getattr(langgraph, "__version__", "未知"))

    db_path = pathlib.Path(tempfile.mkdtemp()) / "probe.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        await saver.setup()
        graph = _build_graph(saver)

        # ---------------------------------------------------------- 先跑三轮
        for turn in (1, 2, 3):
            await graph.ainvoke(
                {"messages": [HumanMessage(content=f"第{turn}轮", id=f"h{turn}")]},
                _config(),
            )
        original = await _dump(graph, "原始分支（三轮）")

        head = await graph.aget_state(_config())
        print("\n头节点消息：", [m.content for m in head.values.get("messages", [])])

        # 取「第 1 轮之后」的检查点作为分叉点：它是 3 条消息（h1、回复<h1>）
        fork_point = None
        for item in reversed(original):
            contents = [getattr(m, "content", "") for m in item.values.get("messages", [])]
            if len(contents) == 2:
                fork_point = item.config["configurable"]["checkpoint_id"]
                break
        print("\n选定分叉点（第 1 轮之后）：", fork_point)

        # ------------------------------------------------ 问题 1：从历史点分叉
        print("\n=== 问题 1：按 checkpoint_id 续跑是否产生新分支 ===")
        await graph.ainvoke(
            {"messages": [HumanMessage(content="第1轮改", id="h1b")]},
            _config(fork_point),
        )
        forked = await _dump(graph, "分叉后的分支")
        new_head = forked[0].config["configurable"]["checkpoint_id"]
        print("新分支头：", new_head)
        print("新头与旧头不同（说明写的是新检查点而非覆盖）：", new_head != original[0].config["configurable"]["checkpoint_id"])

        # ---------------------------------------------- 问题 2：旧分支是否还在
        print("\n=== 问题 2：旧分支是否仍可按 id 读回 ===")
        old_head_id = original[0].config["configurable"]["checkpoint_id"]
        old_state = await graph.aget_state(_config(old_head_id))
        old_messages = [m.content for m in old_state.values.get("messages", [])]
        print("按旧头 id 读回的消息：", old_messages)
        # 判据是「旧分支后半段还在」，而不是消息条数——条数会随图的节点结构变化
        print("旧分支后半段仍在（第 3 轮没被分叉吃掉）：", "第3轮" in old_messages)

        old_chain = [item async for item in graph.aget_state_history(_config(old_head_id))]
        print("旧分支检查点仍可枚举，数量：", len(old_chain))

        # ------------------------------------ 问题 3：不带 id 的 aget_state 指向谁
        print("\n=== 问题 3：不带 checkpoint_id 时头指向谁 ===")
        now_head = await graph.aget_state(_config())
        now_id = now_head.config["configurable"]["checkpoint_id"]
        print("当前头 id：", now_id)
        print("当前头消息：", [m.content for m in now_head.values.get("messages", [])])
        print("头指向新分支：", now_id == new_head)

        # ---------------------------------- 问题 4：aupdate_state 改写历史消息
        print("\n=== 问题 4：aupdate_state 能否在历史点上改写消息 ===")
        try:
            await graph.aupdate_state(
                _config(fork_point),
                {"messages": [RemoveMessage(id="h1b"), HumanMessage(content="第1轮再改", id="h1c")]},
            )
            print("不带 checkpoint_ns 的改写：成功")
        except Exception as exc:  # noqa: BLE001 - 探针要把失败原因原样带出来
            print("不带 checkpoint_ns 的改写失败：", type(exc).__name__, exc)

        # 带 checkpoint_ns 再试一次：这是 update_state 在指定 checkpoint_id 时的必填项
        try:
            config = _config(fork_point)
            config["configurable"]["checkpoint_ns"] = ""
            await graph.aupdate_state(
                config,
                {"messages": [RemoveMessage(id="h1b"), HumanMessage(content="第1轮再改", id="h1c")]},
            )
            edited = await graph.aget_state(_config())
            print("带 checkpoint_ns 的改写：成功")
            print("改写后头消息：", [m.content for m in edited.values.get("messages", [])])
        except Exception as exc:  # noqa: BLE001
            print("带 checkpoint_ns 的改写失败：", type(exc).__name__, exc)

        # --------------------------- 问题 6：能否给全新会话直接写入历史
        print("\n=== 问题 6：新会话上用 aupdate_state 写入历史 ===")
        try:
            fresh = "import-probe"
            await graph.aupdate_state(
                {"configurable": {"thread_id": fresh}},
                {"messages": [HumanMessage(content="导入的问", id="i-h"), AIMessage(content="导入的答", id="i-a")]},
            )
            imported = await graph.aget_state({"configurable": {"thread_id": fresh}})
            imported_messages = [m.content for m in imported.values.get("messages", [])]
            print("写入后读回的消息：", imported_messages)
            print("内容一致：", imported_messages == ["导入的问", "导入的答"])
            print("拿到了检查点 id：", bool(imported.config["configurable"].get("checkpoint_id")))
        except Exception as exc:  # noqa: BLE001 - 探针要把失败原因原样带出来
            print("写入失败：", type(exc).__name__, exc)

        # --------------------------- 问题 5：切回旧分支继续对话是否干扰新分支
        print("\n=== 问题 5：在旧分支上继续，新分支是否不受影响 ===")
        branch_a_head = fork_point  # 旧分支此刻的头就是分叉点本身
        forked_head_before = (await graph.aget_state(_config())).config["configurable"][
            "checkpoint_id"
        ]
        await graph.ainvoke(
            {"messages": [HumanMessage(content="旧分支继续", id="h-a")]},
            _config(branch_a_head),
        )
        state_a = await graph.aget_state(_config())
        state_b = await graph.aget_state(_config(forked_head_before))
        messages_a = [m.content for m in state_a.values.get("messages", [])]
        messages_b = [m.content for m in state_b.values.get("messages", [])]
        print("旧分支继续后的头消息：", messages_a)
        print("新分支仍可读回：", messages_b)
        print("两条分支内容不同（互不覆盖）：", messages_a != messages_b)
        print("新分支未被写入『旧分支继续』：", "旧分支继续" not in messages_b)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
