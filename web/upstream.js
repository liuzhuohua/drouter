/* ============================ 上游链路详情 ============================
   概览页的「上游链路」卡片：IPv4 / IPv6 协议详情 + DHCPv6 统计。
   独立于 app.js —— 这块要 ethtool 级的外部命令开销和独立的刷新节奏，
   塞进 690KB 的 app.js 会让首屏多背一份重量。

   ⚠️ 一条重要的诚实性约束：
   **「已连接时长」内核不提供**（/sys/class/net/<n>/ 没有该节点，
   `ip -s link` 也没有）。所以这里显示「无法获取」而**不编一个数字** ——
   用户会拿「已连接 5h55m」当判断依据（是不是要重播？），不准的数字
   比没有更糟。
   ------------------------------------------------------------------------ */
(function () {
  'use strict';
  if (window.__drouterUpstream) return;
  window.__drouterUpstream = true;

  const $ = s => document.querySelector(s);
  const esc = window.__drouterEsc || (x => {
    if (x == null) return '';
    if (typeof x === 'object') { try { x = JSON.stringify(x); } catch (e) { /* 循环引用 */ } }
    return String(x).replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  });

  let TIMER = null;

  /* DHCPv6 有效期的秒 → 「1h 4m 20s」 */
  function lifeFmt(s) {
    s = Math.floor(Number(s) || 0);
    if (s <= 0) return t('已过期');
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    let out = '';
    if (d) out += d + ` ${t('天')} `;
    if (h || d) out += h + t(' 小时 ');
    out += m + ` ${t('分')} ` + sec + ` ${t('秒')}`;
    return out.trim();
  }

  /* 已连接时长 → 「3d 5h 20m」
     ⚠️ 后端用 uptime 差值算（内核没这个字段），带 trusted 标记：
     trusted=false 说明起点是「drouter 第一次记录到它 up」的时刻，
     不一定是接口插上的时刻 —— 这时**必须如实说明**，不能装作精确。 */
  function linkUp(u) {
    if (u.uptime_s == null) {
      return `<span class="tag gray">${t('正在累计')}</span>`;
    }
    if (!u.uptime_s) {
      return `<span class="tag gray">${t('正在累计')}</span>`;
    }
    const s = Math.floor(u.uptime_s);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    let txt = '';
    if (d) txt += d + ` ${t('天')} `;
    if (h || d) txt += h + t(' 小时 ');
    txt += m + ` ${t('分')} ` + sec + ` ${t('秒')}`;
    return `<span class="mono">${esc(txt.trim())}</span>` + (
      u.uptime_trusted ? ''
      : `<span style="color:var(--txt3);font-size:12px">　${t('（从面板首次记录该接口在线时开始计，非插线时刻）')}</span>`);
  }

  function kv(k, v, hint) {
    return `<div class="kv"><b>${esc(k)}</b><span>${v}${hint
      ? `<span style="color:var(--txt3);font-size:12px">　${esc(hint)}</span>` : ''}</span></div>`;
  }

  function linkRow(u) {
    // 网桥 / MAC：用户排障时最常问的两句「走的是哪个口」「MAC 是多少」
    const dev = u.bridge
      ? `${t('网桥')} <span class="mono">${esc(u.bridge)}</span>`
      : `${t('物理接口')} <span class="mono">${esc(u.name)}</span>`;
    return kv(t('设备'), dev
      + '　MAC <span class="mono">' + esc(u.mac || '—') + '</span>');
  }

  function v6Block(u, d6) {
    const addrs = (u.addrs || []).map(a =>
      `<span class="mono">${esc(a)}</span>`).join('<br>');
    let h = `
      <div class="card up-card">
        <h3>${t('ups.v6')}<span class="tag info">${esc(bt('UPS_PROTO', u.proto_cn, 'en') || u.proto_cn || '—')}</span>
          <span class="tag ${u.valid_life ? 'ok' : 'gray'}">
            ${u.valid_life ? t('地址有效') : t('有效期未知')}</span></h3>
        ${kv(t('ups.addr'), addrs || '<span class="mono">—</span>')}
        ${u.gw ? kv(t('ups.gw'), `<span class="mono">${esc(u.gw)}</span>`) : ''}
        ${kv(t('剩余有效期'), u.valid_life
          ? `<span class="mono">${esc(lifeFmt(u.valid_life))}</span>`
          : `<span class="tag gray">${t('内核不提供该信息')}</span>`,
          u.prefer_life ? t('首选期 ') + esc(lifeFmt(u.prefer_life)) : '')}
        ${kv(t('ups.connected'), linkUp(u))}
        ${linkRow(u)}
      </div>`;

    // DHCPv6 统计
    if (d6 && d6.length) {
      const rows = d6.map(x => {
        const bad = /failures|discarded|dropped|error/.test(x.key);
        return `<div class="eth-row${bad ? ' eth-warn' : ''}">
          <span class="eth-k">${esc(x.label || x.key)}</span>
          <span class="eth-v mono ${bad && x.value > 0 ? 'cnt-bad' : ''}">${esc(x.value)}</span>
        </div>`;
      }).join('');
      h += `
        <div class="card up-card">
          <h3>${t('ups.dhcp6Stat')}<span class="tag gray">${d6.length} ${t('项')}</span></h3>
          <p class="desc">${t('来自内核')} <code>/proc/net/snmp6</code> ${t('的 dhcp6s')} ${t('计数器， 累计值（本次开机以来）。「发送失败」「丢弃的包」非')} 0 ${t('值得留意。')}</p>
          <div class="eth-grid">${rows}</div>
        </div>`;
    } else {
      h += `
        <div class="card up-card">
          <h3>DHCPv6 ${t('统计')}</h3>
          <div class="eth-box eth-off">
            <span class="tag gray">${t('ups.dhcp6Off')}</span>
            <span class="eth-reason">${t('ups.dhcp6OffTip')}</span>
          </div>
        </div>`;
    }
    return h;
  }

  function v4Block(u) {
    return `
      <div class="card up-card">
        <h3>${t('ups.v4')}<span class="tag info">${esc(bt('UPS_PROTO', u.proto_cn, 'en') || u.proto_cn || t('静态地址'))}</span>
          <span class="tag ok">${t('已连接')}</span></h3>
        ${kv(t('ups.addr'), `<span class="mono">${esc(u.addr || '—')}</span>`)}
        ${u.gw ? kv(t('ups.gw'), `<span class="mono">${esc(u.gw)}</span>`) : ''}
        ${kv('DNS', `<span class="mono">${esc(u.dns || '—')}</span>`,
          u.dns && u.dns.includes('127.0.0.1') ? t('本机缓存服务（dnsmasq）') : '')}
        ${kv(t('ups.connected'), linkUp(u))}
        ${linkRow(u)}
      </div>`;
  }

  async function render() {
    const host = $('#upstream-box');
    if (!host) return;
    let d = null;
    try {
      const r = await api('/api/upstream');
      if (r && r.ok) d = r.data;
    } catch (e) { /* 下面统一处理 */ }

    if (!d || (!d.v4 || !d.v4.length) && (!d.v6 || !d.v6.length)) {
      host.innerHTML = `<div class="card"><h3>${t('ups.title')}</h3>
        <div class="eth-box eth-off">
          <span class="tag gray">${t('ups.noUpstream')}</span>
          <span class="eth-reason">${t('ups.noUpstreamTip')}</span>
        </div></div>`;
      return;
    }
    let h = '';
    (d.v4 || []).forEach(u => { h += v4Block(u); });
    (d.v6 || []).forEach(u => { h += v6Block(u, d.dhcp6); });
    host.innerHTML = h;
  }

  /* 10 秒刷新一次：上游信息变化很慢（地址租期是小时级），
     而每个请求都要 fork 多个 ip 命令。5 秒太密，30 秒又太钝。 */
  function start() {
    if (TIMER) clearInterval(TIMER);
    TIMER = setInterval(() => {
      // 离开概览页就停 —— 否则一直在 fork ip/resolvectl
      const b = $('#upstream-box');
      if (!b) { stop(); return; }
      render();
    }, 10000);
  }
  function stop() { if (TIMER) { clearInterval(TIMER); TIMER = null; } }
  window.__drouterUpstreamStop = stop;

  window.drouterRenderUpstream = () => { render(); start(); };
  if (window.i18n) window.i18n.register('upstream',
    () => { render(); start(); });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => render());
  }
})();
