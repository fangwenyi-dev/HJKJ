# 慧尖 LoRa 网关一体化插件

[![版本](https://img.shields.io/github/v/release/fangwenyi-dev/HJKJ?color=blue)](https://github.com/fangwenyi-dev/HJKJ/releases)

慧尖开窗器 LoRa 网关的 Home Assistant 一体化插件：**内置 Mosquitto Broker（端口 2022）+ mDNS 广播 + 网关集成 + 管理 Web UI + 小程序局域网直连**，装一个插件即可获得全部能力，无需再装官方 MQTT 加载项。

> ## 【迁仓说明 · 2026-10-06】这里就是唯一商店仓
>
> 本仓 `https://github.com/fangwenyi-dev/HJKJ`（Gitee 容灾
> `https://gitee.com/fangwenyi-dev/HJKJ`）是慧尖两个加载项（LoRa 网关 / HA语音插件）
> 唯一的发布源。旧地址 `ha-gateway-plugin` 与 `huijian-gateway-plugin-yy` 已冻结停更，
> 不会再有版本推送——**从旧卡用过来的，照下面四步迁一次**；新装用户直接看「安装」小节。
>
> **为什么必须重装、不能"改个名就继承"**：Supervisor 给加载项编的身份是
> `sha1(你当初添加的那个仓库地址)[:8] + "_" + config.yaml 里的 slug`
> （`supervisor/store/utils.py:12-15`、`supervisor/store/data.py:240`），`/data`
> 也按这个全名开目录（`supervisor/apps/app.py:758-760`）。换地址＝换身份＝
> 开一个新的空数据目录，所以旧卡不会自动指到这里。
>
> **迁移顺序别颠倒**：两个实例会抢宿主 2022 与 10998 端口，第二个起不来。
>
> 1. 先在旧卡的本加载项「配置」页，把已填的值抄下来（只存在加载项自己的 `/data`，不随迁移走）。
> 2. 卸载旧加载项；商店 ⋮ → 存储库，删除 `ha-gateway-plugin` 这个地址。
> 3. 添加 `https://github.com/fangwenyi-dev/HJKJ`，安装「慧尖 LoRa 网关」，回填配置，启动。
> 4. 重启一次 HA Core。
>
> **不会丢**：网关实体与子设备重命名（在 HA 实体注册表里）、成员称呼（写在 HA 的
> `/config`，不在加载项 `/data`）。**会丢**：旧加载项「配置」页的值，需要重填。

## 安装

1. **添加仓库**：设置 → 加载项 → 加载项商店 → 右上角 ⋮ → 存储库，添加
   `https://github.com/fangwenyi-dev/HJKJ`
   （国内网络若商店迟迟刷不出卡片，可改用 Gitee 镜像源
   `https://gitee.com/fangwenyi-dev/HJKJ`，内容逐字同步。）
2. **安装**：商店搜索「慧尖」→ 进入卡片点「安装」→「启动」。
3. **重启一次 HA**（仅首次安装必须）：集成代码随加载项落盘、HA 启动时加载。

启动后全自动：Broker 监听 2022、mDNS 广播 `huijian.local`、HA 的 MQTT 集成
自动指向内置 broker、网关集成自动落盘、ACL 用户隔离自动创建。

> **安装卡在"下载镜像"或报 connection reset**：镜像主源自 v1.7.17 起走
> 国内毫秒镜像（ghcr.1ms.run 透传）。若仍失败：①等 1 分钟重试（新版本
> 发布后镜像边缘缓存同步有短暂窗口）；②加载项详情页「配置 → 高级 →
> 镜像(Image)」覆盖为备选源，如
> `ghcr.nju.edu.cn/fangwenyi-dev/amd64-huijian-mqtt-broker`（树莓派等写
> aarch64），改完重新点安装；③商店 ⋮ → 检查更新后再试。

## 添加网关

LoRa 网关**上电即被自动发现**：HA 弹出「发现慧尖 LoRa 网关」卡片，点「添加」
并在表单确认即完成配置（v1.7.62 起**每一台都需在卡片上点确认**，含首台——
此前首台会静默自动添加、不可见）。也可在插件
Web UI（侧边栏「慧尖」）或 设置 → 设备与服务 → 添加集成 → 搜「慧尖」手动添加。

网关固件侧无需任何配置：固件内置连接参数（`huijian.local:2022`），
配网后自动接入本插件。设备实体覆盖 Cover / Button / Sensor / Number，
支持开/关/停/内倒/位置/速度/力度与子设备重命名。

## 与其他 MQTT 加载项共存（zigbee2mqtt / ESPHome / Tasmota…）

慧尖内置 broker 支持多账号 ACL 隔离，可与 zigbee2mqtt 加载项共存，两条路径任选：

### 路径 A（推荐）— z2m 直连慧尖内置 broker，不装官方 Mosquitto

1. 安装本插件并启动（自动创建 z2m 专用账号）；
2. zigbee2mqtt 加载项「配置」里 `mqtt.server` 填 `mqtt://<HA主机IP>:2022`；
3. `user` 填 `huijian_z2m`，`password` 填慧尖「配置」页的 MQTT 密码（默认 `huijian2022`）；
4. 重启 zigbee2mqtt。z2m 设备发现/控制开箱即用。

`huijian_z2m` 账号 ACL 仅 `zigbee2mqtt/#` + `homeassistant/#`，触不到慧尖
网关协议主题；慧尖网关域也触不到 z2m。HA 的 MQTT 集成已被慧尖指向内置
broker，单 broker 即全家桶，**无需共存桥**。

### 路径 B — 已有官方「Mosquitto broker」加载项（共存桥，默认关）

若 z2m/其他 MQTT 加载项必须跑在官方 Mosquitto 上（如已有存量设备/其他系统
依赖），慧尖提供自动共存桥：在慧尖「配置」页把 **MQTT 共存桥
（coexist_bridge_enabled）** 开关打开，并**成对填写**官方 broker 的账号密码
（`coexist_official_user/password`——必须是官方加载项自己配置页添加的账号，
勿填 huijian/huijian2022，官方用户库里没有），重启慧尖即建桥。

背景（为何需要桥）：HA 的 MQTT 集成全局唯一，慧尖为保证网关可达必须把它
指向内置 broker（:2022）；官方 broker 上的其他设备要与 HA 互通，唯一通道
就是这座方向分离桥。

- 默认桥只接 `zigbee2mqtt/#` 双向 + `homeassistant/#` 单向进；
- **v1.7.19 起可配置追加主题树**（`coexist_bridge_topics`）：逗号分隔
  `主题[:方向]`，方向 `in`=官方→慧尖（设备状态进 HA）、`out`=慧尖→官方
  （HA 命令送达设备）、缺省 `both`。常用生态示例：

  | 加载项 | 追加桥接主题填法 |
  |--------|------------------|
  | ESPHome（MQTT 模式） | `esphome/#` |
  | Tasmota | `tele/#:in, stat/#:in, cmnd/#:out` |
  | Shelly | `shellies/#` |
  | 自研前缀 | `你的前缀/#` |

  每条桥腿会自动、逐条对齐地扩 HA 连接账号的最小权限 ACL（in→只读、
  out→只写、both→读写），爆炸半径不超桥腿（v1.6.24 安全不变量）；
- **代码级红线（配置不可解除）**：`gateway/…`、`test/…`、`$SYS`、`#` 或
  `+` 开头的通配全匹配一律拒绝写入——开窗器指令面永不跨桥（防未认证
  物理控制）；`homeassistant` 树 out 腿永久钳制（防心跳回灌干扰官方侧
  用户）。违规项启动日志会打 `[共存桥] 拒绝 …` 并跳过，不影响其他项；
- 改配置后重启慧尖生效；官方 Mosquitto 停止/卸载后桥自动拆除；开关改回
  关，已建桥 30 秒内自动拆；
- 该开关**默认关闭**（v1.7.13 定案）：官方 7.x 起强制认证，未配凭据的桥
  连不上只会在官方侧日志留下周期拒绝记录，故改为手动开闸制；
- 若你的 `1883` 端口跑的是其他第三方服务（部分 NAS 套件如此），请保持
  开关关闭，勿开闸——桥会把任何监听 1883 的服务当官方对端。

## 微信「慧尖」小程序直连

插件内置与 Matter 固件网关 1:1 对等的 WS 网关（`ws://<HA>:9001/ws`，默认
开启），小程序经 mDNS 发现后局域网直连控制。注意：小程序与固件恒拨固定
端口 **9001**（微信 mDNS 不透传 TXT），请勿在选项中改端口；小程序列表只
显示已在集成中配对的网关。

## 升级

设置 → 加载项 → 慧尖 → 「更新」→ 重启加载项；**集成代码有变更时再重启
一次 HA**（Release 说明会标注）。数据不会丢失：网关/设备数据存于 HA 配置
目录并带自动备份与损坏救援。

## 配置项说明

加载项「配置」页每个字段都带**中文标题与中文说明**（v1.7.15 起官方本地化；
Web UI 内亦有速查卡），照说明填即可。要点：

- **MQTT 用户名/密码：不要修改**。固件内置 `huijian`/`huijian2022`，改动会
  在启动时被自动恢复——改了只会把自家网关挡在 broker 门外；
- 端口固定 2022（固件约定，不可配）；
- 其余开关（自动配置 MQTT 集成 / 自动装集成 / 共存桥 / 快速发现）均有
  默认值，多数用户零配置即可用。

---

- 架构详解：[ARCHITECTURE.md](ARCHITECTURE.md)
- 完整版本历史：[CHANGELOG.md](../CHANGELOG.md)
