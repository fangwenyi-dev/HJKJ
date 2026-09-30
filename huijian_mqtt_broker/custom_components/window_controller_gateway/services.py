"""服务处理与注册 - 开窗器网关的服务处理器与服务注册"""
import logging
import re
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

# v1.6.3："调用即失败"场景必须让 REST 返回非 2xx，而不是记完日志静默
# return（前端收到 200 弹「已发送」假成功 toast）。
# v1.6.4 纠偏（上游取证）：REST 服务视图仅把 vol.Invalid/ServiceNotFound
# 映射为 400，ServiceValidationError（HomeAssistantError 子类）实测落到
# aiohttp 兜底 500——对前端 "!resp.ok → 错误 toast" 语义等价，且 WS/自动化
# 侧保留 HomeAssistantError 正确语义；代价是 HA 日志会记 traceback。
# ServiceValidationError 自 HA 2024.3 提供；测试假环境缺失时退化为 Exception。
try:
    from homeassistant.exceptions import ServiceValidationError
except ImportError:  # pragma: no cover
    class ServiceValidationError(Exception):  # type: ignore[no-redef]
        def __init__(self, message, **kwargs):
            super().__init__(message)

from .const import (
    DOMAIN,
    CONF_GATEWAY_SN,
    SERVICE_START_PAIRING,
    SERVICE_REFRESH_DEVICES,
    SERVICE_RENAME_DEVICE,
    SERVICE_TRANSFER_DEVICE,
    ATTR_NEW_NAME,
    DEVICE_TO_GATEWAY_MAPPING,
    GATEWAY_PAIRING_TIMEOUT,
    POSITION_MIN,
    POSITION_MAX,
    POSITION_CAPABLE_SN_PREFIXES,
    DEVICE_TYPE_WINDOW_OPENER,
    COMMAND_SET_POSITION,
)

# 导入辅助函数
from .utils import find_gateway_by_device_id, find_device_by_device_id

_LOGGER = logging.getLogger(__name__)


def _reject_bool_position(value):
    """拒绝布尔位置参数

    Python 中 bool 是 int 子类，`vol.Coerce(int)` 会把 True 静默转为 1，
    必须在 Coerce 之前显式拒绝，避免 `position: true` 被当作位置 1 下发。
    """
    if type(value) is bool:
        raise vol.Invalid("position 不能是布尔值")
    return value


async def handle_start_pairing(hass: HomeAssistant, call: ServiceCall) -> None:
    """处理开始配对服务调用"""
    device_id = call.data.get("device_id")
    duration = call.data.get("duration", GATEWAY_PAIRING_TIMEOUT)

    if not device_id:
        # v1.6.4：假成功根治（同 check_gateway_status），REST 非 2xx
        raise ServiceValidationError("开始配对：未指定 device_id")

    _LOGGER.info("收到开始配对请求，设备ID: %s，持续时间: %d秒", device_id, duration)
    
    gateway_data, gateway_sn = find_gateway_by_device_id(hass, device_id)
    if not gateway_data:
        _LOGGER.error("未找到设备ID %s 对应的网关", device_id)
        raise ServiceValidationError(f"未找到设备ID {device_id} 对应的网关")

    mqtt_handler = gateway_data.get("mqtt_handler")
    if not mqtt_handler:
        _LOGGER.error("未找到MQTT处理器")
        raise ServiceValidationError("未找到该网关的 MQTT 处理器")

    try:
        # P1 修复：使用 mqtt_handler.pairing_timeout_handle 统一管理配对超时，
        # 确保服务调用和按钮按下共享同一个超时句柄，避免重复超时回调。
        if mqtt_handler.pairing_timeout_handle:
            mqtt_handler.pairing_timeout_handle.cancel()
            mqtt_handler.pairing_timeout_handle = None

        success = await mqtt_handler.send_command(mqtt_handler.gateway_sn, "start_pairing")
        if not success:
            # v1.6.4：网关离线导致命令未送达——以前静默 return，前端 200
            # 弹「配对模式已启动（60秒）」假成功并停留在"配对中"徽标
            _LOGGER.error("发送配对命令失败")
            raise ServiceValidationError("发送配对命令失败：网关可能离线")

        mqtt_handler.pairing_active = True
        mqtt_handler._notify_status_change()

        _pairing_status_task = hass.async_create_task(
            gateway_data["device_manager"].update_gateway_status("pairing")
        )
        # 任务引用存入条目 _bg_tasks，供卸载时统一取消
        gateway_data.setdefault("_bg_tasks", []).append(_pairing_status_task)

        _LOGGER.info("已为网关 %s 发起配对，持续时间: %d秒", gateway_sn, duration)

        def pairing_timeout():
            mqtt_handler.pairing_timeout_handle = None
            mqtt_handler.pairing_active = False
            mqtt_handler._notify_status_change()
            _status_restore_task = hass.async_create_task(
                gateway_data["device_manager"].update_gateway_status(
                    "online" if mqtt_handler.connected else "offline"
                )
            )
            # 任务引用存入条目 _bg_tasks，供卸载时统一取消
            gateway_data.setdefault("_bg_tasks", []).append(_status_restore_task)
            _LOGGER.info("配对模式已超时，恢复正常状态")

        mqtt_handler.pairing_timeout_handle = hass.loop.call_later(duration, pairing_timeout)
    except ServiceValidationError:
        # v1.6.9（外部审计确认，高严重度）：:88 的 raise 位于 try 内，此前被
        # 末尾 except Exception 捕获仅记日志 → REST 200 → Web 弹「配对模式已
        # 启动」假成功，而 pairing_active/超时定时器根本没设置。这是 v1.6.4
        # 假成功根治漏掉的一条路径（rename 已有同款保护，此处对齐）
        # v1.6.10（审计 B2）：上抛前先清理上次配对残留——本 try 开头已把旧
        # 超时定时器 cancel，若此时 pairing_active 仍为 True 将无人复位，
        # 网关永久卡「配对中」
        mqtt_handler.abort_pairing_if_active()
        raise
    except (ConnectionError, TimeoutError) as e:
        # v1.6.9：同族收口——send_command 抛连接类异常同样=未送达，如实报错
        mqtt_handler.abort_pairing_if_active()  # v1.6.10（审计 B2）
        _LOGGER.error("网关 %s 连接或超时错误: %s", gateway_sn, e)
        raise ServiceValidationError(f"启动配对失败：网关连接或超时（{e}）") from e
    except (KeyError, AttributeError) as e:
        mqtt_handler.abort_pairing_if_active()  # v1.6.10（审计 B2）
        _LOGGER.error("网关 %s MQTT处理器未找到或配置错误: %s", gateway_sn, e)
        raise ServiceValidationError(f"启动配对失败：处理器配置错误（{e}）") from e
    except Exception as e:
        mqtt_handler.abort_pairing_if_active()  # v1.6.10（审计 B2）
        _LOGGER.error("网关 %s 执行配对命令失败: %s", gateway_sn, e)
        raise ServiceValidationError(f"启动配对失败：{e}") from e

async def handle_rename_device(hass: HomeAssistant, call: ServiceCall) -> None:
    """处理重命名设备服务调用"""
    device_id = call.data.get("device_id")
    new_name = call.data.get(ATTR_NEW_NAME)

    if not device_id or not new_name:
        # v1.6.4：假成功根治——参数不完整/设备不存在/重命名返回 False
        # 都必须让调用方（Web UI/自动化）收到非 2xx，
        # 以前静默 return 时前端弹「重命名成功」假成功
        _LOGGER.error("重命名设备服务调用失败：参数不完整")
        raise ServiceValidationError("重命名：device_id 与新名称均不可为空")

    # P0 修复：使用 find_device_by_device_id 解析出设备 SN，
    # 而非直接把 device_id（可能是 HA 设备 ID）传给 rename_device。
    device, gateway_data, gateway_sn = find_device_by_device_id(hass, device_id)
    if not device or not gateway_data:
        _LOGGER.error("未找到设备ID %s 对应的设备", device_id)
        raise ServiceValidationError(f"未找到设备ID {device_id} 对应的设备")

    device_manager = gateway_data.get("device_manager")
    if not device_manager:
        _LOGGER.error("未找到设备管理器")
        raise ServiceValidationError("未找到该网关的设备管理器")

    try:
        device_sn = device["sn"]
        success = await device_manager.rename_device(device_sn, new_name)
        if success:
            _LOGGER.info("设备 %s 已重命名为 %s", device_sn, new_name)
        else:
            # rename_device 返回 False（名称超 50 字符/设备不存在等于线等）
            raise ServiceValidationError(f"重命名设备 {device_sn} 失败（名称过长或设备不存在）")
    except ServiceValidationError:
        raise
    except Exception as e:
        _LOGGER.error("设备 %s 重命名失败: %s", device_id, e)
        raise ServiceValidationError(f"重命名失败: {e}") from e

async def handle_refresh_devices(hass: HomeAssistant, call: ServiceCall) -> None:
    """处理刷新设备服务调用

    协议说明：002 是网关主动发起的上报，HA 无法主动触发设备发现。
    设备列表更新完全依赖网关主动发送 002 消息，HA 被动接收。
    """
    device_id = call.data.get("device_id")

    if not device_id:
        _LOGGER.error("刷新设备服务调用失败：未指定设备ID")
        raise ServiceValidationError("刷新设备：未指定 device_id")

    gateway_data, gateway_sn = find_gateway_by_device_id(hass, device_id)
    if not gateway_data:
        _LOGGER.error("未找到设备ID %s 对应的网关", device_id)
        raise ServiceValidationError(f"未找到设备ID {device_id} 对应的网关")

    # 协议说明：002 是网关主动发起，HA 无法主动触发设备发现
    # 设备列表更新依赖网关定期主动上报 002 消息
    _LOGGER.info(
        "网关 %s 的设备列表更新依赖网关主动上报（002），HA 无法主动触发。"
        "请等待网关下一次自动上报，或重启网关触发上报。",
        gateway_sn
    )

async def handle_set_position(hass: HomeAssistant, call: ServiceCall) -> None:
    """处理设置位置服务调用 - 优化版，减少阻塞"""
    device_id = call.data.get("device_id")
    position = call.data.get("position")

    if not device_id:
        _LOGGER.error("设置位置服务调用失败：未指定设备ID")
        raise ServiceValidationError("设置位置：未指定 device_id")

    if position is None:
        _LOGGER.error("设置位置服务调用失败：未指定位置")
        raise ServiceValidationError("设置位置：未指定 position")

    # 加强位置参数验证（type() is int 排除 bool：Python 中 bool 是 int 子类，
    # isinstance(True, int) 为 True，会导致 position: true 被静默转为位置 1）
    if type(position) is not int or position < 0 or position > 100:
        _LOGGER.error("设置位置服务调用失败：位置必须是0-100之间的整数")
        raise ServiceValidationError("设置位置：position 必须是 0-100 之间的整数")

    _LOGGER.info("收到设置位置请求，设备ID: %s，位置: %d", device_id, position)
    
    device, gateway_data, gateway_sn = find_device_by_device_id(hass, device_id)
    if not device or not gateway_data:
        _LOGGER.error("未找到设备ID %s 对应的设备", device_id)
        raise ServiceValidationError(f"未找到设备ID {device_id} 对应的设备")

    # v1.7.33（全量审计）：机型百分比能力闸 + 设备类型闸——与 cover.py 实体侧
    # 同规则（"绝不把无效的 w_travel 指令打到 LoRa 空口"）。此前 Web 面板滑块/
    # 自动化/REST 直调绕过实体侧校验：无百分比能力机型上滑块可拖、指令照发、
    # HTTP 200 报成功而硬件无动作（假成功）。能力集合单一真源 = const。
    _dev_sn = str(device.get("sn") or "")
    if _dev_sn[:4] not in POSITION_CAPABLE_SN_PREFIXES:
        _LOGGER.error("设置位置失败：机型 %s 不支持百分比定位", _dev_sn[:4] or "未知")
        raise ServiceValidationError(
            f"设置位置失败：机型 {_dev_sn[:4] or '未知'} 不支持百分比定位")
    if device.get("type") and device.get("type") != DEVICE_TYPE_WINDOW_OPENER:
        _LOGGER.error("设置位置失败：目标设备 %s 不是开窗器", _dev_sn)
        raise ServiceValidationError("设置位置失败：目标设备不是开窗器")

    mqtt_handler = gateway_data.get("mqtt_handler")
    if not mqtt_handler:
        _LOGGER.error("未找到MQTT处理器")
        raise ServiceValidationError("未找到该网关的 MQTT 处理器")

    # v1.6.9（外部审计确认，中严重度）：原为 fire-and-forget 且内部把
    # ConnectionError/TimeoutError/Exception 全吞成日志——broker 掉线/设备离线
    # 时前端永远收 200「已提交」假成功。改为同步 await、失败如实抛错
    # （send_command 为 QoS1 发布语义，返回 False 即链路未送达，无 ack 误判）
    try:
        success = await mqtt_handler.send_command(
            device["sn"],
            "set_position",
            {"position": position}
        )
    except (ConnectionError, TimeoutError) as e:
        _LOGGER.error("设备 %s 设置位置连接或超时: %s", device["sn"], e)
        raise ServiceValidationError(f"设置位置失败：网关连接或超时（{e}）") from e
    except Exception as e:
        _LOGGER.error("设置设备位置失败: %s", e)
        raise ServiceValidationError(f"设置位置失败：{e}") from e
    if not success:
        _LOGGER.error("设备 %s 设置位置命令未送达", device["sn"])
        raise ServiceValidationError("设置位置失败：命令未送达（网关或设备离线）")
    _LOGGER.info("已为设备 %s 设置位置: %d", device["sn"], position)

async def handle_check_gateway_status(hass: HomeAssistant, call: ServiceCall) -> None:
    """处理检查网关状态服务调用"""
    device_id = call.data.get("device_id")
    gateway_sn = call.data.get("gateway_sn")

    if not device_id and not gateway_sn:
        _LOGGER.error("检查网关状态服务调用失败：未指定设备ID或网关SN")
        raise ServiceValidationError("检查网关状态：未指定 device_id 或 gateway_sn")

    # 优先使用 device_id 查找，其次使用 gateway_sn
    gateway_data = None
    resolved_sn = None
    if device_id:
        gateway_data, resolved_sn = find_gateway_by_device_id(hass, device_id)
    elif gateway_sn:
        # 通过 gateway_sn 直接查找
        for _entry_id, data in hass.data.get(DOMAIN, {}).items():
            if isinstance(data, dict) and data.get("gateway_sn", "").lower() == gateway_sn.lower():
                gateway_data = data
                resolved_sn = gateway_sn
                break

    if not gateway_data:
        target = device_id or gateway_sn
        _LOGGER.error("未找到对应的网关: %s", target)
        # v1.6.3：抛错使 REST 返回非 2xx（v1.6.4 取证：实际为 500 非 400），
        # 前端如实提示；静默 return 会让 Web UI 弹「状态检查已发送」假成功
        raise ServiceValidationError(f"未找到对应的网关: {target}")

    _LOGGER.info("收到检查网关状态请求，网关SN: %s", resolved_sn)
    
    try:
        is_connected = await gateway_data["mqtt_handler"].check_connection()
        gateway_info = gateway_data["device_manager"].get_gateway_info()
        _LOGGER.info("网关 %s 状态检查结果: 在线=%s, 信息=%s", 
                    gateway_info.get("name"), is_connected, gateway_info)
    except (ConnectionError, TimeoutError) as e:
        # v1.6.10（审计 B3）：执行异常此前仅日志 → 200「已发送」假成功，
        # 与 v1.6.9 契约同族收口（is_connected=False 是合法检查结果，不抛）
        _LOGGER.error("网关 %s 连接或超时错误: %s", resolved_sn, e)
        raise ServiceValidationError(f"状态检查失败：网关连接或超时（{e}）") from e
    except (KeyError, AttributeError) as e:
        _LOGGER.error("网关 %s 配置错误: %s", resolved_sn, e)
        raise ServiceValidationError(f"状态检查失败：配置错误（{e}）") from e
    except Exception as e:
        _LOGGER.error("检查网关状态失败: %s", e)
        raise ServiceValidationError(f"状态检查失败：{e}") from e

async def handle_migrate_devices(hass: HomeAssistant, call: ServiceCall) -> None:
    """完善的设备迁移服务"""
    old_gateway_sn = call.data.get("old_gateway_sn")  # 旧网关SN
    new_gateway_sn = call.data.get("new_gateway_sn")  # 新网关SN
    remove_old_gateway = call.data.get("remove_old_gateway", False)  # 是否移除旧网关

    # 添加更严格的参数验证
    if not isinstance(old_gateway_sn, str) or len(old_gateway_sn) < 10:
        _LOGGER.error("旧网关SN格式无效: %s", old_gateway_sn)
        raise ServiceValidationError("迁移设备：old_gateway_sn 格式无效，长度必须 >= 10")
    
    if not isinstance(new_gateway_sn, str) or len(new_gateway_sn) < 10:
        _LOGGER.error("新网关SN格式无效: %s", new_gateway_sn)
        raise ServiceValidationError("迁移设备：new_gateway_sn 格式无效，长度必须 >= 10")
    
    # 验证SN格式：与 config_flow.py 的 validate_gateway_sn 保持一致，允许所有字母和数字
    if not re.match(r'^[a-zA-Z0-9]+$', old_gateway_sn):
        _LOGGER.error("旧网关SN格式无效，只允许字母和数字: %s", old_gateway_sn)
        raise ServiceValidationError("迁移设备：old_gateway_sn 只允许字母和数字")
    
    if not re.match(r'^[a-zA-Z0-9]+$', new_gateway_sn):
        _LOGGER.error("新网关SN格式无效，只允许字母和数字: %s", new_gateway_sn)
        raise ServiceValidationError("迁移设备：new_gateway_sn 只允许字母和数字")
    
    if not isinstance(remove_old_gateway, bool):
        _LOGGER.error("remove_old_gateway参数必须是布尔值: %s", remove_old_gateway)
        raise ServiceValidationError("迁移设备：remove_old_gateway 必须是布尔值")

    # 检查新旧网关是否相同
    if old_gateway_sn.lower() == new_gateway_sn.lower():
        _LOGGER.error("新旧网关不能相同: %s", old_gateway_sn)
        raise ServiceValidationError("迁移设备：新旧网关不能相同")

    _LOGGER.info("开始设备迁移，新网关: %s, 旧网关: %s", new_gateway_sn, old_gateway_sn)

    # 1. 验证网关存在
    def find_gateway_entry(gateway_sn):
        for entry in hass.config_entries.async_entries(DOMAIN):
            if CONF_GATEWAY_SN in entry.data and entry.data[CONF_GATEWAY_SN].lower() == gateway_sn.lower():
                return entry
        return None

    old_gateway_entry = find_gateway_entry(old_gateway_sn)
    new_gateway_entry = find_gateway_entry(new_gateway_sn)

    if not old_gateway_entry or not new_gateway_entry:
        _LOGGER.error("网关不存在，旧网关SN: %s, 新网关SN: %s", old_gateway_sn, new_gateway_sn)
        raise ServiceValidationError(f"迁移设备：网关不存在，旧网关SN: {old_gateway_sn}, 新网关SN: {new_gateway_sn}")

    _LOGGER.info("找到网关条目，旧网关: %s, 新网关: %s", old_gateway_entry.entry_id, new_gateway_entry.entry_id)

    # 2. 获取设备管理器
    old_manager = None
    new_manager = None

    if old_gateway_entry.entry_id in hass.data[DOMAIN]:
        old_manager = hass.data[DOMAIN][old_gateway_entry.entry_id].get("device_manager")

    if new_gateway_entry.entry_id in hass.data[DOMAIN]:
        new_manager = hass.data[DOMAIN][new_gateway_entry.entry_id].get("device_manager")

    if not old_manager or not new_manager:
        _LOGGER.error("设备管理器不存在")
        raise ServiceValidationError("迁移设备：设备管理器不存在")

    # 3. 执行迁移
    try:
        # 发送迁移开始事件
        hass.bus.async_fire(
            f"{DOMAIN}_migration_progress",
            {
                "old_gateway_sn": old_gateway_sn,
                "new_gateway_sn": new_gateway_sn,
                "status": "started",
                "progress": 0,
                "message": "开始设备迁移"
            }
        )
        
        # 使用安全迁移方法，支持旧网关不在线的情况
        success, migrated_devices = await new_manager.safe_migrate_devices(
            old_gateway_sn,
            new_gateway_sn
        )

        if success:
            # 发送迁移完成事件
            hass.bus.async_fire(
                f"{DOMAIN}_migration_progress",
                {
                    "old_gateway_sn": old_gateway_sn,
                    "new_gateway_sn": new_gateway_sn,
                    "status": "devices_migrated",
                    "progress": 50,
                    "message": "设备迁移完成，开始验证实体"
                }
            )
            
            _LOGGER.info("设备迁移成功")
            
            # 直接发送迁移成功事件
            hass.bus.async_fire(
                f"{DOMAIN}_migration_progress",
                {
                    "old_gateway_sn": old_gateway_sn,
                    "new_gateway_sn": new_gateway_sn,
                    "status": "verified",
                    "progress": 75,
                    "message": "设备迁移完成"
                }
            )
            
            # 5. 不再重新加载平台，而是发送事件让前端刷新
            try:
                _LOGGER.info("发送迁移完成事件，通知前端刷新")
                
                # 发送事件通知前端刷新
                hass.bus.async_fire(
                    f"{DOMAIN}_devices_migrated",
                    {
                        "old_gateway_sn": old_gateway_sn,
                        "new_gateway_sn": new_gateway_sn,
                        "success": True,
                        "device_count": len(migrated_devices)
                    }
                )
                
                # P1 修复：移除不存在的 homeassistant/reload_entities 事件（死代码），
                # 该事件并非 HA 标准事件，不会触发任何 UI 刷新。
                _LOGGER.info("已通知前端刷新，用户可能需要手动刷新页面或等待自动更新")
                
            except Exception as reload_error:
                _LOGGER.error("发送刷新事件失败: %s", reload_error)

            # 6. 可选：卸载旧网关
            if remove_old_gateway:
                try:
                    _LOGGER.info("移除旧网关: %s", old_gateway_entry.entry_id)
                    # 发送移除旧网关事件
                    hass.bus.async_fire(
                        f"{DOMAIN}_migration_progress",
                        {
                            "old_gateway_sn": old_gateway_sn,
                            "new_gateway_sn": new_gateway_sn,
                            "status": "removing_old_gateway",
                            "progress": 95,
                            "message": "正在移除旧网关"
                        }
                    )
                    
                    # 先清理旧网关的设备注册，再删除配置条目
                    await old_manager._cleanup_old_gateway(old_gateway_sn)
                    await hass.config_entries.async_remove(old_gateway_entry.entry_id)
                    _LOGGER.info("旧网关移除成功")
                except Exception as remove_error:
                    _LOGGER.error("移除旧网关失败: %s", remove_error)
            else:
                # 保留旧网关时，重载其平台以清理已迁移设备的旧实体
                try:
                    _LOGGER.info("重载旧网关 %s 的平台，清理已迁移设备实体", old_gateway_sn)
                    await hass.config_entries.async_reload(old_gateway_entry.entry_id)
                except Exception as reload_error:
                    _LOGGER.warning("重载旧网关平台失败: %s", reload_error)
            
            # 发送迁移完成事件
            hass.bus.async_fire(
                f"{DOMAIN}_migration_progress",
                {
                    "old_gateway_sn": old_gateway_sn,
                    "new_gateway_sn": new_gateway_sn,
                    "status": "completed",
                    "progress": 100,
                    "message": "迁移完成"
                }
            )
            
            # 重新加载新网关的平台，确保实体正确显示
            try:
                _LOGGER.info("重新加载新网关 %s 的平台", new_gateway_sn)
                await hass.config_entries.async_reload(new_gateway_entry.entry_id)
                _LOGGER.info("新网关平台重新加载完成")
            except Exception as reload_error:
                _LOGGER.error("重新加载新网关平台失败: %s", reload_error)
                    
    except Exception as e:
        _LOGGER.error("迁移失败: %s", e)
        import traceback
        _LOGGER.error("详细错误信息: %s", traceback.format_exc())
        # 发送错误通知
        hass.bus.async_fire(
            f"{DOMAIN}_migration_failed",
            {
                "old_gateway_sn": old_gateway_sn,
                "new_gateway_sn": new_gateway_sn,
                "error": str(e)
            }
        )
        # 发送迁移失败事件
        hass.bus.async_fire(
            f"{DOMAIN}_migration_progress",
            {
                "old_gateway_sn": old_gateway_sn,
                "new_gateway_sn": new_gateway_sn,
                "status": "failed",
                "progress": 0,
                "message": f"迁移失败: {str(e)}"
            }
        )
        # v1.6.10（审计 B4）：本服务当前未注册（register 已注释禁用，dead code），
        # 但按契约收口执行失败路径——事件保留供进度监听，同时如实抛错，
        # 防止将来重新注册时把"假成功"复活
        raise ServiceValidationError(f"迁移设备失败：{e}") from e

async def handle_transfer_device(hass: HomeAssistant, call: ServiceCall) -> None:
    """处理转移设备服务调用"""
    device_id = call.data.get("device_id")
    new_gateway_sn = call.data.get("new_gateway_sn")

    if not device_id or not new_gateway_sn:
        _LOGGER.error("转移设备服务调用失败：参数不完整")
        raise ServiceValidationError("转移设备：device_id 与 new_gateway_sn 均不可为空")

    _LOGGER.info("收到转移设备请求，设备ID: %s，目标网关: %s", device_id, new_gateway_sn)

    # 解析设备SN（支持直接传入设备SN或HA设备ID）
    device_sn = None

    # 方法1：直接检查 device_id 是否是映射表中的设备SN
    if DOMAIN in hass.data and DEVICE_TO_GATEWAY_MAPPING in hass.data[DOMAIN]:
        mapping = hass.data[DOMAIN][DEVICE_TO_GATEWAY_MAPPING]
        if device_id in mapping:
            device_sn = device_id

    # 方法2：通过设备注册表查找（device_id 可能是 HA 设备注册表 ID）
    if not device_sn:
        try:
            from homeassistant.helpers.device_registry import async_get as async_get_device_registry
            dr = async_get_device_registry(hass)
            device_entry = dr.async_get(device_id)
            if device_entry:
                for identifier in device_entry.identifiers:
                    if identifier[0] == DOMAIN:
                        device_sn = identifier[1]
                        break
        except Exception as e:
            # 保持韧性：注册表解析失败不阻断转移，但必须留下可排查日志
            _LOGGER.warning(
                "通过设备注册表解析设备SN失败（将尝试其他方式）: %s", e, exc_info=True
            )

    # 方法3：在所有设备管理器的设备列表中查找
    if not device_sn:
        # v1.7.33：DOMAIN 数据面缺失时不裸 KeyError（同函数上方已有守卫写法）
        for _entry_id, data in (hass.data.get(DOMAIN) or {}).items():
            if isinstance(data, dict):
                dm = data.get("device_manager")
                if dm:
                    for device in dm.get_all_devices():
                        device_sn_candidate = device.get("sn", "")
                        # 使用精确匹配：device_id 等于 SN，或 SN 是 device_id 按 _ 分割后的某一段
                        if device_id == device_sn_candidate or device_sn_candidate in device_id.split("_"):
                            device_sn = device_sn_candidate
                            break
                    if device_sn:
                        break

    if not device_sn:
        _LOGGER.error("未找到设备ID %s 对应的设备SN", device_id)
        raise ServiceValidationError(f"转移设备：未找到设备ID {device_id} 对应的设备SN")

    # 查找任意一个设备管理器实例来执行转移
    device_manager = None
    # v1.7.33：DOMAIN 数据面缺失时不裸 KeyError（同函数上方已有守卫写法）
    for _entry_id, data in (hass.data.get(DOMAIN) or {}).items():
        if isinstance(data, dict) and data.get("device_manager"):
            device_manager = data["device_manager"]
            break

    if not device_manager:
        _LOGGER.error("未找到可用的设备管理器")
        raise ServiceValidationError("转移设备：未找到可用的设备管理器")

    # 执行转移
    try:
        success = await device_manager.transfer_device(device_sn, new_gateway_sn)
        if success:
            _LOGGER.info("设备 %s 已成功转移到网关 %s", device_sn, new_gateway_sn)
        else:
            # v1.6.10（审计 B1，P1 级）：v1.6.9 只把校验/查找路径改成了 raise，
            # 执行块未动——transfer_device 返回 False（映射缺失/目标不可达）时
            # 仅日志 → REST 200 假成功。服务已注册（dev tools/自动化可达），
            # 按项目自身"假成功根治"契约收口
            _LOGGER.error("设备 %s 转移失败", device_sn)
            raise ServiceValidationError(
                f"转移设备失败：{device_sn} 不在映射表或目标网关 {new_gateway_sn} 不可用"
            )
    except ServiceValidationError:
        raise
    except Exception as e:
        _LOGGER.error("转移设备失败: %s", e)
        raise ServiceValidationError(f"转移设备失败：{e}") from e


async def handle_unignore_gateway(hass: HomeAssistant, call: ServiceCall) -> None:
    """取消忽略网关（v1.7.18，第 7 轮审计 BUG-3）。

    发现卡片上误点"忽略"后，v1.7.12 起忽略记录持久化跨重启——该 SN 永不
    再出卡，而 async_unignore_gateway 一直没有任何生产调用方（误操作无
    自救入口）。本服务是它的唯一出口：开发工具/自动化/YAML 均可调用。
    取消后该网关的下一次上报即可重新触发发现卡片。
    """
    from .discovery import async_unignore_gateway

    gateway_sn = str(call.data.get("gateway_sn", "")).strip()
    if not gateway_sn:
        raise ServiceValidationError("取消忽略：gateway_sn 不可为空")
    # 审计 2026-09-30 B-5：返回值必须接住。旧写法无条件打"已取消忽略"，
    # 而 async_unignore_gateway 在 "discovery" 键缺失时曾是整体 no-op ⇒
    # 唯一自救入口失效却留下成功痕迹（假成功比失败更难归因）。
    unignored = await async_unignore_gateway(hass, gateway_sn)
    if unignored:
        _LOGGER.info("已取消忽略网关 %s（下次上报将重新出发现卡片）", gateway_sn)
    else:
        _LOGGER.warning(
            "网关 %s 本来就不在忽略列表里（可能已被清理，或 SN 大小写/拼写不符）"
            "——本次没有取消任何忽略；若该网关仍不出发现卡片，请检查它是否已被添加为集成条目",
            gateway_sn)


def register_services(hass: HomeAssistant) -> bool:
    """注册服务"""
    # P0 修复：所有服务处理器均为 async def，必须用 async 包装器传入
    # async_register。lambda call: async_fn(hass, call) 返回协程对象但不
    # await 它 → HA 的 SyncWorker 调度时协程被丢弃，"coroutine was never awaited"。

    async def _start_pairing(call: ServiceCall) -> None:
        await handle_start_pairing(hass, call)

    async def _refresh_devices(call: ServiceCall) -> None:
        await handle_refresh_devices(hass, call)

    async def _set_position(call: ServiceCall) -> None:
        await handle_set_position(hass, call)

    async def _check_gateway_status(call: ServiceCall) -> None:
        await handle_check_gateway_status(hass, call)

    async def _rename_device(call: ServiceCall) -> None:
        await handle_rename_device(hass, call)

    async def _transfer_device(call: ServiceCall) -> None:
        await handle_transfer_device(hass, call)

    async def _unignore_gateway(call: ServiceCall) -> None:
        await handle_unignore_gateway(hass, call)

    # 注册服务
    try:
        hass.services.async_register(
            DOMAIN,
            SERVICE_START_PAIRING,
            _start_pairing,
            schema=vol.Schema({
                vol.Required("device_id"): cv.string,
                # v1.6.19（第六轮审计 B-LOW4）：003 配对报文不携带时长，
                # duration 纯本地兜底超时——服务与 UI 行为一致收 10-300s
                # （Web UI 选择器 min/max 同为 10-300；此前 schema 只有
                # positive_int，REST/YAML 传 99999 可造出永不超时的
                # "配对中"态）。
                vol.Optional("duration", default=GATEWAY_PAIRING_TIMEOUT): vol.All(
                    cv.positive_int, vol.Range(min=10, max=300)
                ),
            })
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_DEVICES,
            _refresh_devices,
            schema=vol.Schema({
                vol.Required("device_id"): cv.string,
            })
        )

        hass.services.async_register(
            DOMAIN,
            COMMAND_SET_POSITION,
            _set_position,
            schema=vol.Schema({
                vol.Required("device_id"): cv.string,
                vol.Required("position"): vol.All(
                    _reject_bool_position,
                    vol.Coerce(int),
                    vol.Range(min=POSITION_MIN, max=POSITION_MAX),
                ),
            })
        )

        hass.services.async_register(
            DOMAIN,
            "check_gateway_status",
            _check_gateway_status,
            schema=vol.Schema({
                vol.Optional("device_id"): cv.string,
                vol.Optional("gateway_sn"): cv.string,
            })
        )

        # ============ 迁移服务（migrate_devices）暂禁用 ============
        # 设备迁移功能先不使用：协议/迁移逻辑待后续版本完善后再启用。
        # 若需重新启用，取消下面 async_register 的注释即可。
        # async def _migrate_devices(call: ServiceCall) -> None:
        #     await handle_migrate_devices(hass, call)
        # hass.services.async_register(
        #     DOMAIN,
        #     SERVICE_MIGRATE_DEVICES,
        #     _migrate_devices,
        #     schema=vol.Schema({
        #         vol.Required("old_gateway_sn"): cv.string,
        #         vol.Required("new_gateway_sn"): cv.string,
        #         vol.Optional("remove_old_gateway", default=False): cv.boolean,
        #     })
        # )

        hass.services.async_register(
            DOMAIN,
            SERVICE_RENAME_DEVICE,
            _rename_device,
            schema=vol.Schema({
                vol.Required("device_id"): cv.string,
                vol.Required(ATTR_NEW_NAME): cv.string,
            })
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_TRANSFER_DEVICE,
            _transfer_device,
            schema=vol.Schema({
                vol.Required("device_id"): cv.string,
                vol.Required("new_gateway_sn"): cv.string,
            })
        )

        # v1.7.18（第 7 轮审计 BUG-3）：误点"忽略"后的唯一自救出口
        hass.services.async_register(
            DOMAIN,
            "unignore_gateway",
            _unignore_gateway,
            schema=vol.Schema({
                vol.Required("gateway_sn"): cv.string,
            })
        )

        # v1.7.33（全量审计）：登记本次注册的服务名——最后一个条目被**删除**
        # 时按名注销（v1.7.34 起落点在 async_remove_entry，见该处注释），
        # 防"集成卸载后服务句柄仍在注册表"（调用得到裸 KeyError/500，而非
        # 可读的 ServiceValidationError），也顺带把重复 setup 的覆盖显性化。
        hass.data.setdefault(DOMAIN, {})["_registered_services"] = [
            SERVICE_START_PAIRING, SERVICE_REFRESH_DEVICES, "set_position",
            "check_gateway_status", SERVICE_RENAME_DEVICE, SERVICE_TRANSFER_DEVICE,
            "unignore_gateway",
        ]
        _LOGGER.info("开窗器网关服务注册成功")
    except vol.Invalid as e:
        _LOGGER.error("服务参数模式无效: %s", e)
        return False
    except Exception as e:
        _LOGGER.error("注册服务时发生意外错误: %s", e)
        return False

    return True
