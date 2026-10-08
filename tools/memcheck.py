# -*- coding: utf-8 -*-
"""内存压力测试：连续打开大动图 / 大图，检查内存是否受控。

用法::

    python tools/memcheck.py            # 打开最大的 12 个动图
    python tools/memcheck.py -n 20      # 自定义数量

判定标准：整个过程中内存增长不超过 250MB，且单次打开耗时不超过 1.5 秒。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 源码模式下项目根没有 .cache 时，指向打包目录里的缓存，
# 这样测试/校验脚本可以直接复用打包版建好的索引。
_PACKAGED_CACHE = Path(__file__).resolve().parent.parent / "QQEmojiExporter" / ".cache"
if _PACKAGED_CACHE.is_dir() and not os.environ.get("QQEMOJI_CACHE"):
    os.environ["QQEMOJI_CACHE"] = str(_PACKAGED_CACHE)

from qqemoji.gui import App  # noqa: E402


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_psapi.GetProcessMemoryInfo.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
    ctypes.wintypes.DWORD,
]
_psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
_THIS_PROCESS = _kernel32.GetCurrentProcess()


def memory_mb() -> float:
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    ok = _psapi.GetProcessMemoryInfo(_THIS_PROCESS, ctypes.byref(counters), counters.cb)
    if not ok:
        return -1.0
    return counters.WorkingSetSize / 1048576


def pump(app, seconds=1.0, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        if until and until():
            return True
        time.sleep(0.02)
    return bool(until and until())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--number", type=int, default=12)
    args = parser.parse_args()

    app = App()
    pump(app, 30, until=lambda: app.total > 0 and bool(app.items))
    pump(app, 2.0)

    # 只看动图 + 按体积降序，前 N 个就是最占内存的那批
    app.animated_var.set(True)
    app.sort_var.set("体积：大 → 小")
    pump(app, 20, until=lambda: (not app.fetching) and bool(app.items))
    pump(app, 1.0)

    targets = app.items[: args.number]
    print(f"待测：{len(targets)} 个最大的动图\n")
    base = memory_mb()
    print(f"起始内存：{base:.0f} MB\n")

    samples: list[float] = []
    slowest = 0.0
    for position, record in enumerate(targets):
        start = time.time()
        app.detail.open(position)
        pump(app, 1.2, until=lambda: app.detail.ready)
        opened = time.time() - start
        pump(app, 1.2)               # 播一会儿
        after = memory_mb()
        samples.append(after)
        slowest = max(slowest, opened)
        print(f"  {position + 1:2d}. {(record.get('name') or '')[:18]:20} "
              f"{_size(record.get('size')):>8}  帧数 {app.detail.frame_count:4d}  "
              f"打开 {opened * 1000:6.0f} ms  内存 {after:6.0f} MB")
        app.detail.close()
        app.update()

    peak = max(samples) if samples else base
    growth = peak - base
    print(f"\n峰值内存 {peak:.0f} MB，较起始增长 {growth:.0f} MB，最慢一次打开 {slowest * 1000:.0f} ms")

    # 再走一遍，看是否持续增长（泄漏判断）
    middle = memory_mb()
    for position in range(min(len(targets), 5)):
        app.detail.open(position)
        pump(app, 1.0, until=lambda: app.detail.ready)
        app.detail.close()
        app.update()
        pump(app, 0.3)
    end = memory_mb()
    print(f"第二轮后内存 {end:.0f} MB（第一轮同位置 {middle:.0f} MB）")

    ok_growth = growth <= 250
    ok_second = (end - middle) <= 120
    ok_speed = slowest <= 1.5
    print(f"\n判定：增长受控={ok_growth}  无持续泄漏={ok_second}  打开够快={ok_speed}")

    app._on_close()
    return 0 if (ok_growth and ok_second and ok_speed) else 1


def _size(value) -> str:
    value = float(value or 0)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


if __name__ == "__main__":
    raise SystemExit(main())
