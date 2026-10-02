#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter 配置自动备份守护脚本（由 drouter-backupd.timer 每天调用一次）

职责：
  1. 调用 helper 的 backup/create 导出一份全量配置包
  2. 按「保留份数 + 保留天数」清理旧包
  3. 全程写结构化中文日志，界面能直接看到上次备份结果

设计原则（与 drouter-logd / drouter-snapshotd 保持同一套）：
  * 轻量：单次只在凌晨跑一次，导出的都是文本配置（几十 KB 级）
  * 安全：只读配置、只写自己的备份目录，不碰任何网络状态
  * 幂等：同一天重复跑会先按份数策略清掉超出部分，不会把盘堆满
  * 不吞错：失败必须非零退出并写日志，绝不静默返回 0
     （drouter-logd 曾把磁盘写失败吞成 return 0，日志从此静默停更）
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
               'module': 'backupd', 'code': code, 'msg_cn': msg_cn,
               'detail': detail or ''}
        line = json.dumps(rec, ensure_ascii=False)
        for fn in ('all.jsonl', 'backupd.jsonl'):
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
        return json.loads(p.stdout or '{}')
    except Exception as e:
        return {'ok': False, 'msg_cn': str(e)}


def disk_ok(path='/opt/drouter'):
    """备份前先看盘：写不进去还要一直试，只会把失败刷满日志。"""
    try:
        st = os.statvfs(path if os.path.isdir(path) else '/')
        free_gb = st.f_bavail * st.f_frsize / (1024.0 ** 3)
        if free_gb < 0.2:
            return False, '剩余空间 %.2f GB，不足 0.2 GB' % free_gb
        return True, ''
    except Exception as e:
        return True, str(e)


def main():
    # 1) 读设置。读不到就默认「不导出」而不是默认开启 ——
    #    定时器被谁 enable 的不好追溯，默认执行一个会写盘的动作不安全。
    r = call('backup', {'op': 'status'})
    if not r.get('ok'):
        log('warn', 'BK_STATUS_FAIL', '读取备份设置失败：%s' % r.get('msg_cn'))
        return 1
    conf = (r.get('data') or {}).get('conf') or {}
    if not conf.get('auto_enabled'):
        log('info', 'BK_SKIP', '自动备份未开启，跳过本次导出')
        return 0

    ok_disk, why = disk_ok()
    if not ok_disk:
        log('error', 'BK_DISK_LOW', '磁盘空间不足，跳过自动备份：%s' % why)
        return 1

    # 2) 导出。include_sensitive 不在这里覆盖 —— 定时备份默认不含私钥，
    #    私钥包需要用户在界面上主动生成（那个包权限是 0600）。
    r = call('backup', {'op': 'create'})
    if not r.get('ok'):
        log('error', 'BK_CREATE_FAIL', '自动备份导出失败：%s' % r.get('msg_cn'))
        return 1
    d = r.get('data') or {}
    log('info', 'BK_CREATED', '自动备份已导出 %s（%d 个文件，%.1f KB）'
        % (d.get('name', ''), d.get('files', 0), (d.get('size') or 0) / 1024.0))

    # 3) 清理
    r = call('backup', {'op': 'prune', 'keep_count': conf.get('keep_count', 0),
                        'keep_days': conf.get('keep_days', 0)})
    if not r.get('ok'):
        # 清理失败不算这次备份失败：包已经导出成功了，
        # 只是没清掉旧的。记 warn 后正常返回。
        log('warn', 'BK_PRUNE_FAIL', '清理旧备份包失败：%s' % r.get('msg_cn'))
        return 0
    removed = (r.get('data') or {}).get('removed') or []
    if removed:
        log('info', 'BK_PRUNED', '已清理 %d 个旧备份包' % len(removed))
    return 0


if __name__ == '__main__':
    sys.exit(main())
