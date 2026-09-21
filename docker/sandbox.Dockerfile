# Tier 2（docker 档位）的**执行镜像**——Agent 的 shell 命令跑在这里面。
#
# WHY 与根目录的 Dockerfile 分开：那份构建的是**应用自身**的镜像（装依赖、跑 Web 服务、
# 非 root、暴露端口）；这份是**命令的执行环境**。两者受众相反——应用镜像越完整越好，
# 执行镜像越空越好，因为一次成功的命令逃逸能碰到的东西，就等于这个镜像里有什么。
# 把两者合成一份，执行环境会自动获得应用的全部依赖与源码。
#
# 构建与用法见 README「Docker 沙箱」一节；一键准备：python scripts/setup_sandbox_image.py
#
# WHY 基础镜像选 python:3.14-slim：
# - 与根 Dockerfile 同源（应用与执行环境的基础层一致，排障时不必记两套发行版差异）；
# - 默认档位的主要用途是跑脚本与构建，Python 是其中的大头；
# - slim 已含 coreutils 与 /bin/sh，够跑常规命令，且不预装编译器（装不了就没法就地编译载荷）。
#
# WHY 基础镜像做成 ARG：Docker Hub 在部分网络下不可达（本机实测如此），需要能换成内网
# 镜像源或本机已有的等价镜像。若只写死 FROM，setup 脚本的 --base 就只能传给一个不存在的
# 构建参数——docker 对未声明的 build-arg 只给一行告警，于是这个开关会**静默失效**。
ARG BASE_IMAGE=python:3.14-slim
FROM ${BASE_IMAGE}

# 非 root 且不可登录。
# WHY 必须非 root：容器内 root 在 Linux 宿主上写出的文件在宿主侧属主为 root（实测
# UID 0），会把挂载进来的工作区文件变成宿主进程改不动的状态；更要紧的是，容器逃逸的
# 收益与容器内身份直接相关，root 让收益最大化。
# WHY 固定高位 uid/gid：便于宿主侧核对挂载目录属主，也便于用 `--user` 覆盖时对照。
RUN groupadd --system --gid 10002 sandbox \
    && useradd --system --uid 10002 --gid sandbox --home-dir /work --shell /usr/sbin/nologin sandbox

# 容器内的工作目录，与 runner 的挂载点一致（宿主工作区 → /work）。
WORKDIR /work

# WHY 不预装任何额外工具（curl / git / gcc 都没有）：本镜像的用途是「跑用户已有的脚本」，
# 而不是「变成一个开发机」。装什么就等于给一次逃逸多一件工具；确有需要的部署应基于本
# 文件另建镜像，并把那份镜像名写进 SANDBOX_DOCKER_IMAGE。
#
# WHY 不用 USER 指令切身份、而由 runner 传 `--user`：挂载进来的工作区属主由宿主决定，
# 固定的镜像内 uid 在 Windows 宿主（Docker Desktop 代为处理属主）与 Linux 宿主（uid 必须
# 与宿主用户一致才写得动）上无法同时正确。由 runner 按宿主决定，是这两个平台上唯一
# 都能工作的做法——这与「容器内默认身份」是同一个决定的两面。
CMD ["sh"]
