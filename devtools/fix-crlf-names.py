#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理因「CRLF 脚本在 Linux 上执行」而留下的脏文件名。

背景：把 Windows 上编辑的 .sh 用 CRLF 传到 Linux 直接执行时，每一行的结尾
\\r 会被当成参数的一部分，于是 `mkdir -p /etc/dnsmasq.d` 真的建出了名字叫
`/etc/dnsmasq.d\\r` 的目录，`echo default > /etc/drouter/active-theme` 真的
写出了一个名字带 \\r 的文件。这些条目功能上无害（正常名字的那份也在），
但会让 `ls` 出现重影（\\r 让终端光标回到行首，同名条目看起来出现两遍）、
被配置快照反复打包、并干扰排查。

删除策略（保守，宁可漏删）：
  1. 只处理名字里含控制字符（<0x20 或 0x7f）的条目；
  2. 名字去掉控制字符后，**同级必须存在同名兄弟** —— 证明它是重复品；
  3. 目录：先比较相对文件集合，只要里面有兄弟目录没有的文件就**拒绝删除**
     （避免删掉唯一副本）；
  4. 默认只打印计划，加 --apply 才真删。

用法：
  python3 fix-crlf-names.py                 # 只看，不动
  python3 fix-crlf-names.py --apply         # 执行删除
  python3 fix-crlf-names.py --apply --roots /etc /opt/drouter
"""
import os
import re
import shutil
import sys

DEFAULT_ROOTS = ['/etc', '/opt', '/var/log', '/usr/local', '/root', '/srv']
# 快照是「某个时刻的备份」，必须保持原样；在里面删东西会破坏回滚保真度
EXCLUDE = (os.path.sep + 'snapshots' + os.path.sep,
           os.path.sep + 'snapshots')

CTRL = re.compile(r'[\x00-\x1f\x7f]')


def has_ctrl(name):
    return bool(CTRL.search(name))


def clean_name(name):
    return CTRL.sub('', name)


def rel_files(root):
    """目录内所有普通文件的相对路径集合。"""
    out = set()
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: None):
        for f in filenames:
            out.add(os.path.relpath(os.path.join(dirpath, f), root))
    return out


def scan(roots):
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            if any(x in dirpath + os.path.sep for x in EXCLUDE):
                dirnames[:] = []
                continue
            for n in list(dirnames) + filenames:
                if has_ctrl(n):
                    found.append(os.path.join(dirpath, n))
    return sorted(found)


def main():
    apply = '--apply' in sys.argv
    roots = DEFAULT_ROOTS
    if '--roots' in sys.argv:
        roots = sys.argv[sys.argv.index('--roots') + 1:]
        if not roots:
            roots = DEFAULT_ROOTS

    cands = scan(roots)
    print('扫描根：%s' % ' '.join(roots))
    print('名字含控制字符的条目：%d 个' % len(cands))
    print()

    delete, skip = [], []
    for p in cands:
        parent, name = os.path.split(p)
        sibling = os.path.join(parent, clean_name(name))
        why = None
        if not os.path.exists(sibling):
            why = '同级没有同名正常条目（可能是唯一副本），不动'
        elif os.path.isdir(p):
            extra = rel_files(p) - rel_files(sibling)
            if extra:
                why = '目录里有兄弟没有的文件：%s' % '、'.join(sorted(extra)[:3])
        if why:
            skip.append((p, why))
        else:
            delete.append(p)

    # 父级已在删除列表里的子项不用再单列（父目录一删就跟着走），
    # 否则报告里会出现「/opt/drouter\r 可删」+「/opt/drouter\r/docs 需人工确认」
    # 这种自相矛盾的两行。
    deleteset = set(delete)
    delete = [p for p in delete
              if not any(os.path.dirname(a) == p or a.startswith(p + os.sep)
                         for a in deleteset)]
    skip = [(p, w) for p, w in skip
            if not any(os.path.dirname(a) == p or a.startswith(p + os.sep)
                       for a in deleteset)]

    print('=== 可安全删除（存在同名正常兄弟）%d 个 ===' % len(delete))
    for p in delete:
        kind = 'dir ' if os.path.isdir(p) else 'file'
        print('  %s %s' % (kind, p))

    if skip:
        print()
        print('=== 需人工确认 %d 个 ===' % len(skip))
        for p, why in skip:
            print('  %s\n      %s' % (p, why))

    if not apply:
        print()
        print('（预演模式，未做任何修改；加 --apply 执行删除）')
        return 0

    print()
    ok = fail = 0
    for p in delete:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            ok += 1
            print('  已删除 %s' % p)
        except Exception as e:
            fail += 1
            print('  删除失败 %s：%s' % (p, e))
    print()
    print('删除成功 %d / 失败 %d' % (ok, fail))
    # 复查
    left = scan(roots)
    print('复查：仍剩 %d 个含控制字符的条目' % len(left))
    return 0


if __name__ == '__main__':
    sys.exit(main())
