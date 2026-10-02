#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter 告警检测守护脚本（由 drouter-alertd.timer 定时调用，1.0.7）

职责：
  1. 调 helper 的 alert/run 做一次判定 + 按需推送
  2. 全程写结构化中文日志，界面能直接看到上次检测结果与命中的规则

设计原则（与 drouter-logd / drouter-backupd 一致）：
  * 薄壳：判定逻辑全在 helper 里，本脚本只做「调一次 + 记日志」。
    这么分是因为判定需要 root（读 /proc、ping 网关），而界面也要能手动触发
    同一次判定 —— 两条路径必须是同一份代码，否则「手动测通了、定时没跑」。
  * 不吞错：helper 返回失败必须非零退出。这里返回 0 只会让 systemd 以为
    一切正常，用户在界面上看到的是「上次检测：正常」，而实际是崩了。
  * 不自己判断 enabled：关没关由 helper 读配置决定，本脚本照实转发。
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
CALL_TIMEOUT = 180


def log(level, code, msg_cn, detail=None):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'level': level,
               'module': 'alertd', 'code': code, 'msg_cn': msg_cn,
               'detail': detail or ''}
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', 'alertd.jsonl'):
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
    r = call('alert', {'op': 'run'})
    if not r.get('ok'):
        log('error', 'AL_RUN_FAIL', '告警检测失败：%s' % r.get('msg_cn'))
        return 1
    d = r.get('data') or {}
    if not d.get('enabled'):
        log('info', 'AL_DISABLED', '告警未启用，本次只探测不推送')
        return 0
    fired = d.get('fired') or 0
    hits = d.get('hits') or []
    skipped = d.get('skipped') or []
    detail = {'hits': [h.get('key') for h in hits],
              'skipped': [s.get('key') for s in skipped]}
    if fired:
        log('warn', 'AL_FIRED', '本次检测命中 %d 条规则，已推送 %d 条'
            % (len(hits), fired), detail)
    elif hits:
        log('info', 'AL_COOLDOWN', '本次检测命中 %d 条规则，但都在冷却期或免打扰时段内，'
                                   '未推送' % len(hits), detail)
    else:
        log('info', 'AL_CLEAR', '本次检测未发现异常', detail)
    return 0


if __name__ == '__main__':
    sys.exit(main())
