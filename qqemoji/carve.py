"""从旧版 QQ 的表情数据库里雕刻图片。

旧版 QQ（也存在于 NTQQ 账号目录中）把自定义表情以 OLE 复合文档的形式存进
``CustomFace.db`` / ``FaceStore.db``。这两个文件使用了被改动过的 DIFAT 结构，
``olefile`` 之类的库无法直接解析，但图片数据本身是明文存放的，因此这里采用
**按签名雕刻** 的方式：扫描 JPEG/PNG/GIF 的起止标记并逐个校验。
"""

from __future__ import annotations

import io
from pathlib import Path

SOI = b"\xff\xd8\xff"
EOI = b"\xff\xd9"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
IEND = b"IEND\xae\x42\x60\x82"
GIF_SIGS = (b"GIF87a", b"GIF89a")
GIF_TRAILER = b"\x3b"

# 单个被雕刻出来的图片的最小/最大尺寸限制
MIN_BLOB = 256
MAX_BLOB = 24 * 1024 * 1024


def _find_spans(buf: bytes, start: bytes, end: bytes) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        a = buf.find(start, pos)
        if a < 0:
            break
        b = buf.find(end, a + len(start))
        if b < 0:
            break
        b += len(end)
        spans.append((a, b))
        pos = b
    return spans


def carve_spans(buf: bytes) -> list[tuple[int, int]]:
    """返回所有候选图片的字节区间（按偏移排序）。"""
    spans: list[tuple[int, int]] = []
    spans.extend(_find_spans(buf, SOI, EOI))
    spans.extend(_find_spans(buf, PNG_SIG, IEND))
    for sig in GIF_SIGS:
        spans.extend(_find_spans(buf, sig, GIF_TRAILER))
    spans = sorted(set(spans))
    return [(a, b) for a, b in spans if MIN_BLOB <= b - a <= MAX_BLOB]


def carve_file(path: str | Path, validate: bool = True) -> list[tuple[bytes, str, str, int]]:
    """雕刻一个数据库文件里的图片。

    :returns: ``[(图片字节, 扩展名, MIME, 源偏移), ...]``
    """
    from . import deobf

    raw = Path(path).read_bytes()
    results: list[tuple[bytes, str, str, int]] = []
    seen: set[bytes] = set()
    for a, b in carve_spans(raw):
        blob = raw[a:b]
        ext, mime = deobf.sniff(blob)
        if ext == ".bin":
            continue
        if validate and not _validate(blob):
            continue
        digest = blob[:64] + len(blob).to_bytes(4, "little")
        if digest in seen:
            continue
        seen.add(digest)
        results.append((blob, ext, mime, a))
    return results


def _validate(blob: bytes) -> bool:
    """用 Pillow 校验图片能否真正解码（不可用时退化为弱校验）。"""
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None
        with Image.open(io.BytesIO(blob)) as im:
            im.load()
        return True
    except ImportError:
        return True
    except Exception:
        return False
