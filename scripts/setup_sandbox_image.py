"""构建 Tier 2 的执行镜像。

与 ``setup_embed_venv.py`` 同一个位置：两者都是「某个后端档位依赖的外部产物」，而由脚本
而不是由应用在启动时顺手创建——原因也一样：这类动作要么耗时（拉基础镜像、装依赖），要么
需要联网，把它塞进启动路径会让「起不来」和「环境没准备好」这两件事混在一起。

用法::

    python scripts/setup_sandbox_image.py
    python scripts/setup_sandbox_image.py --tag my-sandbox:v1 --no-cache

退出码：0 成功；2 环境不具备（无 docker 或构建失败）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "docker" / "sandbox.Dockerfile"
DEFAULT_TAG = "harness-sandbox:latest"


def main() -> int:
    """按命令行参数构建执行镜像。"""
    parser = argparse.ArgumentParser(description="构建 Tier 2 执行镜像")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"镜像标签（默认 {DEFAULT_TAG}）")
    parser.add_argument("--no-cache", action="store_true", help="不使用构建缓存")
    parser.add_argument("--base", default="", help="覆盖基础镜像（供离线或内网镜像源使用）")
    args = parser.parse_args()

    if not DOCKERFILE.is_file():
        print(f"找不到构建文件：{DOCKERFILE}")
        return 2

    argv = ["docker", "build", "-f", str(DOCKERFILE), "-t", args.tag]
    if args.no_cache:
        argv.append("--no-cache")
    if args.base:
        # WHY 允许覆盖基础镜像：Docker Hub 在部分网络下不可达（本机实测如此），此时
        # 需要一个内网镜像源或本机已有的等价镜像。把它做成参数而不是改文件，是为了让
        # 「本仓的镜像定义」保持唯一一份。
        argv += ["--build-arg", f"BASE_IMAGE={args.base}"]
    argv.append(str(DOCKERFILE.parent))

    print("执行：" + " ".join(argv))
    try:
        completed = subprocess.run(argv, timeout=1800)
    except FileNotFoundError:
        print("找不到 docker 可执行文件：本机不具备构建执行镜像的条件。")
        return 2
    except subprocess.TimeoutExpired:
        print("构建超时（30 分钟）。")
        return 2

    if completed.returncode != 0:
        print(f"构建失败（退出码 {completed.returncode}）。")
        return 2

    print(f"\n执行镜像已就绪：{args.tag}")
    print("把 SANDBOX_TIER 设为 docker 即可使用；验收：python scripts/smoke_docker_sandbox.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
