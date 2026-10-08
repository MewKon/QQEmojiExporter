"""QQ 表情文件解码。

NTQQ 的「市场表情包」原图（``nt_data/Emoji/marketface/<包ID>/<md5>``，无扩展名）
做过一层轻量混淆：**每 50 字节为一组，前 20 字节与 0xFF 异或，后 30 字节原样保留**，
不足一组时按同样规则处理剩余部分。

例如 PNG 文件的开头 ``89 50 4E 47`` 被写成 ``76 AF B1 B8``，中间的 IHDR 数据区
（第 20~49 字节）仍是明文，因此直接看会呈现「乱码 + 明文交替」的样子。
"""

from __future__ import annotations

from pathlib import Path

KEEP_BYTES = 30
XOR_BYTES = 20
XOR_MASK = 0xFF
BLOCK = KEEP_BYTES + XOR_BYTES  # 50

# 明文文件头 -> (扩展名, MIME)
_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"RIFF", ".webp", "image/webp"),  # 需再校验 WEBP 标记
    (b"BM", ".bmp", "image/bmp"),
    (b"II*\x00", ".tif", "image/tiff"),
    (b"MM\x00*", ".tif", "image/tiff"),
]

# 混淆后的文件头（明文签名逐字节取反）
_OBFUSCATED_HEADS = [bytes(b ^ XOR_MASK for b in sig[:8]) for sig, _, _ in _SIGNATURES]


def deobfuscate(data: bytes) -> bytes:
    """还原市场表情包原图。"""
    out = bytearray(len(data))
    pos = 0
    length = len(data)
    while pos < length:
        # 前 20 字节：与 0xFF 异或
        end = pos + XOR_BYTES
        if end > length:
            end = length
        for i in range(pos, end):
            out[i] = data[i] ^ XOR_MASK
        pos = end
        # 后 30 字节：原样保留
        end = pos + KEEP_BYTES
        if end > length:
            end = length
        out[pos:end] = data[pos:end]
        pos = end
    return bytes(out)


def is_obfuscated(data: bytes) -> bool:
    """判断字节流是否看起来是市场表情包的混淆格式。"""
    head = data[:8]
    return any(head.startswith(h[: len(head)]) for h in _OBFUSCATED_HEADS)


def sniff(data: bytes) -> tuple[str, str]:
    """根据文件头判断 (扩展名, MIME)。无法识别时返回 ('.bin', 'application/octet-stream')。"""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    for sig, ext, mime in _SIGNATURES:
        if data.startswith(sig):
            if sig == b"RIFF":
                continue
            return ext, mime
    return ".bin", "application/octet-stream"


def decode_bytes(data: bytes, obfuscated: bool | None = None) -> tuple[bytes, str, str]:
    """按需解混淆并识别格式。

    :param obfuscated: ``True`` 强制解混淆，``False`` 跳过，``None`` 自动判断
    :returns: ``(解码后的字节, 扩展名, MIME)``
    """
    if obfuscated is None:
        obfuscated = is_obfuscated(data)
    if obfuscated:
        data = deobfuscate(data)
    ext, mime = sniff(data)
    return data, ext, mime


def decode_file(path: str | Path, obfuscated: bool | None = None) -> tuple[bytes, str, str]:
    """读取文件并按需解混淆。"""
    raw = Path(path).read_bytes()
    return decode_bytes(raw, obfuscated=obfuscated)
