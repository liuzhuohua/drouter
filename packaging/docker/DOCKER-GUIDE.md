# Drouter · Docker 使用说明

> 适用镜像：`drouter:1.0.3`（`drouter-1.0.3-docker.tar`，约 169 MB）

---

## ⚠️ 先说清楚：Docker 形态的定位

**这是「体验 / 调试 / CI 渲染校验」用的，不建议把容器当主路由长期跑。**

原因很实在，不是谦虚：

| 限制 | 具体表现 | 影响 |
|---|---|---|
| 容器里没有 systemd | 「服务管理」页显示 `active: unknown` | 这是**预期内的优雅降级**，不是故障 |
| 无法持久化 systemd 单元 | 容器重启后 dnsmasq/radvd 等不被托管 | 托管能力缺失 |
| 需要 `NET_ADMIN` 才完全可用 | 不加时报 `Operation not permitted` | 防火墙页失效 |
| 网络命名空间限制 | 即使 host 网络，也没法操作宿主的内核模块、sysctl 全集 | 部分高级功能受限 |

不过 **Web 终端不受 systemd 缺失影响** —— 1.0.1 起终端守护会绕开 init
系统直接启动，容器里一样能开出 root shell（见第六节第 4 条自测）。

**真正当路由器跑** → 用 `drouter-1.0.3-offline-amd64.tar.gz` 装到 Debian 13 物理机/虚拟机上。
那才是这个项目的主路径，也是它被设计出来的场景。

**想快速看看界面长什么样、试试 API** → 用 Docker，一分钟就能跑起来。

---

## 一、加载镜像

```bash
docker load -i drouter-1.0.3-docker.tar
```

加载完会看到两个标签（同一个镜像）：

```
drouter:1.0.3
liuzhuohua/drouter:1.0.3
```

验证：

```bash
docker images | grep drouter
```

> **为什么有两个标签？**
> 带用户名的那个是给将来推 Docker Hub 用的。
> 日常用短的 `drouter:1.0.3` 就行。

> **如果你用的是 Podman**：本镜像最初用 buildah 构建，内部标签曾为
> `localhost/drouter:1.0.3`。发布版已统一改成 `drouter:1.0.3`
> （另有别名 `liuzhuohua/drouter:1.0.3`），`podman load` 和 `docker load`
> 都能直接加载。若你手上是更早期的 tar 包，引用时才需要 `localhost/` 前缀。

---

## 二、方式 A：docker compose（推荐）

我已经写好编排文件，直接用：

```bash
# 1) 把 compose 文件放到任意目录
mkdir -p ~/drouter && cd ~/drouter
#    把 docker-compose.yml 放进来

# 2) 起
docker compose up -d

# 3) 看状态（等 health 变成 healthy）
docker compose ps
```

访问 **`https://<宿主IP>:8443/`**

- 账号 `admin` / `admin123`
- 自签证书，浏览器会报「不安全」→ 点「高级」→「继续访问」
- **第一件事：改密码**

停止 / 卸载：

```bash
docker compose down            # 停容器，配置（命名卷）保留
docker compose down -v         # 停容器并删除配置卷（彻底清空）
docker compose logs -f         # 看日志
```

---

## 三、方式 B：docker run（不想用 compose）

```bash
docker run -d \
  --name drouter \
  --restart unless-stopped \
  --network host \
  --cap-add NET_ADMIN \
  --cap-add NET_RAW \
  -e TZ=Asia/Shanghai \
  -v drouter-etc:/etc/drouter \
  -v drouter-data:/opt/drouter/data \
  -v drouter-snapshots:/opt/drouter/snapshots \
  -v drouter-log:/var/log/drouter \
  drouter:1.0.3
```

就这几行。参数含义与 compose 里**完全一致**，逐个对应。

> **别用 `--privileged`**
> 网上很多教程图省事都写 `--privileged`。实际上 Drouter 只需要
> `NET_ADMIN` + `NET_RAW` 两个能力，给 `privileged` 等于把宿主机完全敞开。
> 除非你在排查权限问题，否则不需要。

---

## 四、参数为什么这么配

### `--network host` 是硬要求

面板要干的事：读宿主网卡状态、写 nftables 规则、看 conntrack 连接跟踪。
这些全都在**宿主机的网络命名空间**里。用 bridge 模式的话容器看到的是一个
虚拟网络栈 —— 里面只有 `eth0`，看不到你的 `ens18`/`ens19`，
也管不了宿主的 NAT 规则。所以必须 `network_mode: host`。

副作用：`ports:` 映射会失效（host 模式下 Docker 直接忽略它）。
端口由容器内 `/etc/drouter/web-port` 决定，默认 8443。

### `NET_ADMIN` 不能省

不加的话，`nft list ruleset` 会返回 `Operation not permitted`：
「防火墙」页会显示成空的，看起来像「没有规则」，而不是像「没有权限」——
**这个误导性很强**，所以我在镜像里也加了 HEALTHCHECK 之外的显式提示。
实测加上 `NET_ADMIN` 后 `/api/nft` 正常返回真实规则集。

### `NET_RAW`

给 ping / traceroute / arping 这类需要原始套接字的探测功能用。
「网络诊断」工具页会用到。

### 四个命名卷必须挂

`docker compose down` 默认**保留**命名卷，但如果你用了 `down -v`
或者直接 `docker rm -f` 再重建，没挂出来的目录就全没了。所以四个都挂上：

| 卷 | 挂载点 | 装什么 |
|---|---|---|
| `drouter-etc` | `/etc/drouter` | 配置核心：settings、web-port、active-theme、证书索引 |
| `drouter-data` | `/opt/drouter/data` | SQLite 库、流量统计 |
| `drouter-snapshots` | `/opt/drouter/snapshots` | 配置快照（回滚用） |
| `drouter-log` | `/var/log/drouter` | 日志 |

### `DROUTER_WEB_PORT` 只在首次启动生效

这是个**故意的**设计。容器入口脚本只在 `/etc/drouter/web-port` 不存在时
才把环境变量写进去，之后会 `unset`。

原因：后端读端口时**环境变量优先级高于配置文件**。如果一直留着这个环境变量，
你在界面上把端口从 8443 改成别的，看起来「改了却没生效」——
实际的坑是环境变量把它覆盖回去了。

**所以：改端口请去界面改，不要改 compose 里的环境变量。**

---

## 五、常用运维命令

```bash
# 看日志（实时）
docker logs -f drouter

# 健康状态
docker inspect --format '{{.State.Health.Status}}' drouter

# 进容器（注意：dash 里很多命令查不到，要 bash -lc）
docker exec -it drouter bash -lc 'nft list ruleset'
docker exec -it drouter bash -lc 'cat /etc/drouter/web-port'
docker exec -it drouter bash -lc 'ls -la /opt/drouter/backend/'

# 手动触发一次健康检查
docker exec drouter bash -lc 'PORT=$(cat /etc/drouter/web-port||echo 8443); curl -fsSk https://127.0.0.1:$PORT/api/health'

# 备份配置（把卷打包出来）
docker run --rm -v drouter-etc:/src -v "$PWD":/backup alpine \
  tar czf /backup/drouter-etc-$(date +%F).tar.gz -C /src .

# 备份日志
docker logs drouter > drouter-$(date +%F).log 2>&1
```

> **`docker exec` 默认用 `/bin/sh`（dash），不是 bash。**
> 直接 `docker exec drouter command -v nft` 会说找不到东西 —— 
> 那是因为 dash 的 `command -v` 行为不同。加 `bash -lc` 就正常了。

---

## 六、验证容器里的功能是否真的可用

我在构建时做过一轮完整验收，你可以自己复现。

### 1) 健康检查

```bash
docker inspect --format '{{.State.Health.Status}}' drouter
# 期望：healthy
```

### 2) 登录拿 token

```bash
TOKEN=$(curl -sk -X POST https://127.0.0.1:8443/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
echo "$TOKEN"
```

> **鉴权走 `X-Token` 请求头，不是 `?token=` 查询参数。**
> 这是最容易搞错的地方。

### 3) 打几个真实接口

```bash
for ep in sysinfo ifaces metrics ipv6 leases users; do
  printf '%-10s ' "$ep"
  curl -sk -o /dev/null -w '%{http_code}\n' \
    -H "X-Token: $TOKEN" https://127.0.0.1:8443/api/$ep
done
# 期望：全部 200
```

### 4) Web 终端（容器形态也能用）

容器里没有 systemd，但**终端不受影响** —— 1.0.1 起守护会绕开 init 系统
直接以进程方式启动。想确认它真的能开：

```bash
curl -sk -X POST -H "X-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"user":"root","cols":100,"rows":30}' \
  https://127.0.0.1:8443/api/webshell/connect
# 期望：{"ok": true, "msg_cn": "终端已连接", "data": {"sid": "...", ...}}

# 守护进程与 socket
docker exec drouter pgrep -af shelld
docker exec drouter ls -l /run/drouter/shell.sock
```

### 5) 对比「预期差异」

这两个**不是 bug**，是容器形态的正常表现：

```bash
# 服务状态 → active: unknown（容器无 systemd，优雅降级）
curl -sk -H "X-Token: $TOKEN" https://127.0.0.1:8443/api/services

# nft 规则 → 不加 NET_ADMIN 会 Operation not permitted
curl -sk -H "X-Token: $TOKEN" https://127.0.0.1:8443/api/nft
```

---

## 七、常见问题

<details>
<summary><b>浏览器打不开 8443</b></summary>

按顺序查：

```bash
# 1) 容器活着吗
docker ps | grep drouter

# 2) 健康吗
docker inspect --format '{{.State.Health.Status}}' drouter

# 3) 端口在听吗（在宿主上跑）
ss -tlnp | grep 8443

# 4) 日志里有报错吗
docker logs --tail 50 drouter
```

如果容器在跑、端口也在听但访问不了 → 大概率是**宿主防火墙**挡了。
</details>

<details>
<summary><b>防火墙页是空的 / 报 Operation not permitted</b></summary>

镜像里 99% 是这个原因：启动时漏了 `--cap-add NET_ADMIN`。

```bash
# 确认当前容器有哪些能力
docker inspect --format '{{.HostConfig.CapAdd}}' drouter
# 期望：[NET_ADMIN NET_RAW]
```

修正：删掉容器重建（配置在命名卷里，不会丢）：

```bash
docker rm -f drouter
# 然后用带 cap_add 的 compose 或 docker run 重新起
```
</details>

<details>
<summary><b>Web 终端报「终端守护未运行」</b></summary>

容器里没有 systemd，所以**不能**用 `systemctl start drouter-shelld`（敲不了）。

1.0.1 起面板会自动回退成直接启动守护进程，正常情况不会走到这里。
若仍报错，按顺序查：

```bash
# 1) 守护进程在不在
docker exec drouter pgrep -af shelld

# 2) socket 建出来了没（权限应为 srw------- root root）
docker exec drouter ls -l /run/drouter/shell.sock

# 3) 伪终端入口在不在 —— 少了它内核会误报 "out of pty devices"
docker exec drouter ls -l /dev/ptmx      # 应指向 pts/ptmx

# 4) 守护自己的日志
docker logs --tail 50 drouter | grep -i shelld
```

守护进程起不来时，通常是 `/opt/drouter/backend/drouter-shelld.py` 被卷挂载
覆盖掉了（比如误把宿主目录挂到 `/opt/drouter`）。检查 `docker inspect` 的
`Mounts` 里有没有多余的 `/opt/drouter` 挂载。
</details>

<details>
<summary><b>健康检查一直 unhealthy</b></summary>

两个可能：

1. **面板还没起来** → `start_period: 20s` 内是正常的，等一会
2. **curl 没加 `-k`** → 面板是 HTTPS + 自签证书，不加 `-k` 证书校验必失败

手动验一下：

```bash
docker exec drouter bash -lc \
  'curl -fsSk --max-time 4 https://127.0.0.1:8443/api/health'
```
</details>

<details>
<summary><b>改了端口没生效</b></summary>

检查是不是 compose 里还留着 `DROUTER_WEB_PORT` 环境变量 ——
它的优先级高于配置文件。改端口请走界面，不要在 compose 里改这个变量。
</details>

<details>
<summary><b>容器重启后配置没了</b></summary>

确认四个卷都挂了：

```bash
docker inspect --format '{{json .Mounts}}' drouter | python3 -m json.tool
```

应该看到 `drouter-etc` → `/etc/drouter` 等四条。
</details>

<details>
<summary><b>能直接用 --privileged 吗</b></summary>

能，但没必要。Drouter 只需 `NET_ADMIN` + `NET_RAW`。
`privileged: true` 会让容器能访问宿主所有设备 —— 权限面大得多。
除非在排查权限问题，否则建议保持最小权限。
</details>

---

## 八、从镜像回退到 deb 安装

如果你用 Docker 体验完，决定正式部署：

1. 备份配置（见上面「常用运维命令」的备份命令）
2. 在目标 Debian 13 机器上装离线包
3. 把备份的 `/etc/drouter` 内容恢复过去（路径一致，可直接用）

```bash
# 目标机上
tar xzf drouter-1.0.3-offline-amd64.tar.gz
cd drouter-1.0.3-offline-amd64
sudo bash install.sh

# 恢复你在容器里试出来的配置
sudo tar xzf drouter-etc-2026-09-30.tar.gz -C /etc/drouter --strip-components=0
sudo systemctl restart drouter-web drouter-helpd drouter-shelld
```

---

## 附：镜像技术信息

| 项 | 值 |
|---|---|
| 基础镜像 | `debian:trixie-slim` |
| 架构 | amd64 / linux |
| 标签 | `drouter:1.0.3`、`liuzhuohua/drouter:1.0.3` |
| 入口 | `/opt/drouter/bin/docker-init.sh` |
| 默认命令 | `/usr/bin/python3 /opt/drouter/backend/drouter-web.py` |
| 暴露端口 | 8443（HTTPS 面板）、8080（备用 HTTP，默认关） |
| 数据卷 | `/etc/drouter`、`/opt/drouter/data`、`/opt/drouter/snapshots`、`/var/log/drouter` |
| 时区 | `Asia/Shanghai` |
| 许可 | MIT |

`docker-init.sh` 做的事（因为容器里没有 systemd，这些原本由 systemd 单元负责）：

1. 建齐运行所需目录、设好属主（数据归低权用户 `drouter`，代码归 root）
2. 首次启动播种默认主题 `active-theme` = `default`
3. 播种运营商 DNS 占位文件（避免渲染时找不到文件报错）
4. 首次启动把 `DROUTER_WEB_PORT` 写进 `/etc/drouter/web-port`，然后 `unset`
5. `exec` 真正的主进程（保证信号能正确传递，`docker stop` 能优雅退出）
