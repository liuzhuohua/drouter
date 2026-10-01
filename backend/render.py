#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drouter - 配置渲染器
职责：把 Web 层保存在 SQLite 中的结构化配置，渲染成各原生服务的配置文件文本。
本模块为纯逻辑（不碰系统、不需要 root），可独立单元测试。
"""
import re
import ipaddress

# ---------------------------------------------------------------- 校验工具

IPV4_RE = re.compile(r'^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$')
IFNAME_RE = re.compile(r'^[a-zA-Z0-9._:-]{1,15}$')
MAC_RE = re.compile(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')
DOMAIN_RE = re.compile(r'^(?!-)[a-zA-Z0-9-_.]{1,253}$')


class ValidateError(Exception):
    """输入校验失败，附带简体中文提示"""
    def __init__(self, msg_cn, field=None):
        super().__init__(msg_cn)
        self.msg_cn = msg_cn
        self.field = field


def v_ipv4(v, field='值', allow_empty=False):
    v = (v or '').strip()
    if not v:
        if allow_empty:
            return ''
        raise ValidateError(f'{field}不能为空，请填写合法的 IPv4 地址', field)
    if not IPV4_RE.match(v):
        raise ValidateError(f'{field}不是合法的 IPv4 地址：{v}', field)
    return v


def v_ipv6(v, field='值', allow_empty=False, global_only=True):
    """校验 IPv6 地址。默认要求为全球单播（拒绝链路本地 / ULA / 组播）。"""
    v = (v or '').strip().split('%')[0]
    if not v:
        if allow_empty:
            return ''
        raise ValidateError(f'{field}不能为空，请填写合法的 IPv6 地址', field)
    try:
        a = ipaddress.IPv6Address(v)
    except Exception:
        raise ValidateError(f'{field}不是合法的 IPv6 地址：{v}', field)
    if global_only and not a.is_global:
        raise ValidateError(
            f'{field}必须是公网可路由的 IPv6 地址（不能是 fe80::/fc00::/ff00:: 等）：{v}', field)
    return str(a)


def v_ipv4_list(v, field='值', allow_empty=True):
    """IPv4 列表，用于 DNS 服务器。兼容两种入参：list 或逗号分隔字符串。"""
    if isinstance(v, (list, tuple)):
        parts = [str(x).strip() for x in v if str(x).strip()]
    else:
        v = (v or '').strip()
        parts = [x.strip() for x in v.split(',') if x.strip()] if v else []
    if not parts:
        if allow_empty:
            return []
        raise ValidateError(f'{field}不能为空', field)
    out = []
    for item in parts:
        if not IPV4_RE.match(item):
            raise ValidateError(f'{field}包含非法 IPv4 地址：{item}', field)
        out.append(item)
    if len(out) > 8:
        raise ValidateError(f'{field}最多允许 8 个地址', field)
    return out


def v_cidr(v, field='值', allow_empty=False):
    v = (v or '').strip()
    if not v:
        if allow_empty:
            return ''
        raise ValidateError(f'{field}不能为空', field)
    try:
        net = ipaddress.IPv4Network(v, strict=False)
    except Exception:
        raise ValidateError(f'{field}不是合法的网段（示例 192.168.7.1/24）：{v}', field)
    return f'{net.network_address}/{net.prefixlen}'


def v_int(v, field='值', lo=0, hi=65535, default=None, allow_empty=False):
    s = str(v if v is not None else '').strip()
    if s == '':
        if allow_empty:
            return None
        if default is not None:
            return default
        raise ValidateError(f'{field}不能为空', field)
    # 必须先看 ascii 再看 digit：'²'（上标二）、'٣'（阿拉伯数字）这类
    # Unicode 字符 str.isdigit() 也是 True，但 int('²') 会直接抛 ValueError ——
    # 那就不走 ValidateError，而是变成一个 500 + traceback，页面只剩「请求失败」。
    if not (s.isascii() and s.isdigit()):
        raise ValidateError(f'{field}必须是数字：{s}', field)
    try:
        n = int(s)
    except Exception:
        raise ValidateError(f'{field}必须是数字：{s}', field)
    if n < lo or n > hi:
        raise ValidateError(f'{field}必须在 {lo} ~ {hi} 之间', field)
    return n


def v_ifname(v, field='网卡名', allow_empty=False):
    v = (v or '').strip()
    if not v:
        if allow_empty:
            return ''
        raise ValidateError(f'{field}不能为空', field)
    if not IFNAME_RE.match(v):
        raise ValidateError(f'{field}包含非法字符：{v}', field)
    return v


def v_mac(v, field='MAC 地址'):
    v = (v or '').strip()
    if not MAC_RE.match(v):
        raise ValidateError(f'{field}格式不正确（示例 bc:24:11:1a:8f:43）：{v}', field)
    return v.lower()


def v_bool(v):
    return bool(v) and str(v).lower() not in ('false', '0', 'no', 'off', '')


def v_choice(v, choices, field='值'):
    v = (v or '').strip()
    if v not in choices:
        raise ValidateError(f'{field}只能是：{", ".join(choices)}', field)
    return v


CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')


def v_text(v, field='值', maxlen=128, allow_empty=False, pattern=None):
    """渲染进配置文件的自由文本：必须过滤换行与控制字符。

    这类字段看着最无害，风险却最大 —— 配置都是一行一条指令，用户填的值里
    只要有一个 \\n，后面那一截就会变成新的配置行（往 radvd.conf 里加一个
    prefix、往 peers 文件里加一个 noauth、往 miniupnpd.conf 里改哪条链，
    都是灾难），而界面只会回一句英文 syntax error，指不到是哪个输入框。
    所以凡是「拿用户原话直接拼进配置行」的地方，都必须先过这里。
    """
    v = (v or '').strip()
    if not v:
        if allow_empty:
            return ''
        raise ValidateError(f'{field}不能为空', field)
    if CTRL_RE.search(v):
        raise ValidateError(f'{field}不能包含换行或不可见控制字符', field)
    if len(v) > maxlen:
        raise ValidateError(f'{field}过长（最多 {maxlen} 个字符）', field)
    if pattern is not None:
        m = pattern.match(v) if hasattr(pattern, 'match') else re.match(pattern, v)
        if not m:
            raise ValidateError(f'{field}含不支持的字符：{v}', field)
    return v


def v_ipv6_suffix(v, field='IPv6 后缀'):
    """dhcpcd 的 ia_pd 第 5 段（SLA-ID 后缀），形如 ::1 / 1 / 0:0:0:1。

    页面上的默认值是 ::1，用户也可能只填 1 或写成 0:0:0:2。统一按
    IPv6 地址校验（不完整的写法补 :: 前缀再解析），既能挡住注入，
    也能把 SLA-ID 写错导致的 ia_pd 配置失效挡在保存之前。
    """
    v = (v or '').strip()
    if not v:
        return '::1'
    if CTRL_RE.search(v) or not re.match(r'^[0-9a-fA-F:]{1,39}$', v):
        raise ValidateError(f'{field}只能由十六进制数字与冒号组成：{v}', field)
    cand = v if ':' in v else ('::' + v)
    try:
        return str(ipaddress.IPv6Address(cand))
    except Exception:
        raise ValidateError(f'{field}不是合法的 IPv6 后缀：{v}（示例 ::1）', field)


def v_option_code(v):
    return v_int(v, field='DHCP Option Code', lo=1, hi=254)


def v_option_value(v, code):
    """按 Option Code 校验值：3/6/44 等为 IP 列表，其他允许字符串（hex 或文本）"""
    v = (v or '').strip()
    if not v:
        raise ValidateError('Option 值不能为空', 'value')
    if code in (1, 3, 4, 5, 6, 7, 16, 28, 41, 44, 45, 48, 49, 50, 51, 54, 65, 68, 69, 70, 71, 72, 73, 74, 76):
        return ','.join(v_ipv4_list(v, field=f'Option {code} 的值'))
    if len(v) > 255:
        raise ValidateError(f'Option {code} 的值过长（最多 255 字符）', 'value')
    if re.search(r'[\r\n]', v):
        raise ValidateError(f'Option {code} 的值不能包含换行', 'value')
    return v


# ---------------------------------------------------------------- dnsmasq

def render_dnsmasq(cfg):
    """cfg 结构见 web 配置：lan_iface / pool / options / dns / static_leases"""
    lan = v_ifname(cfg.get('lan_iface') or '', field='LAN 接口')
    # domain / log_file 原先是直接取用户原值拼进配置行的：同段的 pool、
    # option 都有校验，唯独这两个漏了。后果不只是「填错 dnsmasq 起不来」——
    # 值里带一个换行就能凭空造出一条新指令（比如再插一条 dhcp-range），
    # 而界面只回一句英文语法错误，看不出是哪个输入框的问题。
    domain = v_text(cfg.get('domain') or 'lan', field='局域网域名',
                    maxlen=253, pattern=DOMAIN_RE)
    log_file = (v_path(cfg.get('log_file'), field='dnsmasq 日志文件')
                if (cfg.get('log_file') or '').strip()
                else '/var/log/drouter/dnsmasq.log')
    lines = [
        '# 由 drouter 自动生成，请勿手工修改',
        '# 模块：dnsmasq（DHCPv4 + DNS 转发）',
        '',
        '# ---- 基本行为 ----',
        f'interface={lan}',
        'bind-interfaces',
        'domain-needed',
        'bogus-priv',
        'expand-hosts',
        f'domain={domain}',
        'local=/lan/',
        '',
        '# ---- DHCPv4 地址池 ----',
    ]
    enabled = v_bool(cfg.get('dhcp_enabled'))
    if enabled:
        pool_start = v_ipv4(cfg.get('pool_start'), field='地址池起始')
        pool_end = v_ipv4(cfg.get('pool_end'), field='地址池结束')
        netmask = v_ipv4(cfg.get('pool_netmask') or '255.255.255.0', field='子网掩码')
        lease = v_int(cfg.get('lease_time') or 7200, field='租期', lo=120, hi=604800)
        lines.append(f'dhcp-range={pool_start},{pool_end},{netmask},{lease}s')
        # code 3 / 6 有专属字段，但下面的「自定义 Options」表同样可以写这两个 code。
        # 两处都填时 dnsmasq 会把值**追加**（3/6 是列表型 option），于是下发成
        #   dhcp-option=3,192.168.7.3        ← 专属字段
        #   dhcp-option=3,192.168.7.3        ← 自定义表
        # 客户端因此收到重复值，DNS 列表顺序也会被搅乱。
        # 这里按 code 归并成**一行**：专属字段在前，自定义值追加在后，顺序去重。
        # （每处各自仍受 v_ipv4_list 的 8 条上限约束；合并后不再二次限制，
        #   免得把历史上能保存的配置变成保存失败。）
        merged = {
            '3': v_ipv4_list(cfg.get('option_gateway'), field='Option 3 网关'),
            '6': v_ipv4_list(cfg.get('option_dns'), field='Option 6 DNS'),
        }
        forced = []   # 带「强制」的 3/6 行没法并进普通行，保持单独输出
        others = []   # 其余 code 原样输出
        for opt in cfg.get('options') or []:
            if not v_bool(opt.get('enabled')):
                continue
            code = v_option_code(opt.get('code'))
            val = v_option_value(opt.get('value'), code)
            force = v_bool(opt.get('force'))
            key = str(code)
            if key not in merged:
                others.append((code, val, force))
            elif force:
                forced.append((code, val))
            else:
                merged[key].extend(val.split(','))
        for code in ('3', '6'):
            seen, vals = set(), []
            for item in merged[code]:
                if item not in seen:
                    seen.add(item)
                    vals.append(item)
            if vals:
                lines.append('dhcp-option=%s,%s' % (code, ','.join(vals)))
        for code, val, force in others:
            lines.append(f'dhcp-option{"-force" if force else ""}={code},{val}')
        for code, val in forced:
            lines.append(f'dhcp-option-force={code},{val}')
        # 静态绑定
        for sl in cfg.get('static_leases') or []:
            if not v_bool(sl.get('enabled')):
                continue
            mac = v_mac(sl.get('mac'))
            ip = v_ipv4(sl.get('ip'), field='静态绑定 IP')
            name = (sl.get('name') or '').strip()
            name = name if re.match(r'^[a-zA-Z0-9-]{0,63}$', name) else ''
            lines.append(f'dhcp-host={mac},{ip}' + (f',{name}' if name else ''))
    else:
        lines.append('# DHCP 服务已关闭（仅启用 DNS 转发）')
        lines.append('no-dhcp-interface=' + lan)

    lines += ['', '# ---- DNS 上游 ----']
    upstream_mode = v_choice(cfg.get('dns_mode') or 'custom',
                             ['isp', 'custom', 'both'], field='DNS 来源')
    servers = []
    if upstream_mode in ('custom', 'both'):
        servers += v_ipv4_list(cfg.get('dns_custom') or '', field='自定义 DNS')
    if upstream_mode in ('isp', 'both'):
        servers.append('# 运营商下发 DNS 由 /etc/drouter/generated/isp-dns.conf 动态注入')
        lines.append('resolv-file=/etc/drouter/generated/isp-dns.conf')
    for s in servers:
        if s.startswith('#'):
            continue
        lines.append(f'server={s}')
    if cfg.get('dns_lan_server'):
        lines.append(f'server=/{domain}/{v_ipv4(cfg.get("dns_lan_server"), field="内网 DNS")}')
    lines += [
        f"cache-size={v_int(cfg.get('dns_cache_size') or 1000, field='DNS 缓存', lo=0, hi=20000)}",
        'no-negcache' if not v_bool(cfg.get('dns_negcache', True)) else '',
        'log-queries' if v_bool(cfg.get('dns_query_log')) else '',
        f'log-facility={log_file}',
        'dhcp-leasefile=/var/lib/misc/dnsmasq.leases',
    ]
    return '\n'.join([l for l in lines if l != '']) + '\n'


# ---------------------------------------------------------------- nftables

NFT_HEADER = '# 由 drouter 自动生成 / 可手工编辑\n' \
             '# 应用前会执行 nft -c 语法预检，失败将自动回滚\n'

# 防火墙日志开关默认限速（每秒最多打印的包数），避免刷爆内核日志
FW_LOG_RATE = 20

# 日志前缀：/var/log/drouter/fw4.log 的采集脚本据此识别本机防火墙日志。
# 用统一前缀便于后续「连接与流日志」页面把 IPv4/IPv6 防火墙日志聚合到一起。
FW_LOG_PREFIX = 'DROUTER-FW'


def _fw_log_rule(prefix, rate, comment):
    """生成一条带限速的 log 规则文本（log prefix 里带 IPv4/IPv6 标记）。"""
    return ('    log prefix "%s-%s " limit rate %d/second '
            'comment "%s"' % (FW_LOG_PREFIX, prefix, rate, comment))


def _mss_rule(wan, mode, mss, comment):
    """MSS 钳制规则。

    clamp —— 按出接口路由的 MTU 自动推算（PPPoE 拨号时最省心，MTU 变了不用改配置）；
    fixed —— 写死一个值（多拨、隧道、GRE 等 MTU 不一致的场景才需要手工指定）。
    """
    if mode == 'fixed':
        try:
            val = max(576, min(int(mss), 9000))
        except Exception:
            val = 1452
        return (f'    oifname "{wan}" tcp flags syn tcp option maxseg size set {val} '
                f'comment "{comment}"')
    return (f'    oifname "{wan}" tcp flags syn tcp option maxseg size set rt mtu '
            f'comment "{comment}"')


def _default_v4(wan, lan_ifaces, mss_clamp=True, mss=1452,
                log_drop=True, log_accept=False, log_rate=FW_LOG_RATE,
                pf_enable=False, pf_rules=None, pf_dmz_enable=False, pf_dmz_host='',
                masquerade=True, mss_mode='clamp'):
    """生成 IPv4 默认规则集。

    log_drop   : 在默认拒绝前记录被丢弃的包（默认开启，方便排查"为什么不通"）
    log_accept : 是否同时记录放通的包（数据量大，默认关闭）
    log_rate   : 日志限速（包/秒），防止日志风暴拖垮小内存设备
    pf_*       : 端口转发 / DMZ 参数（见 _pf_rules）
    masquerade : 是否做出向地址伪装（SNAT）。关掉后内网将「只转发不改写源地址」，
                 只有在上游已有回程路由（静态路由 / 已指派网段）时才通。
    mss_mode   : clamp=按路由 MTU 自动算 / fixed=写死 mss 值
    """
    lan_set = ', '.join(f'"{i}"' for i in lan_ifaces) or '"br0"'
    log_drop = bool(log_drop)
    log_accept = bool(log_accept)
    try:
        log_rate = max(1, min(int(log_rate or FW_LOG_RATE), 1000))
    except Exception:
        log_rate = FW_LOG_RATE
    pf_nat, pf_filter = _pf_rules(
        {'pf_enable': pf_enable, 'pf_rules': pf_rules,
         'pf_dmz_enable': pf_dmz_enable, 'pf_dmz_host': pf_dmz_host,
         'wan_iface': wan}, '4', 'ip')
    s = [
        NFT_HEADER,
        'table ip drouter4 {',
        '  # 局域网接口集合（PPPoe/WAN 口之外的接口）',
        '  set lanifs {',
        '    type ifname',
        f'    elements = {{ {lan_set} }}' if lan_set else '    elements = { "br0" }',
        '  }',
        '',
        '  chain input {',
        '    type filter hook input priority filter; policy accept;',
        '    iif "lo" accept',
        '    ct state established,related accept',
    ]
    if log_drop:
        s += ['    ct state invalid '
              + _fw_log_rule('4-IN-INVALID', log_rate, '入站非法状态包（已记录）').strip(),
              '    ct state invalid drop']
    else:
        s += ['    ct state invalid drop']
    s += [
        '    iifname @lanifs accept comment "允许局域网访问本机管理口"',
    ]
    if log_accept:
        s += [_fw_log_rule('4-IN-ACCEPT', log_rate, '入站被放通（已记录）')]
    s += [
        '    # 默认放通 ICMP，便于排障；如需要可在下方自行添加拒绝规则',
        '    icmp type echo-request accept',
    ]
    if log_drop:
        s += [_fw_log_rule('4-IN-REJECT', log_rate, '入站默认拒绝（已记录）')]
    s += [
        '    counter comment "入站总计数"',
        '  }',
        '',
        '  chain forward {',
        '    type filter hook forward priority filter; policy accept;',
        '    ct state established,related accept',
    ]
    if log_drop:
        s += ['    ct state invalid '
              + _fw_log_rule('4-FWD-INVALID', log_rate, '转发非法状态包（已记录）').strip(),
              '    ct state invalid drop']
    else:
        s += ['    ct state invalid drop']
    s += ['    iifname @lanifs accept comment "允许局域网转发上网"']
    if log_accept:
        s += [_fw_log_rule('4-FWD-ACCEPT', log_rate, '转发被放通（已记录）')]
    # 端口转发 / DMZ 的放通规则（需在默认拒绝之前）
    if pf_filter:
        s += ['    # ---- 端口转发 / DMZ 放通（由「端口转发」页面生成）----']
        s += pf_filter
    if log_drop:
        s += [_fw_log_rule('4-FWD-REJECT', log_rate, '转发默认拒绝（已记录）')]
    s += [
        '    counter comment "转发总计数"',
        '  }',
        '',
    ]
    # DNAT 链：端口转发与 DMZ 均在此完成目的地址改写
    if pf_nat:
        s += [
            '  # ---- 目的地址转换（DNAT）：端口转发 / DMZ ----',
            '  chain nat_pre {',
            '    type nat hook prerouting priority dstnat; policy accept;',
        ]
        s += pf_nat
        s += [
            '  }',
            '',
        ]
    nat_body = []
    if masquerade:
        nat_body.append(f'    oifname "{wan}" masquerade comment "WAN 出向地址伪装"')
    else:
        nat_body.append('    # 出向地址伪装已关闭：内网地址原样发出，'
                        '需要上游路由器已有回程路由才通')
    s += ['  chain nat_post {',
          '    type nat hook postrouting priority srcnat; policy accept;'] \
        + nat_body + ['  }']
    if mss_clamp:
        s += [
            '',
            '  chain mangle_forward {',
            '    type filter hook forward priority mangle; policy accept;',
            _mss_rule(wan, mss_mode, mss, 'MSS 钳制（解决部分网站打不开）'),
            '  }',
        ]
    s += ['}']
    return '\n'.join(s) + '\n'


def _default_v6(wan, lan_ifaces,
                log_drop=True, log_accept=False, log_rate=FW_LOG_RATE,
                pf_enable=False, pf_rules=None, pf_dmz_enable=False, pf_dmz_host='',
                mss_clamp=False, mss=1432, mss_mode='clamp', masquerade=False):
    """生成 IPv6 默认规则集（日志语义同 IPv4）。

    IPv6 的两项默认与 IPv4 不同，别照抄：
      masquerade 默认「关」—— IPv6 地址足够多，正常玩法是端到端直连（每个设备都有
        公网 v6），做 NAT66 反而会让 BT / 摄像头 / 远程桌面这类入向应用失效；
        只有在「上游只给了一个 /64 且不再下发 PD」时才需要打开。
      mss_clamp 默认「关」—— IPv6 依赖 PMTUD 且不允许中间设备分片，
        绝大多数场景无需钳制；只有 PPPoE / 隧道把 MTU 压到 1500 以下时才需要。
    """
    lan_set = ', '.join(f'"{i}"' for i in lan_ifaces) or '"br0"'
    log_drop = bool(log_drop)
    log_accept = bool(log_accept)
    try:
        log_rate = max(1, min(int(log_rate or FW_LOG_RATE), 1000))
    except Exception:
        log_rate = FW_LOG_RATE
    pf_nat, pf_filter = _pf_rules(
        {'pf_enable': pf_enable, 'pf_rules': pf_rules,
         'pf_dmz_enable': pf_dmz_enable, 'pf_dmz_host': pf_dmz_host,
         'wan_iface': wan}, '6', 'ip6')
    s = [
        NFT_HEADER,
        'table ip6 drouter6 {',
        '  set lanifs {',
        '    type ifname',
        f'    elements = {{ {lan_set} }}' if lan_set else '    elements = { "br0" }',
        '  }',
        '',
        '  chain input {',
        '    type filter hook input priority filter; policy accept;',
        '    iif "lo" accept',
        '    ct state established,related accept',
    ]
    if log_drop:
        s += [_fw_log_rule('6-IN-INVALID', log_rate, '入站非法状态包（已记录）'),
              '    ct state invalid drop']
    else:
        s += ['    ct state invalid drop']
    s += [
        '    # ICMPv6 是 IPv6 正常工作的基础，必须放通',
        '    icmpv6 type { echo-request, echo-reply, nd-router-advert, nd-router-solicit,',
        '                  nd-neighbor-solicit, nd-neighbor-advert, destination-unreachable,',
        '                  packet-too-big, parameter-problem, time-exceeded } accept',
        '    iifname @lanifs accept',
    ]
    if log_accept:
        s += [_fw_log_rule('6-IN-ACCEPT', log_rate, '入站被放通（已记录）')]
    if log_drop:
        s += [_fw_log_rule('6-IN-REJECT', log_rate, '入站默认拒绝（已记录）')]
    s += [
        '  }',
        '',
        '  chain forward {',
        '    type filter hook forward priority filter; policy accept;',
        '    ct state established,related accept',
    ]
    if log_drop:
        s += [_fw_log_rule('6-FWD-INVALID', log_rate, '转发非法状态包（已记录）'),
              '    ct state invalid drop']
    else:
        s += ['    ct state invalid drop']
    s += [
        '    icmpv6 type { echo-request, destination-unreachable, packet-too-big,',
        '                  parameter-problem, time-exceeded } accept',
        '    iifname @lanifs accept',
    ]
    if log_accept:
        s += [_fw_log_rule('6-FWD-ACCEPT', log_rate, '转发被放通（已记录）')]
    # 端口转发 / DMZ 放通（IPv6 直接路由，无需 NAT，仅需放通）
    if pf_filter:
        s += ['    # ---- 端口转发 / DMZ 放通（由「端口转发」页面生成）----']
        s += pf_filter
    if log_drop:
        s += [_fw_log_rule('6-FWD-REJECT', log_rate, '转发默认拒绝（已记录）')]
    s += [
        '  }',
    ]
    if pf_nat:
        s += [
            '',
            '  # ---- IPv6 端口转发（DNAT，仅当地址非直连时使用）----',
            '  chain nat_pre {',
            '    type nat hook prerouting priority dstnat; policy accept;',
        ]
        s += pf_nat
        s += ['  }']
    if masquerade:
        s += [
            '',
            '  # ---- NAT66：上游只给一个 /64 时才需要 ----',
            '  chain nat_post {',
            '    type nat hook postrouting priority srcnat; policy accept;',
            f'    oifname "{wan}" masquerade comment "IPv6 出向地址伪装（NAT66）"',
            '  }',
        ]
    if mss_clamp:
        s += [
            '',
            '  chain mangle_forward {',
            '    type filter hook forward priority mangle; policy accept;',
            _mss_rule(wan, mss_mode, mss, 'IPv6 MSS 钳制'),
            '  }',
        ]
    s += [
        '}',
    ]
    return '\n'.join(s) + '\n'


def render_nft(kind, cfg):
    """
    kind: 'v4' | 'v6'
    cfg: {wan_iface, lan_ifaces[], mss_clamp, mss, raw,
          log_drop, log_accept, log_rate, portfwd}
    如果用户提供了 raw 自定义规则，则直接采用 raw（高级模式）
    """
    wan = v_ifname(cfg.get('wan_iface') or 'ppp0', field='WAN 接口')
    lans = cfg.get('lan_ifaces') or []
    lans = [v_ifname(x, field='LAN 接口') for x in lans] or ['br0']
    raw = (cfg.get('raw') or '').strip()
    log_kw = {
        'log_drop': cfg.get('log_drop', True),
        'log_accept': cfg.get('log_accept', False),
        'log_rate': v_int(cfg.get('log_rate') or FW_LOG_RATE,
                          field='防火墙日志限速', lo=1, hi=1000),
    }
    pf = cfg.get('portfwd') or {}
    pf_kw = {
        'pf_enable': bool(pf.get('enable')),
        'pf_rules': pf.get('rules') or [],
        'pf_dmz_enable': bool((pf.get('dmz') or {}).get('enable')),
        'pf_dmz_host': (pf.get('dmz') or {}).get('host') or '',
    }
    if raw:
        return raw if raw.endswith('\n') else raw + '\n'
    mss = v_int(cfg.get('mss') or (1452 if kind == 'v4' else 1432),
                field='MSS', lo=576, hi=9000)
    mss_mode = str(cfg.get('mss_mode') or 'clamp')
    if mss_mode not in ('clamp', 'fixed'):
        raise ValidateError('MSS 方式只能是 clamp（按 MTU 自动算）或 fixed（写死数值）')
    masq = v_bool(cfg.get('masquerade', True if kind == 'v4' else False))
    if kind == 'v4':
        return _default_v4(wan, lans, v_bool(cfg.get('mss_clamp', True)), mss,
                           **log_kw, **pf_kw, masquerade=masq, mss_mode=mss_mode)
    return _default_v6(wan, lans, **log_kw, **pf_kw,
                       mss_clamp=v_bool(cfg.get('mss_clamp', False)),
                       mss=mss, mss_mode=mss_mode, masquerade=masq)


# ---------------------------------------------------------------- 端口转发 / DMZ（#7）
#
# 目标：用 nftables 的 DNAT（prerouting）实现主流、稳定、好用的端口转发与 DMZ。
#
# 设计要点：
#   * 规则进入已有的 drouter4 / drouter6 表，避免多个表互相遮蔽；
#   * 采用「转发 + 放通」两段式：nat 链做 DNAT，filter forward 链放通；
#     这样即使 forward 默认策略是 accept，也能显式记录与统计；
#   * 同时自动生成内网侧的 hairpin（NAT 回环）规则，使「内网用公网域名访问」也能通；
#   * DMZ = 「全部端口转发到某台主机」的快捷方式，等价于 1:1 全端口映射，
#     国内家用最常见的做法；产生一条规则并自动放通。

PF_PROTO_CHOICES = ['tcp', 'udp', 'tcp/udp']


def _pf_rules_v4(cfg):
    """生成 IPv4 端口转发与 DMZ 的 nft 片段。"""
    return _pf_rules(cfg, '4', 'ip')


def _pf_rules_v6(cfg):
    """生成 IPv6 端口转发与 DMZ 的 nft 片段（IPv6 不需要 masquerade/DNAT 回环处理一致）。"""
    return _pf_rules(cfg, '6', 'ip6')


def _pf_rules(cfg, fam, family_kw):
    """按协议族生成端口转发规则集合。

    cfg 内字段：
      pf_enable      : 总开关
      pf_rules       : [{name, proto, ext_port, int_ip, int_port, enable, comment}]
      pf_dmz_enable  : DMZ 开关
      pf_dmz_host    : DMZ 主机内网地址
    返回 (nat_lines, filter_lines)
    """
    nat_lines = []
    filter_lines = []

    # ---- 普通端口转发 ----
    if cfg.get('pf_enable'):
        for i, r in enumerate(cfg.get('pf_rules') or []):
            if not r.get('enable', True):
                continue
            proto = (r.get('proto') or 'tcp').strip().lower()
            if proto not in PF_PROTO_CHOICES:
                raise ValidateError('端口转发第 %d 条协议不合法：%s' % (i + 1, proto))
            extp = v_int(r.get('ext_port'), field='端口转发第 %d 条外部端口' % (i + 1),
                         lo=1, hi=65535)
            intp = v_int(r.get('int_port') or r.get('ext_port'),
                         field='端口转发第 %d 条内部端口' % (i + 1), lo=1, hi=65535)
            ip = r.get('int_ip')
            if fam == '4':
                ip = v_ipv4(ip, '端口转发第 %d 条内网 IP' % (i + 1))
            else:
                ip = v_ipv6(ip, '端口转发第 %d 条内网 IPv6' % (i + 1))
            # 备注整串写进 comment "…"，原先只删了双引号：留下换行同样能
            # 闭合注释、再起一行任意 nft 指令。这里连同控制字符一起去掉。
            name = CTRL_RE.sub('', str(r.get('name') or '').replace('"', ''))[:40].strip()
            cmt = name or ('端口转发 %d' % (i + 1))
            protos = ['tcp', 'udp'] if proto == 'tcp/udp' else [proto]
            for pr in protos:
                nat_lines.append(
                    '    iifname "%s" %s dport %d dnat to %s%s%s comment "%s"'
                    % (v_ifname(cfg.get('wan_iface') or 'ppp0', field='WAN 接口'),
                       ('tcp' if pr == 'tcp' else 'udp'), extp,
                       '[' + ip + ']' if fam == '6' else ip,
                       (':' + str(intp)) if intp != extp else '',
                       '', '%s(%s→%s)' % (cmt, pr, extp)))
                filter_lines.append(
                    '    %s dport %d %s daddr %s accept comment "放通转发：%s"'
                    % (('tcp' if pr == 'tcp' else 'udp'), intp,
                       'ip6' if fam == '6' else 'ip',
                       ip, cmt))

    # ---- DMZ（全端口映射到某台主机）----
    if cfg.get('pf_dmz_enable') and (cfg.get('pf_dmz_host') or '').strip():
        host = cfg.get('pf_dmz_host')
        if fam == '4':
            host = v_ipv4(host, 'DMZ 主机 IP')
        else:
            host = v_ipv6(host, 'DMZ 主机 IPv6')
        wan = v_ifname(cfg.get('wan_iface') or 'ppp0', field='WAN 接口')
        dest = '[' + host + ']' if fam == '6' else host
        # 到本机自身的流量必须先放行，否则 DMZ 会把管理口一并转走。
        #
        # 原先写的是 `daddr != <DMZ 主机> dnat to <DMZ 主机>`，这个条件是反的：
        # 它排除的正是「本来就要发给 DMZ 主机」的包（不 DNAT 也照样走到它），
        # 而真正需要保护的、发往本机 WAN 地址的流量（SSH 22、面板 8443/8080）
        # 反而全部命中规则、被丢到内网那台 DMZ 主机上 —— 也就是注释声称要
        # 避免的那件事，恰好是这条规则在做的事。表现是「一开 DMZ，路由器
        # 自己就登不上了」，而且很难联想到是 DMZ 干的。
        #
        # fib daddr type local 判断目的地址是否属于本机（含本机所有地址），
        # accept 只结束本条 prerouting 链的后续规则、不再做 DNAT。
        nat_lines.append(
            '    iifname "%s" fib daddr type local accept comment "到本机自身的流量不进 DMZ"'
            % wan)
        nat_lines.append(
            '    iifname "%s" %s dnat to %s comment "DMZ 全端口映射"'
            % (wan, family_kw, dest))
        filter_lines.append(
            '    %s daddr %s accept comment "放通 DMZ 主机"' % (family_kw, host))
    return nat_lines, filter_lines


# ---------------------------------------------------------------- systemd-networkd

# systemd-networkd 的单元目录。render() 返回的是 **完整路径**，
# render_network 内部只用文件名拼装，出口再补上这里的前缀。
NETWORKD_DIR = '/etc/systemd/network'


def render_network(cfg):
    """
    返回 [{name, content}]，写入 /etc/systemd/network/
    cfg: {wan: {...}, lan: {...}, bridge: {...}, ifaces: [...]}
    """
    out = []
    wan = cfg.get('wan') or {}
    wif = v_ifname(wan.get('iface') or '', field='WAN 网卡')
    wmode = v_choice(wan.get('mode') or 'dhcp', ['dhcp', 'static', 'pppoe'], field='WAN 模式')

    # ---- WAN ----
    if wmode == 'pppoe':
        # PPPoE 的底层网卡保持 up、不配地址，由 pppd 创建 ppp0
        out.append({
            'name': f'10-drouter-wan-{wif}.network',
            'content': '\n'.join([
                '# 由 drouter 自动生成：PPPoE 底层网卡（不配置 IP，由 pppd 接管）',
                '[Match]',
                f'Name={wif}',
                '',
                '[Link]',
                f"MTUBytes={v_int(wan.get('mtu') or 1500, field='MTU', lo=576, hi=9000)}",
                'RequiredForOnline=no',
                '',
                '[Network]',
                'LinkLocalAddressing=no',
                'LLDP=no',
                'EmitLLDP=no',
                'IPv6AcceptRA=no',
                'DHCP=no',
                '',
            ])
        })
    elif wmode == 'static':
        lines = [
            '# 由 drouter 自动生成：WAN 静态地址',
            '[Match]',
            f'Name={wif}',
            '',
            '[Link]',
            f"MTUBytes={v_int(wan.get('mtu') or 1500, field='MTU', lo=576, hi=9000)}",
            '',
            '[Network]',
            f"Address={v_cidr(wan.get('address'), field='WAN 地址')}",
        ]
        gw = (wan.get('gateway') or '').strip()
        if gw:
            lines.append(f'Gateway={v_ipv4(gw, field="WAN 网关")}')
        for d in v_ipv4_list(wan.get('dns') or '', field='WAN DNS'):
            lines.append(f'DNS={d}')
        lines += ['', '[Route]', 'GatewayOnLink=yes', '']
        out.append({'name': f'10-drouter-wan-{wif}.network', 'content': '\n'.join(lines)})
    else:
        out.append({
            'name': f'10-drouter-wan-{wif}.network',
            'content': '\n'.join([
                '# 由 drouter 自动生成：WAN 自动获取（DHCP）',
                '[Match]',
                f'Name={wif}',
                '',
                '[Link]',
                f"MTUBytes={v_int(wan.get('mtu') or 1500, field='MTU', lo=576, hi=9000)}",
                '',
                '[Network]',
                'DHCP=yes',
                'IPv6AcceptRA=yes',
                '',
                '[DHCPv4]',
                # WAN 页 DHCP 模式下的「使用上级下发的 DNS」开关（pppoe.dhcp_use_dns）
                # 由前端透传到这里。以前这一项是写死的 yes —— 用户在界面上关掉它，
                # 照样会被上级的 DNS 覆盖，等于一个改不生效的假开关。
                'UseDNS=%s' % ('yes' if v_bool(wan.get('use_dns', True)) else 'no'),
                'UseRoutes=yes',
                f"RouteMetric={v_int(wan.get('metric') or 100, field='路由优先级', lo=1, hi=9999)}",
                '',
            ])
        })

    # ---- 桥接 ----
    br = cfg.get('bridge') or {}
    brname = (br.get('name') or 'br0').strip() or 'br0'
    members = [v_ifname(x, field='桥接成员') for x in (br.get('members') or [])]
    if v_bool(br.get('enabled')) and members:
        out.append({
            'name': f'20-drouter-{brname}.netdev',
            'content': '\n'.join([
                '# 由 drouter 自动生成：局域网网桥',
                '[NetDev]',
                f'Name={v_ifname(brname, field="网桥名")}',
                'Kind=bridge',
                '',
                '[Bridge]',
                'STP=no' if not v_bool(br.get('stp')) else 'STP=yes',
                f"ForwardDelaySec={v_int(br.get('fd') or 2, field='转发延迟', lo=0, hi=30)}",
                '',
            ])
        })
        for m in members:
            out.append({
                'name': f'21-drouter-{brname}-member-{m}.network',
                'content': '\n'.join([
                    '# 由 drouter 自动生成：桥接成员口',
                    '[Match]',
                    f'Name={m}',
                    '',
                    '[Network]',
                    f'Bridge={v_ifname(brname, field="网桥名")}',
                    'LinkLocalAddressing=no',
                    '',
                ])
            })
        # 桥本身承载 IP
        lan = cfg.get('lan') or {}
        addr = v_cidr(lan.get('address') or '192.168.7.1/24', field='LAN 地址')
        lines = [
            '# 由 drouter 自动生成：局域网桥地址',
            '[Match]',
            f'Name={v_ifname(brname, field="网桥名")}',
            '',
            '[Network]',
            f'Address={addr}',
            'DHCP=no',
            'LinkLocalAddressing=ipv6',
            'IPv6AcceptRA=no',
        ]
        if v_bool(lan.get('ipv6_assign')) and lan.get('ipv6_addr'):
            lines.append(f"Address={lan['ipv6_addr'].strip()}")
        lines.append('')
        out.append({'name': f'30-drouter-{brname}.network', 'content': '\n'.join(lines)})
    else:
        # 无桥接：LAN 地址直接配在单网卡上
        lan = cfg.get('lan') or {}
        lif = v_ifname(lan.get('iface') or '', field='LAN 网卡')
        addr = v_cidr(lan.get('address') or '192.168.7.1/24', field='LAN 地址')
        lines = [
            '# 由 drouter 自动生成：局域网地址',
            '[Match]',
            f'Name={lif}',
            '',
            '[Link]',
            f"MTUBytes={v_int(lan.get('mtu') or 1500, field='MTU', lo=576, hi=9000)}",
            '',
            '[Network]',
            f'Address={addr}',
            'DHCP=no',
            'LinkLocalAddressing=ipv6',
        ]
        if v_bool(lan.get('ipv6_assign')) and lan.get('ipv6_addr'):
            lines.append(f"Address={lan['ipv6_addr'].strip()}")
        lines.append('')
        out.append({'name': f'30-drouter-lan-{lif}.network', 'content': '\n'.join(lines)})
    # render() 的对外契约是 [(完整路径, 内容)]，其余渲染器都按这个返回。
    # 这里内部为了方便拼装用了 {name, content} 字典，必须在出口转成契约形态——
    # 否则 act_migrate_networkd 的 `for path, content in files` 解包 dict 时
    # 只会拿到它的两个**键名**字符串 'name' 和 'content'，
    # 于是把字面量 'content' 当成文件内容、写进一个叫 'name' 的文件里。
    return [('%s/%s' % (NETWORKD_DIR, u['name']), u['content']) for u in out]


# ---------------------------------------------------------------- PPPoE

def _sub(cfg, key):
    """兼容两种入参形态：嵌套 {<key>:{...}} 或平铺 {...}。
    这样前端、helper、单元测试可以统一按平铺结构传参。"""
    if not isinstance(cfg, dict):
        return {}
    v = cfg.get(key)
    if isinstance(v, dict):
        merged = dict(v)
        # 平铺字段若存在且嵌套中缺失，则以平铺为准补齐
        for k, val in cfg.items():
            if k == key:
                continue
            merged.setdefault(k, val)
        return merged
    return cfg


def render_ppp(cfg):
    """
    返回 [(path, content)]：peers 文件 + secrets
    兼容两种入参：平铺 {iface, username, ...} 或嵌套 {pppoe:{...}}
    """
    p = _sub(cfg, 'pppoe')
    iface = v_ifname(p.get('iface') or '', field='PPPoE 网卡')
    user = (p.get('username') or '').strip()
    passwd = (p.get('password') or '').strip()
    if not user:
        raise ValidateError('PPPoE 用户名不能为空', 'username')
    if re.search(r'[\s"\']', user):
        raise ValidateError('PPPoE 用户名不能包含空格或引号', 'username')
    if not passwd:
        raise ValidateError('PPPoE 密码不能为空（仅保存、暂不拨号）', 'password')
    if re.search(r'[\s"\']', passwd):
        raise ValidateError('PPPoE 密码不能包含空格或引号', 'password')
    mtu = v_int(p.get('mtu') or 1492, field='PPPoE MTU', lo=576, hi=9000)
    mru = v_int(p.get('mru') or mtu, field='PPPoE MRU', lo=576, hi=9000)
    # 这两个字段用户改得很随意，但也有同样的注入面：
    #   isp            —— 进了注释行，带换行就会变成真的 pppd 选项
    #   service_name   —— 被写进 servicename "…"，含引号就能闭合引号接着写选项
    # 用户名/密码早就检查了引号与空格，这里补上缺的那两个（原先只对 caveman
    # 做了限制，servicename 完全没有）。
    isp = v_text(p.get('isp') or '自动', field='运营商', maxlen=32,
                 pattern=r'^[A-Za-z0-9\u4e00-\u9fff ._+-]{1,32}$')
    svc = v_text(p.get('service_name') or '', field='PPPoE 服务名',
                 maxlen=64, allow_empty=True,
                 pattern=r'^[A-Za-z0-9._-]{1,64}$')

    peers = [
        '# 由 drouter 自动生成：PPPoE 拨号配置',
        '# 运营商：' + isp,
        f'user "{user}"',
        f'plugin rp-pppoe.so {iface}',
        'noipdefault',
        'defaultroute',
        'replacedefaultroute',
        'hide-password',
        'noauth',
        'persist' if v_bool(p.get('persist', True)) else '',
        f'maxfail {v_int(p.get("maxfail") or 0, field="最大失败次数", lo=0, hi=100)}',
        f'holdoff {v_int(p.get("holdoff") or 5, field="重拨间隔", lo=1, hi=600)}',
        'usepeerdns',
        'lcp-echo-interval 20',
        'lcp-echo-failure 3',
        f'mtu {mtu}',
        f'mru {mru}',
        'noaccomp',
        'nopcomp',
        'novj',
        'novjccomp',
    ]
    if svc:
        peers.append(f'servicename "{svc}"')
    peers += ['', '']
    secrets = f'"{user}" * "{passwd}"\n'
    return [('/etc/ppp/peers/drouter-wan', '\n'.join([l for l in peers if l != ''])),
            ('/etc/ppp/chap-secrets', secrets),
            ('/etc/ppp/pap-secrets', secrets)]


# ---------------------------------------------------------------- radvd (RA)

def render_radvd(cfg):
    r = _sub(cfg, 'ra')
    iface = v_ifname(r.get('iface') or 'br0', field='RA 接口')
    prefix = (r.get('prefix') or '').strip()
    lines = [
        '# 由 drouter 自动生成：IPv6 路由通告 (RA)',
        f'interface {iface} {{',
        '    AdvSendAdvert on;',
        f"    AdvManagedFlag {'on' if v_bool(r.get('managed')) else 'off'};",
        f"    AdvOtherConfigFlag {'on' if v_bool(r.get('other_config')) else 'off'};",
        f"    MaxRtrAdvInterval {v_int(r.get('max_interval') or 600, field='RA 最大间隔', lo=4, hi=1800)};",
        f"    MinRtrAdvInterval {v_int(r.get('min_interval') or 200, field='RA 最小间隔', lo=3, hi=1350)};",
        f"    AdvDefaultLifetime {v_int(r.get('lifetime') or 1800, field='默认生命周期', lo=0, hi=9000)};",
        f"    AdvCurHopLimit {v_int(r.get('hop_limit') or 64, field='跳数限制', lo=0, hi=255)};",
        f"    AdvReachableTime {v_int(r.get('reachable') or 0, field='可达时间', lo=0, hi=3600000)};",
        f"    AdvRetransTimer {v_int(r.get('retrans') or 0, field='重传计时', lo=0, hi=60000)};",
    ]
    if prefix:
        try:
            ipaddress.IPv6Network(prefix, strict=False)
        except Exception:
            raise ValidateError(f'IPv6 前缀不合法：{prefix}', 'prefix')
        lines += [
            f'    prefix {prefix} {{',
            '        AdvOnLink on;',
            '        AdvAutonomous on;',
            f"        AdvValidLifetime {v_int(r.get('valid_life') or 86400, field='有效生命期', lo=60, hi=4294967295)};",
            f"        AdvPreferredLifetime {v_int(r.get('pref_life') or 14400, field='首选生命期', lo=60, hi=4294967295)};",
            '    };',
        ]
    if r.get('rdnss'):
        # RDNSS 只认 IPv6 地址：RA 通告里根本没有 IPv4 的位置，而且多值必须是
        # **空格分隔**（逗号形式 radvd 会直接 syntax error）。
        # 这里原先完全不校验，于是「下发 DNS」框里顺手填了 IPv4（223.5.5.5 最顺手）
        # 就会渲染出一份 radvd 读不进去的配置 —— 界面只回一句英文 syntax error，
        # 指不到是哪个字段。实测 IPv4 / 逗号列表 / 域名 三种输入都会踩中。
        ips = []
        for item in str(r['rdnss']).replace(',', ' ').split():
            try:
                ipaddress.IPv6Address(item)
            except Exception:
                raise ValidateError(
                    f'RDNSS 只能是 IPv6 地址：{item}（RA 通告无法下发 IPv4 DNS，'
                    f'IPv4 请填在 DHCP 的 option 6 里）', 'rdnss')
            ips.append(item)
        lines.append('    RDNSS %s {' % ' '.join(ips))
        lines.append(f"        AdvRDNSSLifetime {v_int(r.get('rdnss_life') or 600, field='RDNSS 生命期', lo=60, hi=9000)};")
        lines.append('    };')
    if r.get('dnssl'):
        # 同 RDNSS：多个域名用空格分隔（逗号归一成空格），逐个校验，
        # 免得一个写错的域名让 radvd 整份配置起不来。
        doms = []
        for dom in str(r['dnssl']).replace(',', ' ').split():
            if not DOMAIN_RE.match(dom):
                raise ValidateError(f'DNSSL 域名不合法：{dom}', 'dnssl')
            doms.append(dom)
        lines.append('    DNSSL %s {' % ' '.join(doms))
        # 生命期优先取 dnssl_life；没这个字段时退回 rdnss_life ——
        # 直接写 rdnss_life 会让「DNSSL 生命期」这个 label 指向别人的值。
        lines.append(f"        AdvDNSSLLifetime {v_int(r.get('dnssl_life') or r.get('rdnss_life') or 600, field='DNSSL 生命期', lo=60, hi=9000)};")
        lines.append('    };')
    lines += ['};', '']
    # IPv6 地址池说明块（供人工与后续脚本参考；radvd 本身通过 prefix 段生效）
    if r.get('pool_enabled', True) is not False:
        # 整块都是「先取值再原样拼」的写法，池名/前缀/策略三个字段原先一个
        # 都没校验：填了换行的池名会把注释后面变成真的 radvd 指令，而页面
        # 只会报一句 radvd.conf 语法错误。这里逐个过一遍。
        pol = listify(r.get('pool_policy')) or ['recommend']
        for it in pol:
            v_text(it, field='IPv6 池策略', maxlen=32,
                   pattern=r'^[A-Za-z0-9_-]{1,32}$')
        pool_prefix = v_text(r.get('pool_prefix') or '', field='IPv6 池前缀',
                             maxlen=45, allow_empty=True,
                             pattern=r'^[0-9a-fA-F:.]{1,45}$')
        if pool_prefix and '/' in pool_prefix:
            try:
                ipaddress.IPv6Network(pool_prefix, strict=False)
            except Exception:
                raise ValidateError(f'IPv6 池前缀不合法：{pool_prefix}（示例 2408:8207:xxxx::/56）',
                                    'pool_prefix')
        # 「起始子网 ID」允许 0，别写成 `or 1` —— 0 会被当成没填而悄悄改成 1，
        # 用户以为从 0 号子网开始分，实际渲染出来是 1（同 _clean_auto 的 hour=0）。
        _ps, _pe = r.get('pool_start'), r.get('pool_end')
        if _ps in (None, ''):
            _ps = 1
        if _pe in (None, ''):
            _pe = 254
        pstart = v_int(_ps, field='起始子网 ID', lo=0, hi=65535)
        pend = v_int(_pe, field='结束子网 ID', lo=0, hi=65535)
        if pstart > pend:
            raise ValidateError(
                f'IPv6 池分配范围不合法：起始子网 ID（{pstart}）大于结束子网 ID（{pend}）',
                'pool_start')
        lines += [
            '# ---------------- IPv6 地址池 / 池策略（RouterOS 风格） ----------------',
            f"# 池名称：{v_text(r.get('pool_name') or 'drouter-wan-pool', field='IPv6 池名称', maxlen=48, pattern=r'^[A-Za-z0-9._-]{1,48}$')}",
            f"# 地址来源：{v_text(r.get('pool_from') or 'pool', field='IPv6 池来源', maxlen=32, pattern=r'^[A-Za-z0-9._-]{1,32}$')}",
            f"# 池前缀：{pool_prefix or '（跟随运营商 PD）'}",
            f"# 子网 ID 长度：{v_int(r.get('pool_subnet_bits') or 8, field='子网 ID 长度', lo=0, hi=64)}",
            f"# 分配范围：子网 ID {pstart}"
            f" - {pend}",
            f"# 池策略：{', '.join(pol) if pol else 'recommend'}",
            '# ---------------------------------------------------------------------',
            '',
        ]
    return '\n'.join(lines)


# ---------------------------------------------------------------- dhcpcd (DHCPv6-PD)

def render_dhcpcd(cfg):
    d = _sub(cfg, 'dhcpv6')
    wan = v_ifname(d.get('iface') or d.get('wan_iface') or 'ppp0', field='DHCPv6 WAN 接口')
    lan = v_ifname(d.get('lan_iface') or 'br0', field='DHCPv6 LAN 接口')
    # 前端传来的前缀长度可能是 "/60" 这种带斜杠形式
    raw_len = str(d.get('prefix_len') or 64).lstrip('/')
    try:
        plen = int(raw_len)
    except Exception:
        plen = 64
    enabled = d.get('enabled', True) is not False
    lines = [
        '# 由 drouter 自动生成：DHCPv6 客户端 / 前缀委派 (PD)',
        '# 注意：是否真正拿到 PD 前缀取决于运营商，页面会显示实际获取结果',
        'duid',
        'noipv6rs',
        'waitip 6',
        '',
        f'interface {wan}',
    ]
    if not enabled:
        lines.insert(0, '# DHCPv6 客户端已在页面中关闭，以下为保留配置（未启用）')
    if v_bool(d.get('request_address')):
        lines.append('    ipv6rs')
    if v_bool(d.get('request_pd', True)):
        iaid = v_int(d.get('iaid') or 0, field='IAID', lo=0, hi=4294967295)
        # sla_id 原先只做了 .strip()，然后直接拼进 ia_pd 的第 5 段：
        # 一个换行就能在 dhcpcd.conf 里加一行 parameter-like 指令，
        # 填个非 IPv6 的串则让整个 PD 请求失效（而且只在系统日志里有痕迹）。
        suffix = v_ipv6_suffix(d.get('sla_id'), field='SLA-ID 后缀')
        lines.append(f'    ia_pd {iaid}/{lan}/0/{plen}/{suffix}')
    if d.get('rapid_commit'):
        lines.append('    option rapid_commit')
    if d.get('slaac') or d.get('method') in ('slaac', 'both'):
        lines.append('    ia_na 1')
    lines += ['', f'interface {lan}', '    noipv6rs', '']
    return '\n'.join(lines)


# ---------------------------------------------------------------- miniupnpd

def render_miniupnpd(cfg):
    u = _sub(cfg, 'upnp')
    ext = v_ifname(u.get('ext_iface') or 'ppp0', field='UPnP 外网接口')
    # 监听网卡名（nftables/新版 miniupnpd 需要网卡名而非 IP：
    # 用 IP 会告警 "it is advised to use network interface name instead"，
    # 且会禁用 IPV6/SSDPv6 支持）。listen_ip 字段仅用于兼容旧配置。
    lan_if = u.get('lan_iface') or u.get('listen_ip') or 'ens18'
    try:
        lan_if = v_ifname(lan_if, field='UPnP 监听接口')
    except Exception:
        lan_if = 'ens18'
    # UPnP 权限规则：仅允许内网网段申请临时端口（>=1024），末尾全拒。
    # 网段取自 system.lan_address；取不到时退回 192.168.0.0/16 保守放行。
    lan_cidr = '192.168.0.0/16'
    sysc = _sub(cfg, 'system')
    addr = str(sysc.get('lan_address') or '')
    if '/' in addr:
        try:
            lan_cidr = v_cidr(addr, field='UPnP 允许网段')
        except Exception:
            lan_cidr = '192.168.0.0/16'
    elif addr:
        try:
            lan_cidr = v_ipv4(addr, field='UPnP 允许网段') + '/24'
        except Exception:
            lan_cidr = '192.168.0.0/16'
    lines = [
        '# 由 drouter 自动生成：miniupnpd',
        f'ext_ifname={ext}',
        f'listening_ip={lan_if}',
        # http_port 是 nftables/新版 miniupnpd 的正确选项名；
        # 写成 port= 会被忽略（旧版 iptables 变体才用 port=）。
        f"http_port={v_int(u.get('port') or 5000, field='UPnP 端口', lo=1024, hi=65535)}",
        'enable_upnp=yes' if v_bool(u.get('enable', True)) else 'enable_upnp=no',
        'enable_natpmp=yes' if v_bool(u.get('natpmp')) else 'enable_natpmp=no',
        f"secure_mode={'yes' if v_bool(u.get('secure_mode', True)) else 'no'}",
        # nftables 后端（miniupnpd 2.3.9）支持的 chain 选项。
        # 注意：upnp_nat_prerouting_chain 在 2.3.x 中并不存在，
        # 写入会触发 "invalid option" 导致 miniupnpd 启动失败（实测 2026-09-28）。
        'upnp_forward_chain=forward_miniupnpd',
        'upnp_nat_chain=prerouting_miniupnpd',
        'upnp_nat_postrouting_chain=postrouting_miniupnpd',
        'system_uptime=yes',
        # lease_file / uuid 原先直接用用户的原值，没设在用户表格里就能被外部
        # 直接调 API 改成带换行的值，从而往 miniupnpd.conf 里追加任意指令。
        f"lease_file={v_path(u.get('lease_file'), field='UPnP 租约文件') if (u.get('lease_file') or '').strip() else '/var/run/miniupnpd.leases'}",
        f"uuid={v_text(u.get('uuid') or 'drouter-0001', field='UPnP UUID', maxlen=64, pattern=r'^[A-Za-z0-9._-]{1,64}$')}",
        'force_igd_desc_v1=yes' if v_bool(u.get('igd_v1')) else '',
        '',
        '# UPnP 权限规则：仅允许内网申请临时端口，末尾默认拒绝',
        f'allow 1024-65535 {lan_cidr} 1024-65535',
        'deny 0-65535 0.0.0.0/0 0-65535',
    ]
    return '\n'.join([l for l in lines if l != ''])


# ---------------------------------------------------------------- chrony

def render_chrony(cfg):
    n = _sub(cfg, 'ntp')
    raw = n.get('servers')
    if isinstance(raw, (list, tuple)):
        servers = [str(x).strip() for x in raw if str(x).strip()]
    else:
        servers = [x.strip() for x in (raw or '').split(',') if x.strip()]
    if not servers:
        servers = ['ntp.aliyun.com', 'time.cloudflare.com']
    for s in servers:
        # 原先只认「域名」或「IPv4」，于是 IPv6 的 NTP 服务器（2400:3200::1
        # 这种国内常用的阿里公共 NTP）一律被判非法 —— 用户只能退回填域名。
        if not DOMAIN_RE.match(s) and not IPV4_RE.match(s):
            try:
                ipaddress.IPv6Address(s.split('%')[0])
            except Exception:
                raise ValidateError(
                    f'NTP 服务器地址不合法：{s}（可填域名、IPv4 或 IPv6 地址）', 'servers')
    lines = ['# 由 drouter 自动生成：NTP 客户端']
    for s in servers:
        lines.append(f'server {s} iburst')
    lines += [
        'driftfile /var/lib/chrony/chrony.drift',
        'rtcsync',
        'makestep 1.0 3',
        'leapsectz right/UTC',
        '',
    ]
    if v_bool(n.get('allow_lan')) and n.get('allow_net'):
        lines.append(f'allow {v_cidr(n["allow_net"], field="允许同步的网段")}')
    return '\n'.join(lines)


# ------------------------------------------------- SMB / NFS 内网文件共享（#9）

# 用户名 / 组名：Samba 与 NFS 共用，限制为 POSIX 可移植字符集
UNAME_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,30}\$?$')
SHARE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')


def v_uname(v, field='名称', allow_empty=False):
    v = (v or '').strip()
    if not v:
        if allow_empty:
            return ''
        raise ValidateError(f'{field}不能为空', field)
    if not UNAME_RE.match(v):
        raise ValidateError(
            f'{field}不合规：{v}（只能用小写字母、数字、下划线、短横线，且以字母或下划线开头）', field)
    return v


def v_path(v, field='路径'):
    """共享目录路径：必须是绝对路径，且不含 .. 与 NUL，避免目录穿越。"""
    v = (v or '').strip()
    if not v:
        raise ValidateError(f'{field}不能为空', field)
    if not v.startswith('/'):
        raise ValidateError(f'{field}必须是绝对路径（以 / 开头）：{v}', field)
    if '\x00' in v or '..' in v.split('/'):
        raise ValidateError(f'{field}不能包含 .. 或空字节：{v}', field)
    if not re.match(r'^/[A-Za-z0-9._/\u4e00-\u9fff-]*$', v):
        raise ValidateError(f'{field}含不支持的字符：{v}', field)
    return v.rstrip('/') or '/'


def listify(v):
    """把 list / 逗号或空白分隔的字符串统一成去空列表。"""
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r'[\s,;]+', v or '') if x.strip()]


def render_samba(cfg):
    """渲染 smb.conf（/etc/samba/drouter.conf，由主配置 include）。"""
    sb = _sub(cfg, 'samba')
    workgroup = (sb.get('workgroup') or 'WORKGROUP').strip().upper()
    if not re.match(r'^[A-Z0-9_-]{1,15}$', workgroup):
        raise ValidateError('工作组名不合法（1–15 位大写字母/数字/-/_）', 'workgroup')
    server_string = (sb.get('server_string') or '').strip()
    if server_string and not re.match(r'^[A-Za-z0-9\u4e00-\u9fff ._-]{1,48}$', server_string):
        raise ValidateError('服务器描述含不支持的字符（可用中英文、数字、空格、. _ -）', 'server_string')

    lines = [
        '# 由 drouter 自动生成：SMB / CIFS 文件共享（#9）',
        '# 被 /etc/samba/smb.conf 通过 include 引入，请勿手改。',
        '',
        '[global]',
        f'   workgroup = {workgroup}',
        '   server role = standalone server',
        '   # 关闭 NetBIOS/老协议，只保留 SMB2/3，兼容 Windows 10/11、macOS 与 Linux',
        '   server min protocol = SMB2',
        '   client min protocol = SMB2',
        '   server max protocol = SMB3',
        '   # 让 macOS 更容易发现（Bonjour / mDNS）',
        '   multicast dns register = yes',
        '   # 各平台文件名兼容：UTF-8 + 规范化，避免中文乱码',
        '   unix charset = UTF-8',
        '   dos charset = CP936',
        '   map to guest = bad user',
        '   log file = /var/log/samba/log.%m',
        '   max log size = 1000',
        '   logging = file',
        '   panic action = /usr/share/samba/panic-action %d',
        '   server string = %s' % (server_string or 'drouter 文件共享'),
        '',
    ]
    if v_bool(sb.get('disable_netbios')):
        lines += ['   # 关闭 NetBIOS 名称服务（纯本地网段建议开启，减少广播）',
                  '   disable netbios = yes', '']
    # WINS 原先写成「wins_enable 与 wins_support 必须同时为真」，但页面上只有
    # 一个开关 —— 它写入的是 wins_enable（sw('#sh-wins','wins_enable')），
    # wins_support 从来没有任何界面会去写。于是勾上「本机充当 WINS 服务器」
    # 再保存、应用，smb.conf 里一行都不会多出来，是个彻头彻尾的死开关。
    # 这里以 wins_enable 为准，wins_support 降级为历史配置里的别名。
    if v_bool(sb.get('wins_enable')) or v_bool(sb.get('wins_support')):
        lines += ['   # 由本机充当 WINS 服务器',
                  '   wins support = yes',
                  '   local master = yes',
                  '   preferred master = yes',
                  '   os level = 65', '']
    # 多网卡 / 指定监听
    ifaces = listify(sb.get('interfaces'))
    if ifaces:
        for i in ifaces:
            if not IFNAME_RE.match(i):
                raise ValidateError(f'监听网卡名不合法：{i}', 'interfaces')
        lines += [f'   interfaces = {" ".join(ifaces)}',
                  '   bind interfaces only = yes', '']
    for extra in listify(sb.get('global_extra')):
        lines.append(f'   {extra}')
    if listify(sb.get('global_extra')):
        lines.append('')

    shares = sb.get('shares') or []
    used = set()
    for i, s in enumerate(shares):
        if not v_bool(s.get('enable', True)):
            continue
        name = (s.get('name') or '').strip()
        if not name:
            raise ValidateError(f'第 {i + 1} 个共享缺少名称', 'name')
        if not SHARE_NAME_RE.match(name):
            raise ValidateError(
                f'共享名「{name}」不合法：只能用字母、数字、. _ -，且不能以符号开头', 'name')
        if name.lower() in used:
            raise ValidateError(f'共享名重复：{name}', 'name')
        used.add(name.lower())
        path = v_path(s.get('path'), field=f'共享「{name}」的目录')
        mode = (s.get('mode') or 'auth').strip()
        comment = (s.get('comment') or '').strip().replace('\n', ' ')
        lines.append(f'[{name}]')
        lines.append(f'   path = {path}')
        lines.append(f'   comment = {comment or name}')
        lines.append('   browseable = yes')
        lines.append('   read only = %s' % ('no' if v_bool(s.get('writable')) else 'yes'))
        lines.append('   create mask = 0664')
        lines.append('   directory mask = 0775')
        lines.append('   # 让复制进来的文件继承父目录的组与 setgid 位，跨用户可写')
        lines.append('   inherit permissions = yes')
        lines.append('   vfs objects = catia fruit streams_xattr')
        lines.append('   fruit:metadata = stream')
        lines.append('   fruit:model = MacSamba')
        lines.append('   # macOS 会写一些以 ._ 开头的元数据文件，隐藏它们')
        lines.append('   fruit:veto_appledouble = no')
        lines.append('   fruit:posix_rename = yes')
        lines.append('   fruit:zero_file_id = yes')
        lines.append('   fruit:nfs_aces = no')

        if mode == 'public':
            lines += ['   guest ok = yes', '   guest only = yes',
                      '   force user = nobody', '   force group = nogroup']
        elif mode == 'readonly':
            lines += ['   guest ok = yes', '   read only = yes']
        else:
            lines += ['   guest ok = no', '   valid users = %s' % (
                ', '.join(v_uname(u, field=f'共享「{name}」的允许用户')
                          for u in (listify(s.get('users')) or ['drouter'])))]

        allowed = listify(s.get('allow_hosts'))
        if allowed:
            # Samba 用空格分隔，支持 192.168.7.0/24 形式
            for a in allowed:
                try:
                    ipaddress.ip_network(a, strict=False)
                except Exception:
                    raise ValidateError(f'共享「{name}」允许网段不合法：{a}', 'allow_hosts')
            lines.append('   hosts allow = %s' % ' '.join(allowed))
            lines.append('   hosts deny = 0.0.0.0/0 ::/0')
        denied = listify(s.get('deny_hosts'))
        if denied:
            lines.append('   hosts deny = %s' % ' '.join(denied))
        for extra in listify(s.get('extra')):
            lines.append(f'   {extra}')
        lines.append('')
    return '\n'.join(lines)


# NFS 客户端导出选项：按「平台模板」预置，兼顾兼容性与安全性
NFS_OPTS = {
    'linux': 'rw,sync,no_subtree_check,secure,root_squash',
    'macos': 'rw,sync,no_subtree_check,secure,all_squash,insecure',
    'windows': 'rw,sync,no_subtree_check,secure,all_squash,no_auth_nlm,insecure,anonuid=65534,anongid=65534',
    'readonly': 'ro,sync,no_subtree_check,secure,root_squash',
    'legacy': 'rw,sync,no_subtree_check,insecure,no_root_squash',
}


def render_nfs(cfg):
    """渲染 /etc/exports（NFSv4 + NFSv3 兼容）。"""
    n = _sub(cfg, 'nfs')
    lines = [
        '# 由 drouter 自动生成：NFS 导出（#9）',
        '# 只做内网共享，未列出的网段默认拒绝（由 nftables 放通 2049/111）。',
        '',
    ]
    exports = n.get('exports') or []
    if not exports or not any(v_bool(x.get('enable', True)) for x in exports):
        lines.append('# 未配置任何导出（NFS 已启用但没有共享项）')
        return '\n'.join(lines) + '\n'

    for i, e in enumerate(exports):
        if not v_bool(e.get('enable', True)):
            continue
        path = v_path(e.get('path'), field=f'第 {i + 1} 个 NFS 导出目录')
        clients = e.get('clients') or []
        if not clients:
            raise ValidateError(f'NFS 导出「{path}」至少需要一个客户端网段', 'clients')
        seen = set()
        rendered = []
        for c in clients:
            net = (c.get('net') or '').strip()
            preset = (c.get('preset') or 'linux').strip().lower()
            # 模板名写错时必须报错而不是「静默退回 linux」：no_root_squash 这类的
            # 差异会让用户以为选的是 macOS 模板，实际拿到一套完全不同的导出参数。
            if preset not in NFS_OPTS:
                raise ValidateError(
                    f'NFS 导出「{path}」的客户端模板不支持：{preset}'
                    f'（可选：{", ".join(sorted(NFS_OPTS))}）', 'preset')
            if not net:
                raise ValidateError(f'NFS 导出「{path}」存在空的客户端网段', 'clients')
            # 通配符 * 允许，其余按 CIDR / 单机地址校验
            if net != '*':
                # 支持 192.168.7.0/24 与 IPv6 前缀
                try:
                    ipaddress.ip_network(net, strict=False)
                except Exception:
                    raise ValidateError(
                        f'NFS 导出「{path}」的客户端不合法：{net}'
                        f'（应为 * 或 CIDR，如 192.168.7.0/24）', 'clients')
            if net in seen:
                continue
            seen.add(net)
            opts = (c.get('options') or '').strip() or NFS_OPTS.get(preset, NFS_OPTS['linux'])
            # 自定义选项整串会原样写进 /etc/exports 的 (…) 里：带个空格或换行
            # 就能越过当前共享项去改别的导出（exports 是按行解析的）。
            v_text(opts, field=f'NFS 导出「{path}」的选项', maxlen=200,
                   pattern=r'^[A-Za-z0-9_,=:.\-]{1,200}$')
            # 兜底：用户自定义选项里若出现危险组合，给出中文提示
            if 'no_root_squash' in opts and preset != 'legacy':
                raise ValidateError(
                    f'NFS 导出「{path}」对 {net} 使用了 no_root_squash，'
                    f'这会让客户端 root 直接以 root 身份写共享目录，存在安全风险。'
                    f'如确需如此，请把模板选为「兼容旧设备（不安全）」。', 'options')
            rendered.append(f'{net}({opts})')
        # 备注进了注释行，同样要挡换行（smb 那边早就这么做了，这里补上）。
        cmt = v_text(e.get('comment') or path, field=f'第 {i + 1} 个 NFS 导出的备注',
                     maxlen=120, allow_empty=False,
                     pattern=r'^[A-Za-z0-9\u4e00-\u9fff ._/@:-]{1,120}$')
        lines.append(f'# {cmt}')
        lines.append(f'{path} {" ".join(rendered)}')
        lines.append('')
    return '\n'.join(lines)


def render_nfs_conf(cfg):
    """渲染 /etc/nfs.conf.d/drouter.conf：线程数 / 版本 / 端口固定。"""
    n = _sub(cfg, 'nfs')
    # 原先是裸 int(threads or 8)：填个 abc 会抛 ValueError（500 + traceback），
    # 而不是「NFS 服务线程数必须是数字」这种能看懂的提示。这里只补上捕获，
    # 范围提示沿用原文案（页面上的说明写的是「1–128」）。
    try:
        threads = int(str(n.get('threads') or 8).strip())
    except Exception:
        raise ValidateError('NFS 服务线程数必须是数字', 'threads')
    if not (1 <= threads <= 128):
        raise ValidateError('NFS 服务线程数应在 1–128 之间', 'threads')
    lines = [
        '# 由 drouter 自动生成：NFS 服务端参数（#9）',
        '# 固定端口，便于在 nftables 中精确放通。',
        '',
        '[nfsd]',
        f'threads={threads}',
        'vers3=y',
        'vers4=y',
        'vers4.0=y',
        'vers4.1=y',
        'vers4.2=y',
        '# 固定端口，避免防火墙需要放通随机端口',
        'port=2049',
        '',
        '[mountd]',
        'port=20048',
        'manage-gids=y',
        '',
        '[statd]',
        'port=32765',
        'outgoing-port=32766',
        '',
        '[lockd]',
        'port=32767',
        '',
    ]
    return '\n'.join(lines)


# ---------------------------------------------------------------- 汇总渲染入口

RENDERERS = {
    'dnsmasq': lambda cfg: [('/etc/dnsmasq.d/drouter.conf', render_dnsmasq(cfg))],
    'nft_v4': lambda cfg: [('/etc/nftables.d/drouter-v4.nft', render_nft('v4', cfg))],
    'nft_v6': lambda cfg: [('/etc/nftables.d/drouter-v6.nft', render_nft('v6', cfg))],
    'network': render_network,
    'ppp': render_ppp,
    'radvd': lambda cfg: [('/etc/radvd.conf', render_radvd(cfg))],
    'dhcpv6': lambda cfg: [('/etc/dhcpcd.conf', render_dhcpcd(cfg))],
    'miniupnpd': lambda cfg: [('/etc/miniupnpd/miniupnpd.conf', render_miniupnpd(cfg))],
    'chrony': lambda cfg: [('/etc/chrony/chrony.conf', render_chrony(cfg))],
    'samba': lambda cfg: [('/etc/samba/drouter.conf', render_samba(cfg))],
    'nfs': lambda cfg: [('/etc/exports', render_nfs(cfg)),
                        ('/etc/nfs.conf.d/drouter.conf', render_nfs_conf(cfg))],
}

# 前端/API 使用的模块别名 -> 渲染模块名
ALIASES = {
    'upnp': 'miniupnpd',
    'ntp': 'chrony',
    'dhcpv6': 'dhcpv6',
    'nfs': 'nfs',
    'smb': 'samba',
    # WAN 口页面在前端叫 pppoe，渲染器叫 ppp（render_ppp 用 _sub(cfg,'pppoe')
    # 兼容平铺入参，所以别名解析后依然能拿到正确的字段）
    'pppoe': 'ppp',
}


def canonical(module):
    """把前端/API 使用的模块名解析成真正的渲染模块名。

    helper 的语法预检要按「渲染模块名」分支（chrony / miniupnpd …），
    如果直接拿前端传来的别名（ntp / upnp）去比，会静默跳过语法检查。
    """
    return ALIASES.get(module, module)


def render(module, cfg):
    """返回 [(path, content)]；校验失败抛 ValidateError（含中文提示）"""
    module = ALIASES.get(module, module)
    if module not in RENDERERS:
        raise ValidateError(f'未知的配置模块：{module}')
    return RENDERERS[module](cfg)


if __name__ == '__main__':
    # 简易自测：渲染默认配置，确认无语法异常
    demo = {
        'lan_iface': 'br0', 'domain': 'lan',
        'dhcp_enabled': True, 'pool_start': '192.168.7.100', 'pool_end': '192.168.7.200',
        'pool_netmask': '255.255.255.0', 'lease_time': 7200,
        'option_dns': '223.5.5.5,119.29.29.29',
        'options': [{'enabled': True, 'code': 3, 'value': '192.168.7.1', 'force': True}],
        'dns_mode': 'custom', 'dns_custom': '223.5.5.5',
        'wan_iface': 'ppp0', 'lan_ifaces': ['br0'], 'mss_clamp': True,
    }
    print(render_dnsmasq(demo))
    print('---- nft v4 ----')
    print(render_nft('v4', demo))
    print('SELFTEST OK')
