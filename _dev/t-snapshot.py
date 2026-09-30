#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快照覆盖率与容错测试（#6）：专防「界面报成功、实际没备份」。

踩过的坑：/etc/drouter 下有一个 0600 属主 root 的 rescue.conf，
以 drouter 身份运行的快照读不到 → shutil.copytree 整体抛异常 →
这个目录半截留在盘上、saved 列表里也不记 → 回滚时根本还原不了。
"""
import ast
import io
import os
import shutil
import stat
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
fails = 0


def chk(label, cond, extra=''):
    global fails
    if not cond:
        fails += 1
    print('[%s] %s %s' % ('OK' if cond else 'FAIL', label, extra))


with io.open(os.path.join(ROOT, 'backend/drouter-helper.py'), encoding='utf-8') as f:
    HELPER = f.read()
with io.open(os.path.join(ROOT, 'scripts/deploy.sh'), encoding='utf-8') as f:
    DEPLOY = f.read()
TREE = ast.parse(HELPER)


def const(name):
    for n in TREE.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(n.value)
    return None


# ---------------------------------------------------------------- 1. 覆盖范围
FILES = const('SNAPSHOT_ETC_FILES') or []
DIRS = const('SNAPSHOT_ETC_DIRS') or []
chk('快照包含 /etc/fstab（外置存储自动挂载）', '/etc/fstab' in FILES)
chk('快照包含 /etc/drouter 整目录', '/etc/drouter' in DIRS)
chk('快照包含 /etc/systemd/network', '/etc/systemd/network' in DIRS)
# 曾经把清单命名为 ETC_FILES，与另一处（helper 临时目录相关）重名被静默覆盖
chk('不存在与快照清单重名的旧 ETC_FILES 常量', const('ETC_FILES') is None)

# ---------------------------------------------------------------- 2. 容错复制
fn = None
for n in TREE.body:
    if isinstance(n, ast.FunctionDef) and n.name == '_copytree_soft':
        fn = n
chk('定义了 _copytree_soft（逐文件容错复制）', fn is not None)
if fn is None:
    print('\n结果: %d 项失败' % fails)
    sys.exit(1)

ns = {'os': os, 'shutil': shutil}
exec(compile(ast.Module(body=[fn], type_ignores=[]), '<x>', 'exec'), ns)
copytree_soft = ns['_copytree_soft']

# 造一个含「读不到的文件」的目录
tmp = tempfile.mkdtemp(prefix='snap-')
try:
    src = os.path.join(tmp, 'src')
    os.makedirs(os.path.join(src, 'sub'))
    for name, body in (('a.conf', 'A'), ('b.conf', 'B'), ('secret.conf', 'S')):
        with io.open(os.path.join(src, name), 'w') as f:
            f.write(body)
    with io.open(os.path.join(src, 'sub', 'c.conf'), 'w') as f:
        f.write('C')
    # 让其中一个文件不可读（Windows 上 chmod 000 不一定生效，能生效就测容错）
    os.chmod(os.path.join(src, 'secret.conf'), 0o000)
    blocked = True
    try:
        with io.open(os.path.join(src, 'secret.conf')) as f:
            f.read()
        blocked = False
    except Exception:
        pass
    dst = os.path.join(tmp, 'dst')
    n, sk = copytree_soft(src, dst)
    if blocked:
        chk('读不到的文件被跳过而不是报废整个目录', n == 3, '实际复制 %d 个' % n)
        chk('跳过文件有记录', len(sk) == 1 and 'secret.conf' in sk[0], sk)
    else:
        # 当前平台 chmod 000 不生效（Windows），至少保证全量复制成功
        chk('全量复制成功（该平台无法模拟权限拒绝）', n == 4, '实际复制 %d 个' % n)
        chk('无跳过记录', sk == [], sk)
    chk('其余文件都复制到了', os.path.isfile(os.path.join(dst, 'a.conf'))
        and os.path.isfile(os.path.join(dst, 'b.conf'))
        and os.path.isfile(os.path.join(dst, 'sub', 'c.conf')))
    # 空目录也要建出来（还原时目录结构不能少）
    os.makedirs(os.path.join(src, 'emptydir'))
    n2, sk2 = copytree_soft(src, dst)
    chk('空目录也会被创建', os.path.isdir(os.path.join(dst, 'emptydir')))
finally:
    for base, dirs, files in os.walk(tmp):
        for f in files:
            try:
                os.chmod(os.path.join(base, f), stat.S_IRWXU)
            except Exception:
                pass
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 3. 调用点
chk('快照目录复制不再直接用 shutil.copytree',
    'shutil.copytree(sub, dst' not in HELPER and 'shutil.copytree(src, sub' not in HELPER)
chk('快照会记录 skipped 到元数据', "'skipped': skipped_all" in HELPER
    or "'skipped': skipped_all[:50]" in HELPER)
chk('rescue.conf 放宽到 0640 且属组 drouter',
    '0o640' in HELPER and "shutil.chown(RESCUE_CONF, group='drouter')" in HELPER)
chk('部署脚本有存量 rescue.conf 权限迁移',
    'chmod 0640 /etc/drouter/rescue.conf' in DEPLOY)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)
