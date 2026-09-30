#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校验 README.md（及其它 md）中引用的本地文件是否真实存在。

只在「文档里写了相对路径 → 仓库里必须有这个文件」这个语义上做检查，
不联网、不检查外链（外链交给 CI 或人工）。

三种引用形式都要覆盖：
  1. Markdown 图片/链接   ![alt](path)  [text](path)
  2. HTML img src         <img src="path">
  3. 目录树里的裸路径     （不查，块里的东西本来就是示意）
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MD_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def check(md_path):
    with open(md_path, encoding="utf-8") as f:
        text = f.read()
    base = os.path.dirname(md_path)
    bad = []
    seen = set()
    for m in list(MD_RE.finditer(text)) + list(IMG_RE.finditer(text)):
        target = m.group(1).strip()
        if target in seen:
            continue
        seen.add(target)
        # 跳过外部链接、锚点、mailto 等
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
            continue
        # 去掉锚点与查询串
        p = target.split("#", 1)[0].split("?", 1)[0]
        if not p:
            continue
        full = os.path.normpath(os.path.join(base, p))
        if not os.path.exists(full):
            bad.append((target, m.start()))
    return bad, len(seen)


def main():
    total_refs = 0
    all_bad = []
    mds = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                mds.append(os.path.join(dirpath, fn))

    for md in sorted(mds):
        bad, n = check(md)
        total_refs += n
        rel = os.path.relpath(md, ROOT)
        if bad:
            print("!! %s" % rel)
            for t, _ in bad:
                print("     死链 -> %s" % t)
            all_bad.extend((rel, t) for t, _ in bad)
        else:
            print("ok  %-34s %d 个本地引用全部命中" % (rel, n))

    print("-" * 60)
    print("扫描 %d 个 md，共 %d 个本地引用" % (len(mds), total_refs))
    if all_bad:
        print("!! 发现 %d 条死链" % len(all_bad))
        return 1
    print("没有死链 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
