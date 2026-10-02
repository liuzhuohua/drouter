<div align="center">

<img src="web/logo.svg" width="96" height="96" alt="Drouter">

# Drouter

**Debian 13 软路由管理系统 · Web 管理面板**

*不接管你的网络，直到你亲手点下「应用」。*

[![Platform](https://img.shields.io/badge/平台-Debian%2013%20trixie-A80030?style=flat-square)](https://www.debian.org/)
[![Arch](https://img.shields.io/badge/架构-amd64%20%7C%20arm64-2f6feb?style=flat-square)](#-安装)
[![Backend](https://img.shields.io/badge/后端-Python%203%20标准库-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Frontend](https://img.shields.io/badge/前端-原生%20JS%20·%20无构建-F7DF1E?style=flat-square&logo=javascript&logoColor=black)](#-设计取舍)
[![License](https://img.shields.io/badge/许可-MIT-green?style=flat-square)](LICENSE)

</div>

---

## 目录

- [这是什么](#-这是什么)
- [特色](#-特色)
- [截图](#-截图)
- [功能](#-功能)
- [推荐配置](#-推荐配置)
- [安装](#-安装)
- [快速上手](#-快速上手)
- [注意事项](#-注意事项)
- [项目结构](#-项目结构)
- [设计与安全取舍](#-设计与安全取舍)
- [常见问题](#-常见问题)
- [更新日志](#-更新日志)
- [参与贡献](#-参与贡献)

---

## 📖 这是什么

Drouter 是一套跑在 **Debian 13 (trixie)** 上的软路由管理系统。它提供了 43 个功能页面的 Web 面板，覆盖一台家用 / 小型办公路由器需要的一切：WAN 接入、PPPoE 多拨、DHCP/DNS、IPv6、防火墙、QoS 限速、访问控制、DDNS、文件共享、Docker 管理……

但它的**核心设计目标和别人不一样**：

> **它不重新实现任何协议。**

PPPoE 交给 `ppp`，DHCP 交给 `dnsmasq`，IPv6 RA 交给 `radvd`，NAT 交给 `nftables`，时间同步交给 `chrony`。
Web 面板只做三件事：**渲染原生配置文件 → 语法预检 → 原子写入并 reload**。

这样做的好处是，你的路由器上跑的永远是发行版维护的、有安全更新的上游服务，
而不是某个"自己写了一套 DHCP 服务端"的黑盒。面板哪天不想要了，删掉它，
系统仍是一台配置完好的 Debian 路由器。

### 它和 OpenWrt / RouterOS / iStoreOS 有什么不同

| | Drouter | OpenWrt | RouterOS |
|---|---|---|---|
| **底座** | 原版 Debian 13 | 自建发行版 | 自有内核 |
| **包管理** | `apt`（25,000+ 包直接可用） | `opkg`（软件包有限） | 无 |
| **配置来源** | 原生服务配置文件（可读可改） | UCI 数据库 | 二进制配置 |
| **进系统的方式** | SSH / 本地桌面 / Web | SSH / LuCI | WinBox |
| **Docker** | 原生 `docker.io` | 需自建，限制多 | 容器支持受限 |
| **适合谁** | 有 Linux 基础、想要可审计可迁移 | 想要开箱即用 | 想要稳定商用 |

---

## ✨ 特色

### 🛡️ 一、默认不接管你的网络

这是整个项目的**第一原则**，也是最花功夫的部分。

装完 Drouter，你的网络**不会有任何变化**：

- `ip_forward` 仍是 `0` —— 不做路由转发
- 默认网关仍是原来的 —— 不抢网关
- `dnsmasq` / `radvd` / `kea` **不会**被启动 —— 不抢 53 / 67 / 547 端口
- `nftables` 规则集为空 —— 不动防火墙

你可以在一台**正在被使用的机器**上装它、慢慢配它、反复预览渲染结果，
直到某个你准备好的时刻，才让它真正生效。

所有会改变网络状态的动作，都遵循同一条链路：

```
配置入库  →  渲染预览（看清会写出什么）  →  语法预检  →  自动快照  →  原子写入  →  reload
             ↑                                                    ↑
        这一步就可以停下来                                  出问题一键回滚
```

> **为什么坚持这个设计？**
> 因为我见过太多"装个软路由结果全家断网、还得摸黑进机房"的事故。
> 一台路由器大半时间里是家里的**唯一**网络出口 —— 它不该有"试试看"这种操作。

### 🔍 二、能看见每一步在做什么

- **渲染预览**：保存前能看到渲染出的真实配置文件内容，不是"相信系统会处理"
- **依赖自检**：43 项功能依赖逐个探测，缺什么、装什么、影响哪个功能，一目了然
- **实时监控**：CPU / 内存 / 磁盘 / 进程 / 网速 / 延迟 / 抖动，2 秒刷新
- **统一日志**：系统日志、防火墙日志（IPv4+IPv6）、连接跟踪流日志、PPPoE 拨号日志集中可查
- **保存 ≠ 应用**：改配置和让它生效是**两个按钮**，中间隔着一次确认

### 🪶 三、轻到 2 核 4 GB 的小主机就够跑

| 指标 | 数值 |
|---|---|
| 后端常驻内存 | **< 25 MB** |
| 依赖 | **Python 3 标准库**（`http.server` + `ssl` + `sqlite3`） |
| 前端构建 | **无**（原生 HTML/CSS/JS，改完刷新即可） |
| npm / node_modules | **零** |
| 首次启动到可用 | **< 3 秒** |

没有 Flask、没有 FastAPI、没有 gunicorn、没有 Redis、没有 Node 运行时。
一台 2 核 4 GB 内存的小主机，内存几乎全能留给 PPPoE 转发和 Docker ——
本项目全部开发与实测都在这样一台机器上完成，**这是参考环境，不是准入门槛**：
最低 1 核 1 GB 就能起来，详见[推荐配置](#-推荐配置)。

### 🧩 四、配置可以带走

所有配置都存在 SQLite 里，但**SQLite 不是唯一配置源**：

每次「应用」都会渲染出**真正的原生配置文件**落盘。
这意味着——把面板删掉，配置还在；把 `/etc` 打包拷到另一台 Debian，直接就能用。

没有"只有它自己看得懂的配置格式"这种锁定。

### 🧯 五、有三个层次的"救命"机制

1. **快照回滚**：每次应用前自动快照，出问题回退到上一个已知良好状态
2. **紧急救援通道**：`drouter-rescue` 提供独立于主配置的最小网络恢复（默认关闭，可一键启用）
3. **构建保护模式**：`/etc/drouter/BUILD_MODE` 存在时，后端拒绝一切改网络 / 抢端口的动作

第三点是给"在正在使用的机器上装机"这个场景准备的 —— 装的时候保护，配好了再解除。

---

## 📸 截图

> 以下全部是**真实运行界面**的截图（不是效果图），取自一台 2 核 4 GB 内存的 Debian 13 虚拟机。
> 共 78 张，按功能分组放在折叠块里 —— 点开对应分组即可查看。

<div align="center">

| 系统概览 | 防火墙 IPv4 |
|---|---|
| ![系统概览](docs/screenshots/02-overview.jpg) | ![防火墙 IPv4](docs/screenshots/05-fw4.jpg) |

| 网络状态 / 加速 | 智能限速 QoS |
|---|---|
| ![网络状态](docs/screenshots/02-netstat.jpg) | ![QoS](docs/screenshots/06-qos.jpg) |

| 网卡与桥接 | Web 终端 / 文件 |
|---|---|
| ![网卡与桥接](docs/screenshots/03-iface.jpg) | ![Web 终端](docs/screenshots/07-webshell.jpg) |

| 连接与流日志 | Docker / Compose |
|---|---|
| ![流日志](docs/screenshots/08-flowlog.jpg) | ![Docker](docs/screenshots/07-docker.jpg) |

</div>

### 完整图集（78 张 · 点击分组展开）

<details>
<summary><b>① 登入</b> —— 1 张</summary>
<br>

| | |
|---|---|
| ![登录页](docs/screenshots/01-login.jpg) | |

</details>

<details>
<summary><b>② 概览</b> —— 6 张</summary>
<br>

| | |
|---|---|
| ![系统概览](docs/screenshots/02-overview.jpg)<br>系统概览 | ![硬件仪表盘](docs/screenshots/02-overview-2.jpg)<br>硬件仪表盘 |
| ![网络与服务状态](docs/screenshots/02-overview-3.jpg)<br>网络与服务状态 | ![网络状态 / 加速](docs/screenshots/02-netstat.jpg)<br>网络状态 / 加速 |
| ![路由表与接口地址](docs/screenshots/02-netstat-2.jpg)<br>路由表与接口地址 | ![flowtable 软加速](docs/screenshots/02-netstat-3.jpg)<br>flowtable 软加速 |

</details>

<details>
<summary><b>③ 接口</b> —— 9 张</summary>
<br>

| | |
|---|---|
| ![网卡与桥接](docs/screenshots/03-iface.jpg)<br>网卡与桥接 | ![WAN 口](docs/screenshots/03-wan.jpg)<br>WAN 口 · 接入方式 |
| ![WAN 口实时状态](docs/screenshots/03-wan-2.jpg)<br>WAN 口 · 实时状态 | ![WAN 口拨号日志](docs/screenshots/03-wan-3.jpg)<br>WAN 口 · 拨号日志 |
| ![PPPoE 多拨](docs/screenshots/03-pppoe.jpg)<br>PPPoE 多拨 | ![PPPoE 会话状态](docs/screenshots/03-pppoe-2.jpg)<br>PPPoE · 会话状态 |
| ![LAN 口](docs/screenshots/03-lan.jpg)<br>LAN 口 | ![VLAN / IPTV](docs/screenshots/03-vlan.jpg)<br>VLAN / IPTV 单线复用 |
| ![网络唤醒 WOL](docs/screenshots/03-wol.jpg)<br>网络唤醒 WOL | |

</details>

<details>
<summary><b>④ 寻址与路由</b> —— 12 张</summary>
<br>

| | |
|---|---|
| ![DHCP 服务](docs/screenshots/04-dhcp.jpg)<br>DHCP 服务 | ![DHCP 租约表](docs/screenshots/04-dhcp-2.jpg)<br>DHCP · 租约表 |
| ![DNS 服务](docs/screenshots/04-dns.jpg)<br>DNS 服务 | ![IPv6 / RA](docs/screenshots/04-ipv6ra.jpg)<br>IPv6 / RA |
| ![IPv6 前缀与 RDNSS](docs/screenshots/04-ipv6ra-2.jpg)<br>IPv6 / RA · 前缀与 RDNSS | ![IPv6 运行状态](docs/screenshots/04-ipv6ra-3.jpg)<br>IPv6 / RA · 运行状态 |
| ![DHCPv6 / 前缀委派](docs/screenshots/04-dhcpv6.jpg)<br>DHCPv6 / 前缀委派 | ![动态域名 DDNS](docs/screenshots/04-ddns.jpg)<br>动态域名 DDNS |
| ![DDNS IPv6 记录](docs/screenshots/04-ddns-2.jpg)<br>DDNS · IPv6 记录 | ![DDNS 解析结果](docs/screenshots/04-ddns-3.jpg)<br>DDNS · 解析结果 |
| ![真·公网 IP 判定](docs/screenshots/04-publicip.jpg)<br>真·公网 IP 判定 | ![NAT 类型检测](docs/screenshots/04-publicip-2.jpg)<br>真·公网 IP · NAT 类型 |

</details>

<details>
<summary><b>⑤ 安全</b> —— 7 张</summary>
<br>

| | |
|---|---|
| ![防火墙 IPv4](docs/screenshots/05-fw4.jpg)<br>防火墙 IPv4 | ![防火墙 IPv4 规则与日志](docs/screenshots/05-fw4-2.jpg)<br>防火墙 IPv4 · 规则与日志 |
| ![防火墙 IPv6](docs/screenshots/05-fw6.jpg)<br>防火墙 IPv6 | ![防火墙 IPv6 规则与日志](docs/screenshots/05-fw6-2.jpg)<br>防火墙 IPv6 · 规则与日志 |
| ![端口转发 / DMZ](docs/screenshots/05-dnat.jpg)<br>端口转发 / DMZ | ![UPnP / NAT-PMP](docs/screenshots/05-upnp.jpg)<br>UPnP / NAT-PMP |
| ![访问控制 / 时间组](docs/screenshots/05-acl.jpg)<br>访问控制 / 时间组 | |

</details>

<details>
<summary><b>⑥ 服务</b> —— 14 张</summary>
<br>

| | |
|---|---|
| ![智能限速 QoS](docs/screenshots/06-qos.jpg)<br>智能限速 QoS | ![QoS 队列与规则](docs/screenshots/06-qos-2.jpg)<br>QoS · 队列与规则 |
| ![应用识别 DPI](docs/screenshots/06-dpi.jpg)<br>应用识别 DPI | ![DPI 识别结果](docs/screenshots/06-dpi-2.jpg)<br>DPI · 识别结果 |
| ![NTP 时间同步](docs/screenshots/06-ntp.jpg)<br>NTP 时间同步 | ![文件共享 SMB / NFS](docs/screenshots/06-smb.jpg)<br>文件共享 SMB / NFS |
| ![共享目录](docs/screenshots/06-smb-2.jpg)<br>文件共享 · 共享目录 | ![打印服务 CUPS](docs/screenshots/06-cups.jpg)<br>打印服务 CUPS |
| ![打印队列](docs/screenshots/06-cups-2.jpg)<br>打印服务 · 打印队列 | ![USB 直通](docs/screenshots/06-cups-3.jpg)<br>打印服务 · USB 直通 |
| ![打印共享设置](docs/screenshots/06-cups-4.jpg)<br>打印服务 · 共享设置 | ![AC / AP 管理中心](docs/screenshots/06-acap.jpg)<br>AC / AP 管理中心 |
| ![AC / AP 无线与 VLAN](docs/screenshots/06-acap-2.jpg)<br>AC / AP · 无线与 VLAN | |

</details>

<details>
<summary><b>⑦ 工具</b> —— 11 张</summary>
<br>

| | |
|---|---|
| ![Web 终端 / 文件](docs/screenshots/07-webshell.jpg)<br>Web 终端 / 文件 | ![通用 API 接口](docs/screenshots/07-api.jpg)<br>通用 API 接口 |
| ![OpenAPI 文档](docs/screenshots/07-api-2.jpg)<br>通用 API · OpenAPI 文档 | ![网络诊断工具](docs/screenshots/07-diag.jpg)<br>网络诊断工具 |
| ![IPv6 连通性测试](docs/screenshots/07-ipv6test.jpg)<br>IPv6 连通性测试 | ![Docker / Compose](docs/screenshots/07-docker.jpg)<br>Docker / Compose |
| ![Docker 容器与镜像](docs/screenshots/07-docker-2.jpg)<br>Docker · 容器与镜像 | ![Docker Compose 项目](docs/screenshots/07-docker-3.jpg)<br>Docker · Compose 项目 |
| ![Docker 引擎配置](docs/screenshots/07-dockerconf.jpg)<br>Docker 引擎配置 | ![镜像源测速](docs/screenshots/07-dockerconf-2.jpg)<br>Docker 引擎 · 镜像源测速 |
| ![daemon.json](docs/screenshots/07-dockerconf-3.jpg)<br>Docker 引擎 · daemon.json | |

</details>

<details>
<summary><b>⑧ 日志与审计</b> —— 3 张</summary>
<br>

| | |
|---|---|
| ![系统日志](docs/screenshots/08-syslog.jpg)<br>系统日志 | ![连接与流日志](docs/screenshots/08-flowlog.jpg)<br>连接与流日志 |
| ![conntrack 连接表](docs/screenshots/08-flowlog-2.jpg)<br>连接与流日志 · conntrack | |

</details>

<details>
<summary><b>⑨ 系统</b> —— 15 张</summary>
<br>

| | |
|---|---|
| ![依赖自检与安装](docs/screenshots/09-deps.jpg)<br>依赖自检与安装 | ![依赖探测结果](docs/screenshots/09-deps-2.jpg)<br>依赖自检 · 探测结果 |
| ![磁盘与日志清理](docs/screenshots/09-cleanup.jpg)<br>磁盘与日志清理 | ![占用明细](docs/screenshots/09-cleanup-2.jpg)<br>磁盘清理 · 占用明细 |
| ![内核转发与加速](docs/screenshots/09-kernel.jpg)<br>内核转发与加速 | ![内核开关与联动](docs/screenshots/09-kernel-2.jpg)<br>内核转发 · 开关与联动 |
| ![证书 / SSL](docs/screenshots/09-tls.jpg)<br>证书 / SSL | ![证书签发与部署](docs/screenshots/09-tls-2.jpg)<br>证书 / SSL · 签发与部署 |
| ![SSL 握手体检](docs/screenshots/09-tls-3.jpg)<br>证书 / SSL · 握手体检 | ![电源控制](docs/screenshots/09-power.jpg)<br>电源控制 |
| ![用户与密钥](docs/screenshots/09-user.jpg)<br>用户与密钥 | ![SSH 公钥](docs/screenshots/09-user-2.jpg)<br>用户与密钥 · SSH 公钥 |
| ![系统设置](docs/screenshots/09-settings.jpg)<br>系统设置 | ![时区与主机名](docs/screenshots/09-settings-2.jpg)<br>系统设置 · 时区与主机名 |
| ![Web 端口](docs/screenshots/09-settings-3.jpg)<br>系统设置 · Web 端口 | ![审计日志](docs/screenshots/09-settings-4.jpg)<br>系统设置 · 审计日志 |

</details>

<details>
<summary><b>关于截图本身（点开）</b></summary>
<br>

- 原始分辨率 **1272 × 900**（「Web 终端 / 文件」一张为 1280 × 1024），未做压缩裁剪
- 仓库里以 **ASCII 文件名**入库（`docs/screenshots/02-overview.jpg` 这种），
  避免中文名 + `&` 在 URL 编码、CI、shell 里反复出问题
- 图片版权随项目走 **MIT**，可以自由用于介绍 / 二次分发
- 想自己重拍：登录后按 `F11` 全屏，把窗口拉到 **1272px 宽**，按本文档的分组顺序截，存成同名文件覆盖即可

</details>

<details>
<summary><b>还没截图的 3 个页面</b></summary>
<br>

43 个功能页里已有 **40 个**配了截图，还剩 3 个欢迎补充：

| 功能页 | 说明 |
|---|---|
| **新手向导** | 四步配网（外网 → 内网 → DNS → IPv6），新手第一眼就会看到，优先级最高 |
| **主题之家** | Web 主题设计与离线预览 |
| **升级与保护** | 系统升级 + 构建保护模式开关 |

补图流程：

```bash
# 1. 把截图放进 docs/screenshots/，按现有命名规则编号
#    例如 02-wizard.jpg / 09-theme.jpg / 09-upgrade.jpg
# 2. 在 README「完整图集」对应分组的折叠块里加一行
# 3. 跑一下覆盖度检查，确认没有漏登记
python3 devtools/check-shots.py
```

</details>

---

## 🎛️ 功能

43 个功能页面，分 8 组。

### 概览

| 页面 | 做什么 |
|---|---|
| **新手向导** | 四个步骤把一台裸 Debian 配成能上网的路由器：外网 → 内网 → DNS → IPv6，每步都有独立可读的成败结论 |
| **系统概览** | 硬件仪表盘（CPU / 内存 / 磁盘 / 负载 / 温度 / 虚拟化 / BIOS）+ 网络状态 + 服务状态 + IPv6 状态，含实时网速与历史累计流量 |
| **网络状态 / 加速** | 路由表（IPv4/IPv6）、接口地址、nftables flowtable 软加速开关 |

### 接口

| 页面 | 做什么 |
|---|---|
| **网卡与桥接** | 以 **MAC 为主键**管理网卡（改名后角色自动跟随），标注备注 / 角色 / 链路 / 速率 / MTU / 驱动；支持多口桥接 |
| **WAN 口** | DHCP / 静态 / PPPoE / 其它接入方式，含实时状态与拨号日志 |
| **PPPoE 多拨** | 多会话并发拨号、聚合、负载均衡策略 |
| **LAN 口** | LAN 网段与地址配置 |
| **VLAN / IPTV** | 802.1Q VLAN 子接口创建 / 删除，用于光猫 IPTV 单线复用 |
| **网络唤醒 (WOL)** | 向局域网内机器发魔术包唤醒 |

### 寻址与路由

| 页面 | 做什么 |
|---|---|
| **DHCP 服务** | 地址池、静态绑定、Option 下发；含租约表（在线设备）与「转静态 / 回收」操作 |
| **DNS 服务** | 上游 DNS 模式（运营商 / 自定义 / 合并）、自定义规则、静态解析 |
| **IPv6 / RA** | radvd 前缀 / RDNSS 配置与状态 |
| **DHCPv6 / 前缀委派** | DHCPv6 服务端与 PD 获取 |
| **动态域名 DDNS** | IPv4 + IPv6 双栈，国内（阿里 / 腾讯 / DNSPod…）与国外服务商 |
| **真·公网 IP 判定** | 判断本机拿到的是不是真公网地址（含 NAT 类型自动检测） |

### 安全

| 页面 | 做什么 |
|---|---|
| **防火墙 IPv4** | nftables 规则编辑、实时日志滚动查看（带开关） |
| **防火墙 IPv6** | 同上，IPv6 独立规则集 |
| **端口转发 / DMZ** | nftables DNAT，简洁稳定的端口映射 |
| **UPnP / NAT-PMP** | miniupnpd 开关与状态 |
| **访问控制 / 时间组** | Firewalla 风格：按设备 / 时间组 / 应用维度管控（复用 QoS + DPI 链路） |

### 服务

| 页面 | 做什么 |
|---|---|
| **智能限速 QoS** | CAKE / HTB 队列算法、DSCP 优先级（含「视频优先」规则）、按 IP / 端口流控 |
| **应用识别 DPI** | nDPI 识别库管理，多前缀代理下载更新 |
| **NTP 时间同步** | chrony 上游与状态 |
| **文件共享 SMB/NFS** | 跨平台预设模板、权限管理，支持外置设备挂载与格式化 |
| **打印服务** | CUPS 打印服务器 / USB 打印机 RAW 直通（互斥二选一），共享给手机与电脑 |
| **AC/AP 管理中心** | OpenSOHO 无线控制器：集中管理 OpenWRT AP 的 Wi-Fi / VLAN / PoE |

### 工具

| 页面 | 做什么 |
|---|---|
| **Web 终端 / 文件** | 浏览器内的 SSH 终端与文件管理器（实时输入输出回显） |
| **通用 API 接口** | 对外通用 REST API，带 OpenAPI 文档与调用范例 |
| **网络诊断工具** | ping / traceroute / mtr / iperf3 / DNS 查询 |
| **IPv6 连通性测试** | 分阶段诊断 IPv6 通不通、卡在哪一步 |
| **Docker / Compose** | 容器 / 镜像 / 网络 / 卷的日常运维 + Compose 项目管理，附 `docker run` → `docker-compose.yml` 转换 |
| **Docker 引擎配置** | `daemon.json` 可视化配置：IPv6 一键开启、国内镜像源切换与测速、日志滚动、默认网桥网段 |

### 日志与审计

| 页面 | 做什么 |
|---|---|
| **系统日志** | journal / syslog 查看与过滤 |
| **连接与流日志** | conntrack 连接表与流量日志（统一日志系统） |

### 系统

| 页面 | 做什么 |
|---|---|
| **依赖自检与安装** | 43 项依赖逐项探测 + 一键安装（区分必需 / 推荐 / 可选） |
| **磁盘与日志清理** | 回收日志 / 缓存 / 临时文件，可设阈值自动清理 |
| **内核转发与加速** | IP 转发 · 出向伪装 · MSS 钳制 · BBR · SNMP，五项内核开关，每项附说明与联动影响 |
| **证书 / SSL** | 自建 CA · 签发服务器证书 · 导入 · 部署给管理后台，附 SSL/TLS 握手体检 |
| **电源控制** | 重启 / 关机 / 定时任务 |
| **用户与密钥** | 系统用户增删改 + SSH 公钥管理（粘贴或上传 `.pub`） |
| **系统设置** | Web 端口、时区、主机名、审计日志 |
| **主题之家** | Web 主题设计与离线预览，支持 ZIP 导入导出 |
| **升级与保护** | 系统升级与构建保护模式开关 |

---

## 💻 推荐配置

### 最低配置

| | 要求 |
|---|---|
| CPU | 1 核 x86-64 |
| 内存 | 1 GB |
| 硬盘 | 8 GB |
| 网卡 | 1 个（先做 LAN + 管理口） |
| 系统 | Debian 13 (trixie) |

### 推荐配置

| | 建议 | 说明 |
|---|---|---|
| CPU | **2 核以上** | 单核也能跑，但开着 PPPoE + QoS + Docker 时会明显吃紧 |
| 内存 | **4 GB** | 2 GB 可用，但 Docker 跑起来后余量紧张 |
| 硬盘 | **16 GB SSD** | 日志和 Docker 镜像会持续增长，机械盘会让面板变卡 |
| 网卡 | **2 个千兆以上** | WAN + LAN 分开，物理隔离最省心；单口也能用 |
| 网卡芯片 | **Intel i210 / i225 / i226** | Linux 驱动成熟，支持多队列与硬件 offload |
| 供电 | 无风扇低功耗平台 (N100 / J4125) | 7×24 跑，功耗和静音比峰值性能重要 |

### 部署形态

Drouter 是普通 Debian 软件包，跑在哪都行：

- **物理机**：直接装 Debian 13，最推荐 —— 没有虚拟化开销，网卡直通无损耗
- **PVE 虚拟机**：需要开 `net.ipv4.ip_forward` 之外，还要给 VM 开 **混杂模式**，
  否则桥接的 VLAN 包会被 PVE 丢掉
- **旧笔记本 / 迷你主机**：换上 SSD 就是一台安静的路由器

### 网卡数量的选择

| 网卡数 | 拓扑 | 适用 |
|---|---|---|
| 1 | 单臂路由（LAN = WAN 同接口，靠 VLAN 或子网区分） | 最小可行，配置略绕 |
| **2** | **WAN + LAN** | **推荐**，物理隔离，最直观 |
| 3+ | WAN + LAN + IPTV / DMZ | 有 IPTV 或要隔离 IoT 设备时 |

---

## 📦 安装

### ⭐ 方式一：离线自包含包（推荐，Release 主推）

> **目标机可以完全断网 —— 没有外网、没有内网源、DNS 都不通，照样一次装完。**
> 这是本项目对「路由器系统」这个使用场景的默认交付形态。

路由器往往出现在**不方便接外网**的地方：机房、弱电箱、隔离网段、
或者一台你根本不想让它联网的机器。所以 Release 里的
`drouter-1.0.5-offline-amd64.tar.gz` 不是「只发一个 `.deb`」，
而是把 **Drouter 本体 + 它跑起来需要的全部 Debian 依赖**一起打成了一个包。

解包出来是这样：

```bash
# 1) 把 tar.gz 拷到目标机（U 盘 / scp / 内网共享都行）
# 2) 解包
tar xzf drouter-1.0.5-offline-amd64.tar.gz
cd drouter-1.0.5-offline-amd64

# 3) 一条命令装完（会自动 sudo）
sudo bash install.sh
```

装完访问 `https://<目标机IP>:8443/`。**全程不碰网络。**

它到底自包含到什么程度：

| | |
|---|---|
| **依赖包数量** | **236 个 `.deb`**（Drouter 直接依赖 + 全部传递依赖） |
| **覆盖范围** | 从 `nftables` / `dnsmasq` / `radvd` / `ppp` / `chrony` 一路递归到最底层 `libc6` / `zlib1g` / `dash` / `diffutils` / `libc-bin` |
| **本地仓库** | 附 `debs/Packages` + `Packages.gz`，apt 直接当本地 `file:` 源用 |
| **完整性校验** | `SHA256SUMS`，237 条（236 依赖 + 本体） |
| **包体积** | 约 **83 MB**（tar.gz 压缩后） |
| **系统服务** | 本体 `.deb` 内含 6 个 systemd 单元，`postinst` 自动 enable |

<details>
<summary><b>包里到底装了什么（点开看）</b></summary>

```
drouter-1.0.5-offline-amd64/
├── install.sh                 安装脚本（自动提权 + 本地 file: 源，零外网）
├── README-离线安装.md          给不熟悉 Linux 的人看的逐步说明
├── SHA256SUMS                 237 条校验和（236 依赖 + 本体）
├── drouter_1.0.5_all.deb      Drouter 本体（含 6 个 systemd 单元）
└── debs/                      236 个依赖 .deb
    ├── nftables_*.deb         ├─ 直接依赖
    ├── dnsmasq_*.deb
    ├── radvd_*.deb
    ├── ppp_*.deb
    ├── chrony_*.deb
    ├── ...
    ├── libc6_*.deb            └─ 最底层传递依赖
    ├── zlib1g_*.deb
    ├── dash_*.deb                 （Essential 包，最容易被漏掉）
    ├── diffutils_*.deb
    ├── libc-bin_*.deb
    ├── Packages                  本地仓库索引
    └── Packages.gz               （同上，gzip 版）
```

`install.sh` 做的事情很简单，但每一步都有原因：

1. **把 `debs/` 注册成一个临时 `file:` 源**，然后 `apt-get install` 里
   所有包名。这样 apt 会**自己算依赖顺序**。
   —— 比 `dpkg -i debs/*.deb` 一通乱装可靠得多：`dpkg` 不看顺序，
   装到一半就会因为「依赖还没满足」而大面积失败。
2. **只挂本地源**（`sourceparts` 指向 `/dev/null`），避免系统里原有的
   外网源被读进来、apt 跑去联网找「更新的版本」而卡死。
3. 装完报告服务状态和访问地址。

**为什么不能只发一个 `.deb` 让用户自己解决依赖：**
`.deb` 的 `Depends` 只**声明**依赖，真正把依赖装上的是 `apt`。
目标机没外网时 apt 无源可用，用户就得自己凑齐 236 个包 ——
还得算对传递依赖、别漏掉 `Essential: yes` 的包（它们**不会**出现在
`apt-cache depends --recurse` 的输出里）、版本还得跟 `trixie` 对得上。
这些坑我们已经在打包脚本里踩完了，所以直接帮你打好。

</details>

**想要更小的包？** 如果你的目标机有网（哪怕是内网自建 apt 镜像），
可以只拿 `drouter_1.0.5_all.deb`（约 445 KB），见下方「方式二」。

**已经装过依赖、只想升级本体？** 同样只用那个 445 KB 的 `.deb`
`dpkg -i` 覆盖安装即可，不必重新走离线包。

### 方式二：在线安装（目标机有网）

如果目标机能连上 Debian 官方源（或内网 apt 镜像），只需要本体那一个文件：

```bash
sudo apt install ./drouter_1.0.5_all.deb
sudo drouter-ctl status
```

> `apt install ./xxx.deb` 会自动把 `Depends` 交给 apt 去网上补齐 ——
> 这正是它能「只发一个 445 KB 文件」的前提。
> 目标机没网时请走**方式一**，否则 apt 会卡在拉依赖上。

### 方式三：Docker（体验用）

项目提供 `drouter-1.0.5-docker.tar`（约 169 MB，已含完整根文件系统）。

**方式 A：docker compose（推荐）**

Release 里附了现成的 `docker-compose.yml`，直接下下来用：

```bash
docker load -i drouter-1.0.5-docker.tar

# 把 docker-compose.yml 放到当前目录
docker compose up -d
docker compose ps          # 等 health 变 healthy
```

**方式 B：docker run**

```bash
docker load -i drouter-1.0.5-docker.tar

docker run -d --name drouter --restart unless-stopped \
  --network host \
  --cap-add NET_ADMIN --cap-add NET_RAW \
  -e TZ=Asia/Shanghai \
  -v drouter-etc:/etc/drouter \
  -v drouter-data:/opt/drouter/data \
  -v drouter-snapshots:/opt/drouter/snapshots \
  -v drouter-log:/var/log/drouter \
  drouter:1.0.5
```

两种方式加载后镜像标签都是 **`drouter:1.0.5`**（另有一个
`liuzhuohua/drouter:1.0.5` 别名，同一个镜像）。

访问 `https://<宿主IP>:8443/`，账号 `admin` / `admin123`。

关于参数，实测结论如下：

| 参数 | 必要性 | 说明 |
|---|---|---|
| `--network host` | **必需** | 容器要直接读写宿主的网络接口和 nftables 规则，bridge 模式下看不到真实网卡 |
| `--cap-add NET_ADMIN` | **必需** | 否则 `nft` 操作报 `Operation not permitted`，防火墙页会失效 |
| `--cap-add NET_RAW` | 建议 | 部分探测类功能（ping / traceroute）需要 |
| 四个命名卷 | **建议** | 配置、数据、快照、日志。不挂出来容器重建就全丢 |

> **不需要 `--privileged`**。网上很多教程图省事都写它，但 Drouter 只需要
> `NET_ADMIN` + `NET_RAW`。给 `privileged` 等于把宿主机完全敞开 ——
> 除非在排查权限问题，否则用最小权限就好。

> **端口由配置文件决定，不是环境变量**。`network_mode: host` 下 `ports:` 会被
> Docker 忽略。端口存在 `/etc/drouter/web-port`，默认 8443，
> 要改请去界面改（`DROUTER_WEB_PORT` 环境变量只在**首次启动**播种，之后会失效，
> 这是故意的 —— 见 `packaging/docker/packaging/docker/DOCKER-GUIDE.md`）。

> Docker 形态只建议用来**体验界面和 API**。容器里没有 systemd，
> 所以「服务管理」页会显示 `active: unknown`（这是预期的优雅降级，不是故障）；
> 真正当路由器用请走 deb 安装。

### 从源码构建

```bash
git clone https://github.com/liuzhuohua/drouter.git
cd drouter

# 本体 deb（→ dist/drouter_1.0.5_all.deb，约 445 KB）
bash packaging/build-deb.sh 1.0.5

# 离线依赖库（→ dist/offline-deps/，236 个 .deb + Packages 索引）
bash scripts/make-offline-deps.sh

# 自包含离线包（→ dist/drouter-1.0.5-offline-amd64.tar.gz，约 83 MB）
bash packaging/build-offline-bundle.sh 1.0.5

# Docker 镜像（→ dist/drouter-1.0.5-docker.tar，约 169 MB）
bash packaging/build-docker.sh 1.0.5
```

构建顺序上，`build-offline-bundle.sh` 会**自己**先调 `build-deb.sh`
和 `make-offline-deps.sh`，所以想一步到位只跑它就行。

> **构建环境要求**：Debian 13 (trixie) / amd64。
> 依赖闭包是在**空 dpkg status** 的临时 apt 根里求解的（而不是用当前机器的
> 已装状态），否则「开发机上恰好装过」的包会被 apt 判为"已满足"而漏收 ——
> 到了干净目标机上就变成 `Depends: xxx but it is not installable`。
> 这个坑在 `make-offline-deps.sh` 里有详细注释。

---

## 🚀 快速上手

装好之后的**第一小时**，建议按这个顺序来：

### 第 1 步：打开面板

浏览器访问 `https://<路由器IP>:8443/`

> ⚠️ 用的是**自签证书**，浏览器会报「不安全」。这是正常的 —— 点「高级」→「继续访问」。
> 想要不报警，去**证书 / SSL** 页面签发一张自己的证书并部署。

默认账号：`admin` / `admin123`
**第一件事就是改掉它**（系统设置 → 改密码）。

### 第 2 步：走一遍新手向导

左侧菜单第一项就是**新手向导**。它会带你确认四件事：

1. **外网**通不通（有默认路由 + 能 ping 通国内地址）
2. **内网**在不在发地址（DHCP 开着吗？地址池有没有把自己圈进去？）
3. **DNS** 能不能解析
4. **IPv6** 需不需要开

每一步都会给出**独立的成败结论**，而不是一个笼统的总分 ——
因为新手真正需要知道的是"哪一步还没做"，不是"你得了 60 分"。

### 第 3 步：认一下你的网卡

进**网卡与桥接**，给每张网卡标注：

- **备注**：这是哪个口（电信光猫 / 客厅交换机 / 书房墙插）
- **角色**：WAN / LAN / 未分配

> 角色是按 **MAC 地址**记的。以后网卡改名（`ens18` → `enp1s0`）角色不会丢。

### 第 4 步：配 WAN

进 **WAN 口**，选你的接入方式：

- **PPPoE**：填宽带账号密码（运营商光猫桥接时用这个）
- **DHCP**：光猫已经拨号，路由器拿地址即可
- **静态**：有固定 IP 的专线

点**保存** → 点**应用** → 看**拨号日志**确认连上。

### 第 5 步：（可选）让它真正接管路由

到这一步为止，Drouter 都还只是个"旁观者"。要让它接管，需要：

1. 确认 **WAN 口** 通了、**LAN 口** 配好了
2. 进**内核转发与加速**，开启 **IP 转发** 和 **出向伪装**
3. 关掉原路由器的 DHCP，或把 Drouter 的 LAN 口接到交换机上
4. 把客户端的网关指向 Drouter

> ⚠️ 第 3、4 步会**切断现有网络**。建议留一个能进物理控制台的方式（显示器 + 键盘，
> 或者确保 RealVNC 会话可用），万一配错了还能救回来。

---

## ⚠️ 注意事项

### 🔴 安装前必读

| 事项 | 说明 |
|---|---|
| **不要在唯一出口上直接试** | 第一次装建议装在一台**没在承担路由**的机器上，配好了再切 |
| **RealVNC 用户注意** | 本项目所有动作都避开了 **5900 端口**，也不会动 X 会话 / LightDM。如果你跑着 VNC，它不会受影响 |
| **端口冲突** | 装完**不会**自动启动 `dnsmasq` / `radvd` / `kea`。但如果这些服务**本来就在跑**，请先停掉再让 Drouter 接管 |
| **PVE 虚拟机** | 需要给 VM 网卡开**混杂模式**，否则桥接 / VLAN 的包会被宿主丢掉 |
| **系统时间** | 面板和证书校验都依赖正确时间。裸装 Debian 若时间不对，先 `sudo timedatectl set-ntp true` |
| **离线安装前** | 确认目标机是 **amd64**、剩余磁盘 **≥ 1.5 GB**。离线包里的依赖是按 **Debian 13 (trixie)** 下载的，装到别的版本上可能出现版本不匹配 |

### 📴 关于离线安装（目标机无外网）

这一节单独说，因为它是本项目**默认推荐**的交付方式。

**能保证什么**

- 目标机**无外网、无内网源、DNS 不可达**也能完整装好 —— 全部 236 个依赖
  已随包提供，`install.sh` 走的是本地 `file:` 源，不发起任何网络请求。
- 安装过程**不需要**目标机上有 `apt` 之外的工具（`install.sh` 只用
  POSIX shell + `df`/`cut`，连 `awk` 都不依赖 —— 它不一定在最小系统里）。
- 包内 `SHA256SUMS` 可离线校验完整性：
  ```bash
  sha256sum -c SHA256SUMS
  ```

**必须知道的约束**

| 约束 | 原因 |
|---|---|
| **架构必须匹配** | 包名带 `-amd64`，里面是 x86_64 的二进制。arm64 目标机请按文档自行重建 |
| **发行版必须匹配** | 依赖来自 `trixie` 仓库。跨版本（如 bookworm）会出现 `libc6` 版本冲突 |
| **磁盘 ≥ 1.5 GB** | 236 个 deb 解包 + 安装要占几百 MB；`install.sh` 会在低于 1.5 GB 时告警 |
| **装完要手动改密码** | 默认 `admin` / `admin123`，离线环境下尤其要第一时间改 |

**装完不会发生什么**（这点很重要）

`install.sh` **只安装软件**，不会：

- ❌ 启动 `dnsmasq` / `radvd` / `kea` 等会发 DHCP 的服务
- ❌ 修改默认路由或打开 IP 转发
- ❌ 改动任何现有网络配置

装完之后这台机器**仍然是一台普通主机**，直到你打开面板、亲手点「应用」。
这是整个项目的核心设计原则，离线安装同样遵守。

### 🟡 使用中的注意点

- **保存 ≠ 应用**。改了配置只点「保存」，重启后不会生效。要生效必须点「应用」。
- **默认只写盘不生效**。「应用」时会先渲染预览，确认没问题再下发。
- **快照会占空间**。自动快照默认保留最近若干份，硬盘小时去**磁盘与日志清理**里调阈值。
- **构建保护模式**。如果 `/etc/drouter/BUILD_MODE` 存在，所有改网络的动作会被拒绝。
  这是给"在正在使用的机器上装机"准备的，配好后去**升级与保护**页面解除。
- **Web 端口改动要重启面板**。改完 `8443` 后记得用新端口访问；改错了可以用
  `sudo drouter-ctl webport 8443` 从命令行改回来。
- **API 接口无鉴权时不要暴露**。**通用 API 接口**页面默认只监听本机，
  如果要对内网开放，请自行在防火墙里加来源限制。

### 🟢 兼容性

| 项目 | 支持情况 |
|---|---|
| Debian 13 (trixie) | ✅ 主要目标，全功能验证 |
| Debian 12 (bookworm) | ⚠️ 可用，但 `docker-compose` 包名不同，nftables 版本较老 |
| Ubuntu 24.04 | ⚠️ 大体可用，`docker-compose-v2` 是 Ubuntu 的叫法 |
| Debian 11 及更早 | ❌ 不支持（Python 版本与 nftables 语法不兼容） |
| 架构 | amd64 已充分验证；arm64 理论可用（`Architecture: all`，依赖需自行确认） |

---

## 🗂️ 项目结构

```
drouter/
├── backend/                     Python 3 后端（标准库，无第三方依赖）
│   ├── drouter-web.py           HTTP/HTTPS 服务、路由层、静态文件与 gzip 缓存
│   ├── drouter-helper.py        业务逻辑主体：动作分发、43 个功能实现、渲染调用
│   ├── drouter-helpd.py         常驻执行守护（避免每次请求 fork 一个 helper）
│   ├── render.py                配置渲染器：settings → 原生配置文件
│   ├── theme.py                 主题之日：主题解析、离线预览、导入导出
│   ├── drouter-logd.py          统一日志采集
│   ├── drouter-snapshotd.py     自动快照
│   ├── drouter-rescue.py        紧急救援通道
│   └── drouter-shelld.py        Web 终端的 shell 会话
├── web/                         前端（原生，无构建）
│   ├── index.html               入口页
│   ├── app.js                   单文件 SPA：43 个视图 + 路由 + 状态管理
│   ├── app.css                  样式
│   └── logo.svg                 标识
├── _dev/                        测试与探测
│   ├── t-*.py                   40 套单元测试（预检链里全跑）
│   ├── live-*.py                真机验收脚本（需在目标机上执行）
│   └── *.js                     前端契约测试
├── devtools/                    开发工具
│   ├── sync.sh                  一键部署：预检 → 打包 → 上传 → 部署
│   ├── shots-map.py             截图入库：中文名 → ASCII 名映射与复制
│   ├── check-shots.py           截图覆盖度：功能页 ↔ 截图 ↔ README 三方对账
│   ├── check-md-links.py        文档本地引用死链检查
│   ├── check-views.js           菜单 ↔ 视图映射检查
│   ├── check-render.js          35+ 视图离线渲染检查
│   └── check-mobile.js          手机适配检查
├── scripts/                     运维脚本
│   ├── deploy.sh                部署脚本（被 sync.sh 调用）
│   ├── drouter-ctl.sh           命令行管理工具
│   ├── make-offline-deps.sh     离线依赖打包
│   └── install-base.sh          底座安装
├── packaging/                   打包
│   ├── build-deb.sh             deb 构建
│   ├── build-docker.sh          Docker 镜像构建
│   ├── deb/                     control / postinst / prerm / postrm
│   └── docker/                  Dockerfile / docker-init.sh
└── docs/                        文档
    ├── *.md                     规格 / 部署 / 测试等中文文档
    └── screenshots/             78 张真实界面截图（jpg）
```

### 后端运行时模型

```
浏览器
  │  HTTPS (8443)
  ▼
drouter-web.py           ThreadingHTTPServer，单进程多线程
  │  Unix socket IPC
  ▼
drouter-helpd.py         常驻守护（socketserver.ThreadingUnixStreamServer）
  │  run_action() 无全局锁，可并发
  ▼
drouter-helper.py        75 个 ACTIONS 动作
  │
  ├─ render.py           渲染原生配置
  └─ 系统调用            nft / systemctl / pppd / dnsmasq ...
```

关键设计：

- **常驻守护**：Web 层先试 `helpd` 快路径，失败才回退 fork 一个新 helper。
  否则每个 API 请求都要 fork 一个 1.6 万行的 Python —— 在 2 核机器上不可接受。
- **无全局锁**：`run_action()` 不加锁，多个请求可以并发执行（读操作占绝大多数）。
- **零 fork 读接口**：`read_sysinfo` / `read_metrics` 这类"每次切页面都调"的接口，
  全部直读 `/proc` 与 `os.statvfs()`，不 fork `df` / `hostname` / `uname`。
- **批量取服务状态**：`read_services` 用一次 `systemctl show` 取回 18 个单元的状态，
  而不是 18 × 2 = 36 次 `systemctl is-active/is-enabled`。

---

## 🔐 设计与安全取舍

这一节解释几个**看起来奇怪但故意这么做**的决定。

### 为什么默认不启动 DHCP / 不接管路由

因为最容易出事的场景是：在一台**正在提供服务**的机器上装路由系统，
装完发现 DHCP 服务端起来了，整个局域网开始抢地址。

所以所有"抢端口"的行为都需要用户**显式**触发。这不是偷懒，是设计。

### 为什么把 `smartmontools` 放在 Suggests 而不是 Recommends

因为 apt 默认会装 `Recommends`，而 `smartmontools` 装完会自动拉起 `smartd` 常驻。
对一台路由器来说这是个意料之外的常驻服务。

同理，`samba` / `nfs-kernel-server` / `docker.io` 也都降级到 `Suggests` ——
它们装完会启用文件共享服务和 Docker 守护，在 4GB 机器上很重。
用户需要时在**依赖自检与安装**页面上逐项装。

### 为什么 `.deb` 里的 `DEBIAN/control` 没有注释

因为 `control` 是 **deb822 格式，不保证支持 `#` 注释行**。
某些 dpkg 工具遇到注释会解析失败。所以所有说明都写在 `build-deb.sh` 里。

### `ifb-drouter` 是什么

它是 **QoS 智能限速**创建的 IFB 虚拟网卡。

Linux 的流量整形（tc）**只能管出向流量**。要限速入向流量，得先把入向包
重定向到一块 IFB 设备上，再在那上面做整形。

所以看到 `ifb-drouter` 时不用担心 —— 它不是紧急通道，也不用给它指派角色。

### 为什么前端不用框架

单文件 SPA + 原生 JS，11200 行，没有构建步骤。

因为目标是"改完刷新就能看到"，而不是"改完等 30 秒 webpack"。
对于这个规模的面板，框架带来的收益（组件复用、状态管理）
抵不过构建链带来的复杂度。

代价是：定时器清理、事件解绑、DOM 判空这些事**得自己记住**。
项目里用 40 套单测 + 3 个前端检查脚本（菜单映射 / 离线渲染 / 手机适配）守住这些点。

---

## ❓ 常见问题

<details>
<summary><b>装完打不开 8443 端口？</b></summary>

```bash
sudo drouter-ctl status          # 看服务状态
sudo journalctl -u drouter-web -n 50   # 看报错
sudo ss -tlnp | grep 8443        # 看端口有没有在听
```

常见原因：
1. **服务没起来** —— 看 `journalctl` 报什么
2. **防火墙挡了** —— 检查 `nft list ruleset` 有没有放行 8443
3. **浏览器拒绝自签证书** —— 换 Firefox 或手动点「继续访问」

</details>

<details>
<summary><b>改错了配置，网络不通了怎么办？</b></summary>

按严重程度依次尝试：

1. **回滚快照** —— 有本地控制台的话，`sudo drouter-ctl rollback`
2. **救援通道** —— `sudo systemctl start drouter-rescue`，它会起一个最小可用的网络配置
3. **物理控制台** —— 接显示器和键盘，直接改 `/etc/network/interfaces` 或 `nmcli`

> 这也是为什么**装之前一定要留一个备用进系统的方式**。

</details>

<details>
<summary><b>为什么我的 `<某个包>` 装不上？</b></summary>

大概率是**包名在你那个发行版上不一样**。这个项目主要在 Debian 13 上测。

最典型的例子：

| 功能 | Debian 13 | Ubuntu |
|---|---|---|
| Compose v2 | `docker-compose` | `docker-compose-v2` |

去**依赖自检与安装**页面看它探测的是哪个包名，然后对照你系统的实际包名。

> 💡 一个已实测的 apt 行为：`apt-get install a b c` 里只要有**一个**包名查不到，
> 整体就会中止，同命令里的其它包**一个都不装**。所以批量安装失败时，
> 先单独 `apt-cache policy <包名>` 确认每个包都存在。

</details>

<details>
<summary><b>能当旁路由用吗？</b></summary>

能。这正是它的设计场景之一 —— 先作为旁路由跑起来（不接管主路由的 DHCP），
把 QoS、DPI、DDNS、Docker 这些"附加能力"先接上，
等你觉得稳了，再决定要不要把它提到主路由位置。

</details>

<details>
<summary><b>怎么升级？</b></summary>

```bash
# 下载新版 deb
sudo apt install ./drouter_1.0.5_all.deb    # 直接覆盖安装，配置保留
```

配置在 `/etc/drouter/` 和 SQLite 里，`apt remove` 也会保留（`purge` 才删）。

</details>

<details>
<summary><b>怎么完全卸载？</b></summary>

```bash
sudo apt remove drouter       # 删程序，保留配置
sudo apt purge  drouter       # 连配置一起删
```

注意：**已经应用过的原生配置**（`/etc/nftables.conf`、`/etc/dnsmasq.conf` 等）
不会自动还原 —— 它们是系统配置，不是软件包的一部分。
要还原请用快照回滚，或手动改。

</details>

---

## 📝 更新日志

### v1.0.5 — 深挖第二轮：堵提权/注入，给内存和并发装上闸

纯修复与加固版本，无新增功能，**不改动配置存储格式**（老配置直接可用）。
1.0.4 清完「假输入框」之后，这一轮把前后端再过一遍，主题是三类：
**能被利用的安全缺陷、会慢慢吃掉资源的生命周期问题、以及低配置机器上的性能水位**。

- **安全：改密码可以顺手拿到 root。** `chpasswd` 是按行读的，用户名或密码里
  塞一个换行，第二行就会被当成另一条记录执行 —— 等于直接改 root 密码。
  现在用户名/密码一律拒绝换行、回车与控制字符。同类还修了：删除用户从
  「写死保护 root/ajeef」改成「保护全部 UID<1000 系统账号」；
  WOL 唤醒的接口名/目标地址加白名单（原先拼进 `sh -c`）；
  ACL 的 hosts 字段改用 `ipaddress` 严格校验（原先 `':' in h` 这种弱判断
  能漏进 nftables 规则）；存储挂载点限定在 `/mnt|/media|/srv` 之下
  （原先 `target=/etc` 会把 U 盘盖到 /etc 上，系统立刻不可用）。
- **安全：文件管理器的目录穿越收口。** 拒绝清单从「文件级」扩到「目录级」
  （`.ssh`、`/etc/sudoers.d`、`/etc/pam.d` 等），再加一道按文件名的拦截
  （`authorized_keys`、`shadow`、`sudoers` …）—— 原先 `..` 绕一层就能
  写到 root 的 SSH 密钥，等于开后门。
- **安全：前端 201 处未转义的 `value="${...}"` 全部补 `esc()`**，
  `href` 一律过 `httpUrl()` 只放行 http/https（防 `javascript:` 伪协议），
  主题导出的注释同时剥掉 `/*` 与 `*/`（1.0.4 只剥了一半，照样能闭合注入）。
- **丢数据：三个页面切走再回来，未保存的改动被静默冲掉。**
  访问控制 / 文件共享 / DDNS 进入页面时都会无条件用服务端数据覆盖草稿。
  现在页内任何改动都会登记「脏页」，再进页面时保留你的编辑；
  保存成功才清除。这是全站唯一真正不可逆的丢数据路径。
- **假功能：LAN 页的「网关地址 / 域名后缀」改了等于没改。**
  这两个值实际由 dnsmasq 下发，页面却写进了谁也不会读的 `system` 模块死键。
  现在与 DHCP 页 Option 3 同源。另外 UPnP 的「刷新映射表」按钮原先会
  **顺手把服务启动**然后给你看一句静态提示 —— 现在走真正的只读接口
  `read:upnpmap`，查询绝不再产生副作用。
- **资源：PTY 终端三条命。** 缓冲区不再无上限（8MB 封顶，超出丢最旧）、
  会话数用信号量真正卡住（原先是「先查再用」的竞态）、空闲会话有后台
  线程定期回收（原先只在「满了」那一刻才顺手清）。离开终端页面时
  前端也会主动断开会话，不再白占 30 分钟。
- **资源：Web 服务并发与内存水位。** 工作线程 48 封顶（原先 `daemon_threads`
  只是「退出不等待」，并不是限流）、请求体 16MB 封顶、会话表 500 封顶且
  过期判断改用单调时钟（原先系统时间一回调，会话集体「永生」或集体过期）、
  审计表 2 万行封顶自动裁剪、登录失败计数表封顶。
- **资源：日志与快照不再能撑爆磁盘。** helper 自身日志 32MB 封顶自动轮转；
  快照前检查剩余空间（<1GB 直接跳过并说明）；文件下载封顶从 512MB
  收到 20MB —— 512MB 这个数字本来就超过了下载通道 32MB 的单条响应上限，
  大文件只会静默失败，回退路径还会把 ~700MB 输出整个读进内存。
- **性能：`/api/config` 从 16 次开关 SQLite 改成 1 次**；磁盘清理状态页的
  递归扫描有 30 秒缓存（原先每进一次页面就全量 `os.walk` 一遍 /var/log）；
  gzip 缓存按字节数设闸（24MB）；标签页在后台时心跳暂停、终端轮询降频。
- **加固：全部 11 处「断电就剩半截」的配置写入改原子写**（临时文件 +
  `os.replace`）；rescue 与 snapshot 两个 systemd 单元补上 `MemoryMax`
  内存闸（连同 helper 重建单元的那处模板，三处同步）。
- **修：DHCPv6「停用」不停用。** `enabled=false` 时 dhcpcd 配置里仍然输出
  实际配置行，现在停用就只留注释。另修租约回收改用 `dhcp_release`
  （已登记进依赖清单，未安装时自动退化为删租约文件 + 重载并如实告知）、
  VLAN 只删自己创建的接口（原先按名字匹配，可能删掉用户手建的）、
  探测公网前清掉过期结果文件（原先一次探测成功就永久显示「外网可达」）。

### v1.0.4 — 清「点了没反应」与「东西写错地方」这批缺陷

纯修复版本，无新增功能，**不改动配置存储格式**（老配置直接可用）。
这一轮是把全量代码（约 4.5 万行）从头过了一遍，重点找两类最伤人的问题：

- **「假输入框」** —— 页面上有这个框、能填、能保存，后端却从来不读，等于白填；
- **「隐身字段」** —— 后端真的会用，页面上却没有入口，永远走默认值。

排查方式是拿 AST 把渲染器里所有读取的配置键抽出来，再和页面上的输入框清单做差集。

- **修：点了「迁移到 systemd-networkd」，写出来的文件名叫 `name`、内容写着 `content`。**
  渲染层约定是所有渲染器都返回「（绝对路径, 内容）」二元组列表，唯独 networkd 这个
  返回的是字典。调用方按二元组去解包，字典的两个键刚好被当成路径和内容 ——
  迁移不是「失败」，而是**真的生成了一堆垃圾文件**。这是本轮唯一会直接写坏系统的缺陷。
- **修：DMZ 规则方向写反。** 注释写的是「排除到本机自身的流量」，条件却写成了它的
  反面：本来只有外部流量该被转进内网主机，实际连管理流量也一并转走。
- **修：一批「填了等于没填」的输入框（5 处）。** ① WAN 页的 MTU 与「是否接受
  上游 DNS」后端从不读，现在真正写进 `systemd-networkd` 的 `MTUBytes=` / `UseDNS=`；
  ② DHCPv6 页的「子网 ID」绑的是个不存在的键，真正生效的前缀委派 ID 无入口，
  现在绑对了；③ Samba 的「WINS 支持」要求另一个从不回填的字段同时为真，勾了也
  永不生效；④ 网桥的「启用 STP」没有绑定事件，勾完保存不进去；⑤ 主题设计器里
  「按主色生成整套配色」按钮压根没接线，点了没反应。
- **修：IPv6 路由通告页缺少「下发 DNS / 搜索域」的生命期输入框。** 1.0.3 修了字段
  错位，但页面上一直没有入口 —— 现在补齐。
- **修：迁移预览永远报「WAN 网卡不能为空」。** 前端只提交了 LAN 段，WAN 和网桥配置
  一次都没发过，预览里既看不到 WAN 也看不到网桥。
- **修：一批校验缺失导致的「保存完服务起不来 / 页面 500」。** 过去这些字段是直接从
  表单拼进配置文件的：dnsmasq 的域名与日志文件路径、miniupnpd 的租约文件路径与
  UUID、PPPoE 的运营商名与服务名、IPv6 前缀委派 ID、路由通告的地址池五项、
  NFS 的导出选项与线程数 —— 现在全部按各自语法校验并给中文提示。另外
  chrony 的服务器地址校验只认域名和 IPv4，**填 IPv6 的 NTP 服务器会被误判成非法**。
  顺带修了 `int()` 前只判断 `isdigit()` 的老毛病：Unicode 的上标数字能通过 `isdigit()`
  却会让 `int()` 抛异常，界面直接 500。
- **修：自签证书的 SAN 里写死了开发者的内网 IP。** 生成的证书主题别名里必定带上
  `192.168.7.3`，跟你的机器一点关系都没有；有些客户端会因为它校验不过而报警。
  现在改为从本机实际地址和默认路由推导，并保底保留 `127.0.0.1` / `localhost`。
  同时补了 openssl 失败检测 —— 过去生成失败时不会报错，只是给你一个不存在的证书路径。
- **修：两处模板注入。** ① DDNS 面板的「记录名 / 服务商」是拼进 HTML 的，
  填一段带尖括号的内容就能在页面上执行自己的脚本；② 导出的主题 CSS 把主题名和版本
  拼进注释里，名字里带 `*/` 就能提前闭合注释，往里塞任意 CSS。现在前端转义、
  后端清注释结束符，两边都堵住。

### v1.0.3 — 修「部署之后设置莫名回退」与几处静默失败

纯修复版本，无新增功能，**不改动配置存储格式**（老配置直接可用）。

- **修：IPv6 路由通告里填了 IPv4 DNS，就会生成一份 radvd 起不来的配置。**
  `RDNSS` 字段原先完全不校验（同一段代码里 `prefix` 和 `DNSSL` 都校验了，唯独漏了它）。
  在「下发 DNS」框里顺手填 `223.5.5.5`（最顺手的写法）会原样写进 `radvd.conf`，
  radvd 直接 `syntax error` 起不来；界面上只回一句英文，完全指不到是哪个字段。
  现在逐项按 IPv6 地址校验并给出中文提示，且多值支持逗号/空格两种写法
  （统一按空格输出 —— 逗号形式 radvd 本来就不认）。顺带修了「DNSSL 生命期」
  实际读的是 RDNSS 生命期这个张冠李戴。
- **修：自动清理的「每天 00:00」存不下来，实际跑在 04:00。**
  前端 `Number(v) || 4` 与后端 `int(cfg.get('hour') or 4)` 都把 0 当成「没填」。
  另外执行时刻的判定从「正好等于」改成「不早于」：机器在计划时刻恰好在关机/重启时，
  当天会补做，而不是整天跳过、要等到第二天。
- **修：重新部署会静默回退用户设置（3 处）。** 这是最烦人的一类问题 ——
  用户只会觉得「部署完我的设置莫名其妙变回去了」。
  ① 生效主题被无条件写回 `default`，自定义主题每次部署都被打回；
  ② `isp-dns.conf` 被无条件覆盖，pppd 已经拿到的运营商 DNS 被擦掉，上游解析变空
  直到下次拨号；③ 自动清理被无条件 `enabled=true`，用户在界面上**明确关掉**的
  「自动删文件」策略会被重新打开（清理是本项目唯一真删文件的模块，这条最不该发生）。
  现在三者都改成「只在不存在时初始化」，并复用容器版 `docker-init.sh` 已有的写法。
- **修：浏览器切页/关标签会在 journal 里刷一整段 Python traceback。**
  `BrokenPipeError` / `ConnectionResetError` 属于「对面先挂了」，不是服务端的错，
  却足以把 journal 刷满、淹掉真正的报错（实测 3 天攒了几十条）。现在只吞这几类。
- **修：`/etc`、`/opt`、`/var/log` 里残留的名字带 `\r` 的脏条目。**
  早先 CRLF 的部署脚本在 Linux 上执行时，行尾的 `\r` 粘进了参数，真的建出了
  `dnsmasq.d\r`、`nftables.d\r`、`active-theme\r`、`/opt/drouter\r` 这类条目 ——
  `ls` 会出现「同名条目两遍」的重影，配置快照也会反复把它们打包进去。
  新增 `devtools/fix-crlf-names.py`（默认只预演，加 `--apply` 才删；只删**存在同名
  正常兄弟**的条目，目录里若有兄弟没有的文件就拒绝删），并把「文件名不得含控制字符」
  加进发布前预检。

### v1.0.2 — DHCP 渲染去重 + 预检诊断

纯修复版本，无新增功能，**不改动配置存储格式**（老配置直接可用）。

- **修：DHCP 页会把 `option 3` / `option 6` 渲染成重复指令。** 页面上有专属字段
  （`Option code 3 · 网关` / `Option code 6 · DNS`）和「自定义 Options」表**两处**
  能写同一个 code，两处都填就会各输出一遍 —— 而 dnsmasq 对 3/6 这类列表型 option
  是**追加**语义，于是客户端收到重复的网关、以及顺序被搅乱的 DNS 列表
  （公网 DNS 排在前面，本机 dnsmasq 等于形同虚设）。
  现在按 code 合并成一行并顺序去重；标了「强制」的 3/6 行语义不同，仍单独输出。
  新建配置也不再预置与专属字段重复的 code 3/6 行。
- **改：`code 6` 的填写提示。** 原占位符是 `223.5.5.5,119.29.29.29`，容易让人把
  **上游** DNS 填进下发给客户端的 option 6。现改为提示填**本机**地址
  （客户端查本机、本机再转发到上游）。
- **修：dnsmasq 预检失败时给出可读诊断。** 原先把子进程的英文原文直接抛给用户，
  看不出问题出在哪。例如 `failed to seed the random number generator`
  其实与配置无关 —— 是宿主机的 `/dev/urandom` 设备节点不存在（`/dev` 曾被清空），
  现在会明确指向系统层面的原因与补救方向。

随包附 `devtools/fix-dev-nodes.sh`：幂等地补齐缺失的 `/dev` 设备节点，
设备号取自内核 `devices.txt`、`/proc/partitions` 与 sysfs，不硬编码。

### v1.0.1 — Web 终端可用性修复

纯修复版本，无新增功能，不改动任何配置格式。

- **修：Web SSH 终端任何账号都连不上（`out of pty devices`）。**
  真因不是伪终端耗尽（`kernel.pty.nr` 为 0），而是 `/dev/ptmx` 这个符号链接丢失。
  现在守护进程启动时与每次创建失败时都会自愈（补链接 → `mknod` 兜底），
  并给出可照抄的修复提示，不再只抛一句误导性的系统报错。
- **修：终端并发满员后整个服务被冻死。** 会话锁不可重入，而「满员 → 回收空闲会话」
  会在持锁状态下二次取同一把锁，同线程永久阻塞且不报错、不写日志。
  改为可重入锁 + 回收动作移出锁外。实测第 9 个连接 0.01 秒返回 `BUSY`，
  8 个会话 0.87 秒全部清理、残留 0，无 fd 泄漏。
- **改：终端外观。** 隐藏右侧滚动条（保留滚动能力）、高度随视口自适应并跟随最新输出、
  新增「清屏」与「↓ 最新」按钮。
- **修：页脚协议标注错误。** 原先写「GNU GPL v3」，与仓库实际的 MIT `LICENSE` 不符。

### v1.0.0 — 首个正式版

43 个功能页面、9 个纯标准库后端模块、6 个 systemd 单元、自包含离线安装包
（本体 + 236 个依赖）、Docker 镜像与 compose 编排。

---

## 🤝 参与贡献

欢迎 Issue 和 PR。

### 开发环境

```bash
git clone https://github.com/liuzhuohua/drouter.git
cd drouter

# 本地跑测试（不需要目标机）
python3 _dev/t-*.py              # 40 套单元测试
node devtools/check-views.js     # 菜单映射检查
node devtools/check-render.js    # 视图渲染检查
node devtools/check-mobile.js    # 手机适配检查
```

### 提交前请确认

1. **全部单测通过**（40 套）
2. **前端三查通过**（check-views / check-render / check-mobile）
3. **文件是 LF 换行**（用 Windows 编辑容易写成 CRLF，Linux 上会执行失败）
4. 新增功能请**同时加测试** —— 这个项目的复杂度已经到了"不能靠肉眼保证"的程度

### 部署到测试机

```bash
bash devtools/sync.sh
```

这条命令会依次做：跑全部预检 → 打包 → 上传 → 部署。预检不过就不会上传。

### 代码风格

- 后端：Python 3 标准库，不用第三方包
- 前端：原生 JS，`function viewXxx()` 必须是**函数声明**（因为 `const VIEWS` 在模块顶层求值，靠的是变量提升）
- 注释：解释**为什么**这么做，而不是**做了什么**。特别是那些"看起来可以简化但其实是踩过坑"的地方

---

<div align="center">

**Drouter** · 让 Debian 成为一台你可以完全掌控的路由器

MIT License · 作者 [火麒麟 (ajeef)](https://github.com/liuzhuohua)

</div>
