#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成真机排版体检脚本 _dev/.live-run.js。

把 _dev/.text-probe.js 的探针源码整体嵌进一个「逐视图跑 go(k) + 探针」的
运行器里，一次性丢给真机页面的 eval 执行，避免每个视图起一次浏览器。

产物是**单个 JS 表达式**（IIFE），agent-browser eval 后返回 'started'，
结果累积在 window.__aud，由 bash 侧轮询取回。
"""
import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
probe = io.open(os.path.join(ROOT, '_dev', '.text-probe.js'),
                encoding='utf-8').read().strip()
# 探针文件是 `(() => { ... })()`；原样塞进字符串，运行时 eval 出来即可。
assert probe.startswith('(() =>') and probe.endswith(')()'), '探针格式变了'

runner = """(function () {
  var RAW = %s;
  if (!window.__aud || window.__aud.done) {
    window.__aud = { done: false, rows: [], keys: [] };
    var keys = Object.keys(VIEWS).filter(function (k) {
      try { return typeof VIEWS[k] === 'function'; } catch (e) { return false; }
    });
    window.__aud.keys = keys;
    var i = 0;
    // ⚠️ 有些视图的正文**点按钮才渲染**：依赖自检（#dep-check）的结果卡片
    //    在「开始检测」之前根本不存在 —— 只 go(k) 就量，等于量了个空页面，
    //    这就是「依赖自检页正文被压成 27px」一直没被发现的原因（2026-10-10）。
    //    这类视图：go(k) 之后先点按钮，再多等几秒让它渲染完再量。
    var PRE = { depcheck: ['#dep-check', 8000] };
    var step = function () {
      if (i >= keys.length) { window.__aud.done = true; return; }
      var k = keys[i++];
      var pre = PRE[k];
      try { go(k); } catch (e) {
        window.__aud.rows.push({ v: k, err: String(e).slice(0, 120) });
        return setTimeout(step, 60);
      }
      if (pre) {
        try {
          var b = document.querySelector(pre[0]);
          if (b) b.click();
        } catch (e) { /* ignore */ }
      }
      setTimeout(function () {
        try {
          var r = JSON.parse(eval(RAW));
          r.v = k;
          window.__aud.rows.push(r);
        } catch (e) {
          window.__aud.rows.push({ v: k, err: String(e).slice(0, 120) });
        }
        setTimeout(step, 120);
      }, pre ? pre[1] : 900);
    };
    step();
  }
  return 'started';
})()
""" % json.dumps(probe)

out = os.path.join(ROOT, '_dev', '.live-run.js')
io.open(out, 'w', encoding='utf-8', newline='\n').write(runner)
print('wrote %s (%d bytes)' % (out, len(runner.encode('utf-8'))))
