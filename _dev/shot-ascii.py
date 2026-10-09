#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把截图降采样成 ASCII 图打印出来 —— 用文字「看」布局。

⚠️ 必须用 **max-pooling（任一像素有墨就算有墨）**，不能用均值/LANCZOS：
界面文字只有 1~2px 的笔画，一平均就被白色背景冲淡，整张图会变成一片空白
（第一版踩的就是这个坑）。

用法：
  python _dev/shot-ascii.py <图片> [cols] [x y w h] [thresh] [invert]
    cols    每行字符数（默认 150）
    x y w h 只渲染这块区域
    thresh  墨迹阈值（默认 200，灰度 < thresh 算墨迹）
    invert  传 1 表示反色（深色底界面）
"""
import sys

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def pool(ink, rows, cols):
    h, w = ink.shape
    ys = np.linspace(0, h, rows + 1).astype(int)
    xs = np.linspace(0, w, cols + 1).astype(int)
    out = np.zeros((rows, cols), dtype=bool)
    for r in range(rows):
        band = ink[ys[r]:max(ys[r] + 1, ys[r + 1]), :]
        if band.size == 0:
            continue
        for c in range(cols):
            cell = band[:, xs[c]:max(xs[c] + 1, xs[c + 1])]
            if cell.size and cell.any():
                out[r, c] = True
    return out


def render(path, cols=150, box=None, thresh=200, invert=False):
    im = Image.open(path).convert('L')
    if box:
        x, y, w, h = box
        im = im.crop((x, y, x + w, y + h))
    w, h = im.size
    a = np.asarray(im)
    ink = (a > thresh) if invert else (a < thresh)
    rows = max(1, int(round(h / float(w) * cols * 0.5)))
    art = pool(ink, rows, cols)
    print('--- %s crop=%s 原图 %dx%d 输出 %dx%d 墨迹 %.2f%%' % (
        path.split('\\')[-1], box or 'full', w, h, cols, rows,
        100.0 * ink.mean()))
    for r in range(rows):
        line = ''.join('#' if art[r, c] else ' ' for c in range(cols))
        print(line.rstrip())
    print('--- 列标尺（每 10 格）: ' + ''.join(
        str((i // 10) % 10) if i % 10 == 0 else ' ' for i in range(cols)))


if __name__ == '__main__':
    p = sys.argv[1]
    cols = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    box = None
    if len(sys.argv) > 6:
        box = tuple(int(v) for v in sys.argv[3:7])
    th = int(sys.argv[7]) if len(sys.argv) > 7 else 200
    inv = len(sys.argv) > 8 and sys.argv[8] == '1'
    render(p, cols, box, th, inv)
