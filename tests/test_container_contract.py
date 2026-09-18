"""容器化交付的静态契约。

WHY 用静态断言而不是「构建一次镜像」：跑 Docker 不是每台机器、每次 CI 都有的能力，
而这些承诺（非 root、探活打哪、默认开不开认证、密钥不进镜像层）全部写在文本里，
静态断言恰好能钉住它们，且在任意环境都能跑。

WHY 值得钉：这几条一旦被改坏，症状都不会立刻出现在测试里——以 root 运行、探活改成
查依赖、认证退回 disabled、`.env` 被打进镜像层，都是「部署之后才发现」的类型。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_image_runs_as_non_root() -> None:
    dockerfile = _read("Dockerfile")

    assert "USER app" in dockerfile, "运行阶段必须切到非 root 用户"
    # 非 root 的前提是那个用户真的被建出来，且属主交给了它
    assert "useradd" in dockerfile
    assert "--chown=app:app" in dockerfile


def test_healthcheck_probes_liveness_not_readiness() -> None:
    dockerfile = _read("Dockerfile")

    assert "HEALTHCHECK" in dockerfile
    # WHY 必须是 /health：/ready 会查数据库，依赖抖动会让编排系统反复重启容器，
    # 把「依赖故障」放大成「服务不可用」——这正是应用里两者分开的理由。
    healthcheck = dockerfile.split("HEALTHCHECK", 1)[1].split("\n\n", 1)[0]
    assert "/health" in healthcheck
    assert "/ready" not in healthcheck


def test_secrets_are_excluded_from_build_context() -> None:
    dockerignore = _read(".dockerignore")

    # 打进镜像层的东西会被任何拿到镜像的人解开，且删掉源文件也删不掉历史层
    assert ".env" in dockerignore


def test_compose_keeps_authentication_on_and_persists_state() -> None:
    compose = _read("docker-compose.yml")

    # WHY 必须显式覆盖：容器会把端口映射到宿主机，网络边界比本机进程宽得多，
    # 跟随本机开发配置（disabled）等于把 Agent 交给任何能连上该端口的人
    assert "AUTH_MODE: apikey" in compose
    assert "/app/.data" in compose
    assert "/app/workspace" in compose


def test_compose_binds_to_loopback_by_default() -> None:
    compose = _read("docker-compose.yml")

    # 默认不暴露到局域网：要暴露时应当是一处刻意的改动，而不是默认行为
    assert "127.0.0.1:8000:8000" in compose
