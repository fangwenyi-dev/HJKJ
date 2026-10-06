# CLAUDE.md - 慧尖 Gateway Plugin 开发指南

## 核心规则

### 推送与验证流程
**2026-09-17 用户裁定：网关仓恢复双推 Gitee**（推翻 2026-09-16 的"只推 GitHub"）。
停推的实际代价已实证：Gitee 镜像冻结在 v1.7.24/41a1506，而**已配置 Gitee
仓库的 HA 实例，其 Supervisor 商店数据停在 `latest=1.7.24`**（真机
192.168.1.91 实测：`update.hui_jian_lora_wang_guan_update state=off
latest=1.7.24 installed=1.7.24`）→ 1.7.25 及以后对这批机器**永久不可见**，
"有可用更新"提示静默失效。故此后每次发版必须：

```bash
# 1. 推 GitHub（SSH 通道，见下方「GitHub 推送通道」，勿改回 HTTPS）
git push origin main

# 2. 推 Gitee（main 必推；商店读的是仓库内 config.yaml 的 version，
#    故 main 到位即可被发现）
git push gitee main

# 3. 推本次 tag（两源都要有 tag：镜像/回退按 tag 定位，Release 正文亦挂 tag）
git push origin v1.7.x
git push gitee v1.7.x        # 若本地缺该 tag：先 git fetch origin tag v1.7.x --force

# 4. 等待 CI 启动后检查状态（GitHub）
gh run list --repo fangwenyi-dev/HJKJ --limit 3
#    gh 未认证时可用只读 REST（公开仓）：
#    https://api.github.com/repos/fangwenyi-dev/HJKJ/actions/runs?per_page=5
#    以及镜像可拉性：GET https://ghcr.io/token?scope=repository:<org>/<arch-IMAGE_NAME>:pull&service=ghcr.io
#    再 HEAD https://ghcr.io/v2/<org>/<arch-IMAGE_NAME>/manifests/<版本>

# 5. 如果有失败，查看详情并修复
gh run view <run-id> --repo fangwenyi-dev/HJKJ --log-failed
```

**Gitee Release 已自动化（2026-10-06，语音侧并入商店仓那一刀的 D4）**：`ci.yaml` 的
`gitee-release` job 从墓碑复活。此前它下线后 v1.7.63/64/65 三条 Gitee Release 全靠手工
POST——漏一条就静默漂移且没人知道。现在它 `needs: [prepare, release]`，正文取 CHANGELOG
对应版本段（缺段回落 prepare 产物），同 tag 已存在则 **PATCH 同步正文**（PUT 必 405，
PATCH 必须同载 tag_name+name），并**先等 Gitee 镜像仓真的有本次 sha 才建**——等不到就
响亮失败而不是把 Release 指到旧提交。⇒ 双推顺序从此是硬依赖：`git push origin main`
之后**尽快** `git push gitee main`；该 job 若报"Gitee 镜像仓 10 分钟内没有本次提交"，
补推 gitee 后重跑该 job 即可（GitHub 侧 Release 不受影响）。两条背景不变：
- Supervisor 商店不受影响（它只读仓库内 `config.yaml`）；
- 但慧尖 Web UI 的升级徽章走的是「GitHub + Gitee releases 双源并集取最大」
  （`www/js/huijian.js` `fetchLatestRelease`），GitHub 一路失败/被限流时只剩
  陈旧 Gitee 数据（实测 Gitee Release 首页最大 v1.7.15），会把"最新发布版"
  误判成旧值而**静默不亮徽章**。改这条逻辑（Gitee 降级为显式回退 + 源不可见
  要留痕）前，勿把徽章沉默当作"已是最新"。

**CI 状态说明：**
- `success` ✅ — 通过
- `failure` ❌ — 需要修复
- `startup_failure` ⚠️ — workflow 文件问题
- `queued` ⏳ — 等待执行

**发现问题必须：**
1. 查看失败日志
2. 修复问题
3. 重新推送
4. 再次验证 CI 通过

---

### 回归测试（本地必做）
CI 的 lint job 已跑 `pytest huijian_mqtt_broker/tests`（v1.6.3 起）。
本地推送前必须同步跑通：

```bash
# Windows 侧（WSL python3 无 pytest 时用本机 python）
cd huijian_mqtt_broker
/mnt/d/progrem/python/python.exe -m pytest tests -q
bash -n run.sh                      # shell 语法
# v1.6.26（审计 A-2）：必须 compileall 递归——py_compile 的 `*.py` glob 只
# 匹配顶层，mqtt_handler/ 子包（v1.6.25 拆包引入）会整体漏出语法门。
python -m compileall -q custom_components/window_controller_gateway
```

**两套测试不得在同一 pytest 进程里混跑（2026-10-06 语音侧并入后新增的约束）**：
`huijian_voice/tests` 里 10+ 处写的是 `from conftest import FakeHAClient`——裸模块名，
而合并后仓里有两个 `tests/conftest.py`，同一进程内先被导入的那个会占住
`sys.modules["conftest"]`，另一套就拿错实现（实发：混跑网关 `test_audit_round8` 时语音
`test_relay_passes_through_echoed` 报 `ImportError: cannot import name 'FakeHAClient'
from 'conftest' (…\huijian_mqtt_broker\tests\conftest.py)`，而单跑该文件 10/10 绿）。
⇒ 本地要全绿就分两条命令跑（各自 CI 入口本来就分开：网关 lint 跑
`pytest huijian_mqtt_broker/tests`，语音 lint 跑 `pytest huijian_voice/tests`）。
真要消掉这颗雷，改法是给语音侧那 10+ 处换成按文件路径取 helper（或把 FakeHAClient 移进
普通模块），不是给某一套加 `sys.path` 顺序技巧——顺序技巧只是把雷换个埋法。

另：语音侧 opus 相关测试在 Windows 本机需要未跟踪的 `_winlibs/opus.dll`（gitignored，
按设计不随 `git archive` 搬动）。合并树里跑时给 `HUIJIAN_OPUS_DLL_DIR` 指一份即可，
不指的表现是 21 条以「opus 绑定缺失」红——**环境性，不是产品缺陷**（Linux/CI 用系统 libopus）。

给 registry 兼容层/事件属性等"静默失效面"加改动时，须补断言实参的测试
（参考 tests/test_utils.py 的 RecordingEntityRegistry 模式）——
v1.6.0 的 "entity" 字面量回归曾骗过全部 38 个测试，教训记录于此。

---

### GitHub 推送通道：SSH（2026-09-22 定案，实测通过）
**origin 已切 SSH**：`git@github.com:fangwenyi-dev/HJKJ.git`。
本机 `~/.ssh/config` 把 `github.com` 指向 `ssh.github.com:443`（22 端口被本网络
拒，必须走 443）。**禁止把 origin 改回 HTTPS**。

GitHub 侧一律 `git push origin main`，**Qoder 内置 git 2.52 也能推**（SSH 不经
schannel），Qoder IDE 源代码管理面板同样适用。2026-09-22 实测：系统 git 与
`/mingw64/bin/git` 直推 SSH 均得 `Everything up-to-date`；`ssh -T -p 443
git@ssh.github.com` 回 `Hi fangwenyi-dev!`。

**HTTPS 仅作后备**，且必须同时满足两条，缺一即挂：
1. `gh auth setup-git` 已配（`credential.https://github.com.helper` → gh 的
   `auth git-credential`；GCM 里本来没有 github.com 条目，单走 GCM 会卡在
   要用户名密码）；
2. 用**系统 git 2.55**（`D:/progrem/git/Git/cmd/git.exe`）——内置 2.52 走
   schannel 推 GitHub 恒撞 `CRYPT_E_NO_REVOCATION_CHECK`（Gitee 不受影响）。
   Qoder Bash 不读 `~/.bashrc`，故本机以 `~/bin/git` wrapper 兜底。

---

### Gitee 凭据（现行生效：2026-09-17 恢复双推；remote 仍不带 token）
```bash
# remote 保持干净 URL（.git/config 不落任何密钥）
git remote set-url gitee https://gitee.com/fangwenyi-dev/HJKJ.git
```
**两条已验证的认证路径（均无需 URL 内嵌 token）：**
1. **Windows 侧推送（2026-09-17 再次实测可用）**：Git Credential Manager 已存
   gitee.com 条目（host=gitee.com, username=oauth2），`git push gitee main`
   直接走 GCM——本次恢复双推即经此路径成功（41a1506..dc1e7de）。设
   `GIT_TERMINAL_PROMPT=0` 可确保不弹交互。
2. **WSL/agent 侧推送**：一次性注入 token，不落盘到 remote 配置：
   ```bash
   TOK=$(cat /mnt/c/Users/fangwenyi/.gitee_token | tr -d '\r\n')
   GIT_TERMINAL_PROMPT=0 git push "https://oauth2:${TOK}@gitee.com/fangwenyi-dev/HJKJ.git" main
   ```
GitHub 侧现已改 SSH（见上节「GitHub 推送通道」，Windows 与 WSL 均优先 SSH）；
仅在确实要走 HTTPS 时才用 `gh auth token` 注入一次性 URL（WSL 无 GCM 交互）。
禁止再把 token 写回 `git remote set-url`——历史做法会让明文 token 长期驻留
`.git/config`，任何读取该文件的工具/日志/备份都可能带出。
`git push gitee --tags` 对已存在 tag 会逐条报 `! [rejected] ... (already exists)`
并整体 exit=1——这不是失败，新 tag 照样推上去了；要精确推指定 tag 用
`git push gitee refs/tags/v1.7.x`（本地缺该 tag 时先 `git fetch origin tag v1.7.x --force`）。

---

## 项目架构

### 一体化插件
- **插件**: `huijian_mqtt_broker/` — Mosquitto Broker + mDNS + 网关集成
- **集成**: `huijian_mqtt_broker/custom_components/window_controller_gateway/`
- **Web UI**: `huijian_mqtt_broker/www/`（三文件化：`index.html` 骨架 +
  `css/huijian.css` + `js/huijian.js`，v1.6.25 起；`version.json` 供更新检查）

### 版本管理
- `config.yaml` — 插件版本
- `www/version.json` — 版本号
- `www/index.html` — `CURRENT_VERSION` 变量
- `custom_components/.../manifest.json` — 集成版本

**所有版本号必须一致！**

---

## MQTT 协议

> **端口：2022**。固件 v1.4.2 起 Broker 只监听 2022（1883 已废弃，避与官方
> Mosquitto 加载项冲突）。mosquitto.conf / run.sh / mDNS / mqtt_bootstrap 全部 2022。

### 主题
| 主题 | 方向 |
|------|------|
| `gateway/{gateway_sn}/req` | HA → 网关 |
| `gateway/rpt_rsp` | 网关 → HA |

### 命令
> 线值均为**字符串**（const.py COMMAND_VALUE_*，v1.6.17 对照固件
> app_mqtt_business.c 逐一实证）。旧表 open=0/close=1/stop=2 是废弃固件
> 时代记载，已按现网订正——改动命令映射前以 const.py 与固件为准，勿信旧文档。

| 命令 | 值 | 说明 |
|------|-----|------|
| `open` | "100" | 开窗 |
| `close` | "0" | 关窗 |
| `stop` | "101" | 停止 |
| `a` | "200" | 内倒 (toggle) |
| `wind_lock_tilt` | "0" | 内倒模式 |
| `wind_lock_flat` | "1" | 平开模式 |
| `set_position` | 0-100 | 设置位置 |
| `set_speed` | 0-100 | 设置速度 |
| `set_strength` | 0-100 | 设置力度 |

---

## 设备类型

### 5005 设备（支持内倒）
- 开/关/停
- 位置滑块 0-100%
- 速度/力度滑块
- 内倒按钮 (command "a", value 200)
- 平开模式/内倒模式按钮
- 重命名/移除

### 非5005 设备
- 开/关/停
- 位置滑块 0-100%
- 速度/力度滑块
- 重命名/移除

---

## 小程序局域网直连 WS 网关（v1.6.15 引入，v1.6.16 默认开定案）

- 集成内置 aiohttp WS 服务器（`ws_gateway.py`），1:1 复刻固件
  `app_ws_gateway.c` 的 JSON-over-WebSocket 协议：`ws://<HA主机>:9001/ws`，
  令牌走 Sec-WebSocket-Protocol 子协议头握手（不匹配不发 101）。
- **默认开**（`DEFAULT_WS_GATEWAY_ENABLED=True`）：对齐固件"配网完成即
  常听"语义（matter-broker main.cpp 无条件 `app_ws_gateway_start`，无用户
  开关）。教训实锤（2026-09-02）：默认关导致小程序 mDNS 发现网关后
  9001 恒 Connection refused，且客户无从知晓该隐藏开关。
- 安全门禁与固件同构：令牌握手 401 拒连（默认令牌 = 小程序内置同值，
  **自定义令牌必须两侧同步**，否则握手全拒）、认证成功才占槽（≤4）、
  空闲 300s 断开、帧长上限；options 可显式关闭/改端口/令牌。
- 生效条件：加载项升级后必须 **重启 HA**（集成代码随加载项落盘、HA 启动
  时加载，运行中容器不换代码）。1.6.15 在线实例可先在「集成→慧尖→
  选项」勾选"小程序局域网直连"立即启用。

### 与小程序/固件联审定案（v1.6.17，四路独立审计 + 一手复核）

四路并行只读审计（消息契约 / 业务语义联动 / 发现与网络配置 / 握手会话）
结论：**协议骨架三方逐字对齐**（`cmd` 请求键 vs `type` 响应键、子协议
令牌握手、错误文案、-1=未知、帧长/槽位/空闲超时）。真实缺口不在协议，
在下面这些**语义与生命周期**上，改动本模块前务必对照：

- **视图层必须做固件同款入界钳制**：`device_ws_view` 是唯一出口——
  position 只接受 0..100（r_travel=255 是"未校准/离线"标记，固件
  `app_protocol_bridge.cpp:2133/2781` 直接丢弃），state 从**钳制后**的
  position 推导；电池 raw 只接受 [80,140]（固件 BATTERY_RAW_MIN/MAX，
  12V 锂电 9.5–12.6V 放宽到 8–14V）。HA 侧 sensor 的"未校准"文案语义
  不受影响（钳制只在 WS 视图层收敛）。
- **WS 通道不经过 HA「删除」按钮，必须自己闭环本地删除**：003 解绑
  确认分支注释"本地删除已由删除按钮流程完成"，所以 `_cmd_unbind` 不
  remove_device 的话设备永远留在缓存/注册表/映射并在下次 get_devices
  复活（幽灵设备）。同理 003 绑定确认走 `add_device` 直达、不经
  `update_device_status` 推送漏斗，需补一次 `_notify_status_listeners`
  新设备才能即时出现在小程序。
- **`connected` 是业务口径（1800s 无上报），不等于"MQTT 发布成败"**：
  control 映射缺失时广播给**全部**已注册网关（固件 P2 定式），跳过
  connected=False 网关属行为分歧；而 gateway_list 的 online 反过来要
  比固件更严——`WS_GATEWAY_ONLINE_STALE_SECONDS=900` 新鲜度双条件。
- **令牌持久化失败必须回滚内存值**：否则形成"小程序已存新令牌、HA
  重启回退旧令牌"的永久 401（固件 NVS 写失败同款回滚语义）。
- **改「小程序 WS 网关端口」= 直连静默失联**：微信 mDNS **不透传 TXT
  记录**，小程序恒拨 9001，端口无法协商下发；options 文案与 README
  FAQ 已警示。
- **半开口径**：WS 能连 ≠ 可控。服务器任一网关条目存在即监听，但列表
  只反映**已注册的集成条目**——小程序连上后列表为空属正常，需先在
  集成中添加该 LoRa 网关。
- **与固件网关共存**：两者都广播 `_mqtt._tcp`，实例名分别为
  `huijian-mqtt`（插件，mdns_publisher.py:88）与 `huijian`（固件），
  小程序必须读 `res.serviceName`（**`res.name` 恒 undefined**——
  "发现服务: undefined" 根因）才能区分；两者还都广播主机名
  `huijian.local`（A 记录冲突，待真机验证，建议只保留一个广播者）。
- 小程序会话层定案（已在小程序仓库同批修复）：onShow/前台恢复传
  `connect(false)` 且 `connect()` 入口清 `_reconnectTimer`（否则重连
  阶梯无限续命，"第1→4→回到1"日志根因）；`_cleanup()` 要 close 底层
  socket（防服务端僵尸连接占槽）；`pair_ack`/`type:"error"` 必须发
  事件让 UI 可见。

## Supervisor API（2026-08-27 实测定案，v1.6.3 纠偏）

### 权限要求
```yaml
# config.yaml
homeassistant_api: true   # Web UI 经 /api/ha/ 代理访问 HA Core REST API 的前提
# hassio_api 已移除：插件零调用 Supervisor 管理 API，权限最小化
```

### API 自更新不可行（勿再尝试）
Supervisor 安全设计禁止加载项通过任何 API 更新自身：
- `/addons/self/update`、`/store/addons/{slug}/update`：因 REQUEST_FROM 校验恒 **403**
- `hassio.addon_update`（HA Core 服务）：add-on token 调用实测 **400**

**唯一升级路径**：管理员在 Supervisor「设置 → 加载项 → 慧尖」页面点击「更新」。
Web UI 的"一键升级"按钮自 v1.5.3 起仅跳转加载项详情页（doUpgrade 无任何 API 调用）。
本节旧文档「/addons/self/update 免认证」「一键升级依赖 hassio_api」均为错误记载，
`/api/supervisor/` nginx 死代理已随之删除（run.sh 与 ingress.conf 同步）。

### API 代理路径（现状）
- HA Core API: nginx `/api/ha/` → `http://supervisor/core/api/`（token 由 nginx 注入，不进浏览器）
- 更新检查: `/api/gitee/releases` + `/api/github/releases` **双源并集取版本号
  最大者**（v1.6.7 定案，见 huijian.js fetchLatestRelease；旧"默认源→失败回退"
  记载与代码不符——Gitee Release 自 v1.6.21 起才由 CI 自动补发且可能滞后，
  单信任何一源都会把"最新发布版"误判成旧值）
- 本地状态: `/api/status`（status.json 探活）、`/api/broker`（连接数）、`/api/version`、`/api/integration`

---

## 常见问题

### CI startup_failure
通常是 workflow 文件问题，检查：
1. YAML 语法
2. Action 版本
3. Secrets 配置

### 401 Unauthorized
检查 `config.yaml` 是否包含 `homeassistant_api: true`

### 内倒按钮不工作
确认使用 command `"a"` 和 value `200`，不是 `wind_lock_tilt`
