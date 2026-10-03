#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把两个 node 侧的前端检查器纳入 python 回归。

为什么要有这么一个壳，而不是直接改 run-static.py 去调 node：

  1. run-static.py 的收集规则是 `glob('t-*.py')` + EXCL 黑名单。
     让 node 检查器**以 t-*.py 的身份出现**，就不用给 run-static.py
     加第二套收集逻辑（那是两条会各自漂移的路径 —— 以后再加一个
     node 检查器时，十有八九只记得改其中一处）。

  2. `devtools/check-jsstrip.js` 需要一份「剥完注释的 app.js」当输入，
     而那份产物只能由 python 生成（node 自己跑 python 在 Windows 上
     EBUSY，spawnSync 会被信号杀掉、rc=null）。所以顺序必须是
     **先 python 备料，再 node 验收** —— 这个顺序约束也只有放在
     python 侧才表达得自然。

用法：
    python _dev/t-109-node-front.py
    python _dev/run-static.py t-109        # 跟全量回归一起跑
"""
import glob
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

NODE = os.environ.get('NODE_BIN') or shutil.which('node') or 'node'

# 要串行跑的 node 检查器。**顺序有意义**：jsstrip 的产物是
# check-jsstrip.js 的输入，所以它虽然只是「备料」，也放在同一串里，
# 免得有人在只跑单个检查器时得到「产物不存在」的假红。
NODE_CHECKS = [
    ('check-jsstrip.js', '剥注释器（词法扫描版）的 V8 交叉验证'),
    ('check-snap-render.js', '快照列表内嵌渲染 + 备注/上锁/二次确认交互'),
]


def run(cmd, what):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=180)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except FileNotFoundError:
        # 没装 node 不是代码的错。别让一个环境问题把整轮回归判红。
        return 127, '%s：找不到可执行文件 %r\n' % (what, cmd[0])
    except subprocess.TimeoutExpired:
        return 124, '%s：超时（180s）\n' % what


def main():
    ok_all, bad_all = 0, []

    # ---- 步骤 1：跑 jsstrip.py 生成 .jsstrip-appjs.out ----
    # 这一步自己会跑一遍 44 项 selftest，产物是副产品。
    rc, out = run([sys.executable, os.path.join(HERE, 'jsstrip.py')],
                  'jsstrip.py')
    m = re.findall(r'通过 (\d+) 项', out)
    if rc == 0:
        print('[OK] jsstrip.py 自检%s 项（并写出 .jsstrip-appjs.out）'
              % ((' ' + m[-1]) if m else ''))
    else:
        print('[NG] jsstrip.py 自检失败 rc=%s' % rc)
        for line in out.splitlines():
            if line.strip().startswith('·') or 'Traceback' in line:
                print('      ' + line.rstrip())
        bad_all.append('jsstrip.py')

    # ---- 步骤 2：逐个跑 node 检查器 ----
    for name, desc in NODE_CHECKS:
        f = os.path.join(ROOT, 'devtools', name)
        if not os.path.isfile(f):
            print('[NG] %-24s 文件不存在：devtools/%s' % (name, name))
            bad_all.append(name)
            continue
        rc, out = run([NODE, f], name)
        m = re.findall(r'通过 (\d+) 项', out)
        n = int(m[-1]) if m else 0
        if rc == 0:
            ok_all += n
            print('[OK] %-24s %s（%d 项）' % (name, desc, n))
        else:
            print('[NG] %-24s %s rc=%s' % (name, desc, rc))
            for line in out.splitlines():
                if '[NG]' in line or line.strip().startswith('·') \
                        or 'Error' in line:
                    print('      ' + line.rstrip())
            bad_all.append(name)

    print()
    print('=' * 60)
    print('通过 %d 项，失败 %d 项' % (ok_all, len(bad_all)))
    for b in bad_all:
        print('  · %s' % b)
    print('=' * 60)
    return 1 if bad_all else 0


if __name__ == '__main__':
    sys.exit(main())