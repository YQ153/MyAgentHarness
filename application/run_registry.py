"""运行登记表：一次运行的槽位、计数与人工审批挂起状态的唯一所有者。

职责边界：只维护「本次运行的即时状态」并回答关于它的查询与准入判定，不推进运行、
不翻译事件、不写审计。运行句柄（``RunHandle``）也定义在这里，因为它就是这张
登记表的元素类型——把「登记表的元素」挪出去，两个模块还得互相 import。

WHY 单独成模块而不是留在 ``RunService`` 里：槽位占用、累计计数、HITL 挂起与过期
标记、并发上限与限流判定读写的是**同一份**状态，必须共用同一把锁才能保证「指标
采集读到的组合自洽」（例如不会出现「槽位已满但拒绝数为 0」）。把这组状态和它的
读写收进一个类之后，治理巡检与指标端点只能通过它的方法访问，不会再有人「顺手
在别处另建一份计数」——那正是这类状态最终漂移的原因。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent.run_context import ANONYMOUS_USER_ID
from application.errors import (
    REASON_CONCURRENCY,
    REASON_RATE,
    RunRejectedError,
    ThreadBusyError,
)
from runtime.rate_limiter import RateLimiter

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

STOP_REASON_STOPPED = "stopped"
"""DONE 事件的停止原因：用户主动停止。"""

STOP_REASON_TIMEOUT = "timeout"
"""DONE 事件的停止原因：运行超过 ``run_max_seconds`` 被治理协程强制取消。

WHY 与 ``stopped`` 区分：两者对前端都是「流已关闭、内容不完整」，但责任方
不同——一个是用户按了停止，一个是系统判定超时；混成一个值会让用户在没有
任何操作的情况下看到「已停止」，从而误判界面出了 bug。
"""


@dataclass(frozen=True, eq=False)
class RunHandle:
    """一次运行中会话的运行句柄。

    WHY 独立成类而不是继续用裸集合：停止（``stop``）、运行超时（后续迭代）
    与运行指标（``/metrics``）都需要「thread_id → 取消信号 + 开始时间」这
    同一份登记，各自另写一套必然出现口径不一致（例如超时任务看到的运行
    集合与 stop 看到的不一致）。

    eq=False：句柄的身份就是对象本身，按字段比较两个句柄（含 ``Event``）
    没有意义，反而容易在集合操作中被误判相等。
    """

    thread_id: str
    started_at: float
    """``time.monotonic()`` 口径的开始时间，用于超时判断与指标。"""
    cancel_event: asyncio.Event
    """停止信号；置位后运行在下一个分片边界被中止。"""
    model_name: str | None = None
    """本轮使用的模型别名；``None`` 表示默认模型（落用量时按配置解析）。"""
    owner_id: str = ""
    """会话所有者；用量记录按它聚合，认证关闭时为空串。"""
    actor_id: str = ""
    """发起本轮运行的主体标识；工具审计按它归因，认证关闭时为 ``anonymous``。"""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    """本轮发生的工具调用记录。

    WHY 挂在句柄上而不是服务上：一轮运行的工具调用天然属于这一轮，按
    thread_id 另建一份字典会多出一套「运行结束即清理」的生命周期管理，
    而句柄本来就随运行释放。

    WHY 用可变列表而不是 frozen 语义：记录是在同步热路径（``_track_event``）
    里逐条追加的，落库则在流结束后统一进行；可变容器是这一写多读场景下
    唯一不需要加锁的形态。
    """
    stop_reason: str | None = None
    """停止原因；``None`` 表示尚未收到停止请求，取值见模块级常量。"""
    fork_checkpoint: str = ""
    """本次运行的分叉起点检查点 id；空串表示接着当前分支的头跑。"""
    workspace: str = ""
    """本轮运行的工作区绝对路径；空串表示「未声明」。

    WHY 挂在句柄上而不是每次重读配置：工具输出留存与「完整输出」的虚拟路径引用都要
    按**本轮运行**的工作区换算，而收尾阶段（流结束、写用量、剪留存）已经拿不到入口
    参数；句柄本来就是「这一轮的全部信息」的载体，与此前的模型别名、所有者同一理由。
    """

    @property
    def stop_requested(self) -> bool:
        """是否已收到停止请求。"""
        return self.cancel_event.is_set()

    @property
    def elapsed_seconds(self) -> float:
        """已运行时长（秒）。"""
        return time.monotonic() - self.started_at

    @property
    def memory_owner(self) -> str:
        """本轮运行长期记忆的归属主体。

        WHY 直接复用 ``owner_id``：记忆是「这个用户的偏好」，与会话归属同源，
        另存一份必然出现两者漂移。认证关闭时 ``owner_id`` 为空串，这里统一
        落到匿名标识——否则空串会被当成一个独立命名空间，让同一台机器上
        「CLI 写的记忆 Web 读不到」。
        """
        return self.owner_id or ANONYMOUS_USER_ID

    def request_stop(self, reason: str = STOP_REASON_STOPPED) -> None:
        """请求停止本次运行；重复调用时首次的原因生效。

        WHY 保留首次原因：超时强制取消之后用户再点停止（或反过来），
        先到达的那个才是运行的真实终止原因；覆盖它会让审计与前端
        「已超时」的结论被后来的操作改写。

        WHY 用 ``object.__setattr__`` 而不是把整个句柄改成可变：句柄的
        ``thread_id`` / ``started_at`` 一旦可写，运行登记就失去了可信度；
        这里只为「一次性记录原因」破一个口子，比整体降级为可变更安全。

        Args:
            reason: 停止原因；取 ``STOP_REASON_STOPPED`` 或
                ``STOP_REASON_TIMEOUT``。

        Raises:
            ValueError: ``reason`` 不是非空字符串。
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空字符串")
        if self.stop_reason is None:
            object.__setattr__(self, "stop_reason", reason.strip())
        self.cancel_event.set()


@dataclass(eq=False)
class ToolCallRecord:
    """一次工具调用的审计草稿。

    WHY 单独成类：工具调用的开始（TOOL_CALL）与结束（TOOL_RESULT）是两个
    不同的事件，耗时只有把它们配对后才能算出来；用一个记录对象承载这对
    状态，比在两个字典里分别记时间戳更容易保证不漏、不串。
    """

    name: str
    started_at: float
    args_preview: str = ""
    status: str = ""
    """工具结果的 status；空串表示运行结束前都未收到结果。"""
    elapsed_ms: int | None = None


class RunRegistry:
    """运行槽位、累计计数与人工审批挂起登记的唯一所有者。

    锁的纪律：所有状态读写都在 ``_guard`` 内完成，且临界区内**不 await**。
    这不仅是习惯——释放动作必须能在生成器的 ``finally`` 里同步完成，而客户端
    断开连接时那里正处于 ``GeneratorExit``，任何 await（包括 ``asyncio.Lock``
    的获取）都会破坏生成器的关闭流程。
    """

    def __init__(self, config: AppConfig) -> None:
        """构造登记表。

        Args:
            config: 应用配置，提供并发上限、限流窗口与重试建议时长。

        Raises:
            ValueError: ``config`` 为 ``None``。
        """
        if config is None:
            raise ValueError("config 不能为 None")

        self._config = config

        # WHY 用 threading.Lock 保护「运行中」登记表：加解锁之间不 await，
        # 临界区极短；更重要的是释放动作必须能在 finally 里同步完成（见类文档）。
        self._guard = threading.Lock()
        self._running: dict[str, RunHandle] = {}

        # WHY 累计运行数与 HITL 挂起登记与运行登记表共用一把锁：三者都是
        # 「本次运行的即时状态」，若各自加锁，指标采集会读到互相矛盾的组合
        # （例如累计运行数已加一，但槽位尚未登记）。
        self._started_runs = 0
        self._hitl_pending: dict[str, float] = {}
        """会话 ID → 挂起登记时刻（``time.monotonic``），用于 TTL 判定。"""
        self._hitl_expired: dict[str, float] = {}
        """会话 ID → 被判定超期的时刻；用于拒绝过期审批与指标展示。

        WHY 与 ``_hitl_pending`` 分开存：过期是「曾经挂起且已作废」的历史事实，
        而挂起是当下状态。合成一个字典就要用哨兵值区分二者，届时每个读取点
        都要记得判断哨兵——漏一处就会出现「已过期的审批被放行」。
        """
        self._timed_out_runs = 0
        """进程启动以来被运行超时强制取消的运行数。"""
        self._expired_hitl = 0
        """进程启动以来被判定超期作废的审批挂起数。"""
        self._rejected_runs = 0
        """进程启动以来因超出并发上限或被限流而拒绝的运行数。"""

        # WHY 限流器由登记表自己持有而不是放进装配层：它的键是「发起本轮的主体」，
        # 而主体（owner_id）是运行期才算出来的——放进装配层就要把同一份配置再读一遍，
        # 两处配置迟早分叉，表现为「改了配置但限流阈值没变」。
        self._run_limiter = RateLimiter(
            window_seconds=config.run_rate_limit_window_seconds,
            max_attempts=config.run_rate_limit_max_attempts,
        )

        logger.info(
            "RunRegistry 就绪：并发上限=%s 限流=%s/%ss",
            config.max_concurrent_runs,
            config.run_rate_limit_max_attempts,
            config.run_rate_limit_window_seconds,
        )

    # ------------------------------------------------------------------ 准入

    def at_capacity_locked(self) -> bool:
        """并发是否已达上限。

        WHY 要求调用方已持锁：上限判定与随后的槽位占用必须是同一个原子动作，
        否则两个请求可以同时看到「还剩一个空位」然后一起挤进来。
        """
        cap = self._config.max_concurrent_runs
        return cap > 0 and len(self._running) >= cap

    def check_limits(self, owner_key: str) -> None:
        """在真正触碰会话之前判定并发与限流。

        WHY 必须排在所有权校验之前：若排在之后，被限流的调用方可以从「404 还是
        429」推断出某个会话在不在——限流不该成为一把探测他人会话的尺子。权限校验
        仍在最前面，未授权的调用方连这一层都到不了。

        WHY 并发数直接数运行登记表而不另设计数器：既有语义里「删除 / 归档会话不会
        中断正在进行的运行」，运行因此可能比会话本身活得更久；另立的计数迟早与登记表
        漂移，而漂移的方向恰恰是「指标说还有空位，实际已经排不动」。

        Args:
            owner_key: 发起本轮的主体标识；空串表示认证关闭，落到匿名主体。

        Raises:
            RunRejectedError: 超出并发上限或被限流。
        """
        retry_after = self._config.run_rejected_retry_after_seconds
        # WHY 认证关闭时落到匿名主体：单用户场景下所有请求本就属于同一个人，
        # 按空串计数会让「限流」在该场景下等于关闭。
        key = owner_key or ANONYMOUS_USER_ID

        if not self._run_limiter.is_allowed(key):
            with self._guard:
                self._rejected_runs += 1
            logger.warning("运行被限流：owner=%s", key)
            raise RunRejectedError(REASON_RATE, retry_after)

        with self._guard:
            if self.at_capacity_locked():
                self._rejected_runs += 1
                logger.warning("运行被拒：并发已达上限 %s", self._config.max_concurrent_runs)
                raise RunRejectedError(REASON_CONCURRENCY, retry_after)

    def acquire(
        self,
        thread_id: str,
        *,
        model_name: str | None = None,
        owner_id: str = "",
        actor_id: str = "",
        fork_checkpoint: str = "",
        workspace: str = "",
    ) -> RunHandle:
        """占用该会话的运行槽位并登记运行句柄。

        WHY 必须互斥：同一会话并发发起两轮会让图状态产生竞争——两轮各自读写
        同一 thread 的检查点，后写的一方会覆盖先写一方的中间结果，表现为消息
        丢失或工具结果错配。

        WHY 把模型别名与所有者一起登记进句柄：用量在流结束时才落库，那一刻
        已经拿不到本轮的参数；挂在句柄上才能「谁的模型、谁的用量」对齐。

        Args:
            thread_id: 已规范化的会话 ID。
            model_name: 本轮使用的模型别名；``None`` 表示默认模型。
            owner_id: 会话所有者；认证关闭时为空串。
            actor_id: 发起本轮运行的主体标识，用于工具审计归因。
            fork_checkpoint: 分叉起点检查点 id；空串表示接着当前分支的头。
            workspace: 本轮运行的工作区绝对路径；空串表示未声明。

        Returns:
            本次运行的句柄；停止请求与运行指标都通过它传递。

        Raises:
            ValueError: ``thread_id`` 不是非空字符串。
            ThreadBusyError: 该会话已有运行中的轮次。
            RunRejectedError: 并发上限在等待期间已被占满。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")

        with self._guard:
            if thread_id in self._running:
                raise ThreadBusyError(thread_id)

            # WHY 在这里复查一次上限：``check_limits`` 与本次占用之间隔着所有权
            # 校验等 await，足够另一轮把最后一个槽位占走。检查与占用同在锁内才是原子的；
            # 而这里被拒的只可能是「自己有权限的会话」，不存在借状态码探测他人的问题。
            if self.at_capacity_locked():
                self._rejected_runs += 1
                raise RunRejectedError(
                    REASON_CONCURRENCY, self._config.run_rejected_retry_after_seconds
                )

            handle = RunHandle(
                thread_id=thread_id,
                started_at=time.monotonic(),
                cancel_event=asyncio.Event(),
                model_name=model_name,
                owner_id=owner_id,
                actor_id=actor_id,
                fork_checkpoint=fork_checkpoint,
                workspace=workspace,
            )
            self._running[thread_id] = handle
            # 累计运行数在此累加：这里是「一轮运行真正开始」的唯一入口，
            # 放在 stream / resume 里会漏掉其中一条路径。
            self._started_runs += 1
        return handle

    def release(self, thread_id: str) -> None:
        """释放该会话的运行槽位；本就没有占位时是 no-op。

        WHY 对外公开：生成器只会在被消费时通过 ``finally`` 释放槽位。若调用方
        拿到生成器后因异常未能消费（例如构造响应体时出错），槽位就再也没人释放，
        该会话会被永久判定为「运行中」。公开此方法让调用方能在这种情况下归还。

        Args:
            thread_id: 会话 ID。
        """
        with self._guard:
            self._running.pop(thread_id, None)

    # ------------------------------------------------------------------ 运行查询

    def handle(self, thread_id: str) -> RunHandle | None:
        """返回指定会话的运行句柄；未在运行时为 ``None``。

        WHY 公开：运行指标（``/metrics``）与运行超时治理需要读同一份登记，
        各自维护一套集合会出现口径不一致。
        """
        with self._guard:
            return self._running.get(thread_id)

    def is_running(self, thread_id: str) -> bool:
        """该会话当前是否有运行中的轮次。"""
        with self._guard:
            return thread_id in self._running

    def handles(self) -> dict[str, RunHandle]:
        """当前全部运行句柄的快照（会话 ID → 句柄）。

        WHY 需要整表快照而不是逐个查 ``handle``：运行治理要在同一时刻
        判断「哪些运行超时」，逐个查询会让每个判断落在不同时刻，从而把扫描
        期间才启动的运行也算进本轮结论里。
        """
        with self._guard:
            return dict(self._running)

    def running_ids(self) -> tuple[str, ...]:
        """当前运行中的会话 ID 快照（供指标暴露）。"""
        with self._guard:
            return tuple(self._running)

    def record_timeout(self) -> None:
        """累计一次「被运行超时强制取消」。

        WHY 不由治理协程直接改计数：计数与运行登记表共用一把锁（见类文档），
        只有经由本方法自增，才不会出现「加了计数但没置位停止信号」的半截状态。
        """
        with self._guard:
            self._timed_out_runs += 1

    @property
    def started_runs(self) -> int:
        """进程启动以来累计发起的运行次数。

        WHY 做成指标：单看「运行中」只能知道当下忙不忙，累计值才能回答
        「这台实例跑过多少轮」，是容量规划与异常检测的最小数据集。
        """
        with self._guard:
            return self._started_runs

    @property
    def timed_out_runs(self) -> int:
        """进程启动以来被运行超时强制取消的运行次数。"""
        with self._guard:
            return self._timed_out_runs

    @property
    def expired_hitl(self) -> int:
        """进程启动以来因超期未决策而作废的审批挂起次数。"""
        with self._guard:
            return self._expired_hitl

    @property
    def rejected_runs(self) -> int:
        """进程启动以来因并发上限或限流被拒绝的运行次数。

        WHY 与运行登记表共用一把锁：拒绝计数要在「判定超限」的同一临界区内自增，
        否则指标会读到「已满但拒绝数为 0」这种自相矛盾的组合。
        """
        with self._guard:
            return self._rejected_runs

    @property
    def max_concurrent_runs(self) -> int:
        """配置的全局并发上限；``0`` 表示不限制。"""
        return self._config.max_concurrent_runs

    @property
    def available_slots(self) -> int:
        """当前可用槽位数；上限为 ``0``（不限制）时返回 ``-1``。

        WHY 不限制时用 ``-1`` 而不是 ``0``：``0`` 在容量语境里天然读作「一个空位都
        没有」，而这里恰恰相反。让「不限制」有一个不可能与「已满」混淆的取值，
        看指标的人就不必再回去查配置。
        """
        if self._config.max_concurrent_runs <= 0:
            return -1
        with self._guard:
            return max(0, self._config.max_concurrent_runs - len(self._running))

    # ------------------------------------------------------------------ 审批挂起

    def mark_hitl_pending(self, thread_id: str) -> None:
        """登记该会话有一个等待人工审批的中断，并记录挂起起始时刻。

        WHY 由登记表持有而不是让指标端点去遍历图状态：遍历需要对每个会话
        调一次 ``aget_state``，成本随会话数线性增长；而中断事件在事件流里
        已经出现过一次，登记是零成本的。

        WHY 重复登记不刷新起始时刻：同一轮运行里中断事件可能出现多次
        （多个待审批工具），若每次都重置，TTL 就永远走不完；以首次登记
        为准才能让「挂起太久」这个判断成立。

        Args:
            thread_id: 已规范化的会话 ID。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        with self._guard:
            # 新一轮中断作废上一次的过期标记：既然又等上了，说明用户确实
            # 在跟这个会话交互，此前的过期结论不再适用。
            self._hitl_expired.pop(thread_id, None)
            if thread_id not in self._hitl_pending:
                self._hitl_pending[thread_id] = time.monotonic()

    def clear_hitl_pending(self, thread_id: str) -> None:
        """清除该会话的待审批登记与过期标记；本就没有挂起时是 no-op。

        Args:
            thread_id: 会话 ID。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        with self._guard:
            self._hitl_pending.pop(thread_id, None)
            self._hitl_expired.pop(thread_id, None)

    def expire_hitl_pending(self, thread_id: str) -> bool:
        """把该会话的挂起审批标记为过期并释放占位。

        Args:
            thread_id: 会话 ID。

        Returns:
            是否真的作废了一次挂起；``False`` 表示该会话此刻没有挂起
            （已被用户应答或已被并发清理过），调用方据此跳过后续处理。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        with self._guard:
            started_at = self._hitl_pending.pop(thread_id, None)
            if started_at is None:
                return False
            self._hitl_expired[thread_id] = time.monotonic()
            self._expired_hitl += 1
        logger.info(
            "会话 %s 的审批挂起已超期作废：等待 %.1f 秒",
            thread_id,
            time.monotonic() - started_at,
        )
        return True

    def is_hitl_expired(self, thread_id: str) -> bool:
        """该会话是否有一个已作废（超期）的审批挂起。

        Raises:
            ValueError: ``thread_id`` 非法。
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id 必须是非空字符串")
        with self._guard:
            return thread_id in self._hitl_expired

    def hitl_pending_age(self, thread_id: str) -> float | None:
        """该会话的审批已挂起秒数；未挂起时为 ``None``。"""
        with self._guard:
            started_at = self._hitl_pending.get(thread_id)
        return None if started_at is None else time.monotonic() - started_at

    def pending_hitl_ids(self) -> tuple[str, ...]:
        """当前等待人工审批的会话 ID 快照。

        WHY 需要这份登记：HITL 挂起的运行既不占运行槽位（图已经暂停），
        也不是错误，只有单独记录才能被指标与后续的挂起 TTL 治理看到。
        """
        with self._guard:
            return tuple(self._hitl_pending)
