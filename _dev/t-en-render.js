// 离线视图渲染回归：用最小 DOM 桩 + 假 fetch，把所有视图函数都跑一遍，
// 检查是否抛异常、是否渲染出内容。不依赖真实路由器。
// 用法：node devtools/check-render.js web/app.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// 以脚本自身位置定位，避免依赖运行时的当前目录（否则换个目录跑就 ENOENT）。
const DEFAULT_APP = path.join(__dirname, '..', 'web', 'app.js');
const file = process.argv[2] || DEFAULT_APP;
const code = fs.readFileSync(file, 'utf8');

function mkEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', disabled: false, checked: false,
    className: '', tagName: 'DIV', dataset: {}, style: {}, children: [],
    classList: {
      add(c) { el.className += ' ' + c; },
      remove(c) { el.className = el.className.replace(c, ''); },
      toggle() { return false; }, contains() { return false; },
    },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { el.children.push(c); },
    removeChild() {},
    querySelector() { return mkEl('sub'); },
    querySelectorAll() { return []; },
    get parentElement() { return mkEl('parent'); },
    get firstChild() { return mkEl('first'); },
    insertBefore() {}, replaceChild() {}, cloneNode() { return mkEl('clone'); },
    focus() {}, select() {}, scrollIntoView() {}, click() {},
    onclick: null, onchange: null, oninput: null, onkeydown: null,
    getContext() {
      // canvas 2d 桩：realtime.js 用 canvas 画实时折线图。
      // 早先这里返回 {}，一调 setTransform 就 TypeError，把整个检查器带崩
      // （2026-10-09 接独立模块时踩到）。
      return {
        setTransform() {}, clearRect() {}, beginPath() {}, closePath() {},
        moveTo() {}, lineTo() {}, stroke() {}, fill() {}, fillRect() {},
        fillText() {}, strokeRect() {}, save() {}, restore() {}, translate() {},
        scale() {}, arc() {}, measureText(t) { return { width: String(t == null ? '' : t).length * 7 }; },
        fillStyle: '', strokeStyle: '', lineWidth: 1, font: '', textAlign: '',
        textBaseline: '', globalAlpha: 1,
      };
    },
  };
  return el;
}

const ELS = new Map();
function getEl(sel) {
  const k = String(sel).replace(/^#/, '').split(/[\s>]/)[0];
  if (!ELS.has(k)) ELS.set(k, mkEl(k));
  return ELS.get(k);
}
// 新建元素也必须登记进 ELS：否则「createElement → innerHTML → appendChild」这条
// 渲染路径对扫描器完全不可见（第二类结构性盲区，和动态子块那次同源）。
let EL_SEQ = 0;
function mkTracked(tag) {
  const e = mkEl('new:' + tag + ':' + (++EL_SEQ));
  ELS.set(e.id, e);
  return e;
}

const document = {
  getElementById: id => getEl(id),
  querySelector: getEl,
  querySelectorAll: () => [],
  createElement: t => mkTracked(t),
  body: mkEl('body'),
  addEventListener() {},
};

/* 向导专用的 probe 数据桩。
 * 字段名与真机 helper._wiz_probe() 一致（不能凭印象填），
 * 中文值就是真机下发的那些——正是要被 i18n 翻掉的那些。
 * 同时故意给满值（非空字符串），不然清单路径不会被走到。*/
const WIZ_STEPS_FIXTURE = [
  { k: 'wan', n: '第 1 步 · 接上外网', icon: '⇄', d: '告诉这台机器「从哪里接宽带」，并且确认它真的连上了。' },
  { k: 'lan', n: '第 2 步 · 给内网发地址', icon: '⌘', d: '让手机、电脑插上网线或连上 Wi-Fi 就能自动拿到 IP，不用手工设置。' },
  { k: 'dns', n: '第 3 步 · 能打开网址', icon: '⌖', d: '域名要翻译成 IP 才能访问。' },
  { k: 'v6', n: '第 4 步 · 要不要 IPv6', icon: '⁶', d: 'IPv6 是新一代地址。' },
];
const WIZ_WAN_MODES_FIXTURE = [
  { k: 'pppoe', n: 'PPPoE 拨号',
    d: '需要宽带账号和密码，由本机发起拨号。',
    when: '光猫已设为桥接，或者你打算让本机取代运营商光猫拨号。' },
  { k: 'dhcp', n: 'DHCP 自动获取（IPoE）', d: '网线插上就有地址，不需要账号密码。', when: '光猫已经在拨号。' },
  { k: 'static', n: '静态地址', d: '运营商给了固定的 IP、网关和 DNS，手工填进去。', when: '企业专线、固定 IP 宽带。' },
];
const WIZ_DNS_PRESETS_FIXTURE = [
  { k: 'ali', n: '阿里云 AliDNS', v4: '223.5.5.5,223.6.6.6', d: '国内解析最快、覆盖最广。' },
  { k: 'dnspod', n: '腾讯 DNSPod', v4: '119.29.29.29,182.254.116.116', d: '国内老牌。' },
  { k: 'isp', n: '跟随运营商下发', v4: '', d: '用宽带拨号时运营商给的 DNS。' },
];
const WIZ_POOL_HINT_FIXTURE = {
  why: '内网设备的地址由这里开始分配。填错了轻则上不了网，重则和上游路由器打架。',
  rules: [
    '前三段必须和本机 LAN 地址一致，最后一段落在 2–254 之间。',
    '起始–结束这段不要包含本机自己的地址（否则会和本机抢 IP）。',
    '常见做法是让池子从 .100 开始，把 .2–.99 留给打印机、NAS 这类固定设备。',
  ],
};

function WIZ_PROBE_FIXTURE() {
  return {
    steps: WIZ_STEPS_FIXTURE,
    wan: { mode: 'pppoe', mode_cn: 'PPPoE 拨号', iface: 'ens19', has_wan_iface: true,
            has_account: false, default_route: '', has_default_route: false, online: false,
            ping_note: '2 packets transmitted, 2 received, 0% packet loss, time 1001ms' },
    lan: { iface: 'ens18', lan_ip: '192.168.7.3', dhcp_enabled: false,
           pool_start: '192.168.7.100', pool_end: '192.168.7.200',
           pool_netmask: '255.255.255.0', lease_time: 7200,
           gateway: '192.168.7.3', dns_option: '192.168.7.3',
           pool_problems: ['还没给 LAN 口配 IP 地址',
                           '地址池把本机自己的地址（%8%）也包含进去了'],
           dnsmasq_active: false, ok: false },
    dns: { mode: 'isp', mode_cn: '仅运营商下发', custom: '',
           first: '', probe_ok: false,
           probe_note: '选了「跟随运营商」，但当前还没拿到运营商下发的 DNS',
           presets: WIZ_DNS_PRESETS_FIXTURE,
           default_custom: '223.5.5.5,119.29.29.29', ok: false },
    v6: { lan_iface: 'ens18', wan_iface: 'ens19', lan_has_global: false,
          wan_has_global: false, forwarding: false, radvd_active: false,
          radvd_installed: false, prefix: '', rdnss: '', advice: '建议开启', ok: false },
    wan_modes: WIZ_WAN_MODES_FIXTURE,
    pool_hint: WIZ_POOL_HINT_FIXTURE,
    dns_presets: WIZ_DNS_PRESETS_FIXTURE,
    internet_ok: false,
    todo: ['第 1 步：外网还没通', '第 2 步：内网还没在发地址',
           '第 3 步：DNS 还解析不出结果', '第 4 步：IPv6 还没启用（可选）'],
    build_mode: false,
    lan_ifaces: [{ name: 'ens18', mac: 'aa:bb:cc:dd:ee:01', state: 'UP', role: 'lan' }],
    wan_ifaces: [{ name: 'ens19', mac: 'aa:bb:cc:dd:ee:02', state: 'UP', role: 'wan' }],
    lease_default: 7200, dns_cache_default: 1000,
  };
}

// 假 fetch：所有 GET 返回一个宽松的 ok 响应
/* 真机实测数据（_real_fixtures.js，由 _analyze_real.py 从 192.168.7.3 抓取生成）。
 * 为什么必须用它：**不要照后端源码臆造字段** —— 我曾臆造过 sysinfo.temp.src /
 * accel.hw_note 等真机上根本不存在的字段，判据看着全绿，真机实际一堆中文。
 * 这里 require 进来当桩，字段名/中文文案/层级与真机完全一致。*/
const REAL = require('../_real_fixtures.js');
const REAL_F = {
  qos: REAL.REAL_QOS, acl: REAL.REAL_ACL, storage: REAL.REAL_STORAGE,
  opensoho: REAL.REAL_OPENSOHO, share: REAL.REAL_SHARE, docker: REAL.REAL_DOCKER,
  deps: REAL.REAL_DEPS_CHECK, dpi: REAL.REAL_DPI, wol: REAL.REAL_WOL,
  accel: REAL.REAL_ACCEL, sysinfo: REAL.REAL_SYSINFO, natlast: REAL.REAL_NAT_LAST,
  pppmulti: REAL.REAL_PPPMULTI, vlan: REAL.REAL_VLAN, wanlog: REAL.REAL_WANLOG,
  ulog: REAL.REAL_ULOG, audit: REAL.REAL_AUDIT,
  depscheck: REAL.REAL_DEPS_CHECK, logs: REAL.REAL_LOGS,
  apiex: REAL.REAL_API_EX, apipaths: REAL.REAL_API_PATHS,
  fwlog: REAL.REAL_FWLOG,
  upstream: REAL.REAL_UPSTREAM, netdetail: REAL.REAL_NETDETAIL,
  metrics: REAL.REAL_METRICS,
  updateCheck: REAL.REAL_UPDATE_CHECK, updateState: REAL.REAL_UPDATE_STATE,
  updateCheckFail: REAL.REAL_UPDATE_CHECK_FAIL,
  status: REAL.REAL_STATUS, statusBad: REAL.REAL_STATUS_BAD,
};

// ★ 真机实测数据（_real_api_more.json）：目标机上直调 helper.run_action 抓取的
//   52 个只读接口真实 payload。为什么必须覆盖手写桩：桩里有一批**英文臆造值**
//   （主题描述写死 'Theme preview'、主题变量 cn 写成变量名、/api/ifaces 给 []），
//   渲染出来的 HTML 是英文 —— 扫描器全绿，真机英文界面却全是中文。
//   假绿的根因就是桩不像真机，所以这里一律真机数据优先。
const MORE = require('../_real_api_more.json');
const URL2REAL = [
  [/^\/api\/sysinfo$/, 'read:sysinfo'], [/^\/api\/metrics$/, 'read:metrics'],
  [/^\/api\/upstream$/, 'read:upstream'],
  [/^\/api\/services$/, 'read:services'], [/^\/api\/ifaces$/, 'read:ifaces'],
  [/^\/api\/ipv6$/, 'read:ipv6'], [/^\/api\/routes$/, 'read:routes'],
  [/^\/api\/netdetail$/, 'read:netdetail'], [/^\/api\/ntp$/, 'read:ntp'],
  [/^\/api\/ddns$/, 'read:ddns'], [/^\/api\/acl$/, 'read:acl'],
  [/^\/api\/share$/, 'read:share'], [/^\/api\/docker$/, 'read:docker'],
  [/^\/api\/ulog\/conf$/, 'read:ulog_conf'], [/^\/api\/ulog\/flow$/, 'read:ulog_flow'],
  [/^\/api\/ulog\/daemon$/, 'read:logd'], [/^\/api\/ulog$/, 'read:ulog'],
  [/^\/api\/backupd$/, 'read:backupd'], [/^\/api\/backup$/, 'backup/status'],
  [/^\/api\/alert$/, 'alert/status'], [/^\/api\/quotad$/, 'read:quotad'],
  [/^\/api\/nat\/last$/, 'read:nat_last'], [/^\/api\/fw\/log$/, 'read:fw_log'],
  [/^\/api\/users$/, 'read:users'], [/^\/api\/logs$/, 'read:logs'],
  [/^\/api\/journal$/, 'read:journal'], [/^\/api\/leases$/, 'read:leases'],
  [/^\/api\/nft$/, 'read:nft'], [/^\/api\/iface_method$/, 'read:iface_method'],
  [/^\/api\/ppp\/log$/, 'read:ppp_log'], [/^\/api\/wan\/log$/, 'read:wan_log'],
  [/^\/api\/cleanup$/, 'cleanup/status'], [/^\/api\/kern$/, 'kern/status'],
  [/^\/api\/ca$/, 'ca/status'], [/^\/api\/dpi$/, 'dpi/get'],
  [/^\/api\/qos$/, 'qos/get'], [/^\/api\/vlan$/, 'vlan/get'],
  [/^\/api\/wol$/, 'wol/get'], [/^\/api\/accel$/, 'accel/get'],
  [/^\/api\/pppoe-multi$/, 'pppoe_multi/get'],
  [/^\/api\/opensoho$/, 'opensoho/status'], [/^\/api\/print$/, 'print/status'],
  [/^\/api\/dcfg$/, 'dcfg/status'], [/^\/api\/storage$/, 'storage/get'],
  [/^\/api\/deps\/check$/, 'depcheck/check'], [/^\/api\/snapshots$/, 'snapshot_list'],
  // pubip 不覆盖：前端 op=check 的形状（verdict/evidence 带 en_* 字段）与
  // probe_status 不同，现有桩已经是真机形状，换成 probe_status 会让页面渲染不到内容。
];

const CALLED = [];
async function fetchStub(url, opts) {
  CALLED.push({ url, method: (opts && opts.method) || 'GET' });
  let data = { ok: true, data: {}, msg_cn: 'ok' };
  // 真机数据优先：命中 URL2REAL 就用真机 payload，否则沿用下面的手写桩
  const _u = String(url).split('?')[0];
  const _qs = String(url).split('?')[1] || '';
  let _real = false;
  if (_u === '/api/theme') {
    const _op = (/op=([a-z_]+)/.exec(_qs) || [null, 'list'])[1];
    const _k = 'read:theme/' + _op;
    if (MORE[_k]) { data = MORE[_k]; _real = true; }
  } else {
    for (const _e of URL2REAL) {
      if (_e[0].test(_u) && MORE[_e[1]]) { data = MORE[_e[1]]; _real = true; break; }
    }
  }
  // ⚠ 命中真机数据必须**立刻返回**：下面那一长串手写桩 if/else 里有同 URL 的分支
  // （尤其 /api/theme 的英文臆造主题），不早返回就会被它覆盖回去，扫描器继续假绿。
  if (_real) return { status: 200, ok: true, json: async () => data };
  // 各接口按需返回形状合理的假数据，避免视图因字段类型不符而抛异常
  if (/\/api\/ifaces/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/iface\/meta/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/iface_method/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/ddns/.test(url)) data = { ok: true, data: {
    cfg: { provider: 'custom', domain: '', ipv4: true, ipv6: false },
    public: { combo: 'both', v4: '1.2.3.4', v6: '2408::1', note: '' },
    note: { level: 'ok', text: '' },
    providers: [{ v: 'custom', name: '自定义', region: 'other', fields: [] }],
    records: [], last_result: '' } };
  else if (/\/api\/ulog\/daemon/.test(url)) data = { ok: true, data: { units: { 'drouter-logd.timer': { active: 'active', enabled: 'enabled' } }, next: 'in 42s', last: { ts: '2026-09-28T22:00:00', msg_cn: '已归档 12 条日志', code: 'ULOG_ARCHIVE_OK' } } };
  else if (/\/api\/ulog\/daemon/.test(url)) data = { ok: true, data: {
    units: { 'drouter-logd.timer': { active: 'active', enabled: 'enabled' } },
    next: 'in 42s',
    last: { ts: '2026-09-28T22:00:00', msg_cn: '已归档 12 条日志', code: 'ULOG_ARCHIVE_OK' } } };
  else if (/\/api\/ulog\/conf/.test(url)) data = { ok: true, data: {
    conf: { enabled: true, archive: true, keep_days: 7, keep_rows: 200000,
            min_level: 'debug', sources: { fw: true, conntrack: true, flow: true,
              wan: true, ddns: true, app: true, system: true } },
    caps: { conntrack: true, journalctl: true },
    conntrack: { enabled: true, count: 120, max: 65536 }, fw_log: true,
    sources: [{ v: 'fw', n: '防火墙', desc: '' }, { v: 'conntrack', n: '连接跟踪', desc: '' },
              { v: 'flow', n: '当前连接', desc: '' }, { v: 'wan', n: 'WAN 接入', desc: '' },
              { v: 'ddns', n: '动态域名', desc: '' }, { v: 'app', n: '应用日志', desc: '' },
              { v: 'system', n: '系统日志', desc: '' }],
    levels: [{ v: 'err', n: '错误' }, { v: 'warn', n: '警告' }, { v: 'info', n: '信息' }],
    archive: { path: '/var/log/drouter/ulog.jsonl', size: 20480, rows: 128 } } };
  else if (/\/api\/ulog\/flow/.test(url)) data = { ok: true, data: {
    rows: [{ ts: '', src: 'flow', level: 'info', action: 'ORIG', proto: 'TCP',
             saddr: '192.168.7.100', sport: '51234', daddr: '1.1.1.1', dport: '443',
             msg_cn: 'TCP 连接', extra: { state: 'ESTABLISHED', state_cn: '已建立',
             bytes: 20480, reply: false } }],
    stat: { total: 1, tcp: 1, udp: 0, icmp: 0, other: 0 },
    cap: { count: 120, max: 65536 }, err: '', note: '快照' } };
  else if (/\/api\/ulog/.test(url)) data = { ok: true, data: {
    items: [{ ts: '2026-09-28T10:00:00', src: 'fw', level: 'warn', level_cn: '警告',
              action: 'DROP', proto: 'TCP', saddr: '203.0.113.9', sport: '54321',
              daddr: '192.168.7.3', dport: '22', iface: 'ens19', msg_cn: 'Dropped SSH',
              raw: 'DROUTER-FW DROP ...' }],
    stat: { total: 1, by_src: { fw: 1 }, by_level: { warn: 1 }, by_action: { DROP: 1 },
            by_proto: { TCP: 1 }, top_src_ip: [{ v: '203.0.113.9', n: 1 }],
            top_dst_port: [{ v: '22', n: 1 }] },
    meta: { sources_used: ['fw'], collected: 1, matched: 1, shown: 1, errors: {} },
    conf: { enabled: true, archive: true, keep_days: 7, keep_rows: 200000,
            min_level: 'debug', sources: { fw: true } },
    sources: [{ v: 'fw', n: '防火墙', desc: '' }],
    levels: [{ v: 'err', n: '错误' }, { v: 'warn', n: '警告' }, { v: 'info', n: '信息' }],
    archive: { path: '/var/log/drouter/ulog.jsonl', size: 20480, rows: 128 },
    note: '统一日志' } };
  else if (/\/api\/fflow/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/metrics/.test(url)) data = { ok: true, data: { cpu: 3, mem: 21, disk: 12, up: 0, down: 0 } };
  else if (/\/api\/dash|sysinfo/.test(url)) data = { ok: true, data: {} };
  else if (/\/api\/users/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/leases/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/routes/.test(url)) data = { ok: true, data: [] };
  // 主题之家（#12）：list / vars / get 三种 op
  const TM_VARS_ALL = ['--bg', '--bg2', '--panel', '--panel2', '--line', '--line2',
    '--txt', '--txt2', '--txt3', '--pri', '--pri-d', '--pri-l', '--ok', '--ok-l',
    '--warn', '--warn-l', '--err', '--err-l', '--info', '--info-l',
    '--r', '--sh', '--grad', '--side', '--side-c'];
  function mkTheme(id, name, builtin, dark, pri, bg) {
    const sw = [pri, bg, '#ffffff', '#161f30'];
    return { id, name, description: 'Theme preview', author: builtin ? 'Drouter Built-in' : 'tester',
      version: '1.0', dark, builtin, vars_count: 25, has_css: false, swatch: sw,
      vars: { '--pri': pri, '--bg': bg } };
  }
  if (/\/api\/theme/.test(url)) {
    const op = /op=([a-z_]+)/.exec(url);
    const which = op ? op[1] : 'list';
    if (which === 'vars') {
      const defaults = {};
      TM_VARS_ALL.forEach(k => { defaults[k] = k.startsWith('--side') ? '236px' : '#1f6feb'; });
      data = { ok: true, data: {
        vars: TM_VARS_ALL.map(k => ({ name: k, cat: 'base', cat_cn: '基础配色', cn: k })),
        cats: [{ k: 'base', n: '基础配色' }, { k: 'color', n: '主色与状态色' },
               { k: 'shape', n: '圆角 / 阴影 / 渐变' }, { k: 'layout', n: '布局尺寸' }],
        defaults } };
    } else if (which === 'get') {
      data = { ok: true, data: { id: 'teal', name: '青竹', description: 'd', author: 'x',
        version: '1.0', dark: false, css: '', css_text: ':root{--pri:#0d9488}',
        vars: { '--pri': '#0d9488', '--bg': '#f2fbf9' } } };
    } else {
      data = { ok: true, data: {
        themes: [
          mkTheme('default', 'Drouter 经典蓝', true, false, '#1f6feb', '#eef2f9'),
          mkTheme('teal', '青竹', true, false, '#0d9488', '#f2fbf9'),
          mkTheme('violet', '紫罗兰', true, false, '#7c3aed', '#f6f4fe'),
          mkTheme('dark-green', '暗夜墨绿', true, true, '#0f766e', '#111827'),
          mkTheme('warm', '暖阳', true, false, '#f97316', '#fff8f2'),
          mkTheme('dark-rose', '暗夜绯红', true, true, '#e11d48', '#150d14'),
          mkTheme('my-theme', 'My Custom Theme', false, false, '#3366cc', '#f7f9fc'),
        ],
        active: 'my-theme', active_name: 'My Custom Theme',
        vars_total: 25, dir: '/etc/drouter/themes',
        css_path: '/etc/drouter/generated/theme.css' } };
    }
  }
  else if (/\/api\/services/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/snapshot\/list/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/config/.test(url)) data = { ok: true, data: {} };
  else if (/\/api\/wizard/.test(url)) data = { ok: true, data: WIZ_PROBE_FIXTURE(), msg_cn: '体检完成' };
  // ---- 真机实测数据（字段与中文全部来自 192.168.7.3 的真实响应）----
  // 后端下发的中文全在这些字段里，判据必须能看到它们，否则「0 残留中文」是假绿。
  else if (/\/api\/accel/.test(url)) data = { ok: true, data: REAL_F.accel };
  else if (/\/api\/wol/.test(url)) data = { ok: true, data: REAL_F.wol };
  else if (/\/api\/sysinfo|dash|sysinfo/.test(url)) data = { ok: true, data: REAL_F.sysinfo };
  else if (/\/api\/vlan/.test(url)) data = { ok: true, data: REAL_F.vlan };
  else if (/\/api\/wan\/log/.test(url)) data = { ok: true, data: REAL_F.wanlog };
  else if (/\/api\/pppoe-multi/.test(url)) data = { ok: true, data: REAL_F.pppmulti };
  else if (/\/api\/nat\/(last|check)/.test(url)) data = { ok: true, data: REAL_F.natlast };
  else if (/\/api\/acl/.test(url)) data = { ok: true, data: REAL_F.acl };
  else if (/\/api\/qos/.test(url)) data = { ok: true, data: REAL_F.qos };
  else if (/\/api\/share/.test(url)) data = { ok: true, data: REAL_F.share };
  else if (/\/api\/storage/.test(url)) data = { ok: true, data: REAL_F.storage };
  else if (/\/api\/opensoho/.test(url)) data = { ok: true, data: REAL_F.opensoho };
  else if (/\/api\/examples/.test(url)) data = { ok: true, data: REAL_F.apiex };
  else if (/\/api\/openapi/.test(url)) data = { ok: true, data: REAL_F.apipaths };
  else if (/\/api\/docker/.test(url)) data = { ok: true, data: Object.assign(
    // 真机 /api/docker 带 note（helper L7772），旧桩漏了 → docker 页假绿
    { note: 'Docker 面板支持容器/镜像/网络/卷的日常运维，以及 Compose 项目管理。「命令转换」可以把任意 docker run 命令转成 docker-compose.yml。' },
    REAL_F.docker || {}) };
  else if (/\/api\/deps\/check/.test(url)) data = { ok: true, data: REAL_F.depscheck };
  else if (/\/api\/dpi/.test(url)) data = { ok: true, data: REAL_F.dpi };
  else if (/\/api\/ulog/.test(url)) data = { ok: true, data: REAL_F.ulog };
  else if (/\/api\/journal/.test(url)) data = { ok: true, data: REAL_F.ulog };
  else if (/\/api\/logs/.test(url)) data = { ok: true, data: REAL_F.logs };
  else if (/\/api\/alert/.test(url)) data = { ok: true, data: {
    conf: { enabled: true, interval_sec: 300, cooldown_min: 30, quiet_from: '', quiet_to: '',
      rules: { wan_down: 90, disk_full: 85, temp_high: 80, mem_high: 92, load_high: 3,
        loss_high: 30, backup_fail: 2, snapshot_fail: 3 },
      channels: [] },
    rules: [
      { key: 'wan_down', name: '\u5916\u7f51\u8fde\u63a5\u4e2d\u65ad', lv: 'critical', unit: '\u79d2', value: 90, default: 90, min: 30, max: 3600, step: 1, why: '\u8fde\u7eed\u8fd9\u4e48\u4e45\u62ff\u4e0d\u5230\u7f51\u5173\u56de\u5e94\u624d\u5224\u5b9a\u4e3a\u6389\u7ebf\u3002', firing: false },
      { key: 'disk_full', name: '\u78c1\u76d8\u5360\u7528\u8fc7\u9ad8', lv: 'warn', unit: '%', value: 85, default: 85, min: 50, max: 99, step: 1, why: '\u65e5\u5fd7\u3001\u5feb\u7167\u3001\u5907\u4efd\u90fd\u5728\u5f80\u6839\u5206\u533a\u5199\u3002', firing: false },
      { key: 'temp_high', name: 'CPU \u6e29\u5ea6\u8fc7\u9ad8', lv: 'warn', unit: '\u2103', value: 80, default: 80, min: 50, max: 110, step: 1, why: 'x86 \u5c0f\u4e3b\u673a\u6563\u70ed\u4e00\u822c\uff0c\u8d85\u8fc7 80\u2103\u5c31\u8be5\u68c0\u67e5\u98ce\u6247\u548c\u673a\u7bb1\u79ef\u7070\u3002', firing: false },
      { key: 'mem_high', name: '\u5185\u5b58\u5360\u7528\u8fc7\u9ad8', lv: 'warn', unit: '%', value: 92, default: 92, min: 60, max: 99, step: 1, why: '\u8fd9\u53f0\u673a\u5668\u53ea\u6709 4GB\uff0c\u53ef\u7528\u5185\u5b58\u8017\u5c3d\u65f6 dnsmasq \u4e0e Web \u9762\u677f\u4f1a\u5148\u6302\u3002', firing: false },
      { key: 'load_high', name: '\u8d1f\u8f7d\u6301\u7eed\u504f\u9ad8', lv: 'info', unit: '', value: 3, default: 3, min: 1, max: 32, step: 0.1, why: '2 \u6838\u673a\u5668\u4e0a\u8d1f\u8f7d\u957f\u671f\u8d85\u8fc7 3 \u8bf4\u660e\u6709\u4efb\u52a1\u5728\u5fd9\u3002', firing: false },
      { key: 'loss_high', name: '\u5230\u7f51\u5173\u4e22\u5305\u4e25\u91cd', lv: 'warn', unit: '%', value: 30, default: 30, min: 5, max: 100, step: 1, why: 'ping \u4e22\u5305\u9ad8\u901a\u5e38\u610f\u5473\u7740\u7f51\u7ebf\u3001\u7f51\u53e3\u534f\u5546\u6216 Wi-Fi \u4fe1\u53f7\u95ee\u9898\u3002', firing: false },
      { key: 'backup_fail', name: '\u81ea\u52a8\u5907\u4efd\u8fde\u7eed\u5931\u8d25', lv: 'warn', unit: '\u6b21', value: 2, default: 2, min: 1, max: 20, step: 1, why: '\u5907\u4efd\u5931\u8d25\u4e00\u6b21\u53ef\u80fd\u662f\u78c1\u76d8\u6b63\u597d\u6ee1\u4e86\uff0c\u8fde\u7eed\u5931\u8d25\u8bf4\u660e\u8bbe\u7f6e\u672c\u8eab\u6709\u95ee\u9898\u3002', firing: false },
      { key: 'snapshot_fail', name: '\u81ea\u52a8\u5feb\u7167\u8fde\u7eed\u5931\u8d25', lv: 'info', unit: '\u6b21', value: 3, default: 3, min: 1, max: 20, step: 1, why: '\u5feb\u7167\u662f\u4f60\u300c\u6539\u574f\u4e86\u80fd\u9000\u56de\u53bb\u300d\u7684\u5e95\u5ea7\u3002', firing: false },
    ],
    channel_types: [], units: {}, next: '', last: {},
    state: { ts: '', firing: {}, probes: {} },
    quiet_now: false, enabled_ch: 0, history: []
  } };
  else if (/\/api\/upstream/.test(url)) data = { ok: true, data: REAL_F.upstream };
  else if (/\/api\/netdetail/.test(url)) data = { ok: true, data: REAL_F.netdetail };
  else if (/\/api\/status/.test(url)) data = { ok: true,
    data: (sandbox.__stOverride || REAL_F.status) };
  else if (/\/api\/metrics/.test(url)) data = { ok: true, data: REAL_F.metrics };
  else if (/\/api\/update\/check/.test(url)) data = { ok: true,
    data: (sandbox.__updCheckOverride || REAL_F.updateCheck) };
  else if (/\/api\/update\/state/.test(url)) data = { ok: true, data: REAL_F.updateState };
  else if (/\/api\/audit/.test(url)) data = { ok: true, data: REAL_F.audit };
  else if (/\/api\/fw\/log|\/api\/fwlog/.test(url)) data = { ok: true, data: REAL_F.fwlog || {} };
  else if (/\/api\/wizard/.test(url)) data = { ok: true, data: WIZ_PROBE_FIXTURE(), msg_cn: '体检完成' };
  // ---- 下面这些桩按真机实际下发内容构造（含后端下发的中文）----
  // ⚠️ 之前这些接口走默认 {ok:true,data:{}}，于是后端下发的中文
  //    （NAT 类型名、WOL 说明、温度提示…）在判据里**根本没被渲染**，
  //    「英文残留中文 0」是假绿（2026-10-07 真机实测 539 处）。
  else if (/\/api\/nat\/(last|check)/.test(url)) data = { ok: true, data: {
    nat: 'NAT2', title: 'NAT2 \u00b7 \u5730\u5740\u53d7\u9650\u9525\u5f62\uff08Restricted Cone\uff09',
    desc: '\u516c\u7f51\u5730\u5740\u56fa\u5b9a\u3001\u6620\u5c04\u7aef\u53e3\u968f\u76ee\u6807\u53d8\u5316\u3002\u53ea\u8981\u4f60\u5148\u4e3b\u52a8\u8bbf\u95ee\u8fc7\u5bf9\u65b9\uff0c\u5bf9\u65b9\u5c31\u80fd\u56de\u8fde\u4f60\u3002\u65e5\u5e38\u4e0a\u7f51\u3001\u6e38\u620f\u8054\u673a\u57fa\u672c\u591f\u7528\u3002',
    detail: { public_ip: '101.70.135.90', public_port: '41234',
              same_ip: true, same_port: false, local_ip: '192.168.7.3',
              probes: [{ server: 'stun.aliyun.com', ip: '101.70.135.90', port: '41234' },
                       { server: 'stun.l.google.com', ip: '101.70.135.90', port: '56789' }] },
    checked_at: '2026-09-29 13:14:26' }, msg_cn: '\u68c0\u6d4b\u5b8c\u6210' };

  else if (/\/api\/accel/.test(url)) data = { ok: true, data: {
    on: false, hw: false, offloaded: 0,
    hw_note: '\u5f53\u524d\u7f51\u5361\u672a\u62a5\u544a\u652f\u6301\u786c\u4ef6\u5378\u8f7d\uff0c\u5c06\u4f7f\u7528\u8f6f\u4ef6\u5feb\u901f\u8def\u5f84\uff08\u5bf9\u5bb6\u7528\u573a\u666f\u5df2\u8db3\u591f\uff09\u3002',
    scope: '\u5f53\u524d\u4f5c\u7528\u8303\u56f4\uff1aIPv4',
    nft: '', v4: 'ip protocol { tcp, udp } ct state established,related flow add @f',
    v6: 'ip6 nexthdr { tcp, udp, sctp, dccp, udplite } ct state established,related flow add @f',
    impact: { note: '' } }, msg_cn: '\u6210\u529f' };

  else if (/\/api\/wol/.test(url)) data = { ok: true, data: [
    { name: 'ens18', mac: 'aa:bb:cc:dd:ee:01', supports_wol: false,
      note: '\u8be5\u7f51\u5361\u6216\u9a71\u52a8\u672a\u4e0a\u62a5 WOL \u80fd\u529b', enabled: false },
    { name: 'ens19', mac: 'aa:bb:cc:dd:ee:02', supports_wol: false,
      note: '\u8be5\u7f51\u5361\u6216\u9a71\u52a8\u672a\u4e0a\u62a5 WOL \u80fd\u529b', enabled: false }] };

  else if (/\/api\/sysinfo/.test(url)) data = { ok: true, data: {
    hostname: 'debian-primaryrouter', uptime: 123456, virt: { name: 'QEMU / KVM \u865a\u62df\u673a' },
    temp: { c: null, src: '\u672a\u8bfb\u53d6\u5230\u6e29\u5ea6\u4f20\u611f\u5668\uff0c\u4ec5\u663e\u793a\u8fdb\u7a0b\u6570' },
    procs: 178,
    virt_note: '\u5f53\u524d\u8fd0\u884c\u5728 QEMU / KVM \u865a\u62df\u673a \u4e2d\uff0c\u865a\u62df\u5316\u5c42\u9ed8\u8ba4\u4e0d\u4f1a\u628a\u5bbf\u4e3b\u673a CPU \u6e29\u5ea6\u4f20\u611f\u5668\u66b4\u9732\u7ed9\u5ba2\u6237\u673a\uff0c\u56e0\u6b64\u6e29\u5ea6\u65e0\u6cd5\u8bfb\u53d6\u3002' } };

  else if (/\/api\/vlan/.test(url)) data = { ok: true, data: {
    vlans: [], ifaces: [{ name: 'ens18', role: 'lan' }],
    presets: [
      { name: '\u4e2d\u56fd\u7535\u4fe1 IPTV', vid: 85 },
      { name: '\u4e2d\u56fd\u8054\u901a IPTV', vid: 3961 },
      { name: '\u4e2d\u56fd\u79fb\u52a8 IPTV', vid: 3961 },
      { name: '\u901a\u7528 VLAN', vid: 100 }] } };

  else if (/\/api\/wan\/log/.test(url)) data = { ok: true, data: {
    mode: 'pppoe', mode_hint: '\u5149\u732b\u6865\u63a5 + \u7535\u8111/\u8def\u7531\u62e8\u53f7\uff0c\u9700\u8981\u5bbd\u5e26\u8d26\u53f7\u5bc6\u7801\uff08\u7535\u4fe1/\u8054\u901a/\u79fb\u52a8/\u5e7f\u7535\u5747\u5e38\u89c1\uff09',
    iface: 'ens19', dial_state: '\u672a\u62e8\u53f7\uff08\u62e8\u53f7\u8fdb\u7a0b\u672a\u8fd0\u884c\uff09',
    lines: [] } };

  else if (/\/api\/pppoe-multi/.test(url)) data = { ok: true, data: {
    sessions: [], presets: [],
    notes: ['\u591a\u62e8\u80fd\u5426\u6210\u529f\u53d6\u51b3\u4e8e\u8fd0\u8425\u5546\u662f\u5426\u5141\u8bb8\u540c\u4e00\u8d26\u53f7\u5e76\u53d1\u62e8\u53f7\uff08\u90e8\u5206\u7701\u4efd\u9650\u5236\u4e3a 1 \u6761\uff09\u3002',
            '\u5c1d\u8bd5 N \u4e2a\u4f1a\u8bdd\u524d\uff0c\u5efa\u8bae\u5148\u53ea\u7528 1 \u6761\u786e\u8ba4\u8d26\u53f7\u53ef\u6b63\u5e38\u62e8\u901a\u3002',
            '\u82e5\u8fd0\u8425\u5546\u62d2\u7edd\u5e76\u53d1\uff0c\u4f1a\u8bdd\u4f1a\u53cd\u590d\u91cd\u62e8\u5e76\u5728\u8fd9\u9875\u663e\u793a\u4e3a\u300c\u672a\u8fde\u63a5\u300d\uff0c\u5c5e\u6b63\u5e38\u73b0\u8c61\u3002',
            '\u591a\u62e8\u4f1a\u6539\u53d8\u51fa\u7f51\u8def\u7531\uff0c\u8bf7\u786e\u8ba4\u5df2\u586b\u597d\u8d26\u53f7\u5e76\u663e\u5f0f\u70b9\u51fb\u300c\u5e94\u7528\u5e76\u8fde\u63a5\u300d\u3002'] } };

  else if (/\/api\/pubip/.test(url)) data = { ok: true, data: {
    ip: '101.70.135.90', v6: '2408:8240:5416:ad61:acf0:c128:bf9e:44ac',
    egress: 'ens18', gw: '192.168.7.2', v4_local: '192.168.7.3',
    'class': 'public', combo: 'both', checked_at: '2026-10-07 22:53:09',
    evidence_cached: false,
    verdict: {
      key: 'likely', level: 'warn',
      title: '疑似公网（地址是公网段，入向未验证）',
      conclusion: '出口地址属于公网段、多个外部服务回显也一致，但还没有实测过「外网能不能主动连进来」。',
      advice: ['点下方「开始入向实测」', '没有外网主机时，可用手机蜂窝网络访问一次', '实测前不要急着配置端口转发'],
      en_title: "Suspected public (address is in a public range, inbound not verified)",
      en_conclusion: "The egress address falls in a public range and multiple external services echo the same address, but inbound has not been measured.",
      en_advice: ["Click Start inbound test below", "Without an external host, use mobile cellular network", "Do not rush to configure port forwarding before the actual test"]
    },
    evidence: [
      { key: 'class', name: '出口地址段', en_name: 'Egress address range',
        value: '101.70.135.90', en_value: '101.70.135.90', pass: true,
        detail: '属于公网地址段，在互联网上可路由', en_detail: 'Belongs to a public address range, routable on the Internet' },
      { key: 'echo', name: '多源回显一致性', en_name: 'Multi-source echo consistency',
        value: '101.70.135.90', en_value: '101.70.135.90', pass: true,
        detail: '2 个独立服务都返回同一个地址，出口地址可信', en_detail: '2 independent services all returned the same address; the egress address is trustworthy' },
      { key: 'path', name: '首跳链路', en_name: 'First-hop link',
        value: '192.168.7.2 \u2192 172.30.80.1', en_value: '192.168.7.2 \u2192 172.30.80.1', pass: false,
        detail: '出网第一跳是私网地址（192.168.7.2），说明本机上面至少还有一层 NAT', en_detail: 'The first egress hop is a private address (192.168.7.2), meaning there is at least one more layer of NAT above this host' },
      { key: 'inbound', name: '入向可达性实测', en_name: 'Inbound reachability test',
        value: '未测试', en_value: 'Not tested', pass: false, probed: false,
        detail: '尚未做入向实测 —— 这是唯一能拍板「外网能不能主动连进来」的证据', en_detail: 'No inbound test has been run yet — this is the only evidence that can decide whether the Internet can actively connect in' }
    ],
    msg_cn: '判定完成：疑似公网（地址是公网段，入向未验证）'
  } };

  else if (/\/api\/ddns/.test(url)) data = { ok: true, data: {
    cfg: {
      enabled: true, provider: 'custom', provider_cn: '自定义（URL 模板）',
      provider_region: '通用', provider_desc: '填入服务商给出的更新 URL，支持占位符 {ip} {ipv6} {domain} {token} {user} {pass}',
      provider_fields: ['domain', 'url4', 'url6', 'token', 'user', 'pass'],
      provider_doc: '按服务商文档填写；适用于任何提供 HTTP 更新的服务商',
      provider_supported: true, timer_active: true, timer_unit: 'drouter-ddns.timer',
      domain: '', subdomain: '', ipv4: true, ipv6: false, ttl: 300, interval: 300,
      last_update: '', last_ip4: '', last_ip6: '', last_msg_cn: ''
    },
    public: {
      v4_local: '192.168.7.3', v4_has: true, v4_nat: false, v4_public: '101.70.135.90', v4_source: 'stun',
      v6_local: '2408:8240:5416:ad61:acf0:c128:bf9e:44ac', v6_has: true, v6_public: '2408:8240:5416:ad61:acf0:c128:bf9e:44ac', v6_source: 'stun',
      egress: 'ens18', gw: '192.168.7.2', checked_at: '2026-10-07 22:53:09', combo: 'both'
    },
    note: {
      title: '双栈公网（IPv4 + IPv6 均有公网能力）', level: 'ok',
      conclusion: '你的宽带同时具备公网 IPv4 与公网 IPv6，DDNS 可同时解析 A（IPv4）与 AAAA（IPv6）记录，对外访问兼容性最好。',
      advice: ['建议同时启用 A 与 AAAA 记录，客户端会优先走 IPv6（延迟更低）', 'IPv6 地址通常是动态前缀（DHCPv6-PD），需开启前缀变化检测并重新下发', '公网 IPv4 若为动态，请把 TTL 设为 300 秒以内，加快解析生效'],
      en_title: "Dual-stack public (both IPv4 + IPv6 have public capability)",
      en_conclusion: "Your broadband has both public IPv4 and public IPv6. DDNS can resolve both A and AAAA records, giving the best compatibility for inbound access.",
      en_advice: ["Enable both A and AAAA records; clients will prefer IPv6 (lower latency)", "IPv6 addresses usually have a dynamic prefix (DHCPv6-PD); enable prefix-change detection and re-publish", "If the public IPv4 is dynamic, set TTL to 300 seconds or less to speed up propagation"]
    },
    warn: '',
    providers: [
      { v: 'custom', n: '自定义（URL 模板）', region: '通用',
        desc: '填入服务商给出的更新 URL，支持占位符 {ip} {ipv6} {domain} {token} {user} {pass}',
        doc: '按服务商文档填写；适用于任何提供 HTTP 更新的服务商', fields: ['domain', 'url4', 'url6', 'token', 'user', 'pass'] },
      { v: 'aliyun', n: '阿里云解析 DNS', region: '国内',
        desc: 'AccessKey 签名（HMAC-SHA1）调用 Alidns OpenAPI，支持 IPv4/IPv6 双记录',
        doc: 'https://help.aliyun.com/zh/dns/api-alidns-2015-01-09-update-domain-record', fields: ['domain', 'subdomain', 'access_key_id', 'access_key_secret', 'ttl'] },
      { v: 'cloudflare', n: 'Cloudflare', region: '国际',
        desc: 'API Token + Zone ID，支持 A / AAAA 记录，免费版即可',
        doc: 'https://developers.cloudflare.com/api/operations/dns-records-for-a-zone-update-dns-record', fields: ['domain', 'subdomain', 'api_token', 'zone_id', 'proxied'] }
    ],
    catalog_note: '服务商接口依据 2026 年现行公开文档整理，接入方式如有调整请以官方文档为准。'
  } };

  else if (/\/api\/fwlog|flowlog_state/.test(url)) data = { ok: true, data: {
    enabled: true, loaded: false, count: 0, dropped: 0, allowed: 0,
    v4: 0, v6: 0,
    empty_note: '\u5f53\u524d\u5185\u6838\u91cc\u6ca1\u6709 drouter \u7684\u9632\u706b\u5899\u89c4\u5219\uff08nft \u89c4\u5219\u96c6\u4e3a\u7a7a\uff09\uff0c\u8fd8\u6ca1\u6709\u4efb\u4f55\u5305\u7ecf\u8fc7\u672c\u673a\u7684\u9632\u706b\u5899\u94fe\uff0c\u6240\u4ee5\u8fd9\u91cc\u4e0d\u4f1a\u6709\u8bb0\u5f55\u3002\u8bf7\u5230\u300c\u9632\u706b\u5899 IPv4 / IPv6\u300d\u9875\u9762\u70b9\u4e00\u6b21\u300c\u5e94\u7528\u300d\uff0c\u89c4\u5219\u8f7d\u5165\u540e\u5373\u53ef\u5f00\u59cb\u8bb0\u5f55\u3002',
    lines: [] } };

  else if (/\/api\/acl/.test(url)) data = { ok: true, data: {
    enabled: false, dev_groups: [], time_groups: [], rules: [],
    dpi_installed: false, qos_enabled: false,
    summary_note: '\u65f6\u95f4\u89c4\u5219\u6309\u5317\u4eac\u65f6\u95f4\uff08UTC+8\uff09\u6362\u7b97\u540e\u5199\u5165\u5185\u6838\uff1b\u8de8\u96f6\u70b9\u7684\u65f6\u95f4\u6bb5\u4f1a\u81ea\u52a8\u62c6\u6210\u4e24\u6bb5\u3002\u5e94\u7528\u8bc6\u522b\u4f9d\u8d56 DPI \u5e93\uff0c\u9650\u901f\u52a8\u4f5c\u4f9d\u8d56 QoS \u6a21\u5757\u3002' } };

  else if (/\/api\/qos/.test(url)) data = { ok: true, data: {
    enabled: false, mode: 'cake', cake_mode: 'diffserv3',
    bw_down: '', bw_up: '',
    qdisc: 'qdisc fq_codel 0: root refcnt 2 limit 10240p',
    nft_table: false,
    cake_hint: '\u6309 Diffserv \u5206\u4e3a\u4e09\u6863\uff1a\u8bed\u97f3\uff08CS7/CS6/EF/VA\uff0c\u5360 25% \u5e26\u5bbd\u4efd\u989d\uff0cCodel \u95f4\u9694\u7f29\u77ed\uff09\u3001\u5c3d\u529b\uff08\u666e\u901a\u6d41\u91cf\uff0c\u5360\u6ee1\u5e26\u5bbd\uff09\u3001\u6279\u91cf\uff08CS1/LE\uff0c\u4f4e\u4f18\u5148\u7ea7\uff0c\u53ea\u5206\u5230 6.25% \u5e26\u5bbd\u4efd\u989d\uff09\u3002',
    impacts: [
      { k: 'flowtable', title: '\u8f6f\u52a0\u901f\u8ba9\u5df2\u5efa\u7acb\u8fde\u63a5\u7ed5\u8fc7 netfilter\uff0cDSCP \u6253\u6807\u4e0e mark \u5206\u7c7b\u4f1a\u5931\u6548\u3002' },
      { k: 'cpu', title: 'CAKE \u662f\u9010\u5305\u8c03\u5ea6\uff0c\u5e26\u5bbd\u8d8a\u9ad8 CPU \u5f00\u9500\u8d8a\u5927\u30021Gbps \u4ee5\u5185\u901a\u5e38\u65e0\u538b\u529b\u3002' },
      { k: 'htb', title: '\u9650\u901f\u5bf9\u4e00\u4e2a\u5185\u7f51 IP \u7684\u4e0a\u4e0b\u884c\u540c\u65f6\u751f\u6548\u3002' },
      { k: 'dpi', title: 'HTTPS / QUIC \u5df2\u52a0\u5bc6\uff0c\u4ec5\u9760\u7aef\u53e3\u4e0e DSCP \u65e0\u6cd5\u51c6\u786e\u533a\u5206\u3002' },
      { k: 'connlimit', title: '\u9650\u5236\u5355 IP \u5e76\u53d1\u8fde\u63a5\u6570\u80fd\u6291\u5236\u5f02\u5e38\u8bbe\u5907\u62d6\u57ae\u8def\u7531\u3002' }] } };

  else if (/\/api\/share/.test(url)) data = { ok: true, data: {
    smb: { on: false, installed: false, pkgs: ['samba', 'samba-common-bin'],
           services: { smbd: 'inactive', nmbd: 'inactive' } },
    nfs: { on: false, installed: false, pkgs: ['nfs-kernel-server'],
           services: { 'nfs-server': 'inactive', rpcbind: 'inactive' } },
    shares: [], exports: [], lan_ip: '192.168.7.3', include_written: false,
    workgroup: 'WORKGROUP', desc: 'drouter \u6587\u4ef6\u5171\u4eab', iface: '',
    threads: 8,
    intro: 'SMB \u9762\u5411 Windows / macOS / Linux \u5168\u5e73\u53f0\uff0c\u517c\u5bb9\u6027\u6700\u597d\uff1bNFS \u66f4\u9002\u5408 Linux \u4e4b\u95f4\u4e92\u4f20\uff0c\u6027\u80fd\u66f4\u9ad8\u4f46 Windows \u9700\u8981\u989d\u5916\u5ba2\u6237\u7aef\u3002\u4e24\u8005\u53ef\u4ee5\u540c\u65f6\u5f00\u542f\uff0c\u5171\u4eab\u540c\u4e00\u4e2a\u76ee\u5f55\u3002',
    win_hint: '\u6253\u5f00\u300c\u6b64\u7535\u8111\u300d\uff0c\u5728\u5730\u5740\u680f\u8f93\u5165 \\\\\\\\192.168.7.3 \u56de\u8f66',
    mac_hint: '\u8bbf\u8fbe \u2192 \u9876\u90e8\u83dc\u5355\u300c\u524d\u5f80\u300d\u2192\u300c\u8fde\u63a5\u670d\u52a1\u5668\u300d(\\u2318K)',
    linux_hint: 'SMB\uff1a\u5b89\u88c5 cifs-utils \u540e\u6302\u8f7d\uff1bNFS\uff1a\u5b89\u88c5 nfs-common \u540e\u6302\u8f7d\u3002',
    templates: [] } };

  else if (/\/api\/storage/.test(url)) data = { ok: true, data: {
    devices: [], internal: [{ fstype: 'ext4', size: '40G' }, { fstype: 'xfs', size: '0' }],
    fs_missing: ['xfs', 'f2fs'],
    note: '\u5916\u63a5 U \u76d8 / \u79fb\u52a8\u786c\u76d8 / Type-C \u4e0e\u96f7\u7535\u786c\u76d8\u76d2\u4f1a\u663e\u793a\u5728\u4e0b\u65b9\u3002',
    conn_note: '\u300c\u8fde\u63a5\u65b9\u5f0f\u300d\u663e\u793a\u7684\u662f\u603b\u7ebf\u7c7b\u578b\uff08USB / \u96f7\u7535 / NVMe / SATA\uff09\uff1bType-C \u662f\u63d2\u5934\u5f62\u6001\u3002',
    empty: '\u672a\u68c0\u6d4b\u5230\u5916\u63a5\u8bbe\u5907\u3002\u63d2\u4e0a U \u76d8\u6216\u79fb\u52a8\u786c\u76d8\u540e\u70b9\u300c\u5237\u65b0\u300d\u3002' } };

  else if (/\/api\/opensoho/.test(url)) data = { ok: true, data: {
    installed: false, version: '', services: { state: 'inactive', enabled: 'disabled' },
    listening: false, console: '',
    note: '\u771f\u6b63\u7684 Wi-Fi\u3001VLAN\u3001PoE \u914d\u7f6e\u90fd\u5728 OpenSOHO \u81ea\u5df1\u7684\u63a7\u5236\u53f0\u91cc\u505a\u3002',
    ap_hint: 'AP \u4fa7\u9700\u8981\u5728 OpenWRT \u4e0a\u88c5 openwisp-config \u5e76\u586b\u5165\u4e0b\u9762\u7684\u5171\u4eab\u5bc6\u94a5\u6765\u6ce8\u518c\u3002' } };

  else if (/\/api\/examples|\/api\/openapi/.test(url)) data = { ok: true, data: {
    groups: [{ k: 'common', n: '\u901a\u7528', items: [
      { m: 'GET', p: '/api/health', d: '\u5065\u5eb7\u68c0\u67e5\uff08\u65e0\u9700\u767b\u5f55\uff09' },
      { m: 'POST', p: '/api/login', d: '\u767b\u5165\u83b7\u53d6 Token' }] }],
    examples: [{ k: 'curl', n: 'cURL\uff08\u6700\u901a\u7528\uff0cLinux/macOS/Windows \u5747\u53ef\uff09',
                 code: '# 1) \u767b\u5165\u62ff token\nTOKEN=$(curl -sk -X POST "$BASE/api/login")' }] } };

  else if (/\/api\/docker/.test(url)) data = { ok: true, data: {
    pkgs: { docker: { installed: false }, compose: { installed: false },
            install_cmd: 'apt-get install -y docker.io' },
    info: {}, services: { docker: 'inactive', containerd: 'inactive' },
    containers: [], images: [], networks: [], volumes: [], stacks: [],
    mirrors: [
      { n: '\u817e\u8baf\u4e91\u516c\u5171\u955c\u50cf\u6e90\uff0c\u957f\u671f\u7a33\u5b9a\uff0c\u65e0\u9700\u767b\u5f55\u3002' },
      { n: 'DaoCloud \u516c\u5171\u52a0\u901f\u8282\u70b9\uff0c\u56fd\u5185\u53ef\u7528\u6027\u8f83\u597d\u3002' },
      { n: '\u7f51\u6613\u7684\u8001\u724c\u955c\u50cf\u6e90\uff0c\u901f\u5ea6\u7a33\u5b9a\u4f46\u5076\u5c14\u540c\u6b65\u6ede\u540e\u3002' },
      { n: '\u6559\u80b2\u7f51\u4e0e\u7535\u4fe1\u94fe\u8def\u8868\u73b0\u597d\uff0c\u9ad8\u5cf0\u671f\u5076\u5c11\u9650\u901f\u3002' },
      { n: '\u5357\u5927\u5f00\u6e90\u955c\u50cf\u7ad9\uff0c\u6559\u80b2\u7f51\u7528\u6237\u4f18\u5148\u3002' },
      { n: '\u4e0a\u6d77\u4ea4\u5927\u955c\u50cf\u7ad9\uff0c\u534e\u4e1c\u5730\u533a\u8f83\u5feb\u3002' },
      { n: '\u963f\u91cc\u4e91\u4e0d\u518d\u63d0\u4f9b\u901a\u7528\u52a0\u901f\u5730\u5740\u3002' }] } };

  else if (/\/api\/deps\/check/.test(url)) data = { ok: true, data: {
    ok: false, missing: ['nfdpi', 'nginx'],
    dpi_note: 'DPI \u9700\u8981\u72ec\u7acb\u8fdb\u7a0b\uff08nDPI Reader / Suricata\uff09\uff0c\u4f1a\u989d\u5916\u5360\u7528\u5185\u5b58\uff1b\u672c\u673a\u4e3a 4GB \u5185\u5b58\u7684\u865a\u62df\u673a\uff0c\u5efa\u8bae\u53ea\u88c5 nDPI \u4e00\u9879\u3002',
    apt_note: 'Debian 13 \u4ed3\u5e93\u76f4\u63a5\u88c5\uff0c\u65e0\u9700\u7f16\u8bd1\u3002nDPI \u7248\u672c\u53ef\u80fd\u7565\u65e7\uff0c\u4f46\u591f\u5bb6\u7528\u3002',
    dpi2_note: '\u89c4\u5219\u5e93\u9ed8\u8ba4\u4ece GitHub \u83b7\u53d6\uff0c\u56fd\u5185\u76f4\u8fde\u5e38\u5e38\u8d85\u65f6 \u2014\u2014 \u8bf7\u5728\u4e0b\u65b9\u9009\u62e9\u52a0\u901f\u524d\u7f00\u3002',
    proxy_note: '\u8001\u724c\u516c\u5171\u52a0\u901f\uff0c\u7a33\u5b9a\u6027\u4e00\u822c\uff0c\u5076\u5c14\u9650\u6d41\u3002',
    order_note: '\u6309\u300c\u5982\u4f55\u7701\u5fc3 \u2192 \u6700\u65b0 \u2192 \u6700\u5f3a\u300d\u987a\u5e8f\u9009\u4e00\u4e2a\u3002' } };

  else if (/\/api\/dpi/.test(url)) data = { ok: true, data: {
    installed: false, version: '', rules_ver: '',
    proxy_note: '\u8001\u724c\u516c\u5171\u52a0\u901f\uff0c\u7a33\u5b9a\u6027\u4e00\u822c\uff0c\u5076\u5c14\u9650\u6d41\u3002',
    speed_note: '\u6d4b\u901f\u4ece\u8def\u7531\u5668\u81ea\u8eab\u53d1\u8d77\uff0c\u53cd\u6620\u7684\u662f Docker \u771f\u5b9e\u94fe\u8def\u3002' } };

  else if (/\/api\/ulog|journal/.test(url)) data = { ok: true, data: {
    rows: [{ ts: '2026-10-07T23:03:14', level: 'info', module: 'ulogd',
             msg_cn: '\u5df2\u5f52\u6863 801 \u6761\u65e5\u5fd7\uff08\u91c7\u96c6 801 \u6761\uff09', msg_en: 'Archived 801 log entries (collected 801)' },
            { ts: '2026-10-07T22:42:58', level: 'info', module: 'ulogd',
             msg_cn: '\u5f52\u6863\u53bb\u91cd\u79fb\u9664 798 \u6761\u91cd\u590d\u8bb0\u5f55', msg_en: 'Removed 798 duplicate entries while archiving' },
            { ts: '2026-10-07T22:43:34', level: 'info', module: 'helpd',
             msg_cn: '\u5e38\u9a7b\u6267\u884c\u5b88\u62a4\u5df2\u542f\u52a8\uff08socket=/run/drouter/helper.sock\uff0c\u52a8\u4f5c 89 \u4e2a\uff09',
             msg_en: 'Resident exec daemon started (socket=/run/drouter/helper.sock, 89 actions)' }],
    total: 300 } };

  else if (/\/api\/audit/.test(url)) data = { ok: true, data: {
    rows: [{ ts: '2026-10-07T22:47:05', user: 'admin', action: '\u767b\u5f55\u6210\u529f',
             detail: '192.168.7.46', ok: true },
            { ts: '2026-10-07T22:33:00', user: 'admin', action: '\u767b\u5f55\u5931\u8d25',
             detail: '192.168.7.8', ok: false },
            { ts: '2026-10-05T14:03:13', user: 'admin', action: '\u8bca\u65ad\u5de5\u5177',
             detail: 'ping 223.5.5.5', ok: true }] } };

  if (/\/api\/docker/.test(url)) {
    data = { ok: true, data: {
      pkgs: { docker: { installed: true, pkgs: ['docker.io'] },
              compose: { installed: true }, install_cmd: 'apt-get install -y docker.io' },
      info: { version: '27.5.1', containers: '2', running: '1', images: '5',
              driver: 'overlay2', root: '/var/lib/docker' },
      services: { docker: 'active', containerd: 'active' },
      containers: [
        { id: 'abc123def456', name: 'web', image: 'nginx:alpine', state: 'running',
          status: 'Up 3 hours', ports: '0.0.0.0:8080->80/tcp', created: '2 hours ago',
          size: '12MB', stat: { cpu: '0.42%', mem: '24.1MiB / 1GiB', mem_perc: '2.35%',
          net: '1.2MB / 340kB', block: '0B / 0B', pids: '5' } },
        { id: 'ffffeeee1111', name: 'db', image: 'postgres:16', state: 'exited',
          status: 'Exited (0) 1 hour ago', ports: '5432/tcp', created: '1 day ago',
          size: '90MB', stat: {} },
      ],
      images: [{ repo: 'nginx', tag: 'alpine', id: 'a1b2c3d4e5f6', size: '48MB',
                 created: '2026-09-20 10:00:00 +0800 CST' }],
      networks: [{ name: 'bridge', driver: 'bridge', scope: 'local' }],
      volumes: [{ name: 'pgdata', driver: 'local', mountpoint: '/var/lib/docker/volumes/pgdata' }],
      stacks: [{ name: 'my-app', dir: '/etc/drouter/docker/stacks/my-app',
                 file: 'docker-compose.yml', mtime: '2026-09-27 12:00:00', size: 320 }],
      compose_cmd: 'docker compose', stack_dir: '/etc/drouter/docker/stacks',
      note: 'Test data' }, msg_cn: 'ok' };
  } else if (/\/api\/convert/.test(url)) {
    data = { ok: true, data: { ok: true, yaml: 'services:\n  web:\n    image: nginx\n',
             service: 'web', image: 'nginx', warnings: [], msg_cn: '解析成功' } };
  }
  return { status: 200, ok: true, json: async () => data };
}

const sandbox = {
  document, fetch: fetchStub, console,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  requestAnimationFrame: () => 0, alert() {}, confirm: () => false,
  location: { reload() {}, href: '' },
  navigator: { userAgent: 'node' }, window: {},
  TextEncoder, TextDecoder, URL, Blob: function () {}, FormData: function () {},
};
// 1.0.10 起 app.js 里的文案走 t()（i18n.js 提供）。离线检查器不加载
// i18n.js（它要 DOM + localStorage），所以这里给一个**恒等兜底**：
// 保证渲染检查能跑完、且不会因为缺 t() 而把「概览页抛异常」误报成
// 真 bug。⚠️ 必须是兜底而不是「加载真的 i18n.js」——
// 后者会让检查器依赖字典，而字典天天在改，改一个词就可能连带
// 让 check-render 失败，掩盖真正的渲染问题。
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);

/* —— 真 i18n（不是恒等兜底）—— */
// i18n.js 的 setLang() 会写 document.documentElement.lang，
// applyDom() 会遍历 document —— check-render 的桩里没有，先补上。
if (!document.documentElement) document.documentElement = {};
if (!document.head) document.head = mkEl('head');
const i18nSrc = fs.readFileSync(path.join(__dirname, '..', 'web', 'i18n.js'), 'utf8');
vm.runInContext(i18nSrc, sandbox);
{
  const I = sandbox.window.i18n || sandbox.i18n;
  if (!I) { console.error('i18n.js 没导出 window.i18n'); process.exit(1); }
  // ⚠ 真机顺序是「先按界面语言加载 app.js，之后用户才切英文」。
  //   顶层 const 里的 T('中文') **只在加载那一刻求值一次**，切语言不会重算
  //   —— 这正是顶栏日期/农历、IPv6 前缀策略等一批残留的真因。
  //   所以这里先按 zh-CN 加载，加载完再切 en-US，完整复现真机路径。
  //   （PRELOAD_LANG=en-US 可退回旧行为，用于对比排查。）
  I.setLang(process.env.PRELOAD_LANG || 'zh-CN', { force: true });
  sandbox.t = I.t;          // 其它 web 文件用的是 window.t
  sandbox.i18n = I;
  if (sandbox.localStorage && sandbox.localStorage.setItem) {
    sandbox.localStorage.setItem('drouter_lang', process.env.PRELOAD_LANG || 'zh-CN');
  }
}

try {
  vm.runInContext(code, ctx, { filename: file });
} catch (e) {
  console.error('脚本加载失败：' + e.message);
  process.exit(1);
}

/* —— 独立模块（不在 VIEWS 顶层键里，此前是英文残留的盲区）——
   upstream / netdetail / realtime / update 各自 IIFE + i18n.register，
   必须**在切 en-US 之前**载入（复现真机顺序：先按 zh-CN 加载，用户再切语言）。*/
for (const _m of ['upstream.js', 'netdetail.js', 'realtime.js', 'update.js']) {
  try {
    vm.runInContext(
      fs.readFileSync(path.join(__dirname, '..', 'web', _m), 'utf8'), ctx,
      { filename: _m });
  } catch (e) {
    console.error('模块加载失败 ' + _m + '：' + e.message);
    process.exit(1);
  }
}

// `const` 声明不会挂到 context 对象上，需用表达式取出
let VIEWS;
try {
  VIEWS = vm.runInContext('VIEWS', ctx);
} catch (e) {
  VIEWS = null;
}
if (!VIEWS || typeof VIEWS !== 'object') {
  console.error('未找到 VIEWS 导出（脚本需在顶层用 const VIEWS = {...}）');
  process.exit(1);
}

/* —— app.js 已按 zh-CN 加载完毕，现在切到英文（复现用户手动切语言）—— */
{
  const I = sandbox.window.i18n || sandbox.i18n;
  I.setLang('en-US', { force: true });
  sandbox.t = I.t;
  if (sandbox.localStorage && sandbox.localStorage.setItem) {
    sandbox.localStorage.setItem('drouter_lang', 'en-US');
  }
}

// 带内部标签页的视图：{ state: S 上的字段名, tabs: [...], render: '渲染函数名' }
// 向导每一步的正文写在 #wz-body（不在 #view），
// 而步骤条与进度受全局 WIZ_STEP 驱动。
// 只跑 viewWizard() 只能拿到第 1 步。
const WIZ_STEPS_IDS = { wizard: 4 };

// 点击后才有内容的视图：渲染后手动触发加载器，再抓 #view HTML。
const EXTRA_LOADERS = {
  depcheck: ['runDepCheck()'],
  api: ['loadApi()'],
  log: ['loadLogs()'],
  fw4: ['fwLogLoad()'],
  fw6: ['fwLogLoad()'],
  alert: ['alLoad(false)'],
};

const TABBED = {
  theme: { state: 'themeTab', tabs: ['gallery', 'design', 'import'], render: 'tmRender' },
};

/* 只取**用户能看见**的中文。
 * 剥掉三类不上屏的内容，否则误报：
 *   ① data-* 属性 —— 里面常放 JSON（如 VLAN 预设的 data-p='{"name":"中国电信 IPTV"}'），
 *      那是给点击回调用的内部数据，按钮文字本身已经是英文；
 *   ② <style> / <script> 块；
 *   ③ HTML 注释。
 * 2026-10-07：因为没剥 data-*，VLAN 页按钮文字已翻成英文却仍被判红。*/
function visibleHan(html) {
  return String(html || '')
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\sdata-[a-z-]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
    .match(/[\u4e00-\u9fff]+/g);
}

(async () => {
  const names = Object.keys(VIEWS);
  let bad = 0, empty = 0, han = 0;
  for (const n of names) {
    try {
      const fn = VIEWS[n];
      if (typeof fn !== 'function') { console.log(`  ✗ ${n}：不是函数`); bad++; continue; }
      ELS.clear();
      await fn();
      for (const ex of (EXTRA_LOADERS[n] || [])) {
        try { await vm.runInContext(ex, ctx); }
        catch (e) { console.log(`  ⚠ ${n} 加载器 ${ex} 异常：${e.message}`); }
      }
      await new Promise(r => setTimeout(r, 300));
      const viewEl = getEl('#view');
      const html = [...ELS.values()].map(e => (e._html || '') + '\n' + (e.textContent || '')).join('\n');
      void viewEl;
      // DUMP_ALL=1：把每个视图导成一张可比对的独立 HTML（挂真 app.css），
      // 供真浏览器按 PC / 移动视口截图做排版检查（_dev/.layout/）。
      if (process.env.DUMP_ALL) {
        const dir = path.join(__dirname, '.layout');
        if (!fs.existsSync(dir)) fs.mkdirSync(dir);
        // 真机上 app.js 的 MutationObserver 会给每个 <table> 套一层 .tw
        // （overflow-x:auto）。预览页没有 JS，必须自己补上，否则
        // 「表格溢出视口」全是假阳性。
        const inner = (getEl('#view').innerHTML || '')
          .replace(/<table[\s\S]*?<\/table>/g, (m) => '<div class="tw">' + m + '</div>');
        // ⚠️ 预览必须带上**真外壳**（侧栏 + 顶栏）。只导 #view 的话，
        //    任何出在菜单/标题栏的排版问题都测不到 —— 而「新增导航项显示成
        //    nav.n.health」「顶栏本页状态灯挤到标题」这类问题恰恰都在外壳上。
        //    侧栏直接调真 renderNav() 生成，不手写假导航（假的测不出真问题）。
        let navHtml = '';
        try {
          vm.runInContext('renderNav()', ctx);
          navHtml = getEl('#nav')._html || getEl('#nav').innerHTML || '';
        } catch (e) { navHtml = '<!-- renderNav 失败：' + e.message + ' -->'; }
        const title = vm.runInContext(
          "T('nav.t.' + " + JSON.stringify(n) + ") || ''", ctx);
        const page = '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
          + '<meta name="viewport" content="width=device-width,initial-scale=1">'
          + '<link rel="stylesheet" href="../../web/app.css">'
          + '<link rel="stylesheet" href="../../web/update.css">'
          + '</head><body><div id="app">'
          + '<aside class="sidebar">'
          + '<a class="brand brand-home"><span class="brand-mark">◆</span>'
          + '<span class="brand-txt"><b>drouter</b><span>Debian 13 x86 router</span></span></a>'
          + '<div class="nav-search"><input placeholder="搜索菜单…" aria-label="search"></div>'
          + '<nav id="nav">' + navHtml + '</nav>'
          + '<div class="side-foot"><div class="dot-line"><span class="dot on"></span>'
          + '<span>后端在线</span></div>'
          + '<button class="side-logout"><span class="sl-txt">退出系统</span></button></div>'
          + '</aside>'
          + '<main>'
          + '<header class="topbar"><div class="tb-left">'
          + '<button class="icon-btn" aria-label="menu">≡</button>'
          + '<h2 id="page-title">' + title + '</h2>'
          + '<span id="page-health" class="page-health">'
          + '<span class="st-dot lv-ok"></span><span class="ph-txt">OK</span>'
          + '<button type="button" class="st-info" title="about">i</button></span>'
          + '</div><div class="tb-right">'
          + '<button class="icon-btn tb-health lv-ok"><span class="tb-h-dot"></span></button>'
          + '<button class="icon-btn" id="btn-lang"><span id="lang-label">EN</span></button>'
          + '<span class="mini">v1.0.9</span></div></header>'
          + '<section id="view" class="view">' + inner
          + '</section></main></div></body></html>';
        fs.writeFileSync(path.join(dir, n + '.html'), page, 'utf8');
      }
      if (process.env.DUMP_VIEW === n) {
        console.log('--- ' + n + ' 渲染 HTML（前 2500 字符）---');
        console.log(html.slice(0, parseInt(process.env.DUMP_LIMIT || '2500', 10)));
        console.log('--- 结束 ---');
      }
      if (html.length < 40) {
        console.log(`  ⚠ ${n}：渲染内容过少（${html.length} 字符）`);
        empty++;
      }
      // ⛔ 本文检查器的**唯一目的**：英文界面不许有汉字
      const cjk = visibleHan(html);
      if (cjk) {
        const uniq = [...new Set(cjk)].slice(0, 8).join(' / ');
        console.log(`  ✗ ${n}：英文界面残留中文 ${cjk.length} 处→ ${uniq}`);
        // 打印每处中文的上下文，省掉「grep 全文找」的往返
        if (process.env.SHOW_CTX) {
          const vis = String(html)
            .replace(/<script[\s\S]*?<\/script>/gi, '')
            .replace(/<style[\s\S]*?<\/style>/gi, '')
            .replace(/<!--[\s\S]*?-->/g, '')
            .replace(/\sdata-[a-z-]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '');
          const re = /[\u4e00-\u9fff]+/g;
          let m2, k = 0;
          while ((m2 = re.exec(vis)) && k < 8) {
            const a = Math.max(0, m2.index - 70);
            console.log('      [%d] %s', ++k,
              vis.slice(a, m2.index + m2[0].length + 20).replace(/\n/g, ' '));
          }
        }
        han++;
      }
      // 带标签页的视图：逐个标签渲染一遍，否则只有默认标签会被真正执行。
      // 主题之家（#12）的三个标签页各自 $("#tm-pane").innerHTML = ...，逻辑完全不同。
      // 向导：逐步验。每一步重渲某个步骤再检 #wz-body。
      // 步骤条与顶部结论一并一并检，不得只看一个容器。
      if (WIZ_STEPS_IDS[n]) {
        for (let st = 0; st < WIZ_STEPS_IDS[n]; st++) {
          try {
            vm.runInContext('WIZ_STEP = ' + st, ctx);
            await vm.runInContext('wizRender()', ctx);
            const hb = getEl('#wz-body').innerHTML || '';
            if (hb.length < 40) {
              console.log(`  ⚠ ${n}/step${st}：步骤内容过少（${hb.length} 字符）`);
              empty++;
            }
            if (/[\u4e00-\u9fff]/.test(hb)) {
              const c = [...new Set(visibleHan(hb))].slice(0, 8).join(' / ');
              console.log(`  ✗ ${n}/step${st}：步骤内容残留中文→ ${c}`);
              han++;
            }
          } catch (e) {
            console.log(`  ✗ ${n}/step${st}：渲染异常 ${e.message}`);
            bad++;
          }
        }
      }
      if (TABBED[n]) {
        for (const tab of TABBED[n].tabs) {
          try {
            vm.runInContext(`S.${TABBED[n].state} = ${JSON.stringify(tab)}`, ctx);
            await vm.runInContext(TABBED[n].render + '()', ctx);
            const h2 = getEl('#tm-pane').innerHTML || '';
            if (h2.length < 40) {
              console.log(`  ⚠ ${n}/${tab}：标签内容过少（${h2.length} 字符）`);
              empty++;
            } else if (visibleHan(h2)) {
              const cj = [...new Set(visibleHan(h2))].slice(0, 6).join(' / ');
              console.log(`  ✗ ${n}/${tab}：英文界面残留中文 → ${cj}`);
              han++;
            } else if (process.env.DUMP_VIEW === n) {
              console.log('--- ' + n + '/' + tab + ' 前 1200 字符 ---');
              console.log(h2.slice(0, 1200));
            }
            // 每个标签都应跑到;< 若某个标签 HTML 里出现未完成标记要报警
            if (/undefined|\[object Object\]/.test(h2)) {
              const hit = (/undefined/.test(h2) ? 'undefined' : '[object Object]');
              console.log(`  ⚠ ${n}/${tab}：输出含 ${hit}`);
              empty++;
            }
          } catch (e) {
            const st = String(e.stack || '').split('\n').slice(1, 4).join(' | ');
            console.log(`  ✗ ${n}/${tab}：抛异常 → ${e.message}\n      ${st}`);
            bad++;
          }
        }
        // 复位到默认标签，避免影响后续视图
        vm.runInContext(`S.${TABBED[n].state} = ${JSON.stringify(TABBED[n].tabs[0])}`, ctx);
      }
    } catch (e) {
      const st = String(e.stack || '').split('\n').slice(1, 4).join(' | ');
      console.log(`  ✗ ${n}：抛异常 → ${e.message}\n      ${st}`);
      bad++;
    }
  }
  // —— accel 是 netstat 内嵌的异步子区块，不在 VIEWS 顶层键里，单独渲染检查 ——
  try {
    await vm.runInContext('loadAccel()', ctx);
    const accelBox = getEl('#accel-box').innerHTML || '';
    const accelImpact = getEl('#accel-impact').innerHTML || '';
    const ahtml = accelBox + accelImpact;
    if (ahtml.length < 40) {
      console.log('  ⚠ accel：渲染内容过少（' + ahtml.length + ' 字符）');
      empty++;
    } else {
      const acjk = visibleHan(ahtml);
      if (acjk) {
        const au = [...new Set(acjk)].slice(0, 8).join(' / ');
        console.log('  ✗ accel：英文界面残留中文 ' + acjk.length + ' 处→ ' + au);
        han++;
      }
    }
  } catch (e) {
    console.log('  ✗ accel：渲染异常 ' + e.message);
    bad++;
  }

  // —— 顶栏时钟不属于任何 VIEWS：日期/星期/农历是最容易残留中文的地方
  //    （顶层常量那批 bug 里它排第一），这里单独渲染一次扫一遍。
  try {
    ELS.clear();
    await vm.runInContext('tickClock()', ctx);
    const chtml = [...ELS.values()].map(e => (e._html || '') + '\n' + (e.textContent || '')).join('\n');
    const ccjk = visibleHan(chtml);
    if (process.env.CLOCK_DEBUG) {
      for (const [k, e] of ELS) {
        if (String(k).indexOf('clock') >= 0) console.log('   [clock] ' + k + ' = ' + JSON.stringify(e.textContent));
      }
    }
    if (ccjk) {
      console.log('  ✗ clock：英文界面残留中文 → ' + [...new Set(ccjk)].slice(0, 8).join(' / '));
      han++;
    }
  } catch (e) {
    console.log('  ⚠ clock：渲染异常 ' + e.message);
  }

  // —— 独立模块渲染：容器不在 #view 里，必须单独跑它们的渲染钩子 ——
  //    ⚠️ 计数必须进汇总：否则「模块根本没渲染」也会显示 0 残留（假绿）。
  let modOk = 0, modBad = 0;
  for (const _h of [
    ['upstream', 'drouterRenderUpstream'],
    ['netdetail', 'drouterRenderNetDetail'],
    ['realtime', 'drouterRenderRealtime'],
    ['update', 'drouterRenderUpdateCard'],
  ]) {
    try {
      ELS.clear();
      await vm.runInContext(_h[1] + '()', ctx);
      await new Promise(r => setTimeout(r, 300));
      const mhtml = [...ELS.values()]
        .map(e => (e._html || '') + '\n' + (e.textContent || '')).join('\n');
      if (mhtml.length < 40) {
        console.log('  ⚠ ' + _h[0] + '：渲染内容过少（' + mhtml.length + ' 字符）');
        empty++;
        continue;
      }
      const mcjk = visibleHan(mhtml);
      if (mcjk) {
        console.log('  ✗ ' + _h[0] + '：英文界面残留中文 ' + mcjk.length + ' 处→ '
          + [...new Set(mcjk)].slice(0, 8).join(' / '));
        if (process.env.SHOW_CTX) {
          const re3 = /[\u4e00-\u9fff]+/g; let m3, k3 = 0;
          while ((m3 = re3.exec(mhtml)) && k3 < 8) {
            const a3 = Math.max(0, m3.index - 70);
            console.log('      [%d] %s', ++k3,
              mhtml.slice(a3, m3.index + m3[0].length + 20).replace(/\n/g, ' '));
          }
        }
        han++; modBad++;
      } else {
        modOk++;
      }
    } catch (e) {
      console.log('  ✗ ' + _h[0] + '：渲染异常 ' + e.message);
      bad++; modBad++;
    }
  }

  // —— update 卡的**失败分支**（!d.ok：msg_cn + errors）——
  //    真机 check 有缓存走的是 ok:true 分支，这里换一份失败 payload 再渲一次。
  try {
    vm.runInContext('window.__updCheckOverride = ' +
      JSON.stringify(REAL.REAL_UPDATE_CHECK_FAIL) + ';', ctx);
    ELS.clear();
    await vm.runInContext('drouterRenderUpdateCard()', ctx);
    await new Promise(r => setTimeout(r, 300));
    const fhtml = [...ELS.values()]
      .map(e => (e._html || '') + '\n' + (e.textContent || '')).join('\n');
    const fcjk = visibleHan(fhtml);
    if (fcjk) {
      console.log('  ✗ update(失败分支)：英文界面残留中文 ' + fcjk.length + ' 处→ '
        + [...new Set(fcjk)].slice(0, 8).join(' / '));
      han++; modBad++;
    } else { modOk++; }
  } catch (e) {
    console.log('  ✗ update(失败分支)：渲染异常 ' + e.message);
    bad++; modBad++;
  }

  // —— 后端 update 模块的文案钩子直测 ——
  //    卡片/进度弹窗不都能靠一次渲染覆盖（进度态要真跑一次更新），
  //    所以直接喂后端源码里的全部文案。字符串取自 backend/drouter-update.py。
  try {
    const I = vm.runInContext('window.__drouterUpdI18n', ctx);
    if (!I) { console.log('  ✗ update 文案钩子缺失'); bad++; }
    else {
      const CASES = [
        ['src', 'GitHub 官方'], ['src', 'ghproxy 镜像'], ['src', 'kkgithub 镜像'],
        ['asset', { key: 'deb', desc: 'Deb 安装包（推荐，523 KB）' }],
        ['asset', { key: 'offline', desc: '离线安装包（87 MB，断网可装）' }],
        ['asset', { key: 'docker', desc: 'Docker 镜像（169 MB）' }],
        ['text', '下载中'], ['text', '准备下载…'], ['text', '准备开始'],
        ['text', '正在备份当前配置…'], ['text', '备份完成，开始下载…'],
        ['text', '正在从 ghproxy 镜像 下载…'], ['text', '正在校验文件完整性…'],
        ['text', '下载完成并已保存到 /var/lib/drouter/update'],
        ['text', '已取消，未做任何改动'], ['text', '下载失败：连接超时，外网可能不通'],
        ['text', '下载失败：所有下载源均不可用'],
        ['text', '备份失败，已中止更新：备份程序未产出备份包（可能未开启自动备份）'],
        ['text', '更新过程出错：连接被拒绝'],
        ['text', '更新过程出错：HTTPS 证书校验失败（可能被劫持或需要安装 CA）'],
        ['text', '更新过程出错：附件不存在（该版本可能没发布这个文件）'],
        ['text', '当前没有进行中的任务'],
        ['text', '校验不通过：文件与官方 SHA256 不一致'],
        ['text', '无法访问 GitHub，请检查本机外网连通性'],
        ['text', '版本号格式不对'], ['text', '未知的附件类型：tar'],
        ['text', '已有更新任务在进行中'], ['text', '已开始更新'],
        ['text', '官方 SHA256（下载源：ghproxy 镜像）'],
        ['text', '未获取到 SHA256 清单，本次下载**未做完整性校验**（下载源：ghproxy 镜像）'],
        ['text', '安装：sudo apt install ./drouter_1.0.9_all.deb（或 dpkg -i 后 apt --fix-broken install）'],
        ['text', '离线安装：解包后进入目录执行 ./install-offline.sh，全程不需要外网'],
        ['text', 'Docker：docker load -i drouter-1.0.9-docker.tar 然后 docker compose up -d'],
        ['err', 'GitHub 官方：连接超时'],
        ['err', 'kkgithub 镜像：返回内容里没有可识别的版本号'],
        ['err', 'GitHub 官方：HTTP 403'],
      ];
      let ubad = 0;
      for (const [kind, v] of CASES) {
        const out = String(kind === 'asset' ? I.asset(v)
          : kind === 'src' ? I.src(v) : kind === 'err' ? I.err(v) : I.text(v));
        const c = visibleHan(out);
        if (c) { ubad++; console.log('  ✗ update 文案残留中文 [' + kind + '] '
          + JSON.stringify(v) + ' → ' + out); }
      }
      if (ubad) { han++; modBad++; } else { modOk++; }
    }
  } catch (e) {
    console.log('  ✗ update 文案钩子异常 ' + e.message);
    bad++; modBad++;
  }
  if (modOk + modBad !== 6) { console.log('  ✗ 独立模块渲染数 != 6，覆盖不完整'); bad++; }

  // —— 健康总览的**有问题**分支（红/黄灯 + 「去处理」按钮）——
  //    真机当前 22 个模块全绿，异常分支拿不到，这里换一份派生 payload 再渲一次。
  try {
    vm.runInContext('window.__stOverride = ' +
      JSON.stringify(REAL.REAL_STATUS_BAD) + ';', ctx);
    ELS.clear();
    await vm.runInContext('viewHealth()', ctx);
    await new Promise(r => setTimeout(r, 300));
    const bhtml = [...ELS.values()]
      .map(e => (e._html || '') + '\n' + (e.textContent || '')).join('\n');
    const bcjk = visibleHan(bhtml);
    if (bcjk) {
      console.log('  ✗ health(异常分支)：英文界面残留中文 ' + bcjk.length + ' 处→ '
        + [...new Set(bcjk)].slice(0, 8).join(' / '));
      han++; modBad++;
    } else { modOk++; }
  } catch (e) {
    console.log('  ✗ health(异常分支)：渲染异常 ' + e.message);
    bad++; modBad++;
  }
  if (modOk + modBad !== 7) { console.log('  ✗ 独立模块/分支渲染数 != 7，覆盖不完整'); bad++; }
  console.log('='.repeat(60));
  console.log(`视图总数 ${names.length}｜异常 ${bad}｜疑似空渲染 ${empty}｜英文残留中文的视图 ${han}`);
  console.log(`独立模块 4 个 + update 失败分支 + 文案钩子 + health 异常分支｜干净 ${modOk}｜有问题 ${modBad}`);
  const dockerCalls = CALLED.filter(c => /\/api\/docker/.test(c.url)).length;
  console.log(`docker 接口调用次数：${dockerCalls}`);
  process.exit(bad || han ? 1 : 0);
})();
