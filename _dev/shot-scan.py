#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从截图里**机器定位**「竖排文字」。

不需要 OCR：横排文字的每个字只占 ~10-16px 高，所以「某个窄列上出现
>= 45px 的连续墨迹」基本只有两种可能 —— 竖排文字，或垂直分隔线/图标。
按墨迹宽度把后者滤掉，剩下的就是竖排文字的位置。

用法：python _dev/shot-scan.py <图片> [图片2 ...]
"""
import sys

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

RUN_MIN = 45          # 连续墨迹最短高度
BAND_W = 18           # 探测列宽
INK_MAX = 150         # 灰度阈值（小于它算墨迹）


def scan(path):
    im = Image.open(path).convert('L')
    a = np.asarray(im)
    h, w = a.shape
    print('=== %s  %dx%d' % (path.split('\\')[-1], w, h))
    ink = a < INK_MAX
    half = BAND_W // 2
    hits = []
    xs = range(half, w - half, 3)
    for x in xs:
        band = ink[:, x - half:x + half]
        rows = band.any(axis=1)
        # 找连续 True 段
        idx = np.flatnonzero(rows)
        if idx.size == 0:
            continue
        splits = np.flatnonzero(np.diff(idx) > 1)
        starts = np.r_[0, splits + 1]
        ends = np.r_[splits, idx.size - 1]
        for s, e in zip(starts, ends):
            y0, y1 = int(idx[s]), int(idx[e])
            if y1 - y0 + 1 < RUN_MIN:
                continue
            seg = ink[y0:y1 + 1, x - half:x + half]
            colsum = seg.sum(axis=0)
            inked_cols = np.flatnonzero(colsum > 0)
            if inked_cols.size == 0:
                continue
            real_w = int(inked_cols[-1] - inked_cols[0] + 1)
            density = seg.sum() / float(seg.size)
            hits.append((x, y0, y1, real_w, density))
    if not hits:
        print('  未发现竖排候选')
        return
    # 合并相邻 x 的同一段（同一块竖排会被多列重复命中）
    hits.sort()
    merged = []
    for x, y0, y1, rw, d in hits:
        for m in merged:
            if abs(m['x1'] - x) <= BAND_W and not (y1 < m['y0'] - 20 or y0 > m['y1'] + 20):
                m['x1'] = max(m['x1'], x)
                m['y0'] = min(m['y0'], y0)
                m['y1'] = max(m['y1'], y1)
                m['rw'] = max(m['rw'], rw)
                m['n'] += 1
                break
        else:
            merged.append({'x0': x, 'x1': x, 'y0': y0, 'y1': y1, 'rw': rw,
                           'd': d, 'n': 1})
    print('  竖排候选 %d 处：' % len(merged))
    for m in merged[:20]:
        bw = m['x1'] - m['x0'] + BAND_W
        bh = m['y1'] - m['y0'] + 1
        ratio = bh / float(max(bw, 1))
        print('   x=%d..%d y=%d..%d  墨迹宽≈%d 高=%d  高/宽=%.1f  列数=%d'
              % (m['x0'], m['x1'], m['y0'], m['y1'], m['rw'], bh, ratio, m['n']))


if __name__ == '__main__':
    for p in sys.argv[1:]:
        try:
            scan(p)
        except Exception as exc:            # noqa: BLE001
            print('=== %s  失败: %s' % (p, exc))
