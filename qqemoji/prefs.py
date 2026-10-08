"""用户偏好设置。

保存在缓存目录下的 ``settings.json``，目前用于记住上次的导出配置
（导出方式、目标文件夹、格式转换、最大边长、去重、命名模板等）。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from . import config

PATH = config.CACHE_DIR / "settings.json"

EXPORT_DEFAULTS: dict[str, Any] = {
    "mode": "zip",                              # zip / folder
    "target_dir": "",
    "convert": "origin",                        # origin / png / gif / webp / jpg
    "max_side": 0,
    "dedupe": True,
    "template": "{index}_{pack}_{name}",
}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def _data() -> dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            loaded = json.loads(PATH.read_text(encoding="utf-8"))
            _cache = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def get(key: str, default: Any = None) -> Any:
    return _data().get(key, default)


def update(**values: Any) -> None:
    """合并写入并立即落盘。"""
    global _cache
    with _lock:
        data = dict(_data())
        data.update(values)
        _cache = data
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


def export_options() -> dict[str, Any]:
    """取上次的导出配置（缺项用默认值补齐）。"""
    options = dict(EXPORT_DEFAULTS)
    saved = get("export")
    if isinstance(saved, dict):
        for key in EXPORT_DEFAULTS:
            if saved.get(key) is not None:
                options[key] = saved[key]
    return options


def save_export_options(options: dict[str, Any]) -> None:
    update(export={key: options.get(key, EXPORT_DEFAULTS[key]) for key in EXPORT_DEFAULTS})


def page_size() -> int:
    try:
        value = int(get("page_size", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def save_page_size(size: int) -> None:
    update(page_size=int(size))
