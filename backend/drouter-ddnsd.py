#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drouter DDNS 守护：按 interval 定时检测公网地址，变化时才下发。

设计要点：
  * **只在变化时下发**。IPv4 可能几个月不变，每 5 分钟去敲一次服务商的
    API 是白消耗配额（DNSPod / 阿里云都有频率限制），也会被对方判定为滥用。
  * **失败不 spam**。同一份配置连续失败只记一条日志并按退避重试，
    成功一次就清零 —— 否则服务商的限流会让 DDNS 在恢复前一直失败。
  * 容器形态没有 systemd 时靠 helper 的 `_shelld_spawn` 拉起（见 helper）。

被 systemd timer 与容器 fallback 两条路径共用，所以**不能依赖 init**。
"""
import importlib.util
import json
import os
import sys
import time

HELPER = '/opt/drouter/backend/drouter-helper.py'

# 连续失败后的退避（秒）。失败一次等 3 个周期，成功一次立刻清零。
BACKOFF_MULT = 3
MAX_BACKOFF = 3600


def _load_helper():
    spec = importlib.util.spec_from_file_location('drouter_helper', HELPER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    h = _load_helper()
    cfg = h._load_setting('ddns', {}) or {}
    interval = max(60, min(int(cfg.get('interval') or 300), 86400))

    if not cfg.get('enabled'):
        h.log('info', 'ddns', 'DDNS_SKIP', 'DDNS 未启用，本次不检查')
        return 0
    if not (cfg.get('domain') or '').strip():
        h.log('warn', 'ddns', 'DDNS_NODOMAIN',
              'DDNS 已启用但没填主域名，本次不检查')
        return 0

    backoff_file = '/var/lib/drouter/ddns-backoff.json'
    try:
        os.makedirs(os.path.dirname(backoff_file), exist_ok=True)
        st = json.load(open(backoff_file, encoding='utf-8'))
        fails = int(st.get('fails') or 0)
    except Exception:
        fails = 0

    # 没到重试时间就别探测外网 —— 失败时一次完整探测要 5 秒以上。
    if fails > 0:
        wait = min(interval * (BACKOFF_MULT ** min(fails, 3)), MAX_BACKOFF)
        try:
            last = os.stat(backoff_file).st_mtime
            if time.time() - last < wait:
                return 0
        except Exception:
            pass

    pub = h.detect_public_ip(force=True)
    targets = []
    if cfg.get('ipv4', True) and pub.get('v4_public'):
        targets.append(('A', pub['v4_public']))
    if cfg.get('ipv6') and pub.get('v6_public'):
        targets.append(('AAAA', pub['v6_public']))
    if not targets:
        h.log('warn', 'ddns', 'DDNS_NOIP', '本次没取到可用公网地址，跳过')
        _save_backoff(backoff_file, fails + 1)
        return 0

    # 与上次成功记录比对，没变就不下发
    changed = []
    for rtype, ip in targets:
        key = 'last_ip4' if rtype == 'A' else 'last_ip6'
        if (cfg.get(key) or '') != ip:
            changed.append((rtype, ip))

    if not changed:
        # 记下这次的地址（首次运行时会在这里落盘），下次才有可比对的值
        for rtype, ip in targets:
            cfg['last_ip4' if rtype == 'A' else 'last_ip6'] = ip
        cfg['last_result'] = 'unchanged'
        cfg['last_msg_cn'] = '公网地址未变化（%s），本次无需下发' % (
            '、'.join(ip for _t, ip in targets))
        h._save_setting('ddns', cfg)
        _save_backoff(backoff_file, 0)
        return 0

    rec = h._ddns_record_name(cfg)
    prov = cfg.get('provider') or 'custom'
    okc = 0
    msgs = []
    for rtype, ip in changed:
        r = h._ddns_push(cfg, rtype, ip)
        if r.get('ok'):
            okc += 1
            cfg['last_ip4' if rtype == 'A' else 'last_ip6'] = ip
        else:
            msgs.append('%s %s：%s' % (rtype, r.get('msg') or '失败',
                                      r.get('detail') or ''))
    cfg['last_update'] = h.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if okc == len(changed):
        cfg['last_result'] = 'ok'
        cfg['last_msg_cn'] = '%s 更新成功：%s' % (prov, rec)
        h.log('info', 'ddns', 'DDNS_OK',
              '公网地址变化，已更新 %s（%s）' % (rec, prov))
        _save_backoff(backoff_file, 0)
    else:
        cfg['last_result'] = 'partial' if okc else 'fail'
        cfg['last_msg_cn'] = '更新失败：' + '；'.join(msgs)
        h.log('warn', 'ddns', 'DDNS_FAIL', cfg['last_msg_cn'])
        _save_backoff(backoff_file, fails + 1)
    h._save_setting('ddns', cfg)
    return 0


def _save_backoff(path, n):
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'fails': int(n), 'ts': time.time()}, f)
        os.replace(tmp, path)
    except Exception:
        pass


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        try:
            _load_helper().log('error', 'ddns', 'DDNS_CRASH',
                               'DDNS 守护异常：%s' % e)
        except Exception:
            pass
        sys.exit(1)
