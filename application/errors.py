"""应用层异常类型。

WHY 单独成模块：接口层需要按异常类型映射 HTTP 状态码。异常若定义在各个服务
内部，路由就必须反向依赖具体服务模块，也会让「哪些错误是可预期的」变得不可
枚举——调用方无法只 import 一个模块就看全所有需要处理的失败模式。
"""

from __future__ import annotations


class ThreadBusyError(RuntimeError):
    """目标会话已有运行中的轮次，本次请求被拒绝。

    WHY 必须拒绝而不是排队：同一会话并发发起两轮会让 LangGraph 的图状态产生
    竞争——两轮各自读写同一 thread 的检查点，后写的一方会覆盖先写一方的中间
    结果，表现为消息丢失或工具结果错配。而一次运行可能持续数分钟，让第二个
    请求排队等待会使其无限期挂起，不如快速失败并让客户端决定重试。

    对应 HTTP 409 Conflict。
    """

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"会话 {thread_id} 正在运行中，请等待本轮结束后再发起")
        self.thread_id = thread_id


class NotFoundError(RuntimeError):
    """目标资源不存在。

    对应 HTTP 404 Not Found。
    """

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(f"{resource} 不存在：{identifier}")
        self.resource = resource
        self.identifier = identifier


class OwnershipError(RuntimeError):
    """目标资源存在但当前主体无权访问。

    对应 HTTP 403 Forbidden。
    """

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(f"无权访问 {resource}：{identifier}")
        self.resource = resource
        self.identifier = identifier


class PermissionDeniedError(RuntimeError):
    """当前主体缺少某项权限。

    对应 HTTP 403 Forbidden。
    """

    def __init__(self, permission: str) -> None:
        super().__init__(f"缺少权限：{permission}")
        self.permission = permission


class InterruptExpiredError(RuntimeError):
    """待审批的中断已超过挂起 TTL，本次审批不再被接受。

    WHY 必须拒绝而不是照常恢复：审批卡对应的执行现场已经停留了远超预期的时间，
    期间工作区文件、外部状态乃至模型对任务的理解都可能变了，用户「补一个批准」
    的真实意图往往已不是当时那次调用；让它执行反而更危险。

    对应 HTTP 409 Conflict：资源（那次中断）已不在可应答状态，
    客户端应重新发起对话而不是重试本请求。
    """

    def __init__(self, thread_id: str, ttl_seconds: int) -> None:
        super().__init__(
            f"会话 {thread_id} 的审批请求已超过 {ttl_seconds} 秒未处理，"
            f"本次审批已失效，请重新发起对话"
        )
        self.thread_id = thread_id
        self.ttl_seconds = ttl_seconds
