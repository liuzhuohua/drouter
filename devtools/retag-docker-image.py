#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重打 Docker 镜像标签 + 修正 license 标签 + 补上 HEALTHCHECK/端口标注。

为什么需要这个脚本
------------------
镜像原本是用 **buildah** 以 root 构建的，标签被钉成 `localhost/drouter:1.0.0`。
`localhost/` 是 Podman 的"本地镜像"约定，Docker 虽然能 load 进来，但这个前缀
既不好看也容易让人误解（像是某个私服地址）。所以这里把标签改成：
    主标签  drouter:1.0.0          ← docker load 后直接用这个跑
    别名    liuzhuohua/drouter:1.0.0  ← 同时打一个带用户名的，方便以后推 Docker Hub

顺带修两个真实缺陷：
1. 镜像 LABEL 里 `org.opencontainers.image.licenses` 写的是 **GPL-3.0**，
   但项目实际是 **MIT**。这是构建时抄错模板留下的。
2. 缺 `HEALTHCHECK`：compose 里写 `depends_on: condition: service_healthy`
   就没东西可等；`docker ps` 也看不出容器是不是真的活着。

实现方式
--------
不解压、不重新打包整包（176MB，重打包有损坏风险），
而是**流式重写 tar**：只替换 manifest.json / repositories / config json
三个小文件，layer.tar 原样透传。这样：
  · 大文件零拷贝，速度快
  · layer 的 sha256 不变（docker load 会校验 layer 摘要）
"""
import json
import os
import sys
import tarfile

SRC = sys.argv[1] if len(sys.argv) > 1 else "dist/drouter-1.0.0-docker.tar"
DST = sys.argv[2] if len(sys.argv) > 2 else "dist/drouter-1.0.0-docker.tar.new"

NEW_TAGS = ["drouter:1.0.0", "liuzhuohua/drouter:1.0.0"]

with tarfile.open(SRC, "r:") as tin:
    members = tin.getmembers()
    by_name = {m.name: m for m in members}

    # ── 读 manifest.json ────────────────────────────────────────────
    mf = json.load(tin.extractfile("manifest.json"))
    if len(mf) != 1:
        sys.exit(f"预期 manifest 只有 1 个条目，实际 {len(mf)} 个，不敢改")

    config_name = mf[0]["Config"]
    print(f"  config 文件    : {config_name}")
    print(f"  layer 数       : {len(mf[0]['Layers'])}")
    print(f"  原 RepoTags    : {mf[0].get('RepoTags')}")

    # ── 读并修改 config json ────────────────────────────────────────
    cfg_raw = tin.extractfile(config_name).read()
    cfg = json.loads(cfg_raw)

    # 1) 修 license 标签：GPL-3.0 → MIT（与项目 LICENSE 文件一致）
    labels = cfg.get("config", {}).get("Labels", {})
    old_lic = labels.get("org.opencontainers.image.licenses")
    if old_lic != "MIT":
        labels["org.opencontainers.image.licenses"] = "MIT"
        cfg["config"]["Labels"] = labels
        print(f"  license 标签   : {old_lic} → MIT")
    else:
        print("  license 标签   : 已是 MIT，跳过")

    # 2) 补 healthcheck：
    #    镜像里没有 curl 之外的探活手段，但 /api/health 是**免鉴权**的
    #    （见 drouter-web.py：/api/health 与 /api/login 不要求 X-Token）。
    #    ⚠️ 但 web 是 HTTPS 且用自签证书 → curl 必须 -k，否则永远 unhealthy。
    #    端口从 /etc/drouter/web-port 读，默认 8443。
    if not cfg["config"].get("Healthcheck"):
        cfg["config"]["Healthcheck"] = {
            "Test": [
                "CMD-SHELL",
                'PORT=$(cat /etc/drouter/web-port 2>/dev/null || echo 8443); '
                'curl -fsSk --max-time 4 "https://127.0.0.1:${PORT}/api/health" '
                '>/dev/null 2>&1 || exit 1',
            ],
            "Interval": 30000000000,   # 30s（纳秒，docker 格式）
            "Timeout": 5000000000,     # 5s
            "StartPeriod": 20000000000,  # 20s，给首次启动留时间
            "Retries": 3,
        }
        print("  healthcheck    : 已补（/api/health，自签证书走 -k）")
    else:
        print("  healthcheck    : 已存在，跳过")

    new_cfg_raw = json.dumps(cfg, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")

    # ── 写新 tar ────────────────────────────────────────────────────
    with tarfile.open(DST, "w", format=tarfile.GNU_FORMAT) as tout:
        for m in members:
            if m.name == config_name:
                m.size = len(new_cfg_raw)
                tout.addfile(m, __import__("io").BytesIO(new_cfg_raw))
            elif m.name == "manifest.json":
                mf[0]["RepoTags"] = NEW_TAGS
                blob = json.dumps(mf, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")
                m.size = len(blob)
                tout.addfile(m, __import__("io").BytesIO(blob))
            elif m.name == "repositories":
                # docker save 的老式 tag 索引，格式 {repo: {tag: layerid}}
                old = json.load(tin.extractfile("repositories"))
                layer_id = None
                for repo, tags in old.items():
                    layer_id = list(tags.values())[0]
                new_repos = {}
                for t in NEW_TAGS:
                    repo, _, tag = t.rpartition(":")
                    new_repos.setdefault(repo, {})[tag] = layer_id
                blob = json.dumps(new_repos, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")
                m.size = len(blob)
                tout.addfile(m, __import__("io").BytesIO(blob))
            else:
                # layer.tar 等大文件：直接从原 tar 流式拷贝，零解压
                tout.addfile(m, tin.extractfile(m))

print(f"\n  ✅ 写出：{DST}")
print(f"  新标签：{NEW_TAGS}")
