"""真机验收：导出文件可再导入成新会话且内容一致（T19 验收原句）。

WHY 要跑真机而不是只靠单元测试：单元测试用的是最小图，它证明的是「我们写状态的
方式正确」；而真实 Agent 图的 state 结构、消息类型要求都由上游决定，**它肯不肯
接受这份还原出来的历史**只有真图能回答。

用法：服务需已在本机运行。
"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"


def _run(client: httpx.Client, thread: str, text: str) -> int:
    """跑一轮真实对话，返回收到的事件数。"""
    events = 0
    with client.stream(
        "POST", BASE + f"/api/threads/{thread}/runs", json={"content": text}
    ) as response:
        if response.status_code != 200:
            return -1
        for line in response.iter_lines():
            if line.startswith("event:"):
                events += 1
    return events


def main() -> int:
    """跑完整条验收路径并返回退出码。"""
    with httpx.Client(timeout=300.0) as client:
        thread = client.post(BASE + "/api/threads").json()["thread_id"]
        print("会话：", thread)
        print("  第 1 轮：HTTP 200，事件", _run(client, thread, "用一句话说明什么是幂等。"))

        exported = client.get(BASE + f"/api/threads/{thread}/export", params={"format": "json"})
        payload = exported.json()
        print("\n=== 导出 ===")
        print("  HTTP", exported.status_code, "|", exported.headers.get("content-type"))
        print("  附件头：", exported.headers.get("content-disposition"))
        print("  消息数：", len(payload["messages"]), "| 版本：", payload["version"])

        markdown = client.get(
            BASE + f"/api/threads/{thread}/export", params={"format": "markdown"}
        )
        print('  Markdown：', markdown.status_code, len(markdown.text), "字符 | 含角色标题：", "## 用户" in markdown.text)

        imported = client.post(BASE + "/api/threads/import", json=payload)
        result = imported.json()
        print("\n=== 导入 ===")
        print("  HTTP", imported.status_code, "| 新会话", result["thread_id"][:12])
        print("  复原消息：", result["message_count"], "| 丢弃：", result["skipped_messages"])
        print("  用量说明：", result["usage_note"])

        re_exported = client.get(
            BASE + f"/api/threads/{result['thread_id']}/export"
        ).json()
        same_content = [item["content"] for item in re_exported["messages"]] == [
            item["content"] for item in payload["messages"]
        ]
        same_roles = [item["role"] for item in re_exported["messages"]] == [
            item["role"] for item in payload["messages"]
        ]

        print("\n=== 结论 ===")
        print("  往返内容逐条一致：", same_content)
        print("  角色序列一致    ：", same_roles)
        print("  新会话 ≠ 原会话 ：", result["thread_id"] != thread)
        print("  原会话仍可访问  ：", client.get(BASE + f"/api/threads/{thread}").status_code == 200)
        return 0 if same_content and same_roles else 1


if __name__ == "__main__":
    raise SystemExit(main())
