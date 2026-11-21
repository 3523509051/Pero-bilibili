"""故事板解码与有时间戳拼图：Pillow 优先，PNG 标准库兜底。

真实故事板为 JPEG；插件环境固定安装 Pillow。标准库兜底只处理 PNG fixture，
不会把 JPEG 当成已解码图片，也不会伪造时间戳。
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path
from typing import Any, Sequence

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CELL_W, _CELL_H, _HEADER_H = 320, 180, 40
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    return Image, ImageDraw, ImageFont


TIMESTAMP_SUPPORT = _pil() is not None


def decode_image(data: bytes) -> tuple[int, int, bytes]:
    """解码故事板为 RGB；JPEG 必须有 Pillow，否则明确失败。"""

    modules = _pil()
    if modules is not None:
        image_cls = modules[0]
        with image_cls.open(io.BytesIO(data)) as image:
            rgb = image.convert("RGB")
            return rgb.width, rgb.height, rgb.tobytes()
    return decode_png(data)


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    if not isinstance(data, bytes) or not data.startswith(_SIGNATURE):
        raise ValueError("unsupported_image")
    position = 8
    width = height = color = depth = interlace = 0
    chunks: list[bytes] = []
    while position + 8 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        tag = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        position += 12 + length
        if tag == b"IHDR":
            width, height, depth, color, _compression, _filter, interlace = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            chunks.append(payload)
        elif tag == b"IEND":
            break
    if not (0 < width <= 8192 and 0 < height <= 8192) or depth != 8 or color not in {2, 6} or interlace:
        raise ValueError("unsupported_png")
    bpp = 3 if color == 2 else 4
    raw = zlib.decompress(b"".join(chunks))
    stride = width * bpp
    if len(raw) != (stride + 1) * height:
        raise ValueError("invalid_png_payload")
    output = bytearray(height * stride)
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind = raw[start]
        current = bytearray(raw[start + 1:start + 1 + stride])
        for index in range(stride):
            left = current[index - bpp] if index >= bpp else 0
            up = previous[index]
            upper_left = previous[index - bpp] if index >= bpp else 0
            if kind == 1:
                current[index] = (current[index] + left) & 0xFF
            elif kind == 2:
                current[index] = (current[index] + up) & 0xFF
            elif kind == 3:
                current[index] = (current[index] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                predictor = left + up - upper_left
                distances = (abs(predictor - left), abs(predictor - up), abs(predictor - upper_left))
                base = (left, up, upper_left)[distances.index(min(distances))]
                current[index] = (current[index] + base) & 0xFF
            elif kind != 0:
                raise ValueError("invalid_png_filter")
        output[row * stride:(row + 1) * stride] = current
        previous = current
    if bpp == 3:
        return width, height, bytes(output)
    rgb = bytearray(width * height * 3)
    for index in range(width * height):
        rgb[index * 3:index * 3 + 3] = output[index * 4:index * 4 + 3]
    return width, height, bytes(rgb)


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return (_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _scale(width: int, height: int, rgb: bytes, target_w: int, target_h: int) -> bytes:
    out = bytearray(target_w * target_h * 3)
    for y in range(target_h):
        source_y = min(height - 1, y * height // target_h)
        for x in range(target_w):
            source_x = min(width - 1, x * width // target_w)
            src = (source_y * width + source_x) * 3
            dst = (y * target_w + x) * 3
            out[dst:dst + 3] = rgb[src:src + 3]
    return bytes(out)


def _font(image_font, size: int):
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return image_font.truetype(candidate, size)
            except OSError:
                continue
    return image_font.load_default()


def build_sheet(frames: Sequence[dict], *, title: str = "", page: int = 1,
                max_bytes: int = 3 * 1024 * 1024) -> tuple[bytes, str]:
    """返回拼图字节与 mime；Pillow 路径绘制实际时间与标题。"""

    modules = _pil()
    count = max(1, len(frames))
    rows = (count + 1) // 2
    width, height = _CELL_W * 2, _HEADER_H + rows * _CELL_H
    if modules is None:
        canvas = bytearray(width * height * 3)
        for index in range(width * _HEADER_H):
            canvas[index * 3:index * 3 + 3] = (38, 62, 104)
        for position, frame in enumerate(frames):
            scaled = _scale(int(frame["width"]), int(frame["height"]), bytes(frame["rgb"]),
                            _CELL_W, _CELL_H)
            left, top = (position % 2) * _CELL_W, _HEADER_H + (position // 2) * _CELL_H
            for y in range(_CELL_H):
                start = ((top + y) * width + left) * 3
                canvas[start:start + _CELL_W * 3] = scaled[y * _CELL_W * 3:(y + 1) * _CELL_W * 3]
        return encode_png(width, height, bytes(canvas)), "image/png"

    image_cls, draw_cls, font_cls = modules
    canvas = image_cls.new("RGB", (width, height), (24, 28, 36))
    draw = draw_cls.Draw(canvas)
    draw.rectangle((0, 0, width, _HEADER_H), fill=(38, 62, 104))
    font = _font(font_cls, 17)
    label = f"P{page}  {title[:40]}"
    try:
        draw.text((12, 10), label, fill=(240, 244, 255), font=font)
    except UnicodeEncodeError:
        draw.text((12, 10), f"P{page}", fill=(240, 244, 255), font=font)
    for position, frame in enumerate(frames):
        tile = image_cls.frombytes("RGB", (int(frame["width"]), int(frame["height"])), bytes(frame["rgb"]))
        tile = tile.resize((_CELL_W, _CELL_H), image_cls.LANCZOS)
        left, top = (position % 2) * _CELL_W, _HEADER_H + (position // 2) * _CELL_H
        canvas.paste(tile, (left, top))
        seconds = float(frame.get("actual_seconds") or 0)
        stamp = f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"
        draw.rectangle((left + 6, top + _CELL_H - 28, left + 78, top + _CELL_H - 6), fill=(0, 0, 0))
        draw.text((left + 10, top + _CELL_H - 26), stamp, fill=(255, 255, 255), font=font)
    for quality in (85, 70, 55):
        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data, "image/jpeg"
    raise ValueError("sheet_too_large")
