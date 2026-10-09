/* ============================ 实时柱状流动图 ============================
   概览页的「实时」卡片：负载 / 带宽 / 连接三块柱状流动图。

   为什么用 canvas 而不是 SVG/DOM 条：
   每 3 秒一个采样点、每点 2 根柱、要做「从右往左推」的动画 ——
   用 DOM 的话每帧要改几十个元素的 style/x，在 2 核机器的浏览器上
   会明显掉帧。canvas 是一次重绘，成本恒定。

   三条设计约束（都是踩过或想清楚的）：
   ① **历史窗口固定**（默认 60 个点 = 3 分钟）。点不够时从左边补空，
      不把已有点挤到左边 —— 那会让「刚才发生的事」在视觉上消失。
   ② **Y 轴自适应但有下限**：负载只有 0.05 时若自动缩放，柱高会占满整格，
      看起来像「负载很高」。所以下限取「数值的 1.5 倍，至少 1」，
      上限再留 15% 余量，避免峰值顶格。
   ③ **降采样只发生在点数超上限时**（丢一半旧点），不是每次都丢。
      否则曲线会被削平，看不出尖峰。
   ------------------------------------------------------------------------ */
(function () {
  'use strict';
  if (window.__drouterRealtime) return;
  window.__drouterRealtime = true;

  const $ = s => document.querySelector(s);
  const esc = window.__drouterEsc || (x => (x == null ? '' : String(x)));

  // 采样窗口：60 个点 × 3 秒 = 3 分钟历史。
  const MAX_POINTS = 60;
  const SAMPLE_MS = 3000;
  const DPR_CAP = 2;          // 高 DPI 下别让 canvas 太大，2 足够清晰

  let TIMER = null;
  // TCP 重传累计值（自开机以来，不是速率）
  let RT_RETRANS = 0;
  // 每块图的历史：{ series: [ [v1,v2,...], ... ], labels: [] }
  const HIST = {
    load: { series: [[], [], []], labels: [] },
    net: { series: [[], []], labels: [] },
    conn: { series: [[]], labels: [] },
  };

  /* ---------------- 采样 ---------------- */
  function push(hist, values, label) {
    // ⚠️ 降采样必须**三条 series + labels 一起做**。
    //    1.0.10 第一版是每条 series 各自判断 `if (arr.length > MAX)` ——
    //    结果：(a) `hist.series[i] = ...` 只是换了个数组引用，forEach 的
    //    迭代目标还是旧数组，后面几条 series 判断的其实是「加了 i 之前的
    //    长度」，可能一条降了另一条没降；(b) labels 单独降采样，
    //    与 series 长度对不上 → 时间轴错位（柱子画在错误的时间点上）。
    //    真跑套件抓到过这个（降采样后长度 121 = 奇数）。
    //    → 现在先 push 完，再统一判断、一起降。
    hist.series.forEach((arr, i) => {
      arr.push(values[i] == null ? 0 : values[i]);
    });
    hist.labels.push(label);
    // 超过 2 倍窗口才降采样一次（不是每次都降，否则尖峰会被削平）
    if (hist.labels.length > MAX_POINTS * 2) {
      hist.series.forEach((arr, i) => {
        hist.series[i] = arr.filter((_, k) => k % 2 === 0);
      });
      hist.labels = hist.labels.filter((_, k) => k % 2 === 0);
    }
  }

  /* ---------------- 纵轴范围 ---------------- */
  function yRange(arr, hardMin) {
    let mx = 0;
    arr.forEach(v => { if (v > mx) mx = v; });
    // 有下限，避免「负载 0.05」被自适应放大成满格
    const lo = hardMin || 0;
    let hi = Math.max(mx * 1.15, lo * 1.5, 0.0001);
    if (mx === 0) hi = lo * 1.5 || 1;
    return { lo: 0, hi: hi };
  }

  /* ---------------- 绘制一块图 ---------------- */
  /* spec: { title, unit, series:[{name,color,data}], fmt, hardMin } */
  function draw(canvas, spec) {
    const dpr = Math.min(window.devicePixelRatio || 1, DPR_CAP);
    const cssW = canvas.clientWidth || 260;
    const cssH = canvas.clientHeight || 74;
    if (canvas.width !== Math.round(cssW * dpr)
        || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }
    const g = canvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, cssW, cssH);

    const padL = 2, padR = 2, padB = 11, padT = 3;
    const w = cssW - padL - padR, h = cssH - padB - padT;
    const n = MAX_POINTS;
    const slot = w / n;
    // 每个 slot 画两条（上下行 / 三档负载）细柱
    const bw = Math.max(1.2, Math.min(4, slot / (spec.series.length * 1.7)));

    // 所有系列共用同一 Y 轴（否则两张柱没法对比）
    const all = [];
    spec.series.forEach(s => { all.push.apply(all, s.data); });
    const { lo, hi } = yRange(all, spec.hardMin);
    const span = hi - lo || 1;

    // 背景：三条淡参考线 + 刻度
    g.strokeStyle = 'rgba(128,128,128,.14)';
    g.lineWidth = 1;
    for (let k = 0; k <= 2; k++) {
      const y = padT + h * (k / 2);
      g.beginPath();
      g.moveTo(padL, Math.round(y) + 0.5);
      g.lineTo(padL + w, Math.round(y) + 0.5);
      g.stroke();
    }

    // 柱：从右往左，最新的在最右
    spec.series.forEach((s, si) => {
      g.fillStyle = s.color;
      const data = s.data;
      for (let k = 0; k < data.length && k < n; k++) {
        // data 是「尾部最新」：索引 n-1-k 越靠右越新
        const idx = n - 1 - (data.length - 1 - k);
        if (idx < 0) continue;
        const v = Math.max(0, (data[data.length - 1 - k] - lo) / span);
        const bh = Math.max(v > 0 ? 1 : 0, v * h);
        const x = padL + (n - 1 - k) * slot + slot / 2
          - (spec.series.length * bw) / 2 + si * bw;
        g.fillRect(x, padT + h - bh, bw, bh);
      }
    });

    // Y 轴上限标注
    g.fillStyle = 'rgba(128,128,128,.62)';
    g.font = '10px ui-monospace, monospace';
    g.textAlign = 'left';
    g.fillText(spec.fmt(hi), padL + 1, padT + 8);
    g.textAlign = 'right';
    g.fillText(spec.fmt(lo), padL + w - 1, padT + h - 1);

    // X 轴时间标签（左 oldest / 右 newest）
    g.textAlign = 'left';
    g.fillText(spec.timeLabels ? spec.timeLabels.left : '', padL, cssH - 1);
    g.textAlign = 'right';
    g.fillText(spec.timeLabels ? spec.timeLabels.right : '', padL + w, cssH - 1);
  }

  /* ---------------- 三块图的取数与格式化 ---------------- */
  const kb = v => (v >= 1048576 ? (v / 1048576).toFixed(1) + 'M'
    : v >= 1024 ? (v / 1024).toFixed(0) + 'K' : Math.round(v) + '');
  const kbB = v => (v >= 1073741824 ? (v / 1073741824).toFixed(2) + 'G'
    : v >= 1048576 ? (v / 1048576).toFixed(1) + 'M'
    : v >= 1024 ? (v / 1024).toFixed(0) + 'K' : Math.round(v) + '');

  /* ⚠️ SPECS 在**模块加载时求值**，所以文案不能写成 `t('系统负载')` ——
     那样切语言时不会重算（用户切英文后这四块图仍是中文标题）。
     改成 title / unit / series[].name 都是**函数**，每次绘制时现查。
     这是 MEMORY 里「模块级常量里的中文切语言不生效」的同一个坑。 */
  const SPECS = {
    load: {
      title: () => t('系统负载'), unit: () => t('1 分钟 / 5 分钟 / 15 分钟'),
      hardMin: 1,
      series: [
        { name: () => t('1 分钟'), color: '#f59e0b', data: HIST.load.series[0] },
        { name: () => t('5 分钟'), color: '#3b82f6', data: HIST.load.series[1] },
        { name: () => t('15 分钟'), color: '#8b5cf6', data: HIST.load.series[2] },
      ],
      fmt: v => v.toFixed(1),
    },
    net: {
      title: () => t('带宽'), unit: () => t('下行 / 上行'),
      hardMin: 1024,
      series: [
        { name: () => t('下行'), color: '#10b981', data: HIST.net.series[0] },
        { name: () => t('上行'), color: '#3b82f6', data: HIST.net.series[1] },
      ],
      fmt: v => kbB(v) + 'B/s',
    },
    conn: {
      title: () => t('连接'), unit: () => t('已建立'),
      hardMin: 10,
      series: [
        { name: () => t('已建立'), color: '#0ea5e9', data: HIST.conn.series[0] },
      ],
      fmt: v => String(Math.round(v)),
    },
  };
  /* SPECS 里所有文案字段都是函数，这里统一解包（省得到处写 typeof 判断）。 */
  const specText = (v) => (typeof v === 'function' ? v() : v);

  function timeLabels() {
    const now = new Date();
    const ago = new Date(now.getTime() - (MAX_POINTS * SAMPLE_MS) / 1000);
    const f = d => d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0');
    return { left: f(ago), right: f(now) };
  }

  function renderAll() {
    Object.keys(SPECS).forEach(k => {
      const cv = document.querySelector('canvas[data-rt="' + k + '"]');
      if (!cv) return;
      const s = Object.assign({}, SPECS[k]);
      s.timeLabels = timeLabels();
      draw(cv, s);
    });
    paintStats();
  }

  /* 数字摘要（均值 / 峰值）——用最近 15 个点算，够代表「刚才」 */
  function stat(series, agg) {
    const s = series.slice(-15);
    if (!s.length) return { avg: 0, max: 0 };
    const sum = s.reduce((a, b) => a + b, 0);
    return { avg: sum / s.length, max: Math.max.apply(null, s) };
  }

  function paintStats() {
    const set = (id, text) => {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    };
    const l1 = stat(HIST.load.series[0]);
    const l5 = stat(HIST.load.series[1]);
    const l15 = stat(HIST.load.series[2]);
    set('rt-l1', `${l1.avg.toFixed(2)} / ${l1.max.toFixed(2)}`);
    set('rt-l5', `${l5.avg.toFixed(2)} / ${l5.max.toFixed(2)}`);
    set('rt-l15', `${l15.avg.toFixed(2)} / ${l15.max.toFixed(2)}`);

    const dn = stat(HIST.net.series[0]);
    const up = stat(HIST.net.series[1]);
    set('rt-dn', `${kbB(dn.max)}B/s`);
    set('rt-up', `${kbB(up.max)}B/s`);

    const c = stat(HIST.conn.series[0]);
    set('rt-conn', `${Math.round(c.max)}`);
    // 重传是**累计值**（自开机以来），不是速率 —— 标出来免得被当成「刚才重传了多少」
    set('rt-retrans', RT_RETRANS ? RT_RETRANS.toLocaleString(i18n.getLang() === 'en-US' ? 'en-US' : 'zh-CN')
      + t('（累计）') : '—');
  }

  /* ---------------- 采样循环 ---------------- */
  async function tick() {
    const box = $('#realtime-box');
    if (!box) { stop(); return; }
    let m = null;
    try { m = (await api('/api/metrics?quick=1')).data; } catch (e) { return; }
    if (!m) return;
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, '0');
    const mm = String(now.getMinutes()).padStart(2, '0');
    const ss = String(now.getSeconds()).padStart(2, '0');
    const label = `${hh}:${mm}:${ss}`;

    const load = Array.isArray(m.load) ? m.load : [0, 0, 0];
    push(HIST.load, [
      Number(load[0]) || 0, Number(load[1]) || 0, Number(load[2]) || 0,
    ], label);

    const net = m.net || {};
    push(HIST.net, [
      Number(net.rx_bps) || 0, Number(net.tx_bps) || 0,
    ], label);

    push(HIST.conn, [Number((m.tcp || {}).estab) || 0], label);
    RT_RETRANS = Number((m.tcp || {}).retrans) || 0;

    renderAll();
  }

  function start() {
    if (TIMER) clearInterval(TIMER);
    tick();
    TIMER = setInterval(tick, SAMPLE_MS);
  }
  function stop() { if (TIMER) { clearInterval(TIMER); TIMER = null; } }
  window.__drouterRealtimeStop = stop;
  window.drouterRenderRealtime = start;
  if (window.i18n) window.i18n.register('realtime', renderAll);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { /* 等容器 */ });
  }
})();
