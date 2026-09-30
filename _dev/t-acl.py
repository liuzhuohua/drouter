# -*- coding: utf-8 -*-
"""访问控制 / 家长时间组 纯逻辑测试。"""
import re, sys, io
SRC = r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend/drouter-helper.py"
src = io.open(SRC, encoding='utf-8').read()

def grab(name):
    m = re.search(r'^def %s\(.*?(?=\n(?:def |[A-Z_]+ *=|# ---|class ))' % re.escape(name), src, re.S | re.M)
    if not m: raise SystemExit('NOT FOUND: ' + name)
    return m.group(0)

def grab_var(name, kind='['):
    m = re.search(r'^%s = \%s.*?^\%s' % (re.escape(name), kind, ']' if kind == '[' else '}'), src, re.S | re.M)
    if not m: raise SystemExit('NOT FOUND VAR: ' + name)
    return m.group(0)

NS = {'re': re, 'json': __import__('json'), 'os': __import__('os')}
class ValidateError(Exception):
    def __init__(self, msg_cn, field=None):
        super().__init__(msg_cn); self.msg_cn = msg_cn; self.field = field
NS['ValidateError'] = ValidateError
NS['QOS_MARK_BASE'] = 0x1000
NS['TZ_OFFSET_H'] = 8
NS['ACL_TABLE'] = 'drouter_acl'
for v in ('ACL_TIME_PRESETS', 'ACL_APP_GROUPS', 'ACL_GROUP_TEMPLATES', 'ACL_ACTIONS'):
    exec(grab_var(v), NS)
for f in ('_acl_hhmm_min', '_acl_min_hhmm', '_acl_localize_hhmm', '_acl_day_bit',
          '_acl_shift_days', '_acl_time_windows', '_acl_time_expr', '_acl_render_nft'):
    exec(grab(f), NS)

L, D, R = NS['_acl_localize_hhmm'], NS['_acl_day_bit'], NS['_acl_render_nft']
fails = 0
def chk(label, got, want):
    global fails
    ok = got == want
    if not ok: fails += 1
    print('[%s] %-46s got=%-22s want=%s' % ('OK' if ok else 'FAIL', label, str(got), str(want)))

print('=== 本地时间 → UTC 换算（UTC+8）===')
chk('07:00 → 23:00 前一日', L('07:00'), '23:00')
chk('17:00 → 09:00', L('17:00'), '09:00')
chk('22:00 → 14:00', L('22:00'), '14:00')
chk('06:30 → 22:30', L('06:30'), '22:30')
chk('00:00 → 16:00', L('00:00'), '16:00')
chk('08:00 → 00:00', L('08:00'), '00:00')
chk('23:59 → 15:59', L('23:59'), '15:59')

print('\n=== 星期集合 ===')
chk('工作日', D([1,2,3,4,5]), ['Mon','Tue','Wed','Thu','Fri'])
chk('周末', D([0,6]), ['Sun','Sat'])
chk('每天', D([0,1,2,3,4,5,6]), ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'])

print('\n=== 时间表达式（含跨零点）===')
NS['_acl_time_expr'] = NS['_acl_time_expr']
E = NS['_acl_time_expr']
chk('上学日白天', 'meta day' in E({'days':[1,2,3,4,5],'start':'07:00','end':'17:00'}), True)
e_night = E({'days':[0,1,2,3,4,5,6],'start':'22:00','end':'06:30'})
# 跨零点预览应拆成两段（段间用「或」连接），且带本地时间说明
chk('夜间跨零点拆两段', ' 或 ' in e_night, True)
chk('夜间预览含本地时间说明', '本地 22:00–06:30' in e_night, True)
print('\n=== 渲染：停用状态 ===')
off = R({'enable': False, 'time_groups': [], 'groups': [], 'rules': []})
chk('停用时无 drop', 'drop' not in off, True)
chk('停用时有计数说明', '访问控制已停用' in off, True)

print('\n=== 渲染：完整规则 ===')
d = {
  'enable': True,
  'time_groups': [
    {'id':'night','name':'夜间睡眠','days':[0,1,2,3,4,5,6],'start':'22:00','end':'06:30'},
    {'id':'school','name':'上学日','days':[1,2,3,4,5],'start':'07:00','end':'17:00'},
  ],
  'groups': [
    {'id':'kids','name':'孩子的设备','hosts':['192.168.7.20','192.168.7.21','2408:8207:1234::20']},
  ],
  'rules': [
    {'name':'夜间断娱乐','enable':True,'action':'block','time_group':'night','group':'kids','apps':['social','game','video']},
    {'name':'上学日禁游戏','enable':True,'action':'block','time_group':'school','group':'kids','apps':['game']},
    {'name':'只记录','enable':True,'action':'log','time_group':'school','group':'kids','apps':['video']},
    {'name':'停用规则','enable':False,'action':'block','time_group':'night','group':'kids','apps':['p2p']},
  ],
}
out = R(d)
print(out)
chk('生成 inet 表', 'table inet drouter_acl' in out, True)
chk('forward 链优先级 -10', 'priority -10' in out, True)
chk('含 IPv4 组地址', '192.168.7.20' in out, True)
chk('含 IPv6 组地址', '2408:8207:1234::20' in out, True)
chk('含 drop 动作', 'counter drop' in out, True)
chk('含应用分类注释', '社交娱乐' in out and '游戏' in out, True)
chk('非跨零点规则未误判', out.count('本地时间跨零点') == 1, True)
chk('本地时间显示正确', '本地 07:00–17:00' in out, True)
# 上学日本地 07:00-17:00 → UTC 23:00-23:59（前一日，delta=-1）+ 00:00-09:00（当日，delta=0）
chk('上学日窗口 23:00-23:59', 'meta hour "23:00"-"23:59"' in out, True)
chk('上学日窗口 00:00-09:00', 'meta hour "00:00"-"09:00"' in out, True)
# 夜间本地 22:00-06:30 跨零点 → 本地 22:00-23:59=UTC 14:00-15:59；本地 00:00-06:30=UTC 16:00-22:30
chk('夜间窗口拆两段', 'meta hour "14:00"-"15:59"' in out and 'meta hour "16:00"-"22:30"' in out, True)
chk('停用规则不生成', 'P2P 下载' not in out, True)
chk('跨零点已拆分提示', '时间跨零点，已自动拆分' in out, True)
# 2 条 block 规则各拆 2 段 → 4 次 drop；log 规则不带 drop
chk('仅记录规则无 drop', out.count('counter drop') == 4, True)

print('\n结果: %s' % ('全部通过' if fails == 0 else '%d 项失败' % fails))
sys.exit(1 if fails else 0)
