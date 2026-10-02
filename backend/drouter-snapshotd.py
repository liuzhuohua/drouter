#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter 自动快照守护脚本（由 drouter-snapshot.timer 定时调用）

职责：
  1. 按当前策略创建一份自动快照
  2. 按「过期时间 / 最大份数」清理旧自动快照
  3. 全程写结构化日志

策略来源优先级：
  环境变量 > /etc/drouter/snapshot.conf > 默认值
（真正的策略由 Web 界面写入 snapshot.conf，本脚本只读）
"""

import json
import os
import subprocess
import sys
from datetime import datetime

# ---------------------------------------------------------------- PATH 归一化
# Debian 上非 root / 非登录 shell 的 PATH 里没有 /usr/sbin 和 /sbin，而 nft、
# dnsmasq、radvd、chronyd 等路由器关键命令全都装在 /usr/sbin 下。
# systemd 服务的默认 PATH 是 /usr/local/bin:/usr/bin:/bin，于是所有 sh(['nft', ...])
# 都会以「未找到命令」静默失败 —— 表现是防火墙页面空白、日志为空。这里补上。
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


def log(level, code, msg_cn, detail=None):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'level': level,
               'module': 'snapshot', 'code': code, 'msg_cn': msg_cn, 'detail': detail or ''}
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', 'snapshot.jsonl'):
            with open(os.path.join(LOG_DIR, fn), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


def call(action, payload=None, timeout=300):
    try:
        p = subprocess.run(
            ['/usr/bin/python3', HELPER, action, json.dumps(payload or {}, ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout, errors='replace')
        return json.loads(p.stdout or '{}')
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


def load_conf():
    conf = {'enabled': True, 'path': '/opt/drouter/snapshots', 'interval_hours': 6,
            'keep_days': 7, 'keep_count': 30, 'keep_manual': True, 'on_apply': True}
    try:
        with open('/etc/drouter/snapshot.conf', encoding='utf-8') as f:
            conf.update(json.load(f) or {})
    except Exception:
        pass
    return conf


def _disk_ok(path):
    """拍快照前的磁盘底线检查。

    顺序原来是「先创建、后清理」：只要清理那一步失败（权限、快照损坏、
    参数问题），下一轮照样再拍一张，快照目录就这么一轮一轮涨到占满磁盘 ——
    而磁盘满了之后，连「清理」本身都救不回来。
    """
    try:
        st = os.statvfs(path if os.path.isdir(path) else '/')
        free_gb = st.f_bavail * st.f_frsize / (1024.0 ** 3)
        if free_gb < 1.0:
            return False, '剩余空间 %.2f GB，不足 1 GB' % free_gb
        return True, ''
    except Exception as e:
        return True, str(e)


def main():
    conf = load_conf()
    if not conf.get('enabled', True):
        log('info', 'SNAPSHOT_SKIP', '自动快照已关闭，跳过本次执行')
        return 0

    # 0) 先清理，再创建；并且盘快满时干脆不拍 —— 与其攒到磁盘耗尽，
    #    不如这一轮少一张快照。
    root = conf.get('path') or '/opt/drouter/snapshots'
    ok, why = _disk_ok(root)
    if not ok:
        log('error', 'SNAPSHOT_DISK_LOW', '磁盘空间不足，跳过本次自动快照：%s' % why)
        return 1

    # 1) 拍一张自动快照
    r = call('snapshot', {'tag': 'auto'})
    if not r.get('ok'):
        log('error', 'SNAPSHOT_AUTO_FAIL', '自动快照创建失败：%s' % r.get('msg_cn'))
        return 1
    ts = (r.get('data') or {}).get('ts') or ''
    log('info', 'SNAPSHOT_AUTO_OK', '自动快照已创建：%s' % ts,
        {'path': (r.get('data') or {}).get('path')})

    # 2) 清理过期 / 超量
    pr = call('snapshot_prune', {
        'keep_days': conf.get('keep_days', 0),
        'keep_count': conf.get('keep_count', 0),
        'keep_manual': conf.get('keep_manual', True),
    })
    if pr.get('ok'):
        removed = (pr.get('data') or {}).get('removed') or []
        if removed:
            log('info', 'SNAPSHOT_AUTO_PRUNE',
                '自动清理 %d 份过期快照' % len(removed), removed)
    else:
        log('warn', 'SNAPSHOT_AUTO_PRUNE_FAIL', '过期清理失败：%s' % pr.get('msg_cn'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
