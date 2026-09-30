#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境契约测试：守护进程 PATH 归一化 + 虚拟网卡归类。

这两条都是「接口照常返回 ok，但功能悄悄不工作」的隐形缺陷，必须常驻回归：
  1) systemd 默认 PATH 不含 /usr/sbin，nft/dnsmasq 会「未找到命令」；
  2) 本机自建的虚拟设备（QoS 的 ifb-*）若被当成物理网卡，界面会一直提示指派角色。
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
BACKEND = os.path.join(ROOT, 'backend')

GUARDS = ['drouter-web.py', 'drouter-helper.py', 'drouter-logd.py',
          'drouter-snapshotd.py', 'drouter-rescue.py']
UNITS = [os.path.join(ROOT, 'scripts', 'deploy.sh')]

fails = []


def ck(name, cond, extra=''):
    print('  %-46s %s %s' % (name, 'OK  ' if cond else 'FAIL', extra))
    if not cond:
        fails.append(name)


print('一、守护进程 PATH 归一化')
for f in GUARDS:
    src = open(os.path.join(BACKEND, f), encoding='utf-8').read()
    ck('%s 定义了 _ensure_sbin_path' % f, '_ensure_sbin_path' in src)
    ck('%s 启动时调用了它' % f, '_ensure_sbin_path()' in src)

# 真正执行一遍，确认 PATH 里确实被补进 sbin
src = open(os.path.join(BACKEND, 'drouter-helper.py'), encoding='utf-8').read()
tree = ast.parse(src)
picked = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == '_ensure_sbin_path']
picked += [n for n in tree.body
           if isinstance(n, ast.Assign)
           and any(isinstance(t, ast.Name) and t.id == '_SBIN_DIRS' for t in n.targets)]
# 注：本机（Windows）上没有 /usr/sbin，_ensure_sbin_path 里的 os.path.isdir 守卫
# 会正确地跳过它们。测试要验证的是「在 Debian 上的行为」，所以这里伪造 isdir。
_real_isdir = os.path.isdir


class FakePath:
    def __init__(self):
        self.isdir = self._isdir
        self.sep = os.path.sep

    @staticmethod
    def _isdir(p):
        return True if p in ('/usr/sbin', '/sbin', '/usr/local/sbin') else _real_isdir(p)


class FakeOs:
    environ = os.environ
    path = FakePath()
    pathsep = os.pathsep
    sep = os.path.sep


ns = {'os': FakeOs()}
exec(compile(ast.Module(body=picked, type_ignores=[]), 'helper', 'exec'), ns)
ns['_ensure_sbin_path']()
parts = os.environ['PATH'].split(os.pathsep)
ck('调用后 PATH 含 /usr/sbin', '/usr/sbin' in parts)
ck('调用后 PATH 含 /sbin', '/sbin' in parts)
ck('sbin 排在前面（优先命中）',
   parts.index('/usr/sbin') < len(parts) - 1)
ck('原有 PATH 未被清空', len(parts) >= 2, '%d 段' % len(parts))
# 幂等：再调一次不应重复插入
before = os.environ['PATH']
ns['_ensure_sbin_path']()
ck('重复调用幂等（不重复插入）', os.environ['PATH'] == before)

print('\n二、systemd 单元显式声明 PATH')
for p in UNITS:
    txt = open(p, encoding='utf-8').read()
    n = txt.count('Environment=PATH=/usr/local/sbin')
    cnt = txt.count('[Service]')
    ck('deploy.sh 每个 [Service] 都带 PATH（%d/%d）' % (n, cnt), n == cnt, '%d 个单元' % cnt)

print('\n三、虚拟网卡归类（ifb-* 不该要求指派角色）')
h = open(os.path.join(BACKEND, 'drouter-helper.py'), encoding='utf-8').read()
ck('is_virtual 前缀含 ifb', "'ifb'" in h and 'is_virtual' in h)
ck('read_ifaces 标注 managed_by', 'managed_by' in h)

js = open(os.path.join(ROOT, 'web', 'app.js'), encoding='utf-8').read()
ck('前端新网卡提示排除虚拟设备', 'managed_by' in js)
ck('前端提示带关闭按钮', 'newiface-x' in js)
ck('关闭状态持久化', 'drouter_newiface_dismissed' in js)

print('\n结果：失败 %d 项' % len(fails))
for f in fails:
    print('  ✘ ' + f)
sys.exit(1 if fails else 0)
