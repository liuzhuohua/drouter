(() => {
  const v = document.querySelector('#view');
  if (!v) return JSON.stringify({ err: 'no #view' });
  const vRect = v.getBoundingClientRect();
  const docSW = document.documentElement.scrollWidth;
  const vOver = v.scrollWidth - v.clientWidth;
  const isIntentional = (el) => {
    // .tw / pre / 终端类 是**有意**的横向滚动容器，不算问题
    for (let p = el; p; p = p.parentElement) {
      const c = p.className;
      if (typeof c === 'string' && /(^|\s)(tw|term-pre|term-screen|logline|ppline)(\s|$)/.test(c)) return true;
      if (p.tagName === 'PRE') return true;
      if (p === v) break;
    }
    return false;
  };
  const items = [];
  if (vOver > 1) {
    for (const el of v.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.width < 1 && r.height < 1) continue;
      if (isIntentional(el)) continue;
      // 两类都要报：① 盒子本身越过内容区右边界；
      // ② 盒子内**内容**溢出（nowrap 长文本，rect 看不出但 scrollWidth 能）。
      const ownOver = el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1;
      if (r.right <= vRect.right + 1 && !ownOver) continue;
      items.push({
        sel: el.tagName.toLowerCase() + (typeof el.className === 'string' && el.className
          ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : ''),
        right: Math.round(r.right), w: Math.round(r.width),
        own: ownOver ? el.scrollWidth - el.clientWidth : 0,
        txt: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
      });
    }
  }
  return JSON.stringify({
    vw: window.innerWidth, docSW, vOver,
    n: items.length, items: items.slice(0, 10),
  });
})()
