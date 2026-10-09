// 校验 PAGES 的 key 是否都在 VIEWS 里有映射。
// tag:'soon' 的占位页不需要渲染函数（点进去显示「等待作者完善」），自动排除。
const fs = require('fs');
// 以脚本自身位置定位，避免依赖运行时的当前目录（否则换个目录跑就 ENOENT）。
const WEB = require('path').join(__dirname, '..', 'web', 'app.js');
const path = process.argv[2] || WEB;
const s = fs.readFileSync(path, 'utf8');

// 惰性写法 `const NAV_GROUPS = () => ([ ... ]);` 也要认（见 t-nav-i18n-run）
const nav = s.match(/const NAV_GROUPS = (?:\(\)\s*=>\s*\()?\[([\s\S]*?)\n\]\)?;/);
if (!nav) { console.log('✘ 未找到 NAV_GROUPS'); process.exit(1); }

const items = [];
for (const m of nav[1].matchAll(/\{([^{}]*?k:\s*'([a-z0-9]+)'[^{}]*?)\}/g)) {
  const body = m[1];
  const key = m[2];
  const soon = /tag:\s*'soon'/.test(body);
  items.push({ key, soon });
}

const block = s.match(/const VIEWS = \{([\s\S]*?)\n\};/);
if (!block) { console.log('✘ 未找到 VIEWS 定义'); process.exit(1); }
const mapped = [];
for (const m of block[1].matchAll(/^\s*([a-z0-9_]+):/gm)) mapped.push(m[1]);

const real = items.filter((x) => !x.soon);
const soon = items.filter((x) => x.soon);
const missing = real.filter((x) => !mapped.includes(x.key)).map((x) => x.key);
const keys = items.map((x) => x.key);
const extra = mapped.filter((k) => !keys.includes(k));

console.log('菜单项:', items.length, '（已实现', real.length, '/ 规划中', soon.length, '）');
console.log('缺失映射:', missing.length ? missing.join(', ') + '  ✘' : '无 ✔');
console.log('多余映射:', extra.length ? extra.join(', ') : '无 ✔');
process.exit(missing.length ? 1 : 0);
