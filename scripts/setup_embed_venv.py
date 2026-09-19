"""准备嵌入用的独立环境：建 venv 并装上模型运行时。

WHY 要有这个脚本而不是让用户照着文档敲三行命令：``EMBEDDING_BACKEND=subprocess``
的第一道门槛就是「独立环境在不在、里面有没有 fastembed」，而它缺东西时的报错发生在
子进程的 stderr 里（客户端会把末几行带出来）。把准备动作固化成一条命令，既能让报错
信息直接指过来，也保证路径与 ``default_embed_python`` 的约定一致。

用法::

    python scripts/setup_embed_venv.py                 # 建到 .data/embed-venv
    python scripts/setup_embed_venv.py --dir D:/embed  # 建到别处（配合 EMBEDDING_PYTHON）
    python scripts/setup_embed_venv.py --force         # 已存在也重建

退出码：``0`` 成功 / ``1`` 失败。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# WHY 强制 UTF-8：子命令输出里可能含非 GBK 字符（uv 的进度条、包名），Windows 控制台
# 默认 GBK 会让 print 抛 UnicodeEncodeError，把一次成功的安装变成假失败。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from llm.embed_process import default_embed_python  # noqa: E402


def _run(args: list[str], timeout: float) -> int:
    """执行外部命令并透传输出；返回其退出码（超时返回 -1）。"""
    try:
        completed = subprocess.run(args, timeout=timeout)
    except FileNotFoundError:
        print(f"命令不存在：{args[0]}（需要先安装 uv）")
        return -1
    except subprocess.TimeoutExpired:
        print(f"命令超时（{timeout}s）：{' '.join(args)}")
        return -1
    return completed.returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="准备嵌入用的独立 Python 环境")
    parser.add_argument(
        "--dir",
        default=None,
        help="venv 父目录（默认取 <项目>/.data，即 default_embed_python 的约定位置）",
    )
    parser.add_argument("--force", action="store_true", help="已存在时先删除再重建")
    parser.add_argument(
        "--index",
        action="store_true",
        help="额外安装知识库索引依赖（langchain-text-splitters）；子进程档位用不到，"
        "但手工验证索引链路时有用",
    )
    args = parser.parse_args(argv)

    data_dir = pathlib.Path(args.dir).expanduser().resolve() if args.dir else ROOT / ".data"
    venv_dir = data_dir / "embed-venv"
    python = default_embed_python(data_dir)

    if venv_dir.exists() and args.force:
        print(f"[1/3] 删除既有环境：{venv_dir}")
        import shutil

        # WHY 整体删除而不是原地升级：模型运行时（onnxruntime）的版本与 ABI 强相关，
        # 在一个可能已损坏的环境上叠加安装，会得到「有的包新、有的包旧」的状态，而
        # 那种环境的报错往往指向无关的位置。
        shutil.rmtree(venv_dir, ignore_errors=True)

    if python.exists():
        print(f"[1/3] 复用已有环境：{venv_dir}")
    else:
        print(f"[1/3] 创建独立环境：{venv_dir}")
        code = _run(
            [
                "uv",
                "venv",
                str(venv_dir),
                "--python",
                f"{sys.version_info.major}.{sys.version_info.minor}",
            ],
            600.0,
        )
        if code != 0:
            print("创建环境失败")
            return 1

    packages = ["fastembed"]
    if args.index:
        packages.append("langchain-text-splitters")

    print(f"[2/3] 安装：{' '.join(packages)}")
    code = _run(["uv", "pip", "install", "--python", str(python), *packages], 1800.0)
    if code != 0:
        print("安装失败")
        return 1

    print("[3/3] 自检")
    code = _run(
        [str(python), "-c", "import fastembed, onnxruntime; print('fastembed', fastembed.__version__ if hasattr(fastembed, '__version__') else 'ok')"],
        300.0,
    )
    if code != 0:
        print("自检失败：环境装上了但 import 不通过")
        return 1

    print(f"\n完成。解释器：{python}")
    print("在 .env 中启用：EMBEDDING_BACKEND=subprocess")
    if args.dir:
        print(f"（自定义目录需同时设置 EMBEDDING_PYTHON={python}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
