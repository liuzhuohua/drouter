// 审计详情英文化回归：用真 i18n.js + 真 audDetail，喂真机 /api/audit 下发过的
// 每一条 detail（从 192.168.7.3 的 audits 表导出），断言英文界面下 0 个残留中文。
// 覆盖的重点是 "<操作名> -> <消息>" 这种复合形状 —— 之前正则锚了 ^，前缀把
// 宿主消息挡住，英文界面整列中文。
// 用法：node _dev/t-audit-en.js [web/app.js]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const APP = process.argv[2] || path.join(ROOT, 'web', 'app.js');
const i18nSrc = fs.readFileSync(path.join(ROOT, 'web', 'i18n.js'), 'utf8');
const appSrc = fs.readFileSync(APP, 'utf8');

function stubEl() {
  return {
    style: {}, dataset: {}, className: '', textContent: '', value: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute() {}, removeAttribute() {}, appendChild() {}, removeChild() {},
    addEventListener() {}, removeEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    innerHTML: '', children: [],
  };
}
const store = {};
const sandbox = {
  console,
  // t/T 桩：本检查器只抽 bt4/audDetail 执行，不跑 app.js 顶层；
  // 但补上桩可防日后扩展到执行 app.js 顶层时第一行 ReferenceError（t-t-stub 守着）。
  t: k => k, T: k => k,
  document: {
    documentElement: {}, readyState: 'complete',
    addEventListener() {}, removeEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    getElementById() { return null; }, createElement() { return stubEl(); },
    body: stubEl(),
  },
  localStorage: {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  },
  navigator: { language: 'zh-CN' },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  fetch: () => Promise.resolve({ status: 200, json: () => Promise.resolve({}) }),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(i18nSrc, sandbox, { filename: 'i18n.js' });

// 从 app.js 里抽 bt4 + audDetail（到各自列 0 的收尾 } 为止）
function grab(name) {
  const re = new RegExp('function ' + name + '\\([\\s\\S]*?\\n\\}');
  const m = appSrc.match(re);
  if (!m) throw new Error('cannot extract ' + name + ' from app.js');
  return m[0];
}
vm.runInContext(grab('bt4') + '\n' + grab('audDetail') +
  '\nglobalThis.__bt4 = bt4; globalThis.__audDetail = audDetail;', sandbox, { filename: 'extract.js' });

sandbox.window.i18n.setLang('en-US', { force: true });
const audDetail = sandbox.__audDetail;
if (typeof audDetail !== 'function') throw new Error('audDetail not extracted');

// 真机 audits 表的 detail（2026-09-28 ~ 2026-10-04，去重后的形态）
const CASES = [
  'apply -> 已应用主题「电光蓝」，刷新页面即可看到效果',
  'apply -> 已应用主题「暗夜绯红」，刷新页面即可看到效果',
  'apply -> 已应用主题「青竹」，刷新页面即可看到效果',
  'apply -> 已应用主题「Drouter 经典蓝」，刷新页面即可看到效果',
  'set -> 未知主题操作：set',
  'apply -> 主题不存在：',
  '__probe__ -> 未知主题操作：__probe__',
  'read -> 未知主题操作：read',
  'ppp live=False -> PPPoE 拨号配置 配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）',
  'ppp live=False -> PPPoE 拨号配置 语法检查通过（仅预检，未写入磁盘、未启用服务）',
  'ntp live=False -> NTP 客户端 配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）',
  'upnp live=False -> UPnP 服务 配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）',
  'dhcpv6 live=False -> DHCPv6 客户端 配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）',
  'radvd live=False -> IPv6 路由通告 配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）',
  'nft_v6 live=False -> IPv6 防火墙 配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）',
  'dnsmasq live=False -> DHCP/DNS 服务 配置已保存到磁盘（配置已写入磁盘，服务未启动 —— 待你确认后再执行切换）',
  'nft_v4 live=False -> IPv4 防火墙 语法检查通过（仅预检，未写入磁盘、未启用服务）',
  'dnsmasq live=True -> 配置语法检查未通过：dnsmasq: failed to seed the random number generator: 没有那个文件或目录',
  'pppoe live=True -> 配置校验失败：PPPoE 网卡不能为空',
  'pppoe live=False -> 配置校验失败：PPPoE 网卡不能为空',
  'radvd live=True -> IPv6 路由通告 配置已保存。该服务当前处于停止状态，已跳过启动以避免影响现有网络；确认可以启用时请使用「启用服务」按钮。',
  'dnsmasq live=True -> DHCP/DNS 服务 配置已保存。该服务当前处于停止状态，已跳过启动以避免影响现有网络；确认可以启用时请使用「启用服务」按钮。',
  'portfwd live=False -> 端口转发与 DMZ 的设置已保存（该模块没有独立配置文件，其内容会作为上下文随防火墙 / DHCP 等模块一起生效）',
  'system live=False -> 网卡与桥接 / 系统基础设置 的设置已保存（该模块没有独立配置文件，其内容会作为上下文随防火墙 / DHCP 等模块一起生效）',
  'ntp live=False -> NTP 配置检查未通过：2026-09-29T08:49:45Z Fatal error : Could not open /run/drouter-verify/chrony.conf : Permission denied',
  'portfwd live=False -> 未知的模块：portfwd',
  'pppoe live=False -> 未知的模块：pppoe',
  'system live=False -> 未知的模块：system',
  '救援通道已开启，可通过 http://169.254.0.1:8888/ 访问（插任一口皆可）',
  '救援通道已开启，可通过 http://169.254.0.1:6688/ 访问（插任一口皆可）。请记下访问令牌 378565 —— 页面上的还原操作需要它，同网段其它机器无法凭猜测还原你的配置',
  '救援通道已关闭（虚拟地址已回收）',
  '备份包已删除',
  '20261003-142114（受保护，已强制删除）',
  '20261003-142114 快照 20261003-142114 已上锁，自动清理不会删除它',
  '20261003-142114 已解除 20261003-142114 的保护',
  '已创建配置快照：20261003-142114',
  '已创建配置快照：20261003-142113（已上锁，不参与自动清理）',
  '已清理 29 份过期快照',
  '备份已导出：21 个文件（20.3 KB）',
  'WireGuard 已停止',
  '配置已写入，但 VPN 处于关闭状态（未启动服务）',
  // 2026-10-09 二次真机导出（89 条去重）暴露的新形状：
  '自动快照策略已保存：已开启，每 6 小时一次，7 天后自动清理',
  'dnsmasq live=True -> 当前处于【构建保护模式】，已阻止配置生效。此模式用于确保构建过程不影响正在运行的局域网。如确需让配置生效，请先在「系统设置 → 构建保护模式」中关闭保护。',
  'ppp live=False -> 配置校验失败：PPPoE 用户名不能为空',
  "ntp live=False -> 执行异常：'list' object has no attribute 'split'",
  "nft_v4 live=False -> 执行异常：[Errno 30] Read-only file system: '/etc/nftables.d/drouter-v4.nft.drouter.tmp'",
  "dnsmasq live=False -> 执行异常：[Errno 30] Read-only file system: '/etc/dnsmasq.d/drouter.conf.drouter.tmp'",
];

const CJK = /[\u4e00-\u9fff]/;
let bad = 0;
for (const d of CASES) {
  const out = String(audDetail('x', d));
  if (CJK.test(out)) {
    bad++;
    console.log('✗ 英文界面仍有中文：\n    IN : ' + d + '\n    OUT: ' + out);
  }
}

// 用户自己填的文本（快照备注、网卡备注）不得被翻译 —— 只翻固定标签（备注=/角色=）。
const USER_DATA = [
  ['20261003-142113 → __T108_LIVE__ 改过的备注', '20261003-142113 → __T108_LIVE__ 改过的备注'],
  ['bc:24:11:1a:8f:43 ens18 备注=LAN管理口 角色=lan', 'bc:24:11:1a:8f:43 ens18 note=LAN管理口 role=lan'],
  ['bc:24:11:f2:5a:47 ens19 备注=WAN口（PPPoE拨号） 角色=wan', 'bc:24:11:f2:5a:47 ens19 note=WAN口（PPPoE拨号） role=wan'],
];
let udBad = 0;
for (const [inp, exp] of USER_DATA) {
  const o = String(audDetail('x', inp));
  if (o !== exp) { udBad++; console.log('✗ 用户数据用例不符：\n    IN : ' + inp + '\n    EXP: ' + exp + '\n    GOT: ' + o); }
}

// 反向验证：中文界面必须原样返回（不能被英文污染）
sandbox.window.i18n.setLang('zh-CN', { force: true });
let zhBad = 0;
for (const d of CASES) {
  const out = String(audDetail('x', d));
  if (out !== d) { zhBad++; console.log('✗ 中文界面被改写：\n    IN : ' + d + '\n    OUT: ' + out); }
}


// 后端下发的 DPI 安装方案 why（/api/dpi 的 presets[].why，中文原文）必须能查到英文；
// 且 app.js 的 #dpi-plan-why 必须真的走 bt4 —— 2026-10-09 真机上它直接用了裸 p.why，
// 英文界面整句中文（"Debian 13 仓库直接装，无需编译…"）。
sandbox.window.i18n.setLang('en-US', { force: true });   // 上面的反向验证把语言切回了中文
const bt4fn = sandbox.__bt4;

// 上游链路协议标签（upstream.js 的 bt('UPS_PROTO', u.proto_cn, 'en')）：
// 后端 /api/upstream 的 proto_cn 必须能查到英文。
// 2026-10-09 的坑：UPS_PROTO 被放在 DICT 顶层（与 bt: 同级），
// bt() 查 DICT.bt 永远落空 → 英文界面「静态地址 / DHCPv6 客户端」两个标签是中文。
let upsBad = 0;
for (const p of ['静态地址', 'DHCPv6 客户端', 'DHCP 自动', 'PPPoE 拨号']) {
  const en = bt4fn('UPS_PROTO', p, 'en', '');
  if (!en || CJK.test(en)) { upsBad++; console.log('✗ UPS_PROTO.' + p + ' 无英文: ' + JSON.stringify(en)); }
}
let dpiBad = 0;
for (const id of ['apt', 'source', 'netfilter']) {
  const en = bt4fn('DPI_INSTALL_PLANS', id, 'why', '');
  if (!en || CJK.test(en)) { dpiBad++; console.log('✗ DPI_INSTALL_PLANS.' + id + '.why 无英文: ' + JSON.stringify(en)); }
}
// 只看 #dpi-plan-why 附近的代码窗口：'DPI_INSTALL_PLANS' 在 <option> 的 name 处
// 也出现，全文件 includes 会被它满足，从而漏掉 why 退化。
const wi = appSrc.indexOf("'#dpi-plan-why'");
const win = wi >= 0 ? appSrc.slice(Math.max(0, wi - 400), wi + 160) : '';
if (!(win.includes("bt4('DPI_INSTALL_PLANS'") && win.includes("'why'"))) {
  dpiBad++; console.log('✗ app.js 的 #dpi-plan-why 未走 bt4(...why)（英文界面会显示后端中文 why）');
}

console.log('---');
console.log('审计详情用例: ' + CASES.length + '，英文残留: ' + bad + '，中文被改写: ' + zhBad + '，DPI why 问题: ' + dpiBad + '，用户数据用例不符: ' + udBad + '，UPS_PROTO 无英文: ' + upsBad);
if (bad || zhBad || dpiBad || udBad || upsBad) { console.log('AUDIT_EN_FAIL'); process.exit(1); }
console.log('AUDIT_EN_OK');
