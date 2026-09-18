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

    # WHY 默认必须是 apikey：容器会把端口映射到宿主机，网络边界比本机进程宽得多，
    # 跟随本机开发配置（disabled）等于把 Agent 交给任何能连上该端口的人。
    # 注意断言的是「带默认值的可覆盖形式」——改回硬编码 apikey 会让 OIDC 档位无法切换。
    assert "${AUTH_MODE:-apikey}" in compose
    assert "/app/.data" in compose
    assert "/app/workspace" in compose


def test_compose_binds_to_loopback_by_default() -> None:
    compose = _read("docker-compose.yml")

    # 默认不暴露到局域网：要暴露时应当是一处刻意的改动，而不是默认行为
    assert "127.0.0.1:8000:8000" in compose


def test_identity_provider_is_behind_a_profile() -> None:
    compose = _read("docker-compose.yml")

    # WHY：IdP 那一套近 2 GB。默认 up 只应起 agent，多起一个 IdP 是刻意的选择
    for service in ("postgresql:", "server:", "worker:"):
        assert service in compose
    assert compose.count("profiles: [oidc]") >= 3, "IdP 的三个服务都应挂在 oidc 档位下"


def test_identity_provider_does_not_mount_the_docker_socket() -> None:
    compose = _read("docker-compose.yml")

    # WHY 这条要在测试里钉死：挂 docker.sock 进容器等价于把宿主的 root 交出去，
    # 而上游示例编排确实带着它（只为 outpost 服务，本部署用不到）。
    # 这种「照抄示例」引入的提权面不报错、不告警，只能靠断言挡住。
    #
    # WHY 断言「挂载形式」而不是子串：编排里**讨论**这件事的注释是正当且必要的，
    # 而 `"docker.sock" not in compose` 会把那句注释判成违规——一条会把正确行为判失败的
    # 断言比没有断言更糟。带冒号的容器侧路径只在真正的 volume 映射里出现。
    assert ":/var/run/docker.sock" not in compose


def test_auth_mode_is_overridable_and_defaults_to_apikey() -> None:
    compose = _read("docker-compose.yml")

    # 默认仍是 apikey（容器化不裸奔）；切 OIDC 必须由 .env 显式决定，而不是改编排文件
    assert "${AUTH_MODE:-apikey}" in compose

