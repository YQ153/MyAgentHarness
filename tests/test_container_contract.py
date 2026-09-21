"""容器化交付的静态契约。

WHY 用静态断言而不是「构建一次镜像」：跑 Docker 不是每台机器、每次 CI 都有的能力，
而这些承诺（非 root、探活打哪、端口只绑回环、密钥不进镜像层）全部写在文本里，
静态断言恰好能钉住它们，且在任意环境都能跑。

WHY 值得钉：这几条一旦被改坏，症状都不会立刻出现在测试里——以 root 运行、探活改成
查依赖、端口默认暴露到局域网、`.env` 被打进镜像层，都是「部署之后才发现」的类型。
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


def test_compose_persists_state_in_one_volume() -> None:
    compose = _read("docker-compose.yml")

    assert "/app/.data" in compose
    # WHY 断的是「会话专属目录也在那个卷里」：不绑定工作空间的会话，其文件根落在
    # SESSIONS_ROOT 下——它必须在可写卷里，否则容器重建一次，那些会话的产物就没了，
    # 而用户不会收到任何提示。
    assert "/app/.data/sessions" in compose


def test_compose_binds_to_loopback_by_default() -> None:
    compose = _read("docker-compose.yml")

    # 默认不暴露到局域网：要暴露时应当是一处刻意的改动，而不是默认行为
    assert "127.0.0.1:8000:8000" in compose


def test_identity_provider_does_not_mount_the_docker_socket() -> None:
    compose = _read("docker-compose.yml")

    # WHY 这条要在测试里钉死：挂 docker.sock 进容器等价于把宿主的 root 交出去。
    # 本项目目前不需要它；一旦有人为了「在容器里跑 docker」把它挂上，这条会立刻失败。
    #
    # WHY 断言「挂载形式」而不是子串：编排里**讨论**这件事的注释是正当且必要的，
    # 而 `"docker.sock" not in compose` 会把那句注释判成违规——一条会把正确行为判失败的
    # 断言比没有断言更糟。带冒号的容器侧路径只在真正的 volume 映射里出现。
    assert ":/var/run/docker.sock" not in compose

