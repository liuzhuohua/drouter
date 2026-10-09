#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析 shot-ocr.py 产出的 TSV，机器判定截图里的排版异常。

判据：
  ① 真竖排：同一 x（±12px）上堆叠 >=3 个**窄**文字块（宽 <= 60px），
     且相邻 y 间距 <= 42px  →  一列一个字。
  ② 又高又窄的单行：h/w >= 0.8 且 h >= 28px。
  ③ 超长间隔的「标签 → 数值」：同一 y 带内，左侧文字右边缘与右侧文字
     左边缘相距 > 700px（宽屏下 label 贴左、value 贴右的观感问题）。
"""
import glob
import io
import sys


def load(f):
    rows = []
    for ln in list(io.open(f, encoding='utf-8'))[1:]:
        p = ln.rstrip('\n').split('\t')
        if len(p) != 6:
            continue
        rows.append({'y': int(p[0]), 'x': int(p[1]), 'w': int(p[2]),
                     'h': int(p[3]), 'r': float(p[4]), 't': p[5]})
    rows.sort(key=lambda r: (r['y'], r['x']))
    return rows


def find_columns(rows):
    """窄块 + 同列 + 纵向密排 = 竖排"""
    narrow = [r for r in rows if r['w'] <= 60 and r['h'] >= 9 and len(r['t']) >= 1]
    used = set()
    cols = []
    for i, a in enumerate(narrow):
        if i in used:
            continue
        col = [a]
        cy = a['y']
        for j in range(i + 1, len(narrow)):
            b = narrow[j]
            if j in used:
                continue
            if abs(b['x'] - a['x']) <= 12 and 0 < b['y'] - cy <= 42:
                col.append(b)
                used.add(j)
                cy = b['y']
        if len(col) >= 3:
            used.add(i)
            cols.append(col)
    return cols


def main():
    for f in sorted(glob.glob('_dev/.ocr-xx*.tsv')):
        rows = load(f)
        name = f.split('.ocr-')[1][:-4]
        print('=' * 78)
        print('%s  共 %d 行文字' % (name, len(rows)))
        print('  页首：' + ' / '.join(r['t'][:20] for r in rows[:5]))
        cols = find_columns(rows)
        print('  ① 竖排候选列：%d' % len(cols))
        for c in cols[:6]:
            print('     x≈%d y=%d..%d  %d 块 | %s'
                  % (c[0]['x'], c[0]['y'], c[-1]['y'], len(c),
                     ' '.join(b['t'][:4] for b in c[:8])))
        tall = [r for r in rows if r['r'] >= 0.8 and r['h'] >= 28 and len(r['t']) >= 2]
        print('  ② 又高又窄单行：%d' % len(tall))
        for r in tall[:6]:
            print('     y=%d x=%d w=%d h=%d | %s' % (r['y'], r['x'], r['w'], r['h'], r['t'][:40]))
        # ③ 同一 y 带内左右相距很远的文字
        far = 0
        ex = []
        for a in rows:
            for b in rows:
                if b is a or b['x'] <= a['x']:
                    continue
                if abs(b['y'] - a['y']) <= 6 and (b['x'] - (a['x'] + a['w'])) > 700:
                    far += 1
                    if len(ex) < 4:
                        ex.append('y=%d 「%s」…「%s」相距 %dpx'
                                  % (a['y'], a['t'][:14], b['t'][:14],
                                     b['x'] - (a['x'] + a['w'])))
                    break
        print('  ③ 左右相距 >700px 的同行文字对：%d' % far)
        for e in ex:
            print('     ' + e)
        # 最右的文字（看内容是否铺满整屏）
        right = max((r['x'] + r['w']) for r in rows) if rows else 0
        print('  最右文字边缘 x=%d（截图宽 1908）' % right)


if __name__ == '__main__':
    sys.exit(main())
