"""测试共享辅助函数和数据。"""
from __future__ import annotations

import struct
import zlib


def minimal_png() -> bytes:
    """创建最小有效 PNG（1x1 红色像素），用于测试 OCR/图片处理路径。

    这是真实的 PNG 数据，可被 PIL/Pillow 正常打开。
    配合 mock 的 pytesseract 可测试完整 OCR 流程。
    """
    sig = b'\x89PNG\r\n\x1a\n'
    # 1x1 pixel, 8-bit depth, RGB color type
    ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    ihdr_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff)
    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + ihdr_crc
    # filter byte (0=none) + RGB pixel (red)
    raw_data = b'\x00\xff\x00\x00'
    compressed = zlib.compress(raw_data)
    idat_crc = struct.pack('>I', zlib.crc32(b'IDAT' + compressed) & 0xffffffff)
    idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + idat_crc
    iend_crc = struct.pack('>I', zlib.crc32(b'IEND') & 0xffffffff)
    iend = struct.pack('>I', 0) + b'IEND' + iend_crc
    return sig + ihdr + idat + iend
