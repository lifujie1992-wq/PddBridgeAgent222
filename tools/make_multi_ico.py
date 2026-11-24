# -*- coding: utf-8 -*-
"""把单尺寸 256x256 的 ico 重采样成多尺寸 ico（16/24/32/48/64/128/256）。

拼多多客户端只带了一个 256 的条目，直接给快捷方式用，桌面 32/48 尺寸会被系统
粗暴缩放（发糊）。这里用盒式平均自己缩，边缘干净得多。
"""
from __future__ import annotations

import struct
import sys

SIZES = (16, 24, 32, 48, 64, 128, 256)


def read_ico(path):
    data = open(path, "rb").read()
    count = struct.unpack_from("<H", data, 4)[0]
    entries = []
    for i in range(count):
        w, h, colors, _r, planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
        entries.append({"w": w or 256, "h": h or 256, "bpp": bpp,
                        "size": size, "offset": offset})
    return data, entries


def decode_bmp_entry(data, entry):
    """解出 (w, h, RGBA 行优先列表)。只支持 32bpp 无压缩。"""
    if entry["bpp"] != 32:
        raise SystemExit("只支持 32bpp，实际 %s" % entry["bpp"])
    off = entry["offset"]
    header = struct.unpack_from("<IiiHHIIiiII", data, off)
    width, height = header[1], header[2]
    width, height = abs(width), abs(height) // 2          # XOR + AND 合并高度
    pixels = []
    base = off + header[0]
    stride = width * 4
    for y in range(height - 1, -1, -1):                   # 底向上
        row = []
        start = base + y * stride
        for x in range(width):
            b, g, r, a = data[start + x * 4: start + x * 4 + 4]
            row.append((r, g, b, a))
        pixels.append(row)
    return width, height, pixels


def box_resize(width, height, pixels, size):
    """盒式平均缩放（带 alpha 预乘，避免透明边发黑）。"""
    out = []
    for oy in range(size):
        y0 = oy * height // size
        y1 = max(y0 + 1, (oy + 1) * height // size)
        row = []
        for ox in range(size):
            x0 = ox * width // size
            x1 = max(x0 + 1, (ox + 1) * width // size)
            r = g = b = a = 0
            n = 0
            for y in range(y0, y1):
                line = pixels[y]
                for x in range(x0, x1):
                    pr, pg, pb, pa = line[x]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
                    n += 1
            if a:
                row.append((r // a, g // a, b // a, a // n))
            else:
                row.append((0, 0, 0, 0))
        out.append(row)
    return out


def encode_entry(size, pixels):
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    body = bytearray()
    for y in range(size - 1, -1, -1):                      # 底向上
        for r, g, b, a in pixels[y]:
            body += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    return header + bytes(body) + b"\x00" * (mask_stride * size)


def write_ico(path, images):
    entries, blobs, offset = [], [], 6 + len(images) * 16
    for size, pixels in images:
        blob = encode_entry(size, pixels)
        entries.append((size, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    out = struct.pack("<HHH", 0, 1, len(images))
    for size, length, off in entries:
        out += struct.pack("<BBBBHHII", size if size < 256 else 0, size if size < 256 else 0,
                           0, 0, 1, 32, length, off)
    for blob in blobs:
        out += blob
    open(path, "wb").write(out)


def main(src, dst):
    data, entries = read_ico(src)
    big = max(entries, key=lambda e: e["w"])
    print("源尺寸: %dx%d %dbpp" % (big["w"], big["h"], big["bpp"]))
    width, height, pixels = decode_bmp_entry(data, big)
    images = []
    for size in SIZES:
        if size > width:
            continue
        images.append((size, box_resize(width, height, pixels, size)))
        print("  生成 %dx%d" % (size, size))
    write_ico(dst, images)
    print("输出:", dst)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
