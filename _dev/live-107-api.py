#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.0.7 真机验收（第二段）：HTTP 接口层 + 四个新页面的字段契约。

live-107.py 验的是「函数级」行为。这段验的是**端到端**：
真的登录拿token、真的 POST、真的看返回体里前端要的字段在不在。

判据来自 _dev/t-107.py 的 A 节（字段契约双向对照），但那是静态扫源码。
这里是真打一次接口 —— 静态能过的组合（路由存在 + op 放行 + 字段返回）
运行时仍可能因为鉴权、异常分支、序列化而少字段。
"""
import json
import ssl
import sys
import urllib.error
import urllib.request

BASE = 'https://127.0.0.1:8443'

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


def call(path, payload=None, token=None, method=None, timeout=90):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    if token:
        # ⚠️ token 走 **X-Token 请求头**，不是 Cookie。
        # 我第一版按常规 web 习惯发 Cookie，8 个接口全部 401，
        # 一度以为「部署后鉴权坏了」。看 drouter-web.py 的 auth() 就知道了：
        # `tok = self.h.headers.get('X-Token') or ''`。
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


def keys_of(d, path=''):
    """递归收集响应体的所有键路径（前端读字段时用的是这套名字）。"""
    out = set()
    if isinstance(d, dict):
        for k, v in d.items():
            p = '%s.%s' % (path, k) if path else str(k)
            out.add(p)
            out |= keys_of(v, p)
    elif isinstance(d, list):
        for x in d[:3]:
            out |= keys_of(x, path + '[]')
    return out


def main():
    print('=' * 68)
    print('v1.0.7 真机验收（第二段）：HTTP 接口 + 字段契约')
    print('=' * 68)

    # ---------- 0. 登录 ----------
    print('\n--- 0. 登录拿 token ---')
    st, r = call('/api/login', {'username': 'admin', 'password': 'admin123'})
    ck('登录成功', st == 200 and r.get('ok') is True,
       '→ HTTP %s %s' % (st, str(r)[:150]))
    tok = r.get('token') or r.get('data', {}).get('token')
    ck('拿到 token', bool(tok), '→ %s' % str(r)[:150])
    if not tok:
        print('\n无法继续（没有 token）')
        return 1

    # ---------- 1. 四个新页面的读接口 ----------
    # ⚠️ 三重教训，都写在这儿免得下次再踩：
    #  ① op 名照真机代码：备份/告警/配额配置都是 `conf`（不是 `config`），
    #     配额**没有 summary**（只有 status / report）。
    #  ② 字段名照**真机真实返回**，不是我「觉得合理」的命名：
    #     备份列表是 `packs` 不是 `items`，探测值是 `state.probes` 不是 `probes`，
    #     配额是 `units` / `ports` 不是 `total` / `by_ip`（原始聚合要等有流量才有）。
    #  ③ 提示字段是 `msg_cn` 不是 `message`/`msg`（前端 265 处都读 msg_cn）。
    #  这三项错了都会表现成「接口没实现」，实际上契约是对齐的。
    # 字段清单最终以 web/app.js 里bkRenderList / alRenderProbes / qtRender* 的
    # 真实读取点为准。
    cases = [
        ('备份 status（GET）', '/api/backup', 'GET', None,
         ['conf', 'packs', 'scope', 'planned', 'present', 'bytes']),
        ('备份配置', '/api/backup', 'POST', {'op': 'conf'},
         ['conf', 'timer_applied']),
        ('备份列表', '/api/backup', 'POST', {'op': 'list'},
         ['packs', 'dir']),
        ('告警 status（GET）', '/api/alert', 'GET', None,
         ['conf', 'state', 'rules', 'history', 'next', 'enabled_ch',
          'channel_types']),
        ('告警配置', '/api/alert', 'POST', {'op': 'conf'},
         ['conf', 'timer_applied']),
        ('配额 status（GET）', '/api/quota', 'GET', None,
         ['conf', 'units', 'ports', 'agg_exists', 'last', 'next']),
        ('配额配置', '/api/quota', 'POST', {'op': 'conf'},
         ['conf', 'enabled', 'plan_total_gb', 'plan_price', 'alloc']),
        ('VPN status（GET）', '/api/vpn', 'GET', None, ['conf', 'live']),
    ]
    print('\n--- 1. 四个新模块的读接口 ---')
    for desc, path, method, payload, want in cases:
        st, r = call(path, payload, tok, method)
        d = r.get('data') if isinstance(r.get('data'), dict) else {}
        allk = keys_of(d if d else r)
        top = sorted(d.keys())[:14] if d else []
        missing = [w for w in want
                   if not any(k == w or k.endswith('.' + w) for k in allk)]
        ck('%s 字段齐全' % desc, st == 200 and r.get('ok') is True and
           not missing,
           '→ HTTP %s ok=%s 缺 %s；实有 %s' % (st, r.get('ok'), missing, top))
        if st != 200 or r.get('ok') is not True:
            print('       响应: %s' % str(r)[:300])

    # ---------- 2. 私钥绝不出现在任何响应里 ----------
    print('\n--- 2. VPN 响应里不得含私钥（本轮缺陷 2 的端到端判据）---')
    st, r = call('/api/vpn', None, tok, 'GET')
    body = json.dumps(r, ensure_ascii=False)
    # WireGuard 私钥是 32 字节 base64（44 字符带 =）。用形状找，不看具体值。
    import re
    keys = re.findall(r'"private_key"\s*:\s*"([^"]*)"', body)
    nonempty = [k for k in keys if k]
    ck('响应里没有非空的 private_key', not nonempty,
       '→ 找到 %d 个非空私钥' % len(nonempty))
    # ⚠️ has_key 只在**有 peer 时**才出现（当前机器一个 peer 都没配）。
    # 所以不能无条件断言它存在 —— 那会把「没配客户端」误判成「脱敏失效」。
    # 有 peer 才检，且顺带确认 peer 里带has_key 标志。
    peers = ((r.get('data') or {}).get('conf') or {}).get('peers') or []
    if peers:
        ck('peer 用 has_key 标志代替私钥本身',
           all(('private_key' not in p) or not p.get('private_key')
               for p in peers) and any('has_key' in p for p in peers),
           '→ %d 个 peer' % len(peers))
    else:
        print('[SKIP] peer 用 has_key 标志代替私钥本身'
              '（本机还没配任何客户端，无 peer 可检）')

    # ---------- 3. 写操作的方法语义 ----------
    print('\n--- 3. 危险写操作必须 POST + 确认门 ---')
    # ⚠️ 拿一个不存在的包去试还原，只能验到「包不存在」——
    # 那样 dry_run 分支根本没走到，断言毫无意义（我第一版就这么写的，
    # 结果把「备份包不存在」误当成「没强制 dry_run」）。
    # 正解：先真导出一个包（create），拿到真包名再试还原。
    # 这条create 同时也是本轮最严重缺陷的端到端验证：
    # 修好之前导出的包里没有 dcfg.json / kern.conf，界面却显示「已导出」。
    st, r = call('/api/backup', {'op': 'create'}, tok, 'POST', timeout=600)
    body = json.dumps(r, ensure_ascii=False)
    print('  导出结果: HTTP %s ok=%s %s' % (st, r.get('ok'),
                                       str(r.get('msg_cn') or '')[:70]))
    d = r.get('data') or {}
    name = d.get('name') or ''
    ck('备份导出成功', st == 200 and r.get('ok') is True and bool(name),
       '→ HTTP %s %s' % (st, body[:220]))
    # d['files'] 是**文件数**（整数），不是文件列表 —— 我第一版当列表用直接崩了。
    # 文件清单在 list 接口的 packs[] 里，每项自带 files 明细。
    print('  导出返回: name=%s files=%s bytes=%s'
          % (name, d.get('files'), d.get('bytes')))

    # 端到端验「本轮最严重缺陷」：导出的包里必须真的有 dcfg.json / kern.conf。
    # 修好之前包里没有，界面却显示「已导出」—— 只有看包内清单才发现。
    st2, rl = call('/api/backup', {'op': 'list'}, tok, 'POST')
    packs = (rl.get('data') or {}).get('packs') or []
    ck('导出后能在列表里看到它', any(p.get('name') == name for p in packs),
       '→ 列表 %d 个' % len(packs))
    mine = [p for p in packs if p.get('name') == name]
    # ⚠️ 包内清单位于 **inspect 接口**，不在 list 的每个 pack 上。
    # list 的 pack 只有 name/size/mtime/note 四个字段（那是给列表页渲染用的），
    # 所以在 pack 上找 files/entries 永远是空 —— 我第一版就这么写的，
    # 得出「包里没有 dcfg.json」的假结论，而那恰恰是本轮修掉的缺陷。
    # 教训和op 名那次一样：**字段要去实现里查，别照「觉得合理」猜**。
    st3, ri = call('/api/backup', {'op': 'inspect', 'name': name}, tok,
                   'POST', timeout=300)
    entries = (ri.get('data') or {}).get('files') or []
    if isinstance(entries, dict):
        entries = list(entries.values())
    paths = []
    for x in entries:
        if isinstance(x, dict):
            paths.append(str(x.get('path') or ''))
        else:
            paths.append(str(x))
    print('  包内清单 %d 项，样例: %s' % (len(paths), paths[:5]))
    ck('导出包含 dcfg.json（本轮缺陷 1 的端到端判据）',
       any('dcfg.json' in x for x in paths),
       '→ %s' % paths[:14])
    ck('导出包含 kern.conf', any('kern.conf' in x for x in paths),
       '→ %s' % paths[:14])
    ck('敏感内容不进包（CA 私钥 / 宽带密码 / WireGuard 私钥）',
       not any(('/ca/' in x or '/wireguard' in x
                or x.endswith('/chap-secrets')
                or x.endswith('/pap-secrets')) for x in paths),
       '→ %s' % [x for x in paths
                 if ('/ca/' in x or '/wireguard' in x
                     or x.endswith('/chap-secrets')
                     or x.endswith('/pap-secrets'))][:5])
    # ⚠️ 这里**不能**判'/etc/ppp/peers/*' 敏感：peers 文件只有 user 与
    # 拨号参数，密码由 render.py 单独写进 chap-secrets/pap-secrets
    # （并配 hide-password）。我第一版用 'ppp' in x.lower() 一刀切，
    # 把peers 也当成敏感 → 报了一个不存在的漏。
    # 但**必须**钉住 peers 里真的没有 password 行，否则这条判据
    # 就成了「假定安全」而不是「验证安全」——
    # 真机上的 peers 文件我们查过只有 user，没法在这里读内容，
    # 所以改成断言 render.py 的渲染结果里不出现 password 行。
    _rp = open('/opt/drouter/backend/render.py', encoding='utf-8').read()
    _i = _rp.find("'/etc/ppp/peers/drouter-wan'")
    _seg = _rp[max(0, _i - 2500):_i]
    ck('peers 文件由渲染器生成且不含 password 行（密码只在 chap-secrets）',
       ('hide-password' in _seg
        and 'password' not in _seg.lower().split('hide-password')[0][-200:]),
       '→ 渲染器可能往 peers 里写了 password 行')
    # 有 excluded 时提示里必须说清楚（否则用户以为「全都备份了」）
    if d.get('excluded'):
        print('  被排除的敏感项 %d 个: %s'
              % (len(d['excluded']),
                 [x.get('path') for x in d['excluded']][:5]))
        ck('有敏感项被排除时，提示里说清楚了',
           '敏感' in str(r.get('msg_cn') or ''),
           '→ msg_cn=%r' % str(r.get('msg_cn') or '')[:120])

    if name:
        st, r = call('/api/backup', {'op': 'restore', 'name': name}, tok,
                     'POST', timeout=600)
        dd = r.get('data') or {}
        dry = dd.get('dry_run')
        ck('还原缺 confirm 时强制 dry_run（不写盘）',
           st == 200 and r.get('ok') is True and dry is True,
           '→ HTTP %s ok=%s dry_run=%s msg=%r'
           % (st, r.get('ok'), dry, str(r.get('msg_cn') or '')[:70]))
        ck('dry_run 返回计划但没有 applied（确实没写盘）',
           not dd.get('applied'),
           '→ data 键: %s' % sorted(dd.keys())[:12])
        print('  dry_run data 键: %s' % sorted(dd.keys())[:14])
    else:
        ck('还原缺 confirm 时强制 dry_run（不写盘）', False, '→ 没能导出备份包')
    # 路径穿越在 HTTP 层必须被挡
    st, r = call('/api/backup', {'op': 'restore', 'name': '../../etc/shadow',
                                 'confirm': True}, tok, 'POST')
    ck('路径穿越在 HTTP 层被挡', st != 200 or r.get('ok') is not True,
       '→ HTTP %s %s' % (st, json.dumps(r, ensure_ascii=False)[:200]))

    # ---------- 4. 写操作不走 GET ----------
    print('\n--- 4. 写操作不得走 GET ---')
    # GET 路径下 Web 层直接走 status 分支，body 里的 op 根本不被读，
    # 所以带载荷的 GET 不可能触发写操作。判据：响应里不出现「已执行」类文案。
    for desc, path, payload, marks in (
            ('备份导出', '/api/backup', {'op': 'create'}, ('已导出', '已创建')),
            ('告警测试', '/api/alert', {'op': 'test'}, ('已发送', '测试邮件')),
            ('配额清零', '/api/quota', {'op': 'reset'}, ('已清空',)),
    ):
        st, r = call(path, payload, tok, 'GET')
        body = json.dumps(r, ensure_ascii=False)
        executed = [m for m in marks if m in body]
        ck('%s 用 GET 不会触发写操作' % desc, not executed,
           '→ HTTP %s 出现执行痕迹 %s' % (st, executed))

    # ---------- 5. 中文提示 ----------
    print('\n--- 5. 错误提示必须是中文且能定位 ---')
    # ⚠️ 提示字段是 `msg_cn`（前端 265 处都读它），不是 message/msg。
    # 我第一版读 message/msg，拿到空串还以为「没给提示」。
    # 只用**不会改状态**的查询类动作。
    # 原本想用 quota reset 触发错误提示，但那是真的会清空聚合数据 ——
    # 拿破坏性操作当测试用例，出错就把用户数据清了。绝不能这么写。
    for desc, path, payload in (
            ('包不存在', '/api/backup', {'op': 'inspect', 'name': 'nope.tar.gz'}),
            ('告警未知 op', '/api/alert', {'op': 'no-such-op'}),
            ('配额未知 op', '/api/quota', {'op': 'no-such-op'}),
    ):
        st, r = call(path, payload, tok, 'POST')
        msg = str(r.get('msg_cn') or r.get('message') or '')
        has_cn = any('\u4e00' <= ch <= '\u9fff' for ch in msg)
        ck('%s 时给中文提示' % desc, bool(msg) and has_cn,
           '→ HTTP %s msg_cn=%r' % (st, msg[:120]))

    print('\n' + '=' * 68)
    print('通过 %d 项，失败 %d 项' % (oks, len(fails)))
    for f in fails:
        print('  ✘ %s' % f)
    print('=' * 68)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
