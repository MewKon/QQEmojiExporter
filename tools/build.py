# -*- coding: utf-8 -*-
"""打包脚本：把程序编译打包成单文件，方便直接启动。

用法::

    python tools/build.py            # 打包成 QQEmojiExporter.pyz（纯标准库，离线可用）
    python tools/build.py --exe      # 额外尝试用 PyInstaller 打包成 exe（需要联网安装）

产物在 dist/ 目录下：

* ``QQEmojiExporter.pyz`` —— 单文件包（源码已编译成字节码），用同目录的「启动.bat」打开
* ``QQEmojiExporter.exe`` —— 独立 exe（仅在装有 PyInstaller 时生成，无需 Python 环境）
"""

from __future__ import annotations

import argparse
import compileall
import os
import shutil
import subprocess
import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 产物直接放在项目根目录：这样打包版与源码版共用同一个 .cache（索引、缩略图不用重建）
DIST = ROOT
STAGE = ROOT / ".build" / "app"
ENTRY = "QQEmojiExporter.pyz"


def build_pyz() -> Path:
    """用 zipapp 打单文件包，并把源码编译成 .pyc 一起打进去。"""
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    # 只带上运行需要的代码
    shutil.copytree(ROOT / "qqemoji", STAGE / "qqemoji",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "app.py", STAGE / "app.py")
    ensure_icon()
    if (ROOT / "assets").is_dir():
        shutil.copytree(ROOT / "assets", STAGE / "assets",
                        ignore=shutil.ignore_patterns("*.png", "*.svg"))
    (STAGE / "__main__.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        '"""单文件包入口。"""\n'
        "import sys\n\n"
        "from app import main\n\n"
        "if __name__ == \"__main__\":\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )

    print("编译字节码 …")
    compileall.compile_dir(str(STAGE), quiet=1, force=True)
    for cached in STAGE.rglob("__pycache__"):
        shutil.rmtree(cached, ignore_errors=True)

    DIST.mkdir(exist_ok=True)
    target = DIST / ENTRY
    if target.exists():
        target.unlink()
    print(f"打包 → {target}")
    zipapp.create_archive(STAGE, target, interpreter="/usr/bin/env python", compressed=True)
    size = target.stat().st_size / 1048576
    print(f"完成：{target.name}（{size:.1f} MB）")
    return target


def ensure_icon() -> Path | None:
    """生成应用图标（深色圆角底 + 白色下载箭头，纯几何图形，不用任何 emoji 字形）。"""
    icon_path = ROOT / "assets" / "app.ico"
    if icon_path.is_file():
        return icon_path
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    icon_path.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, size - 8, size - 8), radius=52,
                           fill=(23, 27, 36, 255), outline=(76, 141, 255, 255), width=7)
    center = size / 2
    draw.line((center, 58, center, 148), fill=(230, 234, 242, 255), width=22)
    draw.polygon([(center - 46, 130), (center + 46, 130), (center, 184)],
                 fill=(230, 234, 242, 255))
    draw.rounded_rectangle((62, 196, size - 62, 214), radius=9, fill=(76, 141, 255, 255))
    image.save(icon_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                 (64, 64), (128, 128), (256, 256)])
    print(f"已生成图标：{icon_path.relative_to(ROOT)}")
    return icon_path


def build_exe(onedir: bool = False) -> Path | None:
    """用 PyInstaller 打独立 exe（没装则提示如何安装）。

    :param onedir: ``True`` 打目录版（启动不解包、更快更稳）；``False`` 打单文件版
    """
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("\n未安装 PyInstaller，跳过 exe 打包。安装后可执行：")
        print("    python -m pip install pyinstaller")
        print("    python tools/build.py --exe")
        return None

    mode = "onedir" if onedir else "onefile"
    name = "QQEmojiExporter"
    print(f"\n用 PyInstaller 打包 exe（{mode}）…")
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", name,
        "--windowed",                      # 不显示控制台窗口
        f"--{mode}",
        "--distpath", str(DIST),
        "--workpath", str(ROOT / ".build" / f"pyinstaller-{mode}"),
        "--specpath", str(ROOT / ".build"),
        "--hidden-import", "PIL._tkinter_finder",
        # CustomTkinter 的主题 json / 字体 / 图标都在它自己的 assets 目录里，必须一起打进去
        "--collect-all", "customtkinter",
        # 系统里如果装过 pywebview，会连带装上 pythonnet / clr_loader；
        # PyInstaller 的自动钩子会把它们一起收进来（体积直接翻三倍），当前界面用不到，排除掉
        "--exclude-module", "clr",
        "--exclude-module", "clr_loader",
        "--exclude-module", "pythonnet",
        "--exclude-module", "webview",
    ]
    icon = ensure_icon()
    if icon is not None:
        args += ["--icon", str(icon)]
        args += ["--add-data", f"{icon}{os.pathsep}assets"]
    args.append(str(ROOT / "app.py"))

    # ⚠️ PyInstaller 的 --noconfirm 会先清空输出目录，而缓存就在该目录里，
    # 因此打包前把缓存暂存出来，打包后放回，避免把索引/缩略图删掉。
    target_dir = DIST / name
    cache_dir = target_dir / ".cache"
    backup = ROOT / ".build" / "cache-backup"
    preserved = False
    if onedir and cache_dir.is_dir():
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        shutil.move(str(cache_dir), str(backup))
        preserved = True
        print("已把程序目录里的缓存暂存起来，打包后放回")

    try:
        result = subprocess.run(args, cwd=str(ROOT))
    finally:
        if preserved and backup.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(cache_dir))
            print("缓存已放回程序目录")

    target = (target_dir / f"{name}.exe") if onedir else (DIST / f"{name}.exe")
    if result.returncode == 0 and target.is_file():
        size = sum(f.stat().st_size for f in target.parent.rglob("*") if f.is_file()) \
            if onedir else target.stat().st_size
        print(f"完成：{target}（{size / 1048576:.1f} MB）")
        return target
    print(f"{mode} 打包失败，可改用另一种模式。")
    return None


def write_launcher(target: Path) -> Path:
    """生成免控制台的启动脚本。"""
    launcher = DIST / "启动.bat"
    launcher.write_text(
        "@echo off\n"
        "chcp 65001 >nul\n"
        'cd /d "%~dp0"\n'
        f'start "" pythonw "{target.name}"\n',
        encoding="utf-8",
        newline="\r\n",
    )
    print(f"完成：{launcher.name}（双击即可启动，不弹黑框）")
    return launcher


def main() -> int:
    parser = argparse.ArgumentParser(
        description="打包 QQ 表情包提取器（默认打目录版 exe）",
        epilog="产物：QQEmojiExporter\\QQEmojiExporter.exe ，缓存保存在同目录的 .cache 里",
    )
    parser.add_argument("--onedir", action="store_true", help="打包目录版 exe（默认）")
    parser.add_argument("--exe", action="store_true",
                        help="打包单文件 exe（每次启动要自解包到临时目录，一般不用）")
    parser.add_argument("--pyz", action="store_true", help="额外打包 .pyz 单文件包")
    args = parser.parse_args()

    if args.pyz:
        write_launcher(build_pyz())
    if args.exe:
        build_exe(onedir=False)
    if args.onedir or not args.exe:
        build_exe(onedir=True)
    print("\n产物目录：", DIST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
