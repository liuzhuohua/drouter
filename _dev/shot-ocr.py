#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Windows 内置 OCR（WinRT）读截图，输出「文字 + 像素包围盒」。

为什么需要：模型看不了图，但排版问题的判据其实是**几何** ——
「一列一个字」的文字块包围盒一定又高又窄。OCR 给出 bbox 就能机器判定。

大图先按 TILE_H 切片（OcrEngine 对单张图有尺寸上限），坐标换算回原图。
切片用 Pillow 做（WinRT 的 BitmapBounds 字段在 PyWinRT 里是只读的）。

用法：python _dev/shot-ocr.py <图片> [最小可疑比例] [输出tsv]
"""
import asyncio
import io
import os
import sys
import tempfile

from PIL import Image
from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage import FileAccessMode, StorageFile

Image.MAX_IMAGE_PIXELS = None
TILE_H = 1100
TILE_OVERLAP = 60


def winpath(p):
    """WinRT 的 StorageFile 只吃反斜杠 Win32 路径，给正斜杠会报
    UNABLE_TO_MASK_PATH（路径过长 32831528 那种鬼错误）。"""
    return os.path.abspath(p).replace('/', '\\')


async def ocr_file(engine, path):
    f = await StorageFile.get_file_from_path_async(winpath(path))
    s = await f.open_async(FileAccessMode.READ)
    dec = await BitmapDecoder.create_async(s)
    bmp = await dec.get_software_bitmap_async()
    res = await engine.recognize_async(bmp)
    out = []
    for ln in res.lines:
        xs, ys, xe, ye = [], [], [], []
        for w in ln.words:
            r = w.bounding_rect
            xs.append(r.x)
            ys.append(r.y)
            xe.append(r.x + r.width)
            ye.append(r.y + r.height)
        if not xs:
            continue
        out.append({'t': ln.text, 'x': int(min(xs)), 'y': int(min(ys)),
                    'w': int(max(xe) - min(xs)), 'h': int(max(ye) - min(ys))})
    return out


async def main():
    path = sys.argv[1]
    ratio_min = float(sys.argv[2]) if len(sys.argv) > 2 else 1.6
    tsv = sys.argv[3] if len(sys.argv) > 3 else ''
    engine = OcrEngine.try_create_from_user_profile_languages()
    print('engine lang = %s' % engine.recognizer_language.language_tag)

    im = Image.open(path)
    W, H = im.size
    print('=== %s  %dx%d' % (os.path.basename(path), W, H))

    tmp = os.path.join(tempfile.gettempdir(), '_ocr_tile.png')
    all_lines = []
    y = 0
    while y < H:
        y1 = min(y + TILE_H, H)
        im.crop((0, y, W, y1)).save(tmp, 'PNG')
        for ln in await ocr_file(engine, tmp):
            ln['y'] += y
            dup = any(abs(ln['y'] - o['y']) < 6 and ln['t'] == o['t']
                      for o in all_lines)
            if not dup:
                all_lines.append(ln)
        y += TILE_H - TILE_OVERLAP

    all_lines.sort(key=lambda r: (r['y'], r['x']))
    print('识别到 %d 行文字' % len(all_lines))
    sus = []
    for r in all_lines:
        r['ratio'] = r['h'] / float(max(r['w'], 1))
        tag = ''
        if r['ratio'] >= ratio_min and len(r['t']) >= 2:
            tag = '   <== 又高又窄（疑似竖排）'
            sus.append(r)
        print('  y=%-6d x=%-5d w=%-5d h=%-4d r=%.2f | %s%s'
              % (r['y'], r['x'], r['w'], r['h'], r['ratio'], r['t'][:60], tag))
    print('可疑（高/宽 >= %.1f 且 >=2 字）共 %d 行' % (ratio_min, len(sus)))
    if tsv:
        with io.open(tsv, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write('y\tx\tw\th\tratio\ttext\n')
            for r in all_lines:
                fh.write('%d\t%d\t%d\t%d\t%.2f\t%s\n'
                         % (r['y'], r['x'], r['w'], r['h'], r['ratio'], r['t']))
        print('TSV -> %s' % tsv)


asyncio.run(main())
