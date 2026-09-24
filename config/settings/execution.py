"""执行与沙箱域配置：``execute`` 的档位选择与沙箱资源/隔离参数。

字段从原 ``config.AppConfig`` 的「执行与安全」「沙箱（仅 sandbox 档位生效）」
两个分区（原 L677–771）整体迁入。
"""

from typing import Annotated

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import NoDecode

from config.constants import DEFAULT_ENV_ALLOWLIST, NETWORK_MODE_NONE
from config.enums import ExecutionMode, SandboxTier
from config.parsing import parse_list_config


class ExecutionSettings(BaseModel):
    """执行与沙箱域的字段：档位、超时、资源上限与环境变量白名单。"""

    # ---------------- 执行与安全 ----------------
    execution_mode: ExecutionMode = ExecutionMode.DISABLED
    shell_timeout: int = Field(default=120, gt=0)
    shell_max_output_bytes: int = Field(default=100_000, gt=0)

    # ---------------- 沙箱（仅 sandbox 档位生效） ----------------
    sandbox_tier: SandboxTier = SandboxTier.AUTO
    """隔离档位；``auto`` 会落到当前已实现的最高档位。"""

    sandbox_timeout: int = Field(default=120, gt=0)
    """单条命令的超时秒数。

    WHY 独立于 ``shell_timeout``：``shell_timeout`` 是 ``local`` 档位的口径，
    沙箱档位需要独立的资源与超时策略，二者混用会导致调一个影响另一个。
    """

    sandbox_max_output_bytes: int = Field(default=100_000, gt=0)
    """stdout / stderr 各自的截断阈值。"""

    sandbox_max_processes: int = Field(default=64, ge=1)
    """活动进程数上限。

    WHY 必须有：LLM 生成或复制来的命令里出现 fork bomb 的概率不高，但
    一旦出现，宿主机在几秒内失去响应，且只能靠重启恢复——上限是唯一防线。
    """

    sandbox_max_memory_mb: int = Field(default=2048, ge=64)
    """单个进程的内存上限（MB）。"""

    sandbox_cpu_percent: int = Field(default=50, ge=1, le=100)
    """CPU 占用硬上限百分比（Windows Job Object 生效）。"""

    sandbox_env_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ENV_ALLOWLIST)
    )
    """允许传入子进程的环境变量白名单。

    WHY 白名单：宿主机环境常含 ``*_API_KEY``、云凭证、``USERPROFILE``，
    黑名单补不全；命令真正需要的变量只有固定的少数几个。

    环境变量支持两种写法：逗号分隔（``SANDBOX_ENV_ALLOWLIST=PATH,TEMP``）
    或 JSON 数组。大小写无需在意——``SandboxPolicy.sanitized_env`` 两侧都按
    ``upper()`` 比较（Windows 环境变量名本身大小写不敏感）。
    """

    sandbox_network_mode: str = Field(default=NETWORK_MODE_NONE, pattern="^(none|host)$")
    """网络模式。

    ``none`` 仅表示不向子进程传递代理类变量；Windows 上进程级网络阻断需要
    管理员权限建防火墙规则，本期不做，残余风险由日志与文档显式标注。
    """

    sandbox_wsl_distro: str | None = None
    """WSL 档位使用的发行版名称；``None`` 时自动挑选首个满足要求的发行版。

    WHY 需要「显式指定」与「自动挑选」两条路：一台机器常有多个发行版，而
    默认发行版未必适合跑命令（例如 Docker Desktop 自带的精简发行版没有
    bash）；自动挑选会按 ``wsl --list --quiet`` 的顺序逐个探测，显式指定
    则能跳过这段冷启动开销。
    """

    sandbox_docker_image: str = "harness-sandbox:latest"
    """``docker`` 档位使用的执行镜像名。

    由 ``scripts/setup_sandbox_image.py`` 依据 ``docker/sandbox.Dockerfile`` 构建。
    WHY 默认指向自建镜像而不是 ``python:3.14-slim``：自建那份以非 root 身份运行、
    不预装任何额外工具；直接指向官方镜像会把「用哪个镜像」这件事的默认值交给一次
    依赖联网的拉取。
    """

    sandbox_docker_workspace_read_only: bool = Field(default=False)
    """是否把工作区以只读方式挂载进容器。

    WHY 默认读写：``execute`` 的主要用途就是跑脚本与构建，产物就落在工作区里，
    只读会让这个档位不可用。需要更严边界的部署可以打开它——那时容器内的命令改不了
    工作区，而 Agent 自己的文件工具（走宿主侧）不受影响。
    """

    sandbox_docker_user: str = ""
    """传给 ``--user`` 的值（形如 ``1000:1000``）；空串表示用镜像默认身份。

    WHY 需要它：容器内以 root 写出文件时，在 **Linux 宿主**上文件属主是 root，之后
    宿主进程可能改不动挂载目录；把这里设成宿主的 ``uid:gid`` 即可避免。Windows 宿主
    由 Docker Desktop 代为处理属主，留空即可（实测容器内写入回到宿主可正常读写）。
    """

    sandbox_require_approval: bool = True
    """沙箱档位下是否仍需人工审批。

    WHY 默认开启：Tier 0 不是安全边界，防不住本地提权与凭据嗅探；审批是
    本档位真正的主防线，关闭它等于只剩资源管控。
    WHY 在 ``docker`` 档位上**同样**默认开启：隔离缩小的是「命令碰到了会怎样」，
    不是「这条命令该不该跑」——一条 ``rm -rf /work`` 在容器里照样能删掉挂载进来的
    工作区。审批与隔离回答的是两个不同问题，不可互相替代（见 T24 的定稿记录）。
    """

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        """容错大小写与空白，避免 ``LOCAL`` / `` local `` 被当成非法值。"""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("sandbox_tier", mode="before")
    @classmethod
    def _normalize_tier(cls, value: object) -> object:
        """容错大小写与空白，与 ``execution_mode`` 保持同一口径。"""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("sandbox_env_allowlist", mode="before")
    @classmethod
    def _parse_env_allowlist(cls, value: object) -> object:
        """按逗号切分环境变量白名单，口径见 ``parse_list_config``。"""
        return parse_list_config(value, field="sandbox_env_allowlist", separators=(",",))

    @field_validator("sandbox_wsl_distro", mode="before")
    @classmethod
    def _blank_distro_to_none(cls, value: object) -> object:
        """把空串归一为 ``None``。

        WHY：``SANDBOX_WSL_DISTRO=""`` 是 shell 里「清空变量」的常见写法，
        若当成一个发行版名去探测，用户只会看到一条与真实意图无关的报错。
        """
        if isinstance(value, str):
            return value.strip() or None
        return value
