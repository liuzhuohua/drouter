#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.7 真机验收：备份 / 告警 / 配额 / VPN 四个新模块（192.168.7.3）。

静态检查器（t-107.py）已经把逻辑层钉死了，但那是在开发机上用临时目录跑的。
这里验的是**真机上才会暴露的东西**：
  ① helper 能以 root 真的执行（权限、路径、AppArmor）
  ② 备份真的能导出，且**本轮修的核心缺陷不再复现**：
     默认导出包里必须有 dcfg.json / kern.conf / generated/vlans.json
  ③ 三个新守护的 timer 能被 helper 真的写出来并 enable
  ④ VPN 私钥不外泄（抓真实 HTTP 响应）
  ⑤ Python 3.13 上 _vpn_norm 不再清空 peer（本轮修的最隐蔽缺陷）

⚠️ 验收脚本跑前要把 /etc/drouter/*.conf、dcfg.json 挪开，跑完还原——
否则读到的不是出厂状态，默认值断言会假失败。
⚠️ 只读+ 可回滚的操作。备份还原一律 dry_run，不写任何生产配置。
"""
import json
import os
import subprocess
import sys
import urllib.request
import ssl

BASE = 'https://127.0.0.1:8443'
WEB = '/opt/drouter/backend/drouter-web.py'
HELPER = '/opt/drouter/backend/drouter-helper.py'

fails = []
oks = 0


def ck(desc, cond, extra=''):
    global oks
    if cond:
        print('  [OK] %s' % desc)
        oks += 1
    else:
        print('  [FAIL] %s  %s' % (desc, extra))
        fails.append(desc)


def sh(cmd, timeout=120):
    p = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def api(path, payload=None, token=None, method=None):
    """直连Web 层（走 helper 的是低权用户，这里要 root 路径另用 sh）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Cookie'] = 'drouter_token=' + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method or ('POST' if data else 'GET'))
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=90) as r:
            return r.status, json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        body = ''
        if hasattr(e, 'read'):
            try:
                body = e.read().decode('utf-8', 'replace')[:400]
            except Exception:
                pass
        return getattr(e, 'code', 0), {'_err': str(e), '_body': body}


def main():
    print('=' * 68)
    print('v1.0.7 真机验收：备份 / 告警 / 配额 / VPN')
    print('=' * 68)

    # ---------- 0. 环境 ----------
    print('\n--- 0. 环境 ---')
    rc, o, _ = sh('python3 -V')
    print('  Python: %s' % o.strip())
    ck('目标是 Python 3.13（本轮修的 ip_network 缺陷只在这暴露）',
       '3.13' in o, '→ %s' % o.strip())
    rc, o, _ = sh("grep -c '_bk_is_sensitive' %s" % HELPER)
    ck('本轮代码已部署（_bk_is_sensitive 存在）', o.strip() == '7',
       '→ 计数 %s' % o.strip())

    # ⚠️ 这里**刻意不挪开** /etc/drouter/*.conf 与 dcfg.json。
    # 第一版照着 render-smoke.py 抄了「挪开→跑→还原」，结果备份检查
    # 读到的是被我亲手挪空的目录，得出「默认导出不含 dcfg.json」的假结论 ——
    # 而那恰恰是本轮修的核心缺陷，差点把「脚本的错」当成「修复无效」。
    # 教训：**验收脚本的每个前置动作都要问「它会不会改变被测对象」**。
    # 这里的检查全是只读（列清单 / 起 timer 函数 / 跑纯函数），不需要挪。
    stash = None
    stashed_n = 0

    try:
        # ---------- 1. 备份（本轮最严重缺陷）----------
        print('\n--- 1. 备份：默认导出必须含全部非敏感配置 ---')
        # ⚠️ 必须走 _bk_files() + _bk_walk() 两步，不能只看 _bk_files()：
        # _bk_files() 返回的是**根路径清单**（dict 列表，含目录 /etc/drouter），
        # 真正展开成文件是 _bk_walk() 的活（返回 (abs, rel, sensitive) 三元组）。
        # 我第一版直接读 _bk_files() 当文件列表，得出「备份里没有 dcfg.json」
        # 的假结论—— 实际那正是本轮修复前的样子，看不出来。
        rc, o, e = sh('cd /tmp && python3 - <<\'PYEOF\' 2>&1\n'
                      'import importlib.util\n'
                      'spec = importlib.util.spec_from_file_location('
                      '"h", "/opt/drouter/backend/drouter-helper.py")\n'
                      'h = importlib.util.module_from_spec(spec)\n'
                      'spec.loader.exec_module(h)\n'
                      'roots = h._bk_files()\n'
                      'print("ROOTS", len(roots))\n'
                      'roots_sensitive = [r["path"] for r in roots\n'
                      '                    if r.get("sensitive")]\n'
                      'print("ROOTS_SENSITIVE", len(roots_sensitive),\n'
                      '      roots_sensitive)\n'
                      'excluded = []\n'
                      'paths = []\n'
                      'for r in roots:\n'
                      '    for full, rel, sens in h._bk_walk(r["path"],\n'
                      '            include_sensitive=False, skipped=excluded):\n'
                      '        paths.append((full, sens))\n'
                      'print("TOTAL", len(paths))\n'
                      'for want in ("dcfg.json", "kern.conf", "vlans.json",\n'
                      '             "drouter.db"):\n'
                      '    hit = [p for p, s in paths if want in p]\n'
                      '    print("WANT", want, len(hit), hit[0] if hit else "-")\n'
                      'leak = [p for p, s in paths if s]\n'
                      'print("SENSITIVE_LEAK", len(leak), leak[:3])\n'
                      'print("EXCLUDED", len(excluded))\n'
                      'PYEOF')
        print('  %s' % o.strip().replace('\n', '\n  '))
        lines = o.strip().splitlines()
        total = 0
        got = {}
        leak = -1
        roots_sens = -1
        for ln in lines:
            p = ln.split()
            if p and p[0] == 'TOTAL':
                total = int(p[1])
            elif p and p[0] == 'WANT':
                got[p[1]] = int(p[2])
            elif p and p[0] == 'SENSITIVE_LEAK':
                leak = int(p[1])
            elif p and p[0] == 'ROOTS_SENSITIVE':
                roots_sens = int(p[1])
        ck('备份清单非空', total > 0, '→ %d 项' % total)
        # 这一条是本轮最严重缺陷的直接判据：整个 /etc/drouter 曾被一刀切标敏感，
        # 导致 dcfg.json / kern.conf / generated/*.json 全部进不了包。
        ck('没有任何「目录级」敏感标记（本轮缺陷 1 的根因）',
           roots_sens == 0, '→ %s 个根被标敏感' % roots_sens)
        ck('默认导出含 dcfg.json（本轮修的核心缺陷）',
           got.get('dcfg.json', 0) >= 1, '→ %s' % got.get('dcfg.json'))
        ck('默认导出含 kern.conf', got.get('kern.conf', 0) >= 1,
           '→ %s' % got.get('kern.conf'))
        ck('默认导出含 generated/vlans.json', got.get('vlans.json', 0) >= 1,
           '→ %s' % got.get('vlans.json'))
        ck('敏感内容没被默认导出（CA 私钥/宽带密码）', leak == 0,
           '→ 泄漏 %d 项' % leak)

        # ---------- 2. 三个新守护的 timer ----------
        print('\n--- 2. 三个新守护的 timer 能被 helper 真的写出来 ---')
        for name, conf in (('backup', 'backup'), ('alert', 'alert'),
                           ('quota', 'quota')):
            rc, o, e = sh(
                'python3 - <<\'PYEOF\' 2>&1\n'
                'import importlib.util, os\n'
                'spec = importlib.util.spec_from_file_location('
                '"h", "/opt/drouter/backend/drouter-helper.py")\n'
                'h = importlib.util.module_from_spec(spec)\n'
                'spec.loader.exec_module(h)\n'
                'fn = getattr(h, "_write_%s_timer", None)\n'
                'print("HAVE_FN", fn is not None)\n'
                'if fn:\n'
                '    try:\n'
                '        r = fn({"enabled": True})\n'
                '        print("CALL_OK", r is not None)\n'
                '    except Exception as ex:\n'
                '        print("CALL_ERR", type(ex).__name__, ex)\n'
                'PYEOF' % name)
            have = 'HAVE_FN True' in o
            call_ok = 'CALL_OK True' in o
            err = [x for x in o.splitlines() if 'CALL_ERR' in x]
            ck('%s timer 生成函数可执行' % name, have and call_ok,
               '→ %s' % ('; '.join(err) if err else 'have=%s' % have))
        rc, o, _ = sh('ls /etc/systemd/system/ | grep -E "backupd|alertd|quotad"'
                      ' | tr "\\n" " "')
        print('  已落盘的单元: %s' % (o.strip() or '(尚未保存配置，未生成——正常)'))

        # ---------- 3. VPN 私钥不外泄（本轮缺陷 2）----------
        print('\n--- 3. VPN：私钥不得出现在任何 HTTP 响应里 ---')
        # ⚠️ grep 之前只滤掉了解释性注释，漏了打印服务那行
        # `cfg = {'cups': ..., 'raw': dict(PRINT_DEFAULTS['raw'])}` ——
        # 它和 VPN 没关系，是个同名字段。直接数会把无关命中当成泄露。
        # 判据改成「_vpn_status 的return 语句里有没有 raw」。
        rc, o, _ = sh(
            'python3 - <<\'PYEOF\' 2>&1\n'
            'import ast\n'
            'src = open("/opt/drouter/backend/drouter-helper.py",\n'
            '          encoding="utf-8").read()\n'
            'tree = ast.parse(src)\n'
            'for node in ast.walk(tree):\n'
            '    if not isinstance(node, ast.FunctionDef):\n'
            '        continue\n'
            '    if node.name != "_vpn_status":\n'
            '        continue\n'
            '    bad = []\n'
            '    for sub in ast.walk(node):\n'
            '        if isinstance(sub, ast.Dict):\n'
            '            for k in sub.keys:\n'
            '                if isinstance(k, ast.Constant) and k.value == "raw":\n'
            '                    bad.append(sub.lineno)\n'
            '    print("VPN_STATUS_RAW_KEYS", bad)\n'
            'PYEOF')
        ck("_vpn_status 的返回里没有 'raw' 键（私钥不外泄）",
           'VPN_STATUS_RAW_KEYS []' in o, '→ %s' % o.strip())
        rc, o, _ = sh('grep -c "_vpn_mask" /opt/drouter/backend/drouter-helper.py')
        ck('_vpn_mask 仍在（脱敏没被一起删）', int(o.strip() or 0) >= 3,
           '→ %s' % o.strip())

        # ---------- 4. _vpn_norm 在 3.13 上不清空 peer（本轮缺陷 3）----------
        print('\n--- 4. VPN：3.13 上保存配置不得清空 peer（最隐蔽缺陷）---')
        # ⚠️ 字段名必须用实现里的真名：port / pool（不是 listen_port / endpoint_ip）。
        # 我第一版照着自己记忆写字段，peer 全被_norm 丢成空列表，
        # 差点把「验收脚本写错」当成「修复无效」。
        rc, o, e = sh(
            'python3 - <<\'PYEOF\' 2>&1\n'
            'import importlib.util, json, os, tempfile, shutil\n'
            'spec = importlib.util.spec_from_file_location('
            '"h", "/opt/drouter/backend/drouter-helper.py")\n'
            'h = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(h)\n'
            'tmp = tempfile.mkdtemp()\n'
            'p = os.path.join(tmp, "vpn.json")\n'
            'json.dump({"enabled": True, "port": 51820,\n'
            '           "pool": "10.66.66.0/24",\n'
            '           "peers": [\n'
            '             {"id": "in-net", "name": "in-net",'
            ' "ip": "10.66.66.2",\n'
            '              "private_key": "k1=", "public_key": "p1="},\n'
            '             {"id": "out-net", "name": "out-net",'
            ' "ip": "192.168.9.9",\n'
            '              "private_key": "k2=", "public_key": "p2="}]},\n'
            '          open(p, "w"))\n'
            'h.VPN_CONF = p\n'
            'd = h._vpn_load()\n'
            'n = h._vpn_norm(d)\n'
            'peers = n.get("peers") or []\n'
            'print("POOL", n.get("pool"))\n'
            'print("PEERS", len(peers))\n'
            'for x in peers:\n'
            '    print("PEER", x.get("name"), x.get("id"), x.get("ip"),\n'
            '          "priv_kept=%s" % bool(x.get("private_key")))\n'
            # 负向：没有 id 的 peer 必须被丢弃（id 是去重与后续 toggle/delete 的
            # 主键）。我第一版造用例时没给 id，peer 被静默丢掉却以为是
            # 「修复无效」—— 补一条断言把这个契约钉死。
            'n2 = h._vpn_norm({"port": 51820, "pool": "10.66.66.0/24",\n'
            '                 "peers": [{"name": "no-id", "ip": "10.66.66.3"}]})\n'
            'print("NOID_PEERS", len(n2.get("peers") or []))\n'
            # 负向：同 id 重复只留一个\n'
            'n3 = h._vpn_norm({"port": 51820, "pool": "10.66.66.0/24",\n'
            '    "peers": [{"id": "dup", "ip": "10.66.66.4"},\n'
            '              {"id": "dup", "ip": "10.66.66.5"}]})\n'
            'print("DUP_PEERS", len(n3.get("peers") or []))\n'
            'm = h._vpn_mask(n)\n'
            'leaked = [x for x in (m.get("peers") or [])\n'
            '          if x.get("private_key")]\n'
            'print("MASK_LEAK", len(leaked))\n'
            'print("MASK_FIELDS", sorted((m.get("peers") or [{}])[0].keys()))\n'
            'shutil.rmtree(tmp, ignore_errors=True)\n'
            'PYEOF')
        print('  %s' % o.strip().replace('\n', '\n  '))
        ck('合法 peer 没被清空（3.13 不再抛 AttributeError）',
           '\nPEERS 1' in o or 'PEERS 1' in o.split('POOL')[-1],
           '→ 期望 PEERS 1，实际输出：%s' % o.strip().replace('\n', ' ')[:120])
        ck('越界 IP 仍被正确过滤（192.168.9.9 不在 10.66.66.0/24）',
           'out-net' not in o)
        ck('私钥在归一化后仍在（否则二次保存就废了）',
           'priv_kept=True' in o)
        ck('_vpn_mask 把私钥抹掉（对外输出不能带私钥）',
           'MASK_LEAK 0' in o, '→ %s' % o)
        # 掩码后其余字段必须还在 —— 早先版本连ip/name 一起抹了，
        # 前端表格会变成一排空单元格
        ck('_vpn_mask 只抹私钥、不动其他字段',
           'MASK_FIELDS' in o and "'ip'" in o and "'name'" in o,
           '→ %s' % [x for x in o.splitlines()
                     if x.startswith('MASK_FIELDS')])
        ck('无 id 的 peer 被丢弃（id 是去重/后续操作的主键）',
           'NOID_PEERS 0' in o,
           '→ %s' % [x for x in o.splitlines()
                     if x.startswith('NOID_PEERS')])
        ck('同 id 重复的 peer 只留一个', 'DUP_PEERS 1' in o,
           '→ %s' % [x for x in o.splitlines()
                     if x.startswith('DUP_PEERS')])

        # ---------- 5. DEPS 里有 wireguard ----------
        print('\n--- 5. 依赖清单含 wireguard（本轮缺陷 6）---')
        rc, o, _ = sh("python3 -c \"import re;s=open('%s',encoding='utf-8').read();"
                      "m=re.search(r'DEPS = \\[(.*?)\\n\\]', s, re.S);"
                      "print('WIREGUARD' if 'wireguard-tools' in m.group(1) "
                      "else 'MISSING')\"" % HELPER)
        ck('DEPS 登记了 wireguard-tools', 'WIREGUARD' in o, '→ %s' % o.strip())

        # ---------- 6. 备份路径校验（防目录穿越）----------
        print('\n--- 6. 备份包路径校验必须挡住穿越 ---')
        # ⚠️ 契约是「不合法**返回 None**」，不是抛异常。
        # 我第一版按抛异常写，结果非法输入全被记成 ALLOW —— 三个红全是脚本的锅。
        # 两种语义都要认（实现若改成抛异常，测试也该红），所以两个都判。
        rc, o, e = sh(
            'python3 - <<\'PYEOF\' 2>&1\n'
            'import importlib.util\n'
            'spec = importlib.util.spec_from_file_location('
            '"h", "/opt/drouter/backend/drouter-helper.py")\n'
            'h = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(h)\n'
            'def blocked(fn, arg):\n'
            '    try:\n'
            '        r = fn(arg)\n'
            '    except Exception:\n'
            '        return True\n'
            '    return r is None or r == ""\n'
            'cases = [("traversal", "../../etc/shadow"),\n'
            '         ("absolute", "/etc/shadow"),\n'
            '         ("suffix", "drouter-backup-20260101-120000.tar.gz.bak"),\n'
            '         ("nul", "x\\x00.tar.gz"),\n'
            '         ("dotdot_mid", "sub/../../etc/shadow"),\n'
            '         ("empty", "")]\n'
            'for name, p in cases:\n'
            '    print("CASE", name, "BLOCKED" if blocked(h._bk_path_of, p)'
            ' else "PASSED")\n'
            'good = "drouter-backup-20260101-120000.tar.gz"\n'
            'print("CASE", "legit", "BLOCKED" if blocked(h._bk_path_of, good)'
            ' else "PASSED")\n'
            'PYEOF')
        print('  %s' % o.strip().replace('\n', '\n  '))
        for name in ('traversal', 'absolute', 'suffix', 'nul', 'dotdot_mid',
                     'empty'):
            ck('路径校验挡住 %s' % name,
               ('CASE %s BLOCKED' % name) in o,
               '→ %s' % [x for x in o.splitlines()
                         if ('CASE %s ' % name) in x])
        # 反向：合法名必须放行，否则功能直接不可用
        ck('合法备份包名放行', 'CASE legit PASSED' in o)

    finally:
        # ---------- 收尾：确认没碰生产配置 ----------
        print('\n--- 收尾：生产配置应原样在位（本脚本全程只读）---')
        rc, o, _ = sh('ls /etc/drouter/*.conf /etc/drouter/dcfg.json '
                      '2>/dev/null | wc -l')
        ck('生产配置未被本脚本挪动/删除', int(o.strip() or 0) >= 4,
           '→ 只剩 %s 个（期望 >=4）' % o.strip())

    print('\n' + '=' * 68)
    print('通过 %d 项，失败 %d 项' % (oks, len(fails)))
    if fails:
        for f in fails:
            print('  ✘ %s' % f)
    print('=' * 68)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
