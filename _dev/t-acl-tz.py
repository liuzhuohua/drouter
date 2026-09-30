# -*- coding: utf-8 -*-
"""验证 ACL 时区换算与星期映射的正确性（穷举验证）。"""
import re, sys, io
SRC = r"C:/Users/lyrz-pve-win10/WorkBuddy/2026-09-27-19-57-00/router-build/backend/drouter-helper.py"
src = io.open(SRC, encoding='utf-8').read()
def grab(name):
    m = re.search(r'^def %s\(.*?(?=\n(?:def |[A-Z_]+ *=|# ---|class ))' % re.escape(name), src, re.S | re.M)
    return m.group(0)
NS = {'re': re}
class ValidateError(Exception):
    def __init__(self, m, f=None): self.msg_cn = m
NS['ValidateError'] = ValidateError
NS['TZ_OFFSET_H'] = 8
for f in ('_acl_hhmm_min', '_acl_min_hhmm', '_acl_day_bit', '_acl_shift_days', '_acl_time_windows'):
    exec(grab(f), NS)
W, SH = NS['_acl_time_windows'], NS['_acl_shift_days']
NAMES = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']

def matches(tg, local_day, local_min):
    """判断本地某天某分钟是否落在时间组内（模拟内核行为）。"""
    base = tg['days']
    start = NS['_acl_hhmm_min'](tg['start'])
    end = NS['_acl_hhmm_min'](tg['end'])
    inside = (start <= local_min <= end) if start <= end else (local_min >= start or local_min <= end)
    return inside and (local_day in base)

def matches_rendered(tg, local_day, local_min):
    """用渲染出的 UTC 窗口反推：把本地时刻换算成 UTC，再判断是否命中。"""
    utc_total = local_day * 1440 + local_min - 8 * 60
    u_day = (utc_total // 1440) % 7
    u_min = utc_total % 1440
    for (s, e, delta) in W(tg):
        days = SH(tg['days'], delta)   # 与渲染器完全一致
        if u_day in days and (s <= u_min <= e):
            return True
    return False

cases = [
    {'name':'上学日','days':[1,2,3,4,5],'start':'07:00','end':'17:00'},
    {'name':'夜间','days':[0,1,2,3,4,5,6],'start':'22:00','end':'06:30'},
    {'name':'周末','days':[0,6],'start':'00:00','end':'23:59'},
    {'name':'单点','days':[3],'start':'12:00','end':'12:30'},
    {'name':'早课','days':[1,2,3,4,5],'start':'00:00','end':'07:59'},
    {'name':'晚自习','days':[1,2,3,4,5],'start':'19:30','end':'23:30'},
    {'name':'凌晨','days':[1,2,3,4,5],'start':'00:30','end':'08:30'},
]
fails = 0
for tg in cases:
    diff = 0
    for d in range(7):
        for m in range(0, 1440):
            a = matches(tg, d, m)
            b = matches_rendered(tg, d, m)
            if a != b:
                diff += 1
                if diff <= 6:
                    print('  [差异] %s %s %02d:%02d 期望=%s 渲染=%s'
                          % (tg['name'], NAMES[d], m//60, m%60, a, b))
    status = 'OK' if diff == 0 else 'FAIL'
    if diff: fails += 1
    print('[%s] %-8s 穷举 7×1440 个时刻，差异 %d 个' % (status, tg['name'], diff))

print('\n结果: %s' % ('时区换算与星期映射完全正确' if fails == 0 else '%d 组存在差异' % fails))
sys.exit(1 if fails else 0)
