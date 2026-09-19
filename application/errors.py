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


REASON_CONCURRENCY = "concurrency"
"""拒绝原因：全局并发已达上限。"""

REASON_RATE = "rate"
"""拒绝原因：该主体在窗口内发起得过于频繁。"""


class RunRejectedError(RuntimeError):
    """本次运行因超出并发上限或被限流而被拒绝。

    WHY 用 429 + Retry-After 而不是排队：排队需要调度器、公平性规则，以及一个
    「请求尚未开始却已占着连接」的等待位；而一次运行可能持续数分钟，等待位会先于
    运行本身耗尽内存。快速失败并明确告知「多久之后可以再来」，客户端才能自己决定
    重试节奏。

    WHY 不是 409：409 表达的是「资源状态冲突、重试无用」，而这里恰恰相反——稍后
    重试正是正确动作。把可重试的拥塞说成冲突，会让客户端与用户都以为出了错。

    WHY 两种拒绝共用一类异常：它们对客户端的含义完全相同（稍后重试），只有
    ``reason`` 不同，供日志与指标区分。

    对应 HTTP 429 Too Many Requests。
    """

    def __init__(self, reason: str, retry_after: int) -> None:
        detail = "并发已达上限" if reason == REASON_CONCURRENCY else "请求过于频繁"
        super().__init__(f"{detail}，请在 {retry_after} 秒后重试")
        self.reason = reason
        self.retry_after = retry_after


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


class VisionUnsupportedError(ValueError):
    """当前模型不接受图片输入，带附件的请求被拒绝。

    WHY 必须显式拒绝而不是把图片丢掉继续跑：静默丢弃会让用户以为「模型看到了图」，
    从而按「它看过这张图」去解读回答——错误结论比一次明确的失败危险得多。这条与
    计划里「不静默退化」的要求一一对应。

    继承 ``ValueError`` 是因为它本质上是「这次请求的输入不被接受」；路由会先按本
    类型映射，以便给出可操作的提示（换哪个模型）。

    对应 HTTP 400 Bad Request。
    """

    def __init__(self, model: str, supported: list[str]) -> None:
        if supported:
            hint = "、".join(supported)
        else:
            hint = "当前没有任何已注册的多模态模型（见 VISION_MODEL_ALIASES）"
        super().__init__(
            f"模型 {model} 不支持图片输入，本次请求未发送图片。请切换到支持多模态的模型：{hint}"
        )
        self.model = model
        self.supported = supported


class UnsupportedDocumentError(ValueError):
    """目标文件不是可索引的文本文档，本次索引被拒绝。

    WHY 显式拒绝而不是跳过：静默跳过会让用户以为「这份文档已经进知识库了」，于是
    检索不到时他会去怀疑检索算法，而不是怀疑「它从来没被索引过」。

    继承 ``ValueError``：它本质上是「这次请求指定的文件不适合做这件事」。路由按 400
    映射并带上原因，用户据此换一份文件或先转换格式。

    对应 HTTP 400 Bad Request。
    """

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path} 无法作为文本文档索引：{reason}")
        self.path = path
        self.reason = reason


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
