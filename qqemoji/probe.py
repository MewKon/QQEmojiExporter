"""只需要文件头就能完成的轻量探测：格式、尺寸、是否动图。

不依赖 Pillow 解码整张图，因此可以在扫描阶段对几万个文件全量执行。
只需读取文件的前 64KB（市场表情包需要先解混淆这一段）。
"""

from __future__ import annotations

import struct

HEAD_BYTES = 8192
# GIF 需要走一遍块结构才能准确数出帧数，小文件直接读全（市场表情包都在 200KB 内）
GIF_EXACT_LIMIT = 256 * 1024

_MIME = {
    ".png": "image/png",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _png(data: bytes) -> dict:
    width = height = 0
    if len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
    animated = b"acTL" in data  # APNG 的动画控制块
    return {
        "ext": ".png",
        "mime": "image/png",
        "width": width,
        "height": height,
        "animated": animated,
        "frames": 2 if animated else 1,
    }


def _skip_sub_blocks(data: bytes, pos: int) -> int:
    """跳过 GIF 的若干数据子块，返回下一个块的位置。"""
    size = len(data)
    while pos < size:
        block = data[pos]
        pos += 1
        if block == 0:
            break
        pos += block
    return pos


def gif_walk(data: bytes) -> tuple[int, bool]:
    """走一遍 GIF 的块结构，返回 ``(帧数, 是否完整解析完)``。"""
    size = len(data)
    if size < 13:
        return 1, False
    flags = data[10]
    pos = 13
    if flags & 0x80:  # 全局调色板
        pos += 3 * (1 << ((flags & 0x07) + 1))
    frames = 0
    while pos < size:
        marker = data[pos]
        if marker == 0x3B:  # 结束符
            return max(frames, 1), True
        if marker == 0x21:  # 扩展块
            if pos + 2 > size:
                break
            pos = _skip_sub_blocks(data, pos + 2)
            continue
        if marker == 0x2C:  # 图像描述符
            frames += 1
            if pos + 10 > size:
                return frames, False
            packed = data[pos + 9]
            pos += 10
            if packed & 0x80:  # 局部调色板
                pos += 3 * (1 << ((packed & 0x07) + 1))
            if pos >= size:
                return frames, False
            pos += 1  # LZW 最小码长
            pos = _skip_sub_blocks(data, pos)
            continue
        break
    return max(frames, 1), False


def _gif(data: bytes) -> dict:
    width = height = 0
    if len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
    frames, complete = gif_walk(data)
    if complete:
        animated = frames > 1
    else:
        # 数据被截断：已经看到多帧可以确定，否则退化为「带循环扩展即视为动图」
        animated = frames > 1 or b"NETSCAPE2.0" in data[:HEAD_BYTES]
    return {
        "ext": ".gif",
        "mime": "image/gif",
        "width": width,
        "height": height,
        "animated": animated,
        "frames": frames if complete else (2 if animated else 1),
    }


def _jpeg(data: bytes) -> dict:
    width = height = 0
    pos = 2
    size = len(data)
    while pos + 9 < size:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xD9:
            break
        if pos + 4 > size:
            break
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 9 <= size:
                height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            break
        pos += 2 + max(length, 2)
    return {"ext": ".jpg", "mime": "image/jpeg", "width": width, "height": height, "animated": False}


def _webp(data: bytes) -> dict:
    width = height = 0
    animated = False
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        flags = data[20]
        animated = bool(flags & 0x02)
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
    elif chunk == b"VP8 " and len(data) >= 30:
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
    elif chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    if b"ANIM" in data[:64]:
        animated = True
    return {"ext": ".webp", "mime": "image/webp", "width": width, "height": height, "animated": animated}


def _bmp(data: bytes) -> dict:
    width = height = 0
    if len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        height = abs(height)
    return {"ext": ".bmp", "mime": "image/bmp", "width": width, "height": height, "animated": False}


def probe_header(data: bytes) -> dict:
    """返回 ``{ext, mime, width, height, animated}``；无法识别时 ext 为 ``.bin``。"""
    if len(data) < 16:
        return {"ext": ".bin", "mime": "application/octet-stream", "width": 0, "height": 0,
                "animated": False, "frames": 1}
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(data)
    if data.startswith(b"GIF8"):
        return _gif(data)
    if data.startswith(b"\xff\xd8\xff"):
        return _jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    if data.startswith(b"BM"):
        return _bmp(data)
    return {"ext": ".bin", "mime": "application/octet-stream", "width": 0, "height": 0,
            "animated": False, "frames": 1}


def mime_for(ext: str) -> str:
    return _MIME.get(ext.lower(), "application/octet-stream")
