# 第三方组件许可告知（慧尖 LoRa 网关加载项）

本文件告知「慧尖 LoRa 网关」加载项运行时包含的第三方组件及其许可证。
每一行都标明**取证方式**：`[逐字]` ＝ 今天读到过许可原文/官方元数据；
`[元数据]` ＝ 只读到 GitHub/PyPI 的 license 字段；`[待核]` ＝ 本机取不到，核到之前不许当结论用。

取证日期：2026-10-20。载体版本以 `config.yaml` 的 `version: 1.8.7` 与
`requirements.txt` 为准。

## 1. 本加载项自己打包的第三方件

| 组件 | 版本 | 许可 | 取证 |
|---|---|---|---|
| `zeroconf`（mDNS 广播，`mdns_publisher.py` 使用） | `0.132.0`（`requirements.txt` 钉死） | **LGPL-2.1-or-later** | `[逐字]` 上游仓库 `COPYING` @ tag `0.132.0` 正文即 GNU LGPL v2.1；`pyproject.toml` @ 同 tag 写 `license = "LGPL"`；`[元数据]` PyPI `zeroconf/0.132.0` classifiers 含 `License :: OSI Approved :: GNU Lesser General Public License v2 (LGPLv2)` |
| Mosquitto / mosquitto-clients | Alpine 仓库当前版本（未钉） | **EPL-2.0 OR BSD-3-Clause**（双许可） | `[逐字]` `eclipse-mosquitto/mosquitto` 的 `LICENSE.txt`：「dual licensed under the Eclipse Public License 2.0 OR the Eclipse Distribution License 1.0」并带 `SPDX-License-Identifier: EPL-2.0 OR BSD-3-Clause` |
| nginx（Ingress 反代） | Alpine 未钉 | BSD-2-Clause | `[元数据]` `nginx/nginx` license.spdx_id |
| curl | Alpine 未钉 | curl license（MIT 式宽松，需保留版权声明） | `[逐字]` `curl/curl` 的 `COPYING` 开头「COPYRIGHT AND PERMISSION NOTICE / Copyright (c) 1996 - 2026, Daniel Stenberg…」 |
| jq | Alpine 未钉 | MIT（软件）+ CC-BY-3.0（docs 目录） | `[逐字]` `jqlang/jq` 的 `COPYING`：前段为 MIT 原文，末段写明 docs 走 CC BY 3.0 |
| OpenSSL | Alpine 未钉 | Apache-2.0 | `[元数据]` `openssl/openssl` license.spdx_id |
| ca-certificates | Alpine 未钉 | 各 CA 自有证书策略（Mozilla CA 政策系） | `[待核]` |
| python3 / py3-pip | Alpine 未钉 | PSF（CPython）/ MIT（pip） | `[待核]` |
| Home Assistant base image<br>`ghcr.io/home-assistant/{arch}-base:latest` | **`latest`（未钉）** | Apache-2.0（镜像仓库本体），其内 Alpine 包许可另计 | `[元数据]` `home-assistant/docker` license.spdx_id；`[逐字]` 待核 |

### 关于 `zeroconf` 是 LGPL 这件事

常见误传是"python-zeroconf 是 Apache-2.0"。**本加载项锁定的 0.132.0 不是**：上游正在走
LGPL-2.1 → Apache-2.0 的**重许可流程**（`RELICENSING.md`，2026-08 起征集同意书），并且该文件
自己写明「Nothing already released changes license」——已经发布的版本不换许可。
所以 0.132.0 这一版按 LGPL-2.1-or-later 交付。

LGPL-2.1 对"随整机分发的容器应用"通常意味着：随附本许可文本、允许用户替换该库、
不为调试修改而禁止逆向。本加载项满足前两条的路径是现成的（加载项源码在公开仓、
`zeroconf` 由 `pip` 装入容器、用户可自行重建镜像替换该库）；
**是否还有别的义务，请交付方法务确认，本文件不下法律结论。**

## 2. 由用户侧 HA 提供、不随本加载项分发的件

| 组件 | 许可 | 说明 |
|---|---|---|
| Home Assistant Core（`mqtt`/`http`/`repairs` 等平台） | Apache-2.0 | `[元数据]` `home-assistant/core`。本加载项的集成代码调用它，但不把它打进镜像 |

## 3. 本仓自写、无第三方许可义务的前端件

`www/js/huijian.js`、`www/js/qr.js`、`www/js/starsky.js`、`www/css/huijian.css`、
`www/index.html` 均为本仓自写。其中 `qr.js` 是**自带实现的零依赖二维码编码器**
（v1.7.38 定案：Ingress 有 CSP、前端从不连外网，因此不引 CDN 库，也不 import/fetch 任何外部资源）。
这些文件随本仓 MIT 许可分发。

## 4. 未决与复跑命令

1. `[待核]` Alpine 侧各包的实际授权（Alpine 重新打包时可能补自己的包装条款）。
   复跑：
   ```bash
   docker run --rm ghcr.io/home-assistant/amd64-base:latest \
     apk add --no-cache mosquitto nginx curl jq openssl ca-certificates python3 py3-pip >/dev/null && \
     apk info -L mosquitto nginx curl jq openssl ca-certificates python3 py3-pip
   ```
2. `[待核]` Home Assistant base image 自身的 LICENSE 原文（`latest` 未钉版本）。
   复跑：`gh api repos/home-assistant/docker/contents/LICENSE --jq .content | base64 -d | head`
3. `[待核]` PyPI 上 `zeroconf/0.132.0` 除 LGPLv2 之外还挂了一个
   `License :: Other/Proprietary License` 分类，含义不明，需要读上游发布说明或提 issue 问。
4. **风险提示（决策项，非缺陷）**：`Dockerfile` 用 `FROM ghcr.io/home-assistant/{arch}-base:latest`，
   Alpine 侧组件一律不钉版本 ⇒ 同一个 `version` tag 的镜像在重新构建时内容会变，
   已发布出去的镜像与其"许可清单"可能不再一致。要在商用侧把这件事做硬，需要
   ① base 镜像钉 digest、② apk 包钉版本、③ 每次构建产出这份许可清单的快照进 Release 附件。

## 5. 与本加载项无关、但常被一起问到的两件事

- 本加载项**不含**大模型/语音模型（语音是另一个加载项「慧尖HA语音插件」，其模型的第三方告知
  由语音侧自己维护，不在本文件范围）。
- 本加载项**不含** ESP32 固件二进制；网关固件由固件仓独立发布，其第三方告知不在本文件范围。
