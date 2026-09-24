"""配置层的档位枚举：执行档位、沙箱隔离档位、MCP 传输方式与嵌入后端档位。

WHY 独立成模块：枚举同时被 ``settings.*``（字段类型）、``app_config``
（加载日志）与包外的 ``runtime`` / ``agent`` 引用（经门面导入）。它们不
依赖任何配置字段，放在包内最底层即可让依赖保持单向。
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionMode(StrEnum):
    """``execute`` 工具的执行档位。

    local:    宿主机直跑 shell，仅限本机开发，与 ``LocalShellBackend`` 的安全
              警告一致——绝不可用于 Web 或多租户环境。
    sandbox:  沙箱内执行 shell。隔离强度由 ``SandboxTier`` 决定，Tier 0 / 1 / 2
              （进程 / WSL / 容器）均已实现；都不是安全边界，须与人工审批配合。
    disabled: 使用非沙盒后端，``execute`` 工具仍存在但调用后返回错误；
              这是默认档位，保证进程上线即处于安全状态。
    """

    LOCAL = "local"
    SANDBOX = "sandbox"
    DISABLED = "disabled"


class SandboxTier(StrEnum):
    """``sandbox`` 档位下的隔离实现档位。

    auto:    按隔离强度从高到低探测，选中首个可用的档位（wsl → process）。
    process: Tier 0——宿主机进程沙箱。Windows 用 Job Object 做进程树管控
             与资源上限，POSIX 用进程组做超时终止。**不是安全边界**，
             必须与 HITL 人工审批配合。
    wsl:     Tier 1——在 WSL2 发行版内执行，与宿主之间隔着 utility VM 边界
             与独立的 Linux 权限模型，资源上限由 Linux rlimit 施加。
             发行版是持久环境（不是一次性容器），且通过 ``/mnt`` 仍能读写
             宿主文件，因此同样需要与人工审批配合。
    docker:  Tier 2——容器内执行。**这是第一个真正限制命令可见面的档位**：
             默认只见镜像内容与显式挂载的工作区，宿主其余路径在容器内不存在；
             网络默认切断（实测 ``--network none`` 下无法解析域名），进程树随
             容器消失。仍**不宣称强隔离**——容器共享宿主内核，内核漏洞逃逸与
             侧信道不在防护范围内。必须与人工审批配合（见 ``agent/guardrails.py``）。
    """

    AUTO = "auto"
    PROCESS = "process"
    WSL = "wsl"
    DOCKER = "docker"


class MCPTransport(StrEnum):
    """MCP 服务器的传输方式。

    取值与 ``langchain-mcp-adapters`` 的 ``Connection`` 字面量一一对应，
    HTTP 侧用 ``streamable_http`` 而非 ``http``——后者是该库早期版本的别名，
    写成 ``http`` 会在建连阶段才报错，而配置应在加载期就拦下。
    """

    STDIO = "stdio"
    SSE = "sse"
    HTTP = "streamable_http"
    WEBSOCKET = "websocket"


class EmbeddingBackendKind(StrEnum):
    """知识库的嵌入后端档位。

    none:          不装配后端。知识库仍可用，但退化为关键词检索——**这是一等
                   档位，不是故障**，因此检索路径要按「有没有嵌入」分支，而不是
                   把「没有」当成异常。
    openai-compat: 调用任意兼容 ``POST /v1/embeddings`` 的服务（OpenAI、TEI、
                   Ollama、各家云厂商）。**容器内加载模型走这一档**——应用不做
                   容器编排（那属 T24），只管往一个地址发请求。
    subprocess:    本机自托管。在独立 venv 内拉起瘦服务进程，按 stdio 通信；
                   主进程不引入 onnxruntime，模型可在空闲时整个回收。
    """

    NONE = "none"
    OPENAI_COMPAT = "openai-compat"
    SUBPROCESS = "subprocess"
