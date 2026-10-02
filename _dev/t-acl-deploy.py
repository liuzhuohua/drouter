# -*- coding: utf-8 -*-
"""验证 ACL 落盘部署逻辑（#8）。

helper 依赖 pwd/systemd，无法在 Windows 直接 import，因此这里用
正则从源码抽取所需片段，注入到隔离命名空间中运行，并用临时目录
替换 ACL_CONF / ACL_JSON / ACL_UNIT。
"""
import re, io, os, sys, tempfile, shutil, json

SRC = r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend/drouter-helper.py"
src = io.open(SRC, encoding='utf-8').read()


def grab(name):
    m = re.search(r'^def %s\(.*?(?=\n(?:def |[A-Z_]+ *=|# ---|class ))' % re.escape(name),
                  src, re.S | re.M)
    if not m:
        raise SystemExit('未找到函数：%s' % name)
    return m.group(0)


class ValidateError(Exception):
    def __init__(self, m, f=None):
        self.msg_cn = m


def grab_const(name):
    m = re.search(r'^%s = .*?$' % re.escape(name), src, re.M)
    return m.group(0)


tmp = tempfile.mkdtemp(prefix='acl-deploy-')
NS = {'re': re, 'os': os, 'json': json, 'subprocess': _stub if False else None,
      'ValidateError': ValidateError, 'TZ_OFFSET_H': 8,
      'fail': lambda m, c=None, d=None: {'ok': False, 'msg_cn': m},
      'ok': lambda d=None, m='', c=0: {'ok': True, 'data': d, 'msg_cn': m}}


class _Sub:
    """假的 subprocess，记录调用而不真的执行。"""
    called = []

    @staticmethod
    def run(args, **kw):
        _Sub.called.append(list(args))

        class R:
            returncode = 0
            stderr = b''
        return R()


NS['subprocess'] = _Sub

# 常量：把路径全部改到临时目录
NS['ACL_CONF'] = os.path.join(tmp, 'generated', 'acl.nft')
NS['ACL_JSON'] = os.path.join(tmp, 'generated', 'acl.json')
NS['ACL_UNIT'] = os.path.join(tmp, 'system', 'drouter-acl.service')
NS['ACL_TABLE'] = 'drouter_acl'
NS['BUILD_MODE_FILE'] = os.path.join(tmp, 'BUILD_MODE')

# 抽取模板
m = re.search(r'^ACL_UNIT_TMPL = """.*?"""', src, re.S | re.M)
if not m:
    raise SystemExit('未找到 ACL_UNIT_TMPL')
exec(m.group(0), NS)

for fn in ('_acl_hhmm_min', '_acl_min_hhmm', '_acl_day_bit', '_acl_shift_days',
           '_acl_time_windows', '_acl_time_expr', '_acl_load', '_acl_save',
           '_acl_render_nft', '_atomic_write'):
    exec(grab(fn), NS)
NS['tempfile'] = tempfile

# _acl_deploy 里的 BUILD_MODE 路径改成临时文件（在命名空间内可见的全局名）
_src_deploy = grab('_acl_deploy').replace(
    "os.path.isfile('/etc/drouter/BUILD_MODE')",
    "os.path.isfile(BUILD_MODE_FILE)")
exec(_src_deploy, NS)
DEPLOY = NS['_acl_deploy']

# 需要 ACL_APP_GROUPS 供 _acl_render_nft 使用
mgrp = re.search(r'^ACL_APP_GROUPS = \[.*?\n\]', src, re.S | re.M)
exec(mgrp.group(0), NS)
mq = re.search(r'^QOS_MARK_BASE = .*?$', src, re.M)
if mq:
    exec(mq.group(0), NS)
else:
    NS['QOS_MARK_BASE'] = 0x100

fails = 0


def chk(name, got, want):
    global fails
    ok = (got == want)
    if not ok:
        fails += 1
    print('[%s] %-34s got=%-22s want=%s' % ('OK' if ok else 'FAIL', name, got, want))


CFG = {
    'enable': True,
    'time_groups': [{'id': 'night', 'name': '夜间', 'days': [0, 1, 2, 3, 4, 5, 6],
                     'start': '22:00', 'end': '06:30'}],
    'groups': [{'id': 'kids', 'name': '孩子设备', 'hosts': ['192.168.7.20']}],
    'rules': [{'name': '夜间断网', 'enable': True, 'action': 'block',
               'time_group': 'night', 'group': 'kids', 'apps': []}],
}

print('=== 落盘（live=False，只写文件）===')
r = DEPLOY(CFG, live=False)
chk('生成 nft 文件', os.path.isfile(NS['ACL_CONF']), True)
chk('返回 applied=False', r.get('applied'), False)
body = io.open(NS['ACL_CONF'], encoding='utf-8').read()
chk('文件含 inet 表', 'table inet drouter_acl' in body, True)
chk('文件含 drop', 'counter drop' in body, True)
chk('unit 文件已生成', os.path.isfile(NS['ACL_UNIT']), True)
unit = io.open(NS['ACL_UNIT'], encoding='utf-8').read()
chk('unit 指向 conf', NS['ACL_CONF'] in unit, True)
chk('unit 有 ExecStart', 'ExecStart=/usr/sbin/nft -f' in unit, True)
chk('未执行 nft（live=False）',
    any(a[:1] == ['nft'] and '-f' in a for a in _Sub.called), False)

print('\n=== 构建保护模式（文件存在时不加载）===')
io.open(NS['BUILD_MODE_FILE'], 'w').write('1')
_Sub.called = []
r2 = DEPLOY(CFG, live=True)
chk('applied=False', r2.get('applied'), False)
chk('提示构建保护', '构建保护' in (r2.get('msg_cn') or ''), True)
chk('未调用 nft', any(a[:1] == ['nft'] for a in _Sub.called), False)
os.remove(NS['BUILD_MODE_FILE'])

print('\n=== 正常加载（live=True）===')
_Sub.called = []
r3 = DEPLOY(CFG, live=True)
chk('applied=True', r3.get('applied'), True)
calls = [a for a in _Sub.called if a[:1] == ['nft']]
chk('先删旧表', calls[0][:4] == ['nft', 'delete', 'table', 'inet'], True)
chk('再加载文件', calls[1][:2] == ['nft', '-f'], True)
chk('加载的是 ACL_CONF', calls[1][2] == NS['ACL_CONF'], True)

print('\n=== 停用状态（enable=False）===')
_Sub.called = []
r4 = DEPLOY(dict(CFG, enable=False), live=True)
chk('applied=True', r4.get('applied'), True)
body2 = io.open(NS['ACL_CONF'], encoding='utf-8').read()
chk('停用时无 drop', 'counter drop' in body2, False)
chk('有停用说明', '访问控制已停用' in body2, True)

print('\n=== 幂等：重复部署不重复写 unit ===')
mtime = os.path.getmtime(NS['ACL_UNIT'])
DEPLOY(CFG, live=False)
chk('unit 未重复写入', os.path.getmtime(NS['ACL_UNIT']), mtime)

shutil.rmtree(tmp, ignore_errors=True)
print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)
