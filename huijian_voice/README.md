# 慧尖HA语音插件（huijian_voice）

Home Assistant 加载项：局域网内的中文语音助手服务器。本地 STT/TTS/NLU，
执行走 HA 意图与 REST；配套慧尖语音卫星与 LoRa 网关。

- 版本：见 [`config.yaml`](config.yaml) 的 `version:`（与面板、集成 manifest、
  `www/version.json`、CHANGELOG 六源同版本，由 `tests/test_release_consistency.py` 钉死）
- 架构：`amd64` / `aarch64`（单仓多架构 manifest）
- 镜像：阿里云 ACR 主源（国内直连），`ghcr.io/fangwenyi-dev/{arch}-huijian-voice` 灾备
- 许可：MIT（仓库根 `LICENSE`）；第三方组件与模型见
  [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)

## 装完即用（3 步）

1. 加载项商店添加仓库 `https://github.com/fangwenyi-dev/HJKJ` → 安装
   「慧尖HA语音插件」→ 启动。（国内网络受扰时改用同源 Gitee 镜像
   `https://gitee.com/fangwenyi-dev/HJKJ`；**二选一，勿同加**——两个地址在
   Supervisor 里是两张独立的卡。）
2. 首启自动下载默认档模型（下载面约 1 GB，占盘约 1.4 GB），管理页「模型」区看进度。
3. **重启一次 HA Core**——集成由 `boot.sh` 落盘到 `custom_components`，运行中的
   容器不换代码。

之后在管理页（Ingress「慧尖语音」）配音色/语速/纠错表，在「设备配对」区复制带
token 的端点给卫星或小程序。完整说明、硬件建议、排障与诚实限制清单见
[`DOCS.md`](DOCS.md)（它同时是商店「说明」页的内容）。

## 开发者须知（本目录内）

| 对象 | 位置 | 说明 |
|---|---|---|
| 级联 NLU | `core/pipeline.py` | 字面表 → klar → TextCNN → 查询族 → LLM 兜底；安全闸（否定/疑问/连排/点名查无/歧义）都在这条链上 |
| 目标解析 | `core/nlu/targets.py` | 区域/设备名/泛称提取与候选评分（⑥ 设备词子串优先，含三道跨词根护栏） |
| 执行 | `core/executor.py` | 慧尖意图话术 + grounded 直调服务映射；播报"台=设备、个=零件" |
| 集成侧 | `custom_components/huijian_ai/` | 随镜像分发、由 `boot.sh` 落盘；窗控/开关意图实装在 `intent_window_control.py` 等 |
| 资产锁 | `models.lock.json` / `firmware.lock.json` | 下载腿 + sha256；换腿/换库要**三处成对**改（image、push-acr、readiness） |

本地回归（两套测试**不得同进程混跑**，`conftest` 会互相占名）：

```bash
cd huijian_voice && python -m pytest tests -q          # 语音侧
python -m pytest huijian_mqtt_broker/tests -q          # 网关侧，另一次调用
```

Windows 本机跑语音侧需要一份未跟踪的 `opus.dll`：给 `HUIJIAN_OPUS_DLL_DIR`
指目录即可；不指的表现是若干条以"opus 绑定缺失"红，属环境不是产品缺陷。

## 版本与发布

发布位是本仓（`fangwenyi-dev/HJKJ`）；发版走 `.github/workflows/ci-voice.yaml`
（**仅 workflow_dispatch**——与网关加载项同仓后，push 触发会让"改语音"连带发网关版，
反之亦然）。六源齐 bump、双推（GitHub + Gitee）、tag 前缀 `vo-`，缺一条即红。
