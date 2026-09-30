#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验收：磁盘与日志清理模块（#3）。

必须 scp 到目标机再跑 —— BASE 是 127.0.0.1:8443，在本机跑会连本机端口。
用法： python3 /tmp/live-cleanup.py

重点验证三件事：
  ① 扫描结果里没有任何「不该碰」的路径；
  ② 试运行（dry）不删任何文件；
  ③ 策略保存后真的落到配置文件里，定时器真的在跑。
"""
import json
import ssl
import sys
import urllib.request

BASE = 'https://127.0.0.1:8443'
CTX = ssl._create_unverified_context()
TOK = None
PASS = FAIL = 0


def call(path, payload=None, timeout=60):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method='POST' if data is not None else 'GET')
    req.add_header('Content-Type', 'application/json')
    if TOK:
        req.add_header('X-Token', TOK)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'ok': False, 'msg_cn': 'HTTP 异常：%s' % e}


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


TOK = call('/api/login', {'username': 'admin', 'password': 'admin123'}).get('data', {}).get('token')
if not TOK:
    print('登录失败，终止')
    sys.exit(1)

# ---------------------------------------------------------------- 1. 扫描
print('--- 扫描接口 ---')
r = call('/api/cleanup', {'op': 'status'}, timeout=90)
chk('status 接口可用', r.get('ok') is True, '→ %s' % r.get('msg_cn'))
d = r.get('data') or {}
disk = d.get('disk') or {}
print('     磁盘：已用 %.0f MB / 共 %.0f MB（%s%%）'
      % (disk.get('used_mb') or 0, disk.get('total_mb') or 0, disk.get('percent')))
chk('返回磁盘信息', disk.get('total_mb') and disk.get('percent') is not None)
items = d.get('items') or []
chk('返回清理项', len(items) >= 8, '→ %d 项' % len(items))
print('     合计可释放 %.1f MB' % ((d.get('reclaimable') or 0) / 1048576.0))

FORBIDDEN = ('/etc/', '/boot/', '/usr/', '/opt/drouter/backend', '/opt/drouter/web',
             '/opt/drouter/data', '/opt/drouter/snapshots', '/root/', '/home/')
bad = []
for it in items:
    for s in (it.get('samples') or []):
        p = s.get('path') or ''
        if p.startswith(FORBIDDEN):
            bad.append(p)
    if not it.get('why') or not it.get('risk'):
        bad.append('<%s 缺说明>' % it.get('key'))
chk('样本路径全部在安全范围内', not bad, bad)

kv = {it['key']: it for it in items}
for k in ('ulog', 'drouter_log', 'journal', 'apt', 'tmp', 'trash', 'core', 'snapdl', 'varlog'):
    it = kv.get(k) or {}
    print('     %-12s 占用 %8.0f KB  可释放 %8.0f KB  命中 %s'
          % (k, (it.get('total') or 0) / 1024.0, (it.get('reclaimable') or 0) / 1024.0,
             it.get('hit_count') if not it.get('whole') else '全部'))
chk('九项齐全', len(kv) == 9)

# 活跃日志绝不出现在候选里
act = []
for it in items:
    for s in (it.get('samples') or []):
        p = (s.get('path') or '')
        base = p.rsplit('/', 1)[-1]
        if base.endswith('.jsonl') or base.endswith('.log'):
            act.append(base)
chk('候选里不含正在写入的活跃日志', not act, act)

# ---------------------------------------------------------------- 2. 试运行
print('\n--- 试运行（dry，不删文件）---')
before_total = sum((it.get('total') or 0) for it in items)
r2 = call('/api/cleanup', {'op': 'run', 'dry': True}, timeout=180)
chk('dry 接口可用', r2.get('ok') is True, '→ %s' % r2.get('msg_cn'))
dd = r2.get('data') or {}
chk('标记为试运行', dd.get('dry') is True)
r3 = call('/api/cleanup', {'op': 'status'}, timeout=90)
after_total = sum((it.get('total') or 0) for it in ((r3.get('data') or {}).get('items') or []))
chk('试运行后磁盘占用没有变化', abs(after_total - before_total) < 4096,
    'Δ=%d 字节' % (after_total - before_total))

# ---------------------------------------------------------------- 3. 策略保存
print('\n--- 策略保存 ---')
r4 = call('/api/cleanup', {'op': 'save', 'enabled': True, 'trigger': 'both',
                           'disk_percent': 88, 'schedule': 'daily', 'hour': 3,
                           'max_mb_per_run': 0,
                           'items': {'ulog': {'on': True, 'days': 5}}}, timeout=60)
chk('save 接口可用', r4.get('ok') is True, '→ %s' % r4.get('msg_cn'))
r5 = call('/api/cleanup', {'op': 'status'}, timeout=90)
c5 = ((r5.get('data') or {}).get('config') or {})
chk('水位阈值已落库', c5.get('disk_percent') == 88, '→ %s' % c5.get('disk_percent'))
chk('执行时间已落库', c5.get('hour') == 3, '→ %s' % c5.get('hour'))
chk('单项天数已落库', ((c5.get('items') or {}).get('ulog') or {}).get('days') == 5)
chk('其余项未被误改', ((c5.get('items') or {}).get('drouter_log') or {}).get('days') == 7)
chk('定时器处于活动状态', (r5.get('data') or {}).get('timer_active') is True)

# 还原成默认值，别把用户的阈值留在 88
call('/api/cleanup', {'op': 'save', 'enabled': True, 'trigger': 'both',
                      'disk_percent': 85, 'schedule': 'daily', 'hour': 4,
                      'max_mb_per_run': 0,
                      'items': {'ulog': {'on': True, 'days': 3}}}, timeout=60)
print('     （已还原为默认阈值 85% / ulog 3 天）')

# ---------------------------------------------------------------- 4. 自动触发判断
print('\n--- 自动触发判断 ---')
r6 = call('/api/cleanup', {'op': 'auto'}, timeout=180)
chk('auto 接口可用', r6.get('ok') is True, '→ %s' % r6.get('msg_cn'))
d6 = r6.get('data') or {}
if d6.get('skipped'):
    print('     本次跳过（水位 %s%%，未到阈值 / 未到时间）—— 这正是期望行为'
          % d6.get('percent'))
    chk('跳过时不产生删除', d6.get('freed') is None or d6.get('freed') == 0)
else:
    print('     已执行，释放 %.1f MB' % ((d6.get('freed') or 0) / 1048576.0))
    chk('执行后带明细', isinstance(d6.get('items'), list))

# ---------------------------------------------------------------- 5. 关闭后再 auto
print('\n--- 关闭自动清理后 auto 应跳过 ---')
call('/api/cleanup', {'op': 'save', 'enabled': False}, timeout=60)
r7 = call('/api/cleanup', {'op': 'auto'}, timeout=60)
chk('关闭后 auto 跳过', ((r7.get('data') or {}).get('skipped')) is True,
    '→ %s' % r7.get('msg_cn'))
call('/api/cleanup', {'op': 'save', 'enabled': True}, timeout=60)
print('     （已重新开启）')

print('\n' + '=' * 60)
print('通过 %d 项，失败 %d 项' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
