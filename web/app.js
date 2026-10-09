/* drouter 管理台前端逻辑 —— 原生 JS，无依赖 */
'use strict';

/* T 是 t() 的**不受局部变量遮蔽**的别名。
 *
 * ⛔ 为什么需要（2026-10-07）：`t` 这个名字在 app.js 里被 6 个函数
 *    用作**形参**（tmThemeCard(t, on) 的 t 是主题对象…），
 *    函数体内的 `t` 指向形参而不是全局翻译函数 ——
 *    渲染时直接抛 `t is not a function`（check-render 抓到）。
 */
const T = window.i18n ? window.i18n.t : window.t;

/* enOrZh(en, zh)：语言感知二选一 —— 真身定义在 i18n.js（window.enOrZh）。
   离线渲染检查器（check-render.js）不加载 i18n.js，sandbox 里没有它 ——
   这里给兜底：恒取 zh，与 t() 离线恒等回落中文的行为一致。
   ⚠️ 不能用裸名 enOrZh(...)：i18n.js 是把它挂到 window 上的，
   check-render 的 sandbox.window = sandbox 且没有该键 → 裸名直接 ReferenceError。 */
const enOrZh = window.enOrZh || function (en, zh) {
  return (zh !== undefined && zh !== null) ? zh : en;
};

/* 后端下发文案的英文取值：bt4(表名, 键, 字段, 后端中文原文)。
   bt() 定义在 i18n.js（3 参、查不到返回 ''）；这里补一个带 fallback 的，
   调用点写成 esc(bt4('QOS_PRESETS', p.id, 'name', p.name)) ——
   查不到 / 中文界面时回落后端原文，**行为与未国际化时完全一致**。

   为什么不能直接给 133 处 `esc(x.name)` 统一套一层：变量名高度复用
   （`x.name` 可能是 5 张表里的任何一张），没法按变量名映射到表。
   所以逐处显式写明表名 —— 换来的是「查错表」时立刻能看出来
   （返回空 → 回落中文，而不是显示别的表的英文）。 */
function bt4(table, key, field, fallback) {
  if (typeof bt === 'function') {
    const v = bt(table, key, field);
    if (v) return v;
  }
  return fallback == null ? '' : String(fallback);
}


/* ============================ 基础 ============================ */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => {
  // 后端偶尔会把「对象」塞进本该是文本的字段（例如 NAT 检测的 detail 是 dict）。
  // 直接 String(obj) 会得到 "[object Object]"，用户完全看不出是什么、也没法排查。
  // 这里退化成紧凑 JSON：仍然一眼能看出「这里的数据结构不对」，但至少有内容可读。
  if (s != null && typeof s === 'object') {
    try { s = JSON.stringify(s); } catch (e) { /* 循环引用等，交给下面兜底 */ }
  }
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
};
/* 只放行 http/https 链接：javascript:/data: 这类伪协议绝不能进 href */
const httpUrl = u => { const s = String(u == null ? '' : u).trim(); return /^https?:\/\//i.test(s) ? s : ''; };
/* 数字输入框的取值：清空时 Number('') === 0，会把 MTU/MRU/租期提交成 0。
   0 在后端要么被钳到下限、要么生成非法配置，用户却以为「我清空了=用默认」。
   所以空串一律给 undefined，让后端的默认值生效。 */
const numOr = v => { const s = String(v == null ? '' : v).trim(); return s === '' ? undefined : Number(s); };
const fmtBytes = n => {
  n = Number(n) || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
};
const fmtDur = s => {
  s = Math.floor(Number(s) || 0);
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + `${T('天')} ` : '') + (h ? h + T('小时 ') : '') + m + `${T('分')}`;
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

const S = { token: localStorage.getItem('drouter_token') || '', user: '', cfg: {}, ifaces: [], meta: {},
  page: 'dash', buildMode: true, navOpen: null, navKw: '',
  docker: {}, share: {}, shareData: {}, acl: {},
  theme: {}, themeMeta: {}, themeEdit: null, themeTab: 'gallery' };



function toast(msg, kind = 'ok', ms = 3600) {
  const t = $('#toast');
  t.className = 'toast ' + kind;
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.add('hidden'), ms);
}

async function api(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (S.token) headers['X-Token'] = S.token;
  // 告诉后端当前界面语言 —— 后端据此决定 msg_en 是真英文还是回落中文。
  // ⚠️ 必须每次都带：语言是可切换的，用缓存的 lang 会在切换后漏发。
  const _lang = (window.i18n && window.i18n.getLang) ? window.i18n.getLang() : 'zh-CN';
  headers['X-Lang'] = _lang;
  let res;
  try {
    res = await fetch(path, Object.assign({ method: opts.method || 'GET' }, opts,
      { headers, body: opts.body ? JSON.stringify(opts.body) : undefined }));
  } catch (e) {
    // 浏览器抛的是原生英文异常（Failed to fetch / NetworkError 等），
    // 直接拼进 msg_cn 会让界面中英混杂。按 message 归类成可读的中文原因，
    // 原始串只放进 detail（排错时看得到，又不会污染主提示）。
    const why = netErrCn(e && e.message);
    return { ok: false, code: 'NET', msg_cn: why, detail: String((e && e.message) || e) };
  }
  if (res.status === 401) {
    logout(true);
    return { ok: false, code: 'UNAUTH', msg_cn: T('登录已过期，请重新登录') };
  }
  let _j;
  try { _j = await res.json(); }
  catch (e) {
    return { ok: false, code: 'PARSE',
             msg_cn: `${T('服务返回的不是合法 JSON（HTTP ')}${res.status}${T('），')}` +
                     T('请确认管理后台进程正常'),
             detail: String((e && e.message) || e) };
  }
  // 1.0.10 起后端按 X-Lang 选好了文案，放在 `msg` 字段里
  // （msg_cn / msg_en 是两个孪生的原文）。
  // ⚠️ **在这里统一回写 msg_cn**，而不是让 271 个 `r.msg_cn` 逐个改成
  //    `r.msg || r.msg_cn` —— 那样既漏改动又难审。
  //    代价是 msg_cn 这个名字从此表示「当前语言的文案」，
  //    需要中文时用 `r.msg_cn_raw`（下面保留）。
  if (_j && typeof _j === 'object' && _j.msg) {
    _j.msg_cn_raw = _j.msg_cn;   // 保留中文原文，个别地方要显示
    _j.msg_cn = _j.msg;
  }
  return _j;
}

/* 把浏览器原生的 fetch 异常翻译成中文原因。
   只按 message 里的关键词归类 —— 拿不到具体原因时给通用说法，
   宁可笼统也不要让英文串漏到界面上。 */
function netErrCn(raw) {
  const s = String(raw || '');
  if (/abort/i.test(s)) return T('请求被中断，可能是在页面切换时自动取消');
  if (/timeout|timed out/i.test(s)) return T('连接路由器管理服务超时，请检查网络或稍后重试');
  if (/certificate|ssl|tls/i.test(s))
    return T('与路由器管理服务的 HTTPS 证书协商失败，请确认访问的是正确地址');
  if (/permission|denied/i.test(s)) return T('浏览器拒绝了该请求，请检查权限设置');
  if (/resolve|dns|name/i.test(s))
    return T('无法解析路由器管理服务的地址，请确认 IP 或域名是否正确');
  if (/refused/i.test(s)) return T('连接被拒绝，路由器管理服务可能未在运行');
  if (/network|failed|fetch/i.test(s))
    return T('无法连接到路由器管理服务，请确认地址可达、服务未停止');
  return T('无法连接到路由器管理服务');
}

function setActionMsg(msg, kind = '') {
  const el = $('#ab-msg');
  el.className = 'ab-msg ' + kind;
  el.textContent = msg;
}

/* ============================ 登录 ============================ */
async function doLogin() {
  const u = $('#lg-user').value.trim(), p = $('#lg-pass').value;
  $('#lg-btn').disabled = true;
  const r = await api('/api/login', { method: 'POST', body: { username: u, password: p } });
  $('#lg-btn').disabled = false;
  if (!r.ok) { $('#lg-msg').className = 'msg err'; $('#lg-msg').textContent = r.msg_cn; return; }
  S.token = r.data.token; S.user = r.data.user;
  localStorage.setItem('drouter_token', S.token);
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  boot();
}

function logout(silent) {
  S.token = ''; localStorage.removeItem('drouter_token');
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
  if (!silent) api('/api/logout', { method: 'POST' });
}

/* ============================ 导航 ============================ */
/* 菜单结构：分组（可折叠、带主题色）→ 页面。
   每个分组有独立色相 hue，用于左侧色条/图标底色，便于快速区分功能域。
   tag:'off' 表示该页尚未开发，点进去显示「等待作者完善」。 */
const NAV_GROUPS = () => ([
  {
    g: T('概览'), icon: '◎', hue: 210, items: [
      { k: 'wizard', n: T('新手向导'), t: T('第一次用这台机器上网？四步走完：接上外网 · 给内网发地址 · 配 DNS · 要不要 IPv6。每一步都带范例和体检结果'), needSave: false },
      { k: 'dash', n: T('系统概览'), t: T('系统概览') },
      { k: 'health', n: T('st.nav'), t: T('st.navTip') },
      { k: 'netstat', n: T('网络状态 / 加速'), t: T('网络状态 · NAT 检测 · 软加速') },
    ],
  },
  {
    g: T('接口'), icon: '⇄', hue: 268, items: [
      { k: 'iface', n: T('网卡与桥接'), t: T('网卡与桥接'), needSave: true },
      { k: 'wan', n: `WAN ${T('口')}`, t: T('WAN 口设置'), needSave: true },
      { k: 'pppmulti', n: T('PPPoE 多拨'), t: T('PPPoE 多拨 / 多会话聚合') },
      { k: 'lan', n: `LAN ${T('口')}`, t: T('LAN 口设置'), needSave: true },
      { k: 'vlan', n: 'VLAN / IPTV', t: T('VLAN 与 IPTV 划分') },
      { k: 'wol', n: T('网络唤醒 (WOL)'), t: T('网卡远程唤醒 WOL') },
    ],
  },
  {
    g: T('寻址与路由'), icon: '⌘', hue: 190, items: [
      { k: 'dhcp', n: T('DHCP 服务'), t: T('DHCP 服务与 Options'), needSave: true },
      { k: 'dns', n: T('DNS 服务'), t: T('DNS 服务'), needSave: true },
      { k: 'ipv6', n: 'IPv6 / RA', t: T('IPv6 与路由通告'), needSave: true },
      { k: 'dhcpv6', n: T('DHCPv6 / 前缀委派'), t: T('DHCPv6 与前缀委派'), needSave: true },
      { k: 'ddns', n: T('动态域名 DDNS'), t: T('动态域名解析 DDNS（IPv4 / IPv6，国内外服务商）'), needSave: false },
      { k: 'pubip', n: T('真·公网 IP 判定'), t: T('判定出口 IP 是不是真的能被外网连进来（入向可达性实测）'), needSave: false },
    ],
  },
  {
    g: T('安全'), icon: '⛨', hue: 0, items: [
      { k: 'fw4', n: T('防火墙 IPv4'), t: T('IPv4 防火墙'), needSave: true },
      { k: 'fw6', n: T('防火墙 IPv6'), t: T('IPv6 防火墙'), needSave: true },
      { k: 'portfwd', n: T('端口转发 / DMZ'), t: T('端口转发与 DMZ（nftables DNAT）'), needSave: true },
      { k: 'upnp', n: 'UPnP / NAT-PMP', t: 'UPnP / NAT-PMP', needSave: true },
      { k: 'acl', n: T('访问控制 / 时间组'), t: T('访问控制与家长时间组'), needSave: true },
      { k: 'vpn', n: 'WireGuard VPN', t: T('远程连回家里访问内网与 NAS：Debian 13 内核自带 WireGuard，逐台设备发放独立配置'), needSave: false },
    ],
  },
  {
    g: T('服务'), icon: '✦', hue: 155, items: [
      { k: 'qos', n: T('智能限速 QoS'), t: T('智能限速与流量整形（CAKE / HTB）') },
      { k: 'dpi', n: T('应用识别 DPI'), t: T('应用识别与规则库更新（nDPI）') },
      { k: 'ntp', n: T('NTP 时间同步'), t: T('NTP 时间同步'), needSave: true },
      { k: 'nfs', n: T('文件共享 SMB/NFS'), t: T('内网文件共享（SMB / NFS，跨平台模板）'), needSave: true },
      { k: 'print', n: T('打印服务'), t: T('CUPS 打印服务器 / USB 打印机 RAW 直通（互斥二选一），共享给手机与电脑'), needSave: false },
      { k: 'opensoho', n: T('AC/AP 管理中心'), t: T('OpenSOHO 无线控制器：集中管理 OpenWRT AP 的 Wi-Fi · VLAN · PoE'), needSave: false },
    ],
  },
  {
    g: T('工具'), icon: '⚙', hue: 35, items: [
      { k: 'webshell', n: T('Web 终端 / 文件'), t: T('Web SSH 终端与文件管理器') },
      { k: 'api', n: T('通用 API 接口'), t: T('对外通用 API 接口与调用范例') },
      { k: 'diag', n: T('网络诊断工具'), t: T('网络诊断工具') },
      { k: 'v6test', n: T('IPv6 连通性测试'), t: T('IPv6 连通性测试') },
      { k: 'docker', n: 'Docker / Compose', t: T('Docker 与 Docker Compose 管理面板') },
      { k: 'dcfg', n: T('Docker 引擎配置'), t: T('daemon.json 可视化配置：IPv6 一键开启 · 国内镜像源切换与测速 · 日志滚动 · 默认网桥网段'), needSave: false },
    ],
  },
  {
    g: T('日志与审计'), icon: '☰', hue: 220, items: [
      { k: 'log', n: T('系统日志'), t: T('日志查看') },
      { k: 'alert', n: T('告警与通知'), t: T('掉线 / 磁盘满 / 温度高主动推送：Bark · 邮件 · 群机器人，带冷却与免打扰'), needSave: false },
      { k: 'flowlog', n: T('连接与流日志'), t: T('连接跟踪与流量日志（统一日志系统）') },
      { k: 'quota', n: T('用量与账单'), t: T('按设备与服务统计每月流量，工作室可按比例分摊话费；后台增量聚合，不会拖慢机器'), needSave: false },
    ],
  },
  {
    g: T('系统'), icon: '⛁', hue: 250, items: [
      { k: 'depcheck', n: T('依赖自检与安装'), t: T('运行依赖自检与一键安装') },
      { k: 'cleanup', n: T('磁盘与日志清理'), t: T('回收日志 / 缓存 / 临时文件，可设定阈值自动清理，防止小硬盘被占满'), needSave: false },
      { k: 'kern', n: T('内核转发与加速'), t: T('IP 转发 · 出向伪装 · MSS 钳制 · BBR · SNMP，五项内核级开关（每项附说明与联动影响）'), needSave: false },
      { k: 'ca', n: T('证书 / SSL'), t: T('CA 证书管理：建自己的 CA · 签发服务器证书 · 导入 · 部署给管理后台；附带 SSL/TLS 握手体检工具'), needSave: false },
      { k: 'backup', n: T('配置备份与还原'), t: T('把全部设置导成一个能下载、能换机还原的包：带清单与逐文件校验，还原前先预检'), needSave: false },
      { k: 'power', n: T('电源控制'), t: T('电源控制') },
      { k: 'user', n: T('用户与密钥'), t: T('系统用户与 SSH 公钥') },
      { k: 'sys', n: T('系统设置'), t: T('系统设置') },
      { k: 'theme', n: T('主题之家'), t: T('主题之家（Web 设计 · 离线预览 · 导入导出）') },
      { k: 'update', n: T('升级与保护'), t: T('系统升级与 RealVNC 保护') },
    ],
  },
]);

/* 扁平化：供 go() 查标题等 */
const PAGES = (() => {
  const out = [];
  NAV_GROUPS().forEach(gr => gr.items.forEach(it => out.push(Object.assign({ grp: gr.g }, it))));
  return out;
})();

/* 页面 → 「保存」与「应用」各自要处理哪些配置模块。
   两者必须分开声明，原因来自两个真实踩过的坑：
     1) system（网卡与桥接 / LAN 口）与 portfwd（端口转发）是「纯配置」模块：
        只入库、没有自己的配置文件，拿去 /api/apply 会直接报「未知的模块」；
     2) 端口转发的规则是作为上下文注入 nft_v4 / nft_v6 一起渲染的，
        所以要「保存 portfwd、应用 nft_v4+nft_v6」，顺序不能反。

   VLAN / WOL / QoS / DPI / 多拨 这些页面有自己的页内按钮（走 /api/vlan、
   /api/qos …），故意不登记在这里 —— 没登记就不会显示全局动作条，
   免得用户点了顶栏按钮却得到一个「当前页面无需保存」的假提示。 */
const PAGE_MODULES = {
  iface:   { save: ['system'],            apply: [] },
  lan:     { save: ['system', 'dnsmasq'], apply: [] },
  wan:     { save: ['pppoe'],             apply: ['pppoe'] },
  dhcp:    { save: ['dnsmasq'],           apply: ['dnsmasq'] },
  dns:     { save: ['dnsmasq'],           apply: ['dnsmasq'] },
  dhcpv6:  { save: ['dhcpv6'],            apply: ['dhcpv6'] },
  ipv6:    { save: ['radvd', 'dhcpv6'],   apply: ['radvd', 'dhcpv6'] },
  fw4:     { save: ['nft_v4'],            apply: ['nft_v4'] },
  fw6:     { save: ['nft_v6'],            apply: ['nft_v6'] },
  portfwd: { save: ['portfwd'],           apply: ['nft_v4', 'nft_v6'] },
  upnp:    { save: ['upnp'],              apply: ['upnp'] },
  ntp:     { save: ['ntp'],               apply: ['ntp'] },
  // 系统设置页也在改 system（访问范围 / 允许网段），并会提示
  // 「请点击保存并应用」。不登记的话动作条不显示，用户点了提示里的
  // 「保存并应用」却找不到那个按钮，改动只能靠重新进页面丢掉。
  // 只保存不 apply：system 模块不在 render 的可应用列表里，
  // 访问控制由 Web 层读配置后自己判断，不该走去重启服务那条路。
  sys:     { save: ['system'],            apply: [] },
};

/* 页面 key → 渲染函数。缺失此项会导致点击菜单时报
   "VIEWS is not defined"，页面永远停在「正在载入…」。 */
const VIEWS = {
  wizard: viewWizard,
  dash: viewDash,
  health: viewHealth,
  netstat: viewNetStat,
  iface: viewIface,
  wan: viewWan,
  pppmulti: viewPppMulti,
  lan: viewLan,
  vlan: viewVlan,
  wol: viewWol,
  dhcp: viewDhcp,
  dns: viewDns,
  ipv6: viewIpv6,
  dhcpv6: viewDhcpv6,
  ddns: viewDdns,
  pubip: viewPubip,
  fw4: viewFw,
  fw6: viewFw,
  portfwd: viewPortfwd,
  acl: viewAcl,
  vpn: viewVpn,
  nfs: viewNfs,
  docker: viewDocker,
  dcfg: viewDcfg,
  print: viewPrint,
  opensoho: viewOpensoho,
  upnp: viewUpnp,
  qos: viewQos,
  dpi: viewDpi,
  ntp: viewNtp,
  diag: viewDiag,
  v6test: viewV6Test,
  webshell: viewWebShell,
  api: viewApi,
  log: viewLog,
  flowlog: viewFlowLog,
  quota: viewQuota,
  theme: viewTheme,
  depcheck: viewDepCheck,
  cleanup: viewCleanup,
  kern: viewKern,
  ca: viewCa,
  backup: viewBackup,
  alert: viewAlert,
  power: viewPower,
  user: viewUser,
  sys: viewSys,
  update: viewUpdate,
};

/* 折叠状态持久化（默认只展开当前页所在分组 + 概览） */
const NAV_OPEN = (() => {
  try {
    const raw = localStorage.getItem('drouter_nav_open');
    if (!raw) return null;
    const o = JSON.parse(raw);
    if (!o || typeof o !== 'object') return null;
    // 1.0.10 起键从「分组中文名」改成「'g' + 序号」（分组名会随语言变，
    // 而这里持久化 —— 用中文当键的话，用户切一次语言展开状态就全丢）。
    // 老数据里是中文键，做一次**就地迁移**而不是直接丢弃：
    // 否则老用户升级后所有分组的展开状态被静默重置（虽然不影响功能，
    // 但「我明明展开了」会变成莫名其妙）。
    if (!Object.keys(o).some(k => /^g\d+$/.test(k))) {
      const moved = {};
      NAV_GROUPS().forEach((g, i) => {
        if (o[g.g]) moved['g' + i] = true;
      });
      return Object.keys(moved).length ? moved : null;
    }
    return o;
  } catch (e) { /* ignore */ }
  return null;
})();

function navSaveOpen() {
  try { localStorage.setItem('drouter_nav_open', JSON.stringify(S.navOpen || {})); } catch (e) { /* ignore */ }
}

/* 分组图标：用色相生成柔和徽标底色 */
function grpIcon(gr, on) {
  return `<span class="ng-ico" style="--h:${gr.hue}">${gr.icon}</span>`;
}

/* 取导航类词条：T() 查不到时**返回 key 原文**（非空！），
   所以 `T('nav.n.' + k) || it.n` 这种写法在漏词条时永远落到 key 上，
   菜单会显示成 `nav.n.health` 这种英文串 —— 2026-10-09 新增「健康总览」
   时漏登记 nav.n.* / nav.t.* 就是这个现象。这里显式判「查没查到」。 */
function navText(key, fallback) {
  const v = T(key);
  return (v && v !== key) ? v : (fallback || '');
}

function renderNav() {
  // ⚠️ S.navOpen 的键一律用 **'g' + 分组序号**（见下面 data-gh 的注释）：
  //    分组名会随语言变，而 navOpen 持久化在 localStorage，
  //    用中文当键的话用户切一次语言，展开状态就再也对不上了。
  const gkeyOf = grpName => {
    const i = NAV_GROUPS().findIndex(g => g.g === grpName);
    return i >= 0 ? 'g' + i : grpName;
  };
  if (!S.navOpen) {
    S.navOpen = NAV_OPEN || {};
    if (!NAV_OPEN) { S.navOpen['g0'] = true; }
  }
  const cur = PAGES.find(p => p.k === S.page);
  if (cur) S.navOpen[gkeyOf(cur.grp)] = true;
  // ⚠️ 搜索要用**当前语言**的文案：用户切到英文后输入 "firewall"，
  //    如果拿中文 it.n 去匹配就搜不到任何东西。
  const kw = (S.navKw || '').trim().toLowerCase();
  const gname = (gi, zh) => T('nav.g.g' + gi) || zh;
  const iname = it => navText('nav.n.' + it.k, it.n);
  const html = NAV_GROUPS().map((gr, gi) => {
    const gzh = gr.g;
    const gen = gname(gi, gzh);
    const items = gr.items.filter(it => {
      if (!kw) return true;
      // 中英都参与匹配（中文界面搜英文也能命中，反之亦然）
      const hay = (it.n + it.t + gzh + iname(it) + navText('nav.t.' + it.k, it.t) + gen)
        .toLowerCase();
      return hay.includes(kw);
    });
    if (!items.length) return '';
    const open = !!S.navOpen['g' + gi] || !!kw;
    const hasCur = items.some(it => it.k === S.page);
    // ⚠️ data-gh / S.navOpen 的键用**分组序号**，不用分组名。
    //    原因：分组名会随语言变（"安全" / "Security"），而 navOpen 是
    //    **持久化**到 localStorage 的 —— 用中文当键的话，用户切一次语言
    //    之前展开的分组就再也对不上了（key 变成 'Security'，找不到）。
    //    序号稳定，与语言无关。
    const gk = 'g' + gi;
    return `<div class="ngroup${open ? ' open' : ''}${hasCur ? ' has-cur' : ''}" style="--h:${gr.hue}" data-g="${esc(gk)}">
      <button class="ng-head" data-gh="${esc(gk)}" aria-expanded="${open}">
        ${grpIcon(gr, open)}
        <span class="ng-t">${esc(gen)}</span>
        <span class="ng-n">${items.length}</span>
        <svg class="ng-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2.6" stroke-linecap="round"
             stroke-linejoin="round"><path class="ng-arrow-i" d="M9 6l6 6-6 6"/></svg>
      </button>
      <div class="ng-body">${items.map(it => `
        <button class="nitem${S.page === it.k ? ' on' : ''}" data-k="${esc(it.k === 'files' ? 'webshell' : it.k)}"
                title="${esc(T('nav.t.' + it.k) || it.t)}">
          <span class="nitem-dot"></span>
          <span class="nitem-t">${esc(iname(it))}</span>
          ${it.tag === 'soon' ? `<span class="nitem-tag">${T('nav.soon')}</span>` : ''}
        </button>`).join('')}</div>
    </div>`;
  }).join('');
  $('#nav').innerHTML = html || `<div class="nav-empty">${T('nav.noMatch')}</div>`;

  $$('#nav .ng-head').forEach(b => b.onclick = () => {
    const g = b.dataset.gh;
    S.navOpen[g] = !S.navOpen[g];
    navSaveOpen();
    renderNav();
  });
  $$('#nav .nitem').forEach(b => b.onclick = () => go(b.dataset.k));
}

/* ------------------------------------------------------------------
   页面级轮询定时器统一管理
   ------------------------------------------------------------------
   为什么要有这个函数：单文件 SPA 里最容易漏的就是「切走页面却不停轮询」。
   之前每个视图只在自己函数开头清自己的定时器 —— 那是「同一个页面重复进入」
   的清理，**切到别的页面时没人清**。结果是：去 WAN 口开了「自动刷新」，
   然后切到概览，WAN 日志仍在每 3 秒查一次 journal；DHCP 租约同理每 10 秒一次。
   在只有 2 核的软路由上，这些后台空转是实打实的浪费（还会持续拉起
   python helper 子进程）。

   所以：所有「只在某个页面内有效」的定时器都登记在这里，go() 一进来先全停，
   再由目标视图按需重新起。视图内的自检（如 `if (!box) return`）只能防住
   DOM 写入，防不住请求，必须靠这里统一关。
   ------------------------------------------------------------------ */
function stopPageTimers() {
  // 公网 IP 判定页有自己的一套轮询（含并发请求），交给它自己的停止函数
  if (typeof pubipProbeStopPoll === 'function') pubipProbeStopPoll();
  // 离开 Web 终端页：本地轮询有 #ws-screen 守卫会自己停，但服务端 PTY 会话
  // 不会主动断 —— 重进页面是全新表单、并不恢复旧会话，不断开就白占一个
  // PTY 直到 30 分钟闲置回收。注意此时 S.page 还是「正要离开」的页面。
  if (S.page === 'webshell' && typeof wsDisconnect === 'function' && WS.sid) wsDisconnect(true);
  // 以下都是 interval 型定时器，清掉后置 null，view 里会重新创建
  [DASH_TIMER, WANLOG_TIMER, FWL_TIMER, DK_TIMER, HEALTH_TIMER].forEach(t => clearInterval(t));
  DASH_TIMER = WANLOG_TIMER = FWL_TIMER = DK_TIMER = HEALTH_TIMER = null;
  // 这两个是「闭包内局部变量」，只能在各自的 view 里清 ——
  // 用全局钩子让它们把清理动作挂出来（见 viewDhcp / viewV6Test）
  if (typeof stopLeaseTimer === 'function') stopLeaseTimer();
  if (typeof stopV6Timer === 'function') stopV6Timer();
  // 实时柱状图轮询（3 秒一次，canvas 重绘）。不登记的话离开页面后
  // 会一直打 /api/metrics 且持续重绘 canvas。
  // 2026-10-07：本轮多次编辑中这两行被冲掉了（t-update 的
  // 「app.js 调用了渲染入口」「轮询已登记进 stopPageTimers」两条判据抓到的）。
  if (typeof window.__drouterRealtimeStop === 'function') {
    window.__drouterRealtimeStop();
  }
  // 2026-10-07：本轮多次编辑把这两个 stop 钩子冲掉了。
  // 上游链路详情（5 秒轮询）与网络明细（3 秒轮询）不登记的话，
  // 离开页面后仍在后台打接口并重绘。
  if (typeof window.__drouterNetDetailStop === 'function') {
    window.__drouterNetDetailStop();
  }
  if (typeof window.__drouterUpstreamStop === 'function') {
    window.__drouterUpstreamStop();
  }
}

/* 导航代次（navigation generation）——
   每次切页面 +1。视图函数在 await 之前记下当前代次，await 返回后如果代次
   变了，说明用户已经切走，直接放弃写入，避免「旧页面的数据覆盖新页面」。
   这是单文件 SPA 里比「每个视图各自判断」更可靠的统一解法：
   视图作者不需要记住加守卫，框架层兜住。 */
let NAV_GEN = 0;

/* 视图内部调用的辅助：在 await 前取一次代次，返回后用它校验 */
function navGen() { return NAV_GEN; }
function navStale(g) { return g !== NAV_GEN; }

/* 页面级「未保存草稿」登记 ——
   ACL / 文件共享 / DDNS 三个页面的表单状态全在 S 里，而进入页面时会无条件
   用服务端数据整体覆盖：用户在页内改了东西没保存、切去别的页再切回来，
   草稿就被静默冲掉。这是全站唯一真正不可逆的丢数据路径。
   约定：页内任何改动都 pageDirty(页名)；进入脏页时保留本地草稿直接重渲染；
   保存成功（或成功拉到新数据）后 pageClean(页名)。 */
const PAGE_DIRTY = {};
function pageDirty(k) { PAGE_DIRTY[k] = 1; }
function pageClean(k) { delete PAGE_DIRTY[k]; }
function pageIsDirty(k) { return !!PAGE_DIRTY[k]; }

function go(k) {
  // 先停掉上一个页面留下的所有轮询，再切页面。
  // 顺序很重要：必须在 S.page 赋值之前或之后都行，但一定要早于目标视图渲染。
  stopPageTimers();
  NAV_GEN++;          // 作废所有在途的旧视图请求
  S.page = k;
  closeDrawer();     // 手机端：选完菜单自动收起抽屉
  renderNav();
  const p = PAGES.find(x => x.k === k) || {};
  $('#page-title').textContent = (p.k ? navText('nav.t.' + p.k, p.t) : (p.t || ''));
  paintPageLight();     // 本页模块状态灯（#5）
  const bar = $('#actionbar');
  // 动作条只给「有全局保存/应用目标」的页面：PAGE_MODULES 里登记过的，
  // 以及 ACL / 文件共享这两个走专属接口、在处理器里单独分支的页面。
  const hasBar = !!PAGE_MODULES[k] || k === 'acl' || k === 'nfs';
  if (hasBar) { bar.classList.remove('hidden'); setActionMsg(T('改动后请点击「保存并应用」')); }
  else bar.classList.add('hidden');
  // 尚未开发的页面：给出统一占位，避免点进去像"坏了"
  if (p.tag === 'soon') { renderSoon(p); return; }
  $('#view').innerHTML = `<div class="card">${T('正在载入…')}</div>`;
  const fn = VIEWS[k];
  if (!fn) { renderSoon(p); return; }
  // 视图可能是 async；必须捕获异常，否则渲染失败会永久停在「正在载入…」
  // 同时用代次校验：如果渲染期间用户已经切到别的页面，就不要再用旧结果
  // 覆盖新页面（无论是成功渲染还是错误面板）。
  const gen = NAV_GEN;
  Promise.resolve()
    .then(() => fn())
    .then(() => { if (navStale(gen)) return; })
    .catch(e => {
      if (navStale(gen)) return;      // 已经切走，错误面板不该盖在新页面上
      const el = $('#view');
      // 原始异常（多半是英文）放小字footnote 供排错，主提示给中文。
      const raw = String((e && e.message) || e);
      if (el) el.innerHTML = `<div class="card" style="border-color:var(--err)">
        <h3 style="color:var(--err)">${T('页面渲染失败')}</h3>
        <p class="desc">${T('这一页在渲染时出错了，其余页面不受影响。')}</p>
        <p class="desc">${T('可点击右上角「刷新」重试，或到「日志」页查看详细信息。')}</p>
        <p class="desc" style="font-family:var(--mono);font-size:12px;opacity:.7">${T('技术细节：')}${esc(raw)}</p></div>`;
      toast(T('页面渲染失败，请点右上角「刷新」重试'), 'err', 6000);
    });
}

/* 占位页：功能已在规划中，界面结构先占位 */
function renderSoon(p) {
  $('#view').innerHTML = `
    <div class="card soon-card">
      <div class="soon-ico"><svg width="52" height="52"><use href="#drouter-logo"/></svg></div>
      <h3>${esc(p.t || T('该功能'))}</h3>
      <p class="desc" style="max-width:560px;margin:6px auto 0">
        ${T('这个模块的界面框架已经就位，具体功能还在排期开发中。')}
      </p>
      <div class="soon-pill">${T('等待作者完善')}</div>
      <p class="desc" style="margin-top:14px">
        ${T('你可以先继续使用其他已完成的模块。如果你希望优先开发这一项， 欢迎到项目主页反馈：')}
        <a href="https://github.com/liuzhuohua" target="_blank" rel="noopener noreferrer">github.com/liuzhuohua</a>
      </p>
    </div>`;
}

/* ============================ 概览 ============================ */
/* ==================== 模块健康总览（#5） ====================
   后端 /api/status（helper read:status）一次汇总全部模块的状态灯。
   它只回**稳定枚举 + 原始数值**，一个中文字符都不下发 ——
   所有模块名 / 状态码 / 指标名 / 提示语都由这里经 t() 出，
   这样英文界面天然不会残留中文（前几轮 i18n 的坑都出在「后端下发中文」）。

   灯的四档语义（颜色见 .st-dot）：
     ok   绿：正常工作
     warn 黄：能跑，但值得看一眼
     err  红：现在就有问题
     off  灰：用户没启用（不是故障）
     na   浅灰：环境不支持 / 不适用（如没装 Docker、虚拟机没有温度传感器）
*/
const ST_LEVELS = ['err', 'warn', 'ok', 'off', 'na'];
const ST_LEVEL_TAG = { ok: 'ok', warn: 'warn', err: 'err', off: 'gray', na: 'gray' };
/* 模块 → 出问题后该去哪一页处理 */
const ST_PAGE = {
  system: 'dash', wan: 'wan', dnsdhcp: 'dhcp', ipv6: 'ipv6', fw: 'fw4',
  upnp: 'upnp', ntp: 'ntp', ddns: 'ddns', acl: 'acl', share: 'nfs',
  docker: 'docker', dpi: 'dpi', qos: 'qos', vpn: 'vpn', accel: 'netstat',
  ulog: 'flowlog', snapshot: 'backup', backup: 'backup', update: 'update',
  alert: 'alert', print: 'print', ca: 'ca',
};
/* 指标展示顺序与单位（未列出的指标不展示，避免后端加字段就冒出来一堆英文） */
const ST_METRICS = [
  ['cpu', '%'], ['mem', '%'], ['disk', '%'], ['temp', ' °C'],
  ['mem_used_mb', ' MB'], ['mem_total_mb', ' MB'],
  ['disk_used_h', ''], ['disk_total_h', ''], ['load', ''], ['proc', ''],
  ['proto', ''], ['iface', ''], ['gw', ''], ['v4', ''], ['v6', ''], ['dns', ''],
  ['leases', ''], ['forwarding', ''], ['tables', ''], ['mappings', ''],
  ['synced', ''], ['offset', ' s'], ['records', ''], ['rules', ''],
  ['shares', ''], ['exports', ''], ['containers', ''], ['running', ''],
  ['version', ''], ['age_days', ' d'], ['down_mbit', ' Mbps'], ['up_mbit', ' Mbps'],
  ['ips', ''], ['peers', ''], ['port', ''], ['devices', ''], ['flows', ''],
  ['sources', ''], ['keep_days', ' d'], ['count', ''], ['age_hours', ' h'],
  ['auto', ''], ['local', ''], ['latest', ''], ['channels', ''], ['certs', ''],
];
const ST_BOOL = { synced: 1, forwarding: 1, auto: 1 };

let HEALTH_TIMER = null;        // 健康总览页的自动刷新
let HEALTH_LIGHT_TIMER = null;  // 顶栏状态灯（全局，与当前页无关）
let HEALTH_LAST = null;         // 最近一次 /api/status 结果（供顶栏灯与本页灯复用）
/* 页面 → 受监控模块。一个页面可能对应多个模块（如 backup 页同时管快照与备份），
   取第一个作为「本页灯」；点它进健康总览。 */
const ST_PAGE_REV = (() => {
  const m = {};
  for (const [mod, page] of Object.entries(ST_PAGE)) {
    if (!m[page]) m[page] = mod;
  }
  return m;
})();

/* 本页模块状态灯：贴在页面标题右边。
   只显示「有对应模块」的页面 —— 系统设置、主题之家这类纯配置页没有可监控对象，
   硬塞一个灰灯只会让人以为坏了。 */
function paintPageLight() {
  const el = $('#page-health');
  if (!el) return;
  const mod = ST_PAGE_REV[S.page];
  const it = (HEALTH_LAST && HEALTH_LAST.items || []).find(x => x.k === mod);
  if (!mod || !it) { el.hidden = true; el.innerHTML = ''; return; }
  const hint = T('st.h.' + mod) || '';
  el.hidden = false;
  el.className = 'page-health lv-' + it.lv;
  el.innerHTML = `<span class="st-dot lv-${esc(it.lv)}"></span>`
    + `<span class="ph-txt">${esc(stLevelText(it.lv))}</span>`
    + (hint ? `<button type="button" class="st-info" title="${esc(hint)}"
        aria-label="${esc(T('st.tip'))}">i</button>` : '');
  el.onclick = (e) => { if (!e.target.classList.contains('st-info')) go('health'); };
}

function stLevelText(lv) { return T('st.lv.' + lv) || lv; }

function stCodeText(it) {
  const k = 'st.c.' + (it.c || 'unknown');
  const v = T(k);
  return (v && v !== k) ? v : (it.c || '');
}

/* 指标值 → 显示文本。数值型补单位，布尔型出 ✓/✗，协议名走 UPS_PROTO 表。 */
function stMetricValue(mk, v) {
  if (v === null || v === undefined || v === '') return '';
  if (mk === 'age_hours') return (typeof v === 'number' && v < 0) ? '' : (v + ' h');
  if (mk === 'proto') return bt('UPS_PROTO', v, 'en') || v;
  if (ST_BOOL[mk]) return v ? '✓' : '✗';
  const unit = (ST_METRICS.find(x => x[0] === mk) || [null, ''])[1];
  return String(v) + (unit || '');
}

function stMetricsHtml(it) {
  const m = it.m || {};
  const out = [];
  for (const [k] of ST_METRICS) {
    if (!(k in m)) continue;
    const val = stMetricValue(k, m[k]);
    if (val === '') continue;
    const lbl = T('st.m.' + k);
    out.push(`<span class="st-chip" title="${esc(lbl)}"><i>${esc(lbl)}</i><b>${esc(val)}</b></span>`);
  }
  return out.join('');
}

function stCard(it) {
  const name = T('st.k.' + it.k) || it.k;
  const hint = T('st.h.' + it.k) || '';
  const page = ST_PAGE[it.k];
  const canGo = page && it.lv !== 'ok' && it.lv !== 'off';
  return `<div class="st-card lv-${esc(it.lv)}" data-k="${esc(it.k)}">
    <div class="st-head">
      <span class="st-dot" aria-hidden="true"></span>
      <b class="st-name">${esc(name)}</b>
      <span class="tag ${ST_LEVEL_TAG[it.lv] || 'gray'}">${esc(stLevelText(it.lv))}</span>
      <span class="st-code">${esc(stCodeText(it))}</span>
      ${hint ? `<button type="button" class="st-info" title="${esc(hint)}"
        aria-expanded="false" aria-label="${esc(T('st.tip'))}">i</button>` : ''}
    </div>
    <div class="st-metrics">${stMetricsHtml(it)}</div>
    ${hint ? `<div class="st-hint" hidden>${esc(hint)}</div>` : ''}
    ${canGo ? `<div class="st-foot"><button type="button" class="small st-go"
      data-go="${esc(page)}">${esc(T('st.goto'))} →</button></div>` : ''}
  </div>`;
}

async function viewHealth() {
  const v = $('#view');
  if (HEALTH_TIMER) { clearInterval(HEALTH_TIMER); HEALTH_TIMER = null; }
  v.innerHTML = `<div class="card"><h3>${T('st.title')}</h3>
    <p class="desc">${T('st.loading')}</p></div>`;
  await paintHealth();
  // 自动刷新：切走时由 go() 的清理路径 clearInterval（见 go()）
  HEALTH_TIMER = setInterval(() => {
    if (document.hidden || S.page !== 'health') return;
    paintHealth();
  }, 15000);
}

async function paintHealth() {
  const v = $('#view');
  if (!v) return;
  const r = await api('/api/status');
  if (!r.ok || !r.data) {
    v.innerHTML = `<div class="card"><h3>${T('st.title')}</h3>
      <p class="desc">${esc(r.msg_cn || T('st.none'))}</p></div>`;
    return;
  }
  const d = r.data;
  const items = (d.items || []).slice();
  // 有问题的排前面（同级按固定顺序，避免每次刷新跳动）
  const rank = {}; ST_LEVELS.forEach((l, i) => { rank[l] = i; });
  items.sort((a, b) => (rank[a.lv] - rank[b.lv]));
  const bad = items.filter(x => x.lv === 'err' || x.lv === 'warn');
  const when = new Date((d.ts || 0) * 1000);
  const hhmm = `${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}:${String(when.getSeconds()).padStart(2, '0')}`;

  v.innerHTML = `
    <div class="card st-summary">
      <h3>${T('st.title')}</h3>
      <div class="st-sum-row">
        <span class="st-dot lv-${esc(d.worst || 'na')}"></span>
        <b>${esc(T('st.worstFmt', [stLevelText(d.worst || 'na')]))}</b>
        <span class="tag ${bad.length ? 'warn' : 'ok'}">${
          bad.length ? esc(T('st.problems')) + ' ' + bad.length : esc(T('st.allOk'))}</span>
        <span class="st-when">${T('st.updated')} ${esc(hhmm)}</span>
        <button type="button" class="small" id="st-refresh">${T('st.refresh')}</button>
      </div>
      <p class="desc" style="margin:10px 0 0">${T('st.desc')}</p>
    </div>
    <div class="st-grid">${items.map(stCard).join('')}</div>`;

  const rb = $('#st-refresh');
  if (rb) rb.onclick = () => paintHealth();
  v.querySelectorAll('.st-go').forEach(b => {
    b.onclick = () => go(b.dataset.go);
  });
  // ⓘ 展开/收起本卡片的行内说明（不用浮动气泡 —— 见 app.css 里的说明）
  v.querySelectorAll('.st-info').forEach(b => {
    b.onclick = (e) => {
      e.stopPropagation();
      const card = b.closest('.st-card');
      const h = card && card.querySelector('.st-hint');
      if (!h) return;
      h.hidden = !h.hidden;
      b.setAttribute('aria-expanded', String(!h.hidden));
    };
  });
}

/* ---------- 顶栏状态灯（全局）----------
   它只做一件事：让你在任何一个页面都能一眼看到「有没有模块出事」。
   点它跳到健康总览。刻意用 30s 慢轮询（比页面内的 15s 更慢）——
   它一直开着，成本要压到最低；后端那边还有 5s 的结果缓存兜底。 */
async function paintHealthLight() {
  const b = $('#tb-health');
  if (!b) return;
  const r = await api('/api/status');
  if (!r.ok || !r.data) { b.className = 'icon-btn tb-health lv-na'; return; }
  const d = r.data;
  HEALTH_LAST = d;
  paintPageLight();
  const bad = (d.items || []).filter(x => x.lv === 'err' || x.lv === 'warn');
  b.className = 'icon-btn tb-health lv-' + (d.worst || 'na');
  b.title = bad.length
    ? T('st.tbBad', [bad.length,
        bad.map(x => T('st.k.' + x.k) || x.k).slice(0, 4).join(' / ')])
    : T('st.tbOk');
  b.setAttribute('aria-label', b.title);
}

function initHealthLight() {
  const right = document.querySelector('.tb-right');
  if (!right || $('#tb-health')) return;
  const b = document.createElement('button');
  b.id = 'tb-health';
  b.type = 'button';
  b.className = 'icon-btn tb-health lv-na';
  b.innerHTML = '<span class="tb-h-dot"></span>';
  b.onclick = () => go('health');
  right.insertBefore(b, right.firstChild);
  if (HEALTH_LIGHT_TIMER) clearInterval(HEALTH_LIGHT_TIMER);
  // 切语言时立刻重绘：灯的颜色不用变，但 title（提示语）是 t() 出来的，
  // 不登记的话它会停在旧语言直到下一次 30s 轮询。
  if (window.i18n && window.i18n.register) {
    window.i18n.register('healthLight', paintHealthLight);
  }
  paintHealthLight();
  HEALTH_LIGHT_TIMER = setInterval(() => {
    if (document.hidden) return;
    paintHealthLight();
  }, 30000);
}

async function viewDash() {
  const v = $('#view');
  if (DASH_TIMER) { clearInterval(DASH_TIMER); DASH_TIMER = null; }
  const [si, met, svc, ifc, v6, route] = await Promise.all([
    api('/api/sysinfo'), api('/api/metrics'), api('/api/services'),
    api('/api/ifaces'), api('/api/ipv6'), api('/api/routes')]);
  const d = (si.data || {});
  const m = (met.data || {});
  const ifs = (ifc.data || []).filter(x => !x.is_virtual || x.name === 'docker0');
  const ppp = ifs.find(x => x.name.startsWith('ppp'));
  const lan = $w('lan') || {}, sys = S.cfg.system || {};
  const wanset = sys.wan_iface ? ifs.find(x => x.name === sys.wan_iface) : null;

  const bios = d.bios_info || {};
  const virt = d.virt || {};
  const cpu = d.cpu || {};
  const now = new Date();
  // 时间格式也走 i18n：中文「2026年1月5日 12时30分」/ 英文「2026-01-05 12:30」
  const timeStr = window.i18n && i18n.getLang() === 'en-US'
    ? `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`
    : fmtDateTime(now, true);

  // 磁盘展示：disk_root 是对象（之前直接拼进模板导致 [object Object]）
  const dk = m.disk || {};
  const diskSub = dk.total_h
    ? T('dash.diskRoot', [dk.used_h, dk.total_h, dk.free_h])
    : T('dash.diskRootNoSize');

  v.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <h3>${T('dash.hostInfo')}</h3>
      <div class="row" style="align-items:center">
        <div style="flex:1 1 0">
          <div class="kv"><b>${T('dash.hostname')}</b><span class="mono">${esc(d.hostname || '—')}</span></div>
          <div class="kv"><b>${T('dash.biosMode')}</b><span>${bios.firmware
      ? `<span class="tag ${bios.is_efi ? 'info' : 'ok'}">${esc(bt4('SYSINFO_NOTES', bios.firmware, 'n', bios.firmware))}</span>`
      : `<span class="tag gray">${T('dash.detecting')}</span>`}
      <span style="color:var(--txt3);font-size:12px">${esc(bios.vendor || '')} ${esc(bios.version || '')} ${esc(bios.date || '')}</span></span></div>
          <div class="kv"><b>${T('dash.platform')}</b><span>${virt.is_vm
      ? `<span class="tag warn" title="${esc(bt4('SYSINFO_NOTES', virt.note, 'd', virt.note) || '')}">⚠ ${esc(bt4('SYSINFO_NOTES', virt.name, 'n', virt.name) || T('dash.vmMachine'))}</span>
             <span style="color:var(--txt3);font-size:12px">${esc(virt.product || '')}</span>`
      : `<span class="tag ok">${T('dash.physMachine')}</span>
             <span style="color:var(--txt3);font-size:12px">${esc(virt.product || '')}</span>`}</span></div>
          <div class="kv"><b>${T('dash.os')}</b><span>${esc(d.os || d.distro || 'Debian GNU/Linux 13 (trixie)')} · ${T('dash.kernel')} ${esc(d.kernel || '')}</span></div>
          <div class="kv"><b>${T('dash.uptime')}</b><span id="dash-uptime">${fmtDur(d.uptime_s)}</span>
            <span style="color:var(--txt3);font-size:12px">${T('dash.load')} <span id="dash-load">${esc((m.load || d.load || []).join(' / '))}</span></span></div>
          <div class="kv"><b>${T('dash.currentTime')}</b><span id="dash-clock">${esc(timeStr)}</span></div>
        </div>
        <div style="flex:0 0 240px;text-align:center">
          <div style="font-size:12px;color:var(--txt3)"
               title="${esc(T('dash.netTotalTip'))}">
            ${T('dash.netTotal')}</div>
          <div style="margin-top:4px">
            <div id="dash-rx" style="font-size:20px;font-weight:600;color:#10b981">↓ — </div>
            <div id="dash-tx" style="font-size:20px;font-weight:600;color:#3b82f6">↑ — </div>
          </div>
          <div id="dash-net-extra" style="font-size:12px;color:var(--txt3);margin-top:6px">${T('dash.netExtra')}</div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px" id="upd-card">
      <h3>${T('upd.title')}</h3>
      <div class="kv"><b>${T('common.version')}</b><span class="mono" id="upd-local">${T('common.reading')}</span></div>
      <div class="upd-row"><span class="tag gray">${T('upd.detecting')}</span></div>
    </div>
    <div class="grid g4" style="margin-bottom:16px" id="dash-gauges">
      ${gauge(T('dash.cpuPct'), m.cpu_pct, cpu.model || (d.cpu_count || '—') + ' ' + T('dash.cores'), '#f59e0b',
      T('dash.cpuSub', [cpu.cores || d.cpu_count || '—', cpu.physical_cores || '—', cpu.mhz || '']))}
      ${gauge(T('dash.memPct'), (m.mem || {}).pct, `${(m.mem || {}).used_mb || 0} / ${(m.mem || {}).total_mb || 0} MB`, '#3b82f6',
      T('dash.memSub', [(m.mem || {}).avail_mb || 0, (m.mem || {}).total_mb || 0]))}
      ${gauge(T('dash.diskPct'), dk.pct, '', '#10b981', diskSub)}
      ${gauge(T('dash.procTemp'), d.proc_pct_scale != null ? d.proc_pct_scale : null,
      T('dash.procCount', [m.proc_count || d.proc_count || 0]), '#8b5cf6',
      ((m.temp_available != null ? m.temp_available : d.temp_available)
        ? T('dash.cpuTemp', [m.temp_c, m.temp_src || ''])
        : T('dash.noTempSensor'))
        + '　' + T('dash.procLimit', [Number(d.proc_max || 0).toLocaleString(), d.proc_pct != null ? d.proc_pct : '—']),
      ((m.temp_available != null ? !m.temp_available : !d.temp_available) && d.temp_hint) ? { warn: bt4('SYSINFO_NOTES', d.temp_hint, 'd', d.temp_hint) } : null)}
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3>${T('dash.netQuality')}</h3>
      <p class="desc">${T('dash.netQualityTip')}</p>
      <div class="grid g4" id="dash-quality">
        ${stat(T('dash.downRate'), '<span id="q-rx">—</span>', `<span id="q-rx-sub">${T('dash.realtime')}</span>`)}
        ${stat(T('dash.upRate'), '<span id="q-tx">—</span>', `<span id="q-tx-sub">${T('dash.realtime')}</span>`)}
        ${stat(T('dash.latency'), '<span id="q-rtt">—</span>', `<span id="q-rtt-sub">${T('dash.probing')}</span>`)}
        ${stat(T('dash.jitter'), '<span id="q-jit">—</span>', '<span id="q-jit-sub">—</span>')}
      </div>
      <div class="row" style="margin-top:8px;gap:16px">
        <span class="desc" style="margin:0">${T('dash.pps')}：<span id="q-pps" class="mono">—</span></span>
        <span class="desc" style="margin:0">${T('dash.estab')}：<span id="q-estab" class="mono">—</span></span>
        <span class="desc" style="margin:0">${T('dash.retrans')}：<span id="q-retrans" class="mono">—</span></span>
        <span class="desc" style="margin:0">${T('dash.statIface')}：<span id="q-dev" class="mono">${T('dash.allPhysIf')}</span></span>
      </div>
    </div>

    <div id="upstream-box"><div class="card">
      <h3>${T('ups.title')}</h3>
      <div class="up-row"><span class="tag gray">${T('ups.readingUp')}</span></div>
    </div></div>

    <div id="netdetail-box"><div class="card">
      <h3>${T('nd.title')}</h3>
      <div class="up-row"><span class="tag gray">${T('nd.readingNet')}</span></div>
    </div></div>

    <div id="realtime-box">
      <div class="card">
        <h3>${T('实时')}<span class="rt-h3t">${T('负载 / 带宽 / 连接')}</span>
          <span class="tag gray" id="rt-sampling">${T('每 3 秒采样 · 保留 3 分钟')}</span></h3>
        <div class="rt-grid">
          <div class="rt-item">
            <div class="rt-t">${T('系统负载')}
              <span class="rt-u">${T('1 / 5 / 15 分钟')}</span></div>
            <canvas data-rt="load" height="74"></canvas>
            <div class="rt-legend">
              <span class="rt-d"><i style="background:#f59e0b"></i>${T('1 分钟')}</span>
              <span class="rt-d"><i style="background:#3b82f6"></i>${T('5 分钟')}</span>
              <span class="rt-d"><i style="background:#8b5cf6"></i>${T('15 分钟')}</span>
            </div>
            <div class="rt-kv"><b>1m</b>
              <span class="mono">${T('均值')} <span id="rt-l1">—</span></span></div>
            <div class="rt-kv"><b>5m</b>
              <span class="mono">${T('均值')} <span id="rt-l5">—</span></span></div>
            <div class="rt-kv"><b>15m</b>
              <span class="mono">${T('均值')} <span id="rt-l15">—</span></span></div>
          </div>
          <div class="rt-item">
            <div class="rt-t">${T('带宽')}
              <span class="rt-u">${T('下行 / 上行')}</span></div>
            <canvas data-rt="net" height="74"></canvas>
            <div class="rt-legend">
              <span class="rt-d"><i style="background:#10b981"></i>${T('下行')}</span>
              <span class="rt-d"><i style="background:#3b82f6"></i>${T('上行')}</span>
            </div>
            <div class="rt-kv"><b>${T('下行峰值')}</b>
              <span class="mono" id="rt-dn">—</span></div>
            <div class="rt-kv"><b>${T('上行峰值')}</b>
              <span class="mono" id="rt-up">—</span></div>
          </div>
          <div class="rt-item">
            <div class="rt-t">${T('连接数')}
              <span class="rt-u">${T('已建立的 TCP')}</span></div>
            <canvas data-rt="conn" height="74"></canvas>
            <div class="rt-legend">
              <span class="rt-d"><i style="background:#0ea5e9"></i>${T('已建立')}</span>
            </div>
            <div class="rt-kv"><b>${T('峰值')}</b>
              <span class="mono" id="rt-conn">—</span></div>
            <div class="rt-kv"><b>${T('重传')}</b>
              <span class="mono" id="rt-retrans">—</span></div>
          </div>
        </div>
        <p class="desc" style="margin:9px 0 0">
          ${T('平均负载是 Linux 跟踪系统资源占用的一项指标，数值等于「正在运行或等待 IO 的进程数」的 1/5/15 分钟均值。柱状图右侧是最新采样点。')}
        </p>
      </div>
    </div>

    <div class="grid g2">
      <div class="card">
        <h3>${T('dash.netState')}</h3><p class="desc">${T('dash.netStateTip')}</p>
        ${ifs.length ? ifs.map(i => `<div class="kv"><b>${esc(bt4('QOS_IMPACT', i.name, 'name', i.name))}</b><span>
          ${i.addrs.filter(a => a.family === 'inet' || a.family === 'inet6').slice(0, 3)
      .map(a => `<span class="mono">${esc(a.addr)}${a.family === 'inet' ? '/' + a.prefix : ''}</span>`).join('<br>') || `<span class="tag gray">${T('dash.noAddr')}</span>`}
          ${i.oper === 'UP' ? '<span class="tag ok">UP</span>' : '<span class="tag gray">DOWN</span>'}
          </span></div>`).join('') : `<div class="kv">${T('dash.none')}</div>`}
        <div class="kv"><b>${T('dash.defRoute')}</b><span class="mono">${esc(((route.data || {}).v4 || [])
      .filter(r => r.dst === 'default').map(r => r.gateway + ' (dev ' + r.dev + ')').join('<br>') || T('dash.none'))}</span></div>
      </div>
      <div class="card">
        <h3>${T('dash.svcState')}</h3><p class="desc">${T('dash.svcStateTip')}</p>
        ${Object.entries(svc.data || {}).map(([k, x]) => {
        const on = x.active === 'active';
        return `<div class="kv"><b>${esc(T('svc.' + k) || k)}</b><span><span class="dot ${on ? 'on' : 'off'}"></span>
            ${on ? T('dash.running') : T('dash.notRunning')} <span class="tag ${x.enabled === 'enabled' ? 'info' : 'gray'}">${x.enabled === 'enabled' ? T('dash.bootStart') : T('dash.disabled')}</span></span></div>`;
      }).join('')}
      </div>
    </div>
    <div class="card">
      <h3>${T('IPv6 状态')}</h3><p class="desc">${T('WAN 地址 / 前缀委派（PD）/ 邻居表')}</p>
      ${(() => {
        const v6d = v6.data || {};
        const g = (v6d.addrs || []).flatMap(a => (a.addr_info || []).filter(x => x.family === 'inet6')
          .map(x => ({ if: a.ifname, a: x.local, p: x.prefixlen, sc: x.scope })));
        const global = g.filter(x => x.sc === 'global');
        return `<div class="kv"><b>${T('公网 IPv6 地址')}</b><span class="mono">${global.length ? global.map(x => esc(x.a) + '/' + x.p + ' <span class="tag gray">' + esc(x.if) + '</span>').join('<br>') : `<span class="tag warn">${T('未获取')}</span>`}</span></div>
          <div class="kv"><b>${T('默认路由')}</b><span class="mono">${esc(v6d.default_route || T('无'))}</span></div>
          <div class="kv"><b>IPv6 ${T('转发')}</b><span>${(v6d.sysctl || {})['all.forwarding'] === '1' ? `<span class="tag ok">${T('已开启')}</span>` : `<span class="tag gray">${T('未开启')}</span>`}</span></div>
          <div class="kv"><b>${T('邻居数量')}</b><span>${(v6d.neigh || []).length}</span></div>`;
      })()}
    </div>
    <div class="card">
      <h3>${T('dash.stageInfo')}</h3>
      <p class="desc">${T('dash.stageTip')}</p>
      <div class="kv"><b>${T('dash.buildStage')}</b><span><span class="tag info">${T('dash.inProgress')}</span>
        ${T('dash.saveOnly')}</span></div>
      <div class="kv"><b>${T('dash.wanIf')}</b><span>${sys.wan_iface ? esc(sys.wan_iface) : `<span class="tag warn">${T('dash.wanUnassigned')}</span>`}</span></div>
      <div class="kv"><b>${T('dash.lanIf')}</b><span class="mono">${esc(sys.lan_iface || '—')} · ${esc(sys.lan_address || '')}</span></div>
      <div class="kv"><b>${T('dash.switchWay')}</b><span>${T('dash.switchWayTip')}</span></div>
    </div>`;
  startDashLive(d, m);
  // 版本卡片：异步填充（要打外网，不能挡住首屏渲染）。
  // 挂在 update.js 里，用 setTimeout 让它跑在本次渲染之后 ——
  // 否则会和上面的 innerHTML 赋值抢同一个 #upd-card。
  if (window.drouterRenderUpdateCard) setTimeout(window.drouterRenderUpdateCard, 0);
  // 上游链路同理：要多 fork 几个 ip 命令，必须异步、且排在渲染之后。
  if (window.drouterRenderUpstream) setTimeout(window.drouterRenderUpstream, 0);
  // 网络明细（邻居/路由/规则）：同样要 fork 几个 ip 命令，异步且排在渲染之后。
  if (window.drouterRenderNetDetail) setTimeout(window.drouterRenderNetDetail, 0);
  // 实时柱状图：3 秒一次采样，自己管轮询（realtime.js 内部有 stop 钩子）
  if (window.drouterRenderRealtime) setTimeout(window.drouterRenderRealtime, 0);
}

/* ---------- 概览实时刷新（#2 / #3 / #13） ----------
   只轮询一个轻量接口 /api/metrics（后端已做差分缓存，成本极低），
   每 2 秒局部更新数字，不重建 DOM，避免闪烁与卡顿。 */
let DASH_TIMER = null;

/* 页面级定时器的「停止钩子」——
   租约与 IPv6 测试的定时器声明在各自 view 的闭包里，外面拿不到引用。
   这里放两个全局钩子，由 view 在创建定时器时挂上、在 go() 里统一调用。
   默认是空函数，所以即使从没进过那个页面也不会报错。 */
let stopLeaseTimer = function () {};
let stopV6Timer = function () {};
/* 网卡页的 ethtool 计数轮询（1.0.10）。ethtool 是外部命令，间隔 5 秒；
   登记进 stopPageTimers，离开页面必须停 —— 否则会一直 fork 进程。 */
let ETH_TIMER = null;
let stopIfaceTimers = function () {};

function startDashLive(d, m) {
  if (DASH_TIMER) { clearInterval(DASH_TIMER); DASH_TIMER = null; }
  const apply = (x) => {
    if (!x) return;
    const set = (id, val, color) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.innerHTML = val;
      if (color) el.style.color = color;
    };
    const mem = x.mem || {}, net = x.net || {}, rtt = x.rtt || {},
      tcp = x.tcp || {}, dk = x.disk || {};
    // 顶部仪表卡（局部更新文字，不重绘卡片结构）
    const g = document.getElementById('dash-gauges');
    if (g) {
      const cards = g.querySelectorAll('.stat');
      const put = (i, pct, mid, sub) => {
        const c = cards[i]; if (!c) return;
        const valEl = c.querySelector('.val');
        const barI = c.querySelector('.bar > i');
        const subEl = c.querySelector('.sub');
        const hasV = pct !== null && pct !== undefined && isFinite(Number(pct));
        const p = hasV ? Math.max(0, Math.min(100, Number(pct))) : 0;
        const col = !hasV ? 'var(--txt3)' : (p >= 85 ? '#ef4444' : (p >= 65 ? '#f59e0b' : null));
        if (valEl) valEl.innerHTML = hasV
          ? `<span style="color:${col || 'inherit'}">${p % 1 ? p.toFixed(1) : p}%</span>
             <span style="font-size:12px;color:var(--txt3);font-weight:400">${esc(mid || '')}</span>`
          : valEl.innerHTML;
        if (barI && hasV) { barI.style.width = p + '%'; if (col) barI.style.background = col; }
        if (subEl && sub != null) subEl.textContent = sub;
      };
      put(0, x.cpu_pct, (d.cpu || {}).model || '', null);
      put(1, mem.pct, `${mem.used_mb || 0} / ${mem.total_mb || 0} MB`,
        `${T('可用')} ${mem.avail_mb || 0} MB　${T('共')} ${mem.total_mb || 0} MB`);
      put(2, dk.pct, '', dk.total_h
        ? `${T('根分区')} ${dk.used_h} / ${dk.total_h}${T('（可用')} ${dk.free_h}）` : null);
      if (cards[3]) {
        const subEl = cards[3].querySelector('.sub');
        if (subEl) subEl.textContent = `${x.proc_count || 0} ${T('个进程')}　${x.temp_available
          ? T('CPU 温度 ') + x.temp_c + ' °C'
          : T(bt4('SYSINFO_NOTES', x.temp_src || '', 'd', x.temp_src || '未读取到温度传感器，仅显示进程数'))}`;
      }
    }
    // 副卡：累计流量（实时速率只保留在下方的「网络实时质量」，避免同页重复显示）
    const nt = x.net_total || {};
    set('dash-rx', '↓ ' + (nt.rx_h || '—'), '#10b981');
    set('dash-tx', '↑ ' + (nt.tx_h || '—'), '#3b82f6');
    set('q-rx', fmtRate(net.rx_bps), '#10b981');
    set('q-tx', fmtRate(net.tx_bps), '#3b82f6');
    set('q-rx-sub', (net.rx_pps != null ? net.rx_pps + ` ${T('包/秒')}` : T('实时')));
    set('q-tx-sub', (net.tx_pps != null ? net.tx_pps + ` ${T('包/秒')}` : T('实时')));
    set('q-rtt', rtt.ms != null ? rtt.ms + ' ms' : (rtt.loss_pct === 100 ? T('超时') : '—'));
    set('q-rtt-sub', T('目标 ') + esc(rtt.target || '—') + (rtt.cached ? T('（缓存）') : ''));
    set('q-jit', rtt.jitter_ms != null ? rtt.jitter_ms + ' ms' : '—');
    set('q-jit-sub', rtt.loss_pct != null ? T('丢包 ') + rtt.loss_pct + '%' : '—');
    set('q-pps', `${fmtRate(net.rx_pps)} / ${fmtRate(net.tx_pps)}`);
    set('q-estab', String(tcp.estab != null ? tcp.estab : '—'));
    set('q-retrans', String(tcp.retrans != null ? tcp.retrans : '—'));
    set('q-dev', (x.ifaces || []).map(i => i.name).join(' / ') || T('全部物理口'));
    if (x.load) set('dash-load', esc(x.load.join(' / ')));
    set('dash-net-extra', nt.since
      ? `${T('自')} ${esc(nt.since)} ${T('起累计')}`
      : T('尚未开始累计统计'));
  };
  apply(m);
  // 实时柱状图：3 秒一次采样，自己管轮询（realtime.js 内部有 stop 钩子）
  if (window.drouterRenderRealtime) setTimeout(window.drouterRenderRealtime, 0);
  DASH_TIMER = setInterval(async () => {
    // 页面切走就停，避免后台白白耗资源
    if (S.page !== 'dash' || !document.getElementById('dash-gauges')) {
      clearInterval(DASH_TIMER); DASH_TIMER = null; return;
    }
    const r = await api('/api/metrics');
    if (r.ok) apply(r.data);
  }, 2000);
}

/* 速率格式化：B/s → 人类可读 */
function fmtRate(bps) {
  const n = Number(bps) || 0;
  if (n < 1024) return n.toFixed(0) + ' B/s';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB/s';
  if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(2) + ' MB/s';
  return (n / 1073741824).toFixed(2) + ' GB/s';
}

const stat = (l, v, s, extra = '') => `<div class="stat"><div class="lbl">${l}</div>
  <div class="val">${v}</div>${extra}<div class="sub">${s || ''}</div></div>`;
const bar = p => `<div class="bar"><i style="width:${Math.min(100, p)}%"></i></div>`;

/* 彩色进度条 + 数值的仪表卡（用于 CPU / 内存 / 磁盘） */
/* 彩色进度条卡。
   pct 为 null/undefined 时表示"该项不可用"（例如虚拟机没有温度传感器），
   此时不画进度条、不显示数字，避免把真实数值（如进程数）误当成百分比。
   注意：第 2 个参数必须是"百分比 0-100"，绝不要把原始数量直接传进来。 */
function gauge(label, pct, mid, color, sub, opt) {
  opt = opt || {};
  const hasVal = pct !== null && pct !== undefined && pct !== '' && isFinite(Number(pct));
  const p = hasVal ? Math.max(0, Math.min(100, Number(pct))) : 0;
  const warn = !hasVal ? 'var(--txt3)' : (p >= 85 ? '#ef4444' : (p >= 65 ? '#f59e0b' : color));
  const head = hasVal
    ? `<span style="color:${warn}">${p % 1 ? p.toFixed(1) : p}%</span>
       <span style="font-size:12px;color:var(--txt3);font-weight:400">${esc(String(mid == null ? '' : mid))}</span>`
    : `<span style="color:var(--txt3);font-size:15px;font-weight:500">—</span>
       <span style="font-size:12px;color:var(--txt3);font-weight:400">${esc(String(mid == null ? T('不可用') : mid))}</span>`;
  const bar = hasVal
    ? `<div class="bar" style="margin:10px 0 6px"><i style="width:${p}%;background:${warn}"></i></div>`
    : `<div class="bar" style="margin:10px 0 6px;background:repeating-linear-gradient(90deg,var(--line2) 0 6px,transparent 6px 12px)"></div>`;
  // 惊叹号提示：用于"该项因环境原因不可用"（虚拟机无温度传感器等）
  // ⚠️ opt.warn 已在传入处过 bt4 翻译（见 L649），这里再保障一道：
  //    上游若因任何原因漏了翻译，这里仍能查表转回英文，不会在界面上露中文。
  const warnText = bt4('SYSINFO_NOTES', opt.warn, 'd', opt.warn) || opt.warn;
  const warnIcon = opt.warn ? `<span class="gauge-warn" title="${esc(warnText)}"
      role="button" tabindex="0" aria-label="${T('查看原因')}">!</span>` : '';
  const warnBox = opt.warn ? `<div class="gauge-note">${esc(warnText)}</div>` : '';
  return `<div class="stat">
    <div class="lbl" style="display:flex;align-items:center;gap:6px">${esc(label)}${warnIcon}</div>
    <div class="val" style="display:flex;align-items:baseline;gap:8px">${head}</div>
    ${bar}
    <div class="sub">${esc(String(sub == null ? '' : sub))}</div>
    ${warnBox}
  </div>`;
}

/* 环形饼图（SVG），用于内存等关键指标 */
function ring(pct, label, color) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  const warn = p >= 85 ? '#ef4444' : (p >= 65 ? '#f59e0b' : color);
  const r = 52, c = 2 * Math.PI * r, off = c * (1 - p / 100);
  return `<svg viewBox="0 0 130 130" width="130" height="130" role="img" aria-label="${esc(label)}">
    <circle cx="65" cy="65" r="${r}" fill="none" stroke="var(--line)" stroke-width="12"></circle>
    <circle cx="65" cy="65" r="${r}" fill="none" stroke="${warn}" stroke-width="12"
      stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"
      transform="rotate(-90 65 65)"></circle>
    <text x="65" y="62" text-anchor="middle" font-size="24" font-weight="600"
      fill="var(--txt)">${p}%</text>
    <text x="65" y="82" text-anchor="middle" font-size="11" fill="var(--txt3)">${esc(label)}</text>
  </svg>`;
}

const $w = k => (S.cfg[k] || {});

/* ============================ 网卡与桥接 ============================ */
const IFACE_ROLES = () => ([
  { v: '', n: T('未分配') },
  { v: 'wan', n: T('WAN（上行 / 光猫）') },
  { v: 'lan', n: T('LAN（局域网主口）') },
  { v: 'bridge', n: T('桥接成员（并入网桥）') },
  { v: 'guest', n: T('访客网络') },
  { v: 'iot', n: T('IoT 设备专网') },
  { v: 'dmz', n: T('DMZ（对外暴露）') },
  { v: 'mgmt', n: T('管理口（仅管理访问）') },
  { v: 'unused', n: T('保留 / 未接线') },
]);

async function viewIface() {
  const [r, m, meth] = await Promise.all([
    api('/api/ifaces'), api('/api/iface/meta'), api('/api/iface_method')]);
  const ifs = (r.data || []).filter(x => !x.is_virtual && x.name !== 'lo');
  const meta = {};
  (m.data || []).forEach(x => meta[x.mac] = x);
  const method = {};
  ((meth.data || {}).ifaces || meth.data || []).forEach(x => {
    if (x && x.name) method[x.name] = x;
  });
  S.meta = meta; S.ifaces = ifs; S.ifaceMethod = method;
  const known = new Set((m.data || []).map(x => x.mac));
  // 只统计「可插拔的物理网卡」：本机自建的虚拟设备（如 QoS 的 ifb-*）不该要求指派角色
  const newOnes = ifs.filter(i => !known.has(i.mac) && !i.is_virtual && !i.managed_by);

  const tip = $('#newiface-tip');
  // 关闭状态按「网卡 MAC 集合」记忆：用户关掉后不再打扰，
  // 但将来真插了一张新卡、集合变了，提示会重新出现。
  const pendingKey = newOnes.map(x => x.mac).sort().join(',');
  const dismissed = () => {
    try { return localStorage.getItem('drouter_newiface_dismissed') || ''; } catch (e) { return ''; }
  };
  // 中文顿号 / 英文逗号 + 空格：跟着界面语言走，否则英文界面里
  // 会出现「a、b」这种中式排版。
  const listSep = () => (window.i18n && i18n.getLang() === 'en-US') ? ', ' : '、';
  const show = newOnes.length > 0 && dismissed() !== pendingKey;
  tip.classList.toggle('hidden', !show);
  if (show) {
    tip.innerHTML = `<div class="tip-body">${esc(T('iface.newTip', [newOnes.length, newOnes.map(x => esc(x.name)).join(listSep())]))}</div>`
      + `<button class="tip-x" id="newiface-x" title="${esc(T('iface.tipClose'))}" aria-label="${esc(T('iface.tipCloseAria'))}">✕</button>`;
    const x = $('#newiface-x');
    if (x) {
      x.onclick = () => {
        try { localStorage.setItem('drouter_newiface_dismissed', pendingKey); } catch (e) { /* ignore */ }
        tip.classList.add('hidden');
      };
    }
  }

  const roleOpts = v => IFACE_ROLES().map(o =>
    `<option value="${esc(o.v)}" ${v === o.v ? 'selected' : ''}>${esc(T('iface.role.' + (o.v || 'none')))}</option>`).join('');

  /* 角色名查询：给备注框旁边显示「当前角色」用。
     ⚠️ 必须复用 IFACE_ROLES() 同一份数据，**不要另写一份中文映射** ——
     另写一份的话，以后改了 IFACE_ROLES() 里某个角色的文案，
     这里会静默显示旧文案，两处对不上却没人发现。
     ⚠️ 文案本身走 T('iface.role.' + v)：v 是**持久化到后端的角色标识**，
     是稳定的英文短码（wan/lan/bridge/…），不含会随语言变的中文 ——
     所以这里只按 v 查词条，不动 v，也不拿文案当键。 */
  const roleName = v => {
    const hit = IFACE_ROLES().find(o => o.v === (v || ''));
    return hit ? T('iface.role.' + (hit.v || 'none')) : (v || T('iface.role.none'));
  };

  /* ---------- ethtool 详细计数（1.0.10）----------
     用户要看丢包 / 组播 / 冲突这些细分项，/sys 只有 6 个基础计数，
     细分项全在 ethtool 里（后端 _ethtool_stats 采集）。 */
  // 大数用 K/M/G/T + 两位小数，和项目里其它地方的容量显示一致。
  const cntFmt = n => {
    n = Number(n) || 0;
    if (n < 1000) return String(n);
    const u = ['K', 'M', 'G', 'T', 'P'];
    let i = -1;
    while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return n.toFixed(2) + ' ' + u[i];
  };
  // 计数为 0 的项灰显、非 0 高亮 —— 一眼看出「哪个真的在出错/丢包」。
  const cntCls = n => (Number(n) > 0 ? 'cnt-bad' : '');
  // ⚠️ 关心的「坏」指标：这些非 0 就值得警惕
  const CNT_ALERT = /错误|丢包|冲突|FIFO|CRC|帧错误|载波/;

  const ethBlock = i => {
    const e = i.ethtool;
    if (!e) return '';
    if (!e.ok) {
      // ⚠️ 显示「为什么没有」而不是显示一堆 0。
      //    显示 0 会让用户以为网卡一切正常，实际上是根本没采到。
      return `<div class="eth-box eth-off">
        <span class="tag gray">${esc(T('iface.ethUnavailable'))}</span>
        <span class="eth-reason">${esc(e.reason || T('iface.ethNoData'))}</span>
      </div>`;
    }
    if (!e.items || !e.items.length) {
      return `<div class="eth-box eth-off">
        <span class="tag gray">${esc(T('iface.ethNoCounters'))}</span>
        <span class="eth-reason">${esc(T('iface.ethNoCountersWhy'))}</span>
      </div>`;
    }
    const rows = e.items.map(it => {
      const [label, val] = it;
      const bad = CNT_ALERT.test(label);
      return `<div class="eth-row${bad ? ' eth-warn' : ''}">
        <span class="eth-k">${esc(label)}</span>
        <span class="eth-v mono ${bad ? cntCls(val) : ''}">${esc(cntFmt(val))}</span>
      </div>`;
    }).join('');
    const link = e.link
      ? (e.link === 'yes'
        ? `<span class="tag ok">${esc(T('iface.linkUp'))}</span>`
        : `<span class="tag warn">${esc(T('iface.linkDown'))}</span>`)
      : '';
    return `<div class="eth-box">
      <div class="eth-head">
        ${e.speed ? `<span class="tag info">${esc(e.speed)}</span>` : ''}
        ${e.duplex ? `<span class="tag gray">${esc(e.duplex)}</span>` : ''}
        ${link}
        <span class="eth-upd" data-eth-ts>${esc(new Date().toLocaleTimeString(
    window.i18n && i18n.getLang() === 'en-US' ? 'en-US' : 'zh-CN'))}</span>
      </div>
      <div class="eth-grid">${rows}</div>
    </div>`;
  };

  /* 备注框旁的「当前角色」实时标签。
     ⚠️ 用 textContent 而不是 innerHTML：角色文案虽来自我们的常量，
     但这段 HTML 一旦被改，innerHTML 就是一条注入路径。 */
  const roleTagHtml = mac => {
    const cur = (S.meta && S.meta[mac] && S.meta[mac].role) || '';
    return `<div class="role-live" id="rl-${esc(mac)}">
      ${esc(T('iface.curRole'))}<b></b><span class="tag gray" data-rl-role></span>
    </div>`;
  };

  /* 驱动来源标签。⚠️ src 是**后端返回的中文字符串**（drouter-helper 的
     _driver_source），这里按「含哪个中文」判类别 —— 判据是数据契约，
     不是界面文案，所以下面的 indexOf 保持中文、不能改成词条 key。
     真要国际化得让后端额外返回一个稳定的英文类别码。 */
  const drvTag = src => {
    if (!src) return `<span class="tag gray">${esc(T('iface.unknown'))}</span>`;
    // 先查表（键=后端下发的中文原文，如「内核内建」「发行版官方源」）；
    // 厂商官方(xxx) 这类动态串表里有 kind 就直接用，没命中再按前缀兜底。
    const en = bt4('IFACE_DRVSRC', src, 'en', '');
    const kd = bt4('IFACE_DRVSRC', src, 'kind', '');
    if (en) return `<span class="tag ${esc(kd || 'warn')}">${esc(en)}</span>`;
    // i18n-keep-cn：比对的是后端下发的中文原文（见本函数上方说明）
    if (src.indexOf('厂商官方') >= 0) return `<span class="tag info">${esc(src.replace('厂商官方', T('iface.vendor')))}</span>`; // i18n-keep-cn
    if (src.indexOf('第三方') >= 0) return `<span class="tag warn">${esc(src.replace('第三方', T('iface.thirdParty')))}</span>`; // i18n-keep-cn
    return `<span class="tag warn">${esc(src)}</span>`;
  };

  $('#view').innerHTML = `
    <div class="card">
      <h3>${esc(T('iface.physTitle'))}</h3>
      <p class="desc">${esc(T('iface.physDesc'))}</p>
      ${ifs.length ? ifs.map(i => {
    const m2 = meta[i.mac] || {};
    const mt = method[i.name] || {};
    const spd = i.speed ? i.speed + ' Mbps' : '—';
    const stTag = mt.state === 'UP' || i.oper === 'UP'
      ? '<span class="tag ok">UP</span>' : '<span class="tag gray">DOWN</span>';
    // ⚠️ mt.method 是**后端返回的中文字符串**（'DHCP 自动' / '静态地址' / '手动'…），
    //   下面的比较是数据契约，不是界面文案 —— 别改成 t()，会永远匹配不上。
    //   要国际化得让后端补一个稳定的英文类别码（method_key）。
    // 后端下发的中文方式名：显示走 IFACE_METHOD 表，标签配色取表里的 kind。
    // （比对着中文原文写三元表达式更稳：新增方式只要补表，不用改这里。）
    const mTag = mt.method
      ? `<span class="tag ${esc(bt4('IFACE_METHOD', mt.method, 'kind', 'gray'))}">${esc(bt4('IFACE_METHOD', mt.method, 'en', mt.method))}</span>`
      : `<span class="tag gray">${esc(T('iface.notConfigured'))}</span>`;
    return `<div class="ifrow" style="align-items:flex-start">
          <div style="flex:0 0 170px">
            <div class="nm">${esc(i.name)} ${stTag}</div>
            <div class="mac mono">${esc(i.mac)}</div>
            <div style="margin-top:4px">${mTag}</div>
          </div>
          <div style="flex:0 0 180px;font-size:12px;color:var(--txt3)">
            ${esc(T('iface.speed'))} ${esc(spd)}<br>MTU ${esc(i.mtu)}<br>
            ${esc(T('iface.driver'))} ${esc(i.driver || '—')}
            ${i.driver_version && i.driver_version !== '未知' ? '<span class="tag gray">v' + esc(i.driver_version) + '</span>' : ''}<br>
            ${drvTag(i.driver_source)}
          </div>
          <div style="flex:0 0 150px;font-size:12px;color:var(--txt3)">
            ${(i.addrs || []).filter(a => a.family === 'inet').map(a => `<span class="mono">${esc(a.addr)}/${esc(a.prefix)}</span>`).join('<br>') || esc(T('iface.noIpv4'))}
            ${mt.connection ? '<br>' + esc(T('iface.linkLabel')) + esc(mt.connection) : ''}
          </div>
          <label style="margin:0;flex:1 1 0">${esc(T('iface.remark'))}
            <input data-mac="${esc(i.mac)}" data-f="remark" value="${esc(m2.remark || '')}" placeholder="${esc(T('iface.remarkPh'))}">
            ${roleTagHtml(i.mac)}</label>
          <label style="margin:0;flex:0 0 190px">${esc(T('iface.roleLabel'))}
            <select data-mac="${esc(i.mac)}" data-f="role">${roleOpts(m2.role || '')}</select></label>
          <button class="small" data-ifsave="${esc(i.mac)}" data-name="${esc(i.name)}">${esc(T('iface.saveNic'))}</button>
        </div>
        ${ethBlock(i)}`;
  }).join('') : `<p class="desc">${esc(T('iface.noPhysNic'))}</p>`}
    </div>

    <div class="card">
      <h3>${esc(T('iface.drvTitle'))}</h3>
      <p class="desc">${esc(T('iface.drvDesc'))}</p>
      <div class="kv"><b><span class="tag ok">${esc(T('iface.drvKernel'))}</span></b><span>${esc(T('iface.drvKernelDesc'))}</span></div>
      <div class="kv"><b><span class="tag info">${esc(T('iface.drvVendor'))}</span></b><span>${esc(T('iface.drvVendorDesc'))}</span></div>
      <div class="kv"><b><span class="tag ok">${esc(T('iface.drvDistro'))}</span></b><span>${esc(T('iface.drvDistroDesc'))}</span></div>
      <div class="kv"><b><span class="tag warn">${esc(T('iface.drvDkms'))}</span></b><span>${esc(T('iface.drvDkmsDesc'))}</span></div>
      <div class="kv"><b><span class="tag warn">${esc(T('iface.drvManual'))}</span></b><span>${esc(T('iface.drvManualDesc'))}</span></div>
    </div>

    <div class="card">
      <h3>${esc(T('iface.brTitle'))}</h3>
      <p class="desc">${esc(T('iface.brDesc'))}</p>
      <label class="switch"><input type="checkbox" id="br-en" ${$w('system').bridge_enabled ? 'checked' : ''}><i></i>${esc(T('iface.brEnable'))}</label>
      <div class="row">
        <label>${esc(T('iface.brName'))}<input id="br-name" value="${esc(($w('system').bridge_name) || 'br0')}"></label>
        <label>${esc(T('iface.brStp'))}<select id="br-stp">
          <option value="0">${esc(T('iface.stpOff'))}</option><option value="1" ${$w('system').bridge_stp ? 'selected' : ''}>${esc(T('iface.stpOn'))}</option></select></label>
      </div>
      <div id="br-members">${ifs.map(i => `<label class="switch"><input type="checkbox" class="brm" value="${esc(i.name)}"
        ${(($w('system').bridge_members) || []).includes(i.name) ? 'checked' : ''}><i></i>${esc(i.name)} <span class="mono" style="color:var(--txt3)">${esc(i.mac)}</span></label>`).join('')}</div>
      <p class="hint-inline">${esc(T('iface.brWarn'))}</p>
    </div>

    <div class="card">
      <h3>${esc(T('iface.monTitle'))}</h3>
      <p class="desc">${esc(T('iface.monDesc'))}</p>
      <div class="kv"><b>${esc(T('iface.monCur'))}</b><span>${ifs.length}</span></div>
      <div class="kv"><b>${esc(T('iface.monKnown'))}</b><span>${(m.data || []).filter(x => x.role).length}</span></div>
      <div class="kv"><b>${esc(T('iface.monTodo'))}</b><span>${newOnes.length ? '<span class="tag warn">' + esc(T('iface.monTodoN', [newOnes.length])) + '</span>' : `<span class="tag ok">${esc(T('iface.monNone'))}</span>`}</span></div>
    </div>`;

  /* 备注框旁的「当前角色」联动（1.0.10 新增）。
     两处更新：
       ① 进页面时用 S.meta 里的已保存角色填初值；
       ② 角色下拉框一变就跟着变（不用等点保存）——
          这是「联动」的关键，只在保存后更新等于没联动。
     ⚠️ 用 textContent 写文案，角色名不进 innerHTML。 */
  const paintRole = mac => {
    const box = document.getElementById('rl-' + mac);
    if (!box) return;
    const sel = document.querySelector(
      `select[data-mac="${mac}"][data-f=role]`);
    const tag = box.querySelector('[data-rl-role]');
    const v = sel ? sel.value : ((S.meta[mac] || {}).role || '');
    const name = roleName(v);
    if (tag) {
      tag.textContent = name;
      // 未分配用灰、成功分配用蓝，一眼能看出这张卡分没分
      tag.className = 'tag ' + (v ? 'info' : 'gray');
    }
  };
  $$('[data-f=role]').forEach(sel => {
    const mac = sel.dataset.mac;
    paintRole(mac);
    sel.onchange = () => { paintRole(mac); setActionMsg(T('iface.roleChanged')); };
  });

  $$('[data-ifsave]').forEach(b => b.onclick = async () => {
    const mac = b.dataset.ifsave;
    const remark = $(`input[data-mac="${mac}"][data-f=remark]`).value;
    const role = $(`select[data-mac="${mac}"][data-f=role]`).value;
    const r2 = await api('/api/iface/save', { method: 'POST', body: { mac, name: b.dataset.name, remark, role } });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err');
    if (r2.ok) {
      // 同步本地缓存，否则重新进页面时标签会退回旧角色
      if (S.meta[mac]) { S.meta[mac].role = role; S.meta[mac].remark = remark; }
      paintRole(mac);
      setActionMsg(T('iface.nicSaved', [b.dataset.name, roleName(role)]), 'ok');
    }
  });
  $('#br-en').onchange = e => { S.cfg.system = Object.assign($w('system'), { bridge_enabled: e.target.checked }); setActionMsg(T('common.dirtyApply')); };
  $('#br-name').oninput = e => { S.cfg.system = Object.assign($w('system'), { bridge_name: e.target.value }); setActionMsg(T('common.dirtyApply')); };
  // STP 下拉框原先只有模板、没有任何 handler —— 改了之后点保存，
  // system.bridge_stp 压根不会被更新，等于一个改不生效的死控件。
  const stp = $('#br-stp');
  if (stp) stp.onchange = e => {
    S.cfg.system = Object.assign($w('system'), { bridge_stp: e.target.value === '1' });
    setActionMsg(T('common.dirtyApply'));
  };
  $$('.brm').forEach(c => c.onchange = () => {
    const sel = $$('.brm').filter(x => x.checked).map(x => x.value);
    S.cfg.system = Object.assign($w('system'), { bridge_members: sel });
    setActionMsg(T('common.dirtyApply'));
  });

  /* ---------- ethtool 计数实时刷新（1.0.10）----------
     5 秒一次（比概览页的 2 秒慢）：ethtool 是外部命令、要 fork 进程，
     间隔太短在 2 核机器上是白白的 CPU 消耗，而这些计数本身变化很慢。
     ⚠️ 必须在切页时停掉 —— 走 stopPageTimers 的钩子机制，
     否则离开这个页后还在打接口。 */
  if (ETH_TIMER) { clearInterval(ETH_TIMER); ETH_TIMER = null; }
  const ethTick = async () => {
    if (S.page !== 'iface') { stopIfaceTimers(); return; }
    let list = null;
    try { list = (await api('/api/ifaces')).data; } catch (e) { return; }
    (list || []).forEach(nv => {
      // ⚠️ 按 data-eth-if 精确定位，**不能用 $$('.eth-box')[k] 反查**
      //    —— 那个下标依赖 DOM 顺序，一旦某张卡没有 ethtool 块
      //    （虚拟网卡、ethtool 不可用），后面每张卡的接口名就全错位了，
      //    刷新时会把 A 网卡的数据写到 B 网卡上。
      const box = document.querySelector(
        `[data-eth-if="${CSS.escape(nv.name)}"]`);
      if (!box) return;
      const tmp = document.createElement('div');
      tmp.innerHTML = ethBlock(nv);
      const fresh = tmp.firstElementChild;
      if (fresh) { fresh.setAttribute('data-eth-if', nv.name); box.replaceWith(fresh); }
    });
  };
  // 渲染时就把接口名写进属性（ethBlock 里拿不到 i.name，所以在这里补）
  $$('.eth-box').forEach((b, k) => {
    const it = (S.ifaces || []).filter(x => x.ethtool)[k];
    if (it) b.setAttribute('data-eth-if', it.name);
  });
  ETH_TIMER = setInterval(ethTick, 5000);
  stopIfaceTimers = () => { if (ETH_TIMER) { clearInterval(ETH_TIMER); ETH_TIMER = null; } };
}

/* ============================ WAN ============================ */
let WANLOG_TIMER = null;
function viewWan() {
  const p = $w('pppoe'), sys = $w('system');
  const hasWan = !!sys.wan_iface;
  // 若 pppoe.iface 未设置，默认选中角色为 wan 的网卡（免去用户手选）
  const roleWan = (S.ifaces.find(i => i.role === 'wan') || {}).name || '';
  const defaultWan = sys.wan_iface || roleWan;
  // 当前连接方式：优先取已保存的，其次按是否填了账号推断
  const curMode = p.mode || 'pppoe';
  $('#view').innerHTML = `
    ${hasWan ? `<div class="card" style="border-color:#bfe3c4;background:var(--ok-l)">
      <h3 style="color:var(--ok)">${T('已检测到 WAN 口：')}${esc(sys.wan_iface)}</h3>
      <p class="desc" style="color:var(--ok);margin:0">
      ${T('可在下方填写宽带账号与密码。点击「仅保存」只写入配置，不会拨号。 需要正式拨号时，先解除构建保护模式，再点「开始拨号」。')}</p></div>`
      : `<div class="card" style="border-color:#f0e0a8;background:var(--warn-l)">
      <h3 style="color:var(--warn)">${T('尚未分配 WAN 口')}</h3>
      <p class="desc" style="color:var(--warn);margin:0">
      ${T('请在虚拟机平台中为该主机添加第二张网卡，然后在「网卡与桥接」页面将其角色设为 WAN。 在此之前，下面填写的 PPPoE 凭据只会被保存，不会执行拨号。')}</p></div>`}
    <div class="card">
      <h3>${T('连接方式')}</h3>
      <p class="desc">${T('支持')} PPPoE ${T('拨号、')}DHCP ${T('自动获取、静态地址三种模式，可一键切换（切换会影响全网）。')}</p>
      <div class="seg" id="wan-mode">
        <button data-m="pppoe" class="${curMode === 'pppoe' ? 'on' : ''}">PPPoE ${T('拨号')}</button>
        <button data-m="dhcp" class="${curMode === 'dhcp' ? 'on' : ''}">DHCP ${T('自动')}</button>
        <button data-m="static" class="${curMode === 'static' ? 'on' : ''}">${T('静态地址')}</button>
      </div>
      <div class="kv" style="margin-top:12px"><b>${T('当前模式')}</b><span id="wan-mode-cur">${wanModeName(curMode)}</span></div>
      <div class="hint-inline" id="wan-mode-hint"></div>
      <div id="wan-mode-panel" style="margin-top:10px"></div>
    </div>

    <div class="card">
      <h3>PPPoE ${T('宽带拨号')}</h3>
      <p class="desc">${T('适用于中国大陆运营商：中国电信')} / ${T('中国联通')} / ${T('中国移动')} / ${T('中国广电。 实际认证方式与是否需要服务名取决于当地运营商，请以实测为准。')}</p>
      <div class="row">
        <label>${T('运营商模板')}<select id="pp-isp">
          <option value="auto" ${p.isp === 'auto' ? 'selected' : ''}>${T('自动')} / ${T('通用')}</option>
          <option value="ct" ${p.isp === 'ct' ? 'selected' : ''}>${T('中国电信')}</option>
          <option value="cu" ${p.isp === 'cu' ? 'selected' : ''}>${T('中国联通')}</option>
          <option value="cm" ${p.isp === 'cm' ? 'selected' : ''}>${T('中国移动')}</option>
          <option value="cbn" ${p.isp === 'cbn' ? 'selected' : ''}>${T('中国广电')}</option>
        </select></label>
        <label>${T('拨号网卡（')}WAN）<select id="pp-iface">
          <option value="">${T('未分配')}</option>
          ${S.ifaces.map(i => `<option value="${esc(i.name)}" ${(p.iface || defaultWan) === i.name ? 'selected' : ''}>${esc(i.name)} (${esc(i.mac)})${i.role === 'wan' ? ' — WAN' : ''}</option>`).join('')}
        </select></label>
      </div>
      <div class="row">
        <label>${T('宽带账号')}<input id="pp-user" value="${esc(p.username || '')}" placeholder="${T('例如')} 0512xxxxxxxx" autocomplete="off"></label>
        <label>${T('宽带密码')}<input id="pp-pass" type="password" value="${esc(p.password || '')}" placeholder="${T('运营商提供的密码')}" autocomplete="new-password"></label>
      </div>
      <div class="row">
        <label>${T('服务名（可留空）')}<input id="pp-svc" value="${esc(p.service_name || '')}" placeholder="${T('多数地区留空')}"></label>
        <label>MTU<input id="pp-mtu" type="number" value="${esc(p.mtu || 1492)}"></label>
        <label>MRU<input id="pp-mru" type="number" value="${esc(p.mru || 1492)}"></label>
      </div>
      <label class="switch"><input type="checkbox" id="pp-persist" ${p.persist !== false ? 'checked' : ''}><i></i>${T('断线自动重连（')}persist）</label>
      <div class="row">
        <label>${T('重拨间隔（秒）')}<input id="pp-holdoff" type="number" value="${esc(p.holdoff || 5)}"></label>
        <label>${T('最大失败次数（')}0=${T('无限）')}<input id="pp-maxfail" type="number" value="${esc(p.maxfail || 0)}"></label>
      </div>
      <p class="hint-inline">${T('凭据保存在本机')} SQLite ${T('数据库中，应用时写入')} <span class="mono">/etc/ppp/chap-secrets</span>${T('（权限')} 600）。</p>
    </div>

    <div class="card">
      <h3>WAN ${T('静态地址（静态模式时生效）')}</h3>
      <div class="row">
        <label>IP ${T('地址')} / ${T('掩码')}<input id="wn-addr" value="${esc(p.static_address || '')}" placeholder="192.168.1.2/24"></label>
        <label>${T('网关')}<input id="wn-gw" value="${esc(p.static_gateway || '')}" placeholder="192.168.1.1"></label>
        <label>DNS<input id="wn-dns" value="${esc(p.static_dns || '')}" placeholder="223.5.5.5,119.29.29.29"></label>
      </div>
    </div>

    <div class="card">
      <h3>WAN ${T('接入方式实时状态与日志')}</h3>
      <p class="desc">${T('支持中国大陆家庭宽带全部主流接入方式，日志每')} 3 ${T('秒自动滚动刷新，全部中文呈现， 并针对每种方式给出排错提示。日志来源：')}<span id="wan-src" class="mono" style="font-size:12px">—</span></p>
      <div class="row">
        <label>${T('接入方式')}<select id="wan-access">
          <option value="pppoe">PPPoE ${T('拨号')}</option>
          <option value="dhcp">DHCP ${T('自动（')}IPoE）</option>
          <option value="static">${T('静态地址（专线')} / ${T('固定')} IP）</option>
          <option value="pppoe_dhcp6">PPPoE + DHCPv6-PD${T('（双栈）')}</option>
          <option value="ipoe_dhcp6">IPoE + DHCPv6-PD${T('（双栈）')}</option>
          <option value="bridge">${T('桥接旁路（不改')} WAN）</option>
        </select></label>
        <label>${T('时间范围')}<select id="wan-since">
          <option value="30 min ago">${T('最近')} 30 ${T('分钟')}</option>
          <option value="2 hours ago" selected>${T('最近')} 2 ${T('小时')}</option>
          <option value="12 hours ago">${T('最近')} 12 ${T('小时')}</option>
          <option value="2 days ago">${T('最近')} 2 ${T('天')}</option>
        </select></label>
        <button class="ghost small fixed" id="wan-refresh">${T('立即刷新')}</button>
      </div>
      <p class="hint-inline" id="wan-access-desc"></p>
      <div id="pp-statusbar" class="ppbar">
        <span class="dot off" id="pp-dot"></span>
        <b id="pp-stext">${T('读取中')}…</b>
        <span id="pp-smeta" style="color:var(--txt3);font-size:12px"></span>
      </div>
      <div id="wan-meta" class="kv" style="margin-top:8px"></div>
      <div class="row" style="margin-top:10px">
        <label class="switch"><input type="checkbox" id="pp-log-auto" checked><i></i>${T('自动刷新日志（每')} 3 ${T('秒）')}</label>
        <button class="ghost small fixed" id="pp-log-clear">${T('暂停自动刷新')}</button>
      </div>
      <div id="pp-live" class="pplog"><div class="ppline">${T('正在读取接入日志')}…</div></div>
      <p class="hint-inline" id="wan-tip"></p>
    </div>

    <div class="card">
      <h3>${T('当前')} WAN ${T('状态')}</h3>
      <p class="desc">${T('实时读取，无需手动刷新')}</p>
      <div id="wan-live">${T('读取中')}…</div>
      <div class="row" style="margin-top:14px">
        <button class="ghost fixed" id="wan-test">${T('测试连通性（')}ping 223.5.5.5）</button>
        <button class="ghost fixed" id="pp-status">${T('查询拨号状态')}</button>
      </div>
      <div id="wan-out"></div>
    </div>
    <div class="card" style="border-color:#f0c8c3">
      <h3 style="color:var(--err)">${T('执行切换（请在我方确认后进行）')}</h3>
      <p class="desc">${T('以下按钮会真正改变网络状态，导致当前连接短暂中断。请在填写并保存凭据、且确认')} WAN ${T('口就绪后再执行。')}</p>
      <div class="row">
        <button class="fixed" id="pp-conn">${T('开始拨号（')}PPPoE connect）</button>
        <button class="fixed danger" id="pp-disc">${T('断开拨号（')}PPPoE disconnect）</button>
      </div>
    </div>`;
  liveWan();
  liveWanLog();
  // 接入日志自动刷新（3 秒）
  const pppAuto = $('#pp-log-auto');
  const startPpp = () => {
    if (WANLOG_TIMER) { clearInterval(WANLOG_TIMER); WANLOG_TIMER = null; }
    if (pppAuto && pppAuto.checked) WANLOG_TIMER = setInterval(liveWanLog, 3000);
  };
  if (pppAuto) pppAuto.onchange = startPpp;
  startPpp();

  // 接入方式切换
  const accEl = $('#wan-access');
  if (accEl) {
    accEl.value = p.access_type || 'pppoe';
    accEl.onchange = () => {
      S.cfg.pppoe = Object.assign($w('pppoe'), { access_type: accEl.value });
      const d = $('#wan-access-desc');
      const info = (WAN_ACCESS_MAP()[accEl.value] || {});
      if (d) d.textContent = info.desc || '';
      setActionMsg(T('WAN 接入方式已选为「') + (info.n || accEl.value) + T('」，请点击保存并应用'));
      liveWanLog();
    };
  }
  const sinceEl = $('#wan-since');
  if (sinceEl) sinceEl.onchange = liveWanLog;
  const wr = $('#wan-refresh'); if (wr) wr.onclick = () => { liveWanLog(); toast(T('已刷新接入日志'), 'ok'); };
  const plc = $('#pp-log-clear'); if (plc) plc.onclick = () => {
    if (pppAuto) { pppAuto.checked = false; startPpp(); }
    toast(T('已暂停自动刷新'), 'ok');
  };

  $$('#wan-mode button').forEach(b => b.onclick = () => {
    $$('#wan-mode button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    const m = b.dataset.m;
    S.cfg.pppoe = Object.assign($w('pppoe'), { mode: m });
    const cur = $('#wan-mode-cur');
    if (cur) cur.innerHTML = wanModeName(m);
    renderWanModePanel(m);
    setActionMsg(T('连接方式已改为「') + wanModeName(m) + T('」，请点击右上角「保存并应用」'));
  });
  renderWanModePanel(curMode);
  const bind = (id, key, conv) => {
    const el = $(id); if (!el) return;
    const h = () => { S.cfg.pppoe = Object.assign($w('pppoe'), { [key]: conv ? conv(el.value) : el.value }); setActionMsg(T('已修改，请点击保存并应用')); };
    el.oninput = h; if (el.tagName === 'SELECT') el.onchange = h;
  };
  bind('#pp-isp', 'isp'); bind('#pp-user', 'username');
  bind('#pp-pass', 'password'); bind('#pp-svc', 'service_name');
  // 拨号网卡单独处理，确保 iface 一定被写入
  const ifEl = $('#pp-iface');
  if (ifEl) {
    ifEl.onchange = () => {
      S.cfg.pppoe = Object.assign($w('pppoe'), { iface: ifEl.value });
      setActionMsg(T('拨号网卡已选为 ') + (ifEl.value || T('未分配')) + T('，请点击保存并应用'));
    };
  }
  bind('#pp-mtu', 'mtu', numOr); bind('#pp-mru', 'mru', numOr);
  bind('#pp-holdoff', 'holdoff', numOr); bind('#pp-maxfail', 'maxfail', numOr);
  bind('#wn-addr', 'static_address'); bind('#wn-gw', 'static_gateway'); bind('#wn-dns', 'static_dns');
  $('#pp-persist').onchange = e => { S.cfg.pppoe = Object.assign($w('pppoe'), { persist: e.target.checked }); setActionMsg(T('已修改')); };
  $('#wan-test').onclick = async () => {
    $('#wan-out').innerHTML = `<pre>${T('正在测试…')}</pre>`;
    const r = await api('/api/diag', { method: 'POST', body: { tool: 'ping', target: '223.5.5.5', count: 4 } });
    $('#wan-out').innerHTML = `<pre>${esc((r.data || {}).out || r.msg_cn)}</pre>`;
  };
  $('#pp-status').onclick = async () => {
    const r = await api('/api/ppp', { method: 'POST', body: { op: 'status' } });
    toast(r.msg_cn || (r.data && r.data.up ? T('已连接') : T('未连接')), r.ok ? 'ok' : 'err');
  };
  const confirmThen = (op, word, label) => {
    modal(`${label}`, `<p>${T('此操作会立即改变')} WAN ${T('连接状态，可能导致当前网络中断。')}</p>
      <p>${T('请输入')} <b>${word}</b> ${T('以确认：')}</p><input id="cfm-in" placeholder="${word}">`,
      async () => {
        if ($('#cfm-in').value.trim() !== word) { toast(T('确认文字不正确'), 'err'); return false; }
        const r = await api('/api/ppp', { method: 'POST', body: { op, confirm: true } });
        toast(r.msg_cn, r.ok ? 'ok' : 'err');
        setTimeout(liveWan, 3000);
      });
  };
  $('#pp-conn').onclick = () => confirmThen('connect', T('确认拨号'), T('开始 PPPoE 拨号'));
  $('#pp-disc').onclick = () => confirmThen('disconnect', T('确认断开'), T('断开 PPPoE 拨号'));
}

/* WAN 接入方式统一实时状态栏 + 中文日志（#4） */
const WAN_ACCESS_MAP = () => ({
  pppoe: { n: T('PPPoE 拨号'), desc: T('光猫桥接 + 本机拨号，需要宽带账号密码（电信/联通/移动/广电均常见）。') },
  dhcp: { n: T('DHCP 自动（IPoE）'), desc: T('光猫已拨号，下挂设备自动获取地址；也用于部分地区「IPoE 免拨号」接入。') },
  static: { n: T('静态地址（专线 / 固定 IP）'), desc: T('运营商分配固定公网 IP，手动配置地址、网关与 DNS，常见于企业专线。') },
  pppoe_dhcp6: { n: T('PPPoE + DHCPv6-PD（双栈）'), desc: T('IPv4 走 PPPoE、IPv6 通过 DHCPv6-PD 下发前缀，国内主流双栈方式。') },
  ipoe_dhcp6: { n: T('IPoE + DHCPv6-PD（双栈）'), desc: T('IPv4/IPv6 均由上级自动下发，适用于光猫路由模式 + IPv6 直连。') },
  bridge: { n: T('桥接旁路（不改 WAN）'), desc: T('仅做二层透传/旁路，不参与拨号，WAN 状态由上级设备决定。') },
});

async function liveWanLog() {
  const box = $('#pp-live');
  // 先判 DOM 再发请求。早期写法是先 fetch 再 `if (!box) return`，
  // 切走页面后请求照发（每 3 秒一次 journal 查询），只是结果被丢弃。
  if (!box) { if (WANLOG_TIMER) { clearInterval(WANLOG_TIMER); WANLOG_TIMER = null; } return; }
  const acc = ($('#wan-access') || {}).value || (($w('pppoe').access_type) || 'pppoe');
  const since = ($('#wan-since') || {}).value || '2 hours ago';
  const ifname = $w('system').wan_iface || $w('pppoe').iface || '';
  const r = await api('/api/wan/log?access=' + encodeURIComponent(acc) +
    '&since=' + encodeURIComponent(since) + '&iface=' + encodeURIComponent(ifname) +
    '&limit=150');
  const d = r.data || {};
  if (!r.ok) {
    box.innerHTML = `<div class="ppline lv-err">${T('读取失败：')}${esc(r.msg_cn || T('未知错误'))}</div>`;
    return;
  }
  // 说明与来源
  const sd = $('#wan-src');
  if (sd) sd.textContent = d.source ? bt4('WAN_LOG_SOURCE', d.source, 'en', d.source) : '—';
  const ad = $('#wan-access-desc'); if (ad) ad.textContent = bt4('WAN_ACCESS_TYPES', d.access, 'desc', d.access_desc || '');
  // 状态栏
  const dot = $('#pp-dot'), stext = $('#pp-stext'), smeta = $('#pp-smeta');
  if (dot && stext) {
    const state = d.state || 'unknown';
    const cls = /connect|up/.test(state) ? 'on' : (/connect|retry|config/.test(state) ? 'warn' : 'off');
    dot.className = 'dot ' + cls;
    if (d.state === 'retrying' && d.remain_s != null) {
      stext.textContent = T('重拨中…（{0} 秒后重试）', [d.remain_s]);
    } else {
      stext.textContent = d.state_cn ? bt4('WAN_STATE', d.state_cn, 'en', d.state_cn) : T('未知状态');
    }
    if (smeta) {
      const acn = d.access ? bt4('WAN_ACCESS_TYPES', d.access, 'n', d.access_cn || d.access) : (d.access_cn || '');
      smeta.textContent = acn + (d.iface ? T('　接口 ') + d.iface : '');
    }
  }
  // 元信息
  const mt = $('#wan-meta');
  if (mt) {
    const rows = d.meta || [];
    mt.innerHTML = rows.length
      ? rows.map(x => `<div class="kv"><b>·</b><span>${esc(wanMetaI18n(x))}</span></div>`).join('')
      : '';
  }
  // 日志（最新在最下，自动滚到底）
  const lines = d.lines || [];
  if (lines.length) {
    box.innerHTML = lines.map(l => {
      const lv = l.level || 'info';
      const _en = window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US';
      const _txt = _en ? (l.en || l.raw || l.cn || '') : (l.cn || l.raw || '');
      return `<div class="ppline lv-${esc(lv)}"><span class="ppt">${esc(l.ts || '')}</span>${esc(_txt)}</div>`;
    }).join('');
    box.scrollTop = box.scrollHeight;
  } else {
    box.innerHTML = `<div class="ppline">${esc(wanTipI18n(d.tip) || T('暂无日志。'))}</div>`;
  }
  const tp = $('#wan-tip'); if (tp) tp.textContent = lines.length ? '' : (wanTipI18n(d.tip) || '');
}

const WAN_MODES = () => ({
  pppoe: { n: T('PPPoE 拨号'), hint: T('通过宽带账号密码拨号上网，适用于中国大陆运营商光猫桥接场景。') },
  dhcp: { n: T('DHCP 自动获取'), hint: T('由上级光猫或路由器自动分配 IP，适用于光猫已拨号后再接本机的情况。') },
  static: { n: T('静态地址'), hint: T('手动指定 IP / 掩码 / 网关 / DNS，适用于专线或固定 IP 场景。') },
});
const wanModeName = m => (WAN_MODES()[m] || WAN_MODES().pppoe).n;

function renderWanModePanel(mode) {
  const el = $('#wan-mode-panel'); if (!el) return;
  const hint = $('#wan-mode-hint');
  const info = WAN_MODES()[mode] || WAN_MODES().pppoe;
  if (hint) hint.innerHTML = info.hint;
  const p = $w('pppoe');
  if (mode === 'dhcp') {
    el.innerHTML = `
      <div class="kv"><b>${T('地址获取')}</b><span>${T('自动从上级')} DHCP ${T('服务器获取')} IPv4 ${T('地址与网关')}</span></div>
      <label>MTU<input id="dh-mtu" type="number" value="${esc(p.dhcp_mtu || 1500)}"></label>
      <label class="switch"><input type="checkbox" id="dh-dns" ${p.dhcp_use_dns !== false ? 'checked' : ''}><i></i>${T('使用上级下发的')} DNS</label>
      <p class="hint-inline">DHCP ${T('模式下，')}WAN ${T('口会自动获取地址；')}DNS ${T('可沿用上级下发，也可在「')}DNS ${T('服务」页面自行指定。')}</p>`;
    const mt = $('#dh-mtu'); if (mt) mt.oninput = () => { S.cfg.pppoe = Object.assign($w('pppoe'), { dhcp_mtu: Number(mt.value) }); setActionMsg(T('已修改，请点击保存并应用')); };
    const dn = $('#dh-dns'); if (dn) dn.onchange = () => { S.cfg.pppoe = Object.assign($w('pppoe'), { dhcp_use_dns: dn.checked }); setActionMsg(T('已修改，请点击保存并应用')); };
  } else if (mode === 'static') {
    el.innerHTML = `
      <div class="row">
        <label>IP ${T('地址')} / ${T('掩码')}<input id="st-addr" value="${esc(p.static_address || '')}" placeholder="${T('例如')} 192.168.1.2/24"></label>
        <label>${T('网关')}<input id="st-gw" value="${esc(p.static_gateway || '')}" placeholder="${T('例如')} 192.168.1.1"></label>
      </div>
      <div class="row">
        <label>DNS 1<input id="st-dns1" value="${esc((p.static_dns || '').split(',')[0] || '')}" placeholder="223.5.5.5"></label>
        <label>DNS 2<input id="st-dns2" value="${esc((p.static_dns || '').split(',')[1] || '')}" placeholder="119.29.29.29"></label>
      </div>
      <p class="hint-inline">${T('静态地址需与上级网络同网段，否则无法上网。填写后可点下方「测试连通性」验证。')}</p>`;
    const bs = (id, k) => { const e = $(id); if (e) e.oninput = () => { S.cfg.pppoe = Object.assign($w('pppoe'), { [k]: e.value }); setActionMsg(T('已修改，请点击保存并应用')); }; };
    bs('#st-addr', 'static_address'); bs('#st-gw', 'static_gateway');
    const collectDns = () => {
      const a = ($('#st-dns1') || {}).value || '', b = ($('#st-dns2') || {}).value || '';
      S.cfg.pppoe = Object.assign($w('pppoe'), { static_dns: [a, b].filter(Boolean).join(',') });
      setActionMsg(T('已修改，请点击保存并应用'));
    };
    if ($('#st-dns1')) $('#st-dns1').oninput = collectDns;
    if ($('#st-dns2')) $('#st-dns2').oninput = collectDns;
  } else {
    el.innerHTML = `<div class="kv"><b>${T('拨号参数')}</b><span>${T('请在下方的「')}PPPoE ${T('宽带拨号」卡片中填写账号密码与网卡')}</span></div>`;
  }
  return el;
}

async function liveWan() {
  const el = $('#wan-live'); if (!el) return;
  const r = await api('/api/ifaces');
  const ifs = r.data || [];
  const wan = ifs.find(i => /^ppp/.test(i.name)) || ifs.find(i => i.name === ($w('system').wan_iface || ''));
  const sys = $w('system');
  if (!sys.wan_iface) { el.innerHTML = `<div class="kv"><b>${T('WAN 口')}</b><span><span class="tag warn">${T('未分配')}</span></span></div>`; return; }
  if (!wan) { el.innerHTML = `<div class="kv"><b>${T('WAN 口')}</b><span class="mono">${esc(sys.wan_iface)}</span></div>
    <div class="kv"><b>${T('连接状态')}</b><span><span class="tag gray">${T('未连接')}</span></span></div>`; return; }
  const rx = (wan.stat || {}).rx_bytes || 0, tx = (wan.stat || {}).tx_bytes || 0;
  const ip4 = (wan.addrs || []).filter(a => a.family === 'inet')[0];
  const ip6 = (wan.addrs || []).filter(a => a.family === 'inet6' && a.scope === 'global')[0];
  el.innerHTML = `
    <div class="kv"><b>${T('接口')}</b><span class="mono">${esc(wan.name)} ${wan.oper === 'UP' ? '<span class="tag ok">UP</span>' : '<span class="tag gray">DOWN</span>'}</span></div>
    <div class="kv"><b>IPv4 ${T('地址')}</b><span class="mono">${ip4 ? esc(ip4.addr) + '/' + ip4.prefix : `<span class="tag warn">${T('未获取')}</span>`}</span></div>
    <div class="kv"><b>IPv6 ${T('地址')}</b><span class="mono">${ip6 ? esc(ip6.addr) + '/' + ip6.prefix : `<span class="tag warn">${T('未获取')}</span>`}</span></div>
    <div class="kv"><b>MTU</b><span>${wan.mtu}</span></div>
    <div class="kv"><b>${T('累计接收')}</b><span>${fmtBytes(rx)}</span></div>
    <div class="kv"><b>${T('累计发送')}</b><span>${fmtBytes(tx)}</span></div>
    <div class="kv"><b>${T('实时速率')}</b><span id="wan-rate">${T('测量中')}…</span></div>`;
  const t0 = Date.now(), r0 = rx, x0 = tx;
  setTimeout(async () => {
    const r2 = await api('/api/ifaces');
    const w2 = (r2.data || []).find(i => i.name === wan.name); if (!w2) return;
    const dt = (Date.now() - t0) / 1000;
    const dr = ((w2.stat.rx_bytes || 0) - r0) / dt, dx = ((w2.stat.tx_bytes || 0) - x0) / dt;
    const e2 = $('#wan-rate');
    if (e2) e2.innerHTML = `↓ ${fmtBytes(dr)}/s &nbsp;&nbsp; ↑ ${fmtBytes(dx)}/s`;
  }, 1500);
}

/* ============================ LAN ============================ */
function viewLan() {
  const sys = $w('system');
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('局域网接口')}</h3>
      <p class="desc">${T('指定本路由器在局域网中的地址。当前使用')} DHCP ${T('获取的地址，建议在正式切换时改为静态。')}</p>
      <div class="row">
        <label>LAN ${T('网卡')}<select id="ln-if">
          <option value="">${T('未指定')}</option>
          ${S.ifaces.map(i => `<option value="${esc(i.name)}" ${sys.lan_iface === i.name ? 'selected' : ''}>${esc(i.name)} (${esc(i.mac)})</option>`).join('')}
        </select></label>
        <label>LAN ${T('地址')} / ${T('掩码')}<input id="ln-addr" value="${esc(sys.lan_address || '192.168.7.3/24')}" placeholder="192.168.7.3/24"></label>
        <label>MTU<input id="ln-mtu" type="number" value="${esc(sys.lan_mtu || 1500)}"></label>
      </div>
      <div class="row">
        <label>${T('网关地址（下发给客户端）')}<input id="ln-gw" value="${esc($w('dnsmasq').option_gateway || '')}" placeholder="${T('与 DHCP 页 Option 3')} ${T('同源')}"></label>
        <label>${T('域名后缀')}<input id="ln-dom" value="${esc($w('dnsmasq').domain || 'lan')}"></label>
      </div>
      <p class="hint-inline">${T('当前系统实际地址：')}<span class="mono" id="ln-cur"></span></p>
    </div>
    <div class="card">
      <h3>${T('桥接成员')}</h3>
      <p class="desc">${T('桥接后在「网卡与桥接」页勾选成员口；此处显示当前桥接状态。')}</p>
      <div class="kv"><b>${T('网桥状态')}</b><span>${sys.bridge_enabled ? `<span class="tag ok">${T('已启用')}</span>` : `<span class="tag gray">${T('未启用')}</span>`}</span></div>
      <div class="kv"><b>${T('成员口')}</b><span class="mono">${(sys.bridge_members || []).join(', ') || T('无')}</span></div>
      <div class="kv"><b>${T('说明')}</b><span>${T('当前仅一个物理网口，无剩余口可桥接')}</span></div>
    </div>`;
  const bind = (id, key, conv) => {
    const el = $(id); if (!el) return;
    el.oninput = el.onchange = () => { S.cfg.system = Object.assign($w('system'), { [key]: conv ? conv(el.value) : el.value }); setActionMsg(T('已修改，请点击保存并应用')); };
  };
  bind('#ln-if', 'lan_iface'); bind('#ln-addr', 'lan_address');
  bind('#ln-mtu', 'lan_mtu', numOr);
  // 网关 / 域名后缀实际由 dnsmasq 下发：render 只读 dnsmasq 模块，
  // 原先错绑到 system.gateway / system.domain —— 那两个键谁也读不到，改了等于没改
  const bindQ = (id, key) => {
    const el = $(id); if (!el) return;
    el.oninput = el.onchange = () => { S.cfg.dnsmasq = Object.assign($w('dnsmasq'), { [key]: el.value }); setActionMsg(T('已修改，请点击保存并应用')); };
  };
  bindQ('#ln-gw', 'option_gateway'); bindQ('#ln-dom', 'domain');
  api('/api/ifaces').then(r => {
    const i = (r.data || []).find(x => x.name === sys.lan_iface);
    const a = i && (i.addrs || []).find(x => x.family === 'inet');
    const e = $('#ln-cur'); if (e) e.textContent = a ? a.addr + '/' + a.prefix + '（' + (a.valid ? T('动态获取') : T('静态')) + '）' : '—';
  });
}

/* ============================ DHCP ============================ */
function viewDhcp() {
  const d = $w('dnsmasq');
  const opts = d.options || [];
  $('#view').innerHTML = `
    <div class="card">
      <h3>DHCP ${T('服务开关')}</h3>
      <p class="desc">${T('关闭时仅做')} DNS ${T('转发，不分配地址。构建阶段默认关闭，避免与现有网络中的')} DHCP ${T('冲突。')}</p>
      <label class="switch"><input type="checkbox" id="dh-en" ${d.dhcp_enabled ? 'checked' : ''}><i></i>${T('启用')} DHCP ${T('服务')}</label>
    </div>
    <div class="card">
      <h3>${T('地址池')}</h3>
      <div class="row">
        <label>${T('起始地址')}<input id="dh-s" value="${esc(d.pool_start || '')}"></label>
        <label>${T('结束地址')}<input id="dh-e" value="${esc(d.pool_end || '')}"></label>
        <label>${T('子网掩码')}<input id="dh-m" value="${esc(d.pool_netmask || '255.255.255.0')}"></label>
        <label>${T('租期（秒）')}<input id="dh-t" type="number" value="${esc(d.lease_time || 7200)}"></label>
      </div>
    </div>
    <div class="card">
      <h3>DHCP Options（RouterOS ${T('风格）')}</h3>
      <p class="desc">${T('对应')} <span class="mono">dhcp-option=&lt;code&gt;,&lt;value&gt;</span>。
      <b>code 3</b> = ${T('默认网关，')}<b>code 6</b> = ${T('客户端该用哪台')} DNS${T('（一般填')}<b>${T('本机')}</b>${T('地址，由本机再转发到上游，')}
      <b>${T('不是')}</b>${T('填上游')} DNS${T('）。可自由添加任意')} code${T('（如 43、121 等）。')}</p>
      <div class="row" style="margin-bottom:12px">
        <label style="flex:0 0 160px">Option code 3 · ${T('网关')}<input id="op-gw" value="${esc(d.option_gateway || '')}" placeholder="192.168.7.3"></label>
        <label style="flex:0 0 260px">Option code 6 · DNS<input id="op-dns" value="${esc(d.option_dns || '')}" placeholder="192.168.7.3"></label>
      </div>
      <table><thead><tr><th style="width:44px">${T('启用')}</th><th style="width:90px">Code</th>
        <th>${T('值（Value）')}</th><th style="width:150px">${T('备注')}</th><th style="width:88px">${T('强制')}</th><th style="width:64px"></th></tr></thead>
      <tbody id="op-body">
      ${opts.map((o, i) => `<tr>
        <td><input type="checkbox" class="op-en" data-i="${i}" ${o.enabled ? 'checked' : ''}></td>
        <td><input class="op-code mono" data-i="${i}" value="${esc(o.code)}"></td>
        <td><input class="op-val mono" data-i="${i}" value="${esc(o.value)}" placeholder="IP ${T('或文本')}"></td>
        <td><input class="op-rm" data-i="${i}" value="${esc(o.remark || '')}"></td>
        <td><input type="checkbox" class="op-fc" data-i="${i}" ${o.force ? 'checked' : ''}></td>
        <td><button class="small danger op-del" data-i="${i}">${T('删除')}</button></td></tr>`).join('')}
      </tbody></table>
      <div style="margin-top:12px"><button class="small" id="op-add">+ ${T('添加自定义')} Option</button></div>
      <p class="hint-inline">${T('提示：勾选「强制」会使用')} dhcp-option-force${T('，即使客户端未请求也会下发。')}
      code 3 / 6 ${T('请优先用上面的专属字段')} —— ${T('下表里重复写同一个')} code ${T('不会报错， 会与专属字段')}**${T('合并成一行')}**${T('（顺序去重），所以不必担心写出重复指令。')}</p>
    </div>
    <div class="card">
      <h3>${T('静态地址绑定')}</h3>
      <p class="desc">${T('按 MAC')} ${T('固定分配')} IP${T('，格式：')}<span class="mono">dhcp-host=MAC,IP[,${T('名称')}]</span></p>
      <table><thead><tr><th style="width:44px">${T('启用')}</th><th>MAC</th><th>IP</th><th>${T('名称')}</th><th style="width:64px"></th></tr></thead>
      <tbody id="sl-body">
      ${(d.static_leases || []).map((s, i) => `<tr>
        <td><input type="checkbox" class="sl-en" data-i="${i}" ${s.enabled ? 'checked' : ''}></td>
        <td><input class="sl-mac mono" data-i="${i}" value="${esc(s.mac || '')}"></td>
        <td><input class="sl-ip mono" data-i="${i}" value="${esc(s.ip || '')}"></td>
        <td><input class="sl-nm" data-i="${i}" value="${esc(s.name || '')}"></td>
        <td><button class="small danger sl-del" data-i="${i}">${T('删除')}</button></td></tr>`).join('')}
      </tbody></table>
      <div style="margin-top:12px"><button class="small" id="sl-add">+ ${T('添加静态绑定')}</button></div>
    </div>
    <div class="card">
      <h3>${T('当前租约（实时）')}</h3>
      <p class="desc">dnsmasq ${T('已分配的客户端，类似')} RouterOS ${T('的 Lease')} ${T('列表。')}
      <b>${T('双击')} IP</b> ${T('可为该客户端选择')} DHCP Options${T('；每行右侧可一键转为静态绑定、或回收该')} IP ${T('回地址池。')}</p>
      <div class="row" style="margin-bottom:10px">
        <button class="ghost small fixed" id="dh-lease-refresh">${T('刷新租约')}</button>
        <label class="switch" style="margin-left:14px"><input type="checkbox" id="dh-lease-auto" checked><i></i>${T('每 10')} ${T('秒自动刷新')}</label>
      </div>
      <div id="dh-leases">${T('读取中')}…</div>
    </div>`;

  const sync = () => {
    const o = (d.options || []).map((x, i) => {
      const g = k => $(`.op-${k}[data-i="${i}"]`);
      return { enabled: g('en').checked, code: g('code').value, value: g('val').value, remark: g('rm').value, force: g('fc').checked };
    });
    const s = (d.static_leases || []).map((x, i) => {
      const g = k => $(`.sl-${k}[data-i="${i}"]`);
      return { enabled: g('en').checked, mac: g('mac').value, ip: g('ip').value, name: g('nm').value };
    });
    S.cfg.dnsmasq = Object.assign($w('dnsmasq'), {
      dhcp_enabled: $('#dh-en').checked,
      pool_start: $('#dh-s').value, pool_end: $('#dh-e').value,
      pool_netmask: $('#dh-m').value, lease_time: numOr($('#dh-t').value),
      option_gateway: $('#op-gw').value, option_dns: $('#op-dns').value,
      options: o, static_leases: s,
    });
    setActionMsg(T('已修改，请点击保存并应用'));
  };
  // 每次 rebuild 都会重跑整个 viewDhcp，新闭包里的 leaseTimer 会覆盖
  // stopLeaseTimer 指向的旧闭包 —— 旧 interval 的句柄就此丢失，
  // clearInterval 再也调不到它。加 10 条自定义 Option 就泄漏 10 个
  // 10 秒轮询，持续空打 /api/leases。所以这里先停掉旧的再重建。
  const rebuild = () => {
    stopLeaseTimer();
    S.cfg.dnsmasq = Object.assign($w('dnsmasq'), { options: d.options, static_leases: d.static_leases });
    viewDhcp();
  };
  $$('.op-en,.op-code,.op-val,.op-rm,.op-fc').forEach(e => e.oninput = e.onchange = sync);
  $$('.sl-en,.sl-mac,.sl-ip,.sl-nm').forEach(e => e.oninput = e.onchange = sync);
  $('#dh-en').onchange = sync; $('#dh-s').oninput = sync; $('#dh-e').oninput = sync;
  $('#dh-m').oninput = sync; $('#dh-t').oninput = sync;
  $('#op-gw').oninput = sync; $('#op-dns').oninput = sync;
  $$('.op-del').forEach(b => b.onclick = () => { d.options.splice(+b.dataset.i, 1); rebuild(); });
  $$('.sl-del').forEach(b => b.onclick = () => { d.static_leases.splice(+b.dataset.i, 1); rebuild(); });
  $('#op-add').onclick = () => { d.options.push({ enabled: true, code: '', value: '', remark: '', force: false }); rebuild(); };
  $('#sl-add').onclick = () => { d.static_leases.push({ enabled: true, mac: '', ip: '', name: '' }); rebuild(); };
  // 租约刷新（手动 / 自动）
  let leaseTimer = null;
  const loadLeases = () => {
    // 先判 DOM 再发请求：切走页面后 box 就不存在了，早期实现是先 fetch 再判，
    // 结果切页后仍在每 10 秒空打一次 /api/leases。这里前置判断 + 停表双保险。
    if (!$('#dh-leases')) {
      if (leaseTimer) { clearInterval(leaseTimer); leaseTimer = null; }
      return Promise.resolve();
    }
    return api('/api/leases').then(r => {
    const rows = r.data || [];
    const box = $('#dh-leases'); if (!box) return;
    box.innerHTML = rows.length ? `<table><thead><tr>
      <th style="width:130px">IP ${T('地址')}</th><th style="width:150px">MAC ${T('地址')}</th><th>${T('主机名')}</th>
      <th style="width:110px">${T('剩余时间')}</th><th style="width:96px">${T('类型')}</th><th style="width:210px">${T('操作')}</th></tr></thead><tbody>
      ${rows.map(x => `<tr>
        <td class="mono lease-ip" data-ip="${esc(x.ip)}" data-mac="${esc(x.mac)}" data-host="${esc(x.name || '')}"
            style="cursor:pointer;text-decoration:underline dotted" title="${T('双击选择 DHCP Options')}">${esc(x.ip)}</td>
        <td class="mono">${esc(x.mac)}</td>
        <td>${esc(x.name || '—')}</td>
        <td>${esc(x.remain_text || '—')}</td>
        <td>${x.is_static ? `<span class="tag ok">${T('静态')}</span>` : `<span class="tag gray">${T('动态')}</span>`}</td>
        <td>
          <button class="small lease-mk" data-ip="${esc(x.ip)}" data-mac="${esc(x.mac)}" data-host="${esc(x.name || '')}"
            ${x.is_static ? 'disabled' : ''}>${T('转为静态')}</button>
          <button class="small danger lease-rm" data-ip="${esc(x.ip)}" data-mac="${esc(x.mac)}">${T('回收 IP')}</button>
        </td></tr>`).join('')}
      </tbody></table>` : `<p class="desc">${T('暂无租约记录（DHCP 服务未启动或尚无客户端）。')}</p>`;
    bindLeaseActions();
    });
  };
  // 把「停表」动作挂到全局钩子，go() 切页时会调用它（闭包内变量外面拿不到）
  stopLeaseTimer = () => { if (leaseTimer) { clearInterval(leaseTimer); leaseTimer = null; } };
  const rf = $('#dh-lease-refresh');
  if (rf) rf.onclick = () => { loadLeases(); toast(T('租约已刷新'), 'ok'); };
  const auto = $('#dh-lease-auto');
  if (auto) {
    auto.onchange = () => {
      stopLeaseTimer();
      if (auto.checked) leaseTimer = setInterval(loadLeases, 10000);
    };
    if (auto.checked) leaseTimer = setInterval(loadLeases, 10000);
  }
}

/* 绑定租约表格上的按钮：转为静态 / 回收 / 双击选 Options */
function bindLeaseActions() {
  $$('.lease-mk').forEach(b => b.onclick = async () => {
    const r = await api('/api/lease/static', { method: 'POST',
      body: { ip: b.dataset.ip, mac: b.dataset.mac, host: b.dataset.host } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err');
    if (r.ok && S.cfg.dnsmasq) {
      // 同步到页面上的静态绑定表
      const lst = ($w('dnsmasq').static_leases || []);
      if (!lst.some(x => (x.mac || '').toLowerCase() === b.dataset.mac.toLowerCase())) {
        lst.push({ enabled: true, mac: b.dataset.mac, ip: b.dataset.ip, name: b.dataset.host });
        S.cfg.dnsmasq = Object.assign($w('dnsmasq'), { static_leases: lst });
      }
      setActionMsg(T('已把 ') + b.dataset.ip + T(' 转为静态绑定，请点击保存并应用'));
    }
  });
  $$('.lease-rm').forEach(b => b.onclick = () => {
    modal(T('回收 IP 地址'), `<p>${T('即将回收客户端')} <b class="mono">${esc(b.dataset.ip)}</b>（${esc(b.dataset.mac)}${T('）的租约。')}</p>
      <p>${T('该客户端会暂时断网，下次续租时将重新分配地址。')}</p>
      <p>${T('请输入')} <b>${T('确认回收')}</b> ${T('以继续：')}</p><input id="cfm-lease" placeholder="${T('确认回收')}">`, async () => {
      if ($('#cfm-lease').value.trim() !== T('确认回收')) { toast(T('确认文字不正确'), 'err'); return false; }
      const r = await api('/api/lease/release', { method: 'POST',
        body: { ip: b.dataset.ip, mac: b.dataset.mac, confirm: true } });
      toast(r.msg_cn, r.ok ? 'ok' : 'err');
      if (r.ok) {
        const rf = $('#dh-lease-refresh'); if (rf) rf.click();
      }
    });
  });
  $$('.lease-ip').forEach(td => td.ondblclick = () => leaseOptions(td.dataset));
}

/* 双击租约 IP：为该客户端配置 DHCP Options（RouterOS 风格） */
function leaseOptions(ds) {
  const ip = ds.ip, mac = ds.mac, host = ds.host || '';
  modal(T('为该客户端下发 DHCP Options'), `
    <p>${T('客户端：')}<b class="mono">${esc(ip)}</b>　${T('主机名：')}${esc(host || '—')}　MAC：<span class="mono">${esc(mac)}</span></p>
    <p class="desc">${T('这些')} Options ${T('仅对当前客户端生效，等价于')} dnsmasq ${T('的')}
      <span class="mono">dhcp-host</span> + <span class="mono">dhcp-option</span> ${T('组合。')}</p>
    <label>${T('网关（')}code 3）<input id="lo-gw" placeholder="${T('留空则用全局网关')}"></label>
    <label>DNS（code 6）<input id="lo-dns" placeholder="223.5.5.5,119.29.29.29"></label>
    <label>${T('自定义')} Options${T('（每行一个')} code=${T('值）')}<textarea id="lo-extra" rows="3" placeholder="43=...&#10;121=..."></textarea></label>
    <label>${T('同时转为静态绑定')}<select id="lo-static">
      <option value="1">${T('是（推荐）')}</option><option value="0">${T('否，仅下发')} Options</option></select></label>`,
    async () => {
      const gw = $('#lo-gw').value.trim(), dns = $('#lo-dns').value.trim();
      const extra = $('#lo-extra').value.split('\n').map(x => x.trim()).filter(Boolean);
      const mkStatic = $('#lo-static').value === '1';
      const lst = ($w('dnsmasq').static_leases || []);
      const found = lst.find(x => (x.mac || '').toLowerCase() === mac.toLowerCase());
      const entry = found || { enabled: true, mac, ip, name: host };
      entry.enabled = true; entry.mac = mac; entry.ip = ip; entry.name = host;
      entry.options = [];
      if (gw) entry.options.push({ enabled: true, code: '3', value: gw, remark: T('客户端网关'), force: false });
      if (dns) entry.options.push({ enabled: true, code: '6', value: dns, remark: T('客户端 DNS'), force: false });
      extra.forEach(line => {
        const i = line.indexOf('=');
        if (i > 0) entry.options.push({ enabled: true, code: line.slice(0, i).trim(), value: line.slice(i + 1).trim(), remark: T('自定义'), force: false });
      });
      if (!found) lst.push(entry);
      S.cfg.dnsmasq = Object.assign($w('dnsmasq'), { static_leases: lst });
      if (mkStatic) {
        const r = await api('/api/lease/static', { method: 'POST', body: { ip, mac, host } });
        toast(r.msg_cn, r.ok ? 'ok' : 'err');
      }
      setActionMsg(T('已为该客户端配置 Options，请点击保存并应用'));
      return true;
    });
}

/* ============================ 防火墙 ============================ */

/* 防火墙示例规则：全部用 # 注释包裹，用户删除注释符即可启用 */
function FW_EXAMPLE(kind) {
  const v4 = kind === '4';
  const head = `# =====================================================================
# drouter ${v4 ? 'IPv4' : 'IPv6'} ${T('防火墙示例规则')}
# ${T('说明：以下每一行都以')} # ${T('开头（注释状态），不会生效。')}
#       ${T('删除行首的')} # ${T('即可启用该条规则；改完点击下方「仅语法检查」再「保存并应用」。')}
#       ${T('留空整个文本框')} = ${T('由系统按页面顶部参数自动生成默认规则。')}
# =====================================================================

# --- ${T('表与链的骨架（一般保持启用）')} ---
table inet drouter {
    chain input {
        type filter hook input priority 0; policy drop;
        # --- ${T('放行已建立连接（务必保留）')} ---
        ct state established,related accept
        # --- ${T('放行本机回环')} ---
        iif lo accept
        # --- ${T('放行')} ICMP（ping） ---
        ip protocol icmp accept
        # --- ${T('放行')} SSH${T('（按需修改端口）')} ---
        tcp dport 22 accept
        # --- ${T('放行')} Web ${T('管理端口')} ---
        tcp dport { 8443, 8080 } accept
    }
`;

  const body4 = `
    # --- ${T('放行')} LAN ${T('侧全部流量')} ---
    chain forward {
        type filter hook forward priority 0; policy drop;
        ct state established,related accept
        iifname "ens18" accept
        oifname "ens18" accept
    }
    # --- ${T('出站')} NAT（PPPoE / WAN ${T('共享上网）')} ---
    chain postrouting {
        type nat hook postrouting priority 100; policy accept;
        oifname "ppp0" masquerade
    }
    # --- PPPoE MSS ${T('钳制（避免部分网站打不开）')} ---
    chain forward_mss {
        type filter hook forward priority -1; policy accept;
        tcp flags syn tcp option maxseg size set rt mtu
    }
# =====================================================================
# ${T('常用可选规则（取消注释后生效）')}
# =====================================================================
# ${T('禁止某台设备联网：')}
#    iifname "ens18" ether saddr 00:11:22:33:44:55 drop
# ${T('限制某端口仅内网访问：')}
#    iifname "ppp0" tcp dport 3389 drop
# ${T('只允许指定')} DNS ${T('服务器：')}
#    iifname "ens18" udp dport 53 ip daddr != 192.168.7.3 drop
}

# ${T('如需端口转发（把外网')} 8080 ${T('转到内网')} 192.168.7.100:80）：
# table ip drouter_nat {
#     chain prerouting {
#         type nat hook prerouting priority -100; policy accept;
#         iifname "ppp0" tcp dport 8080 dnat to 192.168.7.100:80
#     }
# }`;

  const body6 = `
    # --- ${T('放行')} LAN ${T('侧全部流量')} ---
    chain forward {
        type filter hook forward priority 0; policy drop;
        ct state established,related accept
        iifname "ens18" accept
        oifname "ens18" accept
    }
# =====================================================================
# ${T('常用可选规则（取消注释后生效）')}
# =====================================================================
# ${T('放行')} ICMPv6（IPv6 ${T('必需，建议保留启用）：')}
#    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, echo-request } accept
# ${T('禁止某台设备使用')} IPv6 ${T('上网：')}
#    iifname "ens18" ether saddr 00:11:22:33:44:55 drop
# ${T('限制某端口仅内网访问：')}
#    iifname "ppp0" tcp dport 22 drop
}

# ${T('如需')} IPv6 ${T('端口转发（把外网')} 8080 ${T('转到内网')} [fd00::100]:80）：
# table ip6 drouter_nat6 {
#     chain prerouting {
#         type nat hook prerouting priority -100; policy accept;
#         iifname "ppp0" tcp dport 8080 dnat to [fd00::100]:80
#     }
# }`;

  return head + (v4 ? body4 : body6);
}

function viewFw(kind) {
  const key = kind === '4' ? 'nft_v4' : 'nft_v6';
  const c = $w(key);
  $('#view').innerHTML = `
    <div class="card">
      <h3>IPv${kind} ${T('防火墙规则')}</h3>
      <p class="desc">${T('使用')} nftables ${T('语法。应用前会自动执行')} <span class="mono">nft -c</span> ${T('语法预检， 失败会阻止应用并保留现有规则；应用前自动创建快照，可一键回滚。')}</p>
      <div class="row" style="margin-bottom:12px">
        <label style="flex:0 0 190px">WAN ${T('接口')}<input id="fw-wan" value="${esc(c.wan_iface || 'ppp0')}"></label>
        <label style="flex:0 0 250px">LAN ${T('接口（逗号分隔）')}<input id="fw-lan" value="${esc((c.lan_ifaces || []).join(','))}"></label>
        ${kind === '4' ? `<label style="flex:0 0 150px">PPPoE MSS ${T('钳制')}<select id="fw-mss">
          <option value="1" ${c.mss_clamp !== false ? 'selected' : ''}>${T('开启（推荐）')}</option>
          <option value="0" ${c.mss_clamp === false ? 'selected' : ''}>${T('关闭')}</option></select></label>` : ''}
      </div>
      <div class="row" style="margin-bottom:12px">
        <label style="flex:0 0 190px">…</label>
        <button class="ghost small fixed" id="fw-def">${T('按当前参数生成默认规则')}</button>
        <button class="ghost small fixed" id="fw-view">${T('查看系统当前生效规则')}</button>
      </div>
      <div class="card" style="background:var(--bg2);margin-bottom:12px">
        <h3 style="font-size:14px">${T('防火墙日志（滚动）')}</h3>
        <p class="desc">${T('把 nftables')} ${T('命中的包记录到内核日志，并在下面实时滚动显示（已翻译成简体中文）。 记录本身会占用少量')} CPU${T('，因此内置限速（默认')} 20 ${T('包/')}${T('秒），避免日志风暴拖垮小内存设备。')}</p>
        <div class="row" style="margin-bottom:8px">
          <label class="switch"><input type="checkbox" id="fw-logdrop" ${c.log_drop !== false ? 'checked' : ''}><i></i>${T('记录被拒绝')} / ${T('丢弃的包（推荐）')}</label>
        </div>
        <div class="row">
          <label class="switch"><input type="checkbox" id="fw-logaccept" ${c.log_accept === true ? 'checked' : ''}><i></i>${T('同时记录被放通的包（数据量大）')}</label>
          <label style="flex:0 0 170px">${T('日志限速（包')}/${T('秒）')}<input id="fw-lograte" type="number" min="1" max="1000" value="${esc(Number(c.log_rate || 20))}"></label>
        </div>
        <p class="hint-inline">${T('修改后请点击右上角「保存并应用」才会写入规则并生效。')}</p>
      </div>
      <label>${T('规则内容（可直接编辑）')}
        <textarea id="fw-raw" rows="26" spellcheck="false" placeholder="${T('留空则按上面参数自动生成默认规则')}">${esc(c.raw || FW_EXAMPLE(kind))}</textarea></label>
      <p class="hint-inline">${T('留空')} = ${T('使用系统按参数生成的默认规则（含')} NAT${T('、转发、')}MSS ${T('钳制）。 下方默认内容已用')} <span class="mono">#</span> ${T('注释包裹，删除注释符即可启用对应规则。')}</p>
    </div>
    <div class="card">
      <h3>${T('防火墙实时日志（滚动显示）')}</h3>
      <p class="desc">${T('数据来自内核日志中带')} <span class="mono">DROUTER-FW</span> ${T('前缀的规则命中记录，已逐条翻译。 开启自动滚动后可当作实时监控窗口使用。')}</p>
      <div class="row" style="margin-bottom:10px">
        <label style="flex:0 0 130px">${T('协议族')}<select id="fwl-fam">
          <option value="all">IPv4 + IPv6</option>
          <option value="4">${T('仅 IPv4')}</option>
          <option value="6">${T('仅 IPv6')}</option>
        </select></label>
        <label style="flex:0 0 140px">${T('动作')}<select id="fwl-act">
          <option value="">${T('全部动作')}</option>
          <option value="REJECT">${T('拒绝')}</option>
          <option value="DROP">${T('丢弃')}</option>
          <option value="ACCEPT">${T('放通')}</option>
        </select></label>
        <label style="flex:0 0 150px">${T('时间范围')}<select id="fwl-since">
          <option value="15 min ago">${T('最近')} 15 ${T('分钟')}</option>
          <option value="60 min ago" selected>${T('最近')} 1 ${T('小时')}</option>
          <option value="6 hours ago">${T('最近')} 6 ${T('小时')}</option>
          <option value="24 hours ago">${T('最近')} 24 ${T('小时')}</option>
        </select></label>
        <label style="flex:1 1 160px">${T('关键字')}<input id="fwl-q" placeholder="IP / ${T('端口')} / ${T('接口名')}"></label>
      </div>
      <div class="row" style="margin-bottom:10px">
        <button class="ghost fixed" id="fwl-refresh">${T('立即刷新')}</button>
        <label class="switch"><input type="checkbox" id="fwl-auto"><i></i>${T('自动滚动（每')} 3 ${T('秒）')}</label>
        <button class="ghost small fixed" id="fwl-export">${T('导出')} JSON</button>
        <button class="ghost small fixed" id="fwl-clear">${T('清空显示')}</button>
      </div>
      <div id="fwl-stat" class="desc"></div>
      <div id="fwl-out" style="margin-top:10px"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('预检与应用')}</h3>
      <div class="row">
        <button class="ghost fixed" id="fw-check">${T('仅语法检查（不应用）')}</button>
        <button class="fixed" id="fw-rollback">${T('查看快照列表')}</button>
      </div>
      <div id="fw-out" style="margin-top:12px"></div>
    </div>`;
  const sync = () => {
    S.cfg[key] = Object.assign($w(key), {
      wan_iface: $('#fw-wan').value,
      lan_ifaces: $('#fw-lan').value.split(',').map(x => x.trim()).filter(Boolean),
      raw: $('#fw-raw').value,
      log_drop: $('#fw-logdrop').checked,
      log_accept: $('#fw-logaccept').checked,
      log_rate: Number($('#fw-lograte').value) || 20,
      ...(kind === '4' ? { mss_clamp: $('#fw-mss').value === '1' } : {}),
    });
    setActionMsg(T('已修改，请点击保存并应用'));
  };
  ['#fw-wan', '#fw-lan', '#fw-raw', '#fw-lograte'].forEach(s => { const e = $(s); if (e) e.oninput = sync; });
  ['#fw-logdrop', '#fw-logaccept'].forEach(s => { const e = $(s); if (e) e.onchange = sync; });
  if (kind === '4') $('#fw-mss').onchange = sync;
  $('#fw-def').onclick = () => { $('#fw-raw').value = ''; sync(); toast(T('已切换为按参数自动生成默认规则'), 'ok'); };
  $('#fw-view').onclick = async () => {
    $('#fw-out').innerHTML = `<pre>${T('读取中…')}</pre>`;
    const r = await api('/api/nft');
    $('#fw-out').innerHTML = `<pre>${esc((r.data || {}).raw || r.msg_cn)}</pre>`;
  };
  $('#fw-check').onclick = async () => {
    toast(T('正在执行语法预检…'), 'ok');
    // 必须显式传 check_only:true。/api/apply 的 live 默认 False 只代表
    // 「写盘但不生效」，不代表「不写盘」—— 少了 check_only，
    // 这个「仅语法检查」按钮会把规则真写进 /etc/nftables.d/ 覆盖掉现有配置，
    // 而用户以为什么都没发生（下次「保存并应用」之前的任何回滚假设都不成立）。
    const r = await api('/api/apply', { method: 'POST',
      body: { module: key, data: $w(key), check_only: true, live: false } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 6000);
  };
  /* 防火墙页的「回滚」入口。快照列表现在只存在于系统设置页的
     「紧急救援通道」卡片里（内嵌，不再有 modal 分支），所以这里
     直接带用户过去，而不是渲染一个残缺的弹窗 —— 那个弹窗里
     压根没有备注和上锁开关，回滚哪一份只能靠猜时间戳。 */
  $('#fw-rollback').onclick = () => { go('sys'); toast(T('请在「紧急救援通道 → 可回滚的快照」里挑选并回滚'), 'ok', 5000); };
  fwLogBind(kind);
}

/* ---------- 防火墙实时日志（#1） ---------- */
let FWL_TIMER = null;

function fwLogBind(kind) {
  if (FWL_TIMER) { clearInterval(FWL_TIMER); FWL_TIMER = null; }
  const fam = $('#fwl-fam');
  if (fam) fam.value = 'all';
  const load = () => fwLogLoad();
  $('#fwl-refresh').onclick = load;
  ['#fwl-fam', '#fwl-act', '#fwl-since'].forEach(s => { const e = $(s); if (e) e.onchange = load; });
  const q = $('#fwl-q');
  if (q) q.oninput = debounce(load, 400);
  $('#fwl-export').onclick = async () => {
    const r = await api(fwLogUrl());
    const blob = new Blob([JSON.stringify((r.data || {}).items || [], null, 2)],
      { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'drouter-firewall-log-' + Date.now() + '.json';
    a.click();
  };
  $('#fwl-clear').onclick = () => { $('#fwl-out').innerHTML = `<p class="desc">${T('已清空显示（不影响内核日志）。')}</p>`; };
  const auto = $('#fwl-auto');
  if (auto) auto.onchange = () => {
    if (FWL_TIMER) { clearInterval(FWL_TIMER); FWL_TIMER = null; }
    if (auto.checked) {
      load();
      FWL_TIMER = setInterval(load, 3000);
      toast(T('已开启自动滚动（每 3 秒）'), 'ok', 2000);
    } else {
      toast(T('已关闭自动滚动'), 'ok', 2000);
    }
  };
  load();
}

function fwLogUrl() {
  const fam = ($('#fwl-fam') || {}).value || 'all';
  const act = ($('#fwl-act') || {}).value || '';
  const since = ($('#fwl-since') || {}).value || '60 min ago';
  const q = ($('#fwl-q') || {}).value || '';
  return `/api/fw/log?family=${encodeURIComponent(fam)}&action=${encodeURIComponent(act)}`
    + `&since=${encodeURIComponent(since)}&q=${encodeURIComponent(q)}&limit=300`;
}

async function fwLogLoad() {
  const out = $('#fwl-out');
  if (!out) return;
  const r = await api(fwLogUrl());
  if (!r.ok) { out.innerHTML = `<p class="desc" style="color:var(--err)">${esc(r.msg_cn || T('读取失败'))}</p>`; return; }
  const d = r.data || {};
  const items = d.items || [];
  const st = d.stat || {};
  const statEl = $('#fwl-stat');
  if (statEl) {
    const en = d.enabled !== false;
    // 规则没载入内核时，后面两个 tag 会直接解释「为什么一条都没有」，
    // 避免用户对着空白页以为这个功能根本没做。
    const ap = d.applied !== false;
    const fw = d.forwarding === true;
    statEl.innerHTML = `${en ? `<span class="tag ok">${T('日志开关已开启')}</span>` : `<span class="tag warn">${T('日志开关已关闭')}</span>`}
      ${ap ? `<span class="tag ok">${T('规则已载入')}</span>` : `<span class="tag warn">${T('规则未载入')}</span>`}
      ${fw ? `<span class="tag ok">${T('IP 转发已开启')}</span>` : `<span class="tag gray" title="${T('本机当前不转发流量，通常是还没接管路由')}">${T('IP 转发未开启')}</span>`}
      <span class="tag gray">${T('共')} ${st.total || 0} ${T('条')}</span>
      <span class="tag err">${T('拒绝')}/${T('丢弃')} ${st.reject || 0}</span>
      <span class="tag info">${T('放通')} ${st.accept || 0}</span>
      <span class="tag gray">IPv4 ${st.v4 || 0}</span>
      <span class="tag gray">IPv6 ${st.v6 || 0}</span>
      <span style="color:var(--txt3);font-size:12px">　${T('区间：')}${esc(d.since || '')}</span>`;
  }
  if (d.warn) {
    out.innerHTML = `<p class="desc" style="color:var(--warn,#f59e0b)">${esc(bt4('PAGE_NOTES', d.warn, 'd', d.warn) || d.warn)}</p>`;
    return;
  }
  if (!items.length) {
    out.innerHTML = `<p class="desc">${T('该范围内没有命中记录（这是好事：说明没有异常流量被拒绝）。')}</p>`;
    return;
  }
  // 最新的排在最上面，便于滚动观察
  const rows = items.slice().reverse();
  out.innerHTML = `<div style="max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:8px;background:var(--bg2)">
    ${rows.map(x => `<div class="logline" title="${esc(x.raw || '')}">
      <span class="lv ${x.action === 'ACCEPT' ? 'ok' : (x.action === 'INVALID' ? 'warn' : 'err')}">${esc(x.family === 'IPv6' ? 'v6' : 'v4')}</span>
      <span class="ts">${esc(x.ts || '')}</span>
      <span class="mo">${esc(x.action_cn || '')}</span>
      <span class="ms">${esc(x.summary_cn || '')}
        ${x.len ? `<span style="color:var(--txt3)">· ${esc(x.len)} ${T('字节')}${x.ttl ? ' · TTL/Hop ' + esc(x.ttl) : ''}</span>` : ''}
      </span>
    </div>`).join('')}</div>`;
}

/* ============================ IPv6 ============================ */

/* IPv6 池策略（对应 RouterOS pool-policy） */
const V6P_POLICIES = () => ([
  { v: 'recommend', n: T('推荐（recommend）'), d: T('默认策略，优先选用推荐前缀，兼容性最好') },
  { v: 'strict', n: T('严格（strict）'), d: T('严格按池范围分配，池用尽时不分新地址') },
  { v: 'without-acquire', n: T('不主动获取（without-acquire）'), d: T('仅使用已有前缀，不主动向运营商索取新的 PD') },
  { v: 'auto-link-local', n: T('自动链路本地（auto link-local）'), d: T('无公网前缀时回退使用 fe80:: 链路本地地址') },
]);

function viewIpv6() {
  const r = $w('radvd'), d = $w('dhcpv6');
  $('#view').innerHTML = `
    <div class="card">
      <h3>WAN ${T('侧 IPv6')} ${T('获取')}</h3>
      <p class="desc">${T('通过')} DHCPv6 ${T('客户端获取地址与前缀委派（')}PD）。
      <b>${T('注意：')}</b>${T('运营商是否下发')} PD ${T('需实测，有链路地址不代表能拿到可分配给内网的前缀。')}</p>
      <div id="v6-live">${T('读取中')}…</div>
    </div>
    <div class="card">
      <h3>DHCPv6 / ${T('前缀委派（')}PD）</h3>
      <div class="row">
        <label>WAN ${T('接口')}<input id="dv-wan" value="${esc(d.wan_iface || 'ppp0')}"></label>
        <label>LAN ${T('接口')}<input id="dv-lan" value="${esc(d.lan_iface || 'ens18')}"></label>
        <label>${T('子网前缀长度')}<input id="dv-plen" type="number" value="${esc(d.prefix_len || 64)}"></label>
      </div>
      <div class="row">
        <label>SLA-ID ${T('后缀')}<input id="dv-sla" value="${esc(d.sla_id || '::1')}"></label>
        <label>IAID<input id="dv-iaid" type="number" value="${esc(d.iaid || 0)}"></label>
      </div>
      <label class="switch"><input type="checkbox" id="dv-pd" ${d.request_pd !== false ? 'checked' : ''}><i></i>${T('请求前缀委派（')}PD）</label>
      <label class="switch"><input type="checkbox" id="dv-na" ${d.slaac ? 'checked' : ''}><i></i>${T('同时请求接口地址（')}IA_NA）</label>
    </div>
    <div class="card">
      <h3>${T('局域网')} RA ${T('通告（')}radvd）</h3>
      <p class="desc">${T('向局域网设备通告')} IPv6 ${T('路由与地址前缀。')}</p>
      <div class="row">
        <label>${T('通告接口')}<input id="ra-if" value="${esc(r.iface || 'ens18')}"></label>
        <label>${T('通告前缀（如运营商下发')} /64）<input id="ra-pfx" value="${esc(r.prefix || '')}" placeholder="2408:xxxx:xxxx:xxxx::/64"></label>
      </div>
      <div class="row">
        <label>${T('最大通告间隔（秒）')}<input id="ra-max" type="number" value="${esc(r.max_interval || 600)}"></label>
        <label>${T('最小通告间隔（秒）')}<input id="ra-min" type="number" value="${esc(r.min_interval || 200)}"></label>
        <label>${T('默认路由生命期')}<input id="ra-life" type="number" value="${esc(r.lifetime || 1800)}"></label>
        <label>${T('跳数限制')}<input id="ra-hop" type="number" value="${esc(r.hop_limit || 64)}"></label>
      </div>
      <div class="row">
        <label>${T('前缀有效生命期')}<input id="ra-vl" type="number" value="${esc(r.valid_life || 86400)}"></label>
        <label>${T('前缀首选生命期')}<input id="ra-pl" type="number" value="${esc(r.pref_life || 14400)}"></label>
        <label>RDNSS${T('（下发')} DNS）<input id="ra-rdnss" value="${esc(r.rdnss || '')}" placeholder="2400:3200::1"></label>
        <label>DNSSL${T('（域名）')}<input id="ra-dnssl" value="${esc(r.dnssl || '')}" placeholder="lan"></label>
        <label>RDNSS ${T('生命期（秒）')}<input id="ra-rdnss-life" type="number" min="60" max="9000"
               value="${esc(Number(r.rdnss_life || 600))}"></label>
        <label>DNSSL ${T('生命期（秒）')}<input id="ra-dnssl-life" type="number" min="60" max="9000"
               value="${esc(Number(r.dnssl_life || r.rdnss_life || 600))}"></label>
      </div>
      <p class="hint-inline">${T('两个生命期决定客户端保留这份')} DNS ${T('多久。')}DNSSL ${T('没填时跟随')} RDNSS ${T('的值； 注意不要写成')} <span class="mono">r.rdnss_life || 600</span> ${T('之外的省略形式')} —— ${T('它们是数字输入框，')}
      0 ${T('不会被使用，这里给的都是合法默认值。')}</p>
      <label class="switch"><input type="checkbox" id="ra-mg" ${r.managed ? 'checked' : ''}><i></i>M ${T('标志（客户端用')} DHCPv6 ${T('取地址）')}</label>
      <label class="switch"><input type="checkbox" id="ra-oc" ${r.other_config ? 'checked' : ''}><i></i>O ${T('标志（客户端用')} DHCPv6 ${T('取其他配置）')}</label>
    </div>
    <div class="card">
      <h3>ND ${T('邻居发现参数')}</h3>
      <p class="desc">${T('来自内核')} /proc/sys/net/ipv6${T('，只读展示；如需修改可在服务器上用')} sysctl ${T('调整。')}</p>
      <div id="v6-nd">${T('读取中')}…</div>
    </div>
    <div class="card">
      <h3>IPv6 ${T('地址池')} / ${T('前缀下发（')}RouterOS ${T('风格）')}</h3>
      <p class="desc">${T('把从运营商获取的')} IPv6 ${T('前缀分配给内网。类似')} RouterOS ${T('的')}
      <span class="mono">/ipv6 address from-pool=</span> + <span class="mono">pool-policy</span>。</p>
      <label class="switch"><input type="checkbox" id="v6p-en" ${($w('radvd').pool_enabled !== false) ? 'checked' : ''}><i></i>${T('启用地址池（从池中分配地址给内网接口）')}</label>
      <div class="row">
        <label>${T('地址来源')}<select id="v6p-from">
          <option value="pool" ${($w('radvd').pool_from || 'pool') === 'pool' ? 'selected' : ''}>${T('从地址池分配（')}from pool）</option>
          <option value="manual" ${$w('radvd').pool_from === 'manual' ? 'selected' : ''}>${T('手动指定前缀')}</option>
          <option value="pd" ${$w('radvd').pool_from === 'pd' ? 'selected' : ''}>${T('自动跟随')} PD ${T('前缀')}</option>
        </select></label>
        <label>${T('池名称')}<input id="v6p-name" value="${esc($w('radvd').pool_name || 'drouter-wan-pool')}"></label>
        <label>${T('池前缀（可留空，取自')} PD）<input id="v6p-prefix" value="${esc($w('radvd').pool_prefix || '')}" placeholder="2408:xxxx::/56"></label>
      </div>
      <div class="row">
        <label>${T('子网')} ID ${T('长度（借用主机位做子网）')}<input id="v6p-sub" type="number" value="${esc($w('radvd').pool_subnet_bits || 8)}"></label>
        <label>${T('分配起始子网')} ID<input id="v6p-start" type="number" value="${esc($w('radvd').pool_start || 1)}"></label>
        <label>${T('分配结束子网')} ID<input id="v6p-end" type="number" value="${esc($w('radvd').pool_end || 254)}"></label>
      </div>
    </div>

    <div class="card">
      <h3>${T('池策略（')}Pool Policy）</h3>
      <p class="desc">${T('对应')} RouterOS ${T('的 pool-policy')}${T('，可多选。勾选的策略会在生成地址与')} RA ${T('通告时生效。')}</p>
      <div id="v6p-policies">
        ${V6P_POLICIES().map(p => `<label class="switch"><input type="checkbox" class="v6pol" value="${esc(p.v)}"
          ${(($w('radvd').pool_policy || ['recommend']).includes(p.v)) ? 'checked' : ''}><i></i>
          <b>${p.n}</b>　<span style="color:var(--txt3);font-size:12px">${p.d}</span></label>`).join('')}
      </div>
    </div>

    <div class="card">
      <h3>${T('当前获取到的')} IPv6 ${T('前缀（实时）')}</h3>
      <p class="desc">${T('系统自动探测，可在此为每个前缀添加备注（例如「电信')} /56」）。</p>
      <div id="v6-pool"><p class="desc">${T('读取中')}…</p></div>
    </div>`;
  const rsync = () => {
    S.cfg.radvd = Object.assign($w('radvd'), {
      iface: $('#ra-if').value, prefix: $('#ra-pfx').value,
      max_interval: Number($('#ra-max').value), min_interval: Number($('#ra-min').value),
      lifetime: Number($('#ra-life').value), hop_limit: Number($('#ra-hop').value),
      valid_life: Number($('#ra-vl').value), pref_life: Number($('#ra-pl').value),
      rdnss: $('#ra-rdnss').value, dnssl: $('#ra-dnssl').value,
      rdnss_life: Number($('#ra-rdnss-life').value) || 600,
      dnssl_life: Number($('#ra-dnssl-life').value) || Number($('#ra-rdnss-life').value) || 600,
      managed: $('#ra-mg').checked, other_config: $('#ra-oc').checked,
    });
    S.cfg.dhcpv6 = Object.assign($w('dhcpv6'), {
      wan_iface: $('#dv-wan').value, lan_iface: $('#dv-lan').value,
      prefix_len: Number($('#dv-plen').value), sla_id: $('#dv-sla').value,
      iaid: Number($('#dv-iaid').value),
      request_pd: $('#dv-pd').checked, slaac: $('#dv-na').checked,
    });
    setActionMsg(T('已修改，请点击保存并应用'));
  };
  ['#ra-if', '#ra-pfx', '#ra-max', '#ra-min', '#ra-life', '#ra-hop', '#ra-vl', '#ra-pl',
    '#ra-rdnss', '#ra-dnssl', '#ra-rdnss-life', '#ra-dnssl-life',
    '#dv-wan', '#dv-lan', '#dv-plen', '#dv-sla', '#dv-iaid']
    .forEach(s => { const e = $(s); if (e) e.oninput = rsync; });
  ['#ra-mg', '#ra-oc', '#dv-pd', '#dv-na'].forEach(s => { const e = $(s); if (e) e.onchange = rsync; });

  // IPv6 地址池 / 池策略
  const psync = () => {
    const policy = $$('.v6pol').filter(x => x.checked).map(x => x.value);
    S.cfg.radvd = Object.assign($w('radvd'), {
      pool_enabled: $('#v6p-en').checked,
      pool_from: $('#v6p-from').value,
      pool_name: $('#v6p-name').value,
      pool_prefix: $('#v6p-prefix').value,
      pool_subnet_bits: Number($('#v6p-sub').value),
      pool_start: Number($('#v6p-start').value),
      pool_end: Number($('#v6p-end').value),
      pool_policy: policy,
    });
    setActionMsg(T('地址池设置已修改，请点击保存并应用'));
  };
  ['#v6p-name', '#v6p-prefix', '#v6p-sub', '#v6p-start', '#v6p-end'].forEach(s => {
    const e = $(s); if (e) e.oninput = psync;
  });
  ['#v6p-en', '#v6p-from'].forEach(s => { const e = $(s); if (e) e.onchange = psync; });
  $$('.v6pol').forEach(c => c.onchange = psync);

  api('/api/ipv6').then(r2 => {
    const d2 = r2.data || {};
    const g = (d2.addrs || []).flatMap(a => (a.addr_info || []).filter(x => x.family === 'inet6')
      .map(x => ({ if: a.ifname, a: x.local, p: x.prefixlen, sc: x.scope })));
    const glob = g.filter(x => x.sc === 'global');
    $('#v6-live').innerHTML = `
      <div class="kv"><b>${T('公网地址')}</b><span class="mono">${glob.length ? glob.map(x => esc(x.a) + '/' + x.p + ' @' + esc(x.if)).join('<br>') : `<span class="tag warn">${T('未获取')}</span>`}</span></div>
      <div class="kv"><b>${T('默认路由')}</b><span class="mono">${esc(d2.default_route || T('无'))}</span></div>
      <div class="kv"><b>PD ${T('前缀')}</b><span class="mono">${glob.some(x => x.p <= 60) ? `<span class="tag ok">${T('似乎已获取委派前缀')}</span>` : `<span class="tag gray">${T('暂未检测到 PD（需运营商支持）')}</span>`}</span></div>`;
    $('#v6-nd').innerHTML = Object.entries(d2.sysctl || {}).map(([k, v]) =>
      `<div class="kv"><b class="mono">${esc(k)}</b><span class="mono">${esc(v)}</span></div>`).join('') || `<p class="desc">${T('无数据')}</p>`;
    $('#v6-pool').innerHTML = glob.length
      ? `<table><thead><tr><th>${T('接口')}</th><th>${T('地址')}</th><th>${T('前缀长度')}</th><th>${T('备注')}</th></tr></thead><tbody>
         ${glob.map(x => `<tr><td class="mono">${esc(x.if)}</td><td class="mono">${esc(x.a)}</td><td>/${x.p}</td>
         <td><input class="v6prem" data-pfx="${esc(x.a)}/${x.p}" placeholder="${T('例如 电信')} /56" style="width:200px"
           value="${esc(($w('radvd').pool_remarks || {})[x.a + '/' + x.p] || '')}"></td></tr>`).join('')}</tbody></table>`
      : `<p class="desc">${T('暂无可记录的前缀（运营商可能未下发公网 IPv6）。')}</p>`;
    $$('.v6prem').forEach(inp => inp.oninput = () => {
      const rem = Object.assign({}, $w('radvd').pool_remarks || {});
      rem[inp.dataset.pfx] = inp.value;
      S.cfg.radvd = Object.assign($w('radvd'), { pool_remarks: rem });
      setActionMsg(T('前缀备注已修改，请点击保存并应用'));
    });
  });
}

/* ============================ DNS ============================ */
function viewDns() {
  const d = $w('dnsmasq');
  $('#view').innerHTML = `
    <div class="card">
      <h3>DNS ${T('上游来源')}</h3>
      <p class="desc">${T('可继承运营商通过')} PPPoE ${T('下发的')} DNS${T('，也可使用自定义公共')} DNS ${T('或内网')} DNS ${T('服务器。')}</p>
      <div class="seg" id="dns-mode">
        <button data-m="isp" class="${d.dns_mode === 'isp' ? 'on' : ''}">${T('仅运营商下发')}</button>
        <button data-m="custom" class="${(d.dns_mode || 'custom') === 'custom' ? 'on' : ''}">${T('仅自定义')}</button>
        <button data-m="both" class="${d.dns_mode === 'both' ? 'on' : ''}">${T('两者合并')}</button>
      </div>
      <div class="row" style="margin-top:14px">
        <label>${T('自定义公共')} DNS<input id="dn-c" value="${esc(d.dns_custom || '')}" placeholder="223.5.5.5,119.29.29.29"></label>
        <label>${T('内网')} DNS ${T('服务器')}<input id="dn-l" value="${esc(d.dns_lan_server || '')}" placeholder="192.168.7.x${T('（可留空）')}"></label>
      </div>
      <div class="row">
        <label>${T('缓存条数')}<input id="dn-cs" type="number" value="${esc(d.dns_cache_size || 1000)}"></label>
        <label>${T('本地域名')}<input id="dn-dom" value="${esc(d.domain || 'lan')}"></label>
      </div>
      <label class="switch"><input type="checkbox" id="dn-neg" ${d.dns_negcache !== false ? 'checked' : ''}><i></i>${T('缓存否定结果（')}no-negcache ${T('的反向）')}</label>
      <label class="switch"><input type="checkbox" id="dn-log" ${d.dns_query_log ? 'checked' : ''}><i></i>${T('记录')} DNS ${T('查询日志（调试用，较占资源，默认关闭）')}</label>
      <div class="row" style="margin-top:10px">
        <button class="ghost fixed" id="dns-test">${T('测试解析（本机')} dig）</button>
        <button class="ghost fixed" id="dns-cur">${T('查看系统当前')} DNS</button>
      </div>
      <div id="dns-out" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h3>${T('常用公共')} DNS ${T('参考')}</h3>
      <table><thead><tr><th>${T('服务商')}</th><th>IPv4</th><th>IPv6</th></tr></thead><tbody>
        <tr><td>${T('阿里')} AliDNS</td><td class="mono">223.5.5.5 / 223.6.6.6</td><td class="mono">2400:3200::1</td></tr>
        <tr><td>${T('腾讯')} DNSPod</td><td class="mono">119.29.29.29 / 182.254.116.116</td><td class="mono">2402:4e00::</td></tr>
        <tr><td>114 DNS</td><td class="mono">114.114.114.114 / 114.114.115.115</td><td class="mono">—</td></tr>
        <tr><td>Cloudflare</td><td class="mono">1.1.1.1 / 1.0.0.1</td><td class="mono">2606:4700:4700::1111</td></tr>
        <tr><td>Google</td><td class="mono">8.8.8.8 / 8.8.4.4</td><td class="mono">2001:4860:4860::8888</td></tr>
      </tbody></table>
    </div>`;
  const sync = () => {
    S.cfg.dnsmasq = Object.assign($w('dnsmasq'), {
      dns_custom: $('#dn-c').value, dns_lan_server: $('#dn-l').value,
      dns_cache_size: Number($('#dn-cs').value), domain: $('#dn-dom').value,
      dns_negcache: $('#dn-neg').checked, dns_query_log: $('#dn-log').checked,
    });
    setActionMsg(T('已修改，请点击保存并应用'));
  };
  ['#dn-c', '#dn-l', '#dn-cs', '#dn-dom'].forEach(s => { const e = $(s); if (e) e.oninput = sync; });
  ['#dn-neg', '#dn-log'].forEach(s => { const e = $(s); if (e) e.onchange = sync; });
  $$('#dns-mode button').forEach(b => b.onclick = () => {
    $$('#dns-mode button').forEach(x => x.classList.remove('on')); b.classList.add('on');
    S.cfg.dnsmasq = Object.assign($w('dnsmasq'), { dns_mode: b.dataset.m });
    setActionMsg(T('DNS 来源已改为「') + b.textContent + T('」，请点击保存并应用'));
  });
  $('#dns-test').onclick = async () => {
    $('#dns-out').innerHTML = `<pre>${T('解析中…')}</pre>`;
    const r = await api('/api/diag', { method: 'POST', body: { tool: 'dns', target: 'www.baidu.com' } });
    $('#dns-out').innerHTML = `<pre>${esc((r.data || {}).out || r.msg_cn)}</pre>`;
  };
  $('#dns-cur').onclick = async () => {
    const r = await api('/api/diag', { method: 'POST', body: { tool: 'ping', target: '223.5.5.5', count: 1 } });
    $('#dns-out').innerHTML = `<pre>${esc(r.msg_cn)}\n${esc((r.data || {}).out || '')}</pre>`;
  };
}

/* ============================ 端口转发 / DMZ（#7） ============================ */
const PF_PROTO_CN = { tcp: 'TCP', udp: 'UDP', 'tcp/udp': 'TCP+UDP' };

function viewPortfwd() {
  const c = $w('portfwd');
  // 编辑中的草稿只存在 S.pfRules 里，页面重进时原本从 c.rules 重建，
  // 于是「填了一半切页回来」全丢。pageDirty 机制就是干这个的：
  // 标脏之后重进保留草稿，真正保存后才清。
  const rules = (pageIsDirty('portfwd') && S.pfRules && S.pfRules.length
    ? S.pfRules : (c.rules || [])).map(x => Object.assign({}, x));
  S.pfRules = rules.length ? rules : [newPfRule()];
  const hasWan = !!($w('system').wan_iface);

  $('#view').innerHTML = `
    ${hasWan ? '' : `<div class="card" style="border-color:#f0e0a8;background:var(--warn-l)">
      <h3 style="color:var(--warn)">${T('尚未分配 WAN 口')}</h3>
      <p class="desc" style="color:var(--warn);margin:0">${T('端口转发需要先有一个 WAN 口。')}
      ${T('请到「网卡与桥接」把外网网卡角色设为 WAN。配置可先保存，应用时会给出提示。')}</p></div>`}

    <div class="card">
      <h3>${T('端口转发（')}DNAT）</h3>
      <p class="desc">${T('把外网访问的某个端口，转发到内网某台设备的端口。这是最主流、最稳定的做法： 只在')} <span class="mono">prerouting</span> ${T('链做目的地址改写，并自动在')} <span class="mono">forward</span> ${T('链放通， 同时兼容内网回环访问（')}hairpin）。</p>
      <label class="switch"><input type="checkbox" id="pf-en4" ${c.enable_v4 ? 'checked' : ''}><i></i>${T('启用')} IPv4 ${T('端口转发')}</label>
      <label class="switch" style="margin-top:6px"><input type="checkbox" id="pf-en6" ${c.enable_v6 ? 'checked' : ''}><i></i>${T('启用')} IPv6 ${T('端口转发')}</label>
      <p class="hint-inline">IPv6 ${T('是直连路由，通常无需')} NAT${T('；此处的')} IPv6 ${T('转发用于上级做过前缀转换的场景。')}</p>
      <div id="pf-list" style="margin-top:14px"></div>
      <div class="row" style="margin-top:10px">
        <button class="ghost fixed" id="pf-add">+ ${T('新增一条转发')}</button>
        <button class="ghost fixed" id="pf-clear">${T('清空全部')}</button>
      </div>
    </div>

    <div class="card">
      <h3>DMZ ${T('主机')}</h3>
      <p class="desc">DMZ ${T('会把')}<strong>${T('所有')}</strong>${T('外网端口都转发到指定的一台内网主机，等价于「')}1:1 ${T('全端口映射」。 常用于游戏主机、监控')} NVR ${T('等需要大量端口的设备。')}<strong>${T('注意：开启后该主机将完全暴露在公网， 安全性取决于该主机自身的防护，请谨慎使用。')}</strong></p>
      <div class="row">
        <label class="switch" style="flex:0 0 auto"><input type="checkbox" id="pf-dmz4" ${c.dmz_v4_enable ? 'checked' : ''}><i></i>IPv4 DMZ</label>
        <label>DMZ ${T('主机（内网')} IPv4）<input id="pf-dmz4host" value="${esc(c.dmz_v4_host || '')}" placeholder="${T('例如')} 192.168.7.50"></label>
      </div>
      <div class="row">
        <label class="switch" style="flex:0 0 auto"><input type="checkbox" id="pf-dmz6" ${c.dmz_v6_enable ? 'checked' : ''}><i></i>IPv6 DMZ</label>
        <label>DMZ ${T('主机（')}IPv6）<input id="pf-dmz6host" value="${esc(c.dmz_v6_host || '')}" placeholder="${T('例如')} 2408:8207:1234::50"></label>
      </div>
      <p class="hint-inline">DMZ ${T('与上面的端口转发可同时存在；同名端口以端口转发规则优先。')}</p>
    </div>

    <div class="card">
      <h3>${T('规则预览（将会写入防火墙）')}</h3>
      <p class="desc">${T('下面是当前配置渲染出的')} nftables ${T('规则片段，仅显示与端口转发相关的部分。')}</p>
      <button class="ghost small fixed" id="pf-preview">${T('生成预览')}</button>
      <div id="pf-out" style="margin-top:10px"></div>
    </div>`;

  renderPfList();

  const bindSwitch = (id, k) => {
    const e = $(id); if (!e) return;
    e.onchange = () => { S.cfg.portfwd = Object.assign($w('portfwd'), { [k]: e.target.checked }); setActionMsg(T('已修改，请点击保存并应用')); };
  };
  bindSwitch('#pf-en4', 'enable_v4'); bindSwitch('#pf-en6', 'enable_v6');
  bindSwitch('#pf-dmz4', 'dmz_v4_enable'); bindSwitch('#pf-dmz6', 'dmz_v6_enable');
  const bindText = (id, k) => {
    const e = $(id); if (!e) return;
    e.oninput = () => { S.cfg.portfwd = Object.assign($w('portfwd'), { [k]: e.value }); setActionMsg(T('已修改，请点击保存并应用')); };
  };
  bindText('#pf-dmz4host', 'dmz_v4_host'); bindText('#pf-dmz6host', 'dmz_v6_host');

  $('#pf-add').onclick = () => {
    S.pfRules.push(newPfRule());
    renderPfList();
    pageDirty('portfwd');
    setActionMsg(T('已新增一条空白转发规则，请填写后保存'));
  };
  $('#pf-clear').onclick = () => {
    modal(T('清空全部端口转发'), `<p>${T('确定要清空所有端口转发规则吗？此操作需在保存并应用后生效。')}</p>`, () => {
      S.pfRules = [newPfRule()];
      renderPfList();
      S.cfg.portfwd = Object.assign($w('portfwd'), { rules: [] });
      pageDirty('portfwd');
      setActionMsg(T('已清空端口转发规则，请点击保存并应用'));
    });
  };
  $('#pf-preview').onclick = async () => {
    syncPfRules();
    $('#pf-out').innerHTML = `<pre>${T('正在渲染…')}</pre>`;
    const r2 = await api('/api/render', { method: 'POST', body: { module: 'nft_v4' } });
    const txt = (r2.data || {}).text || '';
    if (!r2.ok || !txt) {
      $('#pf-out').innerHTML = `<pre>${esc(r2.msg_cn || T('无法获取预览'))}</pre>`;
      return;
    }
    const lines = txt.split('\n').filter(x => /dnat|DMZ|放通转发|nat_pre|dstnat/.test(x));
    $('#pf-out').innerHTML = `<pre>${esc(lines.length ? lines.join('\n') : T('当前无端口转发规则。'))}</pre>`;
  };
}

function newPfRule() {
  return { name: '', proto: 'tcp', ext_port: '', int_ip: '', int_port: '', enable: true, family: 'v4' };
}

function renderPfList() {
  const el = $('#pf-list'); if (!el) return;
  const rows = S.pfRules || [];
  el.innerHTML = rows.map((r, i) => `
    <div class="pfrow" data-i="${i}" style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;
      padding:10px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px">
      <label style="flex:1 1 130px;margin:0">${T('名称')}
        <input data-f="name" value="${esc(r.name || '')}" placeholder="${T('例如 群晖')} DSM"></label>
      <label style="flex:0 0 110px;margin:0">${T('协议')}
        <select data-f="proto">
          ${['tcp', 'udp', 'tcp/udp'].map(p => `<option value="${esc(p)}" ${r.proto === p ? 'selected' : ''}>${PF_PROTO_CN[p]}</option>`).join('')}
        </select></label>
      <label style="flex:0 0 110px;margin:0">IP ${T('版本')}
        <select data-f="family">
          <option value="v4" ${(r.family || 'v4') === 'v4' ? 'selected' : ''}>IPv4</option>
          <option value="v6" ${r.family === 'v6' ? 'selected' : ''}>IPv6</option>
        </select></label>
      <label style="flex:0 0 110px;margin:0">${T('外网端口')}
        <input data-f="ext_port" type="number" value="${esc(r.ext_port || '')}" placeholder="5000"></label>
      <label style="flex:1 1 160px;margin:0">${T('内网地址')}
        <input data-f="int_ip" value="${esc(r.int_ip || '')}" placeholder="${(r.family === 'v6') ? '2408:...::10' : '192.168.7.10'}"></label>
      <label style="flex:0 0 110px;margin:0">${T('内网端口')}
        <input data-f="int_port" type="number" value="${esc(r.int_port || '')}" placeholder="${T('同外网')}"></label>
      <label class="switch" style="margin:0;flex:0 0 auto"><input type="checkbox" data-f="enable" ${r.enable !== false ? 'checked' : ''}><i></i>${T('启用')}</label>
      <button class="ghost small" data-del="${i}" style="flex:0 0 auto">${T('删除')}</button>
    </div>`).join('') || `<p class="desc">${T('暂无规则，点击下方「新增一条转发」开始添加。')}</p>`;

  $$('#pf-list [data-f]').forEach(e => {
    e.onchange = e.oninput = () => {
      const row = e.closest('.pfrow');
      const i = Number(row.dataset.i);
      const f = e.dataset.f;
      let v = e.type === 'checkbox' ? e.checked : e.value;
      if (f === 'ext_port' || f === 'int_port') v = v === '' ? '' : Number(v);
      S.pfRules[i][f] = v;
      if (f === 'family') renderPfList();
      pageDirty('portfwd');
      setActionMsg(T('已修改，请点击保存并应用'));
    };
  });
  $$('#pf-list [data-del]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.del);
    if (S.pfRules.length <= 1) { S.pfRules = [newPfRule()]; }
    else { S.pfRules.splice(i, 1); }
    renderPfList();
    pageDirty('portfwd');
    setActionMsg(T('已删除一条规则，请点击保存并应用'));
  });
}

function syncPfRules() {
  const clean = (S.pfRules || []).filter(r => String(r.ext_port || '').trim() !== ''
    || String(r.int_ip || '').trim() !== '');
  S.cfg.portfwd = Object.assign($w('portfwd'), { rules: clean });
  return clean;
}

/* ===================== 访问控制 / 家长时间组（#8） ===================== */
const ACL_DAY_CN = () => [T('周日'), T('周一'), T('周二'), T('周三'), T('周四'), T('周五'), T('周六')];
const ACL_ACTION_TAG = { block: 'err', allow: 'ok', limit: 'warn', log: 'gray' };

async function viewAcl() {
  // 有未保存草稿时不要拉服务端数据覆盖 —— 切走再回来，编辑内容原样保留
  if (pageIsDirty('acl') && S.acl) {
    renderAcl();
    pageDirty('acl'); setActionMsg(T('本页有未保存的改动，已为你保留，记得「保存并应用」'), 'warn');
    return;
  }
  $('#view').innerHTML = `<div class="card"><h3>${T('访问控制')} / ${T('家长时间组')}</h3>
    <p class="desc">${T('正在读取配置与联动状态')}…</p></div>`;
  const r = await api('/api/acl');
  if (!r.ok) {
    $('#view').innerHTML = `<div class="card"><h3 style="color:var(--err)">${T('读取失败')}</h3>
      <p class="desc">${esc(r.msg_cn || T('无法读取访问控制配置'))}</p></div>`;
    return;
  }
  const d = r.data || {};
  S.acl = Object.assign({}, d, {
    time_groups: (d.time_groups || []).map(x => Object.assign({ days: [0, 1, 2, 3, 4, 5, 6] }, x)),
    groups: (d.groups || []).map(x => Object.assign({}, x)),
    rules: (d.rules || []).map(x => Object.assign({}, x)),
  });
  pageClean('acl');
  renderAcl();
}

function renderAcl() {
  const d = S.acl || {};
  const tgs = d.time_groups || [];
  const grps = d.groups || [];
  const rules = d.rules || [];

  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('总开关')}
        <span class="tag ${d.enable ? 'ok' : 'gray'}" style="float:right">${d.enable ? T('已启用') : T('已停用')}</span></h3>
      <p class="desc">${T('访问控制基于「设备组')} + ${T('时间组')} + ${T('应用分类」三段式匹配：命中后在转发链上执行阻断')} / ${T('放通')} / ${T('限速')} / ${T('仅记录。 应用分类直接复用')} <b>DPI（nDPI）</b> ${T('分类库，时间规则按')}<b>${T('北京时间（')}UTC+${esc(String(d.tz_offset || 8))}）</b>${T('换算， 跨零点的时间段会自动拆成两段写入内核。')}</p>
      <label class="switch"><input type="checkbox" id="acl-en" ${d.enable ? 'checked' : ''}><i></i>${T('启用访问控制')}</label>
      <div class="row" style="margin-top:8px">
        <span class="tag ${d.dpi_ready ? 'ok' : 'warn'}">DPI ${T('分类库：')}${d.dpi_ready ? T('已就绪') : T('未安装（应用识别不可用）')}</span>
        <span class="tag ${d.qos_enabled ? 'ok' : 'warn'}">QoS：${d.qos_enabled ? T('已启用（限速动作可用）') : T('未启用（限速动作不生效）')}</span>
        <span class="tag gray">${T('时间组')} ${tgs.length} · ${T('设备组')} ${grps.length} · ${T('规则')} ${rules.length}</span>
      </div>
      <p class="hint-inline">${esc(bt4('PAGE_NOTES', d.note, 'd', d.note) || '')}</p>
    </div>

    <div class="card">
      <h3>① ${T('设备组')} <button class="ghost small fixed" id="acl-add-grp" style="float:right">+ ${T('新增设备组')}</button></h3>
      <p class="desc">${T('把需要统一管控的设备归到一组，支持')} IPv4 / IPv6 ${T('混合填写，也支持网段（如')} 192.168.7.0/24）。</p>
      <div id="acl-grps"></div>
    </div>

    <div class="card">
      <h3>② ${T('时间组')} <button class="ghost small fixed" id="acl-add-tg" style="float:right">+ ${T('新增时间组')}</button></h3>
      <p class="desc">${T('常用的作息模板已内置，点击即可套用，再按需微调。')}</p>
      <div id="acl-presets" style="margin-bottom:12px"></div>
      <div id="acl-tgs"></div>
    </div>

    <div class="card">
      <h3>③ ${T('访问规则')} <button class="ghost small fixed" id="acl-add-rule" style="float:right">+ ${T('新增规则')}</button></h3>
      <p class="desc">${T('规则')} = ${T('谁（设备组）')}+ ${T('何时（时间组）')}+ ${T('什么应用（')}DPI ${T('分类）')}→ ${T('怎么做（动作）。规则自上而下匹配，命中即止。')}</p>
      <div id="acl-rules"></div>
    </div>

    <div class="card">
      <h3>${T('规则预览（将会写入内核）')}</h3>
      <p class="desc">${T('下面是当前配置渲染出的')} nftables ${T('规则，可直接核对时间换算与星期映射是否正确。')}</p>
      <button class="ghost small fixed" id="acl-preview">${T('生成预览')}</button>
      <div id="acl-out" style="margin-top:10px"></div>
    </div>`;

  renderAclPresets();
  renderAclGroups();
  renderAclTimeGroups();
  renderAclRules();

  const en = $('#acl-en');
  if (en) en.onchange = () => {
    S.acl.enable = en.target.checked;
    pageDirty('acl'); setActionMsg(T('总开关已修改，请在下方点击「保存并应用」'));
  };
  $('#acl-add-grp').onclick = () => {
    S.acl.groups.push({ id: 'g' + Date.now().toString(36), name: '', hosts: [] });
    renderAclGroups();
    pageDirty('acl'); setActionMsg(T('已新增一个空白设备组，请填写后保存'));
  };
  $('#acl-add-tg').onclick = () => {
    S.acl.time_groups.push({
      id: 't' + Date.now().toString(36), name: '', days: [0, 1, 2, 3, 4, 5, 6],
      start: '00:00', end: '23:59',
    });
    renderAclTimeGroups();
    pageDirty('acl'); setActionMsg(T('已新增一个空白时间组，请填写后保存'));
  };
  $('#acl-add-rule').onclick = () => {
    S.acl.rules.push({
      name: '', enable: true, action: 'block', group: (S.acl.groups[0] || {}).id || '',
      time_group: (S.acl.time_groups[0] || {}).id || '', apps: [],
    });
    renderAclRules();
    pageDirty('acl'); setActionMsg(T('已新增一条空白规则，请填写后保存'));
  };
  $('#acl-preview').onclick = async () => {
    $('#acl-out').innerHTML = `<pre>${T('正在渲染…')}</pre>`;
    const r2 = await api('/api/acl', { method: 'POST', body: { op: 'plan', enable: S.acl.enable,
      time_groups: S.acl.time_groups, groups: S.acl.groups, rules: S.acl.rules } });
    if (!r2.ok) {
      $('#acl-out').innerHTML = `<pre>${esc(r2.msg_cn || T('渲染失败'))}</pre>`;
      return;
    }
    const dd = r2.data || {};
    $('#acl-out').innerHTML =
      `<div class="kv"><b>${T('统计')}</b><span>${T('启用规则')} ${dd.rules} ${T('条')} · ${T('时间组')} ${dd.time_groups} ${T('个')} · ${T('设备组')} ${dd.groups} ${T('个')}</span></div>
       <pre>${esc(dd.text || '')}</pre>`;
  };
}

function renderAclPresets() {
  const el = $('#acl-presets'); if (!el) return;
  const presets = (S.acl || {}).time_presets || [];
  el.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:8px">${
    presets.map((p, i) => `<button class="ghost small" data-preset="${i}"
      title="${esc(bt4('ACL_TIME_PRESETS', p.id, 'why', p.why))}">${esc(bt4('ACL_TIME_PRESETS', p.id, 'name', p.name))}</button>`).join('')}</div>
    <p class="hint-inline">${T('点击模板会新建一个时间组；已存在同名则跳过。')}</p>`;
  $$('#acl-presets [data-preset]').forEach(b => b.onclick = () => {
    const p = ((S.acl || {}).time_presets || [])[Number(b.dataset.preset)];
    if (!p) return;
    if ((S.acl.time_groups || []).some(x => x.name === p.name)) {
      toast(T('已存在同名时间组「') + p.name + '」', 'warn'); return;
    }
    S.acl.time_groups.push({
      id: 't' + Date.now().toString(36), name: p.name,
      days: (p.days || [0, 1, 2, 3, 4, 5, 6]).slice(),
      start: p.start || '00:00', end: p.end || '23:59',
    });
    renderAclTimeGroups();
    pageDirty('acl'); setActionMsg(T('已套用模板「') + p.name + T('」，请点击保存并应用'));
  });
}

function renderAclGroups() {
  const el = $('#acl-grps'); if (!el) return;
  const rows = (S.acl || {}).groups || [];
  const templates = (S.acl || {}).group_templates || [];
  el.innerHTML = rows.map((g, i) => `
    <div class="aclrow" data-i="${i}" style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;
      padding:10px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px">
      <label style="flex:1 1 180px;margin:0">${T('名称')}
        <input data-f="name" value="${esc(g.name || '')}" placeholder="${T('例如 孩子的设备')}"></label>
      <label style="flex:2 1 320px;margin:0">${T('地址列表（逗号或空格分隔）')}
        <input data-f="hosts" value="${esc((g.hosts || []).join(', '))}"
          placeholder="192.168.7.20, 192.168.7.21, 2408:8207:1234::20"></label>
      <button class="ghost small" data-tpl="${i}" style="flex:0 0 auto"
        title="${T('套用预设名称')}">${T('模板')}</button>
      <button class="ghost small" data-del="${i}" style="flex:0 0 auto">${T('删除')}</button>
    </div>`).join('') || `<p class="desc">${T('暂无设备组，点击右上角「新增设备组」开始。')}</p>`;

  $$('#acl-grps [data-f]').forEach(e => {
    e.onchange = e.oninput = () => {
      const i = Number(e.closest('.aclrow').dataset.i);
      const f = e.dataset.f;
      if (f === 'hosts') {
        S.acl.groups[i].hosts = e.value.split(/[\s,;]+/).map(x => x.trim()).filter(Boolean);
      } else {
        S.acl.groups[i][f] = e.value;
      }
      pageDirty('acl'); setActionMsg(T('已修改，请点击保存并应用'));
    };
  });
  $$('#acl-grps [data-del]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.del);
    const gid = (S.acl.groups[i] || {}).id;
    S.acl.groups.splice(i, 1);
    // 同步清理引用了该组的规则
    (S.acl.rules || []).forEach(r => { if (r.group === gid) r.group = ''; });
    renderAclGroups(); renderAclRules();
    pageDirty('acl'); setActionMsg(T('已删除设备组，请点击保存并应用'));
  });
  $$('#acl-grps [data-tpl]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.tpl);
    if (!templates.length) { toast(T('没有可用的预设模板'), 'warn'); return; }
    modal(T('套用设备组模板'), '<div style="display:flex;flex-direction:column;gap:8px">'
      + templates.map(t => `<button class="ghost" data-pick="${t.id}">${esc(bt4('ACL_GROUP_TEMPLATES', t.id, 'name', t.name))}<br>
          <span style="font-size:12px;color:var(--txt3)">${esc(bt4('ACL_GROUP_TEMPLATES', t.id, 'why', t.why))}</span></button>`).join('')
      + '</div>');
    $$('#modal [data-pick]').forEach(pb => pb.onclick = () => {
      const t = templates.find(x => x.id === pb.dataset.pick);
      if (t) { S.acl.groups[i].name = t.name; }
      $('#modal').classList.add('hidden');
      renderAclGroups();
      pageDirty('acl'); setActionMsg(T('已套用模板名称，请补充地址后保存'));
    });
  });
}

function renderAclTimeGroups() {
  const el = $('#acl-tgs'); if (!el) return;
  const rows = (S.acl || {}).time_groups || [];
  el.innerHTML = rows.map((t, i) => `
    <div class="aclrow" data-i="${i}" style="padding:10px;border:1px solid var(--line);
      border-radius:8px;margin-bottom:8px">
      <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
        <label style="flex:1 1 180px;margin:0">${T('名称')}
          <input data-f="name" value="${esc(t.name || '')}" placeholder="${T('例如 上学日')}"></label>
        <label style="flex:0 0 130px;margin:0">${T('开始（本地）')}
          <input data-f="start" type="time" value="${esc(t.start || '00:00')}"></label>
        <label style="flex:0 0 130px;margin:0">${T('结束（本地）')}
          <input data-f="end" type="time" value="${esc(t.end || '23:59')}"></label>
        <button class="ghost small" data-del="${i}" style="flex:0 0 auto">${T('删除')}</button>
      </div>
      <div style="margin-top:8px">
        <span style="font-size:12px;color:var(--txt3);margin-right:8px">${T('生效星期：')}</span>
        ${ACL_DAY_CN().map((n, di) => `<label class="switch" style="margin:0 10px 0 0;display:inline-flex">
          <input type="checkbox" data-day="${di}" ${(t.days || []).includes(di) ? 'checked' : ''}><i></i>${n}</label>`).join('')}
      </div>
      <div class="hint-inline" style="margin-top:6px">${
        aclCrossHint(t)}</div>
    </div>`).join('') || `<p class="desc">${T('暂无时间组，点击右上角「新增时间组」或直接点上面的模板。')}</p>`;

  $$('#acl-tgs [data-f]').forEach(e => {
    e.onchange = e.oninput = () => {
      const i = Number(e.closest('.aclrow').dataset.i);
      S.acl.time_groups[i][e.dataset.f] = e.value;
      if (e.dataset.f === 'start' || e.dataset.f === 'end') {
        const row = e.closest('.aclrow');
        const hint = row.querySelector('.hint-inline');
        if (hint) hint.textContent = aclCrossHint(S.acl.time_groups[i]);
      }
      pageDirty('acl'); setActionMsg(T('已修改，请点击保存并应用'));
    };
  });
  $$('#acl-tgs [data-day]').forEach(e => e.onchange = () => {
    const i = Number(e.closest('.aclrow').dataset.i);
    const di = Number(e.dataset.day);
    const t = S.acl.time_groups[i];
    const set = new Set(t.days || []);
    if (e.target.checked) set.add(di); else set.delete(di);
    t.days = Array.from(set).sort();
    pageDirty('acl'); setActionMsg(T('已修改，请点击保存并应用'));
  });
  $$('#acl-tgs [data-del]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.del);
    const tid = (S.acl.time_groups[i] || {}).id;
    S.acl.time_groups.splice(i, 1);
    (S.acl.rules || []).forEach(r => { if (r.time_group === tid) r.time_group = ''; });
    renderAclTimeGroups(); renderAclRules();
    pageDirty('acl'); setActionMsg(T('已删除时间组，请点击保存并应用'));
  });
}

function aclCrossHint(t) {
  const s = (t.start || '00:00'), e = (t.end || '23:59');
  const toMin = x => { const p = String(x).split(':'); return (Number(p[0]) || 0) * 60 + (Number(p[1]) || 0); };
  const days = (t.days || []).length;
  if (!days) return T('⚠ 未选择任何星期，该时间组不会命中任何流量。');
  if (toMin(s) > toMin(e)) {
    return T('ℹ 本地时间跨零点（') + esc(s) + T(' 次日 ') + esc(e) + T('），会自动拆成两段；注意「生效星期」指的是<b>起始那天</b>。');
  }
  if (toMin(s) < 8 * 60 && toMin(e) >= 8 * 60) {
    return T('ℹ 该时段横跨本地 08:00（UTC 日分界），会自动拆成两段写入内核。');
  }
  return '';
}

function renderAclRules() {
  const el = $('#acl-rules'); if (!el) return;
  const rows = (S.acl || {}).rules || [];
  const tgs = (S.acl || {}).time_groups || [];
  const grps = (S.acl || {}).groups || [];
  const apps = (S.acl || {}).app_groups || [];
  const acts = (S.acl || {}).actions || [];

  el.innerHTML = rows.map((r, i) => `
    <div class="aclrow" data-i="${i}" style="padding:10px;border:1px solid var(--line);
      border-left:4px solid var(--${ACL_ACTION_TAG[r.action] === 'err' ? 'err' : (ACL_ACTION_TAG[r.action] === 'ok' ? 'ok' : 'warn')});
      border-radius:8px;margin-bottom:8px">
      <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
        <label style="flex:1 1 160px;margin:0">${T('规则名称')}
          <input data-f="name" value="${esc(r.name || '')}" placeholder="${T('例如 夜间断娱乐')}"></label>
        <label style="flex:0 0 150px;margin:0">${T('设备组')}
          <select data-f="group">
            <option value="">${T('全部设备')}</option>
            ${grps.map(g => `<option value="${esc(g.id)}" ${r.group === g.id ? 'selected' : ''}>${esc(g.name || g.id)}</option>`).join('')}
          </select></label>
        <label style="flex:0 0 160px;margin:0">${T('时间组')}
          <select data-f="time_group">
            <option value="">${T('始终生效')}</option>
            ${tgs.map(t => `<option value="${esc(t.id)}" ${r.time_group === t.id ? 'selected' : ''}>${esc(t.name || t.id)}</option>`).join('')}
          </select></label>
        <label style="flex:0 0 150px;margin:0">${T('动作')}
          <select data-f="action">
            ${acts.map(a => `<option value="${esc(a.v)}" ${r.action === a.v ? 'selected' : ''}>${esc(bt4('ACL_ACTIONS', a.v, 'n', a.n))}</option>`).join('')}
          </select></label>
        <label class="switch" style="margin:0;flex:0 0 auto"><input type="checkbox" data-f="enable" ${r.enable !== false ? 'checked' : ''}><i></i>${T('启用')}</label>
        <button class="ghost small" data-del="${i}" style="flex:0 0 auto">${T('删除')}</button>
      </div>
      <div style="margin-top:8px">
        <span style="font-size:12px;color:var(--txt3);margin-right:8px">${T('应用范围（')}DPI ${T('分类，可多选）：')}</span>
        ${apps.map(a => `<label class="switch" style="margin:0 10px 0 0;display:inline-flex">
          <input type="checkbox" data-app="${esc(a.id)}" ${(r.apps || []).includes(a.id) ? 'checked' : ''}><i></i>${esc(bt4('ACL_APP_GROUPS', a.id, 'name', a.name))}</label>`).join('')}
      </div>
      ${(r.apps || []).length ? '' : `<div class="hint-inline" style="margin-top:6px">${T('未选择应用分类 → 该规则匹配')}<b>${T('该设备组的全部流量')}</b>。</div>`}
    </div>`).join('') || `<p class="desc">${T('暂无规则，点击右上角「新增规则」开始。')}</p>`;

  $$('#acl-rules [data-f]').forEach(e => {
    e.onchange = e.oninput = () => {
      const i = Number(e.closest('.aclrow').dataset.i);
      const f = e.dataset.f;
      S.acl.rules[i][f] = (e.type === 'checkbox') ? e.checked : e.value;
      if (f === 'action') renderAclRules();
      pageDirty('acl'); setActionMsg(T('已修改，请点击保存并应用'));
    };
  });
  $$('#acl-rules [data-app]').forEach(e => e.onchange = () => {
    const i = Number(e.closest('.aclrow').dataset.i);
    const r = S.acl.rules[i];
    const set = new Set(r.apps || []);
    if (e.target.checked) set.add(e.dataset.app); else set.delete(e.dataset.app);
    r.apps = Array.from(set);
    pageDirty('acl'); setActionMsg(T('已修改，请点击保存并应用'));
  });
  $$('#acl-rules [data-del]').forEach(b => b.onclick = () => {
    S.acl.rules.splice(Number(b.dataset.del), 1);
    renderAclRules();
    pageDirty('acl'); setActionMsg(T('已删除规则，请点击保存并应用'));
  });
}

/* ================== 内网文件共享 SMB / NFS（#9） ================== */
function sb() {
  S.share = S.share || {};
  S.share.samba = S.share.samba || { shares: [] };
  S.share.nfs = S.share.nfs || { exports: [] };
  S.share.samba.shares = S.share.samba.shares || [];
  S.share.nfs.exports = S.share.nfs.exports || [];
  return S.share;
}

// server_string 是后端铸的默认值（「drouter 文件共享」），显示时跟随界面语言；
// 用户改过的自定义值原样显示。保存以输入框实际值为准。
function shSrvStr(c) {
  const v = (c && c.server_string) || '';
  // 哨兵串必须用后端默认值原文字面量（\u 转义写法仅为避开 i18n 字面量扫描）；
  // 不能用 t() 包，否则英文模式下比对失败、本地化回落失效。
  return v === 'drouter \u6587\u4ef6\u5171\u4eab' ? 'drouter ' + T('文件共享') : v;
}
async function viewNfs() {
  // 有未保存草稿时保留本地编辑，不用服务端数据覆盖
  if (pageIsDirty('nfs') && S.share) {
    renderShare();
    pageDirty('nfs'); setActionMsg(T('本页有未保存的改动，已为你保留，记得「保存并应用」'), 'warn');
    return;
  }
  $('#view').innerHTML = `<div class="card"><h3>${T('内网文件共享')}</h3>
    <p class="desc">${T('正在读取共享配置与依赖状态')}…</p></div>`;
  const r = await api('/api/share');
  if (!r.ok) {
    $('#view').innerHTML = `<div class="card"><h3 style="color:var(--err)">${T('读取失败')}</h3>
      <p class="desc">${esc(r.msg_cn || T('无法读取文件共享配置'))}</p></div>`;
    return;
  }
  S.shareData = r.data || {};
  S.share = (r.data || {}).cfg || {};
  sb();
  pageClean('nfs');
  renderShare();
}

function renderShare() {
  const D = S.shareData || {};
  const c = sb();
  const pk = D.pkgs || {};
  const svc = D.services || {};
  const dirs = D.dirs || {};
  const hints = D.hints || {};
  const smbOn = !!c.enable_smb, nfsOn = !!c.enable_nfs;
  const smCfg = c.samba || {}, nfCfg = c.nfs || {};

  const smbSvc = ['smbd', 'nmbd'].map(n => `${n}:${(svc[n] || {}).active || '?'}`).join(' · ');
  const nfsSvc = ['nfs-server', 'rpcbind'].map(n => `${n}:${(svc[n] || {}).active || '?'}`).join(' · ');

  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('内网文件共享')}
        <span class="tag ${smbOn ? 'ok' : 'gray'}" style="float:right">SMB ${smbOn ? T('已启用') : T('未启用')}</span>
        <span class="tag ${nfsOn ? 'ok' : 'gray'}" style="float:right;margin-right:6px">NFS ${nfsOn ? T('已启用') : T('未启用')}</span></h3>
      <p class="desc">${esc(bt4('PAGE_NOTES', D.note, 'd', D.note) || '')}</p>
      <div class="grid2" style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px">
        <div class="kv"><b>Samba ${T('软件包')}</b><span>
          ${pk.samba && pk.samba.installed ? `<span class="tag ok">${T('已安装')}</span>` : `<span class="tag warn">${T('未安装')}</span>`}
          <span class="mono" style="font-size:12px">${esc(((pk.samba || {}).pkgs || []).join(' '))}</span></span></div>
        <div class="kv"><b>NFS ${T('软件包')}</b><span>
          ${pk.nfs && pk.nfs.installed ? `<span class="tag ok">${T('已安装')}</span>` : `<span class="tag warn">${T('未安装')}</span>`}
          <span class="mono" style="font-size:12px">${esc(((pk.nfs || {}).pkgs || []).join(' '))}</span></span></div>
        <div class="kv"><b>SMB ${T('服务')}</b><span class="mono">${esc(smbSvc)}</span></div>
        <div class="kv"><b>NFS ${T('服务')}</b><span class="mono">${esc(nfsSvc)}</span></div>
        <div class="kv"><b>${T('本机')} LAN ${T('地址')}</b><span class="mono">${esc((D.host || {}).lan_ip || '—')}</span></div>
        <div class="kv"><b>${T('主配置')} include</b><span>${D.smb_configured ? `<span class="tag ok">${T('已写入')}</span>` : `<span class="tag gray">${T('未写入')}</span>`}</span></div>
      </div>
      ${(!pk.samba || !pk.samba.installed) || (!pk.nfs || !pk.nfs.installed) ? `
        <p class="hint-inline" style="color:var(--warn)">${T('尚未安装的软件包可用下面的命令安装（Debian 13 官方源）：')}
        ${!pk.samba || !pk.samba.installed ? `<br><span class="mono">${esc(pk.install_cmd_smb || '')}</span>` : ''}
        ${!pk.nfs || !pk.nfs.installed ? `<br><span class="mono">${esc(pk.install_cmd_nfs || '')}</span>` : ''}</p>` : ''}
    </div>

    <div class="card" id="stg-card">
      <h3>${T('外置存储设备（')}U ${T('盘')} · ${T('移动硬盘')} · Type-C · ${T('雷电）')}
        <button class="ghost small" id="stg-refresh" style="float:right">${T('刷新')}</button></h3>
      <p class="desc">${T('插上的外置盘会列在下面。可直接挂载、格式化、设为开机自动挂载， 并把挂载点一键加入上面的')} SMB / NFS ${T('共享。')}
        <b>${T('「连接方式」显示的是总线类型')}</b>：Type-C ${T('只是插头形状，电气上仍归')} USB ${T('或雷电， 所以按真实总线展示更准确。')}</p>
      <div id="stg-list"><p class="desc">${T('正在读取块设备')}…</p></div>
    </div>

    <div class="card">
      <h3>SMB / CIFS${T('（推荐：')}Windows · macOS · Linux ${T('通吃）')}</h3>
      <p class="desc">${T('面向全平台，兼容性最好。')}<b>Windows ${T('双击就能用')}</b>${T('，macOS 与 Linux')} ${T('也原生支持。 配置会写入')} <span class="mono">/etc/samba/drouter.conf</span>${T('，并在主配置中')} include ${T('引入。')}</p>
      <label class="switch"><input type="checkbox" id="sh-smb" ${smbOn ? 'checked' : ''}><i></i>${T('启用')} SMB ${T('文件共享')}</label>
      <div class="row" style="margin-top:10px">
        <label>${T('工作组')}<input id="sh-wg" value="${esc(smCfg.workgroup || 'WORKGROUP')}" placeholder="WORKGROUP"></label>
        <label>${T('服务器描述')}<input id="sh-ss" value="${esc(shSrvStr(smCfg))}" placeholder="drouter ${T('文件共享')}"></label>
        <label>${T('监听网卡')}<input id="sh-if" value="${esc(smCfg.interfaces || '')}" placeholder="${T('留空')}=${T('全部（如')} br0 ens18）"></label>
      </div>
      <div class="row" style="margin-top:4px">
        <label class="switch" style="margin:0"><input type="checkbox" id="sh-nb" ${smCfg.disable_netbios ? 'checked' : ''}><i></i>${T('关闭')} NetBIOS${T('（推荐）')}</label>
        <label class="switch" style="margin:0"><input type="checkbox" id="sh-wins" ${smCfg.wins_enable ? 'checked' : ''}><i></i>${T('本机充当')} WINS ${T('服务器')}</label>
      </div>
      <p class="hint-inline">${T('关闭')} NetBIOS ${T('可减少局域网广播；若局域网里有老旧设备（')}XP / ${T('老打印机）找不到共享，可尝试取消勾选。')}</p>
      <h4 style="margin:16px 0 8px">${T('共享目录')}</h4>
      <div id="sh-shares"></div>
      <div class="row" style="margin-top:10px">
        <button class="ghost small fixed" id="sh-add-share">+ ${T('新增共享')}</button>
        <button class="ghost small fixed" id="sh-tpl-share">${T('套用目录模板')}</button>
      </div>
    </div>

    <div class="card">
      <h3>NFS${T('（推荐：')}Linux / macOS ${T('之间，性能更高）')}</h3>
      <p class="desc">NFS ${T('是 Linux')} ${T('原生协议，局域网内传输性能通常优于')} SMB${T('。 导出规则写入')} <span class="mono">/etc/exports</span>${T('，服务参数写入')} <span class="mono">/etc/nfs.conf.d/drouter.conf</span>${T('， 端口已固定，便于防火墙精确放通。')}</p>
      <label class="switch"><input type="checkbox" id="sh-nfs" ${nfsOn ? 'checked' : ''}><i></i>${T('启用')} NFS ${T('文件共享')}</label>
      <div class="row" style="margin-top:10px">
        <label>${T('服务线程数')}<input id="sh-th" type="number" value="${esc(nfCfg.threads || 8)}" min="1" max="128"></label>
      </div>
      <p class="hint-inline">${T('线程数决定并发处理能力，家用')} 4–16 ${T('足够；调大反而会占用内存。')}</p>
      <h4 style="margin:16px 0 8px">${T('导出目录')}</h4>
      <div id="sh-exports"></div>
      <button class="ghost small fixed" id="sh-add-exp" style="margin-top:10px">+ ${T('新增导出')}</button>
    </div>

    <div class="card">
      <h3>${T('什么都不用改：三平台连接方式')}</h3>
      <p class="desc">${T('下面的命令可以直接复制使用，把')} <span class="mono">${esc((D.host || {}).lan_ip || '192.168.7.1')}</span>
      ${T('换成你的路由器地址即可。')}</p>
      <div id="sh-hints"></div>
    </div>

    <div class="card">
      <h3>${T('配置预览（将会写入磁盘）')}</h3>
      <p class="desc">${T('核对生成的')} SMB / NFS ${T('配置是否符合预期。')}</p>
      <button class="ghost small fixed" id="sh-preview">${T('生成预览')}</button>
      <div id="sh-out" style="margin-top:10px"></div>
    </div>`;

  renderShareList();
  renderExportList();
  renderShareHints();
  stgLoad();   // 外置存储设备（挂载 / 格式化后可直接共享）
  // 目录存在性提示
  const miss = Object.values(dirs).filter(x => !x.exists);
  if (miss.length) {
    const box = document.createElement('div');
    box.className = 'hint-inline';
    box.style.color = 'var(--warn)';
    box.innerHTML = T('⚠ 以下共享目录还不存在：') + miss.map(x =>
      `<span class="mono">${esc(x.path)}</span>`).join('、') +
      T('。可在下方共享项里点「创建」按钮一键建立。');
    const first = $('#view').querySelector('.card');
    if (first) first.appendChild(box);
  }

  const sw = (id, k, sub) => {
    const e = $(id); if (!e) return;
    e.onchange = () => {
      const t = sub ? sb()[sub] : sb();
      t[k] = e.target.checked;
      pageDirty('nfs'); setActionMsg(T('已修改，请点击保存并应用'));
    };
  };
  sw('#sh-smb', 'enable_smb'); sw('#sh-nfs', 'enable_nfs');
  sw('#sh-nb', 'disable_netbios', 'samba'); sw('#sh-wins', 'wins_enable', 'samba');
  const tx = (id, k, sub) => {
    const e = $(id); if (!e) return;
    e.oninput = () => {
      const t = sub ? sb()[sub] : sb();
      t[k] = e.type === 'number' ? Number(e.value) : e.value;
      pageDirty('nfs'); setActionMsg(T('已修改，请点击保存并应用'));
    };
  };
  tx('#sh-wg', 'workgroup', 'samba'); tx('#sh-ss', 'server_string', 'samba');
  tx('#sh-if', 'interfaces', 'samba'); tx('#sh-th', 'threads', 'nfs');

  $('#sh-add-share').onclick = () => {
    sb().samba.shares.push(newShare());
    renderShareList();
    pageDirty('nfs'); setActionMsg(T('已新增一个共享，请填写后保存'));
  };
  $('#sh-add-exp').onclick = () => {
    sb().nfs.exports.push(newExport());
    renderExportList();
    pageDirty('nfs'); setActionMsg(T('已新增一个 NFS 导出，请填写后保存'));
  };
  $('#sh-tpl-share').onclick = () => {
    const tpls = D.dir_templates || [];
    modal(T('套用目录模板'), '<div style="display:flex;flex-direction:column;gap:8px">'
      + tpls.map((t, i) => `<button class="ghost" data-dt="${i}">${bt4('SHARE_DIR_TEMPLATES', t.id, 'name', t.name)}<br>
          <span style="font-size:12px;color:var(--txt3)">${esc(t.path)} ｜ ${bt4('SHARE_DIR_TEMPLATES', t.id, 'note', t.note)}</span></button>`).join('')
      + '</div>');
    $$('#modal [data-dt]').forEach(b => b.onclick = () => {
      const t = tpls[Number(b.dataset.dt)];
      if (t) {
        const s = newShare();
        s.name = t.name.split('（')[0];
        s.path = t.path;
        s.comment = t.name;
        if (t.id === 'public') { s.mode = 'public'; s.writable = true; }
        else if (t.id === 'media') { s.mode = 'readonly'; s.writable = false; }
        else { s.mode = 'auth'; s.writable = true; }
        sb().samba.shares.push(s);
      }
      $('#modal').classList.add('hidden');
      renderShareList();
      pageDirty('nfs'); setActionMsg(T('已套用模板，请补充地址后保存'));
    });
  };
  $('#sh-preview').onclick = async () => {
    $('#sh-out').innerHTML = `<pre>${T('正在渲染…')}</pre>`;
    const r2 = await api('/api/share', { method: 'POST', body: { op: 'plan', ...sb() } });
    if (!r2.ok) { $('#sh-out').innerHTML = `<pre>${esc(r2.msg_cn || T('渲染失败'))}</pre>`; return; }
    $('#sh-out').innerHTML = `<div class="kv"><b>${T('涉及文件')}</b><span class="mono">${
      esc(((r2.data || {}).files || []).join('、'))}</span></div>
      <pre>${esc((r2.data || {}).text || '')}</pre>`;
  };
}

/* ============================ 外置存储设备 ============================
   U 盘 / 移动硬盘 / Type-C / 雷电硬盘盒：识别 → 格式化 → 挂载 → 一键共享。
   数据来自 /api/storage（后端 lsblk + sysfs 总线判定）。 */
let STG = null;

async function stgLoad() {
  const box = $('#stg-list');
  if (!box) return;
  box.innerHTML = `<p class="desc">${T('正在读取块设备…')}</p>`;
  const r = await api('/api/storage', { method: 'POST', body: { op: 'get' } });
  if (!r.ok) {
    box.innerHTML = `<p class="desc" style="color:var(--err)">${esc(r.msg_cn || T('读取失败'))}</p>`;
    return;
  }
  STG = r.data || {};
  stgRender();
}

function stgRender() {
  const box = $('#stg-list');
  if (!box) return;
  const devs = (STG && STG.devices) || [];
  if (!devs.length) {
    box.innerHTML = `<p class="desc">${T('没有检测到块设备。虚拟机里没有直通硬盘时属于正常现象。')}</p>`;
    return;
  }
  const fstab = (STG && STG.fstab) || '';
  const row = d => {
    const auto = d.uuid && fstab.indexOf('UUID=' + d.uuid) >= 0;
    const plat = f => ['win', 'mac', 'linux'].filter(k => f[k])
      .map(k => ({ win: 'Win', mac: 'macOS', linux: 'Linux' }[k])).join(' · ');
    const meta = ((STG && STG.fs_types) || []).find(f => f.v === d.fstype);
    return `<tr>
      <td><span class="mono">${esc(d.dev)}</span>
        <div style="font-size:12px;color:var(--txt3)">${esc(bt4('STORAGE_TYPE', d.type_cn, 'en', d.type_cn))}${
        d.model ? ' · ' + esc(d.model) : ''}</div></td>
      <td>${esc(d.size_h)}</td>
      <td><span class="tag ${d.external ? 'info' : 'gray'}">${esc(bt4('STORAGE_BUS', d.bus_cn, 'en', d.bus_cn))}</span>${
        d.external ? '' : `<div style="font-size:12px;color:var(--txt3)">${T('内置')}</div>`}</td>
      <td>${d.fstype ? `<span class="mono">${esc(d.fstype)}</span>${
        d.label ? `<div style="font-size:12px;color:var(--txt3)">${T('卷标')} ` + esc(d.label) + '</div>' : ''}`
        : `<span style="color:var(--txt3)">${T('未格式化')}</span>`}</td>
      <td>${d.mounted ? `<span class="mono">${esc(d.mountpoint)}</span>`
        : `<span style="color:var(--txt3)">${T('未挂载')}</span>`}${
        auto ? `<div><span class="tag ok">${T('开机自动挂载')}</span></div>` : ''}</td>
      <td style="white-space:nowrap">
        ${d.mounted
          ? `<button class="ghost small" data-stg="umount" data-n="${esc(d.name)}">${T('卸载')}</button>
             <button class="ghost small" data-stg="share" data-n="${esc(d.name)}">${T('共享此目录')}</button>`
          : (d.fstype
            ? `<button class="primary small" data-stg="mount" data-n="${esc(d.name)}">${T('挂载')}</button>`
            : `<span style="font-size:12px;color:var(--txt3)">${T('需先格式化')}</span>`)}
        <button class="ghost small" data-stg="format" data-n="${esc(d.name)}"
          ${d.mounted ? T('disabled title="请先卸载再格式化"') : ''}>${T('格式化')}</button>
        ${d.uuid ? `<button class="ghost small" data-stg="auto" data-n="${esc(d.name)}"
          data-auto="${auto ? '0' : '1'}">${auto ? T('取消开机挂载') : T('开机挂载')}</button>` : ''}
      </td></tr>`;
  };
  // .tw 是项目统一的「表格横向滚动」容器（手机适配检查会认它），别换成自定义 class
  const tbl = list => `<div class="tw"><table>
    <thead><tr><th>${T('设备')}</th><th>${T('容量')}</th><th>${T('连接方式')}</th><th>${T('文件系统')}</th>
    <th>${T('挂载点')}</th><th>${T('操作')}</th></tr></thead>
    <tbody>${list.map(row).join('')}</tbody></table></div>`;
  const ext = devs.filter(d => d.external);
  const inner = devs.filter(d => !d.external);
  const miss = ((STG && STG.fs_state) || {});
  const unready = Object.keys(miss).filter(k => !miss[k].ready);
  box.innerHTML = `
    ${ext.length ? `<h4 style="margin:0 0 6px">${T('外置设备（')}${ext.length}）</h4>${tbl(ext)}`
      : `<p class="desc">${T('未检测到外置设备。插上 U 盘或移动硬盘后点「刷新」。')}</p>`}
    ${inner.length ? `<details style="margin-top:10px"><summary style="cursor:pointer">
      ${T('内置磁盘（')}${inner.length}）</summary>${tbl(inner)}</details>` : ''}
    ${unready.length ? `<p class="hint-inline" style="color:var(--warn);margin-top:8px">
      ${T('以下文件系统缺少格式化工具：')}${unready.map(k =>
        `<span class="mono">${esc(k)}</span>`).join('、')}
      —— ${T('可到「系统')} · ${T('依赖自检与安装」页一键安装。')}</p>` : ''}
    <p class="hint-inline" style="margin-top:8px">${esc(bt4('PAGE_NOTES', (STG && STG.note) || '', 'd', (STG && STG.note) || '') || '')}</p>`;
  stgBind();
}

function stgBind() {
  $$('#stg-list [data-stg]').forEach(b => {
    if (b.disabled) return;
    b.onclick = () => {
      const name = b.dataset.n;
      const d = ((STG && STG.devices) || []).find(x => x.name === name);
      if (!d) return;
      const act = b.dataset.stg;
      if (act === 'mount') return stgMount(d);
      if (act === 'umount') return stgUmount(d);
      if (act === 'format') return stgFormat(d);
      if (act === 'auto') return stgAuto(d, b.dataset.auto === '1');
      if (act === 'share') return stgShare(d);
    };
  });
  const rf = $('#stg-refresh');
  if (rf && !rf._bound) { rf._bound = 1; rf.onclick = stgLoad; }
}

async function stgOp(body, okMsg) {
  const r = await api('/api/storage', { method: 'POST', body });
  if (!r.ok) { toast(r.msg_cn || T('操作失败'), 'err', 6000); return null; }
  toast(okMsg || (r.msg_cn || T('完成')), 'ok', 4000);
  await stgLoad();
  return r.data || {};
}

async function stgMount(d) {
  await stgOp({ op: 'mount', name: d.name }, T('已挂载 /dev/') + d.name);
}

async function stgUmount(d) {
  modal(T('卸载 /dev/') + d.name,
    `<p class="desc">${T('确定要卸载挂载在')} <span class="mono">${esc(d.mountpoint)}</span> ${T('的设备吗？ 正在读写该目录的程序会中断。')}</p>`,
    async () => { await stgOp({ op: 'umount', name: d.name }); });
}

async function stgAuto(d, on) {
  if (on) {
    await stgOp({ op: 'fstab_add', name: d.name, target: d.mountpoint || '' });
  } else {
    modal(T('取消开机自动挂载'),
      `<p class="desc">${T('将从')} <span class="mono">/etc/fstab</span> ${T('移除该设备的条目。 原文件已备份在')} <span class="mono">/etc/drouter/generated/fstab.drouter.bak</span>。</p>`,
      async () => { await stgOp({ op: 'fstab_del', uuid: d.uuid }); });
  }
}

/* 格式化：三重保护中的「二次确认」在前端这一层 —— 必须手输设备名才能提交，
   后端还会再校验一次（系统盘 / 已挂载 / 整盘含分区 都会被拒）。 */
function stgFormat(d) {
  const types = (STG && STG.fs_types) || [];
  const state = (STG && STG.fs_state) || {};
  const opts = types.map(f => {
    const st = state[f.v] || {};
    return `<option value="${esc(f.v)}" ${st.ready ? '' : 'disabled'}>${
      esc(f.n)}${st.ready ? '' : T('（缺工具，需安装 ') + esc(f.pkg) + '）'}</option>`;
  }).join('');
  modal(T('格式化 /dev/') + d.name,
    `<p class="desc" style="color:var(--err)">
      ⚠ ${T('格式化会清除')} <span class="mono">${esc(d.dev)}</span>（${esc(d.size_h)}${T('）上的')}
      <b>${T('全部数据，且无法恢复')}</b>。</p>
    <div class="row">
      <label>${T('文件系统')}<select id="stg-fs">${opts}</select></label>
      <label>${T('卷标（可留空）')}<input id="stg-label" maxlength="16" placeholder="${T('例如')} backup"></label>
    </div>
    <p class="desc" id="stg-fs-note"></p>
    <p class="desc" style="margin-top:10px">${T('确认请输入设备名')}
      <span class="mono" style="color:var(--err)">${esc(d.name)}</span>：</p>
    <input id="stg-confirm" class="mono" placeholder="${esc(d.name)}">`,
    async () => {
      const fs = $('#stg-fs').value;
      const confirm = ($('#stg-confirm').value || '').trim();
      if (confirm !== d.name) {
        toast(T('安全确认未通过：请输入设备名 ') + d.name, 'err', 5000);
        return false;   // 保持弹窗打开，让用户改
      }
      const r = await api('/api/storage', { method: 'POST', body: {
        op: 'format', name: d.name, fs, label: $('#stg-label').value.trim(),
        confirm } });
      if (!r.ok) { toast(r.msg_cn || T('格式化失败'), 'err', 8000); return false; }
      toast(r.msg_cn || T('格式化完成'), 'ok', 5000);
      await stgLoad();
    }, T('确认格式化'));
  const sel = $('#stg-fs');
  const note = $('#stg-fs-note');
  const upd = () => {
    const f = types.find(x => x.v === sel.value);
    if (f && note) {
      const plat = [['win', 'Windows'], ['mac', 'macOS'], ['linux', 'Linux']]
        .filter(([k]) => f[k]).map(([, n]) => n).join(' · ');
      note.innerHTML = `${esc(f.n)}：${bt4('FS_TYPES', f.v, 'note', f.note)}<br>
        <b>${T('可直接读写：')}</b>${esc(plat || T('无（需额外驱动）'))}`;
    }
  };
  if (sel) { sel.onchange = upd; upd(); }
}

/* 把外置盘的挂载点一键加入 SMB 共享，省掉手工填路径 */
async function stgShare(d) {
  if (!d.mountpoint) { toast(T('该设备尚未挂载'), 'err'); return; }
  const name = (d.label || d.name).replace(/[^A-Za-z0-9_-]/g, '_');
  const s = newShare();
  s.name = name;
  s.path = d.mountpoint + '/';
  s.comment = T('外置存储 ') + d.dev + '（' + d.size_h + '）';
  s.mode = 'public';
  s.writable = true;
  sb().samba.shares.push(s);
  if (!sb().enable_smb) sb().enable_smb = true;
  renderShare();
  pageDirty('nfs'); setActionMsg(T('已把 ') + d.mountpoint + T(' 加入 SMB 共享，请点击保存并应用'));
  toast(T('已加入 SMB 共享列表：') + d.mountpoint, 'ok', 4000);
}

function newShare() {
  return { name: '', path: '/srv/share/', comment: '', enable: true,
    mode: 'auth', writable: true, users: 'drouter', allow_hosts: '', deny_hosts: '' };
}

function newExport() {
  return { path: '/srv/share/', enable: true, comment: '',
    clients: [{ net: '192.168.7.0/24', preset: 'linux', options: '' }] };
}

function renderShareList() {
  const el = $('#sh-shares'); if (!el) return;
  const rows = sb().samba.shares || [];
  const dirs = (S.shareData || {}).dirs || {};
  const MODE = { auth: T('需账号密码'), public: T('所有人可访问'), readonly: T('只读公开') };
  el.innerHTML = rows.map((s, i) => {
    const dm = dirs[s.path] || {};
    return `
    <div class="sharecard" data-i="${i}" style="padding:10px;border:1px solid var(--line);
      border-radius:8px;margin-bottom:10px;background:#fbfcfe">
      <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
        <label style="flex:0 0 140px;margin:0">${T('共享名')}
          <input data-f="name" value="${esc(s.name || '')}" placeholder="Public"></label>
        <label style="flex:2 1 260px;margin:0">${T('目录路径')}
          <input data-f="path" value="${esc(s.path || '')}" placeholder="/srv/share/public"></label>
        <button class="ghost small" data-mkdir="${i}" style="flex:0 0 auto"
          title="${T('在路由器上创建该目录')}">${dm.exists ? T('目录已有') + (dm.size ? '（' + esc(dm.size) + '）' : '') : T('创建目录')}</button>
        <label class="switch" style="margin:0;flex:0 0 auto"><input type="checkbox" data-f="enable" ${s.enable !== false ? 'checked' : ''}><i></i>${T('启用')}</label>
        <button class="ghost small" data-del="${i}" style="flex:0 0 auto">${T('删除')}</button>
      </div>
      <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-top:8px">
        <label style="flex:0 0 170px;margin:0">${T('访问方式')}
          <select data-f="mode">
            ${Object.keys(MODE).map(k => `<option value="${esc(k)}" ${(s.mode || 'auth') === k ? 'selected' : ''}>${MODE[k]}</option>`).join('')}
          </select></label>
        <label style="flex:1 1 220px;margin:0">${T('备注')}
          <input data-f="comment" value="${esc(s.comment || '')}" placeholder="${T('例如 全家共享')}"></label>
        <label class="switch" style="margin:0;flex:0 0 auto"><input type="checkbox" data-f="writable" ${s.writable !== false ? 'checked' : ''}><i></i>${T('可写入')}</label>
      </div>
      ${(s.mode === 'auth') ? `
      <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-top:8px">
        <label style="flex:1 1 240px;margin:0">${T('允许的用户（逗号分隔，需系统里已存在）')}
          <input data-f="users" value="${esc(s.users || '')}" placeholder="drouter, alice"></label>
      </div>` : ''}
      <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-top:8px">
        <label style="flex:1 1 240px;margin:0">${T('允许网段（留空')}=${T('不限制）')}
          <input data-f="allow_hosts" value="${esc(s.allow_hosts || '')}" placeholder="192.168.7.0/24"></label>
        <label style="flex:1 1 200px;margin:0">${T('拒绝网段（可选）')}
          <input data-f="deny_hosts" value="${esc(s.deny_hosts || '')}" placeholder="192.168.7.99"></label>
      </div>
      ${dm.exists === false ? `<div class="hint-inline" style="color:var(--warn);margin-top:6px">
        ⚠ ${T('目录')} <span class="mono">${esc(s.path)}</span> ${T('不存在，点上面的「创建目录」即可建立。')}</div>` : ''}
    </div>`;
  }).join('') || `<p class="desc">${T('暂无共享目录，点下面「新增共享」或「套用目录模板」。')}</p>`;

  $$('#sh-shares [data-f]').forEach(e => {
    e.onchange = e.oninput = () => {
      const i = Number(e.closest('.sharecard').dataset.i);
      const f = e.dataset.f;
      const v = (e.type === 'checkbox') ? e.target.checked : e.value;
      sb().samba.shares[i][f] = v;
      if (f === 'mode') renderShareList();
      setActionMsg(T('已修改，请点击保存并应用'));
    };
  });
  $$('#sh-shares [data-del]').forEach(b => b.onclick = () => {
    sb().samba.shares.splice(Number(b.dataset.del), 1);
    renderShareList();
    setActionMsg(T('已删除共享，请点击保存并应用'));
  });
  $$('#sh-shares [data-mkdir]').forEach(b => b.onclick = async () => {
    const i = Number(b.dataset.mkdir);
    const s = sb().samba.shares[i];
    const mode = (s.mode === 'public') ? 'guest' : 'auth';
    setActionMsg(T('正在创建目录 ') + s.path + ' …');
    const r = await api('/api/share', { method: 'POST', body: { op: 'mkdir', path: s.path, mode } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 6000);
    if (r.ok) {
      const r2 = await api('/api/share');
      if (r2.ok) { S.shareData = r2.data; renderShare(); }
    }
  });
}

function renderExportList() {
  const el = $('#sh-exports'); if (!el) return;
  const rows = sb().nfs.exports || [];
  const presets = (S.shareData || {}).nfs_presets || [];
  el.innerHTML = rows.map((x, i) => `
    <div class="sharecard" data-i="${i}" style="padding:10px;border:1px solid var(--line);
      border-radius:8px;margin-bottom:10px;background:#fbfcfe">
      <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
        <label style="flex:2 1 260px;margin:0">${T('目录路径')}
          <input data-f="path" value="${esc(x.path || '')}" placeholder="/srv/share/public"></label>
        <label style="flex:1 1 180px;margin:0">${T('备注')}
          <input data-f="comment" value="${esc(x.comment || '')}" placeholder="${T('例如 影音库')}"></label>
        <label class="switch" style="margin:0;flex:0 0 auto"><input type="checkbox" data-f="enable" ${x.enable !== false ? 'checked' : ''}><i></i>${T('启用')}</label>
        <button class="ghost small" data-del="${i}" style="flex:0 0 auto">${T('删除')}</button>
      </div>
      <div style="margin-top:8px">
        <span style="font-size:12px;color:var(--txt3)">${T('客户端与权限：')}</span>
        <div style="margin-top:6px">${(x.clients || []).map((cl, j) => `
          <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-bottom:6px">
            <label style="flex:0 0 220px;margin:0">${T('客户端')}
              <input data-c="net" data-ci="${j}" value="${esc(cl.net || '')}" placeholder="192.168.7.0/24 ${T('或 *')}"></label>
            <label style="flex:0 0 200px;margin:0">${T('平台模板')}
              <select data-c="preset" data-ci="${j}">
                ${presets.map(p => `<option value="${esc(p.v)}" ${cl.preset === p.v ? 'selected' : ''}>${esc(bt4('NFS_PRESETS', p.v, 'n', p.n))}</option>`).join('')}
              </select></label>
            <label style="flex:1 1 240px;margin:0">${T('选项（留空用模板默认）')}
              <input data-c="options" data-ci="${j}" value="${esc(cl.options || '')}" placeholder="rw,sync,no_subtree_check"></label>
            <button class="ghost small" data-cdel="${j}" style="flex:0 0 auto">${T('删')}</button>
          </div>`).join('')}</div>
        <button class="ghost small fixed" data-cadd="${i}">+ ${T('添加客户端')}</button>
      </div>
    </div>`).join('') || `<p class="desc">${T('暂无 NFS 导出目录，点下面「新增导出」。')}</p>`;

  $$('#sh-exports [data-f]').forEach(e => {
    e.onchange = e.oninput = () => {
      const i = Number(e.closest('.sharecard').dataset.i);
      const f = e.dataset.f;
      sb().nfs.exports[i][f] = (e.type === 'checkbox') ? e.target.checked : e.value;
      setActionMsg(T('已修改，请点击保存并应用'));
    };
  });
  $$('#sh-exports [data-c]').forEach(e => {
    e.onchange = e.oninput = () => {
      const wrap = e.closest('.sharecard');
      const i = Number(wrap.dataset.i);
      const j = Number(e.dataset.ci);
      const ex = sb().nfs.exports[i];
      ex.clients = ex.clients || [];
      ex.clients[j] = ex.clients[j] || {};
      ex.clients[j][e.dataset.c] = e.value;
      setActionMsg(T('已修改，请点击保存并应用'));
    };
  });
  $$('#sh-exports [data-del]').forEach(b => b.onclick = () => {
    sb().nfs.exports.splice(Number(b.dataset.del), 1);
    renderExportList();
    setActionMsg(T('已删除导出，请点击保存并应用'));
  });
  $$('#sh-exports [data-cadd]').forEach(b => b.onclick = () => {
    const i = Number(b.dataset.cadd);
    sb().nfs.exports[i].clients = sb().nfs.exports[i].clients || [];
    sb().nfs.exports[i].clients.push({ net: '192.168.7.0/24', preset: 'linux', options: '' });
    renderExportList();
    setActionMsg(T('已添加客户端，请填写后保存'));
  });
  $$('#sh-exports [data-cdel]').forEach(b => b.onclick = () => {
    const i = Number(b.closest('.sharecard').dataset.i);
    const j = Number(b.dataset.cdel);
    sb().nfs.exports[i].clients.splice(j, 1);
    renderExportList();
    setActionMsg(T('已删除客户端，请点击保存并应用'));
  });
}

function renderShareHints() {
  const el = $('#sh-hints'); if (!el) return;
  const h = (S.shareData || {}).hints || {};
  const order = [['windows', '🪟'], ['macos', ''], ['linux', '🐧']];
  el.innerHTML = order.map(([k, icon]) => {
    const x = h[k]; if (!x) return '';
    return `<div style="padding:10px;border:1px solid var(--line);border-radius:8px;margin-bottom:10px">
      <b>${icon} ${esc(shT(x.title))}</b>
      <ol style="margin:8px 0 0 18px;color:var(--txt2);font-size:13px;line-height:1.9">
        ${(x.steps || []).map(s => `<li>${esc(shT(s))}</li>`).join('')}
      </ol>
      ${(x.cmds || []).length ? `<div style="margin-top:8px">
        ${(x.cmds || []).map(c => `<div class="mono" style="font-size:12px;background:#f2f4f8;
          padding:6px 8px;border-radius:5px;margin-bottom:5px;user-select:all">${esc(c)}</div>`).join('')}
      </div>` : ''}
      ${x.nfs_note ? `<p class="hint-inline" style="margin-top:6px">${esc(shT(x.nfs_note))}</p>` : ''}
    </div>`;
  }).join('');
}


// 共享页连接指南翻译：先查 SHARE_TEXTS，再按模式匹配动态步骤（IP/路径混在句中）。
function shT(s) {
  const v = (typeof bt === 'function') ? bt('SHARE_TEXTS', s, 'd') : '';
  if (v) return v;
  if (!(window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US')) return s;
  const pats = [
    [/^打开「此电脑」，在地址栏输入 \\\\(.+) 回车$/, 'Open "This PC" and enter \\\\$1 in the address bar'],
    [/^输入 smb:\/\/(.+) 回车，选择要挂载的共享$/, 'Enter smb://$1 and pick the share to mount'],
    [/^NFS 则输入 nfs:\/\/(.+)$/, 'For NFS enter nfs://$1'],
  ];
  for (const p of pats) { if (p[0].test(s)) return s.replace(p[0], p[1]); }
  return s;
}

/* ============================ UPnP ============================ */
function viewUpnp() {
  const u = $w('upnp');
  $('#view').innerHTML = `
    <div class="card">
      <h3>UPnP / NAT-PMP</h3>
      <p class="desc">${T('允许局域网设备自动申请端口映射。出于安全考虑，')}<b>${T('默认禁止在')} WAN ${T('侧开放')}</b>${T('， 仅对指定内网地址生效。建议同时开启「安全模式」以限制误映射。')}</p>
      <label class="switch"><input type="checkbox" id="up-en" ${u.enable ? 'checked' : ''}><i></i>${T('启用')} UPnP ${T('服务')}</label>
      <label class="switch"><input type="checkbox" id="up-np" ${u.natpmp ? 'checked' : ''}><i></i>${T('同时启用')} NAT-PMP</label>
      <label class="switch"><input type="checkbox" id="up-sec" ${u.secure_mode !== false ? 'checked' : ''}><i></i>${T('安全模式（仅允许映射到内网地址）')}</label>
      <label class="switch"><input type="checkbox" id="up-igd" ${u.igd_v1 ? 'checked' : ''}><i></i>${T('强制使用')} IGD v1 ${T('描述（兼容老旧设备）')}</label>
      <div class="row">
        <label>WAN ${T('接口')}<input id="up-ext" value="${esc(u.ext_iface || 'ppp0')}"></label>
        <label>${T('监听地址（')}LAN ${T('侧）')}<input id="up-lis" value="${esc(u.listen_ip || '192.168.7.3')}"></label>
        <label>${T('监听端口')}<input id="up-port" type="number" value="${esc(u.port || 5000)}"></label>
      </div>
    </div>
    <div class="card">
      <h3>${T('当前映射表')}</h3>
      <p class="desc">${T('实时展示哪个内网设备申请了哪个端口，便于安全审查。')}</p>
      <div class="row"><button class="ghost small fixed" id="up-list">${T('刷新映射表')}</button></div>
      <div id="up-out" style="margin-top:12px"><p class="desc">${T('点击上方按钮查询。')}</p></div>
    </div>`;
  const sync = () => {
    S.cfg.upnp = Object.assign($w('upnp'), {
      enable: $('#up-en').checked, natpmp: $('#up-np').checked,
      secure_mode: $('#up-sec').checked, igd_v1: $('#up-igd').checked,
      ext_iface: $('#up-ext').value, listen_ip: $('#up-lis').value,
      port: numOr($('#up-port').value),
    });
    setActionMsg(T('已修改，请点击保存并应用'));
  };
  // 元素可能被条件隐藏（如 igd_v1 关掉时某些行不渲染），少一个 if 防御
  // 就是一次 TypeError 把整个页面的 sync 绑定全打断。
  ['#up-en', '#up-np', '#up-sec', '#up-igd'].forEach(s => { const e = $(s); if (e) e.onchange = sync; });
  ['#up-ext', '#up-lis', '#up-port'].forEach(s => { const e = $(s); if (e) e.oninput = sync; });
  $('#up-list').onclick = async () => {
    $('#up-out').innerHTML = `<pre>${T('读取中…')}</pre>`;
    // 只读接口：查询绝不顺手启动服务（「保存」与「生效」必须分开）
    const r = await api('/api/upnpmap');
    if (!r.ok) { $('#up-out').innerHTML = `<pre>${esc(r.msg_cn || T('读取失败'))}</pre>`; return; }
    const d = r.data || {};
    if (!d.active) { $('#up-out').innerHTML = `<p class="desc">${esc(d.note || T('miniupnpd 未在运行'))}</p>`; return; }
    const rows = d.mappings || [];
    if (!rows.length) { $('#up-out').innerHTML = `<p class="desc">${T('服务运行中，当前没有任何端口映射。')}</p>`; return; }
    $('#up-out').innerHTML = `<table><thead><tr><th>${T('协议')}</th><th>${T('外部端口')}</th><th>${T('内网地址')}</th><th>${T('内网端口')}</th><th>${T('说明')}</th></tr></thead>
      <tbody>${rows.map(m => `<tr><td>${esc(m.proto)}</td><td class="mono">${esc(m.ext_port)}</td>
        <td class="mono">${esc(m.int_ip)}</td><td class="mono">${esc(m.int_port)}</td>
        <td>${esc(m.desc || '')}</td></tr>`).join('')}</tbody></table>`;
  };
}

/* ============================ NTP ============================ */
function viewNtp() {
  const n = $w('ntp');
  $('#view').innerHTML = `
    <div class="card">
      <h3>NTP ${T('时间同步')}</h3>
      <p class="desc">${T('可自定义时间服务器地址，支持多个（逗号分隔）。')}</p>
      <label>${T('时间服务器')}<input id="nt-s" value="${esc(n.servers || '')}" placeholder="ntp.aliyun.com,time.cloudflare.com"></label>
      <label class="switch"><input type="checkbox" id="nt-allow" ${n.allow_lan ? 'checked' : ''}><i></i>${T('允许局域网设备向本机同步时间')}</label>
      <div class="row">
        <label>${T('允许同步的网段')}<input id="nt-net" value="${esc(n.allow_net || '')}" placeholder="192.168.7.0/24"></label>
      </div>
      <div class="row" style="margin-top:10px"><button class="ghost small fixed" id="nt-refresh">${T('刷新同步状态')}</button></div>
      <div id="nt-out" style="margin-top:14px">${T('读取中')}…</div>
    </div>
    <div class="card">
      <h3>${T('常见时间服务器')}</h3>
      <table><thead><tr><th>${T('来源')}</th><th>${T('地址')}</th></tr></thead><tbody>
        <tr><td>${T('阿里云')}</td><td class="mono">ntp.aliyun.com</td></tr>
        <tr><td>${T('腾讯云')}</td><td class="mono">ntp.tencent.com</td></tr>
        <tr><td>${T('国家授时中心')}</td><td class="mono">ntp.ntsc.ac.cn</td></tr>
        <tr><td>Cloudflare</td><td class="mono">time.cloudflare.com</td></tr>
        <tr><td>Google</td><td class="mono">time.google.com</td></tr>
      </tbody></table>
    </div>`;
  const sync = () => {
    S.cfg.ntp = Object.assign($w('ntp'), {
      servers: $('#nt-s').value, allow_lan: $('#nt-allow').checked, allow_net: $('#nt-net').value,
    });
    setActionMsg(T('已修改，请点击保存并应用'));
  };
  $('#nt-s').oninput = sync; $('#nt-net').oninput = sync; $('#nt-allow').onchange = sync;
  const load = async () => {
    const r = await api('/api/ntp');
    const d = r.data || {};
    const tracking = (d.tracking || '').split('\n').reduce((o, l) => {
      const m = l.match(/^([^:]+):\s*(.*)$/); if (m) o[m[1].trim()] = m[2].trim(); return o;
    }, {});
    $('#nt-out').innerHTML = `
      <div class="kv"><b>${T('系统时间')}</b><span class="mono">${esc(d.now || '—')}</span></div>
      <div class="kv"><b>${T('同步状态')}</b><span>${tracking['Leap status'] === 'Normal' ? `<span class="tag ok">${T('已同步')}</span>` : '<span class="tag warn">' + esc(tracking['Leap status'] || T('未同步')) + '</span>'}</span></div>
      <div class="kv"><b>${T('时间偏移')}</b><span class="mono">${esc(tracking['System time'] || '—')} ${T('秒')}</span></div>
      <div class="kv"><b>${T('参考源')}</b><span class="mono">${esc(tracking['Reference ID'] || '—')}</span></div>
      <div class="kv"><b>${T('层级')}</b><span>${esc(tracking['Stratum'] || '—')}</span></div>
      <div class="kv"><b>${T('源列表')}</b><pre style="margin-top:6px">${esc(d.sources || T('（chrony 未运行）'))}</pre></div>`;
  };
  $('#nt-refresh').onclick = load;
  load();
}

/* ============================ 诊断 ============================ */
function viewDiag() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>Ping ${T('连通性测试')}</h3>
      <div class="row">
        <label>${T('目标地址')}<input id="dg-ping-t" value="223.5.5.5"></label>
        <label style="flex:0 0 120px">${T('次数')}<input id="dg-ping-c" type="number" value="4"></label>
        <button class="ghost fixed" data-diag="ping">${T('开始')} Ping</button>
      </div>
    </div>
    <div class="card">
      <h3>${T('路由追踪')} / MTR</h3>
      <div class="row">
        <label>${T('目标地址')}<input id="dg-tr-t" value="223.5.5.5"></label>
        <button class="ghost fixed" data-diag="traceroute">Traceroute</button>
        <button class="ghost fixed" data-diag="mtr">MTR${T('（5 次）')}</button>
      </div>
    </div>
    <div class="card">
      <h3>${T('吞吐测试')}</h3>
      <p class="desc">${T('内网测速使用')} iperf3${T('（需对端运行')} <span class="mono">iperf3 -s</span>${T('）；互联网测速通过下载测速文件估算下行带宽。')}</p>
      <div class="row">
        <label>${T('对端地址')}<input id="dg-ip-t" placeholder="192.168.7.x"></label>
        <button class="ghost fixed" data-diag="iperf">iperf3 ${T('内网测速')}</button>
        <button class="ghost fixed" data-diag="speedtest">${T('互联网测速')}</button>
        <button class="ghost fixed" data-diag="iperf_server">${T('启动')} iperf3 ${T('服务端')}</button>
      </div>
    </div>
    <div class="card">
      <h3>${T('抓包与连接状态')}</h3>
      <div class="row">
        <label>${T('抓包接口')}<select id="dg-cp-i">${S.ifaces.map(i => `<option>${esc(i.name)}</option>`).join('')}</select></label>
        <label style="flex:0 0 120px">${T('包数')}<input id="dg-cp-c" type="number" value="50"></label>
        <button class="ghost fixed" data-diag="tcpdump">${T('开始抓包')}</button>
        <button class="ghost fixed" data-diag="conntrack">${T('连接跟踪')}</button>
      </div>
    </div>
    <div class="card">
      <h3>${T('输出结果')}</h3>
      <div id="dg-out"><p class="desc">${T('请在上方选择工具并执行。')}</p></div>
    </div>`;
  $$('[data-diag]').forEach(b => b.onclick = () => runDiag(b.dataset.diag));
}

async function runDiag(tool) {
  const out = $('#dg-out');
  out.innerHTML = `<pre>${T('执行中，请稍候…')}</pre>`;
  let body = { tool };
  if (tool === 'ping') body = { tool, target: $('#dg-ping-t').value, count: Number($('#dg-ping-c').value) };
  if (tool === 'traceroute' || tool === 'mtr') body = { tool, target: $('#dg-tr-t').value };
  if (tool === 'iperf') body = { tool, target: $('#dg-ip-t').value };
  if (tool === 'tcpdump') body = { tool, iface: $('#dg-cp-i').value, count: Number($('#dg-cp-c').value) };
  const r = await api('/api/diag', { method: 'POST', body });
  const d = r.data || {};
  out.innerHTML = `<div style="margin-bottom:8px"><span class="tag ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn)}</span></div>
    <pre>${esc(d.out || d.err || T('（无输出）'))}</pre>`;
}

/* ============================ IPv6 连通性测试 ============================ */
function viewV6Test() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>IPv6 ${T('连通性测试')}</h3>
      <p class="desc">${T('一键检测本机与互联网的')} IPv6 ${T('连通情况。测试过程会实时回显结果。')}</p>
      <div class="row">
        <button class="fixed" id="v6t-cn">${T('国内')} IPv6 ${T('测试（')}testipv6.cn）</button>
        <button class="fixed" id="v6t-intl">${T('国际')} IPv6 ${T('测试（')}testipv6.com）</button>
        <button class="ghost fixed" id="v6t-all">${T('全部测试')}</button>
      </div>
      <label class="switch"><input type="checkbox" id="v6t-auto" checked><i></i>${T('自动实时刷新（每')} 30 ${T('秒）')}</label>
      <div class="kv"><b>${T('上次测试时间')}</b><span id="v6t-time">${T('尚未测试')}</span></div>
    </div>

    <div class="card">
      <h3>${T('本机')} IPv6 ${T('环境')}</h3>
      <p class="desc">WAN/LAN ${T('是否取得公网')} IPv6${T('、是否开启转发、默认路由是否就绪。')}</p>
      <div id="v6t-env"><p class="desc">${T('读取中')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('测试结果')}</h3>
      <div id="v6t-out"><p class="desc">${T('点击上方按钮开始测试。')}</p></div>
    </div>`;
  loadV6Env();
  $('#v6t-cn').onclick = () => runV6Test('cn');
  $('#v6t-intl').onclick = () => runV6Test('intl');
  $('#v6t-all').onclick = () => runV6Test('all');
  let timer = null;
  const auto = $('#v6t-auto');
  const setupTimer = () => {
    if (timer) { clearInterval(timer); timer = null; }
    if (auto.checked) timer = setInterval(() => runV6Test('cn', true), 30000);
  };
  // 挂到全局钩子：go() 切页时会调用它停表（闭包内变量外面拿不到）
  stopV6Timer = () => { if (timer) { clearInterval(timer); timer = null; } };
  auto.onchange = setupTimer;
  setupTimer();
}

async function loadV6Env() {
  const el = $('#v6t-env'); if (!el) return;
  const r = await api('/api/ipv6');
  const d = r.data || {};
  const g = (d.addrs || []).flatMap(a => (a.addr_info || []).filter(x => x.family === 'inet6')
    .map(x => ({ if: a.ifname, a: x.local, p: x.prefixlen, sc: x.scope })));
  const global = g.filter(x => x.sc === 'global');
  const fwd = (d.sysctl || {})['all.forwarding'] === '1';
  const ready = global.length > 0 && fwd && d.default_route;
  el.innerHTML = `
    <div class="kv"><b>${T('整体判定')}</b><span>${ready
      ? `<span class="tag ok">${T('IPv6 环境就绪')}</span>`
      : `<span class="tag warn">${T('尚未就绪')}</span>`}</span></div>
    <div class="kv"><b>${T('公网')} IPv6</b><span class="mono">${global.length
      ? global.slice(0, 4).map(x => esc(x.a) + '/' + x.p + ' <span class="tag gray">' + esc(x.if) + '</span>').join('<br>')
      : `<span class="tag warn">${T('未获取公网地址')}</span>`}</span></div>
    <div class="kv"><b>${T('默认路由')}</b><span class="mono">${esc(d.default_route || T('无'))}</span></div>
    <div class="kv"><b>IPv6 ${T('转发')}</b><span>${fwd ? `<span class="tag ok">${T('已开启')}</span>` : `<span class="tag warn">${T('未开启')}</span>`}</span></div>
    <div class="kv"><b>${T('邻居表条数')}</b><span>${(d.neigh || []).length}</span></div>`;
}

async function runV6Test(which, silent) {
  const out = $('#v6t-out');
  if (!out) return;
  if (!silent) out.innerHTML = `<pre>${T('测试中，正在连接测试站点…')}</pre>`;
  const r = await api('/api/diag', { method: 'POST', body: { tool: 'ipv6test', which } });
  const d = r.data || {};
  const t = $('#v6t-time');
  if (t) t.textContent = new Date().toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-');
  if (silent && r.ok && out.querySelector('pre')) {
    // 静默刷新时仅在结果区已有内容时更新
  }
  const blocks = (d.results || []).map(x => `
    <div class="card" style="margin:0 0 12px;border-color:${x.ok ? 'var(--ok)' : 'var(--line)'}">
      <h3 style="margin:0 0 6px;font-size:14px">${esc(x.name)} <span class="tag ${x.ok ? 'ok' : 'err'}">${x.ok ? T('连通') : T('不通')}</span></h3>
      <div class="kv"><b>${T('目标')}</b><span class="mono">${esc(x.target || '—')}</span></div>
      <div class="kv"><b>${T('耗时')}</b><span>${x.ms != null ? x.ms + ' ms' : '—'}</span></div>
      ${x.detail ? `<pre style="margin-top:6px">${esc(x.detail)}</pre>` : ''}
    </div>`).join('');
  out.innerHTML = `<div style="margin-bottom:8px"><span class="tag ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</span></div>
    ${blocks || `<pre>${esc(d.out || d.err || T('（无输出）'))}</pre>`}`;
}

/* ============================ DHCPv6 / 前缀委派 ============================ */
function viewDhcpv6() {
  const c = $w('dhcpv6');
  const enabled = c.enabled !== false;
  $('#view').innerHTML = `
    <div class="card">
      <h3>DHCPv6 ${T('前缀委派（')}PD）</h3>
      <p class="desc">${T('向运营商申请')} IPv6 ${T('前缀（如')} /60、/56${T('），再下发给内网设备。 大陆家庭宽带通常需要')} PD ${T('才能获得可用的公网')} IPv6。</p>
      <label class="switch"><input type="checkbox" id="d6-en" ${enabled ? 'checked' : ''}><i></i>${T('启用')} DHCPv6 ${T('客户端')}</label>
      <div class="row">
        <label>${T('请求前缀长度')}<select id="d6-len">
          ${[56, 60, 62, 64].map(x => `<option value="${x}" ${Number(c.prefix_len || 60) === x ? 'selected' : ''}>/${x}</option>`).join('')}
        </select></label>
        <label>PD ${T('网卡（')}WAN）<select id="d6-iface">
          <option value="">${T('跟随 WAN 口')}</option>
          ${S.ifaces.map(i => `<option value="${esc(i.name)}" ${c.iface === i.name ? 'selected' : ''}>${esc(i.name)}${i.role === 'wan' ? ' — WAN' : ''}</option>`).join('')}
        </select></label>
      </div>
      <label class="switch"><input type="checkbox" id="d6-rapid" ${c.rapid_commit ? 'checked' : ''}><i></i>${T('快速提交（')}Rapid Commit）</label>
      <p class="hint-inline">${T('保存后需点击「保存并应用」才会写入')} <span class="mono">/etc/dhcpcd.conf</span> ${T('与网络配置。')}</p>
    </div>

    <div class="card">
      <h3>${T('下游地址分配')}</h3>
      <p class="desc">LAN ${T('侧如何把前缀下发给内网设备（配合「')}IPv6 / RA${T('」页面的地址池）。')}</p>
      <div class="row">
        <label>${T('分配方式')}<select id="d6-method">
          <option value="slaac" ${(c.method || 'slaac') === 'slaac' ? 'selected' : ''}>SLAAC${T('（无状态，推荐）')}</option>
          <option value="dhcpv6" ${c.method === 'dhcpv6' ? 'selected' : ''}>DHCPv6 ${T('有状态')}</option>
          <option value="both" ${c.method === 'both' ? 'selected' : ''}>${T('两者同时')}</option>
        </select></label>
        <label>${T('子网')} ID（SLA-ID）<input id="d6-sid" value="${esc(c.sla_id || '::1')}" placeholder="${T('例如')} ::1 ${T('或 0')}"></label>
      </div>
      <p class="hint-inline">${T('这里的「子网')} ID${T('」与「IPv6 / RA')}${T('」页面里的「')}SLA-ID ${T('后缀」是同一个值 （原先这里写的是')} subnet_id${T('，渲染器读的是')} sla_id —— ${T('改了什么都不生效）。')}</p>
    </div>

    <div class="card">
      <h3>${T('当前')} IPv6 ${T('状态')}</h3>
      <div id="d6-live"><p class="desc">${T('读取中')}…</p></div>
    </div>`;
  const bind = (id, k, conv) => {
    const e = $(id); if (!e) return;
    const h = () => { S.cfg.dhcpv6 = Object.assign($w('dhcpv6'), { [k]: conv ? conv(e.value) : e.value }); setActionMsg(T('已修改，请点击保存并应用')); };
    e.onchange = h; if (e.tagName === 'INPUT') e.oninput = h;
  };
  // prefix_len 统一存数字：IPv6 页写的是 Number()，这里原来靠 <option>
  // 的文本当值（'/60' 字符串），两页共享同一个 dhcpv6 模块却类型不同 ——
  // 在 IPv6 页存成 64 后进本页，四个选项全不选中（浏览器显示第一项 /56），
  // 用户动一下别的字段点保存就把 prefix_len 覆盖成 '/56'，反向污染 IPv6 页
  // （type=number 收到 '/60' 变成空，Number('') = 0）。
  bind('#d6-len', 'prefix_len', Number); bind('#d6-iface', 'iface');
  bind('#d6-method', 'method');
  // 写 sla_id 而不是 subnet_id：渲染器（render_dhcpcd 的 ia_pd 第 5 段）读的是
  // sla_id，存到 subnet_id 的话用户改完点「保存并应用」什么都不会变。
  bind('#d6-sid', 'sla_id');
  $('#d6-en').onchange = e => { S.cfg.dhcpv6 = Object.assign($w('dhcpv6'), { enabled: e.target.checked }); setActionMsg(T('已修改，请点击保存并应用')); };
  $('#d6-rapid').onchange = e => { S.cfg.dhcpv6 = Object.assign($w('dhcpv6'), { rapid_commit: e.target.checked }); setActionMsg(T('已修改，请点击保存并应用')); };
  liveV6();
}

/* ============================ 真·公网 IP 判定（#2） ============================ */
/* 「有没有公网地址」和「外网能不能连进来」是两件事。
   很多宽带给的是公网地址却在入向做了封锁 —— 这时 DDNS 能更新成功、
   域名也解析得到，但别人就是连不上。本页把判定拆成四条独立证据，
   其中只有「入向实测」能拍板，所以页面会把这一步摆在最显眼的位置。 */

let PUBIP_TIMER = null;

async function viewPubip() {
  const v = $('#view');
  v.innerHTML = `
    <div class="card">
      <h3>${T('真')}·${T('公网')} IP ${T('判定')}
        <button class="ghost small" id="pi-check" style="float:right">${T('重新判定')}</button></h3>
      <p class="desc">${T('判定出口')} IP ${T('是不是')}<b>${T('真的能被外网主动连进来')}</b>${T('。 只看「地址是不是公网段」会误判')} —— ${T('不少宽带给的是公网地址却在入向做了封锁， 从别的')} VPS ${T('根本')} ping ${T('不通。')}</p>
      <div id="pi-body"><p class="desc">${T('正在判定')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('入向实测（唯一能拍板的一步）')}</h3>
      <p class="desc" id="pi-probe-desc">${T('下面会启动一次监听，然后给你一条命令。 在')}<b>${T('另一台能上外网的主机')}</b>${T('（你的')} VPS${T('、云主机，或手机开热点连着的电脑）上执行它， 本机能收到就说明外网确实可以主动访问进来。')}</p>
      <div class="row" style="gap:8px;flex-wrap:wrap;margin-top:8px">
        <button class="primary" id="pi-start">${T('开始入向实测')}</button>
        <button class="ghost" id="pi-stop">${T('停止')}</button>
        <span class="desc" id="pi-probe-msg" style="margin:0"></span>
      </div>
      <div id="pi-cmd" class="hidden" style="margin-top:12px">
        <p class="desc" id="pi-how"></p>
        <div class="code-wrap">
          <button class="ghost small copy-btn" data-copy="#pi-cmdtext">${T('复制命令')}</button>
          <pre id="pi-cmdtext" class="log-pre"></pre>
        </div>
        <p class="desc" id="pi-left"></p>
      </div>
    </div>`;
  $('#pi-check').onclick = () => pubipCheck(true);
  $('#pi-start').onclick = () => pubipProbe('probe_start');
  $('#pi-stop').onclick = () => pubipProbe('probe_stop');
  bindCopyButtons(v);
  pubipCheck(false);
}

function pubipVerdictTag(level) {
  return { ok: 'ok', warn: 'warn', err: 'err', info: 'info' }[level] || 'gray';
}

async function pubipCheck(force) {
  const box = $('#pi-body');
  if (!box) return;
  box.innerHTML = `<p class="desc">${T('正在判定（会并发查询多个外部回显服务，首次约需 10 秒，之后走缓存）…')}</p>`;
  const r = await api('/api/pubip', { method: 'POST', body: { op: 'check', force: !!force } });
  if (!r.ok) { box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('判定失败'))}</div>`; return; }
  const d = r.data || {};
  const vd = d.verdict || {};
  const tag = pubipVerdictTag(vd.level);

  // 「复用缓存」提示里那个重测按钮。
  // 放在 innerHTML 写完之后拿 —— 元素是 innerHTML 生成的，
  // 在写之前 find 必然是 null。
  const f2 = document.getElementById('pi-force2');
  if (f2) f2.onclick = () => pubipCheck(true);

  const ev = (d.evidence || []).map(e => `
    <div class="dep-item ${e.pass ? 'ok' : (e.probed === false ? 'warn' : 'err')}">
      <span class="dep-ico">${e.pass ? '✓' : (e.probed === false ? '?' : '✕')}</span>
      <div class="dep-body">
        <b>${esc(enOrZh(e.en_name, e.name))}：${esc(String(enOrZh(e.en_value, e.value)))}</b>
        <div class="desc">${esc(enOrZh(e.en_detail, e.detail) || '')}</div>
      </div>
      <span class="tag ${e.pass ? 'ok' : (e.probed === false ? 'gray' : 'err')}">${e.pass ? T('通过') : (e.probed === false ? T('未测') : T('不通过'))}</span>
    </div>`).join('');

  box.innerHTML = `
    <div class="notice ${tag}" style="margin-bottom:12px">
      <b>${esc(enOrZh(vd.en_title, vd.title) || '')}</b>
      <div style="margin-top:6px">${esc(enOrZh(vd.en_conclusion, vd.conclusion) || '')}</div>
    </div>
    <div class="grid2" style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px">
      <div class="kv"><b>${T('出口')} IPv4</b><span class="mono">${esc(d.ip || '—')}</span></div>
      <div class="kv"><b>${T('地址分类')}</b><span class="mono">${esc(d.class || '—')}</span></div>
      <div class="kv"><b>${T('本机地址')}</b><span class="mono">${esc(d.v4_local || '—')}</span></div>
      <div class="kv"><b>${T('出口网卡')}</b><span class="mono">${esc(d.egress || '—')}</span></div>
    </div>
    <h4 style="margin:14px 0 6px">${T('四条判定依据')}</h4>
    <div class="dep-grid">${ev}</div>
    ${(vd.advice || []).length ? `
    <h4 style="margin:14px 0 6px">${T('接下来该怎么做')}</h4>
    <ul class="desc" style="margin:0;padding-left:20px;line-height:1.9">
      ${enOrZh(vd.en_advice, vd.advice).map(a => `<li>${esc(a)}</li>`).join('')}
    </ul>` : ''}
    <p class="desc" style="margin-top:10px">${T('判定时间：')}${esc(d.checked_at || '')}</p>
    ${d.evidence_cached ? `<p class="desc" style="margin-top:4px">${T('「多源回显一致性」与「首跳链路」两条复用了上一次探测的结果（30 分钟内）。 刚改过网络、宽带或端口转发的话，点上方')}<button class="ghost small" id="pi-force2">${T('重新检测')}</button>${T('重测。')}</p>` : ''}`;
}

async function pubipProbe(op) {
  const msg = $('#pi-probe-msg');
  const r = await api('/api/pubip', { method: 'POST', body: { op: op } });
  const d = r.data || {};
  if (msg) msg.textContent = r.msg_cn || (r.ok ? T('已提交') : T('操作失败'));
  if (!r.ok) return;

  if (op === 'probe_start') {
    const card = $('#pi-cmd');
    card.classList.remove('hidden');
    $('#pi-how').textContent = d.how || '';
    $('#pi-cmdtext').textContent = d.cmd || '';
    pubipProbePoll();
    return;
  }
  if (op === 'probe_stop') {
    pubipProbeStopPoll();
    $('#pi-cmd').classList.add('hidden');
    return;
  }
}

function pubipProbeStopPoll() {
  if (PUBIP_TIMER) { clearInterval(PUBIP_TIMER); PUBIP_TIMER = null; }
}

function pubipProbePoll() {
  pubipProbeStopPoll();
  PUBIP_TIMER = setInterval(async () => {
    const r = await api('/api/pubip', { method: 'POST', body: { op: 'probe_status' } });
    const d = r.data || {};
    const msg = $('#pi-probe-msg');
    const left = $('#pi-left');
    if (d.state === 'hit') {
      pubipProbeStopPoll();
      if (msg) msg.textContent = T('已收到入向报文 —— 外网可以主动访问本机');
      if (left) left.innerHTML = `<span class="tag ok">${T('入向可达')}</span> ${T('来源：')}${esc((d.hits || []).join('、'))}`;
      toast(T('入向实测成功：外网确实能连进来'), 'ok', 5000);
      await pubipCheck(true);
      return;
    }
    if (d.state === 'timeout') {
      pubipProbeStopPoll();
      if (msg) msg.textContent = T('等待超时，没有收到来自外网的报文');
      if (left) left.innerHTML = `<span class="tag err">${T('入向不可达')}</span> ${T('已判定为「有公网地址但入向被拦」')}`;
      await pubipCheck(true);
      return;
    }
    if (d.state === 'running') {
      if (left) left.textContent = `${T('仍在等待外网报文，还剩')} ${d.left || 0} ${T('秒')}`;
    }
  }, 3000);
}

/* ======================== 磁盘与日志清理（#3） ========================
   菜单放在「系统」组下，位置紧挨依赖自检 —— 两者同属日常维护。
   页面刻意把「策略」和「清理项」分成两张卡片：策略决定"什么时候自动做"，
   清理项决定"做哪些、留多久"。混在一张卡里用户很难分清哪个开关管什么。 */

let CLEAN_DATA = null;

const CLEAN_TRIGGER_TEXT = () => ({ disk: T('仅水位触发'), schedule: T('仅定时触发'), both: T('水位 + 定时') });

async function viewCleanup() {
  const v = $('#view');
  v.innerHTML = `
    <div class="card">
      <h3>${T('磁盘占用')}
        <button class="ghost small" id="cl-rescan" style="float:right">${T('重新扫描')}</button></h3>
      <p class="desc">${T('路由器常年不关机，日志与缓存会一直长。这里按「可再生产物」分类回收')} ——
        ${T('配置文件、证书、代码、配置快照')}<b>${T('一律不在清理范围内')}</b>${T('，正在写入的活跃日志也不动。')}</p>
      <div id="cl-disk"><p class="desc">${T('正在扫描')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('自动清理策略')}</h3>
      <p class="desc">${T('决定「什么时候自动动手」。清理哪些内容、各留多久，在下面一张卡片里单独设置。')}</p>
      <div id="cl-policy"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('清理项')}
        <span class="tag gray" id="cl-sum"></span>
        <button class="ghost small" id="cl-dry" style="float:right">${T('试运行')}</button></h3>
      <p class="desc">${T('每一项都可以单独开关，并自定义保留天数。')}
        <b>${T('试运行')}</b>${T('只统计不删除，用来先看清楚会清掉多少；确认无误再点「立即清理」。')}</p>
      <div id="cl-items"><p class="desc">${T('正在扫描')}…</p></div>
      <div class="row" style="margin-top:14px">
        <button class="primary" id="cl-run">${T('立即清理选中项')}</button>
        <button class="ghost" id="cl-selectall">${T('全选')}</button>
        <button class="ghost" id="cl-selectnone">${T('全不选')}</button>
      </div>
      <div id="cl-result" class="hidden" style="margin-top:14px"></div>
    </div>`;
  $('#cl-rescan').onclick = () => cleanupLoad(true);
  $('#cl-dry').onclick = () => cleanupRun(true);
  $('#cl-run').onclick = () => {
    if (!confirm(T('确定立即清理所有勾选的项目吗？被删的日志文件无法恢复。\\n\\n（建议先点「试运行」看清楚会清掉多少）'))) return;
    cleanupRun(false);
  };
  $('#cl-selectall').onclick = () => cleanupSelectAll(true);
  $('#cl-selectnone').onclick = () => cleanupSelectAll(false);
  cleanupLoad(false);
}

function cleanupSelectAll(on) {
  document.querySelectorAll('#cl-items .cl-on').forEach(el => { el.checked = on; });
}

async function cleanupLoad(toastIt) {
  const r = await api('/api/cleanup', { method: 'POST', body: { op: 'status' } });
  if (!r.ok) {
    $('#cl-disk').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('扫描失败'))}</div>`;
    return;
  }
  CLEAN_DATA = r.data;
  if (toastIt) toast(T('扫描完成'), 'ok');
  cleanupRenderDisk();
  cleanupRenderPolicy();
  cleanupRenderItems();
}

function cleanupRenderDisk() {
  const d = (CLEAN_DATA && CLEAN_DATA.disk) || {};
  const pct = Number(d.percent || 0);
  const warn = pct >= 85 ? 'err' : (pct >= 70 ? 'warn' : 'ok');
  const reclaim = CLEAN_DATA ? (CLEAN_DATA.reclaimable || 0) : 0;
  $('#cl-disk').innerHTML = `
    <div class="row" style="align-items:center;margin-bottom:6px">
      <div><b style="font-size:22px">${pct}%</b>
        <span class="tag ${warn}" style="margin-left:8px">${pct >= 85 ? T('紧张') : (pct >= 70 ? T('偏高') : T('正常'))}</span></div>
      <div class="desc" style="margin:0;text-align:right">
        ${T('已用')} ${fmtBytes((d.used_mb || 0) * 1048576)} / ${T('共')} ${fmtBytes((d.total_mb || 0) * 1048576)}
        ${T('，可用')} ${fmtBytes((d.free_mb || 0) * 1048576)}
      </div>
    </div>
    <div class="bar"><i style="width:${Math.min(100, pct)}%;background:${
      warn === 'err' ? 'var(--err)' : (warn === 'warn' ? 'var(--warn)' : 'var(--pri)')}"></i></div>
    <p class="desc" style="margin:10px 0 0">${T('按当前策略，勾选中的项目')}<b>${T('可释放')} ${fmtBytes(reclaim)}</b>。</p>`;
}

function cleanupRenderPolicy() {
  const c = (CLEAN_DATA && CLEAN_DATA.config) || {};
  // 0 点是合法值（falsy），不能写 c.hour || 4 —— 配了 00:00 会显示成 04:00
  const cHour = (c.hour === 0 || c.hour) ? Number(c.hour) : 4;
  const hours = Array.from({ length: 24 }, (_, i) =>
    `<option value="${esc(i)}"${i === cHour ? ' selected' : ''}>${String(i).padStart(2, '0')}:00</option>`).join('');
  const st = (CLEAN_DATA && CLEAN_DATA.state) || {};
  $('#cl-policy').innerHTML = `
    <div class="row" style="align-items:center">
      <label class="switch"><input type="checkbox" id="cl-enabled"${c.enabled ? ' checked' : ''}><i></i>
        <span>${T('启用自动清理')}</span></label>
      <label class="desc" style="flex:1 1 260px;margin:0">${T('触发方式')}
        <select id="cl-trigger" style="width:100%;margin-top:4px">
          <option value="both"${c.trigger === 'both' ? ' selected' : ''}>${T('水位')} + ${T('定时（推荐）')}</option>
          <option value="disk"${c.trigger === 'disk' ? ' selected' : ''}>${T('仅水位触发')}</option>
          <option value="schedule"${c.trigger === 'schedule' ? ' selected' : ''}>${T('仅定时触发')}</option>
        </select></label>
      <label class="desc" style="flex:0 0 120px;margin:0">${T('水位阈值')}
        <input id="cl-pct" type="number" min="50" max="99" value="${esc(Number(c.disk_percent || 85))}"
               style="width:100%;margin-top:4px"><span style="font-size:12px">% ${T('使用率超过就清')}</span></label>
    </div>
    <div class="row" style="margin-top:10px;align-items:center">
      <label class="desc" style="flex:0 0 130px;margin:0">${T('周期')}
        <select id="cl-sched" style="width:100%;margin-top:4px">
          <option value="daily"${c.schedule === 'daily' ? ' selected' : ''}>${T('每天')}</option>
          <option value="weekly"${c.schedule === 'weekly' ? ' selected' : ''}>${T('每周一')}</option>
        </select></label>
      <label class="desc" style="flex:0 0 130px;margin:0">${T('执行时间')}
        <select id="cl-hour" style="width:100%;margin-top:4px">${hours}</select></label>
      <label class="desc" style="flex:1 1 200px;margin:0">${T('单次上限')}
        <input id="cl-max" type="number" min="0" max="1048576" value="${esc(Number(c.max_mb_per_run || 0))}"
               style="width:100%;margin-top:4px"><span style="font-size:12px">MB，0 = ${T('不限（防止一次删太多）')}</span></label>
    </div>
    <p class="desc" style="margin:10px 0 0">${T('定时器每小时被拉起一次，自己判断该不该真动手')} ——
      ${T('水位没到、时间没到就什么都不做。当前定时器：')}
      <span class="tag ${CLEAN_DATA && CLEAN_DATA.timer_active ? 'ok' : 'gray'}">${
        CLEAN_DATA && CLEAN_DATA.timer_active ? T('已启用') : T('未启用')}</span>
      ${CLEAN_DATA && CLEAN_DATA.next_run ? `${T('下次检查')} ${esc(CLEAN_DATA.next_run)}` : ''}
      ${st.last_run ? `　${T('上次执行')} ${esc(st.last_run)}${T('，释放')} ${fmtBytes(st.last_freed || 0)}` : ''}</p>
    <div class="row" style="margin-top:10px">
      <button class="primary" id="cl-save">${T('保存策略')}</button>
      <button class="ghost" id="cl-now">${T('按策略立即执行一次')}</button>
    </div>`;
  $('#cl-save').onclick = cleanupSave;
  $('#cl-now').onclick = async () => {
    const r = await api('/api/cleanup', { method: 'POST', body: { op: 'auto' } });
    toast(r.msg_cn || T('已执行'), r.ok ? 'ok' : 'err', 5000);
    if (r.ok && !(r.data || {}).skipped) cleanupLoad(false);
  };
}

function cleanupRenderItems() {
  const items = (CLEAN_DATA && CLEAN_DATA.items) || [];
  let sum = 0;
  items.forEach(it => { if (it.on) sum += it.reclaimable || 0; });
  const el = $('#cl-sum');
  if (el) el.textContent = `${items.length} ${T('项')} · ${T('可释放')} ${fmtBytes(sum)}`;
  $('#cl-items').innerHTML = `<div class="dep-grid">${items.map(it => `
    <div class="dep-item ${it.on ? 'warn' : ''}">
      <span class="dep-ico" style="background:${it.on ? 'var(--warn)' : '#c3ccd9'}">${
        it.on ? (it.reclaimable > 0 ? '!' : '·') : '－'}</span>
      <div class="dep-body" style="flex:1 1 200px">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <label class="switch" style="margin:0"><input type="checkbox" class="cl-on" data-k="${esc(it.key)}"${
            it.on ? ' checked' : ''}><i></i></label>
          <b>${esc(bt4('CLEAN_ITEMS', it.key, 'name', it.name))}</b>
          ${it.reclaimable > 0 ? `<span class="tag warn">${T('可释放')} ${fmtBytes(it.reclaimable)}</span>`
                              : `<span class="tag gray">${T('无需清理')}</span>`}
        </div>
        <div class="desc" style="margin:6px 0 0">${esc(bt4('CLEAN_ITEMS', it.key, 'why', it.why))}</div>
        <div class="row" style="margin:8px 0 0;align-items:center;gap:8px">
          ${it.kind === 'age'
            ? `<label class="desc" style="flex:0 0 auto;margin:0">${T('保留')}
                 <input class="cl-days" data-k="${esc(it.key)}" type="number" min="0" max="3650"
                        value="${esc(Number(it.days || 0))}" style="width:72px"> ${T('天内的文件')}</label>`
            : `<span class="tag gray">${T('由系统工具整体回收')}</span>`}
          <span class="desc" style="flex:1 1 auto;margin:0;text-align:right">
            ${T('轮转件')} ${fmtBytes(it.total)}　${T('命中')} ${it.whole ? T('全部') : (it.hit_count || 0) + T(' 个文件')}${
            it.active ? `<br><span style="color:var(--txt3)">${T('正在写入')} ${fmtBytes(it.active)}${T('（保留不删）')}</span>` : ''}</span>
        </div>
        ${(it.samples || []).length ? `<div class="desc" style="margin:6px 0 0;font-size:12px">${T('例如：')}${
          it.samples.map(s => esc(String(s.path).split('/').pop()) + '（' + fmtBytes(s.size) + '）').join('、')}</div>` : ''}
        <div class="desc" style="margin:6px 0 0;font-size:12px;color:var(--txt3)">${esc(bt4('CLEAN_ITEMS', it.key, 'risk', it.risk))}</div>
      </div>
    </div>`).join('')}</div>`;
}

async function cleanupSave() {
  const items = {};
  document.querySelectorAll('#cl-items .cl-on').forEach(el => {
    items[el.dataset.k] = { on: el.checked };
  });
  document.querySelectorAll('#cl-items .cl-days').forEach(el => {
    items[el.dataset.k] = items[el.dataset.k] || {};
    items[el.dataset.k].days = Number(el.value) || 0;
  });
  const body = {
    op: 'save',
    enabled: $('#cl-enabled').checked,
    trigger: $('#cl-trigger').value,
    disk_percent: Number($('#cl-pct').value) || 85,
    schedule: $('#cl-sched').value,
    // 同上：0 点是合法值，`Number(v) || 4` 会把 00:00 静默改成 04:00
    hour: ($('#cl-hour').value === '' ? 4 : Number($('#cl-hour').value)),
    max_mb_per_run: Number($('#cl-max').value) || 0,
    items: items,
  };
  const r = await api('/api/cleanup', { method: 'POST', body: body });
  toast(r.msg_cn || T('已保存'), r.ok ? 'ok' : 'err', 4200);
  if (r.ok) cleanupLoad(false);
}

async function cleanupRun(dry) {
  const only = [];
  document.querySelectorAll('#cl-items .cl-on').forEach(el => { if (el.checked) only.push(el.dataset.k); });
  const box = $('#cl-result');
  box.classList.remove('hidden');
  box.innerHTML = `<p class="desc">${dry ? T('正在试运行（只读扫描，不删除任何文件）…') : T('正在清理…')}</p>`;
  const r = await api('/api/cleanup', { method: 'POST', body: { op: 'run', dry: !!dry, only: only } });
  if (!r.ok) { box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('执行失败'))}</div>`; return; }
  const d = r.data || {};
  const rows = (d.items || []).map(x => `
    <div class="kv"><b>${esc(x.name)}</b><span>${fmtBytes(x.freed)}${
      x.count ? `　${x.count} ${T('个文件')}` : ''}${
      x.stopped ? `　<span class="tag warn">${T('已达单次上限')}</span>` : ''}${
      (x.errors || []).length ? `　<span class="tag err">${esc(x.errors[0])}</span>` : ''}</span></div>`).join('');
  box.innerHTML = `
    <div class="notice ${d.freed > 0 ? (dry ? 'info' : 'ok') : 'ok'}">
      <b>${esc(r.msg_cn || '')}</b>
    </div>
    <div style="margin-top:10px">${rows || `<p class="desc">${T('没有项目被选中。')}</p>`}</div>`;
  toast(r.msg_cn || T('完成'), 'ok', 5000);
  cleanupLoad(false);
}

/* ======================== 内核转发与加速（#4） ========================
   五项开关的说明与「联动影响」写在后端 KERN_ITEMS / KERN_FW_ITEMS 里，
   前端只负责渲染 —— 免得文案在两处各写一份后走偏。
   注意 MSS 与 masquerade 的落点是 nft_v4 / nft_v6 配置库：本页改完要到
   「防火墙 IPv4 / IPv6」页点一次「保存并应用」才会写进规则集。 */

let KERN_DATA = null;

function kernImpact(list) {
  // 后端下发的中文说明：英文界面走 KERN_IMPACT 表（键=中文原文）
  return (list || []).map(x => `<li>${esc(bt4('KERN_IMPACT', x, 'en', x))}</li>`).join('');
}

function kernCard(it, ctrl, tbl) {
  return `
    <div class="dep-item">
      <span class="dep-ico" style="background:var(--info)">i</span>
      <div class="dep-body" style="flex:1 1 200px">
        <b>${esc(bt4(tbl || 'KERN_ITEMS', it.key, 'name', it.name))}</b>
        <div class="desc" style="margin:6px 0 0">${esc(bt4(tbl || 'KERN_ITEMS', it.key, 'why', it.why))}</div>
        <div style="margin:8px 0 0">${ctrl}</div>
        <details style="margin-top:8px">
          <summary class="desc" style="cursor:pointer;color:var(--pri)">${T('打开')}/${T('关闭会带来什么')}</summary>
          <ul class="desc" style="margin:6px 0 0;padding-left:20px;line-height:1.85">${kernImpact(it.impact)}</ul>
        </details>
      </div>
    </div>`;
}

async function viewKern() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('内核转发与加速')}
        <button class="ghost small" id="kn-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">${T('这五个开关决定「这台机器到底是不是一台路由器」。 每一项都写了')}<b>${T('通俗说明')}</b>${T('和')}<b>${T('联动影响')}</b> —— ${T('点开「打开')}/${T('关闭会带来什么」看清楚再动。')}</p>
      <div id="kn-body"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('出向地址伪装与')} MSS ${T('钳制')}</h3>
      <p class="desc">${T('这两项最终写进防火墙规则集。本页改完后，需要到')}
        <b>${T('「安全')} → ${T('防火墙')} IPv4 / IPv6」</b>${T('点一次「保存并应用」才会真正生效。')}</p>
      <div id="kn-fw"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>SNMP ${T('监控')}</h3>
      <p class="desc">${T('让网管软件能采集本机指标。默认关闭')} —— ${T('开了就多一个')} UDP ${T('监听。')}</p>
      <div id="kn-snmp"><p class="desc">${T('正在读取')}…</p></div>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="kn-save">${T('保存并生效')}</button>
      </div>
      <div id="kn-out" class="hidden" style="margin-top:12px"></div>
    </div>`;
  $('#kn-reload').onclick = () => kernLoad();
  $('#kn-save').onclick = kernSave;
  kernLoad();
}

async function kernLoad() {
  const r = await api('/api/kern', { method: 'POST', body: { op: 'status' } });
  if (!r.ok) {
    $('#kn-body').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  KERN_DATA = r.data;
  kernRender();
}

function kernRender() {
  const d = KERN_DATA || {};
  const c = d.config || {};
  const live = d.live || {};
  const fw = d.fw || {};

  const sw = (id, on) =>
    `<label class="switch"><input type="checkbox" id="${id}"${on ? ' checked' : ''}><i></i></label>`;

  $('#kn-body').innerHTML = `
    ${d.build_mode ? `<div class="notice warn" style="margin-bottom:10px">${T('当前处于')}<b>${T('构建保护模式')}</b>${T('：保存只会写盘，不会修改内核参数、不会启停服务。')}</div>` : ''}
    <div class="dep-grid">
      ${(d.items || []).map(it => {
        const key = it.key;
        if (key === 'snmp') return '';
        const on = c[key] !== undefined ? c[key] : it.default;
        const nowLive = key === 'fwd_v4' ? live.ip_forward
          : key === 'fwd_v6' ? live.ipv6_forwarding
          : (live.congestion === 'bbr' ? '1' : '0');
        const okOn = String(nowLive) === (on ? '1' : '0');
        return kernCard(it, `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            ${sw('kn-' + key, on)}
            <span class="tag ${okOn ? 'ok' : 'warn'}">${
              key === 'bbr'
                ? (live.congestion === 'bbr' ? T('当前 bbr + ') + esc(live.qdisc || '') : T('当前 ') + esc(live.congestion || '—'))
                : (on ? T('已开启') : T('已关闭'))}</span>
            ${key === 'bbr' && on && live.congestion !== 'bbr'
              ? `<span class="tag err">${T('未生效（内核未加载 tcp_bbr）')}</span>` : ''}
          </div>`);
      }).join('')}
    </div>
    <div class="kv" style="margin-top:12px"><b>${T('内核可用拥塞算法')}</b>
      <span class="mono">${esc(live.available || '—')}</span></div>
    <div class="kv"><b>${T('持久化文件')}</b><span class="mono">${esc(d.sysctl_file || '—')}</span></div>`;

  const fwm = {};
  (d.fw_items || []).forEach(it => { fwm[it.key] = it; });
  const mssCtrl = (fam) => {
    const on = fw['mss_' + fam] !== undefined ? fw['mss_' + fam] : (fwm['mss_' + fam] || {}).default;
    const mode = fw['mss_' + fam + '_mode'] || 'clamp';
    const val = fw['mss_' + fam + '_value'] || (fam === 'v4' ? 1452 : 1432);
    return `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        ${sw('kn-mss-' + fam, on)}
        <select id="kn-mssmode-${fam}" style="width:auto">
          <option value="clamp"${mode === 'clamp' ? ' selected' : ''}>${T('按 MTU')} ${T('自动算（推荐）')}</option>
          <option value="fixed"${mode === 'fixed' ? ' selected' : ''}>${T('写死数值')}</option>
        </select>
        <input id="kn-mssval-${fam}" type="number" min="576" max="9000" value="${esc(Number(val))}" style="width:96px">
        <span class="desc" style="margin:0">${T('字节')}</span>
      </div>`;
  };
  const masqCtrl = (fam) => {
    const on = fw['masquerade_' + fam] !== undefined
      ? fw['masquerade_' + fam] : (fwm['masquerade_' + fam] || {}).default;
    return `<div style="display:flex;align-items:center;gap:10px">${sw('kn-masq-' + fam, on)}
        <span class="tag ${on ? 'ok' : 'gray'}">${on ? T('开启') : T('关闭')}</span></div>`;
  };
  $('#kn-fw').innerHTML = `<div class="dep-grid">
      ${kernCard(fwm.masquerade_v4 || {}, masqCtrl('v4'), 'KERN_FW_ITEMS')}
      ${kernCard(fwm.mss_v4 || {}, mssCtrl('v4'), 'KERN_FW_ITEMS')}
      ${kernCard(fwm.masquerade_v6 || {}, masqCtrl('v6'), 'KERN_FW_ITEMS')}
      ${kernCard(fwm.mss_v6 || {}, mssCtrl('v6'), 'KERN_FW_ITEMS')}
    </div>`;

  const s = c.snmp || {};
  const inst = d.snmp_installed;
  $('#kn-snmp').innerHTML = `
    ${inst ? '' : `<div class="notice warn" style="margin-bottom:10px">${T('未安装')} <b>snmpd</b>${T('。请到「系统 → 依赖自检与安装」安装后再回来开启。')}</div>`}
    <div class="row" style="align-items:center">
      <label class="switch"><input type="checkbox" id="kn-snmp-en"${s.enabled ? ' checked' : ''}><i></i>
        <span>${T('启用')} SNMP ${T('服务')}</span></label>
      <span class="tag ${d.snmp_active ? 'ok' : 'gray'}">${d.snmp_active ? T('运行中') : T('未运行')}</span>
    </div>
    <div class="row" style="margin-top:10px">
      <label>${T('团体名（只读口令）')}<input id="kn-snmp-comm" value="${esc(s.community || 'public')}"></label>
      <label style="flex:0 0 130px">${T('端口')}<input id="kn-snmp-port" type="number" min="1" max="65535" value="${esc(Number(s.port || 161))}"></label>
      <label>${T('监听地址（留空＝全部）')}<input id="kn-snmp-listen" value="${esc(s.listen || '')}" placeholder="0.0.0.0"></label>
    </div>
    <div class="row" style="margin-top:10px">
      <label>${T('设备名称')}<input id="kn-snmp-name" value="${esc(s.sysname || '')}"></label>
      <label>${T('位置')}<input id="kn-snmp-loc" value="${esc(s.location || '')}"></label>
      <label>${T('联系人')}<input id="kn-snmp-contact" value="${esc(s.contact || '')}"></label>
    </div>`;
}

async function kernSave() {
  const q = id => document.getElementById(id);
  const body = {
    op: 'save',
    fwd_v4: q('kn-fwd_v4') ? q('kn-fwd_v4').checked : undefined,
    fwd_v6: q('kn-fwd_v6') ? q('kn-fwd_v6').checked : undefined,
    bbr: q('kn-bbr') ? q('kn-bbr').checked : undefined,
    fw: {
      masquerade_v4: q('kn-masq-v4') ? q('kn-masq-v4').checked : undefined,
      masquerade_v6: q('kn-masq-v6') ? q('kn-masq-v6').checked : undefined,
      mss_v4: q('kn-mss-v4') ? q('kn-mss-v4').checked : undefined,
      mss_v6: q('kn-mss-v6') ? q('kn-mss-v6').checked : undefined,
      mss_v4_mode: q('kn-mssmode-v4') ? q('kn-mssmode-v4').value : undefined,
      mss_v6_mode: q('kn-mssmode-v6') ? q('kn-mssmode-v6').value : undefined,
      mss_v4_value: q('kn-mssval-v4') ? Number(q('kn-mssval-v4').value) : undefined,
      mss_v6_value: q('kn-mssval-v6') ? Number(q('kn-mssval-v6').value) : undefined,
    },
    snmp: {
      enabled: q('kn-snmp-en') ? q('kn-snmp-en').checked : undefined,
      community: q('kn-snmp-comm') ? q('kn-snmp-comm').value : undefined,
      port: q('kn-snmp-port') ? Number(q('kn-snmp-port').value) : undefined,
      listen: q('kn-snmp-listen') ? q('kn-snmp-listen').value : undefined,
      sysname: q('kn-snmp-name') ? q('kn-snmp-name').value : undefined,
      location: q('kn-snmp-loc') ? q('kn-snmp-loc').value : undefined,
      contact: q('kn-snmp-contact') ? q('kn-snmp-contact').value : undefined,
    },
  };
  const r = await api('/api/kern', { method: 'POST', body: body });
  const box = $('#kn-out');
  box.classList.remove('hidden');
  const errs = ((r.data || {}).errors) || [];
  box.innerHTML = `<div class="notice ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</div>
    ${errs.length ? `<ul class="desc" style="margin:8px 0 0;padding-left:20px">${
      errs.map(e => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}`;
  toast(r.msg_cn || T('已保存'), r.ok ? 'ok' : 'err', 5000);
  if (r.ok) kernLoad();
}

/* ==================== 新手向导（上网四步走） ====================
   放在「概览」组第一项：这是第一次使用这台机器的人唯一需要先看的东西。
   设计取向与其余页面不同 —— 别的页面默认「你已经知道自己在干什么」，
   这个页面默认「你什么都不知道」。所以：
     * 每一步的说明都先讲「这是干什么的」，再讲「怎么填」；
     * 每个输入框旁边给一个可以直接抄的范例，而不是只给字段名；
     * 体检结论用「能不能上网」的说法，不用「接口状态 / 路由表」这类词；
     * IPv6 明确标成可选 —— 不通不影响上网，不该让新手以为卡住了。
   全部文案尽量口语化，避免「DHCP 服务未启用」这种只有同行看得懂的句子。 */

let WIZ = null;
let WIZ_STEP = 0;

async function viewWizard() {
  $('#view').innerHTML = `<div class="card"><p class="desc">${T('正在给这台机器做体检…')}</p></div>`;
  await wizLoad();
}

async function wizLoad() {
  const r = await api('/api/wizard', { method: 'POST', body: { op: 'probe' } });
  if (!r.ok) {
    $('#view').innerHTML = `<div class="card"><div class="notice err">${
      esc(r.msg_cn || T('体检失败，请稍后重试'))}</div></div>`;
    return;
  }
  WIZ = r.data;
  wizRender();
}

/* 向导专用：后端下发的中文枚举值 -> 英文。
 * 走 T() 而不是 bt4，因为它们是内联 dict 不是 bt 表；
 * T() 的 key 就是后端原文，查不到就回落原文（中文界面无感）。*/
function wizDnsMode(mode, fallback) {
  if (mode === 'isp') return T('仅运营商下发');
  if (mode === 'custom') return T('仅自定义');
  if (mode === 'both') return T('两者合并');
  return fallback || '\u2014';
}
/* IPv6 建议：后端给的是中文原文。同样只用于判别，不直接上屏。*/
const WIZ_V6_ADVICE = { on: '建议开启', off: '可以先不开' };

function wizAdvice(a) {
  if (a === WIZ_V6_ADVICE.on) return T('建议开启');
  if (a === WIZ_V6_ADVICE.off) return T('可以先不开');
  return a || '';
}

/* 地址池问题：后端是「整句中文 + %s 填值」，动态拼出来的字符串
 * 永远不可能是 T() 的 key（MEMORY 里记的「动态 key 查不到」）。
 * 正解：按语义归类 → 用固定 key 的词条 + 自己填变量。
 * ⚠️ 后端新增同类文案时这里要同步加分支，否则英文界面回落中文。
 * 注意 lan_ip 必须显式传进来 —— 这些helper 是模块级函数，拿不到闭包里的 l。*/
/* 地址池问题的**判别前缀**（后端原文的前几个字，用来认出是哪一类问题）。
 * ⛔ 它们不是 UI 文案，不要包 T() —— 一包就变成译文比对，逻辑全坏。
 * 真正的翻译 key 在下面的返回值里，与这里的前缀一一对应。*/
const WIZ_POOL_PREFIX = {
  noIp:    '还没给 LAN 口配 IP 地址',
  start:   '地址池起始地址（',
  end:     '地址池结束地址（',
  clash:   '地址池把本机自己的地址（',
};

function wizPoolProblem(x, lanIp) {
  const t = String(x || '');
  const arg = (t.match(/\uff08(.+?)\uff09/) || [])[1] || '';
  const fmt = (s) => String(s).replace(/\{(\d)\}/g, (m, i) => (i === '0' ? arg : (lanIp || '')));
  if (t.startsWith(WIZ_POOL_PREFIX.noIp))
    return `<li>${T('还没给 LAN 口配 IP 地址')}</li>`;
  if (t.startsWith(WIZ_POOL_PREFIX.start))
    return `<li>${fmt(T('地址池起始地址（{0}）和本机 LAN 地址（{1}）不在同一个网段'))}</li>`;
  if (t.startsWith(WIZ_POOL_PREFIX.end))
    return `<li>${fmt(T('地址池结束地址（{0}）和本机 LAN 地址（{1}）不在同一个网段'))}</li>`;
  if (t.startsWith(WIZ_POOL_PREFIX.clash))
    return `<li>${fmt(T('地址池把本机自己的地址（{0}）也包括进去了，会和本机抢 IP'))}</li>`;
  return `<li>${esc(t)}</li>`;
}

/* DNS 探测结论：后端整句中文，同属动态串。
 * 前缀常量与上面 WIZ_POOL_PREFIX 同理 —— 只用于 startsWith() 判别，不上屏。*/
const WIZ_DNS_NOTE_PREFIX = { ispWaiting: '选了「跟随运营商」' };

function wizDnsNote(x) {
  const t = String(x || '');
  if (t.startsWith(WIZ_DNS_NOTE_PREFIX.ispWaiting))
    return T('选了「跟随运营商」，但当前还没拿到运营商下发的 DNS（多半是还没拨号成功）');
  return t;
}

function wizStepTag(done, optional) {
  if (done) return `<span class="tag ok">${T('已就绪')}</span>`;
  if (optional) return `<span class="tag gray">${T('可选')}</span>`;
  return `<span class="tag warn">${T('待设置')}</span>`;
}

function wizRender() {
  const d = WIZ || {};
  const w = d.wan || {}, l = d.lan || {}, n = d.dns || {}, v = d.v6 || {};

  // ---- 顶部：一句话结论 ----
  const headline = d.internet_ok
    ? T('这台机器已经能把内网接到外网了。下面的步骤可以随时回来复查或调整。')
    : T('还差几步就能上网。按下面的顺序一步步来，每步做完都能看到结果。');
  const hcls = d.internet_ok ? 'ok' : 'warn';

  // ---- 步骤条 ----
  const steps = d.steps || [];
  const stateOf = k => k === 'wan' ? !!w.online : k === 'lan' ? !!l.ok
    : k === 'dns' ? !!n.ok : !!v.ok;
  const stepsBar = steps.map((s, i) => {
    const ok = stateOf(s.k);
    const cur = i === WIZ_STEP;
    return `<button class="wiz-step ${ok ? 'ok' : ''} ${cur ? 'on' : ''}" data-i="${i}">
      <span class="wiz-step-ico">${ok ? '✓' : (i + 1)}</span>
      <span class="wiz-step-n">${esc(bt4('WIZ_STEPS', s.k, 'n', s.n))}</span>
    </button>`;
  }).join('<span class="wiz-arrow">›</span>');

  $('#view').innerHTML = `
    ${d.build_mode ? `<div class="notice warn" style="margin-bottom:12px">
      <b>${T('当前处于构建保护模式')}</b>${T('：向导会把配置写到磁盘，但不会重启服务、 不会真正拨号。等你关闭保护模式后再点一次「保存并生效」即可。')}</div>` : ''}

    <div class="card" style="border-color:${hcls === 'ok' ? '#bfe3c4' : '#f0e0a8'}">
      <h3>${T('新手向导')}
        <span class="tag ${hcls}">${d.internet_ok ? T('可以上网了') : T('还差 ') + (d.todo || []).filter(x => !x.includes('可选')).length + T(' 步')}</span>
        <button class="ghost small fixed" id="wz-reload" style="float:right">${T('重新体检')}</button></h3>
      <p class="desc" style="margin-bottom:14px">${esc(headline)}</p>
      <div class="wiz-steps">${stepsBar}</div>
      ${(d.todo || []).length ? `<div class="hint-inline" style="margin-top:12px">
        ${T('还没完成的：')}${(d.todo || []).map(x => esc(T(x))).join(' · ')}</div>` : ''}
    </div>

    <div id="wz-body"></div>`;

  $('#wz-reload').onclick = () => wizLoad();
  $$('.wiz-step').forEach(b => b.onclick = () => {
    WIZ_STEP = Number(b.dataset.i);
    wizRender();
  });

  wizRenderStep();
}

function wizRenderStep() {
  const d = WIZ || {};
  const box = $('#wz-body');
  if (!box) return;
  const k = ((d.steps || [])[WIZ_STEP] || {}).k;
  if (k === 'wan') box.innerHTML = wizStepWan();
  else if (k === 'lan') box.innerHTML = wizStepLan();
  else if (k === 'dns') box.innerHTML = wizStepDns();
  else if (k === 'v6') box.innerHTML = wizStepV6();
  wizBindStep(k);
}

/* ---------- 第 1 步：接上外网 ---------- */
function wizStepWan() {
  const d = WIZ || {}, w = d.wan || {};
  const modes = d.wan_modes || [];
  const cur = w.mode || 'pppoe';
  return `
    <div class="card">
      <h3>${T('第 1 步')} · ${T('接上外网')} ${wizStepTag(w.online)}</h3>
      <p class="desc">${T('先告诉这台机器「宽带从哪里进来」。这一步做完，本机自己就能上网； 要让内网设备也上，还要做完第')} 2 ${T('步。')}</p>

      <div class="kv"><b>${T('上网方式')}</b><span>${esc(bt4('WIZ_WAN_MODES', w.mode, 'n', w.mode_cn) || T('还没设置'))}</span></div>
      <div class="kv"><b>${T('接宽带的网卡')}</b><span class="mono">${esc(w.iface || T('还没指定'))}</span></div>
      <div class="kv"><b>${T('默认路由')}</b><span class="mono">${esc(w.default_route || T('还没有（说明还没连上外网）'))}</span></div>
      <div class="kv"><b>${T('能 ping')} ${T('通外网')}</b><span>${w.online
        ? `<span class="tag ok">${T('可以')}</span>`
        : `<span class="tag err">${T('不通')}</span>`}</span></div>
      ${w.ping_note ? `<div class="hint-inline">${T('最近一次探测：')}${esc(w.ping_note)}</div>` : ''}

      <h4 style="margin:18px 0 8px">${T('你的宽带是哪一种？')}</h4>
      <div class="seg" id="wz-wan-mode">
        ${modes.map(m => `<button data-m="${m.k}" class="${cur === m.k ? 'on' : ''}">${esc(bt4('WIZ_WAN_MODES', m.k, 'n', m.n))}</button>`).join('')}
      </div>
      <div id="wz-wan-desc" style="margin-top:12px"></div>

      <div id="wz-wan-form" style="margin-top:14px"></div>

      <div class="row" style="margin-top:16px">
        <button class="primary fixed" id="wz-wan-save">${T('保存并生效')}</button>
        ${cur === 'pppoe' ? `<button class="ghost fixed" id="wz-wan-dial">${T('立即拨号测试')}</button>` : ''}
      </div>
      <div id="wz-wan-out" class="hidden" style="margin-top:12px"></div>
    </div>`;
}

function wizWanForm(mode) {
  const d = WIZ || {}, w = d.wan || {};
  const ifaces = d.wan_ifaces || [];
  const ifOpts = (sel) => [`<option value="">${T('请选择网卡')}</option>`]
    .concat(ifaces.map(i => `<option value="${esc(i.name)}"${sel === i.name ? ' selected' : ''}>${
      esc(i.name)}（${esc(i.mac)}）${i.state === 'UP' ? T(' 已连接') : T(' 未连接')}</option>`)).join('');
  const iface = w.iface || (ifaces[0] || {}).name || '';

  if (mode === 'pppoe') {
    return `
      <div class="row">
        <label>${T('接宽带的网卡')}<select id="wz-iface">${ifOpts(iface)}</select></label>
        <label>${T('运营商')}<select id="wz-isp">
          <option value="auto">${T('自动')} / ${T('通用')}</option>
          <option value="ct">${T('中国电信')}</option>
          <option value="cu">${T('中国联通')}</option>
          <option value="cm">${T('中国移动')}</option>
          <option value="cbn">${T('中国广电')}</option>
        </select></label>
      </div>
      <div class="row">
        <label>${T('宽带账号')}<input id="wz-user" placeholder="${T('范例：')}051212345678 ${T('或 13800000000')}" autocomplete="off"></label>
        <label>${T('宽带密码')}<input id="wz-pass" type="password" placeholder="${T('办理宽带时给的密码')}" autocomplete="new-password"></label>
      </div>
      <p class="hint-inline"><b>${T('范例说明：')}</b>${T('账号通常是你办理宽带时的「宽带账号」或手机号， 电信多为')} <span class="mono">0512</span> ${T('开头的区号加号码； 密码不是')} Wi-Fi ${T('密码，是宽带密码，忘了就打运营商客服重置。 不确定的话先按上面格式填，保存后再点「立即拨号测试」看结果。')}</p>`;
  }
  if (mode === 'dhcp') {
    return `
      <label>${T('接宽带的网卡')}<select id="wz-iface">${ifOpts(iface)}</select></label>
      <p class="hint-inline"><b>${T('范例说明：')}</b>${T('这种方式不需要账号密码。 把网线从光猫的')} LAN ${T('口接到这台机器的这张网卡上就行。 光猫要是已经在拨号（大多数家庭就是这样），选这个最省事。')}</p>`;
  }
  return `
    <label>${T('接宽带的网卡')}<select id="wz-iface">${ifOpts(iface)}</select></label>
    <div class="row" style="margin-top:10px">
      <label>IP ${T('地址')} / ${T('掩码')}<input id="wz-addr" placeholder="${T('范例：')}192.168.1.2/24"></label>
      <label>${T('网关')}<input id="wz-gw" placeholder="${T('范例：')}192.168.1.1"></label>
    </div>
    <label>DNS<input id="wz-sdns" placeholder="${T('范例：')}223.5.5.5,223.6.6.6"></label>
    <p class="hint-inline"><b>${T('范例说明：')}</b>${T('这三个值运营商都会在开通单上写给你， 照抄即可。')}<span class="mono">/24</span> ${T('表示掩码是')} 255.255.255.0${T('，不用改。')}</p>`;
}

/* ---------- 第 2 步：给内网发地址 ---------- */
function wizStepLan() {
  const d = WIZ || {}, l = d.lan || {}, h = d.pool_hint || {};
  const lan_ifaces = d.lan_ifaces || [];
  const ifOpts = [`<option value="">${T('请选择网卡')}</option>`].concat(
    lan_ifaces.map(i => `<option value="${esc(i.name)}"${l.iface === i.name ? ' selected' : ''}>${
      esc(i.name)}（${esc(i.mac)}）</option>`)).join('');
  const lanIp = l.lan_ip || '';
  const base = lanIp ? lanIp.slice(0, lanIp.lastIndexOf('.')) : '192.168.7';
  return `
    <div class="card">
      <h3>${T('第 2 步')} · ${T('给内网发地址')} ${wizStepTag(l.ok)}</h3>
      <p class="desc">${T('手机、电脑连上来之后要自动拿到')} IP ${T('地址、网关和')} DNS${T('， 否则会出现「连上了')} Wi-Fi ${T('但打不开网页」。这个功能就叫')} DHCP。</p>

      <div class="kv"><b>${T('本机内网地址')}</b><span class="mono">${esc(lanIp || T('还没设置'))}</span></div>
      <div class="kv"><b>DHCP ${T('服务')}</b><span>${l.dhcp_enabled
        ? `<span class="tag ok">${T('已开启')}</span>` : `<span class="tag warn">${T('未开启')}</span>`}</span>
        ${l.dnsmasq_active ? `<span class="tag ok">${T('服务运行中')}</span>` : `<span class="tag err">${T('服务未运行')}</span>`}</div>
      <div class="kv"><b>${T('当前地址池')}</b><span class="mono">${esc(l.pool_start || '—')} ~ ${esc(l.pool_end || '—')}</span></div>
      ${(l.pool_problems || []).length ? `<div class="notice err" style="margin-top:10px">
        <b>${T('当前地址池有问题：')}</b><ul style="margin:6px 0 0 18px">${
        (l.pool_problems || []).map(x => wizPoolProblem(x, lanIp)).join('')}</ul></div>` : ''}

      <h4 style="margin:18px 0 8px">${T('填写地址池')}</h4>
      <label>${T('内网网卡')}<select id="wz-lan-iface">${ifOpts}</select></label>
      <div class="row" style="margin-top:10px">
        <label>${T('起始地址')}<input id="wz-ps" value="${esc(l.pool_start || base + '.100')}"></label>
        <label>${T('结束地址')}<input id="wz-pe" value="${esc(l.pool_end || base + '.200')}"></label>
      </div>
      <div class="row">
        <label>${T('子网掩码')}<input id="wz-pm" value="${esc(l.pool_netmask || '255.255.255.0')}"></label>
        <label>${T('租期（秒）')}<input id="wz-lt" type="number" value="${esc(Number(l.lease_time || 7200))}"></label>
      </div>
      <div class="row">
        <label>${T('下发给设备的网关')}<input id="wz-og" value="${esc(l.gateway || lanIp)}"></label>
        <label>${T('下发给设备的')} DNS<input id="wz-od" value="${esc(l.dns_option || lanIp)}"></label>
      </div>

      <div class="notice" style="margin-top:12px;background:var(--info-l);border-color:#cfe0f5;color:#1b4f8a">
        <b>${T('地址池怎么填？（照着抄就行）')}</b>
        <ul style="margin:8px 0 0 18px;line-height:1.9">
          ${(h.rules || []).map(x => `<li>${esc(T(x))}</li>`).join('')}
        </ul>
        <div style="margin-top:8px">${T('本机内网地址是')} <span class="mono">${esc(lanIp || '—')}</span>${T('， 所以地址池用')} <span class="mono">${esc(base)}.100</span> ~
          <span class="mono">${esc(base)}.200</span> ${T('最稳妥。')}</div>
      </div>

      <div class="row" style="margin-top:16px">
        <button class="primary fixed" id="wz-lan-save">${T('保存并生效')}</button>
      </div>
      <div id="wz-lan-out" class="hidden" style="margin-top:12px"></div>
    </div>`;
}

/* ---------- 第 3 步：能打开网址（DNS） ---------- */
function wizStepDns() {
  const d = WIZ || {}, n = d.dns || {};
  const presets = n.presets || [];
  const curMode = n.mode || 'custom';
  return `
    <div class="card">
      <h3>${T('第 3 步')} · ${T('能打开网址')} ${wizStepTag(n.ok)}</h3>
      <p class="desc">${T('你在浏览器里输入的是网址（比如')} www.baidu.com${T('）， 但机器之间只认')} IP ${T('地址。把它们翻译过来的服务就叫')} DNS${T('。 这一步决定用谁的')} DNS。</p>

      <div class="kv"><b>${T('当前来源')}</b><span>${esc(wizDnsMode(n.mode, n.mode_cn))}</span></div>
      <div class="kv"><b>${T('正在用的')} DNS</b><span class="mono">${esc(n.first || T('还没配'))}</span></div>
      <div class="kv"><b>${T('解析测试')}</b><span>${n.probe_ok
        ? `<span class="tag ok">${T('正常')}</span>` : `<span class="tag warn">${T('有问题')}</span>`}</span></div>
      ${n.probe_note ? `<div class="hint-inline">${esc(wizDnsNote(n.probe_note))}</div>` : ''}

      <h4 style="margin:18px 0 8px">${T('选一个')} DNS ${T('来源')}</h4>
      <div class="seg" id="wz-dns-mode">
        <button data-m="custom" class="${curMode === 'custom' ? 'on' : ''}">${T('用公共')} DNS</button>
        <button data-m="isp" class="${curMode === 'isp' ? 'on' : ''}">${T('跟随运营商下发')}</button>
        <button data-m="both" class="${curMode === 'both' ? 'on' : ''}">${T('两者合并')}</button>
      </div>

      <div id="wz-dns-custom" style="margin-top:14px">
        <label>${T('选择服务商')}</label>
        <div class="wiz-dns-grid">
          ${presets.map(p => `<button class="wiz-dns ${p.k === 'isp' ? 'wiz-dns-isp' : ''}" data-v="${esc(p.v4)}">
            <b>${esc(bt4('WIZ_DNS_PRESETS', p.k, 'n', p.n))}</b>
            <span class="mono">${esc(p.v4 || T('拨号时自动获取'))}</span>
            <span class="wiz-dns-d">${esc(bt4('WIZ_DNS_PRESETS', p.k, 'd', p.d))}</span>
          </button>`).join('')}
        </div>
        <label style="margin-top:12px">${T('也可以自己填（多个之间用英文逗号分隔）')}
          <input id="wz-dns-custom" value="${esc(n.custom || n.default_custom || '')}"
            placeholder="${T('范例：')}223.5.5.5,119.29.29.29"></label>
        <p class="hint-inline"><b>${T('范例说明：')}</b>${T('上面几个都是可以直接用的公共')} DNS。
          <span class="mono">223.5.5.5</span> ${T('是阿里云的，国内速度快、出错少， 不知道选哪个就用它。填两个地址的作用是一个不通时自动换另一个。')}</p>
      </div>

      <div class="row" style="margin-top:16px">
        <button class="primary fixed" id="wz-dns-save">${T('保存并生效')}</button>
      </div>
      <div id="wz-dns-out" class="hidden" style="margin-top:12px"></div>
    </div>`;
}

/* ---------- 第 4 步：IPv6（可选） ---------- */
function wizStepV6() {
  const d = WIZ || {}, v = d.v6 || {};
  const lan_ifaces = d.lan_ifaces || [];
  const ifOpts = [`<option value="">${T('请选择网卡')}</option>`].concat(
    lan_ifaces.map(i => `<option value="${esc(i.name)}"${v.lan_iface === i.name ? ' selected' : ''}>${
      esc(i.name)}（${esc(i.mac)}）</option>`)).join('');
  return `
    <div class="card">
      <h3>${T('第 4 步')} · ${T('要不要用')} IPv6 ${wizStepTag(v.ok, true)}</h3>
      <p class="desc">IPv6 ${T('是新一代的网络地址。国内宽带基本都支持了。')}
        <b>${T('不开也不影响上网')}</b>${T('，只是有些网站和服务用')} IPv6 ${T('会更快、更直接。 拿不准就开着，出问题的概率很低。')}</p>

      <div class="kv"><b>${T('宽带那边')}</b><span>${v.wan_has_global
        ? `<span class="tag ok">${T('运营商已下发 IPv6')}</span>`
        : `<span class="tag gray">${T('还没拿到 IPv6 地址')}</span>`}</span></div>
      <div class="kv"><b>${T('内网这边')}</b><span>${v.lan_has_global
        ? `<span class="tag ok">${T('已在用公网 IPv6')}</span>`
        : `<span class="tag gray">${T('还没启用')}</span>`}</span></div>
      <div class="kv"><b>${T('路由通告服务')}</b><span>${v.radvd_active
        ? `<span class="tag ok">${T('运行中')}</span>`
        : (v.radvd_installed ? `<span class="tag warn">${T('已安装未运行')}</span>`
          : `<span class="tag err">${T('未安装')}</span>`)}</span></div>
      <div class="kv"><b>${T('建议')}</b><span>${esc(wizAdvice(v.advice))}</span></div>

      <div class="notice ${v.wan_has_global ? 'ok' : 'warn'}" style="margin-top:12px">
        ${v.wan_has_global
          ? T('运营商已经给你的宽带下发了 IPv6 地址，可以放心打开。')
          : `${T('目前还没从运营商那里拿到 IPv6 地址。')}<b>${T('打开也不会坏')}</b>，`
            + T('只是暂时不生效；等运营商下发后它会自动开始工作。')}
      </div>

      <div class="row" style="margin-top:16px;align-items:center">
        <label class="switch"><input type="checkbox" id="wz-v6-en"${v.ok || v.radvd_active ? ' checked' : ''}><i></i>
          <span>${T('在内网启用')} IPv6</span></label>
      </div>

      <div id="wz-v6-form" style="margin-top:12px">
        <label>${T('内网网卡')}<select id="wz-v6-iface">${ifOpts}</select></label>
        <div class="row" style="margin-top:10px">
          <label>${T('内网')} IPv6 ${T('前缀（可留空）')}<input id="wz-v6-prefix" value="${esc(v.prefix || '')}"
            placeholder="${T('范例：')}2408:8207:1234:5678::/64"></label>
          <label>IPv6 DNS（RDNSS）<input id="wz-v6-rdnss" value="${esc(v.rdnss || '2400:3200::1')}"
            placeholder="${T('范例：')}2400:3200::1"></label>
        </div>
        <p class="hint-inline"><b>${T('范例说明：')}</b>${T('前缀留空即可')} —— ${T('大多数家庭宽带会在拨号后 自动把运营商的前缀交给内网用。要手工填的话， 运营商给的前缀一般是')} <span class="mono">/56</span> ${T('或')} <span class="mono">/60</span>${T('， 这里填')} <span class="mono">/64</span> ${T('那一段（比如把它写成')}
          <span class="mono">2408:8207:1234:5678::/64</span>${T('）。 内网通告只能用')} /64${T('，填别的位数手机和电脑会直接忽略。')}</p>
      </div>

      <div class="row" style="margin-top:16px">
        <button class="primary fixed" id="wz-v6-save">${T('保存并生效')}</button>
      </div>
      <div id="wz-v6-out" class="hidden" style="margin-top:12px"></div>
    </div>`;
}

/* ---------- 交互绑定 ---------- */
function wizBindStep(k) {
  const d = WIZ || {};
  if (k === 'wan') {
    let mode = (d.wan || {}).mode || 'pppoe';
    const form = $('#wz-wan-form'), desc = $('#wz-wan-desc');
    const paint = () => {
      const m = (d.wan_modes || []).find(x => x.k === mode) || {};
      desc.innerHTML = `<div class="notice" style="margin:0"><b>${esc(bt4('WIZ_WAN_MODES', m.k, 'n', m.n))}</b> —— ${esc(bt4('WIZ_WAN_MODES', m.k, 'd', m.d))}
        <div style="margin-top:6px;color:var(--txt2)">${T('什么时候选它：')}${esc(bt4('WIZ_WAN_MODES', m.k, 'when', m.when))}</div></div>`;
      form.innerHTML = wizWanForm(mode);
    };
    $$('#wz-wan-mode button').forEach(b => b.onclick = () => {
      $$('#wz-wan-mode button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      mode = b.dataset.m;
      paint();
      // 拨号按钮只在 PPPoE 下出现：换了方式要重画那一行
      const row = $('#wz-wan-save').parentElement;
      const old = $('#wz-wan-dial');
      if (mode === 'pppoe' && !old) {
        const btn = document.createElement('button');
        btn.className = 'ghost fixed'; btn.id = 'wz-wan-dial'; btn.textContent = T('立即拨号测试');
        row.appendChild(btn); wizBindDial();
      } else if (mode !== 'pppoe' && old) {
        old.remove();
      }
    });
    paint();
    wizBindDial();
    $('#wz-wan-save').onclick = () => wizApplyWan(mode);
  } else if (k === 'lan') {
    $('#wz-lan-save').onclick = wizApplyLan;
  } else if (k === 'dns') {
    const custom = $('#wz-dns-custom');
    $$('#wz-dns-mode button').forEach(b => b.onclick = () => {
      $$('#wz-dns-mode button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      const box = $('#wz-dns-custom').parentElement;
      box.style.opacity = (b.dataset.m === 'isp') ? '.45' : '1';
    });
    $$('.wiz-dns').forEach(b => b.onclick = () => {
      if (b.classList.contains('wiz-dns-isp')) {
        const ispBtn = $$('#wz-dns-mode button').find(x => x.dataset.m === 'isp');
        if (ispBtn) ispBtn.click();
        return;
      }
      if (custom) custom.value = b.dataset.v;
      const cBtn = $$('#wz-dns-mode button').find(x => x.dataset.m === 'custom');
      if (cBtn) cBtn.click();
      // click() 会把 custom 框变暗，这里立刻点回 custom 态再填值
      if (custom) { custom.value = b.dataset.v; custom.parentElement.style.opacity = '1'; }
    });
    $('#wz-dns-save').onclick = wizApplyDns;
  } else if (k === 'v6') {
    const en = $('#wz-v6-en'), form = $('#wz-v6-form');
    const paint = () => { form.style.opacity = en.checked ? '1' : '.45';
      form.style.pointerEvents = en.checked ? '' : 'none'; };
    en.onchange = paint;
    paint();
    $('#wz-v6-save').onclick = wizApplyV6;
  }
}

function wizBindDial() {
  const btn = $('#wz-wan-dial');
  if (!btn) return;
  btn.onclick = () => {
    modal(T('现在拨号试试？'),
      `<p>${T('这会真的向运营商发起一次拨号。如果这台机器正在承担现有网络， 拨号过程会让网络短暂中断几十秒。')}</p>
       <p>${T('只是保存账号密码的话，')}<b>${T('不用点这个按钮')}</b> —— ${T('点上面的「保存并生效」就够了。')}</p>
       <p>${T('确认现在拨号，请输入')} <b>${T('拨号')}</b>：</p><input id="wz-cfm" placeholder="${T('拨号')}">`,
      async () => {
        if ($('#wz-cfm').value.trim() !== T('拨号')) { toast(T('确认文字不正确'), 'err'); return false; }
        wizOut('wz-wan-out', `<div class="notice">${T('正在拨号，通常需要 5–20 秒…')}</div>`);
        const r = await api('/api/wizard', { method: 'POST', body: { op: 'dial', action: 'connect', confirm: true } });
        wizOut('wz-wan-out', r.ok
          ? `<div class="notice ok">${esc(r.msg_cn || T('拨号指令已发出'))}</div>`
          : `<div class="notice err">${esc(r.msg_cn || T('拨号失败'))}</div>`);
        setTimeout(wizLoad, 4000);
      });
  };
}

function wizOut(id, html, reload) {
  const box = document.getElementById(id);
  if (!box) return;
  box.classList.remove('hidden');
  box.innerHTML = html;
  if (reload) setTimeout(wizLoad, 2500);
}

function wizCollect(id) { const e = document.getElementById(id); return e ? e.value.trim() : ''; }
function wizChecked(id) { const e = document.getElementById(id); return e ? e.checked : false; }

async function wizSubmit(step, body, outId) {
  wizOut(outId, `<div class="notice">${T('正在保存并生效…')}</div>`);
  const r = await api('/api/wizard', { method: 'POST', body: Object.assign({ op: 'apply_step', step: step }, body) });
  const dd = r.data || {};
  const errs = dd.errors || [], notes = dd.notes || [], done = dd.done || [];
  let html = `<div class="notice ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</div>`;
  if (done.length || notes.length) {
    html += `<ul class="desc" style="margin:8px 0 0;padding-left:20px;line-height:1.9">${
      done.concat(notes).map(x => `<li>${esc(x)}</li>`).join('')}</ul>`;
  }
  if (errs.length) {
    html += `<div class="notice err" style="margin-top:8px">${T('未完成：')}<ul style="margin:6px 0 0 18px">${
      errs.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>`;
  }
  wizOut(outId, html);
  toast(r.ok ? T('这一步已完成') : T('这一步没有全部成功'), r.ok ? 'ok' : 'err', 6000);
  if (r.ok || done.length) setTimeout(wizLoad, 2200);
}

function wizApplyWan(mode) {
  const body = {
    mode: mode,
    iface: wizCollect('wz-iface'),
    username: wizCollect('wz-user'),
    password: wizCollect('wz-pass'),
    isp: wizCollect('wz-isp'),
    static_address: wizCollect('wz-addr'),
    static_gateway: wizCollect('wz-gw'),
    static_dns: wizCollect('wz-sdns'),
  };
  if (!body.iface) { toast(T('请先选择接宽带的网卡'), 'err'); return; }
  wizSubmit('wan', body, 'wz-wan-out');
}

function wizApplyLan() {
  wizSubmit('lan', {
    iface: wizCollect('wz-lan-iface'),
    pool_start: wizCollect('wz-ps'),
    pool_end: wizCollect('wz-pe'),
    pool_netmask: wizCollect('wz-pm'),
    lease_time: wizCollect('wz-lt'),
    option_gateway: wizCollect('wz-og'),
    option_dns: wizCollect('wz-od'),
  }, 'wz-lan-out');
}

function wizApplyDns() {
  const modeBtn = $$('#wz-dns-mode button').find(x => x.classList.contains('on'));
  const mode = modeBtn ? modeBtn.dataset.m : 'custom';
  wizSubmit('dns', { mode: mode, custom: wizCollect('wz-dns-custom') }, 'wz-dns-out');
}

function wizApplyV6() {
  wizSubmit('v6', {
    enable: wizChecked('wz-v6-en'),
    lan_iface: wizCollect('wz-v6-iface'),
    prefix: wizCollect('wz-v6-prefix'),
    rdnss: wizCollect('wz-v6-rdnss'),
  }, 'wz-v6-out');
}

/* ============================ 动态域名 DDNS（#6） ============================ */
/* ⚠️ 下面这些中文**故意不包 t()**：它们是用来比对**后端下发的中文原文**
   （DDNS 服务商的 region 字段就是「国内 / 国际 / 通用」）。包了 t() 的话，
   英文界面下 T 返回英文，这里就永远匹配不上 → 标签配色全部失效。
   判据见 t-i18n-cover：行尾带 `i18n-keep-cn` 的字面量豁免。 */
const DDNS_REGION_TAG = r => (r === '国内' ? 'ok' : (r === '国际' ? 'info' : 'gray')); // i18n-keep-cn

async function viewDdns() {
  const r = await api('/api/ddns');
  const d = r.data || {};
  // 有未保存草稿时，表单字段用草稿渲染；公网检测等状态区仍用实时数据。
  // 草稿存活于 S.ddnsDirty，合并进 c 即可让下面的模板原样工作。
  const draft = (pageIsDirty('ddns') && S.ddnsDirty) ? S.ddnsDirty : null;
  const c = draft ? Object.assign({}, d.cfg || {}, draft) : (d.cfg || {});
  const pub = d.public || {};
  const note = d.note || {};
  S.ddns = d;
  const provs = d.providers || [];
  const prov = provs.find(x => x.v === c.provider) || provs[0] || {};
  const isCustom = c.provider === 'custom';

  // 厂商按地区分组，便于国内用户快速找到
  const byRegion = {};
  provs.forEach(p => { (byRegion[p.region] = byRegion[p.region] || []).push(p); });

  const noteCls = note.level === 'ok' ? 'ok' : (note.level === 'err' ? 'err' : 'warn');
  const comboTag = { both: 'ok', v4only: 'info', v6only: 'warn', neither: 'err' }[pub.combo] || 'gray';

  $('#view').innerHTML = `
    <div class="card ${noteCls === 'err' ? '' : ''}" style="border-color:${
      noteCls === 'ok' ? '#bfe3c4' : (noteCls === 'err' ? '#f0c8c3' : '#f0e0a8')}">
      <h3>${T('公网能力检测')}
        <span class="tag ${comboTag}">${esc(enOrZh(note.en_title, note.title) || T('检测中…'))}</span>
        <button class="ghost small fixed" id="dd-retest" style="float:right">${T('重新检测')}</button></h3>
      <p class="desc">${esc(enOrZh(note.en_conclusion, note.conclusion) || '')}</p>
      <div class="grid2" style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px">
        <div class="kv"><b>IPv4 ${T('出口地址')}</b><span class="mono">${esc(pub.v4_local || '—')}
          ${pub.v4_has ? `<span class="tag ok">${T('公网')}</span>` : (pub.v4_nat ? `<span class="tag warn">NAT ${T('后')}</span>` : `<span class="tag gray">${T('未获取')}</span>`)}</span></div>
        <div class="kv"><b>IPv4 ${T('回显地址')}</b><span class="mono">${esc(pub.v4_public || '—')}
          ${pub.v4_source ? '<span class="tag gray">' + esc(pub.v4_source) + '</span>' : ''}</span></div>
        <div class="kv"><b>IPv6 ${T('全局地址')}</b><span class="mono">${esc(pub.v6_local || '—')}
          ${pub.v6_has ? `<span class="tag ok">${T('公网')}</span>` : `<span class="tag gray">${T('无')}</span>`}</span></div>
        <div class="kv"><b>IPv6 ${T('回显地址')}</b><span class="mono">${esc(pub.v6_public || '—')}
          ${pub.v6_source ? '<span class="tag gray">' + esc(pub.v6_source) + '</span>' : ''}</span></div>
      </div>
      <div class="kv"><b>${T('默认路由出口')}</b><span class="mono">${esc(pub.egress || '—')}
        ${pub.gw ? T(' 网关 ') + esc(pub.gw) : ''}</span></div>
      <div class="kv"><b>${T('检测时间')}</b><span>${esc(pub.checked_at || '—')}</span></div>
      <p class="desc" style="margin-top:10px"><b>${T('建议做法')}</b></p>
      <ul style="margin:6px 0 0 18px;color:var(--txt2);font-size:13px;line-height:1.9">
        ${enOrZh(note.en_advice, note.advice).map(x => `<li>${esc(x)}</li>`).join('')}
      </ul>
      ${d.warn ? `<div class="hint-inline" style="color:var(--warn);margin-top:10px">⚠ ${esc(d.warn)}</div>` : ''}
      <div class="notice warn" style="margin-top:12px">
        ${T('这里只能说明「地址是不是公网段」，')}<b>${T('不能说明外网能不能连进来')}</b>${T('。 配 DDNS')} ${T('之前建议先做一次入向实测')} ——
        ${T('不少宽带给的是公网地址却在入向做了封锁，那种情况域名解析得到、 但别人就是连不上。')}
        <div style="margin-top:8px">
          <button class="primary small" id="dd-gopubip">${T('去做真')}·${T('公网')} IP ${T('判定')}</button>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>DDNS ${T('开关')}</h3>
      <p class="desc">${T('开启后由后台定时任务按下面设定的间隔检测公网地址，')}
        <b>${T('只在地址变化时才下发')}</b>（IP ${T('没变不会去敲服务商的接口，避免触发频率限制）。')}</p>
      <label class="switch"><input type="checkbox" id="dd-en" ${c.enabled ? 'checked' : ''}><i></i>${T('启用动态域名解析')}</label>
      <div class="row">
        <label>${T('记录类型')}
          <div style="display:flex;gap:16px;padding-top:6px">
            <label class="switch" style="margin:0"><input type="checkbox" id="dd-ipv4" ${c.ipv4 !== false ? 'checked' : ''}><i></i>A（IPv4）</label>
            <label class="switch" style="margin:0"><input type="checkbox" id="dd-ipv6" ${c.ipv6 ? 'checked' : ''}><i></i>AAAA（IPv6）</label>
          </div>
        </label>
        <label>TTL${T('（秒）')}<input id="dd-ttl" type="number" value="${esc(c.ttl || 300)}" min="60" max="86400"></label>
        <label>${T('检测间隔（秒）')}<input id="dd-int" type="number" value="${esc(c.interval || 300)}" min="60" max="86400"></label>
      </div>
      <p class="hint-inline">IPv6 ${T('地址常为动态前缀，建议间隔')} ≤ 300 ${T('秒、TTL')} ≤ 300 ${T('秒，以加快变更生效。')}</p>
      <div class="kv" style="margin-top:8px"><b>${T('自动更新定时器')}</b><span>
        ${c.enabled
          ? (c.timer_active
            ? `<span class="tag ok">${T('运行中')}</span> ${T('每')} ` + esc(c.interval || 300) + T(' 秒检测一次')
            : `<span class="tag warn">${T('未启动')}</span> ${T('未能拉起')} ` + esc(c.timer_unit || 'drouter-ddns.timer') +
              T('，自动更新不会发生。请点下方「保存配置」重试，或检查这台机器是否支持 systemd。'))
          : T('未启用（打开上方开关后生效）')}</span></div>
    </div>

    <div class="card">
      <h3>${T('服务商与接入方式')}</h3>
      <p class="desc">${T('已内置')} ${provs.length} ${T('家国内外主流服务商，接口依据')} 2026 ${T('年现行公开文档整理。')}</p>
      <div class="row">
        <label>${T('服务商')}<select id="dd-prov">
          ${Object.keys(byRegion).map(reg => `<optgroup label="${esc(bt4('DDNS_REGION', reg, 'en', reg))}">${
            byRegion[reg].map(p => `<option value="${esc(p.v)}" ${c.provider === p.v ? 'selected' : ''}>${esc(bt4('DDNS_PROVIDERS', p.v, 'n', p.n))}</option>`).join('')
          }</optgroup>`).join('')}
        </select></label>
        <label>${T('主域名')}<input id="dd-domain" value="${esc(c.domain || '')}" placeholder="${T('例如')} example.com"></label>
        <label>${T('主机记录')}<input id="dd-sub" value="${esc(c.subdomain || '')}" placeholder="${T('留空或')} @ ${T('表示主域名')}"></label>
      </div>
      <div class="kv"><b>${T('当前服务商')}</b><span>${esc(bt4('DDNS_PROVIDERS', prov.v, 'n', prov.n) || prov.n || '')}
        <span class="tag ${DDNS_REGION_TAG(prov.region)}">${esc(bt4('DDNS_REGION', prov.region, 'en', prov.region) || prov.region || '')}</span></span></div>
      <p class="desc" style="margin-top:6px">${esc(bt4('DDNS_PROVIDERS', prov.v, 'desc', prov.desc) || prov.desc || '')}</p>
      ${c.provider_supported === false ? `<div class="notice warn" style="margin-top:8px">
        <b>${esc(bt4('DDNS_CUSTOM', prov.n, 'n', prov.n) || prov.n)}${T('在本版尚未接入真实下发')}</b>${T('，点「立即更新」只会得到「暂未接入」的失败提示， DNS 记录不会变动。建议改用阿里云')} / DNSPod / Cloudflare / ${T('自定义 URL')}
        —— ${T('这几种已验证可用，覆盖了绝大多数家庭宽带与 NAS 的需求。')}</div>` : ''}
      ${prov.doc ? (String(prov.doc).indexOf('http') === 0
          ? `<p class="hint-inline">${T('官方文档：')}<a href="${esc(httpUrl(prov.doc))}" target="_blank" rel="noopener noreferrer">${esc(prov.doc)}</a></p>`
          : `<p class="hint-inline">${esc(bt4('DDNS_CUSTOM', prov.doc, 'd', prov.doc))}</p>`)
        : ''}
      <div id="dd-fields" style="margin-top:10px"></div>
      <div class="kv"><b>${T('完整域名')}</b><span class="mono" id="dd-fqdn">—</span></div>
    </div>

    <div class="card">
      <h3>${T('实时状态')}</h3>
      <p class="desc">${T('每次进入本页或点击刷新都会重新检测；结果缓存')} 20 ${T('秒以免频繁请求外部接口。')}</p>
      <div class="kv"><b>${T('启用状态')}</b><span>${c.enabled
        ? `<span class="tag ok">${T('已启用')}</span>` : `<span class="tag gray">${T('未启用')}</span>`}</span></div>
      <div class="kv"><b>${T('上次更新')}</b><span>${esc(c.last_update || T('从未更新'))}</span></div>
      <div class="kv"><b>${T('上次')} IPv4</b><span class="mono">${esc(c.last_ip4 || '—')}</span></div>
      <div class="kv"><b>${T('上次')} IPv6</b><span class="mono">${esc(c.last_ip6 || '—')}</span></div>
      <div class="kv"><b>${T('上次结果')}</b><span>${esc(c.last_msg_cn || '—')}</span></div>
      <div class="row" style="margin-top:14px">
        <button class="fixed" id="dd-update">${T('立即更新记录')}</button>
        <button class="ghost fixed" id="dd-refresh">${T('刷新状态')}</button>
      </div>
      <div id="dd-out"></div>
    </div>`;

  const bind = (id, k, conv) => {
    const e = $(id); if (!e) return;
    const h = () => {
      S.ddnsDirty = Object.assign(S.ddnsDirty || {}, { [k]: conv ? conv(e.value) : e.value });
      pageDirty('ddns'); setActionMsg(T('DDNS 已修改，点击下方「保存配置」生效'));
    };
    e.oninput = h; if (e.tagName === 'SELECT') e.onchange = h;
  };
  const ddField = $('#dd-prov');
  if (ddField) ddField.onchange = () => {
    S.ddnsDirty = Object.assign(S.ddnsDirty || {}, { provider: ddField.value, fields: {} });
    renderDdFields(ddField.value, {}, true);
    pageDirty('ddns'); setActionMsg(T('已切换服务商，请填写对应凭据后保存'));
  };
  bind('#dd-domain', 'domain'); bind('#dd-sub', 'subdomain');
  bind('#dd-ttl', 'ttl', numOr); bind('#dd-int', 'interval', numOr);
  $('#dd-en').onchange = e => { S.ddnsDirty = Object.assign(S.ddnsDirty || {}, { enabled: e.target.checked }); pageDirty('ddns'); setActionMsg(T('已修改，点击下方保存')); };
  $('#dd-ipv4').onchange = e => { S.ddnsDirty = Object.assign(S.ddnsDirty || {}, { ipv4: e.target.checked }); pageDirty('ddns'); setActionMsg(T('已修改，点击下方保存')); };
  $('#dd-ipv6').onchange = e => { S.ddnsDirty = Object.assign(S.ddnsDirty || {}, { ipv6: e.target.checked }); pageDirty('ddns'); setActionMsg(T('已修改，点击下方保存')); };
  if (!draft) S.ddnsDirty = { enabled: !!c.enabled, provider: c.provider, domain: c.domain || '',
                  subdomain: c.subdomain || '', ipv4: c.ipv4 !== false, ipv6: !!c.ipv6,
                  ttl: c.ttl || 300, interval: c.interval || 300,
                  url4: c.url4 || '', url6: c.url6 || '', fields: Object.assign({}, c.fields || {}) };

  renderDdFields(c.provider, c.fields || {}, false);
  updFqdn();

  // 保存 / 重测 / 更新
  const saveBtn = document.createElement('button');
  saveBtn.className = 'fixed'; saveBtn.textContent = T('保存配置');
  saveBtn.id = 'dd-save';
  $('#dd-out').parentElement.querySelector('.row').appendChild(saveBtn);
  saveBtn.onclick = async () => {
    const b = Object.assign({}, S.ddnsDirty, { op: 'save' });
    b.fields = Object.assign({}, S.ddnsDirty.fields || {});
    $$('#dd-fields input').forEach(i => { b.fields[i.dataset.f] = i.value; });
    const r2 = await api('/api/ddns', { method: 'POST', body: b });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err');
    if (r2.ok) { pageClean('ddns'); setTimeout(viewDdns, 400); }
  };
  $('#dd-retest').onclick = async () => {
    const b = $('#dd-out'); if (b) b.innerHTML = `<pre>${T('正在重新检测公网能力…')}</pre>`;
    const r2 = await api('/api/ddns', { method: 'POST', body: { op: 'test' } });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err');
    setTimeout(viewDdns, 400);
  };
  const gp = $('#dd-gopubip');
  if (gp) gp.onclick = () => go('pubip');
  $('#dd-update').onclick = async () => {
    const out = $('#dd-out');
    out.innerHTML = `<pre>${T('正在下发更新…')}</pre>`;
    $('#dd-update').disabled = true;
    try {
      const r2 = await api('/api/ddns', { method: 'POST', body: { op: 'update' } });
      const rs = (r2.data || {}).results || [];
      const lines = rs.map(x =>
        `  ${x.ok ? '✓' : '✗'} ${esc(x.type)}  ${esc(x.record)} → ${esc(x.ip)}（${esc(x.provider)}）`
        + (x.ok ? '' : `\n      ${T('失败原因：')}${esc(x.msg || '')}`
                 + (x.detail ? `　${esc(x.detail)}` : ''))).join('\n');
      out.innerHTML = `<pre>${esc(r2.msg_cn || '')}${lines ? '\n' + lines : ''}</pre>`;
      toast(r2.msg_cn, r2.ok ? 'ok' : 'err');
    } finally {
      // ⛔ 必须在 finally 里恢复按钮：请求失败（网络断、helper 超时）时
      // 抛异常走不到这里，按钮就永久卡在 disabled —— 用户只能刷新页面。
      $('#dd-update').disabled = false;
    }
  };
  $('#dd-refresh').onclick = () => { toast(T('已刷新 DDNS 状态'), 'ok'); viewDdns(); };

  // 字段变化时实时更新 FQDN 预览
  $$('#dd-domain,#dd-sub').forEach(e => e.addEventListener('input', updFqdn));
}

/* 按服务商渲染所需凭据字段 */
function renderDdFields(provider, vals, clear) {
  const el = $('#dd-fields'); if (!el) return;
  const d = (S.ddns || {});
  const prov = (d.providers || []).find(x => x.v === provider) || {};
  const fields = prov.fields || [];
  if (provider === 'custom') {
    el.innerHTML = `
      <div class="row">
        <label>IPv4 ${T('更新')} URL<input id="dd-url4" value="${esc(clear ? '' : vals.url4 || '')}" placeholder="https://ddns.example.com/update?host={domain}&ip={ip}&token={token}"></label>
      </div>
      <div class="row">
        <label>IPv6 ${T('更新')} URL<input id="dd-url6" value="${esc(clear ? '' : vals.url6 || '')}" placeholder="${T('留空则复用')} IPv4 ${T('的 URL')}${T('（自动替换')} {ip} ${T('为 IPv6）')}"></label>
      </div>
      <div class="row">
        <label>Token<input id="dd-ftoken" data-f="token" value="${esc(clear ? '' : vals.token || '')}"></label>
        <label>${T('用户名')}<input id="dd-fuser" data-f="user" value="${esc(clear ? '' : vals.user || '')}"></label>
        <label>${T('密码')}<input id="dd-fpass" data-f="pass" type="password" value="${esc(clear ? '' : vals.pass || '')}"></label>
      </div>
      <p class="hint-inline">${T('可用占位符：')}<span class="mono">{ip} {ipv6} {domain} {token} {user} {pass}</span></p>`;
    const u4 = $('#dd-url4'), u6 = $('#dd-url6');
    if (u4) u4.oninput = () => { S.ddnsDirty.url4 = u4.value; pageDirty('ddns'); setActionMsg(T('已修改，点击下方保存')); };
    if (u6) u6.oninput = () => { S.ddnsDirty.url6 = u6.value; pageDirty('ddns'); setActionMsg(T('已修改，点击下方保存')); };
    bindDdFieldInputs();
    return;
  }
  if (!fields.length) { el.innerHTML = `<p class="desc">${T('该服务商无需额外凭据。')}</p>`; return; }
  const label = {
    access_key_id: 'AccessKey ID', access_key_secret: 'AccessKey Secret',
    secret_id: 'SecretId', secret_key: 'SecretKey',
    access_key: 'Access Key', api_token: 'API Token', zone_id: 'Zone ID',
    api_key: 'API Key', api_secret: 'API Secret',
    app_key: 'Application Key', app_secret: 'Application Secret',
    consumer_key: 'Consumer Key', token: T('Token / 更新密钥'),
    user: T('用户名'), pass: T('密码 / DDNS Key'), region: T('区域（如 cn-north-4）'),
    proxied: T('Cloudflare 代理（1=开 / 0=关）'),
  };
  const secretLike = /secret|token|pass|proxy/i;
  el.innerHTML = `<div class="row">` + fields.map(f => {
    const ty = secretLike.test(f) && !/zone|region|id$|key$/i.test(f) ? 'password' : 'text';
    return `<label>${esc(label[f] || f)}<input id="dd-f-${f}" data-f="${f}" type="${ty}" value="${esc(clear ? '' : (vals[f] || ''))}" autocomplete="off"></label>`;
  }).join('') + `</div>
  <p class="hint-inline">${T('凭据保存在本机数据库，仅用于向该服务商发起更新请求。')}</p>`;
  bindDdFieldInputs();
}

function bindDdFieldInputs() {
  $$('#dd-fields input').forEach(i => {
    i.oninput = () => {
      S.ddnsDirty.fields = S.ddnsDirty.fields || {};
      S.ddnsDirty.fields[i.dataset.f] = i.value;
      pageDirty('ddns'); setActionMsg(T('已修改，点击下方保存'));
    };
  });
}

function updFqdn() {
  const el = $('#dd-fqdn'); if (!el) return;
  const dom = (($('#dd-domain') || {}).value || '').trim();
  const sub = (($('#dd-sub') || {}).value || '').trim().replace(/^\.+|\.+$/g, '');
  el.textContent = dom ? (sub && sub !== '@' ? sub + '.' + dom : dom) : '—';
}

async function liveV6() {
  const el = $('#d6-live'); if (!el) return;
  const r = await api('/api/ipv6');
  const d = r.data || {};
  const g = (d.addrs || []).flatMap(a => (a.addr_info || []).filter(x => x.family === 'inet6')
    .map(x => ({ if: a.ifname, a: x.local, p: x.prefixlen, sc: x.scope })));
  const global = g.filter(x => x.sc === 'global');
  el.innerHTML = `
    <div class="kv"><b>${T('公网')} IPv6</b><span class="mono">${global.length
      ? global.slice(0, 4).map(x => esc(x.a) + '/' + x.p + ' <span class="tag gray">' + esc(x.if) + '</span>').join('<br>')
      : `<span class="tag warn">${T('未获取（运营商可能未下发 PD）')}</span>`}</span></div>
    <div class="kv"><b>${T('默认路由')}</b><span class="mono">${esc(d.default_route || T('无'))}</span></div>
    <div class="kv"><b>${T('转发开关')}</b><span>${(d.sysctl || {})['all.forwarding'] === '1' ? `<span class="tag ok">${T('已开启')}</span>` : `<span class="tag warn">${T('未开启')}</span>`}</span></div>`;
}

/* ============================ 网络状态 · NAT · 软加速 ============================ */
const NAT_STYLE = () => ({
  NAT1: { c: 'ok', ico: '◎', hint: T('最理想的出口类型') },
  NAT2: { c: 'info', ico: '◉', hint: T('常见的家用级 NAT') },
  NAT3: { c: 'warn', ico: '◍', hint: T('对称型 NAT，穿透较难') },
  NAT4: { c: 'err', ico: '●', hint: T('双重 NAT 或运营商级 NAT') },
  NONE: { c: 'gray', ico: '○', hint: T('未获取到公网映射信息') },
  NA: { c: 'gray', ico: '?', hint: T('检测不可用') },
});

async function viewNetStat() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>NAT ${T('类型检测')}</h3>
      <p class="desc">${T('通过公共')} STUN ${T('服务器自动探测当前出口的')} NAT ${T('行为（全锥型')} / ${T('受限')} / ${T('端口受限')} / ${T('对称）。 检测过程完全自动完成，约需')} 3–10 ${T('秒。')}</p>
      <div class="row" style="align-items:center;gap:10px">
        <button id="nat-btn" class="primary">${T('开始检测')}</button>
        <span class="desc" id="nat-when" style="margin:0"></span>
      </div>
      <div id="nat-out" style="margin-top:14px"><p class="desc">${T('点击「开始检测」以自动判定当前')} NAT ${T('类型。')}</p></div>
    </div>

    <div class="card">
      <h3>${T('软加速（')}nftables flowtable）</h3>
      <p class="desc">${T('利用内核')} flowtable ${T('把已建立的连接直接卸载到转发快路径， 等价于')} OpenWRT ${T('的「软件流量分载')} / FastTrack${T('」。可显著降低')} CPU ${T('占用、提升吞吐。')}</p>
      <div id="accel-box"><p class="desc">${T('读取中')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('加速影响说明')}</h3>
      <p class="desc">${T('开启前请了解可能受影响的程序与协议')} —— ${T('若你依赖下列任一功能，建议先保持关闭。')}</p>
      <div id="accel-impact"><p class="desc">${T('读取中')}…</p></div>
    </div>`;

  $('#nat-btn').onclick = runNatCheck;
  loadAccel();
  loadNatLast();
}

async function loadNatLast() {
  const r = await api('/api/nat/last');
  const d = (r.data || {});
  if (d.nat && d.checked_at) {
    renderNat({ ok: true, data: d });
  }
}

async function runNatCheck() {
  const btn = $('#nat-btn'), out = $('#nat-out');
  btn.disabled = true; btn.textContent = T('检测中…');
  out.innerHTML = `<p class="desc">${T('正在与 STUN 服务器通信…')}</p>`;
  const r = await api('/api/nat/check', { method: 'POST' });
  btn.disabled = false; btn.textContent = T('重新检测');
  if (!r.ok) {
    out.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('检测失败'))}</div>`;
    return;
  }
  renderNat(r);
}

function renderNat(r) {
  const d = r.data || {};
  const st = NAT_STYLE()[d.nat] || NAT_STYLE().NA;
  const when = d.checked_at ? `${T('上次检测：')}${esc(d.checked_at)}` : '';
  if ($('#nat-when')) $('#nat-when').innerHTML = when;
  const out = $('#nat-out');
  if (!out) return;
  out.innerHTML = `
    <div class="nat-badge ${st.c}">
      <span class="nat-ico">${st.ico}</span>
      <div class="nat-txt">
        <div class="nat-t">${esc(bt4('NAT_TYPES', d.nat, 't', d.title) || d.nat || T('未知'))}</div>
        <div class="nat-d">${esc(bt4('NAT_TYPES', d.nat, 'd', d.desc) || st.hint)}</div>
      </div>
      <span class="tag ${st.c}">${esc(d.nat || '?')}</span>
    </div>
    ${natDetailHtml(d.detail)}`;
}

/* NAT 检测的 detail 在后端是**对象**（local_ip / probes / servers_used …），
   之前它被当成纯文本交给转义函数，页面上就出现一个 "[object Object]" 方块。
   这里按结构拆开显示 —— 每个 STUN 服务器回显的公网 IP:端口 正是判断
   NAT 类型的依据，对排障很有价值，本来就该展示出来。 */
function natDetailHtml(detail) {
  if (!detail) return '';
  // 兼容后端把它改成纯字符串的情况
  if (typeof detail === 'string') {
    return `<pre class="nat-detail">${esc(detail)}</pre>`;
  }
  if (typeof detail !== 'object') return '';
  const probes = Array.isArray(detail.probes) ? detail.probes : [];
  const row = p => `<tr>
    <td class="mono">${esc(p.server || '—')}</td>
    <td class="mono">${esc(p.ip || '—')}</td>
    <td class="mono">${esc(p.port == null ? '—' : p.port)}</td></tr>`;
  const used = Array.isArray(detail.servers_used) ? detail.servers_used : [];
  return `<div class="nat-detail-box" style="margin-top:12px">
    <div class="kv"><b>${T('本机出口地址')}</b><span class="mono">${esc(detail.local_ip || '—')}
      ${detail.local_private === true
        ? `<span class="tag warn">${T('私网地址（说明在 NAT 后）')}</span>`
        : (detail.local_private === false ? `<span class="tag ok">${T('公网地址')}</span>` : '')}</span></div>
    ${detail.reason ? `<p class="hint-inline">${T('原因：')}${esc(detail.reason)}</p>` : ''}
    ${probes.length ? `<div class="tw" style="margin-top:8px"><table>
      <thead><tr><th>STUN ${T('服务器')}</th><th>${T('回显公网 IP')}</th><th>${T('回显端口')}</th></tr></thead>
      <tbody>${probes.map(row).join('')}</tbody></table></div>
      <p class="hint-inline">${T('多次探测的端口一致')} = ${T('锥形 NAT；每个目标端口都不同')} = ${T('对称型 NAT。')}</p>`
      : ''}
    ${used.length ? `<p class="hint-inline">${T('本次成功通信的服务器：')}${used.map(esc).join('、')}</p>` : ''}
  </div>`;
}

function accelHwNote(d) {
  const raw = d.hw_note || '';
  // 英文模式下，若后端上报了支持硬件卸载的网卡，用接口名拼接英文说明；
  // 否则走 bt4 表（后端下发的静态说明原文）。中文模式一律用后端原文。
  if (window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US'
      && d.hw_offload_ifaces && d.hw_offload_ifaces.length) {
    return 'Detected ' + d.hw_offload_ifaces.join(', ') + ' support hardware offload; enabling it can further lower CPU usage.';
  }
  return bt4('ACCEL_HW_NOTE', raw, 'en', raw);
}

async function loadAccel() {
  const box = $('#accel-box');
  const r = await api('/api/accel', { method: 'POST', body: { op: 'get' } });
  const d = r.data || {};
  const on = !!d.enabled;
  const devs = (d.devices || []);
  const fv4 = d.family_v4 !== false, fv6 = d.family_v6 !== false;
  const scopeCnRaw = d.scope_cn || (fv4 && fv6 ? 'IPv4 + IPv6' : (fv4 ? `${T('仅 IPv4')}` : (fv6 ? `${T('仅 IPv6')}` : T('未覆盖'))));
  const scopeCn = bt4('ACCEL_SCOPE', scopeCnRaw, 'en', scopeCnRaw);
  const scopeNote = d.scope_note || {};
  box.innerHTML = `
    <div class="accel-box ${on ? 'on' : 'off'}">
      <div class="accel-state">
        <span class="dot ${on ? 'on' : 'off'}"></span>
        <b>${on ? T('软加速已开启') : T('软加速已关闭')}</b>
        ${on ? `<span class="tag ok">${T('运行中')}</span>` : `<span class="tag gray">${T('未启用')}</span>`}
        <span class="tag ${fv4 ? 'info' : 'gray'}">IPv4</span>
        <span class="tag ${fv6 ? 'info' : 'gray'}">IPv6</span>
      </div>
      <div class="accel-meta">
        <div class="kv"><b>${T('作用范围')}</b><span><b>${esc(scopeCn)}</b>
          <span style="color:var(--txt3);font-size:12px">
          （flowtable ${T('可同时承载双栈，实际是否卸载取决于规则里的')} ip / ip6 ${T('匹配）')}</span></span></div>
        ${devs.length ? `<div class="kv"><b>${T('卸载网卡')}</b><span class="mono">${devs.map(esc).join(' · ')}</span></div>` : ''}
        ${d.flowtable ? `<div class="kv"><b>flowtable</b><span class="mono">${esc(d.flowtable)}</span></div>` : ''}
        ${d.flows != null ? `<div class="kv"><b>${T('已卸载流')}</b><span class="mono">${esc(d.flows)} ${T('条')}</span></div>` : ''}
        ${d.reason ? `<div class="kv"><b>${T('说明')}</b><span>${esc(d.reason)}</span></div>` : ''}
        ${d.hw_note ? `<div class="kv"><b>${T('硬件卸载')}</b><span>${esc(accelHwNote(d))}</span></div>` : ''}
        ${r.msg_cn ? `<div class="kv"><b>${T('状态')}</b><span>${esc(r.msg_cn)}</span></div>` : ''}
      </div>
      <div class="row" style="margin-top:12px;gap:16px;align-items:center">
        <label class="switch"><input type="checkbox" id="accel-v4" ${fv4 ? 'checked' : ''}><i></i>${T('加速')} IPv4</label>
        <label class="switch"><input type="checkbox" id="accel-v6" ${fv6 ? 'checked' : ''}><i></i>${T('加速')} IPv6</label>
      </div>
      <div class="row" style="margin-top:10px;gap:8px">
        <button id="accel-on" class="primary" ${on ? 'disabled' : ''}>${T('一键开启软加速')}</button>
        <button id="accel-off" class="ghost" ${on ? '' : 'disabled'}>${T('关闭软加速')}</button>
        <button id="accel-apply" class="ghost small" ${on ? '' : 'disabled'}>${T('按所选协议族重新应用')}</button>
        <button id="accel-ref" class="ghost small">${T('刷新状态')}</button>
      </div>
      <div id="accel-scope-note" style="margin-top:12px">${renderAccelScope(scopeNote)}</div>
    </div>`;

  const set = async (op) => {
    const b = $('#accel-' + (op === 'on' ? 'on' : 'off'));
    if (b) { b.disabled = true; b.textContent = op === 'on' ? T('开启中…') : T('关闭中…'); }
    const body = { op };
    if (op === 'on') {
      body.v4 = $('#accel-v4') ? $('#accel-v4').checked : true;
      body.v6 = $('#accel-v6') ? $('#accel-v6').checked : true;
    }
    const rr = await api('/api/accel', { method: 'POST', body });
    toast(rr.msg_cn || (rr.ok ? T('操作完成') : T('操作失败')), rr.ok ? 'ok' : 'err', 5000);
    if (rr.data && rr.data.log) {
      console.log('[accel]', rr.data.log);
    }
    loadAccel();
  };
  const onBtn = $('#accel-on'), offBtn = $('#accel-off'), refBtn = $('#accel-ref');
  const apBtn = $('#accel-apply');
  if (onBtn) onBtn.onclick = () => set('on');
  if (offBtn) offBtn.onclick = () => set('off');
  if (refBtn) refBtn.onclick = () => loadAccel();
  if (apBtn) apBtn.onclick = () => set('on');

  renderAccelImpact(d.impact || []);
}

/* 软加速的 IPv4 / IPv6 作用范围说明（#5） */
function renderAccelScope(note) {
  if (!note || !note.conclusion) return '';
  return `<div class="impact-info" style="border-left:3px solid var(--info,#3b82f6)">
    <b>${esc(bt4('ACCEL_SCOPE_NOTE', note.title || 'title', 'en', note.title) || T('软加速对 IPv4 / IPv6 的作用范围'))}</b>
    <div class="desc" style="margin:6px 0 8px">${esc(bt4('ACCEL_SCOPE_NOTE', note.conclusion || 'conclusion', 'en', note.conclusion))}</div>
    <div class="kv"><b>IPv4 ${T('规则')}</b><span class="mono" style="font-size:12px">${esc((note.v4 || {}).rule || '')}</span></div>
    <div class="desc" style="margin:2px 0 6px;color:var(--txt3);font-size:12px">${esc(bt4('ACCEL_SCOPE_NOTE', (note.v4 || {}).scope, 'en', (note.v4 || {}).scope || ''))}</div>
    <div class="kv"><b>IPv6 ${T('规则')}</b><span class="mono" style="font-size:12px">${esc((note.v6 || {}).rule || '')}</span></div>
    <div class="desc" style="margin:2px 0 6px;color:var(--txt3);font-size:12px">${esc(bt4('ACCEL_SCOPE_NOTE', (note.v6 || {}).scope, 'en', (note.v6 || {}).scope || ''))}</div>
    <ul style="font-size:13px;color:var(--txt2);line-height:1.9;margin:8px 0 0 18px">
      ${(note.points || []).map((p, i) => `<li>${esc(bt4('ACCEL_SCOPE_NOTE', p, 'en', p))}</li>`).join('')}
    </ul>
  </div>`;
}

function renderAccelImpact(items) {
  const el = $('#accel-impact'); if (!el) return;
  if (!items.length) { el.innerHTML = `<p class="desc">${T('暂无说明（后端未返回影响清单）。')}</p>`; return; }
  el.innerHTML = `<div class="impact-list">${items.map(it => `
    <div class="impact-item">
      <span class="impact-dot ${it.level || 'info'}"></span>
      <div><b>${esc(bt4('ACCEL_IMPACT', it.name, 'name', it.name))}</b><div class="desc" style="margin:2px 0 0">${esc(bt4('ACCEL_IMPACT', it.name, 'why', it.why))}</div></div>
    </div>`).join('')}</div>`;
}

/* ============================ VLAN / IPTV ============================ */
async function viewVlan() {
  const r = await api('/api/vlan');
  const d = r.data || {};
  const list = d.items || d.vlans || [];
  const presets = d.presets || [];
  const ifs = S.ifaces || [];
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('新增')} / ${T('编辑')} VLAN</h3>
      <p class="desc">${T('在物理网卡上划分')} 802.1Q ${T('子接口，常用于')} IPTV ${T('组播、访客隔离或多拨。')}</p>
      <div class="row">
        <label>${T('父网卡')}<select id="vl-parent">
          ${ifs.map(i => `<option value="${esc(i.name)}">${esc(i.name)}${i.role ? ' — ' + esc(i.role) : ''}</option>`).join('') || `<option value="">${T('（无可用网卡）')}</option>`}
        </select></label>
        <label>VLAN ID<input id="vl-vid" type="number" min="1" max="4094" placeholder="1 - 4094" value=""></label>
        <label>${T('别名')}<input id="vl-name" placeholder="${T('例如')} iptv"></label>
        <label>${T('用途')}<select id="vl-purpose">
          <option value="iptv">IPTV ${T('组播')}</option>
          <option value="lan">LAN ${T('扩展')}</option>
          <option value="wan">WAN ${T('多拨')}</option>
          <option value="guest">${T('访客隔离')}</option>
          <option value="other">${T('其他')}</option>
        </select></label>
      </div>
      <div class="row" style="align-items:flex-end;gap:8px">
        <button id="vl-add" class="primary">${T('添加')} VLAN</button>
        <span class="desc" id="vl-msg" style="margin:0"></span>
      </div>
      ${presets.length ? `<div class="desc" style="margin-top:10px">${T('常用预设：')}
        ${presets.map(p => `<button class="ghost small vl-preset" data-p='${esc(JSON.stringify(p))}'>${esc(bt4('VLAN_PRESETS', p.name || p.label, 'n', p.name || p.label))} (${esc(p.vid)})</button>`).join(' ')}</div>` : ''}
    </div>

    <div class="card">
      <h3>${T('已配置的')} VLAN</h3>
      <p class="desc">${T('保存后需点击「保存并应用」；应用时才会真正创建子接口。')}</p>
      <div id="vl-list">${list.length ? renderVlanRows(list) : `<p class="desc">${T('暂无 VLAN 配置。')}</p>`}</div>
    </div>

    <div class="card">
      <h3>IPTV ${T('组播注意事项')}</h3>
      <p class="desc">${T('机顶盒通常通过')} VLAN ${T('获取组播电视流，需要：')}</p>
      <div class="kv"><b>1</b><span>${T('在光猫上开启')} IPTV ${T('绑定，并记录其')} VLAN ID${T('（常见')} 85 / 41 / 3961 ${T('等）。')}</span></div>
      <div class="kv"><b>2</b><span>${T('此处创建同')} ID ${T('的 VLAN')} ${T('子接口，父网卡选连接光猫的那张网卡。')}</span></div>
      <div class="kv"><b>3</b><span>${T('网桥')}/${T('交换机端口需开启')} IGMP Snooping${T('，避免组播泛洪。')}</span></div>
      <div class="kv"><b>4</b><span>${T('机顶盒接入')} IPTV VLAN ${T('所在的端口即可正常收看。')}</span></div>
    </div>`;

  const presetBtns = $$('#vl-list .vl-del');
  presetBtns.forEach(b => b.onclick = () => delVlan(b.dataset.id));
  $$('.vl-preset').forEach(b => b.onclick = () => {
    const p = JSON.parse(b.dataset.p);
    if ($('#vl-vid')) $('#vl-vid').value = p.vid || '';
    if ($('#vl-name')) $('#vl-name').value = (p.name || '').replace(/\s*IPTV$/, '') || 'iptv';
    if ($('#vl-purpose')) $('#vl-purpose').value = p.purpose || 'iptv';
  });

  $('#vl-add').onclick = addVlan;
}

function renderVlanRows(list) {
  return list.map(v => `
    <div class="vlan-row">
      <div>
        <b class="mono">${esc(v._iface || (v.parent + '.' + v.vid))}</b>
        <span class="tag info">VLAN ${esc(v.vid)}</span>
        ${v.note ? `<span class="tag gray">${esc(bt4('PAGE_NOTES', v.note, 'd', v.note))}</span>` : ''}
        ${v._addr ? `<span class="mono" style="font-size:12px">${esc(v._addr)}</span>` : ''}
        ${v._present ? `<span class="tag ok">${T('已创建')}</span>` : `<span class="tag gray">${T('未创建')}</span>`}
      </div>
      <div>
        <button class="ghost small vl-del" data-id="${esc(v._iface || (v.parent + '.' + v.vid))}">${T('删除')}</button>
      </div>
    </div>`).join('');
}

async function addVlan() {
  const parent = ($('#vl-parent') || {}).value || '';
  const vid = parseInt(($('#vl-vid') || {}).value || '0', 10);
  const name = (($('#vl-name') || {}).value || '').trim();
  const purpose = ($('#vl-purpose') || {}).value || 'iptv';
  if (!parent) { toast(T('请先选择父网卡'), 'err'); return; }
  if (!(vid >= 1 && vid <= 4094)) { toast(T('VLAN ID 必须在 1–4094 之间'), 'err'); return; }
  const r = await api('/api/vlan', { method: 'POST', body: { op: 'save', parent, vid, note: name || purpose } });
  if (!r.ok) { toast(r.msg_cn || T('保存失败'), 'err', 5000); return; }
  toast(T('已保存，点击「保存并应用」后生效'), 'ok');
  viewVlan();
}

async function delVlan(id) {
  const r = await api('/api/vlan', { method: 'POST', body: { op: 'delete', id } });
  if (!r.ok) { toast(r.msg_cn || T('删除失败'), 'err'); return; }
  toast(T('已删除'), 'ok');
  viewVlan();
}

/* ============================ WOL 网络唤醒 ============================ */
async function viewWol() {
  const r = await api('/api/wol');
  const d = r.data || {};
  const ifs = d.ifaces || [];
  const saved = d.targets || [];
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('网卡唤醒能力')}</h3>
      <p class="desc">${T('每张网卡的')} WOL（Wake-on-LAN${T('）支持情况。唤醒需要目标设备已关机但仍带电。')}</p>
      <div class="table-wrap"><table>
        <thead><tr><th>${T('网卡')}</th><th>${T('状态')}</th><th>WOL ${T('支持')}</th><th>${T('当前设置')}</th><th>${T('操作')}</th></tr></thead>
        <tbody>
          ${ifs.length ? ifs.map(i => `<tr>
            <td class="mono">${esc(i.name)}</td>
            <td>${i.oper === 'UP' ? '<span class="tag ok">UP</span>' : '<span class="tag gray">DOWN</span>'}</td>
            <td>${i.supports_wol ? `<span class="tag ok">${T('支持')}</span>` : `<span class="tag gray">${T('不支持')}</span>`}</td>
            <td class="mono" style="font-size:12px">${esc(i.wol_mode || '—')}</td>
            <td>${i.supports_wol
      ? `<button class="ghost small wol-en" data-if="${esc(i.name)}">${i.enabled ? T('关闭 WOL') : T('启用 WOL')}</button>`
      : `<span class="desc" title="${esc(bt4('WOL_REASON', i.reason, 'd', i.reason) || '')}">${esc(bt4('WOL_REASON', i.reason, 'd', i.reason) || '—')}</span>`}</td>
          </tr>`).join('') : `<tr><td colspan="5" class="desc">${T('没有可用网卡')}</td></tr>`}
        </tbody>
      </table></div>
    </div>

    <div class="card">
      <h3>${T('唤醒指定设备')}</h3>
      <p class="desc">${T('输入目标设备的')} MAC ${T('地址，发送「魔术包」将其从关机')}/${T('休眠中唤醒。')}</p>
      <div class="row">
        <label>MAC ${T('地址')}<input id="wol-mac" placeholder="AA:BB:CC:DD:EE:FF" class="mono"></label>
        <label>${T('目标网段')}<select id="wol-net">
          <option value="255.255.255.255">${T('全网广播')} 255.255.255.255</option>
          ${(S.cfg.system && S.cfg.system.lan_address ? [S.cfg.system.lan_address] : []).map(a => {
    const n = a.split('.').slice(0, 3).join('.') + '.255';
    return `<option value="${esc(n)}" selected>LAN ${T('广播')} ${esc(n)}</option>`;
  }).join('')}
          <option value="192.168.1.255">192.168.1.255</option>
          <option value="192.168.7.255">192.168.7.255</option>
        </select></label>
        <label>${T('出口网卡')}<select id="wol-if">
          <option value="">${T('自动选择')}</option>
          ${ifs.map(i => `<option value="${esc(i.name)}">${esc(i.name)}</option>`).join('')}
        </select></label>
      </div>
      <div class="row" style="align-items:flex-end;gap:8px">
        <button id="wol-go" class="primary">${T('发送唤醒包')}</button>
        <label class="switch" style="margin:0"><input type="checkbox" id="wol-save"><i></i>${T('同时保存为常用设备')}</label>
      </div>
      <div id="wol-out" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>${T('常用设备')}</h3>
      <p class="desc">${T('已保存的唤醒目标，可一键唤醒。')}</p>
      <div id="wol-saved">${saved.length ? saved.map(t => `
        <div class="vlan-row"><div>
          <b>${esc(t.label || t.mac)}</b>
          <span class="mono" style="font-size:12px">${esc(t.mac)}</span>
          ${t.iface ? `<span class="tag gray">${esc(t.iface)}</span>` : ''}
        </div><div>
          <button class="ghost small wol-wake" data-mac="${esc(t.mac)}" data-if="${esc(t.iface || '')}">${T('唤醒')}</button>
          <button class="ghost small wol-rm" data-mac="${esc(t.mac)}">${T('删除')}</button>
        </div></div>`).join('') : `<p class="desc">${T('暂无保存的设备。')}</p>`}
      </div>
    </div>`;

  $$('.wol-en').forEach(b => b.onclick = async () => {
    const cur = ifs.find(i => i.name === b.dataset.if) || {};
    const rr = await api('/api/wol', { method: 'POST', body: { op: 'iface', iface: b.dataset.if, enabled: !cur.enabled } });
    toast(rr.msg_cn || (rr.ok ? T('已启用') : T('失败')), rr.ok ? 'ok' : 'err');
    viewWol();
  });
  $('#wol-go').onclick = () => sendWol();
  $$('.wol-wake').forEach(b => b.onclick = () => {
    $('#wol-mac').value = b.dataset.mac;
    if (b.dataset.if) $('#wol-if').value = b.dataset.if;
    sendWol();
  });
  $$('.wol-rm').forEach(b => b.onclick = async () => {
    const rest = saved.filter(t => t.mac !== b.dataset.mac);
    const rr = await api('/api/wol', { method: 'POST', body: { op: 'save', devices: rest } });
    toast(rr.msg_cn || T('已删除'), rr.ok ? 'ok' : 'err');
    viewWol();
  });
}

async function sendWol() {
  const mac = ($('#wol-mac') || {}).value.trim();
  const net = ($('#wol-net') || {}).value;
  const iface = ($('#wol-if') || {}).value;
  const save = ($('#wol-save') || {}).checked;
  const out = $('#wol-out');
  if (!/^([0-9a-f]{2}[:.-]){5}[0-9a-f]{2}$/i.test(mac.replace(/\s/g, ''))) {
    out.innerHTML = `<div class="notice err">${T('MAC 地址格式不正确，应为 AA:BB:CC:DD:EE:FF')}</div>`;
    return;
  }
  out.innerHTML = `<p class="desc">${T('正在发送魔术包…')}</p>`;
  const body = { op: 'wake', mac, broadcast: net, iface, save: !!save };
  if (save) body.label = mac;
  const r = await api('/api/wol', { method: 'POST', body });
  if (!r.ok) { out.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('发送失败'))}</div>`; return; }
  out.innerHTML = `<div class="notice ok">${esc(r.msg_cn || T('魔术包已发送'))}
    ${(r.data && r.data.sent || []).length ? `<div class="desc">${T('已发往：')}` + r.data.sent.map(esc).join(' · ') + '</div>' : ''}</div>`;
  if (save) setTimeout(viewWol, 800);
}

/* 网卡候选列表归一化：后端可能返回 ['ens18','ens19'] 或 [{name:'ens18'}...] */
function ifaceNames(list) {
  return (list || []).map(i => (typeof i === 'string' ? i : (i && i.name) || ''))
    .filter(Boolean);
}

/* ============================ PPPoE 多拨 ============================ */
/* 多拨 = 在同一物理口上并发多条 PPPoE 会话。
   爱快叫「多拨」，本质是 N 个 ppp 接口 + 源地址策略路由 + 加权负载均衡。 */
async function viewPppMulti() {
  const r = await api('/api/pppoe-multi');
  const d = r.data || {};
  const sessions = d.sessions || [];
  const ifs = ifaceNames(d.wan_ifaces);
  const strat = d.strategies || [];
  const online = d.online || 0;

  $('#view').innerHTML = `
    <div class="card">
      <h3>PPPoE ${T('多拨')} <span class="tag ${online ? 'ok' : 'gray'}">${online}/${sessions.length} ${T('在线')}</span></h3>
      <p class="desc">${T('在同一物理网口上并发多条')} PPPoE ${T('会话，把总带宽叠加起来（爱快「多拨」的等效实现）。 每条会话是一张独立的')} ppp ${T('接口，配独立的源地址策略路由')} —— ${T('这样从某条会话出去的回包才会从原路返回。')}</p>
      <div class="accel-box" style="margin-top:12px">
        <div class="impact-info"><b>⚠ ${T('前提：运营商必须允许并发拨号')}</b>
          <div class="desc">${T('部分省份限制同一账号只能拨')} 1 ${T('条，多余会话会反复重拨并显示为「未连接」，这是运营商的限制，不是程序故障。 建议先用')} 1 ${T('条确认账号能正常拨通，再逐步增加会话数。')}</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>${T('拨号策略')}</h3>
      <p class="desc">${T('决定多条会话如何共同承载流量。')}</p>
      <div class="row">
        <label>WAN ${T('物理网卡')}<select id="ppm-if">
          ${ifs.length ? ifs.map(i => `<option value="${esc(i)}"${i === d.wan_iface ? ' selected' : ''}>${esc(i)}</option>`).join('')
      : `<option value="">${T('（未检测到物理网卡）')}</option>`}
        </select></label>
        <label>${T('负载策略')}<select id="ppm-strategy">
          ${strat.map(s => `<option value="${esc(s.id)}"${s.id === d.strategy ? ' selected' : ''}>${bt4('PPPM_STRATEGIES', s.id, 'name', s.name)}</option>`).join('')}
        </select></label>
        <label>${T('会话数量')}<input id="ppm-count" type="number" min="1" max="8" value="${esc(sessions.length || 1)}" style="width:90px"></label>
      </div>
      <p class="desc" id="ppm-strategy-why"></p>
      <div class="row" style="align-items:flex-end;gap:8px">
        <label class="switch" style="margin:0"><input type="checkbox" id="ppm-dup"><i></i>${T('单账号多拨（所有会话用同一个账号）')}</label>
        <button id="ppm-expand" class="ghost small">${T('按会话数量生成')}</button>
      </div>
    </div>

    <div class="card">
      <h3>${T('会话账号')}</h3>
      <p class="desc">${T('每条会话一个账号。若运营商允许同账号并发，勾选上面的「单账号多拨」即可全部填同一个账号。')}</p>
      <div id="ppm-list">${ppmRenderSessions(sessions)}</div>
      <div class="row" style="margin-top:12px">
        <button id="ppm-save" class="primary">${T('保存配置')}</button>
      </div>
      <div id="ppm-out" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>${T('应用与连接')}</h3>
      <p class="desc">${T('「应用并连接」会真正发起拨号并改变出网默认路由。请在确认账号无误后执行。')}</p>
      <label class="switch" style="margin:6px 0"><input type="checkbox" id="ppm-confirm"><i></i>
        ${T('我确认账号已填写正确，并了解该操作会改变当前网络的出网路由')}</label>
      <div class="row" style="gap:8px">
        <button id="ppm-apply" class="primary">${T('应用并连接')}</button>
        <button id="ppm-stop" class="ghost">${T('断开全部会话')}</button>
      </div>
    </div>

    <div class="card">
      <h3>${T('会话状态与排错')}</h3>
      <p class="desc">${T('逐条查看会话的接口、获得的地址与对端网关；点「日志」看拨号过程与原因分析。')}</p>
      <div class="table-wrap"><table>
        <thead><tr><th>#</th><th>${T('接口')}</th><th>${T('状态')}</th><th>${T('本端地址')}</th><th>${T('对端网关')}</th><th>${T('路由表')}</th><th>${T('操作')}</th></tr></thead>
        <tbody>
          ${sessions.length ? sessions.map(s => `<tr>
            <td>${s.idx}</td>
            <td class="mono">${esc(s.iface)}</td>
            <td>${s.running ? `<span class="tag ok">${T('已连接')}</span>` : `<span class="tag gray">${T('未连接')}</span>`}</td>
            <td class="mono" style="font-size:12px">${esc(s.addr || '—')}</td>
            <td class="mono" style="font-size:12px">${esc(s.peer || '—')}</td>
            <td class="mono" style="font-size:12px">${s.table}</td>
            <td style="white-space:nowrap">
              <button class="ghost small ppm-act" data-idx="${s.idx}" data-op="session_restart">${T('重拨')}</button>
              <button class="ghost small ppm-act" data-idx="${s.idx}" data-op="session_stop">${T('停止')}</button>
              <button class="ghost small ppm-log" data-idx="${s.idx}">${T('日志')}</button>
            </td>
          </tr>`).join('') : `<tr><td colspan="7" class="desc">${T('尚未配置任何会话')}</td></tr>`}
        </tbody>
      </table></div>
      <div id="ppm-logbox" style="margin-top:12px"></div>
    </div>

    ${(d.notes || []).length ? `<div class="card">
      <h3>${T('使用须知')}</h3>
      <ul class="desc" style="line-height:1.9">${d.notes.map(n => `<li>${esc(bt4('PPPM_NOTES', n, 'd', n))}</li>`).join('')}</ul>
    </div>` : ''}`;

  const upd = () => {
    const sel = $('#ppm-strategy');
    const s = strat.find(x => x.id === sel.value);
    $('#ppm-strategy-why').textContent = s ? bt4('PPPM_STRATEGIES', s.id, 'why', s.why) : '';
  };
  $('#ppm-strategy').onchange = upd; upd();

  $('#ppm-expand').onclick = () => {
    const n = Math.max(1, Math.min(8, parseInt($('#ppm-count').value || '1', 10)));
    const old = ppmReadSessions();
    const dup = $('#ppm-dup').checked;
    const out = [];
    for (let i = 1; i <= n; i++) {
      const prev = old[i - 1] || {};
      out.push(dup && i > 1
        ? Object.assign({}, old[0] || {}, { weight: 1 })
        : Object.assign({ user: '', has_password: false, weight: prev.weight || 1 }, prev));
    }
    $('#ppm-list').innerHTML = ppmRenderSessions(out.map((s, i) => Object.assign({}, s, { idx: i + 1 })));
  };

  $('#ppm-save').onclick = async () => {
    const list = ppmReadSessions();
    if (!list.length) { toast(T('请先生成至少 1 条会话'), 'err'); return; }
    const body = {
      op: 'save', iface: $('#ppm-if').value, strategy: $('#ppm-strategy').value,
      duplicate_account: $('#ppm-dup').checked, sessions: list,
    };
    const rr = await api('/api/pppoe-multi', { method: 'POST', body });
    toast(rr.msg_cn || (rr.ok ? T('已保存') : T('保存失败')), rr.ok ? 'ok' : 'err');
    if (rr.ok) setTimeout(viewPppMulti, 600);
    else ppmOut(rr);
  };

  $('#ppm-apply').onclick = async () => {
    if (!$('#ppm-confirm').checked) { toast(T('请先勾选确认项'), 'err'); return; }
    ppmOut({ msg_cn: T('正在应用并逐条拨号，请稍候…'), ok: true });
    const rr = await api('/api/pppoe-multi', {
      method: 'POST',
      body: { op: 'apply', confirm: true },
    });
    toast(rr.msg_cn || (rr.ok ? T('已应用') : T('应用失败')), rr.ok ? 'ok' : 'err');
    ppmOut(rr);
    setTimeout(viewPppMulti, 1500);
  };

  $('#ppm-stop').onclick = async () => {
    if (!window.confirm(T('确定断开全部多拨会话？出网将恢复为单一默认路由。'))) return;
    const rr = await api('/api/pppoe-multi', { method: 'POST', body: { op: 'stop' } });
    toast(rr.msg_cn || (rr.ok ? T('已断开') : T('操作失败')), rr.ok ? 'ok' : 'err');
    setTimeout(viewPppMulti, 800);
  };

  $$('.ppm-act').forEach(b => b.onclick = async () => {
    const rr = await api('/api/pppoe-multi', {
      method: 'POST',
      body: { op: b.dataset.op, idx: parseInt(b.dataset.idx, 10) },
    });
    toast(rr.msg_cn || '', rr.ok ? 'ok' : 'err');
    setTimeout(viewPppMulti, 900);
  });

  $$('.ppm-log').forEach(b => b.onclick = async () => {
    const box = $('#ppm-logbox');
    box.innerHTML = `<p class="desc">${T('正在读取日志…')}</p>`;
    const rr = await api('/api/pppoe-multi/log', { method: 'POST', body: { idx: parseInt(b.dataset.idx, 10) } });
    const dd = rr.data || {};
    box.innerHTML = `
      <h3 style="font-size:14px">${T('会话')} #${esc(String(dd.idx || b.dataset.idx))} ${T('日志')}</h3>
      ${dd.hint ? `<div class="notice warn"><b>${T('结论：')}</b>${esc(dd.hint)}</div>` : ''}
      <pre class="term-pre" style="max-height:340px">${esc(dd.log || T('（无日志）'))}</pre>`;
  });
}

function ppmRenderSessions(list) {
  if (!list.length) list = [{ idx: 1, user: '', weight: 1 }];
  return list.map((s, i) => `
    <div class="vlan-row ppm-row">
      <div style="flex:1;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <b style="min-width:52px">#${i + 1}</b>
        <input class="ppm-user mono" placeholder="${T('宽带账号（如')} 0791xxxxxxx）" value="${esc(s.user || '')}" style="min-width:220px">
        <input class="ppm-pass mono" type="password"
               placeholder="${s.has_password ? T('••••••（留空表示不修改）') : T('宽带密码')}" style="min-width:160px">
        <label class="desc" style="display:flex;align-items:center;gap:4px">${T('权重')}
          <input class="ppm-weight" type="number" min="1" max="100" value="${esc(String(s.weight || 1))}" style="width:64px">
        </label>
        ${s.running === true ? `<span class="tag ok">${T('已连接')}</span>` : ''}
        ${s.addr ? `<span class="mono tag gray" style="font-size:11px">${esc(s.addr)}</span>` : ''}
      </div>
    </div>`).join('');
}

function ppmReadSessions() {
  const users = $$('.ppm-user'), passes = $$('.ppm-pass'), weights = $$('.ppm-weight');
  const out = [];
  for (let i = 0; i < users.length; i++) {
    const u = users[i].value.trim();
    const p = passes[i].value;
    const w = parseInt(weights[i].value || '1', 10);
    if (!u && !p) continue;   // 整行空 → 跳过
    out.push({ user: u, password: p, weight: isNaN(w) ? 1 : w });
  }
  return out;
}

function ppmOut(r) {
  const box = $('#ppm-out');
  if (!box) return;
  const d = r.data || {};
  let html = `<div class="notice ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</div>`;
  const bad = (d.failed || []);
  if (bad.length) {
    html += `<div class="table-wrap" style="margin-top:8px"><table><thead><tr><th>${T('会话')}</th><th>${T('原因')}</th></tr></thead><tbody>`
      + bad.map(f => `<tr><td>#${f.idx}</td><td class="mono" style="font-size:12px">${esc(f.err || '')}</td></tr>`).join('')
      + '</tbody></table></div>';
  }
  if (d.log) html += `<pre class="term-pre" style="max-height:260px">${esc(d.log)}</pre>`;
  box.innerHTML = html;
}

/* ============================ QoS 智能限速 ============================ */
async function viewQos() {
  const r = await api('/api/qos');
  const d = r.data || {};
  const ips = d.ips || [];
  const dscp = d.dscp || [];

  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('智能限速')} QoS
        <span class="tag ${d.running ? 'ok' : 'gray'}">${d.running ? T('运行中') : T('未启用')}</span>
        ${ips.length ? `<span class="tag warn">${T('按 IP 限速模式')}</span>` : `<span class="tag ok">${T('CAKE 智能模式')}</span>`}
      </h3>
      <p class="desc">${T('用 Linux')} ${T('内核自带的')} tc ${T('流量控制复刻爱快')} QoS：CAKE ${T('负责公平分流与抗')} bufferbloat，
        HTB ${T('负责按')} IP ${T('精确限速，')}nftables ${T('负责')} DSCP ${T('优先级标记与连接数限制。')}</p>
      ${d.accel_conflict ? `<div class="notice err" style="margin-top:10px">
        <b>${T('检测到冲突：软加速（flowtable）正在运行。')}</b>
        flowtable ${T('会让「已建立连接」绕过 netfilter 钩子，导致 DSCP 打标与 IP 限速完全失效。 请先到「概览')} → ${T('网络状态')} / ${T('加速」关闭软加速，再启用 QoS。')}
      </div>` : ''}
      ${!d.modules_ok ? `<div class="notice err" style="margin-top:10px">
        ${T('缺少内核模块：')}${esc((d.missing_modules || []).join('、'))}${T('。请确认使用 Debian 官方内核。')}
      </div>` : ''}
      <div class="accel-box" style="margin-top:12px">
        <div class="impact-info"><b>${T('工作方式')}</b>
          <div class="desc">${T('上行直接在')} WAN ${T('出口排队；下行必须先把入口流量重定向到')} IFB ${T('虚拟设备再排队 （这是')} Linux ${T('的固有约束，与爱快下行限速原理相同）。')}</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>${T('带宽与模式')}</h3>
      <p class="desc">${T('带宽建议填「实测速率的')} 85%~90%${T('」，留一点余量让')} CAKE ${T('有排队空间，抗延迟效果最好。')}</p>
      <div class="row">
        <label>WAN ${T('物理网卡')}<select id="qos-wan">
          ${ifaceNames(d.wan_ifaces).map(n => `<option value="${esc(n)}"${n === d.wan_iface ? ' selected' : ''}>${esc(n)}</option>`).join('')
      || `<option value="${esc(d.wan_iface || '')}">${esc(d.wan_iface || T('（未检测到）'))}</option>`}
        </select></label>
        <label>${T('下行带宽')} (Mbit/s)<input id="qos-down" type="number" min="0" max="100000" value="${esc(String(d.down_mbit || ''))}" placeholder="${T('如 900')}" style="width:110px">
          <span class="desc">0 = ${T('不限速')}</span></label>
        <label>${T('上行带宽')} (Mbit/s)<input id="qos-up" type="number" min="0" max="100000" value="${esc(String(d.up_mbit || ''))}" placeholder="${T('如 90')}" style="width:110px">
          <span class="desc">0 = ${T('不限速')}</span></label>
        <label>CAKE ${T('队列模式')}<select id="qos-mode">
          ${(d.cake_modes || []).map(m => `<option value="${esc(m.id)}"${m.id === d.mode ? ' selected' : ''}>${bt4('QOS_CAKE_MODES', m.id, 'name', m.name)}</option>`).join('')}
        </select></label>
      </div>
      <p class="desc" id="qos-mode-why"></p>
    </div>

    <div class="card">
      <h3>${T('优先级规则（')}DSCP ${T('打标）')}</h3>
      <p class="desc">${T('勾选后，匹配的流量会被标记更高的')} DSCP，CAKE ${T('会把它插到前面转发')} —— ${T('这就是「游戏优先」的实现方式。 注：加密流量（')}HTTPS/QUIC${T('）只能按端口判断，无法准确识别具体应用。')}</p>
      <div class="dep-grid">
        ${(d.presets || []).map(p => {
          const on = dscp.some(x => x.preset === p.id);
          return `<label class="dep-item" style="cursor:pointer">
            <input type="checkbox" class="qos-pre" data-id="${esc(p.id)}" ${on ? 'checked' : ''}>
            <div>
              <b>${bt4('QOS_PRESETS', p.id, 'name', p.name)}</b>
              <div class="desc">${T('端口')} ${esc(p.ports)} → DSCP <span class="mono">${esc(p.dscp)}</span></div>
              <div class="desc" style="font-size:11px">${bt4('QOS_PRESETS', p.id, 'why', p.why)}</div>
            </div>
          </label>`;
        }).join('')}
      </div>
    </div>

    <div class="card">
      <h3>${T('按内网')} IP ${T('精确限速（')}HTB）</h3>
      <p class="desc">${T('给指定设备设「保证带宽」与「峰值带宽」。保证带宽是该设备最低能拿到的速率，峰值是它在空闲时可借用的上限。')}
        <b>${T('填写了任何一条')} IP ${T('规则后，')}CAKE ${T('智能模式会被')} HTB ${T('精确限速取代')}</b> —— ${T('两者不能同时生效。')}</p>
      <div id="qos-ips">${qosRenderIps(ips)}</div>
      <div class="row" style="margin-top:10px">
        <button id="qos-add-ip" class="ghost small">${T('＋ 添加')} IP ${T('限速')}</button>
        <span class="desc">${T('最多')} 64 ${T('条')}</span>
      </div>
    </div>

    <div class="card">
      <h3>${T('连接数限制')}</h3>
      <p class="desc">${T('限制单个内网')} IP ${T('的并发连接数，抑制异常设备（中毒、')}P2P ${T('泛滥）拖垮整台路由。')}</p>
      <div class="row">
        <label class="switch" style="margin:0"><input type="checkbox" id="qos-cl" ${d.connlimit ? 'checked' : ''}><i></i>${T('启用连接数限制')}</label>
        <label>${T('单 IP')} ${T('上限')}<input id="qos-cl-max" type="number" min="10" max="20000" value="${esc(String(d.connlimit_max || 500))}" style="width:110px"></label>
        <span class="desc">${T('建议')} 300 ${T('以上，过低会影响浏览器')}/${T('下载工具')}</span>
      </div>
    </div>

    <div class="card">
      <h3>${T('保存与应用')}</h3>
      <div class="row" style="gap:8px">
        <button id="qos-save" class="primary">${T('保存配置')}</button>
        <button id="qos-on" class="primary" ${d.running ? 'disabled' : ''}>${T('启用')} QoS</button>
        <button id="qos-off" class="ghost" ${d.running ? '' : 'disabled'}>${T('停用')} QoS</button>
        <button id="qos-refresh" class="ghost small">${T('刷新状态')}</button>
      </div>
      <label class="switch" style="margin:10px 0"><input type="checkbox" id="qos-confirm"><i></i>
        ${T('我了解启用')} QoS ${T('会改变流量转发行为')}${d.accel_conflict ? T('（且当前与软加速冲突）') : ''}</label>
      <div id="qos-out" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>${T('当前生效的规则')}</h3>
      <p class="desc">${T('直接读取内核里的')} tc / nftables ${T('状态，可用于确认规则是否真的挂上了。')}</p>
      <pre class="term-pre" style="max-height:300px">${esc(qosLiveText(d.live))}</pre>
    </div>

    <div class="card">
      <h3>${T('影响与取舍')}</h3>
      <div class="dep-grid">
        ${(d.impact || []).map(i => `<div class="impact-item impact-${esc(i.level)}">
          <b>${bt4('QOS_IMPACT', i.name, 'name', i.name)}</b><div class="desc">${esc(bt4('QOS_IMPACT', i.name, 'why', i.why))}</div></div>`).join('')}
      </div>
    </div>`;

  const upd = () => {
    const s = (d.cake_modes || []).find(x => x.id === $('#qos-mode').value);
    $('#qos-mode-why').textContent = s ? bt4('QOS_CAKE_MODES', s.id, 'why', s.why) : '';
  };
  $('#qos-mode').onchange = upd; upd();

  const collect = () => ({
    op: 'save',
    wan_iface: $('#qos-wan').value,
    down_mbit: parseInt($('#qos-down').value || '0', 10) || 0,
    up_mbit: parseInt($('#qos-up').value || '0', 10) || 0,
    mode: $('#qos-mode').value,
    dscp: $$('.qos-pre').filter(c => c.checked).map(c => ({ preset: c.dataset.id })),
    connlimit: $('#qos-cl').checked,
    connlimit_max: parseInt($('#qos-cl-max').value || '500', 10) || 500,
    ips: qosReadIps(),
  });

  $('#qos-add-ip').onclick = () => {
    const cur = qosReadIps();
    cur.push({ ip: '', guar_mbit: 10, ceil_mbit: 50, prio: 2 });
    $('#qos-ips').innerHTML = qosRenderIps(cur);
  };

  $('#qos-save').onclick = async () => {
    const rr = await api('/api/qos', { method: 'POST', body: collect() });
    toast(rr.msg_cn || (rr.ok ? T('已保存') : T('保存失败')), rr.ok ? 'ok' : 'err');
    qosOut(rr);
    if (rr.ok) setTimeout(viewQos, 700);
  };

  $('#qos-on').onclick = async () => {
    if (!$('#qos-confirm').checked) { toast(T('请先勾选确认项'), 'err'); return; }
    qosOut({ msg_cn: T('正在下发 QoS 规则…'), ok: true });
    const body = collect();
    const rr = await api('/api/qos', {
      method: 'POST',
      body: { op: 'on', confirm: true, ignore_accel: d.accel_conflict === true && window.confirm(T('当前软加速与 QoS 冲突，是否仍要继续？')) },
    });
    toast(rr.msg_cn || (rr.ok ? T('已启用') : T('启用失败')), rr.ok ? 'ok' : 'err');
    qosOut(rr);
    setTimeout(viewQos, 1000);
  };

  $('#qos-off').onclick = async () => {
    if (!window.confirm(T('确定停用 QoS？所有 CAKE / HTB 规则将被移除。'))) return;
    const rr = await api('/api/qos', { method: 'POST', body: { op: 'off' } });
    toast(rr.msg_cn || (rr.ok ? T('已停用') : T('操作失败')), rr.ok ? 'ok' : 'err');
    setTimeout(viewQos, 800);
  };

  $('#qos-refresh').onclick = () => viewQos();
  $$('.qos-rm-ip').forEach(b => b.onclick = () => {
    const cur = qosReadIps().filter((_x, i) => String(i) !== b.dataset.i);
    $('#qos-ips').innerHTML = qosRenderIps(cur);
  });
}

function qosRenderIps(ips) {
  if (!ips.length) return `<p class="desc">${T('尚未添加 IP 限速规则 —— 当前使用 CAKE 智能模式，所有设备自动公平分流。')}</p>`;
  return ips.map((r, i) => `
    <div class="vlan-row qos-ip-row">
      <div style="flex:1;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input class="qos-ip mono" placeholder="192.168.7.100" value="${esc(r.ip || '')}" style="min-width:150px">
        <label class="desc" style="display:flex;align-items:center;gap:4px">${T('保证')}
          <input class="qos-guar" type="number" min="1" value="${esc(String(r.guar_mbit || 10))}" style="width:80px">M</label>
        <label class="desc" style="display:flex;align-items:center;gap:4px">${T('峰值')}
          <input class="qos-ceil" type="number" min="1" value="${esc(String(r.ceil_mbit || 50))}" style="width:80px">M</label>
        <label class="desc" style="display:flex;align-items:center;gap:4px">${T('优先级')}
          <input class="qos-prio" type="number" min="0" max="6" value="${esc(String(r.prio == null ? 2 : r.prio))}" style="width:56px"></label>
        <span class="desc" style="font-size:11px">0 ${T('最高')} / 6 ${T('最低')}</span>
      </div>
      <button class="ghost small qos-rm-ip" data-i="${i}">${T('删除')}</button>
    </div>`).join('');
}

function qosReadIps() {
  const ipEls = $$('.qos-ip'), gEls = $$('.qos-guar'), cEls = $$('.qos-ceil'), pEls = $$('.qos-prio');
  const out = [];
  for (let i = 0; i < ipEls.length; i++) {
    const ip = ipEls[i].value.trim();
    if (!ip) continue;
    out.push({
      ip,
      guar_mbit: parseInt(gEls[i].value || '1', 10) || 1,
      ceil_mbit: parseInt(cEls[i].value || '1', 10) || 1,
      prio: (pEls[i] && parseInt(pEls[i].value, 10)) || 0,
    });
  }
  return out;
}

function qosLiveText(live) {
  live = live || {};
  const parts = [];
  parts.push(T('── 入口重定向 / 上行根队列 ──'));
  parts.push(((live.wan_root || []).concat(live.ingress || [])).join('\n') || `${T('（无）')}`);
  parts.push('');
  parts.push(T('── IFB 下行队列 ──'));
  parts.push((live.ifb || []).join('\n') || `${T('（无）')}`);
  parts.push('');
  parts.push(`── nftables drouter_qos ${T('表')} ──`);
  parts.push(live.nft_ok ? (live.nft_dump || `${T('（空）')}`) : T('（未创建）'));
  return parts.join('\n');
}

function qosOut(r) {
  const box = $('#qos-out');
  if (!box) return;
  const d = r.data || {};
  let html = `<div class="notice ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</div>`;
  if (d.log) html += `<pre class="term-pre" style="max-height:280px">${esc(d.log)}</pre>`;
  if (d.live) html += `<pre class="term-pre" style="max-height:220px">${esc(qosLiveText(d.live))}</pre>`;
  box.innerHTML = html;
}

/* ============================ DPI 应用识别 ============================ */
async function viewDpi() {
  const r = await api('/api/dpi');
  const d = r.data || {};
  const srcs = d.sources || [];
  const mirrors = d.mirrors || [];
  const inst = d.installed || {};
  const lastMirror = d.last_mirror || 'ghproxy';

  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('应用识别')} DPI <span class="tag ${srcs.some(s => s.present) ? 'ok' : 'gray'}">
        ${srcs.filter(s => s.present).length}/${srcs.length} ${T('已安装')}</span></h3>
      <p class="desc">${T('按「应用')}/${T('协议」识别流量（爱快的杀手锏）。')}Debian ${T('上对应')} nDPI / Suricata${T('。 识别结果可交给')} QoS ${T('打标限速，或直接封禁某类应用。')}</p>
      <div class="accel-box" style="margin-top:12px">
        <div class="impact-info"><b>⚠ ${T('加密流量的固有限制')}</b>
          <div class="desc">HTTPS / QUIC ${T('已加密，任何')} DPI${T('（包括爱快）都无法')} 100% ${T('识别具体应用， 只能靠')} SNI${T('、端口、流量特征做推测。这是行业共同限制，不是实现缺陷。')}</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>${T('识别引擎状态')}</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>${T('组件')}</th><th>${T('状态')}</th><th>${T('路径')} / ${T('说明')}</th></tr></thead>
        <tbody>
          <tr><td>nDPI Reader</td>
            <td>${inst.ndpiReader && inst.ndpiReader.present ? `<span class="tag ok">${T('已安装')}</span>` : `<span class="tag gray">${T('未安装')}</span>`}</td>
            <td class="mono" style="font-size:12px">${esc((inst.ndpiReader && inst.ndpiReader.path) || '—')}</td></tr>
          <tr><td>Suricata</td>
            <td>${inst.suricata && inst.suricata.present ? `<span class="tag ok">${T('已安装')}</span>` : `<span class="tag gray">${T('未安装')}</span>`}</td>
            <td class="mono" style="font-size:12px">${esc((inst.suricata && inst.suricata.path) || '—')}</td></tr>
          <tr><td>ntopng</td>
            <td>${inst.ntopng && inst.ntopng.present ? `<span class="tag ok">${T('已安装')}</span>` : `<span class="tag gray">${T('未安装')}</span>`}</td>
            <td class="mono" style="font-size:12px">${esc((inst.ntopng && inst.ntopng.path) || '—')}</td></tr>
          <tr><td>xt_ndpi ${T('内核模块')}</td>
            <td>${inst.xt_ndpi_module && inst.xt_ndpi_module.present ? `<span class="tag ok">${T('已加载')}</span>` : `<span class="tag gray">${T('未加载')}</span>`}</td>
            <td class="desc">${T('提供')} nftables ${T('按应用匹配能力，需自行编译')}</td></tr>
          <tr><td>APT ${T('仓库')} nDPI</td>
            <td>${inst.apt_ndpi && inst.apt_ndpi.available ? `<span class="tag ok">${T('可装')}</span>` : `<span class="tag gray">${T('不可用')}</span>`}</td>
            <td class="desc">${T('可直接')} apt install${T('，无需编译')}</td></tr>
        </tbody>
      </table></div>
    </div>

    <div class="card">
      <h3>${T('规则库')} / ${T('源码更新')}</h3>
      <p class="desc">nDPI ${T('的协议识别规则托管在')} GitHub。<b>${T('国内直连')} GitHub ${T('通常超时或极慢')}</b>${T('， 所以这里提供多个公共加速前缀')} —— ${T('先「测速」挑最快的，再更新。')}</p>
      <div class="row">
        <label>${T('加速前缀')}<select id="dpi-mirror">
          ${mirrors.map(m => `<option value="${esc(m.key)}"${m.key === lastMirror ? ' selected' : ''}>${bt4('DPI_HOSTS', m.key, 'name', m.name)}</option>`).join('')}
          <option value="custom"${lastMirror === 'custom' ? ' selected' : ''}>${T('自定义前缀')}…</option>
        </select></label>
        <label>${T('自定义前缀')}<input id="dpi-custom" class="mono" placeholder="https://your-proxy.example.com/"
          value="${esc(d.state && d.state.prefix && !mirrors.some(m => m.prefix === d.state.prefix) ? d.state.prefix : '')}" style="min-width:260px"></label>
      </div>
      <p class="desc" id="dpi-mirror-why"></p>
      <div class="row" style="gap:8px;margin-top:6px">
        <button id="dpi-test" class="ghost">${T('测速并优选')}</button>
        <button id="dpi-update" class="primary">${T('更新规则库')}</button>
        <button id="dpi-log" class="ghost small">${T('查看日志')}</button>
      </div>
      <div id="dpi-out" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>${T('已安装的规则库')}</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>${T('库')}</th><th>${T('状态')}</th><th>${T('更新时间')}</th><th>${T('大小')}</th><th>${T('用途')}</th></tr></thead>
        <tbody>
          ${srcs.map(s => `<tr>
            <td><b>${bt4('DPI_SOURCES', s.id, 'name', s.name)}</b><div class="desc mono" style="font-size:11px">${esc(s.repo)}</div></td>
            <td>${s.present ? `<span class="tag ok">${T('已安装')}</span>` : `<span class="tag gray">${T('未安装')}</span>`}</td>
            <td class="mono" style="font-size:12px">${esc(s.version || '—')}</td>
            <td class="mono" style="font-size:12px">${esc(s.size_h || '—')}</td>
            <td class="desc" style="font-size:12px">${bt4('DPI_SOURCES', s.id, 'why', s.why)}</td>
          </tr>`).join('')}
        </tbody>
      </table></div>
      ${d.last_update ? `<p class="desc" style="margin-top:8px">${T('上次更新：')}${esc(d.last_update)}${T('（前缀')} ${esc(d.last_mirror || '—')}）</p>` : ''}
    </div>

    <div class="card">
      <h3>${T('安装方式')}</h3>
      <p class="desc">${T('三种路径，按「省事')} → ${T('最新')} → ${T('最强」排列。选一个生成命令，复制到')} Web ${T('终端执行即可。')}</p>
      <div class="row">
        <select id="dpi-plan">
          ${(d.presets || []).map(p => `<option value="${esc(p.id)}">${bt4('DPI_INSTALL_PLANS', p.id, 'name', p.name)}</option>`).join('')}
        </select>
        <button id="dpi-plan-go" class="ghost">${T('生成命令')}</button>
      </div>
      <p class="desc" id="dpi-plan-why"></p>
      <div id="dpi-plan-out" style="margin-top:10px"></div>
    </div>

    ${(d.notes || []).length ? `<div class="card">
      <h3>${T('使用须知')}</h3>
      <ul class="desc" style="line-height:1.9">${d.notes.map(n => `<li>${esc(bt4('DPI_NOTES', n, 'd', n))}</li>`).join('')}</ul>
    </div>` : ''}`;

  const updMirror = () => {
    const k = $('#dpi-mirror').value;
    const m = mirrors.find(x => x.key === k);
    $('#dpi-mirror-why').textContent = m
      ? (bt4('DPI_HOSTS', m.key, 'why', m.why) || m.why) : T('自定义前缀：填写你自己搭建或信任的加速服务地址，需以 / 结尾。');
  };
  $('#dpi-mirror').onchange = updMirror; updMirror();

  $('#dpi-test').onclick = async () => {
    const box = $('#dpi-out');
    box.innerHTML = `<p class="desc">${T('正在逐个测试前缀连通性（每个最多 5 秒）…')}</p>`;
    const rr = await api('/api/dpi', {
      method: 'POST',
      body: { op: 'test', custom_prefix: $('#dpi-custom').value.trim() },
    });
    const res = (rr.data || {}).results || [];
    box.innerHTML = `
      <div class="notice ${rr.ok ? 'ok' : 'warn'}">${esc(rr.msg_cn || '')}</div>
      <div class="table-wrap"><table>
        <thead><tr><th>${T('前缀')}</th><th>${T('结果')}</th><th>${T('耗时')}</th><th>${T('地址')}</th></tr></thead>
        <tbody>${res.map(x => `<tr>
          <td>${esc(x.name)}</td>
          <td>${x.ok ? `<span class="tag ok">${T('可用')}</span>` : `<span class="tag gray">${esc(x.err || T('不可用'))}</span>`}</td>
          <td class="mono">${esc(String(x.secs))}s</td>
          <td class="mono" style="font-size:11px">${esc(x.prefix || T('（直连）'))}</td>
        </tr>`).join('')}</tbody>
      </table></div>
      <p class="desc">${T('建议选最快且可用的那个，然后点「更新规则库」。')}</p>`;
    if (rr.data && rr.data.best) {
      const sel = $('#dpi-mirror');
      if ([...sel.options].some(o => o.value === rr.data.best)) sel.value = rr.data.best;
      updMirror();
    }
  };

  $('#dpi-update').onclick = async () => {
    const box = $('#dpi-out');
    const mirror = $('#dpi-mirror').value;
    box.innerHTML = `<p class="desc">${T('正在下载并安装规则库，可能耗时较长（视网速而定）…')}</p>`;
    const rr = await api('/api/dpi', {
      method: 'POST',
      body: { op: 'update', mirror, custom_prefix: $('#dpi-custom').value.trim() },
    });
    const res = (rr.data || {}).results || [];
    let html = `<div class="notice ${rr.ok ? 'ok' : 'err'}">${esc(rr.msg_cn || '')}</div>`;
    if (res.length) {
      html += `<div class="table-wrap"><table><thead><tr><th>${T('库')}</th><th>${T('结果')}</th><th>${T('说明')}</th></tr></thead><tbody>`
        + res.map(x => `<tr><td>${esc(x.name)}</td>
            <td>${x.ok ? `<span class="tag ok">${T('成功')}</span>` : `<span class="tag warn">${T('失败')}</span>`}</td>
            <td class="desc" style="font-size:12px">${esc(x.ok ? ((x.files || 0) + T(' 个文件，') + (x.size_kb || 0) + ' KB') : (x.detail || x.err || ''))}</td>
          </tr>`).join('') + '</tbody></table></div>';
    }
    if ((rr.data || {}).log) html += `<pre class="term-pre" style="max-height:300px">${esc(rr.data.log)}</pre>`;
    html += `<p class="desc">${T('完整日志可用上方「查看日志」按钮获取，便于复制反馈。')}</p>`;
    box.innerHTML = html;
    if (rr.ok) setTimeout(() => $('#dpi-log').click(), 1200);
  };

  $('#dpi-log').onclick = async () => {
    const rr = await api('/api/dpi/log');
    $('#dpi-out').innerHTML = `<pre class="term-pre" style="max-height:400px">${esc((rr.data || {}).log || T('（暂无更新日志）'))}</pre>
      <button class="ghost small" id="dpi-copy-log">${T('复制日志')}</button>`;
    const cb = $('#dpi-copy-log');
    if (cb) cb.onclick = () => {
      navigator.clipboard.writeText((rr.data || {}).log || '');
      toast(T('日志已复制'), 'ok');
    };
  };

  const updPlan = () => {
    const p = (d.presets || []).find(x => x.id === $('#dpi-plan').value);
    const whyEn = (typeof bt4 === 'function') ? bt4('DPI_INSTALL_PLANS', p ? p.id : '', 'why', '') : '';
    $('#dpi-plan-why').textContent = p ? (whyEn || p.why) : '';
  };
  $('#dpi-plan').onchange = updPlan; updPlan();

  $('#dpi-plan-go').onclick = async () => {
    const which = $('#dpi-plan').value;
    const rr = await api('/api/dpi', {
      method: 'POST',
      body: { op: 'plan', which, prefix: $('#dpi-custom').value.trim() || DPI_PREFIX_HINT },
    });
    const cmds = (rr.data || {}).cmds || [];
    $('#dpi-plan-out').innerHTML = `<pre class="term-pre">${esc(cmds.join('\n'))}</pre>
      <button class="ghost small" id="dpi-copy-cmd">${T('复制命令')}</button>`;
    const cb = $('#dpi-copy-cmd');
    if (cb) cb.onclick = () => {
      navigator.clipboard.writeText(cmds.join('\n'));
      toast(T('命令已复制，可粘贴到 Web 终端执行'), 'ok');
    };
  };
}

const DPI_PREFIX_HINT = 'https://ghproxy.com/';

/* ============================ 依赖自检 ============================ */
async function viewDepCheck() {
  const v = $('#view');
  v.innerHTML = `
    <div class="card">
      <h3>${T('运行环境依赖自检')}</h3>
      <p class="desc">${T('检测在一台全新安装的')} <b>Debian 13 + XFCE ${T('桌面')}</b> ${T('上运行')} Drouter ${T('所需的 全部依赖、组件、软件与内核模块。绿色为已就绪，红色为缺失（必需），黄色为可选。')}</p>
      <div class="row" style="align-items:center;gap:10px;flex-wrap:wrap">
        <button id="dep-check" class="primary">${T('开始检测')}</button>
        <button id="dep-install" class="ghost" disabled>${T('一键安装缺失的必需项')}</button>
        <span class="desc" id="dep-sum" style="margin:0"></span>
      </div>
      <p class="desc" style="margin-top:8px">
        <b>${T('「一键安装」只装必需项')}</b>${T('。可选组件（')}Samba / NFS / Docker / ${T('桌面环境等） 需要你逐项点「安装」')}—— ${T('它们装完会拉起常驻服务或占用大量空间， 不该因为一次点错就被全部装上。')}
      </p>
      <div class="row hidden" id="dep-filter" style="align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px">
        <input id="dep-q" class="input" type="search" placeholder="${T('搜索名称')} / ${T('命令')} / ${T('包名，如')} xfs、f2fs、mkfs"
               style="flex:1;min-width:220px" autocomplete="off">
        <label class="desc" style="margin:0;display:flex;align-items:center;gap:4px;white-space:nowrap">
          <input id="dep-only-miss" type="checkbox"> ${T('只看缺失')}
        </label>
      </div>
      <div class="row hidden" id="dep-groups" style="gap:6px;flex-wrap:wrap;margin-top:8px"></div>
    </div>
    <div id="dep-result"><p class="desc">${T('点击「开始检测」开始。')}</p></div>
    <div class="card hidden" id="dep-log-card">
      <h3>${T('安装日志')}</h3>
      <p class="desc">${T('如安装失败，可复制以下日志用于排查。')}</p>
      <div class="code-wrap"><button class="ghost small copy-btn" data-copy="#dep-log">${T('复制日志')}</button>
        <pre id="dep-log" class="log-pre"></pre></div>
    </div>`;

  $('#dep-check').onclick = runDepCheck;
  const inst = $('#dep-install');
  // 必须包一层：直接把函数给 onclick 会把 MouseEvent 当成 keys 参数传进去
  inst.onclick = () => runDepInstall();
  $('#dep-q').oninput = renderDepList;
  $('#dep-only-miss').onchange = renderDepList;
  bindCopyButtons(v);
}

// 上一次检测的完整结果留在内存里，过滤 / 搜索只是重新渲染，不再打接口。
// DEP_REQ_MISSING：本次「缺失且必需」的 key 列表，一键安装只用这个列表，
// 避免把 Samba / NFS / Docker / 桌面环境这类可选大件一起装上。
let DEP_LAST = null;
let DEP_REQ_MISSING = [];
let DEP_FILTER = 'all';

function depMatch(x, q) {
  if (!q) return true;
  const hay = [x.name, x.key, x.target, x.pkg, x.note, x.group_name, x.where]
    .filter(Boolean).join(' ').toLowerCase();
  return hay.indexOf(q) >= 0;
}

function renderDepList() {
  const d = DEP_LAST;
  if (!d) return;
  const box = $('#dep-result');
  const items = d.items || [];
  const q = (($('#dep-q') || {}).value || '').trim().toLowerCase();
  const onlyMiss = !!(($('#dep-only-miss') || {}).checked);

  const sel = items.filter(x => (DEP_FILTER === 'all' || x.group === DEP_FILTER)
    && depMatch(x, q) && (!onlyMiss || !x.ok));

  const req = sel.filter(x => x.required);
  const missReq = req.filter(x => !x.ok);
  const opt = sel.filter(x => !x.required);
  const missOpt = opt.filter(x => !x.ok);

  const g = (title, arr, cls) => arr.length ? `
    <div class="card">
      <h3>${title} <span class="tag ${cls}">${arr.length}</span></h3>
      <div class="dep-grid">${arr.map(renderDepItem).join('')}</div>
    </div>` : '';

  const missing = items.filter(x => !x.ok);
  const total = items.length;
  const shown = sel.length;
  box.innerHTML =
    (missing.length ? '' : `<div class="notice ok" style="margin-bottom:12px">${T('全部依赖均已就绪，无需安装。')}</div>`) +
    (shown === 0
      ? `<div class="notice warn">${T('当前筛选条件下没有匹配项（共')} ${total} ${T('项）。试试清空搜索或切换到「全部」。')}</div>`
      : g(T('缺失（必需）'), missReq, 'err') +
        g(T('缺失（可选）'), missOpt, 'warn') +
        g(T('已就绪（必需）'), req.filter(x => x.ok), 'ok') +
        g(T('已就绪（可选）'), opt.filter(x => x.ok), 'gray')) +
    (shown !== total ? `<p class="desc" style="margin-top:8px">${T('显示')} ${shown} / ${total} ${T('项')}</p>` : '');
  bindDepInstallButtons(box);
}

async function runDepCheck() {
  const btn = $('#dep-check');
  btn.disabled = true; btn.textContent = T('检测中…');
  const r = await api('/api/deps/check');
  btn.disabled = false; btn.textContent = T('再次检测');
  const box = $('#dep-result');
  if (!r.ok) { box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('检测失败'))}</div>`; return; }
  const d = r.data || {};
  const items = d.items || [];
  DEP_LAST = d;
  DEP_FILTER = 'all';

  const req = items.filter(x => x.required);
  const missReq = req.filter(x => !x.ok);
  const opt = items.filter(x => !x.required);
  const missOpt = opt.filter(x => !x.ok);
  $('#dep-sum').innerHTML = `${T('必需')} <b style="color:${missReq.length ? 'var(--err)' : 'var(--ok)'}">${req.length - missReq.length}/${req.length}</b>
    · ${T('可选')} ${opt.length - missOpt.length}/${opt.length}
    ${d.extra && d.extra.os ? ` · ${esc(d.extra.os)}` : ''}`;
  $('#dep-install').disabled = missReq.length === 0;
  // 记下本次必需缺失项的 key：一键安装只能装这些，绝不能顺带把可选项也装上
  DEP_REQ_MISSING = missReq.filter(x => x.pkg).map(x => x.key);

  // 分组过滤按钮：光靠四段标题，70 项平铺时根本找不到想看的那一个
  const groups = d.groups || [];
  const gbox = $('#dep-groups');
  if (groups.length) {
    gbox.classList.remove('hidden');
    const cnt = {};
    items.forEach(x => { cnt[x.group] = (cnt[x.group] || 0) + 1; });
    gbox.innerHTML =
      `<button class="ghost small dep-grp on" data-grp="all">${T('全部')} <span class="tag gray">${items.length}</span></button>` +
      groups.filter(g => cnt[g.id]).map(g =>
        `<button class="ghost small dep-grp" data-grp="${esc(g.id)}">${esc(bt4('DEP_GROUPS', g.id, 'n', g.name) || g.name)} <span class="tag gray">${cnt[g.id]}</span></button>`
      ).join('');
    gbox.querySelectorAll('.dep-grp').forEach(b => {
      b.onclick = () => {
        DEP_FILTER = b.dataset.grp;
        gbox.querySelectorAll('.dep-grp').forEach(x => x.classList.remove('on'));
        b.classList.add('on');
        renderDepList();
      };
    });
  }
  $('#dep-filter').classList.remove('hidden');
  renderDepList();
}

function renderDepItem(x) {
  const cls = x.ok ? 'ok' : (x.required ? 'err' : 'warn');
  const ico = x.ok ? '✓' : (x.required ? '✕' : '!');
  // 缺失且能装的给一个单独的安装按钮：可选项必须由用户逐项确认，
  // 不能混进「一键安装」里（否则一次点错就把桌面环境 / Samba / Docker 全装上）
  const btn = (!x.ok && x.pkg)
    ? `<button class="ghost small" data-dep-key="${esc(x.key)}">${T('安装')}</button>` : '';
  return `<div class="dep-item ${cls}">
    <span class="dep-ico">${ico}</span>
    <div class="dep-body">
      <b>${esc(bt4('DEP_ITEMS', x.key, 'n', x.name))}</b>
      <div class="desc">${esc(bt4('DEP_ITEMS', x.key, 'd', x.note) || '')}</div>
      <div class="mono dep-where">${esc(x.where || x.target || '')}${x.pkg ? T(' · 包名 ') + esc(x.pkg) : ''}</div>
    </div>
    <span class="tag gray">${esc(bt4('DEP_GROUPS', x.group, 'n', x.group_name) || x.group_name || T('其它'))}</span>
    ${x.required ? `<span class="tag err">${T('必需')}</span>` : `<span class="tag gray">${T('可选')}</span>`}
    ${btn}
  </div>`;
}

// 渲染后把每个「安装」按钮接上事件
function bindDepInstallButtons(root) {
  (root || document).querySelectorAll('[data-dep-key]').forEach(b => {
    b.onclick = () => runDepInstall([b.dataset.depKey]);
  });
}

async function runDepInstall(keys) {
  const one = Array.isArray(keys) && keys.length > 0;
  const btn = $('#dep-install');
  if (!one && btn) { btn.disabled = true; btn.textContent = T('安装中…（可能需要几分钟）'); }
  toast(one ? T('正在安装该组件…') : T('正在安装缺失依赖，请稍候…'), 'info', 5000);
  const body = one ? { keys: keys } : { keys: DEP_REQ_MISSING };
  const r = await api('/api/deps/install', { method: 'POST', body: body });
  if (!one && btn) { btn.disabled = false; btn.textContent = T('一键安装缺失的必需项'); }
  const box = $('#dep-result');
  const d = r.data || {};
  if (d.log) {
    $('#dep-log-card').classList.remove('hidden');
    $('#dep-log').textContent = d.log;
  }
  if (!r.ok) {
    toast(r.msg_cn || T('安装失败'), 'err', 6000);
    box.insertAdjacentHTML('afterbegin',
      `<div class="notice err">${T('安装失败：')}${esc(r.msg_cn || '')}${T('。请查看下方「安装日志」。')}</div>`);
    if (!$('#dep-log-card').classList.contains('hidden')) {
      $('#dep-log-card').scrollIntoView({ behavior: 'smooth' });
    }
    return;
  }
  const still = d.still_missing || [];
  toast(still.length ? `${T('安装完成，仍有')} ${still.length} ${T('项缺失')}` : T('全部依赖已安装完成'), still.length ? 'warn' : 'ok', 6000);
  await runDepCheck();
  const box2 = $('#dep-result');
  if (box2) bindDepInstallButtons(box2);
  const chk = $('#dep-check'); if (chk) chk.textContent = T('再次检测');
}

/* ============================ Web 终端 + 文件管理器 ============================ */
/* #4 重做：真 PTY 交互终端。
   前端只负责三件事：
     ① 按键 → 字节（每敲一个键立刻发送，不等回车）；
     ② 回来的字节 → 屏幕（内置一个小型终端模拟器，处理 ANSI 光标与颜色）；
     ③ 尺寸变化时上报 cols/rows，让 top / vi 这类全屏程序排版正确。
   回显、行编辑、Ctrl+C、方向键历史全部由内核终端行规程负责，前端不再自己画假回显。
   注意这是**本机 shell**（root / ajeef / drouter），不是到别的机器的 SSH ——
   界面也就不再摆主机 / 端口 / 密码这些用不上的输入框，免得看着能连其实连不了。 */

const WS_MAX_SB = 600;          // 回滚区最多保留多少行
const WS_WRITE_CHUNK = 1024;    // 单次写入字节上限（粘贴长文本时分片发）
/* 距底部多少像素内算「在底部」。wsAutoScroll（决定要不要继续跟随）
   wsSyncTailBtn（决定要不要显示「↓ 最新」按钮）必须用同一个阈值 ——
   两处不一致会出现「按钮没出现，输出也不跟着滚了」这种迷惑状态。 */
const WS_TAIL_GAP = 40;

const WS = {
  sid: null, cols: 0, rows: 0,
  grid: null, cx: 0, cy: 0, key: '',
  st: null, saved: null,
  dirty: null, lineEls: [],
  carry: '',        // 跨帧拼接用的残字（半个代理对）
};
let WS_PUMP = null;    // 读取轮询定时器
let WS_BUSY = false;   // 防止 read 请求重叠

/* 16 色 ANSI 调色板（偏暗底终端的配色，保证在深色屏上可读） */
const ANSI_FG = ['#5c6b85', '#e06c62', '#7cc488', '#e2b34a', '#6fa4e8', '#b48ce8', '#5cc0d4', '#c9d4e4'];
const ANSI_FGB = ['#8b97ad', '#ff8b7f', '#9ce0ac', '#f2cd72', '#93c0f5', '#cfabf5', '#84dcee', '#eaf0fa'];
const ANSI_BG = ['#26314a', '#6b2b26', '#245239', '#5c4714', '#254063', '#453363', '#1c4a55', '#37455f'];
const ANSI_BGB = ['#3a4763', '#8a3a33', '#33684a', '#7a6220', '#345a86', '#5c4682', '#28636f', '#4b5b78'];

/* ---------------------------- 屏幕缓冲区 ---------------------------- */
function wsBlankRow(cols) {
  const r = new Array(cols);
  for (let x = 0; x < cols; x++) r[x] = { c: ' ', k: '' };
  return r;
}

function wsGridInit(cols, rows) {
  WS.cols = cols; WS.rows = rows;
  WS.grid = [];
  for (let y = 0; y < rows; y++) WS.grid.push(wsBlankRow(cols));
  WS.cx = 0; WS.cy = 0;
  WS.st = { fg: -1, bg: -1, bold: 0, dim: 0, it: 0, ul: 0, rev: 0 };
  WS.key = '';
  WS.saved = null;
  WS.dirty = new Set();
  WS.lineEls = [];
  for (let y = 0; y < rows; y++) WS.dirty.add(y);
}

function wsStyleKey() {
  const s = WS.st, p = [];
  if (s.bold) p.push('b');
  if (s.dim) p.push('d');
  if (s.it) p.push('i');
  if (s.ul) p.push('u');
  if (s.rev) p.push('r');
  if (s.fg >= 0) p.push('f' + s.fg);
  if (s.bg >= 0) p.push('g' + s.bg);
  return p.join('|');
}

function wsStyleAttr(k) {
  if (!k) return { cls: '', style: '' };
  let fg = '', bg = '', cls = [], rev = false;
  k.split('|').forEach(t => {
    if (t === 'b') cls.push('t-b');
    else if (t === 'd') cls.push('t-dim');
    else if (t === 'i') cls.push('t-i');
    else if (t === 'u') cls.push('t-u');
    else if (t === 'r') rev = true;
    else if (t[0] === 'f') { const n = +t.slice(1); fg = n < 8 ? ANSI_FG[n] : ANSI_FGB[n - 8]; }
    else if (t[0] === 'g') { const n = +t.slice(1); bg = n < 8 ? ANSI_BG[n] : ANSI_BGB[n - 8]; }
  });
  if (rev) { const t = fg; fg = bg || '#0d1526'; bg = t || '#c9d4e4'; }
  let style = '';
  if (fg) style += 'color:' + fg + ';';
  if (bg) style += 'background:' + bg + ';';
  return { cls: cls.join(' '), style: style };
}

function wsClearRow(y) {
  const row = WS.grid[y];
  for (let x = 0; x < WS.cols; x++) row[x] = { c: ' ', k: WS.key };
  WS.dirty.add(y);
}

function wsScroll() {
  const top = WS.grid.shift();
  WS.grid.push(wsBlankRow(WS.cols));
  wsScrollbackPush(top);
  WS.cy = WS.rows - 1;
  for (let y = 0; y < WS.rows; y++) WS.dirty.add(y);
}

function wsNewline() {
  WS.cy++;
  if (WS.cy >= WS.rows) wsScroll();
}

/* 字符显示宽度：中文 / 全角在真实终端里占两格。
   以前一律按一格处理，光标列就和真实终端对不上 —— 输入中文时 readline
   算的换行位置和屏幕画出来的不一致，看起来就是「字被切开了」。 */
function wsCharWidth(ch) {
  if (!ch) return 0;
  const c = ch.codePointAt(0);
  if (c < 0x20) return 0;                                  // 控制字符
  if (c >= 0x1100 && c <= 0x115F) return 2;                // 韩文初声
  if (c >= 0x2E80 && c <= 0xA4CF) return 2;                // CJK / 全角符号
  if (c >= 0xAC00 && c <= 0xD7A3) return 2;                // 韩文音节
  if (c >= 0xF900 && c <= 0xFAFF) return 2;                // CJK 兼容
  if (c >= 0xFE30 && c <= 0xFE6F) return 2;                // 全角标点
  if (c >= 0xFF01 && c <= 0xFF60) return 2;                // 全角 ASCII
  if (c >= 0xFFE0 && c <= 0xFFE6) return 2;
  if (c >= 0x20000 && c <= 0x3FFFD) return 2;              // CJK 扩展 B~G
  return 1;
}

function wsPut(ch) {
  const w = wsCharWidth(ch);
  if (!w) return;
  // 宽字符放不下整行剩余宽度时先换行，别把半个字挤到下一行
  if (WS.cx + w > WS.cols) { WS.cx = 0; wsNewline(); }
  WS.grid[WS.cy][WS.cx] = { c: ch, k: WS.key };
  if (w === 2 && WS.cx + 1 < WS.cols) {
    // 第二格是占位，渲染时跳过（见 wsRowHtml）
    WS.grid[WS.cy][WS.cx + 1] = { c: '', k: WS.key, pad: 1 };
  }
  WS.cx += w;
  WS.dirty.add(WS.cy);
}

/* ---------------------------- ANSI 转义序列 ---------------------------- */
function wsSgr(p) {
  const s = WS.st;
  const list = p.length ? p : [0];
  let i = 0;
  while (i < list.length) {
    const v = list[i];
    if (v === 0) { s.fg = -1; s.bg = -1; s.bold = s.dim = s.it = s.ul = s.rev = 0; }
    else if (v === 1) s.bold = 1;
    else if (v === 2) s.dim = 1;
    else if (v === 3) s.it = 1;
    else if (v === 4) s.ul = 1;
    else if (v === 7) s.rev = 1;
    else if (v === 22) { s.bold = 0; s.dim = 0; }
    else if (v === 23) s.it = 0;
    else if (v === 24) s.ul = 0;
    else if (v === 27) s.rev = 0;
    else if (v >= 30 && v <= 37) s.fg = v - 30;
    else if (v === 39) s.fg = -1;
    else if (v >= 40 && v <= 47) s.bg = v - 40;
    else if (v === 49) s.bg = -1;
    else if (v >= 90 && v <= 97) s.fg = v - 90 + 8;
    else if (v >= 100 && v <= 107) s.bg = v - 100 + 8;
    else if (v === 38 || v === 48) {
      // 256 色 / 真彩色：这里只取 16 色近似，其余跳过（排障可读性不受影响）
      if (list[i + 1] === 5) {
        const c = list[i + 2];
        if (typeof c === 'number') { if (v === 38) s.fg = c < 16 ? c : 7; else s.bg = c < 16 ? c : 0; }
        i += 2;
      } else if (list[i + 1] === 2) { i += 4; }
    }
    i++;
  }
  WS.key = wsStyleKey();
}

function wsEraseLine(mode) {
  const row = WS.grid[WS.cy];
  const from = mode === 0 ? WS.cx : 0;
  const to = mode === 1 ? WS.cx + 1 : WS.cols;
  for (let x = from; x < to && x < WS.cols; x++) row[x] = { c: ' ', k: WS.key };
  WS.dirty.add(WS.cy);
}

function wsEraseDisplay(mode) {
  if (mode === 0) { wsEraseLine(0); for (let y = WS.cy + 1; y < WS.rows; y++) wsClearRow(y); }
  else if (mode === 1) { wsEraseLine(1); for (let y = 0; y < WS.cy; y++) wsClearRow(y); }
  else { for (let y = 0; y < WS.rows; y++) wsClearRow(y); }
}

function wsInsertLines(n) {
  for (let i = 0; i < n; i++) { WS.grid.splice(WS.cy, 0, wsBlankRow(WS.cols)); WS.grid.pop(); }
  for (let y = WS.cy; y < WS.rows; y++) WS.dirty.add(y);
}
function wsDeleteLines(n) {
  for (let i = 0; i < n; i++) { WS.grid.splice(WS.cy, 1); WS.grid.push(wsBlankRow(WS.cols)); }
  for (let y = WS.cy; y < WS.rows; y++) WS.dirty.add(y);
}
function wsInsertChars(n) {
  const row = WS.grid[WS.cy], blanks = [];
  for (let i = 0; i < n; i++) blanks.push({ c: ' ', k: WS.key });
  row.splice(WS.cx, 0, ...blanks);
  row.length = WS.cols;
  WS.dirty.add(WS.cy);
}
function wsDeleteChars(n) {
  const row = WS.grid[WS.cy];
  row.splice(WS.cx, n);
  while (row.length < WS.cols) row.push({ c: ' ', k: WS.key });
  WS.dirty.add(WS.cy);
}

function wsCsi(params, final) {
  const p = params.split(';').map(x => parseInt(x, 10));
  const n1 = isNaN(p[0]) ? 1 : p[0];
  const cxMax = WS.cols - 1, cyMax = WS.rows - 1;
  switch (final) {
    case 'A': WS.cy = Math.max(0, WS.cy - n1); break;                       // 光标上移
    case 'B': WS.cy = Math.min(cyMax, WS.cy + n1); break;                   // 下移
    case 'C': WS.cx = Math.min(cxMax, WS.cx + n1); break;                   // 右移
    case 'D': WS.cx = Math.max(0, WS.cx - n1); break;                       // 左移
    case 'E': WS.cy = Math.min(cyMax, WS.cy + n1); WS.cx = 0; break;
    case 'F': WS.cy = Math.max(0, WS.cy - n1); WS.cx = 0; break;
    case 'G': case '`': WS.cx = Math.max(0, Math.min(cxMax, (p[0] || 1) - 1)); break;
    case 'H': case 'f':
      WS.cy = Math.max(0, Math.min(cyMax, (p[0] || 1) - 1));
      WS.cx = Math.max(0, Math.min(cxMax, (p[1] || 1) - 1));
      break;
    case 'J': wsEraseDisplay(p[0] || 0); break;
    case 'K': wsEraseLine(p[0] || 0); break;
    case 'L': wsInsertLines(n1); break;
    case 'M': wsDeleteLines(n1); break;
    case 'P': wsDeleteChars(n1); break;
    case '@': wsInsertChars(n1); break;
    case 'm': wsSgr(p); break;
    case 's': WS.saved = { x: WS.cx, y: WS.cy }; break;
    case 'u': if (WS.saved) { WS.cx = WS.saved.x; WS.cy = WS.saved.y; } break;
    default: break;   // h/l（隐藏光标、启用 Alternate Screen 等）不影响显示，忽略
  }
}

/* 字节流 → 屏幕。状态机：0 普通 / 1 ESC / 2 CSI / 3 OSC / 4 字符集 / 5 OSC-ESC */
let WS_STATE = 0, WS_BUF = '';

function wsPutCh(ch) {
  if (ch === '\n') { wsNewline(); return; }
  if (ch === '\r') { WS.cx = 0; return; }
  if (ch === '\b') { WS.cx = Math.max(0, WS.cx - 1); return; }
  if (ch === '\t') { WS.cx = Math.min(WS.cols - 1, (Math.floor(WS.cx / 8) + 1) * 8); return; }
  if (ch === '\x07') return;   // 响铃：浏览器里别出声
  if (ch < ' ') return;        // 其余控制字符不显示在屏幕上
  wsPut(ch);
}

/* 跨帧喂数据。注意三点：
   1) 必须按码点遍历（Array.from），不能按 UTF-16 单元 —— 否则 emoji / CJK 扩展区
      字符会被拆成两个半 surrogate，显示出来就是「断字」；
   2) 服务端虽然已经保证不在半个 UTF-8 字符上切断，但 JSON 传输前后仍有拼接场景，
      这里再兜一层：末尾若是不完整的代理对，留在缓冲里等下一帧；
   3) 只累计脏行，真正画到 DOM 交给下一帧统一做（见 wsRequestRender）。 */
function wsFeed(text) {
  if (!WS.grid || !text) return;
  if (WS.carry) { text = WS.carry + text; WS.carry = ''; }
  // 末尾是孤立高位代理 → 可能还有半个字符没到
  const tail = text.charCodeAt(text.length - 1);
  if (tail >= 0xD800 && tail <= 0xDBFF) {
    WS.carry = text.slice(-1);
    text = text.slice(0, -1);
  }
  const chars = Array.from(text);
  for (let i = 0; i < chars.length; i++) {
    const ch = chars[i];
    if (WS_STATE === 0) {
      if (ch === '\x1b') { WS_STATE = 1; WS_BUF = ''; }
      else wsPutCh(ch);
    } else if (WS_STATE === 1) {
      if (ch === '[') { WS_STATE = 2; WS_BUF = ''; }
      else if (ch === ']') { WS_STATE = 3; WS_BUF = ''; }
      else if (ch === '(' || ch === ')' || ch === '*' || ch === '+') { WS_STATE = 4; }
      else if (ch === 'M') { WS.cy = Math.max(0, WS.cy - 1); WS_STATE = 0; }  // 反向换行
      else if (ch === '7') { WS.saved = { x: WS.cx, y: WS.cy }; WS_STATE = 0; }
      else if (ch === '8') { if (WS.saved) { WS.cx = WS.saved.x; WS.cy = WS.saved.y; } WS_STATE = 0; }
      else WS_STATE = 0;
    } else if (WS_STATE === 2) {
      const c = ch.charCodeAt(0);
      if ((c >= 0x30 && c <= 0x3f) || (c >= 0x20 && c <= 0x2f)) WS_BUF += ch;   // 参数 / 中间字节
      else if (c >= 0x40 && c <= 0x7e) { wsCsi(WS_BUF, ch); WS_STATE = 0; }      // 终止字节
      else WS_STATE = 0;
    } else if (WS_STATE === 3) {
      // OSC（设置窗口标题等）：到 BEL 或 ESC \ 结束，内容一律丢弃
      if (ch === '\x07') WS_STATE = 0;
      else if (ch === '\x1b') WS_STATE = 5;
    } else if (WS_STATE === 5) WS_STATE = 0;
    else WS_STATE = 0;
  }
}

/* ---------------------------- 渲染 ---------------------------- */
function wsSpan(k, text) {
  if (!text) return '';
  if (!k) return esc(text);
  const a = wsStyleAttr(k), at = [];
  if (a.cls) at.push('class="' + a.cls + '"');
  if (a.style) at.push('style="' + a.style + '"');
  return '<span' + (at.length ? ' ' + at.join(' ') : '') + '>' + esc(text) + '</span>';
}

/* curX >= 0 时在该列画光标方块；其余行传 -1 */
function wsRowHtml(row, curX) {
  let last = row.length - 1;
  while (last >= 0 && row[last].c === ' ' && !row[last].k) last--;
  if (curX >= 0 && curX > last) last = curX;
  if (last < 0) return '';
  let html = '', ck = null, buf = '';
  const flush = () => { if (ck !== null) { html += wsSpan(ck, buf); buf = ''; ck = null; } };
  for (let x = 0; x <= last; x++) {
    const cell = row[x] || { c: ' ', k: '' };
    if (cell.pad) continue;            // 宽字符的第二格，不重复输出
    if (x === curX) {
      flush();
      const a = wsStyleAttr(cell.k), at = [];
      at.push('class="tcur' + (a.cls ? ' ' + a.cls : '') + '"');
      if (a.style) at.push('style="' + a.style + '"');
      html += '<span ' + at.join(' ') + '>' + esc(cell.c || ' ') + '</span>';
      continue;
    }
    if (ck === null) { ck = cell.k; buf = cell.c; continue; }
    if (cell.k === ck) { buf += cell.c; continue; }
    flush(); ck = cell.k; buf = cell.c;
  }
  flush();
  return html;
}

function wsScrollbackPush(row) {
  const sb = $('#ws-scroll');
  if (!sb) return;
  const d = document.createElement('div');
  d.className = 'tl';
  d.innerHTML = wsRowHtml(row, -1);
  if (sb.appendChild) sb.appendChild(d);
  while ((sb.childElementCount || 0) > WS_MAX_SB && sb.removeChild) sb.removeChild(sb.firstChild);
}

/* 渲染合并到动画帧：一帧内可能连着收到好几段输出，如果每段都立刻改 innerHTML
   并读 scrollHeight，浏览器会被迫连续同步重排 —— 输出一大就卡成几秒一跳。
   这里只记「脏了」，真正画到 DOM 每帧最多一次。 */
let WS_RAF = null;
let WS_RAF_FORCE = false;
function wsRequestRender(force) {
  if (force) WS_RAF_FORCE = true;
  if (WS_RAF != null) return;
  const run = () => {
    WS_RAF = null;
    wsRenderScreen();
    wsAutoScroll(WS_RAF_FORCE);
    WS_RAF_FORCE = false;
  };
  if (typeof requestAnimationFrame === 'function') WS_RAF = requestAnimationFrame(run);
  else WS_RAF = setTimeout(run, 16);
}

function wsRenderScreen() {
  const el = $('#ws-screen');
  if (!el || !WS.grid) return;
  if (WS.lineEls.length !== WS.rows) {
    el.innerHTML = '';
    WS.lineEls = [];
    for (let y = 0; y < WS.rows; y++) {
      const d = document.createElement('div');
      d.className = 'tl';
      if (el.appendChild) el.appendChild(d);
      WS.lineEls.push(d);
    }
    for (let y = 0; y < WS.rows; y++) WS.dirty.add(y);
  }
  // 光标所在行每帧都要重画（光标可能只是挪了个位置，没有字符变化）
  const cy = Math.max(0, Math.min(WS.rows - 1, WS.cy));
  WS.dirty.add(cy);
  if (typeof WS._cyPrev === 'number') WS.dirty.add(Math.max(0, Math.min(WS.rows - 1, WS._cyPrev)));
  WS._cyPrev = cy;
  WS.dirty.forEach(y => {
    const d = WS.lineEls[y];
    if (d) d.innerHTML = wsRowHtml(WS.grid[y], y === cy ? WS.cx : -1);
  });
  WS.dirty.clear();
}

function wsAutoScroll(force) {
  const el = $('#ws-term');
  if (!el) return;
  const max = Number(el.scrollHeight) || 0;
  const top = Number(el.scrollTop) || 0;
  const h = Number(el.clientHeight) || max;
  if (force || (max - top - h) < WS_TAIL_GAP) {
    try { el.scrollTop = max; } catch (e) { /* 少数环境是只读的，忽略 */ }
  }
  wsSyncTailBtn();
}

function wsNote(text) {
  wsFeed(text + '\r\n');
  wsRequestRender(true);
}

/* 清屏：只清本地这份 ANSI 状态，不向服务端发任何东西 —— 与在终端里敲
   clear / Ctrl+L 等价。会话、进程、历史都保留，服务端 PTY 完全不知情。
   （服务端只推原始字节流，客户端自己解释，所以本地清屏是安全的：
     全屏程序如 top/vi 下一次刷新会整屏重绘。） */
function wsClearScreen() {
  wsGridInit(WS.cols || 100, WS.rows || 26);
  WS.carry = '';
  const sb = $('#ws-scroll');
  if (sb) sb.innerHTML = '';
  wsRenderScreen();
  const el = $('#ws-term');
  if (el) { try { el.scrollTop = 0; } catch (e) { /* 只读环境忽略 */ } }
  wsSyncTailBtn();
}

/* ---------------------------- 尺寸测量与同步 ---------------------------- */
function wsBox() {
  const el = $('#ws-term');
  if (!el) return null;
  let w = Number(el.clientWidth) || 0;
  let h = Number(el.clientHeight) || 0;
  if (!w || !h) return null;
  if (typeof getComputedStyle === 'function') {
    const cs = getComputedStyle(el);
    w -= (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
    h -= (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  }
  return (w > 60 && h > 60) ? { w: w, h: h } : null;
}

function wsMetrics() {
  const el = $('#ws-term');
  if (!el || !el.appendChild) return null;
  const probe = document.createElement('div');
  probe.className = 'tl ws-probe';
  probe.textContent = 'M'.repeat(100);
  probe.style.position = 'absolute';
  probe.style.left = '-99999px';
  probe.style.top = '0';
  try {
    el.appendChild(probe);
    if (typeof probe.getBoundingClientRect !== 'function') return null;
    const r = probe.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return null;
    return { cw: r.width / 100, lh: r.height };
  } catch (e) {
    return null;
  } finally {
    if (probe.remove) probe.remove();
  }
}

function wsMeasure() {
  const box = wsBox(), m = wsMetrics();
  if (!box || !m) return { cols: WS.cols || 100, rows: WS.rows || 26 };
  return {
    cols: Math.max(20, Math.min(300, Math.floor(box.w / m.cw))),
    rows: Math.max(6, Math.min(120, Math.floor(box.h / m.lh))),
  };
}

function wsResizeGrid(cols, rows) {
  const old = WS.grid, oc = WS.cols, or = WS.rows;
  const ng = [];
  for (let y = 0; y < rows; y++) {
    const src = (old && y < or) ? old[y] : null;
    const row = new Array(cols);
    for (let x = 0; x < cols; x++) row[x] = (src && x < oc) ? src[x] : { c: ' ', k: '' };
    ng.push(row);
  }
  WS.grid = ng; WS.cols = cols; WS.rows = rows;
  WS.cx = Math.max(0, Math.min(cols - 1, WS.cx));
  WS.cy = Math.max(0, Math.min(rows - 1, WS.cy));
  WS.dirty = new Set();
  WS.lineEls = [];
  for (let y = 0; y < rows; y++) WS.dirty.add(y);
}

let WS_RS_T = null;
function wsOnResize() {
  if (!WS.sid) return;
  clearTimeout(WS_RS_T);
  WS_RS_T = setTimeout(() => {
    const m = wsMeasure();
    if (m.cols === WS.cols && m.rows === WS.rows) return;
    wsResizeGrid(m.cols, m.rows);
    wsRenderScreen();
    const sz = $('#ws-size'); if (sz) sz.textContent = m.cols + '×' + m.rows;
    api('/api/webshell/resize', { method: 'POST', body: { sid: WS.sid, cols: m.cols, rows: m.rows } });
  }, 250);
}

/* ---------------------------- 会话读写 ---------------------------- */
function wsStop() {
  if (WS_PUMP) { clearTimeout(WS_PUMP); WS_PUMP = null; }
  if (WS_RAF != null) {
    if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(WS_RAF);
    else clearTimeout(WS_RAF);
    WS_RAF = null;
  }
}

function wsSchedule(ms) {
  if (!WS.sid) return;
  if (WS_PUMP) clearTimeout(WS_PUMP);
  WS_PUMP = setTimeout(wsPump, ms);
}

function wsPump() {
  WS_PUMP = null;
  if (!WS.sid) return;
  if (!$('#ws-screen')) return;      // 已经离开终端页，别再空转
  if (WS_BUSY) { wsSchedule(80); return; }
  WS_BUSY = true;
  // wait 让服务端在没有输出时挂起最多 1.2 秒，输出一到立刻返回 —— 既省掉
  // 每秒十几次空轮询，又让命令的回显几乎零延迟出现
  api('/api/webshell/read', { method: 'POST', body: { sid: WS.sid, wait: 1.2 } })
    .then(r => {
      if (!r.ok) {
        if (r.code === 'NOSESS') wsLost(T('会话已失效，请重新连接'));
        else wsLost(T('读取失败：') + (r.msg_cn || T('未知错误')));
        return;
      }
      const d = r.data || {};
      if (d.data) { wsFeed(d.data); wsRequestRender(); }
      if (d.exit != null && d.exit >= 0) {
        wsNote(T('\\r\\n[shell 已结束，退出码 ') + d.exit + ']');
        wsStop();
        return;
      }
      // 长轮询：有输出说明可能还有后续，立刻再取一次；空手而归说明 shell 空闲。
      // 留 120ms 下限是给「守护还是旧版、不支持 wait」的情况兜底 ——
      // 否则会变成 0 延迟空转，把管理服务打满。
      wsSchedule(d.data ? 0 : (document.hidden ? 1500 : 120));   // 后台标签页降频，省电省请求
    })
    .catch(() => wsSchedule(500))
    .then(() => { WS_BUSY = false; });
}

function wsLost(msg) {
  wsStop();
  WS.sid = null;
  const st = $('#ws-status');
  if (st) st.innerHTML = `<span class="tag err">${T('已断开')}</span>`;
  const c = $('#ws-connect'), d = $('#ws-disconnect');
  if (c) c.disabled = false;
  if (d) d.disabled = true;
  wsNote(T('\\r\\n[连接中断] ') + msg);
  toast(msg, 'err', 5000);
}

function wsWrite(data) {
  if (!WS.sid || !data) return;
  if (data.length <= WS_WRITE_CHUNK) { wsPostWrite(data); return; }
  // 粘贴长文本时分片：一口气几万字节会让服务端写入阻塞，也会让 shell 逐行执行
  let i = 0, n = 0;
  for (; i < data.length; i += WS_WRITE_CHUNK) {
    const part = data.slice(i, i + WS_WRITE_CHUNK);
    setTimeout(part2 => wsPostWrite(part2), n * 25, part);
    n++;
  }
}

function wsPostWrite(data) {
  api('/api/webshell/write', { method: 'POST', body: { sid: WS.sid, data: data } })
    .then(r => {
      if (!r.ok && r.code === 'NOSESS') { wsLost(T('会话已失效，请重新连接')); return; }
      wsSchedule(15);              // 刚输入完，马上去取回显，手感才跟手
    })
    .catch(() => { /* 轮询会兜住，不弹错误打扰输入 */ });
}

async function wsConnect() {
  const user = ($('#ws-user') || {}).value || 'root';
  const btn = $('#ws-connect');
  if (btn) btn.disabled = true;
  const st = $('#ws-status');
  if (st) st.innerHTML = `<span class="tag info">${T('正在连接…')}</span>`;
  const m = wsMeasure();
  wsGridInit(m.cols, m.rows);
  wsRenderScreen();
  const r = await api('/api/webshell/connect', {
    method: 'POST', body: { user: user, cols: m.cols, rows: m.rows },
  });
  if (btn) btn.disabled = false;
  if (!r.ok) {
    if (st) st.innerHTML = `<span class="tag err">${T('连接失败')}</span>`;
    wsNote(T('连接失败：') + (r.msg_cn || T('未知错误')));
    if (r.code === 'NO_SHELLD') wsNote(T('终端守护没起来：systemctl start drouter-shelld（容器形态无 systemd，重启容器即可）'));
    // PTY_FAIL 里最坑的一种是 /dev/ptmx 入口丢失：明明一个终端都没开，
    // 内核却报「out of pty devices」。守护会尝试自愈；还失败的话给条
    // 能照着敲的命令，别让用户对着 errno 干瞪眼。
    if (r.code === 'PTY_FAIL') {
      wsNote(T('若提示 out of pty devices，通常是 /dev/ptmx 缺失（容器/虚拟化环境常见）。'));
      wsNote(T('排查：ls -l /dev/ptmx   修复：ln -sf pts/ptmx /dev/ptmx'));
      wsNote(T('然后重启守护：systemctl restart drouter-shelld'));
    }
    toast(r.msg_cn || T('连接失败'), 'err', 5000);
    return;
  }
  WS.sid = (r.data || {}).sid;
  const tt = $('#ws-title'); if (tt) tt.textContent = user + '@debian-primaryrouter';
  const sz = $('#ws-size'); if (sz) sz.textContent = m.cols + '×' + m.rows;
  if (st) st.innerHTML = `<span class="tag ok">${T('已连接')}</span>`;
  const d = $('#ws-disconnect'); if (d) d.disabled = false;
  wsSchedule(30);
  const ta = $('#ws-key'); if (ta && ta.focus) ta.focus();
  wsAutoScroll(true);
}

async function wsDisconnect(silent) {
  const sid = WS.sid;
  wsStop();
  WS.sid = null;
  const st = $('#ws-status');
  if (st) st.innerHTML = `<span class="tag gray">${T('已断开')}</span>`;
  const c = $('#ws-connect'), d = $('#ws-disconnect');
  if (c) c.disabled = false;
  if (d) d.disabled = true;
  const tt = $('#ws-title'); if (tt) tt.textContent = T('未连接');
  if (sid) {
    // 断开请求不 await 结果：就算服务端已经回收了会话，本地状态也必须立刻清干净
    api('/api/webshell/disconnect', { method: 'POST', body: { sid: sid } });
    if (!silent) wsNote(T('\\r\\n—— 已断开 ——'));
  }
}

/* ---------------------------- 键盘输入 ---------------------------- */
const WS_MAP = {
  Enter: '\r', Backspace: '\x7f', Tab: '\t', Escape: '\x1b',
  ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D',
  Home: '\x1b[H', End: '\x1b[F', Delete: '\x1b[3~', Insert: '\x1b[2~',
  PageUp: '\x1b[5~', PageDown: '\x1b[6~',
  F1: '\x1bOP', F2: '\x1bOQ', F3: '\x1bOR', F4: '\x1bOS',
};

function wsSelection() {
  try {
    if (typeof window !== 'undefined' && typeof window.getSelection === 'function') {
      return String(window.getSelection() || '');
    }
  } catch (e) { /* 某些环境取不到，按没有选中处理 */ }
  return '';
}

function wsKeyDown(e) {
  if (!WS.sid) { e.preventDefault(); return; }
  if (e.isComposing) return;                     // 输入法组词中，交给 compositionend
  if (e.ctrlKey && !e.altKey && !e.metaKey) {
    const k = String(e.key || '').toLowerCase();
    if (k === 'v') return;                        // 粘贴交给浏览器（随后 input 事件会拿到文本）
    if (k === 'c' && wsSelection()) return;       // 有选中内容时 Ctrl+C 是复制，不是 SIGINT
    if (/^[a-z]$/.test(k)) {
      e.preventDefault();
      wsWrite(String.fromCharCode(k.charCodeAt(0) - 96));
      return;
    }
  }
  if (e.altKey && !e.ctrlKey && !e.metaKey && String(e.key || '').length === 1) {
    e.preventDefault(); wsWrite('\x1b' + e.key); return;
  }
  const m = WS_MAP[e.key];
  if (m) { e.preventDefault(); wsWrite(m); return; }
}

/* ---------------------------- 页面装配 ---------------------------- */
async function viewWebShell() {
  const v = $('#view');
  v.innerHTML = `
    <div class="card">
      <h3>Web ${T('终端（本机')} Shell）</h3>
      <p class="desc">${T('在浏览器里直接打开本机')} shell${T('。这是')}<b>${T('实时交互终端')}</b>${T('：每敲一个键立刻发到服务端， 回显由内核产生，所以')} <span class="mono">top</span>、<span class="mono">vi</span>、
      <span class="mono">passwd</span> ${T('这类需要交互的程序都能正常用，')}Ctrl+C ${T('中断、 方向键翻历史也都有效。')}</p>
      <div class="row" style="align-items:center;gap:8px;flex-wrap:wrap">
        <label>${T('登录身份')}<select id="ws-user" style="width:190px">
          <option value="root">root${T('（超级用户）')}</option>
          <option value="ajeef">ajeef${T('（管理员）')}</option>
          <option value="drouter">drouter${T('（服务账号）')}</option>
        </select></label>
        <button id="ws-connect" class="primary">${T('连接')}</button>
        <button id="ws-disconnect" class="ghost" disabled>${T('断开')}</button>
        <button id="ws-clear" class="ghost small" title="${T('清屏：只清本地显示，不中断会话')}">${T('清屏')}</button>
        <button id="ws-copy" class="ghost small">${T('复制可见内容')}</button>
        <span class="desc" id="ws-status" style="margin:0"></span>
      </div>
      <p class="desc" style="margin-top:8px">
        ${T('提示：这是')}<b>${T('本机终端，不是')} SSH ${T('跳板')}</b>；root shell ${T('权限等同物理控制台， 开启与输入都会写审计日志，闲置')} 30 ${T('分钟自动回收。 有选中文字时')} Ctrl+C ${T('仍是「复制」，否则是「中断当前命令」。')}
      </p>
    </div>

    <div class="card">
      <h3>${T('终端输出')}</h3>
      <div class="term-wrap">
        <div class="term-bar">
          <span class="term-dot r"></span><span class="term-dot y"></span><span class="term-dot g"></span>
          <span class="term-title" id="ws-title">${T('未连接')}</span>
          <button type="button" class="term-btn" id="ws-tobottom" hidden
            title="${T('回到底部，继续跟随最新输出')}">↓ ${T('最新')}</button>
          <button type="button" class="term-btn" id="ws-clearbar"
            title="${T('清屏：只清本地显示，不中断会话（等价于命令')} clear）">${T('清屏')}</button>
          <span class="tag gray" id="ws-size">—</span>
        </div>
        <div class="term-body term-screen" id="ws-term" tabindex="0">
          <div id="ws-scroll"></div>
          <div id="ws-screen"></div>
        </div>
        <textarea id="ws-key" class="ws-capture" spellcheck="false" autocomplete="off"
          autocapitalize="off" autocorrect="off" aria-label="${T('终端输入')}"></textarea>
      </div>
      <p class="desc">${T('点击终端区域即可开始输入；窗口大小变化时会自动同步给服务端。 长回显会')}<b>${T('自动跟随最新输出')}</b>${T('（右侧不显示滚动条）')}——${T('想翻看历史用滚轮或')}
        <span class="mono">PgUp</span>${T('，翻上去之后标题栏会出现「')}↓ ${T('最新」按钮一键回到底部。 清屏只清本地显示，会话和正在跑的程序都不受影响。')}</p>
    </div>

    <div class="card" id="fm-card">
      <h3>${T('文件管理器')}</h3>
      <p class="desc">${T('支持上传（')}SFTP/SCP${T('）、下载、文件夹打包为')} zip${T('、重命名、删除、复制路径。删除会移入回收站。')}</p>
      <div class="fm-toolbar">
        <button id="fm-up" class="ghost small">↑ ${T('上级')}</button>
        <button id="fm-ref" class="ghost small">${T('刷新')}</button>
        <button id="fm-mkdir" class="ghost small">${T('新建文件夹')}</button>
        <div class="fm-path mono" id="fm-path">/root</div>
        <label class="ghost small fm-upload">
          ${T('上传文件')}<input type="file" id="fm-file" multiple hidden>
        </label>
        <span class="desc" id="fm-msg" style="margin:0"></span>
      </div>
      <div class="drop-zone" id="fm-drop">${T('把文件拖到这里上传（或点击「上传文件」）')}</div>
      <div id="fm-list"><p class="desc">${T('正在载入')}…</p></div>
      <div id="fm-progress"></div>
    </div>

    <div class="card hidden" id="fm-preview-card">
      <h3>${T('文件预览')} <span class="desc" id="fm-prev-name" style="margin:0"></span>
        <button class="ghost small copy-btn" data-copy="#fm-prev" style="float:right">${T('复制内容')}</button></h3>
      <pre id="fm-prev" class="log-pre"></pre>
    </div>`;

  WS.lineEls = [];
  const m = wsMeasure();
  wsGridInit(m.cols, m.rows);
  const sz = $('#ws-size'); if (sz) sz.textContent = m.cols + '×' + m.rows;
  if (WS.sid) {
    // 从别的页面切回来：会话还在，直接把已有屏幕画出来继续轮询
    wsRenderScreen();
    wsAutoScroll(true);
    wsSchedule(30);
    const st = $('#ws-status');
    if (st) st.innerHTML = `<span class="tag ok">${T('已连接')}</span>`;
    const d = $('#ws-disconnect'); if (d) d.disabled = false;
    const tt = $('#ws-title'); if (tt) tt.textContent = T('已连接终端');
  } else {
    wsNote(T('未连接 —— 点击上方「连接」开始。'));
    wsNote(T('这是本机 shell，输入以 root / ajeef / drouter 身份执行。'));
  }
  wsInit();
  fmInit('/root');
  bindCopyButtons(v);
}

function bindCopyButtons(root) {
  (root || document).querySelectorAll('.copy-btn').forEach(b => {
    b.onclick = () => {
      const el = document.querySelector(b.dataset.copy);
      if (!el) return;
      const txt = el.textContent || '';
      navigator.clipboard.writeText(txt).then(
        () => toast(T('已复制到剪贴板'), 'ok'),
        () => { // 降级
          const ta = document.createElement('textarea');
          ta.value = txt; document.body.appendChild(ta); ta.select();
          try { document.execCommand('copy'); toast(T('已复制'), 'ok'); } catch (e) { toast(T('复制失败'), 'err'); }
          ta.remove();
        });
    };
  });
}

/* ---------- Web 终端：初始化 ---------- */
function wsInit() {
  const c = $('#ws-connect'), d = $('#ws-disconnect');
  if (c) c.onclick = wsConnect;
  if (d) d.onclick = () => wsDisconnect(false);
  if ($('#ws-clear')) $('#ws-clear').onclick = wsClearScreen;
  if ($('#ws-clearbar')) $('#ws-clearbar').onclick = wsClearScreen;
  if ($('#ws-copy')) $('#ws-copy').onclick = wsCopyAll;
  // 隐藏滚动条后，用户滚上去就没有「拖回底部」这个操作了，
  // 所以给一个显式的「↓ 最新」按钮，只在离开底部时才出现。
  const tb = $('#ws-tobottom');
  if (tb) tb.onclick = () => wsAutoScroll(true);
  const box = $('#ws-term');
  if (box && box.addEventListener) box.addEventListener('scroll', wsSyncTailBtn, { passive: true });
  wsBindInput();
  wsSyncTailBtn();
}

/* 距底部 WS_TAIL_GAP 内算「在底部」：此时不需要按钮，且新输出继续自动跟随。 */
function wsSyncTailBtn() {
  const box = $('#ws-term'), tb = $('#ws-tobottom');
  if (!box || !tb) return;
  const max = Number(box.scrollHeight) || 0;
  const top = Number(box.scrollTop) || 0;
  const h = Number(box.clientHeight) || max;
  const atBottom = (max - top - h) < WS_TAIL_GAP;
  if (atBottom) {
    if (!tb.hidden) { tb.hidden = true; }
  } else if (tb.hidden) {
    tb.hidden = false;
  }
}

function wsBindInput() {
  const ta = $('#ws-key'), term = $('#ws-term');
  if (!ta) return;
  if (term && term.addEventListener) {
    // 点终端区域就把焦点给隐藏输入框。
    // 用 mouseup 而不是 mousedown：mousedown 时还没有选区，抢焦点会把用户
    // 正在拖的选区清掉，表现为「终端里的文字选不中」。mouseup 时若已有选区
    // 说明用户在复制，就别抢。
    term.addEventListener('mouseup', () => {
      if (wsSelection()) return;
      if (ta.focus) ta.focus();
    });
  }
  if (ta.addEventListener) {
    ta.addEventListener('keydown', wsKeyDown);
    // 普通字符 / 粘贴 / 输入法提交都走这里：把输入框内容取走发往服务端再清空
    ta.addEventListener('input', e => {
      if (e && e.isComposing) return;
      const v = ta.value; ta.value = '';
      if (v) wsWrite(v);
    });
    ta.addEventListener('compositionend', () => {
      const v = ta.value; ta.value = '';
      if (v) wsWrite(v);
    });
  }
}

function wsScreenText() {
  const lines = [];
  if (WS.grid) for (let y = 0; y < WS.rows; y++) {
    const row = WS.grid[y];
    let s = '';
    for (let x = 0; x < WS.cols; x++) s += row[x].c;
    lines.push(s.replace(/\s+$/, ''));
  }
  while (lines.length && !lines[lines.length - 1]) lines.pop();
  return lines.join('\n');
}

function wsCopyAll() {
  const txt = wsScreenText();
  if (!txt) { toast(T('屏幕是空的'), 'warn'); return; }
  const ta = document.createElement('textarea');
  ta.value = txt;
  if (document.body && document.body.appendChild) document.body.appendChild(ta);
  if (ta.select) ta.select();
  let done = false;
  try { done = document.execCommand ? document.execCommand('copy') : false; } catch (e) { done = false; }
  if (ta.remove) ta.remove();
  toast(done ? T('已复制可见内容') : T('复制失败，请手动选中'), done ? 'ok' : 'err');
}

// 关页面 / 刷新时主动断开，别把 root shell 留在服务端等 30 分钟超时
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('resize', wsOnResize);
  window.addEventListener('beforeunload', () => { if (WS.sid) wsDisconnect(true); });
}

/* ============================ 通用 API 接口 ============================ */
async function viewApi() {
  const v = $('#view');
  v.innerHTML = `
    <div class="card">
      <h3>${T('对外通用')} API ${T('接口')}</h3>
      <p class="desc">Drouter ${T('提供统一的')} JSON REST ${T('接口，跨平台、跨语言、跨框架： 任意支持')} HTTP ${T('的环境（')}curl、Python、Node、Go、PHP、Java、.NET、Shell、
      Postman${T('、工单系统、')}IoT ${T('网关、')}HomeAssistant…${T('）都能直接接入，无需任何')} SDK。</p>
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <button id="api-load" class="primary">${T('载入接口清单与范例')}</button>
        <a id="api-openapi" class="ghost small" href="/api/openapi.json" target="_blank" rel="noopener">${T('查看接口描述（')}JSON）</a>
        <span class="desc" id="api-base" style="margin:0"></span>
      </div>
      <div class="kv"><b>${T('响应格式')}</b><span class="mono">{"ok": true, "msg_cn": "…", "data": …}</span></div>
      <div class="kv"><b>${T('鉴权方式')}</b><span><span class="mono">POST /api/login</span> ${T('取 token')} → ${T('之后每个请求带')} <span class="mono">X-Token</span> ${T('头')}</span></div>
      <div class="kv"><b>${T('跨域支持')}</b><span>${T('服务端已放开跨域，浏览器端也可直接调用')}</span></div>
    </div>
    <div id="api-body"><p class="desc">${T('点击「载入接口清单与范例」查看。')}</p></div>`;
  $('#api-load').onclick = loadApi;
  loadApi();
}

async function loadApi() {
  const box = $('#api-body');
  box.innerHTML = `<p class="desc">${T('载入中…')}</p>`;
  const [ex, desc] = await Promise.all([api('/api/examples'), api('/api/openapi')]);
  if (!ex.ok) { box.innerHTML = `<div class="notice err">${esc(ex.msg_cn || T('载入失败'))}</div>`; return; }
  const d = ex.data || {};
  if ($('#api-base')) $('#api-base').innerHTML = `${T('服务地址：')}<span class="mono">${esc(d.base || '')}</span>`;
  const paths = (desc.ok && desc.data && desc.data.paths) || {};
  const groups = {};
  Object.entries(paths).forEach(([path, methods]) => {
    Object.entries(methods).forEach(([m, node]) => {
      const tag = (node.tags || [T('通用')])[0];
      (groups[tag] = groups[tag] || []).push({ path, m, sum: node.summary || '' });
    });
  });
  const epHtml = Object.entries(groups).map(([tag, list]) => `
    <div class="card">
      <h3>${esc(oaT(tag))} <span class="tag gray">${list.length}</span></h3>
      ${list.map(x => `<div class="probe-row">
        <span class="pk"><span class="tag ${x.m === 'get' ? 'info' : 'ok'}">${x.m.toUpperCase()}</span>
          <span class="mono">${esc(x.path)}</span></span>
        <span class="pv">${esc(oaT(x.sum))}</span>
      </div>`).join('')}
    </div>`).join('');

  const exHtml = (d.examples || []).map((e, i) => `
    <div class="card">
      <h3>${esc(oaT(e.title))}
        <button class="ghost small copy-btn" data-copy="#ex-${i}" style="float:right">${T('复制代码')}</button></h3>
      <div class="code-wrap">
        <pre id="ex-${i}" class="log-pre">${esc(oaLines(e.code))}</pre>
      </div>
    </div>`).join('');

  box.innerHTML = `<div class="notice ok" style="margin-bottom:12px">${esc(bt4('PAGE_NOTES', d.note, 'd', d.note) || '')}</div>`
    + epHtml + exHtml;
  bindCopyButtons(box);
}


// API 文档页翻译（后端 openapi/examples 为静态中文，按原文查 OPENAPI_TEXTS；代码块逐行翻译）。
function oaT(s) {
  const v = (typeof bt === 'function') ? bt('OPENAPI_TEXTS', s, 'en') : '';
  return v || s;
}
function oaLines(code) {
  const t = String(code == null ? '' : code);
  if (!(window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US')) return t;
  return t.split('\n').map(oaT).join('\n');
}

/* ---------- 文件管理器 ---------- */
let FM_PATH = '/root';
let FM_SEL = new Set();

async function fmInit(path) {
  FM_PATH = path || FM_PATH;
  const up = $('#fm-up'), ref = $('#fm-ref'), mk = $('#fm-mkdir');
  if (!up) return;
  up.onclick = () => fmList(FM_PARENT || '/');
  ref.onclick = () => fmList(FM_PATH);
  mk.onclick = fmMkdir;
  const fileInput = $('#fm-file');
  if (fileInput) fileInput.onchange = () => fmUpload(fileInput.files);
  const drop = $('#fm-drop');
  if (drop) {
    drop.onclick = () => fileInput && fileInput.click();
    ['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => {
      e.preventDefault(); drop.classList.add('over');
    }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
      e.preventDefault(); drop.classList.remove('over');
    }));
    drop.addEventListener('drop', e => {
      if (e.dataTransfer && e.dataTransfer.files.length) fmUpload(e.dataTransfer.files);
    });
  }
  fmList(FM_PATH);
}

let FM_PARENT = '/';

async function fmList(path) {
  const box = $('#fm-list'); if (!box) return;
  box.innerHTML = `<p class="desc">${T('正在载入…')}</p>`;
  const r = await api('/api/files/list?path=' + encodeURIComponent(path));
  if (!r.ok) { box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('无法读取目录'))}</div>`; return; }
  const d = r.data || {};
  FM_PATH = d.path || path;
  FM_PARENT = d.parent || '/';
  FM_SEL = new Set();
  if ($('#fm-path')) $('#fm-path').textContent = FM_PATH;
  const items = d.items || [];
  const rows = items.map(it => `
    <tr class="fm-row" data-path="${esc(it.path)}" data-dir="${it.dir ? 1 : 0}">
      <td class="fm-chk"><input type="checkbox" class="fm-cb" data-path="${esc(it.path)}"></td>
      <td class="fm-name">
        ${it.dir ? '📁' : (it.link ? '🔗' : '📄')}
        <span class="fm-open" data-path="${esc(it.path)}" data-dir="${it.dir ? 1 : 0}">${esc(it.name)}</span>
      </td>
      <td>${it.dir ? '—' : fmtBytes(it.size)}</td>
      <td>${fmtTime(it.mtime)}</td>
      <td class="mono" style="font-size:12px">${esc(it.mode || '')}</td>
      <td class="fm-acts">
        ${!it.dir ? `<button class="ghost small fm-dl" data-path="${esc(it.path)}">${T('下载')}</button>` : `<button class="ghost small fm-zip" data-path="${esc(it.path)}">${T('打包')}</button>`}
        <button class="ghost small fm-ren" data-path="${esc(it.path)}" data-name="${esc(it.name)}">${T('重命名')}</button>
        <button class="ghost small fm-cp" data-path="${esc(it.path)}">${T('复制路径')}</button>
        <button class="ghost small fm-del" data-path="${esc(it.path)}">${T('删除')}</button>
      </td>
    </tr>`).join('');
  const crumbs = (d.crumbs || []).map((c, i, a) =>
    `<span class="fm-crumb" data-path="${esc(c.path)}">${esc(c.name)}</span>${i < a.length - 1 ? '<span class="fm-sep">/</span>' : ''}`).join('');
  box.innerHTML = `
    <div class="fm-crumbs">${crumbs}</div>
    <div class="row" style="gap:8px;margin:8px 0">
      <button class="ghost small" id="fm-zipsel" disabled>${T('打包所选')} zip</button>
      <button class="ghost small" id="fm-delsel" disabled>${T('删除所选')}</button>
      <span class="desc" id="fm-selinfo" style="margin:0"></span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th></th><th>${T('名称')}</th><th>${T('大小')}</th><th>${T('修改时间')}</th><th>${T('权限')}</th><th>${T('操作')}</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6" class="desc">${T('目录为空')}</td></tr>`}</tbody>
    </table></div>`;

  box.querySelectorAll('.fm-open').forEach(el => el.onclick = () => {
    if (el.dataset.dir === '1') fmList(el.dataset.path);
    else fmPreview(el.dataset.path);
  });
  box.querySelectorAll('.fm-dl').forEach(b => b.onclick = () => fmDownload(b.dataset.path));
  box.querySelectorAll('.fm-zip').forEach(b => b.onclick = () => fmZip([b.dataset.path]));
  box.querySelectorAll('.fm-ren').forEach(b => b.onclick = () => fmRename(b.dataset.path, b.dataset.name));
  box.querySelectorAll('.fm-cp').forEach(b => b.onclick = () => {
    navigator.clipboard.writeText(b.dataset.path).then(
      () => toast(T('路径已复制：') + b.dataset.path, 'ok'),
      () => toast(T('复制失败，请手动复制'), 'err'));
  });
  box.querySelectorAll('.fm-del').forEach(b => b.onclick = () => fmDelete([b.dataset.path]));
  box.querySelectorAll('.fm-crumb').forEach(c => c.onclick = () => fmList(c.dataset.path));
  const cbs = box.querySelectorAll('.fm-cb');
  const sync = () => {
    FM_SEL = new Set();
    cbs.forEach(cb => { if (cb.checked) FM_SEL.add(cb.dataset.path); });
    $('#fm-zipsel').disabled = FM_SEL.size === 0;
    $('#fm-delsel').disabled = FM_SEL.size === 0;
    $('#fm-selinfo').textContent = FM_SEL.size ? `${T('已选')} ${FM_SEL.size} ${T('项')}` : '';
  };
  cbs.forEach(cb => cb.onchange = sync);
  $('#fm-zipsel').onclick = () => fmZip(Array.from(FM_SEL));
  $('#fm-delsel').onclick = () => fmDelete(Array.from(FM_SEL));
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function fmDownload(path) {
  toast(T('正在准备下载…'), 'info');
  try {
    const res = await fetch('/api/files/download?path=' + encodeURIComponent(path),
      { headers: S.token ? { 'X-Token': S.token } : {} });
    if (!res.ok) { toast(T('下载失败'), 'err'); return; }
    const blob = await res.blob();
    const name = path.split('/').pop() || 'download';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { toast(T('下载失败：') + netErrCn(e && e.message), 'err'); }
}

async function fmZip(paths) {
  const msg = $('#fm-msg');
  if (msg) msg.textContent = T('正在打包…');
  const r = await api('/api/files/zip', { method: 'POST', body: { paths } });
  if (msg) msg.textContent = '';
  if (!r.ok) { toast(r.msg_cn || T('打包失败'), 'err', 5000); return; }
  toast(`${r.msg_cn}（${r.data.size_h}）`, 'ok');
  // 打包完直接下载
  const f = r.data.file;
  fmDownload(f);
}

async function fmDelete(paths) {
  modal(T('确认删除'), `<p>${T('将把以下')} ${paths.length} ${T('项移入回收站（可在服务器')} <span class="mono">/var/lib/drouter/trash</span> ${T('找回）：')}</p>
    <pre class="log-pre" style="max-height:160px">${paths.map(esc).join('\n')}</pre>`, async () => {
    const r = await api('/api/files/delete', { method: 'POST', body: { paths } });
    if (!r.ok) { toast(r.msg_cn || T('删除失败'), 'err', 5000); return false; }
    toast(r.msg_cn || T('已删除'), 'ok');
    fmList(FM_PATH);
  }, T('移入回收站'));
}

function fmRename(path, name) {
  modal(T('重命名'), `<label>${T('新名称')}<input id="fm-new" value="${esc(name)}"></label>`, async () => {
    const v = ($('#fm-new') || {}).value.trim();
    if (!v) { toast(T('名称不能为空'), 'err'); return false; }
    const r = await api('/api/files/rename', { method: 'POST', body: { path, name: v } });
    if (!r.ok) { toast(r.msg_cn || T('重命名失败'), 'err', 5000); return false; }
    toast(T('已重命名'), 'ok');
    fmList(FM_PATH);
  });
}

function fmMkdir() {
  modal(T('新建文件夹'), `<label>${T('文件夹名称')}<input id="fm-newdir" placeholder="${T('例如')} backup"></label>`, async () => {
    const v = ($('#fm-newdir') || {}).value.trim();
    if (!v) { toast(T('名称不能为空'), 'err'); return false; }
    const r = await api('/api/files/mkdir', { method: 'POST', body: { parent: FM_PATH, name: v } });
    if (!r.ok) { toast(r.msg_cn || T('创建失败'), 'err', 5000); return false; }
    toast(T('已创建'), 'ok');
    fmList(FM_PATH);
  });
}

async function fmPreview(path) {
  const card = $('#fm-preview-card');
  const r = await api('/api/files/read?path=' + encodeURIComponent(path));
  if (!r.ok) { toast(r.msg_cn || T('无法预览'), 'warn', 5000); return; }
  card.classList.remove('hidden');
  $('#fm-prev-name').textContent = `（${r.data.name} · ${r.data.size_h}${r.data.truncated ? T(' · 已截断') : ''}）`;
  $('#fm-prev').textContent = r.data.text || '';
  card.scrollIntoView({ behavior: 'smooth' });
}

function fmUpload(files) {
  if (!files || !files.length) return;
  const prog = $('#fm-progress');
  prog.innerHTML = '';
  Array.from(files).forEach(f => {
    const row = document.createElement('div');
    row.className = 'up-item';
    row.innerHTML = `<div class="up-name">${esc(f.name)} <span class="desc">${fmtBytes(f.size)}</span></div>
      <div class="bar"><i style="width:0%"></i></div><div class="up-st desc">${T('等待')}…</div>`;
    prog.appendChild(row);
    const barEl = row.querySelector('i'), st = row.querySelector('.up-st');
    // 小文件直接走 base64 API；大文件用分片写入
    const reader = new FileReader();
    reader.onload = async () => {
      const b64 = String(reader.result).split(',')[1] || '';
      st.textContent = T('上传中…');
      barEl.style.width = '40%';
      const r = await api('/api/files/write', {
        method: 'POST',
        body: { path: FM_PATH, name: f.name, b64, size: f.size },
      });
      if (r.ok) { barEl.style.width = '100%'; st.innerHTML = `<span style="color:var(--ok)">${T('完成')}</span>`; }
      else { barEl.style.width = '100%'; barEl.style.background = 'var(--err)'; st.textContent = T('失败：') + (r.msg_cn || ''); }
      if (files[files.length - 1] === f) fmList(FM_PATH);
    };
    reader.onerror = () => { st.textContent = T('读取本地文件失败'); };
    reader.readAsDataURL(f);
  });
}

/* ============================ 日志 ============================ */
function viewLog() {
  const mods = ['all', 'system', 'config', 'network', 'dhcp', 'dns', 'firewall', 'ipv6', 'pppoe', 'web', 'user', 'power', 'service', 'pkg', 'helper'];
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('结构化日志（')}AI ${T('可读）')}</h3>
      <p class="desc">JSONL ${T('格式，字段：')}ts / level / module / code / msg_cn / detail${T('。 中文提示便于人工排查，原始信息保留便于')} AI ${T('分析。')}</p>
      <div class="row">
        <label style="flex:0 0 200px">${T('模块')}<select id="lg-mod">${mods.map(m => `<option>${m}</option>`).join('')}</select></label>
        <label style="flex:0 0 120px">${T('条数')}<input id="lg-n" type="number" value="300"></label>
        <button class="ghost fixed" id="lg-load">${T('刷新日志')}</button>
        <button class="ghost fixed" id="lg-export">${T('导出')} JSON</button>
        <button class="ghost fixed" id="lg-journal">${T('查看系统日志')}</button>
      </div>
      <div id="lg-out" style="margin-top:12px"><p class="desc">${T('点击「刷新日志」载入。')}</p></div>
    </div>
    <div class="card">
      <h3>Web ${T('操作审计')}</h3>
      <p class="desc">${T('记录谁在什么时间改了什么配置。')}</p>
      <div id="au-out"><p class="desc">${T('载入中')}…</p></div>
    </div>`;
  $('#lg-load').onclick = loadLogs;
  $('#lg-mod').onchange = loadLogs;
  $('#lg-export').onclick = async () => {
    const r = await api(`/api/logs?module=${$('#lg-mod').value}&limit=5000`);
    const blob = new Blob([JSON.stringify(r.data, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'drouter-log-' + Date.now() + '.json';
    a.click();
  };
  $('#lg-journal').onclick = async () => {
    $('#lg-out').innerHTML = `<pre>${T('读取中…')}</pre>`;
    const r = await api('/api/journal?limit=200');
    $('#lg-out').innerHTML = `<pre>${esc(((r.data || {}).lines || []).join('\n') || T('（无）'))}</pre>`;
  };
  loadLogs();
  api('/api/audit').then(r => {
    const rows = r.data || [];
    $('#au-out').innerHTML = rows.length ? `<table><thead><tr><th style="width:150px">${T('时间')}</th><th style="width:90px">${T('用户')}</th>
      <th style="width:130px">${T('操作')}</th><th>${T('详情')}</th><th style="width:70px">${T('结果')}</th></tr></thead><tbody>
      ${rows.map(x => `<tr><td class="mono">${esc(x.ts)}</td><td>${esc(x.user)}</td><td>${esc(audT(x.action))}</td>
      <td>${esc(audDetail(x.action, x.detail))}</td><td>${x.ok ? `<span class="tag ok">${T('成功')}</span>` : `<span class="tag err">${T('失败')}</span>`}</td></tr>`).join('')}
      </tbody></table>` : `<p class="desc">${T('暂无审计记录。')}</p>`;
  });
}

function logDetailText(v) {
  if (v == null) return '';
  if (typeof v === 'object') { try { return JSON.stringify(v); } catch (e) { return String(v); } }
  return String(v).slice(0, 200);
}

// 结构化日志 detail 在英文界面也要脱中：detail 可能是字符串/数组/对象，
// 先安全序列化（修 [object Object]），再按已知模式把中文计数换成英文。
// 后端零改动：和 ulogMsgI18n 同一思路。
function ulogDetailI18n(d) {
  const s = logDetailText(d);
  if (!(window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US')) return s;
  return s
    .replace(/（(\d+) 个文件）/g, '($1 files)')
    .replace(/(\d+) 个文件（/g, '$1 files (')
    .replace(/(\d+) 个文件/g, '$1 files');
}

async function loadLogs() {
  const out = $('#lg-out');
  out.innerHTML = `<pre>${T('读取中…')}</pre>`;
  const r = await api(`/api/logs?module=${$('#lg-mod').value}&limit=${$('#lg-n').value}`);
  const rows = (r.data || []).reverse();
  if (!rows.length) { out.innerHTML = `<p class="desc">${T('暂无日志记录。')}</p>`; return; }
  out.innerHTML = `<div style="max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:8px">
    ${rows.map(x => `<div class="logline">
      <span class="lv ${esc(x.level)}">${esc(x.level)}</span>
      <span class="ts">${esc(x.ts)}</span>
      <span class="mo">${esc(x.module)}</span>
      <span class="ms">${esc(ulogMsgI18n(x))}${x.detail != null ? ' <span style="color:var(--txt3)">· ' + esc(ulogDetailI18n(x.detail)) + '</span>' : ''}</span>
    </div>`).join('')}</div>`;
}

/* ============================ 电源 ============================ */
function viewPower() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('安全操作')}</h3>
      <p class="desc">${T('正常重启与关机，会先停止服务再断电，对文件系统安全。')}</p>
      <div class="row">
        <button class="fixed" id="pw-reboot">${T('安全重启')}</button>
        <button class="fixed" id="pw-off">${T('安全关机')}</button>
      </div>
    </div>
    <div class="card" style="border-color:#f0c8c3">
      <h3 style="color:var(--err)">${T('强制重启（高风险）')}</h3>
      <p class="desc">${T('直接触发内核')} SysRq ${T('立即重启，')}<b>${T('不等待服务停止')}</b>${T('，可能导致数据丢失。 仅在系统完全无响应时使用。')}</p>
      <div class="row"><button class="danger fixed" id="pw-force">${T('强制重启')}</button></div>
    </div>
    <div class="card">
      <h3>${T('定时重启（可选）')}</h3>
      <p class="desc">${T('按需配置，例如每天凌晨自动重启以释放内存。')}</p>
      <div class="row">
        <label style="flex:0 0 200px">${T('执行时间（每日）')}<input id="pw-time" type="time" value="04:00"></label>
        <button class="ghost fixed" id="pw-cron">${T('生成定时任务说明')}</button>
      </div>
      <p class="hint-inline">${T('为避免误操作，定时重启需在服务器上用')} <span class="mono">systemd timer</span> ${T('配置，此处仅提供模板。')}</p>
    </div>`;
  $('#pw-reboot').onclick = () => confirmPower('reboot', T('确认重启'), T('安全重启'));
  $('#pw-off').onclick = () => confirmPower('poweroff', T('确认关机'), T('安全关机'));
  $('#pw-force').onclick = () => confirmPower('force_reboot', T('确认强制重启'), T('强制重启'));
  $('#pw-cron').onclick = () => modal(T('定时重启模板'), `<p>${T('在服务器上执行以下命令可配置每日')} ${$('#pw-time').value} ${T('重启：')}</p>
    <pre>[Unit]
Description=Daily reboot

[Timer]
OnCalendar=*-*-* ${$('#pw-time').value}:00
Persistent=true

[Install]
WantedBy=timers.target</pre>
    <p class="desc">${T('保存为')} /etc/systemd/system/drouter-reboot.timer ${T('后执行')} systemctl enable --now drouter-reboot.timer</p>`, null, T('知道了'));
}

function confirmPower(op, word, label) {
  modal(label, `<p>${T('确认要执行「')}${label}${T('」吗？')}</p><p>${T('请输入')} <b>${word}</b> ${T('以继续：')}</p>
    <input id="cfm-in" placeholder="${word}">`, async () => {
    if ($('#cfm-in').value.trim() !== word) { toast(T('确认文字不正确'), 'err'); return false; }
    const r = await api('/api/power', { method: 'POST', body: { op, confirm_text: word } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
  });
}

/* ============================ 用户与密钥 ============================ */
async function viewUser() {
  const r = await api('/api/users');
  const users = r.data || [];
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('当前登录会话')}</h3>
      <p class="desc">${T('左下角的「退出登录」按钮已移除（避免与其它入口重复），登出操作统一放在这里。')}</p>
      <div class="row" style="align-items:center">
        <span class="kv" style="flex:1 1 200px"><b>${T('登录账号')}</b><span class="mono">${esc(S.user || 'admin')}</span></span>
        <button class="ghost fixed" id="us-logout">${T('退出登录')}</button>
      </div>
    </div>
    <div class="card">
      <h3>${T('系统用户（用于')} SSH ${T('登录）')}</h3>
      <p class="desc">${T('这些是')} Debian ${T('系统的真实用户，与')} Web ${T('管理台账号相互独立。')}
      root ${T('已禁止远程登录。如遇失联，可通过虚拟机平台的控制台，或物理机的')} KVM/IPMI${T('、本地显示器、')}VNC ${T('等方式登录救回。')}</p>
      <table><thead><tr><th>${T('用户名')}</th><th>UID</th><th>${T('家目录')}</th><th>${T('状态')}</th><th>${T('公钥数')}</th><th style="width:230px">${T('操作')}</th></tr></thead>
      <tbody>${users.map(u => `<tr>
        <td><b>${esc(u.name)}</b></td><td>${u.uid}</td><td class="mono">${esc(u.home)}</td>
        <td>${u.locked ? `<span class="tag warn">${T('已锁定')}</span>` : `<span class="tag ok">${T('正常')}</span>`}</td>
        <td>${u.keys.length}</td>
        <td>
          <button class="small" data-pw="${esc(u.name)}">${T('改密码')}</button>
          <button class="small" data-lk="${esc(u.name)}" data-v="${u.locked ? 0 : 1}">${u.locked ? T('解锁') : T('锁定')}</button>
          <button class="small danger" data-del="${esc(u.name)}">${T('删除')}</button>
        </td></tr>`).join('')}</tbody></table>
      <div style="margin-top:14px"><button class="small" id="us-add">+ ${T('新建系统用户')}</button></div>
    </div>
    <div class="card">
      <h3>SSH ${T('公钥登录')}</h3>
      <p class="desc"><b>${T('请上传公钥（')}.pub）</b>${T('，不要上传私钥。私钥应保存在你的本地电脑上，绝不发送到服务器。 支持粘贴文本或选择')} .pub ${T('文件。')}</p>
      <div class="row">
        <label style="flex:0 0 200px">${T('目标用户')}<select id="key-user">
          ${users.filter(u => u.uid >= 1000).map(u => `<option>${esc(u.name)}</option>`).join('')}</select></label>
        <div style="flex:1 1 300px">${T('公钥文件（')}.pub）
          <label class="filepick">${T('选择文件')}<input type="file" id="key-file" accept=".pub,text/plain" hidden></label>
          <span id="key-file-name" class="filemeta"></span></div>
      </div>
      <label>${T('或直接粘贴公钥内容（以')} ssh-rsa / ssh-ed25519 ${T('开头）')}
        <textarea id="key-text" rows="4" placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... user@host"></textarea></label>
      <div class="row">
        <button class="primary fixed" id="key-add">${T('写入公钥')}</button>
      </div>
      <div id="key-out" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h3>${T('已授权公钥')}</h3>
      <div id="keys-list">${users.filter(u => u.keys.length).map(u => `
        <div style="margin-bottom:10px"><b>${esc(u.name)}</b>
        <table><tbody>${u.keys.map(k => `<tr><td class="mono" style="max-width:520px;word-break:break-all">${esc(k.key)}</td>
          <td style="width:80px"><button class="small danger" data-delkey="${esc(u.name)}" data-key="${esc(k.key)}">${T('删除')}</button></td></tr>`).join('')}
        </tbody></table></div>`).join('') || `<p class="desc">${T('尚未配置任何公钥。')}</p>`}</div>
    </div>
    <div class="card">
      <h3>SSH ${T('服务端配置（只读）')}</h3>
      <div class="kv"><b>root ${T('远程登录')}</b><span>${T('已禁止（PermitRootLogin no）')}</span></div>
      <div class="kv"><b>${T('监听端口')}</b><span>22</span></div>
      <div class="kv"><b>${T('公钥认证')}</b><span><span class="tag ok">${T('已启用')}</span></span></div>
    </div>`;

  $('#us-add').onclick = () => modal(T('新建系统用户'), `
    <label>${T('用户名')}<input id="cfm-in" placeholder="${T('小写字母开头')}"></label>
    <label>${T('密码')}<input id="cfm-pw" type="password"></label>
    <label class="switch"><input type="checkbox" id="cfm-sudo"><i></i>${T('加入')} sudo ${T('组')}</label>`, async () => {    const r2 = await api('/api/user', { method: 'POST', body: { op: 'add', name: $('#cfm-in').value.trim(), password: $('#cfm-pw').value, sudo: $('#cfm-sudo').checked } });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err');
    if (r2.ok) setTimeout(viewUser, 400);
  }, T('创建'));
  const loBtn = $('#us-logout');
  if (loBtn) loBtn.onclick = () => logout(false);
  $$('[data-pw]').forEach(b => b.onclick = () => modal(`${T('修改')} ${b.dataset.pw} ${T('的密码')}`,
    `<label>${T('新密码')}<input id="cfm-pw" type="password"></label>`, async () => {
      const r2 = await api('/api/user', { method: 'POST', body: { op: 'passwd', name: b.dataset.pw, password: $('#cfm-pw').value } });
      toast(r2.msg_cn, r2.ok ? 'ok' : 'err');
    }, T('修改')));
  $$('[data-lk]').forEach(b => b.onclick = async () => {
    const r2 = await api('/api/user', { method: 'POST', body: { op: b.dataset.v === '1' ? 'lock' : 'unlock', name: b.dataset.lk } });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err'); if (r2.ok) setTimeout(viewUser, 400);
  });
  $$('[data-del]').forEach(b => b.onclick = () => {
    if (b.dataset.del === 'ajeef' || b.dataset.del === 'root') { toast(T('受保护用户不可删除'), 'err'); return; }
    modal(T('删除用户'), `<p>${T('确认删除用户')} <b>${esc(b.dataset.del)}</b> ${T('及其家目录？此操作不可恢复。')}</p>`,
      async () => {
        const r2 = await api('/api/user', { method: 'POST', body: { op: 'del', name: b.dataset.del } });
        toast(r2.msg_cn, r2.ok ? 'ok' : 'err'); if (r2.ok) setTimeout(viewUser, 400);
      });
  });
  $('#key-file').onchange = e => {
    const f = e.target.files[0]; if (!f) { $('#key-file-name').textContent = ''; return; }
    $('#key-file-name').textContent = f.name;
    if (/private|id_rsa$|id_ed25519$/i.test(f.name) && !f.name.endsWith('.pub')) {
      toast(T('这看起来像私钥文件，请上传 .pub 公钥'), 'err', 6000); e.target.value = ''; return;
    }
    const rd = new FileReader();
    rd.onload = () => { $('#key-text').value = String(rd.result).trim(); toast(T('已读取公钥文件内容'), 'ok'); };
    rd.readAsText(f);
  };
  $('#key-add').onclick = async () => {
    const txt = $('#key-text').value.trim();
    if (!txt) { toast(T('请先粘贴或选择公钥内容'), 'err'); return; }
    if (/BEGIN .*PRIVATE KEY/.test(txt)) {
      toast(T('检测到私钥内容！请勿上传私钥，只上传 .pub 公钥文件。'), 'err', 9000); return;
    }
    const r2 = await api('/api/user', { method: 'POST', body: { op: 'key_add', name: $('#key-user').value, keys: txt.split('\n').filter(Boolean) } });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err', 7000);
    if (r2.ok) { $('#key-text').value = ''; setTimeout(viewUser, 500); }
  };
  $$('[data-delkey]').forEach(b => b.onclick = async () => {
    const r2 = await api('/api/user', { method: 'POST', body: { op: 'key_del', name: b.dataset.delkey, key: b.dataset.key } });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err'); if (r2.ok) setTimeout(viewUser, 400);
  });
}

/* ============================ 系统设置 ============================ */
function viewSys() {
  const sysCfg = $w('system');
  $('#view').innerHTML = `
    <div class="card" style="border-color:${S.buildMode ? '#f0e0a8' : '#bfe3c9'};background:${S.buildMode ? 'var(--warn-l)' : 'var(--ok-l)'}">
      <h3 style="color:${S.buildMode ? 'var(--warn)' : 'var(--ok)'}">${T('构建保护模式：')}${S.buildMode ? T('已开启') : T('已关闭')}</h3>
      <p class="desc" style="color:inherit">
        ${S.buildMode
      ? `${T('开启时，所有「应用」操作只会把配置写入磁盘，')}<b>${T('不会启动服务、不会改变网络')}</b>${T('。这是构建阶段的安全保险，可放心填完所有配置而不影响正在运行的局域网与 RealVNC 会话。')}`
      : `${T('已关闭。此后「保存并应用」会')}<b>${T('真正生效')}</b>${T('：DHCP 服务会启动并占用 53')}/67 ${T('端口，防火墙规则会立即加载。请确保这是你想要的。')}`}
      </p>
      <div class="row">
        ${S.buildMode
      ? `<button class="fixed" onclick="toggleBuildMode(false)">${T('关闭保护，允许配置生效')}</button>`
      : `<button class="ghost fixed" onclick="toggleBuildMode(true)">${T('重新开启保护')}</button>`}
      </div>
    </div>
    <div class="card">
      <h3>Web ${T('管理账号')}</h3>
      <p class="desc">${T('此账号仅用于登录本管理台，与')} Debian ${T('系统用户完全独立。当前用户：')}<b>${esc(S.user)}</b></p>
      <div class="row">
        <label>${T('原密码')}<input id="sy-old" type="password"></label>
        <label>${T('新密码（至少')} 8 ${T('位）')}<input id="sy-new" type="password"></label>
        <label>${T('确认新密码')}<input id="sy-new2" type="password"></label>
      </div>
      <div class="row"><button class="primary fixed" id="sy-pw">${T('修改管理密码')}</button></div>
      <div id="sy-out" class="msg"></div>
    </div>
    <div class="card">
      <h3>Web ${T('管理端口')}</h3>
      <p class="desc">${T('修改管理后台的')} HTTPS ${T('端口，避免与其他服务冲突。默认')} <b>8443</b>${T('。 修改后需要重启管理后台，然后用新地址重新访问（例如')} <span class="mono">https://192.168.7.3:${T('新端口')}/</span>）。</p>
      <div class="row">
        <label style="flex:0 0 200px">HTTPS ${T('端口')}<input id="sy-port" type="number" min="1" max="65535"
          value="${esc(S.webPort || 8443)}" placeholder="8443"></label>
        <button class="fixed" id="sy-port-save">${T('保存并重启后台')}</button>
        <button class="ghost fixed" id="sy-port-reset">${T('恢复默认')} 8443</button>
      </div>
      <div id="sy-port-out" class="msg"></div>
      <p class="hint-inline">${T('明文')} HTTP ${T('端口固定为')} <span class="mono">8080</span>${T('（仅建议本机调试使用）。 当前生效端口：')}<span class="mono" id="sy-port-cur">${S.webPort || 8443}</span></p>
    </div>
    <div class="card">
      <h3>${T('访问控制')}</h3>
      <div class="kv"><b>${T('管理端口')}</b><span class="mono">HTTPS ${S.webPort || 8443}${T('（自签证书）')} / HTTP 8080</span></div>
      <label>${T('访问范围')}
        <select id="sy-scope">
          <option value="lan" ${(sysCfg.access_scope || 'lan') === 'lan' ? 'selected' : ''}>${T('仅限局域网（推荐，')}WAN ${T('侧不开放）')}</option>
          <option value="all" ${sysCfg.access_scope === 'all' ? 'selected' : ''}>${T('允许所有来源（含')} WAN${T('，风险较高）')}</option>
          <option value="custom" ${sysCfg.access_scope === 'custom' ? 'selected' : ''}>${T('自定义允许的网段')}</option>
        </select>
      </label>
      <div id="sy-scope-custom" class="${(sysCfg.access_scope === 'custom') ? '' : 'hidden'}">
        <label>${T('允许访问的网段（逗号分隔，')}CIDR ${T('格式）')}
          <input id="sy-scope-net" value="${esc((sysCfg.access_nets || ['192.168.7.0/24']).join(','))}"
            placeholder="192.168.7.0/24,10.0.0.0/8"></label>
      </div>
      <p class="hint-inline">${T('访问范围仅影响管理后台的防火墙放行策略；改完请点击右上角「保存并应用」。')}</p>
      <div class="kv"><b>${T('会话超时')}</b><span>60 ${T('分钟')}</span></div>
      <div class="kv"><b>${T('登录失败锁定')}</b><span>${T('连续')} 5 ${T('次失败后锁定')} 10 ${T('分钟')}</span></div>
    </div>
    <div class="card">
      <h3>${T('配置快照与回滚')}</h3>
      <p class="desc">${T('快照会')}<b>${T('整体覆盖全部用户设置')}</b>${T('：界面里的所有模块配置（')}DHCP/DNS${T('、防火墙')} IPv4·IPv6${T('、WAN/LAN、IPv6 与 RA、UPnP、NTP')}${T('、网卡角色、访问范围与')} Web ${T('端口等）连同底层配置文件一起打包。回滚时数据库与配置文件一并还原，不会出现「只恢复了一半」的情况。')}</p>
      <div id="sy-scope-box" class="snap-scope"></div>
      <div class="row">
        <label style="flex:0 0 240px">${T('快照备注')}<input id="sy-tag" placeholder="${T('例如：切换前备份')}"></label>
        <label class="switch" style="flex:0 0 auto"><input type="checkbox" id="sy-protect"><i></i>${T('创建后上锁')}</label>
        <button class="ghost fixed" id="sy-snap">${T('立即创建快照')}</button>
        <button class="ghost fixed" id="sy-list">${T('刷新快照列表')}</button>
      </div>
      <p class="hint-inline">${T('快照创建后仍可随时改备注、随时上锁解锁')} —— ${T('列表见下方')}
        <a href="#" id="sy-gorescue">${T('紧急救援通道')}</a>。</p>
    </div>
    <div class="card">
      <h3>${T('自动快照')}</h3>
      <p class="desc">${T('按设定的时间间隔自动为当前配置拍一份快照，并在超过保留期后自动清理旧快照。默认')}<b>${T('开启')}</b>${T('，可随时关闭。')}</p>
      <div class="row" style="align-items:center">
        <label class="switch"><input type="checkbox" id="as-en"><i></i>${T('开启自动快照')}</label>
        <span class="hint-inline" id="as-state"></span>
      </div>
      <div class="row">
        <label>${T('快照时间（小时一次）')}<input id="as-interval" type="number" min="1" max="168" value="6"></label>
        <label>${T('过期时间（天后自动删除）')}<input id="as-days" type="number" min="0" max="3650" value="7"></label>
        <label>${T('最多保留份数（')}0=${T('不限）')}<input id="as-count" type="number" min="0" max="10000" value="30"></label>
      </div>
      <label>${T('快照存放路径')}
        <input id="as-path" class="mono" placeholder="/opt/drouter/snapshots"></label>
      <p class="hint-inline">${T('路径仅允许位于')} <span class="mono">/opt · /srv · /var/backups · /mnt · /media · /home</span> ${T('之下，避免误写系统关键目录。')}</p>
      <div class="row" style="align-items:center">
        <label class="switch"><input type="checkbox" id="as-manual"><i></i>${T('手动快照不参与自动清理')}</label>
        <label class="switch"><input type="checkbox" id="as-apply"><i></i>${T('每次「保存并应用」前自动拍一张')}</label>
      </div>
      <p class="hint-inline">${T('「手动快照不参与自动清理」是')}<b>${T('全局开关')}</b>${T('：按创建方式自动识别，凡不是自动拍的一律保留（手动创建、回滚前、升级前拍的都是手动）。 若只想保住')}<b>${T('其中某一份')}</b>${T('，在下方「紧急救援通道')} → ${T('可回滚的快照」里给它单独上锁即可')} —— ${T('上锁的那份在任何自动清理下都不会被删，且不占用「最多保留份数」的额度。')}</p>
      <div class="row">
        <button class="primary fixed" id="as-save">${T('保存自动快照策略')}</button>
        <button class="ghost fixed" id="as-run">${T('立即执行一次')}</button>
        <button class="ghost fixed" id="as-prune">${T('立即清理过期快照')}</button>
      </div>
      <div id="as-out" class="msg"></div>
      <div class="kv"><b>${T('下次自动快照')}</b><span id="as-next">—</span></div>
      <div class="kv"><b>${T('定时器状态')}</b><span id="as-timer">—</span></div>
      <div class="kv"><b>${T('快照占用')}</b><span id="as-disk">—</span></div>
    </div>
    <div class="card">
      <h3>${T('紧急救援通道')}</h3>
      <p class="desc">${T('当配置失误把网络')}"${T('打崩')}"${T('时（网络风暴、')}IP ${T('冲突、路由环路、防火墙锁死、')}Web ${T('管理页打不开等）， 常规手段往往已经进不去路由器。')}<b>${T('紧急救援通道')}</b>${T('是一条完全独立于')} WAN ${T('口 / LAN')} ${T('口的应急入口： 它会在网卡上额外绑定一组虚拟地址，因此')}<b>${T('把网线插到任意一个网口都能访问')}</b>${T('， 进入后可一键还原最近的快照并自动重启，让设备恢复到可用状态。')}</p>
      <p class="hint-inline">${T('下面这份快照列表就是救援通道的「还原材料」。')}<b>${T('网络已经打崩的时候，最怕的不是没有快照，是不知道该回滚哪一份')}</b> ——
      ${T('所以列表直接放在这里，看得清每份快照的备注、体积和是否受保护，回滚按钮就在手边。')}</p>
      <div class="row" style="align-items:center">
        <label class="switch"><input type="checkbox" id="rs-en"><i></i>${T('开启紧急救援通道')}</label>
        <span class="hint-inline" id="rs-state"></span>
      </div>
      <div class="row">
        <label>${T('救援地址（虚拟')} IP）<input id="rs-vip" class="mono" placeholder="169.254.0.1" value="169.254.0.1"></label>
        <label style="flex:0 0 110px">${T('掩码长度')}<input id="rs-mask" type="number" min="1" max="32" value="16"></label>
        <label>${T('救援端口')}<input id="rs-port" type="number" min="1" max="65535" value="8888"></label>
      </div>
      <div class="row">
        <label>${T('备用救援地址（同网段直连用）')}<input id="rs-alt" class="mono" placeholder="10.99.99.1" value="10.99.99.1"></label>
      </div>
      <label>${T('生效网卡（留空＝自动应用到所有物理网卡，插任一口皆可访问）')}
        <div id="rs-ifaces" class="chipbox"></div></label>
      <div class="row" style="align-items:center">
        <label class="switch"><input type="checkbox" id="rs-reboot"><i></i>${T('一键还原后自动重启')}</label>
      </div>
      <p class="hint-inline" id="rs-hint">${T('启用后访问方式：')}<span class="mono">http://169.254.0.1:8888/</span>${T('（也支持')} <span class="mono">?restore=1</span> ${T('参数直达还原页）。')}</p>
      <div class="row">
        <button class="primary fixed" id="rs-save">${T('保存救援通道设置')}</button>
        <button class="ghost fixed" id="rs-test">${T('连通性自检')}</button>
      </div>
      <div id="rs-out" class="msg"></div>
      <div class="notice warn hidden" id="rs-token" style="margin-top:10px"></div>
      <div class="kv"><b>${T('服务状态')}</b><span id="rs-svc">—</span></div>
      <div class="kv"><b>${T('虚拟网卡')}</b><span id="rs-vif">—</span></div>
      <div id="rs-addrs" class="mono" style="margin-top:8px;font-size:12px;color:var(--muted)"></div>
      <h3 style="margin-top:22px">${T('可回滚的快照')}</h3>
      <p class="desc">${T('回滚')} = ${T('把界面配置与系统配置文件整体恢复到快照那一刻的状态，并重启相关服务。 动手前先看清')}<b>${T('哪一份是你要的')}</b>${T('：备注是你自己写的话，回滚就不容易选错。 回滚之前系统会自动再拍一张')} <span class="mono">before-rollback</span> ${T('快照，回滚完不满意还能退回来。')}</p>
      <div class="notice" id="sy-locktip">
        <b>${T('自动清理规则')}</b>：<b>${T('手动快照')}</b>${T('与')}<b>${T('已上锁的快照')}</b>${T('都不会被「自动快照」的过期清理删掉。 两者区别在于')}——${T('「手动快照」是按创建方式自动识别的全局策略（在「自动快照」卡片里可关），')}
        <b>${T('上锁')}</b>${T('则是你对')}<b>${T('这一份')}</b>${T('的单独决定，任何自动清理都跳过它。上锁不影响手动删除，只是删之前会多问一次。')}
      </div>
      <div id="sy-snapout" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h3>${T('网络后端迁移（高级）')}</h3>
      <p class="desc">${T('当前由')} NetworkManager ${T('管理网卡。如需切换为')} systemd-networkd${T('， 可先做干跑预览查看将写入的配置文件。')}<b>${T('真正执行可能中断网络连接')}</b>——${T('万一失联，可以通过 虚拟机平台的控制台，或者物理机的')} KVM/IPMI${T('、本地显示器、')}VNC ${T('等方式重新登录救回。 确认自己能救回来之后，再执行迁移。')}</p>
      <div class="row">
        <button class="ghost fixed" id="sy-mig">${T('生成迁移预览（不写入）')}</button>
      </div>
      <div id="sy-migout" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h3>${T('配置文件位置')}</h3>
      <table><thead><tr><th>${T('内容')}</th><th>${T('路径')}</th></tr></thead><tbody>
        <tr><td>Web ${T('配置数据库')}</td><td class="mono">/opt/drouter/data/drouter.db</td></tr>
        <tr><td>${T('配置快照')}</td><td class="mono">/opt/drouter/snapshots/</td></tr>
        <tr><td>${T('结构化日志')}</td><td class="mono">/var/log/drouter/*.jsonl</td></tr>
        <tr><td>DHCP/DNS ${T('配置')}</td><td class="mono">/etc/dnsmasq.d/drouter.conf</td></tr>
        <tr><td>${T('防火墙规则')}</td><td class="mono">/etc/nftables.d/drouter-v4.nft · drouter-v6.nft</td></tr>
        <tr><td>PPPoE ${T('配置')}</td><td class="mono">/etc/ppp/peers/drouter-wan</td></tr>
        <tr><td>RA ${T('配置')}</td><td class="mono">/etc/radvd.conf</td></tr>
      </tbody></table>
    </div>`;
  $('#sy-pw').onclick = async () => {
    const a = $('#sy-new').value, b = $('#sy-new2').value;
    if (a !== b) { $('#sy-out').className = 'msg err'; $('#sy-out').textContent = T('两次输入的新密码不一致'); return; }
    const r = await api('/api/password', { method: 'POST', body: { old_password: $('#sy-old').value, new_password: a } });
    $('#sy-out').className = 'msg ' + (r.ok ? 'ok' : 'err');
    $('#sy-out').textContent = r.msg_cn;
    if (r.ok) { $('#sy-old').value = $('#sy-new').value = $('#sy-new2').value = ''; }
  };
  $('#sy-snap').onclick = async () => {
    const r = await api('/api/snapshot', { method: 'POST',
      body: { tag: $('#sy-tag').value || 'manual', protected: $('#sy-protect').checked } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err');
    if (r.ok) {
      // 建完就把输入框清空并取消勾选：留着旧备注会让下一张快照
      // 顶着同一个名字，列表里两行长得一模一样。
      $('#sy-tag').value = '';
      $('#sy-protect').checked = false;
      listSnapshots();
    }
  };
  /* 刷新列表：列表已内嵌到本卡片下方的「可回滚的快照」，所以只要
     容器在就地刷新，不必再弹窗。容器不在（比如从防火墙页触发）
     就把用户带到系统设置页去 —— 别静默什么都不做。 */
  $('#sy-list').onclick = async () => {
    if (!$('#sy-snapout')) { go('sys'); toast(T('已切到系统设置页，快照列表在「紧急救援通道」里'), 'ok', 4000); return; }
    await listSnapshots();
  };
  const gotoRescue = $('#sy-gorescue');
  if (gotoRescue) gotoRescue.onclick = e => {
    e.preventDefault();
    const t = document.getElementById('sy-snapout');
    if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  loadAutoSnapshot();
  loadRescue();
  // Web 端口修改
  const portOut = $('#sy-port-out');
  $('#sy-port-save').onclick = async () => {
    const port = Number($('#sy-port').value);
    if (!(port >= 1 && port <= 65535)) { portOut.className = 'msg err'; portOut.textContent = T('端口必须是 1-65535 的数字'); return; }
    if (!confirm(`${T('确定把管理后台端口改为')} ${port} ${T('吗？改完后需要用')} https://<${T('本机')}IP>:${port}/ ${T('重新访问。')}`)) return;
    portOut.className = 'msg'; portOut.textContent = T('正在保存并重启后台…');
    const r = await api('/api/webport', { method: 'POST', body: { port } });
    portOut.className = 'msg ' + (r.ok ? 'ok' : 'err');
    portOut.textContent = r.msg_cn;
    if (r.ok) {
      S.webPort = port;
      const cur = $('#sy-port-cur'); if (cur) cur.textContent = port;
      toast(T('端口已改为 ') + port + T('，请用新地址访问'), 'ok', 8000);
    }
  };
  $('#sy-port-reset').onclick = () => { $('#sy-port').value = 8443; toast(T('已填入默认端口，请点击「保存并重启后台」'), 'ok'); };
  // 访问范围
  const scope = $('#sy-scope');
  const bindScope = () => {
    const v = scope.value;
    S.cfg.system = Object.assign($w('system'), { access_scope: v });
    const box = $('#sy-scope-custom');
    if (box) box.classList.toggle('hidden', v !== 'custom');
    setActionMsg(T('访问范围已改为「') + scope.options[scope.selectedIndex].text + T('」，请点击保存并应用'));
  };
  if (scope) { scope.onchange = bindScope; }
  const sn = $('#sy-scope-net');
  if (sn) sn.oninput = () => {
    S.cfg.system = Object.assign($w('system'), {
      access_nets: sn.value.split(',').map(x => x.trim()).filter(Boolean),
    });
    setActionMsg(T('允许网段已修改，请点击保存并应用'));
  };
  $('#sy-mig').onclick = async () => {
    const out = $('#sy-migout');
    // render_network 需要 wan/lan/bridge 三段齐全：只传 lan 会让它在
    // `v_ifname(wan.get('iface') or '')` 这里直接抛「WAN 网卡不能为空」，
    // 预览永远出不来；缺 bridge 则用户配好的网桥一个文件都不会显示在预览里。
    const s = $w('system'), p = $w('pppoe');
    if (!s.wan_iface) {
      out.innerHTML = `<p class="desc">${T('请先到「网卡与桥接」页把某个网口的角色设为 WAN，')}`
        + T('否则迁移配置里缺少 WAN 网卡，无法生成。</p>');
      return;
    }
    const mode = p.mode || 'dhcp';
    const wan = { iface: s.wan_iface, mode };
    if (mode === 'static') {
      wan.address = p.static_address || '';
      wan.gateway = p.static_gateway || '';
      wan.dns = p.static_dns || '';
    } else if (mode === 'pppoe') {
      wan.mtu = p.mtu || 1492;
    } else {
      wan.mtu = p.dhcp_mtu || 1500;
      // 「使用上级下发的 DNS」若不给后端，render_network 里会按默认 yes 处理，
      // 用户在 WAN 页关掉它就白关了。
      wan.use_dns = p.dhcp_use_dns !== false;
    }
    out.innerHTML = `<pre>${T('生成中…')}</pre>`;
    const r = await api('/api/migrate/preview', { method: 'POST', body: { cfg: {
      wan,
      lan: { iface: s.lan_iface || '', address: s.lan_address || '192.168.7.3/24', mtu: s.lan_mtu || 1500 },
      bridge: { enabled: !!s.bridge_enabled, name: s.bridge_name || 'br0',
                members: s.bridge_members || [], stp: !!s.bridge_stp },
    } } });
    const files = (r.data || {}).files || [];
    out.innerHTML = files.length
      ? files.map(f => `<div style="margin-bottom:10px"><b class="mono">${esc(f.name)}</b><pre>${esc(f.content)}</pre></div>`).join('')
      : '<p class="desc">' + esc(r.msg_cn || T('未生成任何文件')) + '</p>';
  };
}

/* ---------- 配置快照：列表 / 备注 / 上锁 / 下载 / 回滚 / 删除 ---------- */
/* 列表内嵌在「紧急救援通道」卡片里（容器 #sy-snapout）。
   此前这里是 `if (S.page === 'sys') 内嵌 else modal(...)` 的分叉：同一个
   渲染函数、同一份 HTML，走两条路。结果是**只有系统设置页能改备注/上锁**，
   别的页（防火墙页的 #fw-rollback 按钮）弹出来的 modal 里只有三个按钮。
   现在统一走内嵌，modal 分支删掉 —— 快照从哪看、在哪操作，是同一件事。 */
async function listSnapshots() {
  const r = await api('/api/snapshots');
  const d = r.data || {};
  const rows = (d.items || []) || [];
  const root = d.root || '';
  // 覆盖范围说明
  const scopeBox = $('#sy-scope-box');
  if (scopeBox && (d.scope || []).length) {
    scopeBox.innerHTML = `<div class="snap-scope-t">${T('快照包含以下内容：')}</div>` +
      d.scope.map(g => `<div class="snap-g"><b>${bt4('SNAPSHOT_SCOPE', g.group, 'group', g.group)}</b><ul>${
        (g.items || []).map(i => `<li>${esc(i)}</li>`).join('')}</ul></div>`).join('');
  }
  const disk = d.disk ? `${T('磁盘：剩余')} ${fmtBytes((d.disk.free_mb || 0) * 1024 * 1024)} / ${T('共')} ${fmtBytes((d.disk.total_mb || 0) * 1024 * 1024)}` : '';
  const lockedN = rows.filter(x => x.protected).length;
  const html = rows.length ? `
    <p class="hint-inline">${T('共')} ${rows.length} ${T('份快照')}${lockedN ? `${T('，其中')} <b>${lockedN}</b> ${T('份已上锁')}` : ''}${T('，存放于')} <span class="mono">${esc(root)}</span>。${esc(disk)}</p>
    <table><thead><tr>
      <th style="width:150px">${T('时间')}</th><th>${T('备注')}</th><th style="width:74px">${T('保护')}</th>
      <th style="width:66px">${T('大小')}</th><th style="width:70px">${T('配置库')}</th>
      <th style="width:212px">${T('操作')}</th></tr></thead><tbody>
    ${rows.map(x => `<tr>
      <td class="mono">${esc(x.ts)}</td>
      <td><input class="snap-tag-input" data-tag="${esc(x.ts)}" maxlength="60"
            value="${esc(x.tag || '')}" placeholder="${T('（未命名）')}"></td>
      <td><label class="switch" style="margin:0"><input type="checkbox" data-lock="${esc(x.ts)}"
            ${x.protected ? 'checked' : ''}><i></i></label></td>
      <td class="mono">${x.size_kb != null ? esc(x.size_kb) + ' KB' : '—'}</td>
      <td>${x.db_included ? `<span class="tag-ok">${T('已含')}</span>` : `<span class="tag-warn">${T('旧版')}</span>`}</td>
      <td>
        <button class="small" data-save-tag="${esc(x.ts)}">${T('存备注')}</button>
        <button class="small" data-dl="${esc(x.ts)}">${T('下载')}</button>
        <button class="small" data-rb="${esc(x.ts)}">${T('回滚')}</button>
        <button class="small danger" data-del="${esc(x.ts)}">${T('删除')}</button>
      </td></tr>`).join('')}</tbody></table>`
    : `<p class="desc">${T('暂无快照。可在上方「配置快照与回滚」里立即创建一份，或等自动快照按计划生成。')}</p>`;
  // 纯内嵌。不再回退到 modal —— 页面上永远只有一处快照列表。
  const box = $('#sy-snapout');
  if (box) box.innerHTML = html;

  /* 备注编辑：点「存备注」才提交。
     别在 input 上绑 onchange/每键输入就发请求 —— 那是把一次编辑
     变成几十次 HTTP，而且用户按 Esc 想放弃都来不及。 */
  $$('[data-save-tag]').forEach(b => b.onclick = async () => {
    const ts = b.dataset.saveTag;
    const inp = document.querySelector(`[data-tag="${CSS.escape(ts)}"]`);
    const val = inp ? inp.value : '';
    b.disabled = true;
    const res = await api('/api/snapshot/note', { method: 'POST', body: { ts: ts, tag: val } });
    toast(res.msg_cn, res.ok ? 'ok' : 'err', 4000);
    b.disabled = false;
    if (res.ok && inp) inp.value = (res.data && res.data.tag) || '';
  });
  /* 上锁开关：change 事件（而不是 onclick），这样键盘操作和
     label 包裹的点击也都能触发，且不会重复提交。 */
  $$('[data-lock]').forEach(b => b.onchange = async () => {
    const ts = b.dataset.lock;
    b.disabled = true;
    const res = await api('/api/snapshot/protect',
      { method: 'POST', body: { ts: ts, protected: b.checked } });
    toast(res.msg_cn, res.ok ? 'ok' : 'err', 4000);
    b.disabled = false;
    // 后端拒绝了（例如已被别的会话删掉）就把勾去掉，别让界面显示
    // 一个后端并不认同的状态。
    if (!res.ok) b.checked = !b.checked;
  });
  $$('[data-dl]').forEach(b => b.onclick = async () => {
    const ts = b.dataset.dl;
    toast(T('正在打包快照 ') + ts + '…', 'ok', 2500);
    try {
      const res = await fetch('/api/snapshot/download?ts=' + encodeURIComponent(ts),
        { headers: S.token ? { 'X-Token': S.token } : {} });
      if (!res.ok) {
        let m = T('下载失败');
        try { m = (await res.json()).msg_cn || m; } catch (e) { }
        toast(m, 'err', 6000); return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'drouter-snapshot-' + ts + '.tar.gz';
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast(T('已开始下载快照 ') + ts, 'ok');
    } catch (e) { toast(T('下载失败：') + netErrCn(e && e.message), 'err', 6000); }
  });
  $$('[data-rb]').forEach(b => b.onclick = () => modal(T('确认回滚'),
    `<p>${T('将把配置整体恢复到快照')} <b class="mono">${esc(b.dataset.rb)}</b> ${T('的状态。')}</p>
     <p class="desc">${T('回滚会同时还原界面配置数据库与系统配置文件，并重启相关服务。回滚前系统会自动再拍一张')} <span class="mono">before-rollback</span> ${T('快照，万一不满意还能退回来。')}</p>
     <p class="desc" style="color:var(--warn)">${T('回滚过程中网络可能短暂中断。')}</p>`, async () => {
    const r2 = await api('/api/rollback', { method: 'POST', body: { ts: b.dataset.rb } });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err', 8000);
    if (r2.ok) listSnapshots();
  }, T('确认回滚')));
  /* 删除：后端对已上锁的那份会先回 needs_force，这里据此追加一次确认。
     不能一上来就把「已上锁」三个字去掉 —— 上锁的本意就是「别顺手
     弄没了」，顺手点一下就删掉等于上锁没用。 */
  $$('[data-del]').forEach(b => b.onclick = async () => {
    const ts = b.dataset.del;
    const doDel = async force => {
      const r2 = await api('/api/snapshot/delete', { method: 'POST', body: { ts: ts, force: !!force } });
      if (!r2.ok && r2.data && r2.data.needs_force) return 'again';
      toast(r2.msg_cn, r2.ok ? 'ok' : 'err', r2.ok ? 3000 : 6000);
      if (r2.ok) listSnapshots();
      return r2.ok;
    };
    const first = await doDel(false);
    if (first === 'again') {
      modal(T('这份快照已上锁'),
        `<p>${T('快照')} <b class="mono">${esc(ts)}</b> ${T('已上锁，')}<b>${T('自动清理不会删除它')}</b>。</p>
         <p class="desc">${T('上锁只挡自动清理，不挡你手动删。确定要删掉它吗？此操作不可恢复。')}</p>`,
        async () => { await doDel(true); }, T('仍要删除'));
      return;
    }
  });
}

/* ---------- 自动快照策略 ---------- */
async function loadAutoSnapshot() {
  const r = await api('/api/snapshot/auto');
  if (!r.ok) return;
  const d = r.data || {}, c = d.config || {};
  const setV = (id, v) => { const el = $(id); if (el) el.value = v; };
  setV('#as-interval', c.interval_hours != null ? c.interval_hours : 6);
  setV('#as-days', c.keep_days != null ? c.keep_days : 7);
  setV('#as-count', c.keep_count != null ? c.keep_count : 30);
  setV('#as-path', c.path || '/opt/drouter/snapshots');
  const box = {
    '#as-en': c.enabled !== false,
    '#as-manual': c.keep_manual !== false,
    '#as-apply': c.on_apply !== false,
  };
  Object.keys(box).forEach(id => { const el = $(id); if (el) el.checked = box[id]; });
  const st = $('#as-state');
  if (st) {
    st.className = 'hint-inline';
    st.innerHTML = c.enabled !== false
      ? `<span class="tag-ok">${T('已开启')}</span> ${T('自动快照正在按计划运行')}`
      : `<span class="tag-warn">${T('已关闭')}</span> ${T('不会再自动创建快照')}`;
  }
  const tm = $('#as-timer');
  if (tm) tm.innerHTML = d.timer_active ? `<span class="tag-ok">${T('运行中')}</span>` : `<span class="tag-warn">${T('未运行')}</span>`;
  const nx = $('#as-next');
  if (nx) nx.textContent = d.next_run ? fmtUnix(d.next_run) : '—';
  const dk = $('#as-disk');
  if (dk && d.disk) dk.textContent = `${T('剩余')} ${fmtBytes((d.disk.free_mb || 0) * 1048576)} / ${T('共')} ${fmtBytes((d.disk.total_mb || 0) * 1048576)}`;

  $('#as-save').onclick = async () => {
    const out = $('#as-out');
    const body = {
      op: 'set',
      enabled: $('#as-en').checked,
      interval_hours: Number($('#as-interval').value) || 6,
      keep_days: Number($('#as-days').value) || 0,
      keep_count: Number($('#as-count').value) || 0,
      keep_manual: $('#as-manual').checked,
      on_apply: $('#as-apply').checked,
      path: ($('#as-path').value || '').trim(),
    };
    out.className = 'msg'; out.textContent = T('正在保存…');
    const r2 = await api('/api/snapshot/auto', { method: 'POST', body });
    out.className = 'msg ' + (r2.ok ? 'ok' : 'err');
    out.textContent = r2.msg_cn;
    if (r2.ok) { toast(r2.msg_cn, 'ok'); loadAutoSnapshot(); }
  };
  $('#as-run').onclick = async () => {
    const out = $('#as-out'); out.className = 'msg'; out.textContent = T('正在创建快照…');
    const r2 = await api('/api/snapshot/auto', { method: 'POST', body: { op: 'run_now' } });
    out.className = 'msg ' + (r2.ok ? 'ok' : 'err'); out.textContent = r2.msg_cn;
    if (r2.ok) listSnapshots();
  };
  $('#as-prune').onclick = async () => {
    const out = $('#as-out'); out.className = 'msg'; out.textContent = T('正在清理…');
    const r2 = await api('/api/snapshot/prune', {
      method: 'POST', body: {
        keep_days: Number($('#as-days').value) || 0,
        keep_count: Number($('#as-count').value) || 0,
        keep_manual: $('#as-manual').checked,
      }
    });
    out.className = 'msg ' + (r2.ok ? 'ok' : 'err'); out.textContent = r2.msg_cn;
    if (r2.ok) listSnapshots();
  };
}

/* ---------- 紧急救援通道 ---------- */
async function loadRescue() {
  const r = await api('/api/rescue');
  if (!r.ok) return;
  const d = r.data || {};
  const setV = (id, v) => { const el = $(id); if (el) el.value = v; };
  const en = $('#rs-en');
  if (en) en.checked = !!d.enabled;
  setV('#rs-vip', d.vip || '169.254.0.1');
  setV('#rs-mask', d.vip_mask || '16');
  setV('#rs-port', d.port || 8888);
  setV('#rs-alt', d.alt_vip || '10.99.99.1');
  const rb = $('#rs-reboot'); if (rb) rb.checked = d.auto_reboot !== false;
  // 网卡多选（留空 = 全部物理网卡）
  const phys = d._phys_ifaces || [];
  const chosen = d.ifaces || [];
  const box = $('#rs-ifaces');
  if (box) {
    box.innerHTML = phys.map(n => `<label class="chip"><input type="checkbox" value="${esc(n)}"
      ${chosen.includes(n) ? 'checked' : ''}><span>${esc(n)}</span></label>`).join('')
      || `<span class="desc">${T('未检测到物理网卡')}</span>`;
  }
  const st = $('#rs-state');
  if (st) st.innerHTML = d.enabled
    ? `<span class="tag-ok">${T('已开启')}</span> ${T('插任一口皆可访问救援页')}`
    : `<span class="tag-warn">${T('已关闭')}</span>${T('（默认关闭，需要时再开）')}`;
  const sv = $('#rs-svc');
  if (sv) sv.innerHTML = d._service_active ? `<span class="tag-ok">${T('运行中')}</span>` : `<span class="tag-warn">${T('未运行')}</span>`;
  const vf = $('#rs-vif');
  if (vf) vf.innerHTML = d._vif_present ? `<span class="tag-ok">${T('已创建')}</span>` : `<span class="tag-warn">${T('未创建')}</span>`;
  const ad = $('#rs-addrs');
  if (ad) ad.textContent = (d._addrs || []).length ? T('当前地址：') + (d._addrs || []).join('　|　') : '';
  updRescueHint();

  $('#rs-save').onclick = async () => {
    const out = $('#rs-out');
    const wantOn = $('#rs-en').checked;
    const body = {
      op: 'set',
      enabled: wantOn,
      vip: $('#rs-vip').value.trim(),
      vip_mask: $('#rs-mask').value,
      port: Number($('#rs-port').value),
      alt_vip: $('#rs-alt').value.trim(),
      auto_reboot: $('#rs-reboot').checked,
      ifaces: $$('#rs-ifaces input:checked').map(x => x.value),
      confirm: true,
    };
    out.className = 'msg'; out.textContent = T('正在保存并应用…');
    const r2 = await api('/api/rescue', { method: 'POST', body });
    out.className = 'msg ' + (r2.ok ? 'ok' : 'err');
    out.textContent = r2.msg_cn;
    if (r2.ok) {
      // 开启时后端会返回一次性访问令牌：救援页开启后挂在**每一张**物理网卡上，
      // 没有令牌就等于局域网里任何人都能回滚并重启路由器。必须让用户看见并抄走。
      const tok = (r2.data || {}).token;
      const box = $('#rs-token');
      if (tok && box) {
        box.classList.remove('hidden');
        box.innerHTML = `${T('访问令牌')} <b class="mono">` + esc(tok) + '</b>'
          + T(' —— 救援页上所有的还原操作都要填它。只显示这一次，')
          + T('关掉再开启会换一个。请抄到另一台机器上保存。');
      }
      toast(r2.msg_cn, 'ok', 6000);
      loadRescue();
    }
  };
  $('#rs-test').onclick = async () => {
    const out = $('#rs-out'); out.className = 'msg'; out.textContent = T('正在自检…');
    const r2 = await api('/api/rescue', { method: 'POST', body: { op: 'test' } });
    out.className = 'msg ' + (r2.ok ? 'ok' : 'err'); out.textContent = r2.msg_cn;
    if (r2.ok) loadRescue();
  };
  const vipEl = $('#rs-vip'), portEl = $('#rs-port');
  if (vipEl) vipEl.oninput = updRescueHint;
  if (portEl) portEl.oninput = updRescueHint;
}

function updRescueHint() {
  const el = $('#rs-hint'); if (!el) return;
  const vip = ($('#rs-vip') || {}).value || '169.254.0.1';
  const port = ($('#rs-port') || {}).value || 8888;
  el.innerHTML = `${T('启用后访问方式：')}<span class="mono">http://${esc(vip)}:${esc(port)}/</span>` +
    `${T('（也可用')} <span class="mono">http://${esc(vip)}:${esc(port)}/?restore=1</span> ${T('直达一键还原页）。')}` +
    `${T('此地址不占用')} WAN/LAN${T('，插任一口皆可访问。')}`;
}

function fmtUnix(v) {
  const n = Number(v);
  if (!n) return '—';
  const ms = n > 1e12 ? n / 1000 : n;
  const dt = new Date(ms);
  if (isNaN(dt.getTime())) return String(v);
  const p = x => String(x).padStart(2, '0');
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
}

/* ============================ 升级与保护 ============================ */
async function viewUpdate() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>RealVNC ${T('保护状态')}</h3>
      <p class="desc">${T('本项目不会接管')} VNC${T('，只做状态展示与升级风险预警。')}</p>
      <div id="up-vnc">${T('读取中')}…</div>
    </div>
    <div class="card">
      <h3>${T('可升级软件包')}</h3>
      <p class="desc">${T('系统更新默认')}<b>${T('只列出清单，不自动执行')}</b>${T('。标记为高危的包可能影响桌面环境与')} VNC${T('， 升级前请先创建快照。')}</p>
      <div class="row">
        <button class="ghost fixed" id="pk-upd">${T('更新软件源索引')}</button>
        <button class="ghost fixed" id="pk-list">${T('列出可升级包')}</button>
      </div>
      <div id="pk-out" style="margin-top:12px"><p class="desc">${T('点击上方按钮开始。')}</p></div>
    </div>
    <div class="card">
      <h3>${T('版本锁定（')}apt-mark hold）</h3>
      <p class="desc">${T('本方案')}<b>${T('不推荐')}</b>${T('长期冻结大批系统包。仅当')} RealVNC ${T('官方明确要求固定某版本时， 对该单个包执行锁定。')}</p>
      <div class="row">
        <label style="flex:1 1 320px">${T('包名（逗号分隔）')}<input id="pk-hold-n" placeholder="${T('例如')} xserver-xorg-core"></label>
        <button class="ghost fixed" id="pk-hold">${T('锁定')}</button>
        <button class="ghost fixed" id="pk-unhold">${T('解锁')}</button>
      </div>
      <div id="pk-holds" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <h3>${T('升级后验证清单')}</h3>
      <table><thead><tr><th>${T('项目')}</th><th>${T('验证方式')}</th></tr></thead><tbody>
        <tr><td>${T('图形会话')}</td><td>${T('通过')} RealVNC ${T('连接')} 5900${T('，确认桌面正常')}</td></tr>
        <tr><td>${T('管理台')}</td><td>${T('刷新本页面，确认能正常登录')}</td></tr>
        <tr><td>${T('网络')}</td><td>${T('概览页查看接口地址与默认路由是否正常')}</td></tr>
        <tr><td>${T('路由服务')}</td><td>${T('概览页查看')} dnsmasq / pppd ${T('等服务状态')}</td></tr>
        <tr><td>${T('回滚预案')}</td><td>${T('系统设置')} → ${T('快照列表，选择升级前的快照回滚')}</td></tr>
      </tbody></table>
    </div>`;
  const r = await api('/api/upgrade/info');
  const d = r.data || {};
  $('#up-vnc').innerHTML = `
    <div class="kv"><b>VNC ${T('服务')}</b><span>${d.vnc_active ? `<span class="tag ok">${T('运行中（5900 监听）')}</span>` : `<span class="tag err">${T('未运行')}</span>`}</span></div>
    <div class="kv"><b>${T('已锁定包')}</b><span class="mono">${(d.holds || []).join(', ') || T('无')}</span></div>
    <div class="kv"><b>${T('提示')}</b><span>${esc(d.notice || '')}</span></div>`;
  $('#pk-holds').innerHTML = (d.holds || []).length
    ? `<table><tbody>${d.holds.map(h => `<tr><td class="mono">${esc(h)}</td></tr>`).join('')}</tbody></table>`
    : `<p class="desc">${T('当前没有任何被锁定的包。')}</p>`;
  $('#pk-upd').onclick = async () => {
    $('#pk-out').innerHTML = `<pre>${T('更新中，可能需要 1-2 分钟…')}</pre>`;
    const r2 = await api('/api/pkg', { method: 'POST', body: { op: 'update' } });
    $('#pk-out').innerHTML = `<pre>${esc(r2.msg_cn)}\n\n${esc((r2.data || {}).out || '')}</pre>`;
  };
  $('#pk-list').onclick = async () => {
    $('#pk-out').innerHTML = `<pre>${T('查询中…')}</pre>`;
    const r2 = await api('/api/pkg', { method: 'POST', body: { op: 'list' } });
    const dd = r2.data || {};
    $('#pk-out').innerHTML = `
      <div class="msg ${dd.risky && dd.risky.length ? 'warn' : 'ok'}">${esc(dd.notice || r2.msg_cn)}</div>
      ${(dd.risky || []).length ? `<div style="margin:10px 0"><b>${T('高危包：')}</b>
        ${dd.risky.map(x => `<span class="tag warn" style="margin:2px">${esc(x)}</span>`).join('')}</div>` : ''}
      <pre>${esc((dd.rows || []).join('\n') || T('没有可升级的包'))}</pre>`;
  };
  const holdOp = async op => {
    const names = $('#pk-hold-n').value.split(',').map(x => x.trim()).filter(Boolean);
    if (!names.length) { toast(T('请填写包名'), 'err'); return; }
    const r2 = await api('/api/pkg', { method: 'POST', body: { op, packages: names } });
    toast(r2.msg_cn, r2.ok ? 'ok' : 'err');
    if (r2.ok) setTimeout(viewUpdate, 500);
  };
  $('#pk-hold').onclick = () => holdOp('hold');
  $('#pk-unhold').onclick = () => holdOp('unhold');
}

/* ============================================================
   主题之家（#12）—— Web 内设计主题 / 离线预览 / ZIP 导入导出
   ============================================================ */

/* 可定制变量清单与前端镜像的中文说明。
   后端 theme.py 的 THEME_VARS 是唯一权威来源；这里只是「首屏即时渲染」
   用的副本，页面加载后会用 /api/theme?op=vars 的结果覆盖。 */
const TM_VARS = () => ([
  ['--bg', T('页面背景'), 'base'], ['--bg2', T('悬停背景'), 'base'],
  ['--panel', T('卡片背景'), 'base'], ['--panel2', T('表头背景'), 'base'],
  ['--line', T('边框'), 'base'], ['--line2', T('浅分隔线'), 'base'],
  ['--txt', T('主文字'), 'base'], ['--txt2', T('次要文字'), 'base'], ['--txt3', T('弱化文字'), 'base'],
  ['--pri', T('主题主色'), 'color'], ['--pri-d', T('主色加深'), 'color'], ['--pri-l', T('主色浅底'), 'color'],
  /* 主色本身若太浅（如暖橙），当链接/强调文字用会看不清；
     --pri-text 是同一色调的「可读版本」，--pri 仍保留原色用于填充与按钮。 */
  ['--pri-text', T('主色（文字用）'), 'color'],
  ['--ok', T('成功色'), 'color'], ['--ok-l', T('成功浅底'), 'color'],
  ['--warn', T('警告色'), 'color'], ['--warn-l', T('警告浅底'), 'color'],
  ['--err', T('错误色'), 'color'], ['--err-l', T('错误浅底'), 'color'],
  ['--info', T('信息色'), 'color'], ['--info-l', T('信息浅底'), 'color'],
  ['--r', T('卡片圆角'), 'shape'], ['--sh', T('卡片阴影'), 'shape'], ['--grad', T('主按钮渐变'), 'shape'],
  ['--side', T('侧栏宽度'), 'layout'], ['--side-c', T('侧栏折叠宽度'), 'layout'],
]);
const TM_CAT_CN = () => ({ base: T('基础配色'), color: T('主色与状态色'), shape: T('圆角 / 阴影 / 渐变'), layout: T('布局尺寸') });
const TM_CAT_ORDER = ['base', 'color', 'shape', 'layout'];
/* 具名颜色白名单 —— 必须与后端 theme.py 的 _NAMED 完全一致。
   缺少这一份，前端会把 red / transparent 这类合法取值判为非法，
   导致「明明后端允许，界面却报错」的不一致体验。 */
const TM_NAMED = ['aqua', 'beige', 'black', 'blue', 'brown', 'chocolate', 'coral',
  'crimson', 'currentcolor', 'cyan', 'darkgray', 'darkgreen', 'darkgrey', 'darkorange',
  'darkred', 'dodgerblue', 'forestgreen', 'fuchsia', 'gold', 'goldenrod', 'gray', 'green',
  'grey', 'indigo', 'ivory', 'khaki', 'lavender', 'lightgray', 'lightgrey', 'lime',
  'magenta', 'maroon', 'midnightblue', 'navy', 'olive', 'orange', 'orchid', 'peru', 'pink',
  'plum', 'purple', 'red', 'royalblue', 'salmon', 'seagreen', 'sienna', 'silver', 'skyblue',
  'slategray', 'slategrey', 'steelblue', 'teal', 'tomato', 'transparent', 'turquoise',
  'violet', 'wheat', 'white', 'whitesmoke', 'yellow'];

/* 内置基准值：主题不必写满全部变量，缺失的用这里的基准兜底 */
const TM_BASE = {
  '--bg': '#eef2f9', '--bg2': '#f2f5fa', '--panel': '#ffffff', '--panel2': '#fafbfe',
  '--line': '#e2e7f0', '--line2': '#eef1f7',
  '--txt': '#161f30', '--txt2': '#556480', '--txt3': '#8894a8',
  '--pri': '#1f6feb', '--pri-d': '#1858c4', '--pri-l': '#e8f0fe', '--pri-text': '#1f6feb',
  '--ok': '#12813f', '--ok-l': '#e3f7ea', '--warn': '#9a6700', '--warn-l': '#fff8e1',
  '--err': '#c0392b', '--err-l': '#fdecea', '--info': '#0b6e99', '--info-l': '#e5f4fb',
  '--r': '12px', '--side': '236px', '--side-c': '68px',
  '--sh': '0 1px 2px rgba(18,28,55,.05),0 2px 8px rgba(18,28,55,.05)',
  '--grad': 'linear-gradient(135deg,#2b7bf3 0%,#1f6feb 45%,#1552c0 100%)',
};

/* ---------- 合法性预判：与后端 theme.py 规则一致，用于即时中文提示 ---------- */
function tmCheckValue(v, name) {
  const s = String(v == null ? '' : v).trim();
  if (!s) return T('取值不能为空');
  if (s.length > 220) return T('取值过长（最多 220 字符）');
  const low = s.toLowerCase();
  for (const ch of [';', '{', '}', '<', '>', '@', '\\', '!', '`']) {
    if (s.includes(ch)) return `${T('包含非法字符「')}${ch}」`;
  }
  if (s.includes('/*') || s.includes('*/')) return T('不允许出现注释符号');
  if (/url\(|expression|import|javascript/i.test(s)) return T('不允许使用外部引用（url / expression / import）');
  if (low.startsWith('var(')) {
    const m = /^var\(\s*(--[a-z0-9-]+)\s*(?:,[^)]*)?\)$/.exec(low);
    if (!m) return T('var() 引用写法不正确');
    if (!TM_VARS().some(x => x[0] === m[1])) return `${T('不允许引用未开放的变量')} ${m[1]}`;
    return '';
  }
  if (s.startsWith('#')) {
    return /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(s)
      ? '' : T('颜色格式不正确（支持 #rgb / #rrggbb / #rrggbbaa）');
  }
  if (low.startsWith('rgb') || low.startsWith('hsl')) {
    const N255 = '(?:\\d|[1-9]\\d|1\\d{2}|2[0-4]\\d|25[0-5])';
    const ALPHA = '(?:0|1|0?\\.\\d{1,3})';
    const HUE = '(?:\\d|[1-9]\\d|[12]\\d{2}|3[0-5]\\d|360)(?:\\.\\d+)?(?:deg)?';
    const PCT = '(?:\\d|[1-9]\\d|100)%';
    return (new RegExp(`^rgba?\\(\\s*${N255}\\s*,\\s*${N255}\\s*,\\s*${N255}(\\s*,\\s*${ALPHA})?\\s*\\)$`).test(low)
      || new RegExp(`^hsla?\\(\\s*${HUE}\\s*,\\s*${PCT}\\s*,\\s*${PCT}(,\\s*${ALPHA})?\\s*\\)$`).test(low))
      ? '' : T('颜色函数格式不正确（示例：rgba(31,111,235,.35)，RGB 需在 0-255 内）');
  }
  if (/^\d+(\.\d+)?(px|rem|em|%|vh|vw|pt|ch)$/.test(low)) {
    if (name && ['--r', '--side', '--side-c'].includes(name) && /(vh|vw)$/.test(low)) {
      return T('该变量不支持 vh / vw 单位');
    }
    return '';
  }
  if (low === '0' || TM_NAMED.indexOf(low) >= 0) return '';
  if (/^(linear-gradient|radial-gradient)\(/i.test(s)) return '';
  if (/^(?:(?:-?\d+(?:\.\d+)?(?:px|em|rem)?|rgba?\([^)]*\)|hsla?\([^)]*\)|#[0-9a-fA-F]{3,8}|inset|none|,|\s)+)$/i.test(s)) return '';
  if (/^[a-z]+$/i.test(s)) return T('不支持的具名取值，请用 #rrggbb 或 rgb() 形式');
  return T('取值格式无法识别');
}

function tmCheckCss(css) {
  let s = String(css || '').replace(/\/\*[\s\S]*?\*\//g, '');
  if (!s.trim()) return '';
  if (s.includes('@')) return T('自定义 CSS 不允许使用 @ 规则（如 @import / @media）');
  if (s.includes('\\')) return T('自定义 CSS 不允许使用转义字符');
  if (/javascript\s*:|expression\s*\(|behavior\s*:|url\s*\(/i.test(s)) return T('自定义 CSS 不允许外部引用');
  let rest = s.trim(), guard = 0;
  while (rest) {
    if (++guard > 200) return T('自定义 CSS 结构过于复杂');
    const m = /^([^{}]+)\{([^{}]*)\}/.exec(rest);
    if (!m) return T('自定义 CSS 语法不正确：应为「选择器 { 属性: 取值; }」形式');
    if (!m[1].trim()) return T('自定义 CSS 存在空选择器');
    if (/[<>]/.test(m[1])) return T('自定义 CSS 选择器包含非法字符');
    for (const decl of m[2].split(';')) {
      const d = decl.trim();
      if (!d) continue;
      if (!d.includes(':')) return `${T('自定义')} CSS ${T('声明缺少冒号：')}${d.slice(0, 40)}`;
      const prop = d.slice(0, d.indexOf(':')).trim().toLowerCase();
      if (!/^(--[a-z0-9-]+|[a-z-]+)$/.test(prop)) return `${T('属性名不合法：')}${prop.slice(0, 40)}`;
      const val = d.slice(d.indexOf(':') + 1).trim();
      if (!/^[0-9a-zA-Z#(),.\s%\-"/_]+$/.test(val)) return `${T('取值含非法字符：')}${d.slice(0, 40)}`;
    }
    rest = rest.slice(m[0].length).trim();
  }
  return '';
}

/* #rrggbb → 可用于 <input type=color> 的 6 位色；不是 hex 则返回 fallback */
function tmToHex(v, fallback = '#888888') {
  let s = String(v || '').trim().replace(/^#/, '');
  if (/^[0-9a-fA-F]{6}$/.test(s)) return '#' + s.toLowerCase();
  if (/^[0-9a-fA-F]{3}$/.test(s)) return '#' + s.split('').map(c => c + c).join('').toLowerCase();
  return fallback;
}

/* 当前编辑中的主题对象（DOM → 对象） */
function tmCur() {
  const e = S.themeEdit || {};
  const vars = {};
  TM_VARS().forEach(([k]) => {
    const el = document.getElementById('tm-v-' + k.replace(/[^a-z0-9-]/gi, ''));
    if (!el) return;
    const on = document.getElementById('tm-o-' + k.replace(/[^a-z0-9-]/gi, ''));
    if (on && !on.checked) return;
    const v = String(el.value || '').trim();
    if (v) vars[k] = v;
  });
  const gv = id => { const x = document.getElementById(id); return x ? String(x.value || '').trim() : ''; };
  const cssEl = document.getElementById('tm-css');
  return {
    id: e.id || '',
    name: gv('tm-name') || T('我的主题'),
    author: gv('tm-author'),
    description: gv('tm-desc'),
    version: gv('tm-version') || '1.0',
    dark: !!(document.getElementById('tm-dark') || {}).checked,
    vars,
    css: cssEl ? String(cssEl.value || '') : '',
  };
}

/* 本地粗检（后端还会做一次权威校验） */
/* ⛔ 形参**不能叫 t** —— 它会遮蔽全局翻译函数 t()。
 *    原来的 `function tmLocalIssues(t)` 里既 `t.name` 取主题、
 *    又 `T('请填写主题名称')` 查翻译，后者必抛
 *    「t is not a function」（check-render 抓到）。
 *    主题对象用 th，翻译用别名 T。 */
function tmLocalIssues(th) {
  const out = [];
  const nm = String(th.name || '').trim();
  if (!nm) out.push(T('请填写主题名称'));
  else if (!/^[一-龥A-Za-z0-9 _.\-()]{1,48}$/.test(nm)) {
    out.push(T('主题名称只能包含中文、字母、数字、空格与 - _ . ( )'));
  }
  const keys = Object.keys(th.vars || {});
  if (!keys.length) out.push(T('至少要改动 1 个变量'));
  for (const k of keys) {
    const why = tmCheckValue(th.vars[k], k);
    if (why) out.push(`${T('变量')} ${k} ${T('取值不合法：')}${why}`);
  }
  const cw = tmCheckCss(th.css);
  if (cw) out.push(cw);
  return out;
}

function tmIssuesHTML(list) {
  return list.length
    ? `<div class="tm-check bad"><b>${T('共')} ${list.length} ${T('处问题，已阻止提交：')}</b><ul>${
      list.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>`
    : `<div class="tm-check ok">${T('校验通过：主题结构、全部变量取值与自定义')} CSS ${T('均合法。')}</div>`;
}

/* 把主题渲染为 CSS 文本（与后端一致的输出格式，用于离线预览与「查看代码」） */
function tmToCss(t) {
  const lines = [];
  // 主题名 / 版本是自由文本，原样拼进 /* */ 注释时，一个 "*/" 就能提前闭合注释、
  // 把后面的内容变成真正的 CSS 规则。这里必须先把注释终止符和换行剥掉。
  const cmt = s => String(s || '').replace(/\/\*/g, '').replace(/\*\//g, '').replace(/[\r\n]+/g, ' ');
  lines.push(`/* Drouter ${T('主题：')}${cmt(t.name)}${t.version ? ' v' + cmt(t.version) : ''} */`);
  lines.push(':root{');
  Object.keys(t.vars || {}).sort().forEach(k => { lines.push(`  ${k}:${t.vars[k]};`); });
  lines.push('}');
  lines.push(`:root{color-scheme:${t.dark ? 'dark' : 'light'}}`);
  if (String(t.css || '').trim()) {
    lines.push('', T('/* ---- 作者自定义 CSS ---- */'), String(t.css).trim());
  }
  return lines.join('\n') + '\n';
}

/* 主题名 → 安全缩略色组（优先用主题自己的变量，缺失则回落到基准） */
function tmSwatch(t) {
  const v = Object.assign({}, TM_BASE, t.vars || {});
  return [v['--pri'], v['--bg'], v['--panel'], v['--txt']];
}

/* ---------- 离线预览：在一个隔离 iframe 里渲染一份「Drouter UI 缩影」 ----------
   用 sandbox 隔离 + srcdoc，无需网络请求，也不影响当前页面；
   主题 CSS 作为 :root 变量注入，因此能真实还原管理台的观感。 */
function tmPreviewDoc(t) {
  const v = Object.assign({}, TM_BASE, t.vars || {});
  const cssVars = Object.keys(v).map(k => `${k}:${v[k]};`).join('\n    ');
  const extra = String(t.css || '').trim();
  const r = v['--r'] || '12px';
  const grad = v['--grad'] || 'linear-gradient(135deg,#2b7bf3,#1f6feb)';
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${T('主题离线预览')}</title>
<style>
  :root{
    ${cssVars}
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
    background:var(--bg);color:var(--txt);-webkit-font-smoothing:antialiased}
  .tm-pv{display:flex;height:100vh;min-height:560px}
  .tm-pv-side{flex:0 0 200px;background:var(--panel);border-right:1px solid var(--line);
    padding:14px 12px;display:flex;flex-direction:column;gap:4px}
  .tm-pv-brand{display:flex;align-items:center;gap:9px;margin-bottom:12px}
  .tm-pv-brand i{width:26px;height:26px;border-radius:8px;display:block;background:${grad};flex:0 0 26px}
  .tm-pv-brand b{font-size:15px;color:var(--txt)}
  .tm-pv-g{font-size:11px;font-weight:600;color:hsl(210 72% 42%);margin:8px 0 2px}
  .tm-pv-i{padding:6px 9px;border-radius:8px;font-size:12.5px;color:var(--txt2)}
  .tm-pv-i.on{background:var(--pri-l);color:var(--pri);font-weight:600}
  .tm-pv-main{flex:1;min-width:0;display:flex;flex-direction:column}
  .tm-pv-top{height:52px;flex:0 0 52px;display:flex;align-items:center;justify-content:space-between;
    padding:0 18px;background:var(--panel);border-bottom:1px solid var(--line)}
  .tm-pv-top h4{margin:0;font-size:15px;color:var(--txt)}
  .tm-pv-ck{font-size:12.5px;color:var(--txt3)}
  .tm-pv-body{flex:1;overflow:auto;padding:16px 18px}
  .tm-pv-card{background:var(--panel);border:1px solid var(--line);border-radius:${r};
    padding:14px 16px;margin-bottom:12px;box-shadow:var(--sh)}
  .tm-pv-card h5{margin:0 0 10px;font-size:13.5px;color:var(--txt)}
  .tm-pv-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
  .tm-pv-st{background:var(--panel);border:1px solid var(--line);border-radius:${r};padding:11px 13px}
  .tm-pv-st .l{font-size:11px;color:var(--txt3);margin-bottom:4px}
  .tm-pv-st .v{font-size:17px;font-weight:650;color:var(--txt)}
  .tm-pv-st .b{height:6px;border-radius:3px;background:var(--line2);margin-top:6px;overflow:hidden}
  .tm-pv-st .b i{display:block;height:100%;background:var(--pri)}
  .tm-pv-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  .tm-pv-btn{padding:6px 14px;border-radius:9px;font-size:12.5px;border:1px solid var(--line);
    background:var(--panel);color:var(--txt)}
  .tm-pv-btn.pri{background:${grad};color:#fff;border-color:transparent;font-weight:600}
  .tm-pv-btn.dg{color:var(--err);border-color:var(--err)}
  .tm-pv-tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .tm-pv-tag{font-size:11px;padding:2px 7px;border-radius:5px}
  .tm-pv-tag.ok{background:var(--ok-l);color:var(--ok)}
  .tm-pv-tag.wn{background:var(--warn-l);color:var(--warn)}
  .tm-pv-tag.er{background:var(--err-l);color:var(--err)}
  .tm-pv-tag.if{background:var(--info-l);color:var(--info)}
  table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
  th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line2)}
  th{color:var(--txt2);background:var(--panel2);font-size:11px;font-weight:600}
  td{color:var(--txt2)}
  .tm-pv-note{font-size:11.5px;color:var(--txt3);margin-top:6px}
  ${extra}
</style></head>
<body>
<div class="tm-pv">
  <aside class="tm-pv-side">
    <div class="tm-pv-brand"><i></i><b>Drouter</b></div>
    <div class="tm-pv-g">${T('概览')}</div>
    <div class="tm-pv-i on">${T('系统概览')}</div>
    <div class="tm-pv-i">${T('网络状态')} / ${T('加速')}</div>
    <div class="tm-pv-g">${T('接口')}</div>
    <div class="tm-pv-i">${T('网卡与桥接')}</div>
    <div class="tm-pv-i">${T('WAN 口')}</div>
    <div class="tm-pv-i">LAN ${T('口')}</div>
    <div class="tm-pv-g">${T('安全')}</div>
    <div class="tm-pv-i">${T('防火墙')} IPv4</div>
    <div class="tm-pv-i">${T('端口转发')} / DMZ</div>
  </aside>
  <main class="tm-pv-main">
    <header class="tm-pv-top">
      <h4>${T('系统概览')}</h4>
      <span class="tm-pv-ck">2026${T('年9月28')}${T('日 星期一')} · ${T('农历八月')}</span>
    </header>
    <div class="tm-pv-body">
      <div class="tm-pv-card">
        <h5>${T('实时状态')}</h5>
        <div class="tm-pv-grid">
          <div class="tm-pv-st"><div class="l">CPU ${T('使用率')}</div><div class="v">18%</div>
            <div class="b"><i style="width:18%"></i></div></div>
          <div class="tm-pv-st"><div class="l">${T('内存占用')}</div><div class="v">42%</div>
            <div class="b"><i style="width:42%"></i></div></div>
          <div class="tm-pv-st"><div class="l">${T('下行速率')}</div><div class="v">86.4</div>
            <div class="b"><i style="width:64%"></i></div></div>
          <div class="tm-pv-st"><div class="l">${T('在线终端')}</div><div class="v">12</div>
            <div class="b"><i style="width:35%"></i></div></div>
        </div>
      </div>
      <div class="tm-pv-card">
        <h5>${T('操作与服务')}</h5>
        <div class="tm-pv-row">
          <button class="tm-pv-btn pri">${T('保存并应用')}</button>
          <button class="tm-pv-btn">${T('仅保存')}</button>
          <button class="tm-pv-btn dg">${T('删除')}</button>
        </div>
        <div class="tm-pv-tags">
          <span class="tm-pv-tag ok">${T('运行中')}</span>
          <span class="tm-pv-tag if">${T('信息')}</span>
          <span class="tm-pv-tag wn">${T('警告')}</span>
          <span class="tm-pv-tag er">${T('错误')}</span>
        </div>
        <p class="tm-pv-note">${T('这是离线预览')} —— ${T('与主界面使用同一套')} CSS ${T('变量，所见即所得。')}</p>
      </div>
      <div class="tm-pv-card">
        <h5>${T('连接简表')}</h5>
        <table>
          <thead><tr><th>${T('源地址')}</th><th>${T('目标')}</th><th>${T('协议')}</th><th>${T('状态')}</th></tr></thead>
          <tbody>
            <tr><td>192.168.7.35</td><td>93.184.216.34:443</td><td>TCP</td><td>${T('已建立')}</td></tr>
            <tr><td>192.168.7.18</td><td>223.5.5.5:53</td><td>UDP</td><td>${T('已建立')}</td></tr>
            <tr><td>192.168.7.62</td><td>140.82.114.4:443</td><td>TCP</td><td>${T('已建立')}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </main>
</div>
</body></html>`;
}

function tmRefreshPreview() {
  const t = tmCur();
  const issues = tmLocalIssues(t);
  const box = document.getElementById('tm-issues');
  if (box) box.innerHTML = tmIssuesHTML(issues);
  const frame = document.getElementById('tm-frame');
  if (frame) frame.srcdoc = tmPreviewDoc(t);
  const code = document.getElementById('tm-code');
  if (code) code.textContent = tmToCss(t);
}

/* ---------- ZIP 导入导出（纯浏览器 + 后端 Base64 通道） ---------- */
function tmDownloadB64(b64, filename) {
  try {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([arr], { type: 'application/zip' }));
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1500);
    return true;
  } catch (e) {
    toast(T('下载失败：') + netErrCn(e && e.message), 'err');
    return false;
  }
}

async function tmUpload(file) {
  const tip = document.getElementById('tm-import-msg');
  const show = (cls, txt) => { tip.className = 'tm-check ' + cls; tip.innerHTML = txt; };
  if (!file) return;
  const kb = Math.round(file.size / 1024);
  if (file.size > 4 * 1024 * 1024) { show('bad', `${T('文件过大（')}${kb} KB${T('），主题包上限')} 4 MB`); return; }
  if (!/\.(zip|drtheme)$/i.test(file.name)) {
    show('bad', T('请选择 .zip 主题包（由「导出」按钮生成的那种）'));
    return;
  }
  show('', T('正在读取并校验…'));
  let b64;
  try {
    const dataUrl = await new Promise((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.onerror = () => rej(new Error(T('文件读取失败')));
      fr.readAsDataURL(file);
    });
    b64 = String(dataUrl).split(',')[1] || '';
  } catch (e) {
    show('bad', T('文件读取失败：') + esc(netErrCn(e && e.message))); return;
  }
  const r = await api('/api/theme/op', { method: 'POST', body: { op: 'import', b64 } });
  if (!r.ok) {
    // 不规范的主题：给出中文原因，且绝不应用
    show('bad', `<b>${T('导入失败，主题未应用。')}</b><br>${esc(r.msg_cn)}`);
    toast(T('主题校验未通过，已拒绝应用'), 'err', 6000);
    return;
  }
  show('ok', `<b>${T('导入成功')}</b> —— ${esc(r.msg_cn)}`);
  toast(T('主题导入成功'), 'ok');
  await tmReload();
  const d = r.data || {};
  if (d.id) tmApply(d.id, true);
}

/* ---------- 主题操作 ---------- */
async function tmApply(id, quiet) {
  const r = await api('/api/theme/op', { method: 'POST', body: { op: 'apply', id } });
  if (!r.ok) { toast(r.msg_cn, 'err', 6000); return false; }
  // 强制浏览器重新拉取 theme.css（绕开缓存）
  const lk = document.getElementById('theme-css');
  if (lk) lk.href = '/theme.css?v=' + Date.now();
  toast(r.msg_cn, 'ok');
  if (!quiet) await tmReload();
  return true;
}

async function tmReload() {
  const [lst, meta] = await Promise.all([
    api('/api/theme?op=list'),
    api('/api/theme?op=vars'),
  ]);
  if (lst.ok) S.theme = lst.data || {};
  if (meta.ok) {
    S.themeMeta = meta.data || {};
    const d = S.themeMeta.defaults || {};
    Object.keys(d).forEach(k => { if (d[k]) TM_BASE[k] = d[k]; });
  }
}

/* ---------- 主视图 ---------- */
async function viewTheme() {
  const v = $('#view');
  v.innerHTML = `<div class="card">${T('正在读取主题列表…')}</div>`;
  await tmReload();
  if (!S.theme || !S.theme.themes) {
    v.innerHTML = `<div class="card"><h3 style="color:var(--err)">${T('读取失败')}</h3>
      <p class="desc">${T('未能读取主题列表，请确认后端服务正常后重试。')}</p></div>`;
    return;
  }
  if (!S.themeEdit) S.themeEdit = { id: '' };
  tmRender();
}

function tmRender() {
  const TH = S.theme || {};
  const themes = TH.themes || [];
  const active = TH.active || 'default';
  const cur = themes.find(x => x.id === active);
  const e = S.themeEdit || { id: '' };
  const v = $('#view');

  v.innerHTML = `
    <div class="card">
      <h3>${T('主题之家')}
        <span class="tag ${cur ? 'ok' : 'gray'}" style="float:right">
          ${T('当前：')}${esc(cur ? tmName(cur) : T('未应用主题'))}</span></h3>
      <p class="desc">${T('在这里挑选、设计并把玩界面外观。所有变更')}<b>${T('只影响')} Web ${T('管理台外观')}</b>${T('， 不会改动任何网络配置、防火墙规则或服务。')}</p>
      <div class="tm-current">
        <div class="tm-cur-swatch">${tmSwatch(cur || { vars: {} }).map(c =>
    `<i style="background:${esc(c)}"></i>`).join('')}</div>
        <div class="tm-cur-body">
          <b>${esc(cur ? tmName(cur) : T('内置默认'))}</b>
          <p class="desc" style="margin:3px 0 0">${esc(cur && cur.description ? (bt4('THEME_DESC', cur.id, 'en', cur.description) || cur.description) : T('尚未应用自定义主题'))}</p>
        </div>
        <div class="tm-cur-acts">
          <button class="ghost small fixed" id="tm-reset">${T('恢复默认主题')}</button>
        </div>
      </div>
    </div>

    <div class="tm-tabs">
      <button class="tm-tab${S.themeTab === 'gallery' ? ' on' : ''}" data-tm-tab="gallery">${T('主题广场')}</button>
      <button class="tm-tab${S.themeTab === 'design' ? ' on' : ''}" data-tm-tab="design">${T('主题设计器')}</button>
      <button class="tm-tab${S.themeTab === 'import' ? ' on' : ''}" data-tm-tab="import">${T('导入')} / ${T('导出')}</button>
    </div>

    <div id="tm-pane"></div>`;

  $$('.tm-tab').forEach(b => b.onclick = () => { S.themeTab = b.dataset.tmTab; tmRender(); });
  const rs = $('#tm-reset');
  if (rs) rs.onclick = async () => {
    const r = await api('/api/theme/op', { method: 'POST', body: { op: 'reset' } });
    if (r.ok) {
      const lk = document.getElementById('theme-css');
      if (lk) lk.href = '/theme.css?v=' + Date.now();
      toast(r.msg_cn, 'ok'); await tmReload(); tmRender();
    } else toast(r.msg_cn, 'err');
  };
  tmRenderPane();
}

function tmRenderPane() {
  const TH = S.theme || {};
  const themes = TH.themes || [];
  const active = TH.active || 'default';
  const pane = $('#tm-pane');
  if (!pane) return;
  if (S.themeTab === 'gallery') {
    pane.innerHTML = `
      <div class="card">
        <h3>${T('全部主题（')}${themes.length}）
          <span class="desc" style="float:right;margin:0">${T('共开放')} ${TM_VARS().length} ${T('个可定制变量')}</span></h3>
        <p class="desc">${T('内置主题不可修改或删除；想改的话点「以此为基础」复制到设计器里。')}</p>
        <div class="tm-grid">
          ${themes.map(t => tmThemeCard(t, t.id === active)).join('')}
        </div>
      </div>`;
    $$('[data-tm-act]').forEach(b => b.onclick = () => tmCardAct(b.dataset.tmAct, b.dataset.tmId));
    return;
  }
  if (S.themeTab === 'import') {
    pane.innerHTML = `
      <div class="card">
        <h3>${T('导入主题包')}</h3>
        <p class="desc">${T('选择由「导出」生成的')} <span class="mono">.zip</span> ${T('主题包。 系统会逐项校验（结构、变量白名单、取值合法性、')}CSS ${T('安全性），')}
          <b>${T('任何一项不通过都会给出中文原因并拒绝应用')}</b>${T('，不会污染现有配置。')}</p>
        <div class="tm-drop" id="tm-drop">
          <p style="margin:0 0 10px">${T('把主题包拖到这里，或')}</p>
          <input type="file" id="tm-file" accept=".zip,.drtheme" style="display:none">
          <button class="primary small" id="tm-pick">${T('选择主题包')}</button>
          <p class="desc" style="margin:10px 0 0">${T('单个文件上限')} 4 MB${T('，解压后上限')} 512 KB</p>
        </div>
        <div id="tm-import-msg" class="tm-check" style="display:none"></div>
      </div>

      <div class="card">
        <h3>${T('导出当前主题')}</h3>
        <p class="desc">${T('把主题打包成独立的')} <span class="mono">.zip</span>${T('，可以分享给别人或备份。 包内含')} <span class="mono">theme.json</span>${T('（清单）、')}<span class="mono">theme.css</span>${T('（渲染结果） 与')} <span class="mono">README.txt</span>${T('（安装说明）。')}</p>
        <div class="row">
          <label style="flex:1 1 260px">${T('要导出的主题')}
            <select id="tm-export-sel">
              ${themes.map(t => `<option value="${esc(t.id)}"${t.id === active ? ' selected' : ''}>${tmName(t)}${t.builtin ? T('（内置）') : ''}</option>`).join('')}
            </select></label>
          <button class="ghost fixed" id="tm-export">${T('打包并下载')}</button>
        </div>
        <div id="tm-export-msg" class="tm-check" style="display:none"></div>
      </div>

      <div class="card">
        <h3>${T('主题包格式说明')}</h3>
        <p class="desc">${T('如果你想手工写主题，包的结构非常简单：')}</p>
        <pre>${T('主题包')}.zip
├── theme.json   ← ${T('必需，')}UTF-8${T('，见下方字段')}
├── theme.css    ← ${T('可选，导入时会被重新生成')}
└── README.txt   ← ${T('可选')}</pre>
        <p class="desc" style="margin-top:10px"><span class="mono">theme.json</span> ${T('字段：')}</p>
        <pre>{
  "name":        "${T('我的主题')}",        // ${T('必需，')}≤48 ${T('字')}
  "description": "${T('一句话说明')}",      // ${T('可选')}
  "author":      "${T('作者名')}",          // ${T('可选')}
  "version":     "1.0",            // ${T('可选')}
  "dark":        false,            // ${T('可选，')}true = ${T('深色主题')}
  "vars": {                        // ${T('必需，至少')} 1 ${T('个')}
    "--pri": "#1f6feb",
    "--bg":  "#eef2f9"
  },
  "css": ""                        // ${T('可选，追加的自定义')} CSS
}</pre>
        <p class="hint-inline">${T('只有')} <span class="mono">--bg / --panel / --pri / --txt …</span>
          ${T('这')} ${TM_VARS().length} ${T('个变量会被接受。其它变量、')}<span class="mono">@import</span>、
          <span class="mono">url()</span>、<span class="mono">javascript:</span>
          ${T('一律拒绝并给出中文提示。')}</p>
      </div>`;
    const pick = $('#tm-pick'), file = $('#tm-file'), drop = $('#tm-drop');
    pick.onclick = () => file.click();
    file.onchange = () => tmUpload(file.files[0]);
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
      e.preventDefault(); drop.classList.add('hot');
    }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
      e.preventDefault(); drop.classList.remove('hot');
    }));
    drop.addEventListener('drop', e => {
      if (e.dataTransfer.files && e.dataTransfer.files[0]) tmUpload(e.dataTransfer.files[0]);
    });
    const msg = $('#tm-import-msg');
    msg.style.display = '';
    $('#tm-export').onclick = tmExport;
    $('#tm-export-msg').style.display = '';
    return;
  }
  // design
  const e = S.themeEdit || {};
  pane.innerHTML = `
      <div class="card">
        <h3>${e.id ? T('编辑主题') : T('新建主题')}
          ${e.id ? `<span class="tag info" style="float:right">${esc(e.id)}</span>` : ''}</h3>
        <p class="desc">${T('左侧调整参数，右侧即时预览。')}<b>${T('预览完全在本地完成')}</b>${T('（沙箱')} iframe${T('）， 不会影响真实界面；觉得满意再点「保存并应用」。')}</p>
        <div class="row">
          <label style="flex:1 1 200px">${T('主题名称')}<input id="tm-name" value="${esc(e.name || T('我的主题'))}" maxlength="48"></label>
          <label style="flex:1 1 140px">${T('作者')}<input id="tm-author" value="${esc(e.author || '')}" maxlength="64" placeholder="${T('选填')}"></label>
          <label style="flex:1 1 120px">${T('版本')}<input id="tm-version" value="${esc(e.version || '1.0')}" maxlength="24"></label>
        </div>
        <label>${T('一句话描述')}<textarea id="tm-desc" rows="2" maxlength="300"
          placeholder="${T('选填，会显示在主题卡片上')}">${esc(e.description || '')}</textarea></label>
        <label class="switch" style="display:inline-flex;margin-right:20px">
          <input type="checkbox" id="tm-dark"${e.dark ? ' checked' : ''}><i></i>${T('深色主题')}</label>
        <div class="row" style="margin-top:4px">
          <label style="flex:0 0 110px">${T('一键配色主色')}
            <input type="color" id="tm-seed" value="${esc(tmToHex((e.vars || {})['--pri'], '#1f6feb'))}"></label>
          <button class="ghost fixed" id="tm-palette">${T('按此主色生成整套配色')}</button>
          <span class="desc" style="flex:1 1 200px;margin:0">${T('会同时重算背景、文字、状态色与按钮渐变，保证对比度')}</span>
        </div>
      </div>

      <div class="tm-split">
        <div class="card">
          <h3>${T('变量调整')}</h3>
          <p class="desc">${T('勾选表示「包含在主题里」。取消勾选的变量不会写进主题， 应用到管理台时会回落到默认值。')}</p>
          <div id="tm-vars">${tmVarsHTML(e)}</div>
          <h3 style="margin-top:18px">${T('自定义')} CSS${T('（高级）')}</h3>
          <p class="desc">${T('只接受「选择器')} { ${T('属性')}: ${T('取值')}; }${T('」形式；禁止')}
            <span class="mono">@import / @media</span>、<span class="mono">url()</span>、
            <span class="mono">javascript:</span> ${T('等外部引用。')}</p>
          <textarea id="tm-css" rows="6" spellcheck="false"
            placeholder=".card{border-width:2px}&#10;.stat .val{font-size:22px}">${esc(e.css || '')}</textarea>
        </div>
        <div class="card">
          <h3>${T('离线预览')}</h3>
          <p class="desc">${T('下方是隔离沙箱中的真实渲染效果，随左侧改动实时更新。')}</p>
          <iframe id="tm-frame" class="tm-frame" sandbox="allow-same-origin"
                  title="${T('主题离线预览')}"></iframe>
          <div class="row" style="margin-top:10px">
            <button class="ghost small fixed" id="tm-frame-new">${T('在新窗口打开预览')}</button>
            <button class="ghost small fixed" id="tm-frame-re">${T('重新渲染预览')}</button>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>${T('校验与操作')}</h3>
        <div id="tm-issues" class="tm-check"></div>
        <div class="row" style="margin-top:12px">
          <button class="ghost fixed" id="tm-check">${T('校验主题')}</button>
          <button class="ghost fixed" id="tm-save">${T('仅保存')}</button>
          <button class="primary fixed" id="tm-save-apply">${T('保存并应用')}</button>
          <button class="ghost fixed" id="tm-export-cur">${T('导出为')} ZIP</button>
        </div>
      </div>

      <div class="card">
        <h3>${T('主题代码')}</h3>
        <p class="desc">${T('最终写入')} <span class="mono">theme.css</span> ${T('的完整内容，可直接复制到别处使用。')}</p>
        <div class="code-wrap">
          <button class="code-copy" id="tm-copy-css">${T('复制')}</button>
          <pre id="tm-code" class="term-pre"></pre>
        </div>
      </div>`;
    tmBindDesign();
    tmRefreshPreview();
}

function tmVarsHTML(e) {
  const vars = e.vars || {};
  return TM_CAT_ORDER.map(cat => {
    const items = TM_VARS().filter(x => x[2] === cat);
    return `<div class="tm-cat">
      <div class="tm-cat-t">${esc(TM_CAT_CN()[cat] || cat)}</div>
      ${items.map(([k, cn]) => {
      const key = k.replace(/[^a-z0-9-]/gi, '');
      const val = vars[k] || TM_BASE[k] || '';
      const isColor = !['--r', '--sh', '--grad', '--side', '--side-c'].includes(k);
      return `<div class="tm-vrow">
          <label class="switch" style="margin:0;flex:0 0 auto">
            <input type="checkbox" id="tm-o-${key}"${vars[k] ? ' checked' : ''}><i></i></label>
          <span class="tm-vname">${esc(cn)}<br><span class="mono" style="font-size:11px;color:var(--txt3)">${esc(k)}</span></span>
          ${isColor ? `<input type="color" class="tm-color" data-for="${key}"
              value="${esc(tmToHex(val, '#cccccc'))}">` : ''}
          <input type="text" id="tm-v-${key}" class="tm-vtext mono" value="${esc(val)}"
            spellcheck="false" autocomplete="off">
          <button class="ghost small tm-vreset" data-reset="${key}"
            title="${T('回到默认')}">↺</button>
        </div>`;
    }).join('')}
    </div>`;
  }).join('');
}

function tmBindDesign() {
  $$('.tm-color').forEach(c => c.oninput = () => {
    const el = document.getElementById('tm-v-' + c.dataset.for);
    if (el) { el.value = c.value; tmSyncSwatch(c.dataset.for, c.value); tmRefreshPreview(); }
  });
  $$('.tm-vtext').forEach(t => t.oninput = () => {
    const cb = document.getElementById('tm-o-' + t.id.replace('tm-v-', ''));
    if (cb) cb.checked = true;
    const sw = document.querySelector('.tm-color[data-for="' + t.id.replace('tm-v-', '') + '"]');
    if (sw) sw.value = tmToHex(t.value, sw.value);
    tmRefreshPreview();
  });
  $$('[data-reset]').forEach(b => b.onclick = () => {
    const el = document.getElementById('tm-v-' + b.dataset.reset);
    const sw = document.querySelector('.tm-color[data-for="' + b.dataset.reset + '"]');
    if (el) el.value = TM_BASE['--' + b.dataset.reset.replace(/^/, '').replace(/^/, '')] || el.value;
    const key = '--' + b.dataset.reset;
    if (el) el.value = TM_BASE[key] || el.value;
    if (sw) sw.value = tmToHex(el.value, sw.value);
    tmRefreshPreview();
  });
  $$('.tm-vrow input[type=checkbox]').forEach(c => c.onchange = () => tmRefreshPreview());
  const dk = document.getElementById('tm-dark');
  if (dk) dk.onchange = () => tmRefreshPreview();
  const cssEl = document.getElementById('tm-css');
  if (cssEl) cssEl.oninput = debounce(() => tmRefreshPreview(), 250);
  ['tm-name', 'tm-desc', 'tm-version', 'tm-author'].forEach(id => {
    const x = document.getElementById(id);
    if (x) x.oninput = debounce(() => tmRefreshPreview(), 300);
  });
  $('#tm-frame-re').onclick = () => tmRefreshPreview();
  $('#tm-frame-new').onclick = () => {
    const t = tmCur();
    const w = window.open('', '_blank');
    if (w) { w.document.write(tmPreviewDoc(t)); w.document.close(); }
    else toast(T('浏览器拦截了弹出窗口，请允许后再试'), 'warn');
  };
  $('#tm-check').onclick = () => {
    const issues = tmLocalIssues(tmCur());
    $('#tm-issues').innerHTML = tmIssuesHTML(issues);
    toast(issues.length ? `${T('发现')} ${issues.length} ${T('处问题')}` : T('校验通过'), issues.length ? 'err' : 'ok');
  };
  $('#tm-save').onclick = () => tmSave(false);
  $('#tm-save-apply').onclick = () => tmSave(true);
  $('#tm-export-cur').onclick = () => tmExportDraft();
  // 「按此主色生成整套配色」原先把取色器和按钮都渲染出来了，却从没接过任何 handler，
  // 后端也没有对应的 op —— 用户点了完全没反应。配色统一交给后端 make_palette，
  // 与内置主题共用同一套对比度保证，前端不自己造一份（否则会和有权重的那份跑偏）。
  const palBtn = $('#tm-palette');
  if (palBtn) palBtn.onclick = async () => {
    const seed = ($('#tm-seed') || {}).value || '#1f6feb';
    const dkEl = document.getElementById('tm-dark');
    palBtn.disabled = true;
    palBtn.textContent = T('生成中…');
    const r = await api('/api/theme/op', { method: 'POST',
      body: { op: 'palette', color: seed, dark: !!(dkEl && dkEl.checked) } });
    palBtn.disabled = false;
    palBtn.textContent = T('按此主色生成整套配色');
    if (!r.ok) { toast(T('配色生成失败：') + (r.msg_cn || ''), 'err', 6000); return; }
    const vars = (r.data || {}).vars || {};
    Object.keys(vars).forEach(k => {
      const key = k.replace(/[^a-z0-9-]/gi, '');
      const txt = document.getElementById('tm-v-' + key);
      if (txt) txt.value = vars[k];
      const cb = document.getElementById('tm-o-' + key);
      if (cb) cb.checked = true;          // 勾选 = 写进主题
      const sw = document.querySelector('.tm-color[data-for="' + key + '"]');
      if (sw) sw.value = tmToHex(vars[k], sw.value);
    });
    tmRefreshPreview();
    toast(`${T('已按主色生成')} ${Object.keys(vars).length} ${T('个配色变量')}`, 'ok');
  };
  $('#tm-copy-css').onclick = () => {
    const txt = document.getElementById('tm-code').textContent;
    navigator.clipboard.writeText(txt).then(
      () => toast(T('CSS 已复制到剪贴板'), 'ok'),
      () => toast(T('复制失败，请手动选择文本'), 'err'));
  };
}

function tmSyncSwatch(key, val) {
  const sw = document.querySelector('.tm-color[data-for="' + key + '"]');
  if (sw) sw.value = tmToHex(val, sw.value);
}

async function tmSave(applyAfter) {
  const t = tmCur();
  const issues = tmLocalIssues(t);
  $('#tm-issues').innerHTML = tmIssuesHTML(issues);
  if (issues.length) {
    toast(`${T('有')} ${issues.length} ${T('处问题，未保存')}`, 'err', 6000);
    return;
  }
  const payload = Object.assign({}, t, { id: (S.themeEdit || {}).id || '' });
  const r = await api('/api/theme/op', { method: 'POST', body: { op: 'save', theme: payload } });
  if (!r.ok) {
    $('#tm-issues').innerHTML = `<div class="tm-check bad"><b>${T('后端拒绝了保存：')}</b>
      <br>${esc(r.msg_cn)}</div>`;
    toast(T('保存失败：') + r.msg_cn, 'err', 6000);
    return;
  }
  S.themeEdit = Object.assign({}, t, { id: r.data.id });
  toast(r.msg_cn, 'ok');
  await tmReload();
  if (applyAfter) await tmApply(r.data.id, false);
  tmRender();
}

async function tmExportDraft() {
  const issues = tmLocalIssues(tmCur());
  $('#tm-issues').innerHTML = tmIssuesHTML(issues);
  if (issues.length) { toast(T('请先修正问题再导出'), 'err'); return; }
  const r = await api('/api/theme/op', { method: 'POST', body: { op: 'preview', theme: tmCur() } });
  if (!r.ok) { toast(T('打包失败：') + r.msg_cn, 'err', 6000); return; }
  // 草稿未落盘，导出在浏览器侧合成 zip 内容需要主题对象，这里走一次临时保存
  const s = await api('/api/theme/op', { method: 'POST', body: { op: 'save', theme: tmCur() } });
  if (!s.ok) { toast(T('打包前保存失败：') + s.msg_cn, 'err', 6000); return; }
  S.themeEdit = Object.assign({}, tmCur(), { id: s.data.id });
  await tmDoExport(s.data.id);
  await tmReload();
}

async function tmExport() {
  const id = $('#tm-export-sel').value;
  await tmDoExport(id);
}

async function tmDoExport(id) {
  const msg = $('#tm-export-msg') || $('#tm-issues');
  const r = await api('/api/theme/op', { method: 'POST', body: { op: 'export', id } });
  if (!r.ok) {
    if (msg) { msg.className = 'tm-check bad'; msg.innerHTML = esc(r.msg_cn); }
    toast(T('导出失败：') + r.msg_cn, 'err', 6000);
    return;
  }
  tmDownloadB64(r.data.b64, r.data.filename);
  if (msg) {
    msg.className = 'tm-check ok';
    msg.innerHTML = `${T('已生成')} <span class="mono">${esc(r.data.filename)}</span>（${Math.max(1, Math.round(r.data.size / 1024))} KB${T('）， 浏览器应已开始下载；没弹窗的话请检查浏览器的下载拦截设置。')}`;
  }
  toast(T('主题包已导出'), 'ok');
}

function tmThemeCard(t, on) {
  const sw = tmSwatch(t);
  return `<div class="tm-card${on ? ' on' : ''}">
    <div class="tm-card-top" style="--sw0:${esc(sw[0])};--sw1:${esc(sw[1])};--sw2:${esc(sw[2])};--sw3:${esc(sw[3])}">
      <div class="tm-card-mock">
        <div class="tm-mk-side"><i></i><span></span><span></span></div>
        <div class="tm-mk-main"><div class="tm-mk-bar"></div><div class="tm-mk-body"></div></div>
      </div>
      ${on ? `<span class="tm-card-use">${T('使用中')}</span>` : ''}
    </div>
    <div class="tm-card-info">
      <b>${tmName(t)}</b>
      <span class="tag ${t.dark ? 'gray' : 'info'}">${t.dark ? T('深色') : T('浅色')}</span>
      ${t.builtin ? `<span class="tag gray">${T('内置')}</span>` : ''}
      <p class="desc" style="margin:4px 0 0">${esc(bt4('THEME_DESC', t.id, 'en', t.description) || t.description || T('（无描述）'))}</p>
      <p class="desc" style="margin:2px 0 0;font-size:11.5px">
        ${esc(t.author ? T('作者：') + bt4('THEME_AUTHOR', t.author, 'en', t.author) : T('未署名'))} · ${t.vars_count || 0} ${T('个变量')}
        ${t.has_css ? T(' · 含自定义 CSS') : ''}</p>
    </div>
    <div class="tm-card-acts">
      ${on ? '' : `<button class="primary small" data-tm-act="apply" data-tm-id="${esc(t.id)}">${T('应用')}</button>`}
      <button class="ghost small" data-tm-act="fork" data-tm-id="${esc(t.id)}">${T('以此为基础')}</button>
      <button class="ghost small" data-tm-act="export" data-tm-id="${esc(t.id)}">${T('导出')}</button>
      ${t.builtin ? '' : `<button class="ghost small danger" data-tm-act="del" data-tm-id="${esc(t.id)}">${T('删除')}</button>`}
    </div>
  </div>`;
}

async function tmCardAct(act, id) {
  const t = (S.theme.themes || []).find(x => x.id === id);
  if (act === 'apply') { await tmApply(id, false); tmRender(); return; }
  if (act === 'export') { await tmDoExport(id); return; }
  if (act === 'fork') {
    const r = await api('/api/theme?op=get&id=' + encodeURIComponent(id));
    if (!r.ok) { toast(T('读取主题失败：') + r.msg_cn, 'err'); return; }
    const d = r.data || {};
    S.themeEdit = {
      id: '', name: (d.name || T('我的主题')) + T(' 副本'),
      author: '', description: d.description || '', version: '1.0',
      dark: !!d.dark, vars: Object.assign({}, d.vars || {}), css: d.css || '',
    };
    S.themeTab = 'design';
    tmRender();
    toast(T('已载入到设计器，改好后保存即为新主题'), 'ok');
    return;
  }
  if (act === 'del') {
    modal(T('删除主题'), `<p>${T('确定要删除主题「')}<b>${esc(t ? t.name : id)}</b>${T('」吗？')}</p>
      <p class="desc">${T('删除后无法恢复。若你还想保留，建议先点「导出」备份。')}</p>`,
      async () => {
        const r = await api('/api/theme/op', { method: 'POST', body: { op: 'delete', id } });
        toast(r.msg_cn, r.ok ? 'ok' : 'err', 5000);
        if (r.ok) { await tmReload(); tmRender(); }
      }, T('删除'));
  }
}

/* ============================ 通用弹窗 ============================ */
function modal(title, html, onOk, okText = T('确定')) {
  const m = $('#modal');
  $('#modal-box').innerHTML = `<h3>${esc(title)}</h3><div class="modal-body">${html}</div>
    <div class="acts"><button id="md-cancel" class="ghost">${T('取消')}</button>
    ${onOk ? `<button id="md-ok" class="primary">${esc(okText)}</button>` : ''}</div>`;  m.classList.remove('hidden');
  const close = () => m.classList.add('hidden');
  $('#md-cancel').onclick = close;
  m.onclick = e => { if (e.target === m) close(); };
  if (onOk) $('#md-ok').onclick = async () => {
    const keep = await onOk();
    if (keep !== false) close();
  };
}

/* ============================ 顶栏时钟 / 农历（#12） ============================
   需求：顶栏不再显示重复的「主机名 · 内存 21% · 运行 22小时18分」，
   改为时钟 + 农历 + 年月日 + 星期。运行时间保留在概览页里。
   农历用经典的「1900-2100 压缩表」算法，纯本地计算，不依赖网络。 */

// 农历数据表（1900-2100）：每年一个 16 进制数
// 低 4 位 = 闰月月份（0 表示无闰月）；中间 12 位 = 每月大小月（1 大 30 天 / 0 小 29 天）；
// 最高位 = 闰月大小（1=30 天 / 0=29 天）。逐行 10 个，便于核对。
const lunarInfo = [
  0x04bd8, 0x04ae0, 0x0a570, 0x054d5, 0x0d260, 0x0d950, 0x16554, 0x056a0, 0x09ad0, 0x055d2,
  0x04ae0, 0x0a5b6, 0x0a4d0, 0x0d250, 0x1d255, 0x0b540, 0x0d6a0, 0x0ada2, 0x095b0, 0x14977,
  0x04970, 0x0a4b0, 0x0b4b5, 0x06a50, 0x06d40, 0x1ab54, 0x02b60, 0x09570, 0x052f2, 0x04970,
  0x06566, 0x0d4a0, 0x0ea50, 0x06e95, 0x05ad0, 0x02b60, 0x186e3, 0x092e0, 0x1c8d7, 0x0c950,
  0x0d4a0, 0x1d8a6, 0x0b550, 0x056a0, 0x1a5b4, 0x025d0, 0x092d0, 0x0d2b2, 0x0a950, 0x0b557,
  0x06ca0, 0x0b550, 0x15355, 0x04da0, 0x0a5b0, 0x14573, 0x052b0, 0x0a9a8, 0x0e950, 0x06aa0,
  0x0aea6, 0x0ab50, 0x04b60, 0x0aae4, 0x0a570, 0x05260, 0x0f263, 0x0d950, 0x05b57, 0x056a0,
  0x096d0, 0x04dd5, 0x04ad0, 0x0a4d0, 0x0d4d4, 0x0d250, 0x0d558, 0x0b540, 0x0b6a0, 0x195a6,
  0x095b0, 0x049b0, 0x0a974, 0x0a4b0, 0x0b27a, 0x06a50, 0x06d40, 0x0af46, 0x0ab60, 0x09570,
  0x04af5, 0x04970, 0x064b0, 0x074a3, 0x0ea50, 0x06b58, 0x055c0, 0x0ab60, 0x096d5, 0x092e0,
  0x0c960, 0x0d954, 0x0d4a0, 0x0da50, 0x07552, 0x056a0, 0x0abb7, 0x025d0, 0x092d0, 0x0cab5,
  0x0a950, 0x0b4a0, 0x0baa4, 0x0ad50, 0x055d9, 0x04ba0, 0x0a5b0, 0x15176, 0x052b0, 0x0a930,
  0x07954, 0x06aa0, 0x0ad50, 0x05b52, 0x04b60, 0x0a6e6, 0x0a4e0, 0x0d260, 0x0ea65, 0x0d530,
  0x05aa0, 0x076a3, 0x096d0, 0x04afb, 0x04ad0, 0x0a4d0, 0x1d0b6, 0x0d250, 0x0d520, 0x0dd45,
  0x0b5a0, 0x056d0, 0x055b2, 0x049b0, 0x0a577, 0x0a4b0, 0x0aa50, 0x1b255, 0x06d20, 0x0ada0,
  0x14b63, 0x09370, 0x049f8, 0x04970, 0x064b0, 0x168a6, 0x0ea50, 0x06b20, 0x1a6c4, 0x0aae0,
  0x0a2e0, 0x0d2e3, 0x0c960, 0x0d557, 0x0d4a0, 0x0da50, 0x05d55, 0x056a0, 0x0a6d0, 0x055d4,
  0x052d0, 0x0a9b8, 0x0a950, 0x0b4a0, 0x0b6a6, 0x0ad50, 0x055a0, 0x0aba4, 0x0a5b0, 0x052b0,
  0x0b273, 0x06930, 0x07337, 0x06aa0, 0x0ad50, 0x14b55, 0x04b60, 0x0a570, 0x054e4, 0x0d160,
  0x0e968, 0x0d520, 0x0daa0, 0x16aa6, 0x056d0, 0x04ae0, 0x0a9d4, 0x0a2d0, 0x0d150, 0x0f252,
  0x0d520,
];
const LUNAR_MONTH = () => [`${T('正')}`, `${T('二')}`, `${T('三')}`, `${T('四')}`, `${T('五')}`, `${T('六')}`, `${T('七')}`, `${T('八')}`, `${T('九')}`, `${T('十')}`, `${T('冬')}`, `${T('腊')}`];
const LUNAR_DAY_PRE = () => [`${T('初')}`, `${T('十')}`, `${T('廿')}`, `${T('卅')}`];
const LUNAR_DAY_SUF = () => ['', `${T('一')}`, `${T('二')}`, `${T('三')}`, `${T('四')}`, `${T('五')}`, `${T('六')}`, `${T('七')}`, `${T('八')}`, `${T('九')}`, `${T('十')}`];
const GAN = () => [`${T('甲')}`, `${T('乙')}`, `${T('丙')}`, `${T('丁')}`, `${T('戊')}`, `${T('己')}`, `${T('庚')}`, `${T('辛')}`, `${T('壬')}`, `${T('癸')}`];
const ZHI = () => [`${T('子')}`, `${T('丑')}`, `${T('寅')}`, `${T('卯')}`, `${T('辰')}`, `${T('巳')}`, `${T('午')}`, `${T('未')}`, `${T('申')}`, `${T('酉')}`, `${T('戌')}`, `${T('亥')}`];
const ZODIAC = () => [`${T('鼠')}`, `${T('牛')}`, `${T('虎')}`, `${T('兔')}`, `${T('龙')}`, `${T('蛇')}`, `${T('马')}`, `${T('羊')}`, `${T('猴')}`, `${T('鸡')}`, `${T('狗')}`, `${T('猪')}`];
const WEEK_CN = () => [T('星期日'), T('星期一'), T('星期二'), T('星期三'), T('星期四'), T('星期五'), T('星期六')];
// 顶栏日期在英文界面走英文格式，这两张表是固定英文缩写（不是翻译词条）
const WEEK_EN = () => ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MON_EN = () => ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const SOLAR_TERM = () => [T('小寒'), T('大寒'), T('立春'), T('雨水'), T('惊蛰'), T('春分'), T('清明'), T('谷雨'), T('立夏'),
  T('芒种'), T('夏至'), T('小暑'), T('大暑'), T('立秋'), T('处暑'), T('白露'), T('秋分'), T('寒露'), T('霜降'), T('立冬'),
  T('大雪'), T('冬至')];

function fmtDateTime(d, withTime) {
  const en = i18n.getLang() === 'en-US';
  try {
    const opt = en
      ? { year: 'numeric', month: 'short', day: 'numeric',
          ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}) }
      : { year: 'numeric', month: 'long', day: 'numeric',
          ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}) };
    return new Intl.DateTimeFormat(en ? 'en-US' : 'zh-CN', opt).format(d);
  } catch (e) {
    // Intl 不可用（精简环境）时手工拼，仍然按语言区分顺序
    const p2 = n => String(n).padStart(2, '0');
    const hm = withTime ? ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes()) : '';
    return en
      ? `${d.getMonth() + 1}/${d.getDate()}/${d.getFullYear()}${hm}`
      : `${d.getFullYear()}${T('年')}${d.getMonth() + 1}${T('月')}${d.getDate()}${T('日')}${hm}`;
  }
}


function lunarLeapMonth(y) { return lunarInfo[y - 1900] & 0xf; }
function lunarLeapDays(y) {
  return lunarLeapMonth(y) ? ((lunarInfo[y - 1900] & 0x10000) ? 30 : 29) : 0;
}
function lunarMonthDays(y, m) { return (lunarInfo[y - 1900] & (0x10000 >> m)) ? 30 : 29; }
function lunarYearDays(y) {
  let sum = 348;
  for (let i = 0x8000; i > 0x8; i >>= 1) sum += (lunarInfo[y - 1900] & i) ? 1 : 0;
  return sum + lunarLeapDays(y);
}

/* 公历 → 农历（经典压缩表算法，1900-2100，已用 2024/2025/2026 春节与中秋逐日校验） */
function solarToLunar(dt) {
  const base = new Date(1900, 0, 31);
  let offset = Math.floor((new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()) - base) / 86400000);
  let temp = 0, i;
  for (i = 1900; i < 2101 && offset > 0; i++) { temp = lunarYearDays(i); offset -= temp; }
  if (offset < 0) { offset += temp; i--; }
  const lunarYear = i;
  const leap = lunarLeapMonth(i);
  let isLeap = false;
  for (i = 1; i < 13 && offset > 0; i++) {
    if (leap > 0 && i === leap + 1 && isLeap === false) {
      --i; isLeap = true; temp = lunarLeapDays(lunarYear);
    } else {
      temp = lunarMonthDays(lunarYear, i);
    }
    if (isLeap === true && i === leap + 1) isLeap = false;
    offset -= temp;
  }
  if (offset === 0 && leap > 0 && i === leap + 1) {
    if (isLeap === true) isLeap = false; else { isLeap = true; --i; }
  }
  if (offset < 0) { offset += temp; --i; }
  return {
    lunarYear, month: i, day: offset + 1, isLeap,
    ganZhi: GAN()[(lunarYear - 4) % 10] + ZHI()[(lunarYear - 4) % 12],
    zodiac: ZODIAC()[(lunarYear - 4) % 12],
  };
}

function lunarDayCn(d) {
  if (d === 10) return T('初十');
  if (d === 20) return T('二十');
  if (d === 30) return T('三十');
  return LUNAR_DAY_PRE()[Math.floor(d / 10)] + LUNAR_DAY_SUF()[d % 10];
}

function lunarText(date) {
  try {
    const l = solarToLunar(date);
    // 初一显示月份，其余显示日
    const md = (l.day === 1)
      ? `${l.isLeap ? T('闰') : ''}${LUNAR_MONTH()[l.month - 1]}${T('月')}`
      : `${LUNAR_MONTH()[l.month - 1]}${T('月')}${lunarDayCn(l.day)}`;
    return `${l.ganZhi}${l.zodiac}${T('年')} ${md}`;
  } catch (e) { return ''; }
}

/* 节气：使用「寿星公式」（1900-2100 通用，误差 0 天，已用 2024/2025/2026 立春·冬至·夏至校验） */
const SOLAR_TERM_MIN = [0, 21208, 42467, 63836, 85337, 107014, 128867, 150921, 173149,
  195551, 218072, 240693, 263343, 285989, 308563, 331033, 353350, 375494, 397447,
  419210, 440795, 462224, 483532, 504758];

function solarTermDate(y, n) {
  // n: 0=小寒 … 23=冬至；返回该节气的公历日期
  const minutes = 525948.76 * (y - 1900) + SOLAR_TERM_MIN[n];
  const base = Date.UTC(1900, 0, 6, 2, 5, 0);
  const d = new Date(base + minutes * 60000);
  return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate() };
}

function solarTermText(date) {
  try {
    const y = date.getFullYear(), m = date.getMonth() + 1, d = date.getDate();
    const i0 = (m - 1) * 2;
    const a = solarTermDate(y, i0), b = solarTermDate(y, i0 + 1);
    if (a.y === y && a.m === m && a.d === d) return SOLAR_TERM()[i0];
    if (b.y === y && b.m === m && b.d === d) return SOLAR_TERM()[i0 + 1];
    return '';
  } catch (e) { return ''; }
}

let CLOCK_24H = localStorage.getItem('drouter_clock24') !== '0';
let CLOCK_TIMER = null;
let HEARTBEAT_TIMER = null;   // boot() 每次登录成功都会跑，心跳句柄要可清

function tickClock() {
  const now = new Date();
  const h = now.getHours(), mi = String(now.getMinutes()).padStart(2, '0'),
    s = String(now.getSeconds()).padStart(2, '0');
  let timeStr;
  if (CLOCK_24H) {
    timeStr = `${String(h).padStart(2, '0')}:${mi}:${s}`;
  } else {
    const ap = h < 12 ? T('上午') : T('下午');
    const h12 = h % 12 === 0 ? 12 : h % 12;
    timeStr = `${ap} ${h12}:${mi}:${s}`;
  }
  const t = document.getElementById('clock-time');
  const dEl = document.getElementById('clock-date');
  const lEl = document.getElementById('clock-lunar');
  if (t) t.textContent = timeStr;
  const isEn = !!(window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US');
  if (dEl) {
    if (isEn) {
      // 英文界面用「Thu, Oct 8, 2026」。节气没有通行英文说法，英文下直接不显示，
      // 免得出现「2026year10month8day Thursday」那种半中半英。
      dEl.textContent = `${WEEK_EN()[now.getDay()]}, ${MON_EN()[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;
    } else {
      const term = solarTermText(now);
      dEl.textContent = `${now.getFullYear()}${T('年')}${now.getMonth() + 1}${T('月')}${now.getDate()}${T('日')} ${WEEK_CN()[now.getDay()]}`
        + (term ? ` · ${term}` : '');
    }
  }
  // 农历同样只在中文界面显示
  if (lEl) lEl.textContent = isEn ? '' : lunarText(now);
}

function startClock() {
  if (CLOCK_TIMER) clearInterval(CLOCK_TIMER);
  tickClock();
  CLOCK_TIMER = setInterval(tickClock, 1000);
  const box = document.getElementById('clock-box');
  if (box) box.onclick = () => {
    CLOCK_24H = !CLOCK_24H;
    try { localStorage.setItem('drouter_clock24', CLOCK_24H ? '1' : '0'); } catch (e) { /* ignore */ }
    tickClock();
    toast(T('已切换为 ') + (CLOCK_24H ? T('24 小时制') : T('12 小时制')), 'ok', 1500);
  };
}

/* ============================ 顶部与守护 ============================ */
/* 左上角「构建保护 / 可生效」状态点：必须走 T()，且要在切语言时重绘
   （innerHTML 字面量，applyDom 的 data-i18n 机制扫不到静态节点之外的部分）。 */
function paintBuildBadge() {
  const v = document.getElementById('vnc-dot');
  if (!v) return;
  v.innerHTML = S.buildMode
    ? `<span class="dot off"></span>${T('构建保护模式')}`
    : `<span class="dot on"></span>${T('可生效模式')}`;
}
if (window.i18n) window.i18n.register('buildBadge', paintBuildBadge);

async function loadAll() {
  let cfg, si, svc, ifc, bm, wp;
  try {
    [cfg, si, svc, ifc, bm, wp] = await Promise.all([
      api('/api/config'), api('/api/sysinfo'), api('/api/services'),
      api('/api/ifaces'), api('/api/buildmode', { method: 'GET' }),
      api('/api/webport')]);
  } catch (e) {
    cfg = si = svc = ifc = bm = wp = { ok: false, msg_cn: T('加载失败：') + netErrCn(e && e.message) };
  }
  if (cfg.ok) S.cfg = cfg.data || {};
  if (bm && bm.ok) S.buildMode = !!((bm.data || {}).enabled);
  if (wp && wp.ok) S.webPort = (wp.data || {}).port || 8443;
  S.ifaces = ((ifc && ifc.data) || []).filter(x => !x.is_virtual && x.name !== 'lo');
  // 顶栏已改为时钟 / 农历，不再显示重复的主机名与内存占用（#12）
  paintBuildBadge();
  // 构建模式全局横幅
  const tip = $('#newiface-tip');
  if (tip) {
    if (S.buildMode) {
      tip.classList.remove('hidden');
      tip.innerHTML = `<div><b>${T('构建保护模式已开启')}</b> —— ${T('所有「应用」只会写入磁盘，不会启动服务、不会改变网络。 这样可以安全地把配置准备好，等你添加好')} WAN ${T('口并确认无误后，再一次性切换。')}</div>
        <button class="ghost small" onclick="toggleBuildMode(false)">${T('我准备好执行切换')}</button>`;
    } else {
      tip.classList.add('hidden');
    }
  }
  // 任一请求未通过鉴权，直接打回登录页，避免页面卡在「正在载入…」
  const anyUnauth = [cfg, si, svc, ifc, bm, wp].some(r => r && r.code === 'UNAUTH');
  if (anyUnauth) { logout(true); throw new Error('UNAUTH'); }
}

function initLangSwitch() {
  if (!window.i18n) return;
  const btn = document.getElementById('btn-lang');
  const lab = document.getElementById('lang-label');
  const paint = () => {
    if (!lab) return;
    const en = window.i18n.getLang() === 'en-US';
    // 按钮文字显示「**当前**是什么语言」，不是「点了会变成什么」
    lab.textContent = en ? 'EN' : `${T('中')}`;
  };
  paint();
  // 切换后：重绘导航（分组名）+ 重绘当前页（视图内文案）
  //          + 通知各异步模块自己重绘（它们的卡片是 fetch 回来才填的，
  //            go() 不会顺带重建 → 不通知就停在旧语言）
  window.i18n.onChange(() => {
    paint();
    renderNav();
    if (window.S && S.page) go(S.page);
    if (window.i18n.rerenderAll) window.i18n.rerenderAll();
  });
  if (btn) btn.onclick = () => window.i18n.toggle();
}


function boot() {
  // 语言开关必须在这里绑定：它是**全局**动作，不属于任何单个视图，
  // 所以不能放进 go()/renderNav() 的重绘路径。
  // ⚠️ 2026-10-07 修过一个真故障：initLangSwitch 定义了但**从未被调用**，
  //    导致 #btn-lang 的 onclick 永远是 null —— 点「中/EN」毫无反应。
  //    判据见_dev/t-langswitch.py（它断言「定义次数 < 出现次数」）。
  initLangSwitch();
  renderNav();
  startClock();
  // 顶栏模块健康灯（全局，跨页面常驻）
  initHealthLight();
  loadAll().then(() => go('dash')).catch(e => {
    // 鉴权失效已在 loadAll 内处理（打回登录页）；其余错误给出可见提示，避免卡在「正在载入…」
    if (e && e.message === 'UNAUTH') return;
    go('dash');
    toast(T('初始化未完全成功，部分数据可能未加载：') + netErrCn(e && e.message), 'warn');
  });
  // 轻量心跳：仅用于检测后端是否在线（顶栏不再展示内存，避免与概览重复）
  // boot() 在每次登录成功后都会执行：不先清掉上一个，重登录一次就多一个心跳
  if (HEARTBEAT_TIMER) clearInterval(HEARTBEAT_TIMER);
  HEARTBEAT_TIMER = setInterval(async () => {
    if (document.hidden) return;               // 标签页在后台不探测，省请求
    const r = await api('/api/health');
    if (!r.ok) toast(T('与后端连接中断，请检查管理服务是否在运行'), 'err', 8000);
  }, 60000);
}

/* ============================ 保存 / 应用 ============================ */

function pageModules() { return PAGE_MODULES[S.page] || null; }

function currentModule() {
  const m = pageModules();
  return m && m.save.length ? m.save[0] : null;
}

/* 端口转发：保存前把编辑中的规则同步进配置，并同时应用防火墙模块 */
/* 访问控制 / 家长时间组（#8）：不走 /api/config，用专属 /api/acl 接口 */
async function saveAcl(live) {
  const d = S.acl || {};
  const r = await api('/api/acl', { method: 'POST', body: {
    op: 'save', live: !!live, enable: !!d.enable,
    time_groups: d.time_groups || [], groups: d.groups || [], rules: d.rules || [],
  } });
  return r;
}

/* 内网文件共享 SMB/NFS（#9）：同样走专属 /api/share 接口 */
async function saveShare(live) {
  const d = sb();
  const r = await api('/api/share', { method: 'POST', body: {
    op: 'save', live: !!live, enable_smb: !!d.enable_smb, enable_nfs: !!d.enable_nfs,
    samba: d.samba || {}, nfs: d.nfs || {},
  } });
  return r;
}

/* ================== Docker / Docker Compose 面板（#10） ================== */
const DK_STATE_TAG = { running: 'ok', exited: 'gray', created: 'info', paused: 'warn',
  restarting: 'warn', removing: 'err', dead: 'err' };
const DK_STATE_CN = () => ({ running: T('运行中'), exited: T('已退出'), created: T('已创建'), paused: T('已暂停'),
  restarting: T('重启中'), removing: T('删除中'), dead: T('异常') });

/* "12.3MiB / 1GiB" → {used, total}（字节），用于画占比条 */
function dkMemParts(s) {
  const toB = t => {
    const m = /^([\d.]+)\s*([KMGT]?i?B)?$/i.exec(String(t || '').trim());
    if (!m) return 0;
    const u = (m[2] || 'B').toUpperCase().replace('I', '');
    const k = { B: 1, KB: 1024, MB: 1048576, GB: 1073741824, TB: 1099511627776 }[u] || 1;
    return parseFloat(m[1]) * k;
  };
  const p = String(s || '').split('/');
  return { used: toB(p[0]), total: toB(p[1]) };
}

function dkBar(perc, kind) {
  const n = Math.max(0, Math.min(100, parseFloat(String(perc || '0').replace('%', '')) || 0));
  return `<span class="dk-bar"><i style="width:${n}%;background:var(--${kind})"></i></span>
    <span class="mono" style="font-size:11.5px">${n.toFixed(1)}%</span>`;
}

function dkContainerRow(c) {
  const st = c.stat || {};
  const tag = DK_STATE_TAG[c.state] || 'gray';
  const ports = (c.ports || '').split(',').map(x => x.trim()).filter(Boolean);
  return `<div class="dk-trow dk-crow">
    <span>
      <b>${esc(c.name)}</b>
      <span class="tag ${tag}" style="margin-left:6px">${esc(DK_STATE_CN()[c.state] || c.state)}</span>
      <br><span class="mono" style="font-size:11px;color:var(--txt3)">${esc(c.image)} · ${esc(c.id)}</span>
      ${ports.length ? `<br><span class="mono" style="font-size:11px;color:var(--txt2)">${esc(ports.join('  '))}</span>` : ''}
    </span>
    <span class="dk-metric">${st.cpu ? dkBar(st.cpu, 'ok') : '<span class="mono">—</span>'}</span>
    <span class="dk-metric">${st.mem_perc ? dkBar(st.mem_perc, 'info') : '<span class="mono">—</span>'}
      <br><span class="mono" style="font-size:11px;color:var(--txt3)">${esc(st.mem || '')}</span></span>
    <span class="mono" style="font-size:11.5px">${esc(st.net || '—')}<br>
      <span style="color:var(--txt3)">${esc(st.block || '')}</span></span>
    <span class="dk-acts">
      ${c.state === 'running'
    ? `<button class="ghost small" data-dk="stop" data-id="${esc(c.name)}">${T('停止')}</button>
         <button class="ghost small" data-dk="restart" data-id="${esc(c.name)}">${T('重启')}</button>`
    : `<button class="ghost small" data-dk="start" data-id="${esc(c.name)}">${T('启动')}</button>`}
      <button class="ghost small" data-dk="logs" data-id="${esc(c.name)}">${T('日志')}</button>
      <button class="ghost small" data-dk="inspect" data-id="${esc(c.name)}">${T('详情')}</button>
      ${c.state !== 'running'
    ? `<button class="ghost small danger" data-dk="rm" data-id="${esc(c.name)}">${T('删除')}</button>` : ''}
    </span>
  </div>`;
}

async function viewDocker() {
  $('#view').innerHTML = `<div class="card"><h3>Docker ${T('管理面板')}</h3>
    <p class="desc">${T('正在读取')} Docker ${T('引擎状态与容器列表')}…</p></div>`;
  const r = await api('/api/docker');
  if (!r.ok) {
    $('#view').innerHTML = `<div class="card"><h3 style="color:var(--err)">${T('读取失败')}</h3>
      <p class="desc">${esc(r.msg_cn || T('无法读取 Docker 状态'))}</p></div>`;
    return;
  }
  S.docker = r.data || {};
  renderDocker();
}

function renderDocker() {
  const D = S.docker || {};
  const pk = D.pkgs || {};
  const info = D.info || {};
  const svc = D.services || {};
  const conts = D.containers || [];
  const imgs = D.images || [];
  const nets = D.networks || [];
  const vols = D.volumes || [];
  const stacks = D.stacks || [];
  const dOn = !!((pk.docker || {}).installed);
  const cOn = !!((pk.compose || {}).installed);
  const running = conts.filter(x => x.state === 'running').length;
  const svcTags = Object.entries(svc).map(([n, st]) =>
    `<span class="tag ${st === 'active' ? 'ok' : 'gray'}">${esc(n)} ${esc(st)}</span>`).join(' ');

  const installBlock = (!dOn || !cOn) ? `
    <div class="card">
      <h3>${T('安装')} Docker</h3>
      <p class="desc">${T('检测到')} Docker ${T('或 Compose')} ${T('尚未安装。执行下面的命令即可安装')} Debian 13 ${T('官方源的版本：')}</p>
      <p class="mono" style="background:var(--panel2);padding:10px;border-radius:8px">${esc(pk.install_cmd || 'apt-get install -y docker.io docker-compose')}</p>
      <p class="hint-inline">${T('安装后执行')} <span class="mono">systemctl enable --now docker</span> ${T('启动。')}drouter ${T('不会自动安装，避免影响你现有环境。')}</p>
    </div>` : '';

  $('#view').innerHTML = `
    <div class="card">
      <h3>Docker ${T('管理面板')}
        <span class="tag ${running ? 'ok' : 'gray'}" style="float:right">${T('容器')} ${running} / ${conts.length} ${T('运行中')}</span>
        <span class="tag ${dOn ? 'ok' : 'warn'}" style="float:right;margin-right:6px">${T('引擎')} ${dOn ? T('已安装') : T('未安装')}</span>
        <span class="tag ${cOn ? 'ok' : 'warn'}" style="float:right;margin-right:6px">Compose ${cOn ? T('已安装') : T('未安装')}</span></h3>
      <p class="desc">${esc(bt4('PAGE_NOTES', D.note, 'd', D.note) || '')}</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 18px">
        <div class="kv"><b>${T('服务状态')}</b><span>${svcTags || '—'}</span></div>
        <div class="kv"><b>${T('引擎版本')}</b><span class="mono">${esc(info.version || '—')}</span></div>
        <div class="kv"><b>${T('镜像')} / ${T('容器')}</b><span class="mono">${esc((info.images || '0') + T(' 镜像 · ') + (info.containers || '0') + T(' 容器'))}</span></div>
        <div class="kv"><b>${T('存储驱动')}</b><span class="mono">${esc(info.driver || '—')}</span></div>
        <div class="kv"><b>${T('数据目录')}</b><span class="mono">${esc(info.root || '—')}</span></div>
        <div class="kv"><b>Compose ${T('命令')}</b><span class="mono">${esc(D.compose_cmd || 'docker compose')}</span></div>
      </div>
      ${info.error ? `<p class="hint-inline" style="color:var(--warn)">${T('提示：')}${esc(info.error)}</p>` : ''}
      <div class="row" style="margin-top:12px">
        <button class="ghost small fixed" id="dk-refresh">${T('刷新')}</button>
        <button class="ghost small fixed" id="dk-prune-containers">${T('清理已停止容器')}</button>
        <button class="ghost small fixed" id="dk-prune-images">${T('清理悬空镜像')}</button>
        <button class="ghost small fixed" id="dk-prune-volumes">${T('清理无用数据卷')}</button>
        <button class="ghost small fixed" onclick="go('dcfg')">${T('引擎配置')} daemon.json ⚙</button>
      </div>
    </div>

    ${installBlock}

    <div class="card">
      <h3>${T('容器')}
        <span class="desc" style="float:right;margin:0">${T('每 5')} ${T('秒自动刷新资源占用')}</span></h3>
      <p class="desc">CPU / ${T('内存为')} <span class="mono">docker stats</span> ${T('的实时快照。')}</p>
      <div id="dk-conts">${conts.length ? `<div class="dk-table">
        <div class="dk-thead"><span>${T('容器')}</span><span>CPU</span><span>${T('内存')}</span><span>${T('网络')} / ${T('磁盘 IO')}</span><span>${T('操作')}</span></div>
        ${conts.map(dkContainerRow).join('')}</div>`
    : `<p class="hint-inline">${T('当前没有容器。可在下方 Compose 项目中创建，或先在 Web 终端里运行 docker run。')}</p>`}</div>
    </div>

    <div class="card">
      <h3>${T('镜像（')}${imgs.length}）</h3>
      ${imgs.length ? `
      <div class="dk-table">
        <div class="dk-thead"><span>${T('仓库:标签')}</span><span>${T('镜像 ID')}</span><span>${T('大小')}</span><span>${T('创建时间')}</span></div>
        ${imgs.map(i => `<div class="dk-trow">
          <span class="mono">${esc(i.repo)}:${esc(i.tag)}</span>
          <span class="mono">${esc((i.id || '').slice(0, 12))}</span>
          <span class="mono">${esc(i.size)}</span>
          <span class="mono" style="font-size:11.5px">${esc(String(i.created || '').replace(' +0800 CST', ''))}</span>
        </div>`).join('')}
      </div>` : `<p class="hint-inline">${T('暂无镜像。')}</p>`}
    </div>

    <div class="card">
      <h3>${T('网络与数据卷')}</h3>
      <div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0 22px">
        <div>
          <h4 style="margin:0 0 6px;font-size:13px">${T('网络（')}${nets.length}）</h4>
          ${nets.length ? nets.map(n => `<div class="kv"><b class="mono">${esc(n.name)}</b>
            <span class="mono">${esc(n.driver)}${n.scope ? ' · ' + esc(n.scope) : ''}</span></div>`).join('')
    : `<p class="hint-inline">${T('暂无网络')}</p>`}
        </div>
        <div>
          <h4 style="margin:0 0 6px;font-size:13px">${T('数据卷（')}${vols.length}）</h4>
          ${vols.length ? vols.map(v => `<div class="kv"><b class="mono">${esc(v.name)}</b>
            <span class="mono" style="font-size:11px">${esc(v.driver)}</span></div>`).join('')
    : `<p class="hint-inline">${T('暂无数据卷')}</p>`}
        </div>
      </div>
    </div>

    <div class="card">
      <h3>Compose ${T('项目（')}${stacks.length}）</h3>
      <p class="desc">${T('项目统一放在')} <span class="mono">${esc(D.stack_dir || '/etc/drouter/docker/stacks')}</span> ${T('下， 每个项目一个子目录，可一键启动')} / ${T('停止')} / ${T('重建。')}</p>
      <div id="dk-stacks">${dkStacksHTML(stacks)}</div>
      <div class="row" style="margin-top:12px">
        <label style="flex:1 1 220px">${T('项目名（小写字母')}/${T('数字')}/-/_）<input id="dk-st-name" placeholder="my-app"></label>
        <button class="ghost small fixed" id="dk-st-new">${T('新建')} / ${T('编辑')}</button>
        <button class="ghost small fixed" id="dk-st-load">${T('载入已有')}</button>
      </div>
      <label style="display:block;margin-top:10px">docker-compose.yml ${T('内容')}
        <textarea id="dk-st-yaml" rows="12" spellcheck="false"
          style="font-family:ui-monospace,Consolas,monospace;font-size:12.5px"
          placeholder="services:&#10;  web:&#10;    image: nginx:alpine&#10;    ports:&#10;      - &quot;8080:80&quot;"></textarea></label>
      <div class="row" style="margin-top:8px">
        <button class="primary small fixed" id="dk-st-save">${T('保存项目')}</button>
        <button class="ghost small fixed" id="dk-st-up">${T('启动（')}up -d）</button>
        <button class="ghost small fixed" id="dk-st-down">${T('停止（')}down）</button>
        <button class="ghost small fixed" id="dk-st-restart">${T('重启')}</button>
        <button class="ghost small fixed" id="dk-st-pull">${T('拉取镜像')}</button>
        <button class="ghost small fixed" id="dk-st-logs">${T('查看状态')}</button>
      </div>
    </div>

    <div class="card">
      <h3>docker run → docker-compose.yml ${T('转换')}</h3>
      <p class="desc">${T('把任意')} <span class="mono">docker run</span> ${T('命令粘贴进来，自动生成等价的')}
      compose ${T('文件（本地解析，不联网、不执行命令）。支持多行续行、引号、新版空格写法。')}</p>
      <label style="display:block">docker run ${T('命令')}
        <textarea id="dk-cv-in" rows="7" spellcheck="false"
          style="font-family:ui-monospace,Consolas,monospace;font-size:12.5px"
          placeholder="docker run -d --name web -p 8080:80 -v /data:/usr/share/nginx/html nginx:alpine"></textarea></label>
      <div class="row" style="margin-top:8px;align-items:flex-end">
        <label>${T('服务名（可留空自动取）')}<input id="dk-cv-name" placeholder="web"></label>
        <label>${T('版本号（留空')}=${T('现代格式）')}<input id="dk-cv-ver" placeholder="${T('留空推荐')}"></label>
        <button class="primary small fixed" id="dk-cv-go">${T('转换')}</button>
        <button class="ghost small fixed" id="dk-cv-tostack">${T('转为项目并填入上方')}</button>
      </div>
      <div id="dk-cv-msg"></div>
      <div id="dk-cv-out"></div>
    </div>

    <div class="card" id="dk-out-card" style="display:none">
      <h3 id="dk-out-title">${T('输出')}</h3>
      <pre id="dk-out" class="mono" style="max-height:460px;overflow:auto;background:var(--panel2);
        padding:12px;border-radius:8px;font-size:12px;line-height:1.6;white-space:pre-wrap"></pre>
    </div>
  `;
  dkBind();
  dkAutoRefresh();
}

function dkStacksHTML(stacks) {
  if (!stacks.length) return `<p class="hint-inline">${T('还没有项目。用下面的编辑框创建一个。')}</p>`;
  return stacks.map(s => `<div class="dk-trow">
    <span><b class="mono">${esc(s.name)}</b>
      <br><span class="mono" style="font-size:11px;color:var(--txt3)">${esc(s.file)} · ${esc(s.mtime)}</span></span>
    <span class="dk-acts">
      <button class="ghost small" data-dkst="load" data-name="${esc(s.name)}">${T('编辑')}</button>
      <button class="ghost small" data-dkst="up" data-name="${esc(s.name)}">${T('启动')}</button>
      <button class="ghost small" data-dkst="down" data-name="${esc(s.name)}">${T('停止')}</button>
      <button class="ghost small danger" data-dkst="delete" data-name="${esc(s.name)}">${T('删除')}</button>
    </span></div>`).join('');
}

let DK_TIMER = null;
function dkAutoRefresh() {
  if (DK_TIMER) clearInterval(DK_TIMER);
  DK_TIMER = setInterval(async () => {
    if (S.page !== 'docker') { clearInterval(DK_TIMER); DK_TIMER = null; return; }
    const r = await api('/api/docker');
    if (!r.ok) return;
    S.docker = r.data || {};
    // 只更新容器区，避免打断用户正在输入的文本框
    const box = $('#dk-conts');
    if (!box) return;
    const conts = (S.docker || {}).containers || [];
    box.innerHTML = conts.length ? `<div class="dk-table">
      <div class="dk-thead"><span>${T('容器')}</span><span>CPU</span><span>${T('内存')}</span><span>${T('网络')} / ${T('磁盘')} IO</span><span>${T('操作')}</span></div>
      ${conts.map(dkContainerRow).join('')}</div>`
      : `<p class="hint-inline">${T('当前没有容器。')}</p>`;
    dkBindRows();
  }, 5000);
}

function dkShow(title, text) {
  const c = $('#dk-out-card');
  if (!c) return;
  $('#dk-out-title').textContent = title;
  $('#dk-out').textContent = text || T('（无输出）');
  c.style.display = '';
  c.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function dkOp(body, okMsg) {
  const r = await api('/api/docker', { method: 'POST', body });
  toast(r.msg_cn || (r.ok ? T('完成') : T('失败')), r.ok ? 'ok' : 'err', 8000);
  return r;
}

async function dkReload() {
  const r = await api('/api/docker');
  if (r.ok) { S.docker = r.data || {}; renderDocker(); }
}

function dkBindRows() {
  $$('#dk-conts [data-dk]').forEach(b => {
    b.onclick = async () => {
      const op = b.dataset.dk, id = b.dataset.id;
      if (op === 'logs') {
        const r = await api('/api/docker', { method: 'POST', body: { op: 'logs', id, tail: 300 } });
        dkShow(T('容器日志 · ') + id, r.ok ? (r.data || {}).text : (r.msg_cn || T('读取失败')));
        toast(r.msg_cn || '', r.ok ? 'ok' : 'err'); return;
      }
      if (op === 'inspect') {
        const r = await api('/api/docker', { method: 'POST', body: { op: 'inspect', id } });
        dkShow(T('容器详情 · ') + id, r.ok ? (r.data || {}).text : (r.msg_cn || T('读取失败')));
        toast(r.msg_cn || '', r.ok ? 'ok' : 'err'); return;
      }
      if (op === 'rm') {
        modal(T('删除容器'), `<p>${T('将删除容器')} <b class="mono">${esc(id)}</b>${T('。若容器未运行才会成功， 数据卷不会被删除。')}</p><p>${T('确认继续？')}</p>`,
        async () => { await dkOp({ op: 'rm', id }, ''); dkReload(); }, T('确认删除'));
        return;
      }
      const cn = { start: T('启动'), stop: T('停止'), restart: T('重启') }[op] || op;
      if (op === 'stop') {
        modal(T('停止容器'), `<p>${T('将停止容器')} <b class="mono">${esc(id)}</b>${T('，其提供的服务会中断。')}</p><p>${T('确认继续？')}</p>`,
          async () => { await dkOp({ op, id }, ''); dkReload(); }, T('确认停止'));
        return;
      }
      const r = await dkOp({ op, id }, cn);
      if (r.ok) dkReload();
    };
  });
}

function dkBind() {
  dkBindRows();
  const on = (sel, fn) => { const e = $(sel); if (e) e.onclick = fn; };

  on('#dk-refresh', () => dkReload());
  on('#dk-prune-containers', () => modal(T('清理已停止容器'),
    `<p>${T('会删除所有已停止的容器。不会影响正在运行的容器与镜像。')}</p><p>${T('确认继续？')}</p>`,
    async () => { await dkOp({ op: 'prune', what: 'containers' }); dkReload(); }, T('清理')));
  on('#dk-prune-images', () => modal(T('清理悬空镜像'),
    `<p>${T('会删除未被任何容器引用的悬空镜像（dangling）。')}</p><p>${T('确认继续？')}</p>`,
    async () => { await dkOp({ op: 'prune', what: 'images' }); dkReload(); }, T('清理')));
  on('#dk-prune-volumes', () => modal(T('清理无用数据卷'),
    `<p>${T('会删除没有被容器使用的数据卷，')}<b>${T('其中的数据会永久丢失')}</b>。</p><p>${T('确认继续？')}</p>`,
    async () => { await dkOp({ op: 'prune', what: 'volumes' }); dkReload(); }, T('确认清理')));

  $$('#dk-stacks [data-dkst]').forEach(b => {
    b.onclick = async () => {
      const act = b.dataset.dkst, name = b.dataset.name;
      const mod = S.page;
      if (act === 'load') {
        const r = await api('/api/docker', { method: 'POST', body: { op: 'stack_get', name } });
        if (!r.ok) { toast(r.msg_cn, 'err'); return; }
        $('#dk-st-name').value = name;
        $('#dk-st-yaml').value = (r.data || {}).content || '';
        toast(T('已载入项目 ') + name, 'ok');
        return;
      }
      if (act === 'delete') {
        modal(T('删除项目'), `<p>${T('会删除项目')} <b class="mono">${esc(name)}</b> ${T('的配置文件目录。 正在运行的容器请先「停止」。')}</p><p>${T('请输入项目名以确认删除：')}</p>
          <input id="dk-del-name" placeholder="${esc(name)}" style="width:100%;margin-top:6px">`,
        async () => {
          const inp = $('#dk-del-name');
          const v = inp ? inp.value.trim() : '';
          await dkOp({ op: 'stack_delete', name, confirm: v });
          dkReload();
        }, T('确认删除'));
        return;
      }
      const r = await api('/api/docker', { method: 'POST', body: { op: 'stack_' + act, name } });
      if (r.ok) {
        dkShow(`${T('项目')} ${name} · ${act}`, (r.data || {}).text || r.msg_cn);
      }
      toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
      dkReload();
      void mod;
    };
  });

  const stackName = () => ($('#dk-st-name').value || '').trim();
  const stackYaml = () => $('#dk-st-yaml').value || '';

  on('#dk-st-new', () => { $('#dk-st-name').value = ''; $('#dk-st-yaml').value = ''; });
  on('#dk-st-save', async () => {
    const r = await dkOp({ op: 'stack_save', name: stackName(), content: stackYaml() });
    if (r.ok) dkReload();
  });
  ['up', 'down', 'restart', 'pull'].forEach(k => {
    on('#dk-st-' + k, async () => {
      const n = stackName();
      if (!n) { toast(T('请先填写项目名'), 'warn'); return; }
      const r = await api('/api/docker', { method: 'POST', body: { op: 'stack_' + k, name: n } });
      if (r.ok) dkShow(`${T('项目')} ${n} · ${k}`, (r.data || {}).text || r.msg_cn);
      toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
      dkReload();
    });
  });
  on('#dk-st-logs', async () => {
    const n = stackName();
    if (!n) { toast(T('请先填写项目名'), 'warn'); return; }
    const r = await api('/api/docker', { method: 'POST', body: { op: 'stack_ps', name: n } });
    dkShow(`${T('项目')} ${n} ${T('状态')}`, r.ok ? ((r.data || {}).text || T('（无容器）')) : r.msg_cn);
    toast(r.msg_cn, r.ok ? 'ok' : 'err');
  });

  const doConvert = async (toStack) => {
    const cmd = $('#dk-cv-in').value || '';
    const box = $('#dk-cv-msg'); const out = $('#dk-cv-out');
    if (!cmd.trim()) { toast(T('请输入 docker run 命令'), 'warn'); return null; }
    const r = await api('/api/docker', { method: 'POST', body: {
      op: 'convert', cmd, service: $('#dk-cv-name').value.trim(),
      version: $('#dk-cv-ver').value.trim() } });
    const d = r.data || {};
    if (!r.ok || !d.ok) {
      box.innerHTML = `<p class="hint-inline" style="color:var(--err)">${esc(d.msg_cn || r.msg_cn || T('转换失败'))}</p>`;
      out.innerHTML = '';
      toast(d.msg_cn || r.msg_cn || T('转换失败'), 'err');
      return null;
    }
    const warn = (d.warnings || []).length
      ? `<p class="hint-inline" style="color:var(--warn)">${T('提示：')}${d.warnings.map(esc).join('；')}</p>` : '';
    box.innerHTML = `<p class="hint-inline" style="color:var(--ok)">${esc(d.msg_cn || T('解析成功'))}
      · ${T('镜像')} <span class="mono">${esc(d.image)}</span> · ${T('服务名')} <span class="mono">${esc(d.service)}</span></p>` + warn;
    out.innerHTML = `<div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:6px">
        <button class="ghost small" id="dk-cv-copy">${T('复制')} YAML</button>
        ${toStack ? `<button class="ghost small" id="dk-cv-fill">${T('填入上方项目')}</button>` : ''}</div>
      <pre class="mono" style="max-height:360px;overflow:auto;background:var(--panel2);padding:12px;
        border-radius:8px;font-size:12px;line-height:1.6;white-space:pre-wrap">${esc(d.yaml || '')}</pre>`;
    const cp = $('#dk-cv-copy');
    if (cp) cp.onclick = () => {
      const ta = document.createElement('textarea');
      ta.value = d.yaml || '';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); toast(T('已复制到剪贴板'), 'ok'); }
      catch (e) { toast(T('复制失败，请手动选中'), 'warn'); }
      document.body.removeChild(ta);
    };
    const fl = $('#dk-cv-fill');
    if (fl) fl.onclick = () => {
      $('#dk-st-name').value = d.service || '';
      $('#dk-st-yaml').value = d.yaml || '';
      toast(T('已填入下方 Compose 项目编辑器'), 'ok');
      const el = $('#dk-st-yaml');
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    return d;
  };
  on('#dk-cv-go', () => doConvert(false));
  on('#dk-cv-tostack', () => doConvert(true));
}

/* ============ Docker 引擎配置 daemon.json（#5） ============
   页面只做三件事：把表单攒成一个 op=preview 发给后端拿真实代码、把代码显示出来、
   把代码写进 /etc/docker/daemon.json。预览一律以后端为准 —— 前端再抄一份合并
   逻辑迟早会和后端不一致，用户看到的代码和写进去的代码必须对得上。 */
let DCFG = null;
let DCFG_PV_TIMER = null;
const DCFG_BAK_KEEP_TXT = '5';

function dcfgForm(op) {
  const q = id => document.getElementById(id);
  const mirrors = [];
  $$('#dc-mirrors [data-dcm]').forEach(cb => { if (cb.checked) mirrors.push(cb.dataset.dcm); });
  return {
    op: op || 'preview',
    ipv6: q('dc-v6-on') ? !!q('dc-v6-on').checked : false,
    fixed_cidr_v6: q('dc-v6-cidr') ? q('dc-v6-cidr').value.trim() : '',
    experimental: q('dc-v6-exp') ? !!q('dc-v6-exp').checked : false,
    mirrors: mirrors,
    mirror_custom: q('dc-mirror-custom') ? q('dc-mirror-custom').value.trim() : '',
    log_rotate: q('dc-log-on') ? !!q('dc-log-on').checked : false,
    log_max_size: q('dc-log-size') ? q('dc-log-size').value.trim() : '10m',
    log_max_file: q('dc-log-file') ? Number(q('dc-log-file').value) : 3,
    live_restore: q('dc-live') ? !!q('dc-live').checked : false,
    bip: q('dc-bip') ? q('dc-bip').value.trim() : '',
  };
}

async function dcfgPreview() {
  const box = document.getElementById('dc-code-pre');
  if (!box) return;
  const r = await api('/api/dcfg', { method: 'POST', body: dcfgForm('preview') });
  if (!r.ok) { box.textContent = T('（预览失败：') + (r.msg_cn || T('未知错误')) + '）'; return; }
  const d = r.data || {};
  box.textContent = d.preview || '';
  const w = document.getElementById('dc-code-warn');
  if (w) {
    const errs = d.errs || [], warns = d.warns || [];
    w.innerHTML = errs.length
      ? `<p class="hint-inline" style="color:var(--err)">✗ ${errs.map(esc).join('；')}${T('（这样保存会被拒绝）')}</p>`
      : (warns.length
        ? `<p class="hint-inline" style="color:var(--warn)">⚠ ${warns.map(esc).join('；')}</p>` : '');
  }
}

function dcfgSchedulePreview() {
  clearTimeout(DCFG_PV_TIMER);
  DCFG_PV_TIMER = setTimeout(dcfgPreview, 250);
}

async function viewDcfg() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>Docker ${T('引擎配置（')}daemon.json）
        <button class="ghost small" id="dc-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">Docker ${T('的所有引擎级设置都写在')}
        <span class="mono">/etc/docker/daemon.json</span> ${T('里。这一页把它变成可勾选的表单：')}
        <b>IPv6 ${T('一键开关')}</b>、<b>${T('国内镜像源切换与测速')}</b>${T('，顺带把容器日志滚动、')}
        live-restore${T('、默认网桥网段也纳管进来。')}</p>
      <div id="dc-state"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>IPv6 ${T('一键开关')}</h3>
      <div id="dc-v6"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('镜像源（国内加速）')}</h3>
      <div id="dc-mirror"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('日志滚动与其他')}</h3>
      <div id="dc-misc"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('配置代码（就是将要写入的')} daemon.json）
        <button class="ghost small" id="dc-copy" style="float:right">${T('复制')}</button></h3>
      <p class="desc">${T('下面这段代码随上面的选项实时变化，和点「保存」后真正落盘的内容')}
        <b>${T('逐字节一致')}</b>${T('。你在文件里自己写的、本页不管的键（例如')}
        <span class="mono">data-root</span>${T('）会原样保留在最后。')}</p>
      <div id="dc-code-warn"></div>
      <pre class="mono" id="dc-code-pre" style="max-height:420px;overflow:auto;background:var(--panel2);
        padding:12px;border-radius:8px;font-size:12px;line-height:1.6;white-space:pre-wrap">${T('（读取中')}…）</pre>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="dc-save">${T('保存到')} daemon.json</button>
        <button class="ghost" id="dc-save-restart">${T('保存并重启')} Docker</button>
      </div>
      <div id="dc-bak" style="margin-top:12px"></div>
      <div id="dc-out" class="hidden" style="margin-top:12px"></div>
    </div>`;
  $('#dc-reload').onclick = dcfgLoad;
  $('#dc-save').onclick = () => dcfgSave(false);
  $('#dc-save-restart').onclick = () => dcfgSave(true);
  $('#dc-copy').onclick = () => {
    const t = ($('#dc-code-pre') || {}).textContent || '';
    const ta = document.createElement('textarea');
    ta.value = t; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast(T('已复制到剪贴板'), 'ok'); }
    catch (e) { toast(T('复制失败，请手动选中'), 'warn'); }
    document.body.removeChild(ta);
  };
  dcfgLoad();
}

async function dcfgLoad() {
  const r = await api('/api/dcfg', { method: 'POST', body: { op: 'status' } });
  if (!r.ok) {
    $('#dc-state').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  DCFG = r.data;
  dcfgRender();
  dcfgPreview();
}

function dcfgRender() {
  const d = DCFG || {};
  const c = d.cfg || {};
  const inst = !!d.installed;
  const on = d.active === 'active';

  // ---------- 状态 ----------
  $('#dc-state').innerHTML = `
    ${inst ? '' : `<div class="notice warn" style="margin-bottom:10px">
      ${T('这台机器上')}<b>${T('还没有安装 Docker')}</b>（<span class="mono">docker</span> ${T('命令不存在）。 这不影响你在这里预先把配置写好')} —— ${T('文件会照常保存到')}
      <span class="mono">/etc/docker/daemon.json</span>${T('，将来装好 Docker 一启动就直接生效。 要现在装，请去「系统')} → ${T('依赖自检与安装」或直接')}
      <span class="mono">apt-get install -y docker.io docker-compose</span>。</div>`}
    ${d.parse_error ? `<div class="notice err" style="margin-bottom:10px">
      ${T('现有的 daemon.json 无法解析：')}${esc(d.parse_error)}<br>
      ${T('为安全起见本页')}<b>${T('不会覆盖它')}</b>${T('，请先手工修正或把文件挪走。')}</div>` : ''}
    ${(d.unmanaged || []).length ? `<div class="notice warn" style="margin-bottom:10px">
      ${T('现有文件里有')} <b>${(d.unmanaged || []).length}</b> ${T('个本页不管的键：')}
      <span class="mono">${(d.unmanaged || []).map(esc).join('、')}</span>${T('。 保存时它们会')}<b>${T('原样保留')}</b>${T('，不会被清掉。')}</div>` : ''}
    ${d.build_mode ? `<div class="notice warn" style="margin-bottom:10px">${T('当前处于')}<b>${T('构建保护模式')}</b>${T('：可以写配置文件，但不会重启 Docker。')}</div>` : ''}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 18px">
      <div class="kv"><b>Docker ${T('引擎')}</b><span>
        <span class="tag ${inst ? 'ok' : 'warn'}">${inst ? T('已安装') : T('未安装')}</span>
        <span class="tag ${on ? 'ok' : 'gray'}" style="margin-left:6px">${on ? T('运行中') : esc(d.active || '—')}</span></span></div>
      <div class="kv"><b>${T('版本')}</b><span class="mono">${esc(d.version || '—')}</span></div>
      <div class="kv"><b>${T('开机自启')}</b><span class="mono">${esc(d.enabled || '—')}</span></div>
      <div class="kv"><b>${T('配置文件')}</b><span class="mono">${esc(d.file || '—')}</span></div>
      <div class="kv"><b>${T('文件是否存在')}</b><span class="mono">${d.exists ? T('存在') : T('不存在（保存时创建）')}</span></div>
    </div>`;

  // ---------- IPv6 ----------
  const v6on = !!c.ipv6;
  $('#dc-v6').innerHTML = `
    <div class="dep-item">
      <span class="dep-ico" style="background:var(--info)">6</span>
      <div class="dep-body" style="flex:1 1 200px">
        <b>${T('让容器网络支持')} IPv6</b>
        <div class="desc" style="margin:6px 0 0">${T('打开后')} Docker ${T('会给默认网桥分配一个')} IPv6 ${T('网段， 容器就能拿到')} v6 ${T('地址、也能用')} v6 ${T('互相访问。默认')}<b>${T('关闭')}</b> —— ${T('因为开了不等于能上')} v6 ${T('外网， 还得上游路由器配合。')}</div>
        <div style="margin:8px 0 0;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <label class="switch"><input type="checkbox" id="dc-v6-on"${v6on ? ' checked' : ''}><i></i></label>
          <span class="tag ${v6on ? 'ok' : 'gray'}">${v6on ? T('开启') : T('关闭')}</span>
        </div>
        <div class="row" style="margin-top:10px">
          <label style="flex:1 1 260px">${T('容器')} IPv6 ${T('网段（')}fixed-cidr-v6）
            <input id="dc-v6-cidr" value="${esc(c.fixed_cidr_v6 || '')}"
              placeholder="${T('留空＝保存时自动生成一个')} fd00::/8 ${T('私有段')}"></label>
          <button class="ghost small" id="dc-v6-ula">${T('随机生成一个私有段')}</button>
        </div>
        <div style="margin-top:8px">
          <label class="switch"><input type="checkbox" id="dc-v6-exp"${c.experimental ? ' checked' : ''}><i></i>
            <span>${T('同时写入')} <span class="mono">"experimental": true</span></span></label>
        </div>
        <details style="margin-top:8px">
          <summary class="desc" style="cursor:pointer;color:var(--pri)">${T('打开')}/${T('关闭会带来什么')}</summary>
          <ul class="desc" style="margin:6px 0 0;padding-left:20px;line-height:1.85">
            <li>${T('会同时写入')} <span class="mono">ip6tables: true</span> —— ${T('没有它')} Docker ${T('不会为容器 生成')} IPv6 ${T('的防火墙')}/NAT ${T('规则，等于白开')}</li>
            <li>${T('这里默认用')} <span class="mono">fd00::/8</span> ${T('私有段（')}ULA${T('）。 容器拿到私有')} v6 ${T('后，')}<b>${T('能互通但上不了')} v6 ${T('外网')}</b>${T('，除非你在防火墙')} IPv6 ${T('页 做了')} NAT66 ${T('或上游下发了前缀委派并配好路由')}</li>
            <li>${T('用公网段（')}2408: / 2001: ${T('这类）时，还需要上游路由器做')} NDP ${T('代理， 否则包出得去回不来，家用环境多半搞不定')}</li>
            <li><span class="mono">experimental</span> ${T('只有')} Docker 24 / 25 ${T('需要它来解锁')}
              ip6tables${T('；26 起 ip6tables')} ${T('已转正。装的是')} 26 ${T('及以上就别勾， 个别版本见到这一项会直接拒绝启动')}</li>
            <li>${T('改完必须')}<b>${T('重启')} Docker</b> ${T('才生效，而且已有的容器要重建才会拿到')} v6 ${T('地址')}</li>
          </ul>
        </details>
      </div>
    </div>`;

  // ---------- 镜像源 ----------
  const sel = c.mirrors || [];
  $('#dc-mirror').innerHTML = `
    <p class="desc">${T('镜像源就是「')}Docker Hub ${T('的国内代理」：拉镜像时先问它要，能省下大量等待。 可以同时勾多个')} —— Docker ${T('会')}<b>${T('按顺序')}</b>${T('试，前一个失败就换下一个。')}</p>
    <div id="dc-mirrors" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:2px 18px">
      ${(d.mirrors || []).map(m => {
        const isOn = sel.indexOf(m.key) >= 0;
        const ph = (m.url || '').indexOf('<') >= 0;
        return `<label class="switch" style="padding:6px 0">
          <input type="checkbox" data-dcm="${esc(m.key)}"${isOn && !ph ? ' checked' : ''}${ph ? ' disabled' : ''}><i></i>
          <span>${bt4('DCFG_MIRRORS', m.key, 'n', m.name)}${ph ? T('（占位，请用下面的自定义源）') : ''}</span></label>
          <div class="desc" style="margin:-2px 0 6px;font-size:12px">${esc(bt4('DCFG_MIRRORS', m.key, 'd', m.note) || '')}</div>`;
      }).join('')}
    </div>
    <div class="row" style="margin-top:10px">
      <label style="flex:1 1 320px">${T('自定义镜像源')}
        <input id="dc-mirror-custom" value="${esc(c.mirror_custom || '')}"
          placeholder="https://xxxx.mirror.aliyuncs.com"></label>
      <button class="ghost small" id="dc-mt">${T('一键测速')}</button>
    </div>
    <p class="desc">${T('测速从')}<b>${T('这台路由器自己')}</b>${T('发起，反映的是')} Docker ${T('拉镜像时的真实链路 （已加')} <span class="mono">--noproxy</span>${T('，不会被环境里的代理掩盖）。')}
      HTTP <span class="mono">200</span> ${T('或')} <span class="mono">401</span> ${T('都表示通')} ——
      Registry ${T('的探针本来就要求带凭证，回')} 401 ${T('是正常应答。')}</p>
    <div id="dc-mt-out" style="margin-top:8px"></div>`;

  // ---------- 其他 ----------
  $('#dc-misc').innerHTML = `
    <div class="dep-item">
      <span class="dep-ico" style="background:var(--info)">L</span>
      <div class="dep-body" style="flex:1 1 200px">
        <b>${T('容器日志滚动（强烈建议开）')}</b>
        <div class="desc" style="margin:6px 0 0">${T('限制每个容器日志文件的大小和份数。 不开的话，一个死循环打印日志的容器能在几小时内把小硬盘写满')} ——
          ${T('这是「路由器磁盘莫名爆满」最常见的原因。')}</div>
        <div style="margin:8px 0 0;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <label class="switch"><input type="checkbox" id="dc-log-on"${c.log_rotate === false ? '' : ' checked'}><i></i></label>
          <label style="flex:0 0 130px">${T('单文件上限')}<input id="dc-log-size" value="${esc(c.log_max_size || '10m')}" placeholder="10m"></label>
          <label style="flex:0 0 110px">${T('保留份数')}<input id="dc-log-file" type="number" min="1" max="20" value="${esc(Number(c.log_max_file || 3))}"></label>
        </div>
      </div>
    </div>
    <div class="dep-item" style="margin-top:10px">
      <span class="dep-ico" style="background:var(--info)">R</span>
      <div class="dep-body" style="flex:1 1 200px">
        <b>live-restore${T('（重启')} dockerd ${T('时保住容器）')}</b>
        <div class="desc" style="margin:6px 0 0">${T('开启后，重启')} / ${T('升级')} Docker ${T('守护进程时')}
          <b>${T('正在运行的容器不会被一起杀掉')}</b>${T('。对一台跑着常驻服务的路由器来说很值。')}</div>
        <div style="margin:8px 0 0">
          <label class="switch"><input type="checkbox" id="dc-live"${c.live_restore === false ? '' : ' checked'}><i></i></label>
        </div>
      </div>
    </div>
    <div class="dep-item" style="margin-top:10px">
      <span class="dep-ico" style="background:var(--info)">B</span>
      <div class="dep-body" style="flex:1 1 200px">
        <b>${T('默认网桥网段（')}bip）</b>
        <div class="desc" style="margin:6px 0 0">Docker ${T('默认用')} <span class="mono">172.17.0.0/16</span>${T('。 如果你的局域网或')} VPN ${T('正好也是这个段，容器网络就会和它打架， 表现为「装了')} Docker ${T('之后某些内网地址访问不了」。留空表示不动，用')} Docker ${T('默认值。')}</div>
        <div style="margin:8px 0 0">
          <label style="flex:1 1 260px"><input id="dc-bip" value="${esc(c.bip || '')}" placeholder="${T('留空＝用')} Docker ${T('默认')} 172.17.0.1/16"></label>
        </div>
      </div>
    </div>`;

  // ---------- 备份还原 ----------
  const bks = d.backups || [];
  $('#dc-bak').innerHTML = `
    <div class="row" style="align-items:center">
      <span class="desc" style="margin:0">${T('每次保存都会自动备份旧文件（保留最近')} ${DCFG_BAK_KEEP_TXT} ${T('份， 目录')} <span class="mono">${esc(d.bak_dir || '')}</span>）</span>
      ${bks.length ? `<select id="dc-bak-sel" style="width:auto">
        ${bks.map(b => `<option value="${esc(b.file)}">${esc(b.mtime)} · ${esc(fmtBytes(b.size))}</option>`).join('')}
      </select>
      <button class="ghost small" id="dc-bak-go">${T('还原这份备份')}</button>` : `<span class="desc">${T('（暂无备份）')}</span>`}
    </div>`;

  dcfgBind();
  const out = $('#dc-out');
  if (out) { out.classList.add('hidden'); out.innerHTML = ''; }
}

function dcfgBind() {
  ['dc-v6-on', 'dc-v6-exp', 'dc-log-on', 'dc-live'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', dcfgSchedulePreview);
  });
  ['dc-v6-cidr', 'dc-mirror-custom', 'dc-log-size', 'dc-log-file', 'dc-bip'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', dcfgSchedulePreview);
  });
  $$('#dc-mirrors [data-dcm]').forEach(cb => {
    cb.addEventListener('change', dcfgSchedulePreview);
  });
  const ula = document.getElementById('dc-v6-ula');
  if (ula) ula.onclick = async () => {
    const r = await api('/api/dcfg', { method: 'POST', body: { op: 'ula' } });
    if (!r.ok) { toast(r.msg_cn || T('生成失败'), 'err'); return; }
    const el = document.getElementById('dc-v6-cidr');
    if (el) { el.value = (r.data || {}).ula || ''; dcfgPreview(); }
  };
  const mt = document.getElementById('dc-mt');
  if (mt) mt.onclick = async () => {
    const box = $('#dc-mt-out');
    box.innerHTML = `<p class="desc">${T('正在逐个探测，最多约一分钟…')}</p>`;
    const body = dcfgForm('mirror_test');
    const r = await api('/api/dcfg', { method: 'POST', body });
    if (!r.ok) { box.innerHTML = `<p class="hint-inline" style="color:var(--err)">${esc(r.msg_cn)}</p>`; return; }
    const rows = ((r.data || {}).results) || [];
    box.innerHTML = `<div class="dk-table">
      <div class="dk-thead"><span>${T('镜像源')}</span><span>${T('结果')}</span><span>${T('耗时')}</span></div>
      ${rows.map(t => `<div class="dk-trow">
        <span><b>${esc(bt4('SHARE_DIR_TEMPLATES', t.id, 'name', t.name))}</b><br><span class="mono" style="font-size:11px;color:var(--txt3)">${esc(t.url)}</span></span>
        <span class="tag ${t.ok ? 'ok' : (t.skipped ? 'gray' : 'err')}">${esc(t.reason || (t.ok ? T('可用') : T('不可用')))}
          ${t.code ? ' · ' + esc(t.code) : ''}</span>
        <span class="mono" style="font-size:11.5px">${t.ms ? esc(t.ms + ' ms') : '—'}</span>
      </div>`).join('')}</div>
      <p class="desc">${T('先把最快的那个勾上（可以多选几个当备用），再回上面点保存。')}</p>`;
  };
  const bg = document.getElementById('dc-bak-go');
  if (bg) bg.onclick = () => {
    const sel = document.getElementById('dc-bak-sel');
    const f = sel ? sel.value : '';
    if (!f) { toast(T('请先选择一份备份'), 'warn'); return; }
    modal(T('还原 daemon.json'), `<p>${T('会用备份')} <span class="mono">${esc(f)}</span> ${T('覆盖当前的')}
      <span class="mono">/etc/docker/daemon.json</span>。</p>
      <p>${T('当前文件会先另存一份新备份，不会丢。')}</p><p>${T('确认继续？')}</p>`,
    async () => {
      const r = await api('/api/dcfg', { method: 'POST', body: { op: 'restore', file: f } });
      toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
      if (r.ok) dcfgLoad();
    }, T('确认还原'));
  };
}

async function dcfgSave(thenRestart) {
  const r = await api('/api/dcfg', { method: 'POST', body: dcfgForm('save') });
  const box = $('#dc-out');
  box.classList.remove('hidden');
  box.innerHTML = `<div class="notice ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</div>`;
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
  if (!r.ok) return;
  const d = r.data || {};
  if (d.content) {
    const pre = $('#dc-code-pre');
    if (pre) pre.textContent = d.content;      // 用服务端真实写入的内容校准预览
  }
  dcfgLoad();
  if (thenRestart) dcfgAskRestart();
}

function dcfgAskRestart() {
  const d = DCFG || {};
  if (!d.installed) {
    toast(T('这台机器上没有 Docker，无需重启。配置已保存，装好 Docker 后自动生效'), 'warn', 8000);
    return;
  }
  if (d.build_mode) {
    toast(T('当前处于构建保护模式：配置已写入 /etc/docker/daemon.json，')
      + T('但不会重启 Docker（避免影响正在运行的局域网与 RealVNC 会话）'), 'warn', 9000);
    return;
  }
  modal(T('重启 Docker 使配置生效'), `<p>${T('重启会')}<b>${T('中断所有正在运行的容器')}</b>${T('（通常几十秒内恢复）， 并且会重建')} <span class="mono">docker0</span> ${T('网桥、重新插入')} iptables / nftables ${T('链。')}</p>
    <p>${T('对一台正在跑')} easytier / cloudflared / AdGuardHome ${T('这类常驻服务的路由器来说， 这意味着相关服务会有一次短暂中断。')}</p>
    <p>${T('如果容器没有设')} <span class="mono">restart: unless-stopped</span>${T('， 重启后需要你手工拉起来。')}</p><p>${T('确认继续？')}</p>`,
  async () => {
    const r = await api('/api/dcfg', { method: 'POST', body: { op: 'restart', confirm: true } });
    const box = $('#dc-out');
    box.classList.remove('hidden');
    box.innerHTML = `<div class="notice ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</div>`;
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
    if (r.ok) dcfgLoad();
  }, T('确认重启 Docker'));
}

/* ============ 打印服务 CUPS / USB RAW 直通（#7） ============
   两种模式抢的是同一个 USB 打印机，所以必须互斥。页面把「当前实际状态」和
   「推荐值」分开显示：默认值只是建议，改不改由用户点保存决定。 */
let PRINT = null;

async function viewPrint() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('打印服务')}
        <button class="ghost small" id="pt-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">${T('把这台路由器变成打印服务器，让局域网里的电脑、手机直接打。 两种模式')}<b>${T('互斥二选一')}</b> —— ${T('它们抢的是同一个')} USB ${T('打印机。')}</p>
      <div id="pt-state"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('工作模式')}</h3>
      <div id="pt-mode"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>CUPS ${T('模式设置')}</h3>
      <div id="pt-cups"><p class="desc">${T('正在读取')}…</p></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="pt-save">${T('保存并生效')}</button>
      </div>
      <div id="pt-out" class="hidden" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h3>${T('打印队列')}</h3>
      <div id="pt-queues"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('添加队列')}</h3>
      <div id="pt-add"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>USB ${T('打印机与可接入设备')}</h3>
      <div id="pt-usb"><p class="desc">${T('正在读取')}…</p></div>
    </div>`;
  $('#pt-reload').onclick = printLoad;
  $('#pt-save').onclick = printSave;
  printLoad();
}

async function printLoad() {
  const r = await api('/api/print', { method: 'POST', body: { op: 'status' } });
  if (!r.ok) {
    $('#pt-state').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  PRINT = r.data;
  printRender();
}

function printSvcTag(s) {
  const on = (s || {}).active === 'active';
  return `<span class="tag ${on ? 'ok' : 'gray'}">${on ? T('运行中') : T('已停止')}</span>
    <span class="mono" style="font-size:11px;color:var(--txt3)">${esc((s || {}).enabled || '')}</span>`;
}

function printRender() {
  const d = PRINT || {};
  const cfg = d.cfg || {};
  const c = cfg.cups || {};
  const raw = cfg.raw || {};
  const mode = cfg.mode || 'cups';
  const inst = !!d.installed;

  // ---------- 状态 ----------
  $('#pt-state').innerHTML = `
    ${inst ? '' : `<div class="notice warn" style="margin-bottom:10px">
      ${T('print.notInstPre')} →
      <span class="mono">apt-get install -y ${esc((d.need_install || []).join(' '))}</span>${T('print.notInstPost')}</div>`}
    ${d.build_mode ? `<div class="notice warn" style="margin-bottom:10px">${T('print.buildModePre')}<b>${T('print.buildModeB')}</b>${T('print.buildModePost')}</div>` : ''}
    ${inst && (d.listen || '').indexOf('127.0.0.1') >= 0 && mode === 'cups'
    ? `<div class="notice warn" style="margin-bottom:10px">
        CUPS ${T('print.localhostPre')}<b>${T('print.localhostB')}</b>${T('print.localhostPost')}</div>` : ''}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 18px">
      <div class="kv"><b>CUPS</b><span>${inst ? printSvcTag(d.cups) : `<span class="tag warn">${T('print.notInstalled')}</span>`}</span></div>
      <div class="kv"><b>cups-browsed</b><span>${printSvcTag(d.browsed)}</span></div>
      <div class="kv"><b>${T('print.rawTitle')}</b><span>${printSvcTag(d.raw)}</span></div>
      <div class="kv"><b>${T('print.cupsVersion')}</b><span class="mono">${esc(d.cups_version || '—')}</span></div>
      <div class="kv"><b>${T('print.listenReal')}</b><span class="mono">${esc(d.listen || T('print.notListening'))}</span></div>
      <div class="kv"><b>${T('print.avahiLabel')}</b><span>
        <span class="tag ${d.avahi_active ? 'ok' : 'warn'}">${d.avahi_active ? T('print.running') : T('print.notRunning')}</span></span></div>
      <div class="kv"><b>${T('print.adminPage')}</b><span class="mono">${esc(d.cups_url || '')}</span></div>
    </div>`;

  // ---------- 模式 ----------
  const modes = [
    { k: 'cups', n: T('print.modeCups'), d: T('print.modeCupsD') },
    { k: 'raw', n: T('print.modeRaw'), d: T('print.modeRawD') },
    { k: 'off', n: T('print.modeOff'), d: T('print.modeOffD') },
  ];
  $('#pt-mode').innerHTML = `
    <div class="dep-grid">
      ${modes.map(m => `<label class="dep-item" style="cursor:pointer">
        <span class="dep-ico" style="background:${mode === m.k ? 'var(--ok)' : 'var(--txt3)'}">
          ${mode === m.k ? '✓' : '·'}</span>
        <div class="dep-body" style="flex:1 1 200px">
          <b>${esc(m.n)}</b>
          <div class="desc" style="margin:4px 0 0">${esc(m.d)}</div>
          <div style="margin:6px 0 0">
            <input type="radio" name="pt-mode" value="${esc(m.k)}"${mode === m.k ? ' checked' : ''}>
            <span class="desc" style="margin:0">${mode === m.k ? T('print.isCurrent') : T('print.switchTo')}</span>
          </div>
        </div></label>`).join('')}
    </div>
    ${(d.usb_printers || []).length === 0 && mode === 'raw'
    ? `<div class="notice warn" style="margin-top:10px">${T('print.noUsbPre')}<b>${T('print.noUsbB')}</b>${T('print.noUsbPost')}</div>` : ''}
    <details style="margin-top:10px">
      <summary class="desc" style="cursor:pointer;color:var(--pri)">${T('print.diffTitle')}</summary>
      <ul class="desc" style="margin:6px 0 0;padding-left:20px;line-height:1.85">
        <li><b>${T('print.diffCupsPre')}</b>${T('print.diffCupsB')}<b>${T('print.diffCupsB2')}</b>${T('print.diffCupsPost')}</li>
        <li><b>${T('print.diffRawPre')}</b>${T('print.diffRawB')}<b>${T('print.diffRawB2')}</b>${T('print.diffRawPost')}</li>
        <li>${T('print.diffBothPre')}<b>${T('print.diffBothB')}</b>${T('print.diffBothPost')}<span class="mono">/dev/usb/lp0</span>${T('print.diffBothPost2')}</li>
        <li>${T('print.diffSwitch')}</li>
      </ul>
    </details>`;

  // ---------- CUPS 设置 ----------
  const sw = (id, on) =>
    `<label class="switch"><input type="checkbox" id="${id}"${on ? ' checked' : ''}><i></i></label>`;
  const items = {};
  (d.items || []).forEach(it => { items[it.key] = it; });
  const card = (key, ctrl) => {
    const it = items[key] || { name: key, why: '', impact: [] };
    return `<div class="dep-item">
      <span class="dep-ico" style="background:var(--info)">i</span>
      <div class="dep-body" style="flex:1 1 200px">
        <b>${bt4('PRINT_ITEMS', key, 'name', it.name)}</b>
        <div class="desc" style="margin:6px 0 0">${bt4('PRINT_ITEMS', key, 'why', it.why)}</div>
        <div style="margin:8px 0 0">${ctrl}</div>
        ${(it.impact || []).length ? `<details style="margin-top:8px">
          <summary class="desc" style="cursor:pointer;color:var(--pri)">${T('print.impactTitle')}</summary>
          <ul class="desc" style="margin:6px 0 0;padding-left:20px;line-height:1.85">
            ${it.impact.map(x => `<li>${esc(bt4('PRINT_IMPACT', x, 'en', x))}</li>`).join('')}</ul></details>` : ''}
      </div></div>`;
  };
  $('#pt-cups').innerHTML = `
    <div class="dep-grid">
      ${card('listen', `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <select id="pt-listen" style="width:auto">
          <option value="lan"${c.listen === 'lan' ? ' selected' : ''}>${T('print.optLan')}</option>
          <option value="local"${c.listen === 'local' ? ' selected' : ''}>${T('print.optLocal')}</option>
        </select>
        <span class="tag ${c.listen === 'lan' ? 'ok' : 'gray'}">${
          c.listen === 'lan' ? T('print.openToLan') : T('print.lanOnly')}</span></div>`)}
      ${card('share', `<div style="display:flex;align-items:center;gap:10px">
        ${sw('pt-share', !!c.share)}
        <span class="desc" style="margin:0">${T('print.shareHint')}</span></div>`)}
      ${card('web_iface', `<div style="display:flex;align-items:center;gap:10px">
        ${sw('pt-web', !!c.web_iface)}</div>`)}
      ${card('remote_admin', `<div style="display:flex;align-items:center;gap:10px">
        ${sw('pt-radmin', !!c.remote_admin)}
        <span class="tag ${c.remote_admin ? 'warn' : 'gray'}">${c.remote_admin ? T('print.opened') : T('print.offByDefault')}</span></div>`)}
      ${card('browsed', `<div style="display:flex;align-items:center;gap:10px">
        ${sw('pt-browsed', !!c.browsed)}</div>`)}
    </div>
    <div class="row" style="margin-top:12px">
      <label style="flex:0 0 220px">${T('print.rawDev')}
        <input id="pt-raw-dev" value="${esc(raw.device || '/dev/usb/lp0')}" placeholder="/dev/usb/lp0"></label>
      <label style="flex:0 0 130px">${T('print.listenPort')}
        <input id="pt-raw-port" type="number" min="1" max="65535" value="${esc(Number(raw.port || 9100))}"></label>
      <label style="flex:0 0 170px">${T('print.bindAddr')}
        <input id="pt-raw-bind" value="${esc(raw.bind || '0.0.0.0')}" placeholder="0.0.0.0"></label>
    </div>
    <p class="desc">${T('print.socatsPre')}<b>${T('print.socatsB')}</b>${T('print.socatsPost')}<span class="mono">socat</span>${T('print.socatsPost2')}</p>
    ${d.socat ? '' : `<div class="notice warn">${T('print.noSocat')}<b>socat</b>${T('print.noSocatB')}</div>`}`;

  // ---------- 队列 ----------
  const qs = d.queues || [];
  $('#pt-queues').innerHTML = qs.length ? `<div class="dk-table">
      <div class="dk-thead"><span>${T('print.thQueue')}</span><span>${T('print.newUri')}</span><span>${T('print.thState')}</span><span>${T('print.thActs')}</span></div>
      ${qs.map(q => `<div class="dk-trow">
        <span><b>${esc(q.name)}</b>
          ${q.default ? `<span class="tag ok" style="margin-left:6px">${T('print.isDefault')}</span>` : ''}
          ${q.not_saved_yet
          ? `<span class="tag warn" style="margin-left:6px" title="${T('print.notSavedTitle')}">${T('print.notSaved')}</span>` : ''}
          <br><span class="desc" style="margin:0;font-size:12px">${esc(q.info || q.make_model || '')}</span></span>
        <span class="mono" style="font-size:11px">${esc(q.uri || '—')}</span>
        <span>
          <span class="tag ${q.shared ? 'ok' : 'warn'}">${q.shared ? T('print.shared') : T('print.notShared')}</span>
          <span class="tag ${q.accepting ? 'ok' : 'gray'}">${q.accepting ? T('print.accepting') : T('print.paused')}</span>
          <br><span class="desc" style="margin:0;font-size:12px">${esc(q.state || '')}</span></span>
        <span class="dk-acts">
          <button class="ghost small" data-ptq="share" data-n="${esc(q.name)}" data-v="${q.shared ? '0' : '1'}">
            ${q.shared ? T('print.unshare') : T('print.doShare')}</button>
          <button class="ghost small" data-ptq="default" data-n="${esc(q.name)}">${T('print.setDefault')}</button>
          <button class="ghost small" data-ptq="test" data-n="${esc(q.name)}">${T('print.testPage')}</button>
          <button class="ghost small" data-ptq="${q.accepting ? 'reject' : 'accept'}" data-n="${esc(q.name)}">
            ${q.accepting ? T('print.doPause') : T('print.doResume')}</button>
          <button class="ghost small danger" data-ptq="del" data-n="${esc(q.name)}">${T('print.doDelete')}</button>
        </span></div>`).join('')}</div>
      <div class="row" style="margin-top:10px">
        <button class="ghost small" id="pt-jobs">${T('print.viewJobs')}</button>
      </div>
      <div id="pt-jobs-out"></div>`
    : `<p class="hint-inline">${T('print.noQueuePre')}`
      + T('print.noQueuePost') + '</p>';

  // ---------- 添加队列 ----------
  const devs = (d.devices || []).filter(x => x.uri && !x.uri.startsWith('beh'));
  $('#pt-add').innerHTML = `
    <div class="row">
      <label style="flex:0 0 220px">${T('print.newName')}
        <input id="pt-new-name" placeholder="HP-LaserJet"></label>
      <label style="flex:1 1 320px">${T('print.newUri')}
        <input id="pt-new-uri" list="pt-uri-list" placeholder="ipp://192.168.7.231/ipp/print">
        <datalist id="pt-uri-list">
          ${devs.map(x => `<option value="${esc(x.uri)}">${esc(x.kind)} · ${esc(x.uri)}</option>`).join('')}
        </datalist></label>
    </div>
    <div class="row" style="margin-top:10px">
      <label style="flex:1 1 320px">${T('print.newDriver')}
        <select id="pt-new-drv">
          ${(d.drivers || []).map(x => `<option value="${esc(x.key)}">${bt4('PRINT_DRIVERS', x.key, 'name', x.name)}</option>`).join('')}
        </select></label>
      <label style="flex:1 1 200px">${T('print.newInfo')}<input id="pt-new-info" placeholder="${T('print.phInfo')}"></label>
      <label style="flex:1 1 200px">${T('print.newLoc')}<input id="pt-new-loc" placeholder="${T('print.phLoc')}"></label>
    </div>
    <p class="desc">${(d.drivers || []).map(x => bt4('PRINT_DRIVERS', x.key, 'note', x.note)).join(' ') || ''}</p>
    <div class="row" style="margin-top:10px">
      <button class="primary" id="pt-add-go">${T('print.addQueue')}</button>
    </div>`;

  // ---------- USB 与设备 ----------
  const usbs = d.usb_printers || [];
  $('#pt-usb').innerHTML = `
    ${usbs.length ? `<p class="hint-inline" style="color:var(--ok)">${T('print.usbFound', usbs.length)}</p>
      ${usbs.map(u => `<div class="kv"><b>${esc(u.name)}</b>
        <span class="mono">${esc(u.node || '')} ${u.vid ? '· ' + esc(u.vid + ':' + u.pid) : ''}</span></div>`).join('')}`
    : `<div class="notice warn">${T('print.noUsbTitle')} <b>PVE ${T('print.noUsbPve')}</b>${T('print.noUsbBody')}
        ${d.usblp_loaded ? '' : T('print.noUsblp')}</div>`}
    <p class="desc" style="margin-top:12px">${T('print.netDevPre')}<span class="mono">lpinfo -v</span>）：</p>
    ${(d.devices || []).length ? `<div class="dk-table">
      <div class="dk-thead"><span>${T('print.thType')}</span><span>${T('print.thAddr')}</span></div>
      ${(d.devices || []).map(x => `<div class="dk-trow">
        <span class="mono" style="font-size:11.5px">${esc(x.kind)}</span>
        <span class="mono" style="font-size:11.5px">${esc(x.uri)}</span></div>`).join('')}</div>`
      : `<p class="hint-inline">${T('print.none')}</p>`}`;

  printBind();
  const out = $('#pt-out');
  if (out) { out.classList.add('hidden'); out.innerHTML = ''; }
}

function printBind() {
  $$('[data-ptq]').forEach(b => {
    b.onclick = async () => {
      const act = b.dataset.ptq, n = b.dataset.n;
      if (act === 'del') {
        modal(T('删除打印队列'), `<p>${T('将删除队列')} <span class="mono">${esc(n)}</span>。</p>
          <p>${T('队列里未完成的任务会一起清掉。确认继续？')}</p>`, async () => {
          const r = await api('/api/print', { method: 'POST',
            body: { op: 'queue_del', name: n, confirm: n } });
          toast(r.msg_cn, r.ok ? 'ok' : 'err', 7000);
          if (r.ok) printLoad();
        }, T('确认删除'));
        return;
      }
      if (act === 'test') {
        const r = await api('/api/print', { method: 'POST', body: { op: 'testpage', name: n } });
        toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
        return;
      }
      const body = { op: 'queue_set', name: n };
      if (act === 'share') body.share = b.dataset.v === '1';
      if (act === 'accept') body.accept = true;
      if (act === 'reject') body.accept = false;
      if (act === 'default') body.default = true;
      const r = await api('/api/print', { method: 'POST', body });
      toast(r.msg_cn, r.ok ? 'ok' : 'err', 7000);
      if (r.ok) printLoad();
    };
  });
  const add = $('#pt-add-go');
  if (add) add.onclick = async () => {
    const name = ($('#pt-new-name').value || '').trim();
    const uri = ($('#pt-new-uri').value || '').trim();
    if (!name) { toast(T('请填写队列名'), 'warn'); return; }
    if (!uri) { toast(T('请填写设备地址'), 'warn'); return; }
    const r = await api('/api/print', { method: 'POST', body: {
      op: 'queue_add', name, uri,
      driver: $('#pt-new-drv').value,
      info: ($('#pt-new-info').value || '').trim(),
      location: ($('#pt-new-loc').value || '').trim(),
      share: true } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
    if (r.ok) printLoad();
  };
  const jobs = $('#pt-jobs');
  if (jobs) jobs.onclick = async () => {
    const r = await api('/api/print', { method: 'POST', body: { op: 'jobs' } });
    const box = $('#pt-jobs-out');
    if (!r.ok) { box.innerHTML = `<p class="hint-inline" style="color:var(--err)">${esc(r.msg_cn)}</p>`; return; }
    const js = (r.data || {}).jobs || [];
    box.innerHTML = js.length ? `<div class="dk-table" style="margin-top:10px">
      <div class="dk-thead"><span>${T('任务')}</span><span>${T('用户')}</span><span>${T('大小')}</span><span>${T('操作')}</span></div>
      ${js.map(j => `<div class="dk-trow">
        <span class="mono" style="font-size:11.5px">${esc(j.id)}</span>
        <span class="mono" style="font-size:11.5px">${esc(j.user)}</span>
        <span class="mono" style="font-size:11.5px">${esc(j.size)}</span>
        <span><button class="ghost small" data-ptj="${esc(j.id)}">${T('取消')}</button></span></div>`).join('')}</div>`
      : `<p class="hint-inline">${T('当前没有打印任务。')}</p>`;
    $$('[data-ptj]').forEach(b => {
      b.onclick = async () => {
        const r2 = await api('/api/print', { method: 'POST', body: { op: 'job_cancel', id: b.dataset.ptj } });
        toast(r2.msg_cn, r2.ok ? 'ok' : 'err', 6000);
        if (r2.ok) jobs.click();
      };
    });
  };
}

async function printSave() {
  const q = id => document.getElementById(id);
  const mode = ($$('input[name="pt-mode"]').find(x => x.checked) || {}).value || 'cups';
  const r = await api('/api/print', { method: 'POST', body: {
    op: 'save', mode,
    cups: {
      listen: q('pt-listen') ? q('pt-listen').value : undefined,
      share: q('pt-share') ? q('pt-share').checked : undefined,
      web_iface: q('pt-web') ? q('pt-web').checked : undefined,
      remote_admin: q('pt-radmin') ? q('pt-radmin').checked : undefined,
      browsed: q('pt-browsed') ? q('pt-browsed').checked : undefined,
    },
    raw: {
      device: q('pt-raw-dev') ? q('pt-raw-dev').value.trim() : undefined,
      port: q('pt-raw-port') ? Number(q('pt-raw-port').value) : undefined,
      bind: q('pt-raw-bind') ? q('pt-raw-bind').value.trim() : undefined,
    },
  } });
  const box = $('#pt-out');
  box.classList.remove('hidden');
  const errs = ((r.data || {}).errors) || [];
  box.innerHTML = `<div class="notice ${r.ok && !errs.length ? 'ok' : (r.ok ? 'warn' : 'err')}">
      ${esc(r.msg_cn || '')}</div>
    ${errs.length ? `<ul class="desc" style="margin:8px 0 0;padding-left:20px;line-height:1.8">
      ${errs.map(e => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}`;
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
  if (r.ok) printLoad();
}

/* ============ AC / AP 管理中心（OpenSOHO，#10） ============ */
let OH = null;
let OH_PROBE = null;

async function viewOpensoho() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>AC / AP ${T('管理中心')}
        <button class="ghost small" id="oh-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">${T('用 OpenSOHO')} ${T('集中管理家里的')} OpenWRT ${T('无线')} AP：Wi-Fi、VLAN、PoE ${T('一次配好统一下发。')}
        AP ${T('侧装')} <span class="mono">openwisp-config</span> ${T('并用下面的共享密钥注册进来。')}</p>
      <div id="oh-state"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('安装')}</h3>
      <div id="oh-install"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('共享密钥')}</h3>
      <div id="oh-secret"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>${T('基本设置')}</h3>
      <div id="oh-cfg"><p class="desc">${T('正在读取')}…</p></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="oh-save">${T('保存并生效')}</button>
      </div>
      <div id="oh-out" class="hidden" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h3>AP ${T('与客户端概况')}
        <button class="ghost small" id="oh-probe" style="float:right">${T('读取')}</button></h3>
      <div id="oh-stats"><p class="desc">${T('点「读取」从')} OpenSOHO ${T('拉一次实时数据。')}</p></div>
    </div>
    <div class="card">
      <h3>${T('卸载')}</h3>
      <div id="oh-uninstall"><p class="desc">${T('正在读取')}…</p></div>
    </div>`;
  $('#oh-reload').onclick = ohLoad;
  $('#oh-save').onclick = ohSave;
  $('#oh-probe').onclick = ohProbe;
  ohLoad();
}

async function ohLoad() {
  const r = await api('/api/opensoho', { method: 'POST', body: { op: 'status' } });
  if (!r.ok) {
    $('#oh-state').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  OH = r.data;
  OH_PROBE = null;
  ohRender();
}

function ohSvcTag(s) {
  const on = (s || {}).active === 'active';
  return `<span class="tag ${on ? 'ok' : 'gray'}">${on ? T('运行中') : T('已停止')}</span>
    <span class="mono" style="font-size:11px;color:var(--txt3)">${esc((s || {}).enabled || '')}</span>`;
}

function ohRender() {
  const d = OH || {};
  const c = d.cfg || {};
  const inst = !!d.installed;
  const ip = d.lan_ip || '127.0.0.1';

  // ---------- 状态 ----------
  $('#oh-state').innerHTML = `
    ${inst ? '' : `<div class="notice warn" style="margin-bottom:10px">
      OpenSOHO ${T('还没装。它不是 apt 包，请在下面「安装」里装。')}</div>`}
    ${d.build_mode ? `<div class="notice warn" style="margin-bottom:10px">${T('当前处于')}<b>${T('构建保护模式')}</b>${T('：安装与保存只会写盘，不会真的启动服务。')}</div>` : ''}
    ${inst && d.healthy === false && (d.service || {}).active === 'active'
    ? `<div class="notice warn" style="margin-bottom:10px">
        systemd ${T('说服务在跑，但')} <span class="mono">/api/health</span> ${T('没应答（')}${esc(d.health_msg || '')}${T('）。 多半是启动后立刻退出了，看')} <span class="mono">journalctl -u opensoho</span>。</div>` : ''}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:0 18px">
      <div class="kv"><b>OpenSOHO</b><span>${inst ? `<span class="tag ok">${T('已安装')}</span>` : `<span class="tag warn">${T('未安装')}</span>`}</span></div>
      <div class="kv"><b>${T('版本')}</b><span class="mono">${esc(d.version || '—')}</span></div>
      <div class="kv"><b>${T('服务')}</b><span>${ohSvcTag(d.service)}</span></div>
      <div class="kv"><b>${T('健康检查')}</b><span>
        <span class="tag ${d.healthy ? 'ok' : 'gray'}">${d.healthy ? T('正常') : T('无应答')}</span></span></div>
      <div class="kv"><b>${T('实际监听')}</b><span class="mono">${(d.listen || []).length ? esc(d.listen.join('、')) : T('未监听')}</span></div>
      <div class="kv"><b>${T('控制台')}</b><span>${d.console_url && inst
      ? `<a href="${esc(httpUrl(d.console_url))}" target="_blank" rel="noopener" class="mono">${esc(d.console_url)}</a>` : '—'}</span></div>
    </div>
    <p class="desc" style="margin:10px 0 0">${esc(bt4('PAGE_NOTES', d.note, 'd', d.note) || '')}</p>`;

  // ---------- 安装 ----------
  const cands = d.local_candidates || [];
  $('#oh-install').innerHTML = `
    <div class="row">
      <button class="primary" id="oh-inst-net"${inst ? '' : ''}>${inst ? T('重新安装 / 升级') : T('在线安装（最新版）')}</button>
    </div>
    <div style="margin-top:14px">
      <label class="desc" style="display:block;margin-bottom:4px">
        ${T('从本机已有文件安装（机器访问不了')} GitHub ${T('时用这个）')}</label>
      <div class="row">
        <select id="oh-local" style="flex:1;min-width:200px">
          <option value="">— ${T('手动输入路径')} —</option>
          ${cands.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('')}
        </select>
        <input id="oh-local-path" class="mono" placeholder="/tmp/opensoho ${T('或 /tmp/opensoho')}_xxx.zip"
               style="flex:1;min-width:220px">
        <button class="ghost" id="oh-inst-local">${T('安装')}</button>
      </div>
      <p class="desc" style="margin:6px 0 0">
        ${T('先在「')}Web ${T('终端')} / ${T('文件」把二进制或')} zip ${T('传到')} <span class="mono">/tmp</span>${T('， 再在上面选中它。升级走同一条路径，数据目录会原样保留，已注册的')} AP ${T('不受影响。')}</p>
    </div>`;
  $('#oh-inst-net').onclick = () => ohInstall('');
  $('#oh-inst-local').onclick = () => {
    const sel = document.getElementById('oh-local').value;
    const typed = (document.getElementById('oh-local-path').value || '').trim();
    const path = typed || sel;
    if (!path) { toast(T('请先选择一个文件或填写路径'), 'err'); return; }
    ohInstall(path);
  };

  // ---------- 共享密钥 ----------
  $('#oh-secret').innerHTML = inst ? `
    <div class="row">
      <input class="mono" id="oh-secret-val" readonly value="${esc(d.secret || '')}"
             style="flex:1;min-width:240px;font-size:13px">
      <button class="ghost" id="oh-secret-copy">${T('复制')}</button>
      <button class="ghost" id="oh-secret-regen">${T('重新生成')}</button>
    </div>
    ${d.secret_weak ? `<div class="notice warn" style="margin-top:10px">${T('当前共享密钥偏短，建议重新生成一个。')}</div>` : ''}
    <p class="desc" style="margin:8px 0 0">
      ${T('在 AP 的 OpenWRT')} ${T('上装')} <span class="mono">openwisp-config</span>${T('，把控制器地址填')}
      <span class="mono">http://${esc(ip)}:${esc(String(c.port || 8090))}</span>${T('、共享密钥填上面这串。 重新生成后，')}<b>${T('已注册的')} AP ${T('都要重填一次')}</b>。</p>
    ${d.admin_email ? `<p class="desc" style="margin:6px 0 0">
      ${T('控制台管理员：')}<span class="mono">${esc(d.admin_email)}</span>
      ${T('密码')} <span class="mono">${esc(d.admin_password || '—')}</span>${T('（进控制台用它登录）')}</p>` : ''}
    <p class="desc" style="margin:6px 0 0">${T('配置静态加密：')}
      <span class="tag ${d.enc_ok ? 'ok' : 'warn'}">${d.enc_ok ? T('已启用') : T('未启用')}</span>
      Wi-Fi ${T('密码等敏感项在磁盘上是')}${d.enc_ok ? T('加密') : T('明文')}${T('存放的。')}</p>` :
    `<p class="desc">${T('安装后这里会显示 AP 注册用的共享密钥。')}</p>`;
  if (inst) {
    const cp = document.getElementById('oh-secret-copy');
    if (cp) cp.onclick = () => {
      navigator.clipboard.writeText(d.secret || '').then(() => toast(T('已复制'), 'ok'), () => toast(T('复制失败'), 'err'));
    };
    const rg = document.getElementById('oh-secret-regen');
    if (rg) rg.onclick = ohRegenSecret;
  }

  // ---------- 基本设置 ----------
  $('#oh-cfg').innerHTML = inst ? `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:0 18px">
      <div class="kv"><b>${T('监听地址')}</b><span>
        <select id="oh-bind">
          ${(d.bind_choices || []).map(b => `<option value="${esc(b)}"${b === (c.bind || '0.0.0.0') ? ' selected' : ''}>${esc(b)}</option>`).join('')}
        </select></span></div>
      <div class="kv"><b>${T('端口')}</b><span>
        <input id="oh-port" type="number" min="1024" max="65535" value="${esc(String(c.port || 8090))}"
               style="width:110px"></span></div>
    </div>
    <div class="dep-grid" style="margin-top:12px">
      <label class="dep-item"><input type="checkbox" id="oh-newdev"${c.enable_new_devices !== false ? ' checked' : ''}>
        <div class="dep-body"><b>${T('自动接管新')} AP</b>
          <div class="desc" style="margin:2px 0 0">${T('关掉')} = ${T('只监控不下发配置')}</div></div></label>
      <label class="dep-item"><input type="checkbox" id="oh-enc"${c.encryption !== false ? ' checked' : ''}${inst ? ' disabled' : ''}>
        <div class="dep-body"><b>${T('配置静态加密')}</b>
          <div class="desc" style="margin:2px 0 0">${T('加密存放')} Wi-Fi ${T('密码等敏感项')}${inst ? T('（库已建好，装完后不可再切换）') : ''}</div></div></label>
      <label class="dep-item"><input type="checkbox" id="oh-auto"${c.autostart !== false ? ' checked' : ''}>
        <div class="dep-body"><b>${T('开机自启')}</b>
          <div class="desc" style="margin:2px 0 0">${T('随系统一起启动')}</div></div></label>
    </div>
    <p class="desc" style="margin:10px 0 0">${T('改监听地址或端口会重建')} systemd ${T('单元并重启服务， 已注册的')} AP ${T('需要用新地址重新注册。')}</p>` :
    `<p class="desc">${T('安装后可在这里改监听地址、端口与几个开关。')}</p>`;

  // ---------- 卸载 ----------
  $('#oh-uninstall').innerHTML = inst ? `
    <div class="row">
      <label class="desc" style="margin:0"><input type="checkbox" id="oh-purge">
        ${T('同时删除数据目录（')}AP ${T('清单与配置）')}</label>
      <button class="danger" id="oh-uninst">${T('卸载')} OpenSOHO</button>
    </div>
    <p class="desc" style="margin:6px 0 0">${T('默认保留数据目录，下次重装直接接着用。')}</p>` :
    `<p class="desc">${T('尚未安装。')}</p>`;
  const ub = document.getElementById('oh-uninst');
  if (ub) ub.onclick = ohUninstall;

  // ---------- 概况 ----------
  ohRenderStats();
}

function ohRenderStats() {
  const box = document.getElementById('oh-stats');
  if (!box) return;
  const d = OH_PROBE;
  if (!d) {
    box.innerHTML = `<p class="desc">${T('点右上角「读取」从 OpenSOHO 拉一次实时数据。')}</p>`;
    return;
  }
  const stats = d.stats || [];
  const devs = d.devices || [];
  box.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0 18px">
      ${stats.map(s => `<div class="kv"><b>${esc(s.name)}</b><span class="mono">${
        s.count === null ? `<span class="tag warn">${T('读取失败')}</span>` : esc(String(s.count))
      }</span></div>`).join('')}
    </div>
    ${devs.length ? `<div style="margin-top:14px;overflow-x:auto">
      <table class="tbl"><thead><tr>
        <th>${T('名称')}</th><th>MAC</th><th>${T('地址')}</th><th>${T('型号')}</th><th>${T('状态')}</th>
      </tr></thead><tbody>
      ${devs.map(x => `<tr>
        <td>${esc(x.name || '—')}</td>
        <td class="mono">${esc(x.mac || '—')}</td>
        <td class="mono">${esc(x.ip || '—')}</td>
        <td>${esc(x.model || '—')}</td>
        <td><span class="tag ${x.enabled ? 'ok' : 'gray'}">${x.enabled ? T('已启用') : T('已停用')}</span></td>
      </tr>`).join('')}
      </tbody></table></div>` : `<p class="desc" style="margin-top:12px">${T('还没有 AP 注册进来。')}</p>`}`;
}

async function ohInstall(path) {
  const r = await api('/api/opensoho', { method: 'POST', body: { op: 'install', local_path: path || '' } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 12000);
  if (r.ok) ohLoad();
}

async function ohUninstall() {
  const purge = (document.getElementById('oh-purge') || {}).checked;
  if (!window.confirm(purge
    ? T('确定卸载 OpenSOHO 并删除数据目录？AP 清单与配置将一并清除，不可恢复。')
    : T('确定卸载 OpenSOHO？数据目录会保留，下次重装可直接使用。'))) return;
  const r = await api('/api/opensoho', { method: 'POST', body: { op: 'uninstall', purge } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
  if (r.ok) ohLoad();
}

async function ohSave() {
  const q = id => document.getElementById(id);
  const r = await api('/api/opensoho', { method: 'POST', body: {
    op: 'save',
    cfg: {
      bind: q('oh-bind') ? q('oh-bind').value : undefined,
      port: q('oh-port') ? Number(q('oh-port').value) : undefined,
      enable_new_devices: q('oh-newdev') ? q('oh-newdev').checked : undefined,
      encryption: q('oh-enc') ? q('oh-enc').checked : undefined,
      autostart: q('oh-auto') ? q('oh-auto').checked : undefined,
    },
  } });
  const box = $('#oh-out');
  box.classList.remove('hidden');
  box.innerHTML = `<div class="notice ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</div>`;
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
  if (r.ok) ohLoad();
}

async function ohRegenSecret() {
  if (!window.confirm(T('重新生成共享密钥后，已注册的 AP 都要用新密钥重新注册一次。确定继续？'))) return;
  const r = await api('/api/opensoho', { method: 'POST', body: { op: 'secret' } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
  if (r.ok) ohLoad();
}

async function ohProbe() {
  const r = await api('/api/opensoho', { method: 'POST', body: { op: 'probe' } });
  if (!r.ok) {
    toast(r.msg_cn || T('读取失败'), 'err', 9000);
    return;
  }
  OH_PROBE = r.data;
  ohRenderStats();
  toast(r.msg_cn, 'ok');
}

/* ============ CA 证书管理 + SSL 测试（#11） ============ */
let CA_DATA = null;
let CA_TEST = null;

async function viewCa() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('管理后台正在用的证书')}
        <button class="ghost small" id="ca-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">${T('浏览器打开')} <span class="mono">https://${T('本机')}:${T('端口')}</span> ${T('时看到的就是这张。 默认是一张自签证书')} —— ${T('能加密，但浏览器不认识它，所以会提示「不安全」。 想消除提示，在下面建一个自己的')} CA${T('，用它签一张服务器证书，再把根证书装进你的设备。')}</p>
      <div id="ca-web"><p class="desc">${T('正在读取')}…</p></div>
      <div class="row" style="margin-top:12px">
        <button class="ghost" id="ca-restore">${T('重新生成一张默认自签证书')}</button>
      </div>
    </div>
    <div class="card">
      <h3>${T('证书库')}</h3>
      <p class="desc">CA ${T('证书用来签发；服务器证书可以直接部署给管理后台； 证书签名请求（')}CSR${T('）是拿去给公共证书机构签名的半成品。')}</p>
      <div id="ca-list"><p class="desc">${T('正在读取')}…</p></div>
    </div>
    <div class="card">
      <h3>① ${T('创建自己的')} CA</h3>
      <p class="desc">CA ${T('是「发证机构」。建好之后，你签的每一张证书都属于同一个信任体系')} ——
        ${T('只要把这张')} CA ${T('装进手机')} / ${T('电脑一次，之后签的所有证书都不再提示不安全。')}</p>
      <div class="row">
        <label>${T('名称（显示用）')}<input id="ca-nc-name" placeholder="${T('家里的根证书')}" style="flex:1;min-width:160px"></label>
        <label>${T('证书主体')} CN<input id="ca-nc-cn" value="drouter-ca" style="flex:1;min-width:160px"></label>
        <label style="flex:0 0 150px">${T('有效期（天）')}<input id="ca-nc-days" type="number" min="1" max="36500" value="3650"></label>
        <label style="flex:0 0 200px">${T('私钥算法')}<select id="ca-nc-key"></select></label>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="ca-nc-go">${T('创建')} CA</button>
      </div>
    </div>
    <div class="card">
      <h3>② ${T('用 CA')} ${T('签一张服务器证书')}</h3>
      <p class="desc">${T('签好的证书会带上你填的域名和')} IP${T('。浏览器访问时，地址必须出现在')}
        <b>SAN${T('（使用者可选名称）')}</b>${T('里，否则照样报「名称不匹配」')}—— ${T('这是最常被忽略的一步。')}</p>
      <div class="row">
        <label>${T('用哪张')} CA ${T('签')}<select id="ca-sg-ca" style="flex:1;min-width:200px"></select></label>
        <label>${T('证书主体')} CN<input id="ca-sg-cn" placeholder="drouter.lan" style="flex:1;min-width:160px"></label>
      </div>
      <div class="row" style="margin-top:10px">
        <label style="flex:1">${T('域名（多个用逗号分隔）')}
          <input id="ca-sg-dns" placeholder="drouter.lan,router.home" ></label>
        <label style="flex:1">IP${T('（多个用逗号分隔）')}
          <input id="ca-sg-ip" class="mono"></label>
      </div>
      <div class="row" style="margin-top:10px">
        <label style="flex:0 0 150px">${T('有效期（天）')}<input id="ca-sg-days" type="number" min="1" max="36500" value="825"></label>
        <label style="flex:0 0 200px">${T('私钥算法')}<select id="ca-sg-key"></select></label>
        <button class="primary" id="ca-sg-go">${T('签发')}</button>
      </div>
    </div>
    <div class="card">
      <h3>③ ${T('导入已有证书')}</h3>
      <p class="desc">${T('从证书机构买的、别处签好的，直接粘进来（或填这台机器上的文件路径）。 证书和私钥可以粘在同一个框里，会自动拆开。')}</p>
      <div class="row">
        <label style="flex:0 0 170px">${T('类型')}<select id="ca-im-kind">
          <option value="server">${T('服务器证书')}</option>
          <option value="ca">CA ${T('证书')}</option>
        </select></label>
        <label style="flex:1">${T('补到哪条记录（可选，用来补全')} CSR）
          <select id="ca-im-id" style="width:100%"></select></label>
      </div>
      <label class="desc" style="display:block;margin:10px 0 4px">${T('证书')} PEM${T('（可含私钥）')}</label>
      <textarea id="ca-im-crt" rows="5" class="mono" style="width:100%;font-size:12px"
        placeholder="-----BEGIN CERTIFICATE-----&#10;…&#10;-----END CERTIFICATE-----"></textarea>
      <div class="row" style="margin-top:10px">
        <label style="flex:1">${T('或用这台机器上的文件')}
          <input id="ca-im-path" class="mono" placeholder="/tmp/fullchain.pem"></label>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="ca-im-go">${T('导入')}</button>
      </div>
    </div>
    <div class="card">
      <h3>④ ${T('生成证书签名请求（')}CSR）</h3>
      <p class="desc">${T('如果你有正式域名并想用公共证书机构签发的证书：在这里生成')} CSR${T('， 把它提交给证书机构，签回来的证书回到上面「导入」里补全即可。')}</p>
      <div class="row">
        <label>${T('域名')} CN<input id="ca-cs-cn" placeholder="router.example.com" style="flex:1;min-width:180px"></label>
        <label style="flex:1">${T('其它域名（逗号分隔）')}<input id="ca-cs-dns"></label>
        <label style="flex:0 0 200px">${T('私钥算法')}<select id="ca-cs-key"></select></label>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="ca-cs-go">${T('生成')} CSR</button>
      </div>
      <div id="ca-cs-out" class="hidden" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h3>SSL / TLS ${T('体检')}</h3>
      <p class="desc">${T('对任意主机的任意端口做一次真实握手，看协议版本、加密套件、证书链和校验结论。 默认测本机管理后台；也可以测局域网里其它设备、或者外网站点。')}</p>
      <div class="row">
        <label style="flex:1;min-width:170px">${T('主机')}<input id="ca-ts-host" placeholder="127.0.0.1"></label>
        <label style="flex:0 0 110px">${T('端口')}<input id="ca-ts-port" type="number" min="1" max="65535" value="8443"></label>
        <label style="flex:1;min-width:150px">SNI${T('（留空＝同主机）')}<input id="ca-ts-sni" placeholder="${T('可留空')}"></label>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="desc" style="margin:0"><input type="checkbox" id="ca-ts-insecure">
          ${T('不看校验结果，只要握手能通就行')}</label>
        <button class="primary" id="ca-ts-go">${T('开始测试')}</button>
        <button class="ghost" id="ca-ts-self">${T('测本机管理后台')}</button>
      </div>
      <div id="ca-ts-out" style="margin-top:12px"><p class="desc">${T('填好主机点「开始测试」。')}</p></div>
    </div>`;
  $('#ca-reload').onclick = caLoad;
  $('#ca-restore').onclick = caRestore;
  $('#ca-nc-go').onclick = caNewCA;
  $('#ca-sg-go').onclick = caSign;
  $('#ca-im-go').onclick = caImport;
  $('#ca-cs-go').onclick = caGenCsr;
  $('#ca-ts-go').onclick = () => caSslTest(false);
  $('#ca-ts-self').onclick = () => caSslTest(true);
  caLoad();
}

async function caLoad() {
  const r = await api('/api/ca', { method: 'POST', body: { op: 'status' } });
  if (!r.ok) {
    $('#ca-web').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  CA_DATA = r.data || {};
  caRender();
}

function caListSep() {
  return (window.i18n && i18n.getLang() === 'en-US') ? ', ' : '、';
}


function caDaysTag(days) {
  // 到期阈值由后端下发，前端不另写一份 —— 免得改了后端这边悄悄走偏
  const warn = (CA_DATA || {}).warn_days || 30;
  if (days === null || days === undefined) return `<span class="tag gray">${T('未知')}</span>`;
  if (days < 0) return `<span class="tag err">${T('已过期')} ${esc(String(-days))} ${T('天')}</span>`;
  if (days <= warn) return `<span class="tag warn">${T('剩')} ${esc(String(days))} ${T('天')}</span>`;
  return `<span class="tag ok">${T('剩')} ${esc(String(days))} ${T('天')}</span>`;
}

function caRender() {
  const d = CA_DATA || {};
  const w = d.web || {};
  const items = d.items || [];

  // ---------- 正在用的证书 ----------
  $('#ca-web').innerHTML = `
    ${d.build_mode ? `<div class="notice warn" style="margin-bottom:10px">${T('当前处于')}<b>${T('构建保护模式')}</b>${T('：部署只会写盘，不会重启 Web 服务。')}</div>` : ''}
    ${(d.index_broken) ? `<div class="notice err" style="margin-bottom:10px">${T('证书库索引文件损坏，列表可能不完整。新建证书会重建它。')}</div>` : ''}
    ${w.installed ? '' : '<div class="notice err" style="margin-bottom:10px">'
      + T('管理后台现在没有可用的证书：') + esc(w.err || T('证书文件缺失或无法解析')) + '。</div>'}
    ${(w.days_left !== null && w.days_left !== undefined && w.days_left <= (d.warn_days || 30))
      ? `<div class="notice ${w.days_left < 0 ? 'err' : 'warn'}" style="margin-bottom:10px">
          ${T('这张证书')}${w.days_left < 0 ? T('已经过期') : T('快到期了')}${T('（剩')} ${esc(String(w.days_left))} ${T('天）。 建议现在换一张，否则浏览器会彻底拒绝连接。')}</div>` : ''}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:0 18px">
      <div class="kv"><b>${T('证书主题')}</b><span class="mono">${esc(w.subject || '—')}</span></div>
      <div class="kv"><b>${T('颁发者')}</b><span class="mono">${esc(w.issuer || '—')}</span></div>
      <div class="kv"><b>${T('有效期')}</b><span class="mono">${esc(w.not_before || '—')} → ${esc(w.not_after || '—')}</span></div>
      <div class="kv"><b>${T('剩余')}</b><span>${caDaysTag(w.days_left)}</span></div>
      <div class="kv"><b>${T('类型')}</b><span>
        ${w.self_signed ? `<span class="tag warn">${T('自签（浏览器会提示）')}</span>`
          : `<span class="tag ok">${T('由 CA 签发')}</span>`}</span></div>
      <div class="kv"><b>${T('算法')}</b><span class="mono">${esc(w.sig_alg || w.key_alg || '—')}</span></div>
      <div class="kv"><b>SHA256 ${T('指纹')}</b><span class="mono" style="font-size:11px">${esc(w.fingerprint || '—')}</span></div>
      <div class="kv"><b>${T('在证书库里')}</b><span>${w.in_lib
        ? `<span class="tag ok">${T('是（')}${esc(w.in_lib)}）</span>`
        : `<span class="tag gray">${T('否（自动生成，未入库）')}</span>`}</span></div>
    </div>
    ${(w.san || []).length ? `<p class="desc" style="margin:10px 0 0">
      ${T('覆盖的名称：')}<span class="mono">${esc((w.san || []).join('、'))}</span></p>` : ''}
    ${(d.last) ? `<div class="notice ${d.last.ok ? 'ok' : 'err'}" style="margin-top:12px">
      ${T('上次部署（')}${esc(d.last.ts || '')}）：${esc(d.last.msg || '')}</div>` : ''}`;

  // ---------- 证书库 ----------
  if (!items.length) {
    $('#ca-list').innerHTML = `<p class="desc">${T('证书库是空的。先在下面「① 创建自己的 CA」建一张。')}</p>`;
  } else {
    $('#ca-list').innerHTML = `
      <div style="overflow-x:auto">
      <table class="tbl"><thead><tr>
        <th>${T('名称')}</th><th>${T('类型')}</th><th>${T('主题')} / CN</th><th>${T('有效期至')}</th><th>${T('剩余')}</th>
        <th>${T('私钥')}</th><th>${T('状态')}</th><th style="white-space:nowrap">${T('操作')}</th>
      </tr></thead><tbody>
      ${items.map(x => `<tr>
        <td>${esc(x.name || '—')}</td>
        <td><span class="tag ${x.kind === 'ca' ? 'info' : (x.kind === 'csr' ? 'gray' : 'ok')}">${esc(x.kind_cn || x.kind || '')}</span></td>
        <td class="mono" style="font-size:11px">${esc(x.subject || x.err || '—')}</td>
        <td class="mono" style="font-size:11px">${esc(x.not_after || '—')}</td>
        <td>${caDaysTag(x.days_left)}</td>
        <td>${x.has_key ? `<span class="tag ok">${T('有')}</span>` : `<span class="tag gray">${T('无')}</span>`}</td>
        <td>${x.in_use ? `<span class="tag ok">${T('正在用')}</span>`
          : (x.can_sign ? `<span class="tag info">${T('可签发')}</span>`
            : (x.has_csr ? `<span class="tag gray">${T('待签回')}</span>` : ''))}</td>
        <td style="white-space:nowrap">
          ${x.kind === 'server' && x.has_key && !x.in_use
            ? `<button class="ghost small" data-ca-deploy="${esc(x.id)}">${T('部署')}</button>` : ''}
          <button class="ghost small" data-ca-del="${esc(x.id)}">${T('删除')}</button>
        </td>
      </tr>`).join('')}
      </tbody></table></div>`;
    document.querySelectorAll('#ca-list [data-ca-deploy]').forEach(b => {
      b.onclick = () => caDeploy(b.getAttribute('data-ca-deploy'));
    });
    document.querySelectorAll('#ca-list [data-ca-del]').forEach(b => {
      b.onclick = () => caDelete(b.getAttribute('data-ca-del'));
    });
  }

  // ---------- 下拉：CA 列表 / 密钥算法 / 待补全的 CSR ----------
  const cas = items.filter(x => x.kind === 'ca' && x.has_key);
  const csrs = items.filter(x => x.kind === 'csr');
  const kts = d.key_types || [];
  ['ca-nc-key', 'ca-sg-key', 'ca-cs-key'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = kts.map(k => `<option value="${esc(k.v)}"${k.v === 'rsa:2048' ? ' selected' : ''}>${esc(bt4('CA_KEY_TYPES', k.v, 'en', k.n))}</option>`).join('')
      || '<option value="rsa:2048">RSA 2048</option>';
  });
  const selCa = document.getElementById('ca-sg-ca');
  if (selCa) {
    selCa.innerHTML = cas.length
      ? cas.map(x => `<option value="${esc(x.id)}">${esc(x.name)}（${esc(x.subject || x.id)}）</option>`).join('')
      : `<option value="">${T('— 还没有 CA，先创建一张 —')}</option>`;
  }
  const selId = document.getElementById('ca-im-id');
  if (selId) {
    selId.innerHTML = `<option value="">${T('— 新建一条记录 —')}</option>`
      + csrs.map(x => `<option value="${esc(x.id)}">${T('补全：')}${esc(x.name)}</option>`).join('');
  }
  const ipIn = document.getElementById('ca-sg-ip');
  if (ipIn && !ipIn.value) ipIn.value = (d.default_ips || []).join(',');
  const hostIn = document.getElementById('ca-ts-host');
  if (hostIn && !hostIn.value) hostIn.value = '127.0.0.1';
  const portIn = document.getElementById('ca-ts-port');
  if (portIn && !portIn.value) portIn.value = String(d.web_port || 8443);
}

async function caNewCA() {
  const q = id => document.getElementById(id);
  const r = await api('/api/ca', { method: 'POST', body: {
    op: 'new_ca',
    name: (q('ca-nc-name') || {}).value || '',
    cn: (q('ca-nc-cn') || {}).value || '',
    days: Number((q('ca-nc-days') || {}).value) || 3650,
    key_type: (q('ca-nc-key') || {}).value || 'rsa:2048',
  } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
  if (r.ok) caLoad();
}

async function caSign() {
  const q = id => document.getElementById(id);
  const caId = (q('ca-sg-ca') || {}).value || '';
  if (!caId) { toast(T('请先创建一张 CA 证书'), 'err'); return; }
  const split = s => (s || '').split(/[,，\s]+/).map(x => x.trim()).filter(Boolean);
  const r = await api('/api/ca', { method: 'POST', body: {
    op: 'sign', ca_id: caId,
    cn: (q('ca-sg-cn') || {}).value || '',
    san_dns: split((q('ca-sg-dns') || {}).value),
    san_ip: split((q('ca-sg-ip') || {}).value),
    days: Number((q('ca-sg-days') || {}).value) || 825,
    key_type: (q('ca-sg-key') || {}).value || 'rsa:2048',
  } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
  if (r.ok) caLoad();
}

async function caImport() {
  const q = id => document.getElementById(id);
  const r = await api('/api/ca', { method: 'POST', body: {
    op: 'import',
    kind: (q('ca-im-kind') || {}).value || 'server',
    complete_id: (q('ca-im-id') || {}).value || '',
    cert: (q('ca-im-crt') || {}).value || '',
    path: (q('ca-im-path') || {}).value || '',
  } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 10000);
  if (r.ok) caLoad();
}

async function caGenCsr() {
  const q = id => document.getElementById(id);
  const cn = ((q('ca-cs-cn') || {}).value || '').trim();
  if (!cn) { toast(T('请填写域名'), 'err'); return; }
  const r = await api('/api/ca', { method: 'POST', body: {
    op: 'csr', cn,
    san_dns: (q('ca-cs-dns') || {}).value.split(/[,，\s]+/).map(x => x.trim()).filter(Boolean),
    key_type: (q('ca-cs-key') || {}).value || 'rsa:2048',
  } });
  const box = $('#ca-cs-out');
  box.classList.remove('hidden');
  if (!r.ok) {
    box.innerHTML = `<div class="notice err">${esc(r.msg_cn || '')}</div>`;
    toast(r.msg_cn, 'err', 9000);
    return;
  }
  box.innerHTML = `<div class="notice ok">${esc(r.msg_cn)}</div>
    <label class="desc" style="display:block;margin:10px 0 4px">${T('把它交给证书机构（点一下全选复制）')}</label>
    <textarea id="ca-cs-text" rows="8" class="mono" readonly
      style="width:100%;font-size:12px">${esc((r.data || {}).csr || '')}</textarea>
    <div class="row" style="margin-top:8px">
      <button class="ghost" id="ca-cs-copy">${T('复制')} CSR</button></div>`;
  const cp = document.getElementById('ca-cs-copy');
  if (cp) cp.onclick = () => {
    navigator.clipboard.writeText((r.data || {}).csr || '')
      .then(() => toast(T('已复制'), 'ok'), () => toast(T('复制失败'), 'err'));
  };
  caLoad();
}

// 换证书要重启 drouter-web，而 helper 与它同在一个 systemd cgroup —— 重启会
// 把这次 HTTP 响应一起带走。这不是失败：结果已经写到 /etc/drouter/ca/
// last-deploy.json，刷新回来在本页顶部就能看到。所以这里要把「连接断了」
// 和「真的失败了」分开处理，否则用户会以为换证书的路上出了事。
function caRestarting(r) {
  return !r.ok && (r.code === 'NET' || r.code === 'PARSE');
}

function caAfterRestart() {
  toast(T('Web 服务正在重启，10 秒后自动刷新（需要重新登录一次）'), 'ok', 14000);
  setTimeout(() => location.reload(), 10000);
}

async function caDeploy(id) {
  if (!window.confirm(
    T('把这张证书部署给管理后台？\\n\\n')
    + T('· Web 服务会重启，你需要重新登录一次\\n')
    + T('· 换上后如果握手不通过，会自动还原原来的证书\\n')
    + T('· 自签证书浏览器仍会提示，那是正常的（加密是生效的）'))) return;
  const r = await api('/api/ca', { method: 'POST', body: { op: 'deploy', id } });
  if (caRestarting(r)) { caAfterRestart(); return; }
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 14000);
  if (r.ok) setTimeout(caLoad, 1500);
}

async function caDelete(id) {
  if (!window.confirm(T('确定删除这张证书？它的私钥会一起删除，不可恢复。'))) return;
  const r = await api('/api/ca', { method: 'POST', body: { op: 'delete', id } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
  if (r.ok) caLoad();
}

async function caRestore() {
  if (!window.confirm(
    T('重新生成一张默认自签证书并立即部署？\\n\\n')
    + T('它会带上本机所有 IP 与 localhost，有效期十年。')
    + T('Web 服务会重启，你需要重新登录。'))) return;
  const r = await api('/api/ca', { method: 'POST', body: { op: 'restore', cn: 'drouter.local' } });
  if (caRestarting(r)) { caAfterRestart(); return; }
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 14000);
  if (r.ok) setTimeout(caLoad, 1500);
}

async function caSslTest(selfMode) {
  const q = id => document.getElementById(id);
  const d = CA_DATA || {};
  if (selfMode) {
    const h = document.getElementById('ca-ts-host');
    const p = document.getElementById('ca-ts-port');
    if (h) h.value = '127.0.0.1';
    if (p) p.value = String(d.web_port || 8443);
  }
  const host = ((q('ca-ts-host') || {}).value || '').trim();
  if (!host) { toast(T('请填写要测试的主机'), 'err'); return; }
  $('#ca-ts-out').innerHTML = `<p class="desc">${T('正在握手…')}</p>`;
  const r = await api('/api/ca', { method: 'POST', body: {
    op: 'ssltest', host,
    port: Number((q('ca-ts-port') || {}).value) || 443,
    sni: (q('ca-ts-sni') || {}).value || '',
    insecure: !!(q('ca-ts-insecure') || {}).checked,
  } });
  if (!r.ok) {
    $('#ca-ts-out').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('测试失败'))}</div>`;
    return;
  }
  CA_TEST = r.data || {};
  caRenderTest();
}

function caRenderTest() {
  const t = CA_TEST || {};
  const box = $('#ca-ts-out');
  if (!box) return;
  if (!t.ok) {
    box.innerHTML = `<div class="notice err">${esc(t.host || '')}:${esc(String(t.port || ''))}
      —— ${esc(t.why || T('握手失败'))}</div>`;
    return;
  }
  const chain = t.chain || [];
  box.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:0 18px">
      <div class="kv"><b>${T('握手')}</b><span><span class="tag ok">${T('成功')}</span></span></div>
      <div class="kv"><b>${T('协议')}</b><span class="mono">${esc(t.proto || '—')}</span></div>
      <div class="kv"><b>${T('加密套件')}</b><span class="mono" style="font-size:11px">${esc(t.cipher || '—')}</span></div>
      <div class="kv"><b>${T('耗时')}</b><span class="mono">${esc(String(t.ms || 0))} ms</span></div>
    </div>
    <div class="dep-grid" style="margin-top:12px">
      ${(t.checks || []).map(c => `
        <div class="dep-item">
          <span class="tag ${c.ok ? 'ok' : 'err'}">${c.ok ? T('通过') : T('注意')}</span>
          <div class="dep-body"><b>${esc(c.n || '')}</b>
            <div class="desc" style="margin:2px 0 0">${esc(c.d || '')}</div></div>
        </div>`).join('')}
    </div>
    ${chain.length ? `<div style="margin-top:14px;overflow-x:auto">
      <table class="tbl"><thead><tr>
        <th>#</th><th>${T('主题')}</th><th>${T('颁发者')}</th><th>${T('有效期至')}</th><th>${T('剩余')}</th><th>${T('签名算法')}</th>
      </tr></thead><tbody>
      ${chain.map(c => `<tr>
        <td>${esc(String(c.level))}</td>
        <td class="mono" style="font-size:11px">${esc(c.subject || '—')}</td>
        <td class="mono" style="font-size:11px">${esc(c.issuer || '—')}</td>
        <td class="mono" style="font-size:11px">${esc(c.not_after || '—')}</td>
        <td>${caDaysTag(c.days_left)}</td>
        <td class="mono" style="font-size:11px">${esc(c.sig_alg || '—')}</td>
      </tr>`).join('')}
      </tbody></table>
      <p class="desc" style="margin:8px 0 0">${T('叶子证书 SHA256：')}
        <span class="mono" style="font-size:11px">${esc((chain[0] || {}).fingerprint || '—')}</span></p>
    </div>` : ''}`;
}

/* ============ 连接跟踪与流量日志 —— 统一日志系统（#11） ============ */
const UL_LEVEL_TAG = { emerg: 'err', alert: 'err', crit: 'err', err: 'err',
  warn: 'warn', notice: 'info', info: 'gray', debug: 'gray' };
// ⚠️ 显示名不能放模块级常量：切语言不重载页面（i18n.onChange 重绘），
// 常量只在脚本加载时求值一次会残留旧语言。改为渲染时求值。
function UL_SRC_MAP() {
  return { fw: T('防火墙'), conntrack: T('连接跟踪'), flow: T('当前连接'),
    wan: T('WAN 接入'), ddns: T('动态域名'), app: T('应用日志'), system: T('系统日志') };
}
const UL_SRC_ICON = { fw: '⛨', conntrack: '⇄', flow: '≡', wan: '⇅',
  ddns: '⌂', app: '✦', system: '⛁' };
function UL_PRESET_SINCE() {
  return [
    { v: '5 min ago', n: T('最近 5 分钟') },
    { v: '30 min ago', n: T('最近 30 分钟') },
    { v: '2 hours ago', n: T('最近 2 小时') },
    { v: '12 hours ago', n: T('最近 12 小时') },
    { v: '24 hours ago', n: T('最近 1 天') },
    { v: '7 days ago', n: T('最近 7 天') },
  ];
}

function ulState() {
  if (!S.ulog) {
    S.ulog = { view: 'log', since: '30 min ago', level: '', src: '', proto: '',
      action: '', q: '', limit: 400, items: [], stat: {}, meta: {},
      conf: {}, sources: [], levels: [], archive: {},
      // 实时连接事件订阅默认关：它会让服务端阻塞若干秒等 conntrack 事件。
      // 想看实时新建连接时再手动打开（#7：默认查询不该替它买单 2 秒）。
      live: false, liveSec: 3 };
  }
  return S.ulog;
}

/* ============ 配置备份与还原（1.0.7） ============ */
let BK_DATA = null;
let BK_INSPECT = null;

async function viewBackup() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('这是什么')}
        <button class="ghost small" id="bk-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">${T('这里导出的不是「整机镜像」，而是')}<b>${T('你这台路由器的全部设置')}</b>${T('： 网卡与')} WAN ${T('参数、')}DHCP ${T('与 DNS')}${T('、防火墙规则、限速与识别、访问控制、 共享与打印、')}Docker ${T('配置、主题外观。换机器、重装系统、或者改坏了想回到 某个时间点，从这里导出的包就能一键还原。')}</p>
      <p class="desc">${T('每个包里都带一份清单（导出于哪台机器、什么版本、每个文件的校验值）， 还原前会先校验，文件对不上就拒绝写入')} —— ${T('避免把一个损坏的包还原成半截配置。')}</p>
      <div id="bk-stat"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('备份覆盖范围')}</h3>
      <p class="desc">${T('下面这份清单与「系统设置')} → ${T('配置快照」同源，两边不会一份有一份没有。')}</p>
      <div id="bk-scope"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('立即导出')}</h3>
      <p class="desc">${T('导出会逐个文件计算校验值，几十')} KB ${T('的配置通常一两秒内完成。')}</p>
      <div class="row" style="margin:10px 0">
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="bk-sens">
          <span>${T('包含敏感文件（私钥')} · ${T('宽带密码')} · CA ${T('证书）')}</span>
        </label>
      </div>
      <p class="desc" style="color:var(--warn)">
        ${T('勾上之后包里的文件权限会收紧到仅')} root ${T('可读。含私钥的包请不要丢在共享目录里。')}</p>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="bk-create">${T('导出备份包')}</button>
      </div>
      <div id="bk-create-out" class="hidden" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>${T('已导出的备份包')}
        <span class="tag gray" id="bk-cnt"></span>
        <button class="ghost small" id="bk-prune" style="float:right">${T('清理旧包')}</button></h3>
      <p class="desc">${T('点文件名那一行的「校验」可以先看清包里有什么、哪些文件已损坏， 确认无误再还原。')}</p>
      <div id="bk-list"><p class="desc">${T('正在读取')}…</p></div>
      <div id="bk-inspect" class="hidden" style="margin-top:14px"></div>
    </div>

    <div class="card">
      <h3>${T('自动备份')}</h3>
      <p class="desc">${T('每天凌晨自动导出一份并清理旧包。默认')}<b>${T('不包含敏感文件')}</b> ——
        ${T('定时任务在后台跑，没人盯着的时候把私钥写出去不是好主意。')}</p>
      <div id="bk-auto"><p class="desc">${T('正在读取')}…</p></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="bk-conf">${T('保存自动备份设置')}</button>
      </div>
    </div>`;
  $('#bk-reload').onclick = () => bkLoad(true);
  $('#bk-create').onclick = bkCreate;
  $('#bk-conf').onclick = bkSaveConf;
  $('#bk-prune').onclick = bkPrune;
  bkLoad(false);
}

function bkRenderStat(d) {
  const mb = (d.bytes || 0) / 1048576.0;
  $('#bk-stat').innerHTML = `
    <table class="kv">
      <tr><td>${T('本机名称')}</td><td class="mono">${esc(d.hostname || T('未知'))}</td></tr>
      <tr><td>drouter ${T('版本')}</td><td class="mono">${esc(d.version || T('未知'))}</td></tr>
      <tr><td>${T('备份目录')}</td><td class="mono">${esc(d.dir || '')}
        ${d.exists ? '' : `<span class="tag warn">${T('尚未创建，首次导出会自动建')}</span>`}</td></tr>
      <tr><td>${T('本次将打包')}</td><td>${d.present || 0} / ${d.planned_total || 0} ${T('个来源项 （约')} ${mb < 0.01 ? '<1' : mb.toFixed(1)} MB）</td></tr>
      <tr><td>${T('含敏感文件')}</td><td>${d.conf && d.conf.include_sensitive
        ? `<span class="tag warn">${T('是')}</span>` : T('否')}</td></tr>
    </table>`;
  $('#bk-scope').innerHTML = (d.scope || []).map(g => `
    <div style="margin-bottom:12px">
      <b>${bt4('BACKUP_SCOPE', g.group, 'group', g.group)}</b>
      <ul class="tight">${(g.items || []).map(i => `<li>${esc(bt4('BACKUP_ITEMS', i, 'en', i))}</li>`).join('')}</ul>
    </div>`).join('');
}

function bkRenderList(packs) {
  $('#bk-cnt').textContent = (packs || []).length + ` ${T('个')}`;
  if (!packs || !packs.length) {
    $('#bk-list').innerHTML = `<p class="desc">${T('还没有导出过备份包。上面点一次「导出备份包」就会出现在这里。')}</p>`;
    return;
  }
  $('#bk-list').innerHTML = `<table class="tbl">
    <thead><tr><th>${T('包名')}</th><th>${T('大小')}</th><th>${T('导出时间')}</th><th class="nowrap">${T('操作')}</th></tr></thead>
    <tbody>${packs.map(x => `<tr>
      <td class="mono" style="font-size:12px">${esc(x.name)}</td>
      <td>${fmtBytes(x.size)}</td>
      <td class="mono" style="font-size:12px">${esc(x.mtime)}</td>
      <td class="nowrap">
        <button class="small" data-bkdl="${esc(x.name)}">${T('下载')}</button>
        <button class="small" data-bkck="${esc(x.name)}">${T('校验')}</button>
        <button class="small" data-bkrs="${esc(x.name)}">${T('还原')}</button>
        <button class="small danger" data-bkdel="${esc(x.name)}">${T('删除')}</button>
      </td></tr>`).join('')}</tbody></table>`;
  $$('[data-bkdl]').forEach(b => b.onclick = () => bkDownload(b.dataset.bkdl));
  $$('[data-bkck]').forEach(b => b.onclick = () => bkInspect(b.dataset.bkck));
  $$('[data-bkdel]').forEach(b => b.onclick = () => bkDelete(b.dataset.bkdel));
  $$('[data-bkrs]').forEach(b => b.onclick = () => bkRestore(b.dataset.bkrs));
}

function bkRenderAuto(t) {
  const c = (t && t.conf) || {};
  const units = (t && t.units) || {};
  const sw = (id, on) =>
    `<label class="switch"><input type="checkbox" id="${id}"${on ? ' checked' : ''}><i></i></label>`;
  const stTxt = u => {
    const v = u && (u.active !== undefined ? u.active : u);
    if (v === 'active') return `<span class="tag ok">${T('运行中')}</span>`;
    if (v === 'activating') return `<span class="tag ok">${T('启动中')}</span>`;
    if (v === 'failed') return `<span class="tag err">${T('失败')}</span>`;
    if (v === 'inactive') return `<span class="tag gray">${T('已停止')}</span>`;
    return `<span class="tag gray">${T('未知')}</span>`;
  };
  $('#bk-auto').innerHTML = `
    <table class="kv">
      <tr><td>${T('定时器')}</td><td>${stTxt(units['drouter-backupd.timer'])}</td></tr>
      <tr><td>${T('下次执行')}</td><td class="mono">${esc((t && t.next) || T('未启用'))}</td></tr>
      <tr><td>${T('上次结果')}</td><td>${t && t.last && t.last.msg_cn
        ? esc(t.last.msg_cn) + '<br><span class="desc mono" style="font-size:11px">'
          + esc(t.last.ts) + '</span>' : `<span class="desc">${T('暂无记录')}</span>`}</td></tr>
    </table>
    <div class="row" style="margin-top:12px;align-items:center;gap:14px;flex-wrap:wrap">
      <span>${T('每天自动导出')}</span>${sw('bk-auto-en', c.auto_enabled)}
      <span>${T('执行时刻')}</span>
      <input type="number" id="bk-auto-hour" min="0" max="23" value="${esc(c.auto_hour || 3)}"
        style="width:72px"> <span class="desc">${T('点（0')}–23）</span>
    </div>
    <div class="row" style="margin-top:10px;align-items:center;gap:14px;flex-wrap:wrap">
      <span>${T('最多保留')}</span>
      <input type="number" id="bk-keep-count" min="0" max="999"
        value="${esc(c.keep_count || 0)}" style="width:86px"> <span class="desc">${T('份（0')} = ${T('不限）')}</span>
      <span>${T('超过')}</span>
      <input type="number" id="bk-keep-days" min="0" max="3650"
        value="${esc(c.keep_days || 0)}" style="width:86px"> <span class="desc">${T('天就删（')}0 = ${T('不按时间）')}</span>
    </div>
    <p class="desc" style="margin-top:8px">${T('定时器固定在设定时刻之后的')} 17 ${T('分执行')} ——
      ${T('错开整点是为了不和磁盘清理、自动快照挤在同一分钟。')}</p>`;
}

async function bkLoad(toastIt) {
  const r = await api('/api/backup', { method: 'GET' });
  if (!r.ok) {
    $('#bk-stat').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  BK_DATA = r.data || {};
  bkRenderStat(BK_DATA);
  bkRenderList(BK_DATA.packs);
  const t = await api('/api/backupd');
  if (t.ok) bkRenderAuto(t.data);
  else $('#bk-auto').innerHTML =
    `<div class="notice warn">${esc(t.msg_cn || T('读取自动备份状态失败'))}</div>`;
  if (toastIt) toast(T('已重新读取'), 'ok');
}

async function bkCreate() {
  const sens = $('#bk-sens') && $('#bk-sens').checked;
  if (sens && !confirm(
    T('即将导出包含私钥与宽带密码的备份包。\\n\\n') +
    T('这类包必须妥善保管 —— 拿到包的人等于拿到你的管理后台密码和宽带账号。\\n\\n确定继续吗？'))) return;
  toast(T('正在导出备份包…'), 'ok', 4000);
  const r = await api('/api/backup', {
    method: 'POST', body: { op: 'create', include_sensitive: !!sens }, timeout: 600000
  });
  if (!r.ok) {
    toast(r.msg_cn || T('导出失败'), 'err', 8000);
    return;
  }
  const d = r.data || {};
  const miss = (d.missing || []).length;
  // 敏感项被排除要和「缺失」分开列：缺失是「本机没有这个功能」，
  // 排除是「有意不给」。混在一起用户会以为备份不完整。
  const exc = d.excluded || [];
  $('#bk-create-out').classList.remove('hidden');
  $('#bk-create-out').innerHTML = `
    <div class="notice ok">${esc(r.msg_cn || T('备份已导出'))}</div>
    <table class="kv">
      <tr><td>${T('包名')}</td><td class="mono">${esc(d.name || '')}</td></tr>
      <tr><td>${T('大小')}</td><td>${fmtBytes(d.size || 0)}</td></tr>
      <tr><td>${T('文件数')}</td><td>${d.files || 0}</td></tr>
      <tr><td>${T('缺失项')}</td><td>${miss ? miss + T(' 个来源项在本机不存在（已跳过）') : T('无')}</td></tr>
      <tr><td>${T('敏感排除')}</td><td>${exc.length
        ? exc.length + T(' 项未包含（见下）') : T('无')}</td></tr>
    </table>
    ${miss ? `<p class="desc" style="margin-top:8px">${T('缺失的一般是没启用的功能')}`
      + `${T('（比如没配打印服务就不会有 CUPS 配置），不影响还原。')}</p>` : ''}
    ${exc.length ? '<div class="notice warn" style="margin-top:8px">'
      + `${T('以下内容')}<b>${T('没有')}</b>${T('放进包里（默认不导出敏感项）：')}</div>`
      + '<ul class="tight">' + exc.slice(0, 30).map(e =>
        `<li class="mono" style="font-size:11px">${esc(e.path)} —— ${esc(e.why)}</li>`
      ).join('') + (exc.length > 30
        ? `<li class="desc">…${T('另有')} ${exc.length - 30} ${T('项')}</li>` : '')
      + '</ul>'
      + `<p class="desc">${T('换机还原后，这些内容需要在新机器上重新生成')}`
      + `${T('（证书可从旧机器单独拷贝）。')}</p>` : ''}
    <div class="row" style="margin-top:10px">
      <button class="primary" id="bk-dl-now">${T('现在下载')}</button>
    </div>`;
  const b = $('#bk-dl-now');
  if (b) b.onclick = () => bkDownload(d.name);
  toast(r.msg_cn || T('备份已导出'), 'ok');
  bkLoad(false);
}

async function bkDownload(name) {
  toast(T('正在准备下载 ') + name + '…', 'ok', 4000);
  try {
    const res = await fetch('/api/backup/download?name=' + encodeURIComponent(name),
      { headers: S.token ? { 'X-Token': S.token } : {} });
    if (!res.ok) {
      let m = T('下载失败');
      try { m = (await res.json()).msg_cn || m; } catch (e) { }
      toast(m, 'err', 8000);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast(T('已开始下载 ') + name, 'ok');
  } catch (e) {
    toast(T('下载失败：') + netErrCn(e && e.message), 'err', 8000);
  }
}

async function bkInspect(name) {
  const box = $('#bk-inspect');
  box.classList.remove('hidden');
  box.innerHTML = `<p class="desc">${T('正在校验包内每个文件的校验值…')}</p>`;
  const r = await api('/api/backup', {
    method: 'POST', body: { op: 'inspect', name }, timeout: 300000
  });
  if (!r.ok) {
    box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('校验失败'))}</div>`;
    return;
  }
  const d = r.data || {};
  const mf = d.manifest || {};
  BK_INSPECT = d;
  const bad = d.bad || [];
  const miss = d.missing || [];
  // excluded 来自 manifest：这个包**有意**没包含的敏感内容。
  // 必须在还原之前告诉用户，否则他还原完才发现证书没了。
  const exc = mf.excluded || [];
  const groups = {};
  (d.files || []).forEach(f => {
    const g = f.group || T('其它');
    (groups[g] = groups[g] || []).push(f);
  });
  box.innerHTML = `
    <h4>${esc(name)} ${T('的内容')}</h4>
    <table class="kv">
      <tr><td>${T('导出于')}</td><td class="mono">${esc(mf.created || '')}</td></tr>
      <tr><td>${T('来源机器')}</td><td class="mono">${esc(mf.hostname || '')}</td></tr>
      <tr><td>drouter ${T('版本')}</td><td class="mono">${esc(mf.drouter_version || '')}</td></tr>
      <tr><td>${T('含敏感文件')}</td><td>${mf.include_sensitive
        ? `<span class="tag warn">${T('是')}</span>` : T('否')}</td></tr>
      <tr><td>${T('敏感排除')}</td><td>${exc.length
        ? exc.length + T(' 项未包含') : T('无')}</td></tr>
      <tr><td>${T('备注')}</td><td>${esc(mf.note || T('（无）'))}</td></tr>
      <tr><td>${T('校验结果')}</td><td>${T('正常')} ${d.ok_count || 0} ${T('个')}${
        bad.length ? ` · <span class="tag err">${T('损坏')} ` + bad.length + ` ${T('个')}</span>` : ''}${
        miss.length ? ` · <span class="tag warn">${T('缺失')} ` + miss.length + ` ${T('个')}</span>` : ''}</td></tr>
    </table>
    ${bad.length ? `<div class="notice err" style="margin-top:10px">${T('包内')} `
      + bad.length + T(' 个文件与清单不符 —— 这个包在传输中损坏过，')
      + `<b>${T('不要还原')}</b>${T('，请重新导出。')}</div>` : ''}
    ${exc.length ? '<div class="notice warn" style="margin-top:10px">'
      + `${T('这个包')}<b>${T('有意')}</b>${T('未包含')} ` + exc.length + T(' 项敏感内容')
      + `${T('（CA 私钥、宽带密码等）。配置本身能还原，但这些需要还原后重新生成。')}</div>`
      + `<details style="margin-top:6px"><summary>${T('查看被排除的')} `
      + exc.length + ` ${T('项')}</summary><ul class="tight">`
      + exc.slice(0, 40).map(e => `<li class="mono" style="font-size:11px">${
        esc(e.path)} —— ${esc(e.why)}</li>`).join('')
      + (exc.length > 40 ? `<li class="desc">…${T('另有')} ${
        exc.length - 40} ${T('项')}</li>` : '') + '</ul></details>' : ''}
    ${Object.keys(groups).map(g => `
      <details style="margin-top:8px"><summary>${esc(g)}（${groups[g].length} ${T('个文件）')}</summary>
      <table class="tbl"><thead><tr><th>${T('文件')}</th><th>${T('大小')}</th><th>${T('状态')}</th></tr></thead>
      <tbody>${groups[g].map(f => `<tr>
        <td class="mono" style="font-size:11px">${esc(f.path)}${
          f.sensitive ? ` <span class="tag warn">${T('敏感')}</span>` : ''}</td>
        <td>${fmtBytes(f.size || 0)}</td>
        <td>${f.state === '正常' ? `<span class="tag ok">${T('正常')}</span>`
          : '<span class="tag err">' + esc(f.state) + '</span>'}</td></tr>`).join('')}
      </tbody></table></details>`).join('')}`;
}

function bkDelete(name) {
  modal(T('删除备份包'),
    `<p>${T('确定删除')} <b class="mono">${esc(name)}</b> ${T('吗？')}</p>
     <p class="desc">${T('如果这个包已经下载到本地电脑，删掉不影响你手上的那份。')}</p>`,
    async () => {
      const r = await api('/api/backup', { method: 'POST', body: { op: 'delete', name } });
      toast(r.msg_cn, r.ok ? 'ok' : 'err');
      if (r.ok) { $('#bk-inspect').classList.add('hidden'); bkLoad(false); }
    }, T('删除'));
}

function bkRestore(name) {
  const n = (v, d) => (v === '' || v === null || v === undefined) ? d : Number(v);
  const steps = [
    { op: 'inspect', name },
    { op: 'restore', name, dry_run: true },
    { op: 'restore', name, confirm: true, dry_run: false },
  ];
  const box = $('#bk-inspect');
  box.classList.remove('hidden');
  box.innerHTML = `<p class="desc">${T('正在预检…（第 1 步：读取包内容）')}</p>`;
  // 三步走：先校验包本身，再 dry-run 出计划，最后才真写。
  // 少任何一步都会出问题：跳过校验会把损坏包写进系统，
  // 跳过预检就是让用户闭着眼点「确认」。
  (async () => {
    const r1 = await api('/api/backup', {
      method: 'POST', body: steps[0], timeout: 300000
    });
    if (!r1.ok) {
      box.innerHTML = `<div class="notice err">${esc(r1.msg_cn || T('读取备份包失败'))}</div>`;
      return;
    }
    const bad = (r1.data.bad || []).length;
    if (bad) {
      box.innerHTML = `<div class="notice err">${T('包内')} ${bad} ${T('个文件校验不通过，')}`
        + T('这个包已损坏，<b>已中止还原</b>。请重新导出。</div>');
      return;
    }
    box.innerHTML = `<p class="desc">${T('正在预检…（第 2 步：算出还原计划）')}</p>`;
    const r2 = await api('/api/backup', {
      method: 'POST', body: steps[1], timeout: 300000
    });
    if (!r2.ok) {
      box.innerHTML = `<div class="notice err">${esc(r2.msg_cn || T('预检失败'))}</div>`;
      return;
    }
    const plan = r2.data.plan || [];
    const errs = r2.data.errors || [];
    const skip = r2.data.skipped || [];
    box.innerHTML = `
      <h4>${T('还原预检结果')}</h4>
      <div class="notice ${errs.length ? 'err' : 'ok'}">
        ${T('将写入')} <b>${plan.length}</b> ${T('个文件')}${errs.length
          ? `${T('，另有')} <b>` + errs.length + `</b> ${T('项被拒绝')}` : T('，没有冲突')}</div>
      ${errs.length ? '<ul class="tight" style="color:var(--err)">'
        + errs.map(e => `<li>${esc(e)}</li>`).join('') + '</ul>' : ''}
      ${skip.length ? `<p class="desc" style="margin-top:8px">${T('以下文件按设计跳过：')}</p>`
        + '<ul class="tight">' + skip.map(e =>
          `<li class="mono" style="font-size:11px">${esc(e.path)} —— ${esc(e.why)}</li>`).join('')
        + '</ul>' : ''}
      <details style="margin-top:8px"><summary>${T('查看完整清单（')}${plan.length} ${T('项）')}</summary>
        <table class="tbl"><thead><tr><th>${T('文件')}</th><th>${T('大小')}</th><th>${T('本机现状')}</th></tr></thead>
        <tbody>${plan.map(f => `<tr>
          <td class="mono" style="font-size:11px">${esc(f.path)}${
            f.sensitive ? ` <span class="tag warn">${T('敏感')}</span>` : ''}</td>
          <td>${fmtBytes(f.size || 0)}</td>
          <td>${f.exists ? T('将覆盖') : T('将新建')}</td></tr>`).join('')}
        </tbody></table></details>`;
    if (errs.length) return;
    modal(T('确认还原配置'),
      `<p>${T('即将把')} <b class="mono">${esc(name)}</b> ${T('里的')} <b>${plan.length}</b> ${T('个文件写入本机。')}</p>
       <p class="desc">${T('现有同名文件会先被复制成')} <span class="mono">.drouter-restore-bak</span>
       ${T('备份再覆盖，所以这一操作本身是可退的。')}</p>
       <p class="desc">${T('还原')}<b>${T('只写文件、不重启服务')}</b>${T('。写完之后请到相关页面确认， 再点一次「保存并应用」让改动真正生效。')}</p>
       <p class="desc" style="color:var(--warn)">${T('网络相关配置在服务重启瞬间可能短暂中断。')}</p>`,
      async () => {
        box.innerHTML = `<p class="desc">${T('正在还原…')}</p>`;
        const r3 = await api('/api/backup', {
          method: 'POST', body: steps[2], timeout: 600000
        });
        if (!r3.ok) {
          box.innerHTML = `<div class="notice err">${esc(r3.msg_cn || T('还原失败'))}</div>`;
          toast(r3.msg_cn || T('还原失败'), 'err', 10000);
          return;
        }
        const dd = r3.data || {};
        box.innerHTML = `<div class="notice ok">${esc(r3.msg_cn || T('还原完成'))}</div>`;
        toast(r3.msg_cn || T('还原完成'), 'ok', 10000);
        bkLoad(false);
        return dd.applied;
      }, T('确认还原'));
  })();
}

async function bkPrune() {
  const c = (BK_DATA && BK_DATA.conf) || {};
  const kc = c.keep_count || 0;
  const kd = c.keep_days || 0;
  if (kc === 0 && kd === 0) {
    toast(T('当前设置是「不按份数、不按天数」清理，没有可清理的旧包'), 'warn', 6000);
    return;
  }
  const r = await api('/api/backup', {
    method: 'POST', body: { op: 'prune', keep_count: kc, keep_days: kd }
  });
  toast(r.msg_cn, r.ok ? 'ok' : 'err');
  if (r.ok) bkLoad(false);
}

async function bkSaveConf() {
  const g = id => { const e = $('#' + id); return e ? e.value : ''; };
  const conf = {
    auto_enabled: !!(($('#bk-auto-en') || {}).checked),
    auto_hour: g('bk-auto-hour'),
    keep_count: g('bk-keep-count'),
    keep_days: g('bk-keep-days'),
  };
  const r = await api('/api/backup', { method: 'POST', body: { op: 'conf', conf } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
  if (r.ok) {
    if (conf.auto_enabled && r.data && r.data.timer_applied === false) {
      toast(T('设置已保存，但定时器未成功启用（详见下方状态表）'), 'warn', 10000);
    }
    bkLoad(false);
  }
}

/* ============ 告警与通知中心（1.0.7） ============ */
let AL_DATA = null;
const AL_LV = () => ({ critical: { n: T('严重'), c: 'err' }, warn: { n: T('警告'), c: 'warn' },
                info: { n: T('提示'), c: 'info' } });

async function viewAlert() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('它解决什么问题')}
        <button class="ghost small" id="al-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">${T('在此之前，这套系统是「坏了你自己来看」')}—— ${T('日志、快照、流日志 全都齐全，但没有任何东西会在你睡觉或出门的时候告诉你「家里断网了」。 断')} 4 ${T('小时和断')} 4 ${T('分钟，对在家办公的人差别巨大。')}</p>
      <p class="desc">${T('判定和推送都由后台定时任务完成，')}<b>${T('不需要你打开这个页面')}</b>${T('。 这里只是配置阈值、添加通知通道、查看历史。')}</p>
      <div id="al-top"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('通知通道')}</h3>
      <p class="desc">${T('告警要送到哪里去。至少配一个并勾上「启用」，否则规则命中了也只 出现在下面的历史记录里。建议手机推送与邮件各配一个作为双保险。')}</p>
      <div id="al-ch"></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="al-add-bark">${T('添加')} Bark</button>
        <button class="primary" id="al-add-smtp">${T('添加邮件')}</button>
        <button class="primary" id="al-add-hook">${T('添加群机器人')}</button>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="ghost" id="al-test">${T('发一条测试消息')}</button>
      </div>
      <div id="al-test-out" class="hidden" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>${T('告警规则与阈值')}</h3>
      <p class="desc">${T('每条规则下面都写了「为什么是这个数」。阈值太松会漏报， 太紧会变成每天几十条推送的噪音，最后你直接把通知静音')} —— ${T('那就白配了。')}</p>
      <div id="al-rules"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('总开关与推送策略')}</h3>
      <div id="al-pol"><p class="desc">${T('正在读取')}…</p></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="al-save">${T('保存设置')}</button>
        <button class="ghost" id="al-run">${T('立即检测一次')}</button>
      </div>
      <div id="al-run-out" class="hidden" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>${T('实时探测值')}
        <span class="tag gray" id="al-probe-ts"></span></h3>
      <p class="desc">${T('这是判定用的原始数据。阈值调之前先看看这里的实际数值合不合理。')}</p>
      <div id="al-probes"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('告警历史')}
        <span class="tag gray" id="al-hcnt"></span>
        <button class="ghost small danger" id="al-clear" style="float:right">${T('清空历史')}</button></h3>
      <p class="desc">${T('冷却状态也会一并清空，所以清完之后下一次命中会立刻推送。')}</p>
      <div id="al-hist"><p class="desc">${T('正在读取')}…</p></div>
    </div>`;
  $('#al-reload').onclick = () => alLoad(true);
  $('#al-save').onclick = alSave;
  $('#al-run').onclick = alRun;
  $('#al-test').onclick = alTest;
  $('#al-clear').onclick = alClear;
  $('#al-add-bark').onclick = () => alAddChannel('bark');
  $('#al-add-smtp').onclick = () => alAddChannel('smtp');
  $('#al-add-hook').onclick = () => alAddChannel('webhook');
  alLoad(false);
}

function alRenderTop(d) {
  const u = d.units || {};
  const on = d.conf && d.conf.enabled;
  const t = u['drouter-alertd.timer'];
  const tv = t && (t.active !== undefined ? t.active : t);
  const tag = tv === 'active' ? `<span class="tag ok">${T('运行中')}</span>`
    : tv === 'failed' ? `<span class="tag err">${T('失败')}</span>`
    : on ? `<span class="tag warn">${T('已启用但未运行')}</span>`
    : `<span class="tag gray">${T('已停用')}</span>`;
  $('#al-top').innerHTML = `
    <table class="kv">
      <tr><td>${T('告警总开关')}</td><td>${on
        ? `<span class="tag ok">${T('已启用')}</span>` : `<span class="tag gray">${T('未启用')}</span>`}</td></tr>
      <tr><td>${T('定时检测')}</td><td>${tag}</td></tr>
      <tr><td>${T('检测间隔')}</td><td>${(((d.conf || {}).interval_sec || 300) / 60).toFixed(0)} ${T('分钟')}</td></tr>
      <tr><td>${T('下次执行')}</td><td class="mono">${esc(d.next || T('未启用'))}</td></tr>
      <tr><td>${T('已启用通道')}</td><td>${d.enabled_ch || 0} ${T('个')}${
        d.enabled_ch ? '' : ` <span class="tag warn">${T('没有通道，告警只记历史不发出去')}</span>`}</td></tr>
      <tr><td>${T('上次检测')}</td><td>${d.last && d.last.msg_cn
        ? esc(d.last.msg_cn) + '<br><span class="desc mono" style="font-size:11px">'
          + esc(d.last.ts) + '</span>' : `<span class="desc">${T('暂无记录')}</span>`}</td></tr>
      ${d.quiet_now ? `<tr><td>${T('免打扰')}</td><td><span class="tag info">${T('当前处于免打扰时段')}`
        + `${T('（严重告警仍会推送）')}</span></td></tr>` : ''}
    </table>
    ${on && tv !== 'active'
      ? `<div class="notice warn" style="margin-top:10px">${T('总开关是开的，但定时器没在跑。')}`
        + T('点一次「保存设置」会重写单元并尝试启用；如果仍不生效，')
        + `${T('在 Web 终端里执行')} <span class="mono">systemctl status drouter-alertd.timer</span> ${T('看原因。')}</div>`
      : ''}`;
}

function alRenderChannels(d) {
  const chs = (d.conf && d.conf.channels) || [];
  if (!chs.length) {
    $('#al-ch').innerHTML = `<p class="desc">${T('还没有添加任何通知通道。')}`
      + T('从下面三个按钮里挑一个开始 —— 手机推送最省事。</p>');
    return;
  }
  const meta = {};
  (d.channel_types || []).forEach(t => { meta[t.k] = t; });
  $('#al-ch').innerHTML = chs.map((c, i) => {
    const m = meta[c.type] || { n: c.type, d: '' };
    const f = [];
    if (c.type === 'bark') {
      f.push([T('推送地址 / Key'), 'url', `https://api.day.app/${T('你的Key')}`,
        T('留空会当成只填 Key 处理，自动补成官方地址')]);
    } else if (c.type === 'smtp') {
      f.push([T('SMTP 服务器'), 'host', 'smtp.qq.com', '']);
      f.push([T('端口'), 'port', '465', T('465 走 SSL，587 走 STARTTLS，25 一般是明文')]);
      f.push([T('账号'), 'user', 'you@qq.com', '']);
      f.push([T('授权码 / 密码'), 'pass', '', T('留空表示不修改已保存的密码')]);
      f.push([T('发件人'), 'from', '', T('留空则用账号')]);
      f.push([T('收件人'), 'to', 'you@qq.com', T('多个用逗号分隔')]);
    } else {
      f.push([T('Webhook 地址'), 'url',
        'https://oapi.dingtalk.com/robot/send?access_token=xxx',
        T('支持钉钉 / 企业微信 / 飞书，按地址自动选报文格式')]);
    }
    return `<div class="dep-item" style="margin-bottom:12px">
      <div class="row" style="align-items:center;gap:10px;flex-wrap:wrap">
        <b>${esc(c.name || m.n)}</b>
        <span class="tag gray">${esc(bt4('ALERT_CHANNELS', m.k, 'n', m.n))}</span>
        ${c.enabled ? `<span class="tag ok">${T('已启用')}</span>`
          : `<span class="tag gray">${T('未启用')}</span>`}
        <label class="switch" style="margin-left:auto">
          <input type="checkbox" data-al-en="${i}"${c.enabled ? ' checked' : ''}><i></i>
        </label>
      </div>
      <p class="desc" style="margin:4px 0 8px">${esc(bt4('ALERT_CHANNELS', m.k, 'd', m.d))}</p>
      ${f.map(([lb, key, ph, hint]) => `<div class="row" style="align-items:center;gap:10px;margin-bottom:6px">
        <span style="width:130px" class="desc">${esc(lb)}</span>
        <input type="${key === 'pass' ? 'password' : 'text'}" data-al-c="${i}" data-al-k="${key}"
          value="${esc(c[key] == null ? '' : c[key])}" placeholder="${esc(ph)}"
          style="flex:1;min-width:180px">
      </div>${hint ? `<p class="desc" style="margin:-2px 0 6px 140px">${esc(hint)}</p>` : ''}`).join('')}
      <div class="row" style="margin-top:6px">
        <button class="ghost small" data-al-t1="${i}">${T('单独测试')}</button>
        <button class="ghost small danger" data-al-rm="${i}">${T('删除')}</button>
      </div>
    </div>`;
  }).join('');
  $$('[data-al-rm]').forEach(b => b.onclick = () => alRmChannel(Number(b.dataset.alRm)));
  $$('[data-al-t1]').forEach(b => b.onclick = () => alTest(b.dataset.alT1 != null
    ? (AL_DATA.conf.channels[Number(b.dataset.alT1)] || {}).type : ''));
}

function alRenderRules(d) {
  $('#al-rules').innerHTML = (d.rules || []).map(r => {
    const step = r.step || 1;
    return `<div class="dep-item" style="margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--line)">
      <div class="row" style="align-items:center;gap:10px;flex-wrap:wrap">
        <b>${esc(bt4('ALERT_RULES', r.key || r.k, 'n', r.name))}</b>
        <span class="tag ${(AL_LV()[r.lv] || {}).c || 'gray'}">${esc((AL_LV()[r.lv] || {}).n || r.lv)}</span>
        ${r.firing ? `<span class="tag err">${T('当前命中中')}</span>` : ''}
      </div>
      <p class="desc" style="margin:4px 0 6px">${esc(bt4('ALERT_RULES', r.key || r.k, 'why', r.why))}</p>
      <div class="row" style="align-items:center;gap:8px">
        <span class="desc">${T('阈值')}</span>
        <input type="number" data-al-r="${esc(r.key)}" value="${esc(r.value)}"
          min="${esc(r.min)}" max="${esc(r.max)}" step="${esc(step)}" style="width:110px">
        <span class="desc">${esc(bt4('ALERT_RULES', r.key || r.k, 'unit', r.unit) || r.unit || '')}</span>
        <span class="desc">${T('（可填')} ${esc(r.min)} – ${esc(r.max)}${T('，默认')} ${esc(r.default)}）</span>
      </div>
    </div>`;
  }).join('');
}

function alRenderPol(d) {
  const c = d.conf || {};
  $('#al-pol').innerHTML = `
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px">
      <span>${T('启用告警')}</span>
      <label class="switch"><input type="checkbox" id="al-enabled"${c.enabled ? ' checked' : ''}><i></i></label>
      <span class="desc">${T('关掉后仍然会探测，但不会推送任何消息')}</span>
    </div>
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <span>${T('检测间隔')}</span>
      <input type="number" id="al-interval" min="60" max="3600" step="60"
        value="${esc(c.interval_sec || 300)}" style="width:110px"><span class="desc">${T('秒（60')}–3600）</span>
      <span style="margin-left:16px">${T('同一条规则最短重复间隔')}</span>
      <input type="number" id="al-cooldown" min="1" max="1440"
        value="${esc(c.cooldown_min || 30)}" style="width:90px"><span class="desc">${T('分钟')}</span>
    </div>
    <div class="row" style="align-items:center;gap:10px;flex-wrap:wrap">
      <span>${T('免打扰时段')}</span>
      <input type="time" id="al-qfrom" value="${esc(c.quiet_from || '')}" style="width:130px">
      <span class="desc">${T('至')}</span>
      <input type="time" id="al-qto" value="${esc(c.quiet_to || '')}" style="width:130px">
      <span class="desc">${T('留空表示不设。跨零点（如')} 23:00 → 07:00${T('）也支持')}</span>
    </div>
    <p class="desc" style="margin-top:8px">
      ${T('免打扰只压「警告」和「提示」，')}<b>${T('严重告警（外网中断）永远会推送')}</b> ——
      ${T('「半夜别吵我」不包括「家里断网了」。')}</p>`;
}

function alRenderProbes(p) {
  if (!p || !Object.keys(p).length) {
    $('#al-probes').innerHTML = `<p class="desc">${T('尚无探测数据。点一次「立即检测」就会刷新。')}</p>`;
    return;
  }
  const rows = [];
  const w = p.wan || {};
  rows.push([T('外网连通性'), w.up
    ? `<span class="tag ok">${T('正常')}</span> ${T('网关')} ` + esc(w.gw || T('未知'))
      + (w.loss_pct != null ? T('，丢包 ') + w.loss_pct + '%' : '')
    : `<span class="tag err">${T('不可达')}</span> ` + esc(w.why || ''),
    w.up ? 'ok' : 'err']);
  const d = p.disk || {};
  if (d.pct != null) {
    rows.push([T('最满的磁盘'), esc(d.mount || '/') + T(' · 已用 ') + d.pct
      + T('%（剩余 ') + esc(d.avail || T('未知')) + '）',
      d.pct >= 85 ? 'err' : d.pct >= 75 ? 'warn' : 'ok']);
  }
  if (p.temp && p.temp.c != null) {
    rows.push([T('CPU 温度'), p.temp.c + T(' ℃（来源 ') + esc(p.temp.src || T('未知')) + '）',
      p.temp.c >= 80 ? 'err' : p.temp.c >= 70 ? 'warn' : 'ok']);
  }
  const m = p.mem || {};
  if (m.pct != null) {
    rows.push([T('内存'), T('已用 ') + m.pct + T('%（可用 ') + m.avail_mb + ` MB / ${T('共')} `
      + m.total_mb + ' MB）', m.pct >= 92 ? 'err' : m.pct >= 80 ? 'warn' : 'ok']);
  }
  const l = p.load || {};
  if (l.load1 != null) {
    rows.push([T('系统负载'), T('1 分钟 ') + l.load1 + T(' · 5 分钟 ') + l.load5
      + T(' · 15 分钟 ') + l.load15, l.load1 >= 3 ? 'warn' : 'ok']);
  }
  const lt = p.latency || {};
  if (lt.ms != null || lt.loss_pct != null) {
    rows.push([T('到 ') + esc(lt.target || T('网关')) + T(' 的延迟'),
      (lt.ms != null ? lt.ms + ' ms' : T('超时'))
      + (lt.loss_pct != null ? T(' · 丢包 ') + lt.loss_pct + '%' : ''),
      (lt.loss_pct != null && lt.loss_pct >= 30) ? 'err' : 'ok']);
  }
  if (p.backup_fail != null) {
    rows.push([T('自动备份连续失败'), p.backup_fail + ` ${T('次')}`,
      p.backup_fail >= 2 ? 'warn' : 'ok']);
  }
  if (p.snapshot_fail != null) {
    rows.push([T('自动快照连续失败'), p.snapshot_fail + ` ${T('次')}`,
      p.snapshot_fail >= 3 ? 'warn' : 'ok']);
  }
  $('#al-probes').innerHTML = `<table class="tbl"><thead><tr>
    <th>${T('探测项')}</th><th>${T('当前值')}</th></tr></thead><tbody>${rows.map(([a, b, c]) =>
      `<tr><td>${esc(a)}</td><td>${b}</td></tr>`).join('')}</tbody></table>`;
}

function alRenderHist(items) {
  $('#al-hcnt').textContent = (items || []).length + ` ${T('条')}`;
  if (!items || !items.length) {
    $('#al-hist').innerHTML = `<p class="desc">${T('还没有任何告警记录。')}`
      + T('这说明要么一切正常，要么告警没启用 —— 看上面的总开关。</p>');
    return;
  }
  $('#al-hist').innerHTML = `<table class="tbl">
    <thead><tr><th>${T('时间')}</th><th>${T('级别')}</th><th>${T('内容')}</th><th>${T('推送')}</th></tr></thead>
    <tbody>${items.map(x => {
      const lv = AL_LV()[x.lv] || { n: x.lv, c: 'gray' };
      return `<tr>
        <td class="mono" style="font-size:12px">${esc(x.ts || '')}</td>
        <td><span class="tag ${lv.c}">${esc(lv.n)}</span></td>
        <td>${esc(x.msg_cn || '')}</td>
        <td>${x.key === 'test' ? `<span class="tag info">${T('测试')}</span>`
          : x.ok ? `<span class="tag ok">${T('已送达')}</span>`
          : `<span class="tag err">${T('失败')}</span>`}
          ${(x.errs || []).length ? '<br><span class="desc" style="font-size:11px">'
            + esc((x.errs || []).join('；')) + '</span>' : ''}</td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

async function alLoad(toastIt) {
  const r = await api('/api/alert', { method: 'GET' });
  if (!r.ok) {
    $('#al-top').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  AL_DATA = r.data || {};
  alRenderTop(AL_DATA);
  alRenderChannels(AL_DATA);
  alRenderRules(AL_DATA);
  alRenderPol(AL_DATA);
  alRenderProbes((AL_DATA.state || {}).probes);
  alRenderHist(AL_DATA.history);
  const st = (AL_DATA.state || {}).ts;
  $('#al-probe-ts').textContent = st
    ? T('数据来自 ') + new Date(st * 1000).toLocaleString('zh-CN') : '';
  if (toastIt) toast(T('已重新读取'), 'ok');
}

function alChPayload() {
  // 把表单里的开关与输入框收集回通道数组
  const base = ((AL_DATA && AL_DATA.conf && AL_DATA.conf.channels) || []).map(c =>
    Object.assign({}, c));
  $$('[data-al-en]').forEach(el => {
    const i = Number(el.dataset.alEn);
    if (base[i]) base[i].enabled = el.checked;
  });
  $$('[data-al-c]').forEach(el => {
    const i = Number(el.dataset.alC);
    const k = el.dataset.alK;
    if (base[i]) base[i][k] = el.value;
  });
  return base;
}

function alAddChannel(type) {
  if (!AL_DATA) return;
  const chs = alChPayload();
  const names = { bark: T('手机推送'), smtp: T('邮件通知'), webhook: T('群机器人') };
  chs.push({ type, name: names[type] || type, enabled: true, url: '',
             host: '', port: type === 'smtp' ? 465 : '', user: '', pass: '',
             from: '', to: '', tls: true });
  AL_DATA.conf.channels = chs;
  alRenderChannels(AL_DATA);
  toast(T('已添加「') + (names[type] || type) + T('」，填完内容记得点「保存设置」'), 'ok', 6000);
}

function alRmChannel(i) {
  const chs = alChPayload();
  const c = chs[i];
  if (!c) return;
  const nm = c.name || c.type;
  chs.splice(i, 1);
  AL_DATA.conf.channels = chs;
  alRenderChannels(AL_DATA);
  toast(T('已移除「') + nm + T('」，点「保存设置」后生效'), 'ok', 5000);
}

async function alSave() {
  const rules = {};
  $$('[data-al-r]').forEach(el => { rules[el.dataset.alR] = el.value; });
  const conf = {
    enabled: !!(($('#al-enabled') || {}).checked),
    interval_sec: ($('#al-interval') || {}).value,
    cooldown_min: ($('#al-cooldown') || {}).value,
    quiet_from: ($('#al-qfrom') || {}).value || '',
    quiet_to: ($('#al-qto') || {}).value || '',
    rules,
    channels: alChPayload(),
  };
  const r = await api('/api/alert', { method: 'POST', body: { op: 'conf', conf } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
  if (r.ok) {
    if (conf.enabled && r.data && r.data.timer_applied === false) {
      toast(T('设置已保存，但定时器未成功启用，请看页面顶部的状态'), 'warn', 10000);
    }
    alLoad(false);
  }
}

async function alRun() {
  const box = $('#al-run-out');
  box.classList.remove('hidden');
  box.innerHTML = `<p class="desc">${T('正在检测…（含一次到网关的 ping，约 3 秒）')}</p>`;
  const r = await api('/api/alert', { method: 'POST', body: { op: 'run', force: true } },
    );
  if (!r.ok) {
    box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('检测失败'))}</div>`;
    return;
  }
  const d = r.data || {};
  const hits = d.hits || [];
  const skipped = d.skipped || [];
  box.innerHTML = `
    <div class="notice ${hits.length ? 'warn' : 'ok'}">${esc(r.msg_cn || T('检测完成'))}</div>
    ${hits.length ? '<table class="tbl" style="margin-top:8px"><thead><tr>'
      + `<th>${T('规则')}</th><th>${T('级别')}</th><th>${T('命中内容')}</th></tr></thead><tbody>`
      + hits.map(h => {
        const lv = AL_LV()[h.lv] || { n: h.lv, c: 'gray' };
        return '<tr><td>' + esc(h.key) + '</td><td><span class="tag ' + lv.c + '">'
          + esc(lv.n) + '</span></td><td>' + esc(h.msg_cn) + '</td></tr>';
      }).join('') + '</tbody></table>' : ''}
    ${skipped.length ? `<details style="margin-top:8px"><summary>${T('被跳过的')} `
      + skipped.length + ` ${T('条')}</summary><ul class="tight">`
      + skipped.map(s => '<li>' + esc(s.msg_cn) + ' —— ' + esc(s.why)
        + '</li>').join('') + '</ul></details>' : ''}
    <p class="desc" style="margin-top:8px">${T('提示：这里勾了「立即检测」会绕过冷却期强制推送， 用来验证通道是否真的通。日常检测不需要这么做。')}</p>`;
  alLoad(false);
}

async function alTest(type) {
  const box = $('#al-test-out');
  box.classList.remove('hidden');
  box.innerHTML = `<p class="desc">${T('正在发送测试消息…')}</p>`;
  const r = await api('/api/alert', { method: 'POST', body: { op: 'test', type: type || '' } });
  const d = r.data || {};
  const rows = (d.results || []).map(x => `<tr>
    <td>${esc(x.name)}</td>
    <td>${x.ok ? `<span class="tag ok">${T('成功')}</span>` : `<span class="tag err">${T('失败')}</span>`}</td>
    <td class="desc">${esc(x.msg_cn || '')}</td></tr>`).join('');
  box.innerHTML = `<div class="notice ${r.ok ? 'ok' : 'err'}">${esc(r.msg_cn || '')}</div>
    ${rows ? `<table class="tbl" style="margin-top:8px"><thead><tr><th>${T('通道')}</th>`
      + `<th>${T('结果')}</th><th>${T('说明')}</th></tr></thead><tbody>` + rows + '</tbody></table>' : ''}
    ${r.ok ? '' : `<p class="desc" style="margin-top:8px">${T('通道配置没保存也会导致发送失败')} —— `
      + `${T('加完通道记得点一次「保存设置」。')}</p>`}`;
}

function alClear() {
  modal(T('清空告警历史'),
    `<p>${T('确定清空全部告警历史吗？')}</p>
     <p class="desc">${T('同时会清掉每条规则的冷却状态，所以清完之后下一次命中会立刻推送 （而不是继续等冷却期走完）。')}</p>`,
    async () => {
      const r = await api('/api/alert', { method: 'POST', body: { op: 'clear' } });
      toast(r.msg_cn, r.ok ? 'ok' : 'err');
      if (r.ok) alLoad(false);
    }, T('清空'));
}

/* ============ WireGuard VPN（1.0.7） ============ */
let VPN_DATA = null;

async function viewVpn() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('远程连回家里')}
        <button class="ghost small" id="vp-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">${T('人在外面想回内网拿文件、访问家里的')} NAS${T('，用它。')}
        Debian 13 ${T('的内核')}<b>${T('自带')} WireGuard</b>${T('，不需要装任何第三方内核模块， 性能也远好于')} OpenVPN。</p>
      <p class="desc">${T('工作方式是：给每台设备（手机')} / ${T('笔记本）生成一份独立配置， 导入对方的')} WireGuard ${T('客户端')} App${T('，连上之后就能像在家一样访问内网地址。')}</p>
      <div id="vp-top"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('服务端设置')}</h3>
      <p class="desc">${T('保存只写配置文件，')}<b>${T('不会启动服务')}</b>${T('；点「保存并应用」才会真正生效。 改动这两个动作是分开的，和其它模块的「保存')} / ${T('生效」一致。')}</p>
      <div id="vp-conf"><p class="desc">${T('正在读取')}…</p></div>
      <div id="vp-warn"></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="vp-apply">${T('保存并应用')}</button>
        <button class="ghost" id="vp-save">${T('仅保存')}</button>
        <button class="ghost" id="vp-stop">${T('停止服务')}</button>
      </div>
      <div id="vp-out" class="hidden" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <h3>${T('客户端设备')}
        <span class="tag gray" id="vp-pcnt"></span></h3>
      <p class="desc">${T('每台设备一套独立密钥。删除某台设备等于吊销它的访问权限')} ——
        ${T('手机丢了就删掉那一条，比改密码快得多。')}</p>
      <div id="vp-peers"><p class="desc">${T('正在读取')}…</p></div>
      <div class="row" style="margin-top:12px">
        <input type="text" id="vp-new-name" placeholder="${T('设备名称，比如「我的手机」')}"
          style="flex:1;min-width:200px">
        <button class="primary" id="vp-add">${T('添加设备')}</button>
      </div>
    </div>

    <div class="card">
      <h3>${T('连接状态')}</h3>
      <p class="desc">${T('握手时间是很久以前（比如几个月前）说明这台设备很久没连了， 或者配置已经失效。')}</p>
      <div id="vp-live"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('危险操作')}</h3>
      <p class="desc">${T('删除全部')} VPN ${T('配置、密钥与放行规则。所有人立刻连不上。')}</p>
      <button class="danger" id="vp-wipe">${T('删除全部')} VPN ${T('配置')}</button>
    </div>`;
  $('#vp-reload').onclick = () => vpnLoad(true);
  $('#vp-save').onclick = () => vpnSave(false);
  $('#vp-apply').onclick = () => vpnSave(true);
  $('#vp-stop').onclick = vpnStop;
  $('#vp-add').onclick = vpnAddPeer;
  $('#vp-wipe').onclick = vpnWipe;
  vpnLoad(false);
}

function vpnRenderTop(d) {
  const c = d.conf || {};
  const live = d.live || {};
  // 后端给的是四态诊断（state/mod_loaded/mod_file/tool/kernel/
  // virt/autoload/fixable）。老版本后端只给 installed 字符串，
  // **那时候两态信息已经丢了**，所以这里一律映射成 unknown ——
  // 绝不能把旧的 'no' 直接当成 unsupported：旧判据分不清
  // 「模块没加载」和「内核真没模块」，猜错就是本轮修的那个坑。
  const env0 = d.env || { state: 'unknown', kernel: '', virt: '',
    autoload: false };
  let env = '';
  // ⚠️ 这段文案早先只有两档，其中一档还写死了「通常说明跑在
  // 精简容器里」—— 那是**猜**的，而且在 PVE 的 KVM 虚拟机上
  // 明确说错了（systemd-detect-virt 返回 kvm，是虚拟机不是容器）。
  // 真正的原因是「模块文件在，只是没加载」。所以现在按后端给的
  // 四态各说各话，每一档都给出**能做什么**，不猜运行环境。
  if (env0.state === 'unsupported') {
    env = `<div class="notice err">${T('本机内核')} <span class="mono">`
      + esc(env0.kernel || '') + T('</span> 里没有 wireguard 模块，无法启动服务。')
      + `<span class="mono">${T('精简容器镜像和自编内核会裁掉它；')}`
      + T('Debian 官方内核与 PVE / KVM 虚拟机都自带。</span></div>');
  } else if (env0.state === 'need_module') {
    env = `<div class="notice warn">${T('内核里有 wireguard 模块，但当前')}`
      + T('<b>还没加载</b>，所以服务起不来。')
      + '<span class="mono">' + esc(env0.kernel || '')
      + T('/kernel/drivers/net/wireguard</span> 下的模块文件是在的，')
      + T('点下面的按钮加载一下即可（本地操作，几秒钟）。</div>')
      + '<div class="row" style="margin-top:8px">'
      + `<button class="primary" id="vp-fix">${T('一键修复（加载模块并配置开机自启）')}</button>`
      + '</div>';
  } else if (env0.state === 'need_tool') {
    env = `<div class="notice warn">${T('内核模块已加载，但缺少')} `
      + `<span class="mono">wireguard-tools</span>${T('（提供 wg / wg-quick 命令），')}`
      + T('无法启动服务。点下面的按钮自动安装。</div>')
      + '<div class="row" style="margin-top:8px">'
      + `<button class="primary" id="vp-fix">${T('一键修复（安装 wireguard-tools）')}</button>`
      + '</div>';
  } else if (env0.state === 'ready' && !env0.autoload) {
    env = `<div class="notice warn">WireGuard ${T('可用，但')}<b>${T('没有配置开机自动加载')}</b>。`
      + T('重启后模块不会被加载，VPN 会自己起不来。')
      + `<span class="mono">${T('Debian 走 udev 按需加载，/etc/modules 默认不写它。')}</span></div>`
      + '<div class="row" style="margin-top:8px">'
      + `<button class="primary" id="vp-fix">${T('配置开机自动加载')}</button>`
      + '</div>';
  } else if (env0.state === 'unknown') {
    env = `<div class="notice warn">${T('后端版本较旧，')}`
      + T('无法诊断 WireGuard 环境（缺少内核模块 / 工具的分项检测）。')
      + T('升级到 1.0.8 后本页会明确告诉你是模块没加载、工具没装，')
      + T('还是内核真不支持，并提供一键修复。</div>');
  } else if (!d.has_systemd) {
    env = `<div class="notice warn">${T('当前环境没有 systemd（容器形态）。')}`
      + T('服务会由 drouter 直接管理接口，功能可用，但不能用 ')
      + `<span class="mono">systemctl</span> ${T('查看状态。')}</div>`;
  }
  $('#vp-top').innerHTML = env + `<table class="kv">
    <tr><td>${T('服务状态')}</td><td>${live.up
      ? `<span class="tag ok">${T('运行中')}</span>` : `<span class="tag gray">${T('未启动')}</span>`}</td></tr>
    <tr><td>${T('运行环境')}</td><td>${env0.state === 'ready'
      ? `<span class="tag ok">${T('WireGuard 就绪')}</span>`
      : env0.state === 'unknown' ? `<span class="tag gray">${T('未检测')}</span>`
      : '<span class="tag err">'
        + esc({ need_module: T('模块未加载'), need_tool: T('缺少工具'),
                unsupported: T('内核无模块') }[env0.state] || env0.state)
        + '</span>'}<span class="desc" style="margin-left:8px">${T('内核')} ${esc(
          env0.kernel || '')}${env0.virt ? ' · ' + esc(env0.virt) : ''} · ${
          env0.autoload ? T('已配置开机自启')
            : (env0.state === 'ready' ? T('未配置开机自启') : '')}</span></td></tr>
    <tr><td>${T('配置开关')}</td><td>${c.enabled
      ? `<span class="tag ok">${T('已开启')}</span>` : `<span class="tag gray">${T('已关闭')}</span>`}</td></tr>
    <tr><td>${T('监听端口')}</td><td class="mono">UDP ${c.port || T('未设置')}${
      d.port_busy ? ` <span class="tag warn">${T('该端口当前被其他程序占用')}</span>` : ''}</td></tr>
    <tr><td>${T('地址池')}</td><td class="mono">${esc(c.pool || '')}${
      (d.pool_conflict || []).length
        ? ` <span class="tag err">${T('与内网网段')} ` + esc(d.pool_conflict.join('、'))
          + ` ${T('重叠，会导致路由冲突')}</span>` : ''}</td></tr>
    <tr><td>${T('本机内网网段')}</td><td class="mono">${esc((d.lan_nets || []).join('、') || T('未识别'))}</td></tr>
    <tr><td>${T('配置文件')}</td><td class="mono" style="font-size:11px">${esc(d.path || '')}</td></tr>
  </table>`;
  const fx = $('#vp-fix');
  if (fx) fx.onclick = vpnFix;
}

function vpnRenderConf(d) {
  const c = d.conf || {};
  const sw = (id, on) =>
    `<label class="switch"><input type="checkbox" id="${id}"${on ? ' checked' : ''}><i></i></label>`;
  $('#vp-conf').innerHTML = `
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px">
      <span>${T('启用')} VPN ${T('服务端')}</span>${sw('vp-enabled', c.enabled)}
    </div>
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <span>${T('监听端口')}</span>
      <input type="number" id="vp-port" min="1" max="65535" value="${esc(c.port || 51820)}" style="width:110px">
      <span class="desc">UDP ${T('端口。占用时会自动换一个（换完会明确告诉你）')}</span>
    </div>
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <span>${T('客户端地址池')}</span>
      <input type="text" id="vp-pool" value="${esc(c.pool || '')}" style="width:170px">
      <span class="desc">${T('必须是私有网段，且不能和内网网段重叠')}</span>
    </div>
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <span>${T('对外地址')} (Endpoint)</span>
      <input type="text" id="vp-ep" value="${esc(c.endpoint_host || '')}"
        placeholder="${T('公网')} IP ${T('或 DDNS')} ${T('域名')}" style="flex:1;min-width:200px">
      <button class="ghost small" id="vp-ep-guess">${T('检测可用地址')}</button>
    </div>
    <p class="desc" id="vp-ep-hint" style="margin:-4px 0 10px"></p>
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <span>${T('保活间隔')}</span>
      <input type="number" id="vp-ka" min="0" max="300" value="${esc(c.keepalive || 25)}" style="width:90px">
      <span class="desc">${T('秒。NAT')} ${T('后面的设备需要它才能保持在线，')}0 ${T('表示关闭')}</span>
    </div>
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px">
      <span>${T('下发给客户端的')} DNS</span>
      <input type="text" id="vp-dns" value="${esc(c.dns || '')}"
        placeholder="${T('留空则用客户端自己的')}" style="width:200px">
    </div>
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px">
      <span>${T('全部流量经过本机（')}Exit Node）</span>${sw('vp-exit', c.exit_node)}
    </div>
    <p class="desc" style="color:var(--warn)">
      ${T('打开这一项，客户端的所有上网流量都会从你家出去再出去（按两倍流量计费）， 通常只在「人在国外需要用家里的宽带」时才开。')}</p>`;
  const b = $('#vp-ep-guess');
  if (b) b.onclick = vpnGuessEp;
}

function vpnRenderPeers(d) {
  const ps = (d.conf && d.conf.peers) || [];
  const live = d.live || {};
  const byIp = {};
  (live.peers || []).forEach(x => { byIp[(x.allowed || '').replace('/32', '')] = x; });
  $('#vp-pcnt').textContent = ps.length + ` ${T('台')}`;
  if (!ps.length) {
    $('#vp-peers').innerHTML = `<p class="desc">${T('还没有客户端。在下面填个设备名称就能创建。')}</p>`;
    return;
  }
  $('#vp-peers').innerHTML = `<table class="tbl">
    <thead><tr><th>${T('设备')}</th><th>${T('地址')}</th><th>${T('状态')}</th><th>${T('最近握手')}</th>
      <th class="nowrap">${T('操作')}</th></tr></thead>
    <tbody>${ps.map(x => {
      const l = byIp[x.ip];
      const hs = l && l.handshake ? l.handshake.replace('T', ' ') : '';
      return `<tr>
      <td><b>${esc(x.name)}</b>${x.note ? '<br><span class="desc" style="font-size:11px">'
        + esc(x.note) + '</span>' : ''}</td>
      <td class="mono">${esc(x.ip)}</td>
      <td>${!x.enabled ? `<span class="tag gray">${T('已停用')}</span>`
        : !x.has_key ? `<span class="tag err">${T('缺密钥')}</span>`
        : l ? `<span class="tag ok">${T('已连接')}</span>` : `<span class="tag gray">${T('未连接')}</span>`}</td>
      <td class="mono" style="font-size:11px">${esc(hs || '—')}${
        l ? '<br><span class="desc" style="font-size:10px">↓' + fmtBytes(l.rx)
          + ' ↑' + fmtBytes(l.tx) + '</span>' : ''}</td>
      <td class="nowrap">
        <button class="small" data-vp-cf="${esc(x.id)}">${T('下载配置')}</button>
        <button class="small" data-vp-tg="${esc(x.id)}">${x.enabled ? T('停用') : T('启用')}</button>
        <button class="small danger" data-vp-rm="${esc(x.id)}">${T('删除')}</button>
      </td></tr>`;
    }).join('')}</tbody></table>`;
  $$('[data-vp-cf]').forEach(b => b.onclick = () => vpnPeerConf(b.dataset.vpCf));
  $$('[data-vp-tg]').forEach(b => b.onclick = (e) => vpnPeerToggle(
    b.dataset.vpTg, e.target.textContent === T('启用')));
  $$('[data-vp-rm]').forEach(b => b.onclick = () => vpnPeerDel(b.dataset.vpRm));
}

function vpnRenderLive(d) {
  const live = d.live || {};
  if (!live.up) {
    $('#vp-live').innerHTML = `<p class="desc">${T('服务未启动，暂无连接信息。')}</p>`;
    return;
  }
  $('#vp-live').innerHTML = `<table class="kv">
    <tr><td>${T('接口')}</td><td class="mono">${esc(d.path || '').split('/')[-1]}</td></tr>
    <tr><td>${T('实际监听端口')}</td><td class="mono">UDP ${live.listen_port || T('未知')}</td></tr>
    <tr><td>${T('已连接设备')}</td><td>${(live.peers || []).length} ${T('台')}</td></tr>
  </table>`;
}

async function vpnLoad(toastIt) {
  const r = await api('/api/vpn', { method: 'GET' });
  if (!r.ok) {
    $('#vp-top').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  VPN_DATA = r.data || {};
  vpnRenderTop(VPN_DATA);
  vpnRenderConf(VPN_DATA);
  vpnRenderPeers(VPN_DATA);
  vpnRenderLive(VPN_DATA);
  const warn = $('#vp-warn');
  if ((VPN_DATA.pool_conflict || []).length) {
    warn.innerHTML = `<div class="notice err" style="margin-top:10px">${T('客户端地址池与本机内网网段')}`
      + T('重叠，两套路由会互相打架，表现为「连上了但访问不了内网」。')
      + T('请换一个不重叠的地址池。</div>');
  } else {
    warn.innerHTML = '';
  }
  if (toastIt) toast(T('已重新读取'), 'ok');
}

async function vpnGuessEp() {
  const r = await api('/api/vpn', { method: 'POST', body: { op: 'endpoint' } });
  const box = $('#vp-ep-hint');
  if (!r.ok) { box.textContent = r.msg_cn || T('检测失败'); return; }
  const cs = (r.data && r.data.candidates) || [];
  box.innerHTML = T('候选：') + cs.map(c =>
    `<span class="tag ${c.v ? 'ok' : 'warn'}">${esc(c.n)}${c.v ? '：' + esc(c.v) : ''}</span>`
  ).join(' ');
  const good = cs.find(c => c.v);
  if (good) {
    const e = $('#vp-ep');
    if (e && !e.value) e.value = good.v;
    box.innerHTML += ` <span class="desc">${T('（已填入第一个可用候选，可手动改）')}</span>`;
  }
}

function vpnCollect() {
  const c = VPN_DATA && VPN_DATA.conf ? VPN_DATA.conf : {};
  const g = id => { const e = $('#' + id); return e ? e.value : ''; };
  return {
    enabled: !!(($('#vp-enabled') || {}).checked),
    port: g('vp-port'),
    pool: g('vp-pool'),
    endpoint_host: g('vp-ep'),
    keepalive: g('vp-ka'),
    dns: g('vp-dns'),
    exit_node: !!(($('#vp-exit') || {}).checked),
    lan_allow: c.lan_allow !== false,
  };
}

async function vpnSave(apply) {
  const conf = vpnCollect();
  const box = $('#vp-out');
  box.classList.remove('hidden');
  if (conf.exit_node && apply
      && !confirm(T('确定要启用「全部流量经过本机」吗？\\n\\n')
        + T('开启后，每台连上来的设备的所有上网流量都会从你家出去再出去，')
        + T('会消耗双倍带宽，也会被上游运营商看到。')
        + T('只在「人在国外、需要借用家里的宽带」时才该开。'))) return;
  box.innerHTML = `<p class="desc">${T('正在保存…')}</p>`;
  let r = await api('/api/vpn', { method: 'POST', body: { op: 'save', conf } });
  if (!r.ok) {
    box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('保存失败'))}</div>`;
    toast(r.msg_cn || T('保存失败'), 'err', 8000);
    return;
  }
  if (r.data && r.data.port_swapped) {
    box.innerHTML += `<div class="notice warn" style="margin-top:8px">${
      esc(r.msg_cn || '')}</div>`;
  }
  if (!apply) {
    box.innerHTML += `<div class="notice ok" style="margin-top:8px">${
      esc(r.msg_cn || T('已保存'))}</div>
      <p class="desc" style="margin-top:6px">${T('配置已写入但服务未启动。 点「保存并应用」才会真正开始监听。')}</p>`;
    toast(r.msg_cn || T('已保存'), 'ok');
    vpnLoad(false);
    return;
  }
  box.innerHTML += `<p class="desc" style="margin-top:8px">${T('正在启动服务…')}</p>`;
  r = await api('/api/vpn', {
    method: 'POST',
    body: { op: 'apply', conf, confirm_exit: conf.exit_node ? true : undefined }
  });
  if (!r.ok) {
    const j = (r.data && r.data.journal) || '';
    box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('启动失败'))}</div>
      ${j ? `<details style="margin-top:8px"><summary>${T('查看服务日志')}</summary>`
        + '<pre class="mono" style="font-size:11px;white-space:pre-wrap">'
        + esc(j) + '</pre></details>' : ''}`;
    toast(r.msg_cn || T('启动失败'), 'err', 10000);
    return;
  }
  box.innerHTML = `<div class="notice ok">${esc(r.msg_cn || T('已应用'))}</div>`;
  toast(r.msg_cn || T('已应用'), 'ok', 8000);
  vpnLoad(false);
}

async function vpnStop() {
  const r = await api('/api/vpn', { method: 'POST', body: { op: 'stop' } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err');
  if (r.ok) vpnLoad(false);
}

async function vpnFix() {
  // 会装软件包，必须先说清代价：要走 apt、可能要下载几百 KB。
  if (!confirm(T('一键修复会做这几件事：\\n\\n')
    + T('1. modprobe wireguard（加载内核模块，本地操作）\\n')
    + T('2. 写 /etc/modules-load.d/drouter-wireguard.conf（开机自动加载）\\n')
    + T('3. 如果还没装，apt-get install wireguard-tools（需要联网下载）\\n\\n')
    + T('不会改动你的网络接口、防火墙和现有 VPN 配置。\\n要继续吗？'))) return;
  const box = $('#vp-out');
  box.classList.remove('hidden');
  box.innerHTML = `<p class="desc">${T('正在修复 WireGuard 环境')}…`
    + `<br><span class="desc">${T('装包时可能要等十几秒。')}</span></p>`;
  const r = await api('/api/vpn', { method: 'POST', body: { op: 'fix' } });
  const d = r.data || {};
  const notes = d.notes || [];
  if (!r.ok) {
    box.innerHTML = `<div class="notice err">${esc(r.msg_cn || T('修复失败'))}</div>`
      + (notes.length ? '<ul class="desc" style="margin-top:6px">'
        + notes.map(x => `<li>${esc(x)}</li>`).join('') + '</ul>' : '');
    toast(r.msg_cn || T('修复失败'), 'err', 9000);
    vpnLoad(false);
    return;
  }
  box.innerHTML = `<div class="notice ok">${esc(r.msg_cn || T('修复完成'))}</div>`;
  toast(r.msg_cn || T('修复完成'), 'ok', 8000);
  vpnLoad(false);
}

async function vpnAddPeer() {
  const e = $('#vp-new-name');
  const name = e ? e.value.trim() : '';
  if (!name) { toast(T('请先填写设备名称'), 'warn'); return; }
  const r = await api('/api/vpn', { method: 'POST', body: { op: 'peer_add', name } });
  if (!r.ok) { toast(r.msg_cn || T('创建失败'), 'err', 8000); return; }
  const d = r.data || {};
  if (e) e.value = '';
  // 私钥只在这里出现一次，必须让用户当场下载
  const conf = d.client_conf || '';
  modal(T('客户端已创建 —— 请立即下载配置'),
    `<p>${T('设备')} <b>${esc((d.peer || {}).name || '')}</b> ${T('已分配地址')}
      <span class="mono">${esc((d.peer || {}).ip || '')}</span>。</p>
     <p class="desc">${T('下面这份配置')}<b>${T('含私钥')}</b>${T('，只显示这一次。关掉这个窗口就 只能重新下载（私钥仍存在机器上，但你自己看不到它）。')}</p>
     <p class="desc" style="margin-top:8px"><b>${T('怎么用：')}</b></p>
     <ol class="tight" style="margin:6px 0 0 18px">
       <li>${T('手机装')} WireGuard ${T('官方')} App（App Store / ${T('应用商店搜')} WireGuard）</li>
       <li>${T('点「+」')}→${T('「从文件或二维码导入」')}</li>
       <li>${T('选下面这份')} <span class="mono">${esc(d.config_name || '')}</span></li>
       <li>${T('连上后就能直接访问内网地址了')}</li>
     </ol>
     <pre class="mono" style="font-size:11px;white-space:pre-wrap;max-height:220px;
       overflow:auto;background:var(--bg2);padding:10px;border-radius:8px;margin-top:10px"
       >${esc(conf) || T('（服务端密钥尚未生成，请先点一次「保存并应用」）')}</pre>`,
    // onOk 放「下载」：这样窗口右上角的取消和这个按钮都能拿到配置。
    // 传 null 会让用户只能自己复制粘贴，容易漏掉第 3 行 AllowedIPs。
    () => vpnDownloadConf(d.config_name, conf), T('下载配置文件'));
  const box = $('#vp-out');
  box.classList.remove('hidden');
  box.innerHTML = `<div class="notice ok">${T('设备已创建。配置含私钥， 请点上方窗口里的「下载配置文件」保存到本地。')}</div>
    <div class="row" style="margin-top:10px">
      <button class="primary" id="vp-dl-conf">${T('下载配置文件')}</button>
    </div>`;
  const b = $('#vp-dl-conf');
  if (b) b.onclick = () => vpnDownloadConf(d.config_name, conf);
  vpnLoad(false);
}

function vpnDownloadConf(name, text) {
  const blob = new Blob([text || ''], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name || 'drouter-wg.conf';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  toast(T('已开始下载 ') + (name || T('配置文件')), 'ok');
}

async function vpnPeerConf(id) {
  const r = await api('/api/vpn', { method: 'POST', body: { op: 'peer_conf', id } });
  if (!r.ok) { toast(r.msg_cn || T('生成失败'), 'err', 8000); return; }
  const d = r.data || {};
  modal(T('客户端配置（') + id + '）',
    `<p class="desc"><b>${T('含私钥，请妥善保管')}</b>${T('，不要发到群里。')}</p>
     <pre class="mono" style="font-size:11px;white-space:pre-wrap;max-height:320px;
       overflow:auto;background:var(--bg2);padding:10px;border-radius:8px;margin-top:8px"
     >${esc(d.client_conf || '')}</pre>`,
    () => vpnDownloadConf(d.config_name, d.client_conf), T('下载'));
}

async function vpnPeerToggle(id, on) {
  const r = await api('/api/vpn', { method: 'POST', body: { op: 'peer_toggle', id, enabled: on } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err');
  if (r.ok) vpnLoad(false);
}

function vpnPeerDel(id) {
  modal(T('删除客户端'),
    `<p>${T('确定删除设备')} <b class="mono">${esc(id)}</b> ${T('吗？')}</p>
     <p class="desc">${T('它的密钥会被清除，')}<b>${T('已导入该配置的设备立刻连不上')}</b>${T('。 手机丢了就删掉对应那一条')} —— ${T('这是最快的吊销方式。')}</p>`,
    async () => {
      const r = await api('/api/vpn', { method: 'POST', body: { op: 'peer_del', id } });
      toast(r.msg_cn, r.ok ? 'ok' : 'err');
      if (r.ok) vpnLoad(false);
    }, T('删除'));
}

function vpnWipe() {
  modal(T('删除全部 VPN 配置'),
    `<p class="desc" style="color:var(--err)">${T('这会删除服务端密钥、全部客户端配置 和防火墙放行规则。')}</p>
     <p>${T('所有已导入配置的设备')}<b>${T('立刻连不上')}</b>${T('，需要重新逐台创建。')}</p>`,
    async () => {
      const r = await api('/api/vpn', { method: 'POST', body: { op: 'delete_all' } });
      toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
      if (r.ok) vpnLoad(false);
    }, T('确认删除'));
}

/* ============ 用量统计（1.0.7） ============ */
let QT_DATA = null;
let QT_REPORT = null;
let QT_MONTH = '';

async function viewQuota() {
  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('谁用了多少流量')}
        <button class="ghost small" id="qt-reload" style="float:right">${T('重新读取')}</button></h3>
      <p class="desc">${T('按设备统计这个月用了多少流量、按什么服务用掉的。 工作室场景下还能按预设比例把话费分摊到人头或机器上。')}</p>
      <p class="desc">${T('数据来源是「连接与流日志」里的连接跟踪事件， 由后台每')} 10 ${T('分钟聚合一次。这里点查询')}<b>${T('不会')}</b>${T('去扫原始日志 （那会有几百')} MB${T('，在 4GB')} ${T('内存的机器上会直接把进程打死）。')}</p>
      <div id="qt-top"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('本月用量排行')}
        <span class="tag gray" id="qt-month"></span>
        <select id="qt-mpick" style="float:right;max-width:180px">
          <option value="">${T('选择月份')}…</option>
        </select></h3>
      <div id="qt-devs"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('按服务分类')}</h3>
      <p class="desc">${T('看看流量主要花在什么上。视频流、网盘同步、')}Windows ${T('更新 通常是前几名。')}</p>
      <div id="qt-svc"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('每日趋势')}</h3>
      <div id="qt-days"><p class="desc">${T('正在读取')}…</p></div>
    </div>

    <div class="card">
      <h3>${T('套餐与费用分摊')}</h3>
      <p class="desc">${T('填了套餐流量和月租之后，下面会算出总费用和两种分摊口径。 「按用量分摊」是各设备实际占比；「按预设比例分摊」是你给每台设备 指定应承担的百分比（老板按人头、机器按台数这种）。')}</p>
      <div id="qt-bill-cfg"><p class="desc">${T('正在读取')}…</p></div>
      <div id="qt-bill" style="margin-top:12px"></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="qt-save">${T('保存设置')}</button>
        <button class="ghost" id="qt-agg">${T('立即聚合一次')}</button>
      </div>
    </div>

    <div class="card">
      <h3>${T('数据维护')}
        <button class="ghost small danger" id="qt-reset" style="float:right">${T('清空全部统计')}</button></h3>
      <p class="desc">${T('清空后从下一轮聚合重新开始，历史数据无法恢复。')}</p>
    </div>`;
  $('#qt-reload').onclick = () => qtLoad(true);
  $('#qt-save').onclick = qtSave;
  $('#qt-agg').onclick = qtAggregate;
  $('#qt-reset').onclick = qtReset;
  $('#qt-mpick').onchange = e => { QT_MONTH = e.target.value; qtReport(); };
  qtLoad(false);
}

function qtRenderTop(d) {
  const c = d.conf || {};
  const u = d.units || {};
  const tv = u['drouter-quotad.timer'] && (u['drouter-quotad.timer'].active
    !== undefined ? u['drouter-quotad.timer'].active : u['drouter-quotad.timer']);
  const lag = d.lag_bytes;
  let env = '';
  if (!d.has_archive) {
    env = `<div class="notice warn">${T('还没找到统一日志归档文件。')}`
      + T('请先到「日志与审计 → 连接与流日志」确认统一日志已启用，')
      + T('否则这里永远不会有数据。</div>');
  } else if (!c.enabled) {
    env = `<div class="notice warn">${T('自动聚合未启用，新流量不会被统计。')}`
      + T('在下面勾上「启用自动聚合」并保存。</div>');
  } else if (tv !== 'active') {
    env = `<div class="notice warn">${T('自动聚合已开启但定时器没在跑。')}`
      + T('点一次「保存设置」会重写单元并尝试启用。</div>');
  }
  const fresh = lag == null ? T('未知')
    : lag < 1048576 ? (lag / 1024).toFixed(0) + ' KB'
    : (lag / 1048576).toFixed(1) + ' MB';
  $('#qt-top').innerHTML = env + `<table class="kv">
    <tr><td>${T('聚合定时器')}</td><td>${tv === 'active'
      ? `<span class="tag ok">${T('运行中')}</span>`
      : tv === 'failed' ? `<span class="tag err">${T('失败')}</span>`
      : `<span class="tag gray">${T('未运行')}</span>`}</td></tr>
    <tr><td>${T('聚合间隔')}</td><td>${c.interval_min || 10} ${T('分钟')}</td></tr>
    <tr><td>${T('下次执行')}</td><td class="mono">${esc(d.next || T('未启用'))}</td></tr>
    <tr><td>${T('上次聚合')}</td><td>${d.last && d.last.msg_cn
      ? esc(d.last.msg_cn) + '<br><span class="desc mono" style="font-size:11px">'
        + esc(d.last.ts) + '</span>' : `<span class="desc">${T('暂无记录')}</span>`}</td></tr>
    <tr><td>${T('待聚合数据')}</td><td>${fresh}</td></tr>
    <tr><td>${T('统计库大小')}</td><td>${fmtBytes(d.agg_size || 0)}</td></tr>
  </table>`;
}

function qtBar(pct, color) {
  const w = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div style="background:var(--line);height:8px;border-radius:4px;
    overflow:hidden;min-width:60px">
    <div style="width:${w}%;height:100%;background:${color || 'var(--pri)'}"></div>
  </div>`;
}

function qtRenderReport(r) {
  QT_MONTH = r.month || QT_MONTH;
  $('#qt-month').textContent = r.month || '';
  const pk = $('#qt-mpick');
  if (pk) {
    const ms = r.months || [];
    pk.innerHTML = `<option value="">${T('选择月份…')}</option>` + ms.map(m =>
      `<option value="${esc(m)}"${m === r.month ? ' selected' : ''}>${esc(m)}</option>`
    ).join('');
  }
  if (r.empty) {
    $('#qt-devs').innerHTML = `<div class="notice warn">${esc(r.msg_cn || '')}</div>`;
    $('#qt-svc').innerHTML = `<p class="desc">${T('暂无数据')}</p>`;
    $('#qt-days').innerHTML = `<p class="desc">${T('暂无数据')}</p>`;
    $('#qt-bill').innerHTML = '';
    return;
  }
  // 设备排行
  $('#qt-devs').innerHTML = `
    <div class="notice info" style="margin-bottom:10px">${esc(r.note || '')}</div>
    <table class="tbl"><thead><tr>
      <th>${T('设备')}</th><th>IP</th><th>MAC</th><th>${T('用量')}</th><th>${T('占比')}</th><th>${T('连接数')}</th>
    </tr></thead><tbody>${(r.devices || []).map(d => `<tr data-qt-dev="${esc(d.ip)}"
        style="cursor:pointer">
      <td><b>${esc(d.name)}</b>${d.mac ? '' : ` <span class="tag warn">${T('无租约')}</span>`}</td>
      <td class="mono">${esc(d.ip)}</td>
      <td class="mono" style="font-size:11px">${esc(d.mac || '—')}</td>
      <td>${fmtBytes(d.b)}</td>
      <td style="min-width:90px">${qtBar(d.pct)}<br>
        <span class="desc" style="font-size:11px">${d.pct}%</span></td>
      <td>${d.n}</td>
    </tr>`).join('')}</tbody></table>
    <p class="desc" style="margin-top:8px">${T('点任意一行看这台设备的明细。')}</p>`;
  $$('[data-qt-dev]').forEach(tr => tr.onclick = () => qtDevice(tr.dataset.qtDev));
  // 服务分类
  const sv = r.services || [];
  $('#qt-svc').innerHTML = sv.length ? `<table class="tbl">
    <thead><tr><th>${T('服务')}</th><th>${T('用量')}</th><th>${T('占比')}</th><th>${T('连接数')}</th></tr></thead>
    <tbody>${sv.slice(0, 15).map(s => `<tr>
      <td>${esc(s.svc)}</td><td>${fmtBytes(s.b)}</td>
      <td style="min-width:90px">${qtBar(s.pct)}<br>
        <span class="desc" style="font-size:11px">${s.pct}%</span></td>
      <td>${s.n}</td></tr>`).join('')}</tbody></table>
    ${sv.length > 15 ? `<p class="desc">${T('只显示前 15 项。')}</p>` : ''}`
    : `<p class="desc">${T('暂无数据')}</p>`;
  // 每日趋势
  const ds = r.days || [];
  if (!ds.length) {
    $('#qt-days').innerHTML = `<p class="desc">${T('暂无数据')}</p>`;
  } else {
    const max = Math.max(...ds.map(x => x.b), 1);
    $('#qt-days').innerHTML = `<div style="display:flex;align-items:flex-end;gap:2px;
      height:120px;padding:8px;background:var(--bg2);border-radius:8px;overflow-x:auto">
      ${ds.map(x => `<div title="${esc(x.d)}：${fmtBytes(x.b)}"
        style="flex:1;min-width:8px;height:${Math.max(3, x.b * 100 / max)}%;
        background:var(--pri);border-radius:2px 2px 0 0"></div>`).join('')}
    </div>
    <div class="row" style="justify-content:space-between;margin-top:4px">
      <span class="desc">${esc(ds[0].d)}</span>
      <span class="desc">${esc(ds[ds.length - 1].d)}</span>
    </div>
    <p class="desc" style="margin-top:6px">${T('峰值')} ${fmtBytes(max)}</p>`;
  }
  qtRenderBill(r);
}

function qtRenderBill(r) {
  const b = r.bill || {};
  const c = b.currency || '¥';
  if (!b.price) {
    $('#qt-bill').innerHTML = `<p class="desc">${T('填了「月租」之后这里会算出费用分摊。')}</p>`;
    return;
  }
  const row = x => `<tr><td>${esc(x.name)}</td>
    <td class="mono" style="font-size:11px">${esc(x.ip || '')}</td>
    <td>${x.gb != null ? x.gb + ' GB' : (x.pct != null ? x.pct + '%' : '—')}</td>
    <td><b>${c}${x.money}</b></td></tr>`;
  $('#qt-bill').innerHTML = `
    <table class="kv" style="margin-bottom:12px">
      <tr><td>${T('套餐流量')}</td><td>${b.total_gb || 0} GB</td></tr>
      <tr><td>${T('本月已用')}</td><td>${b.used_gb || 0} GB${
        b.total_gb && b.used_gb > b.total_gb
          ? ` <span class="tag warn">${T('已超出套餐')} ` + (b.used_gb - b.total_gb).toFixed(1)
            + ' GB</span>' : ''}</td></tr>
      <tr><td>${T('本月费用')}</td><td><b style="font-size:16px">${c}${b.price}</b>${
        b.used_gb > b.total_gb ? ` <span class="desc">${T('（含超出部分估算）')}</span>` : ''}</td></tr>
    </table>
    <h4>${T('口径一：按实际用量分摊')}</h4>
    <table class="tbl"><thead><tr><th>${T('设备')}</th><th>IP</th><th>${T('用量')}</th><th>${T('应摊')}</th></tr></thead>
    <tbody>${(b.by_usage || []).map(row).join('')}</tbody></table>
    ${(b.by_alloc || []).length ? `
      <h4 style="margin-top:14px">${T('口径二：按预设比例分摊')}</h4>
      <table class="tbl"><thead><tr><th>${T('设备')}</th><th></th><th>${T('比例')}</th><th>${T('应摊')}</th></tr></thead>
      <tbody>${b.by_alloc.map(row).join('')}</tbody></table>
      <p class="desc" style="margin-top:6px">${T('在下面「分摊比例」里给设备填百分比。 没填的会归到「未指定」。')}</p>` : ''}`;
}

async function qtLoad(toastIt) {
  const r = await api('/api/quota', { method: 'GET' });
  if (!r.ok) {
    $('#qt-top').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('读取失败'))}</div>`;
    return;
  }
  QT_DATA = r.data || {};
  qtRenderTop(QT_DATA);
  qtRenderBillConf(QT_DATA.conf || {});
  await qtReport();
  if (toastIt) toast(T('已重新读取'), 'ok');
}

async function qtReport() {
  const q = QT_MONTH ? '?month=' + encodeURIComponent(QT_MONTH) : '';
  const r = await api('/api/quota' + q, { method: 'GET' });
  if (!r.ok) {
    $('#qt-devs').innerHTML = `<div class="notice err">${esc(r.msg_cn || T('查询失败'))}</div>`;
    return;
  }
  const d = r.data || {};
  if (!QT_MONTH) QT_MONTH = d.month || '';
  QT_REPORT = d;
  qtRenderReport(d);
}

function qtRenderBillConf(c) {
  const sw = (id, on) =>
    `<label class="switch"><input type="checkbox" id="${id}"${on ? ' checked' : ''}><i></i></label>`;
  $('#qt-bill-cfg').innerHTML = `
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px">
      <span>${T('启用自动聚合')}</span>${sw('qt-enabled', c.enabled)}
      <span style="margin-left:16px">${T('聚合间隔')}</span>
      <input type="number" id="qt-int" min="5" max="240" value="${esc(c.interval_min || 10)}"
        style="width:90px"><span class="desc">${T('分钟')}</span>
    </div>
    <div class="row" style="align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <span>${T('套餐流量')}</span>
      <input type="number" id="qt-tgb" min="0" step="1" value="${esc(c.plan_total_gb || 0)}"
        style="width:120px"><span class="desc">GB / ${T('月')}</span>
      <span style="margin-left:16px">${T('月租')}</span>
      <input type="number" id="qt-price" min="0" step="1" value="${esc(c.plan_price || 0)}"
        style="width:110px">
      <input type="text" id="qt-cur" value="${esc(c.currency || '¥')}" style="width:60px">
    </div>
    <div id="qt-alloc"></div>`;
  qtRenderAlloc(c);
}

function qtRenderAlloc(c) {
  const devs = (QT_REPORT && QT_REPORT.devices) || [];
  const allocs = c.alloc || {};
  if (!devs.length) {
    $('#qt-alloc').innerHTML = `<p class="desc">${T('先查一次用量报表，')}`
      + T('这里就能给每台设备填「应承担百分之多少」了。</p>');
    return;
  }
  $('#qt-alloc').innerHTML = `<h4>${T('分摊比例（可选）')}</h4>
    <p class="desc">${T('留空表示按实际用量算。填了数字就按你填的比例分， 适合「人头均摊」「按台数」这种和用量无关的算法。')}</p>
    <table class="tbl"><thead><tr><th>${T('设备')}</th><th>MAC</th><th>${T('应承担')}</th></tr></thead>
    <tbody>${devs.map(d => {
      const key = d.mac || d.ip;
      return `<tr><td>${esc(d.name)}</td>
        <td class="mono" style="font-size:11px">${esc(d.mac || d.ip)}</td>
        <td><input type="number" min="0" max="100" step="0.1"
          data-qt-al="${esc(key)}" value="${esc(allocs[key] || '')}"
          placeholder="${T('按用量')}" style="width:100px"> %</td></tr>`;
    }).join('')}</tbody></table>`;
}

async function qtSave() {
  const g = id => { const e = $('#' + id); return e ? e.value : ''; };
  const alloc = {};
  $$('[data-qt-al]').forEach(el => {
    const v = String(el.value || '').trim();
    if (v !== '') alloc[el.dataset.qtAl] = v;
  });
  const conf = {
    enabled: !!(($('#qt-enabled') || {}).checked),
    interval_min: g('qt-int'),
    plan_total_gb: g('qt-tgb'),
    plan_price: g('qt-price'),
    currency: g('qt-cur') || '¥',
    alloc,
  };
  const r = await api('/api/quota', { method: 'POST', body: { op: 'conf', conf } });
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
  if (r.ok) {
    if (conf.enabled && r.data && r.data.timer_applied === false) {
      toast(T('设置已保存，但定时器未成功启用，请看页面顶部'), 'warn', 10000);
    }
    // 先刷报表拿到最新设备列表，再重画分摊表 ——
    // 否则新出现的设备在这一轮里没法填比例
    await qtReport();
    qtRenderBillConf(QT_DATA.conf || {});
  }
}

async function qtAggregate() {
  toast(T('正在聚合…'), 'ok', 4000);
  const r = await api('/api/quota', { method: 'POST', body: { op: 'aggregate' } },
    );
  toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
  if (r.ok) qtLoad(false);
}

async function qtDevice(ip) {
  const r = await api('/api/quota', {
    method: 'POST', body: { op: 'device', ip, month: QT_MONTH }, timeout: 180000
  });
  if (!r.ok) { toast(r.msg_cn || T('查询失败'), 'err'); return; }
  const d = r.data || {};
  if (d.empty) { toast(d.msg_cn || T('无数据'), 'warn'); return; }
  const max = Math.max(...(d.days || []).map(x => x.b), 1);
  modal(d.name + ' · ' + d.month,
    `<table class="kv" style="margin-bottom:10px">
      <tr><td>IP</td><td class="mono">${esc(d.ip)}</td></tr>
      <tr><td>MAC</td><td class="mono">${esc(d.mac || T('未知'))}</td></tr>
      <tr><td>${T('本月合计')}</td><td><b>${fmtBytes(d.total_b)}</b></td></tr>
    </table>
    <h4>${T('每日')}</h4>
    <div style="display:flex;align-items:flex-end;gap:2px;height:80px;
      padding:6px;background:var(--bg2);border-radius:8px;overflow-x:auto">
      ${(d.days || []).map(x => `<div title="${esc(x.d)}：${fmtBytes(x.b)}"
        style="flex:1;min-width:8px;height:${Math.max(3, x.b * 100 / max)}%;
        background:var(--pri);border-radius:2px 2px 0 0"></div>`).join('')}
    </div>
    <h4 style="margin-top:12px">${T('按服务')}</h4>
    <table class="tbl"><thead><tr><th>${T('服务')}</th><th>${T('用量')}</th><th>${T('占比')}</th></tr></thead>
    <tbody>${(d.services || []).slice(0, 10).map(s => `<tr>
      <td>${esc(s.svc)}</td><td>${fmtBytes(s.b)}</td>
      <td style="min-width:80px">${qtBar(s.pct)}<br>
        <span class="desc" style="font-size:11px">${s.pct}%</span></td>
    </tr>`).join('')}</tbody></table>`, null, T('关闭'));
}

function qtReset() {
  modal(T('清空全部用量统计'),
    `<p>${T('确定清空吗？')}</p>
     <p class="desc">${T('会删除已聚合的历史数据与聚合游标，统计将从零重新开始。 这一步')}<b>${T('无法恢复')}</b>。</p>`,
    async () => {
      const r = await api('/api/quota', { method: 'POST', body: { op: 'reset' } });
      toast(r.msg_cn, r.ok ? 'ok' : 'err');
      if (r.ok) { QT_MONTH = ''; qtLoad(false); }
    }, T('清空'));
}

function ulQuery() {
  const u = ulState();
  return { op: 'query', view: u.view, since: u.since, level: u.level,
    src: u.src, proto: u.proto, action: u.action, q: u.q, limit: u.limit,
    live: !!u.live, live_sec: u.liveSec || 3 };
}

async function viewFlowLog() {
  $('#view').innerHTML = `<div class="card"><h3>${T('连接与流日志')}</h3>
    <p class="desc">${T('正在采集各源日志')}…${T('（防火墙')} / ${T('连接跟踪')} / WAN / DDNS / ${T('应用')} / ${T('系统）')}</p></div>`;
  const u = ulState();
  if (!u.sources.length) {
    const c = await api('/api/ulog/conf');
    if (c.ok) {
      u.conf = (c.data || {}).conf || {};
      u.sources = (c.data || {}).sources || [];
      u.levels = (c.data || {}).levels || [];
      u.archive = (c.data || {}).archive || {};
      u.caps = (c.data || {}).caps || {};
      u.conntrack = (c.data || {}).conntrack || {};
      u.fwLog = (c.data || {}).fw_log;
    }
  }
  await ulLoad();
}

async function ulLoad() {
  const u = ulState();
  if (u.view === 'flow') {
    const r = await api('/api/ulog/flow', { method: 'POST', body: {
      proto: u.proto, q: u.q, limit: 500 } });
    if (!r.ok) { toast(r.msg_cn || T('读取连接表失败'), 'err'); return; }
    u.flow = r.data || {};
    renderFlowLog();
    return;
  }
  const r = await api('/api/ulog', { method: 'POST', body: ulQuery() });
  if (!r.ok) { toast(r.msg_cn || T('读取日志失败'), 'err'); return; }
  const d = r.data || {};
  u.items = d.items || [];
  u.stat = d.stat || {};
  u.meta = d.meta || {};
  u.conf = d.conf || u.conf;
  u.sources = d.sources || u.sources;
  u.levels = d.levels || u.levels;
  u.archive = d.archive || u.archive;
  renderFlowLog();
}

function ulStatChips(stat, map) {
  const e = Object.entries(stat || {});
  if (!e.length) return '<span class="mono" style="color:var(--txt3)">—</span>';
  return e.sort((a, b) => b[1] - a[1]).map(([k, v]) =>
    `<span class="ul-chip"><b>${esc((map || {})[k] || k)}</b> ${v}</span>`).join(' ');
}

function LV_STAT_I18N() {
  return { emerg: T('flowlog.lvlErr'), alert: T('flowlog.lvlErr'),
    crit: T('flowlog.lvlErr'), err: T('flowlog.lvlErr'),
    warn: T('flowlog.lvlWarn'), notice: T('flowlog.lvlNotice'),
    info: T('flowlog.lvlInfo'), debug: T('flowlog.lvlDebug') };
}


// 主题名走 bt4：内置主题 id 稳定，英文表在 i18n.js 的 bt.THEME_NAMES；
// 自定义主题（用户命名）查不到就回落后端中文名。
function tmName(t) {
  return bt4('THEME_NAMES', (t && t.id) || '', 'name', (t && t.name) || '');
}
function renderFlowLog() {
  const u = ulState();
  const isFlow = u.view === 'flow';
  const srcOpt = u.sources.map(s =>
    `<option value="${esc(s.v)}" ${u.src === s.v ? 'selected' : ''}>${esc(bt4('ULOG_SOURCES', s.v, 'n', s.n))}</option>`).join('');
  const lvOpt = u.levels.map(l =>
    `<option value="${esc(l.v)}" ${u.level === l.v ? 'selected' : ''}>${esc(LV_STAT_I18N()[l.v] || l.n)}（${esc(l.v)}）</option>`).join('');
  const sinceOpt = UL_PRESET_SINCE().map(p =>
    `<option value="${esc(p.v)}" ${u.since === p.v ? 'selected' : ''}>${esc(p.n)}</option>`).join('');

  const items = u.items || [];
  const st = u.stat || {};

  $('#view').innerHTML = `
    <div class="card">
      <h3>${T('连接与流日志')}
        <span class="tag ${u.conf.enabled ? 'ok' : 'gray'}" style="float:right">
          ${T('统一日志')} ${u.conf.enabled ? T('已开启') : T('已关闭')}</span></h3>
      <p class="desc">${T('把防火墙、连接跟踪、')}WAN ${T('接入、')}DDNS${T('、应用与系统日志')}
      <b>${T('规范化为同一种记录结构')}</b>${T('，可用同一套条件检索、统计与导出。 归档文件：')}<span class="mono">${esc((u.archive || {}).path || '/var/log/drouter/ulog.jsonl')}</span>
      （${(u.archive || {}).rows || 0} ${T('条')} · ${fmtBytes((u.archive || {}).size || 0)}）。</p>
      <div class="row" style="align-items:center;gap:8px;flex-wrap:wrap">
        <div class="seg" style="flex:0 0 auto">
          <button data-ulview="log" class="${isFlow ? '' : 'on'}">${T('统一日志')}</button>
          <button data-ulview="flow" class="${isFlow ? 'on' : ''}">${T('当前连接')}</button>
        </div>
        <button class="ghost small fixed" id="ul-refresh">${T('刷新')}</button>
        ${isFlow ? '' : `<label class="switch" title="${T('打开后会订阅 conntrack 实时事件流， 查询会多等几秒以捕捉这段时间内新建的连接。默认关闭以保证查询速度。')}">
          <input type="checkbox" id="ul-live" ${u.live ? 'checked' : ''}><i></i>
          ${T('抓实时连接事件（')}${u.liveSec || 3} ${T('秒）')}</label>`}
        <span class="desc" id="ul-meta" style="margin:0"></span>
      </div>
      ${u.fwLog === false && !isFlow ? `<p class="hint-inline" style="color:var(--warn)">
        ${T('防火墙日志开关当前为关闭：规则里没有 log 语句，因此「防火墙」源不会有新记录。 可到「防火墙 IPv4')} / IPv6${T('」页打开「记录被拒绝的包」。')}</p>` : ''}
    </div>

    <div class="card">
      <h3>${T('筛选与检索')}</h3>
      <div class="row">
        <label>${T('时间范围')}<select id="ul-since" ${isFlow ? 'disabled' : ''}>${sinceOpt}</select></label>
        <label>${T('日志源')}<select id="ul-src">
          <option value="">${T('全部来源')}</option>${srcOpt}</select></label>
        <label>${T('最低级别')}<select id="ul-level" ${isFlow ? 'disabled' : ''}>
          <option value="">${T('不限')}</option>${lvOpt}</select></label>
        <label>${T('协议')}<select id="ul-proto">
          <option value="">${T('全部')}</option>
          ${['TCP', 'UDP', 'ICMP', 'ICMPv6'].map(p =>
    `<option value="${esc(p)}" ${u.proto === p ? 'selected' : ''}>${p}</option>`).join('')}
        </select></label>
        <label>${T('动作')}<input id="ul-action" value="${esc(u.action)}"
          placeholder="DROP / ACCEPT / NEW…" ${isFlow ? 'disabled' : ''}></label>
        <label style="flex:2 1 260px">${T('关键字')}
          <input id="ul-q" value="${esc(u.q)}" placeholder="IP${T('、端口、中文描述')}…"></label>
        <label style="flex:0 0 120px">${T('条数')}
          <input id="ul-limit" type="number" min="20" max="3000" value="${esc(u.limit)}"></label>
        <button class="primary small fixed" id="ul-go">${T('查询')}</button>
        <button class="ghost small fixed" id="ul-reset">${T('重置')}</button>
      </div>
      <p class="hint-inline">${T('「最低级别」按严重程度过滤：选「警告」会同时显示错误与警告。 关键字对中文描述与原始行同时匹配。')}</p>
    </div>

    <div class="card">
      <h3>${T('分布统计')}</h3>
      <div class="ul-stats">
        <div><b>${T('按来源')}</b><div>${ulStatChips(st.by_src, UL_SRC_MAP())}</div></div>
        <div><b>${T('按级别')}</b><div>${ulStatChips(st.by_level)}</div></div>
        <div><b>${T('按动作')}</b><div>${ulStatChips(st.by_action)}</div></div>
        <div><b>${T('按协议')}</b><div>${ulStatChips(st.by_proto)}</div></div>
        <div><b>${T('来源地址')} TOP</b><div>${(st.top_src_ip || []).map(x =>
    `<span class="ul-chip mono">${esc(x.v)} ${x.n}</span>`).join(' ') || '<span class="mono" style="color:var(--txt3)">—</span>'}</div></div>
        <div><b>${T('目的端口')} TOP</b><div>${(st.top_dst_port || []).map(x =>
    `<span class="ul-chip mono">:${esc(x.v)} ${x.n}</span>`).join(' ') || '<span class="mono" style="color:var(--txt3)">—</span>'}</div></div>
      </div>
    </div>

    ${isFlow ? ulFlowTable(u) : ulLogTable(items)}

    <div class="card">
      <h3>${T('保留策略与导出')}</h3>
      <p class="desc">${T('归档会按下面的策略自动清理（由后台定时任务执行）。开启归档后， 即使系统')} journal ${T('轮转，历史连接与流量日志仍可追溯。')}</p>
      <div id="ul-daemon" class="hint-inline"></div>
      <div class="row">
        <label class="switch" style="margin:0"><input type="checkbox" id="ul-enabled"
          ${u.conf.enabled ? 'checked' : ''}><i></i>${T('启用统一日志')}</label>
        <label class="switch" style="margin:0"><input type="checkbox" id="ul-archive"
          ${u.conf.archive ? 'checked' : ''}><i></i>${T('归档到磁盘')}</label>
        <label>${T('保留天数')}<input id="ul-keep-days" type="number" min="1" max="365"
          value="${esc(u.conf.keep_days != null ? u.conf.keep_days : 7)}" style="width:100px"></label>
        <label>${T('最大条数')}<input id="ul-keep-rows" type="number" min="1000"
          value="${esc(u.conf.keep_rows != null ? u.conf.keep_rows : 200000)}" style="width:130px"></label>
        <label>${T('归档最低级别')}<select id="ul-minlevel">
          ${(u.levels || []).map(l => `<option value="${esc(l.v)}"
            ${u.conf.min_level === l.v ? 'selected' : ''}>${esc(LV_STAT_I18N()[l.v] || l.n)}</option>`).join('')}
        </select></label>
      </div>
      <h4 style="margin:14px 0 6px;font-size:13px">${T('采集源开关')}</h4>
      <div class="row" id="ul-srcs">
        ${(u.sources || []).map(s => `<label class="switch" style="margin:0;flex:0 0 auto">
          <input type="checkbox" data-ulsrc="${esc(s.v)}"
            ${((u.conf.sources || {})[s.v]) ? 'checked' : ''}><i></i>${esc(bt4('ULOG_SOURCES', s.v, 'n', s.n))}</label>`).join('')}
      </div>
      <div class="row" style="margin-top:12px">
        <button class="primary small fixed" id="ul-save">${T('保存设置')}</button>
        <button class="ghost small fixed" id="ul-prune">${T('立即清理')}</button>
        <button class="ghost small fixed" id="ul-export-json">${T('导出')} JSON</button>
        <button class="ghost small fixed" id="ul-export-csv">${T('导出')} CSV</button>
        <button class="ghost small fixed danger" id="ul-clear">${T('清空归档')}</button>
      </div>
      <div id="ul-out"></div>
    </div>
  `;
  ulBind();
  ulLoadDaemon();
  const m = u.meta || {};
  const el = $('#ul-meta');
  if (el) {
    el.textContent = isFlow
      ? `${T('连接表')} ${((u.flow || {}).stat || {}).total || 0} ${T('条')}` +
        (((u.flow || {}).cap || {}).max
          ? ` · ${T('内核表')} ${((u.flow || {}).cap || {}).count || 0}/${((u.flow || {}).cap || {}).max}` : '')
      : `${T('采集')} ${m.collected || 0} ${T('条')} · ${T('命中')} ${m.matched || 0} ${T('条')} · ${T('显示')} ${m.shown || 0} ${T('条')}`;
  }
}

function ulLogTable(items) {
  if (!items.length) {
    return `<div class="card"><h3>${T('日志明细')}</h3><p class="hint-inline">`
      + T('当前条件下没有日志。可放宽时间范围或取消筛选条件；')
      + T('若「防火墙」源为空，请检查防火墙日志开关。</p></div>');
  }
  return `<div class="card">
    <h3>${T('日志明细（')}${items.length}）</h3>
    <div class="ul-table">
      <div class="ul-thead"><span>${T('时间')}</span><span>${T('来源')}</span><span>${T('级别')}</span>
        <span>${T('动作')} / ${T('协议')}</span><span>${T('连接')}</span><span>${T('描述')}</span></div>
      ${items.map(ulRow).join('')}
    </div>
  </div>`;
}

// logd/ulog 归档消息按模式翻译（后端零改动）。错误详情等动态内容保留中文原文。

// 审计表翻译：动作查 AUDIT_ACTIONS；详情按模式替换（快照 id / IP / 数量保留原文）。
function audT(a) {
  const v = (typeof bt === 'function') ? bt('AUDIT_ACTIONS', a, 'n') : '';
  return v || a;
}
function audDetail(action, d0) {
  const s = String(d0 == null ? '' : d0);
  if (!(window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US')) return s;
  if (!/[\u4e00-\u9fff]/.test(s)) return s;

  // 预置主题名先按 AUD_THEMES 表翻（audit 详情里只有中文名，没有 id）。
  // ⚠️ 必须最先做：下面的锚定/子串模式会把「」换成 "，之后「」就找不到了。
  let out = s.replace(/[「]([^」]+)[」]/g, (m, n) => {
    const en = (typeof bt4 === 'function') ? bt4('AUD_THEMES', n, 'en', '') : '';
    return en ? '"' + en + '"' : m;
  });

  const pats = [
    [/^已创建配置快照：(.+?)（已上锁，不参与自动清理）$/, 'Snapshot $1 created (locked, excluded from auto-clean)'],
    [/^已创建配置快照：(.+?)$/, 'Snapshot $1 created'],
    [/^(.+?)（受保护，已强制删除）$/, '$1 (protected, force-deleted)'],
    [/^(.+?) 快照 (.+?) 已上锁，自动清理不会删除它$/, '$1 snapshot $2 locked; auto-clean will not remove it'],
    [/^(.+?) 已解除 (.+?) 的保护$/, 'Protection removed from $2 by $1'],
    [/^已清理 (\d+) 份过期快照$/, 'Cleaned $1 expired snapshots'],
    [/^备份包已删除$/, 'Backup pack deleted'],
    [/^备份已导出：(\d+) 个文件（(.+?)）$/, 'Backup exported: $1 files ($2)'],
    [/^已应用主题[「"](.+?)[」"]，刷新页面即可看到效果$/, 'Theme "$1" applied — refresh the page to see it'],
    [/^未知主题操作：(.+)$/, 'Unknown theme action: $1'],
    [/^主题不存在：(.*)$/, 'No such theme: $1'],
    [/^配置已写入，但 VPN 处于关闭状态（未启动服务）$/, 'Config written, but the VPN is off (service not started)'],
    [/^WireGuard 已停止$/, 'WireGuard stopped'],
    [/^救援通道已开启，可通过 (.+?) 访问（插任一口皆可）。请记下访问令牌 (\d+) —— 页面上的还原操作需要它，同网段其它机器无法凭猜测还原你的配置$/,
     'Rescue channel opened at $1 (works through any port). Note the access token $2 — restore actions on the page require it, and other machines on the LAN cannot restore your config by guessing'],
    [/^救援通道已开启，可通过 (.+?) 访问（插任一口皆可）$/, 'Rescue channel opened at $1 (works through any port)'],
    [/^救援通道已关闭（虚拟地址已回收）$/, 'Rescue channel closed (virtual address released)'],
    [/^配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）$/, 'Config saved to disk (written; service not started — switch over only after you confirm)'],
    [/^语法检查通过（仅预检，未写入磁盘、未启用服务）$/, 'Syntax check passed (dry run only; nothing written, no service started)'],
    [/^该服务当前处于停止状态，已跳过启动以避免影响现有网络；确认可以启用时请使用「启用服务」按钮。$/,
     'The service is currently stopped; start-up was skipped to avoid affecting the live network. Use the "Enable service" button when you are ready.'],
    [/^配置已保存。$/, 'Config saved.'],
    [/^配置校验失败：([\s\S]+)$/, 'Config validation failed: $1'],
    [/^配置语法检查未通过：([\s\S]+)$/, 'Config syntax check failed: $1'],
    [/^(.+?) 配置检查未通过：([\s\S]+)$/, '$1 config check failed: $2'],
    [/^未知的模块：(.+)$/, 'Unknown module: $1'],
    [/^(.+?) 的设置已保存（该模块没有独立配置文件，其内容会作为上下文随防火墙 \/ DHCP 等模块一起生效）$/,
     '$1 settings saved (this module has no configuration file of its own; its content takes effect as context together with modules such as firewall and DHCP)'],
    [/^自动快照策略已保存：已开启，每 (\d+) 小时一次，(\d+) 天后自动清理$/,
     'Automatic snapshot policy saved: enabled, once every $1 hours, cleaned up after $2 days'],
    [/^自动快照策略已保存：已关闭，每 (\d+) 小时一次，(\d+) 天后自动清理$/,
     'Automatic snapshot policy saved: disabled, once every $1 hours, cleaned up after $2 days'],
    [/^当前处于【构建保护模式】，已阻止配置生效。此模式用于确保构建过程不影响正在运行的局域网。如确需让配置生效，请先在「系统设置 → 构建保护模式」中关闭保护。$/,
     'The system is in [build protection mode]; applying the configuration was blocked. This mode keeps the build process from affecting the running LAN. To let the configuration take effect, first turn protection off under "System settings -> Build protection mode".'],
    [/^当前处于【构建保护模式】，已阻止「回滚并生效」。如需在保护模式下还原配置，请先关闭保护模式，或改用不重启服务的回滚（不带 reload 参数）。$/,
     'The system is in [build protection mode]; "rollback and apply" was blocked. To restore a configuration while protected, turn protection off first, or use a rollback that does not restart services (without the reload parameter).'],
    [/^当前处于【构建保护模式】，已阻止启动 (.+?)。该服务会占用 53\/67 端口或改变网络，可能影响现有局域网。$/,
     'The system is in [build protection mode]; starting $1 was blocked. That service would occupy ports 53/67 or change the network, possibly affecting the existing LAN.'],
    [/^当前处于【构建保护模式】，已阻止网络切换类操作。$/, 'The system is in [build protection mode]; network-switching operations were blocked.'],
    [/^当前处于【构建保护模式】，已阻止重启\/关机。$/, 'The system is in [build protection mode]; reboot/power-off was blocked.'],
    [/^当前处于【构建保护模式】，已阻止自动升级（可在关闭保护后手动升级）。$/, 'The system is in [build protection mode]; automatic upgrades were blocked (upgrade manually after turning protection off).'],
    [/^([0-9A-Fa-f:]{17}) → (.+)$/, '$1 -> $2'],
  ];
  const apply1 = (str) => {
    for (const p of pats) { if (p[0].test(str)) return str.replace(p[0], p[1]); }
    return str;
  };

  // ① 整串匹配（简单详情）；② 否则按 " -> " 分段翻译 —— 真机上最常见的形状是
  //    "<操作名> -> <消息>"（apply -> 主题不存在：/ dnsmasq live=True -> 配置校验失败：…），
  //    操作名前缀不该挡住宿主消息的匹配。这是之前英文界面漏翻的主因。
  const whole = apply1(out);
  if (whole !== out) {
    out = whole;
  } else if (out.indexOf(' -> ') >= 0) {
    out = out.split(' -> ').map(apply1).join(' -> ');
  }

  // ③ 子串级兜底：处理 "<模块名> <消息>" 这类没有锚定形状的拼接。
  if (/[\u4e00-\u9fff]/.test(out)) {
    const subs = [
      [/配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）/g,
       'config saved to disk (written; service not started — switch over only after you confirm)'],
      [/语法检查通过（仅预检，未写入磁盘、未启用服务）/g,
       'syntax check passed (dry run only; nothing written, no service started)'],
      [/配置已保存。该服务当前处于停止状态，已跳过启动以避免影响现有网络；确认可以启用时请使用「启用服务」按钮。/g,
       'configuration saved. The service is currently stopped and was not started, to avoid affecting the existing network; use the "Enable service" button when you are ready.'],
      [/已应用主题[「"](.+?)[」"]，刷新页面即可看到效果/g,
       'theme "$1" applied — refresh the page to see it'],
      [/快照 ([0-9-]+) 已上锁，自动清理不会删除它/g,
       'snapshot $1 locked; auto-clean will not remove it'],
      [/（已上锁，不参与自动清理）/g, '(locked, excluded from auto-clean)'],
      [/配置检查未通过：/g, 'config check failed: '],
      [/未知的模块：/g, 'Unknown module: '],
      [/PPPoE 网卡不能为空/g, 'PPPoE interface must not be empty'],
      [/没有那个文件或目录/g, 'No such file or directory'],
      [/端口转发与 DMZ/g, 'Port forwarding & DMZ'],
      [/网卡与桥接 \/ 系统基础设置/g, 'Interfaces & bridging / system basics'],
      [/执行异常：/g, 'Execution error: '],
      [/PPPoE 用户名不能为空/g, 'PPPoE username must not be empty'],
      [/PPPoE 用户名不能包含空格或引号/g, 'PPPoE username must not contain spaces or quotes'],
      [/PPPoE 密码不能为空（仅保存、暂不拨号）/g, 'PPPoE password must not be empty (saved only; dialing is not started)'],
      [/PPPoE 密码不能为空/g, 'PPPoE password must not be empty'],
      [/PPPoE 密码不能包含空格或引号/g, 'PPPoE password must not contain spaces or quotes'],
      [/备注=/g, 'note='],
      [/角色=/g, 'role='],
      [/PPPoE 拨号配置/g, 'PPPoE dial-up config'],
      [/IPv4 防火墙/g, 'IPv4 firewall'],
      [/IPv6 防火墙/g, 'IPv6 firewall'],
      [/DHCP\/DNS 服务/g, 'DHCP/DNS service'],
      [/IPv6 路由通告/g, 'IPv6 router advertisement'],
      [/DHCPv6 客户端/g, 'DHCPv6 client'],
      [/NTP 客户端/g, 'NTP client'],
      [/UPnP 服务/g, 'UPnP service'],
    ];
    for (const p of subs) out = out.replace(p[0], p[1]);
  }
  return out;
}

function ulogMsgI18n(r) {
  const s = (r && (r.msg_cn || r.msg || '')) || '';
  if (!s || !(window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US')) return s;
  const pats = [
    [/^已归档 (\d+) 条日志（采集 (\d+) 条）$/, 'Archived $1 log entries ($2 collected)'],
    [/^已归档 (\d+) 条日志$/, 'Archived $1 log entries'],
    [/^本次无新增日志（采集 (\d+) 条）$/, 'No new log entries this round ($1 collected)'],
    [/^归档去重移除 (\d+) 条重复记录$/, 'Deduplication removed $1 duplicate entries'],
    [/^磁盘空间不足，跳过本次日志采集：([\s\S]+)$/, 'Low disk space, skipping log collection: $1'],
    [/^日志归档失败：([\s\S]+)$/, 'Log archiving failed: $1'],
    [/^部分日志源采集失败：([\s\S]+)$/, 'Some log sources failed to collect: $1'],
    [/^统一日志已关闭，跳过本次采集$/, 'Unified logging is off, skipping this run'],
    [/^归档已关闭，跳过本次采集$/, 'Archiving is off, skipping this run'],
    [/^常驻执行守护已启动（socket=(.+?)，动作 (\d+) 个）$/, 'Resident execution daemon started (socket=$1, $2 actions)'],
    // 开关值是后端下发的中文「开启/关闭」，得一起换成 on/off
    [/^磁盘清理策略已更新（(.+?)，触发=(.+?)，水位=(\d+)%）$/,
      (m, on, trig, wm) => 'Disk cleanup policy updated (enabled='
        + (on === '开启' ? 'on' : (on === '关闭' ? 'off' : on)) // i18n-keep-cn
        + ', trigger=' + trig + ', watermark=' + wm + '%)'],
    // —— 快照类（备份/快照守护写进日志的中文，英文界面要显示英文）——
    [/^已创建配置快照 (\S+)（(.+?)，共 (\d+) 项([\s\S]*?)）$/,
      (m, ts, tag, n, suf) => 'Configuration snapshot created ' + ts + ' (' + tag + ', '
        + n + ' items' + suf.replace(/，(\d+) 个文件因权限跳过/, ', $1 files skipped due to permissions') + ')'],
    [/^已创建配置快照：(\S+)([\s\S]*)$/, 'Configuration snapshot created: $1$2'],
    [/^自动快照已创建：(\S+)$/, 'Automatic snapshot created: $1'],
    [/^自动清理快照 (\d+) 份（保留 (\d+)）$/, 'Auto-clean removed $1 snapshot(s), $2 kept'],
    [/^自动清理 (\d+) 份过期快照$/, 'Auto-clean removed $1 expired snapshot(s)'],
    [/^已删除快照 (\S+)$/, 'Snapshot $1 deleted'],
    [/^已修改快照 (\S+) 的备注$/, 'Note of snapshot $1 updated'],
    [/^快照跳过 (\d+) 个读不到的文件（多为 root 专属权限）$/,
      'Snapshot skipped $1 unreadable file(s) (usually root-only permissions)'],
    [/^快照已打包（(\d+) KB）$/, 'Snapshot packaged ($1 KB)'],
    [/^操作成功$/, 'Operation succeeded'],
  ];
  for (const p of pats) { if (p[0].test(s)) return s.replace(p[0], p[1]); }
  // 兜底：conntrack 事件行（如「新建连接：TCP 192.168.7.3:56060 → 1.1.1.1:443（SYN 已发送）」）。
  // 状态词是后端 _CONNTRACK_STATE_CN 下的中文，逐词映射比整句正则更稳（新增状态补表即可）。
  const CT_STATE = { // i18n-keep-cn-table
    '已建立': 'established', 'SYN 已发送': 'SYN sent', 'SYN 已接收': 'SYN received',
    'SYN 重传': 'SYN retransmit', 'FIN 等待': 'FIN wait', '等待回收': 'TIME_WAIT',
    '半关闭': 'half-closed', '最后确认': 'LAST-ACK', '已关闭': 'closed',
    '监听中': 'listening', '已确认': 'assured', '新建': 'new',
  };
  const CT_EV = { // i18n-keep-cn-table
    '新建连接': 'New connection', '连接更新': 'Connection updated',
    '连接关闭': 'Connection closed', '单向无响应': 'One-way, no response',
  };
  // ⛔ 变量名不能叫 t —— 它会遮蔽全局的 t()，本函数里就再也调不了翻译了
  //    （t-t-shadow 专门守这条）。这里改用 out。
  let out = s.replace(/^IPV4 /, 'IPv4 ').replace(/^IPV6 /, 'IPv6 ');
  out = out.replace(/^(新建连接|连接更新|连接关闭|单向无响应)：/, (m, e) => CT_EV[e] + ': ');
  out = out.replace(/：协议 /, ': protocol ');
  out = out.replace(/（单向（无回包））/g, '(one-way, no reply)');
  out = out.replace(/（([^（）]+)）/g, (m, inner) => (CT_STATE[inner] ? '(' + CT_STATE[inner] + ')' : m));
  out = out.replace(/（无回包）$/, ' (no reply)');
  if (out !== s) return out;
  return s;
}

// WAN / PPPoE 接入日志的元信息行与空日志提示：英文界面要显示英文。
// 后端下发的是中文原文（动态部分如 IP / MAC 用捕获组保留）。
function wanMetaI18n(x) {
  const s = x || '';
  if (!s || !(window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US')) return s;
  const pats = [
    [/^本端 IP (.+)$/, 'Local IP $1'],
    [/^对端网关 (.+)$/, 'Peer gateway $1'],
    [/^MTU (.+)$/, 'MTU $1'],
    [/^将在 (\d+) 秒后自动重拨$/, 'Will auto-redial in $1 s'],
    [/^IPv6 地址 (.+)$/, 'IPv6 address $1'],
    [/^IPv6 网关 (.+)$/, 'IPv6 gateway $1'],
    [/^IPv6 默认路由缺失$/, 'IPv6 default route missing'],
    [/^IPv6 未获取（检查上级是否开启 IPv6）$/, 'IPv6 not obtained (check if upstream has IPv6 enabled)'],
    [/^接口 (.+)，桥接模式不参与拨号$/, 'Interface $1, bridge mode does not dial'],
    [/^接口 (.+)$/, 'Interface $1'],
    [/^地址 (.+)$/, 'Address $1'],
    [/^网关可达（MAC (.+?)）$/, 'Gateway reachable (MAC $1)'],
    [/^网关暂不可达（可能为上级设备关闭 ICMP\/ARP 未学习）$/, 'Gateway temporarily unreachable (upstream may have disabled ICMP / ARP not learned)'],
    [/^网关未响应 ARP —— 请核对网关地址是否与上级同网段$/, 'Gateway did not answer ARP — verify the gateway is on the same subnet as the upstream'],
    [/^网关 (.+)$/, 'Gateway $1'],
    [/^尚未配置 IPv4 地址$/, 'IPv4 address not yet configured'],
    [/^当前默认路由 → (.+)（出口 (.+?)）$/, 'Current default route → $1 (egress $2)'],
    [/^当前无 IPv4 默认路由$/, 'No IPv4 default route currently'],
  ];
  for (const p of pats) { if (p[0].test(s)) return s.replace(p[0], p[1]); }
  return s;
}
function wanTipI18n(tip) {
  const s = tip || '';
  if (!s || !(window.i18n && window.i18n.getLang && window.i18n.getLang() === 'en-US')) return s;
  const M = {
    '暂无拨号日志：请确认已点击「开始拨号」，且构建保护模式未阻止操作。': 'No dial log yet: please confirm you clicked "Start dialing" and build-protection mode is not blocking the operation.',
    '暂无 DHCP 协商日志：若已获取到地址，说明协商发生在更早时段；可调大时间范围查看。': 'No DHCP negotiation log yet: if an address was obtained, negotiation happened earlier; widen the time range to view.',
    '暂无链路事件：静态模式下地址由配置文件直接写入，通常不会产生协商日志。': 'No link events: in static mode addresses are written directly from config, so negotiation logs are rare.',
    '暂无双栈日志：请确认 pppd 已拨号且上级下发了 IPv6 前缀（IA_PD）。': 'No dual-stack log yet: confirm pppd has dialed and the upstream delegated an IPv6 prefix (IA_PD).',
    '暂无 IPv6 事件：请确认上级已开启 IPv6 且已下发路由器通告（RA）。': 'No IPv6 events: confirm the upstream enabled IPv6 and sent Router Advertisements (RA).',
    '暂无网桥事件：桥接模式仅在链路状态变化时产生日志。': 'No bridge events: bridge mode only logs when link state changes.',
    '暂无日志。': 'No logs.',
  };
  return M[s] || s;
}

function ulRow(r) {
  const lv = UL_LEVEL_TAG[r.level] || 'gray';
  return `<div class="ul-trow" data-ulraw="${esc((r.raw || '').slice(0, 400))}">
    <span class="mono" style="font-size:11.5px">${esc((r.ts || '').replace('T', ' '))}</span>
    <span><span class="ul-ico">${UL_SRC_ICON[r.src] || '•'}</span>
      ${esc(UL_SRC_MAP()[r.src] || r.src)}</span>
    <span><span class="tag ${lv}">${esc(LV_STAT_I18N()[r.level] || r.level_cn || r.level)}</span></span>
    <span class="mono" style="font-size:11.5px">${esc(r.action || '—')}
      ${r.proto ? '<br>' + esc(r.proto) : ''}</span>
    <span class="mono" style="font-size:11.5px">${r.saddr
    ? esc(r.saddr + (r.sport ? ':' + r.sport : '')) + '<br>→ ' +
      esc(r.daddr + (r.dport ? ':' + r.dport : '')) : '—'}</span>
    <span>${esc(ulogMsgI18n(r))}${r.iface ? `<br><span class="mono"
      style="font-size:11px;color:var(--txt3)">${esc(r.iface)}</span>` : ''}</span>
  </div>`;
}

function ulFlowTable(u) {
  const f = u.flow || {};
  const rows = f.rows || [];
  const st = f.stat || {};
  if (!rows.length) {
    return `<div class="card"><h3>${T('当前连接')}</h3>
      <p class="hint-inline">${T('没有读到连接记录')}${f.err ? '：' + esc(f.err) : ''}${T('。 若系统未安装')} <span class="mono">conntrack</span> ${T('工具或未开启连接跟踪，此页会为空。')}</p></div>`;
  }
  return `<div class="card">
    <h3>${T('当前连接（')}${rows.length}）
      <span class="tag gray" style="float:right">TCP ${st.tcp || 0} · UDP ${st.udp || 0}
        · ICMP ${st.icmp || 0} · ${T('其他')} ${st.other || 0}</span></h3>
    <p class="desc">${esc(f.note || '')}</p>
    <div class="ul-table">
      <div class="ul-thead"><span>${T('协议')}</span><span>${T('状态')}</span>
        <span>${T('源地址')}</span><span>${T('目的地址')}</span><span>${T('已传输')}</span><span>${T('方向')}</span></div>
      ${rows.map(r => `<div class="ul-trow">
        <span class="mono">${esc(r.proto)}</span>
        <span><span class="tag ${r.extra && r.extra.state === 'ESTABLISHED' ? 'ok'
    : (r.extra && r.extra.state === 'UNREPLIED' ? 'warn' : 'gray')}">
          ${esc((r.extra || {}).state_cn || (r.extra || {}).state || '—')}</span></span>
        <span class="mono" style="font-size:11.5px">${esc(r.saddr)}${r.sport ? ':' + esc(r.sport) : ''}</span>
        <span class="mono" style="font-size:11.5px">${esc(r.daddr)}${r.dport ? ':' + esc(r.dport) : ''}</span>
        <span class="mono" style="font-size:11.5px">${r.extra && r.extra.bytes
    ? esc(fmtBytes(r.extra.bytes)) : '—'}</span>
        <span class="mono" style="font-size:11.5px">${esc(r.action || '')}</span>
      </div>`).join('')}
    </div>
  </div>`;
}

function ulBind() {
  const u = ulState();  const on = (sel, ev, fn) => { const e = $(sel); if (e) e[ev] = fn; };

  $$('[data-ulview]').forEach(b => {
    b.onclick = () => { u.view = b.dataset.ulview; ulLoad(); };
  });
  on('#ul-refresh', 'onclick', () => ulLoad());
  // 实时事件订阅：勾选后立刻按新设置重查一次（它会让查询多等几秒）
  on('#ul-live', 'onchange', e => {
    u.live = !!(e && e.target && e.target.checked);
    ulLoad();
  });
  on('#ul-go', 'onclick', () => {
    u.since = ($('#ul-since') || {}).value || u.since;
    u.src = ($('#ul-src') || {}).value || '';
    u.level = ($('#ul-level') || {}).value || '';
    u.proto = ($('#ul-proto') || {}).value || '';
    u.action = ($('#ul-action') || {}).value || '';
    u.q = ($('#ul-q') || {}).value || '';
    const lim = parseInt(($('#ul-limit') || {}).value, 10);
    u.limit = (isFinite(lim) && lim >= 20) ? Math.min(lim, 3000) : 400;
    ulLoad();
  });
  on('#ul-reset', 'onclick', () => {
    Object.assign(u, { since: '30 min ago', src: '', level: '', proto: '',
      action: '', q: '', limit: 400 });
    ulLoad();
  });

  // 点行展开原始内容
  $$('.ul-trow').forEach(tr => {
    tr.onclick = () => {
      const raw = tr.dataset.ulraw || '';
      if (!raw) return;
      modal(T('原始日志内容'), `<pre class="mono" style="max-height:420px;overflow:auto;
        background:var(--panel2);padding:12px;border-radius:8px;font-size:12px;
        white-space:pre-wrap">${esc(raw)}</pre>`, () => {}, T('关闭'), true);
    };
  });

  on('#ul-save', 'onclick', async () => {
    const srcs = {};
    $$('#ul-srcs [data-ulsrc]').forEach(cb => { srcs[cb.dataset.ulsrc] = !!cb.checked; });
    const r = await api('/api/ulog', { method: 'POST', body: {
      op: 'save_conf',
      enabled: !!($('#ul-enabled') || {}).checked,
      archive: !!($('#ul-archive') || {}).checked,
      keep_days: parseInt(($('#ul-keep-days') || {}).value, 10) || 7,
      keep_rows: parseInt(($('#ul-keep-rows') || {}).value, 10) || 200000,
      min_level: ($('#ul-minlevel') || {}).value || 'debug',
      sources: srcs,
    } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
    if (r.ok) { u.conf = (r.data || {}).conf || u.conf; ulLoad(); }
  });

  on('#ul-prune', 'onclick', async () => {
    const r = await api('/api/ulog', { method: 'POST', body: { op: 'prune' } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
    if (r.ok) ulLoad();
  });

  const doExport = async (fmt) => {
    const r = await api('/api/ulog', { method: 'POST',
      body: Object.assign(ulQuery(), { op: 'export', format: fmt }) });
    if (!r.ok) { toast(r.msg_cn, 'err'); return; }
    const d = r.data || {};
    const name = 'drouter-log-' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')
      + (fmt === 'csv' ? '.csv' : '.jsonl');
    ulDownload(d.text || '', name);
    toast(r.msg_cn || (T('已导出 ') + (d.rows || 0) + ` ${T('条')}`), 'ok', 7000);
    const out = $('#ul-out');
    if (out) {
      out.innerHTML = `<p class="hint-inline">${T('已导出')} <b>${d.rows || 0}</b> ${T('条记录， 文件名')} <span class="mono">${esc(name)}</span>。</p>`;
    }
  };
  on('#ul-export-json', 'onclick', () => doExport('json'));
  on('#ul-export-csv', 'onclick', () => doExport('csv'));

  on('#ul-clear', 'onclick', () => {
    modal(T('清空日志归档'), `<p>${T('会删除')} <span class="mono">/var/log/drouter/ulog.jsonl</span>${T('， 历史连接与流量日志将')}<b>${T('永久丢失')}</b>${T('（不影响系统')} journal）。</p><p>${T('确认继续？')}</p>`,
    async () => {
      const r = await api('/api/ulog', { method: 'POST', body: { op: 'clear_archive' } });
      toast(r.msg_cn, r.ok ? 'ok' : 'err');
      if (r.ok) ulLoad();
    }, T('确认清空'));
  });
}

async function ulLoadDaemon() {
  const box = $('#ul-daemon');
  if (!box) return;
  const r = await api('/api/ulog/daemon');
  if (!r.ok) { box.innerHTML = ''; return; }
  const d = r.data || {};
  const t = (d.units || {})['drouter-logd.timer'] || {};
  const last = d.last || {};
  const on = t.active === 'active';
  box.innerHTML = `${T('后台采集：')}<span class="tag ${on ? 'ok' : 'gray'}">
      ${on ? T('运行中') : T('未运行')}</span>
    <span class="mono" style="font-size:11.5px">${esc(d.next || '')}</span>
    ${last.ts ? `　${T('最近一次：')}<span class="mono" style="font-size:11.5px">${esc(last.ts)}</span>
      ${esc(ulogMsgI18n(last))}` : ''}`;
}

function ulDownload(text, filename) {  try {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (e) {
    toast(T('导出失败：') + netErrCn(e && e.message), 'err');
  }
}

function preSaveSync() {
  if (S.page === 'portfwd') {
    syncPfRules();
    return ['portfwd', 'nft_v4', 'nft_v6'];
  }
  return null;
}

$('#ab-save').onclick = async () => {
  const mod = currentModule();
  if (S.page === 'acl') {
    const r = await saveAcl(false);
    if (r.ok) pageClean('acl');
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 7000);
    setActionMsg(r.msg_cn, r.ok ? 'ok' : 'err');
    return;
  }
  if (S.page === 'nfs') {
    const r = await saveShare(false);
    if (r.ok) pageClean('nfs');
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 7000);
    setActionMsg(r.msg_cn, r.ok ? 'ok' : 'err');
    return;
  }
  preSaveSync();                       // 端口转发：先把编辑中的规则同步回配置
  const pm = pageModules();
  if (!pm || !pm.save.length) { toast(T('当前页面的操作按钮就在页面内部，无需全局保存'), 'warn'); return; }
  let allOk = true, msgs = [];
  for (const m of pm.save) {
    const r = await api('/api/config', { method: 'POST', body: { module: m, data: S.cfg[m] || {} } });
    msgs.push(r.msg_cn); if (!r.ok) allOk = false;
  }
  const tail = pm.apply.length ? '' : T('（该页面的设置没有独立配置文件，')
    + T('会作为上下文随防火墙 / DHCP 等模块一起生效）');
  // 保存成功必须清脏标记。原来只在 acl / nfs / ddns 三条专属分支里写
  // pageClean()，通用保存路径漏了 —— 于是 PAGE_MODULES 里那 20 多个页面
  // 保存后 PAGE_DIRTY 永不消失，之后每次进页面都拿旧草稿覆盖刚从库里
  // 读回来的新值（别人用 CLI 改过、或另开标签页保存过都会踩到）。
  if (allOk) pageClean(S.page);
  toast(msgs.join('；') + tail, allOk ? 'ok' : 'err');
  setActionMsg(msgs.join('；') + tail, allOk ? 'ok' : 'err');
};

$('#ab-apply').onclick = async () => {
  if (S.page === 'acl') {
    const doAcl = async (live) => {
      setActionMsg(T('正在校验并应用访问控制规则…'));
      const r = await saveAcl(live);
      if (r.ok) pageClean('acl');
      toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
      setActionMsg(r.msg_cn, r.ok ? 'ok' : 'err');
      const r2 = await api('/api/acl');
      if (r2.ok) { S.acl = r2.data || S.acl; renderAcl(); }
    };
    if (S.buildMode) {
      modal(T('构建保护模式已开启'), `<p>${T('当前处于')} <b>${T('构建保护模式')}</b>${T('，规则只会写入')}
        <span class="mono">/etc/drouter/generated/acl.nft</span>，<b>${T('不会加载到内核')}</b>${T('， 以确保不影响正在运行的局域网与')} RealVNC ${T('会话。')}</p>
        <p>${T('确认要把当前规则写入磁盘（暂不生效）？')}</p>`, () => doAcl(false), T('写入磁盘'));
    } else {
      modal(T('应用访问控制规则'), `<p>${T('规则会立即加载到内核（表')} <span class="mono">inet drouter_acl</span>）。</p>
        <p>${T('如果规则写得不严谨，可能阻断目标设备的正常上网，但不影响路由器本身与其他设备。')}</p>
        <p>${T('确认继续？')}</p>`, () => doAcl(true), T('确认应用'));
    }
    return;
  }
  if (S.page === 'nfs') {
    const doSh = async (live) => {
      setActionMsg(T('正在校验并应用共享配置…'));
      const r = await saveShare(live);
      if (r.ok) pageClean('nfs');
      toast(r.msg_cn, r.ok ? 'ok' : 'err', 9000);
      setActionMsg(r.msg_cn, r.ok ? 'ok' : 'err');
      const r2 = await api('/api/share');
      if (r2.ok) { S.shareData = r2.data; S.share = (r2.data || {}).cfg || S.share; renderShare(); }
    };
    if (S.buildMode) {
      modal(T('构建保护模式已开启'), `<p>${T('当前处于')} <b>${T('构建保护模式')}</b>${T('，配置只会写入')}
        <span class="mono">/etc/samba/drouter.conf</span> ${T('与')} <span class="mono">/etc/exports</span>，
        <b>${T('不会重启')} smbd / nfs-server</b>${T('，以确保不影响正在运行的局域网与')} RealVNC ${T('会话。')}</p>
        <p>${T('确认要把当前配置写入磁盘（暂不生效）？')}</p>`, () => doSh(false), T('写入磁盘'));
    } else {
      modal(T('应用文件共享配置'), `<p>${T('会写入配置文件并重启对应的共享服务：')}</p>
        <ul style="font-size:13px;color:var(--txt2);line-height:1.9">
          <li>${T('启用')} SMB ${T('时：重启')} <span class="mono">smbd</span> / <span class="mono">nmbd</span></li>
          <li>${T('启用')} NFS ${T('时：重启')} <span class="mono">nfs-server</span> ${T('并重载导出表')}</li>
        </ul>
        <p>${T('已经挂载的客户端可能会短暂断开，一般会自动重连。')}</p>
        <p>${T('确认继续？')}</p>`, () => doSh(true), T('确认应用'));
    }
    return;
  }
  const pm = pageModules();
  if (!pm) { toast(T('当前页面的「应用」按钮就在页面内部，无需全局应用'), 'warn'); return; }

  const doApply = async (live) => {
    setActionMsg(T('正在校验并应用…'));
    preSaveSync();                     // 端口转发：先把编辑中的规则同步回配置
    let ok = true, msgs = [];
    // 1) 先入库：纯配置模块（system / portfwd）只有这一步
    for (const m of pm.save) {
      const r = await api('/api/config', { method: 'POST', body: { module: m, data: S.cfg[m] || {} } });
      if (!r.ok) { msgs.push(r.msg_cn); ok = false; }
    }
    // 2) 再渲染生效：只有真正有配置文件的模块才走 /api/apply
    for (const m of pm.apply) {
      const r = await api('/api/apply', { method: 'POST', body: { module: m, data: S.cfg[m] || {}, live } });
      msgs.push(r.msg_cn); if (!r.ok) ok = false;
    }
    if (!pm.apply.length) {
      msgs.push(T('设置已保存（该页面没有独立的配置文件需要渲染，')
        + T('其设置会作为上下文随防火墙 / DHCP 等模块一起生效）'));
    }
    if (ok) pageClean(S.page);
    toast(msgs.join('；'), ok ? 'ok' : 'err', 8000);
    setActionMsg(msgs.join('；'), ok ? 'ok' : 'err');
    loadAll();
  };

  const highRisk = ['iface', 'lan', 'wan'].includes(S.page);
  const buildMode = S.buildMode;

  if (buildMode) {
    modal(T('构建保护模式已开启'), `<p>${T('当前处于')} <b>${T('构建保护模式')}</b>${T('，配置只会写入磁盘，')}
      <b>${T('不会启动服务、不会改变网络')}</b>${T('，以确保不影响正在运行的局域网与')} RealVNC ${T('会话。')}</p>
      <p>${T('确认要把当前页面的配置写入磁盘（暂不生效）？')}</p>`, () => doApply(false), T('写入磁盘'));
    return;
  }
  if (highRisk) {
    modal(T('高危操作确认'), `<p>${T('当前页面的配置变更会')}<b>${T('真正生效')}</b>${T('，可能影响网络连接，包括')} RealVNC ${T('远程会话。')}</p>
      <p>${T('系统会在应用前自动创建配置快照，失败时自动回滚。')}</p>
      <p>${T('确认继续？')}</p>`, () => doApply(true), T('确认应用'));
  } else {
    doApply(true);
  }
};

/* 构建保护模式开关 */
async function toggleBuildMode(enable) {
  const doIt = async () => {
    const r = await api('/api/buildmode', { method: 'POST', body: { op: enable ? 'on' : 'off', confirm: !enable } });
    toast(r.msg_cn, r.ok ? 'ok' : 'err', 8000);
    if (r.ok) { S.buildMode = enable; loadAll(); go(S.page); }
  };
  if (!enable) {
    modal(T('关闭构建保护模式'), `<p>${T('关闭后，所有「保存并应用」操作将')}<b>${T('真正生效')}</b>：</p>
      <ul style="font-size:13px;color:var(--txt2);line-height:1.9">
        <li>DHCP ${T('服务会启动并占用')} <span class="mono">53/67</span> ${T('端口')}</li>
        <li>${T('防火墙规则会立即加载')}</li>
        <li>${T('网络类改动会导致连接中断')}</li>
      </ul>
      <p>${T('请确认你已经准备好执行切换。')}</p>
      <p>${T('输入')} <b>${T('确认关闭保护')}</b> ${T('以继续：')}</p>
      <input id="cfm-in" placeholder="${T('确认关闭保护')}">`, async () => {
      if ($('#cfm-in').value.trim() !== T('确认关闭保护')) { toast(T('确认文字不正确'), 'err'); return false; }
      await doIt();
    }, T('关闭保护'));
  } else { await doIt(); }
}

/* ============================ 入口 ============================ */
$('#lg-btn').onclick = doLogin;
$('#lg-pass').onkeydown = e => { if (e.key === 'Enter') doLogin(); };
/* 左下角「退出登录」与右上角「刷新数据」已按要求移除（#12）：
   退出可在左侧用户区或会话过期后自动跳回；刷新由手动 F5 或各页自带刷新按钮完成。 */

/* 侧边栏：宽屏折叠成图标栏，窄屏变成抽屉（手机适配 #14） */
const SIDE_KEY = 'drouter_side_collapsed';
const isNarrow = () => !!(typeof window !== 'undefined' && window.matchMedia
  && window.matchMedia('(max-width:760px)').matches);
if (localStorage.getItem(SIDE_KEY) === '1') $('#app').classList.add('side-collapsed');

/* 关闭手机端抽屉。做成具名函数是因为 go() 里也要调用（选完菜单自动收起）。 */
function closeDrawer() {
  const el = $('#app');
  if (el) el.classList.remove('m-drawer');
}

/* 左上角 logo → 回系统概览。
   go() 自己会 closeDrawer()，窄屏下抽屉会一起收起，不用额外处理。
   ⚠️ 已经在 dash 页时不重复 go()：go() 会 stopPageTimers + 重新拉数据，
   在概览页上连点 logo 会白白清掉轮询再重建，看着像「卡了一下」。
   概览页自己有刷新按钮，需要重拉数据用那个。 */
const brandHome = $('#brand-home');
if (brandHome) {
  brandHome.addEventListener('click', e => {
    e.preventDefault();
    closeDrawer();
    if (S.page === 'dash') return;
    go('dash');
  });
}

$('#btn-side').onclick = () => {
  // 窄屏走抽屉，不写折叠状态——否则回到宽屏时会莫名是折叠的
  if (isNarrow()) { $('#app').classList.toggle('m-drawer'); return; }
  const c = $('#app').classList.toggle('side-collapsed');
  try { localStorage.setItem(SIDE_KEY, c ? '1' : '0'); } catch (e) { /* ignore */ }
};

/* 右上角「刷新」：重新执行当前页面的视图函数，等于原地重进一次。
   图标转一圈给反馈；失败时仍由 go() 的统一错误卡片兜住。 */
const btnRefresh = $('#btn-refresh');
if (btnRefresh) {
  btnRefresh.onclick = () => {
    const k = S.page;
    if (!k) return;
    btnRefresh.classList.add('spinning');
    // 先移除动画类再加，保证连续点击也能看到旋转
    setTimeout(() => btnRefresh.classList.remove('spinning'), 700);
    go(k);
    const p = PAGES.find(x => x.k === k) || {};
    toast(T('已刷新「') + (p.t || k) + '」', 'ok', 1500);
  };
}

/* 左下角「退出系统」：带一次确认，避免误点把正在配置的内容弄丢 */
const btnLogout = $('#btn-logout');
if (btnLogout) {
  btnLogout.onclick = () => {
    modal(
      T('退出系统'),
      `<p class="desc">${T('确定要退出管理后台吗？未保存的配置改动会丢失。')}</p>`,
      () => { logout(false); },
      T('退出')
    );
  };
}

/* 抽屉遮罩：点击关闭；Esc 也关闭 */
const mMask = $('#m-mask');
if (mMask) mMask.onclick = closeDrawer;
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDrawer();
});
/* 从窄屏拉回宽屏时清掉抽屉状态，否则遮罩会一直挡住页面 */
if (typeof window !== 'undefined' && window.matchMedia) {
  const mq = window.matchMedia('(max-width:760px)');
  const onW = () => { if (!mq.matches) closeDrawer(); };
  if (mq.addEventListener) mq.addEventListener('change', onW);
  else if (mq.addListener) mq.addListener(onW);
}

/* 表格横向滚动（#14）：渲染后统一给 <table> 包一层 .tw。
   全站有 20 多处表格渲染点（还含 ajax 异步刷新的），逐个改不现实，
   这里用 MutationObserver 兜住「直接赋值 innerHTML」和「异步回填」两种路径。 */
function wrapTables(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return;
  root.querySelectorAll('table').forEach(t => {
    const p = t.parentNode;
    if (!p) return;
    if (p.classList && p.classList.contains('tw')) return;   // 已包过
    const w = document.createElement('div');
    w.className = 'tw';
    p.insertBefore(w, t);
    w.appendChild(t);
  });
}
(function watchTables() {
  const v = $('#view');
  if (!v || typeof MutationObserver === 'undefined') return;
  let busy = false;
  new MutationObserver(() => {
    if (busy) return;
    busy = true;
    try { wrapTables(v); } catch (e) { /* 忽略：包裹失败只是少了横向滚动 */ }
    busy = false;
  }).observe(v, { childList: true, subtree: true });
})();

/* 菜单搜索：输入时自动展开匹配分组 */
$('#nav-filter').oninput = debounce(e => {
  S.navKw = e.target.value;
  renderNav();
}, 140);
$('#nav-filter').onkeydown = e => {
  if (e.key === 'Escape') { e.target.value = ''; S.navKw = ''; renderNav(); }
};

if (S.token) {
  api('/api/me').then(r => {
    if (r.ok) {
      S.user = r.data.user;
      $('#login').classList.add('hidden');
      $('#app').classList.remove('hidden');
      boot();
    } else { logout(true); }
  });
}

function caKindText(x) {
  const k = (x && x.kind) || '';
  return (k && T('ca.kind.' + k)) || (x && x.kind_cn) || k || '';
}

