"""图片读取、探测与缩略图生成。"""

from __future__ import annotations

import io
import os
from pathlib import Path

from . import config, deobf

try:
    from PIL import Image, ImageSequence

    Image.MAX_IMAGE_PIXELS = None
    _HAS_PIL = True
except ImportError:  # pragma: no cover - 依赖缺失时降级
    Image = None
    ImageSequence = None
    _HAS_PIL = False

# 超过这个像素数的图片不做完整解码（防止内存爆炸）
_MAX_PIXELS = 60_000_000


class Loader:
    """按索引记录读取表情的原始字节。

    索引里的 ``path`` + ``offset`` / ``length`` 指向真实的存储位置：
    普通来源就是磁盘上的文件；旧版数据库雕刻出来的表情则指向
    ``CustomFace.db`` 里的某一段。
    """

    @staticmethod
    def read_raw(record: dict) -> bytes:
        path = record["path"]
        offset = record.get("offset")
        length = record.get("length")
        with open(path, "rb") as fh:
            if offset is not None and length:
                fh.seek(int(offset))
                return fh.read(int(length))
            return fh.read()

    @classmethod
    def load(cls, record: dict) -> bytes:
        """读取并（必要时）解混淆后的图片字节。"""
        data = cls.read_raw(record)
        if record.get("obfuscated"):
            data = deobf.deobfuscate(data)
        return data


# --------------------------------------------------------------------------- #
# 动图：用 Pillow 自带的 ImageSequence 组件按帧处理
# --------------------------------------------------------------------------- #
# 扫描帧时长时的上限，避免几千帧的 GIF 卡住
DURATION_SCAN_LIMIT = 150
# 超过这个体积就只读第一帧的时长（逐帧 seek 在大 GIF 上要一秒多）
DURATION_SCAN_BYTES = 1_500_000


def describe_animation(data: bytes) -> dict:
    """用 Pillow 的 ``ImageSequence`` 读取动图信息（不解码全部像素）。

    GIF 的帧数优先用 :func:`qqemoji.probe.gif_walk` 直接扫块结构得到——
    Pillow 的 ``n_frames`` 会把整个文件扫一遍，大 GIF 上要一秒多。

    :returns: ``{frames, durations, size, animated}``
    """
    info = {"frames": 1, "durations": [80], "size": (0, 0), "animated": False}
    if not _HAS_PIL:
        return info
    frames = None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        try:
            from . import probe as probe_mod

            counted, complete = probe_mod.gif_walk(data)
            if complete:
                frames = counted
        except Exception:
            frames = None
    try:
        with Image.open(io.BytesIO(data)) as im:
            total = int(frames or getattr(im, "n_frames", 1) or 1)
            total = max(1, total)
            info["size"] = im.size
            info["frames"] = total
            info["animated"] = total > 1
            if total > 1:
                # 逐帧读时长要反复 seek（大 GIF 上很慢），因此只在文件不大或帧数不多时全扫，
                # 否则只取第一帧的时长并假定各帧一致（QQ 表情几乎都是固定帧间隔）。
                scan_all = total <= DURATION_SCAN_LIMIT and len(data) <= DURATION_SCAN_BYTES
                limit = total if scan_all else 1
                durations: list[int] = []
                for index, frame in enumerate(ImageSequence.Iterator(im)):
                    if index >= limit:
                        break
                    durations.append(max(30, int(frame.info.get("duration", 80) or 80)))
                if durations:
                    if not scan_all:
                        durations = durations * total  # 统一时长，供取模使用
                    info["durations"] = durations
    except Exception:
        pass
    return info


def load_frame(data: bytes, index: int) -> Image.Image | None:
    """按需解码动图的第 ``index`` 帧（内存里只保留这一帧）。"""
    if not _HAS_PIL:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            total = max(1, int(getattr(im, "n_frames", 1)))
            im.seek(index % total)
            return im.convert("RGBA")
    except Exception:
        return None


def probe(data: bytes) -> dict:
    """探测图片信息：格式、尺寸、帧数。"""
    if not _HAS_PIL:
        ext, mime = deobf.sniff(data)
        return {"width": 0, "height": 0, "frames": 1, "animated": False, "format": mime, "ext": ext}
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            frames = getattr(im, "n_frames", 1)
            fmt = im.format or ""
            return {
                "width": width,
                "height": height,
                "frames": frames,
                "animated": frames > 1,
                "format": fmt,
                "ext": deobf.sniff(data)[0],
            }
    except Exception:
        ext, mime = deobf.sniff(data)
        return {"width": 0, "height": 0, "frames": 1, "animated": False, "format": mime, "ext": ext}


def make_thumbnail(data: bytes, dest_dir: Path, key: str, size: int = config.THUMB_SIZE) -> tuple[Path | None, dict]:
    """生成缩略图，返回 (缩略图路径, 图片信息)。"""
    info = probe(data)
    if not _HAS_PIL:
        return None, info
    if info["width"] * info["height"] > _MAX_PIXELS:
        return None, info

    webp = dest_dir / f"{key}.webp"
    png = dest_dir / f"{key}.png"
    if webp.exists():
        return webp, info
    if png.exists():
        return png, info

    try:
        with Image.open(io.BytesIO(data)) as im:
            try:
                im.seek(0)
            except Exception:
                pass
            if im.mode in ("P", "PA", "LA"):
                im = im.convert("RGBA")
            elif im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGB")
            im.thumbnail((size, size), Image.LANCZOS)

            tmp = dest_dir / f".{key}.tmp"
            try:
                im.save(tmp, "WEBP", quality=88, method=4)
                os.replace(tmp, webp)
                return webp, info
            except Exception:
                try:
                    im.save(tmp, "PNG")
                    os.replace(tmp, png)
                    return png, info
                except Exception:
                    tmp.unlink(missing_ok=True)
                    return None, info
    except Exception:
        return None, info


def convert_image(data: bytes, target: str, max_side: int = 0) -> tuple[bytes, str]:
    """把图片转换成指定格式，返回 (字节, 扩展名)。

    :param target: ``origin`` 保持原样；``png``/``webp``/``gif``/``jpg`` 为目标格式
    :param max_side: 大于 0 时限制最长边（动图会逐帧缩放，输出到 PNG/JPG 时只保留首帧）
    """
    if not _HAS_PIL or target in ("", "origin", "keep"):
        if not max_side:
            return data, deobf.sniff(data)[0]
        target = deobf.sniff(data)[0].lstrip(".") or "png"

    target = target.lower().lstrip(".")
    if target == "jpeg":
        target = "jpg"
    try:
        with Image.open(io.BytesIO(data)) as im:
            frames = getattr(im, "n_frames", 1)
            animated = frames > 1
            animated_target = target in ("gif", "webp")

            if animated and animated_target:
                out = _convert_animated(im, frames, target, max_side)
                if out is not None:
                    return out

            if max_side and max(im.size) > max_side:
                im.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            if target == "png":
                if im.mode not in ("RGB", "RGBA", "L", "P"):
                    im = im.convert("RGBA")
                im.save(buf, "PNG")
                return buf.getvalue(), ".png"
            if target == "webp":
                im.save(buf, "WEBP", quality=92)
                return buf.getvalue(), ".webp"
            if target == "jpg":
                im = _flatten(im)
                im.save(buf, "JPEG", quality=92)
                return buf.getvalue(), ".jpg"
            if target == "gif":
                if im.mode not in ("RGB", "RGBA", "P"):
                    im = im.convert("RGB")
                im.save(buf, "GIF")
                return buf.getvalue(), ".gif"
    except Exception:
        pass
    return data, deobf.sniff(data)[0]


def _flatten(im):
    """把带透明通道的图片铺到白底上，便于保存为 JPG。"""
    if im.mode in ("RGBA", "P", "LA"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if im.mode != "RGB":
        return im.convert("RGB")
    return im


def _convert_animated(im, frames: int, target: str, max_side: int) -> tuple[bytes, str] | None:
    """逐帧缩放后保存动图；帧数过多或体积过大时放弃（返回 None 走单帧逻辑）。"""
    if frames > 240:
        return None
    if im.size[0] * im.size[1] * frames > 200_000_000:
        return None
    try:
        rendered = []
        durations = []
        for index in range(frames):
            im.seek(index)
            frame = im.convert("RGBA")
            if max_side and max(frame.size) > max_side:
                frame.thumbnail((max_side, max_side), Image.LANCZOS)
            rendered.append(frame.copy())
            durations.append(im.info.get("duration", 60) or 60)
        buf = io.BytesIO()
        if target == "gif":
            rendered[0].save(
                buf, "GIF", save_all=True, append_images=rendered[1:],
                duration=durations, loop=im.info.get("loop", 0), disposal=2,
            )
            return buf.getvalue(), ".gif"
        rendered[0].save(
            buf, "WEBP", save_all=True, append_images=rendered[1:],
            duration=durations, loop=im.info.get("loop", 0), quality=88,
        )
        return buf.getvalue(), ".webp"
    except Exception:
        return None
