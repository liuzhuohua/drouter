#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 docs/screenshots/ 下的占位截图（SVG → 直接当 PNG 用不合适，这里输出 SVG）。

设计取舍：
  README 里引用的是 .png。但仓库里没有真实截图，硬引用会得到 6 个死链。
  两种做法：
    A) 生成真 PNG（需要 Pillow 或 cairosvg，构建机上不一定有）
    B) 生成 SVG 占位，README 里改引用 .svg
  选 B：SVG 是纯文本、零依赖、可随仓库走、而且缩放不糊。
  用户日后截了真图，按同名替换 + 改扩展名即可（README 里已写明）。
"""
import os

PRI = "#1f6feb"
BG = "#eef2f9"
PANEL = "#ffffff"
LINE = "#e2e7f0"
TXT = "#161f30"
TXT2 = "#556480"
TXT3 = "#8894a8"

W, H = 1440, 900

SHOTS = [
    ("01-dash",     "系统概览",     "CPU / 内存 / 磁盘 / 负载 / 温度 + 实时网速与历史流量"),
    ("02-iface",    "网卡与桥接",   "以 MAC 为主键管理网卡，标注备注 / 角色 / 链路 / 速率 / MTU"),
    ("03-fw4",      "防火墙 IPv4",  "nftables 规则可视化编辑，支持链 / 表 / 规则级操作"),
    ("04-qos",      "智能限速 QoS", "基于 IFB 的入向整形 + 分类限速，规则实时下发"),
    ("05-flowlog",  "连接与流日志", "conntrack 连接跟踪 + 实时流日志滚动查看"),
    ("06-webshell", "Web 终端",     "浏览器内直接操作 shell，支持多会话"),
]

def logo_mark(x, y, s=1.0):
    """复刻 web/logo.svg 的路由环，作为占位图的水印标识。"""
    return f'''<g transform="translate({x},{y}) scale({s})">
  <circle cx="24" cy="24" r="21" fill="none" stroke="{PRI}" stroke-width="2.4"
          stroke-linecap="round" stroke-dasharray="96 36" transform="rotate(-42 24 24)" opacity=".55"/>
  <path d="M11 31.5 20.5 22 27 27.5 37 15.5" fill="none" stroke="{PRI}" stroke-width="3"
        stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="11" cy="31.5" r="3.1" fill="{PRI}"/>
  <circle cx="20.5" cy="22" r="2.4" fill="{PRI}"/>
  <circle cx="27" cy="27.5" r="2.4" fill="{PRI}"/>
  <path d="M33.2 12.6h4.6v4.6" fill="none" stroke="{PRI}" stroke-width="3"
        stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M12 38.5h24" fill="none" stroke="{PRI}" stroke-width="2.4"
        stroke-linecap="round" opacity=".45"/>
</g>'''

def skeleton(w, h, x, y, rows=4, gap=14):
    """画一块灰色的内容骨架，暗示「这里将来是真的界面」。"""
    out = []
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" '
               f'fill="{PANEL}" stroke="{LINE}"/>')
    yy = y + 22
    for i in range(rows):
        # 行宽交替，看起来像文字块
        ww = int(w * (0.72 if i % 2 == 0 else 0.52))
        out.append(f'<rect x="{x+20}" y="{yy}" width="{ww}" height="10" rx="5" '
                   f'fill="{LINE}"/>')
        yy += 10 + gap
    return "\n  ".join(out)

def svg(key, title, desc):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"
     width="{W}" height="{H}" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',system-ui,sans-serif">
  <rect width="{W}" height="{H}" fill="{BG}"/>

  <!-- 侧边栏 -->
  <rect x="0" y="0" width="236" height="{H}" fill="{PANEL}"/>
  <line x1="236" y1="0" x2="236" y2="{H}" stroke="{LINE}"/>
  {logo_mark(18, 14, 0.9)}
  <text x="58" y="34" font-size="17" font-weight="700" fill="{TXT}">Drouter</text>
  <text x="58" y="50" font-size="11" fill="{TXT3}">Debian 13 路由管理</text>

  <!-- 侧栏分组骨架 -->
  <g opacity=".85">
    <rect x="14" y="86" width="208" height="30" rx="8" fill="{PRI}" opacity=".10"/>
    <rect x="26" y="96" width="96" height="10" rx="5" fill="{PRI}" opacity=".55"/>
    <rect x="26" y="132" width="128" height="10" rx="5" fill="{LINE}"/>
    <rect x="26" y="164" width="104" height="10" rx="5" fill="{LINE}"/>
    <rect x="26" y="196" width="140" height="10" rx="5" fill="{LINE}"/>
    <rect x="26" y="228" width="88"  height="10" rx="5" fill="{LINE}"/>
    <rect x="26" y="260" width="120" height="10" rx="5" fill="{LINE}"/>
    <rect x="26" y="292" width="100" height="10" rx="5" fill="{LINE}"/>
  </g>

  <!-- 顶栏 -->
  <rect x="236" y="0" width="{W-236}" height="62" fill="{PANEL}"/>
  <line x1="236" y1="62" x2="{W}" y2="62" stroke="{LINE}"/>
  <text x="268" y="39" font-size="19" font-weight="700" fill="{TXT}">{title}</text>
  <rect x="{W-330}" y="19" width="120" height="24" rx="12" fill="{LINE}"/>
  <circle cx="{W-56}" cy="31" r="15" fill="{PRI}" opacity=".13"/>
  <text x="{W-61}" y="36" font-size="13" font-weight="700" fill="{PRI}">A</text>

  <!-- 内容骨架 -->
  {skeleton(540, 214, 268, 92, rows=4)}
  {skeleton(540, 214, 832, 92, rows=4)}
  {skeleton(1104, 300, 268, 330, rows=6)}
  {skeleton(468, 300, 600, 330, rows=6)}
  {skeleton(468, 300, 1096, 330, rows=6)}
  {skeleton(1104, 168, 268, 660, rows=3)}

  <!-- 水印：占位提示 -->
  <g opacity=".92">
    <rect x="{W/2-330}" y="{H/2-58}" width="660" height="116" rx="16"
          fill="{PANEL}" stroke="{PRI}" stroke-opacity=".35" stroke-width="1.5"/>
    <text x="{W/2}" y="{H/2-14}" font-size="21" font-weight="700" fill="{TXT}"
          text-anchor="middle">【占位图】{title}</text>
    <text x="{W/2}" y="{H/2+16}" font-size="14" fill="{TXT2}"
          text-anchor="middle">{desc}</text>
    <text x="{W/2}" y="{H/2+42}" font-size="12" fill="{TXT3}"
          text-anchor="middle">真实界面请以安装后为准 · 替换 docs/screenshots/{key}.svg 即可</text>
  </g>
</svg>
'''

out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "docs", "screenshots")
os.makedirs(out_dir, exist_ok=True)
for key, title, desc in SHOTS:
    p = os.path.join(out_dir, key + ".svg")
    # newline="\n" 是必须的：Windows 上 Python 默认把 \n 翻成 \r\n，
    # 生成的 SVG 在 git 里会被 .gitattributes 回写成 LF，导致每次重建都
    # 产生"改了又没改"的假 diff。显式指定 \n 后，生成结果与入库结果一致。
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(svg(key, title, desc))
    print("wrote", p)
print("done:", len(SHOTS), "placeholders")
