# 多阶段构建：构建阶段装依赖，运行阶段只带结果。
# WHY 分阶段：编译器、uv 缓存与构建期临时文件都不该出现在运行镜像里——它们既增大体积，
# 也扩大攻击面（一个装着完整工具链的镜像，被拿下后能就地编译下一段载荷）。
# 用法见 README「容器化运行」；编排在 docker-compose.yml。

# ---------------------------------------------------------------- 构建阶段
FROM python:3.14-slim AS builder

# uv 版本与开发机一致（0.9.17）：镜像里的解析结果必须与 uv.lock 的生成环境同源，
# 版本漂移会让「按锁文件精确复现」这件事在容器里悄悄失效。
COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
# WHY UV_LINK_MODE=copy：uv 默认把依赖从缓存硬链接进虚拟环境，而缓存不会随镜像带走，
# 于是留下满目录指向不存在文件的链接——构建阶段一切正常，运行阶段表现为「模块找不到」。

WORKDIR /app

# 先只拷依赖清单：源码改动不会让这一层失效，改一次代码不必重装一次依赖。
# --frozen 用 uv.lock 原样安装（不重新解析），--no-dev 不把 pytest 等带进运行镜像。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 本项目没有构建后端（pyproject 里无 [build-system]），uv 视其为虚拟项目：依赖装完即可，
# 不需要再「安装本项目」，因此这里不再重复 sync。
COPY . .

# ---------------------------------------------------------------- 运行阶段
FROM python:3.14-slim AS runtime

# 非 root 且不可登录：容器里跑的是能执行 shell 工具的 Agent，以 root 运行会让一次越权
# 直接等于宿主上的 root。组与用户都用固定高位 uid/gid，便于宿主机侧核对卷属主。
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /app /app

# WHY 先建目录并把属主交给卷：命名卷首次挂载会**继承镜像里该目录的属主**。
# 不建的话它们属于 root，非 root 进程写 `.data`（SQLite、会话专属目录）会当场失败。
RUN mkdir -p /app/.data \
    && chown -R app:app /app/.data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
# WHY PYTHONUNBUFFERED：不加则 stdout 被缓冲，docker logs 在进程被强杀前什么都看不到——
# 而排障最需要日志的，恰恰是进程没来得及正常退出的那一刻。

USER app

EXPOSE 8000

# 存活探针打 /health，不打 /ready：/ready 会查数据库，一次依赖抖动会让编排系统把容器
# 反复重启，把「依赖故障」放大成「服务不可用」。这与应用自身的存活/就绪划分一致
# （interfaces/web/health.py 的 docstring 写明了这条理由）。
# WHY 用 python 而不是 curl：slim 镜像里没有 curl，为一条探针再装一个包不划算。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# 必须监听 0.0.0.0：容器里的 127.0.0.1 只对容器自身可见，端口映射会连不上，
# 表现为「容器起来了但宿主机访问被拒」。
CMD ["python", "main.py", "web", "--host", "0.0.0.0", "--port", "8000"]
