#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 docs/screenshots/ 里的截图生成「论坛可直接贴」的三种格式 URL 清单。

三种格式覆盖主流论坛：
  1. Markdown  ![alt](url)          —— GitHub / 语雀 / Obsidian / 少数派 / Discourse
  2. BBCode    [img]url[/img]       —— Discuz / phpBB / NGA / 恩山 / Chiphell / v2ex
  3. HTML      <img src="url">      —— 博客 / 自建站 / 富文本编辑器

URL 用两种：
  - 浮动版 @main   ：仓库更新后自动跟着变（想"永远最新"用这个）
  - 冻结版 @<sha>  ：锁定到某个 commit，永不改变（想"发出去的帖子永远一致"用这个）

为什么默认推荐 jsDelivr：
  它挂了 CORS、CDN 缓存 7 天、国内可达性明显好于 raw.githubusercontent.com。
  raw 也可以，但国内部分网络会连不上（被墙/被限速），帖子图片就成了白框。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "screenshots")

OWNER = "liuzhuohua"
REPO = "drouter"
BRANCH = "main"

# 缩略图说明：供 --with-caption 用，读取 shots-map 的图注
sys.path.insert(0, os.path.join(ROOT, "devtools"))


def load_captions():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sm", os.path.join(ROOT, "devtools", "shots-map.py"))
    sm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sm)
    return {dst[:-4] if dst.endswith(".jpg") else dst: cap for _, dst, cap in sm.MAP}


def jsdelivr(ref, path):
    # jsDelivr 的 GitHub 形式：/gh/<user>/<repo>@<ref>/<path>
    return "https://cdn.jsdelivr.net/gh/%s/%s@%s/%s" % (OWNER, REPO, ref, path)


def raw(ref, path):
    return "https://raw.githubusercontent.com/%s/%s/%s/%s" % (OWNER, REPO, ref, path)


def main():
    withcap = "--with-caption" in sys.argv
    commit = subprocess.run(["git", "rev-parse", "HEAD"],
                            cwd=ROOT, capture_output=True, text=True).stdout.strip()
    files = sorted(f for f in os.listdir(SHOTS) if f.lower().endswith(".jpg"))
    caps = load_captions()

    def emit(title, urlfn, fmt):
        print("=" * 96)
        print("【%s】  base=%s" % (title, urlfn(BRANCH, "docs/screenshots/").split("/docs/")[0]))
        print("=" * 96)
        for f in files:
            p = "docs/screenshots/" + f
            u = urlfn(BRANCH, p)
            cap = caps.get(f[:-4], f)
            if fmt == "md":
                line = "![%s](%s)" % (cap, u)
            elif fmt == "bb":
                line = "[img]%s[/img]" % u
            else:
                line = '<img src="%s" alt="%s">' % (u, cap)
            if withcap:
                print("%-28s %s" % (f, line))
            else:
                print(line)
        print()

    print("# Drouter 截图外链清单")
    print("# 仓库: https://github.com/%s/%s" % (OWNER, REPO))
    print("# 图片: %d 张，位于 docs/screenshots/" % len(files))
    print("# 冻结版本 commit: %s" % commit)
    print("#")
    print("# 用法：三种格式任选其一，直接整段复制到论坛/博客编辑器即可。")
    print("# 域名说明见文末「该用哪个域名」。")
    print()

    emit("① Markdown 格式  —  GitHub / 语雀 / Discourse / Obsidian", jsdelivr, "md")
    emit("② BBCode 格式  —  Discuz / phpBB / NGA / 恩山 / Chiphell / v2ex", jsdelivr, "bb")
    emit("③ HTML 格式  —  博客 / 自建站 / 富文本编辑器", jsdelivr, "html")

    print("=" * 96)
    print("该用哪个域名")
    print("=" * 96)
    print("""
  域名                                国内可达   缓存     建议
  ----------------------------------  --------  -------  --------------------------
  cdn.jsdelivr.net/gh/...             好        7 天     首选，本文档默认
  raw.githubusercontent.com/...       时好时坏  5 分钟   备选；国内可能连不上
  github.com/..../raw/main/...        同上      5 分钟   不推荐（多一次 302 跳转）
  cdn.statically.io/gh/...            较好      1 天     备选 CDN
  gcore.jsdelivr.net/gh/...           好        7 天     jsDelivr 备用节点（主站被墙时）

  实测结论（2026-09-30）：四种形式全部 200 且 CORS 全开、无防盗链，
  带外部 Referer 也能正常返回图片。可以放心当图床。

  想换域名：把 URL 里的 `cdn.jsdelivr.net/gh` 换成上表任一即可，
  其余部分原样保留。

  ⚠️ 关于「冻结版」（把 @main 换成 @<commit-sha>）：
     GitHub 的图床链接一旦仓库被删/改名就失效。若帖子要长期留存，
     建议本地也存一份原图，或者用 @<sha> 形式锁定 —— 至少内容不会被
     后续提交改变（但仓库删除依然会失效）。
""")

    print("=" * 96)
    print("冻结版（@%s）示例 —— 前 3 张" % commit[:8])
    print("=" * 96)
    for f in files[:3]:
        p = "docs/screenshots/" + f
        print("  %-24s %s" % (f, raw(commit, p)))
    print()
    print("  全文冻结版生成：把本脚本里 BRANCH 换成 commit sha 再跑一次")


if __name__ == "__main__":
    main()
