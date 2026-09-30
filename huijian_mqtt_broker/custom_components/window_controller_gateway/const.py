"""开窗器网关常量定义"""
from typing import Final

# ==================== 集成域 ====================
DOMAIN: Final = "window_controller_gateway"

# ==================== 配置相关 ====================
CONF_GATEWAY_SN: Final = "gateway_sn"
CONF_GATEWAY_NAME: Final = "gateway_name"
CONF_DEVICE_SN: Final = "device_sn"
CONF_DEVICE_NAME: Final = "device_name"
DEFAULT_GATEWAY_NAME: Final = "慧尖网关"
CONF_DISCOVERY_INTERVAL: Final = "discovery_interval"
CONF_AUTO_DISCOVERY: Final = "auto_discovery"
CONF_DEBUG_LOGGING: Final = "debug_logging"
DEFAULT_DISCOVERY_INTERVAL: Final = 300
DEFAULT_AUTO_DISCOVERY: Final = True
DEFAULT_DEBUG_LOGGING: Final = False

# ==================== 服务相关 ====================
SERVICE_START_PAIRING: Final = "start_pairing"
SERVICE_REFRESH_DEVICES: Final = "refresh_devices"
SERVICE_MIGRATE_DEVICES: Final = "migrate_devices"
SERVICE_RENAME_DEVICE: Final = "rename_device"
SERVICE_TRANSFER_DEVICE: Final = "transfer_device"

# ==================== 属性相关 ====================
ATTR_DEVICE_SN: Final = "device_sn"
ATTR_DEVICE_NAME: Final = "device_name"
ATTR_NEW_NAME: Final = "name"
ATTR_DEVICE_TYPE: Final = "device_type"
ATTR_POSITION: Final = "position"
ATTR_CURRENT_POSITION: Final = "current_position"
ATTR_TARGET_POSITION: Final = "target_position"
ATTR_BATTERY: Final = "battery"
ATTR_VOLTAGE: Final = "voltage"
POSITION_MIN: Final = 0
POSITION_MAX: Final = 100
SENSOR_TIMEOUT_MINUTES: Final = 15

# ==================== 设备相关 ====================
DEVICE_TYPE_WINDOW_OPENER: Final = "window_opener"
DEVICE_TYPE_GATEWAY: Final = "gateway"
MAX_DEVICES_PER_GATEWAY: Final = 32
DEVICE_TO_GATEWAY_MAPPING: Final = "device_to_gateway_mapping"
GLOBAL_MANUALLY_REMOVED_DEVICES: Final = "global_manually_removed_devices"
# v1.7.12（第 6 轮审计 E-1/CF-F2）："忽略网关"跨 HA 重启持久层——discovery
# 的 ignored_gateways 集合与它共享同一 set 对象，随 persist.py 统一落盘/加载
# （旧版纯内存，重启后已忽略的网关卡片复活）
GLOBAL_IGNORED_GATEWAYS: Final = "global_ignored_gateways"
DEVICE_SETPOINTS: Final = "device_setpoints"  # 设备参数设定值（速度/力度等），持久化，重启不丢失

# ==================== MQTT 相关 ====================
DEFAULT_COMMAND_ID: Final = 1
MAX_COMMAND_ID: Final = 999999
GATEWAY_TIMEOUT_SECONDS: Final = 1800  # 网关30分钟无上报即判定离线（网关约5分钟心跳上报一次）
TOPIC_GATEWAY_REQ_FORMAT: Final = "gateway/{gateway_sn}/req"
TOPIC_GATEWAY_RSP: Final = "gateway/rpt_rsp"
MQTT_MAX_RETRIES: Final = 5
MQTT_MIN_JITTER: Final = 0.5
MQTT_MAX_JITTER: Final = 1.5
MQTT_RETRY_DELAY_MAX: Final = 60
PROTOCOL_HEAD: Final = "$SH"
DEVICE_TYPE_CURTAIN_CTR: Final = "curtain_ctr"
PAIRING_SN_PLACEHOLDER: Final = "FFFFFFFFFFFF"
COMMAND_VALUE_OPEN: Final = "100"
COMMAND_VALUE_CLOSE: Final = "0"
COMMAND_VALUE_STOP: Final = "101"
COMMAND_VALUE_TOGGLE: Final = "200"
ATTRIBUTE_W_TRAVEL: Final = "w_travel"
ATTRIBUTE_WIND_LOCK_MODE: Final = "rwp_wind_lock_mode"
ATTRIBUTE_WINACT_SPEED: Final = "rwp_winact_speed"  # 开窗速度（0-100）
ATTRIBUTE_WINACT_STRENGTH: Final = "rwp_winact_strength"  # 开窗力度（0-100）
SPEED_MIN: Final = 0  # rwp_winact_* 系列参数共用范围下限（速度/力度）
SPEED_MAX: Final = 100  # rwp_winact_* 系列参数共用范围上限（速度/力度）
COMMAND_VALUE_WIND_LOCK_TILT: Final = "0"   # 内倒模式
COMMAND_VALUE_WIND_LOCK_FLAT: Final = "1"    # 平开模式

# ==================== 状态相关 ====================
STATE_PAIRING: Final = "pairing"
STATE_CONNECTED: Final = "connected"
STATE_DISCONNECTED: Final = "disconnected"
STATE_OPENING: Final = "opening"
STATE_CLOSING: Final = "closing"
STATE_STOPPED: Final = "stopped"
STATE_OPEN: Final = "open"
STATE_CLOSED: Final = "closed"
STATE_UNKNOWN: Final = "unknown"
GATEWAY_STATUS_ONLINE: Final = "online"
GATEWAY_STATUS_OFFLINE: Final = "offline"
GATEWAY_STATUS_PAIRING: Final = "pairing"
PAIRING_STATUS_ACTIVE: Final = "active"
PAIRING_STATUS_INACTIVE: Final = "inactive"

# 子设备状态词汇（统一使用，避免各模块混用 online/offline/connected 等）
DEVICE_STATUS_UNKNOWN: Final = "unknown"        # 未收到任何上报
DEVICE_STATUS_CONNECTED: Final = "connected"    # 已关联/在线
DEVICE_STATUS_OPEN: Final = "open"              # 窗户打开（由 r_travel 推导）
DEVICE_STATUS_CLOSED: Final = "closed"          # 窗户关闭（由 r_travel 推导）
DEVICE_STATUS_ERROR: Final = "error"            # 更新状态时发生异常

# ==================== 错误代码相关 ====================
ERROR_CODE_SUCCESS: Final = 0
ERROR_CODE_BIND_EXISTS: Final = 7

# ==================== 事件相关 ====================
EVENT_DEVICE_DISCOVERED: Final = "window_controller_device_discovered"
EVENT_DEVICE_UPDATED: Final = "window_controller_device_updated"
EVENT_GATEWAY_CONNECTED: Final = "window_controller_gateway_connected"
EVENT_GATEWAY_DISCONNECTED: Final = "window_controller_gateway_disconnected"

# ==================== 命令相关 ====================
COMMAND_OPEN: Final = "open"
COMMAND_CLOSE: Final = "close"
COMMAND_STOP: Final = "stop"
COMMAND_SET_POSITION: Final = "set_position"
COMMAND_A: Final = "a"
COMMAND_PAIR: Final = "pair"
COMMAND_DISCOVER: Final = "discover"
COMMAND_STATUS: Final = "status"
COMMAND_START_PAIRING: Final = "start_pairing"
COMMAND_WIND_LOCK_TILT: Final = "wind_lock_tilt"   # 内倒模式
COMMAND_WIND_LOCK_FLAT: Final = "wind_lock_flat"    # 平开模式
COMMAND_SET_SPEED: Final = "set_speed"              # 开窗速度（rwp_winact_speed）
COMMAND_SET_STRENGTH: Final = "set_strength"        # 开窗力度（rwp_winact_strength）

# ==================== 实体相关 ====================
ENTITY_GATEWAY_PREFIX: Final = "gateway_"
# v1.7.31（C-3）：值从死漂移的 "_pair" 订正为实态 "_pairing" 并接入真实
# 构造点（gateway.py）成单一真源——旧值零消费者且与实现不符，未来按常量
# 写清理必静默 miss（同族 _online 与实现逐字一致，证明本意即单一真源）。
# ⚠️ 此值参与 unique_id 生成：改动=实体迁移事故，两侧现由测试锁死
#    "输出逐字不变"（test_v1731_field_fixes）。
ENTITY_PAIRING_BUTTON_SUFFIX: Final = "_pairing"
ENTITY_ONLINE_SENSOR_SUFFIX: Final = "_online"

# ==================== 时间相关（秒） ====================
SCAN_INTERVAL: Final = 300
GATEWAY_READY_DELAY: Final = 1
GATEWAY_CHECK_INTERVAL: Final = 30
INITIAL_RETRY_DELAY: Final = 5
RESTART_DELAY: Final = 1
GATEWAY_PAIRING_TIMEOUT: Final = 60
GATEWAY_CONNECT_TIMEOUT: Final = 10      # 配置流程中等待网关首次上报的最长时间（秒）
SLIDER_DEBOUNCE_SECONDS: Final = 1       # 速度/力度滑动条防抖：停止拖动 N 秒后才发送命令

# ==================== 设备SN前缀（机型码） ====================
DEVICE_SN_PREFIX_WIND_LOCK: Final = "5005"  # 支持内倒/平开模式的LoRa子设备SN前四位

# v1.7.21（用户 2026-09-10 提供的权威机型矩阵）：SN 前四位 = 机型码。
# 百分比（位置）能力逐机型不同，而 HomeKit 的准入判据是
# covered by supported_features & SET_POSITION（上游 homekit/accessories.py:
# window + SET_POSITION → Window 服务；无该位 → WindowCoveringBasic）：
#   5001 推拉窗            ✓ 百分比
#   5002 平开窗            ✗ 暂不支持（用户："以后有可能支持"）
#   5003 低功耗窗帘        ✓
#   5004 指纹锁            — 非开窗器
#   5005 内开内倒执手电机  ✓（另有内倒/平开模式按钮）
#   5006 平推主机          ✓
#   5007 后装开窗电机      ✓
# 因此 SET_POSITION 只对下表机型声明：5002（及未知前缀）走 HA/Apple 原生
# 三态语义（WindowCoveringBasic：>70 开 / <30 关 / 中间停=暂停），既不会
# 出现"拖了没反应"的假滑块，也不会把无效位指令打到 LoRa 空口上。
# 5002 未来固件支持百分比时：把 "5002" 加进本表即可，无需其他改动。
POSITION_CAPABLE_SN_PREFIXES: Final = frozenset({"5001", "5003", "5005", "5006", "5007"})

# v1.7.21 位置命令合并窗口（机制二：首发立即 + 窗口内只发最终值）。
# 由来：Apple 窗子磁贴拖动期**持续写** TargetPosition（实机日志 34→46→47
# 间隔约 250ms），旧实现每条都直发 004 → 一次拖动十几条报文全压到 LoRa
# 空口（还要等网关逐条 ack）。首发立即发保住 v1.6.9 failfast 契约（未送达
# 仍同步抛错），窗内后续只记 pending，静默 0.5s 后补发最后一条。
POSITION_COALESCE_SECONDS: Final = 0.5

# ==================== 小程序局域网 WS 网关（v1.6.15） ====================
# 复刻固件 app_ws_gateway.c 的 JSON-over-WebSocket 契约，让微信慧尖小程序
# 在局域网直连 HA 主机（ws://<HA-IP>:<port>/ws）。协议常量与固件对齐：
# - 端口 9001（Kconfig WS_GATEWAY_PORT default）
# - 令牌经 Sec-WebSocket-Protocol 子协议头做握手前校验，不匹配拒发 101
# - 默认令牌 = 小程序内置值（weichat-huijian-hz ws-gateway.js 硬编码，
#   与固件 sdkconfig WS_GATEWAY_TOKEN 同值），开箱即可直连
CONF_WS_GATEWAY_ENABLED: Final = "ws_gateway_enabled"
CONF_WS_GATEWAY_PORT: Final = "ws_gateway_port"
CONF_WS_GATEWAY_TOKEN: Final = "ws_gateway_token"
# v1.6.16 用户定案：默认**开**。实证 2026-09-02 小程序日志——mDNS 已发现
# 网关（2022 OPEN）但 9001 Connection refused：默认关使"开箱即可直连"落空，
# 且客户无从知晓还有隐藏开关。对齐固件行为（matter-broker main.cpp：
# 配网完成/WiFi 就绪即 app_ws_gateway_start，无任何用户侧开关——常听）。
# 安全面不变：握手子协议令牌校验（默认令牌=小程序内置共享值）401 拒连、
# 认证成功才占槽（≤4）、帧长与空闲限制照固件；options 仍可显式关闭。
DEFAULT_WS_GATEWAY_ENABLED: Final = True

# v1.6.23：vivo HomeBridge（vivohomebridge）等第三方桥的 cover 枚举
# 只放行 device_class==curtain（vbridge.py:385 源码实证），本集成
# "开窗器"默认 WINDOW（HA 原生语义正确、不动存量用户）；勾选本项
# 后以 CURTAIN 暴露，vivo 智慧生活才能选到该设备
CONF_EXPOSE_COVER_AS_CURTAIN: Final = "expose_cover_as_curtain"
DEFAULT_EXPOSE_COVER_AS_CURTAIN: Final = False
DEFAULT_WS_GATEWAY_PORT: Final = 9001
# v1.7.33（全量审计 G2）：端口升为一等真值源。此前 2022/10998 只以字面量
# 散在 config.yaml、mosquitto.conf、run.sh heredoc、ingress.conf 与本集合的
# 大括号里，没有任何一处能反查一致性——换口时"五处收口"全靠人肉。
MQTT_PORT: Final = 2022            # 内置 Mosquitto（1883 已废弃避让官方加载项）
INGRESS_PORT: Final = 10998        # Web UI nginx ingress（Supervisor 注册口）
# v1.6.19（第六轮审计 B-LOW10）：本栈保留端口——WS 网关端口选项若撞上这些
# 口，bind 失败只进 HA 日志、小程序恒 Connection refused 静默失联，
# config_flow 在源头拒绝。2022=内置 Mosquitto，10998=Web UI nginx ingress，
# 8123=HA core，1883=外部 broker 惯用口。
WS_RESERVED_PORTS: Final = frozenset({MQTT_PORT, INGRESS_PORT, 8123, 1883})
DEFAULT_WS_GATEWAY_TOKEN: Final = "hIZ56jhQ-wzA3ENiP2xGzo55PXsewUWM"

# 交叉引用锚（v1.6.21）：run.sh 的 mqtt_password_is_default 判定与
# config.yaml schema 的 password default 都写死 "huijian2022" 字符串——
# 改默认值必须同步 run.sh（grep huijian2022）与本行，测试钉桩会拦截脱节
DEFAULT_MQTT_PASSWORD: Final = "huijian2022"
WS_GATEWAY_PATH: Final = "/ws"
WS_TOKEN_MIN_LEN: Final = 8            # 固件：新 token 至少 8 字符（防弱 token）
WS_TOKEN_MAX_LEN: Final = 63           # 固件判式 strlen >= sizeof(s_ws_token)-1（63）即拒绝 → 允许 ≤62
WS_TOKEN_CHARSET: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)  # 固件 ws_token_charset_ok：仅 [A-Za-z0-9_-]，RFC6455 子协议安全字符集
WS_MAX_CLIENTS: Final = 4              # 固件 MAX_WS_CLIENTS=4（握手认证成功才占槽）
WS_MAX_FRAME_BYTES: Final = 1024       # 固件 WS_RX_BUF_SIZE：超长→error+断连
WS_RECV_TIMEOUT_SECONDS: Final = 300   # 固件 recv_wait_timeout=300s 空闲断连
# v1.6.17（联审）：小程序 gateway_list 的 online 判定口径。固件以
# GATEWAY_OFFLINE_TIMEOUT_SEC=900（app_mqtt_business.c）把 15 分钟无上报
# 的 LoRa 网关标为离线；插件 HA 内部 1800s 超时是实体可用性口径（改动
# 面大且无必要），WS 视图层单独收紧到 900s 与固件展示语义对齐。
WS_GATEWAY_ONLINE_STALE_SECONDS: Final = 900

# ==================== 其他 ====================
MANUFACTURER: Final = "慧尖"
MODEL: Final = "慧尖开窗器网关"
# 003 绑定/解绑命令方向记录上限：网关不回复/离线时防止 _bind_ops 无限增长
MAX_BIND_OPS: Final = 200
ICON_GATEWAY: Final = "mdi:gateway"
ICON_WINDOW_OPENER: Final = "mdi:window-closed"


def supports_wind_lock_mode(device_sn: str) -> bool:
    """判断设备是否支持内倒/平开模式

    只有SN前四位为5005的LoRa子设备才支持内倒功能，
    5001/5002/5003等设备不支持内倒功能，不创建相关按钮。

    Args:
        device_sn: 设备序列号

    Returns:
        bool: True表示支持内倒/平开模式
    """
    return device_sn[:4] == DEVICE_SN_PREFIX_WIND_LOCK


def get_device_display_name(gateway_sn: str, device_sn: str, device_number: int = None) -> str:
    """统一设备显示名称"""
    short_gw = gateway_sn[-4:]
    short_dev = device_sn[-4:]
    if device_number is not None:
        return f"开窗器 {short_gw}-{short_dev} (#{device_number:02d})"
    return f"开窗器 {short_gw}-{short_dev}"

# ── 慧尖云 hub：安装级单例在 hass.data[DOMAIN] 里的键 ─────────────────
# 放这里而不是 __init__.py，是因为 api.py 也要读同一个键；两处各写一份字面量
# 早晚漂移成"视图永远取不到单例"。
HUB_DATA_KEY = "_hub_client"
HUB_STOP_LISTENER_KEY = "_hub_stop_listener"
# 审计 2026-09-30 C-2：HA 停机闩锁。与 WS_GATEWAY_STOPPED_KEY 同构——STOP 之后
# 条目还会逐个 unload，每条 unload 尾部都会调一次 ensure；没有这把锁就会在关机
# 过程中把刚停掉的长连重新拉起（而它的 STOP 监听注册时事件已派发过 ⇒ 无人回收）。
HUB_STOPPED_KEY = "_hub_stopped"
