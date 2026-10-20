# 第三方组件与模型许可清单（慧尖HA语音插件）

本文件列出慧尖语音加载项**运行期使用或按需下载**的第三方组件及其许可。
取证日期：2026-10-20。每条标注**证据来源**；标"待核"的是本机取证受阻的条目，
附可复跑命令——售卖口径不许把"待核"写成"已核"。

取证口径：模型权重的许可看**上游发布页/包内 LICENSE**，不看转换者（k2-fsa）的
仓库许可；代码依赖看 `pip show` 的 License 字段与上游仓库 `license.spdx_id`。

---

## 一、默认档（客户装完即用的那两个模型）

| 组件 | 用途 | 许可 | 证据 |
|---|---|---|---|
| **FireRedASR2-AED**（int8 onnx） | STT 默认档 | **Apache-2.0** | ModelScope 模型卡 `FireRedTeam/FireRedASR2-AED` 的 `License` 字段实测回 `apache-2.0`（`GET /api/v1/models/…`）；上游代码仓 `FireRedTeam/FireRedASR`（GitHub）`license.spdx_id=Apache-2.0` |
| **MeloTTS zh_en**（vits onnx） | TTS 默认档 | **MIT** | 解包件内自带 `LICENSE`：`Copyright (c) 2024 MyShell.ai` + MIT 正文（本机 `_hjmodels/tts_melo_zh_en/vits-melo-tts-zh_en/LICENSE` 逐字读）；上游 `myshell-ai/MeloTTS`（GitHub）`spdx=MIT` |

Apache-2.0 与 MIT 均**允许商用与再分发**，条件为保留版权声明与许可副本——本文件
即该副本的载体。

## 二、可选档（面板里可切换的引擎）

| 组件 | 用途 | 许可 | 证据/状态 |
|---|---|---|---|
| SenseVoice-Small | STT 候选档 | **Apache-2.0** | ModelScope `iic/SenseVoiceSmall` 模型卡 `License` 字段实测回 `Apache License 2.0` |
| FireRedASR2-CTC | STT 候选档 | 待核 | ModelScope 上 `FireRedTeam/FireRedASR2-CTC` 这条 id 实测 `record not found`（转换自哪个上游卡未定）；同族 AED 与上游代码仓均 Apache-2.0，**但同族不等于同证**。复跑：`curl -s https://www.modelscope.cn/api/v1/models/<正确id> \| jq .Data.License` |
| Kokoro multi-lang v1.1 | TTS 候选档 | 待核 | 权重作者 hexgrad；本机 `huggingface.co` 不可达（`curl` 回 000），未取到卡面许可。ONNX 转换封装 `thewh1teagle/kokoro-onnx`（GitHub）实测 `spdx=MIT`（代码，不含权重）。复跑：`curl -s https://huggingface.co/api/models/hexgrad/Kokoro-82M \| jq .cardData.license` |
| Matcha zh_en + ViCos 声码器 | TTS 候选档 | 待核 | 代码侧 `espnet/espnet`、`wenet-e2e/wenet` 实测均 **Apache-2.0**；权重卡（matcha-icefall-zh-en / vocos-16khz-univ.onnx）本机取不到。复跑同上一条 |

**售卖口径**：默认档两条已逐字取证为可商用；可选档里"待核"三条在核到之前，
**不得写进宣传材料或被设为出厂默认**。当前出厂默认=FireRedASR2-AED + MeloTTS，
不触碰待核项（`models.lock.json` 的 `default_provider` 字段实证）。

## 三、随镜像分发的组件（不下载）

| 组件 | 许可 | 证据 |
|---|---|---|
| sherpa-onnx（推理运行时） | Apache-2.0 | `pip show`：`Apache licensed, as found in the LICENSE file`；上游 `k2-fsa/sherpa-onnx` `spdx=Apache-2.0` |
| onnxruntime | MIT | `pip show`：`MIT License` |
| aiohttp | Apache-2.0 AND MIT | `pip show` 原值 |
| numpy | BSD-3-Clause（以包内 LICENSE 为准） | 本次 `pip show` 的 License 字段为空 ⇒ 未取证，复跑 `python -m pip download numpy` 后看 `LICENSE.txt` |
| pypinyin | MIT | `pip show`：`MIT` |
| zeroconf | 以包内 LICENSE 为准（本次字段空） | 同上 |
| opuslib-next / libopus | 包装器 MIT；libopus BSD-3 | 本次未逐字取 libopus 副本 |
| TextCNN 意图模型（`nlu_data/`，随镜像） | 本家自训资产 | 训练与导出均在本项目内完成（无外部权重） |
| klar-ha-nlu（一级 NLU 引擎，首启从 GitHub Release 拉二进制并核验 sha256） | **MIT** | 上游 `FABbricate-IT-Solutions/klar-ha-nlu`（GitHub）`license.spdx_id=MIT` 实测（2026-10-20 取证，仓库最近推送 2026-10-19）；加载项只做**下载+校验+降级**，失败即回落 TextCNN |

## 四、慧尖自有代码的许可

仓库根 `LICENSE` = **MIT，Copyright (c) 2026 fangwenyi-dev**。MIT 授予下游"使用、
复制、修改、合并、出版、发行、再授权及/或销售"的权利，条件是保留版权声明与本许可。
