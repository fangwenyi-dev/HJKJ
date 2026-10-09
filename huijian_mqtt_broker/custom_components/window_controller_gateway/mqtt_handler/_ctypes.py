"""_CtypeHandlersMixin —— ctype 001-007 业务处理分支（ack 方向契约原样保留：
001/002/005 网关主动发起必 ack、005 finally 保证、003/004/006/007 不回包）

v1.6.25 拆包：代码自 mqtt_handler.py 单文件**逐字原样搬移**，禁止在此顺手优化；方法经
组合类 WindowControllerMQTTHandler 解析（单类形态与拆分前一致）。
"""
import logging
import json
import math
from homeassistant.components import mqtt
from ..const import (
    DOMAIN,
    ATTR_POSITION,
    DEVICE_TYPE_WINDOW_OPENER,
    PROTOCOL_HEAD,
    DEVICE_STATUS_OPEN,
    DEVICE_STATUS_CLOSED,
    DEVICE_TO_GATEWAY_MAPPING,
    get_device_display_name,
)
from ..utils import is_valid_device_sn, wind_lock_mode_or_unknown

# logger 名钉死为拆分前模块 __name__ 值——日志输出零差异（回归要求）
_LOGGER = logging.getLogger("custom_components.window_controller_gateway.mqtt_handler")


class _CtypeHandlersMixin:
    async def _handle_ctype_001(self, payload, ctype, data):
        """处理协议类型001：绑定网关"""
        # 检查是否包含设备信息（vesion, model等字段）或网关主动发起绑定请求
        # 两种情况都需要回复相同的 001 响应
        if "errcode" not in data:
            _LOGGER.debug("收到网关绑定请求/设备信息: %s, 版本: %s",
                         self.gateway_sn, data.get("vesion"))

            # 构建响应消息 - 按照协议要求回复001
            response_payload = {
                "head": PROTOCOL_HEAD,
                "ctype": "001",
                "id": payload.get("id", 0),
                "sn": self.gateway_sn,
                "data": {
                    "errcode": 0,
                    "uuid": self.instance_uuid
                }
            }

            # 发送响应到网关
            await mqtt.async_publish(
                self.hass,
                self.TOPIC_GATEWAY_REQ,
                json.dumps(response_payload),
                1,
                False
            )
            _LOGGER.info("发送网关绑定响应成功到主题: %s", self.TOPIC_GATEWAY_REQ)

            # 更新网关状态
            await self.device_manager.update_gateway_status("online")
        else:
            # 处理网关响应（可能来自其他系统）
            errcode = data.get("errcode", -1)
            if errcode == 0:
                _LOGGER.info("网关绑定成功: %s", self.gateway_sn)
                await self.device_manager.update_gateway_status("online")
            else:
                # v1.7.12（审计 B-11）：%d 格式化遇字符串 errcode 会抛 TypeError
                # 打断处理（006/007 同族）——统一 %s
                _LOGGER.error("网关绑定失败，错误码: %s", errcode)

    async def _handle_ctype_002(self, payload, ctype, data):
        """处理协议类型002：网关状态上报

        002 有两种场景：
        1. 网关定期状态上报：data 含 status/devices 等字段
        2. 解绑确认：HA 发 003(bind=0) 后网关回复 002(data={})，data 为空

        当 data 为空（无 status 字段）时，不覆盖网关已有的在线状态。
        """
        # v1.7.33（全量审计）：002 的 ack 提到处理器**最前**（先应答后处理）。
        # 契约是"002 必 ack"（CLAUDE.md 方向契约），而旧序把 ack 放在全量
        # 批处理之后：32 台设备含新增（注册表写 + 映射落盘）可跨过 5s 去重窗，
        # 网关重发的同一条 002 被判新消息整批重跑（双倍设备编号消耗、双倍
        # ack）。此处只表达"已收到"，与处理结果无关（异常另有日志），
        # 与 _handle_ctype_001"先应答后更新状态"同构。
        # v1.8.4（审计 B-3）：ack 仍排在最前（顺序契约不变），但**它的失败不得吃掉整帧**。
        # _send_ack 会 await mqtt.async_publish——broker 未就绪/发布异常时旧写法直接把这
        # 一帧的 update_gateway_status、全量 devices 批处理、属性更新全跳过，与上面那句
        # "与处理结果无关（异常另有日志）"正好相反。ack 没送达的后果只是网关多重发一次，
        # 而本帧状态已落；两者不可比，所以这里只记警告、继续处理。
        try:
            await self._send_ack("002", payload)
        except Exception as e:  # noqa: BLE001 - ack 失败不得影响本帧处理
            _LOGGER.warning("002 ack 发布失败（本帧照常处理，网关会重发）: %s", e)
        try:
            # 不使用 "unknown" 作为默认值，避免解绑确认的空 002 消息覆盖网关在线状态
            status = data.get("status")
            if status is not None:
                _LOGGER.debug("网关状态上报: %s", status)
                await self.device_manager.update_gateway_status(status)
            else:
                _LOGGER.debug("收到 002 消息（无 status 字段），不更新网关状态")
            # connected 状态已由 handle_gateway_response 在消息分发前设置，此处无需重复
            
            # 002 上报时不再重复触发 async_discover_gateway，
            # 网关已在配置流程中注册，重复发现只会浪费资源。
            # 发现流程由 _subscribe_topics 中收到未配置网关消息时触发。
            
            # 批量处理设备列表
            if "devices" in data:
                devices = data["devices"]
                
                # 使用集合记录已处理的设备，避免重复处理
                processed_sns = set()
                
                # 批量添加和更新任务
                add_tasks = []
                update_tasks = []
                
                for device_info in devices:
                    try:
                        device_sn = device_info.get("sn")
                        if not device_sn:
                            continue
                        # v1.7.12（审计 B-5）：嵌套 SN 与顶层 P0 守卫对齐——
                        # 固件可能以 JSON 数字上报，:115 的 startswith 遇 int
                        # 即 AttributeError（被逐条 except 吞成 error 日志，
                        # 该设备整条丢弃）。统一归一为 str 再入后续全部判定。
                        if isinstance(device_sn, bool) or not isinstance(
                            device_sn, (str, int, float)
                        ):
                            _LOGGER.warning("002 设备 SN 类型非法，跳过: %r", device_sn)
                            continue
                        device_sn = str(device_sn)

                        # v1.8.5（审计 G-7）：格式闸。本轮之前的判空在 str() 之前，
                        # 固件回 sn=0 时归一成 "0" 非空 → 幽灵设备入库。
                        # 与顶层发现分支（_protocol.py 的异网关 SN 闸）同一口径。
                        if not is_valid_device_sn(device_sn):
                            _LOGGER.warning("002 设备 SN 格式非法，跳过: %r", device_sn)
                            continue

                        # 跳过已处理的设备
                        if device_sn in processed_sns:
                            continue
                        processed_sns.add(device_sn)
                        
                        # 检查是否网关设备
                        if device_sn.startswith("1001"):
                            continue
                        
                        # 保留原有检查逻辑作为备份
                        # v1.7.61（10-05 审计 #7）：与 device_sn 同型守卫——
                        # 键存在值为 null 时 `.get(默认)` 返回 None，None.lower()
                        # 抛 AttributeError 被逐条 except 吞掉 ⇒ 该设备本帧既不
                        # 更新也不自动入库（已存在设备的 r_travel/电量随之冻结）。
                        _model = device_info.get("model")
                        _vesion = device_info.get("vesion")
                        device_model = _model.lower() if isinstance(_model, str) else ""
                        device_vesion = _vesion.lower() if isinstance(_vesion, str) else ""
                        if "gateway" in device_model or "网关" in device_model:
                            continue
                        elif "gateway" in device_vesion or "网关" in device_vesion:
                            continue
                        
                        # 检查设备是否已存在
                        existing_device = self.device_manager.get_device(device_sn)
                        if existing_device:
                            # 只更新状态，不重复添加
                            update_tasks.append(self._update_device_attributes(device_sn, device_info))
                        else:
                            # v1.6.12（第五轮审计 #4）：auto_discovery 接线——
                            # 取消勾选后 002 上报中的未知设备不再自动添加
                            # （已有设备状态更新不受影响；配对/手动添加路径不走这里）
                            if not self._auto_discovery_enabled():
                                _LOGGER.debug("auto_discovery 已关闭，跳过自动添加: %s", device_sn)
                                continue
                            # 检查设备是否已添加到其他网关中
                            if DEVICE_TO_GATEWAY_MAPPING in self.hass.data[DOMAIN]:
                                device_to_gateway_mapping = self.hass.data[DOMAIN][DEVICE_TO_GATEWAY_MAPPING]
                                if device_sn in device_to_gateway_mapping:
                                    existing_gateway_sn = device_to_gateway_mapping[device_sn]
                                    if existing_gateway_sn.lower() != self.gateway_sn.lower():
                                        _LOGGER.info("设备 %s 已添加到网关 %s，不自动添加到当前网关 %s", 
                                                    device_sn, existing_gateway_sn, self.gateway_sn)
                                        continue
                            
                            # 快速添加设备任务
                            add_tasks.append(self._quick_add_device(device_sn, device_info))
                            
                    except Exception as e:
                        _LOGGER.error("处理设备信息异常: %s", e, exc_info=True)
                
                # v1.7.33：ack 已在处理器首部下发（先应答后处理），此处不再重复。
                # 分批执行添加任务，每批10个设备
                if add_tasks:
                    await self._batch_process_tasks(add_tasks, "添加设备")
                
                # 分批执行更新任务，每批10个设备
                if update_tasks:
                    await self._batch_process_tasks(update_tasks, "更新设备状态")
        except KeyError as e:
            _LOGGER.error("缺少必要字段: %s, payload: %s", e, payload)
        except ValueError as e:
            _LOGGER.error("数据格式错误: %s, data: %s", e, data)
        except Exception as e:
            _LOGGER.error("处理002消息异常: %s", e, exc_info=True)

    async def _quick_add_device(self, device_sn, device_info):
        """快速添加设备 - 自动发现"""
        # 与手动配对（_handle_ctype_003）一致，用设备管理器的原子计数器分配 #NN 编号
        device_number = self.device_manager.allocate_device_number()
        device_name = get_device_display_name(self.gateway_sn, device_sn, device_number)
        
        # 直接调用设备管理器的添加方法（自动发现，不使用手动配对标记）
        await self.device_manager.add_device(device_sn, device_name, DEVICE_TYPE_WINDOW_OPENER)
        
        # 立即更新设备状态
        await self._update_device_attributes(device_sn, device_info)

    async def _update_device_attributes(self, device_sn, device_info):
        """更新设备属性"""
        attributes = {}
        
        # 提取设备属性
        # v1.6.12（第五轮审计 #2）：两处只捕 ValueError——battery/r_travel 为
        # null/list/dict 时 float()/int() 抛 TypeError，未捕获即杀死本协程，
        # 经 gather(return_exceptions=True) 静默吞没，该设备本轮"位置+状态"
        # 整体丢失且零日志。005 路径同类转换均捕 (ValueError, TypeError)，
        # 此处对齐（battery 失败不得连带丢失其后的 r_travel 更新）
        if "battery" in device_info:
            try:
                voltage = float(device_info["battery"]) / 10
                if not math.isfinite(voltage):
                    raise ValueError("电压值非有限数")
                attributes["voltage"] = voltage
                _LOGGER.debug("设备 %s 电池电压: %.1fV", device_sn, voltage)
            except (ValueError, TypeError, OverflowError) as e:
                _LOGGER.error("电池电压数据格式错误: %s, 值: %s", e, device_info["battery"])

        if "r_travel" in device_info:
            try:
                r_travel = int(device_info["r_travel"])
                attributes["r_travel"] = r_travel
                _LOGGER.debug("设备 %s 位置状态: %d", device_sn, r_travel)
            except (ValueError, TypeError, OverflowError) as e:
                _LOGGER.error("位置状态数据格式错误: %s, 值: %s", e, device_info["r_travel"])
        
        if attributes:
            # 只有当 r_travel 实际存在于上报数据中时才推导设备状态，
            # 避免仅有 battery/voltage 上报时将 None != 0 误判为 "open"
            if "r_travel" in attributes:
                device_status = DEVICE_STATUS_CLOSED if attributes["r_travel"] == 0 else DEVICE_STATUS_OPEN
                await self.device_manager.update_device_status(device_sn, device_status, attributes)
            else:
                # 没有 r_travel 时只更新属性，不覆盖设备的状态字段
                await self.device_manager.update_device_status(device_sn, None, attributes)
            self._notify_device_status_change(device_sn)

    async def _handle_ctype_003(self, payload, ctype, data):
        """处理协议类型003：绑定/解绑子设备响应

        协议流程：
        - 添加设备：HA 发 003(bind=1) → 网关回复 003(errcode=0, sn=设备SN)
        - 解绑设备：HA 发 003(bind=0) → 网关回复 003(errcode=0, sn=设备SN)

        注意：网关的【解绑】回复同样携带 data.sn（真实固件日志已确认，
        如 {"ctype":"003","errcode":0,"sn":"500534380262"}），
        因此不能仅凭 errcode==0 且有 sn 就判定为"绑定成功"——
        否则解绑回复会被误判为绑定，把刚删除的设备重新添加（设备"复活"）。

        区分绑定/解绑：
        - data.bind == 0 → 解绑，不添加设备
        - data.bind == 1 → 绑定，添加设备
        - 网关未回传 data.bind 时，用"设备是否已存在"推断：
          设备已存在 → 视为解绑（不添加）；设备不存在 → 视为绑定（添加）
        """
        errcode = data.get("errcode", -1)
        # 只信任 data.sn（子设备 SN）；顶层 payload.sn 是网关 SN，
        # 若网关未在 data 中回传子设备 SN，不把网关自身误当子设备添加
        device_sn = data.get("sn")
        # v1.7.18（第 7 轮审计 BUG-2）：与 002/005 的 B-5 同型归一补齐——
        # 部分固件 JSON 数字形态回包时 int 直接流入下方路径：get_device/
        # 手动删除列表恒 miss → 绑定/解绑方向误判；add_device 内
        # startswith/[-4:] TypeError 打断处理（配对确认静默丢失、会话滞留
        # 60s；解绑本地删除不生效→幽灵设备面）。
        if isinstance(device_sn, bool) or (
            device_sn is not None and not isinstance(device_sn, (str, int, float))
        ):
            _LOGGER.warning("003 设备 SN 类型非法，按无 SN 处理: %r", device_sn)
            device_sn = None
        elif device_sn is not None:
            device_sn = str(device_sn)
        # v1.8.5（审计 G-7）：格式闸。与 002/005 同口径——固件异常回包
        # sn=0/"0" 归一成非空串后被当作合法设备 SN，最终 add_device 出幽灵设备。
        # 置 None 后下方 `if errcode == 0 and device_sn:` 不成立，自然落进
        # 「设备操作成功但未返回设备SN」的既有无 SN 路径（不新增分支语义）。
        if device_sn is not None and not is_valid_device_sn(device_sn):
            _LOGGER.warning("003 设备 SN 格式非法，按无 SN 处理: %r", device_sn)
            device_sn = None
        bind_value = data.get("bind", None)
        # 按命令 id 匹配最近发出的 003 方向（发送端已记录 _bind_ops；
        # 记录为 (方向, 设备SN) 元组）。id 先经 _norm_cmd_id 归一：网关以
        # "42"/42.0 形态 echo 时精确 pop 不再 miss（v1.6.19 A-LOW4）。
        bind_record = self._bind_ops.pop(self._norm_cmd_id(payload.get("id")), None)
        bind_op = bind_record[0] if bind_record else None
        # 诊断日志：明确记录判定依据，便于定位"绑定成功但未添加"类问题
        _LOGGER.debug(
            "收到 003 回复: id=%s errcode=%s sn=%s data.bind=%s bind_op=%s "
            "手动删除列表=%s",
            payload.get("id"), errcode, device_sn, bind_value, bind_op,
            self.device_manager.is_device_manually_removed(device_sn) if device_sn else None,
        )

        if errcode == 0 and device_sn:
            # 判断是绑定还是解绑（优先级从高到低）：
            # 1. id 匹配发送端记录的命令方向（最可靠，不受设备存在性/时序影响）
            # 2. data.bind 字段（固件回复若带）
            # 3. 设备是否已存在（兜底：网关主动发起/命令未记录）
            existing_device = self.device_manager.get_device(device_sn)
            if bind_op == "unbind" or bind_value == 0:
                is_unbind = True
            elif bind_op == "bind" or bind_value == 1:
                is_unbind = False
            else:
                is_unbind = existing_device is not None

            if is_unbind:
                # 解绑成功：本地删除已由删除按钮流程（remove_device）完成，
                # 这里不需要重复处理，仅记录日志
                _LOGGER.info("设备解绑成功: %s", device_sn)
            else:
                # P0 设备复活守卫：晚到的绑定确认（自动/网关主动发起的重新绑定）
                # 不得复活已手动删除的设备。
                # 但【手动配对确认】（bind_op == "bind"，来自 start_pairing 命令）
                # 是用户主动操作，应允许重新添加被删设备——与 add_device 的
                # is_manual_pairing=True 语义一致。否则手动删除过的设备将永远
                # 无法通过配对重新添加（2026-08-27 实测 bug）。
                if self.device_manager.is_device_manually_removed(device_sn):
                    if bind_op == "bind":
                        _LOGGER.debug(
                            "设备 %s 在手动删除列表中，本次为手动配对确认，"
                            "允许重新添加并从删除列表移除",
                            device_sn,
                        )
                        self.device_manager._manually_removed_devices.discard(device_sn)
                        self.device_manager._save_manually_removed_devices()
                    else:
                        _LOGGER.debug(
                            "设备 %s 在手动删除列表中，拒绝晚到的绑定确认（防止设备复活）",
                            device_sn,
                        )
                        return
                # 绑定成功，添加设备
                device_number = self.device_manager.allocate_device_number()
                device_name = get_device_display_name(self.gateway_sn, device_sn, device_number)
                # 手动配对时使用 is_manual_pairing=True，跳过手动删除列表检查
                await self.device_manager.add_device(device_sn, device_name, DEVICE_TYPE_WINDOW_OPENER, is_manual_pairing=True)
                # v1.6.17（联审 F2）：固件在 003 绑定确认后立即推送设备
                # 状态（新设备 position=-1 视图）；插件的 WS device_update
                # 挂在 update_device_status 漏斗，此处直接 add_device 不
                # 经过它——手动配对的新设备要等下一次 002/005 上报才出现
                # 在小程序。补一次监听器通知（设备刚入缓存、无属性，
                # 视图字段全 -1，与固件推送语义等价）。
                if device_sn in self.device_manager.devices:
                    self.device_manager._notify_status_listeners(device_sn)
                # v1.6.11（审计 #2）：配对会话退出必须限定在"我们自己发起的
                # 绑定确认"（bind_op=="bind"，_bind_ops 记账过）。此前无条件
                # 退出：迟到/非请求的 003（id 无记账、设备恰好不在列表）会
                # cancel 掉**当前**配对会话的定时器并提前关窗。设备添加本身
                # 保留（收到 errcode=0 的绑定确认即事实），会话与状态恢复
                # 交由真正的发起方确认或超时回调处理
                if bind_op == "bind":
                    # 配对成功后立即退出配对模式，UI 可以立刻从"配对中"恢复
                    # 同时取消配对超时定时器，避免超时回调冗余触发
                    if self.pairing_timeout_handle:
                        self.pairing_timeout_handle.cancel()
                        self.pairing_timeout_handle = None
                    self.pairing_active = False
                    self._notify_status_change()
                    # v1.6.10（审计 N1，P2）：状态字段还停在 start_pairing 置的
                    # "pairing"——超时定时器刚被 cancel，_TIMEOUT/_handle_ctype_001
                    # 两条恢复路径都不再触发，「配对中」要等下次 002 心跳才消失。
                    # 成功路径就地恢复（收到绑定确认即在线）
                    await self.device_manager.update_gateway_status("online")
                _LOGGER.info("设备绑定成功: %s, 名称: %s (会话退出=%s)",
                             device_sn, device_name, bind_op == "bind")
        elif errcode == 0 and not device_sn:
            _LOGGER.warning("设备操作成功但未返回设备SN: %s", payload)
        else:
            # 错误码7可能表示通讯距离不够，不记录为错误
            if errcode == 7:
                _LOGGER.debug("设备操作失败，错误码: %s, SN: %s (可能是通讯距离不够)", errcode, device_sn)
            else:
                _LOGGER.warning("设备操作失败，错误码: %s, SN: %s", errcode, device_sn)

    async def _handle_ctype_004(self, payload, ctype, data):
        """处理协议类型004：设备控制响应

        004 是 HA 主动下发的命令，网关回复 errcode:0 表示已收到。
        HA 不需要回复确认，否则会被网关误认为是新命令导致循环。
        """
        # 收到网关回复（命令不启用重发机制）

        errcode = data.get("errcode", -1)
        device_sn = data.get("sn")
        if errcode == 0:
            if device_sn:
                _LOGGER.debug("设备控制成功: %s", device_sn)
            else:
                _LOGGER.debug("设备控制成功，但未返回设备SN")
        else:
            if errcode == 7:
                _LOGGER.debug("设备控制失败，错误码: %s, SN: %s (可能是通讯距离不够)", errcode, device_sn)
            else:
                _LOGGER.warning("设备控制失败，错误码: %s, SN: %s", errcode, device_sn)

    async def _handle_ctype_005(self, payload, ctype, data):
        """处理协议类型005：设备上报

        v1.6.12（第五轮审计 #1）：处理体必须整体包 try/except——此前解析/更新
        阶段（如 data.attrs 非列表、元素为 null）抛出的异常会杀死协程并跳过
        末尾 ack，网关按未确认重发：5s 内重发被去重吞掉（仍无 ack）、5s 后
        重发再次崩溃，形成毒消息无限重传环，该设备状态永久冻结。与 002
        （try 包裹 + ack 恒达）对称加固，ack 移出异常区保证必发。
        """
        try:
            await self._handle_ctype_005_inner(payload, ctype, data)
        except Exception as e:
            _LOGGER.error("处理005消息异常: %s, data: %r", e, data, exc_info=True)
        finally:
            # 回复 005 确认，告知网关已收到设备上报，避免网关重复重发
            await self._send_ack("005", payload)

    async def _handle_ctype_005_inner(self, payload, ctype, data):
        """005 处理体（异常由 _handle_ctype_005 兜底，ack 由其 finally 保证）"""
        device_sn = data.get("sn")
        # v1.7.12（审计 B-5）：嵌套 SN 类型归一，与 002/顶层 P0 守卫同型
        if isinstance(device_sn, bool) or (
            device_sn is not None and not isinstance(device_sn, (str, int, float))
        ):
            _LOGGER.warning("005 设备 SN 类型非法，忽略本条: %r", device_sn)
            return
        if device_sn is not None:
            device_sn = str(device_sn)
        # v1.8.5（审计 G-7）：格式闸。`if device_sn:` 挡不住 sn=0 → "0"，
        # 未知设备会经 update_device_status 的"不存在则自动添加"分支入库。
        # 直接 return：ack 由外层 _handle_ctype_005 的 finally 保证照发。
        if device_sn is not None and not is_valid_device_sn(device_sn):
            _LOGGER.warning("005 设备 SN 格式非法，忽略本条: %r", device_sn)
            return
        if device_sn:
            # v1.7.12（第 6 轮审计 B-6）：auto_discovery 门禁补齐——002 路径
            # 有门（:135），但未知设备的首条 005 经 update_device_status 的
            # "不存在则自动添加"分支无条件入库，勾选关闭后照样冒设备。
            # 仅拦"未知设备自动添加"，已登记设备的正常上报不受影响；
            # ack 由外层 finally 保证照发（协议契约：005 必 ack）。
            if (self.device_manager.get_device(device_sn) is None
                    and not self._auto_discovery_enabled()):
                _LOGGER.info(
                    "auto_discovery 已关闭，忽略未知设备上报（ack 照发）: %s",
                    device_sn,
                )
                return
            # 解析设备上报的状态
            # 不使用 "unknown" 作为默认值，避免仅上报电池电压时覆盖设备已有的开/关状态。
            # 当 status 为 None 时，update_device_status 不会覆盖设备的状态字段，
            # 与 _update_device_attributes（002 处理器）的逻辑保持一致。
            status = data.get("status")
            attributes = {}
            
            # 提取上报的属性
            if "position" in data:
                attributes[ATTR_POSITION] = data["position"]
            if "battery" in data:
                # 统一存储为 voltage，与网关上报保持一致
                battery = data["battery"]
                try:
                    # 转换为浮点数并除以10（如105 → 10.5V）
                    voltage = float(battery) / 10
                    # v1.6.19（第六轮审计 A-HIGH2 源头卫生）：JSON `1e999`
                    # 解析为 float("inf") 不抛错，inf/10 仍是 inf——必须在入库
                    # 前拦截，否则视图层与 HA 状态机承接非有限数（视图层已另
                    # 补 isfinite 钳制双保险）。
                    if not math.isfinite(voltage):
                        raise ValueError("电压值非有限数")
                    attributes["voltage"] = voltage
                    _LOGGER.debug("设备 %s 电池电压: %.1fV", device_sn, voltage)
                except (ValueError, TypeError, OverflowError) as e:
                    _LOGGER.error("设备 %s 电池电压数据格式错误: %s, 值: %s", device_sn, e, battery)
            if "state" in data:
                attributes["state"] = data["state"]
            
            # 处理attrs数组
            if "attrs" in data:
                attrs = data["attrs"]
                # v1.7.12（审计 B-7）：attrs 非列表/元素非对象逐条守卫——
                # 单个毒元素（null/字符串混入）旧版经 attr.get 抛 AttributeError
                # 连坐整批 attrs 丢弃（外层兜底只保 ack），现逐条跳过留痕。
                if not isinstance(attrs, list):
                    _LOGGER.warning("005 attrs 非数组，忽略: %r", attrs)
                    attrs = []
                for attr in attrs:
                    if not isinstance(attr, dict):
                        _LOGGER.warning("005 attrs 含非对象元素，跳过: %r", attr)
                        continue
                    attribute = attr.get("attribute")
                    value = attr.get("value")
                    
                    if attribute == "voltage":
                        # 转换电压值，105表示10.5v
                        try:
                            voltage = float(value) / 10
                            if not math.isfinite(voltage):
                                raise ValueError("电压值非有限数")  # v1.6.19 A-HIGH2
                            attributes["voltage"] = voltage
                        except (ValueError, TypeError, OverflowError) as e:
                            _LOGGER.error("设备 %s 电压属性格式错误: %s, 值: %s", device_sn, e, value)
                    elif attribute == "r_travel":
                        # 处理窗户状态，0表示关闭，其他表示打开
                        try:
                            travel_value = int(value)
                            attributes["r_travel"] = travel_value
                            # 根据r_travel设置状态
                            if travel_value == 0:
                                status = DEVICE_STATUS_CLOSED
                            else:
                                status = DEVICE_STATUS_OPEN
                        except (ValueError, TypeError, OverflowError) as e:
                            _LOGGER.error("设备 %s 位置状态格式错误: %s, 值: %s", device_sn, e, value)
                    elif attribute == "rwp_wind_lock_mode":
                        # 风锁模式上报：0=内倒模式，1=平开模式
                        # v1.8.5（审计 G-9）：本行此前**原样存 value**，而同族的
                        # speed/strength 至少做了 int 归一——域表外（2、999、"1"、
                        # dict）会以"合法数字"形态经 device_list / device_update /
                        # 云快照三条通道一起分发，读侧再也分不出真值与垃圾。
                        # 归一＋域判收在 utils.wind_lock_mode_or_unknown（判据取自
                        # CONTROL_ATTR_DOMAINS，与出站 control 同一份天花板）。
                        mode = wind_lock_mode_or_unknown(value)
                        if mode == -1:
                            _LOGGER.warning(
                                "设备 %s 风锁模式上报越界，按未知处理: 原值=%r", device_sn, value)
                        attributes["wind_lock_mode"] = mode
                        mode_name = {0: "内倒模式", 1: "平开模式"}.get(mode, "未知")
                        _LOGGER.info("设备 %s 风锁模式确认: %s (值=%s)", device_sn, mode_name, value)
                    elif attribute == "rwp_winact_speed":
                        # 开窗速度上报：0-100
                        try:
                            speed = int(value)
                            attributes["winact_speed"] = speed
                            _LOGGER.debug("设备 %s 开窗速度上报: %d", device_sn, speed)
                        except (ValueError, TypeError, OverflowError) as e:
                            _LOGGER.error("设备 %s 开窗速度属性格式错误: %s, 值: %s", device_sn, e, value)
                    elif attribute == "rwp_winact_strength":
                        # 开窗力度上报：0-100
                        try:
                            strength = int(value)
                            attributes["winact_strength"] = strength
                            _LOGGER.debug("设备 %s 开窗力度上报: %d", device_sn, strength)
                        except (ValueError, TypeError, OverflowError) as e:
                            _LOGGER.error("设备 %s 开窗力度属性格式错误: %s, 值: %s", device_sn, e, value)
            
            # 更新设备状态
            await self.device_manager.update_device_status(device_sn, status, attributes)
            # 通知设备状态变化，触发传感器实体更新
            self._notify_device_status_change(device_sn)
            _LOGGER.debug("设备上报处理完成: %s", device_sn)

    async def _handle_ctype_006(self, payload, ctype, data):
        """处理协议类型006：HA 主动发起命令的网关回复

        006 是 HA 主动下发的命令，网关回复 errcode:0 表示已收到。
        HA 不需要回复确认（命令不启用重发机制）。
        """
        # 收到网关回复（命令不启用重发机制）

        errcode = data.get("errcode", -1)
        if errcode == 0:
            _LOGGER.debug("006 命令执行成功: %s", data)
        else:
            _LOGGER.warning("006 命令执行失败，错误码: %s, data: %s", errcode, data)

    async def _handle_ctype_007(self, payload, ctype, data):
        """处理协议类型007：HA 主动发起命令的网关回复

        007 是 HA 主动下发的命令，网关回复 errcode:0 表示已收到。
        HA 不需要回复确认（命令不启用重发机制）。
        """
        # 收到网关回复（命令不启用重发机制）

        errcode = data.get("errcode", -1)
        if errcode == 0:
            _LOGGER.debug("007 命令执行成功: %s", data)
        else:
            _LOGGER.warning("007 命令执行失败，错误码: %s, data: %s", errcode, data)
