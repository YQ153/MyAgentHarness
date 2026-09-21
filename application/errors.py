"""应用层异常类型。

WHY 单独成模块：接口层需要按异常类型映射 HTTP 状态码。异常若定义在各个服务
内部，路由就必须反向依赖具体服务模块，也会让「哪些错误是可预期的」变得不可
枚举——调用方无法只 import 一个模块就看全所有需要处理的失败模式。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


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


class SessionRootLockedError(RuntimeError):
    """会话的文件根已经锁定，本次请求给出的工作空间与它不一致。

    WHY 必须拒绝而不是「以请求为准」：根一旦锁定，这条会话的文件、技能视图、附件目录与
    索引都锚在它上面；中途换根等于把半条会话留在旧目录、半条写到新目录，而两侧都不会
    报错。要换工作空间就新建会话——那是唯一能把「什么时候换的根」讲清楚的形态。

    WHY 锁定时刻是「产生第一条交互」而不是「创建会话」：创建会话时用户还在选，允许他
    改主意；而一旦 Agent 已经在那个根里读过或写过文件，再换就会让此前的产物失联。

    WHY 不继承 ``ValueError``：这不是「输入格式不对」，而是「操作与既有状态冲突」——
    路由按 409 映射（与 ``ThreadBusyError`` 同码）：重试本请求无用，客户端应改参数或
    新建会话。

    对应 HTTP 409 Conflict。
    """

    def __init__(self, thread_id: str, current: str, requested: str) -> None:
        super().__init__(
            f"会话 {thread_id} 的文件根已锁定为 {current}，本次请求给的却是 {requested}；"
            "它不支持中途更换，请新建一个会话"
        )
        self.thread_id = thread_id
        self.current = current
        self.requested = requested


class SessionRootNotReadyError(RuntimeError):
    """这次请求给不出文件根：**既没有会话 ID，也没有工作空间**。

    WHY 会出现：草稿态（连会话 ID 都还没申请）下的面板请求就是这样——没有 ID 就没有可
    派生的专属目录，没有工作空间就没有用户指定的根。

    WHY 「未绑定工作空间的**新**会话」**不在**这一种：它的 ID 在首次发送前已经发出，专属
    目录由该 ID 派生得出（见 ``SessionRegistry.resolve``），所以为它报这个错等于把「不选
    工作空间」这条**正常路径**变成「发不出第一条消息」——而提示里的下一步（「请先发出
    第一条消息」）照着做也出不去。这条错误因此只覆盖「连 ID 都没有」的情形。

    WHY 不偷偷退到某个默认目录：那会让用户在「以为在看自己的项目」的面板里看到应用自己的
    目录，而两者都不会报错。如实说「还没有根、先选工作空间或先发第一条消息」，用户下一步
    该做什么是明确的。

    对应 HTTP 409 Conflict（状态尚未就绪，稍后或换个操作即可）。
    """


class SessionRootUnavailableError(RuntimeError):
    """这条会话的文件根**已经确定**，但它当前不可用（用户选定的目录不见了）。

    WHY 与 ``SessionRootNotReadyError`` 分开：两者的下一步动作完全不同——「还没确定」等
    第一条消息就有了；「已确定但目录没了」只能把目录恢复回来（或删掉这条会话），等多久都
    不会自己好。混成一句话会让用户去等一件永远不会发生的事。

    WHY 不替用户把这个目录重建出来：那是他**选定**的项目目录，重建只会得到一个同名的空
    目录——Agent 会在里面「找不到文件」并写下新文件，而用户看到的现象是「我的项目被清空
    了」。应用自己建的专属目录是另一回事（那是我们的目录，按需创建由
    ``SessionRegistry.resolve`` 负责），所以这条异常只覆盖用户目录的情形。

    WHY 不能是 500：调用方（用户）没有任何办法通过重试或改参数让它通过吗？不是——把目录
    恢复回来就可以。所以这是个「当前状态不允许」的 409，而不是服务端故障。

    对应 HTTP 409 Conflict。
    """

    def __init__(self, path: str | Path, reason: str = "目录不存在或不是目录") -> None:
        super().__init__(
            f"这条会话的文件根当前不可用：{path}（{reason}）。"
            "它由你选定，应用不会替它重建；请恢复该目录后重试，或删除这条会话"
        )
        self.path = str(path)
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
