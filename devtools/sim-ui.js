// 在 Node 里用最小 DOM 桩模拟前端启动流程，验证：
// 1) 登录能拿到 token  2) loadAll 后每个页面都能渲染出内容（不再是「正在载入…」）
// 用法：node sim-ui.js [host] [port]   默认 192.168.7.3:8080
// 说明：这是"无浏览器"回归，能抓到 VIEWS 未定义、接口 404、字段名不匹配等
//       只有真实点击才会暴露的问题。
const args = process.argv.slice(2);
const HOST = args[0] || '192.168.7.3';
const PORT = Number(args[1] || 8080);
let TOKEN = '';

// ---- 极简 DOM 桩 ----
function mkEl(id) {
  const el = {
    id, _html: '', textContent: '', value: '', disabled: false, checked: false,
    className: '', tagName: 'DIV', dataset: {}, style: {}, children: [],
    classList: {
      add(c) { el.className += ' ' + c; }, remove(c) { el.className = el.className.replace(c, ''); },
      toggle() { return false; }, contains() { return false; },
    },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { el.children.push(c); },
    querySelector(s) { return document.querySelector(s); },
    querySelectorAll() { return []; },
    focus() {}, onclick: null, onchange: null, oninput: null, onkeydown: null,
    getContext() { return {}; },
  };
  return el;
}
const ELS = {};
global.document = {
  getElementById: id => (ELS[id] = ELS[id] || mkEl(id)),
  // 关键：querySelector 必须返回同一个实例（视图函数会先赋值 innerHTML 再读回来）
  querySelector: sel => {
    const key = '@sel:' + sel.replace(/^#/, '').replace(/\..*$/, '').replace(/^[^a-zA-Z0-9_-]*/, '');
    return (ELS[key] = ELS[key] || mkEl(key));
  },
  querySelectorAll: () => [],
  createElement: t => mkEl('created:' + t),
  body: mkEl('body'),
};
global.localStorage = { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } };
global.window = global;
global.setTimeout = (f, t) => 0;
global.setInterval = () => 0;
global.clearTimeout = () => {};
const http = require('http');
// 用 node 内置 http 直接请求，避免 Node 的 fetch 在纯 IPv4 环境下的兼容问题
global.fetch = (path, opts = {}) => new Promise((resolve, reject) => {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (TOKEN) headers['X-Token'] = TOKEN;
  const body = opts.body ? Buffer.from(String(opts.body)) : null;
  if (body) headers['Content-Length'] = body.length;
  const req = http.request({
    host: HOST, port: PORT, path, method: opts.method || 'GET', headers,
  }, res => {
    let data = '';
    res.on('data', c => data += c);
    res.on('end', () => resolve({
      status: res.statusCode,
      ok: res.statusCode >= 200 && res.statusCode < 300,
      json: async () => JSON.parse(data || '{}'),
      text: async () => data,
    }));
  });
  req.on('error', reject);
  if (body) req.write(body);
  req.end();
});

// ---- 载入前端逻辑（只取需要的函数，避免真跑入口绑定）----
const fs = require('fs');
let src = fs.readFileSync('../web/app.js', 'utf8');
// 去掉入口绑定与自动登录，手动驱动
src = src.replace(/\/\* ={10,} 入口 ={10,} \*\/[\s\S]*$/, '');
src += '\nmodule.exports={S,api,doLogin,loadAll,go,PAGES,VIEWS,logout};\n';
fs.writeFileSync('/tmp/_app_sim.js', src);
const app = require('/tmp/_app_sim.js');

(async () => {
  // 1. 登录
  const r = await app.api('/api/login', { method: 'POST', body: { username: 'admin', password: 'admin123' } });
  if (!r.ok) { console.log('❌ 登录失败:', r.msg_cn); process.exit(1); }
  app.S.token = r.data.token; TOKEN = r.data.token;
  console.log('✅ 登录成功, token =', TOKEN.slice(0, 14) + '...');

  // 2. loadAll
  await app.loadAll();
  console.log('✅ loadAll 完成');
  console.log('   S.buildMode =', app.S.buildMode);
  console.log('   S.ifaces    =', app.S.ifaces.map(i => i.name).join(', '));
  console.log('   S.cfg keys  =', Object.keys(app.S.cfg).join(', '));

  // 3. 逐页渲染
  const allPages = app.PAGES.filter(p => !p.sep);
  const keys = allPages.map(p => p.k);
  let bad = 0, soon = 0;
  console.log('\n=== 逐页渲染 ===');
  const vEl = document.querySelector('#view');
  for (const k of keys) {
    vEl._html = '';
    // 标注"待完善"的页面走 renderSoon 占位，属预期，跳过
    const page = allPages.find(p => p.k === k) || {};
    if (page.tag === 'soon') { console.log(`  ○ ${k.padEnd(9)} 规划中（等待作者完善）`); soon++; continue; }
    try {
      const fn = app.VIEWS[k];
      if (!fn) { console.log(`  ❌ ${k}: 无视图函数`); bad++; continue; }
      await fn();
      const h = vEl._html || '';
      // 真正的"卡在载入"是整页只剩一个占位卡（长度很短）；
      // 若页面已渲染出大量内容，只在个别子区域出现"正在载入"属正常。
      if (!h || (h.length < 400 && h.includes('正在载入'))) {
        console.log(`  ❌ ${k}: 仍是占位符 (len=${h.length})`); bad++;
      } else if (h.includes('页面渲染失败')) { console.log(`  ❌ ${k}: 渲染失败 ${h.slice(0, 90)}`); bad++; }
      else console.log(`  ✅ ${k.padEnd(9)} 渲染 ${h.length} 字符`);
    } catch (e) {
      console.log(`  ❌ ${k}: 抛异常 ${e.message}`);
      bad++;
    }
  }
  console.log(`\n已完成模块 ${keys.length - soon} 个，规划中 ${soon} 个`);
  console.log(bad === 0 ? '\nUI_SIM_OK ✔ 全部已实现页面渲染正常' : `\nUI_SIM_FAIL ❌ ${bad} 个页面异常`);
})();
