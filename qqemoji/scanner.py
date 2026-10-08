"""扫描各类表情来源，生成索引记录。

支持的来源：

* ``marketface`` —— NTQQ 市场表情包，``nt_data/Emoji/marketface/<包ID>/``，
  原图做过异或混淆（见 :mod:`qqemoji.deobf`），包信息在隔壁 ``json/<包ID>.jtmp``；
* ``personal``    —— 我的收藏表情，``nt_data/Emoji/personal_emoji/Ori/``；
* ``recv``        —— 聊天收到的表情，``nt_data/Emoji/emoji-recv/<年-月>/Ori/``；
* ``related``     —— 表情联想缓存，``nt_data/Emoji/emoji-related/emoji/<哈希>/``；
* ``system``      —— 系统自带小黄脸，``nt_data/Emoji/BaseEmojiSyastems/``；
* ``legacy``      —— 旧版 QQ 的 ``CustomFace.db`` / ``FaceStore.db``（雕刻图片）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable, Iterator

from concurrent.futures import ThreadPoolExecutor

from . import carve, config, deobf, probe as probe_mod

ProgressCb = Callable[[str, int, str], None]

_MARKETFACE_EXCLUDE = {"color.png", "gray.png", "json", ".DS_Store"}
_HEX = set("0123456789abcdef")
_SKIP_SUFFIX = (".tmp", ".jtmp", ".part", ".download")


def _is_md5(name: str) -> bool:
    return len(name) == 32 and all(ch in _HEX for ch in name.lower())


def is_hash_name(name: str) -> bool:
    """判断一个名字是否只是 QQ 的哈希文件名（对浏览毫无意义）。"""
    stem = str(name or "").strip()
    return len(stem) >= 16 and all(ch in _HEX for ch in stem.lower())


def friendly_name(index: int) -> str:
    """给没有名字的表情生成稳定可读的编号名，例如 ``#0123``。"""
    return f"#{index:04d}"


def make_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def _stat(path: Path) -> os.stat_result | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size < config.MIN_FILE_BYTES:
        return None
    return st


def _read_slice(path: Path, offset: int | None, count: int) -> bytes:
    try:
        with open(path, "rb") as fh:
            if offset is not None:
                fh.seek(int(offset))
            return fh.read(count)
    except OSError:
        return b""


def _head_bytes(path: Path, offset: int | None, length: int | None,
                obfuscated: bool, size: int) -> bytes:
    """读取文件（或数据库片段）的头部若干字节，必要时先解混淆。

    GIF 需要走一遍块结构才能准确判断是否为动图，因此在文件不大时读全；
    其它格式只需文件头即可。
    """
    total = int(length or size or 0)
    first = _read_slice(path, offset, 16)
    if obfuscated:
        first = deobf.deobfuscate(first)

    if first.startswith(b"GIF8"):
        count = probe_mod.GIF_EXACT_LIMIT
        if total:
            count = min(count, total)
    else:
        count = probe_mod.HEAD_BYTES
        if total:
            count = min(count, total)

    if count <= len(first):
        return first
    raw = _read_slice(path, offset, count)
    if obfuscated:
        raw = deobf.deobfuscate(raw)
    return raw


def _enrich(record: dict) -> dict:
    """用文件头补齐格式 / 尺寸 / 动图标记（无需完整解码）。"""
    head = _head_bytes(
        Path(record["path"]),
        record.get("offset"),
        record.get("length"),
        bool(record.get("obfuscated")),
        int(record.get("size") or 0),
    )
    if not head:
        return record
    info = probe_mod.probe_header(head)
    if info["ext"] != ".bin":
        record["ext"] = info["ext"]
        record["mime"] = info["mime"]
    elif not record.get("ext"):
        record["ext"] = ".bin"
        record["mime"] = probe_mod.mime_for(".bin")
    record["width"] = info["width"]
    record["height"] = info["height"]
    record["animated"] = 1 if info["animated"] else 0
    record["frames"] = int(info.get("frames") or (2 if info["animated"] else 1))
    return record


def enrich_batch(records: list[dict], workers: int = 8) -> list[dict]:
    """并行补齐一批记录的头部信息（读盘为主，用线程池提速明显）。"""
    if not records:
        return records
    workers = max(1, min(workers, 16, len(records)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_enrich, records))
    return records


def _record(
    *,
    account: str,
    source: str,
    path: Path,
    variant: str = "origin",
    obfuscated: bool = False,
    offset: int | None = None,
    length: int | None = None,
    pack_id: str = "",
    pack_name: str = "",
    pack_author: str = "",
    name: str = "",
    raw_name: str = "",
    keywords: str = "",
    ext: str = "",
    mime: str = "",
    size: int | None = None,
    mtime: float | None = None,
) -> dict:
    st = None
    if size is None or mtime is None:
        st = _stat(path)
    if size is None:
        size = (st.st_size if st else 0) if length is None else length
    if mtime is None:
        mtime = st.st_mtime if st else 0.0
    if not ext:
        ext = path.suffix.lower()
    if not mime:
        mime = {
            ".png": "image/png",
            ".gif": "image/gif",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(ext, "application/octet-stream")
    item_id = make_id(account, source, variant, path, offset if offset is not None else -1)
    return {
        "id": item_id,
        "account": account,
        "source": source,
        "variant": variant,
        "path": str(path),
        "offset": offset,
        "length": length,
        "obfuscated": obfuscated,
        "ext": ext,
        "mime": mime,
        "size": int(size),
        "mtime": float(mtime),
        "pack_id": str(pack_id),
        "pack_name": pack_name[:120],
        "pack_author": pack_author[:80],
        "name": name[:80],
        "raw_name": str(raw_name)[:120],
        "keywords": keywords[:400],
        "width": 0,
        "height": 0,
        "frames": 1,
        "animated": 0,
        "content_hash": "",
    }


# --------------------------------------------------------------------------- #
# 市场表情包
# --------------------------------------------------------------------------- #
def _load_pack_meta(meta_dir: Path, progress: ProgressCb | None = None) -> dict[str, dict]:
    """读取 ``marketface/json/*.jtmp``，返回 包ID -> 元信息。"""
    packs: dict[str, dict] = {}
    if not meta_dir.is_dir():
        return packs
    for entry in os.scandir(meta_dir):
        if not entry.is_file():
            continue
        try:
            raw = Path(entry.path).read_bytes()
        except OSError:
            continue
        text = None
        for encoding in ("utf-8", "gbk", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        pack_id = str(data.get("id") or Path(entry.name).stem)
        items: dict[str, dict] = {}
        for img in data.get("imgs") or []:
            img_id = str(img.get("id") or "").lower()
            if not img_id:
                continue
            kw = img.get("keywords") or []
            if isinstance(kw, str):
                kw = [kw]
            items[img_id] = {
                "name": str(img.get("name") or ""),
                "keywords": "、".join(str(k) for k in kw if k),
            }
        packs[pack_id] = {
            "name": str(data.get("name") or ""),
            "author": str(data.get("author") or ""),
            "mark": str(data.get("mark") or ""),
            "items": items,
        }
    if progress:
        progress("读取表情包信息", len(packs), "个表情包有元数据")
    return packs


def scan_marketface(account_path: Path, uin: str, progress: ProgressCb | None = None) -> Iterator[dict]:
    base = account_path / "nt_qq" / "nt_data" / "Emoji" / "marketface"
    if not base.is_dir():
        return
    packs = _load_pack_meta(base / "json", progress)
    pack_dirs = [
        e for e in os.scandir(base)
        if e.is_dir() and e.name not in _MARKETFACE_EXCLUDE
    ]
    scan_account_path = account_path
    acct_key = str(scan_account_path)
    for pack_entry in pack_dirs:
        pack_id = pack_entry.name
        meta = packs.get(pack_id, {})
        pack_name = meta.get("name") or f"表情包 {pack_id}"
        pack_author = meta.get("author") or ""
        item_meta = meta.get("items") or {}

        # 一个 md5 只取一张图：原图优先，其次聊天大图 _aio，最后缩略图 _thu
        candidates: dict[str, tuple[int, Path, str]] = {}
        try:
            files = list(os.scandir(pack_entry.path))
        except OSError:
            continue
        for f in files:
            if not f.is_file():
                continue
            fname = f.name
            if fname in _MARKETFACE_EXCLUDE or fname.endswith(_SKIP_SUFFIX):
                continue
            path = Path(f.path)
            if fname.endswith("_aio.png"):
                key, rank, variant = fname[:-8], 1, "aio"
            elif fname.endswith("_thu.png"):
                key, rank, variant = fname[:-8], 2, "thumb"
            elif _is_md5(fname):
                key, rank, variant = fname.lower(), 0, "origin"
            else:
                continue
            current = candidates.get(key)
            if current is None or rank < current[0]:
                candidates[key] = (rank, path, variant)

        for seq, (key, (rank, path, variant)) in enumerate(sorted(candidates.items()), 1):
            st = _stat(path)
            if st is None:
                continue
            info = item_meta.get(key, {})
            real_name = (info.get("name") or "").strip()
            obfuscated = variant == "origin"
            yield _record(
                account=acct_key,
                source="marketface",
                path=path,
                variant=variant,
                obfuscated=obfuscated,
                pack_id=pack_id,
                pack_name=pack_name,
                pack_author=pack_author,
                name=real_name or friendly_name(seq),
                raw_name=key,
                keywords=info.get("keywords", ""),
                size=st.st_size,
                mtime=st.st_mtime,
            )


# --------------------------------------------------------------------------- #
# 我的收藏 / 聊天收到 / 联想表情 / 系统黄脸
# --------------------------------------------------------------------------- #
def _scan_media_dir(
    directory: Path,
    account: str,
    source: str,
    name_from: str = "stem",
    pack_id: str = "",
    pack_name: str = "",
    variants: tuple[str, ...] = ("origin",),
    skip_prefix: tuple[str, ...] = (),
) -> Iterator[dict]:
    """扫描一个存放明文图片的目录。

    QQ 给这些缓存文件起的名字就是内容哈希（如 ``01c59ee2….gif``），对人没有意义，
    因此这里对哈希名统一换成该目录内按文件名排序得到的稳定编号 ``#0001``，
    原始哈希保存在 ``raw_name`` 里，仍然可以搜索、导出与查看。
    """
    if not directory.is_dir():
        return
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("ThumbTemp", "OriTemp"))
        counter = 0
        for fname in sorted(files):
            if fname.startswith(skip_prefix) or fname.startswith("."):
                continue
            ext = Path(fname).suffix.lower()
            if ext not in (".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"):
                continue
            path = Path(root) / fname
            st = _stat(path)
            if st is None:
                continue
            counter += 1
            stem = Path(fname).stem
            if name_from == "parent":
                raw = Path(root).name
                name = raw
            else:
                raw = stem
                name = friendly_name(counter) if is_hash_name(stem) else stem
            yield _record(
                account=account,
                source=source,
                path=path,
                variant=variants[0],
                pack_id=pack_id or Path(root).name,
                pack_name=pack_name or Path(root).name,
                name=name,
                raw_name=raw,
                size=st.st_size,
                mtime=st.st_mtime,
            )


def scan_personal(account_path: Path, uin: str) -> Iterator[dict]:
    base = account_path / "nt_qq" / "nt_data" / "Emoji" / "personal_emoji"
    yield from _scan_media_dir(
        base / "Ori", str(account_path), "personal",
        pack_id="personal", pack_name="我的收藏",
    )


def scan_recv(account_path: Path, uin: str) -> Iterator[dict]:
    base = account_path / "nt_qq" / "nt_data" / "Emoji" / "emoji-recv"
    if not base.is_dir():
        return
    for month_entry in sorted(os.scandir(base), key=lambda e: e.name):
        if not month_entry.is_dir():
            continue
        month = month_entry.name
        yield from _scan_media_dir(
            Path(month_entry.path) / "Ori", str(account_path), "recv",
            pack_id=f"recv-{month}", pack_name=f"收到表情 · {month}",
        )


def scan_related(account_path: Path, uin: str) -> Iterator[dict]:
    base = account_path / "nt_qq" / "nt_data" / "Emoji" / "emoji-related" / "emoji"
    if not base.is_dir():
        return
    for pack_entry in os.scandir(base):
        if not pack_entry.is_dir():
            continue
        yield from _scan_media_dir(
            Path(pack_entry.path), str(account_path), "related",
            pack_id=pack_entry.name, pack_name=f"联想表情 · {pack_entry.name[:8]}",
        )


def scan_system(account_path: Path, uin: str) -> Iterator[dict]:
    base = (
        account_path / "nt_qq" / "nt_data" / "Emoji" / "BaseEmojiSyastems"
        / "EmojiSystermResource"
    )
    if not base.is_dir():
        return
    for entry in os.scandir(base):
        if not entry.is_dir():
            continue
        # 优先使用 png 目录（apng 为动图版本）
        for sub in ("png", "apng"):
            target = Path(entry.path) / sub
            if not target.is_dir():
                continue
            for f in os.scandir(target):
                if not f.is_file() or not f.name.lower().endswith(".png"):
                    continue
                path = Path(f.path)
                st = _stat(path)
                if st is None:
                    continue
                yield _record(
                    account=str(account_path), source="system", path=path,
                    pack_id="system", pack_name="系统黄脸",
                    name=Path(f.name).stem, raw_name=f.name,
                    size=st.st_size, mtime=st.st_mtime,
                )
            break


# --------------------------------------------------------------------------- #
# 旧版 QQ
# --------------------------------------------------------------------------- #
def scan_legacy(account_path: Path, uin: str, progress: ProgressCb | None = None) -> Iterator[dict]:
    db_specs = [
        ("CustomFace.db", "legacy-face", "旧版收藏表情"),
        ("FaceStore.db", "legacy-store", "旧版表情商城"),
    ]
    for db_name, pack_id, pack_name in db_specs:
        db_path = account_path / db_name
        if not db_path.is_file():
            continue
        if progress:
            progress("解析旧版表情数据库", 0, f"{db_name} …")
        try:
            blobs = carve.carve_file(db_path)
        except OSError:
            continue
        for index, (blob, ext, mime, offset) in enumerate(blobs):
            info = probe_mod.probe_header(blob)
            width, height = info.get("width", 0), info.get("height", 0)
            if width and height and (width < 16 or height < 16):
                continue
            digest = hashlib.md5(blob).hexdigest()
            yield _record(
                account=str(account_path),
                source="legacy",
                path=db_path,
                variant="origin",
                offset=offset,
                length=len(blob),
                pack_id=pack_id,
                pack_name=pack_name,
                name=f"{pack_name}-{index + 1:04d}",
                raw_name=digest,
                ext=ext,
                mime=mime,
                size=len(blob),
                mtime=db_path.stat().st_mtime,
            ) | {
                "width": width,
                "height": height,
                "animated": 1 if info.get("animated") else 0,
                "content_hash": digest,
            }

    # 旧版「我的收藏」目录
    yield from _scan_media_dir(
        account_path / "MyCollection", str(account_path), "legacy",
        pack_id="legacy-collection", pack_name="旧版我的收藏",
    )


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
SOURCE_SCANNERS = {
    "marketface": scan_marketface,
    "personal": scan_personal,
    "recv": scan_recv,
    "related": scan_related,
    "system": scan_system,
    "legacy": scan_legacy,
}


def available_sources(account_path: Path) -> dict[str, bool]:
    """检测某个账号下实际存在哪些来源。"""
    nt = account_path / "nt_qq" / "nt_data" / "Emoji"
    return {
        "marketface": (nt / "marketface").is_dir(),
        "personal": (nt / "personal_emoji").is_dir(),
        "recv": (nt / "emoji-recv").is_dir(),
        "related": (nt / "emoji-related").is_dir(),
        "system": (nt / "BaseEmojiSyastems").is_dir(),
        "legacy": (account_path / "CustomFace.db").is_file()
        or (account_path / "FaceStore.db").is_file()
        or (account_path / "MyCollection").is_dir(),
    }


def scan_account(
    account_path: Path,
    uin: str,
    sources: list[str] | None = None,
    progress: ProgressCb | None = None,
) -> Iterator[dict]:
    """扫描一个账号下勾选的所有来源。"""
    available = available_sources(account_path)
    chosen = sources or [s for s in SOURCE_SCANNERS if available.get(s)]
    for source in chosen:
        scanner = SOURCE_SCANNERS.get(source)
        if scanner is None or not available.get(source):
            continue
        label = config.SOURCE_LABELS.get(source, source)
        if progress:
            progress(f"扫描 {label}", 0, "")
        count = 0
        for record in scanner(account_path, uin, progress) if source in ("marketface", "legacy") else scanner(account_path, uin):
            count += 1
            if progress and count % 200 == 0:
                progress(f"扫描 {label}", count, "")
            yield record
        if progress:
            progress(f"扫描 {label}", count, "完成")
