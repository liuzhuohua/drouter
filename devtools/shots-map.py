#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把桌面原始截图（中文名）映射成仓库内的 ASCII 序号名，并复制到 docs/screenshots/。

为什么必须改成 ASCII 文件名：
  1. GitHub Releases 资产名已被实测证明会静默剥离非 ASCII 字符
     （DOCKER-使用说明.md → DOCKER-.md），同类坑不要重复踩。
  2. 文件名里含 & （AC&AP管理中心.jpg、UPnp&NAT-PMP.jpg…）在 shell、
     Makefile、CI 里都要额外转义，属于纯粹的负债。
  3. ASCII 名方便 diff、grep、脚本批量处理，也方便日后用户自己补图。

命名规则：<分组号>-<页面短名>[-<序号>].jpg
  例如 05-dhcp.jpg / 05-dhcp-2.jpg / 05-dhcp-3.jpg
"""
import os
import shutil
import sys

SRC = r"C:\Users\lyrz-pve-win10\Desktop\drouter"
DST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "screenshots")

# (中文源文件名, 目标 ASCII 名, 图注标题)
# 顺序 = README 里的展示顺序；分组号与 README 的 8 个功能分组对应。
# 注意：这里必须写全 78 张，任何一张漏掉都会在最后被 assert 抓到。
MAP = [
    # ── 01 登入 ────────────────────────────────────────────────
    ("登入.jpg",                    "01-login.jpg",              "登录页"),

    # ── 02 概览 ────────────────────────────────────────────────
    ("概述.jpg",                    "02-overview.jpg",           "系统概览"),
    ("概述2.jpg",                   "02-overview-2.jpg",         "系统概览 · 硬件仪表盘"),
    ("概述3.jpg",                   "02-overview-3.jpg",         "系统概览 · 网络与服务状态"),
    ("网络状态&加速.jpg",           "02-netstat.jpg",            "网络状态 / 加速"),
    ("网络状态&加速2.jpg",          "02-netstat-2.jpg",          "网络状态 · 路由表与接口地址"),
    ("网络状态&加速3.jpg",          "02-netstat-3.jpg",          "网络状态 · flowtable 软加速"),

    # ── 03 接口 ────────────────────────────────────────────────
    ("网卡与桥接.jpg",              "03-iface.jpg",              "网卡与桥接"),
    ("WAN口.jpg",                   "03-wan.jpg",                "WAN 口 · 接入方式"),
    ("WAN口2.jpg",                  "03-wan-2.jpg",              "WAN 口 · 实时状态"),
    ("WAN口3.jpg",                  "03-wan-3.jpg",              "WAN 口 · 拨号日志"),
    ("PPPOE多拨.jpg",               "03-pppoe.jpg",              "PPPoE 多拨"),
    ("PPPOE多拨2.jpg",              "03-pppoe-2.jpg",            "PPPoE 多拨 · 会话状态"),
    ("LAN口.jpg",                   "03-lan.jpg",                "LAN 口"),
    ("VLAN&IPTV.jpg",               "03-vlan.jpg",               "VLAN / IPTV 单线复用"),
    ("网络唤醒(WOL).jpg",           "03-wol.jpg",                "网络唤醒 WOL"),

    # ── 04 寻址与路由 ──────────────────────────────────────────
    ("DHCP服务.jpg",                "04-dhcp.jpg",               "DHCP 服务"),
    ("DHCP服务2.jpg",               "04-dhcp-2.jpg",             "DHCP 服务 · 租约表"),
    ("DNS服务.jpg",                 "04-dns.jpg",                "DNS 服务"),
    ("IPV6&RA.jpg",                 "04-ipv6ra.jpg",             "IPv6 / RA"),
    ("IPV6&RA2.jpg",                "04-ipv6ra-2.jpg",           "IPv6 / RA · 前缀与 RDNSS"),
    ("IPV6&RA3.jpg",                "04-ipv6ra-3.jpg",           "IPv6 / RA · 运行状态"),
    ("DHCPV6&前缀委派.jpg",         "04-dhcpv6.jpg",             "DHCPv6 / 前缀委派"),
    ("动态域名解析.jpg",            "04-ddns.jpg",               "动态域名 DDNS"),
    ("动态域名解析2.jpg",           "04-ddns-2.jpg",             "动态域名 DDNS · IPv6 记录"),
    ("动态域名解析3.jpg",           "04-ddns-3.jpg",             "动态域名 DDNS · 解析结果"),
    ("真·公网IP判定.jpg",           "04-publicip.jpg",           "真·公网 IP 判定"),
    ("真·公网IP判定2.jpg",          "04-publicip-2.jpg",         "真·公网 IP 判定 · NAT 类型"),

    # ── 05 安全 ────────────────────────────────────────────────
    ("防火墙IPV4.jpg",              "05-fw4.jpg",                "防火墙 IPv4"),
    ("防火墙IPV4-2.jpg",            "05-fw4-2.jpg",              "防火墙 IPv4 · 规则与日志"),
    ("防火墙IPV6.jpg",              "05-fw6.jpg",                "防火墙 IPv6"),
    ("防火墙IPV6-2.jpg",            "05-fw6-2.jpg",              "防火墙 IPv6 · 规则与日志"),
    ("端口转发&DMZ.jpg",            "05-dnat.jpg",               "端口转发 / DMZ"),
    ("UPnp&NAT-PMP.jpg",            "05-upnp.jpg",               "UPnP / NAT-PMP"),
    ("访问控制&时间组.jpg",         "05-acl.jpg",                "访问控制 / 时间组"),

    # ── 06 服务 ────────────────────────────────────────────────
    ("智能限速QOS.jpg",             "06-qos.jpg",                "智能限速 QoS"),
    ("智能限速QOS2.jpg",            "06-qos-2.jpg",              "智能限速 QoS · 队列与规则"),
    ("应用识别DPI.jpg",             "06-dpi.jpg",                "应用识别 DPI"),
    ("应用识别DPI-2.jpg",           "06-dpi-2.jpg",              "应用识别 DPI · 识别结果"),
    ("NTP时间同步.jpg",             "06-ntp.jpg",                "NTP 时间同步"),
    ("文件共享SMB&NFS.jpg",         "06-smb.jpg",                "文件共享 SMB / NFS"),
    ("文件共享SMB&NFS-2.jpg",       "06-smb-2.jpg",              "文件共享 · 共享目录"),
    ("打印服务.jpg",                "06-cups.jpg",               "打印服务 CUPS"),
    ("打印服务2.jpg",               "06-cups-2.jpg",             "打印服务 · 打印队列"),
    ("打印服务3.jpg",               "06-cups-3.jpg",             "打印服务 · USB 直通"),
    ("打印服务4.jpg",               "06-cups-4.jpg",             "打印服务 · 共享设置"),
    ("AC&AP管理中心.jpg",           "06-acap.jpg",               "AC / AP 管理中心"),
    ("AC&AP管理中心2.jpg",          "06-acap-2.jpg",             "AC / AP · 无线与 VLAN"),

    # ── 07 工具 ────────────────────────────────────────────────
    ("WEB终端&文件.jpg",            "07-webshell.jpg",           "Web 终端 / 文件"),
    ("通用API接口.jpg",             "07-api.jpg",                "通用 API 接口"),
    ("通用API接口2.jpg",            "07-api-2.jpg",              "通用 API · OpenAPI 文档"),
    ("网络诊断工具.jpg",            "07-diag.jpg",               "网络诊断工具"),
    ("IPV6连通性测试.jpg",          "07-ipv6test.jpg",           "IPv6 连通性测试"),
    ("docker&compose.jpg",          "07-docker.jpg",             "Docker / Compose"),
    ("docker&compose2.jpg",         "07-docker-2.jpg",           "Docker · 容器与镜像"),
    ("docker&compose3.jpg",         "07-docker-3.jpg",           "Docker · Compose 项目"),
    ("docker引擎配置.jpg",          "07-dockerconf.jpg",         "Docker 引擎配置"),
    ("docker引擎配置2.jpg",         "07-dockerconf-2.jpg",       "Docker 引擎 · 镜像源测速"),
    ("docker引擎配置3.jpg",         "07-dockerconf-3.jpg",       "Docker 引擎 · daemon.json"),

    # ── 08 日志与审计 ──────────────────────────────────────────
    ("系统日志.jpg",                "08-syslog.jpg",             "系统日志"),
    ("连接与流日志.jpg",            "08-flowlog.jpg",            "连接与流日志"),
    ("连接与流日志2.jpg",           "08-flowlog-2.jpg",          "连接与流日志 · conntrack"),

    # ── 09 系统 ────────────────────────────────────────────────
    ("依赖自检与安装.jpg",          "09-deps.jpg",               "依赖自检与安装"),
    ("依赖自检与安装2.jpg",         "09-deps-2.jpg",             "依赖自检 · 探测结果"),
    ("磁盘与日志清理.jpg",          "09-cleanup.jpg",            "磁盘与日志清理"),
    ("磁盘与日志清理2.jpg",         "09-cleanup-2.jpg",          "磁盘与日志清理 · 占用明细"),
    ("内核转发与加速.jpg",          "09-kernel.jpg",             "内核转发与加速"),
    ("内核转发与加速2.jpg",         "09-kernel-2.jpg",           "内核转发 · 开关与联动"),
    ("证书&SSL.jpg",                "09-tls.jpg",                "证书 / SSL"),
    ("证书&SSL2.jpg",               "09-tls-2.jpg",              "证书 / SSL · 签发与部署"),
    ("证书&SSL3.jpg",               "09-tls-3.jpg",              "证书 / SSL · 握手体检"),
    ("电源控制.jpg",                "09-power.jpg",              "电源控制"),
    ("用户与密钥.jpg",              "09-user.jpg",               "用户与密钥"),
    ("用户与密钥2.jpg",             "09-user-2.jpg",             "用户与密钥 · SSH 公钥"),
    ("系统设置.jpg",                "09-settings.jpg",           "系统设置"),
    ("系统设置2.jpg",               "09-settings-2.jpg",         "系统设置 · 时区与主机名"),
    ("系统设置3.jpg",               "09-settings-3.jpg",         "系统设置 · Web 端口"),
    ("系统设置4.jpg",               "09-settings-4.jpg",         "系统设置 · 审计日志"),
]


def main():
    if not os.path.isdir(SRC):
        sys.exit("源目录不存在: " + SRC)

    # 完整性校验：源目录里的 jpg 必须一张不漏、一张不多地出现在 MAP 里
    on_disk = {f for f in os.listdir(SRC) if f.lower().endswith(".jpg")}
    in_map = {src for src, _, _ in MAP}
    missing = sorted(on_disk - in_map)
    ghost = sorted(in_map - on_disk)
    dup = len(MAP) - len(in_map)
    if missing or ghost or dup:
        print("!! 映射表与磁盘不一致，已中止")
        if missing:
            print("   磁盘有、MAP 里没有：", missing)
        if ghost:
            print("   MAP 里有、磁盘上没有：", ghost)
        if dup:
            print("   MAP 里有重复条目的数量：", dup)
        sys.exit(1)

    os.makedirs(DST, exist_ok=True)
    # 先清掉旧的 svg 占位图（调用方决定，这里只处理 jpg）
    for src, dst, _ in MAP:
        shutil.copy2(os.path.join(SRC, src), os.path.join(DST, dst))
    print("copied %d jpg -> %s" % (len(MAP), DST))

    # 输出一份 markdown 引用清单，供 README 章节直接取用
    print("\n" + "=" * 60)
    for _, dst, cap in MAP:
        print("%-24s %s" % (dst, cap))


if __name__ == "__main__":
    main()
