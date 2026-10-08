"""导出选中的表情：打包成 ZIP 或写入指定目录。"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Callable, Iterable

from . import config, deobf, images

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
ProgressCb = Callable[[str, int, int, str], None]


def sanitize(name: str, fallback: str = "emoji", max_len: int = 80) -> str:
    name = INVALID_CHARS.sub("_", str(name)).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    return name[:max_len]


def build_name(record: dict, template: str, index: int, ext: str) -> str:
    """按模板生成文件名（不含扩展名）。"""
    values = {
        "index": f"{index:04d}",
        "source": config.SOURCE_LABELS.get(record.get("source", ""), record.get("source", "")),
        "pack": record.get("pack_name") or record.get("pack_id") or "",
        "name": record.get("name") or "",
        "raw": record.get("raw_name") or record.get("name") or "",
        "id": record.get("id", "")[:8],
        "account": record.get("account", ""),
        "ext": ext.lstrip("."),
    }
    try:
        text = template.format(**values)
    except (KeyError, IndexError, ValueError):
        text = "{index}_{name}"
        text = text.format(**values)
    return sanitize(text, fallback=record.get("id", "emoji")[:8])


HASH_NAME = re.compile(r"^[0-9a-fA-F]{16,}$")


def display_name(record: dict) -> str:
    """给界面用的友好名称。

    QQ 只给「市场表情包」存了名字，其余缓存文件本身就是哈希文件名，
    这类表情在扫描时被换成 ``#0001`` 形式的编号，这里再把表情包名拼在前面，
    例如 ``收到表情 · 2026-09 · #0123``；真实名字则显示为 ``亲切 · 表情包名``。
    """
    name = (record.get("name") or "").strip()
    pack = (record.get("pack_name") or "").strip()
    if not name or HASH_NAME.match(name):
        name = name or (record.get("raw_name") or "").strip()
    if name.startswith("#"):
        return " · ".join(x for x in (pack, name) if x)
    if name and pack and name != pack and not name.startswith(pack):
        return f"{name} · {pack}"
    return name or pack or record.get("raw_name") or record.get("id", "")


def _unique_path(directory: Path, stem: str, ext: str, used: set[str]) -> Path:
    candidate = f"{stem}{ext}"
    n = 1
    while candidate.lower() in used or (directory / candidate).exists():
        candidate = f"{stem}_{n}{ext}"
        n += 1
    used.add(candidate.lower())
    return directory / candidate


def export_items(
    records: Iterable[dict],
    *,
    mode: str = "zip",
    target_dir: str | None = None,
    zip_name: str | None = None,
    convert: str = "origin",
    max_side: int = 0,
    dedupe: bool = True,
    name_template: str = "{index}_{pack}_{name}",
    progress: ProgressCb | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict:
    """执行导出。

    :param mode: ``zip`` 打包下载 / ``folder`` 写入目录
    :param convert: ``origin`` 保持原格式，或 ``png`` / ``gif`` / ``webp`` / ``jpg``
    """
    records = [r for r in records if r.get("path")]
    total = len(records)
    stats = {"total": total, "written": 0, "skipped": 0, "failed": 0, "bytes": 0}
    errors: list[str] = []
    seen_hashes: set[str] = set()
    used_names: set[str] = set()

    zip_path: Path | None = None
    out_dir: Path | None = None
    zf: zipfile.ZipFile | None = None

    if mode == "zip":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        zip_path = config.EXPORT_DIR / (zip_name or f"qq-emoji-{stamp}.zip")
        zf = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
    else:
        if not target_dir:
            raise ValueError("导出到目录时必须提供 target_dir")
        out_dir = Path(target_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress("准备导出", 0, total, f"共 {total} 个表情")

    try:
        for i, record in enumerate(records, 1):
            if should_cancel and should_cancel():
                stats["cancelled"] = True
                break
            try:
                data = images.Loader.load(record)
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                if len(errors) < 20:
                    errors.append(f"{display_name(record)}: {type(exc).__name__}")
                continue

            if dedupe:
                digest = hashlib.md5(data).hexdigest()
                if digest in seen_hashes:
                    stats["skipped"] += 1
                    continue
                seen_hashes.add(digest)

            data, ext = images.convert_image(data, convert, max_side)
            if not ext or ext == ".bin":
                ext = record.get("ext") or ".png"

            folder = ""
            if record.get("source") == "marketface" and record.get("pack_name"):
                folder = sanitize(record["pack_name"], "表情包", 40) + "/"
            elif record.get("pack_name") and mode == "folder":
                folder = ""

            stem = build_name(record, name_template, i, ext)
            target_name = f"{folder}{stem}{ext}"

            try:
                if zf is not None:
                    base = target_name
                    n = 1
                    while base.lower() in used_names:
                        base = f"{folder}{stem}_{n}{ext}"
                        n += 1
                    used_names.add(base.lower())
                    zf.writestr(base, data)
                else:
                    assert out_dir is not None
                    if folder:
                        sub = out_dir / folder.rstrip("/")
                        sub.mkdir(parents=True, exist_ok=True)
                        path = _unique_path(sub, stem, ext, used_names)
                    else:
                        path = _unique_path(out_dir, stem, ext, used_names)
                    path.write_bytes(data)
                stats["written"] += 1
                stats["bytes"] += len(data)
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                if len(errors) < 20:
                    errors.append(f"{display_name(record)}: {type(exc).__name__}")

            if progress and (i % 25 == 0 or i == total):
                progress("导出中", i, total, f"{stats['written']} 个已写入")
    finally:
        if zf is not None:
            zf.close()

    result = dict(stats)
    result["errors"] = errors
    if zip_path is not None:
        result["zip_path"] = str(zip_path)
        result["zip_name"] = zip_path.name
        result["zip_size"] = zip_path.stat().st_size if zip_path.exists() else 0
        result["download_url"] = f"/api/export/download/{zip_path.name}"
    if out_dir is not None:
        result["target_dir"] = str(out_dir)
    if progress:
        progress("导出完成", stats["written"], total, f"成功 {stats['written']} 个")
    return result


def cleanup_exports(keep: int = 20) -> None:
    """清理旧的导出压缩包，只保留最近若干个。"""
    files = sorted(config.EXPORT_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


def safe_export_path(name: str) -> Path | None:
    """校验导出文件是否存在（防止路径穿越）。"""
    candidate = (config.EXPORT_DIR / Path(name).name).resolve()
    try:
        candidate.relative_to(config.EXPORT_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def copy_to_clipboard_hint(path: str) -> str:
    return path
