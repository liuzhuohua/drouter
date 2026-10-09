#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""t-layout 判据的反向验证：逐个把修复点改坏，确认对应判据变红。

一次只注入一处 —— 一次注入多处时中间某处静默跳过会给出假绿（这条踩过）。
改的是工作区真实文件，跑完立刻还原（finally）。
"""
import io
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, 'web', 'app.css')
APP = os.path.join(ROOT, 'web', 'app.js')
PY = sys.executable

MUTS = [
    ('app.css', 'th,td 去掉 keep-all',
     'vertical-align:top;word-break:keep-all;overflow-wrap:break-word',
     'vertical-align:top;overflow-wrap:break-word',
     'th,td 用 word-break:keep-all'),
    ('app.css', 'th 去掉 nowrap',
     'background:#fafbfe;position:sticky;top:0;\n  white-space:nowrap}',
     'background:#fafbfe;position:sticky;top:0}',
     'th 用 white-space:nowrap'),
    ('app.css', '.tag 改回 nowrap',
     'white-space:normal;word-break:keep-all;overflow-wrap:break-word}',
     'white-space:nowrap;word-break:normal}',
     '.tag 用 word-break:keep-all'),
    ('app.css', '.row>label 去掉 keep-all',
     '.row>label{word-break:keep-all}', '.row>label{word-break:normal}',
     '.row>label 用 word-break:keep-all'),
    ('app.css', '#al-rules>.dep-item 改回 flex',
     '#al-rules>.dep-item{display:block}', '#al-rules>.dep-item{display:flex}',
     '#al-rules>.dep-item 是块级'),
    ('app.css', '.dk-trow>* 去掉 min-width:0',
     '.dk-trow>*{min-width:0;overflow-wrap:break-word}',
     '.dk-trow>*{overflow-wrap:break-word}',
     '.dk-trow 单元格可断行'),
    ('app.css', '.ul-trow>* 去掉 min-width:0',
     '.ul-trow>*{min-width:0;overflow-wrap:break-word}',
     '.ul-trow>*{overflow-wrap:break-word}',
     '.ul-trow 单元格可断行'),
    ('app.css', '.dep-item 去掉 flex-wrap',
     'gap:11px;flex-wrap:wrap;padding:11px 13px', 'gap:11px;padding:11px 13px',
     '.dep-item 允许换行'),
    ('app.css', '.dep-body 改回裸 flex:1',
     '.dep-item .dep-body{flex:1 1 200px;min-width:0}',
     '.dep-item .dep-body{flex:1;min-width:0}',
     '.dep-item .dep-body 的 flex-basis 有像素下限'),
    ('app.js', 'app.js 加回内联 flex:1',
     'class="dep-body" style="flex:1 1 200px"', 'class="dep-body" style="flex:1"',
     '内联 style="flex:1"'),
    ('app.css', '.switch 去掉 flex-wrap',
     'gap:8px;flex-wrap:wrap;cursor:pointer', 'gap:8px;cursor:pointer',
     '.switch 允许换行'),
    ('app.css', '.switch b 去掉不收缩',
     '.switch b{flex:0 0 auto}', '.switch b{flex:0 1 auto}',
     '.switch b 不收缩'),
]

FILES = {'app.css': CSS, 'app.js': APP}


def run():
    p = subprocess.run([PY, os.path.join(ROOT, '_dev', 't-layout.py')],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    return p.returncode, (p.stdout or '') + (p.stderr or '')


orig = {k: io.open(v, encoding='utf-8').read() for k, v in FILES.items()}
bak = {k: v + '.rvbak' for k, v in FILES.items()}
for k, v in FILES.items():
    shutil.copyfile(v, bak[k])

rc, out = run()
print('基线: rc=%s  %s' % (rc, 'LAYOUT_OK' if 'LAYOUT_OK' in out else '异常!'))
if rc != 0:
    print('⚠️ 基线不干净，先停下')
    for k, v in FILES.items():
        shutil.copyfile(bak[k], v)
    sys.exit(1)

bad = 0
try:
    for fn, name, old, new, expect in MUTS:
        src = orig[fn]
        if src.count(old) < 1:
            print('  [SKIP] %-26s 替换没命中（锚点变了？）' % name)
            bad += 1
            continue
        io.open(FILES[fn], 'w', encoding='utf-8', newline='\n').write(
            src.replace(old, new))
        rc, out = run()
        red = rc != 0 and expect in out
        print('  [%s] %-26s 期望变红 → rc=%s %s'
              % ('OK' if red else 'BAD', name, rc,
                 '（%s 命中）' % expect if red else '⚠️ 没变红'))
        if not red:
            bad += 1
            for line in out.splitlines():
                if 'FAIL' in line:
                    print('        ' + line.strip())
        io.open(FILES[fn], 'w', encoding='utf-8', newline='\n').write(src)
finally:
    for k, v in FILES.items():
        io.open(v, 'w', encoding='utf-8', newline='\n').write(orig[k])
        os.remove(bak[k])
    rc, out = run()
    print('还原后: rc=%s  %s' % (rc, 'LAYOUT_OK' if 'LAYOUT_OK' in out else '异常!'))

print('\n反向验证: %d 处变异, %d 处未按预期变红' % (len(MUTS), bad))
sys.exit(1 if bad else 0)
