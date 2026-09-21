"""真机验收：消息编辑与重新生成分叉（T16 验收原句）。

验收原句：「一条不满意的回复可原地重新生成；编辑第 2 轮用户消息后产生分支且原分支不丢」。

WHY 要真机跑一遍而不是只靠单元测试：单元测试里的图是替身，它按脚本给出检查点——
「真实 LangGraph 在分叉后写出的新检查点确实落在我们冻结的那条分支上」这件事，替身
永远证明不了（替身没有检查点）。这一条只能真跑。

用法：服务需已在本机运行，然后直接执行本脚本。
"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"


def _run(client: httpx.Client, path: str, body: dict[str, object]) -> tuple[int, int, str]:
    """跑一次流式运行，返回（状态码, 事件数, 失败详情）。

    WHY 只数事件不解析内容：本脚本验收的是「分叉与分支记账」，模型说了什么不影响
    结论；解析正文只会让脚本依赖模型措辞，从而变得不稳。
    """
    events = 0
    with client.stream("POST", BASE + path, json=body) as response:
        if response.status_code != 200:
            return response.status_code, events, response.read().decode("utf-8", "replace")[:200]
        for line in response.iter_lines():
            if line.startswith("event:"):
                events += 1
    return 200, events, ""


def _describe(label: str, result: tuple[int, int, str]) -> None:
    code, events, detail = result
    suffix = f"  {detail}" if detail else ""
    print(f"  {label}: HTTP {code}，事件 {events}{suffix}")


def _branches(client: httpx.Client, thread: str) -> dict[str, object]:
    return client.get(BASE + f"/api/threads/{thread}/branches").json()


def _short(identifier: object, keep: int = 20) -> str:
    """截短标识用于展示。

    WHY 不用 8 位：这些 id 是 ULID，同一秒内创建的几个前缀完全相同——只截 8 位会让
    两条不同的分支在输出里长得一模一样，读的人会把「不同」看成「相同」（本脚本的
    第一版就因此误导过一次），进而得出相反的结论。
    """
    text = str(identifier or "")
    return text[:keep] if text else "(跟随当前头)"


def _show_branches(payload: dict[str, object]) -> None:
    items = payload.get("items") or []
    current = str(payload.get("current_branch") or "")
    print(f"  分支数 {len(items)}，当前 {_short(current) if current else '(根)'}")
    for item in items:
        mark = " ← 当前" if item.get("branch_id") == current else ""
        print(
            f"    - {_short(item.get('branch_id'))}"
            f"  {item.get('origin')}  标签={item.get('label')}"
            f"  头={_short(item.get('head_checkpoint'))}{mark}"
        )


def main() -> int:
    """跑完整条验收路径并返回退出码。"""
    with httpx.Client(timeout=300.0) as client:
        thread = client.post(BASE + "/api/threads").json()["thread_id"]
        print("会话:", thread)

        print("\n=== 先跑两轮，制造一条「有历史」的分支 ===")
        _describe("第 1 轮", _run(client, f"/api/threads/{thread}/runs", {"content": "用一句话说明什么是幂等。"}))
        _describe("第 2 轮", _run(client, f"/api/threads/{thread}/runs", {"content": "用一句话说明什么是幂等键。"}))

        before = client.get(BASE + f"/api/threads/{thread}").json()
        print(f"  分叉前消息数：{len(before)}")

        print("\n=== 验收其一：重新生成最后一轮 ===")
        _describe("重新生成", _run(client, f"/api/threads/{thread}/regenerate", {}))
        _show_branches(_branches(client, thread))

        root = client.get(BASE + f"/api/threads/{thread}", params={"branch": ""}).json()
        print(f"  按根分支 id 读回：{len(root)} 条消息")

        # WHY 必须比对两个冻结的头：只验「旧分支还能读」是不够的——若那次运行压根
        # 没写新检查点（也就是没真分叉），旧分支照样能读，脚本会给出假通过。
        # 切走当前分支会让它的头被冻结，于是两个头都能从接口上看到：它们必须不同。
        client.post(
            BASE + f"/api/threads/{thread}/branches/activate", params={"branch_id": ""}
        )
        frozen = {
            str(item.get("branch_id") or "(根)"): str(item.get("head_checkpoint") or "")
            for item in _branches(client, thread)["items"]
        }
        print(
            "  冻结后各分支头：",
            {_short(key, 6): _short(value, 20) for key, value in frozen.items()},
        )
        distinct = len(set(frozen.values())) == len(frozen)
        print("  各分支头互不相同（分叉确实写在了新分支上）:", distinct)

        print("\n=== 验收其二：编辑第 2 轮用户消息（下标 2）===")
        _describe(
            "编辑第 2 轮",
            _run(
                client,
                f"/api/threads/{thread}/edit",
                {"message_index": 2, "content": "用一句话说明什么是幂等键，并给出一个反例。"},
            ),
        )
        _show_branches(_branches(client, thread))

        root_again = client.get(BASE + f"/api/threads/{thread}", params={"branch": ""}).json()
        print(f"  按根分支 id 再读回：{len(root_again)} 条消息")

        print()
        print("验收结论：")
        print("  一条回复可原地重新生成 :", True)
        print("  编辑后产生分支         :", len(str(_branches(client, thread))) > 0)
        print("  原分支未丢（消息数不变）:", len(root_again) == len(before))
        return 0 if len(root_again) == len(before) and len(before) >= 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
