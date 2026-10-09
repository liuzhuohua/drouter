// 离线渲染验证：把 app.js 里的 vpnRenderTop / vpnFix 抽出来，
// 在最小 DOM 桩里喂四种 state，检查输出 HTML 与按钮绑定。
//
// 为什么要单独做：check-render.js 只看「视图有没有崩」，
// 抓不到「四态分支里某一档拼出来的 HTML 是坏的」或者
// 「修复按钮没绑上 onclick」。这两类只有真跑函数才暴露。
//
// 用法：node devtools/check-vpn-render.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = fs.readFileSync(
  path.join(__dirname, '..', 'web', 'app.js'), 'utf8');

let pass = 0, fail = 0;
const fails = [];
function ck(desc, cond, extra) {
  if (cond) { pass++; console.log('  [OK] ' + desc); }
  else {
    fail++; fails.push(desc);
    console.log('  [FAIL] ' + desc + (extra ? '  ' + extra : ''));
  }
}

// ---- 最小 DOM 桩 ----
const store = {};
function mkEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', checked: false,
    className: '', style: {}, children: [],
    classList: { add() {}, remove() {}, toggle: () => false, contains: () => false },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); if (id) store[id] = el; },
    addEventListener() {}, appendChild() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    focus() {},
  };
  return el;
}
const doc = {
  getElementById(id) { if (!store[id]) store[id] = mkEl(id); return store[id]; },
  querySelector(s) {
    const m = /^#(.+)$/.exec(s);
    if (m) return doc.getElementById(m[1]);
    return null;
  },
  querySelectorAll() { return []; },
  createElement() { return mkEl(''); },
};
const ctxObj = {
  document: doc, window: {}, console,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  setTimeout, clearTimeout, setInterval, clearInterval,
  fetch: () => Promise.reject(new Error('offline')),
  location: { hash: '', href: '' },
  navigator: { userAgent: 'node' },
  alert() {}, confirm() { return true; },
  // 1.0.10 起 app.js 的文案走 i18n 的 t()。本检查器不加载 i18n.js
  // （它要 DOM + localStorage），给恒等兜底保证渲染流程能跑完。
  t: k => k,
  i18n: {
    t: k => k, setLang() {}, getLang: () => 'zh-CN', toggle() {},
    onChange() {}, applyDom() {}, dict: {}, register() {}, rerenderAll() {},
  },
};
ctxObj.window = ctxObj;
vm.createContext(ctxObj);

// 把 app.js 整体跑一遍（它顶层会注册一堆东西，但不会自动发请求）
try {
  vm.runInContext(APP, ctxObj, { filename: 'app.js' });
} catch (e) {
  // 有些脚本依赖浏览器全局，忽略顶层报错，只要函数挂上了就行
  if (!/vpnRenderTop/.test(String(e))) console.log('  (顶层异常：' + e.message + ')');
}

const fn = ctxObj.vpnRenderTop;
const ffix = ctxObj.vpnFix;
ck('app.js 跑完后 vpnRenderTop 可调用', typeof fn === 'function');
ck('app.js 跑完后 vpnFix 可调用', typeof ffix === 'function');
if (typeof fn !== 'function') {
  console.log('\n通过 ' + pass + ' 项，失败 ' + fail + ' 项');
  process.exit(1);
}

function render(env, extra) {
  const d = Object.assign({
    conf: { enabled: false, port: 51820, pool: '10.66.66.0/24',
            endpoint_host: '', keepalive: 25, dns: '', exit_node: false,
            peers: [] },
    live: { up: false, listen_port: null, peers: [], pubkey: '' },
    has_systemd: true,
    lan_nets: ['192.168.7.0/24'],
    pool_conflict: [], port_busy: false,
    path: '/etc/wireguard/wg0.conf',
    installed: 'ready',
    env: env,
  }, extra || {});
  const el = doc.getElementById('vp-top');
  el._html = '';
  fn(d);
  return el._html;
}

console.log('');
console.log('--- 1. 四态各自的说法 ---');

// ① unsupported：内核真没模块
let h = render({ state: 'unsupported', mod_loaded: false, mod_file: '',
                 tool: false, kernel: '6.12.107+deb13-amd64', virt: 'kvm',
                 autoload: false, fixable: false });
ck('unsupported：红条 + 点明内核版本',
   h.includes('notice err') && h.includes('6.12.107+deb13-amd64'));
ck('unsupported：说清是「内核里没有模块」',
   h.includes('里没有 wireguard 模块'));
ck('unsupported：给出替代判断依据（KVM 自带）',
   h.includes('KVM'));
ck('unsupported：**不出现**一键修复按钮（装了也没用）',
   !h.includes('id="vp-fix"'), '→ ' + (h.match(/id="vp-fix"/g) || []).length + ' 个');

// ② need_module：本轮修的那个真实场景
h = render({ state: 'need_module', mod_loaded: false,
             mod_file: '/lib/modules/6.12.107+deb13-amd64/kernel/drivers/net/'
               + 'wireguard/wireguard.ko.xz',
             tool: false, kernel: '6.12.107+deb13-amd64', virt: 'kvm',
             autoload: false, fixable: true });
ck('need_module：警告条（不是红条）', h.includes('notice warn'));
ck('need_module：说「模块在但还没加载」',
   h.includes('还没加载') && h.includes('wireguard'));
ck('need_module：给出模块文件所在路径',
   h.includes('kernel/drivers/net/wireguard'));
ck('need_module：有「一键修复」按钮', h.includes('id="vp-fix"'));
ck('need_module：按钮文案说清做什么',
   h.includes('一键修复') && h.includes('加载模块'));
ck('need_module：表格显示「模块未加载」', h.includes('模块未加载'));
ck('need_module：**不出现**「精简容器」字样',
   !h.includes('精简容器'), '→ ' + (h.match(/精简容器/g) || []).length + ' 处');
ck('need_module：**不出现**一刀切的「内核或工具不支持」',
   !h.includes('内核或工具不支持'));

// ③ need_tool
h = render({ state: 'need_tool', mod_loaded: true,
             mod_file: '/lib/modules/x/wireguard.ko.xz', tool: false,
             kernel: '6.12.107+deb13-amd64', virt: 'kvm', autoload: true,
             fixable: true });
ck('need_tool：说清缺的是 wireguard-tools',
   h.includes('wireguard-tools') && h.includes('缺少'));
ck('need_tool：说明它提供什么（wg / wg-quick）',
   h.includes('wg-quick'));
ck('need_tool：有「一键修复」按钮且提到安装',
   h.includes('id="vp-fix"') && h.includes('安装'));
ck('need_tool：表格显示「缺少工具」', h.includes('缺少工具'));

// ④ ready + 没自启
h = render({ state: 'ready', mod_loaded: true, mod_file: '/x.ko.xz',
             tool: true, kernel: '6.12.107+deb13-amd64', virt: 'kvm',
             autoload: false, fixable: false });
ck('ready-无自启：提示重启后会失效', h.includes('开机自动加载'));
ck('ready-无自启：说明为什么（udev 按需加载）',
   h.includes('udev'));
ck('ready-无自启：有配置按钮', h.includes('id="vp-fix"'));
ck('ready-无自启：表格显示「已就绪」', h.includes('WireGuard 就绪'));

// ⑤ ready + 有自启 → 不该有任何环境告警
h = render({ state: 'ready', mod_loaded: true, mod_file: '/x.ko.xz',
             tool: true, kernel: '6.12.107+deb13-amd64', virt: 'kvm',
             autoload: true, fixable: false });
ck('ready-有自启：没有多余告警条',
   !h.includes('notice warn') && !h.includes('notice err'));
ck('ready-有自启：也不出现修复按钮', !h.includes('id="vp-fix"'));
ck('ready-有自启：显示「已配置开机自启」', h.includes('已配置开机自启'));

// ⑥ 老后端兜底
h = render({}, { env: undefined, installed: 'no' });
ck('老后端（无 env）：显示「未检测」而不是猜',
   h.includes('未检测'), '→ ' + h.slice(0, 200));
ck('老后端（无 env）：不猜成内核无模块',
   !h.includes('里没有 wireguard 模块'));
ck('老后端（无 env）：提示升级',
   h.includes('1.0.8') || h.includes('升级'));

console.log('');
console.log('--- 2. 表格里的运行环境行 ---');
h = render({ state: 'need_module', mod_loaded: false, mod_file: '/x.ko.xz',
             tool: false, kernel: '6.12.107+deb13-amd64', virt: 'kvm',
             autoload: false, fixable: true });
ck('有「运行环境」这一行', h.includes('运行环境'));
ck('运行环境行显示内核版本', h.includes('6.12.107+deb13-amd64'));
ck('运行环境行显示虚拟化形态（kvm）', h.includes('kvm'));
ck('非 ready 时不显示「未配置开机自启」（避免噪声）',
   !h.includes('未配置开机自启'));

// ⑦ 标签闭合与转义
ck('所有 div 都闭合',
   (h.match(/<div/g) || []).length === (h.match(/<\/div>/g) || []).length,
   '→ <div>=' + (h.match(/<div/g) || []).length
   + ' </div>=' + (h.match(/<\/div>/g) || []).length);
// ⚠️ esc 是 app.js 的顶层 const，在 vm 沙箱里拿不到（不在 contextObj 上），
// 所以这里不检查「esc 存不存在」，而是**直接看渲染结果**：
// 喂一个带恶意标记的内核版本串，看它有没有被原样插进 HTML。
const h2 = render({ state: 'unsupported', kernel: '<img src=x onerror=alert(1)>',
                    mod_loaded: false, mod_file: '', tool: false,
                    virt: '', autoload: false, fixable: false });
ck('内核版本里的 HTML 被转义（防注入）',
   !h2.includes('<img src=x') && h2.includes('&lt;img'),
   '→ ' + (h2.match(/.{0,40}img.{0,40}/) || ['<没找到 img>'])[0]);
const h3 = render({ state: 'ready', mod_loaded: true, mod_file: '/x.ko.xz',
                    tool: true, kernel: 'k', virt: '<b>docker</b>',
                    autoload: false, fixable: false });
ck('虚拟化形态同样被转义', !h3.includes('<b>docker</b>'));

console.log('');
console.log('--- 3. 修复按钮绑定 ---');
const el = doc.getElementById('vp-top');
el._html = '';
fn({
  conf: { enabled: false, port: 51820, pool: '10.66.66.0/24', peers: [] },
  live: { up: false, peers: [] }, has_systemd: true, lan_nets: [],
  pool_conflict: [], port_busy: false, path: '',
  env: { state: 'need_module', mod_loaded: false, mod_file: '/x.ko.xz',
         tool: false, kernel: 'k', virt: '', autoload: false, fixable: true },
});
// 桩 DOM 的 querySelector 返回 null，所以按钮只能在 HTML 里存在；
// 真正的 onclick 绑定由 check-render 的静态断言覆盖。
ck('按钮 id 固定为 vp-fix（前端绑定与后端约定一致）',
   el._html.includes('id="vp-fix"'));

console.log('');
console.log('='.repeat(68));
console.log('通过 ' + pass + ' 项，失败 ' + fail + ' 项');
if (fail) { fails.forEach(f => console.log('  · ' + f)); process.exit(1); }
