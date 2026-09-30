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
    getContext() { return {}; },
  };
  return el;
}

const ELS = new Map();
function getEl(sel) {
  const k = String(sel).replace(/^#/, '').split(/[\s>]/)[0];
  if (!ELS.has(k)) ELS.set(k, mkEl(k));
  return ELS.get(k);
}

const document = {
  getElementById: id => getEl(id),
  querySelector: getEl,
  querySelectorAll: () => [],
  createElement: t => mkEl('new:' + t),
  body: mkEl('body'),
  addEventListener() {},
};

// 假 fetch：所有 GET 返回一个宽松的 ok 响应
const CALLED = [];
async function fetchStub(url, opts) {
  CALLED.push({ url, method: (opts && opts.method) || 'GET' });
  let data = { ok: true, data: {}, msg_cn: 'ok' };
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
              daddr: '192.168.7.3', dport: '22', iface: 'ens19', msg_cn: '丢弃 SSH',
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
    return { id, name, description: name + ' 的说明', author: builtin ? 'Drouter 内置' : 'tester',
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
          mkTheme('my-theme', '我的自定义主题', false, false, '#3366cc', '#f7f9fc'),
        ],
        active: 'my-theme', active_name: '我的自定义主题',
        vars_total: 25, dir: '/etc/drouter/themes',
        css_path: '/etc/drouter/generated/theme.css' } };
    }
  }
  else if (/\/api\/services/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/snapshot\/list/.test(url)) data = { ok: true, data: [] };
  else if (/\/api\/config/.test(url)) data = { ok: true, data: {} };
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
      note: '测试数据' }, msg_cn: 'ok' };
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
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(code, ctx, { filename: file });
} catch (e) {
  console.error('脚本加载失败：' + e.message);
  process.exit(1);
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

// 带内部标签页的视图：{ state: S 上的字段名, tabs: [...], render: '渲染函数名' }
const TABBED = {
  theme: { state: 'themeTab', tabs: ['gallery', 'design', 'import'], render: 'tmRender' },
};

(async () => {
  const names = Object.keys(VIEWS);
  let bad = 0, empty = 0;
  for (const n of names) {
    try {
      const fn = VIEWS[n];
      if (typeof fn !== 'function') { console.log(`  ✗ ${n}：不是函数`); bad++; continue; }
      await fn();
      const viewEl = getEl('#view');
      const html = viewEl.innerHTML || '';
      if (process.env.DUMP_VIEW === n) {
        console.log('--- ' + n + ' 渲染 HTML（前 2500 字符）---');
        console.log(html.slice(0, 2500));
        console.log('--- 结束 ---');
      }
      if (html.length < 40) {
        console.log(`  ⚠ ${n}：渲染内容过少（${html.length} 字符）`);
        empty++;
      }
      // 带标签页的视图：逐个标签渲染一遍，否则只有默认标签会被真正执行。
      // 主题之家（#12）的三个标签页各自 $("#tm-pane").innerHTML = ...，逻辑完全不同。
      if (TABBED[n]) {
        for (const tab of TABBED[n].tabs) {
          try {
            vm.runInContext(`S.${TABBED[n].state} = ${JSON.stringify(tab)}`, ctx);
            await vm.runInContext(TABBED[n].render + '()', ctx);
            const h2 = getEl('#tm-pane').innerHTML || '';
            if (h2.length < 40) {
              console.log(`  ⚠ ${n}/${tab}：标签内容过少（${h2.length} 字符）`);
              empty++;
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
  console.log('='.repeat(60));
  console.log(`视图总数 ${names.length}｜异常 ${bad}｜疑似空渲染 ${empty}`);
  const dockerCalls = CALLED.filter(c => /\/api\/docker/.test(c.url)).length;
  console.log(`docker 接口调用次数：${dockerCalls}`);
  process.exit(bad ? 1 : 0);
})();
