"""慧尖小程序局域网 WS 网关（v1.6.15，路线 A）

让微信「慧尖」小程序（weichat-huijian-hz）的「LoRa 网关」入口在局域网
直连 HA：小程序经 mDNS `_mqtt._tcp`（由慧尖加载项广播）发现本机后，固定
拨 `ws://<IP>:<port>/ws` 并只讲 JSON-over-WebSocket——插件此前只有
Mosquitto@2022 与 nginx@8099，"能看到但连不上"即因 9001 无监听者。

本模块在集成内实现与固件 main/app_ws_gateway.c（E:\\AI\\matter-broker）
1:1 对等的 WS-JSON 网关，作为 HA 的"第二张脸"：

  - 命令下行：control 透传 004（mqtt_handler.send_ws_raw_004，逐字节
    等形于固件 app_protocol_bridge_control_device），pair→003 bind=1，
    unbind→003 bind=0（复用集成既有解绑链）；
  - 状态上行：网关/设备视图直接读各 entry 的 mqtt_handler.connected 与
    device_manager.devices 缓存；device_update 推送挂在
    device_manager.add_status_listener（update_device_status 是 002/005
    的唯一漏斗）；
  - 本模块【不】新建第二条 MQTT $SH 桥——ACK、去重、绑定记账仍全部由
    mqtt_handler 单点承担，避免双桥状态分叉。

与固件的有意差异（行为超集，不与小程序现有链路冲突）：
  - set_token 持久化走 config entry options（HA 惯例，reload 生效）而非
    NVS；令牌仅握手时校验——已建立的连接【不会】因 reload/改令牌断开，
    其后续重连才需新令牌。（v1.6.19 注释纠偏 D-F6：旧注释误称"reload 会
    短暂断开现有 WS 连接"，实况是 ensure 同端口走热同步、socket 不断。）

协议契约（固件源码逐行核对）：
  - 握手：客户端以 Sec-WebSocket-Protocol 携带预共享令牌；令牌非空时
    必须精确命中其一（','/' '分隔），否则拒绝握手（不回 101）。
    令牌为空 = 不认证（固件同款语义，含 B16 bootstrap：可在已连接会话
    上 set_token 直接启用认证）。
  - JSON 文本帧，入站载荷 ≤1024B，超限回 {"type":"error",
    "msg":"command too long"} 并断开；非文本帧消费丢弃；空帧忽略。
  - cmd：get_gateways / get_devices / control / pair / unbind / ping /
    set_token；未知 cmd → error "unknown command: <cmd>"；缺 cmd →
    error "missing cmd"。
  - type：gateway_list / device_list / control_ack / pair_ack /
    unbind_ack / pong / set_token_ack / error；主动推送 device_update
    （含 windLockMode；-1 = 未知约定）。
  - 会话：并发 ≤4（认证成功才占槽，固件 M4 定式）；空闲 300s 断连
    （小程序 60s get_gateways 心跳保活）。
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from aiohttp import WSMsgType, web

from homeassistant.core import HomeAssistant

from .const import (
    CONF_WS_GATEWAY_ENABLED,
    CONF_WS_GATEWAY_PORT,
    CONF_WS_GATEWAY_TOKEN,
    CONTROL_ATTR_DOMAINS,
    DEFAULT_WS_GATEWAY_ENABLED,
    DEFAULT_WS_GATEWAY_PORT,
    DEFAULT_WS_GATEWAY_TOKEN,
    DEVICE_TO_GATEWAY_MAPPING,
    DOMAIN,
    CONF_GATEWAY_SN,
    GATEWAY_READY_DELAY,
    WS_GATEWAY_ONLINE_STALE_SECONDS,
    WS_GATEWAY_PATH,
    WS_MAX_CLIENTS,
    WS_MAX_FRAME_BYTES,
    WS_RECV_TIMEOUT_SECONDS,
    WS_RESERVED_PORTS,
    WS_TOKEN_CHARSET,
    WS_TOKEN_MAX_LEN,
    WS_TOKEN_MIN_LEN,
)
from .utils import log_throttled

_LOGGER = logging.getLogger(__name__)

# 固件线值格式：只放行十进制整数/小数。本协议四个出站属性的域全是**整数**，所以
# 格式闸之后还要过 _INT_VALUE_RE 与域表（v1.7.65）。
# 历史上云侧 hub_client 自己抄了一份同串的模式，靠注释"必须逐字同串"维系——
# v1.7.65 起云侧直接委托本模块（validate_control_params → 本文件），两条通道同一实现。
_VALUE_RE = re.compile(r"-?\d+(\.\d+)?")
# 命令值必须是整数形态（上报侧才有小数，如 r_travel 行程分数；下行四个属性都是整数域）
_INT_VALUE_RE = re.compile(r"-?\d+")


def validate_control_command(attribute: Any, value: Any) -> Optional[str]:
    """004 control 的唯一判据（WS 与云共用）。返回可下发的线值字符串，不合格返回 None。

    **返回的是入参原样字符串，不做归一化**：`"0100"` 仍按 `"0100"` 透传，
    与既有"值原样进 004"的语义逐字节一致——本函数只新增拒绝，不重写已通过的值。

    拒绝次序（每条都是真实缺陷，不是防御性装饰）：
      1. attribute 不在域表全集内 → 拒。此前任意非空字符串都放行，而本仓
         `send_ws_raw_004` 的 docstring 自陈"不做语义解释…本方法同样不校验"、
         control_ack 的 ok 又是**发布级**语义 ⇒ 属性名打错一个字母照样发布并回
         **ok:true 假成功**，界面显示"速度已设置"而设备从没收到过这条命令。
      2. 空串 / bool / 非 str-int-float → 拒（v1.7.12 F6：dict/list 经 str() 出 Python repr）。
      3. 不过十进制格式 → 拒（v1.7.18 BUG-16；v1.7.52 取消 str 豁免，'NaN' 就是从那道缝穿的）。
      4. 非整数形态 → 拒（四个属性的值域都是整数：const 的 SPEED_MIN/MAX 与
         COMMAND_VALUE_* 全整数，number 实体 step=1——小数只出现在上报侧）。
      5. 不在该属性值域内 → 拒。范围此前**只有小程序一侧在夹**（pctParam 0..100），
         任何绕开小程序直连 WS 的调用方送 speed=150 会一路发布出去。
    """
    if not isinstance(attribute, str) or attribute not in CONTROL_ATTR_DOMAINS:
        return None
    if value is None or value == "" or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    value_s = str(value)
    if not _VALUE_RE.fullmatch(value_s) or not _INT_VALUE_RE.fullmatch(value_s):
        return None
    lo, hi, extra = CONTROL_ATTR_DOMAINS[attribute]
    n = int(value_s)
    if n in extra or lo <= n <= hi:
        return value_s
    return None


# 小程序 WS 服务器在 hass.data[DOMAIN] 中的单例键（跨 config entry 共享：
# 一台 HA 只监听一个端口，网关/设备视图聚合全部已完成设置的 entry）
WS_GATEWAY_DATA_KEY = "_ws_gateway"
# HA STOP 闩锁：关机序列中 entry 逐个 unload 不得把服务器重新拉起
WS_GATEWAY_STOPPED_KEY = "_ws_gateway_stopped"
# HA STOP 监听句柄（v1.7.33：单例注册，防"停旧起新"路径重复登记回调）
WS_GATEWAY_STOP_LISTENER_KEY = "_ws_gateway_stop_unsub"

# set_token 校验消息——与固件 app_ws_gateway.c 逐字对齐（小程序侧按 msg 提示）
_MSG_MISSING_NEW = "missing newToken"
_MSG_TOO_SHORT = "newToken too short (min 8)"
_MSG_TOO_LONG = "newToken too long"
_MSG_BAD_CHARS = "newToken invalid chars (A-Za-z0-9_- only)"
_MSG_OLD_MISMATCH = "old token mismatch"


# ==================== 纯函数（握手/校验/视图——单测直接驱动） ====================

def offered_subprotocols(header_value: Optional[str]) -> List[str]:
    """解析 Sec-WebSocket-Protocol 头为候选列表。

    固件以 strtok_r(", ") 拆分（',' 与 ' ' 任一均为分隔符），逐字对齐：
    只认 ',' 与 ' ' 两种分隔符。

    v1.7.33（全量审计）：旧实现 `.replace(",", " ").split()` 按**任意空白**
    拆分（\\t \\n \\v \\f 皆断），比固件宽一档，也比 aiohttp 自身协商宽
    （aiohttp 按 ',' 拆再 strip）——出现"插件判命中放行、aiohttp 不回显
    子协议"的静默分歧。收敛为 [ ,]+ 切分。
    """
    if not header_value:
        return []
    return [p for p in re.split(r"[ ,]+", header_value.strip()) if p]


def handshake_token_ok(header_value: Optional[str], token: str) -> bool:
    """握手令牌校验：令牌为空 = 不认证放行；否则候选中须有精确匹配。

    v1.7.33（全量审计）：逐候选改 hmac.compare_digest——`token in list` 的
    字符串相等短路比较逐字节提前返回，配合"101/401 预言机"构成时序侧信道
    （同网段可在线枚举预共享令牌）。
    """
    if not token:
        return True
    # 审计 2026-09-30 A-1：hmac.compare_digest 对含非 ASCII 的 str **直接抛
    # TypeError**（实测 C 解析器与 AIOHTTP_NO_EXTENSIONS 纯 Python 两臂均可达：
    # aiohttp 只拦控制字符，非 ASCII 以 utf-8/surrogateescape 原样交付）。
    # 本调用点无 try（try 从 _handle_ws 的预约计数之后才开始）⇒ 未认证方一发
    # `Sec-WebSocket-Protocol: <非ASCII>,<正确令牌>` 就把契约规定的 401 变成
    # 500 + HA ERROR 栈，且"正确令牌排在非 ASCII 候选之后"的合法客户端一起被拒。
    # 令牌字符集是 [A-Za-z0-9_-]（token_charset_ok），非 ASCII 候选**永远不可能
    # 匹配**，先筛掉再逐候选恒定时间比较：既不抛错，也不引入时序面（筛选只读
    # 攻击者自带的候选，不读密钥内容）。token 自身非 ASCII 属存储被手改的畸形
    # 形态——此时任何客户端都配不上，如实回 401 而不是崩。
    if not token.isascii():
        return False
    return any(hmac.compare_digest(cand, token)
               for cand in offered_subprotocols(header_value)
               if cand.isascii())


def token_charset_ok(token: str) -> bool:
    """固件 ws_token_charset_ok：仅 [A-Za-z0-9_-]（RFC6455 子协议安全字符集）。"""
    return all(c in WS_TOKEN_CHARSET for c in token)


def validate_new_token(new_token: Any, old_token: Any, current_token: str) -> Optional[str]:
    """set_token 校验链，返回错误消息（None = 通过）。固件顺序 1:1：
    missing → min 长度 → max 长度 → 字符集 → （认证启用时）oldToken 匹配。
    认证未启用（current 为空）走 B16 bootstrap：跳过 oldToken 匹配。
    """
    if not isinstance(new_token, str) or new_token == "":
        return _MSG_MISSING_NEW
    if len(new_token) < WS_TOKEN_MIN_LEN:
        return _MSG_TOO_SHORT
    if len(new_token) >= WS_TOKEN_MAX_LEN:
        return _MSG_TOO_LONG
    if not token_charset_ok(new_token):
        return _MSG_BAD_CHARS
    auth_active = bool(current_token)
    # 审计 2026-09-30 A-1 同族第二站点：oldToken 是 WS 帧里的外部输入，
    # 非 ASCII 时 compare_digest 抛 TypeError（newToken 侧上方已有字符集闸，
    # 本行此前只判 isinstance str）。两侧任一带非 ASCII 都配不上（令牌字符集
    # 是 [A-Za-z0-9_-]），如实回 _MSG_OLD_MISMATCH，而不是崩掉整条会话
    # （协议要求每个 cmd 必有回包；崩了小程序只会干等）。
    if auth_active and (not isinstance(old_token, str)
                        or not old_token.isascii()
                        or not current_token.isascii()
                        or not hmac.compare_digest(old_token, current_token)):
        return _MSG_OLD_MISMATCH
    return None


def _as_int(value: Any) -> int:
    """固件 -1=未知约定的安全取整：不可解析/None → -1。

    v1.6.19（第六轮审计 A-HIGH2）：补捕 OverflowError——JSON `1e999` 解析为
    float("inf") 不抛错而 `int(inf)` 抛 OverflowError，ValueError/TypeError
    元组接不住，一个非有限数就能炸穿视图构造。"""
    if value is None or isinstance(value, bool):
        return -1
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return -1


def device_ws_view(device_sn: str, gateway_sn: str, device: Dict[str, Any]) -> Dict[str, Any]:
    """由 device_manager 设备缓存条目构造小程序视图。

    字段拼写/类型与固件 get_devices_json + notify_device_update 对齐：
    position=r_travel（-1 未知）；battery=电压×10 的原始值（固件定式
    "HA 集成上报值 ×10 = 毫伏，/100 前即 voltage_mv"，等价 raw），
    -1 未知；state 按 r_travel==0→0 / >0→1 推导（与集成 DEVICE_STATUS
    推导同源），无 r_travel 时 -1。

    v1.6.17（联审 契约F1/F2）：入界校验与固件同口径，垃圾值不得以
    合法数字形态进入小程序——固件 position/r_travel 仅取 0..100，
    255 等越界值是"未校准/离线标记"直接丢弃
    （app_protocol_bridge.cpp:2133、2781 P3-2），电池 raw 仅接受
    [80,140]（12V 锂电 9.5-12.6V 放宽到 8-14V，防 uint16 溢出/异常
    显示，BATTERY_RAW_MIN/MAX :743-744）。插件缓存为保留 HA 侧
    "未校准"文案语义不裁剪，钳制统一收敛在本视图层。
    """
    attrs = device.get("attributes") or {}
    position = attrs.get("r_travel")
    voltage = attrs.get("voltage")
    position_i = _as_int(position)
    if not 0 <= position_i <= 100:
        position_i = -1
    battery = -1
    if voltage is not None:
        try:
            fv = float(voltage)
            # v1.6.19（第六轮审计 A-HIGH2）：非有限数先行判死——round(inf)
            # 抛 OverflowError（nan 抛 ValueError），若只捕 ValueError/
            # TypeError，inf 电压会炸掉 _device_list_payload 的跨 entry 单趟
            # 遍历：一台坏设备 → 全部网关全部设备在小程序集体消失（命令
            # 异常被 _session 吞掉、get_devices 无回包直到心跳覆盖坏值）。
            if not math.isfinite(fv):
                raise ValueError("voltage 非有限数")
            battery = int(round(fv * 10))
        except (ValueError, TypeError, OverflowError):
            battery = -1
        if not 80 <= battery <= 140:
            # 固件同款范围判定：可解析但越界（0、负值、24.0V 假象的
            # 240 等）一律丢弃为未知
            battery = -1
    # state 从钳制后的 position 推导：r_travel=255（未校准标记）在
    # 固件视图里 state=-1，插件必须一致，不得报"已开"
    state = 0 if position_i == 0 else (1 if position_i > 0 else -1)
    # 速度/力度/锁定模式回显（v1.7.47）：设备上报的 rwp_winact_* 由 _ctypes 解析进
    # attributes["winact_speed"/"winact_strength"]，而小程序此前**只能发不能收**——滑块初值
    # 来自本地存储（默认 60/50），换手机、清缓存、或在 HA 里调过，小程序显示的就与设备
    # 实际值对不上。（HA 侧滑块刻意只显示 setpoint，见 number._update_state。）
    # 入界纪律与 position 同款：越界/不可解析一律 -1（未知），不把垃圾值当合法数字发出去。
    speed = _as_int(attrs.get("winact_speed"))
    if not 0 <= speed <= 100:
        speed = -1
    strength = _as_int(attrs.get("winact_strength"))
    if not 0 <= strength <= 100:
        strength = -1
    return {
        "sn": device_sn,
        "gwSn": gateway_sn,
        "position": position_i,
        "battery": battery,
        "state": state,
        "windLockMode": _as_int(attrs.get("wind_lock_mode")),
        "winactSpeed": speed,
        "winactStrength": strength,
    }


# ==================== 服务器 ====================

class WsGatewayServer:
    """慧尖小程序 WS-JSON 网关服务器（aiohttp，HA 事件循环内运行）。

    生命周期由 async_ensure_ws_gateway 管理：任一 entry 未显式关闭
    ws_gateway_enabled 即启动（v1.6.16 默认开，对齐固件常听语义；
    单例）；全部显式关闭/卸载即停止。命令处理
    每次实时遍历 hass.data[DOMAIN] 取当前 entry 集合，entry 增删无需
    重启服务器。
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        host: str = "0.0.0.0",
        port: int,
        token: str,
    ) -> None:
        self.hass = hass
        self.host = host
        self.port = port
        # 运行时令牌。set_token 成功后：本连接立即生效（内存），并异步
        # 持久化到主控 entry options（latest-wins）。options 重启时回灌。
        self._token = token
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._clients: set = set()
        self._listener: Optional[Callable[[str, str], None]] = None
        self._listener_managers: List[Any] = []
        self._stopping = False
        # 审计 2026-09-30 C-3：首路 async_stop 的收口信号（懒建，第二次调用才需要）。
        self._stop_done: Optional[asyncio.Event] = None
        # 在途握手计数（见 _handle_ws 槽位预约注释）
        self._pending_handshakes = 0
        # v1.6.19（第六轮审计 A-LOW6）：广播任务引用集合——裸 create_task
        # 的任务无持有者，GC 时机不定时报 "Task was destroyed but it is
        # pending!"；统一登记并在 async_stop 时取消。
        self._bg_tasks: set = set()

    # ---------- 生命周期 ----------

    async def async_start(self) -> None:
        """绑定 TCP 并启动。失败抛异常（由 ensure 侧捕获记日志）。"""
        app = web.Application()
        app.router.add_get(WS_GATEWAY_PATH, self._handle_ws)
        # access_log=None：默认 aiohttp.access 会对每次 WS 升级请求打
        # INFO 行，小程序重连风暴（如 Wi-Fi 切换）会刷屏 HA 日志——
        # 本模块已有中文连接/断开日志（_handle_ws），无访问日志需求
        runner = web.AppRunner(app, handle_signals=False, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except OSError:
            await runner.cleanup()
            raise
        self._app = app
        self._runner = runner
        _LOGGER.warning(
            "小程序 WS 网关已启动: ws://%s:%d%s（局域网直连，令牌%s）",
            self.host, self.port, WS_GATEWAY_PATH,
            "已启用" if self._token else "未启用=无认证",
        )

    async def async_stop(self) -> None:
        """注销设备监听、踢下全部客户端并释放端口（幂等）。"""
        # 审计 2026-09-30 C-3：旧写法第二路见 `_stopping` 为真就**立刻返回**，
        # 而首路此刻还卡在客户端 close（最坏 ~10s）或 runner.cleanup 上——端口
        # 并未释放。并发 ensure 因此马上新建实例并 async_start ⇒ OSError 端口仍被占，
        # 而它只等 1.0s 重试一次（:988-1002，短于首路收尾窗口）⇒ 起不来，只留一行
        # "下次条目变更会再补拉"，改端口/改令牌冻结成永不监听。
        # 改为：第二路等首路真正收口后再返回（幂等语义不变，只是不再假称"已停好"）。
        if self._stopping:
            done = self._stop_done
            if done is not None:
                await done.wait()
            return
        self._stopping = True
        self._stop_done = asyncio.Event()
        try:
            self._detach_listeners()
            clients = list(self._clients)
            self._clients.clear()
            if clients:
                # v1.7.12（第 6 轮审计 F3）：逐个 await ws.close()（对端不应答
                # 时默认 10s 超时各拖各的）改并发 gather——4 客户端串行最坏 40s
                # 卡死条目 unload/HA STOP 路径；return_exceptions 容忍单条异常
                # （与旧逐条 except pass 同语义）
                await asyncio.gather(
                    *(ws.close() for ws in clients), return_exceptions=True
                )
            # v1.6.19 A-LOW6：客户端已踢、监听已摘，残余广播任务无意义，取消收尾
            for _t in list(self._bg_tasks):
                _t.cancel()
            self._bg_tasks.clear()
            if self._runner is not None:
                try:
                    await self._runner.cleanup()
                except Exception as e:  # noqa: BLE001
                    _LOGGER.debug("WS 网关 runner cleanup 异常: %s", e)
                self._runner = None
            _LOGGER.info("小程序 WS 网关已停止")
        finally:
            # 端口在这里才算真的释放完——唤醒等待方必须晚于 cleanup
            self._stop_done.set()

    def _attach_listeners(self, managers: List[Any]) -> None:
        """把 device_update 推送监听挂到全部网关的 device_manager。"""
        self._detach_listeners()
        self._listener = self._on_device_status
        for dm in managers:
            try:
                dm.add_status_listener(self._listener)
                self._listener_managers.append(dm)
            except Exception as e:  # noqa: BLE001
                _LOGGER.error("挂载设备状态监听失败: %s", e)

    def _detach_listeners(self) -> None:
        if self._listener is None:
            return
        for dm in self._listener_managers:
            try:
                dm.remove_status_listener(self._listener)
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("摘除设备状态监听失败: %s", e)
        self._listener_managers = []
        self._listener = None

    # ---------- 运行期数据视图（实时遍历，不缓存引用） ----------

    def _entries_data(self) -> List[Tuple[str, Dict[str, Any]]]:
        """全部已完成设置网关的 (gateway_sn, entry_data) 快照。"""
        out: List[Tuple[str, Dict[str, Any]]] = []
        domain_data = self.hass.data.get(DOMAIN, {})
        for entry_id, data in domain_data.items():
            if not isinstance(entry_id, str) or not isinstance(data, dict):
                continue
            if not data.get("_setup_complete"):
                continue
            gw = data.get("gateway_sn")
            handler = data.get("mqtt_handler")
            manager = data.get("device_manager")
            if gw and handler is not None and manager is not None:
                out.append((gw, data))
        return out

    def _find_entry(self, gateway_sn: str) -> Optional[Dict[str, Any]]:
        want = (gateway_sn or "").lower()
        for gw, data in self._entries_data():
            if gw.lower() == want:
                return data
        return None

    def _device_gateway(self, device_sn: str) -> Optional[Dict[str, Any]]:
        """设备→网关路由：先查全局映射表（忽略大小写），再查各网关缓存。"""
        mapping = self.hass.data.get(DOMAIN, {}).get(DEVICE_TO_GATEWAY_MAPPING, {})
        mapped = None
        for dev_key, gw_key in mapping.items():
            if str(dev_key).lower() == device_sn.lower():
                mapped = gw_key
                break
        if mapped:
            data = self._find_entry(str(mapped))
            if data is not None:
                return data
        for _gw, data in self._entries_data():
            if device_sn in data["device_manager"].devices:
                return data
        return None

    def _gateway_list_payload(self) -> Dict[str, Any]:
        # online 口径 = mqtt_handler.connected（1800s 超时）∧ 最近
        # WS_GATEWAY_ONLINE_STALE_SECONDS(900s) 内有真实上报——固件对
        # 静默 15 分钟的网关即显示离线，插件不得比固件"更乐观"。
        # last_gateway_report_time 是 monotonic 时钟（收报即刷新，含
        # 001/002 心跳），None = 从未收报。
        now = time.monotonic()
        gateways = []
        for gw, data in self._entries_data():
            handler = data["mqtt_handler"]
            fresh = (
                handler.last_gateway_report_time is not None
                and now - handler.last_gateway_report_time < WS_GATEWAY_ONLINE_STALE_SECONDS
            )
            gateways.append({"sn": gw, "online": bool(handler.connected and fresh)})
        return {"type": "gateway_list", "gateways": gateways}

    def _device_list_payload(self) -> Dict[str, Any]:
        devices: List[Dict[str, Any]] = []
        for gw, data in self._entries_data():
            manager = data["device_manager"]
            for dev_sn, dev in manager.devices.items():
                devices.append(device_ws_view(dev_sn, gw, dev))
        return {"type": "device_list", "devices": devices}

    # ---------- 命令分发（纯 JSON 进出，便于单测直接驱动） ----------

    async def handle_json_message(self, text: str) -> Optional[Dict[str, Any]]:
        """处理一条入站 JSON 文本，返回应回发的 dict（None = 静默）。"""
        try:
            msg = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"type": "error", "msg": "missing cmd"}
        if not isinstance(msg, dict):
            return {"type": "error", "msg": "missing cmd"}
        cmd = msg.get("cmd")
        if not isinstance(cmd, str) or not cmd:
            return {"type": "error", "msg": "missing cmd"}
        if cmd == "get_gateways":
            return self._gateway_list_payload()
        if cmd == "get_devices":
            return self._device_list_payload()
        if cmd == "control":
            return await self._cmd_control(msg)
        if cmd == "pair":
            return await self._cmd_pair(msg)
        if cmd == "unbind":
            return await self._cmd_unbind(msg)
        if cmd == "ping":
            return {"type": "pong"}
        if cmd == "set_token":
            return await self._cmd_set_token(msg)
        return {"type": "error", "msg": f"unknown command: {cmd}"}

    async def _cmd_control(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """固件 control：四字段（gwSn/devSn/attribute/value）均须非空，
        value 一律转字符串透传 004；路由 = 设备映射网关，缺失时广播到
        全部在线网关（send_004_command_all_gateways 定式）。

        回执带 `attribute` + `cmdsn` 两个**关联字段**（见内部 ack）：小程序的在途命令
        配对靠它们——此前 LAN 回执两者都没有，页面只能退化成"认最早那条在途"的弱 FIFO，
        「打开」的回执会被当成速度命令的回执（误保存/误回退滑块）。
        """
        gw_sn = msg.get("gwSn")
        dev_sn = msg.get("devSn")
        attribute = msg.get("attribute")
        value = msg.get("value")
        cmdsn = msg.get("cmdsn")

        def ack(ok: bool, text: str) -> Dict[str, Any]:
            """所有 control_ack 的唯一出口：关联字段在一处统一，别在七个 return 点各抄一份。

            **additive、向后兼容**：老小程序既不发 cmdsn 也不读这两个键 ⇒ 它收到的回执
            与既往逐字节同形。attribute 只在已是非空 str 时回带（"missing fields" 那条
            本来就没有这个信息）；cmdsn 原样回带，不校验、不改写。
            """
            out: Dict[str, Any] = {"type": "control_ack", "ok": ok, "msg": text}
            if isinstance(attribute, str) and attribute:
                out["attribute"] = attribute
            if isinstance(cmdsn, str) and cmdsn:
                out["cmdsn"] = cmdsn
            return out

        if not all(isinstance(x, str) and x for x in (gw_sn, dev_sn, attribute)) or value is None:
            return ack(False, "missing fields")
        # v1.6.17（联审）：固件把"空字符串 value"按缺失字段拒绝，bool 经
        # str() 会变成 "True"/"False"（固件解析出的是 'true'/'false' 字面量，
        # 设备端两者都不是合法命令值）——同口径拒绝，不透传脏值
        if value == "" or isinstance(value, bool):
            return ack(False, "missing fields")
        # v1.7.12（第 6 轮审计 F6）＋ v1.7.18（BUG-16）＋ v1.7.52（取消 str 豁免）
        # 三道闸的判据自 v1.7.65 起收敛进 validate_control_command（WS 与云同一实现），
        # 失败文案仍逐字沿用 'invalid value'——小程序 LAN_ACK_TEXT 已把它映射成中文，
        # 换码会让正要上线的那版小程序看到裸兜底。
        # 关于"范围校验仍留在客户端与固件（不把范围逻辑在三处各写一份）"这条旧定案：
        # 本批没有复制第三份，而是把域表放进 const.py 由两条通道共读一份 ⇒ 三处变一处，
        # 同时补上两条此前无人拦的路：属性名打错照样 ok:true 的假成功面、以及
        # 直连 WS 的调用方送 speed=150 一路透传。
        value_s = validate_control_command(attribute, value)
        if value_s is None:
            return ack(False, "invalid value")
        data = self._device_gateway(dev_sn)
        if data is not None:
            ok = await data["mqtt_handler"].send_ws_raw_004(dev_sn, attribute, value_s)
            return ack(bool(ok), "ok" if ok else "send failed")
        # 映射缺失 → 广播。v1.6.17（联审）：固件定式是"无条件向全部
        # 网关发布"（app_protocol_bridge.cpp :1628-1650 的 P2 修复，不查
        # 网关在线状态）；插件此前跳过 connected=False（=1800s 无上报，
        # 与 MQTT 链路真实状态本就不是同一件事）的网关，属行为分歧。
        # 改为对全部已注册条目发布，返回语义与固件一致 = 发布级成败；
        # 仅 broker 发布失败（send_ws_raw_004 返回 False）才算 send failed。
        entries = self._entries_data()
        if not entries:
            # v1.7.33（全量审计）：零条目时旧实现循环空转、仍回 ok:true 的
            # 假成功（小程序显示已下发、设备无动作）。control 是执行语义，
            # 无对象可发必须如实失败——与 _cmd_pair 的"广播后恒 ok"定案
            # （固件 P2 广播语义）不同，后者不属此列。
            return ack(False, "no gateway registered")
        publish_failed = False
        for _gw, edata in entries:
            if not await edata["mqtt_handler"].send_ws_raw_004(dev_sn, attribute, value_s):
                publish_failed = True
        if publish_failed:
            return ack(False, "send failed")
        return ack(True, "ok")

    async def _cmd_pair(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """固件 pair：gwSn 指定时须已注册且在线（L5/M6 如实 ack）；
        不带 gwSn 广播给全部已注册网关后恒 ok。"""
        gw_sn = msg.get("gwSn")
        if isinstance(gw_sn, str) and gw_sn:
            data = self._find_entry(gw_sn)
            handler = data["mqtt_handler"] if data else None
            if handler is None or not handler.connected:
                return {"type": "pair_ack", "ok": False,
                        "msg": "gateway offline or not registered"}
            ok = await handler.send_command(handler.gateway_sn, "start_pairing")
            if ok:
                return {"type": "pair_ack", "ok": True}
            return {"type": "pair_ack", "ok": False,
                    "msg": "gateway offline or not registered"}
        entries = self._entries_data()
        if not entries:
            # v1.7.33（全量审计）：空集上的"广播成功"与 control 同属假成功，
            # 如实拒绝；条目存在时仍按固件 P2 广播语义恒 ok（下方不动）。
            return {"type": "pair_ack", "ok": False,
                    "msg": "no gateway registered"}
        for _gw, edata in entries:
            handler = edata["mqtt_handler"]
            try:
                await handler.send_command(handler.gateway_sn, "start_pairing")
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("pair 广播下发失败（gw=%s）: %s", _gw, e)
        return {"type": "pair_ack", "ok": True}

    async def _cmd_unbind(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """固件 unbind：gwSn+devSn 必填；网关须已注册且在线、设备须在该
        网关缓存（M6/L3 如实 ack）；下发复用集成既有 003 bind=0 链。"""
        gw_sn = msg.get("gwSn")
        dev_sn = msg.get("devSn")
        if not (isinstance(gw_sn, str) and gw_sn and isinstance(dev_sn, str) and dev_sn):
            return {"type": "unbind_ack", "ok": False, "msg": "missing gwSn or devSn"}
        data = self._find_entry(gw_sn)
        handler = data["mqtt_handler"] if data else None
        manager = data["device_manager"] if data else None
        known = bool(manager is not None and dev_sn in manager.devices)
        if handler is None or not handler.connected or not known:
            return {"type": "unbind_ack", "ok": False, "msg": "gateway offline or device unknown"}
        try:
            await handler.unbind_device(dev_sn)
        except Exception:  # noqa: BLE001 - broker 发布失败如实 ack，不谎报 ok
            _LOGGER.warning("WS unbind 发布 003 失败: %s", dev_sn, exc_info=True)
            return {"type": "unbind_ack", "ok": False, "msg": "send failed"}
        # v1.6.17（联审 F1，幽灵设备）：固件解绑确认路径会删除本地记录；
        # 插件的本地删除由「设备→删除」按钮流程负责，003 解绑确认分支
        # 明确注释"本地删除已由删除按钮流程完成"——但 WS 通道不经过按钮！
        # 不在此闭环，则设备永远留在缓存/注册表/映射里，下次 get_devices
        # 原样复活。此处完整镜像按钮流程（gateway.py async_press）：
        # 解绑命令已发布 → 等网关处理 → remove_device 本地删除（登记手动
        # 删除列表，防止 002 自动发现把它复活）。
        await asyncio.sleep(GATEWAY_READY_DELAY)
        # v1.6.19（第六轮审计 A-MED2）：sleep 是让出点——期间该 entry 可能
        # 被 reload/删除（另一会话 set_token 持久化即触发 reload），旧
        # manager 在 unload 时被 cleanup 清空 devices，拿旧引用 remove_device
        # 会整体 no-op（不登记手动删除名单、不清映射），新 manager 又按映射
        # 回填设备 → 幽灵设备复活，恰是 F1 要消灭的形态。必须重解析。
        data2 = self._find_entry(gw_sn)
        manager2 = data2["device_manager"] if data2 else None
        if manager2 is None:
            # v1.7.31（B-1）：区分"真删除"与"reload 途中"两种 None——
            # reload 让出窗口命中旧分支时，条目重建后新 manager 按**残留
            # 映射**回填设备（remove_device 未执行→未登记手动删除名单），
            # 幽灵复活+小程序已收假成功 ack，恰是 F1/A-MED2 注释自认的
            # 现实分支却只回滚了一半语义。条目仍在列表=reload 进行中，
            # 如实 ack False；条目已从列表消失=删除随条目收口，ack True。
            matched = [
                e for e in self.hass.config_entries.async_entries(DOMAIN)
                if str((getattr(e, "data", None) or {}).get(CONF_GATEWAY_SN) or "")
                .lower() == gw_sn.lower()
            ]
            # 审计 2026-09-30 E-2 的**反向**结论：本处**不**按 disabled_by 过滤。
            # 这里问的不是"是不是有效配置"，而是"条目还在不不在配置列表里"——
            # 若照单一真源口径滤掉禁用条目，禁用会被读成"已删除"并 ack ok:True，
            # 而设备仍在注册表里（小程序收到假成功，下次 get_devices 又冒出来）。
            # 第三态如实分开：条目在但被禁用 ⇒ 既非 reload 也非删除，重试无用。
            disabled = [e for e in matched if getattr(e, "disabled_by", None)]
            if matched and len(disabled) == len(matched):
                _LOGGER.warning("WS unbind: 条目 %s 已被用户禁用，本地删除未落——"
                                "请在 HA 中重新启用或删除该集成条目后再解绑", gw_sn)
                return {"type": "unbind_ack", "ok": False, "msg": "entry disabled"}
            still_configured = bool(matched)
            if still_configured:
                _LOGGER.warning("WS unbind: 条目 %s 正处 reload，本地删除未落——"
                                "请稍后重试解绑（设备暂在列表中属如实状态）", gw_sn)
                return {"type": "unbind_ack", "ok": False,
                        "msg": "entry reloading, retry later"}
            # 条目已整体删除：设备随条目消失，结果等价于删除成功
            _LOGGER.info("WS unbind: 条目 %s 已删除，本地删除随条目完成", gw_sn)
            return {"type": "unbind_ack", "ok": True}
        try:
            await manager2.remove_device(dev_sn)
        except Exception:  # noqa: BLE001
            # 本地删除失败不再谎报 ok（v1.6.17 修复自己立的"如实 ack"原则，
            # 此前唯独这一处例外）：设备下次 get_devices 仍会出现，小程序
            # 需要真实结果来提示用户。
            _LOGGER.error("WS unbind 本地删除设备失败: %s", dev_sn, exc_info=True)
            return {"type": "unbind_ack", "ok": False, "msg": "local delete failed"}
        return {"type": "unbind_ack", "ok": True}

    async def _cmd_set_token(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """固件 set_token：校验链 1:1；成功后内存即时生效 + 异步持久化
        到主控 entry options（reload 生效，当前连接保持）。"""
        err = validate_new_token(msg.get("newToken"), msg.get("oldToken"), self._token)
        if err:
            _LOGGER.warning("set_token 被拒绝: %s", err)
            return {"type": "set_token_ack", "ok": False, "msg": err}
        new_token = msg["newToken"]
        auth_was_active = bool(self._token)
        prev_token = self._token
        self._token = new_token
        if not auth_was_active:
            _LOGGER.warning("WS 网关令牌从未启用状态被引导开启（固件 B16 bootstrap 同款）")
        self.hass.async_create_task(self._persist_token(new_token, prev_token))
        return {"type": "set_token_ack", "ok": True, "msg": "token updated"}

    async def _persist_token(self, new_token: str, prev_token: str) -> None:
        """把新令牌写入主控 entry options。三种结局都保证"内存==持久化"：
        ①写入/命中成功 → 回灌内存（A-MED3：本任务排队期间 ensure 热同步可能
        用旧 options 覆写内存，成功路径必须把内存夺回来）；
        ②抛异常 → 回滚内存（联审 F6 定案，固件 NVS 写失败同款语义）；
        ③一个可写 enabled 条目都没命中 → 同样回滚（v1.6.19 D-F3：旧写法
        循环空转正常结束不回滚，"小程序已存新令牌、HA 重启回退旧令牌"的
        永久 401 漂移从这条路漏出去）。

        v1.7.33（全量审计）：旧实现只写**第一个** enabled 条目即 break，
        而 ws_gateway_wanted 的运行态取值同样只看第一个 enabled 条目——
        删掉/禁用承载令牌的那个条目后，下一个条目的 options 仍是默认令牌，
        重启静默回退、小程序侧永久 401（正是 docstring 自称已封堵的
        "内存==持久化"之外的第四种结局）。改为写入**全部** enabled 条目。
        """
        wrote = False
        try:
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                # 审计 2026-09-30 E-2 同族：被禁用的条目不是"有效配置"，把新令牌
                # 写进它的 options 既不会被运行态采纳（ws_gateway_wanted 已跳过），
                # 又会让"全部条目禁用"那一路误判成 wrote=True 而不回滚内存值。
                if getattr(entry, "disabled_by", None):
                    continue
                options = dict(entry.options or {})
                if not options.get(CONF_WS_GATEWAY_ENABLED, DEFAULT_WS_GATEWAY_ENABLED):
                    continue
                if options.get(CONF_WS_GATEWAY_TOKEN, DEFAULT_WS_GATEWAY_TOKEN) != new_token:
                    options[CONF_WS_GATEWAY_TOKEN] = new_token
                    self.hass.config_entries.async_update_entry(entry, options=options)
                    _LOGGER.info("WS 网关令牌已持久化到集成选项（条目 %s）", entry.entry_id)
                wrote = True
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("WS 令牌持久化失败，回滚为旧令牌（与固件 NVS 写失败语义一致）: %s", e)
            if self._token == new_token:
                self._token = prev_token
            return
        if not wrote:
            _LOGGER.error("WS 网关无任何 enabled 条目可持久化令牌，回滚内存值（防重启回退漂移）")
            if self._token == new_token:
                self._token = prev_token
            return
        # A-MED3 写后回灌：确保内存令牌与本任务落盘的 options 一致
        self._token = new_token

    # ---------- 主动推送 ----------

    def _on_device_status(self, gateway_sn: str, device_sn: str) -> None:
        """device_manager 状态监听器（同步、事件循环内被调）。"""
        if self._stopping or not self._clients:
            return
        payload = self._device_update_payload(gateway_sn, device_sn)
        if payload is None:
            return
        # v1.6.19（第六轮审计 A-LOW6）：任务登记进 _bg_tasks 由 async_stop
        # 统一取消（消灭 destroyed-pending 尾噪）。同设备事件的多任务先后
        # 顺序无需额外排队：aiohttp≥3.10 每个 WebSocketResponse 自带
        # _send_lock（FIFO 等待），任务以事件产生顺序进入等待队列，帧序
        # 与事件序一致（HA 2026.x 锁定的 aiohttp 版本远高于 3.10）。
        task = self.hass.async_create_task(self._broadcast(payload))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _device_update_payload(self, gateway_sn: str, device_sn: str) -> Optional[Dict[str, Any]]:
        data = self._find_entry(gateway_sn)
        if data is None:
            return None
        dev = data["device_manager"].devices.get(device_sn)
        if dev is None:
            return None
        view = device_ws_view(device_sn, gateway_sn, dev)
        # 固件 device_update 模板（main.cpp:252）七键：type/gwSn/devSn/
        # position/battery/state/windLockMode——注意设备键是 devSn 而非
        # device_list 的 sn，小程序两处的取值代码不同，拼错即静默丢更新。
        # v1.7.47 追加 winactSpeed/winactStrength（**加字段不改字段**，老小程序忽略即可）：
        # 推送与列表必须同源，否则"首屏有值、一动就丢"。取值一律走 view，
        # 别在这里再算一遍——两处算法迟早会漂。
        return {
            "type": "device_update",
            "gwSn": view["gwSn"],
            "devSn": device_sn,
            "position": view["position"],
            "battery": view["battery"],
            "state": view["state"],
            "windLockMode": view["windLockMode"],
            "winactSpeed": view["winactSpeed"],
            "winactStrength": view["winactStrength"],
        }

    async def _broadcast(self, payload: Dict[str, Any]) -> None:
        dead = []
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        for ws in list(self._clients):
            if getattr(ws, "closed", False):
                dead.append(ws)
                continue
            try:
                await ws.send_str(text)
            except Exception:  # noqa: BLE001 - 发送失败按断连处理
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
            # v1.7.33（全量审计）：仅丢引用不关连接，槽位提前释放而 socket
            # 最长再活 300s（要等它自己的空闲超时）——短时"活连接数＞上限"，
            # 且该连接仍能收帧、继续占宿主 fd。明确 close，让会话立刻收口。
            try:
                self.hass.async_create_task(ws.close())
            except Exception:  # noqa: BLE001
                pass

    # ---------- aiohttp 连接处理 ----------

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        # v1.7.33（全量审计）：停机/关闭窗口的准入闸。async_stop 先快照清空
        # _clients 再 await 关连接，其间监听 socket 仍开着——落在该窗口的
        # 握手会拿到一个"已在停止"的服务器（继续完整服务 control/pair/
        # set_token 最长 300s，且此后无人踢它；runner.cleanup 还要等它）。
        if self._stopping:
            return web.Response(status=503, text="shutting down")
        header = request.headers.get("Sec-WebSocket-Protocol")
        if not handshake_token_ok(header, self._token):
            # 固件语义：拒绝握手（不返回 101）。aiohttp 层用 401 显式表达。
            # v1.7.33：按来源节流，防未认证方无限写 HA 日志（刷掉环形缓冲）
            log_throttled(
                self.hass, "_ws_handshake_denied_logged", str(request.remote),
                600.0, _LOGGER.warning,
                "WS 握手令牌校验失败，拒绝连接（来源 %s）", request.remote)
            return web.Response(status=401, text="unauthorized")
        if len(self._clients) + self._pending_handshakes >= WS_MAX_CLIENTS:
            _LOGGER.warning("WS 连接数已满（%d），拒绝新连接", WS_MAX_CLIENTS)
            return web.Response(status=503, text="busy")
        # 认证成功才占槽（固件 M4 定式）：protocols 传入选中的令牌，
        # 使 101 回显 Sec-WebSocket-Protocol（对齐 esp_http_server 行为）
        # v1.6.26（第八轮审计 D-1）：空令牌（"空串=不认证"是 config_flow
        # 明文支持的形态）下旧值 None 会让 aiohttp≥3.9 的 _handshake 对
        # 带子协议头的请求执行 `proto in self._protocols` 抛 TypeError，
        # 握手 500——而微信 connectSocket 恒带子协议，等于免认证直连态
        # 整体不可用（活体复现+修复验证）。空元组可迭代、匹配不中不回显、
        # 握手继续，恰与 esp_http_server 无认证行为一致。
        protocols = {self._token} if self._token else ()
        # v1.6.26（第八轮审计 D-6）定案注释：**故意不传** max_msg_size——
        # 传了会让 aiohttp 在解析层对 >N 帧直接 1009 断连，绕过固件同款
        # "command too long" JSON 错误回包语义。代价有界：分片重组窗口
        # 由 aiohttp 默认 4MiB 兜住，业务层 WS_MAX_FRAME_BYTES 判定后
        # 断开（最坏 4 槽 ≈16MB 瞬时内存，无功能分歧）。维持现状。
        ws = web.WebSocketResponse(
            protocols=protocols,
            autoclose=True,
            heartbeat=None,
        )
        # v1.6.17（联审 会话层F4）：固件的"查满→入册"在同一回调内原子，
        # 插件在两者之间有 ws.prepare 挂起点，并发握手可同时通过容量检查
        # 瞬时超 4。用预约计数把在途握手计入占位，逼近固件原子语义。
        self._pending_handshakes += 1
        try:
            # v1.6.19（第六轮审计 A-LOW5）：递减移入 finally——旧写法只在
            # except Exception 里减，而 runner cleanup 超时强拆/STOP 级联取消
            # 抛出的是 CancelledError（BaseException 子类，except Exception
            # 捕不到），预约计数只增不减，攒满 WS_MAX_CLIENTS 后本实例对新
            # 连接永久 503 直到进程重启。
            await ws.prepare(request)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("WS 握手完成失败: %s", e)
            self._pending_handshakes -= 1
            # v1.7.31（B-2，真栈栈实锤）：旧版 return ws——未 prepared 的
            # WebSocketResponse 交给 aiohttp finish_response 会**无条件再
            # prepare**（web_protocol.py:713）→ _handshake 再抛 HTTPBadRequest
            # 以 "Unhandled exception" ERROR 逃逸、客户端收不到任何状态行
            # （0918 台架 verbatim 复现）。显式 400 回复，第三种结局与 401/503
            # 同规。
            return web.Response(status=400, text="bad websocket request")
        except BaseException:
            self._pending_handshakes -= 1
            raise
        self._pending_handshakes -= 1
        self._clients.add(ws)
        _LOGGER.info("小程序 WS 已连接（来源 %s，在线 %d/%d）",
                     request.remote, len(self._clients), WS_MAX_CLIENTS)
        try:
            await self._session(ws)
        finally:
            self._clients.discard(ws)
            if not ws.closed:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
            _LOGGER.info("小程序 WS 断开（在线 %d/%d）", len(self._clients), WS_MAX_CLIENTS)
        return ws

    async def _session(self, ws: web.WebSocketResponse) -> None:
        """读取循环：固件 recv_wait_timeout=300s 同款空闲超时（小程序
        60s get_gateways 心跳足以保活）。

        v1.7.31（B-3 台架实锤后口径订正）：超时复位只认**业务 TEXT 帧**——
        aiohttp 在协议层吞掉 PING/PONG（web_ws.py autoping 内循环，receive()
        永不返回 ping/pong 帧），纯 RFC6455-PING 保活的第三方客户端会在
        300s 被踢（台架 0918 对照实测：ping 组 300s 整断开、text 组 340s+
        存活）。固件 recv_wait_timeout 是 socket 任意帧复位，此处为已知的
        语义偏差，接入非小程序客户端前须知；小程序侧 60s get_gateways 是
        唯一被联审的保活通道。

        v1.7.33（全量审计）：**实现**与上述口径此前不符——旧代码每轮
        `wait_for(ws.receive(), 300)` 重新起表，任何让 receive() 返回的帧
        （含 BINARY/空 TEXT）都复位计时，而 docstring 声称只认业务 TEXT。
        默认令牌公开在本仓，同网段主机握手后每 299s 发 1 字节二进制帧即可
        永久占槽（WS_MAX_CLIENTS=4 被占满，真小程序恒 503）。改为显式
        deadline：只在业务 TEXT 分支续期；`_stopping` 时主动退出。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WS_RECV_TIMEOUT_SECONDS
        while not ws.closed and not self._stopping:
            remaining = deadline - loop.time()
            if remaining <= 0:
                _LOGGER.info("WS 空闲超 %ds，断开连接", WS_RECV_TIMEOUT_SECONDS)
                return
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                _LOGGER.info("WS 空闲超 %ds，断开连接", WS_RECV_TIMEOUT_SECONDS)
                return
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                            WSMsgType.ERROR):
                if msg.type is WSMsgType.ERROR and msg.data is not None:
                    # v1.7.33：ERROR 帧是唯一的断连归因通道（1009 帧超限等），
                    # 旧实现裸 return 把它丢了，现场只能猜。
                    _LOGGER.info("WS 错误帧断开: %s", msg.data)
                return
            if msg.type == WSMsgType.TEXT:
                if len(msg.data.encode("utf-8", errors="replace")) > WS_MAX_FRAME_BYTES:
                    try:
                        await ws.send_str(
                            json.dumps({"type": "error", "msg": "command too long"})
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return
                if not msg.data:
                    continue  # 空帧：固件忽略（不计业务活动）
                deadline = loop.time() + WS_RECV_TIMEOUT_SECONDS  # 业务帧续期
                try:
                    resp = await self.handle_json_message(msg.data)
                except Exception as e:  # noqa: BLE001 - 单条命令异常不断会话
                    _LOGGER.error("WS 命令处理异常: %s", e, exc_info=True)
                    continue
                if resp is not None:
                    try:
                        await ws.send_str(
                            json.dumps(resp, ensure_ascii=False, separators=(",", ":"))
                        )
                    except Exception:  # noqa: BLE001
                        return
            # BINARY/PING/PONG：固件非文本帧消费丢弃。v1.7.33 起**不再**为
            # 这类帧续期（B-3 口径即"只认业务 TEXT"）；PING 的 PONG 由 aiohttp
            # 协议层自动完成、不经过本循环。


# ==================== 生命周期入口（__init__.py 调用） ====================

def ws_gateway_wanted(hass: HomeAssistant) -> Optional[Tuple[int, str]]:
    """聚合各 entry options：返回 (port, token)——取第一个未显式关闭
    WS 的 entry 的配置（v1.6.16 默认开）；全部显式关闭/无 entry → None。"""
    for entry in hass.config_entries.async_entries(DOMAIN):
        # 审计 2026-09-30 E-2：禁用条目不算有效配置。这是本仓已经收过口的纪律
        # （utils.py:92 把它定为"单一真源"、mqtt_bootstrap.py:229/352/450/563、
        # config_flow.py:564、__init__.py:332 均自过滤 disabled_by），
        # 而 async_entries() 默认 include_disabled=True。漏判的两条后果都实测可达：
        # ①禁用条目①(port/token A) + 启用条目②(9001/token B) ⇒ 禁用触发 unload、
        #   unload 尾部调本函数 ⇒ 采纳**被禁用条目①**的端口与令牌建服，小程序按
        #   B 连恒 401 / 连错口；②全部条目禁用 ⇒ 本函数仍返回非 None，9001 继续
        #   监听并回 gateway_list: []，正是"无任何网关时监听面不得空转残留"立令
        #   禁止的形态。
        if getattr(entry, "disabled_by", None):
            continue
        options = entry.options or {}
        if not options.get(CONF_WS_GATEWAY_ENABLED, DEFAULT_WS_GATEWAY_ENABLED):
            continue
        raw_port = options.get(CONF_WS_GATEWAY_PORT, DEFAULT_WS_GATEWAY_PORT)
        try:
            port = int(raw_port)
            if port < 1024 or port > 65535:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            # v1.7.61（10-05 审计 #2）：补 OverflowError——int(float('inf'))
            # 抛的类型既不是 ValueError 也不是 TypeError（实测 issubclass 均为
            # False），非有限浮点（Storage 手改/旧版本残留）会逃出本函数，
            # 四个调用点只记 error ⇒ 9001 永不监听、改端口/令牌也不重聚合。
            port = DEFAULT_WS_GATEWAY_PORT
        # v1.7.18（第 7 轮审计 BUG-7）：保留端口运行时防线——config_flow
        # 表单已拦，但 Storage 手改/旧版本残留等非表单路径写入 port=2022
        # （内置 broker 持口）会撞 OSError。回退默认口 + 告警，好过静默
        # 不监听。
        if port in WS_RESERVED_PORTS:
            _LOGGER.warning(
                "WS 网关端口 %d 与保留端口冲突（%s），已回退默认 %d",
                port, sorted(WS_RESERVED_PORTS), DEFAULT_WS_GATEWAY_PORT,
            )
            port = DEFAULT_WS_GATEWAY_PORT
        token = options.get(CONF_WS_GATEWAY_TOKEN, DEFAULT_WS_GATEWAY_TOKEN)
        if not isinstance(token, str):
            token = DEFAULT_WS_GATEWAY_TOKEN
        elif token and (not token_charset_ok(token)
                        or len(token) < WS_TOKEN_MIN_LEN
                        or len(token) >= WS_TOKEN_MAX_LEN):
            # v1.7.31（B-4，与 port 侧 BUG-7 同威胁模型补全）：子协议头按
            # ",\s" 拆分候选——含空白/逗号的令牌是不可满足握手（对任何客户端
            # 恒 401 永久自锁，0918 机制实锤），且会写进 101 响应头成非法
            # HTTP 值；≥63 为固件判式上限。表单层与 set_token 层均已拦，
            # Storage 手改/迁移残留不走表单 ⇒ 运行时同样回退默认+告警。
            # 空串=不认证是 D-1 合法形态，不在回退之列。
            # v1.7.33：补 < WS_TOKEN_MIN_LEN——短令牌（storage 手改/迁移残留
            # 可写入 3 字符）在线枚举成本极低，破之即得物理开窗能力；表单层
            # 早有下限判据，运行态此前漏判（判据分叉）。
            _LOGGER.warning(
                "WS 网关令牌含 RFC6455 子协议不安全字符或超长（不可满足握手"
                "=全部客户端永久 401），已回退默认令牌——若小程序侧存有"
                "自定义令牌，请两侧重新同步或修正 storage",
            )
            token = DEFAULT_WS_GATEWAY_TOKEN
        return port, token
    return None


async def async_ensure_ws_gateway(hass: HomeAssistant) -> None:
    """按当前 options 聚合结果，启动/重启/停止单例 WS 服务器。

    幂等：在任一 config entry setup/unload 尾部与 HA STOP 时调用。
    服务器启动失败（端口被占等）只记 error，不影响集成其余功能。
    """
    if DOMAIN not in hass.data:
        return
    domain_data = hass.data[DOMAIN]
    current: Optional[WsGatewayServer] = domain_data.get(WS_GATEWAY_DATA_KEY)
    wanted = ws_gateway_wanted(hass)

    if wanted is None:
        if current is not None:
            await current.async_stop()
            # v1.7.18（第 7 轮审计 BUG-6，F2 对称补齐）：停止后删注册同样
            # 须判等——async_stop 有真实让出点（客户端 close 最长 ~10s），
            # 期间并发 ensure 可能已拉起新实例并登记；无条件 pop 会删掉
            # 他人注册，孤儿监听器持旧令牌再也关不掉（"WS 已关"假象）。
            if domain_data.get(WS_GATEWAY_DATA_KEY) is current:
                domain_data.pop(WS_GATEWAY_DATA_KEY, None)
        return

    port, token = wanted
    if (current is not None and current.port == port
            and current._runner is not None
            # v1.7.12（审计 F2 配套）：热同步不得命中正在停止的实例——
            # cleanup 完成前 _runner 仍非 None，同步令牌/监听器落进将死对象
            # 成为静默无效写（用户改令牌后旧令牌仍被使用）
            and not current._stopping):
        # 端口未变：仅热同步令牌（options 被 UI 改过）与监听器
        current._token = token
        current._attach_listeners([data["device_manager"] for _gw, data in current._entries_data()])
        return

    if current is not None:
        await current.async_stop()

    if domain_data.get(WS_GATEWAY_STOPPED_KEY):
        # HA 正在停止：STOP 后 entry 逐个 unload 也会调本函数，若无此
        # 闩锁会把刚停掉的服务器在关机过程中重新拉起
        return

    server = WsGatewayServer(hass, port=port, token=token)
    # v1.7.18（第 7 轮审计 BUG-6）：端口撞车大概率是上一轮 ensure 的旧实例
    # async_stop 尚未释放完（真实让出点）——旧实现一次撞车即放弃，"已保存
    # 开启"冻结成永不监听。改为：复检活对端（有则 adopt）→ 等 1s 让旧口
    # 释放 → 重试一次；仍败才放弃（放弃语义与注册清理保持 F2 判等）。
    start_error: Optional[OSError] = None
    for _attempt in (1, 2):
        try:
            await server.async_start()
            start_error = None
            break
        except OSError as e:
            start_error = e
            existing = domain_data.get(WS_GATEWAY_DATA_KEY)
            if existing is not None and existing is not server and not existing._stopping:
                # 并发对端已注册成功：热同步 adopt，本实例让位
                existing._token = token
                existing._attach_listeners([data["device_manager"] for _gw, data in existing._entries_data()])
                return
            if _attempt == 1:
                await asyncio.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("小程序 WS 网关启动异常（不影响其余功能）: %s", e, exc_info=True)
            return
    if start_error is not None:
        _LOGGER.error(
            "小程序 WS 网关启动失败（端口 %d 被占用或无权限？不影响其余功能，"
            "下次条目变更/保存选项会再补拉）: %s", port, start_error,
        )
        # v1.7.12（第 6 轮审计 F2）：本失败实例从未登记——无条件 pop 会误删
        # 并发另一路 ensure 成功方的注册（多条目并行 setup 实测窗口）：
        # 注册表真空后 unload/"关闭 WS"都停不掉那台仍在监听 9001 的孤儿
        # 服务器。仅当注册确实指向本实例时才清理。
        if domain_data.get(WS_GATEWAY_DATA_KEY) is server:
            domain_data.pop(WS_GATEWAY_DATA_KEY, None)
        return

    # v1.7.12（审计 F2）：迟到撞车复检——async_start 是让出点，若另一并发
    # ensure 在我们期间完成启动并登记（SO_REUSEPORT/半开等双成功形态），
    # 本实例为冗余监听者：自我让位停掉，保持单例注册不被覆写
    existing = domain_data.get(WS_GATEWAY_DATA_KEY)
    if existing is not None and existing is not server and not existing._stopping:
        _LOGGER.debug("并发对端已先注册 WS 网关，本冗余实例让位停止")
        try:
            await server.async_stop()
        except Exception:  # noqa: BLE001
            pass
        existing._token = token
        existing._attach_listeners([data["device_manager"] for _gw, data in existing._entries_data()])
        return

    domain_data[WS_GATEWAY_DATA_KEY] = server
    server._attach_listeners([data["device_manager"] for _gw, data in server._entries_data()])

    # 审计 2026-09-30 C-3：闩锁在 :977 只判一次，而它到本行之间全是让出点
    # （async_start / 迟到撞车复检）。停机与条目 unload 并发时的具体交错：
    # 本路已过 :977（那时还没置锁）→ 正卡在 async_start → STOP 侧读 current 时
    # 我们尚未登记 ⇒ **谁也没停** → 我们随后登记 ⇒ 整个关机过程 9001 继续监听，
    # 而 STOP 监听是 listen_once、已被消费，再无人来停它。
    # 登记后复检一次：锁已置上就把刚起的自己停掉并摘注册，不留监听面过停机。
    if domain_data.get(WS_GATEWAY_STOPPED_KEY):
        _LOGGER.info("HA 正在停止，让位关掉刚拉起的 WS 网关（登记后复检闩锁）")
        if domain_data.get(WS_GATEWAY_DATA_KEY) is server:
            domain_data.pop(WS_GATEWAY_DATA_KEY, None)
        try:
            await server.async_stop()
        except Exception:  # noqa: BLE001
            pass
        return

    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    async def _on_ha_stop(_event) -> None:
        await async_stop_ws_gateway(hass)

    # v1.7.33（全量审计）：STOP 监听单例——旧实现每次 async_start（改端口/
    # 改令牌的"停旧起新"、启动失败重试）都新注册一个 listen_once 且句柄不存
    # 不摘，停机时按启动次数重复执行 stop（幂等但串行），回调集合无界增长；
    # 与 _attach_listeners/_detach_listeners 的成对生命周期不对称。
    if not domain_data.get(WS_GATEWAY_STOP_LISTENER_KEY):
        try:
            domain_data[WS_GATEWAY_STOP_LISTENER_KEY] = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
        except Exception as e:  # noqa: BLE001 - 无 bus 环境（测试桩）不应阻断服务器
            _LOGGER.debug("注册 WS 网关 STOP 监听失败: %s", e)


async def async_stop_ws_gateway(hass: HomeAssistant) -> None:
    """HA STOP 时强制停止并闩锁（幂等；闩锁后 ensure 不再重新拉起）。"""
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return
    domain_data[WS_GATEWAY_STOPPED_KEY] = True
    current = domain_data.get(WS_GATEWAY_DATA_KEY)
    if current is not None:
        await current.async_stop()
        # 审计 2026-09-30 C-3：判等再 pop——与本文件 :954-959（BUG-6/F2 定案）
        # 同一条理由：async_stop 有真实让出点，期间并发 ensure 可能已拉起新实例
        # 并登记，无条件 pop 会删掉**他人**的注册 ⇒ 那台仍在监听 9001 的服务器
        # 从此无人可停（孤儿持旧令牌）。同一函数上方与这里此前一处判等处不判等。
        if domain_data.get(WS_GATEWAY_DATA_KEY) is current:
            domain_data.pop(WS_GATEWAY_DATA_KEY, None)
