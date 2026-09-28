"""运行治理域配置：护栏阈值、运行超时、审计保留、用量窗口、并发限流与 HTTP 服务。

字段从原 ``config.AppConfig`` 的「护栏」「运行治理」「HTTP 服务」「审计保留」
「用量统计」「运行并发与限流」「日志形态」等分区（原 L773–795、L1012–1098）
整体迁入。它们共同的特点是「约束一次运行能占多少资源、留多久痕」，因此归为
一个治理域。
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class GovernanceSettings(BaseModel):
    """运行治理域的字段：护栏、超时、审计、用量、限流与 HTTP 服务参数。"""

    # ---------------- 护栏 ----------------
    max_model_calls_per_run: int = Field(default=60, gt=0)
    recursion_limit: int = Field(default=100, gt=0)

    # ---------------- 运行治理 ----------------
    run_max_seconds: int = Field(default=900, ge=0)
    """单轮运行的最长秒数；超时由后台巡检协程强制取消。

    WHY 必须有：模型调用卡在网络层、工具死循环、用户忘记点停止，都会让一轮
    运行无限期占用槽位——该会话此后无法再发起任何对话，只能重启进程。

    WHY 允许 ``0``：CLI 的一次性长任务与联调场景需要关闭它，而「不限制」是
    一个显式选择，不应靠把阈值调到极大来变相实现。
    """

    hitl_pending_ttl_seconds: int = Field(default=1800, ge=0)
    """人工审批挂起的最长等待秒数；超期未决策的中断被标记过期。

    WHY 必须有：审批卡一旦无人处理就永久挂起——它既不占运行槽位（图已暂停），
    也不算错误，只有 TTL 能让「等待审批数」这个指标重新可信。

    WHY 允许 ``0``：与 ``run_max_seconds`` 同理，仅用于本地联调。
    """

    run_governance_interval_seconds: int = Field(default=30, ge=1)
    """运行治理协程的巡检间隔（秒）。

    WHY 与两个阈值分开配置：阈值决定「判什么为超时 / 过期」，间隔决定「多久
    扫一次」；间隔即超时判定的最大误差，短间隔更精确但更频繁地取运行快照。
    """

    # ---------------- HTTP 服务 ----------------
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    # ---------------- 审计保留 ----------------
    audit_retention_days: int = Field(default=180, ge=1)
    """审计日志保留天数；超期的记录会被定期清理任务删除。

    WHY 必须有保留策略：审计表随每次运行、审批、登录单调增长，长期运行的
    部署里它会成为最大的一张表，而超过保留期的记录在合规上通常已无留存
    必要。做成配置而非常量，是因为不同部署的合规要求差异极大。
    """

    audit_retention_interval_seconds: int = Field(default=86400, ge=60)
    """保留清理任务的执行间隔（秒）。

    WHY 与保留天数分开配置：保留期决定「删什么」，间隔决定「多久扫一次」；
    小部署希望每天清一次，大表则可能需要更频繁地分批清理。
    """

    audit_archive_enabled: bool = True
    """清理超期审计事件前是否先导出归档文件。

    WHY 默认开启：保留期一到就删，等于把「过期」和「可丢弃」划了等号——
    合规审计经常需要回溯保留期之前的记录。关闭后行为退化为「只删不导出」。

    WHY 归档失败要拦住删除（fail-closed）：目录不可写时若照删不误，数据就是
    静默丢失且无从补救；宁可让审计表继续增长并打出 ERROR 日志，也不能丢记录。
    """

    audit_archive_dir: Path = Field(default=Path("./.data/audit-archive"))
    """超期审计事件的归档目录。

    归档文件按批次写成 JSONL，运维可将其搬到对象存储后自行清理本目录。
    """

    audit_archive_batch_size: int = Field(default=500, ge=1, le=5000)
    """单次归档批次的条数。

    WHY 分批而不是一次读完：超期记录可能有几十万条，一次性载入内存会让
    后台清理任务把进程内存顶上去；分批读取 + 分批落盘使峰值内存与批次
    大小成正比，而与超期总量无关。
    """

    # ---------------- 用量统计 ----------------
    usage_default_window_days: int = Field(default=7, ge=1, le=3650)
    """/api/usage 的默认统计窗口天数。

    WHY 做成配置：不同团队的结算周期不同（按天看成本、按月看预算），
    硬编码一个窗口会让多数组调用都要显式传参。
    """

    usage_max_window_days: int = Field(default=90, ge=1, le=3650)
    """/api/usage 允许查询的最大窗口天数。

    WHY 需要上限：窗口越大扫描的记录越多，无上限的接口可以被用来发起
    一次全表聚合，进而拖慢同一数据库上的会话读写。
    """

    # ---------------- 运行并发与限流 ----------------
    max_concurrent_runs: int = Field(default=4, ge=0)
    """全进程允许同时进行的大模型运行数；``0`` 表示不限制。

    WHY 默认给一个具体值而不是「不限制」：不限并发时，几条长任务就能同时吃掉上游
    配额与本地内存，而那种过载在指标上只表现为「运行数很高」——看着正忙，其实已经
    排不动了。单用户场景下 4 远高于实际并发，等于没有影响。
    """

    run_rate_limit_window_seconds: int = Field(default=60, ge=1)
    run_rate_limit_max_attempts: int = Field(default=30, ge=1)
    """单个主体在窗口内允许发起的运行数。

    WHY 键取 owner_id 而不是 IP：一个 NAT 出口后面可能坐着一整个团队，按 IP 计数
    会把同事的正常使用算成一个人的滥用；而限流要挡的是「某个账号在刷」。
    """

    run_rejected_retry_after_seconds: int = Field(default=5, ge=1)
    """被限流或超出并发上限时回给客户端的 Retry-After 秒数。"""

    # ---------------- 日志形态 ----------------
    log_format: Literal["json", "text"] = "text"
    """日志输出格式；``text`` 供本机阅读，``json`` 供采集系统解析。

    WHY 默认 ``text``：本机开发时肉眼读日志是最主要的用法，默认切成 JSON 会让
    每一次本地排障都先过一道格式转换。结构化是「上线时需要」的能力，不是默认形态。
    """
