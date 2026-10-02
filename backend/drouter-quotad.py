#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter 用量聚合守护脚本（由 drouter-quotad.timer 定时调用，1.0.7）

职责：
  1. 调 helper 的 quota/aggregate，把新产生的流日志聚合成「小时 × 设备 × 服务」桶
  2. 全程写结构化中文日志，界面能直接看到上次聚合结果

为什么必须有这个守护（而不是在页面上现算）：
  ulog.jsonl 正常运行时能到几十万行。用户在页面点「查询」就全量扫一遍，
  4GB 内存的机器会被 OOM kill —— 而这台机器正是给人看流量用的。
  所以聚合必须提前在后台做完，页面只读已经聚合过的小文件。

幂等性：靠 quota.cursor 里记的字节偏移保证同一行只聚合一次。
所以 timer 重叠、手工跑一次、机器重启中断，都不会重复计。
"""

import json
import os
import subprocess
import sys
from datetime import datetime

# ---------------------------------------------------------------- PATH 归一化
_SBIN_DIRS = ('/usr/local/sbin', '/usr/sbin', '/sbin')


def _ensure_sbin_path():
    cur = os.environ.get('PATH') or ''
    parts = [p for p in cur.split(os.pathsep) if p]
    for d in _SBIN_DIRS:
        if d not in parts and os.path.isdir(d):
            parts.insert(0, d)
    os.environ['PATH'] = os.pathsep.join(parts)


_ensure_sbin_path()

HELPER = '/opt/drouter/backend/drouter-helper.py'
LOG_DIR = '/var/log/drouter'
CALL_TIMEOUT = 300


def log(level, code, msg_cn, detail=None):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'level': level,
               'module': 'quotad', 'code': code, 'msg_cn': msg_cn,
               'detail': detail or ''}
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', 'quotad.jsonl'):
            with open(os.path.join(LOG_DIR, fn), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


def call(action, payload=None, timeout=CALL_TIMEOUT):
    try:
        p = subprocess.run(
            ['/usr/bin/python3', HELPER, action,
             json.dumps(payload or {}, ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout, errors='replace')
        if not (p.stdout or '').strip():
            return {'ok': False,
                    'msg_cn': 'helper 没有返回任何内容（退出码 %s）：%s'
                              % (p.returncode, (p.stderr or '').strip()[:300])}
        return json.loads(p.stdout)
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


def main():
    r = call('quota', {'op': 'aggregate'})
    if not r.get('ok'):
        log('error', 'QT_AGG_FAIL', '用量聚合失败：%s' % r.get('msg_cn'))
        return 1
    d = r.get('data') or {}
    rows = d.get('rows') or 0
    byts = d.get('bytes') or 0
    if rows:
        log('info', 'QT_AGG_OK', '已聚合 %d 个统计桶（%.1f MB）'
            % (rows, byts / 1048576.0))
    else:
        log('info', 'QT_AGG_IDLE', '本次没有新的可统计流量（新增日志 %d 条）'
            % (d.get('new') or 0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
