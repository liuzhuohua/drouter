/* ============================ 网络明细（邻居 / 路由 / 规则）============================
   概览页的「网络明细」卡片组。这三张表是排障时最常看的：
   邻居表回答「这台设备是谁」，路由表回答「流量往哪走」，规则表回答「策略怎么定」。

   ⚠️ 三条显示上的诚实性约束（都来自内核语义的坑）：
   ① 邻居表里的 **STALE** 不是故障 —— 它表示「这个 MAC 曾经用过，
      现在静默」。界面上必须弱化显示，否则用户看到一堆 STALE 会以为网络坏了。
      真正要标红的是 **FAILED**（ARP/ND 解析失败）。
   ② 路由表只取 **main 表**。loopback 的 local 表（`::1`、`fe80::1`）
      有十几条，全是噪音，会把真正要看的 default 路由淹掉。
   ③ IPv6 规则表里优先级 0 / 32766 是**内核自动建的**，用户没配过。
      标出来，否则用户会去「删除」它。
   ------------------------------------------------------------------------ */
(function () {
  'use strict';
  if (window.__drouterNetDetail) return;
  window.__drouterNetDetail = true;

  const $ = s => document.querySelector(s);
  const esc = window.__drouterEsc || (x => {
    if (x == null) return '';
    if (typeof x === 'object') { try { x = JSON.stringify(x); } catch (e) { /* 循环引用 */ } }
    return String(x).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  });

  let TIMER = null;

  const EMPTY = '<p class="desc" style="margin:6px 0 0">' + t('nd.empty') + '</p>';

  /* ---------- 邻居表 ---------- */
  function neighTable(list, isV6) {
    if (!list || !list.length) return EMPTY;
    // STALE 排最后：活跃的在前，用户不用往下翻
    const sorted = list.slice().sort((a, b) => {
      const rank = x => (x.failed ? 0 : (x.permanent ? 1 : (x.stale ? 3 : 2)));
      return rank(a) - rank(b) || String(a.dst).localeCompare(String(b.dst));
    });
    const rows = sorted.map(x => {
      // 状态标签：只有 FAILED 标红，STALE 灰显（它是正常的）
      let tag = '';
      if (x.failed) tag = '<span class="tag warn">' + t('nd.failed') + '</span>';
      else if (x.permanent) tag = '<span class="tag gray">' + t('nd.perm') + '</span>';
      else if (x.stale) tag = '<span class="tag gray">' + t('nd.stale') + '</span>';
      else tag = '<span class="tag ok">' + t('nd.active') + '</span>';
      return `<tr>
        <td class="mono">${esc(x.dst)}</td>
        <td class="mono">${esc(x.mac || '—')}</td>
        <td class="mono">${esc(x.dev || '—')}</td>
        <td>${tag}</td>
      </tr>`;
    }).join('');
    const stale = list.filter(x => x.stale).length;
    const failed = list.filter(x => x.failed).length;
    return `<table class="tbl">
      <thead><tr><th>${isV6 ? t('nd.ipv6Addr') : t('nd.ipv4Addr')}</th>
        <th>${t('nd.mac')}</th><th>${t('common.interface')}</th><th>${t('common.state')}</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="desc" style="margin:7px 0 0">
        ${t('nd.countTip', [list.length])}${stale ? t('nd.staleTip', [stale]) : ''}
      </p>`;
  }

  /* ---------- 路由表 ---------- */
  function routeTable(list, isV6) {
    if (!list || !list.length) return EMPTY;
    const rows = list.map(x => {
      // default 路由排在最前 —— 它最重要
      const isDef = x.dst === 'default' || x.dst === '0.0.0.0/0';
      return `<tr${isDef ? ' class="row-def"' : ''}>
        <td class="mono">${esc(x.dev || '—')}</td>
        <td class="mono">${isDef ? '<b>' + t('nd.defRoute') + '</b>' : esc(x.dst)}</td>
        <td class="mono">${esc(x.gw || '—')}</td>
        <td class="mono">${esc(x.src || '—')}</td>
        <td class="mono">${x.metric === '' ? '—' : esc(x.metric)}</td>
        <td>${esc(x.table || 'main')}</td>
        <td>${esc(x.proto || '—')}</td>
      </tr>`;
    }).join('');
    return `<table class="tbl">
      <thead><tr><th>${t('common.device')}</th><th>${t('nd.dst')}</th><th>${t('nd.gw')}</th><th>${t('nd.srcAddr')}</th>
        <th>${t('nd.metric')}</th><th>${t('nd.table')}</th><th>${t('nd.proto')}</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="desc" style="margin:7px 0 0">${t('nd.mainTableOnly')}</p>`;
  }

  /* ---------- IPv6 规则表 ---------- */
  function ruleTable(list) {
    if (!list || !list.length) return EMPTY;
    const rows = list.map(x => `<tr>
      <td class="mono">${esc(x.prio)}</td>
      <td class="mono">${esc(x.src || 'all')}</td>
      <td class="mono">${esc(x.table || '—')}</td>
      <td>${x.builtin
        ? '<span class="tag gray">' + t('nd.builtin') + '</span>'
        : '<span class="tag info">' + t('nd.userPol') + '</span>'}</td>
    </tr>`).join('');
    return `<table class="tbl">
      <thead><tr><th>${t('nd.prio')}</th><th>${t('nd.srcAddr')}</th><th>${t('nd.table')}</th><th>${t('nd.source')}</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="desc" style="margin:7px 0 0">${t('nd.ruleBuiltinTip')}</p>`;
  }

  function card(title, note, body) {
    return `<div class="card up-card">
      <h3>${esc(title)}</h3>
      <p class="desc" style="margin:0 0 4px">${note}</p>
      ${body}
    </div>`;
  }

  async function render() {
    const host = $('#netdetail-box');
    if (!host) return;
    let d = null;
    try {
      const r = await api('/api/netdetail');
      if (r && r.ok) d = r.data;
    } catch (e) { /* 下面统一处理 */ }

    if (!d) {
      host.innerHTML = card(t('网络明细'), t('邻居表 / 路由表 / 规则表'),
        `<div class="eth-box eth-off">
           <span class="tag warn">${t('读取失败')}</span>
           <span class="eth-reason">${t('后端接口无响应，请检查')} drouter-helper ${t('是否在运行')}</span>
         </div>`);
      return;
    }

    const n4 = (d.neigh4 || []).length;
    const n6 = (d.neigh6 || []).length;
    const r4 = (d.routes4 || []).length;
    const r6 = (d.routes6 || []).length;
    const ru = (d.rules6 || []).length;

    host.innerHTML =
      card(t('nd.neigh4'),
        t('nd.neighTip'),
        neighTable(d.neigh4, false)) +
      card(t('nd.neigh6'),
        t('nd.neigh6Tip'),
        neighTable(d.neigh6, true)) +
      card(t('nd.route4'),
        t('nd.route4Tip'),
        routeTable(d.routes4, false)) +
      card(t('nd.route6'),
        t('nd.route6Tip'),
        routeTable(d.routes6, true)) +
      card(t('nd.rule6'),
        t('nd.rule6Tip'),
        ruleTable(d.rules6)) +
      `<div class="up-row" style="margin-top:0">
        <span class="tag gray">${t('nd.neigh4')} ${n4}</span>
        <span class="tag gray">${t('nd.neigh6')} ${n6}</span>
        <span class="tag gray">${t('nd.route4')} ${r4}</span>
        <span class="tag gray">${t('nd.route6')} ${r6}</span>
        <span class="tag gray">${t('nd.rule6')} ${ru}</span>
      </div>`;
  }

  /* 15 秒刷新：ARP/ND 表变化以分钟计，路由表更慢。
     比上游的 10 秒慢一点 —— 邻居表在局域网繁忙时条目多，渲染成本更高。 */
  function start() {
    if (TIMER) clearInterval(TIMER);
    TIMER = setInterval(() => {
      if (!$('#netdetail-box')) { stop(); return; }
      render();
    }, 15000);
  }
  function stop() { if (TIMER) { clearInterval(TIMER); TIMER = null; } }
  window.__drouterNetDetailStop = stop;
  window.drouterRenderNetDetail = () => { render(); start(); };
  if (window.i18n) window.i18n.register('netdetail',
    () => { render(); start(); });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => render());
  }
})();
