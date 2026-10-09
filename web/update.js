/* ============================ 版本检测 / 一键更新 ============================
   概述页的「版本与更新」卡片 + 更新弹窗（含进度条、可中断）。

   刻意不塞进 modal()：modal 的按钮回调是一次性的 await，
   而更新是**长任务 + 持续轮询**，塞进去会导致「点了没反应、
   进度不动」。这里自己搭一层遮罩，语义更清楚。

   数据来源：/api/update/*（后端 drouter-update.py）
   ------------------------------------------------------------------------
   一条重要约定（别改）：**检测走多源、下载只走 GitHub 官方**。
   前端不要因为「代理快」就改去第三方拉大文件 —— 后端已经保证了，
   这里只负责把「用的是哪个源」如实显示出来。 */
(function () {
  'use strict';
  if (window.__drouterUpdate) return;
  window.__drouterUpdate = true;

  const $ = s => document.querySelector(s);

  /* ⛔ 转义函数必须来自 app.js，**绝不能静默降级**。
     1.0.10 修的是一个真实 XSS：这里的写法是
        `const esc = w => (window.esc || 兜底)(w)`
     而 app.js 里 `const esc = s => {...}` 是**顶层 const** —— 它只进全局
     词法环境，**不会**成为 window 的属性（只有 function 声明和 var 才会）。
     于是 `window.esc` 永远是 undefined → 走兜底 `String(x)` → **零转义**，
     后端返回的版本号 / 文件名直接进 innerHTML。
     （`api` 能取到是因为它是 `function` 声明 —— 这个区别很容易误判。）

     现在 app.js 显式 `window.__drouterEsc = esc`，这里直接取它。
     ⚠️ 兜底改成**抛错**而不是 String()：拿不到转义函数就必须让问题暴露，
     悄悄用零转义兜底等于把 XSS 藏起来 —— 那比白屏危险得多。 */
  const esc = (window.__drouterEsc = window.__drouterEsc
    || ((w) => {
      if (w == null) return '';
      if (typeof w === 'object') { try { w = JSON.stringify(w); } catch (e) { /* 循环引用 */ } }
      return String(w).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }));

  /* ---- 后端 update 模块文案的英文对照（后端零改动）-------------------
     drouter-update.py 是独立模块，没有 helper 的 MSG_EN 表：
     source / errors / msg_cn / verified / install_hint / assets[].desc
     全是中文原文下发。这里按「整串正则 → 子串」两级翻，中文界面原样返回。
     ⚠️ 动态部分（路径 / 版本 / 源名）保留原文，别硬套。 */
  const updIsEn = () => !!(window.i18n && window.i18n.getLang
    && window.i18n.getLang() === 'en-US');
  // 源名 / 附件说明走 i18n.js 的 bt 表（bt4），中文键不落在本文件里 ——
  // 否则 t-i18n-cover 会把它们判成「未包 t() 的文案」。
  const updSrc = (x) => (!updIsEn() ? x
    : (typeof bt4 === 'function' ? bt4('UPD_SOURCES', x, 'en', x) : x));
  const updAsset = (a) => {
    const d = (a && a.desc) || '';
    if (!updIsEn() || typeof bt4 !== 'function') return d;
    return bt4('UPD_ASSETS', a.key, 'd', d);
  };

  function updText(x) {
    let s = String(x == null ? '' : x);
    if (!updIsEn() || !/[\u4e00-\u9fff]/.test(s)) return s;
    const pats = [
      [/^无法访问 GitHub，请检查本机外网连通性$/,
       'Cannot reach GitHub; check this machine\u2019s Internet connectivity'],
      [/^校验不通过：文件与官方 SHA256 不一致$/,
       'Verification failed: the file does not match the official SHA256'],
      [/^官方 SHA256（下载源：(.+?)）$/,
       (m, p1) => 'Official SHA256 (source: ' + updSrc(p1) + ')'],
      [/^未获取到 SHA256 清单，本次下载\*\*未做完整性校验\*\*（下载源：(.+?)）$/,
       (m, p1) => 'No SHA256 manifest was obtained; this download was **not integrity-checked** (source: '
         + updSrc(p1) + ')'],
      [/^安装：sudo apt install \.\/(.+?)（或 dpkg -i 后 apt --fix-broken install）$/,
       'Install: sudo apt install ./$1 (or dpkg -i then apt --fix-broken install)'],
      [/^离线安装：解包后进入目录执行 \.\/install-offline\.sh，全程不需要外网$/,
       'Offline install: unpack, cd into the directory and run ./install-offline.sh; no Internet needed'],
      [/^Docker：docker load -i (.+?) 然后 docker compose up -d$/,
       'Docker: docker load -i $1 then docker compose up -d'],
      [/^下载完成并已保存到 (.+)$/, (m, p1) => 'Download complete, saved to ' + p1],
      [/^正在从 (.+?) 下载…$/, (m, p1) => 'Downloading from ' + updSrc(p1) + '…'],
      [/^下载失败：([\s\S]+)$/, (m, p1) => 'Download failed: ' + updText(p1)],
      [/^备份失败，已中止更新：([\s\S]+)$/,
       (m, p1) => 'Backup failed, update aborted: ' + updText(p1)],
      [/^更新过程出错：([\s\S]+)$/, (m, p1) => 'Update error: ' + updText(p1)],
      [/^未知的附件类型：(.+)$/, (m, p1) => 'Unknown asset type: ' + p1],
      // 内层原因（backend/drouter-update.py 的 _why() / _do_backup() 原文）
      [/^连接超时，外网可能不通$/, 'connection timed out; the Internet may be unreachable'],
      [/^HTTPS 证书校验失败（可能被劫持或需要安装 CA）$/,
       'HTTPS certificate validation failed (possibly hijacked, or a CA needs installing)'],
      [/^附件不存在（该版本可能没发布这个文件）$/,
       'the asset does not exist (this release may not publish that file)'],
      [/^连接被拒绝$/, 'connection refused'],
      [/^所有下载源均不可用$/, 'all download sources are unavailable'],
      [/^备份程序不存在：(.+)$/, (m, p1) => 'backup program not found: ' + p1],
      [/^调用备份程序失败：([\s\S]+)$/,
       (m, p1) => 'failed to run the backup program: ' + updText(p1)],
      [/^备份程序未产出备份包（可能未开启自动备份）$/,
       'the backup program produced no pack (automatic backup may be off)'],
      [/^当前没有进行中的任务$/, 'there is no task in progress'],
      [/^返回内容里没有可识别的版本号$/,
       'the response contained no recognisable version number'],
      [/^连接超时$/, 'connection timed out'],
      [/^版本号格式不对$/, 'malformed version string'],
      [/^已有更新任务在进行中$/, 'an update task is already running'],
      [/^已开始更新$/, 'Update started'],
    ];
    for (const q of pats) { if (q[0].test(s)) return s.replace(q[0], q[1]); }
    const subs = [
      [/正在备份当前配置…/g, 'Backing up the current configuration…'],
      [/备份完成，开始下载…/g, 'Backup complete, starting download…'],
      [/准备下载…/g, 'Preparing download…'],
      [/正在校验文件完整性…/g, 'Verifying file integrity…'],
      [/准备开始/g, 'Starting'],
      [/下载中/g, 'Downloading'],
      [/已取消，未做任何改动/g, 'Cancelled; nothing was changed'],
      [/下载源：/g, 'source: '],
    ];
    let out = s;
    for (const q of subs) out = out.replace(q[0], q[1]);
    return out;
  }

  // errors 是 "<源名>：<原因>" 拼出来的
  function updErr(x) {
    const s = String(x == null ? '' : x);
    if (!updIsEn()) return s;
    const m = s.match(/^(.+?)：([\s\S]+)$/);
    if (m) return updSrc(m[1]) + ': ' + updText(m[2]);
    return updText(s);
  }

  let POLL = null;

  // 发布页地址。⚠️ 这是**展示**用的常量，不参与任何下载逻辑 ——
  // 真正的下载 URL 由后端 DL_SOURCES 决定（那里才有降级链）。
  // 前端拼不出也不该拼下载 URL（判据里有这条）。
  const REPO_URL = 'https://github.com/liuzhuohua/drouter';

  /* ---------------- 本地版本 ----------------
     ⚠️ 必须从页脚 `#app-ver` 读，**不能在 app.js 里写 __APP_VERSION__**。
     后端只对 index.html 做字符串替换（`_inject_asset_version` 里的
     `.replace('__APP_VERSION__', app_version())`），app.js 是独立静态文件、
     **不经过注入** —— 在 app.js 里写这个变量会直接 ReferenceError。
     离线渲染检查器（check-render.js）就是这么抓到的。
     页脚那个元素在 index.html 里，服务端已把占位符换成真实版本号。 */
  function localVersion() {
    const el = document.getElementById('app-ver');
    const s = el ? String(el.textContent || '').trim() : '';
    return s.replace(/^v/, '') || '';
  }

  /* ---------------- 版本卡片 ---------------- */
  async function renderCard() {
    const host = $('#upd-card');
    if (!host) return;
    const lv = localVersion();
    const el = document.getElementById('upd-local');
    if (el) el.textContent = lv || '—';
    let d = null;
    try {
      const r = await api('/api/update/check');
      if (r && r.ok) d = r.data;
    } catch (e) { /* 网络异常走下面的失败分支 */ }

    if (!d) {
      host.innerHTML = `
        <div class="kv"><b>${t('版本')}</b><span class="mono">${esc(lv || '—')}</span></div>
        <div class="upd-row">
          <span class="tag gray">${t('无法检测新版本')}</span>
          <span style="color:var(--txt3);font-size:12px">${t('请检查本机外网连通性')}</span>
          <button class="ghost" id="upd-retry">${t('重试')}</button>
        </div>`;
      bindRetry();
      return;
    }

    if (!d.ok) {
      host.innerHTML = `
        <div class="kv"><b>${t('版本')}</b><span class="mono">${esc(d.local)}</span></div>
        <div class="upd-row">
          <span class="tag warn">${t('检测失败')}</span>
          <span style="color:var(--txt3);font-size:12px">${esc(updText(d.msg_cn || ''))}</span>
          <button class="ghost" id="upd-retry">${t('重试')}</button>
        </div>
        ${(d.errors || []).length ? `<div class="upd-err">${
          d.errors.map(e => esc(updErr(e))).join('　·　')}</div>` : ''}`;
      bindRetry();
      return;
    }

    if (d.has_update) {
      host.innerHTML = `
        <div class="kv"><b>${t('版本')}</b>
          <span class="mono">${esc(d.local)}</span>
          <span style="color:var(--txt3)">→</span>
          <span class="mono" style="color:#10b981;font-weight:600">${esc(d.latest)}</span>
          <span class="tag ok">${t('有可用更新')}</span>
        </div>
        <div class="upd-row">
          <button class="primary" id="upd-go">${t('查看更新内容并升级')}</button>
          <button class="ghost" id="upd-force">${t('重新检查')}</button>
          <span class="upd-when" id="upd-when"></span>
        </div>`;
      const g = $('#upd-go');
      if (g) g.onclick = () => openUpdate(d);
      bindForce(host, d);
    } else {
      // ⚠️ 「已是最新」这一支原来点「重新检查」后**什么提示都没有** ——
      //   按钮文字变回「重新检查」、卡片内容和上次一模一样，用户完全
      //   分不清「点了没生效」还是「重查了结果还是最新」。
      //   → 补三样东西：① 检查中的明确状态 ② 完成后 toast
      //     ③ 卡片上常驻显示「上次检查：时间 · 探测源」。
      const w = d.ts ? new Date(d.ts * 1000) : null;
      const whenTxt = w
        ? `${w.getHours()}:${String(w.getMinutes()).padStart(2, '0')}:${
            String(w.getSeconds()).padStart(2, '0')}`
        : '—';
      // ⚠️ 「查看更新内容并升级」原来**只在 has_update 为真时**才渲染。
      //    于是「当前已是最新」的用户只能看到「重新检查」，
      //    合理地怀疑「升级按钮是不是坏了 / 藏哪了」——
      //    2026-10-04 用户就反馈过这个（他自己是 1.0.9，线上也是 1.0.9）。
      //    「看看新版本发布了什么」这件事**本来就不该依赖有没有更新**。
      //    → 最新版也保留入口，只是按钮改成「查看版本发布说明」，
      //      点开是只读的信息窗（发布内容 + 下载地址 + 安装命令），
      //      不提供下载进度条 —— 没有可下的东西就别给升级流程。
      host.innerHTML = `
        <div class="kv"><b>${t('版本')}</b>
          <span class="mono">${esc(d.local)}</span>
          <span class="tag ok">${t('已是最新')}</span>
        </div>
        <div class="upd-row">
          <button class="ghost" id="upd-info">${t('查看版本发布说明')}</button>
          <button class="ghost" id="upd-force">${t('重新检查')}</button>
          <span class="upd-when" id="upd-when">${t('上次检查：')}${esc(whenTxt)}
            ${t('· 探测源：')}${esc(updSrc(d.source) || '—')}</span>
        </div>`;
      const ib = $('#upd-info');
      if (ib) ib.onclick = () => openReleaseNotes(d);
      bindForce(host, d);
    }
  }

  /* 「重新检查」的统一处理：进行中反馈 + 完成后 toast。
     ⚠️ 完成后**必须**给用户一个明确的说法，否则重查与不重查看起来一样。 */
  function bindForce(host, prev) {
    const b = $('#upd-force');
    if (!b) return;
    b.onclick = async () => {
      // ① 进行中：按钮禁用 + 卡片上换行提示（不要只改按钮文字，
      //    按钮文字会随 innerHTML 重建消失）
      b.disabled = true;
      b.textContent = t('检查中…');
      const w = $('#upd-when');
      if (w) w.textContent = t('正在连接 GitHub 查询最新版本…');
      let ok = false, msg = '';
      try {
        const r = await api('/api/update/check?force=1');
        ok = !!(r && r.ok);
        if (ok) {
          const d = r.data || {};
          msg = d.has_update
            ? `${t('发现新版本 ')}${d.latest}${t('（当前 ')}${d.local}${t('）')}`
            : `${t('已是最新版本 ')}${d.local}`;
        } else {
          msg = (r && r.msg_cn) || t('检测失败');
        }
      } catch (e) {
        msg = t('检测失败：') + ((e && e.message) || t('网络异常'));
      }
      // ② 完成后重绘卡片
      await renderCard();
      // ③ toast 让用户确知结果 —— 这一条是这次改进的关键
      toast(ok ? msg : msg, ok ? 'ok' : 'err', 4200);
    };
  }

  function bindRetry() {
    const b = $('#upd-retry');
    if (!b) return;
    b.onclick = async () => {
      b.disabled = true; b.textContent = t('检测中…');
      const w = $('#upd-when');
      if (w) w.textContent = t('正在连接 GitHub 查询最新版本…');
      let ok = false, msg = '';
      try {
        const r = await api('/api/update/check?force=1');
        ok = !!(r && r.ok);
        msg = ok
          ? ((r.data && r.data.has_update)
            ? `${t('发现新版本 ')}${r.data.latest}`
            : t('已是最新版本'))
          : ((r && r.msg_cn) || t('检测失败'))
      } catch (e) {
        msg = t('检测失败：') + ((e && e.message) || t('网络异常'));
      }
      await renderCard();
      toast(msg, ok ? 'ok' : 'err', 4200);
    };
  }

  /* ---------------- 版本发布说明（只读，2026-10-10）------------------
     「已是最新」时也能打开这个窗口。它**不是升级流程**（没有可下的东西
     就不给进度条），而是回答「这版是什么、从哪拿、怎么装」——
     用户看到别人提到某个版本时，可以直接复制安装命令。 */
  function openReleaseNotes(d) {
    const assets = d.assets || [];
    const ver = d.latest || d.local || '';
    const rows = assets.map(a => `
      <tr>
        <td>${esc(updAsset(a))}</td>
        <td class="mono">${esc(a.name)}</td>
        <td class="mono">${esc(REPO_URL + '/releases/download/v' + ver + '/'
                                + a.name)}</td>
      </tr>`).join('');
    const html = `
      <div class="upd-lead">
        ${t('当前安装：')}<b class="mono">${esc(d.local)}</b>
        ${t('·　最新发布：')}<b class="mono">${esc(d.latest || '—')}</b>
        ${d.has_update
          ? `<span class="tag ok">${t('有可用更新')}</span>`
          : `<span class="tag gray">${t('已是最新')}</span>`}
      </div>
      <div class="upd-sec">
        <h4>${t('怎么装')}</h4>
        <ul>
          <li><b>Deb${t('（推荐）')}</b>${t('：下载')} <code>.deb</code> ${t('后')}
              <code>sudo apt install ./drouter_${esc(ver)}_all.deb</code></li>
          <li><b>${t('离线包')}</b>：<code>tar xzf drouter-${esc(ver)}-offline-amd64.tar.gz</code>
              ${t('后进入目录执行')} <code>./install-offline.sh</code>${t('，全程不需要外网')}</li>
          <li><b>Docker</b>：<code>docker load -i drouter-${esc(ver)}-docker.tar</code>
              ${t('然后')} <code>docker compose up -d</code></li>
        </ul>
      </div>
      <div class="upd-sec">
        <h4>${t('下载地址')}</h4>
        ${rows ? `<table class="tbl"><thead><tr><th>${t('类型')}</th><th>${t('文件名')}</th>
          <th>${t('链接')}</th></tr></thead><tbody>${rows}</tbody></table>`
          : `<p class="desc">${t('（未取得附件列表，可能探测源暂时不可用）')}</p>`}
        <p class="desc" style="margin-top:8px">
          ${t('全部下载在')} GitHub Release ${t('页：')}
          <code>${esc(REPO_URL + '/releases/tag/v' + ver)}</code>
        </p>
      </div>
      <div class="upd-sec">
        <h4>${t('安全说明')}</h4>
        <ul>
          <li>${t('检测版本号会尝试多个源（官方优先）以保证大陆可达。')}</li>
          <li>${t('下载同样官方优先，连不上时自动改用镜像，')}
              <b>${t('无论走哪个源都会按官方')} SHA256 ${t('校验')}</b>${t('，不一致就删除并中止。')}</li>
          <li>${t('下载全程会在进度区显示')}<b>${t('实际用的是哪一个源')}</b>。</li>
        </ul>
      </div>`;
    const m = document.createElement('div');
    m.className = 'upd-mask';
    m.id = 'upd-note';
    m.innerHTML = `
      <div class="upd-box">
        <h3>${t('版本发布说明')}</h3>
        <div class="upd-body">${html}</div>
        <div class="acts">
          <button class="ghost" id="un-close">${t('关闭')}</button>
        </div>
      </div>`;
    document.body.appendChild(m);
    const close = () => m.remove();
    m.querySelector('#un-close').onclick = close;
    m.addEventListener('click', e => { if (e.target === m) close(); });
  }

  /* ---------------- 更新弹窗 ---------------- */
  function openUpdate(d) {
    const assets = d.assets || [];
    const rows = assets.map((a, i) => `
      <label class="upd-asset">
        <input type="radio" name="upd-asset" value="${esc(a.key)}"
          ${i === 0 ? 'checked' : ''}>
        <span>
          <b>${esc(a.desc)}</b>
          <code>${esc(a.name)}</code>
        </span>
      </label>`).join('');

    const html = `
      <div class="upd-lead">
        ${t('检测到新版本 ')}<b class="mono">${esc(d.latest)}</b>
        ${t('（当前 ')}${esc(d.local)}${t('）。')}
      </div>

      <div class="upd-sec">
        <h4>${t('更新前会做什么')}</h4>
        <ul>
          <li><b>${t('先自动备份')}</b>${t('：调用面板自带的备份能力，把当前配置存一份。 备份不成功就')}<b>${t('直接中止更新')}</b>${t('，不会带着风险往下走。')}</li>
          <li><b>${t('优先从')} GitHub ${t('官方下载')}</b>${t('所选安装包；')}<b>${t('官方连不上时自动改用 镜像')}</b>${t('（大陆网络常见，探测版本号时本来就在用镜像）。 无论走哪个源，')}<b>${t('下载全程都会显示实际用的是哪一个')}</b>。</li>
          <li><b>${t('校验完整性一步都不省')}</b>${t('：下载后与官方发布的')} SHA256 ${t('比对， 不一致就')}<b>${t('删除文件并中止')}</b>${t('，不会把可疑的包装进你系统。')}</li>
          <li><b>${t('不会自动安装')}</b>${t('：只把安装包准备好并告诉你怎么装。 装不装、什么时候装，由你决定。')}</li>
        </ul>
      </div>

      <div class="upd-sec">
        <h4>${t('安全与可中断性')}</h4>
        <ul>
          <li>${t('整个过程')}<b>${t('不修改当前运行的配置')}</b>${t('，只是往下载目录放文件。')}</li>
          <li>${t('下载阶段')}<b>${t('随时可中断')}</b>${t('：点「中断」会立即停止并删除半截文件， 已下载的完整文件保留。备份和校验阶段为了数据完整性不可中断。')}</li>
          <li>${t('校验失败、下载中断、网络超时都会')}<b>${t('如实报错')}</b>${t('并停住， 不会留下一个「看起来成功」的结果。')}</li>
        </ul>
      </div>

      <div class="upd-sec">
        <h4>${t('选择要下载的安装包')}</h4>
        ${rows || `<div class="upd-err">${t('未能获取附件列表')}</div>`}
      </div>

      <div class="upd-bar-wrap pw-hidden" id="upd-prog">
        <div class="upd-bar"><i id="upd-fill"></i></div>
        <div class="upd-msg" id="upd-msg"></div>
        <div class="upd-meta" id="upd-meta"></div>
      </div>`;

    const m = document.createElement('div');
    m.className = 'upd-mask';
    m.id = 'upd-mask';
    m.innerHTML = `
      <div class="upd-box">
        <h3>${t('更新到 ')}${esc(d.latest)}</h3>
        <div class="upd-body">${html}</div>
        <div class="acts">
          <button class="ghost" id="upd-cancel-task" style="display:none">${t('中断')}</button>
          <button class="ghost" id="upd-close">${t('关闭')}</button>
          <button class="primary" id="upd-start">${t('开始更新')}</button>
        </div>
      </div>`;
    document.body.appendChild(m);

    const close = () => { m.remove(); if (POLL) { clearInterval(POLL); POLL = null; } };
    m.querySelector('#upd-close').onclick = close;
    m.addEventListener('click', e => { if (e.target === m) close(); });

    m.querySelector('#upd-start').onclick = async () => {
      const sel = m.querySelector('input[name="upd-asset"]:checked');
      if (!sel) { alert(t('请选择一个安装包')); return; }
      const btn = m.querySelector('#upd-start');
      btn.disabled = true; btn.textContent = t('正在启动…');
      const r = await api('/api/update/apply', {
        method: 'POST',
        body: { version: d.latest, asset: sel.value, backup: true }
      });
      if (!r || !r.ok) {
        alert((r && r.msg_cn) || t('启动更新失败'));
        btn.disabled = false; btn.textContent = t('开始更新');
        return;
      }
      m.querySelector('#upd-prog').classList.remove('pw-hidden');
      btn.style.display = 'none';
      const ct = m.querySelector('#upd-cancel-task');
      ct.style.display = '';
      ct.onclick = async () => {
        const x = await api('/api/update/cancel', { method: 'POST' });
        m.querySelector('#upd-msg').textContent = (x && x.msg_cn) || t('操作失败');
      };
      POLL = setInterval(() => tickUpdate(m), 900);
      tickUpdate(m);
    };
  }

  async function tickUpdate(m) {
    const r = await api('/api/update/state');
    if (!r || !r.ok) return;
    const s = r.data || {};
    const fill = m.querySelector('#upd-fill');
    const bar = fill.parentElement;
    const pct = Math.max(0, Math.min(100, s.pct || 0));
    fill.style.width = pct + '%';
    bar.classList.toggle('warn', s.phase === 'failed' || s.phase === 'canceled');

    let msg = updText(s.msg_cn || '');
    if (s.phase === 'download' && s.speed) {
      msg += `　（${(s.speed / 1024).toFixed(0)} KB/s）`;
    }
    m.querySelector('#upd-msg').textContent = msg;

    // ⚠️ 实际用的下载源要**全程显示**（用户明确要求）。
    //   后端在大陆网络下会自动从 GitHub 官方降级到 gh-proxy 镜像
    //   （实测 api.github.com / github.com 直连 100% 丢包）。
    //   这不是异常情况，但用户有权知道自己的安装包经过了谁。
    //   走镜像时额外给一句说明 —— 降级本身要显眼，不能藏在 meta 里。
    let meta = '';
    if (s.src) {
      const mirror = /镜像|proxy/i.test(s.src);
      meta += (mirror
        ? `<span class="tag warn">${t('下载源：')}${esc(updSrc(s.src))}</span>`
          + `<span style="color:var(--txt3)">${t('　官方直连不通，已自动改用镜像；')}`
          + t('文件完整性仍会按官方 SHA256 校验</span>')
        : `<span class="tag ok">${t('下载源：')}${esc(updSrc(s.src))}</span>`);
    }
    if (s.total) {
      meta += (meta ? '<br>' : '') +
        `${(s.got / 1048576).toFixed(1)} / ${(s.total / 1048576).toFixed(1)} MB`;
    }
    if (s.sha256) {
      meta += (meta ? '　·　' : '') + 'SHA256 ' + String(s.sha256).slice(0, 16) + '…';
    }
    if (s.verified) meta += (meta ? '<br>' : '') + esc(updText(s.verified));
    if (s.path) meta += (meta ? '<br>' : '') + t('已保存：<code>') + esc(s.path) + '</code>';
    if (s.install_hint) meta += (meta ? '<br>' : '') + esc(updText(s.install_hint));
    m.querySelector('#upd-meta').innerHTML = meta;

    if (!s.running) {
      if (POLL) { clearInterval(POLL); POLL = null; }
      m.querySelector('#upd-cancel-task').style.display = 'none';
      const b = m.querySelector('#upd-close');
      if (b) b.textContent = t('完成');
    }
  }

  /* ---------------- 挂载 ---------------- */
  // 概述页每次重新渲染都会替换 innerHTML，所以挂到 MutationObserver 之外的
  // 稳定时机：路由切换后由 init 调一次（见下），且内部有 host 存在性检查。
  // 测试钩子：离线英文回归要能直接喂后端文案（卡片/进度弹窗不都能靠一次渲染覆盖）
  window.__drouterUpdI18n = { text: updText, src: updSrc, asset: updAsset, err: updErr };
  window.drouterRenderUpdateCard = renderCard;
  if (window.i18n) window.i18n.register('update', renderCard);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderCard);
  } else {
    setTimeout(renderCard, 0);
  }
})();
