"""Tkinter 桌面界面。

零额外依赖（只用标准库 + Pillow），单窗口完成：
路径检测 → 扫描 → 浏览筛选 → 详情预览（内嵌，可缩放） → 导出。

界面结构::

    ┌ 菜单栏 ────────────────────────────────────────────────┐
    │ 工具栏：检测状态      搜索框      导出(N)   开始扫描      │
    ├──────────┬─────────────────────────────────────────────┤
    │ 侧边栏    │ 统计条 + 选择操作                            │
    │ 数据目录  │ 缩略图网格（虚拟化渲染，支持几万张）           │
    │ 表情来源  │  ← 双击卡片在同一窗口内切换到详情视图          │
    │ 筛选      │     （支持放大/缩小/适应窗口/拖拽平移）        │
    ├──────────┴─────────────────────────────────────────────┤
    │ 状态栏 + 进度条 + 取消                                   │
    └────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import ctypes
import io
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from tkinter import filedialog, messagebox, ttk

from PIL import Image

try:  # Pillow 9.1+ 的常量位置
    from PIL import ImageTk

    RESAMPLE_DOWN = Image.Resampling.LANCZOS
    RESAMPLE_UP = Image.Resampling.BICUBIC
    RESAMPLE_PIXEL = Image.Resampling.NEAREST
except ImportError:  # pragma: no cover
    ImageTk = None
    RESAMPLE_DOWN = Image.LANCZOS
    RESAMPLE_UP = Image.BICUBIC
    RESAMPLE_PIXEL = Image.NEAREST

from . import __version__, config, detect, exporter, images, jobs as jobs_mod, tasks

# --------------------------------------------------------------------------- #
# 主题与尺寸
# --------------------------------------------------------------------------- #
COLORS = {
    "bg": "#12151c",
    "bg2": "#171b24",
    "bg3": "#1d2230",
    "bg_hover": "#242b3b",
    "border": "#2a3142",
    "text": "#e6eaf2",
    "dim": "#9aa4b8",
    "mute": "#6b7488",
    "primary": "#4c8dff",
    "accent": "#ff7a59",
    "ok": "#35c98a",
    "cell": "#1b2130",
    "cell_sel": "#3a2a26",
    "hover": "#242c3d",
}

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 12, "bold")
FONT_CANVAS = ("Microsoft YaHei UI", 9)

CELL_W = 138
CELL_H = 178
GAP = 10
PAD = 14
THUMB_BOX = 120
INFO_H = 38
CHECK_SIZE = 18

PAGE_SIZE_OPTIONS = (60, 120, 240)
DEFAULT_PAGE_SIZE = 120
THUMB_CACHE_MAX = 280   # 分页后同时只需一页的缩略图，内存占用恒定
MAX_RENDER_PIXELS = 16_000_000   # 详情视图单帧渲染上限
ANIMATE_PIXEL_LIMIT = 6_000_000  # 超过这个尺寸就暂停自动播放，保证缩放流畅
MAX_ZOOM = 12.0
MIN_ZOOM = 0.05

SORT_OPTIONS = [
    ("时间：新 → 旧", "time_desc"),
    ("时间：旧 → 新", "time_asc"),
    ("体积：大 → 小", "size_desc"),
    ("体积：小 → 大", "size_asc"),
    ("名称", "name_asc"),
    ("按表情包", "pack_asc"),
]
CONVERT_OPTIONS = [
    ("保持原格式", "origin"),
    ("转为 PNG", "png"),
    ("转为 GIF", "gif"),
    ("转为 WebP", "webp"),
    ("转为 JPG", "jpg"),
]


def _enable_dpi_awareness() -> None:
    """让窗口在高分屏上不发虚。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _colorref(hex_color: str) -> int:
    """把 #RRGGBB 转成 Windows 的 COLORREF（0x00BBGGRR）。"""
    text = hex_color.lstrip("#")
    r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    return (b << 16) | (g << 8) | r


def apply_window_theme(window: tk.Misc) -> bool:
    """Windows：把标题栏切成深色并染成和界面一致的底色。

    返回是否成功（非 Windows 或系统不支持时返回 False，不影响功能）。
    """
    if os.name != "nt":
        return False
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()

        applied = False
        dark = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（新旧 SDK 编号）
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(dark), ctypes.sizeof(dark)
            ) == 0:
                applied = True
                break

        # Windows 11：可直接指定标题栏底色与文字颜色（旧系统会失败，忽略即可）
        for attribute, color in ((35, COLORS["bg2"]), (36, COLORS["text"])):
            value = ctypes.c_int(_colorref(color))
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
                )
            except Exception:
                pass
        return applied
    except Exception:
        return False


def _fit(image: Image.Image, box: int) -> Image.Image:
    copy = image.copy()
    copy.thumbnail((box, box), RESAMPLE_DOWN)
    return copy


def _short(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _human_size(size: float) -> str:
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit in ("B", "KB"):
                return f"{size:.0f} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def _safe_int(text: str) -> int:
    try:
        return max(0, int(str(text).strip() or 0))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# 缩略图后台加载
# --------------------------------------------------------------------------- #
class ThumbLoader:
    """在后台线程里准备缩略图，主线程只负责创建 PhotoImage。"""

    def __init__(self, workers: int = 4) -> None:
        self.results: queue.Queue = queue.Queue()
        self.pending: set[str] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="thumb")

    def request(self, record: dict) -> None:
        item_id = record["id"]
        with self._lock:
            if item_id in self.pending:
                return
            self.pending.add(item_id)
        self._pool.submit(self._work, dict(record))

    def _work(self, record: dict) -> None:
        item_id = record["id"]
        try:
            path = self._thumb_file(record)
            if path is None:
                self.results.put((item_id, None, None))
                return
            with Image.open(path) as im:
                image = _fit(im.convert("RGBA"), THUMB_BOX)
            self.results.put((item_id, image, None))
        except Exception as exc:  # noqa: BLE001 - 单张失败不影响整体
            self.results.put((item_id, None, f"{type(exc).__name__}"))
        finally:
            with self._lock:
                self.pending.discard(item_id)

    def _thumb_file(self, record: dict) -> Path | None:
        item_id = record["id"]
        for suffix in (".webp", ".png"):
            cached = config.THUMB_DIR / f"{item_id}{suffix}"
            if cached.is_file():
                return cached
        try:
            data = images.Loader.load(record)
        except Exception:
            return None
        path, info = images.make_thumbnail(data, config.THUMB_DIR, item_id, config.THUMB_SIZE)
        if info.get("width") and (
            int(record.get("width") or 0) != info["width"]
            or int(record.get("height") or 0) != info["height"]
        ):
            try:
                from . import store

                store.get_index().update_probe(
                    item_id, info["width"], info["height"], info["frames"], info["animated"]
                )
            except Exception:
                pass
        return path

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- #
# 导出对话框
# --------------------------------------------------------------------------- #
class ExportDialog(tk.Toplevel):
    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title("导出表情")
        self.configure(bg=COLORS["bg2"])
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        scope = app.export_scope()
        count = scope["count"]
        outer = tk.Frame(self, bg=COLORS["bg2"], padx=20, pady=16)
        outer.pack(fill="both", expand=True)

        scope_text = (
            f"导出范围：当前筛选结果的全部 {count} 个表情"
            if app.select_all_filtered
            else f"导出范围：已选择的 {count} 个表情"
        )
        tk.Label(outer, text=scope_text, bg=COLORS["bg2"], fg=COLORS["text"], font=FONT,
                 anchor="w").pack(fill="x", pady=(0, 14))

        self.mode = tk.StringVar(value="zip")
        self.mode_row = tk.Frame(outer, bg=COLORS["bg2"])
        self.mode_row.pack(fill="x")
        for text, value in (("打包为 ZIP 下载", "zip"), ("导出到文件夹", "folder")):
            tk.Radiobutton(self.mode_row, text=text, value=value, variable=self.mode, command=self._sync,
                           bg=COLORS["bg2"], fg=COLORS["text"], selectcolor=COLORS["bg3"],
                           activebackground=COLORS["bg2"], activeforeground=COLORS["text"],
                           font=FONT, cursor="hand2").pack(side="left", padx=(0, 20))

        self.dir_row = tk.Frame(outer, bg=COLORS["bg2"])
        tk.Label(self.dir_row, text="目标目录", bg=COLORS["bg2"], fg=COLORS["dim"],
                 font=FONT_SMALL).pack(anchor="w")
        inner = tk.Frame(self.dir_row, bg=COLORS["bg2"])
        inner.pack(fill="x", pady=(4, 0))
        self.dir_var = tk.StringVar()
        tk.Entry(inner, textvariable=self.dir_var, bg=COLORS["bg"], fg=COLORS["text"],
                 insertbackground=COLORS["text"], relief="flat", width=44).pack(side="left", ipady=5)
        tk.Button(inner, text="选择文件夹…", command=self._pick_dir, bg=COLORS["bg3"], fg=COLORS["text"],
                  relief="flat", bd=0, padx=12, pady=5, font=FONT_SMALL,
                  cursor="hand2").pack(side="left", padx=8)

        grid = tk.Frame(outer, bg=COLORS["bg2"])
        grid.pack(fill="x", pady=(14, 0))
        grid.columnconfigure(2, weight=1)

        tk.Label(grid, text="图片格式", bg=COLORS["bg2"], fg=COLORS["dim"], font=FONT_SMALL,
                 anchor="w").grid(row=0, column=0, sticky="w")
        self.convert = ttk.Combobox(grid, values=[t for t, _ in CONVERT_OPTIONS], state="readonly",
                                    width=14, font=FONT_SMALL)
        self.convert.current(0)
        self.convert.grid(row=1, column=0, sticky="w", padx=(0, 14), pady=(3, 0))

        tk.Label(grid, text="最大边长（0 = 不限）", bg=COLORS["bg2"], fg=COLORS["dim"],
                 font=FONT_SMALL, anchor="w").grid(row=0, column=1, sticky="w")
        self.max_side = tk.StringVar(value="0")
        tk.Entry(grid, textvariable=self.max_side, bg=COLORS["bg"], fg=COLORS["text"],
                 insertbackground=COLORS["text"], relief="flat", width=10).grid(
            row=1, column=1, sticky="w", pady=(3, 0), ipady=4, padx=(0, 14))

        tk.Label(grid, text="命名模板", bg=COLORS["bg2"], fg=COLORS["dim"], font=FONT_SMALL,
                 anchor="w").grid(row=0, column=2, sticky="w")
        self.template = tk.StringVar(value="{index}_{pack}_{name}")
        tk.Entry(grid, textvariable=self.template, bg=COLORS["bg"], fg=COLORS["text"],
                 insertbackground=COLORS["text"], relief="flat").grid(
            row=1, column=2, sticky="ew", pady=(3, 0), ipady=4)

        tk.Label(outer, text="可用变量：{index} {source} {pack} {name} {raw} {id} {ext} {account}",
                 bg=COLORS["bg2"], fg=COLORS["mute"], font=FONT_SMALL, anchor="w").pack(
            fill="x", pady=(6, 0))
        tk.Label(outer, text="（{raw} 是 QQ 的原始文件名/哈希，{name} 是可读名称）",
                 bg=COLORS["bg2"], fg=COLORS["mute"], font=FONT_SMALL, anchor="w").pack(fill="x")

        self.dedupe = tk.BooleanVar(value=True)
        tk.Checkbutton(outer, text="按内容去重（跳过完全相同的表情）", variable=self.dedupe,
                       bg=COLORS["bg2"], fg=COLORS["text"], selectcolor=COLORS["bg3"],
                       activebackground=COLORS["bg2"], activeforeground=COLORS["text"],
                       font=FONT_SMALL, cursor="hand2").pack(anchor="w", pady=(12, 0))

        tk.Label(outer, text="提示：缩放动图会逐帧处理并保留动画（GIF/WebP）；转为 PNG/JPG 只保留首帧。",
                 bg=COLORS["bg2"], fg=COLORS["mute"], font=FONT_SMALL, anchor="w",
                 wraplength=520, justify="left").pack(fill="x", pady=(8, 0))

        buttons = tk.Frame(outer, bg=COLORS["bg2"])
        buttons.pack(fill="x", pady=(18, 0))
        tk.Button(buttons, text="开始导出", command=self._start, bg=COLORS["primary"], fg="#fff",
                  relief="flat", bd=0, padx=18, pady=7, font=FONT, cursor="hand2").pack(side="right")
        tk.Button(buttons, text="取消", command=self.destroy, bg=COLORS["bg3"], fg=COLORS["text"],
                  relief="flat", bd=0, padx=16, pady=7, font=FONT, cursor="hand2").pack(
            side="right", padx=(0, 10))

        self._sync()
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(30, lambda: apply_window_theme(self))

    def _sync(self) -> None:
        if self.mode.get() == "folder":
            self.dir_row.pack(fill="x", pady=(12, 0), after=self.mode_row)
        else:
            self.dir_row.pack_forget()

    def _pick_dir(self) -> None:
        path = filedialog.askdirectory(title="选择导出目录", parent=self)
        if path:
            self.dir_var.set(path)

    def _start(self) -> None:
        if self.mode.get() == "folder" and not self.dir_var.get().strip():
            messagebox.showwarning("提示", "请先选择导出目录", parent=self)
            return
        scope = self.app.export_scope()
        payload = {
            "ids": scope["ids"],
            "filters": scope["filters"],
            "mode": self.mode.get(),
            "target_dir": self.dir_var.get().strip() or None,
            "convert": dict((t, v) for t, v in CONVERT_OPTIONS)[self.convert.get()],
            "max_side": _safe_int(self.max_side.get()),
            "dedupe": self.dedupe.get(),
            "name_template": self.template.get().strip() or "{index}_{pack}_{name}",
        }
        self.destroy()
        self.app.start_export(payload)


# --------------------------------------------------------------------------- #
# 详情视图（内嵌在主窗口里，不弹新窗口）
# --------------------------------------------------------------------------- #
class DetailView(tk.Frame):
    """同一窗口内的详情面板：显示大图、详情信息，并支持放大缩小与拖拽平移。"""


    def __init__(self, parent: tk.Widget, app: "App") -> None:
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.record: dict | None = None
        self.data: bytes | None = None      # 动图原始字节，逐帧按需解码
        self.frame_count = 0
        self.durations: list[int] = []
        self.frame_index = 0
        self.playing = True
        self.ready = False
        self.size: tuple[int, int] = (0, 0)
        self._cache_key: tuple | None = None
        self._cache_image: Image.Image | None = None
        self._load_token = 0
        self.scale = 1.0
        self.fit_mode = True
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._photo: ImageTk.PhotoImage | None = None
        self._job: str | None = None
        self._drag: tuple[float, float] | None = None
        self._render_job: str | None = None
        self.active = False   # 详情视图是否正在显示（由 App 切换，避免依赖 winfo_ismapped 的时序）
        self._throttled = False  # 因缩放过大临时停播（缩小后自动恢复）

        # 顶部工具条
        top = tk.Frame(self, bg=COLORS["bg2"], height=46)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)
        self._button(top, "返回图库", self.close).pack(side="left", padx=(12, 10), pady=8)
        self.title_label = tk.Label(top, text="", bg=COLORS["bg2"], fg=COLORS["text"], font=FONT,
                                    anchor="w")
        self.title_label.pack(side="left", fill="x", expand=True)
        self._button(top, "上一个", lambda: self.step(-1)).pack(side="right", padx=(0, 12), pady=8)
        self._button(top, "下一个", lambda: self.step(1)).pack(side="right", padx=(0, 6), pady=8)

        # 图片区
        self.canvas = tk.Canvas(self, bg="#0d1016", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._schedule_render())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.canvas.bind("<Double-Button-1>", lambda e: self.toggle_play())

        # 底部信息 + 操作
        bottom = tk.Frame(self, bg=COLORS["bg2"])
        bottom.pack(fill="x", side="bottom")
        actions = tk.Frame(bottom, bg=COLORS["bg2"])
        actions.pack(fill="x", padx=12, pady=(8, 4))
        self._button(actions, "缩小", lambda: self.zoom_step(-1)).pack(side="left")
        self._button(actions, "放大", lambda: self.zoom_step(1)).pack(side="left", padx=6)
        self._button(actions, "适应窗口", self.zoom_fit).pack(side="left")
        self._button(actions, "原始大小", self.zoom_reset).pack(side="left", padx=6)
        self.play_button = self._button(actions, "暂停", self.toggle_play)
        self.play_button.pack(side="left", padx=(0, 6))
        self.zoom_label = tk.Label(actions, text="", bg=COLORS["bg2"], fg=COLORS["dim"],
                                   font=FONT_SMALL, width=16)
        self.zoom_label.pack(side="left", padx=6)
        self._button(actions, "下载原图", self.download).pack(side="right")
        self._button(actions, "复制路径", self.copy_path).pack(side="right", padx=6)
        self._button(actions, "在资源管理器中显示", self.reveal).pack(side="right")
        self.select_button = self._button(actions, "选择此表情", self.toggle_select)
        self.select_button.pack(side="right", padx=6)

        self.meta_label = tk.Label(bottom, text="", bg=COLORS["bg2"], fg=COLORS["mute"],
                                   font=FONT_SMALL, anchor="w", justify="left", wraplength=900)
        self.meta_label.pack(fill="x", padx=14, pady=(0, 2))
        self.path_label = tk.Label(bottom, text="", bg=COLORS["bg2"], fg=COLORS["mute"],
                                   font=("Consolas", 8), anchor="w", justify="left", wraplength=900)
        self.path_label.pack(fill="x", padx=14, pady=(0, 10))

    def _button(self, parent, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=COLORS["bg3"], fg=COLORS["text"],
                         activebackground=COLORS["bg_hover"], activeforeground=COLORS["text"],
                         relief="flat", bd=0, padx=11, pady=5, font=FONT_SMALL, cursor="hand2")

    # ---------------------------------------------------------------- #
    # 打开 / 关闭
    # ---------------------------------------------------------------- #
    def open(self, index: int) -> None:
        self.app.switch_to_detail()
        self.show(index)

    def close(self) -> None:
        self._stop()
        self.app.switch_to_gallery()

    def show(self, index: int) -> None:
        record = self.app.item_at(index)
        if record is None:
            return  # 该下标不在当前页（翻页过程中会出现），等待翻页完成后重新显示
        self.app.current_index = index
        self.record = record
        self.title_label.config(text=exporter.display_name(record))
        self.select_button.config(text="取消选择" if self.app.is_selected(record["id"]) else "选择此表情")

        meta = [
            f"{record.get('width') or '?'} × {record.get('height') or '?'}",
            (record.get("ext") or "").replace(".", "").upper() or "未知格式",
            _human_size(record.get("size") or 0),
            config.SOURCE_LABELS.get(record.get("source", ""), record.get("source", "")),
        ]
        if record.get("pack_name"):
            meta.append(f"表情包：{record['pack_name']}")
        if record.get("animated"):
            meta.append(f"动图：{record.get('frames') or '?'} 帧")
        if record.get("keywords"):
            meta.append(f"关键词：{record['keywords']}")
        raw = (record.get("raw_name") or "").strip()
        if raw and raw != record.get("name"):
            meta.append(f"原始文件名：{raw}")
        self.meta_label.config(text="　|　".join(str(m) for m in meta if m))
        self.path_label.config(text=str(record.get("path", "")))

        self._start_load(record)

    # ---------------------------------------------------------------- #
    # 载入（后台分析帧信息，主线程按需取帧渲染）
    # ---------------------------------------------------------------- #
    def _start_load(self, record: dict) -> None:
        self._stop()
        self._load_token += 1
        token = self._load_token
        self.ready = False
        self.data = None
        self.frame_count = 0
        self.durations = [80]
        self.frame_index = 0
        self.playing = False
        self._throttled = False
        self._cache_key = None
        self._cache_image = None
        self._update_play_button()
        self._show_message("载入中…")

        def work() -> dict:
            data = images.Loader.load(record)
            info = images.describe_animation(data)
            info["data"] = data
            return info

        def done(info: dict) -> None:
            if token != self._load_token:
                return
            self.data = info.get("data")
            self.frame_count = int(info.get("frames") or 1)
            self.size = tuple(info.get("size") or (0, 0))
            self.durations = list(info.get("durations") or [80])
            self.ready = True
            self._render()
            if self.frame_count > 1:
                self._set_playing(True)     # 动图自动播放
            else:
                self._update_play_button()

        # 放到后台线程：几千帧的 GIF 光读时长就能卡住界面
        self.app._run_async(work, done, "载入图片失败")

    def _show_message(self, text: str) -> None:
        cw, ch = self._canvas_size()
        self.canvas.delete("all")
        self.canvas.create_text(cw / 2, ch / 2, text=text, fill=COLORS["mute"], font=FONT)

    def _update_play_button(self) -> None:
        if not hasattr(self, "play_button"):
            return
        if not self.ready:
            text, state = "暂停", "disabled"
        elif self.frame_count <= 1:
            text, state = "单帧", "disabled"
        elif self.playing:
            text, state = "暂停", "normal"
        else:
            text, state = "播放", "normal"
        try:
            self.play_button.config(text=text, state=state)
        except tk.TclError:
            pass

    def _set_playing(self, playing: bool) -> None:
        """统一处理播放状态：定时器、按钮文字、画面提示。"""
        if playing and self.frame_count <= 1:
            playing = False
        self.playing = playing
        self._throttled = False
        if playing:
            self._schedule()
        else:
            self._stop()
        self._update_play_button()
        self._schedule_render(0)

    # ---------------------------------------------------------------- #
    # 帧与渲染
    # ---------------------------------------------------------------- #
    def _start_load(self, record: dict) -> None:
        """后台线程分析帧信息（几千帧的 GIF 光读时长就会卡界面）。"""
        self._stop()
        self._load_token += 1
        token = self._load_token
        self.ready = False
        self.data = None
        self.frame_count = 0
        self.durations = [80]
        self.frame_index = 0
        self.playing = False
        self._throttled = False
        self.size = (0, 0)
        self._cache_key = None
        self._cache_image = None
        self._update_play_button()
        self._show_message("载入中…")

        def work() -> dict:
            data = images.Loader.load(record)
            info = images.describe_animation(data)
            info["data"] = data
            return info

        def done(info: dict) -> None:
            if token != self._load_token:
                return
            self.data = info.get("data")
            self.frame_count = int(info.get("frames") or 1)
            self.size = tuple(info.get("size") or (0, 0))
            self.durations = list(info.get("durations") or [80])
            self.ready = True
            self._render()
            if self.frame_count > 1:
                self._set_playing(True)      # 动图自动播放
            else:
                self._update_play_button()

        self.app._run_async(work, done, "载入图片失败")

    def _frame_image(self, index: int):
        """按需解码并缩放到当前显示尺寸。

        只缓存「显示尺寸」的那张图，解码出来的原图用完立即释放，
        因此内存占用与帧数、原图分辨率都无关。
        """
        if not self.data or self.frame_count <= 0:
            return None
        if self.fit_mode:
            self.scale = self._clamp_scale(self._fit_scale())
        key = (index % self.frame_count, round(self.scale, 4))
        if key == self._cache_key and self._cache_image is not None:
            return self._cache_image, self.scale
        frame = images.load_frame(self.data, index)
        if frame is None:
            return None
        target = (max(1, int(frame.width * self.scale)), max(1, int(frame.height * self.scale)))
        resample = RESAMPLE_PIXEL if self.scale >= 3 else (RESAMPLE_UP if self.scale > 1 else RESAMPLE_DOWN)
        rendered = frame if target == frame.size else frame.resize(target, resample)
        self._cache_key, self._cache_image = key, rendered
        return rendered, self.scale

    def _update_play_button(self) -> None:
        if not hasattr(self, "play_button"):
            return
        if not self.ready:
            text, state = "暂停", "disabled"
        elif self.frame_count <= 1:
            text, state = "单帧", "disabled"
        elif self.playing:
            text, state = "暂停", "normal"
        else:
            text, state = "播放", "normal"
        try:
            self.play_button.config(text=text, state=state)
        except tk.TclError:
            pass

    def _set_playing(self, playing: bool) -> None:
        """统一处理播放状态：定时器、按钮文字、画面提示。"""
        if playing and self.frame_count <= 1:
            playing = False
        self.playing = playing
        self._throttled = False
        if playing:
            self._schedule()
        else:
            self._stop()
        self._update_play_button()
        self._schedule_render(0)

    def _show_message(self, text: str) -> None:
        cw, ch = self._canvas_size()
        self.canvas.delete("all")
        self.canvas.create_text(cw / 2, ch / 2, text=text, fill=COLORS["mute"], font=FONT)

    def _canvas_size(self) -> tuple[int, int]:
        return max(self.canvas.winfo_width(), 80), max(self.canvas.winfo_height(), 80)

    def _image_size(self) -> tuple[int, int]:
        size = getattr(self, "size", None)
        if size and size[0]:
            return size
        return (1, 1)

    def _fit_scale(self) -> float:
        iw, ih = self._image_size()
        cw, ch = self._canvas_size()
        return max(MIN_ZOOM, min(cw / iw, ch / ih) * 0.94)

    def _clamp_scale(self, scale: float) -> float:
        scale = max(MIN_ZOOM, min(MAX_ZOOM, scale))
        iw, ih = self._image_size()
        while iw * ih * scale * scale > MAX_RENDER_PIXELS and scale > MIN_ZOOM:
            scale *= 0.85
        return scale

    def _schedule_render(self, delay: int = 16) -> None:
        if self._render_job:
            try:
                self.after_cancel(self._render_job)
            except Exception:
                pass
        self._render_job = self.after(delay, self._render)

    def _render(self) -> None:
        self._render_job = None
        if not self.ready or not self.active:
            return
        # 刚 pack 出来时画布还没完成布局，重试而不是直接放弃（否则会一直显示上一张图）
        if self.canvas.winfo_width() <= 1:
            self._schedule_render(30)
            return
        result = self._frame_image(self.frame_index)
        if result is None:
            return
        rendered, scale = result
        self._photo = ImageTk.PhotoImage(rendered)

        cw, ch = self._canvas_size()
        self.canvas.delete("all")
        cx = cw / 2 + self.pan_x
        cy = ch / 2 + self.pan_y
        self.canvas.create_image(cx, cy, image=self._photo)
        if self.frame_count > 1 and (not self.playing or self._throttled):
            hint = ("缩放中已暂停播放，缩小后自动继续" if self._throttled
                    else "动画已暂停（点「播放」或双击图片继续）")
            self.canvas.create_text(12, 12, anchor="nw", text=hint, fill=COLORS["mute"],
                                    font=FONT_SMALL)
        self.zoom_label.config(text=f"缩放 {scale * 100:.0f}%")

    def zoom_step(self, direction: int) -> None:
        if not self.ready:
            return
        base = self.scale if not self.fit_mode else self._fit_scale()
        factor = 1.25 if direction > 0 else 1 / 1.25
        self.scale = self._clamp_scale(base * factor)
        self.fit_mode = False
        self._schedule_render(0)
        self._schedule()          # 缩小回可播范围时自动继续播放

    def zoom_fit(self, keep_pan: bool = False) -> None:
        self.fit_mode = True
        if not keep_pan:
            self.pan_x = self.pan_y = 0.0
        self._schedule_render(0)
        self._schedule()

    def zoom_reset(self) -> None:
        self.fit_mode = False
        self.scale = self._clamp_scale(1.0)
        self.pan_x = self.pan_y = 0.0
        self._schedule_render(0)
        self._schedule()

    def _on_press(self, event) -> None:
        self._drag = (event.x, event.y)

    def _on_drag(self, event) -> None:
        if not self._drag:
            return
        dx = event.x - self._drag[0]
        dy = event.y - self._drag[1]
        self._drag = (event.x, event.y)
        self.pan_x += dx
        self.pan_y += dy
        self._schedule_render(0)

    def toggle_play(self) -> None:
        if self.frame_count <= 1:
            return
        self._set_playing(not self.playing)

    def _schedule(self) -> None:
        if self.frame_count <= 1 or not self.playing or not self.active:
            return
        iw, ih = self._image_size()
        if iw * ih * self.scale * self.scale > ANIMATE_PIXEL_LIMIT:
            # 放得太大时先不播（保证缩放/拖动流畅），缩小后会自动恢复
            self._throttled = True
            return
        self._throttled = False
        self._job = self.after(self.durations[self.frame_index % len(self.durations)], self._advance)

    def _advance(self) -> None:
        if self.frame_count <= 0:
            return
        self.frame_index = (self.frame_index + 1) % self.frame_count
        self._render()
        self._schedule()

    def _stop(self) -> None:
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def step(self, delta: int) -> None:
        """详情里切换上一个 / 下一个，跨页时自动翻页。"""
        target = self.app.current_index + delta
        if target < 0:
            if self.app.page > 1:
                self.app.goto_page(self.app.page - 1,
                                   on_done=lambda: self.show(len(self.app.items) - 1))
            return
        if target >= len(self.app.items):
            if self.app.page < self.app.pages:
                self.app.goto_page(self.app.page + 1, on_done=lambda: self.show(0))
            return
        self.show(target)

    # ---------------------------------------------------------------- #
    # 操作
    # ---------------------------------------------------------------- #
    def toggle_select(self) -> None:
        if self.record:
            self.app.toggle_selection(self.record["id"])
            self.app.refresh_selection_visual()
            self.select_button.config(
                text="取消选择" if self.app.is_selected(self.record["id"]) else "选择此表情"
            )

    def download(self) -> None:
        if self.record:
            self.app.download_item(self.record)

    def copy_path(self) -> None:
        if not self.record:
            return
        self.clipboard_clear()
        self.clipboard_append(self.record.get("path", ""))
        self.app.set_status("已复制存储路径")

    def reveal(self) -> None:
        if self.record:
            self.app.open_dir(Path(self.record["path"]).parent)


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        _enable_dpi_awareness()

        self.title(f"QQ 表情包提取器  v{__version__}")
        self.geometry("1320x840")
        self.minsize(1080, 680)
        self.configure(bg=COLORS["bg"])
        # 有图标文件就用上（标题栏 + 任务栏），没有也不影响运行
        for base in (config.BASE_DIR, Path(getattr(sys, "_MEIPASS", config.BASE_DIR))):
            icon = Path(base) / "assets" / "app.ico"
            if icon.is_file():
                try:
                    self.iconbitmap(default=str(icon))
                except tk.TclError:
                    pass
                break

        self.index = None
        self.detect_data: dict = {"accounts": [], "roots": [], "source_defs": []}
        self.account_vars: dict[str, tk.BooleanVar] = {}
        self.source_vars: dict[str, tk.BooleanVar] = {}
        self.extra_paths: list[str] = []

        self.items: list[dict] = []          # 当前页的记录
        self.total = 0                        # 当前筛选条件下的总条数
        self.page = 1
        self.pages = 1
        self.page_size = DEFAULT_PAGE_SIZE
        self.fetching = False
        self.selected: set[str] = set()
        self.select_all_filtered = False
        self.anchor_index: int | None = None
        self._last_click_index: int | None = None
        self.current_index = 0

        self.cells: dict[int, dict] = {}
        self.thumbs: dict[str, ImageTk.PhotoImage] = {}
        self.failed_thumbs: set[str] = set()
        self.hover_index: int | None = None
        self._scrollregion: tuple | None = None
        self._render_signature: tuple | None = None
        self.active_tasks: dict[str, dict] = {}
        self._search_job: str | None = None
        self._filter_job: str | None = None
        self._grid_job: str | None = None
        self._load_token = 0
        self.loader = ThumbLoader()

        self._init_style()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind("<Control-a>", lambda e: self.select_all())
        self.bind("<Control-A>", lambda e: self.select_all())
        self.bind("<F5>", lambda e: self.start_scan())
        self.bind("<Control-e>", lambda e: self.open_export())
        self.bind("<space>", lambda e: self.toggle_current_card())
        self.bind("<Insert>", lambda e: self.toggle_current_card())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(120, self._drain_thumb_queue)
        self.after(150, self._poll_tasks)
        self.after(60, self.bootstrap)
        # 标题栏与界面统一成深色（窗口映射后再补一次，确保生效）
        self.bind("<Map>", lambda e: apply_window_theme(self), add="+")
        self.after(30, lambda: apply_window_theme(self))
        self.after(700, lambda: apply_window_theme(self))

    # ------------------------------------------------------------------ #
    # 样式
    # ------------------------------------------------------------------ #
    def _init_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=FONT)
        style.configure("TCheckbutton", background=COLORS["bg2"], foreground=COLORS["text"], font=FONT_SMALL)
        style.map("TCheckbutton", background=[("active", COLORS["bg2"])],
                  foreground=[("disabled", COLORS["mute"])])
        style.configure("TCombobox", fieldbackground=COLORS["bg"], background=COLORS["bg3"],
                        foreground=COLORS["text"], arrowcolor=COLORS["dim"],
                        bordercolor=COLORS["border"], lightcolor=COLORS["bg3"],
                        darkcolor=COLORS["bg3"], insertcolor=COLORS["text"], font=FONT_SMALL)
        # clam 主题下只设 configure 不够：只读状态下文字/底色由 map 决定，必须显式指定，
        # 否则会出现「灰底白字」看不清的情况
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COLORS["bg"]), ("disabled", COLORS["bg2"]),
                             ("!disabled", COLORS["bg"])],
            foreground=[("readonly", COLORS["text"]), ("disabled", COLORS["mute"]),
                        ("!disabled", COLORS["text"])],
            selectbackground=[("readonly", COLORS["bg"]), ("!disabled", COLORS["primary"])],
            selectforeground=[("readonly", COLORS["text"]), ("!disabled", "#ffffff")],
            background=[("readonly", COLORS["bg3"]), ("active", COLORS["bg_hover"]),
                        ("!disabled", COLORS["bg3"])],
            arrowcolor=[("readonly", COLORS["dim"]), ("!disabled", COLORS["text"])],
            bordercolor=[("focus", COLORS["primary"]), ("!disabled", COLORS["border"])],
        )
        style.configure("TProgressbar", background=COLORS["primary"], troughcolor=COLORS["bg3"],
                        bordercolor=COLORS["bg3"], lightcolor=COLORS["primary"],
                        darkcolor=COLORS["primary"])
        style.configure("Vertical.TScrollbar", background=COLORS["bg3"], troughcolor=COLORS["bg"],
                        arrowcolor=COLORS["dim"], bordercolor=COLORS["bg"], width=12)
        self.option_add("*TCombobox*Listbox.background", COLORS["bg3"])
        self.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", COLORS["primary"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.borderWidth", 0)
        # Entry / Spinbox 的选中色也统一成主题色
        self.option_add("*Entry.selectBackground", COLORS["primary"])
        self.option_add("*Entry.selectForeground", "#ffffff")
        self.option_add("*Spinbox.selectBackground", COLORS["primary"])
        self.option_add("*Spinbox.selectForeground", "#ffffff")

    def _button(self, parent, text, command, kind: str = "normal"):
        palette = {
            "normal": (COLORS["bg3"], COLORS["text"]),
            "primary": (COLORS["primary"], "#ffffff"),
            "accent": (COLORS["accent"], "#ffffff"),
        }[kind]
        return tk.Button(parent, text=text, command=command, bg=palette[0], fg=palette[1],
                         activebackground=COLORS["bg_hover"], activeforeground=COLORS["text"],
                         relief="flat", bd=0, padx=13, pady=7, font=FONT, cursor="hand2",
                         disabledforeground=COLORS["mute"])

    def _small_button(self, parent, text, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=COLORS["bg3"], fg=COLORS["dim"],
                         activebackground=COLORS["bg_hover"], activeforeground=COLORS["text"],
                         relief="flat", bd=0, padx=9, pady=3, font=FONT_SMALL, cursor="hand2")

    # ------------------------------------------------------------------ #
    # 菜单（自绘深色菜单栏：Windows 的原生菜单栏会是一整条浅灰，和界面不搭）
    # ------------------------------------------------------------------ #
    def _dark_menu(self, parent) -> tk.Menu:
        """统一风格的弹出菜单。"""
        return tk.Menu(parent, tearoff=0, bg=COLORS["bg3"], fg=COLORS["text"],
                       activebackground=COLORS["primary"], activeforeground="#ffffff",
                       disabledforeground=COLORS["mute"], bd=0, relief="flat",
                       activeborderwidth=0, font=FONT_SMALL)

    def _build_menu(self) -> None:
        bar = tk.Frame(self, bg=COLORS["bg2"], height=30)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        self.menubar = bar

        definitions: list[tuple[str, list]] = [
            ("文件", [
                ("添加数据文件夹…", self.add_folder),
                ("重新检测 QQ 目录", lambda: self.detect_paths(False)),
                ("深度检测（扫描磁盘，较慢）", lambda: self.detect_paths(True)),
                None,
                ("打开缓存目录", lambda: self.open_dir(config.CACHE_DIR)),
                ("打开导出目录", lambda: self.open_dir(config.EXPORT_DIR)),
                None,
                ("退出", self._on_close),
            ]),
            ("扫描", [
                ("开始扫描", self.start_scan),
                ("重建索引（清空后重扫）", lambda: self.start_scan(rebuild=True)),
                None,
                ("统计信息", self.show_stats),
            ]),
            ("导出", [
                ("导出所选…", self.open_export),
                ("导出当前筛选全部…", self.export_all_filtered),
            ]),
            ("帮助", [
                ("使用说明", self.show_help),
                ("关于", self.show_about),
            ]),
        ]

        self.menu_buttons: list[tk.Menubutton] = []
        for label, items in definitions:
            button = tk.Menubutton(bar, text=label, bg=COLORS["bg2"], fg=COLORS["text"],
                                   activebackground=COLORS["bg_hover"],
                                   activeforeground=COLORS["text"], relief="flat", bd=0,
                                   padx=12, pady=3, font=FONT_SMALL, cursor="hand2")
            menu = self._dark_menu(button)
            for item in items:
                if item is None:
                    menu.add_separator()
                else:
                    menu.add_command(label=item[0], command=item[1])
            button.config(menu=menu)
            button.pack(side="left")
            self.menu_buttons.append(button)

    # ------------------------------------------------------------------ #
    # 工具栏
    # ------------------------------------------------------------------ #
    def _build_toolbar(self) -> None:
        bar = tk.Frame(self, bg=COLORS["bg2"], height=60)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=COLORS["bg2"])
        left.pack(side="left", padx=16)
        titles = tk.Frame(left, bg=COLORS["bg2"])
        titles.pack(side="left")
        tk.Label(titles, text="QQ 表情包提取器", bg=COLORS["bg2"], fg=COLORS["text"],
                 font=FONT_TITLE).pack(anchor="w")
        self.path_hint = tk.Label(titles, text="正在检测 QQ 数据目录…", bg=COLORS["bg2"],
                                  fg=COLORS["mute"], font=FONT_SMALL, anchor="w")
        self.path_hint.pack(anchor="w")

        right = tk.Frame(bar, bg=COLORS["bg2"])
        right.pack(side="right", padx=16)

        self.scan_button = self._button(right, "开始扫描", self.start_scan, kind="primary")
        self.scan_button.pack(side="right", padx=(8, 0))

        search_box = tk.Frame(right, bg=COLORS["bg"], highlightthickness=1,
                              highlightbackground=COLORS["border"])
        search_box.pack(side="right", padx=(0, 8))
        tk.Label(search_box, text="搜索", bg=COLORS["bg"], fg=COLORS["mute"],
                 font=FONT_SMALL).pack(side="left", padx=(9, 4))
        self.search_var = tk.StringVar()
        entry = tk.Entry(search_box, textvariable=self.search_var, bg=COLORS["bg"], fg=COLORS["text"],
                         insertbackground=COLORS["text"], relief="flat", width=28)
        entry.pack(side="left", padx=(0, 6), ipady=6)
        entry.bind("<Return>", lambda e: self.reload(reset_page=True))
        self.search_var.trace_add("write", lambda *_: self._on_search_changed())
        self.search_clear = tk.Button(search_box, text="清空", command=lambda: self.search_var.set(""),
                                      bg=COLORS["bg"], fg=COLORS["dim"], relief="flat", bd=0,
                                      activebackground=COLORS["bg"], activeforeground=COLORS["text"],
                                      font=FONT_SMALL, cursor="hand2")
        self.search_clear.pack(side="left", padx=(0, 8))

        self.export_button = self._button(right, "导出", self.open_export, kind="accent")
        self.export_button.config(state="disabled")
        self.export_button.pack(side="right", padx=(0, 8))

    # ------------------------------------------------------------------ #
    # 主体
    # ------------------------------------------------------------------ #
    def _build_body(self) -> None:
        body = tk.Frame(self, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(body, bg=COLORS["bg2"], width=280)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.main = tk.Frame(body, bg=COLORS["bg"])
        self.main.pack(side="left", fill="both", expand=True)

        # ---------------- 侧边栏（可滚动） ---------------- #
        side_wrap = tk.Frame(self.sidebar, bg=COLORS["bg2"])
        side_wrap.pack(fill="both", expand=True)
        self.sidebar_canvas = tk.Canvas(side_wrap, bg=COLORS["bg2"], highlightthickness=0,
                                        width=262, yscrollincrement=26)
        self.sidebar_canvas.pack(side="left", fill="both", expand=True)
        side_bar = ttk.Scrollbar(side_wrap, orient="vertical", command=self.sidebar_canvas.yview)
        side_bar.pack(side="right", fill="y")
        self.sidebar_canvas.configure(yscrollcommand=side_bar.set)
        self.sidebar_inner = tk.Frame(self.sidebar_canvas, bg=COLORS["bg2"])
        self.sidebar_window = self.sidebar_canvas.create_window(
            (0, 0), window=self.sidebar_inner, anchor="nw", width=258
        )
        self.sidebar_inner.bind(
            "<Configure>",
            lambda e: self.sidebar_canvas.configure(scrollregion=self.sidebar_canvas.bbox("all")),
        )

        self._panel_accounts(self.sidebar_inner)
        self._panel_sources(self.sidebar_inner)
        self._panel_filters(self.sidebar_inner)
        self._panel_cache(self.sidebar_inner)

        # ---------------- 统计条 ---------------- #
        self.info_bar = tk.Frame(self.main, bg=COLORS["bg2"], height=INFO_H)
        self.info_bar.pack(fill="x", side="top")
        self.info_bar.pack_propagate(False)
        self.stats_label = tk.Label(self.info_bar, text="尚未建立索引", bg=COLORS["bg2"],
                                    fg=COLORS["dim"], font=FONT_SMALL)
        self.stats_label.pack(side="left", padx=16)
        self.selection_label = tk.Label(self.info_bar, text="", bg=COLORS["bg2"],
                                        fg=COLORS["accent"], font=FONT_SMALL)
        self.selection_label.pack(side="left", padx=10)
        actions = tk.Frame(self.info_bar, bg=COLORS["bg2"])
        actions.pack(side="right", padx=12)
        self._small_button(actions, "查看详情", self.open_detail_for_current).pack(side="left")
        self._small_button(actions, "选择筛选全部", self.select_all).pack(side="left", padx=6)
        self._small_button(actions, "清空选择", self.clear_selection).pack(side="left")
        self._small_button(actions, "导出所选", self.open_export).pack(side="left", padx=6)

        # ---------------- 图库 / 详情 容器 ---------------- #
        self.stack = tk.Frame(self.main, bg=COLORS["bg"])
        self.stack.pack(fill="both", expand=True)

        self.grid_wrap = tk.Frame(self.stack, bg=COLORS["bg"])
        self.grid_wrap.pack(fill="both", expand=True)
        # 必须先把底部翻页栏 pack 好，否则会被下面带 expand 的画布吃掉全部空间
        self._build_pager()
        self.canvas = tk.Canvas(self.grid_wrap, bg=COLORS["bg"], highlightthickness=0,
                                yscrollincrement=40)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar = ttk.Scrollbar(self.grid_wrap, orient="vertical", command=self.canvas.yview)
        self.vbar.pack(side="right", fill="y")
        # 滚动位置一变就重绘可见区域（否则滚下去只剩空白，点击会落到错误的卡片上）
        self.canvas.configure(yscrollcommand=self._on_canvas_scroll)
        self.canvas.bind("<Configure>", lambda e: self.render_grid(force=True))
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Escape>", self._on_escape)
        self.bind("<Return>", lambda e: self.open_detail_for_current())
        for _key in ("<Left>", "<Right>", "<plus>", "<equal>", "<minus>", "<Key-0>", "<Key-1>"):
            self.bind(_key, self._on_detail_key)

        self.detail = DetailView(self.stack, self)

        self._context_menu = self._dark_menu(self)
        self._context_menu.add_command(label="查看详情", command=self._ctx_preview)
        self._context_menu.add_command(label="选择 / 取消选择", command=self._ctx_toggle)
        self._context_menu.add_command(label="只选这一张", command=self._ctx_select_only)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="下载这个表情", command=self._ctx_download)
        self._context_menu.add_command(label="复制存储路径", command=self._ctx_copy_path)
        self._context_menu.add_command(label="在资源管理器中显示", command=self._ctx_reveal)
        self._context_index: int | None = None

    # ---------------- 底部翻页栏 ---------------- #
    def _build_pager(self) -> None:
        bar = tk.Frame(self.grid_wrap, bg=COLORS["bg2"], height=44)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        self.page_info = tk.Label(bar, text="", bg=COLORS["bg2"], fg=COLORS["dim"], font=FONT_SMALL)
        self.page_info.pack(side="left", padx=14)

        right = tk.Frame(bar, bg=COLORS["bg2"])
        right.pack(side="right", padx=12)

        self.page_last_btn = self._pager_button(right, "末页", lambda: self.goto_page(self.pages))
        self.page_next_btn = self._pager_button(right, "下一页", lambda: self.goto_page(self.page + 1))
        self.page_entry = tk.Entry(right, width=5, bg=COLORS["bg"], fg=COLORS["text"],
                                   insertbackground=COLORS["text"], relief="flat",
                                   justify="center", font=FONT_SMALL)
        self.page_entry.pack(side="right", ipady=4, padx=4)
        self.page_entry.bind("<Return>", lambda e: self._jump_to_entry())
        tk.Label(right, text="第", bg=COLORS["bg2"], fg=COLORS["dim"],
                 font=FONT_SMALL).pack(side="right")
        tk.Label(right, text="页", bg=COLORS["bg2"], fg=COLORS["dim"],
                 font=FONT_SMALL).pack(side="right")
        self.page_prev_btn = self._pager_button(right, "上一页", lambda: self.goto_page(self.page - 1))
        self.page_first_btn = self._pager_button(right, "首页", lambda: self.goto_page(1))

        size_box = tk.Frame(bar, bg=COLORS["bg2"])
        size_box.pack(side="right", padx=(0, 18))
        tk.Label(size_box, text="每页", bg=COLORS["bg2"], fg=COLORS["dim"],
                 font=FONT_SMALL).pack(side="left", padx=(0, 6))
        self.page_size_var = tk.StringVar(value=str(DEFAULT_PAGE_SIZE))
        size_combo = ttk.Combobox(size_box, textvariable=self.page_size_var, state="readonly",
                                  width=5, font=FONT_SMALL,
                                  values=[str(n) for n in PAGE_SIZE_OPTIONS])
        size_combo.pack(side="left")
        size_combo.bind("<<ComboboxSelected>>", lambda e: self._change_page_size())

    def _pager_button(self, parent, text: str, command) -> tk.Button:
        btn = tk.Button(parent, text=text, command=command, bg=COLORS["bg3"], fg=COLORS["text"],
                        activebackground=COLORS["bg_hover"], activeforeground=COLORS["text"],
                        relief="flat", bd=0, padx=10, pady=4, font=FONT_SMALL, cursor="hand2",
                        disabledforeground=COLORS["mute"])
        btn.pack(side="right", padx=3)
        return btn

    def _jump_to_entry(self) -> None:
        target = _safe_int(self.page_entry.get()) or 1
        self.goto_page(target)
        self.canvas.focus_set()

    def _change_page_size(self) -> None:
        try:
            size = int(self.page_size_var.get())
        except ValueError:
            return
        if size == self.page_size:
            return
        # 保持当前浏览位置：换算成第几条
        offset = (self.page - 1) * self.page_size
        self.page_size = size
        self.goto_page(offset // size + 1)

    def _update_pager(self) -> None:
        self.page_info.config(
            text=(f"共 {self.total} 个表情　·　第 {self.page} / {self.pages} 页　·　"
                  f"本页 {len(self.items)} 个")
        )
        if str(self.page_entry.get()) != str(self.page):
            self.page_entry.delete(0, "end")
            self.page_entry.insert(0, str(self.page))
        for button, enabled in (
            (self.page_first_btn, self.page > 1),
            (self.page_prev_btn, self.page > 1),
            (self.page_next_btn, self.page < self.pages),
            (self.page_last_btn, self.page < self.pages),
        ):
            button.config(state="normal" if enabled and not self.fetching else "disabled")

    def _section(self, parent, title: str) -> tk.Frame:
        holder = tk.Frame(parent, bg=COLORS["bg3"], highlightthickness=1,
                          highlightbackground=COLORS["border"])
        holder.pack(fill="x", padx=12, pady=(12, 0))
        tk.Label(holder, text=title, bg=COLORS["bg3"], fg=COLORS["dim"], font=FONT_SMALL,
                 anchor="w").pack(fill="x", padx=10, pady=(8, 4))
        content = tk.Frame(holder, bg=COLORS["bg3"])
        content.pack(fill="x", padx=10, pady=(0, 10))
        return content

    # ---------------- 侧边栏各面板 ---------------- #
    def _panel_accounts(self, parent) -> None:
        content = self._section(parent, "数据目录")
        self.account_box = tk.Frame(content, bg=COLORS["bg3"])
        self.account_box.pack(fill="x")
        tk.Label(self.account_box, text="检测中…", bg=COLORS["bg3"], fg=COLORS["mute"],
                 font=FONT_SMALL).pack(anchor="w")

        row = tk.Frame(content, bg=COLORS["bg3"])
        row.pack(fill="x", pady=(8, 0))
        self._small_button(row, "添加文件夹…", self.add_folder).pack(side="left")
        self._small_button(row, "重新检测", lambda: self.detect_paths(False)).pack(side="left", padx=6)

        self.manual_box = tk.Frame(content, bg=COLORS["bg3"])
        self.manual_box.pack(fill="x")

    def _panel_sources(self, parent) -> None:
        content = self._section(parent, "表情来源")
        tk.Label(content, text="勾选后立即筛选图库；扫描时也按这里的选择执行",
                 bg=COLORS["bg3"], fg=COLORS["mute"], font=FONT_SMALL, anchor="w",
                 wraplength=210, justify="left").pack(fill="x", pady=(0, 4))
        self.source_box = tk.Frame(content, bg=COLORS["bg3"])
        self.source_box.pack(fill="x")

    def _panel_filters(self, parent) -> None:
        content = self._section(parent, "筛选")

        ext_row = tk.Frame(content, bg=COLORS["bg3"])
        ext_row.pack(fill="x")
        self.ext_vars: dict[str, tk.BooleanVar] = {}
        for ext in (".gif", ".png", ".jpg", ".webp"):
            var = tk.BooleanVar(value=False)
            var.trace_add("write", lambda *_: self._apply_filters_now())
            self.ext_vars[ext] = var
            tk.Checkbutton(ext_row, text=ext.replace(".", "").upper(), variable=var,
                           command=self._apply_filters_now, bg=COLORS["bg3"],
                           fg=COLORS["text"], selectcolor=COLORS["bg"], activebackground=COLORS["bg3"],
                           activeforeground=COLORS["text"], font=FONT_SMALL,
                           cursor="hand2").pack(side="left", padx=(0, 6))

        self.animated_var = tk.BooleanVar(value=False)
        self.animated_var.trace_add("write", lambda *_: self._apply_filters_now())
        tk.Checkbutton(content, text="只看动图", variable=self.animated_var,
                       command=self._apply_filters_now, bg=COLORS["bg3"],
                       fg=COLORS["text"], selectcolor=COLORS["bg"], activebackground=COLORS["bg3"],
                       activeforeground=COLORS["text"], font=FONT_SMALL, cursor="hand2").pack(
            anchor="w", pady=(6, 0))

        size_row = tk.Frame(content, bg=COLORS["bg3"])
        size_row.pack(fill="x", pady=(6, 0))
        tk.Label(size_row, text="最小边长", bg=COLORS["bg3"], fg=COLORS["dim"],
                 font=FONT_SMALL).pack(side="left")
        self.min_width_var = tk.StringVar(value="0")
        self.min_width_var.trace_add("write", lambda *_: self._schedule_filter())
        spin = tk.Spinbox(size_row, from_=0, to=4096, increment=16, width=7,
                          textvariable=self.min_width_var, bg=COLORS["bg"], fg=COLORS["text"],
                          insertbackground=COLORS["text"], relief="flat", font=FONT_SMALL,
                          buttonbackground=COLORS["bg3"], command=self._apply_filters_now)
        spin.pack(side="left", padx=8)

        pack_row = tk.Frame(content, bg=COLORS["bg3"])
        pack_row.pack(fill="x", pady=(8, 0))
        tk.Label(pack_row, text="表情包", bg=COLORS["bg3"], fg=COLORS["dim"],
                 font=FONT_SMALL).pack(anchor="w")
        self.pack_var = tk.StringVar(value="全部表情包")
        self.pack_combo = ttk.Combobox(pack_row, textvariable=self.pack_var, state="readonly",
                                       font=FONT_SMALL)
        self.pack_combo.pack(fill="x", pady=(3, 0))
        self.pack_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filters_now())

        sort_row = tk.Frame(content, bg=COLORS["bg3"])
        sort_row.pack(fill="x", pady=(8, 0))
        tk.Label(sort_row, text="排序", bg=COLORS["bg3"], fg=COLORS["dim"],
                 font=FONT_SMALL).pack(anchor="w")
        self.sort_var = tk.StringVar(value=SORT_OPTIONS[0][0])
        sort_combo = ttk.Combobox(sort_row, textvariable=self.sort_var, state="readonly",
                                  values=[t for t, _ in SORT_OPTIONS], font=FONT_SMALL)
        sort_combo.pack(fill="x", pady=(3, 0))
        sort_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filters_now())

        self._small_button(content, "重置筛选", self.reset_filters).pack(fill="x", pady=(10, 0))

    def _panel_cache(self, parent) -> None:
        content = self._section(parent, "缓存")
        tk.Label(content, text=str(config.CACHE_DIR), bg=COLORS["bg3"], fg=COLORS["mute"],
                 font=("Consolas", 8), wraplength=210, justify="left", anchor="w").pack(fill="x")
        row = tk.Frame(content, bg=COLORS["bg3"])
        row.pack(fill="x", pady=(8, 0))
        self._small_button(row, "打开缓存目录",
                           lambda: self.open_dir(config.CACHE_DIR)).pack(side="left")
        self._small_button(row, "打开导出目录",
                           lambda: self.open_dir(config.EXPORT_DIR)).pack(side="left", padx=6)

    # ------------------------------------------------------------------ #
    # 状态栏
    # ------------------------------------------------------------------ #
    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=COLORS["bg2"], height=40)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_label = tk.Label(bar, text="就绪", bg=COLORS["bg2"], fg=COLORS["dim"],
                                     font=FONT_SMALL, anchor="w")
        self.status_label.pack(side="left", padx=14)
        self.progress = ttk.Progressbar(bar, mode="determinate", length=240)
        self.cancel_button = tk.Button(bar, text="取消任务", command=self.cancel_active,
                                       bg=COLORS["bg3"], fg=COLORS["text"], relief="flat", bd=0,
                                       padx=11, pady=4, font=FONT_SMALL, cursor="hand2")

    def set_status(self, text: str) -> None:
        self.status_label.config(text=text)

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> None:
        from . import store

        self.index = store.get_index()
        self._update_stats_line()
        # 先等路径检测完成再加载列表，避免首屏用「还没有筛选条件」的状态取数
        self.detect_paths(False)

    def _after_detect(self) -> None:
        """路径检测完成后：刷新表情包下拉并加载第一页。"""
        self.refresh_pack_options()
        self.reload(reset_page=True)
        stats = self.index.stats()
        self._update_stats_line()
        if not stats["total"]:
            self.set_status("还没有索引任何表情，点击右上角「开始扫描」")
        else:
            self.set_status(f"已载入索引（{stats['scanned_at'] or '未知时间'}）")

    # ------------------------------------------------------------------ #
    # 路径检测
    # ------------------------------------------------------------------ #
    def add_folder(self) -> None:
        path = filedialog.askdirectory(
            title="选择 QQ 数据文件夹（Tencent Files，或其中某个 QQ 号文件夹）", parent=self
        )
        if not path:
            return
        path = str(Path(path))
        if path not in self.extra_paths:
            self.extra_paths.append(path)
        self.detect_paths(False)

    def remove_extra_path(self, path: str) -> None:
        if path in self.extra_paths:
            self.extra_paths.remove(path)
        self.detect_paths(False)

    def detect_paths(self, deep: bool, on_done=None) -> None:
        self.path_hint.config(text="正在检测 QQ 数据目录…")
        self.update_idletasks()
        callback = on_done or self._after_detect

        def work() -> dict:
            return detect.detect_table(deep=deep, extra_paths=self.extra_paths)

        def done(result: dict) -> None:
            self.detect_data = result
            accounts = result.get("accounts") or []
            previous = {p: v.get() for p, v in self.account_vars.items()}
            self.account_vars = {}
            for account in accounts:
                var = tk.BooleanVar(value=previous.get(account["path"], True))
                # 勾选变化立即筛选图库（同时保留 Checkbutton 的 command）
                var.trace_add("write", lambda *_: self._apply_filters_now())
                self.account_vars[account["path"]] = var
            self._render_accounts()
            self._render_manual_paths()
            self._render_sources()
            if accounts:
                self.path_hint.config(
                    text=f"已检测到 {len(accounts)} 个 QQ 账号目录 · {accounts[0]['path']}",
                    fg=COLORS["mute"],
                )
            else:
                self.path_hint.config(text="未检测到 QQ 数据目录，可用左侧「添加文件夹…」指定",
                                      fg=COLORS["accent"])
            callback()

        self._run_async(work, done, "路径检测失败")

    @property
    def pool(self) -> ThreadPoolExecutor:
        pool = getattr(self, "_pool", None)
        if pool is None:
            pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ui")
            self._pool = pool
        return pool

    def _run_async(self, work, done, error_title: str = "操作失败") -> None:
        """在后台线程执行 work()，完成后把结果交给主线程 done()。"""
        box: queue.Queue = queue.Queue()

        def runner() -> None:
            try:
                box.put(("ok", work()))
            except Exception as exc:  # noqa: BLE001
                box.put(("err", exc))

        self.pool.submit(runner)

        def poll() -> None:
            try:
                kind, payload = box.get_nowait()
            except queue.Empty:
                self.after(60, poll)
                return
            if kind == "ok":
                try:
                    done(payload)
                except Exception as exc:  # noqa: BLE001
                    messagebox.showerror(error_title, f"{type(exc).__name__}: {exc}")
            else:
                messagebox.showerror(error_title, f"{type(payload).__name__}: {payload}")

        self.after(60, poll)

    def _render_accounts(self) -> None:
        for child in self.account_box.winfo_children():
            child.destroy()
        accounts = self.detect_data.get("accounts") or []
        if not accounts:
            tk.Label(self.account_box, text="未检测到账号目录，可用下面的「添加文件夹…」指定。",
                     bg=COLORS["bg3"], fg=COLORS["mute"], font=FONT_SMALL,
                     wraplength=210, justify="left").pack(anchor="w")
            return
        for account in accounts:
            row = tk.Frame(self.account_box, bg=COLORS["bg3"])
            row.pack(fill="x", pady=1)
            var = self.account_vars[account["path"]]
            tk.Checkbutton(row, variable=var, command=self._on_account_toggle, bg=COLORS["bg3"],
                           fg=COLORS["text"], selectcolor=COLORS["bg"], activebackground=COLORS["bg3"],
                           activeforeground=COLORS["text"], font=FONT_SMALL,
                           cursor="hand2").pack(side="left")
            text = tk.Frame(row, bg=COLORS["bg3"])
            text.pack(side="left", fill="x", expand=True)
            kinds = []
            if account.get("has_ntqq"):
                kinds.append("NTQQ")
            if account.get("has_legacy"):
                kinds.append("旧版")
            tk.Label(text, text=f"{account['uin']}（{'/'.join(kinds) or '未知版本'}）", bg=COLORS["bg3"],
                     fg=COLORS["text"], font=FONT_SMALL, anchor="w").pack(fill="x")
            tk.Label(text, text=f"约 {_human_size(account.get('size_bytes') or 0)}", bg=COLORS["bg3"],
                     fg=COLORS["mute"], font=("Consolas", 8), anchor="w").pack(fill="x")

    def _render_manual_paths(self) -> None:
        for child in self.manual_box.winfo_children():
            child.destroy()
        for path in self.extra_paths:
            row = tk.Frame(self.manual_box, bg=COLORS["bg3"])
            row.pack(fill="x", pady=1)
            tk.Label(row, text=_short(path, 26), bg=COLORS["bg3"], fg=COLORS["mute"],
                     font=("Consolas", 8), anchor="w").pack(side="left", fill="x", expand=True)
            self._small_button(row, "移除",
                               lambda p=path: self.remove_extra_path(p)).pack(side="right")

    def _render_sources(self) -> None:
        for child in self.source_box.winfo_children():
            child.destroy()
        definitions = self.detect_data.get("source_defs") or [
            {"key": k, "label": v[0], "desc": v[1], "default": v[2]} for k, v in config.SOURCES.items()
        ]
        stats = {}
        if self.index:
            try:
                stats = {row["source"]: row["count"] for row in self.index.stats()["by_source"]}
            except Exception:
                stats = {}
        for definition in definitions:
            key = definition["key"]
            var = self.source_vars.get(key)
            if var is None:
                var = tk.BooleanVar(value=definition.get("default", True))
                # 来源勾选变化立即筛选（trace 保证程序化修改也生效）
                var.trace_add("write", lambda *_: self._apply_filters_now())
                self.source_vars[key] = var
            count = stats.get(key)
            text = definition["label"] if count is None else f"{definition['label']}（{count}）"
            tk.Checkbutton(self.source_box, text=text, variable=var,
                           command=self._apply_filters_now, bg=COLORS["bg3"], fg=COLORS["text"],
                           selectcolor=COLORS["bg"], activebackground=COLORS["bg3"],
                           activeforeground=COLORS["text"], font=FONT_SMALL, cursor="hand2",
                           anchor="w").pack(fill="x")

    def _on_account_toggle(self) -> None:
        self._apply_filters_now()

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    def start_scan(self, rebuild: bool = False) -> None:
        accounts = [p for p, var in self.account_vars.items() if var.get()]
        if not accounts:
            messagebox.showwarning("提示", "请先至少勾选一个 QQ 账号目录")
            return
        sources = [k for k, var in self.source_vars.items() if var.get()]
        if not sources:
            messagebox.showwarning("提示", "请至少勾选一个表情来源")
            return
        existing = tasks.manager.latest("scan")
        if existing and existing.state in ("pending", "running"):
            if not messagebox.askyesno("扫描进行中", "已有扫描任务在运行，是否重新开始？"):
                return
        if rebuild:
            self.index.clear_all()

        task = tasks.manager.submit(
            "scan",
            f"扫描 {len(accounts)} 个账号",
            lambda t: jobs_mod.scan(t, accounts, sources),
            dedupe_key="scan",
            replace=True,
        )
        self._watch_task(task, "正在扫描", on_done=self._after_scan)
        self.set_status("正在扫描…")

    def _after_scan(self, result: dict) -> None:
        total = result.get("total", 0)
        self.refresh_pack_options()
        self._render_sources()
        self.reload(reset_page=True)
        self.set_status(f"扫描完成：共索引 {total} 个表情")
        stats = self.index.stats()
        if stats["total"]:
            messagebox.showinfo(
                "扫描完成",
                f"本次索引 {total} 个表情，索引库共 {stats['total']} 个。\n"
                f"其中动图 {stats['animated']} 个，表情包 {stats['packs']} 个。",
            )

    # ------------------------------------------------------------------ #
    # 任务进度
    # ------------------------------------------------------------------ #
    def _watch_task(self, task: tasks.Task, title: str, on_done=None) -> None:
        self.active_tasks[task.id] = {"task": task, "title": title, "on_done": on_done}
        self.progress.pack(side="right", padx=12)
        self.cancel_button.pack(side="right")
        if task.kind == "scan":
            self.progress.config(mode="indeterminate")
            self.progress.start(60)
        else:
            self.progress.config(mode="determinate", value=0)

    def _poll_tasks(self) -> None:
        finished: list[str] = []
        for task_id, entry in list(self.active_tasks.items()):
            task: tasks.Task = entry["task"]
            info = task.to_dict()
            if task.state in ("done", "error", "cancelled"):
                finished.append(task_id)
                continue
            if task.kind == "export" and info["total"]:
                self.progress.config(mode="determinate", value=info["percent"])
            text = " · ".join(str(x) for x in (entry["title"], info["stage"], info["message"]) if x)
            if info["current"]:
                text += f"（{info['current']}）"
            self.set_status(text)

        for task_id in finished:
            entry = self.active_tasks.pop(task_id)
            task = entry["task"]
            if task.state == "done":
                self.set_status("完成")
                if entry["on_done"]:
                    try:
                        entry["on_done"](task.result)
                    except Exception as exc:  # noqa: BLE001
                        messagebox.showerror("处理结果失败", f"{type(exc).__name__}: {exc}")
                if task.kind == "export":
                    self._show_export_result(task.result)
            elif task.state == "error":
                self.set_status("任务失败")
                messagebox.showerror("任务失败", task.error or "未知错误")
            else:
                self.set_status("任务已取消")

        if not self.active_tasks:
            try:
                self.progress.stop()
            except Exception:
                pass
            self.progress.pack_forget()
            self.cancel_button.pack_forget()
        self.after(150, self._poll_tasks)

    def cancel_active(self) -> None:
        for entry in self.active_tasks.values():
            entry["task"].cancel()
        self.set_status("正在取消…")

    def _show_export_result(self, result: dict) -> None:
        written = result.get("written", 0)
        skipped = result.get("skipped", 0)
        failed = result.get("failed", 0)
        if result.get("zip_path"):
            message = (f"已导出 {written} 个表情到：\n{result['zip_path']}\n\n"
                       f"（{_human_size(result.get('zip_size') or 0)}，跳过重复 {skipped}，失败 {failed}）")
            if messagebox.askyesno("导出完成", message + "\n\n是否打开导出目录？"):
                self.open_dir(config.EXPORT_DIR)
        else:
            message = (f"已导出 {written} 个表情到：\n{result.get('target_dir', '')}\n\n"
                       f"（跳过重复 {skipped}，失败 {failed}）")
            if messagebox.askyesno("导出完成", message + "\n\n是否打开该目录？"):
                self.open_dir(result.get("target_dir") or str(config.EXPORT_DIR))
        if failed and result.get("errors"):
            messagebox.showwarning("部分文件失败", "\n".join(result["errors"][:6]))

    # ------------------------------------------------------------------ #
    # 图库 / 详情 切换
    # ------------------------------------------------------------------ #
    def switch_to_detail(self) -> None:
        self.current_index = max(0, self.current_index)
        self.grid_wrap.pack_forget()
        self.detail.pack(fill="both", expand=True)
        self.detail.active = True
        self.detail.focus_set()
        self.detail.update_idletasks()
        self.detail._schedule_render(0)

    def switch_to_gallery(self) -> None:
        self.detail.active = False
        self.detail.pack_forget()
        self.grid_wrap.pack(fill="both", expand=True)
        self.render_grid(force=True)

    def _on_escape(self) -> None:
        if self.detail.winfo_ismapped():
            self.detail.close()
        else:
            self.clear_selection()

    def _on_detail_key(self, event) -> None:
        """详情视图里的快捷键（输入框获得焦点时不拦截）。"""
        if not self.detail.winfo_ismapped():
            return
        focused = self.focus_get()
        if isinstance(focused, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Spinbox)):
            return
        key = event.keysym
        if key == "Left":
            self.detail.step(-1)
        elif key == "Right":
            self.detail.step(1)
        elif key in ("plus", "equal"):
            self.detail.zoom_step(1)
        elif key == "minus":
            self.detail.zoom_step(-1)
        elif key == "0":
            self.detail.zoom_fit()
        elif key == "1":
            self.detail.zoom_reset()

    def open_detail_for_current(self) -> None:
        """打开详情：优先「当前卡片」（鼠标所指或最后点击的那张），再退回选中项。"""
        if not self.total:
            return
        index: int | None = None
        if 0 <= self.current_index < len(self.items):
            index = self.current_index
        if index is None and self.selected and not self.select_all_filtered:
            for i, item in enumerate(self.items):
                if item["id"] in self.selected:
                    index = i
                    break
        self.detail.open(index if index is not None else 0)

    # ------------------------------------------------------------------ #
    # 查询与列表
    # ------------------------------------------------------------------ #
    def current_filters(self) -> dict:
        sort_key = dict((t, k) for t, k in SORT_OPTIONS).get(self.sort_var.get(), "time_desc")
        filters: dict = {
            "sources": [k for k, v in self.source_vars.items() if v.get()] or None,
            "exts": [k for k, v in self.ext_vars.items() if v.get()] or None,
            "accounts": [p for p, v in self.account_vars.items() if v.get()] or None,
            "q": self.search_var.get().strip(),
            "sort": sort_key,
            "min_width": _safe_int(self.min_width_var.get()),
        }
        if self.animated_var.get():
            filters["animated"] = True
        pack = self.pack_var.get()
        if pack and pack != "全部表情包":
            filters["packs"] = [pack.split(" ", 1)[0]]
        return filters

    def _apply_filters_now(self) -> None:
        """筛选条件一变就立刻刷新图库。"""
        if self._filter_job:
            try:
                self.after_cancel(self._filter_job)
            except Exception:
                pass
        self._filter_job = self.after(30, lambda: self.reload(reset_page=True))

    def _schedule_filter(self, delay: int = 260) -> None:
        if self._filter_job:
            try:
                self.after_cancel(self._filter_job)
            except Exception:
                pass
        self._filter_job = self.after(delay, lambda: self.reload(reset_page=True))

    def reload(self, reset_page: bool = True) -> None:
        """筛选条件变化后回到第一页。"""
        self.load_page(1)

    def load_page(self, page: int, on_done=None) -> None:
        """加载指定页。

        分页式：每次只向索引库取一页数据，界面上始终只有 ``page_size`` 条记录，
        内存占用恒定，也不会出现「一直加载」。
        """
        page = max(1, int(page))
        page_size = self.page_size
        token = self._load_token + 1
        self._load_token = token
        filters = self.current_filters()
        self.fetching = True
        self._update_pager()

        def work() -> dict:
            return self.index.query(filters, page=page, page_size=page_size)

        def done(result: dict) -> None:
            self.fetching = False
            if token != self._load_token:
                return  # 条件已变化，丢弃过期结果
            self.total = result["total"] or 0
            self.pages = max(1, result["pages"] or 1)
            self.page = min(page, self.pages)
            self.items = result["items"]
            self.cells.clear()
            self.canvas.delete("all")
            self.canvas.yview_moveto(0)
            self.current_index = 0
            self.hover_index = None
            self._update_stats_line()
            self.render_grid(force=True)
            self._update_pager()
            if on_done:
                on_done()

        self._run_async(work, done, "加载列表失败")

    def goto_page(self, page: int, on_done=None) -> None:
        """翻到指定页（自动限制在有效范围内）。"""
        target = max(1, min(int(page), max(1, self.pages)))
        if target == self.page and not on_done:
            return
        self.load_page(target, on_done=on_done)

    def item_at(self, index: int) -> dict | None:
        if 0 <= index < len(self.items):
            return self.items[index]
        return None

    def refresh_pack_options(self) -> None:
        try:
            packs = self.index.packs("marketface")
        except Exception:
            packs = []
        values = ["全部表情包"] + [f"{p['pack_id']} {p['pack_name']}" for p in packs[:2000]]
        self.pack_combo.config(values=values)
        if self.pack_var.get() not in values:
            self.pack_var.set("全部表情包")

    def _on_search_changed(self) -> None:
        self.search_clear.config(fg=COLORS["dim"] if self.search_var.get() else COLORS["bg"])
        if self._search_job:
            try:
                self.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.after(380, lambda: self.reload(reset_page=True))

    def reset_filters(self) -> None:
        for var in self.ext_vars.values():
            var.set(False)
        self.animated_var.set(False)
        self.min_width_var.set("0")
        self.pack_var.set("全部表情包")
        self.sort_var.set(SORT_OPTIONS[0][0])
        self.search_var.set("")
        self._apply_filters_now()

    def _update_stats_line(self) -> None:
        try:
            stats = self.index.stats()
        except Exception:
            stats = {"total": self.total, "animated": 0, "packs": 0, "size": 0}
        self.stats_label.config(
            text=(f"共 {stats['total']} 个表情 · 动图 {stats['animated']} · "
                  f"表情包 {stats['packs']} · {_human_size(stats['size'])}"
                  f"　|　当前筛选 {self.total} 个")
        )
        self._update_pager()

    # ------------------------------------------------------------------ #
    # 网格渲染（只画可见的格子）
    # ------------------------------------------------------------------ #
    def _layout(self) -> tuple[int, int]:
        width = max(self.canvas.winfo_width(), 200)
        cols = max(1, (width - PAD * 2 + GAP) // (CELL_W + GAP))
        return cols, width

    def _on_canvas_scroll(self, first, last) -> None:
        """画布滚动时的回调：同步滚动条，并安排一次可见区域重绘。"""
        try:
            self.vbar.set(first, last)
        except tk.TclError:
            pass
        self._schedule_grid_render()

    def _schedule_grid_render(self) -> None:
        if self._grid_job is not None:
            return  # 已经在队列里，合并高频滚动事件
        self._grid_job = self.after(16, self._do_grid_render)

    def _do_grid_render(self) -> None:
        self._grid_job = None
        if not self.grid_wrap.winfo_ismapped():
            return
        # 滚动后鼠标下方已经是别的卡片了：按真实指针位置重新定位，而不是清空了事
        self._refresh_hover_from_pointer()
        self.render_grid()

    def _refresh_hover_from_pointer(self) -> None:
        """按当前鼠标位置重新计算悬停卡片。"""
        try:
            x = self.winfo_pointerx() - self.canvas.winfo_rootx()
            y = self.winfo_pointery() - self.canvas.winfo_rooty()
        except tk.TclError:
            return
        if 0 <= x < self.canvas.winfo_width() and 0 <= y < self.canvas.winfo_height():
            self.hover_index = self._index_from_event(SimpleNamespace(x=x, y=y))
        else:
            self.hover_index = None

    def render_grid(self, force: bool = False) -> None:
        canvas = self.canvas
        count = len(self.items)          # 分页式：只渲染当前页
        cols, width = self._layout()
        rows = (count + cols - 1) // cols if count else 0
        content_h = PAD * 2 + rows * (CELL_H + GAP)
        # 只在真正需要时才改 scrollregion：重复设置会让画布反复重绘（表现为边框闪烁）
        region = (0, 0, width, max(content_h, canvas.winfo_height()))
        if region != self._scrollregion:
            self._scrollregion = region
            canvas.configure(scrollregion=region)

        if not count:
            canvas.delete("all")
            self.cells.clear()
            self._draw_placeholder()
            return

        canvas.delete("placeholder")
        top = canvas.canvasy(0)
        bottom = top + canvas.winfo_height()
        first_row = max(0, int((top - PAD) // (CELL_H + GAP)) - 1)
        last_row = int((bottom - PAD) // (CELL_H + GAP)) + 2
        start = first_row * cols
        end = min(count, (last_row + 1) * cols)

        # 可见范围没变化、格子也都齐了，就什么都不用做（避免空转重绘）
        signature = (start, end, cols, count)
        complete = set(self.cells) == set(range(start, end))
        if not force and complete and signature == self._render_signature:
            return
        self._render_signature = signature

        for index in list(self.cells):
            if index < start or index >= end:
                for item in self.cells.pop(index).values():
                    if isinstance(item, int):
                        canvas.delete(item)

        for index in range(start, end):
            if index not in self.cells:
                self._create_cell(index, cols)
        self._update_hover_outline()

    def _cell_geometry(self, index: int, cols: int) -> tuple[float, float]:
        row, col = divmod(index, cols)
        x = PAD + col * (CELL_W + GAP)
        y = PAD + row * (CELL_H + GAP)
        return x, y

    def _create_cell(self, index: int, cols: int) -> None:
        canvas = self.canvas
        x, y = self._cell_geometry(index, cols)
        record = self.item_at(index)
        selected = bool(record and self.is_selected(record["id"]))
        rect = canvas.create_rectangle(
            x, y, x + CELL_W, y + CELL_H,
            fill=COLORS["cell_sel"] if selected else COLORS["cell"],
            outline=COLORS["accent"] if selected else COLORS["border"],
            tags=("cell",),
        )
        cell: dict = {"rect": rect, "visual": (selected, False)}

        if record is None:  # 数据还没取回来
            cell["pending"] = canvas.create_text(
                x + CELL_W / 2, y + CELL_H / 2, text="载入中…", fill=COLORS["mute"],
                font=FONT_CANVAS, tags=("cell",))
            self.cells[index] = cell
            return

        photo = self.thumbs.get(record["id"])
        if photo is None:
            if record["id"] not in self.failed_thumbs:
                self.loader.request(record)
                glyph = "载入中…"
            else:
                glyph = "无法预览"
            cell["image"] = canvas.create_text(
                x + CELL_W / 2, y + (CELL_H - INFO_H) / 2 + 4, text=glyph, fill=COLORS["mute"],
                font=FONT_CANVAS, tags=("cell",))
        else:
            cell["image"] = canvas.create_image(
                x + CELL_W / 2, y + (CELL_H - INFO_H) / 2 + 4, image=photo, tags=("cell",))

        cell["name"] = canvas.create_text(
            x + 10, y + CELL_H - 30, text=_short(record.get("name") or "", 17),
            fill=COLORS["text"], font=FONT_CANVAS, anchor="w", width=CELL_W - 20, tags=("cell",))
        parts = [config.SOURCE_LABELS.get(record.get("source", ""), record.get("source", ""))]
        if record.get("animated"):
            parts.append("动图")
        elif record.get("ext"):
            parts.append(record["ext"].replace(".", "").upper())
        cell["sub"] = canvas.create_text(
            x + 10, y + CELL_H - 13, text=" · ".join(parts), fill=COLORS["mute"],
            font=("Consolas", 8), anchor="w", tags=("cell",))
        cell["record"] = record

        # 每张卡片左上角的勾选框（始终可见，点一下就勾选，可连续多选）
        box_x, box_y = x + 7, y + 7
        cell["check_box"] = canvas.create_rectangle(
            box_x, box_y, box_x + CHECK_SIZE, box_y + CHECK_SIZE,
            fill=COLORS["cell"] if not selected else COLORS["accent"],
            outline=COLORS["accent"] if selected else COLORS["mute"],
            tags=("cell", "check"), state="normal" if selected else "normal",
        )
        cell["check_mark"] = canvas.create_line(
            box_x + 3.5, box_y + 9, box_x + 7, box_y + 13, box_x + 14.5, box_y + 4.5,
            fill="#ffffff", width=2, capstyle="round", joinstyle="round",
            tags=("cell", "check"), state="normal" if selected else "hidden",
        )
        self.cells[index] = cell

    def _draw_placeholder(self) -> None:
        canvas = self.canvas
        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 200)
        empty_index = False
        try:
            empty_index = bool(self.index and self.index.stats()["total"] == 0)
        except Exception:
            empty_index = False
        if empty_index:
            title = "还没有索引任何表情"
            hint = "点击右上角「开始扫描」，自动解析 QQ 数据目录中的表情包"
        else:
            title = "没有符合筛选条件的表情"
            hint = "试试放宽左侧的筛选条件，或清空搜索关键词"
        canvas.create_text(width / 2, height / 2 - 16, text=title, fill=COLORS["dim"],
                           font=FONT_TITLE, tags=("placeholder",))
        canvas.create_text(width / 2, height / 2 + 16, text=hint, fill=COLORS["mute"],
                           font=FONT_SMALL, tags=("placeholder",))

    def _drain_thumb_queue(self) -> None:
        updated: set[int] = set()
        for _ in range(40):
            try:
                item_id, image, _error = self.loader.results.get_nowait()
            except queue.Empty:
                break
            if image is None:
                self.failed_thumbs.add(item_id)
                continue
            self.thumbs[item_id] = ImageTk.PhotoImage(image)
            if len(self.thumbs) > THUMB_CACHE_MAX:
                for key in list(self.thumbs)[: len(self.thumbs) - THUMB_CACHE_MAX]:
                    self.thumbs.pop(key, None)
            for index, cell in self.cells.items():
                record = cell.get("record")
                if record and record["id"] == item_id:
                    updated.add(index)
        for index in updated:
            self._refresh_cell_image(index)
        if updated and self.grid_wrap.winfo_ismapped():
            self.render_grid()
        self.after(120, self._drain_thumb_queue)

    def _refresh_cell_image(self, index: int) -> None:
        cell = self.cells.get(index)
        record = cell.get("record") if cell else None
        if not cell or not record:
            return
        photo = self.thumbs.get(record["id"])
        if photo is None:
            return
        if "image" in cell:
            self.canvas.delete(cell["image"])
        cols, _ = self._layout()
        x, y = self._cell_geometry(index, cols)
        cell["image"] = self.canvas.create_image(
            x + CELL_W / 2, y + (CELL_H - INFO_H) / 2 + 4, image=photo, tags=("cell",))
        self.canvas.tag_lower(cell["image"], cell["name"])
        for key in ("check_box", "check_mark"):
            if key in cell:
                self.canvas.tag_raise(cell[key])

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_wheel(self, event) -> None:
        """全局滚轮：按鼠标所在区域决定滚侧边栏、滚图库还是缩放详情。"""
        widget = self.winfo_containing(event.x_root, event.y_root)
        if widget is None:
            return
        if self.detail.winfo_ismapped() and self._inside(widget, self.detail):
            self.detail.zoom_step(1 if event.delta > 0 else -1)
            return
        step = -2 if event.delta > 0 else 2
        if self._inside(widget, self.sidebar):
            self.sidebar_canvas.yview_scroll(step, "units")
        elif self._inside(widget, self.grid_wrap):
            self.canvas.yview_scroll(step, "units")

    @staticmethod
    def _inside(widget: tk.Widget, ancestor: tk.Widget) -> bool:
        current: tk.Widget | None = widget
        while current is not None:
            if current is ancestor:
                return True
            try:
                current = current.master  # type: ignore[assignment]
            except Exception:
                return False
        return False

    def _index_from_event(self, event) -> int | None:
        cols, _ = self._layout()
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        col = int((x - PAD) // (CELL_W + GAP))
        row = int((y - PAD) // (CELL_H + GAP))
        if col < 0 or col >= cols or row < 0:
            return None
        local_x = (x - PAD) - col * (CELL_W + GAP)
        local_y = (y - PAD) - row * (CELL_H + GAP)
        if local_x > CELL_W or local_y > CELL_H:
            return None
        index = row * cols + col
        return index if 0 <= index < len(self.items) else None

    def _in_checkbox(self, event, index: int) -> bool:
        """点击是否落在卡片的勾选框上。"""
        cols, _ = self._layout()
        x, y = self._cell_geometry(index, cols)
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        return (x + 3 <= cx <= x + 6 + CHECK_SIZE + 3) and (y + 3 <= cy <= y + 6 + CHECK_SIZE + 3)

    def _on_click(self, event) -> None:
        """单击卡片即勾选 / 取消，可以连续点选多张（无需按修饰键）。"""
        index = self._index_from_event(event)
        if index is None:
            return
        record = self.item_at(index)
        if not record:
            return
        self.current_index = index
        self._last_click_index = index
        shift = bool(event.state & 0x0001)
        ctrl_only = bool(event.state & 0x0004)  # Windows: Control = 0x0004

        if shift and self.anchor_index is not None:
            # Shift：从锚点连选到这一张（累加）
            self.select_all_filtered = False
            lo, hi = sorted((self.anchor_index, index))
            for i in range(lo, hi + 1):
                item = self.item_at(i)
                if item:
                    self.selected.add(item["id"])
        elif ctrl_only:
            # Ctrl：只选这一张
            self.select_all_filtered = False
            self.selected = {record["id"]}
            self.anchor_index = index
        else:
            # 普通单击：切换这一张的勾选状态，可以连续点多张
            self.toggle_selection(record["id"])
            self.anchor_index = index
        self.refresh_selection_visual()

    def _on_double_click(self, event) -> None:
        index = self._index_from_event(event)
        if index is None:
            return
        if self._in_checkbox(event, index):
            return  # 连点勾选框不应弹出详情
        if self._last_click_index is not None and self._last_click_index != index:
            return  # 连续点击了不同卡片（Tk 不判断位置，这里自己拦掉）
        self.detail.open(index)

    def _on_right_click(self, event) -> None:
        index = self._index_from_event(event)
        if index is None:
            return
        self._context_index = index
        record = self.item_at(index)
        if record and record["id"] not in self.selected:
            self.selected = {record["id"]}
            self.refresh_selection_visual()
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._context_menu.grab_release()

    def _on_motion(self, event) -> None:
        index = self._index_from_event(event)
        if index is not None:
            # 鼠标所指的卡片即为「当前」卡片，这样「查看详情」/回车总是打开你正指着的那张
            self.current_index = index
        self._set_hover(index)

    def _set_hover(self, index: int | None) -> None:
        if index == self.hover_index:
            return
        self.hover_index = index
        self._update_hover_outline()

    def _update_hover_outline(self) -> None:
        """按「选中 / 悬停」状态增量刷新卡片外观（状态没变就不碰，避免闪烁）。"""
        for index, cell in self.cells.items():
            record = cell.get("record")
            selected = bool(record and self.is_selected(record["id"]))
            hovered = index == self.hover_index
            visual = (selected, hovered)
            if cell.get("visual") == visual:
                continue
            cell["visual"] = visual
            fill = COLORS["cell_sel"] if selected else (COLORS["hover"] if hovered else COLORS["cell"])
            outline = COLORS["accent"] if selected else (COLORS["primary"] if hovered else COLORS["border"])
            try:
                self.canvas.itemconfig(cell["rect"], fill=fill, outline=outline)
                if "check_box" in cell:
                    self.canvas.itemconfig(
                        cell["check_box"],
                        fill=COLORS["accent"] if selected else COLORS["cell"],
                        outline=COLORS["accent"] if selected else COLORS["mute"],
                    )
                    self.canvas.itemconfig(cell["check_mark"],
                                           state="normal" if selected else "hidden")
                    self.canvas.tag_raise(cell["check_box"])
                    self.canvas.tag_raise(cell["check_mark"])
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ #
    # 选择
    # ------------------------------------------------------------------ #
    def is_selected(self, item_id: str) -> bool:
        return self.select_all_filtered or item_id in self.selected

    def toggle_selection(self, item_id: str) -> None:
        if self.select_all_filtered:
            self.select_all_filtered = False
            self.selected = {i["id"] for i in self.items}
        if item_id in self.selected:
            self.selected.discard(item_id)
        else:
            self.selected.add(item_id)
        self.update_selection_bar()

    def select_all(self) -> None:
        self.select_all_filtered = True
        self.selected.clear()
        self.refresh_selection_visual()

    def clear_selection(self) -> None:
        self.select_all_filtered = False
        self.selected.clear()
        self.refresh_selection_visual()

    def selection_count(self) -> int:
        return self.total if self.select_all_filtered else len(self.selected)

    def refresh_selection_visual(self) -> None:
        self._update_hover_outline()
        self.update_selection_bar()

    def update_selection_bar(self) -> None:
        count = self.selection_count()
        if count:
            text = (f"已选择当前筛选的全部 {count} 个表情" if self.select_all_filtered
                    else f"已选择 {count} 个表情")
            self.selection_label.config(text=text, fg=COLORS["accent"])
            self.export_button.config(state="normal", text=f"导出 ({count})")
        else:
            self.selection_label.config(
                text="单击卡片即勾选，可连续多选 · Shift+单击连选 · Ctrl+单击只选这张 · 双击看详情",
                fg=COLORS["mute"],
            )
            self.export_button.config(state="disabled", text="导出")

    def toggle_current_card(self) -> None:
        """空格 / Insert：切换当前卡片的选中状态（方便不用键盘修饰键也能多选）。"""
        if self.detail.winfo_ismapped() or not self.items:
            return
        focused = self.focus_get()
        if isinstance(focused, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Spinbox)):
            return
        record = self.item_at(self.current_index)
        if record:
            self.toggle_selection(record["id"])
            self.refresh_selection_visual()

    def export_scope(self) -> dict:
        if self.select_all_filtered:
            return {"ids": [], "filters": self.current_filters(), "count": self.total}
        return {"ids": list(self.selected), "filters": {}, "count": len(self.selected)}

    # ------------------------------------------------------------------ #
    # 导出 / 下载 / 打开目录
    # ------------------------------------------------------------------ #
    def open_export(self) -> None:
        if self.selection_count() == 0:
            messagebox.showinfo("提示", "请先选择要导出的表情：\n单击卡片选择，Ctrl+单击多选，"
                                        "或点「选择筛选全部」导出当前筛选结果")
            return
        ExportDialog(self)

    def export_all_filtered(self) -> None:
        self.select_all_filtered = True
        self.selected.clear()
        self.refresh_selection_visual()
        ExportDialog(self)

    def start_export(self, payload: dict) -> None:
        task = tasks.manager.submit("export", "导出表情", lambda t: jobs_mod.export(t, payload))
        self._watch_task(task, "正在导出", on_done=None)
        self.set_status("正在导出…")

    def download_item(self, record: dict) -> None:
        name = exporter.sanitize(exporter.display_name(record), record["id"][:8])
        path = filedialog.asksaveasfilename(
            title="保存表情", initialfile=name + (record.get("ext") or ".png"), parent=self,
        )
        if not path:
            return

        def work():
            data = images.Loader.load(record)
            Path(path).write_bytes(data)
            return path

        def done(result) -> None:
            self.set_status(f"已保存到 {result}")
            if messagebox.askyesno("保存成功", f"已保存到：\n{result}\n\n是否打开所在目录？"):
                self.open_dir(Path(result).parent)

        self._run_async(work, done, "保存失败")

    def open_dir(self, path) -> None:
        try:
            target = str(path)
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            elif os.name == "posix":
                import subprocess

                subprocess.Popen(["xdg-open", target])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # 右键菜单
    # ------------------------------------------------------------------ #
    def _ctx_record(self) -> dict | None:
        if self._context_index is None:
            return None
        return self.item_at(self._context_index)

    def _ctx_preview(self) -> None:
        if self._context_index is not None:
            self.detail.open(self._context_index)

    def _ctx_toggle(self) -> None:
        record = self._ctx_record()
        if record:
            self.toggle_selection(record["id"])
            self.refresh_selection_visual()

    def _ctx_select_only(self) -> None:
        record = self._ctx_record()
        if record:
            self.select_all_filtered = False
            self.selected = {record["id"]}
            self.refresh_selection_visual()

    def _ctx_download(self) -> None:
        record = self._ctx_record()
        if record:
            self.download_item(record)

    def _ctx_copy_path(self) -> None:
        record = self._ctx_record()
        if record:
            self.clipboard_clear()
            self.clipboard_append(record["path"])
            self.set_status("已复制存储路径")

    def _ctx_reveal(self) -> None:
        record = self._ctx_record()
        if record:
            self.open_dir(Path(record["path"]).parent)

    # ------------------------------------------------------------------ #
    # 帮助
    # ------------------------------------------------------------------ #
    def show_stats(self) -> None:
        stats = self.index.stats()
        lines = [
            f"总表情数：{stats['total']}",
            f"动图数量：{stats['animated']}",
            f"表情包数：{stats['packs']}",
            f"总体积：{_human_size(stats['size'])}",
            f"上次扫描：{stats['scanned_at'] or '未知'}",
            "",
            "按来源统计：",
        ]
        for row in stats["by_source"]:
            label = config.SOURCE_LABELS.get(row["source"], row["source"])
            lines.append(f"  {label}：{row['count']} 个（动图 {row['animated'] or 0}）")
        messagebox.showinfo("统计信息", "\n".join(lines))

    def show_help(self) -> None:
        messagebox.showinfo(
            "使用说明",
            "1. 左侧「数据目录」勾选要扫描的 QQ 账号（检测不到时用「添加文件夹…」选择）；\n"
            "2. 左侧勾选表情来源，图库会立刻按来源筛选；\n"
            "3. 点右上角「开始扫描」建立索引（几秒钟完成）；\n"
            "4. 单击卡片即勾选，可连续点多张；Shift+单击连选，Ctrl+单击只选这张；双击看详情；\n"
            "5. 详情视图里可放大 / 缩小 / 适应窗口 / 原始大小，滚轮缩放，按住拖动平移；\n"
            "6. 底部翻页栏可切换页码与每页条数；点「导出 (N)」可打包 ZIP 或写入文件夹。\n\n"
            "解析来源：市场表情包、我的收藏、聊天收到、联想表情、旧版 QQ 收藏、系统黄脸。\n"
            "市场表情包原图经过异或混淆，程序会自动还原为正常的 GIF/PNG。",
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于",
            f"QQ 表情包提取器 v{__version__}\n\n"
            "纯本地运行，不联网、不上传任何数据。\n"
            f"缓存目录：{config.CACHE_DIR}\n"
            f"索引数量：{self.index.stats()['total'] if self.index else 0}",
        )

    # ------------------------------------------------------------------ #
    # 收尾
    # ------------------------------------------------------------------ #
    def _on_close(self) -> None:
        if self.active_tasks and not messagebox.askyesno("任务进行中", "仍有任务在运行，确定退出吗？"):
            return
        try:
            self.cancel_active()
            self.loader.shutdown()
            pool = getattr(self, "_pool", None)
            if pool:
                pool.shutdown(wait=False, cancel_futures=True)
        finally:
            self.destroy()


def main() -> int:
    """启动桌面界面。"""
    if ImageTk is None:
        print("缺少 Pillow（含 ImageTk），请执行：pip install -r requirements.txt")
        return 2
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
