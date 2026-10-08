"""全局配置与工作目录管理。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "QQEmojiExporter"


def _base_dir() -> Path:
    """程序所在目录（兼容 PyInstaller 打包与 zipapp 单文件运行）。"""
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    entry = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if entry is not None and entry.suffix.lower() in (".pyz", ".zip"):
        # zipapp：__file__ 指向压缩包内部，改用启动文件所在目录
        return entry.parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()


def _cache_root() -> Path:
    """缓存目录：固定放在程序目录下的 ``.cache``（便携、集中，不往别处写）。

    只有在程序目录不可写时才依次退回用户目录、临时目录。
    也可以用环境变量 ``QQEMOJI_CACHE`` 指定到别处。
    """
    candidates: list[Path | None] = []
    env = os.environ.get("QQEMOJI_CACHE")
    if env:
        candidates.append(Path(env))
    candidates.append(BASE_DIR / ".cache")
    candidates.append(Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME / "cache")
    candidates.append(Path(tempfile.gettempdir()) / APP_NAME)

    for candidate in candidates:
        if candidate is None:
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_test"
            probe.write_bytes(b"ok")
            probe.unlink()
            return candidate
        except Exception:
            continue
    return Path(tempfile.gettempdir()) / APP_NAME


CACHE_DIR = _cache_root()
THUMB_DIR = CACHE_DIR / "thumbs"
DECODED_DIR = CACHE_DIR / "decoded"
EXPORT_DIR = CACHE_DIR / "exports"
INDEX_DIR = CACHE_DIR / "index"

for _d in (THUMB_DIR, DECODED_DIR, EXPORT_DIR, INDEX_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 单个表情在界面上显示时用的缩略图边长（像素）
THUMB_SIZE = 200
# 缩略图生成的并发线程数
THUMB_WORKERS = 4
# 扫描时忽略小于该字节数的文件
MIN_FILE_BYTES = 64

# 来源定义：key -> (显示名, 说明, 默认是否勾选)
SOURCES = {
    "marketface": ("市场表情包", "QQ 表情商城下载的表情包（含动态图原图，按表情包分组）", True),
    "personal": ("我的收藏", "自己收藏/添加的自定义表情", True),
    "recv": ("聊天收到", "聊天中收到的表情图片，按月份存放", True),
    "related": ("关联表情", "表情联想/推荐缓存的表情包", True),
    "legacy": ("旧版收藏", "旧版 QQ（CustomFace.db / FaceStore.db）中提取的表情", True),
    "system": ("系统黄脸", "QQ 自带的基础小黄脸表情", False),
}

SOURCE_LABELS = {k: v[0] for k, v in SOURCES.items()}
