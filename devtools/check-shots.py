#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
截图覆盖度检查：README 的「功能」章节列了 N 个功能页，docs/screenshots/ 里有 M 张图。

这两个数字天然会漂移 —— 加了新功能页忘了截图、补了截图忘了更新 README、
重命名时漏了一张。人工数容易错，所以钉成脚本。

三件事：
  1. 逐个功能页报告「有没有截图」，缺的单独列出来
  2. 报告哪些截图没被 README 引用（可能是多余的，或忘了写进图集）
  3. 报告哪些截图引用了但文件不存在

设计取舍：
  功能页名 → 截图文件名 的对应关系靠人工维护在 GROUPS 里，不做模糊匹配。
  中文页名做子串匹配看着聪明，实际会误判（「防火墙 IPv4」会同时命中
  「防火墙 IPv6」的规则说明），而且一旦误判就是静默的假绿。
  宁可手工写死一张表 —— 反正每次加页面本来就要改 README。
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS_DIR = os.path.join(ROOT, "docs", "screenshots")
README = os.path.join(ROOT, "README.md")

# 功能页名 → [截图文件名]（不含 .jpg）
# 顺序与 README 的「功能」章节一致。空列表 = 该页暂无截图。
PAGES = [
    # ── 登入（不在 43 个功能页里，但截图集里有）────────────
    ("[登入页]",             ["01-login"]),
    ("新手向导",            []),
    ("系统概览",            ["02-overview", "02-overview-2", "02-overview-3"]),
    ("网络状态 / 加速",      ["02-netstat", "02-netstat-2", "02-netstat-3"]),
    ("网卡与桥接",          ["03-iface"]),
    ("WAN 口",              ["03-wan", "03-wan-2", "03-wan-3"]),
    ("PPPoE 多拨",          ["03-pppoe", "03-pppoe-2"]),
    ("LAN 口",              ["03-lan"]),
    ("VLAN / IPTV",         ["03-vlan"]),
    ("网络唤醒 (WOL)",       ["03-wol"]),
    ("DHCP 服务",           ["04-dhcp", "04-dhcp-2"]),
    ("DNS 服务",            ["04-dns"]),
    ("IPv6 / RA",           ["04-ipv6ra", "04-ipv6ra-2", "04-ipv6ra-3"]),
    ("DHCPv6 / 前缀委派",    ["04-dhcpv6"]),
    ("动态域名 DDNS",        ["04-ddns", "04-ddns-2", "04-ddns-3"]),
    ("真·公网 IP 判定",      ["04-publicip", "04-publicip-2"]),
    ("防火墙 IPv4",         ["05-fw4", "05-fw4-2"]),
    ("防火墙 IPv6",         ["05-fw6", "05-fw6-2"]),
    ("端口转发 / DMZ",       ["05-dnat"]),
    ("UPnP / NAT-PMP",      ["05-upnp"]),
    ("访问控制 / 时间组",     ["05-acl"]),
    ("智能限速 QoS",         ["06-qos", "06-qos-2"]),
    ("应用识别 DPI",         ["06-dpi", "06-dpi-2"]),
    ("NTP 时间同步",         ["06-ntp"]),
    ("文件共享 SMB/NFS",     ["06-smb", "06-smb-2"]),
    ("打印服务",            ["06-cups", "06-cups-2", "06-cups-3", "06-cups-4"]),
    ("AC/AP 管理中心",       ["06-acap", "06-acap-2"]),
    ("Web 终端 / 文件",      ["07-webshell"]),
    ("通用 API 接口",        ["07-api", "07-api-2"]),
    ("网络诊断工具",         ["07-diag"]),
    ("IPv6 连通性测试",      ["07-ipv6test"]),
    ("Docker / Compose",    ["07-docker", "07-docker-2", "07-docker-3"]),
    ("Docker 引擎配置",      ["07-dockerconf", "07-dockerconf-2", "07-dockerconf-3"]),
    ("系统日志",            ["08-syslog"]),
    ("连接与流日志",         ["08-flowlog", "08-flowlog-2"]),
    ("依赖自检与安装",        ["09-deps", "09-deps-2"]),
    ("磁盘与日志清理",        ["09-cleanup", "09-cleanup-2"]),
    ("内核转发与加速",        ["09-kernel", "09-kernel-2"]),
    ("证书 / SSL",          ["09-tls", "09-tls-2", "09-tls-3"]),
    ("电源控制",            ["09-power"]),
    ("用户与密钥",           ["09-user", "09-user-2"]),
    ("系统设置",            ["09-settings", "09-settings-2", "09-settings-3", "09-settings-4"]),
    ("主题之家",            []),
    ("升级与保护",           []),
]


def main():
    on_disk = {os.path.basename(p)[:-4] for p in glob.glob(os.path.join(SHOTS_DIR, "*.jpg"))}
    readme = open(README, encoding="utf-8").read()
    referenced = set(re.findall(r"docs/screenshots/([^)\s\"]+)\.jpg", readme))

    mapped = [k for _, keys in PAGES for k in keys]

    # ── 1. 功能页覆盖度 ──────────────────────────────────────
    # [xxx] 形式的条目是「不在 43 个功能页里的附加截图」，不计入覆盖度分母
    real_pages = [(p, k) for p, k in PAGES if not p.startswith("[")]
    extra_shots = [(p, k) for p, k in PAGES if p.startswith("[")]
    missing_pages = [p for p, keys in real_pages if not keys]
    print("一、功能页覆盖度")
    print("   功能页 %d 个，有截图 %d 个，缺 %d 个"
          % (len(real_pages), len(real_pages) - len(missing_pages), len(missing_pages)))
    if missing_pages:
        for p in missing_pages:
            print("     缺 → %s" % p)
    else:
        print("     全部功能页都有截图 ✓")
    if extra_shots:
        n = sum(len(k) for _, k in extra_shots)
        print("   另有附加截图 %d 张（%s）" % (n, "、".join(p.strip("[]") for p, _ in extra_shots)))
    print()

    # ── 2. 映射表 vs 磁盘 ───────────────────────────────────
    print("二、映射表 vs docs/screenshots/")
    ghost = sorted(set(mapped) - on_disk)      # 表里有、磁盘没有（漏拷/写错名）
    orphan = sorted(on_disk - set(mapped))     # 磁盘有、表里没有（漏登记）
    if ghost:
        print("     表里登记了但文件不存在（%d 个）：" % len(ghost))
        for g in ghost:
            print("       %s.jpg" % g)
    if orphan:
        print("     文件存在但没登记进映射表（%d 个）：" % len(orphan))
        for o in orphan:
            print("       %s.jpg" % o)
    if not ghost and not orphan:
        print("     一一对应 ✓（%d 张）" % len(mapped))
    print()

    # ── 3. README 引用 ──────────────────────────────────────
    print("三、README 引用 vs 实际文件")
    dead = sorted(referenced - on_disk)        # README 引用了但文件不存在
    never = sorted(on_disk - referenced)       # 文件存在但 README 没展示
    if dead:
        print("     README 引用了但文件不存在（%d 个）：" % len(dead))
        for d in dead:
            print("       %s.jpg" % d)
    if never:
        print("     文件存在但 README 没引用（%d 个）：" % len(never))
        for n in never:
            print("       %s.jpg" % n)
    if not dead and not never:
        print("     完全对应 ✓（各 %d 张）" % len(referenced))
    print()

    bad = bool(ghost or orphan or dead)
    print("=" * 60)
    if bad:
        print("发现问题（见上），请修正")
        return 1
    print("结构一致 ✓  缺图功能页 %d 个（不影响一致性，纯提示）" % len(missing_pages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
