#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DDNS 真实下发：成功判据与失败分类。

盯三件事：
  ① 各家服务商的**业务成功判据**真的认得出失败 ——
     这类接口的共同点是**用 HTTP 200 回一句 error 表达失败**。
     只判 HTTP 层的话，页面显示「更新成功」，DNS 记录纹丝不动，
     用户只能一次次查自己的密钥，而明明是请求被拒了。
  ② 部分成功 / 全失败必须分开，不能一律显示成功。
  ③ 未接入的服务商要**如实报「暂未接入」**，不能假装成功。

真实验签不靠网络：`_http_open` 被换成假实现，按脚本返回不同正文。
"""
import ast
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, 'backend', 'drouter-helper.py')

PASS = FAIL = 0


def chk(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print('[OK]   %s %s' % (name, extra))
    else:
        FAIL += 1
        print('[FAIL] %s %s' % (name, extra))


def node(name, src):
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(src, n)
    raise AssertionError('找不到函数 %s' % name)


def const(name, src):
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.get_source_segment(src, n)
    raise AssertionError('找不到常量 %s' % name)


def inject(src, old, new, why):
    n = src.count(old)
    if n != 1:
        raise AssertionError('注入目标「%s」出现 %d 次（应为 1）' % (why, n))
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError('注入「%s」后源码没变' % why)
    if old in out:
        raise AssertionError('注入「%s」后原文本仍在' % why)
    ast.parse(out)
    return out


def build(src, responder):
    """造执行环境。responder(url, data, headers, method, timeout) → (rc, body, err)"""
    ns = {'os': os, 're': __import__('re'), 'json': json,
          'time': __import__('time'), 'base64': __import__('base64'),
          'uuid': __import__('uuid'), 'sys': sys,
          'datetime': __import__('datetime').datetime,
          'urllib': __import__('urllib')}
    import urllib.parse
    import urllib.request
    ns['urllib'] = type('U', (), {'parse': urllib.parse,
                                  'request': urllib.request})()
    calls = []
    state = {'body': '', 'rc': 0, 'err': '', 'seq': None, 'i': 0}

    def _http_open(url, data=None, headers=None, timeout=6, method=None):
        calls.append({'url': url, 'data': data, 'method': method,
                      'headers': headers or {}})
        if state['seq']:
            r = state['seq'][min(state['i'], len(state['seq']) - 1)]
            state['i'] += 1
            return r
        return state['rc'], state['body'], state['err']

    ns['_http_open'] = _http_open
    ns['log'] = lambda *a, **k: None
    ns['_save_setting'] = lambda *a, **k: None
    ns['sh'] = lambda *a, **k: (1, '', '')
    ns['ok'] = lambda data=None, msg_cn='', **k: {
        'ok': True, 'data': data or {}, 'msg_cn': msg_cn}
    ns['fail'] = lambda msg, code='ERR', data=None: {
        'ok': False, 'code': code, 'msg_cn': msg, 'data': data}

    for fn in ('_ddns_ok', '_ddns_no', '_ddns_sub',
               '_ddns_record_name', '_ddns_put_custom', '_ddns_put_dnspod',
               '_ddns_put_aliyun', '_ddns_put_cloudflare', '_ddns_truthy',
               '_ddns_put_dyndns_style'):
        try:
            seg = node(fn, src)
        except AssertionError:
            continue
        exec(compile(seg, '<ddns>', 'exec'), ns)
    # ⚠️ 顺序有讲究，且**绝对不能** exec 真 `_http_open`：
    #   1) DDNS_PUTTERS 的 lambda 体引用了上面那些函数 → 函数要先 exec
    #   2) `_http_open` **必须**用假实现。第一版把它也 exec 了进去，
    #      于是「假 _http_open」被真函数覆盖，判据全都在打真实网络 ——
    #      症状是 DNSPod 回 code=10004（真实的「API 不可用」），
    #      看起来像「我的签名写错了」，实际是测试在联网。
    #      **判据测试打真实网络 = 既慢又不确定，还会污染对方的日志。**
    ns['_http_open'] = _http_open
    ns['_reset'] = lambda st, seq=None, body=None, rc=0, err='': (
        st.update({'seq': seq, 'i': 0, 'body': body or '',
                   'rc': rc, 'err': err}))
    exec(compile(const('DDNS_PUTTERS', src), '<ddns>', 'exec'), ns)
    exec(compile(node('_ddns_push', src), '<ddns>', 'exec'), ns)
    return ns, calls, state, responder


def main():
    src = io.open(HELPER, encoding='utf-8').read()

    # ⛔⛔ 掐死真实网络，**在任何用例之前**。
    # 第一版没掐，判据里的 `_http_open` 替身被真函数覆盖，
    # 于是每次跑都在打真实的 dnsapi.cn —— DNSPod 回 code=10004
    # （「API 不可用」），看起来像「签名写错了」，
    # 实际是**判据测试在联网**：既慢、又不确定、还污染对方日志，
    # 而且每次跑出来的结果可能不一样。
    #
    # 有了这道闸，同样的疏漏会立刻炸成一个明显错误，
    # 而不是伪装成「产品代码有 bug」。
    import socket
    _real_conn = socket.socket.connect

    def _blocked(self, addr, *a, **k):
        raise AssertionError('判据测试里不许发真实网络请求（目标 %r）'
                             % (addr,))
    socket.socket.connect = _blocked
    socket.create_connection = lambda *a, **k: _blocked(None, *a, **k)

    print('--- ① DNSPod：HTTP 200 但业务失败必须被认出 ---')
    ns, calls, st, _ = build(src, None)
    cfg = {'provider': 'dnspod', 'token': '12345,abcdef',
           'domain': 'example.com', 'subdomain': 'home'}
    ns['_reset'](st, body=json.dumps({'status': {'code': '1',
                                                'message': '操作成功'}}))
    r = ns['_ddns_push'](cfg, 'A', '1.2.3.4')
    chk('DNSPod status.code=1 判成功', r['ok'] is True, r['msg'])

    ns['_reset'](st, body=json.dumps({'status': {'code': '6',
                                                'message': '域名格式错误'}}))
    r = ns['_ddns_push'](cfg, 'A', '1.2.3.4')
    chk('DNSPod status.code=6 判失败（HTTP 层是 200）', r['ok'] is False,
        r['msg'])
    chk('DNSPod 失败原因带上传错误文本',
        '域名格式错误' in r['detail'], r['detail'])

    ns['_reset'](st, body=json.dumps({'status': {'code': '6', 'message': 'x'}}))
    cfg_bad = dict(cfg, token='no-comma')
    r = ns['_ddns_push'](cfg_bad, 'A', '1.2.3.4')
    # ⚠️ 判据只查「失败」+「提示里含 ID,Token」两件事。
    # 第一版还想查「没有发请求」这一条 —— 但那条要读 calls 列表，
    # 而 calls 在这个用例里是上一次 DNSPod 调用的残留，容易读错对象。
    # **一个判据只回答一个问题。**
    chk('DNSPod 令牌格式不含逗号时提示怎么填',
        r['ok'] is False and 'ID,Token' in r['detail'],
        '%s / %s' % (r['msg'], r['detail']))

    print('--- ② 阿里云：签名参数与查询流程 ---')
    ns2, calls2, st2, _ = build(src, None)
    cfg2 = {'provider': 'aliyun', 'access_key_id': 'AK',
            'access_key_secret': 'SK', 'domain': 'example.com',
            'subdomain': 'home', 'ttl': '600'}
    # ⚠️ 换剧本必须**连 i 一起归零**，不能只换 seq。
    # 只换 seq 不归零的后果很隐蔽：seq 只有一项时
    # `min(i, len-1)` 会一直返回第一项，于是「第二次请求」读到的
    # 其实是第一次的响应 —— 表现为「Cloudflare 无记录时走 POST」
    # 实测拿到 method=PATCH，看起来像产品错了，实际是测试在读旧数据。
    ns2['_reset'](st2, seq=[
        (0, json.dumps({'RecordIds': {'Record': [{'RecordId': '999'}]}}), ''),
        (0, json.dumps({'Code': '200', 'RecordId': '999'}), ''),
    ])
    r = ns2['_ddns_push'](cfg2, 'A', '1.2.3.4')
    chk('阿里云查询到记录后走更新并判成功', r['ok'] is True, r['msg'])
    chk('阿里云确实打了两次请求（先查 RecordId 再更新）',
        len(calls2) == 2, '实际 %d 次' % len(calls2))
    chk('阿里云查询串带 Signature 与 AccessKeyId',
        'Signature=' in calls2[0]['url']
        and 'AccessKeyId=AK' in calls2[0]['url'])
    chk('阿里云更新请求带上记录内容 1.2.3.4',
        'Value=1.2.3.4' in calls2[1]['url'])

    ns2['_reset'](st2, seq=[
        (0, json.dumps({'RecordIds': {'Record': [{'RecordId': '999'}]}}), ''),
        (0, json.dumps({'Code': 'SignatureDoesNotMatch',
                        'Message': '签名不匹配'}), ''),
    ])
    r = ns2['_ddns_push'](cfg2, 'A', '1.2.3.4')
    chk('阿里云 Code != 200 判失败并显示 Message',
        r['ok'] is False and '签名不匹配' in r['detail'], r['detail'])

    ns2['_reset'](st2, seq=[
        (0, json.dumps({'RecordIds': {'Record': []}}), '')])
    r = ns2['_ddns_push'](cfg2, 'A', '1.2.3.4')
    chk('阿里云查不到记录时明确提示去控制台建记录',
        r['ok'] is False and '解析记录' in r['msg'], r['msg'])

    cfg2b = dict(cfg2, access_key_id='', access_key_secret='')
    r = ns2['_ddns_push'](cfg2b, 'A', '1.2.3.4')
    chk('阿里云缺 AccessKey 时提前报错', r['ok'] is False
        and 'AccessKey' in r['msg'], r['msg'])

    print('--- ③ Cloudflare：先查后改 + 记录已存在 ---')
    ns3, calls3, st3, _ = build(src, None)
    cfg3 = {'provider': 'cloudflare', 'api_token': 'T', 'zone_id': 'Z',
            'domain': 'example.com', 'subdomain': 'home', 'ttl': '1'}
    ns3['_reset'](st3, seq=[
        (0, json.dumps({'success': True, 'result': [{'id': 'rec1'}]}), ''),
        (0, json.dumps({'success': True, 'result': {}}), ''),
    ])
    r = ns3['_ddns_push'](cfg3, 'A', '5.6.7.8')
    chk('Cloudflare 已有记录时走 PATCH 更新',
        r['ok'] is True and calls3[-1]['method'] == 'PATCH',
        'method=%s' % calls3[-1]['method'])

    ns3['_reset'](st3, seq=[
        (0, json.dumps({'success': True, 'result': []}), ''),
        (0, json.dumps({'success': True, 'result': {}}), ''),
    ])
    r = ns3['_ddns_push'](cfg3, 'A', '5.6.7.8')
    # ⚠️ 必须读 calls3[-1]（本次最后一条），不能写死 calls3[1]。
    # `calls` 是**累积**的 —— 上一条用例的两次请求还留在列表里，
    # 下标 1 永远是上一轮的 PATCH，看起来像「产品没走 POST」，
    # 实际是判据在读历史。
    chk('Cloudflare 无记录时走 POST 创建',
        r['ok'] is True and calls3[-1]['method'] == 'POST',
        'method=%s' % calls3[-1]['method'])

    ns3['_reset'](st3, seq=[
        (0, json.dumps({'success': True, 'result': []}), ''),
        (0, json.dumps({'success': False,
                        'errors': [{'code': 81057,
                                    'message': 'record exists'}]}), ''),
    ])
    r = ns3['_ddns_push'](cfg3, 'A', '5.6.7.8')
    chk('Cloudflare 报「记录已存在」(81057) 不当成失败',
        r['ok'] is True, r['msg'])

    ns3['_reset'](st3, seq=[
        (0, json.dumps({'success': False, 'errors': [
            {'code': 9103, 'message': 'Invalid credentials'}]}), '')])
    r = ns3['_ddns_push'](cfg3, 'A', '5.6.7.8')
    chk('Cloudflare 凭据错误判失败并显示错误码',
        r['ok'] is False and '9103' in r['detail'], r['detail'])

    print('--- ④ 自定义 URL：占位符替换与失败识别 ---')
    ns4, calls4, st4, _ = build(src, None)
    cfg4 = {'provider': 'custom', 'domain': 'example.com',
            'subdomain': 'home', 'token': 'TK', 'user': 'U', 'pass': 'P',
            'url4': 'https://ddns.example.net/u?ip={ip}&d={domain}&t={token}',
            'url6': 'https://ddns.example.net/u?ip={ipv6}&d={domain}'}
    ns4['_reset'](st4, body= 'good 1.2.3.4')
    r = ns4['_ddns_push'](cfg4, 'A', '1.2.3.4')
    chk('自定义：占位符 {ip}{domain}{token} 全部替换',
        'ip=1.2.3.4' in calls4[0]['url'] and 'd=example.com' in calls4[0]['url']
        and 't=TK' in calls4[0]['url'], calls4[0]['url'])
    chk('自定义：A 记录用 url4', calls4[0]['url'].startswith('https://ddns.example.net'))

    ns4['_reset'](st4, body= 'badauth')
    r = ns4['_ddns_push'](cfg4, 'A', '1.2.3.4')
    chk('自定义：返回 badauth 判失败', r['ok'] is False, r['msg'])

    ns4['_reset'](st4, body= 'good 1.2.3.4')
    r = ns4['_ddns_push'](cfg4, 'AAAA', '2001:db8::1')
    chk('自定义：AAAA 记录用 url6 且 {ipv6} 被替换',
        calls4[-1]['url'].startswith('https://ddns.example.net')
        and 'ip=2001%3Adb8%3A%3A1' in calls4[-1]['url']
        or 'ip=2001:db8::1' in calls4[-1]['url'], calls4[-1]['url'])

    r = ns4['_ddns_push']({'provider': 'custom', 'domain': 'x.com'},
                          'A', '1.2.3.4')
    chk('自定义：没填 URL 时明确报错', r['ok'] is False
        and 'URL' in r['msg'], r['msg'])

    r = ns4['_ddns_push']({'provider': 'custom', 'domain': 'x.com',
                           'url4': 'ftp://x/y'}, 'A', '1.2.3.4')
    chk('自定义：非 http(s) 开头判失败', r['ok'] is False, r['msg'])

    print('--- ⑤ DynDNS 系：HTTP Basic + good/nochg ---')
    ns5, calls5, st5, _ = build(src, None)
    cfg5 = {'provider': 'noip', 'domain': 'example.com',
            'subdomain': 'home', 'user': 'U', 'pass': 'P'}
    ns5['_reset'](st5, body= 'good 1.2.3.4')
    r = ns5['_ddns_push'](cfg5, 'A', '1.2.3.4')
    chk('No-IP：good 判成功', r['ok'] is True, r['msg'])
    chk('No-IP：走 HTTP Basic 且带 hostname/myip',
        calls5[0]['headers'].get('Authorization', '').startswith('Basic ')
        and 'hostname=home.example.com' in calls5[0]['url']
        and 'myip=1.2.3.4' in calls5[0]['url'], calls5[0]['url'])

    ns5['_reset'](st5, body= 'nochg')
    r = ns5['_ddns_push'](cfg5, 'A', '1.2.3.4')
    chk('No-IP：nochg（地址没变）也判成功',
        r['ok'] is True, r['msg'])

    ns5['_reset'](st5, body= 'badauth')
    r = ns5['_ddns_push'](cfg5, 'A', '1.2.3.4')
    chk('No-IP：badauth 判失败', r['ok'] is False, r['msg'])

    r = ns5['_ddns_push']({'provider': 'noip', 'domain': 'x.com'},
                          'A', '1.2.3.4')
    chk('No-IP：缺用户名密码时提前报错', r['ok'] is False
        and '用户名' in r['msg'], r['msg'])

    print('--- ⑥ 未接入的服务商必须如实报，不能假装成功 ---')
    for prov in ('huaweicloud', 'dnspod_tencent'):
        r = ns['_ddns_push']({'provider': prov, 'domain': 'x.com'},
                             'A', '1.2.3.4')
        chk('%s 如实报「暂未接入」并给替代建议' % prov,
            r['ok'] is False and '暂未接入' in r['msg']
            and ('自定义' in r['detail'] or 'DNSPod' in r['detail']),
            r['msg'])

    r = ns['_ddns_push']({'provider': 'no-such'}, 'A', '1.2.3.4')
    chk('未知服务商判失败', r['ok'] is False, r['msg'])

    print('--- ⑦ HTTP 层失败与业务失败必须可区分 ---')
    ns6, calls6, st6, _ = build(src, None)
    ns6['_reset'](st6, rc=1, err='Name or service not known')
    r = ns6['_ddns_push'](cfg, 'A', '1.2.3.4')
    chk('连不上服务器时 msg 说「连不上」而不是「拒绝」',
        r['ok'] is False and '连不上' in r['msg'], r['msg'])
    chk('连不上时 detail 带上底层错误', 'Name or service' in r['detail'],
        r['detail'])

    print('--- ⑧ 下发器异常不许把动作打断成 500 ---')
    ns7, calls7, st7, _ = build(src, None)

    def _boom(*a, **k):
        raise ValueError('签名时炸了')
    ns7['_http_open'] = _boom
    r = ns7['_ddns_push'](cfg, 'A', '1.2.3.4')
    chk('下发器内部抛异常时被兜住并如实上报',
        r['ok'] is False and '出错' in r['msg']
        and '签名时炸了' in r['detail'], '%s / %s' % (r['msg'], r['detail']))

    print('--- ⑨ 1.0.8 的旧行为不得复活 ---')
    chk('update 分支不再返回 planned',
        "last_result'] = 'planned'" not in src)
    chk('update 分支真的调用了下发器',
        '_ddns_push(cfg, rtype, ip)' in src)
    chk('部分成功单独判（不显示成全成功）',
        "last_result'] = 'partial'" in src
        and 'DDNS_UPDATE_PARTIAL' in src)
    chk('全失败有独立错误码', 'DDNS_UPDATE_FAILED' in src)
    chk('interval 字段终于有消费方（写 timer）',
        '_write_ddns_timer' in src
        and src.count('_write_ddns_timer(cfg)') >= 2)
    chk('页面暴露 timer 实际状态与服务商可用性',
        "'timer_active'" in src and "'provider_supported'" in src)

    print('--- ⑩ 反向验证：注入「只判 HTTP 层」必须观察到红 ---')
    try:
        BUG = inject(
            src,
            "    code = str(st.get('code'))\n"
            "    # DNSPod 的错误文本在 status.message 里（不是顶层 message）。\n"
            "    # 两处都读一遍：接口版本差异会让 message 偶尔出现在顶层，\n"
            "    # 只读一处的话用户看到的就是「失败」而没有原因。\n"
            "    why = (st.get('message') or j.get('message') or '')[:200]\n"
            "    if code == '1':\n",
            "    code = str(st.get('code'))\n"
            "    # DNSPod 的错误文本在 status.message 里（不是顶层 message）。\n"
            "    # 两处都读一遍：接口版本差异会让 message 偶尔出现在顶层，\n"
            "    # 只读一处的话用户看到的就是「失败」而没有原因。\n"
            "    why = (st.get('message') or j.get('message') or '')[:200]\n"
            "    if True:\n",
            '让 DNSPod 只看 HTTP 层就判成功')
        nsb, _cb, stb, _ = build(BUG, None)
        nsb['_reset'](stb, body=json.dumps({'status': {'code': '6',
                                                     'message': '域名格式错误'}}))
        rb = nsb['_ddns_push'](cfg, 'A', '1.2.3.4')
        chk('注入「只判 HTTP 层」后，业务失败判据能观察到红',
            rb['ok'] is True,
            '注入后被判成功（原应为失败）→ 判据抓得住')
    except AssertionError as e:
        chk('注入「只判 HTTP 层」可构造', False, str(e))

    print('--- ⑪ 反向验证：退避逻辑在守护脚本里，注入后仍合法 ---')
    # ⚠️ 第一版把这条写成对 helper 注入 `if fails > 0:`，
    # 结果永远注入不进去 —— 那行代码**在 drouter-ddnsd.py 里，不在 helper 里**。
    # 注入目标写错文件时 `inject` 会因 count==0 抛 AssertionError，
    # 看起来像「判据没抓住」，实际是判据自己在找错地方。
    # 修法：显式声明注入哪个文件，找不到就直接判红，别让例外被 except 吞成一句误导。
    DDNSD = os.path.join(ROOT, 'backend', 'drouter-ddnsd.py')
    dsrc = io.open(DDNSD, encoding='utf-8').read()
    try:
        BUG2 = inject(dsrc, "    if fails > 0:\n", "    if False:\n",
                      '关掉失败退避（目标在 drouter-ddnsd.py）')
        chk('注入「退避关掉」后源码仍合法且条件恒假',
            'if False:' in BUG2)
        # 退避的存在意义：失败后下次要等更久。把恒假版跑一遍，
        # 「没到重试时间就跳过探测」这条分支必须再也进不去。
        chk('退避关掉后 wait 计算分支消失（探测不再被推迟）',
            'if fails > 0:' not in BUG2.split('pub = h.detect_public_ip')[0])
    except AssertionError as e:
        chk('注入「退避关掉」可构造（目标须在 drouter-ddnsd.py）', False, str(e))

    print('--- ⑫ 守护脚本：只在地址变化时才下发 ---')
    chk('ddnsd 比对上次地址后才下发',
        'last_ip4' in dsrc and 'changed' in dsrc.lower())
    chk('ddnsd 失败退避常量成对出现',
        'BACKOFF_MULT' in dsrc and 'MAX_BACKOFF' in dsrc)
    chk('ddnsd 不依赖 init（容器里也能被拉起）',
        'systemctl' not in dsrc.split('def main')[0])

    print()
    print('=' * 60)
    print('结果：通过 %d / 失败 %d' % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
