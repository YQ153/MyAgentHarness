# docker/

容器相关定义的唯一位置：两份 Dockerfile 与一份 compose 编排都在这里。

| 文件 | 产物 | 受众 |
| --- | --- | --- |
| `app.Dockerfile` | `myagentharness:local` | 应用自身：装依赖、跑 Web 服务、非 root、暴露 8000 |
| `sandbox.Dockerfile` | `harness-sandbox:latest` | Tier 2（docker 档位）的命令执行环境，越空越好 |
| `compose.yml` | 单机编排 | 把上面那份应用镜像跑起来（端口只绑回环） |

## 常用命令

```bash
# 应用：启动 / 查看 / 停止（路径相对仓库根；compose 内部路径都相对本文件解析，
# 因此在任何工作目录下执行都等价）
docker compose -f docker/compose.yml up -d --build
docker compose -f docker/compose.yml ps
docker compose -f docker/compose.yml down        # 保留卷；加 -v 连数据一起删

# 应用镜像单独构建：上下文必须是仓库根（末尾的 . ）
docker build -f docker/app.Dockerfile -t myagentharness:local .

# 执行镜像：由脚本构建（含 --base 换源开关），不要手敲 docker build
python scripts/setup_sandbox_image.py
```

## 两条容易踩的约定

**构建上下文是仓库根，不是 `docker/`。** `app.Dockerfile` 要 `COPY` 整棵源码树与
`pyproject.toml` / `uv.lock`；而 `.dockerignore` 只对上下文根目录生效——把上下文收进
`docker/` 会同时把源码挡在门外。因此 `.dockerignore` 留在仓库根，是 docker 的硬性要求，
不是遗漏。

**项目名被钉成 `myagentharness`。** compose 默认取 compose 文件所在目录名当项目名，本文件
从仓库根搬进 `docker/` 后，默认值会从 `myagentharness` 变成 `docker`，卷名随之变成
`docker_agent-data`——已有部署会表现为「数据不见了」（其实是新建了空卷）。`compose.yml`
顶层的 `name:` 就是为此而写，不要删；`tests/test_container_contract.py` 里有一条断言钉着它。
