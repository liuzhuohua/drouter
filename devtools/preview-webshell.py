# -*- coding: utf-8 -*-
"""生成 Web 终端外观的离线预览页（用真实 app.css，不手抄样式）。

用途：终端样式（滚动条 / 自适应高度 / 栏内按钮）改了之后，想确认效果但
不方便在浏览器里连真机时用。它把 viewWebShell() 里的那段终端 DOM 原样抠出来，
填上假的回显内容，套上真的 web/app.css，产出一个能直接打开的 HTML。

刻意不手抄样式和 DOM：手抄的预览页改完样式就过期了，还会给出「看起来对了」
的假信心。这里两样都从源码里取，所以预览和线上永远一致。

用法：
    python3 devtools/preview-webshell.py            # 写到 _preview-webshell.html
    python3 devtools/preview-webshell.py 输出路径
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 造一段"长回显"：既要够长能把容器撑到滚动，又要像真的 shell 输出。
FAKE_SB = [
    "root@debian-primaryrouter:~# ",
    "Linux debian-primaryrouter 6.1.0-18-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.1.90-1 x86_64",
    "",
    "The programs included with the Debian GNU/Linux system are free software;",
    "the exact distribution terms for each program are described in the",
    "individual files in /usr/share/doc/*/copyright.",
    "",
    "root@debian-primaryrouter:~# uname -a",
    "Linux debian-primaryrouter 6.1.0-18-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.1.90-1 x86_64 GNU/Linux",
    "root@debian-primaryrouter:~# free -h",
    "               total        used        free      shared  buff/cache   available",
    "Mem:           3.8Gi       401Mi       1.9Gi       1.0Mi       1.7Gi       3.3Gi",
    "Swap:          2.0Gi          0B       2.0Gi",
    "root@debian-primaryrouter:~# ls -la /etc/drouter/",
    "total 84",
    "drwxr-xr-x  3 root root  4096 Sep 30 21:04 .",
    "drwxr-xr-x 92 root root  4096 Sep 30 20:11 ..",
    "-rw-r--r--  1 root root  1182 Sep 30 21:04 dcfg.json",
    "-rw-r--r--  1 root root   412 Sep 30 20:11 nft_v4.conf",
    "-rw-r--r--  1 root root   186 Sep 30 20:11 nft_v6.conf",
    "-rw-------  1 root root    64 Sep 30 21:02 web-port",
    "root@debian-primaryrouter:~# systemctl status drouter-shelld --no-pager",
    "\u25cf drouter-shelld.service - drouter Web 终端 PTY 会话守护",
    "     Loaded: loaded (/etc/systemd/system/drouter-shelld.service; enabled)",
    "     Active: active (running) since Wed 2026-10-01 01:24:00 CST",
    "   Main PID: 301298 (python3)",
    "      Tasks: 5 (limit: 4608)",
    "     Memory: 13.8M",
    "        CPU: 143ms",
    "root@debian-primaryrouter:~# nft list ruleset",
    "table inet drouter_fw {",
    "        chain input {",
    "                type filter hook input priority filter; policy drop;",
    "                ct state established,related accept",
    "                iif lo accept",
    "                tcp dport 8443 accept",
    "        }",
    "}",
]


def load(rel):
    with io.open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def term_markup(app_js):
    """从 viewWebShell() 里抠出 .term-wrap 那段 DOM，原样使用。"""
    start = app_js.index('<div class="term-wrap">')
    # term-wrap 结束于紧随其后的 </div> 之后的空行 + </div>，用 textarea 结尾定位最稳
    tail = '</textarea>\n      </div>'
    end = app_js.index(tail, start) + len(tail)
    return app_js[start:end]


def main():
    app_js = load(os.path.join('web', 'app.js'))
    app_css = load(os.path.join('web', 'app.css'))
    html_markup = term_markup(app_js)

    # 注意这里查的是 HTML 属性写法 id="ws-term"，不是 CSS 选择器 #ws-term
    if 'id="ws-term"' not in html_markup or 'ws-clearbar' not in html_markup:
        print('预览生成失败：从 viewWebShell() 里没抠到预期的终端 DOM')
        return 1

    def esc(s):
        return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

    sb_html = '\n'.join('          <div class="tl">%s</div>' % esc(l)
                        for l in FAKE_SB)
    # 屏幕区补满行，视觉上就是"回显已经把可视区填满了"
    scr_rows = ['root@debian-primaryrouter:~# cat /proc/sys/kernel/pty/nr',
                '0', 'root@debian-primaryrouter:~# _']
    scr_html = '\n'.join(
        '          <div class="tl">%s</div>' % esc(l) for l in scr_rows)
    scr_html += '\n' + '\n'.join('          <div class="tl"> </div>'
                                 for _ in range(20))

    html_markup = html_markup.replace(
        '<div id="ws-scroll"></div>',
        '<div id="ws-scroll">\n%s\n        </div>' % sb_html, 1)
    html_markup = html_markup.replace(
        '<div id="ws-screen"></div>',
        '<div id="ws-screen">\n%s\n        </div>' % scr_html, 1)

    out = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Web 终端外观预览（真实 app.css）</title>
<style>
%s
</style>
<style>
  /* 仅预览页自己的外壳，不进产品代码 */
  .pv-shell{max-width:1000px;margin:0 auto;padding:22px 20px 60px}
  .pv-hd{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  .pv-hd h1{margin:0;font-size:18px}
  .pv-hd .m{font-size:12.5px;color:var(--txt2);line-height:1.7}
  .pv-note{border:1px solid var(--line);border-radius:10px;background:var(--panel);
    padding:12px 15px;margin-bottom:16px;font-size:12.5px;line-height:1.85;color:var(--txt2)}
  .pv-note b{color:var(--txt)}
  .pv-k{font-family:ui-monospace,Consolas,monospace;background:#eef2f9;
    border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-size:11.5px}
  /* 预览页里的 .ws-capture 是隐藏输入框，绝对定位会跟着滚，这里钉住不显示 */
  .pv-shell .ws-capture{display:none}
</style>
</head>
<body>
<div class="pv-shell">
  <div class="pv-hd">
    <h1>Web 终端外观预览</h1>
    <span class="m">样式直接取自 <span class="pv-k">web/app.css</span>，
      DOM 取自 <span class="pv-k">viewWebShell()</span> —— 不手抄，故与线上一致</span>
  </div>

  <div class="pv-note">
    <b>看这三处：</b><br>
    ① 黑色终端区<b>右侧没有滚动条</b>了（长短回显都一样）<br>
    ② 标题栏右侧多了 <span class="pv-k">清屏</span> 一个小按钮（滚上去还会多出一个
       <span class="pv-k">↓ 最新</span>）<br>
    ③ 终端高度随窗口高度自适应（<span class="pv-k">clamp(260px, 54vh, 560px)</span>），
       内容多了内部自动跟随最新输出，不靠拖滚动条
  </div>

%s
</div>
</body>
</html>
""" % (app_css, html_markup)

    dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, '_preview-webshell.html')
    with io.open(dst, 'w', encoding='utf-8', newline='\n') as f:
        f.write(out)
    print('已生成预览页：%s' % dst)
    print('  真实 CSS %d 字节 / 终端 DOM %d 字节' % (len(app_css), len(html_markup)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
