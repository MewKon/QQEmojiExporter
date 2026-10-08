# -*- coding: utf-8 -*-
"""索引抽样校验：把每个来源的表情真正解码一遍，核对格式、尺寸与内容。

用法::

    python tools/verify.py            # 每个来源抽样 60 个
    python tools/verify.py --all      # 全部校验（较慢）
    python tools/verify.py -n 200     # 自定义抽样数量
"""

from __future__ import annotations

import argparse
import io
import random
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 源码模式下项目根没有 .cache 时，指向打包目录里的缓存，
# 这样测试/校验脚本可以直接复用打包版建好的索引。
_PACKAGED_CACHE = Path(__file__).resolve().parent.parent / "QQEmojiExporter" / ".cache"
if _PACKAGED_CACHE.is_dir() and not os.environ.get("QQEMOJI_CACHE"):
    os.environ["QQEMOJI_CACHE"] = str(_PACKAGED_CACHE)

from PIL import Image  # noqa: E402

from qqemoji import config, images, store  # noqa: E402

warnings.simplefilter("ignore")
Image.MAX_IMAGE_PIXELS = None

EXT_OF_FORMAT = {
    "PNG": ".png",
    "GIF": ".gif",
    "JPEG": ".jpg",
    "WEBP": ".webp",
    "BMP": ".bmp",
}


def check(record: dict) -> dict:
    result = {"ok": False, "reason": "", "format": "", "size": (0, 0), "frames": 1}
    try:
        data = images.Loader.load(record)
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"读取失败 {type(exc).__name__}: {exc}"
        return result
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format or ""
            size = im.size
            frames = getattr(im, "n_frames", 1)
            im.load()
            # 内容检查：抽样若干像素，确认不是纯色空白（动图只检查首帧，可能本身就是空帧）
            if frames <= 1:
                sample = im.convert("RGB").resize((8, 8))
                if len(set(sample.getdata())) <= 1:
                    result["reason"] = "内容为纯色，可能解码异常"
                    result.update(format=fmt, size=size, frames=frames)
                    return result
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"解码失败 {type(exc).__name__}: {exc}"
        return result

    expected_ext = EXT_OF_FORMAT.get(fmt, "")
    if expected_ext and record["ext"] and expected_ext != record["ext"]:
        if not (expected_ext == ".jpg" and record["ext"] in (".jpeg", ".jpg")):
            result["reason"] = f"格式不符：索引 {record['ext']} 实际 {expected_ext}"
            result.update(format=fmt, size=size, frames=frames)
            return result
    if record["width"] and record["height"] and tuple(size) != (record["width"], record["height"]):
        result["reason"] = f"尺寸不符：索引 {record['width']}x{record['height']} 实际 {size[0]}x{size[1]}"
        result.update(format=fmt, size=size, frames=frames)
        return result
    if bool(record["animated"]) != (frames > 1) and record["width"]:
        result["reason"] = f"动图标记不符：索引 animated={record['animated']} 实际帧数 {frames}"
        result.update(format=fmt, size=size, frames=frames)
        return result

    result.update(ok=True, format=fmt, size=size, frames=frames)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--number", type=int, default=60, help="每个来源抽样数量")
    parser.add_argument("--all", action="store_true", help="校验全部")
    parser.add_argument("--seed", type=int, default=20240607)
    args = parser.parse_args()

    random.seed(args.seed)
    index = store.get_index()
    stats = index.stats()
    if not stats["total"]:
        print("索引为空，请先运行：python app.py --scan")
        return 1

    print(f"索引中共有 {stats['total']} 个表情，开始抽样校验…\n")
    overall_fail = 0
    for source in config.SOURCES:
        rows = index.all_matching({"sources": [source]})
        if not rows:
            continue
        sample = rows if args.all else random.sample(rows, min(args.number, len(rows)))
        ok = 0
        fails: list[tuple[dict, str]] = []
        formats: dict[str, int] = {}
        animated = 0
        for record in sample:
            res = check(record)
            if res["ok"]:
                ok += 1
                formats[res["format"]] = formats.get(res["format"], 0) + 1
                if res["frames"] > 1:
                    animated += 1
            else:
                fails.append((record, res["reason"]))
        overall_fail += len(fails)
        rate = ok * 100.0 / len(sample)
        fmt_text = " ".join(f"{k}:{v}" for k, v in sorted(formats.items()))
        print(f"[{config.SOURCE_LABELS[source]}] 抽样 {len(sample)} 个，通过 {ok}（{rate:.1f}%），动图 {animated}")
        print(f"    格式分布：{fmt_text or '—'}")
        for record, reason in fails[:5]:
            print(f"    ✗ {record['id']} {Path(record['path']).name} → {reason}")
        if len(fails) > 5:
            print(f"    … 其余 {len(fails) - 5} 个失败项已省略")

    print()
    if overall_fail:
        print(f"校验结束：共 {overall_fail} 个问题项。")
        return 2
    print("校验通过：所有抽样表情都能正确解码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
