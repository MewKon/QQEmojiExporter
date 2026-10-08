"""QQ 表情包提取器 —— 启动入口（Tkinter 桌面程序 + 命令行模式）。

用法::

    python app.py                     # 启动桌面界面
    python app.py --detect            # 只检测 QQ 数据目录并打印
    python app.py --scan              # 命令行扫描（不打开界面）
    python app.py --scan --account "C:\\Users\\你\\Documents\\Tencent Files\\123456789"
                                   --sources marketface,personal,recv
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 保证 Windows 控制台下的中文输出正常
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from qqemoji import __version__, config  # noqa: E402


def run_detect() -> int:
    from qqemoji import detect

    table = detect.detect_table(deep=True)
    if not table["accounts"]:
        print("未检测到任何 QQ 数据目录。")
        return 1
    print(f"检测到 {table['count']} 个 QQ 账号目录：\n")
    for root in table["roots"]:
        print(f"[{root['reason']}] {root['path']}")
        for acc in root["accounts"]:
            size_mb = acc["size_bytes"] / 1024 / 1024
            print(f"    - {acc['display']}  约 {size_mb:.1f} MB  ({acc['path']})")
    return 0


def run_scan(accounts: list[str], sources: list[str] | None) -> int:
    from qqemoji import scanner, store

    if not accounts:
        from qqemoji import detect

        table = detect.detect_table(deep=False)
        accounts = [a["path"] for a in table["accounts"][:1]]
    if not accounts:
        print("未找到账号目录")
        return 1

    index = store.get_index()
    total = 0
    for account in accounts:
        path = Path(account)
        print(f"扫描 {path.name} …")
        records: list[dict] = []
        for record in scanner.scan_account(path, path.name, sources):
            records.append(record)
            if len(records) % 500 == 0:
                print(f"  已发现 {len(records)} 个表情", end="\r")
        index.clear_account(str(path))
        index.upsert_many(scanner.enrich_batch(records))
        total += len(records)
        print(f"  完成：{len(records)} 个表情           ")
    index.set_meta("scanned_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    print(f"共索引 {total} 个表情，缓存目录：{config.CACHE_DIR}")
    print("启动界面查看：python app.py")
    return 0


def run_info() -> int:
    """打印运行环境与索引信息（排查打包版 / 工作目录问题很有用）。

    打包成无控制台的 exe 时 stdout 看不到，因此同时写一份到缓存目录下的 info.txt。
    """
    from qqemoji import store

    index = store.get_index()
    stats = index.stats()
    packed = bool(getattr(sys, "frozen", False)) or Path(sys.argv[0]).suffix.lower() == ".pyz"
    lines = [
        f"版本：      {__version__}",
        f"程序目录：  {config.BASE_DIR}",
        f"缓存目录：  {config.CACHE_DIR}",
        f"导出目录：  {config.EXPORT_DIR}",
        f"索引数量：  {stats['total']} 个（上次扫描：{stats['scanned_at'] or '未知'}）",
        f"运行方式：  {'打包版（exe）' if getattr(sys, 'frozen', False) else ('单文件包（pyz）' if packed else '源码')}",
        f"Python：    {sys.version.split()[0]}  ({sys.executable})",
    ]
    text = "\n".join(lines)
    print(text)
    try:
        (config.CACHE_DIR / "info.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ 表情包提取器（桌面版）")
    parser.add_argument("--detect", action="store_true", help="只检测 QQ 路径并退出")
    parser.add_argument("--info", action="store_true", help="打印运行环境与索引信息")
    parser.add_argument("--scan", action="store_true", help="命令行扫描模式（不打开界面）")
    parser.add_argument("--account", action="append", default=[], help="指定账号目录（可重复）")
    parser.add_argument(
        "--sources",
        default="",
        help="命令行扫描时指定来源，逗号分隔：marketface,personal,recv,related,legacy,system",
    )
    args = parser.parse_args(argv)

    if args.detect:
        return run_detect()
    if args.info:
        return run_info()
    if args.scan:
        sources = [s.strip() for s in args.sources.split(",") if s.strip()] or None
        return run_scan(args.account, sources)

    try:
        import tkinter  # noqa: F401
        import customtkinter  # noqa: F401
    except ImportError as exc:
        print(f"缺少桌面界面依赖，无法启动界面：{exc}")
        print("请执行：pip install -r requirements.txt")
        print("也可改用命令行模式：python app.py --detect / --scan")
        return 2

    from qqemoji.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
