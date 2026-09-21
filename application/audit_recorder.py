"""业务审计的写入通道协议。

WHY 需要它：``RunGovernor``（治理动作归因给系统）与 ``RunBranchService``（分叉登记）
都要写审计，但它们是 ``RunService`` 的协作者，不能反过来依赖那个上层编排类——那会
形成环。用 ``typing.Protocol`` 描述「能写一条业务审计」这一项能力之后，两侧都只依赖
这个窄接口：生产代码传 ``RunService._audit``，测试传一个记录用的替身，无需继承。
"""

from __future__ import annotations

from typing import Any, Protocol


class AuditRecorder(Protocol):
    """写入一条业务审计事件的能力。

    与 ``application.ports.AuditSink`` 的区别：那个描述的是**存储**能力（含查询），
    由 ``runtime`` 实现、供接口层使用；这一个描述的是**应用层的写入动作**——它已经
    补齐了 IP / UA / trace_id 等请求上下文，调用方只需给出业务语义字段。
    """

    async def __call__(
        self,
        *,
        event_type: str,
        actor_id: str,
        target_id: str | None = None,
        action: str | None = None,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """记录一条审计事件。

        实现方必须自行吞掉写入异常并留日志：审计是旁路职责，不能因为日志库不可用
        而让已经成功的业务操作变成错误。
        """
        ...
