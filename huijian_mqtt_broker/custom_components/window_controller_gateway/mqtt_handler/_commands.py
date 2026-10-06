"""_CommandsMixin —— 出站指令与探测：send_command(004)、send_ws_raw_004、check_connection、unbind、触发发现、配对开始/中止

v1.6.25 拆包：代码自 mqtt_handler.py 单文件**逐字原样搬移**，禁止在此顺手优化；方法经
组合类 WindowControllerMQTTHandler 解析（单类形态与拆分前一致）。
"""
import logging
import json
import asyncio
from typing import Dict, Any, Optional
from homeassistant.components import mqtt
from ..utils import is_mqtt_loaded
from ..utils import is_mqtt_connected
from ..const import (
    MAX_COMMAND_ID,
    PROTOCOL_HEAD,
    DEVICE_TYPE_CURTAIN_CTR,
    PAIRING_SN_PLACEHOLDER,
    COMMAND_VALUE_OPEN,
    COMMAND_VALUE_CLOSE,
    COMMAND_VALUE_STOP,
    COMMAND_VALUE_TOGGLE,
    ATTRIBUTE_W_TRAVEL,
    ATTRIBUTE_WIND_LOCK_MODE,
    ATTRIBUTE_WINACT_SPEED,
    ATTRIBUTE_WINACT_STRENGTH,
    SPEED_MIN,
    SPEED_MAX,
    COMMAND_VALUE_WIND_LOCK_TILT,
    COMMAND_VALUE_WIND_LOCK_FLAT,
)

# logger 名钉死为拆分前模块 __name__ 值——日志输出零差异（回归要求）
_LOGGER = logging.getLogger("custom_components.window_controller_gateway.mqtt_handler")


class _CommandsMixin:
    async def send_command(self, device_sn: str, command: str, params: Optional[Dict[str, Any]] = None) -> bool:
        """发送命令到设备
        
        Args:
            device_sn: 设备SN
            command: 命令类型
            params: 额外参数
            
        Returns:
            bool: 是否成功送达 broker。语义边界（审计 B6，v1.6.10 明确）：
            True = QoS1 publish 被 broker 接收，不代表设备已收到/执行
            （执行实据靠 005 上报）；False = 确定未送达，调用方必须按失败
            处理并抛错——这是 v1.6.9 failfast 契约的适用范围。
        """
        try:
            # 验证参数
            if not device_sn:
                _LOGGER.error("设备SN不能为空")
                return False
            
            if not command:
                _LOGGER.error("命令类型不能为空")
                return False
            
            # 验证命令类型（仅保留实际有 command_map 映射的命令；
            # bind_gateway/discover/status 协议上由网关主动发起，HA 发了网关不响应，已移除）
            valid_commands = ["start_pairing", "open", "close", "stop", "a", "set_position", "set_speed", "set_strength", "wind_lock_tilt", "wind_lock_flat"]
            if command not in valid_commands:
                _LOGGER.error("未知命令类型: %s", command)
                return False
            
            # 控制命令集合（开/关/停/内倒/风锁模式/设置位置/设置速度/设置力度）
            control_commands = ["open", "close", "stop", "a", "set_position", "set_speed", "set_strength", "wind_lock_tilt", "wind_lock_flat"]

            # 设备存在性检查：控制命令跳过——用户要求任何时候都可控制，
            # 设备 SN 由实体提供，设备是否已被网关上报/是否在 device_manager
            # 中都不影响发送。仅对需要在设备存在前提下执行的其他命令检查。
            if command not in ["bind_gateway", "start_pairing", "discover"] + control_commands:
                device = self.device_manager.get_device(device_sn)
                if not device:
                    _LOGGER.error("设备不存在，无法发送命令: %s", device_sn)
                    return False

            # 控制命令与配对命令无论网关在线与否都尝试发送（MQTT QoS 1 保证送达）
            is_offline_allowed_command = command in control_commands + ["start_pairing"]
            
            if is_offline_allowed_command:
                _LOGGER.info("命令 %s 无论网关在线与否都尝试发送", command)
            else:
                if not self.connected:
                    _LOGGER.debug("MQTT连接未建立，尝试重连...")
                    try:
                        # 统一走去重入口，避免与已在运行的重连任务并发
                        self._schedule_reconnect()
                        if self._reconnect_task:
                            try:
                                await self._reconnect_task
                            except asyncio.CancelledError:
                                # Bug3 修复：重连任务被取消（如卸载时），按失败处理
                                pass
                            except Exception:
                                pass
                        if not self.connected:
                            _LOGGER.debug("MQTT重连失败，无法发送命令")
                            return False
                    except Exception as reconnect_error:
                        _LOGGER.debug("MQTT重连失败: %s", reconnect_error)
                        return False
            
            # 根据协议文档，使用标准的协议格式
            # 注意：001/002 是网关主动发起的消息，HA 发送后网关不会响应
            # HA 可主动发送 003（配对）、004（控制）、006、007
            # 001/002/005 是网关主动发起，HA 发了网关不响应
            command_map = {
                # "bind_gateway": "001",  # 001: 网关主动发起，HA 发了无效
                "start_pairing": "003",  # 003: HA 主动发起配对
                # "discover": "002",     # 002: 网关主动发起，HA 发了无效
                "open": "004",  # 004: HA 主动发起控制
                "close": "004",  # 004: HA 主动发起控制
                "stop": "004",  # 004: HA 主动发起控制
                "a": "004",  # 004: HA 主动发起控制
                "set_position": "004",  # 004: HA 主动发起控制
                "set_speed": "004",  # 004: HA 主动发起控制 - 开窗速度
                "set_strength": "004",  # 004: HA 主动发起控制 - 开窗力度
                "wind_lock_tilt": "004",   # 004: HA 主动发起控制 - 内倒模式
                "wind_lock_flat": "004"    # 004: HA 主动发起控制 - 平开模式
            }
            
            ctype = command_map.get(command, "004")
            
            # 构建协议格式的payload
            payload = {
                "head": PROTOCOL_HEAD,
                "ctype": ctype,
                "id": self.command_id,  # 使用自增ID
                "data": {
                }
            }
            
            # 添加sn字段到payload的末尾
            payload["sn"] = self.gateway_sn
            
            # 确保params不为None，避免后续访问 .get() 时崩溃
            if params is None:
                params = {}

            # 添加额外参数
            try:
                payload["data"].update(params)
            except Exception as e:
                _LOGGER.error("更新额外参数失败: %s", e)
            
            # 根据命令类型添加特定参数
            if command == "start_pairing":
                # 清空data并设置正确的配对参数
                payload["data"] = {
                    "bind": 1,  # 新增字段
                    "devtype": DEVICE_TYPE_CURTAIN_CTR,
                    "sn": PAIRING_SN_PLACEHOLDER
                }
                # 在顶层也添加bind字段
                payload["bind"] = 1
                # v1.6.12（第五轮审计 #3）：新配对取代一切陈旧 bind 记账——
                # start_pairing 的记录是 ("bind", None)，而 _clear_bind_ops_for_device
                # 按 device_sn 匹配（None 永不命中）、超时回调也不清理，导致旧会话
                # 迟到确认仍能 pop 命中旧记账、bind_op=="bind" 门控下掐掉当前会话的
                # 定时器提前关窗（v1.6.11 #2 目标的残留窗口）。配对会话不变式：
                # 同一时刻只保留最新一条 bind 记账
                for _stale_id in [k for k, (op, _sn) in self._bind_ops.items() if op == "bind"]:
                    self._bind_ops.pop(_stale_id, None)
                    _LOGGER.debug("已清除陈旧待处理绑定记录: id=%s", _stale_id)
                # 记录本命令方向（id 匹配回复，供 _handle_ctype_003 判定）
                self._record_bind_op(payload["id"], "bind")
            elif command in ["open", "close", "stop", "a"]:
                # 控制命令需要包含子设备SN
                payload["data"]["sn"] = device_sn
                payload["data"]["attribute"] = ATTRIBUTE_W_TRAVEL
                if command == "open":
                    payload["data"]["value"] = COMMAND_VALUE_OPEN
                elif command == "close":
                    payload["data"]["value"] = COMMAND_VALUE_CLOSE
                elif command == "stop":
                    payload["data"]["value"] = COMMAND_VALUE_STOP
                elif command == "a":
                    payload["data"]["value"] = COMMAND_VALUE_TOGGLE
            elif command == "set_position":
                # 设置位置命令
                payload["data"]["sn"] = device_sn
                payload["data"]["attribute"] = ATTRIBUTE_W_TRAVEL
                position = params.get("position", 0)
                # 验证位置参数
                # v1.6.19（第六轮审计 B-LOW11）：非法/越界不再回退 0——
                # 0 是真实语义"关窗"，"想开到 150%" 被静默执行成"关窗"是
                # 反向动作；拒绝下发并如实失败，由调用方（schema 已前置
                # 拦截，走到这里说明是内部误用）感知。
                try:
                    position = int(position)
                except (ValueError, TypeError, OverflowError):
                    _LOGGER.error("位置参数无效，拒绝下发: %s", params.get("position"))
                    return False
                if position < 0 or position > 100:
                    _LOGGER.error("位置参数超出范围(0-100)，拒绝下发: %s", position)
                    return False
                payload["data"]["value"] = str(position)
            elif command in ("set_speed", "set_strength"):
                # 开窗速度/力度控制（rwp_winact_speed / rwp_winact_strength，0-100）
                payload["data"]["sn"] = device_sn
                if command == "set_speed":
                    payload["data"]["attribute"] = ATTRIBUTE_WINACT_SPEED
                    raw_value = params.get("speed", 0)
                else:
                    payload["data"]["attribute"] = ATTRIBUTE_WINACT_STRENGTH
                    raw_value = params.get("strength", 0)
                try:
                    value = int(raw_value)
                except (ValueError, TypeError, OverflowError):
                    # v1.6.19 B-LOW11 同口径：无法解析的值拒绝下发
                    # （回退 0 = 速度/力度"最弱档"，同样是静默反向语义）
                    _LOGGER.error("%s 参数无效，拒绝下发: %s",
                                  payload["data"]["attribute"], raw_value)
                    return False
                if value < SPEED_MIN or value > SPEED_MAX:
                    # v1.7.65：越界由"裁剪后下发"改成**拒绝下发**，与本文件 set_position 分支
                    # 的 v1.6.19 B-LOW11 定案同口径——"想设 150"被静默执行成 100 是反向动作，
                    # 而调用方已经拿到成功回执。此前 speed/strength 是四个可调属性里唯一还在
                    # 裁剪的两个，与 const.CONTROL_ATTR_DOMAINS（越界即拒）形成同一仓库内的
                    # 判据分歧：服务路径放行、WS/云路径拒绝。
                    _LOGGER.error("%s 参数超出范围(%d-%d)，拒绝下发: %s",
                                  payload["data"]["attribute"], SPEED_MIN, SPEED_MAX, value)
                    return False
                payload["data"]["value"] = str(value)
            elif command in ("wind_lock_tilt", "wind_lock_flat"):
                # 风锁模式控制 - 内倒模式(value=0) / 平开模式(value=1)
                payload["data"]["sn"] = device_sn
                payload["data"]["attribute"] = ATTRIBUTE_WIND_LOCK_MODE
                if command == "wind_lock_tilt":
                    payload["data"]["value"] = COMMAND_VALUE_WIND_LOCK_TILT
                else:
                    payload["data"]["value"] = COMMAND_VALUE_WIND_LOCK_FLAT
            # 协议说明：005 是网关主动发起的设备状态上报，HA 无法主动查询设备状态
            # elif command == "status":
            #     # 状态查询命令 - 必须包含设备SN，网关才能知道查询哪个设备
            #     payload["data"]["sn"] = device_sn
            
            # 打印详细的命令信息
            _LOGGER.debug("发送命令到网关: %s, 命令: %s, 设备SN: %s, 载荷: %s", 
                          self.TOPIC_GATEWAY_REQ, command, device_sn, payload)

            # 递增ID，保持在合理范围内
            self.command_id += 1
            if self.command_id > MAX_COMMAND_ID:
                self.command_id = 1
            
            try:
                await mqtt.async_publish(
                    self.hass,
                    self.TOPIC_GATEWAY_REQ,
                    json.dumps(payload),
                    1,
                    False
                )
                _LOGGER.info("发送协议命令: %s (类型: %s) 到设备: %s, 参数: %s", command, ctype, device_sn, payload["data"])

                # 不启用命令重发：MQTT QoS 1 保证消息送达 broker，网关在线即可收到。
                # 网关已执行但回复丢失时自动重发会造成重复配对/重复控制，
                # 命令均由用户主动触发，未生效时用户再次操作即可。
                return True
            except Exception as publish_error:
                _LOGGER.error("MQTT消息发布失败: %s\n命令: %s\n设备: %s\n主题: %s\n载荷: %s", 
                             publish_error, command, device_sn, self.TOPIC_GATEWAY_REQ, payload)
                # 标记连接为断开
                self.connected = False
                self._notify_status_change()
                # v1.6.11（审计 #4）：其余置 connected=False 的失败点
                # （check_connection 两分支/重连放弃）都同步
                # update_gateway_status("offline")，唯独此处漏网——双状态源
                # 就此分叉（gateway_status 恒 "online" 直到超时巡检）。对齐
                self._schedule_async_task(
                    self.device_manager.update_gateway_status("offline")
                )
                return False
        except Exception as e:
            _LOGGER.error("发送MQTT命令失败: %s\n命令: %s\n设备: %s", e, command, device_sn)
            return False

    async def send_ws_raw_004(self, device_sn: str, attribute: str, value: str) -> bool:
        """v1.6.15 小程序 WS 网关：透传一条 004 控制命令，不做语义解释。

        与固件 app_ws_gateway.c/app_protocol_bridge.cpp 的 WS→MQTT 转发
        等形：payload = {"head":"$SH","ctype":"004","id":N,
        "data":{"sn":dev,"attribute":attr,"value":val},"sn":gw}，
        发布到本网关 gateway/{sn}/req（QoS1）。

        与 send_command 的边界：send_command 面向 HA 实体（枚举命令+参数
        校验+裁剪），本方法面向小程序协议透传——值语义（w_travel 的
        100/0/101/200/0-100、rwp_wind_lock_mode 0/1 等）由 LoRa 设备端
        解释，固件不校验，本方法同样不校验，保证双桥行为一致。
        返回值语义同 send_command：True = QoS1 已送达 broker，不代表执行。
        """
        payload = {
            "head": PROTOCOL_HEAD,
            "ctype": "004",
            "id": self.command_id,
            "data": {
                "sn": device_sn,
                "attribute": attribute,
                "value": value,
            },
            "sn": self.gateway_sn,
        }
        self.command_id += 1
        if self.command_id > MAX_COMMAND_ID:
            self.command_id = 1
        try:
            await mqtt.async_publish(
                self.hass,
                self.TOPIC_GATEWAY_REQ,
                json.dumps(payload),
                1,
                False,
            )
            _LOGGER.info("WS透传控制命令: gw=%s dev=%s attr=%s value=%s",
                         self.gateway_sn, device_sn, attribute, value)
            return True
        except Exception as publish_error:
            _LOGGER.error("WS透传控制命令发布失败: %s\n载荷: %s", publish_error, payload)
            # 与 send_command 失败路径同构（v1.6.11 #4 定式）：broker 不可达
            # 即刷新在线状态并同步 device_manager，避免双状态源分叉
            if self.connected:
                self.connected = False
                self._notify_status_change()
            self._schedule_async_task(
                self.device_manager.update_gateway_status("offline")
            )
            return False

    def abort_pairing_if_active(self) -> None:
        """v1.6.10（审计 B2）：配对启动失败时清理上一次配对残留。

        start_pairing（服务）与 GatewayPairingButton（按钮）都先 cancel 旧的
        超时定时器"接管句柄"再发送命令；若发送失败抛错，pairing_active 还是
        上次成功置的 True，而唯一能清它的定时器已被取消——网关卡片永久卡
        "配对中"。所有失败路径必须经本方法：复位标志、通知、恢复状态机。
        幂等，可安全重复调用。
        """
        if self.pairing_timeout_handle:
            try:
                self.pairing_timeout_handle.cancel()
            except Exception:
                pass
            self.pairing_timeout_handle = None
        if self.pairing_active:
            self.pairing_active = False
            self._notify_status_change()
            if self.hass is not None and self.device_manager is not None:
                try:
                    self.hass.async_create_task(
                        self.device_manager.update_gateway_status(
                            "online" if self.connected else "offline"
                        )
                    )
                except Exception as e:
                    _LOGGER.warning("配对中止后恢复网关状态失败: %s", e)

    async def check_connection(self):
        """检查网关连接状态（不再向网关发布任何消息）

        历史问题：旧实现向 `gateway/{sn}/req` 主题发布空 payload 来探测 broker
        可达性。网关固件订阅该主题并尝试解析 JSON，收到空消息会解析失败，
        属于给固件发送垃圾流量，且 publish 成功只代表 broker 可达、不代表网关在线。

        现在的实现：
        - 网关在线状态由 handle_gateway_response 收到网关上报时设置；
        - 网关离线由 _check_gateway_timeout 超时巡检（GATEWAY_TIMEOUT_SECONDS）负责；
        - 这里仅做轻量判断：MQTT 集成未加载或 broker 断开时，网关必然无法通信，
          此时标记离线；否则返回当前网关在线状态。
        """
        try:
            # MQTT 集成未加载：网关必然无法通信
            if not is_mqtt_loaded(self.hass):
                _LOGGER.error("MQTT集成未启用，网关无法通信")
                if self.connected:
                    self.connected = False
                    self._notify_status_change()
                    self._schedule_async_task(
                        self.device_manager.update_gateway_status("offline")
                    )
                return False

            # broker 未连接：网关必然离线（兼容旧版 HA 无此 API 的情况）
            if not is_mqtt_connected(self.hass):
                _LOGGER.debug("MQTT broker 未连接，网关标记为离线")
                if self.connected:
                    self.connected = False
                    self._notify_status_change()
                    self._schedule_async_task(
                        self.device_manager.update_gateway_status("offline")
                    )
                return False

            return self.connected
        except Exception as e:
            _LOGGER.error("检查连接状态失败: %s", e)
            return self.connected

    async def unbind_device(self, device_sn: str):
        """解绑设备 - 使用协议类型003，bind=0

        协议格式：
          {"head":"$SH","ctype":"003","id":<id>,"sn":"<网关SN>",
           "data":{"devtype":"<设备类型>","sn":"<设备SN>","bind":0}}

        解绑的网关回复走 003（errcode=0）。
        """
        # 获取设备实际类型，回退到 DEVICE_TYPE_CURTAIN_CTR
        device = self.device_manager.get_device(device_sn)
        device_type = device.get("type", DEVICE_TYPE_CURTAIN_CTR) if device else DEVICE_TYPE_CURTAIN_CTR

        payload = {
            "head": PROTOCOL_HEAD,
            "ctype": "003",
            "id": self.command_id,
            "data": {
                "bind": 0,
                "devtype": device_type,
                "sn": device_sn
            },
            "sn": self.gateway_sn,
            # 顶层 bind 字段与配对（start_pairing 顶层 bind=1）保持一致，
            # 固件按顶层 bind 识别绑定/解绑命令
            "bind": 0
        }
        sent_command_id = self.command_id
        # 递增ID（命令 id 仍随消息发送，仅不再注册重发）
        self.command_id += 1
        if self.command_id > MAX_COMMAND_ID:
            self.command_id = 1
        _LOGGER.debug("解绑命令 id=%s", sent_command_id)
        # 记录本命令方向（id 匹配回复，供 _handle_ctype_003 判定），
        # 附带设备 SN 供删除时清理待处理记录
        self._record_bind_op(payload["id"], "unbind", device_sn)
        
        # 发送MQTT消息
        try:
            await mqtt.async_publish(
                self.hass,
                self.TOPIC_GATEWAY_REQ,
                json.dumps(payload),
                1,
                False
            )
            _LOGGER.info("解绑命令已发送，设备SN: %s", device_sn)
            _LOGGER.debug("解绑命令payload: %s", payload)

            # 003（解绑）不注册重发：解绑后设备可能立即从注册表移除，
            # 自动重发可能在设备已删除后仍向网关发送命令，由用户再次操作。
        except Exception as e:
            _LOGGER.error("发送解绑命令失败: %s", e)
            raise

    async def trigger_discovery(self):
        """触发设备发现

        协议说明：002 是网关主动发起的上报，HA 无法主动触发网关上报设备列表。
        设备发现完全依赖网关主动发送 002 消息，HA 被动接收。
        此方法保留为空实现，仅记录日志告知调用方。
        """
        _LOGGER.info("设备发现依赖网关主动上报（002），HA 无法主动触发")

    async def fast_discovery(self):
        """快速设备发现

        协议说明：002 和 005 都是网关主动发起的上报，HA 无法主动触发。
        设备发现和状态更新完全依赖网关主动发送 002/005 消息，HA 被动接收。
        此方法保留为空实现，仅记录日志。
        """
        _LOGGER.info("设备发现依赖网关主动上报（002/005），HA 无法主动触发")
