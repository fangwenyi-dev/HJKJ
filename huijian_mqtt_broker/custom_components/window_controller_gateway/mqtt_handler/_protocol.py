"""_ProtocolMixin —— 入站解析与分发：订阅回调（handle_gateway_response）、cmd id 归一、去重分发、ack 发送、批量任务

v1.6.25 拆包：代码自 mqtt_handler.py 单文件**逐字原样搬移**，禁止在此顺手优化；方法经
组合类 WindowControllerMQTTHandler 解析（单类形态与拆分前一致）。
"""
import logging
import json
import asyncio
import time
import re
from homeassistant.components import mqtt
from ..const import (
    DOMAIN,
    ATTR_DEVICE_SN,
    ATTR_DEVICE_NAME,
    ATTR_POSITION,
    ATTR_BATTERY,
    DEVICE_TYPE_WINDOW_OPENER,
    PROTOCOL_HEAD,
    TOPIC_GATEWAY_REQ_FORMAT,
)
from ..utils import inbound_payload_ok, is_valid_device_sn, log_throttled

# logger 名钉死为拆分前模块 __name__ 值——日志输出零差异（回归要求）
_LOGGER = logging.getLogger("custom_components.window_controller_gateway.mqtt_handler")


class _ProtocolMixin:
    @staticmethod
    def _norm_cmd_id(raw):
        """命令 id 归一（v1.6.19 第六轮审计 A-LOW4）：_bind_ops 的键恒为
        int(self.command_id)，但网关回包的 id 可能以 "42"/42.0 形态 echo
        （去重层的 f-string 会抹平类型差异放行，pop 的精确匹配却会 miss，
        退回已知有竞态的存在性推断分支）。bool 特判 False→0/True→1 是固件
        id 语义里不存在的形态，归一为 None 交 miss/旁路分支处理。"""
        if isinstance(raw, bool):
            # v1.7.18（第 7 轮审计 BUG-14）：旧实现"原样返回"实为 False→0/
            # True→1 真值——pop(True) 会命中 _bind_ops 键 1 的记账（dict 中
            # True==1 同哈希）造成绑定方向误判。bool 是固件 id 语义里不存在
            # 的形态，显式返回 None 才真正落到 miss/旁路分支。
            return None
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            try:
                return int(raw.strip())
            except ValueError:
                return raw
        # 审计 2026-09-30 A-4：兜底原样返回会让**不可哈希**的 id（网关/桥接
        # 侧塞进 JSON 的 list/dict）一路走到 _ctypes 的
        # `self._bind_ops.pop(self._norm_cmd_id(...))` ⇒ TypeError: unhashable
        # type，绑定/解绑确认整帧丢弃（设备不入库、配对窗要等 60s 超时才关），
        # 异常还逃逸成 HA「Task exception was never retrieved」。
        # 本函数 docstring 自陈未识别类型应"退回 miss/旁路分支"——None 才是
        # 那个形态（None 可哈希、pop(None) 必 miss），不可哈希的一律收敛为它。
        if isinstance(raw, (list, dict, set, bytearray)):
            return None
        return raw

    async def _subscribe_topics(self) -> bool:
        """订阅网关响应主题（并发闸入口，v1.7.33）。

        重连任务与 30s 巡检都可能进入订阅重建（双方都在 `await
        async_subscribe` 让出），旧实现无互斥：两次订阅的后一个句柄覆盖前一个，
        前一个回调永久泄漏——每条上报双份处理（id=0 的周期 002/005 走旁路、
        不经去重），`cleanup()` 又只取消最后一个。同一时刻只允许一次重建。
        """
        if self._sub_lock.locked():
            _LOGGER.debug("已有订阅重建在进行中，跳过本次（防句柄互相覆盖）")
            return False
        async with self._sub_lock:
            return await self._do_subscribe_topics()

    async def _do_subscribe_topics(self) -> bool:
        """订阅MQTT主题 - 根据协议要求简化为只订阅网关响应主题

        v1.7.12（第 6 轮审计 B-3）：返回订阅结果——旧版吞掉全部异常仍返回
        None，setup/重连循环把失败当成功置 connected=True，"指数退避重连"
        成死代码。False 时唯一安全分支（通用异常）照旧补跳重连。
        B-1 配套：记录订阅所绑定的 MQTT client 实例身份，供巡检在
        MQTT 条目 reload（client 重建）后识别订阅失效并重建。
        v1.7.33：句柄交接改为"先落新的、再退旧的"（旧顺序在失败时留下
        订阅空窗）。
        """
        # 订阅网关响应和数据主题
        def handle_gateway_response(msg):
            """处理网关响应和数据消息"""
            # v1.6.19（第六轮审计 A-LOW7）：入站尺寸闸。mosquitto 默认
            # message_size_limit 不限，LAN 上任一可连 2022 的客户端 publish
            # 一条 50-100MB 的 rpt_rsp 会在事件循环线程 json.loads 卡死整个
            # HA（本回调与分发全在 loop 内）。WS 侧有 1024B 帧闸，MQTT 侧
            # 对称补齐：>64KB 一律拒收。
            # v1.7.33（全量审计）：闸上收 utils.inbound_payload_ok 双耳共用
            # （心跳耳此前无闸，而干净主机首配期只有那只耳），留痕改节流
            # ——旧实现逐条打 WARNING，畸形流量下日志盘被刷。
            if not inbound_payload_ok(msg):
                log_throttled(self.hass, "_inbound_oversize_logged", "rpt_rsp",
                              600.0, _LOGGER.warning,
                              "收到超大 MQTT 报文（%d 字节），拒收处理",
                              len(msg.payload))
                return
            try:
                payload = json.loads(msg.payload)
                _LOGGER.debug("收到网关消息: %s", payload)
                
                # 检查是否是标准协议格式（带head和ctype字段）
                if "head" in payload and "ctype" in payload:
                    # 标准协议格式处理
                    ctype = payload.get("ctype")
                    data = payload.get("data", {})
                    # v1.6.19（第六轮审计 A-HIGH1）：data 归一为 dict。显式
                    # `"data": null` 时 payload.get("data", {}) 返回 None（键
                    # 存在、值无效，默认值不生效），非 dict 的 data 会让
                    # _handle_ctype_001 在 ack 发布前抛 TypeError/AttributeError
                    # ——网关按未确认无限重发，形成毒消息重传环（与 v1.6.12
                    # 第五轮 #1 在 005 修掉的是同一类面，当时漏了 001）。
                    # 在分发单点归一，全部 ctype 处理器同受保护。
                    if not isinstance(data, dict):
                        _LOGGER.warning(
                            "消息 data 字段非对象（%s），已归一为空对象: ctype=%s",
                            type(data).__name__, ctype,
                        )
                        data = {}
                    
                    # 检查响应是否来自此网关
                    response_sn = payload.get("sn")
                    if not response_sn:
                        return

                    # P0 类型守卫：网关固件可能以 JSON 数字形式发送 SN（int/float），
                    # 统一转为字符串再比较；其他畸形类型（bool/dict/list 等）只丢弃
                    # 本消息并记录一行警告，绝不能因 AttributeError 崩溃导致整帧
                    # （含心跳）处理中断 → 网关被误判离线长达超时窗口。
                    if isinstance(response_sn, bool) or not isinstance(response_sn, (str, int, float)):
                        _LOGGER.warning("收到网关SN类型非法，忽略该消息: %r", response_sn)
                        return
                    if not isinstance(response_sn, str):
                        response_sn = str(response_sn)

                    # 消息去重检查 - 使用 ctype + id + sn 作为唯一标识
                    msg_key = f"{ctype}_{payload.get('id', 0)}_{response_sn}"
                    # v1.6.11（审计 #5）：去重时间轴换 monotonic——time.time()
                    # 遇 NTP 回跳会令剪枝条件（current_time - v < duration）
                    # 长期为真，旧条目滞留到时钟追平。该字典唯一喂入点在此，
                    # 与 _dispatch_with_dedup 的剪枝同一时基，整体切换安全；
                    # 网关超时判定（_check_gateway_timeout）本就独立用
                    # monotonic，互不掺混
                    current_time = time.monotonic()
                    
                    # 如果是来自其他网关的消息，触发网关发现
                    if response_sn.lower() != self.gateway_sn.lower():
                        # 防御校验：response_sn 来自 MQTT payload（攻击者可控），
                        # 必须满足 SN 格式（≥10 位字母数字），避免畸形 SN 进入发现/配置流程
                        if not isinstance(response_sn, str) or not re.match(r"^[a-zA-Z0-9]{10,}$", response_sn):
                            _LOGGER.warning("收到格式非法的网关SN，忽略: %r", response_sn)
                            return
                        try:
                            # 快速检查：如果该网关已在配置条目中，跳过发现触发
                            # v1.7.31（A-3）：与心跳耳共用 entry_state_for_sn
                            # 三态门（BUG-5 统一口径）——旧默认调用把**禁用
                            # 条目**也算已配置：被禁用的另一台网关 001 在本
                            # 耳同样零止血、零留痕。disabled 态照答止血、
                            # 跳过发现卡、节流留痕。
                            # v1.7.34：三态门补第四态 not_loaded（setup_error/
                            # setup_retry/not_loaded）——命中条目未加载时正式
                            # handler 根本没订阅，旧口径返回 configured ⇒ 两耳
                            # 一起静默、网关 001 无人应答且零留痕。
                            # v1.7.33：log_throttled 已升模块级导入（本地导入
                            # 会把整个函数作用域的名字标记为局部，遮蔽上方
                            # 入站尺寸闸的错误分支 → UnboundLocalError）
                            from ..utils import entry_state_for_sn
                            _st = entry_state_for_sn(self.hass, response_sn)
                            if _st == "configured":
                                return
                            if _st == "disabled":
                                log_throttled(
                                    self.hass, "_hb_disabled_logged",
                                    response_sn.lower(), 600.0, _LOGGER.warning,
                                    "网关 %s 的条目处于禁用状态但仍上报——本耳继续"
                                    "代答 001 止血但不弹发现卡；恢复使用请到 "
                                    "设置→设备与服务 启用。每 SN 10 分钟去重",
                                    response_sn)
                            if _st == "not_loaded":
                                log_throttled(
                                    self.hass, "_proto_unloaded_logged",
                                    response_sn.lower(), 600.0, _LOGGER.warning,
                                    "网关 %s 的条目已配置但未加载（setup 失败或等待"
                                    "重试），正式 handler 未挂订阅——本耳继续代答 "
                                    "001 止血但不弹发现卡（条目已在列表里）。根因请"
                                    "到 设置→设备与服务 查看该条目错误提示或检索"
                                    "setup 异常日志。每 SN 10 分钟去重",
                                    response_sn)
                            
                            if _st != "configured":
                                # v1.7.26 用户裁定 A / v1.7.27 格式定稿 /
                                # v1.7.30 仲裁收口：未配置网关首报 001 代答
                                # （与心跳监听器同门 should_ear_ack_001）——
                                # 多网关场景下第二台的首报由此分支兜住；本回调
                                # 是同步函数（与下方发现触发同款
                                # _schedule_async_task 派发），不可 await。
                                # v1.7.30 ②：台架实锤每个已配置条目分支都是
                                # 应答者（1 请求 2~3 答）——统一走仲裁入口，
                                # 同一 (sn,id) 只有第一耳真正发布。
                                from ..utils import (should_ear_ack_001,
                                                     async_ear_ack_001_arbitrated)
                                if should_ear_ack_001(ctype, data):
                                    self._schedule_async_task(
                                        async_ear_ack_001_arbitrated(
                                            self.hass, response_sn,
                                            payload.get("id", 0)))
                                if _st in ("disabled", "not_loaded"):
                                    # A-3：禁用网关止血代答已派发——发现卡
                                    # 对"用户主动禁用"是打扰，到此为止。
                                    # v1.7.34：not_loaded 同口径——条目已在
                                    # 列表里，async_discover_gateway 第 3 步
                                    # 命中同 SN 条目本就早退，弹卡是纯噪音。
                                    return
                                # v1.6.26（第八轮审计 A-1）：v1.6.25 拆包回归——
                                # 旧单文件里 `from .discovery` 解析到集成根的
                                # discovery.py；下沉进 mqtt_handler/ 包后同一
                                # 字面量指向不存在的 mqtt_handler.discovery，
                                # ModuleNotFoundError 被外层 except 吞成一行
                                # 日志，多网关发现/替换网关静默失效。正确目标
                                # 在上一层：..discovery。
                                from ..discovery import async_discover_gateway
                                gateway_name = f"网关 {response_sn[-4:]}"
                                
                                # 检查是否处于替换模式
                                replace_mode = False
                                for flow in self.hass.config_entries.flow.async_progress():
                                    if flow["handler"] == DOMAIN and flow.get("context", {}).get("source") == "replace_gateway":
                                        replace_mode = True
                                        break
                                
                                # 触发网关发现，传入替换模式标志
                                self._schedule_async_task(
                                    async_discover_gateway(self.hass, response_sn, gateway_name, replace_mode, self.gateway_sn)
                                )
                        except Exception as e:
                            _LOGGER.error("触发未配置网关发现失败: %s", e)
                        return
                    
                    # v1.6.26（第八轮审计 D-4）：SN 大小写自纠——条目存用户录入
                    # 原样、入站匹配大小写不敏感，但 MQTT 主题大小写敏感：含
                    # 字母 SN 录错大小写会呈现"网关在线、指令全部无动作"。以
                    # 网关实际上报的形态为准，内存订正 gateway_sn（发布主题与
                    # payload sn 全部路径的单一真源），警告行保留用户原录入；
                    # 不改写注册表 data（低频场景，重启后首条上报再次订正）。
                    if response_sn != self.gateway_sn:
                        _LOGGER.warning(
                            "网关上报 SN 形态与条目存储不一致（%s → %s），已按上报值订正",
                            self.gateway_sn, response_sn,
                        )
                        self.gateway_sn = response_sn
                        # TOPIC_GATEWAY_REQ 在 __init__ 一次性 format 定型，
                        # 必须同步重建——否则"订正"只改了 payload sn、req 主题
                        # 仍发往错误大小写（症状原样残留）
                        self.TOPIC_GATEWAY_REQ = TOPIC_GATEWAY_REQ_FORMAT.format(
                            gateway_sn=response_sn)

                    # 更新最后上报时间 - 只要收到网关消息就认为在线（单调时钟）
                    self.last_gateway_report_time = time.monotonic()
                    
                    # 只要收到网关消息就认为在线，更新connected状态
                    if not self.connected:
                        self.connected = True
                        self._notify_status_change()
                        _LOGGER.info("网关 %s 收到消息，标记为在线", self.gateway_sn)
                    
                    # 根据不同的消息类型调用相应的处理函数
                    ctype_handlers = {
                        "001": self._handle_ctype_001,
                        "002": self._handle_ctype_002,
                        "003": self._handle_ctype_003,
                        "004": self._handle_ctype_004,
                        "005": self._handle_ctype_005,
                        "006": self._handle_ctype_006,
                        "007": self._handle_ctype_007
                    }
                    
                    if ctype in ctype_handlers:
                        # v1.7.12（审计 B-10）：旁路判定改走 _norm_cmd_id——
                        # 固件若以字符串 "0" 回显周期上报 id，旧判定
                        # msg_id in (0, None) 漏检（"0" != 0），反而把它塞进
                        # 去重层：5s 窗口内的后续周期上报被当重复丢弃（设备
                        # 位置/电量最长 5s 丢失）。归一后 0/None/""/False 形态
                        # 统一走直dispatch旁路（处理函数幂等，重复无害）。
                        msg_id = self._norm_cmd_id(payload.get("id", 0))
                        if not msg_id:
                            # 网关周期上报（002/005）的 id 可能恒为 0：
                            # 若按 ctype+id+sn 去重，5 秒窗口内的后续上报会被误杀，
                            # 导致设备状态/位置更新丢失。id 无效时直接调度
                            # （处理函数幂等，重复处理无害）。
                            self._schedule_async_task(
                                ctype_handlers[ctype](payload, ctype, data)
                            )
                        else:
                            self._schedule_async_task(
                                self._dispatch_with_dedup(
                                    ctype_handlers[ctype](payload, ctype, data),
                                    msg_key,
                                    current_time
                                )
                            )
                    else:
                        # v1.7.33：按 ctype 分桶节流（固件/第三方高频发未知类型
                        # 时逐条 WARNING 会刷盘，多条目下还 ×N 条各打一行）
                        log_throttled(self.hass, "_proto_unknown_ctype_logged",
                                      str(ctype), 600.0, _LOGGER.warning,
                                      "未知的消息类型: %s", ctype)
                    
                    return
                
                # 处理原有格式的响应（向后兼容）
                gateway_sn = payload.get("gateway_sn")
                if not gateway_sn:
                    return
                # P0 类型守卫：与标准协议格式的 sn 字段一致，畸形类型只丢弃本消息
                if isinstance(gateway_sn, bool) or not isinstance(gateway_sn, (str, int, float)):
                    _LOGGER.warning("收到网关SN类型非法，忽略该消息: %r", gateway_sn)
                    return
                if not isinstance(gateway_sn, str):
                    gateway_sn = str(gateway_sn)
                if gateway_sn.lower() != self.gateway_sn.lower():
                    return
                
                # v1.7.18（第 7 轮审计 BUG-15）：legacy 分支补在线记账——
                # 旧实现收消息不刷新 last_gateway_report_time/connected，
                # legacy 固件的网关永远被标"离线"（却又收得到指令，
                # 1800s 口径与消息事实矛盾）。与标准路径同款收敛。
                self.last_gateway_report_time = time.monotonic()
                if not self.connected:
                    self.connected = True
                    self._notify_status_change()

                response_type = payload.get("type")
                
                if response_type == "device_discovery":
                    devices = payload.get("devices", [])
                    for device_info in devices:
                        # v1.7.12（审计 B-12）：legacy 路径逐条守卫——旧版对
                        # 非 str/int 的 device_sn 直接 device_sn[-6:] 求值，
                        # TypeError 打断整批（含后续合法设备）。与 P0 顶层守卫
                        # 同型收敛。
                        raw_sn = device_info.get(ATTR_DEVICE_SN)
                        if isinstance(raw_sn, bool) or not isinstance(
                            raw_sn, (str, int, float)
                        ):
                            _LOGGER.warning("legacy 发现设备 SN 类型非法，跳过: %r", raw_sn)
                            continue
                        device_sn = str(raw_sn)
                        if not device_sn:
                            continue
                        # v1.8.5（审计 G-7）：与 ctype 层同一格式闸——str() 归一
                        # 之后 "0" 非空，旧判空拦不住固件异常回包 sn=0 的幽灵设备。
                        if not is_valid_device_sn(device_sn):
                            _LOGGER.warning("legacy 发现设备 SN 格式非法，跳过: %r", device_sn)
                            continue
                        # 审计 2026-09-30 A-5：上方对 raw_sn 有逐条守卫，name 是
                        # 同一纪律的漏项。`device_info.get(ATTR_DEVICE_NAME, 默认)`
                        # 在"键存在但值为 null"时返回的是 **None 而不是默认值**，
                        # 交给 add_device 的 device_name.lower() 即 AttributeError，
                        # 一条脏 name 打断整批设备入库（与 B-12 修的同一形态）。
                        device_name = device_info.get(ATTR_DEVICE_NAME)
                        if not isinstance(device_name, str) or not device_name:
                            device_name = f"设备 {device_sn[-6:]}"
                        device_type = device_info.get("device_type", DEVICE_TYPE_WINDOW_OPENER)
                        
                        # v1.7.18（第 7 轮审计 BUG-15）：B-6 同型门禁补齐——
                        # 关闭自动发现后 legacy 通道同样不得入库新设备
                        # （已登记设备走幂等 add/改名路径，不受影响）
                        if (device_sn not in self.device_manager.devices
                                and not self._auto_discovery_enabled()):
                            _LOGGER.debug("auto_discovery 已关闭，跳过自动添加: %s", device_sn)
                            continue
                        self._schedule_async_task(
                            self.device_manager.add_device(device_sn, device_name, device_type)
                        )
                        
                elif response_type == "device_status":
                    device_sn = payload.get(ATTR_DEVICE_SN)
                    if not device_sn:
                        return
                    # v1.7.18（BUG-15）：数值形态 SN 会在 update_device_status
                    # 的"不存在则自动添加"漏斗里 TypeError，同款归一；并补
                    # 未知设备门禁（已登记设备正常更新不受影响）
                    if isinstance(device_sn, bool) or not isinstance(
                        device_sn, (str, int, float)
                    ):
                        return
                    if not isinstance(device_sn, str):
                        device_sn = str(device_sn)
                    # v1.8.5（审计 G-7）：同款格式闸，堵住 sn=0 → "0" 经
                    # update_device_status 自动入库的幽灵设备面。
                    if not is_valid_device_sn(device_sn):
                        _LOGGER.warning("legacy 设备状态 SN 格式非法，忽略: %r", device_sn)
                        return
                    if (self.device_manager.get_device(device_sn) is None
                            and not self._auto_discovery_enabled()):
                        return
                    
                    status = payload.get("status", "unknown")
                    attributes = {}
                    
                    if ATTR_POSITION in payload:
                        attributes[ATTR_POSITION] = payload[ATTR_POSITION]
                    if ATTR_BATTERY in payload:
                        attributes[ATTR_BATTERY] = payload[ATTR_BATTERY]
                    
                    self._schedule_async_task(
                        self.device_manager.update_device_status(device_sn, status, attributes)
                    )
                    
            except json.JSONDecodeError:
                # v1.7.12（审计 B-12）：投毒/损坏报文此前全量入日志（64KB 闸
                # 内单条即可刷满日志盘）——截 256 字节
                # v1.7.33：再按 600s 节流——畸形流量逐条打仍能刷盘，与 A-4
                # 在心跳耳定案的"必响+去重"口径对齐（形态分桶留痕不丢）。
                log_throttled(self.hass, "_proto_parse_err_logged", "json",
                              600.0, _LOGGER.error,
                              "MQTT消息解析失败: %s", msg.payload[:256])
            except KeyError as e:
                log_throttled(self.hass, "_proto_parse_err_logged", "keyerror",
                              600.0, _LOGGER.error,
                              "MQTT消息缺少必要字段: %s", e)
            except ValueError as e:
                log_throttled(self.hass, "_proto_parse_err_logged", "valueerror",
                              600.0, _LOGGER.error,
                              "MQTT消息数据格式错误: %s", e)
            except Exception as e:
                log_throttled(self.hass, "_proto_parse_err_logged",
                              f"exc:{type(e).__name__}", 600.0, _LOGGER.error,
                              "处理网关消息时出错: %s", e)
        
        try:
            # 订阅网关响应主题
            new_unsub = await mqtt.async_subscribe(self.hass, self.TOPIC_GATEWAY_RSP, handle_gateway_response, 1)
            # v1.7.33：新的先落地、再退旧的——旧顺序（先退后订）在订阅失败
            # 时把已工作的订阅也拆了，留下空窗直到下轮巡检。
            old_unsub, self._unsub_rsp = self._unsub_rsp, new_unsub
            if old_unsub:
                try:
                    old_unsub()
                except Exception as e:  # noqa: BLE001
                    _LOGGER.debug("取消旧MQTT订阅时出错: %s", e)
            # v1.7.12（审计 B-1）：记下订阅所绑定的 client 实例身份（弱引用 +
            # 身份整数双份，见 _remember_mqtt_client）
            self._remember_mqtt_client()
            _LOGGER.debug("订阅网关消息主题: %s", self.TOPIC_GATEWAY_RSP)
            return True
        except ConnectionError as e:
            # v1.7.12（审计 B-3）：三个失败分支统一补重连调度并返回 False——
            # 旧版 ConnectionError/TimeoutError 分支连 _schedule_reconnect 都
            # 不发，订阅永久缺失
            _LOGGER.error("MQTT连接失败: %s", e)
            self._schedule_reconnect()
            return False
        except TimeoutError as e:
            _LOGGER.error("MQTT订阅超时: %s", e)
            self._schedule_reconnect()
            return False
        except Exception as e:
            _LOGGER.error("订阅MQTT主题失败: %s", e)
            # 触发重连逻辑
            self._schedule_reconnect()
            return False

    async def _ensure_mqtt_subscription(self) -> bool:
        """v1.7.12（第 6 轮审计 B-1）：MQTT client 换代后重建订阅。

        HA 的 MQTT 集成在配置条目 reload/重建时销毁旧 client 并新建，其他
        集成经 async_subscribe 注册的回调随旧 client 作废且不会自动迁移——
        本集成的发布走当前 client（看起来一切正常），入站 gateway/rpt_rsp
        却永不再达：网关 1800s 后被误判离线、Web/实体全部冻结，须手动重载
        慧尖条目才活。而 mqtt_bootstrap 在配置不匹配分支自己就会 reload MQTT
        条目（多网关追加/改密场景必踩）。由 30s 网关巡检周期调用：client
        身份与订阅所绑定时不一致 → 重跑 _subscribe_topics（内部自带旧订阅
        取消）。返回是否执行了重建。
        """
        # v1.7.33（全量审计）：改用弱引用做身份比较。旧实现存 `id(client)`
        # 整数——MQTT 条目 reload 后旧 client 引用归零被释放，新对象**可能
        # 复用同一地址**（同类型同尺寸），`current == self._mqtt_client_id`
        # 恒真 → "client 未变"假阴性，B-1 要修的形态复活且更隐蔽（发布正常、
        # rpt_rsp 永不再达、全程零日志）。弱引用还活着且 `is` 同一对象才是
        # 真·未换代；引用死亡或指向别的对象皆为换代。
        current = self.hass.data.get("mqtt")
        if self._mqtt_client_id is None:
            return False                       # 从未订阅成功（旧语义）
        if current is None:
            return False                       # client 尚未就绪：下轮再判，不空转
        ref = getattr(self, "_mqtt_client_ref", None)
        if ref is not None:
            stale = ref() is not current
        else:
            # 不可弱引用的替身（测试桩）退回身份整数路径，语义不劣化
            stale = id(current) != self._mqtt_client_id
        if not stale:
            return False
        _LOGGER.warning(
            "检测到 MQTT client 实例已更换（条目 reload/重建），重建 "
            "gateway/rpt_rsp 订阅以避免入站失联"
        )
        ok = await self._subscribe_topics()
        if not ok:
            # v1.7.18（第 7 轮审计 BUG-1）：旧实现在此置 None 兑现"下个巡检
            # 周期再试"——但入口对 None 的语义是"从未订阅→跳过"，置 None
            # 恰使重试分支永不可达（永久自锁）。保留旧身份（≠当前 client），
            # 下轮巡检自然再试；_subscribe_topics 内已补重连调度兜底。
            _LOGGER.warning("订阅重建失败（新 client 可能尚未就绪），保留旧身份待下轮巡检重试")
        return True


    async def _batch_process_tasks(self, tasks, task_type="处理"):
        """批处理异步任务
        
        Args:
            tasks: 要执行的异步任务列表
            task_type: 任务类型描述，用于日志
        """
        if not tasks:
            return
        
        batch_size = 10
        total_success = 0
        for i in range(0, len(tasks), batch_size):
            batch_tasks = tasks[i:i+batch_size]
            results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            success_count = sum(1 for r in results if not isinstance(r, Exception))
            total_success += success_count
            # v1.6.12（第五轮审计 #2）：子任务异常此前只被计数、内容整体丢弃
            # （毒数据引发静默丢更新时无从排查），逐条记警告
            for r in results:
                if isinstance(r, Exception):
                    _LOGGER.warning("批量%s子任务异常: %r", task_type, r)
            _LOGGER.info("批量%s完成，批次: %d，成功: %d，总数: %d", 
                       task_type, i//batch_size + 1, success_count, len(batch_tasks))
        _LOGGER.info("所有批次%s完成，总成功: %d，总总数: %d", task_type, total_success, len(tasks))

    async def _dispatch_with_dedup(self, handler_coro, msg_key: str, current_time: float):
        """带去重检查的异步任务分发"""
        async with self._msg_lock:
            self._processed_messages = {
                k: v for k, v in self._processed_messages.items()
                if current_time - v < self._message_dedup_duration
            }
            if msg_key in self._processed_messages:
                _LOGGER.debug("跳过重复消息: %s", msg_key)
                handler_coro.close()
                return
            self._processed_messages[msg_key] = current_time
        # v1.7.12（第 6 轮审计 B-9）：处理失败时回滚去重记账——旧版"先记账后
        # 执行"且失败不回滚，一次瞬时异常（MQTT not ready/注册表写失败）会让
        # 网关 2s 重发的同一报文被 5s 去重窗吞掉：ack 已承诺的语义没做完、
        # 重发又被丢弃，最长 5s 的更新黑洞。成功才保留记账。
        try:
            await handler_coro
        except Exception:
            async with self._msg_lock:
                if self._processed_messages.get(msg_key) == current_time:
                    self._processed_messages.pop(msg_key, None)
            raise

    async def _send_ack(self, ctype: str, payload: dict):
        """发送确认响应到网关（用于网关主动发起的消息）

        网关主动发起的消息（001/002/005）需要 HA 回复 errcode:0 确认，
        否则网关会重复重发。
        HA 主动下发的命令（003/004/006/007）由网关回复，HA 不需要再回复。
        """
        response_payload = {
            "head": PROTOCOL_HEAD,
            "ctype": ctype,
            "id": payload.get("id", 0),
            "sn": self.gateway_sn,
            "data": {
                "errcode": 0
            }
        }
        await mqtt.async_publish(
            self.hass,
            self.TOPIC_GATEWAY_REQ,
            json.dumps(response_payload),
            1,
            False
        )
        _LOGGER.debug("已发送%s确认响应，id: %s", ctype, payload.get("id", 0))
