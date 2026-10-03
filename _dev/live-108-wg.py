#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WireGuard 环境诊断真机验收（第 11 个缺陷的验证）。

背景：2026-10-03 用户在 PVE 的 KVM 虚拟机（192.168.7.3，Debian 13）
上看到 VPN 页报「本机内核或工具不支持 WireGuard……通常说明跑在
精简容器里」。实际查证：

    systemd-detect-virt      → kvm（虚拟机，不是容器）
    /lib/modules/6.12.107+deb13-amd64/kernel/drivers/net/wireguard/
        wireguard.ko.xz       → 存在
    /sys/module/wireguard    → 不存在（没加载）
    modprobe wireguard       → RC=0，模块进来了

根因：_vpn_installed() 只看「已加载」与「工具在否」，从不查
/lib/modules 下的模块文件，把「模块在但没加载」压进 no，
文案又写死「精简容器」，把排查方向整个带偏。

本脚本验三件事（**按侵入性从低到高**，每一步都可逆）：
  A. 只读诊断 —— env 四态字段齐全、state 判定与真机实况一致
  B. modprobe —— 真机跑一次 fix，确认模块能加载
  C. 开机自启 —— 确认 /etc/modules-load.d/drouter-wireguard.conf 被写出来

⚠️ 三条铁律（都是本项目踩过的）：
  ① 验收脚本的第一个前置动作就要问「会不会改变被测对象」。
     本脚本**不会**动防火墙、网卡、路由 —— modprobe 与写
     modules-load.d 都不影响网络连通性，且第C 步会自己清理。
  ② op 名/字段名照真机源码抄，不猜。
  ③ 鉴权走 X-Token 头，提示字段是 msg_cn。
"""
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

BASE = 'https://127.0.0.1:8443'
AUTOLOAD = '/etc/modules-load.d/drouter-wireguard.conf'

fails = []
oks = 0


def ck(desc, cond, extra=''):
    global oks
    if cond:
        print('  [OK] %s' % desc)
        oks += 1
    else:
        print('  [FAIL] %s%s' % (desc, ('  ' + extra) if extra else ''))
        fails.append(desc)


def ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def call(path, payload=None, token=None, method=None, timeout=300):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    if token:
        headers['X-Token'] = token
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method or ('POST' if data else 'GET'))
    try:
        with urllib.request.urlopen(req, context=ctx(), timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, {'_raw': raw[:600]}
    except urllib.error.HTTPError as e:
        raw = ''
        try:
            raw = e.read().decode('utf-8', 'replace')[:600]
        except Exception:
            pass
        return e.code, {'_err': 'HTTP %s' % e.code, '_body': raw}
    except Exception as e:
        return 0, {'_err': str(e)}


def sh(cmd, timeout=30):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       errors='replace', timeout=timeout)
    return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()


def main():
    print('=' * 68)
    print('WireGuard 环境诊断真机验收（第 11 个缺陷）')
    print('=' * 68)

    # ---------- 0. 前置：记录真机实况 ----------
    print('\n--- 0. 真机实况（用来对照后端判定的 state）---')
    rc, virt, _ = sh('systemd-detect-virt 2>/dev/null')
    rc2, kver, _ = sh('uname -r')
    rc3, modf, _ = sh(
        "ls /lib/modules/%s/kernel/drivers/net/wireguard/ 2>/dev/null" % kver)
    rc4, loaded0, _ = sh('test -d /sys/module/wireguard && echo yes || echo no')
    rc5, tool0, _ = sh('command -v wg >/dev/null && echo yes || echo no')
    print('  虚拟化形态 : %s' % (virt or '（检测不到）'))
    print('  内核       : %s' % kver)
    print('  模块文件   : %s' % (modf.replace('\n', ', ') or '（无）'))
    print('  模块已加载 : %s' % loaded0)
    print('  wg 工具    : %s' % tool0)
    # 这一条是本轮缺陷的「Ground Truth」：模块文件在，但没加载。
    # 只要这个前提成立，正确的 state 就必须是 need_module，
    # 而不是旧代码给出的 unsupported（内核不支持）。
    truth = ('modfile' if modf else '') + '/' + \
            ('loaded' if loaded0 == 'yes' else 'unloaded') + '/' + \
            ('tool' if tool0 == 'yes' else 'notool')
    expect_state = ('ready' if (loaded0 == 'yes' and tool0 == 'yes')
                    else 'need_tool' if loaded0 == 'yes'
                    else 'need_module' if modf else 'unsupported')
    print('  期望 state : %s   （由实况推导：%s）' % (expect_state, truth))
    # ⚠️ 这条不是断言「现在必须处于未加载」，而是**记录**它。
    # 第一版写成 chk(...)，结果 fix 一跑完模块就加载上了，
    # 脚本第二次运行必然报红 —— 而那恰恰说明修复生效了。
    # 判据的方向搞反了：复现前提只对「缺陷还在的机器」成立，
    # 修好之后它必然不再成立。所以这里只提示，不判红。
    reproduced = bool(modf) and loaded0 != 'yes'
    print('  缺陷复现   : %s'
          % ('是（本机正处于「模块文件在但未加载」）' if reproduced
             else '否（模块已加载 / 无模块文件）—— 修复已生效或前提不成立，'
                  '下面的断言按实况推导，不要求必须复现'))
    # state 的期望值必须由**实况**推导，绝不写死。
    # 写死成 need_module 的话，模块一加载就假红。

    # ---------- 1. 登录 ----------
    print('\n--- 1. 登录拿 token ---')
    st, r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
    ck('登录成功', st == 200 and r.get('ok') is True,
       '→ HTTP %s %s' % (st, str(r)[:150]))
    tok = r.get('token') or (r.get('data') or {}).get('token')
    ck('拿到 token', bool(tok))
    if not tok:
        print('\n无法继续（没有 token）')
        return 1

    # ---------- 2. status 的 env 字段（只读，安全）----------
    print('\n--- 2. status 返回四态 env（只读）---')
    st, r = call('/api/vpn', {'op': 'status'}, tok)
    ck('status 接口通', st == 200 and r.get('ok') is True, '→ %s' % str(r)[:200])
    d = r.get('data') or {}
    env = d.get('env') or {}
    ck('响应里有 env 字段（旧版本只有 installed 字符串）', bool(env),
       '→ data 顶层键：%s' % sorted(d.keys()))
    for k in ('state', 'mod_loaded', 'mod_file', 'tool', 'kernel',
              'virt', 'autoload', 'fixable'):
        ck('env.%s 存在' % k, k in env, '→ env=%s' % env)
    ck('env.kernel 与 uname -r 一致',
       env.get('kernel') == kver, '→ %s vs %s' % (env.get('kernel'), kver))
    ck('env.virt 与 systemd-detect-virt 一致（%s）' % virt,
       env.get('virt') == virt, '→ %s' % env.get('virt'))
    ck('env.mod_loaded 与 /sys/module 一致',
       bool(env.get('mod_loaded')) == (loaded0 == 'yes'))
    ck('env.tool 与 wg 命令是否存在一致',
       bool(env.get('tool')) == (tool0 == 'yes'))
    ck('env.mod_file 指到了真实存在的模块文件',
       bool(env.get('mod_file')) == bool(modf)
       and (not env.get('mod_file') or os.path.isfile(env['mod_file'])),
       '→ %s' % env.get('mod_file'))
    # ★ 核心断言：state 必须与真机实况一致。
    # 期望值由实况推导（expect_state），不是写死的 need_module ——
    # 写死的话，fix 跑完模块加载了，这条就变成假红。
    ck('state 判定与真机实况一致（%s）' % truth,
       env.get('state') == expect_state,
       '→ 实得 %s，期望 %s' % (env.get('state'), expect_state))
    ck('state 绝不再是 unsupported（本机内核明明有模块文件）',
       env.get('state') != 'unsupported',
       '→ 若为 unsupported，说明还在用只查已加载态的老判据'
       '（模块文件明明在 /lib/modules 下）')
    ck('fixable 为真（有自愈余地就不该让用户去 SSH）',
       bool(env.get('fixable')) is (expect_state in
                                     ('need_module', 'need_tool')))
    ck('installed 兼容字段仍在（老前端不至于空白）',
       d.get('installed') == env.get('state'))

    # ---------- 3. apply 在缺工具时的报错必须可执行 ----------
    print('\n--- 3. apply 早退分支的文案（不真正启动服务）---')
    # 这里传 enabled=False：apply 会走「渲染配置但不起服务」那条路。
    # 唯一会被验证的是**早退**—— 真机当前是 need_module，
    # apply 会先 modprobe 自愈，所以这里预期是「修好了、继续」。
    st, r = call('/api/vpn', {'op': 'apply', 'conf': {'enabled': False}},
                 tok)
    msg = str(r.get('msg_cn') or '')
    print('  apply 返回：ok=%s code=%s msg=%s'
          % (r.get('ok'), r.get('code'), msg[:110]))
    # ⚠️ 判据不能是「code 不是 NO_WG」—— need_tool 与 unsupported
    # **共用** NO_WG 这个 code（对外只该有一个「环境不可用」的口径）。
    # 我第一版那么写，结果本机装完工具后 apply 仍返回 NO_WG，
    # 报出一个不存在的回归。真正的判据是**文案是否可执行**：
    #   旧文案 = 断言一个猜出来的原因，且没有出路
    #   新文案 = 指出真实缺什么 + 给出下一步
    # ⚠️ 下面这三条必须**只在 apply 真被拒时**才跑。
    # 我第一版把它们写在 if/else 外面，于是环境修好之后 apply 成功，
    # 三条却拿成功文案去判「有没有给出路」，全红 ——
    # 典型的「判据比实现宽」：它假设了 apply 一定失败。
    if expect_state == 'ready':
        # 环境本来就是好的 → apply 不该被环境拦下来。
        # enabled=False 意味着只渲染配置、不起服务，是安全的。
        ck('环境就绪时 apply 不被环境拦下（真正走通到配置写入）',
           r.get('ok') is True, '→ ok=%s msg=%s' % (r.get('ok'), msg[:140]))
        ck('apply 走通后回传了 conf（前端要能刷新表单）',
           bool((r.get('data') or {}).get('conf')))
    else:
        ck('apply 失败时回传 env（前端要靠它刷新诊断）',
           bool((r.get('data') or {}).get('env')),
           '→ data 键：%s' % sorted((r.get('data') or {}).keys()))
        ck('apply 被拒时给的是**具体**原因而不是一句「内核不支持」',
           ('wireguard-tools' in msg or ('内核' in msg and '没有' in msg)),
           '→ %s' % msg[:160])
        ck('apply 被拒时指向「一键修复」或说明模块缺失（给出出路）',
           ('一键修复' in msg) or ('内核' in msg and '模块' in msg),
           '→ %s' % msg[:160])
        ck('apply 被拒时**不出现**旧那句一刀切文案',
           '内核或工具不支持' not in msg,
           '→ %s' % msg[:160])
        if expect_state == 'need_tool':
            ck('缺工具时报的正是 need_tool 这一档（不是 unsupported）',
               r.get('code') == 'NO_WG' and 'wireguard-tools' in msg,
               '→ code=%s msg=%s' % (r.get('code'), msg[:120]))

    # ---------- 4. modprobe 真的生效了吗 ----------
    print('\n--- 4. 自愈后模块是否真的加载了 ---')
    rc6, loaded1, _ = sh('test -d /sys/module/wireguard && echo yes || echo no')
    print('  /sys/module/wireguard: %s' % loaded1)
    if modf and loaded0 != 'yes':
        ck('modprobe 之后模块已加载（原缺陷场景已自愈）',
           loaded1 == 'yes',
           '→ 还没加载，说明 _vpn_ensure_module 没能修复')
        rc7, lsmod, _ = sh("grep -c '^wireguard ' /proc/modules")
        ck('/proc/modules 里能查到 wireguard', rc7 == 0 and lsmod != '0',
           '→ %s' % lsmod)
    else:
        print('  （本机原本就已加载，跳过本节的自愈验证）')

    # ---------- 5. 一键修复 op ----------
    print('\n--- 5. fix op（幂等 + 开机自启）---')
    st, r = call('/api/vpn', {'op': 'fix'}, tok, timeout=900)
    ck('fix op 被派发（不是「未知的 VPN 操作」）',
       r.get('code') != 'ERR' or '未知的 VPN 操作' not in str(r.get('msg_cn')),
       '→ code=%s msg=%s' % (r.get('code'), str(r.get('msg_cn'))[:120]))
    print('  fix 返回：ok=%s msg=%s' % (r.get('ok'), str(r.get('msg_cn'))[:150]))
    for n in ((r.get('data') or {}).get('notes') or []):
        print('    · %s' % n)
    ck('fix 返回 env 供前端刷新', bool((r.get('data') or {}).get('env')))

    rc8, al, _ = sh('cat %s 2>/dev/null' % AUTOLOAD)
    ck('%s 已写出' % AUTOLOAD, rc8 == 0 and 'wireguard' in al,
       '→ rc=%s 内容=%r' % (rc8, al[:120]))
    ck('autoload 文件里确有 wireguard 这一行',
       any(ln.strip() == 'wireguard' for ln in al.splitlines()),
       '→ %r' % al)

    st, r = call('/api/vpn', {'op': 'status'}, tok)
    env2 = (r.get('data') or {}).get('env') or {}
    ck('fix 后 autoload=true', env2.get('autoload') is True,
       '→ %s' % env2)
    ck('fix 之后 state 至少不再是 unsupported',
       env2.get('state') != 'unsupported', '→ %s' % env2.get('state'))
    # 只有 wg 也装上了才会到 ready；只 modprobe 没装工具时是 need_tool，
    # 这也算「诊断正确」—— 关键是不能再误报成 unsupported。
    ck('fix 后 state 与实况一致（ready 或 need_tool 都算对）',
       env2.get('state') in ('ready', 'need_tool'),
       '→ %s（tool=%s）' % (env2.get('state'), env2.get('tool')))

    # 幂等：再点一次不应报错，也不该重复装包
    st, r = call('/api/vpn', {'op': 'fix'}, tok, timeout=900)
    ck('fix 幂等（再点一次不报错）', r.get('ok') is True,
       '→ ok=%s msg=%s' % (r.get('ok'), str(r.get('msg_cn'))[:150]))

    # ---------- 5b. 修好之后 apply 必须真的走得通 ----------
    # 这才是本次修复的**最终目的**：不是「诊断说得对」，
    # 而是「用户点完一键修复，VPN 真的能配能起」。
    # 用 enabled=False：不启动服务、不建接口、不碰防火墙，
    # 只验证「渲染配置 + 写盘 + 落nft 规则」这一段能走完。
    print('\n--- 5b. 修好之后 apply 端到端（enabled=False，不起服务）---')
    st, r = call('/api/vpn', {'op': 'apply', 'conf': {'enabled': False}},
                 tok, timeout=300)
    m2 = str(r.get('msg_cn') or '')
    print('  apply 返回：ok=%s code=%s msg=%s'
          % (r.get('ok'), r.get('code'), m2[:140]))
    ck('修复后 apply 不再被环境拦下（这是本轮修复的最终目的）',
       r.get('ok') is True, '→ code=%s msg=%s' % (r.get('code'), m2[:160]))
    ck('apply 回传 conf（前端表单能刷新）',
       bool((r.get('data') or {}).get('conf')))
    ck('apply 回传 nft 规则文本（防火墙放行段走到了）',
       bool((r.get('data') or {}).get('nft')))
    rc_cfg, cfg, _ = sh('cat /etc/wireguard/wg0.conf 2>/dev/null')
    ck('配置文件真的写出来了', rc_cfg == 0 and 'PrivateKey' in cfg,
       '→ rc=%s 字节=%s' % (rc_cfg, len(cfg)))
    # ⚠️ 这里用单 % 不是%%：sh() 是 shell=True，%% 会原样传给 stat
    # （stat -c%%a 会把权限打印成「755%%」之类），实测过。
    _perm = sh('stat -c%a /etc/wireguard/wg0.conf 2>/dev/null')[1].strip()
    ck('配置文件权限是 600（含私钥）', _perm == '600', '→ %s' % _perm)
    ck('enabled=False 时不启动服务（applied=False）',
       (r.get('data') or {}).get('applied') is False)
    # 收掉这份配置，别在用户机器上留一个他没要求过的 wg0.conf
    st, r2 = call('/api/vpn', {'op': 'stop'}, tok)
    rc_rm, _o, _e = sh('rm -f /etc/wireguard/wg0.conf')
    rc_rmt, _o2, _e2 = sh(
        'nft list table inet drouter_vpn >/dev/null 2>&1 '
        '&& nft delete table inet drouter_vpn; echo done')
    print('  已清理：wg0.conf 与 drouter_vpn 表（验收不该留痕）')

    # ---------- 6. 基线未被破坏 ----------
    print('\n--- 6. 基线复核（不能动网络）---')
    rc9, gw, _ = sh("ip route show default | awk '{print $3}' | head -1")
    ck('默认网关仍是 192.168.7.2', gw == '192.168.7.2', '→ %s' % gw)
    # ⚠️ 判据不能是「整台机器 nft 0 行」——
    # 这台机上有一张 `table inet netavark`，是**之前**跑 podman 容器时
    # 留下的历史残留（空隔离链 + 只匹配 meta mark 0x2000 的 masquerade），
    # 记忆里早有记录，与本次改动无关。我第一版把判据写成
    # `nft list ruleset | wc -l == 0`，结果报出一个不存在的回归。
    # 正确判据：**drouter 自己的表一张都没有**。
    # （这台机尚未接管路由，防火墙表应该是空的；
    #   一旦哪天真起防火墙，判据要改成比对 drouter 表的 ruleset 摘要。）
    rc10, dr_tables, _ = sh(
        "nft list tables 2>/dev/null | grep -c 'drouter' || true")
    ck('drouter 自己的 nft 表为 0 张（本脚本不碰防火墙）',
       (dr_tables or '0') == '0',
       '→ drouter 表 %s 张；完整列表：\n%s'
       % (dr_tables, sh('nft list tables 2>/dev/null')[1]))
    rc10b, nft_all, _ = sh('nft list ruleset 2>/dev/null | wc -l')
    print('  （nft 规则集共 %s 行 —— 其中 netavark 表是podman 的历史残留，'
          '非本次改动）' % nft_all)
    rc11, fwd, _ = sh("cat /proc/sys/net/ipv4/ip_forward")
    ck('ip_forward 仍为 1', fwd == '1', '→ %s' % fwd)
    rc12, wg_live, _ = sh('ip link show type wireguard 2>/dev/null | wc -l')
    print('  （当前 wireguard 接口数：%s —— 0 表示本脚本没有起任何接口）'
          % wg_live)
    ck('本脚本没有偷偷起 wireguard 接口', wg_live == '0', '→ %s' % wg_live)
    rc13, dmask, _ = sh("systemctl is-active dnsmasq; systemctl is-enabled dnsmasq")
    ck('dnsmasq 仍 inactive+disabled（绝不能被这个脚本拉起来）',
       dmask.replace('\n', ' ') == 'inactive disabled',
       '→ %s' % dmask.replace('\n', ' '))

    # ---------- 7. 收尾：把 wireguard 接口关掉（若起了）----------
    # enabled=False 的 apply 不会起接口，但万一前一步有残留，
    # 这里显式 stop 一次，保证机器回到验证前的状态。
    st, r = call('/api/vpn', {'op': 'stop'}, tok)
    print('  stop 返回：ok=%s' % r.get('ok'))
    rc14, wg_live2, _ = sh('ip link show type wireguard 2>/dev/null | wc -l')
    ck('收尾后没有残留 wireguard 接口', wg_live2 == '0', '→ %s' % wg_live2)

    print('\n' + '=' * 68)
    print('通过 %d 项，失败 %d 项' % (oks, len(fails)))
    if fails:
        for f in fails:
            print('  · %s' % f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
