"""开窗器网关实体"""
import logging
import asyncio
import threading

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass
)
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    GATEWAY_READY_DELAY,
    GATEWAY_PAIRING_TIMEOUT,
    ENTITY_ONLINE_SENSOR_SUFFIX,
    ENTITY_PAIRING_BUTTON_SUFFIX,
)

_LOGGER = logging.getLogger(__name__)


def _entry_still_configured(hass, entry_id) -> bool:
    """条目是否仍在 config_entries 列表里（reload 令出点判定用）。

    判定不了（宿主/配置面异常、测试替身无 config_entries）一律返回 False——
    调用方据此退回"照常执行"的原行为，绝不因探测失败而阻断用户操作。
    """
    if not entry_id:
        return False
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:  # noqa: BLE001
        return False
    if not entries:
        return False
    return any(getattr(e, "entry_id", None) == entry_id for e in entries)



class GatewayOnlineSensor(BinarySensorEntity):
    """网关在线状态传感器"""
    
    _attr_has_entity_name = True
    
    def __init__(
        self,
        hass: HomeAssistant,
        device_manager,
        mqtt_handler,
        gateway_sn: str,
        gateway_name: str,
        entry_id: str = None
    ):
        """初始化网关在线状态传感器"""
        self.hass = hass
        self.device_manager = device_manager
        self.mqtt_handler = mqtt_handler
        self.gateway_sn = gateway_sn
        self.gateway_name = gateway_name
        self._entry_id = entry_id
        self._attr_name = "在线"
        # unique_id基于网关SN，确保同一网关只有一个在线状态传感器
        # v1.7.31（C-3）：后缀走 const 单一真源（输出逐字不变，无实体迁移）
        self._attr_unique_id = f"{gateway_sn}{ENTITY_ONLINE_SENSOR_SUFFIX}"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_is_on = False
        # 添加图标
        self._attr_icon = "mdi:access-point"
        
        # 添加状态更新回调
        try:
            self.mqtt_handler.add_status_callback(self._on_status_change)
        except Exception as e:
            _LOGGER.error("添加网关在线状态回调失败: %s", e)
        
        # 初始状态更新
        self._update_state()
    
    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息"""
        return DeviceInfo(
            identifiers={(DOMAIN, self.gateway_sn)},
            name=self.gateway_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=self.gateway_sn
        )
    
    def _update_state(self):
        """更新状态"""
        # 从MQTT处理器获取连接状态
        self._attr_is_on = self.mqtt_handler.connected
        _LOGGER.debug("网关 %s 在线状态更新为: %s", self.gateway_sn, self._attr_is_on)

    @property
    def extra_state_attributes(self):
        """暴露网关配对状态与最近网关状态，供用户查看（不改变按钮可用性）"""
        return {
            "pairing_active": bool(getattr(self.mqtt_handler, "pairing_active", False)),
            "gateway_status": getattr(self.device_manager, "gateway_status", "unknown"),
        }
    
    def _on_status_change(self):
        """当MQTT状态改变时调用"""
        self._update_state()
        # 通知Home Assistant状态已更新
        # 本回调经 hass.add_job 调度，可能在事件循环线程或线程池线程中执行：
        # - 事件循环线程内 → async_write_ha_state（HA 2024.12+ 推荐，避免
        #   schedule_update_ha_state 的弃用告警与多余的线程池跳转）
        # - 线程池线程内 → schedule_update_ha_state（线程安全版本）
        # P1 兼容：hass.loop_thread_id 是 2024.12+ 属性，低版本用 getattr 回退
        # 到 schedule_update_ha_state（线程安全，行为正确，仅多一次线程池跳转）。
        try:
            if self.hass is not None:
                loop_thread_id = getattr(self.hass, "loop_thread_id", None)
                if loop_thread_id is not None and threading.get_ident() == loop_thread_id:
                    self.async_write_ha_state()
                else:
                    self.schedule_update_ha_state()
            else:
                _LOGGER.warning("无法更新网关状态：hass为None")
        except Exception as e:
            _LOGGER.error("更新网关状态失败: %s", e)
    
    async def async_update(self) -> None:
        """更新实体状态"""
        self._update_state()

    async def async_will_remove_from_hass(self) -> None:
        """当实体从HA中移除时调用"""
        # 移除状态更新回调
        self.mqtt_handler.remove_status_callback(self._on_status_change)
        # base_entity v1.6.9 同款纪律：断链会静默跳过 MRO 上挂在钩子处的 mixin
        await super().async_will_remove_from_hass()

class GatewayPairingButton(ButtonEntity):
    """网关配对按键"""
    
    _attr_has_entity_name = True
    
    def __init__(
        self,
        hass: HomeAssistant,
        device_manager,
        mqtt_handler,
        gateway_sn: str,
        gateway_name: str,
        entry_id: str = None
    ):
        """初始化网关配对按键"""
        self.hass = hass
        self.device_manager = device_manager
        self.mqtt_handler = mqtt_handler
        self.gateway_sn = gateway_sn
        self.gateway_name = gateway_name
        self._entry_id = entry_id
        self._attr_name = "配对"
        # unique_id基于网关SN，确保同一网关只有一个配对按钮
        # v1.7.31（C-3）：后缀走 const 单一真源（输出逐字不变，无实体迁移）
        self._attr_unique_id = f"{gateway_sn}{ENTITY_PAIRING_BUTTON_SUFFIX}"
        # 添加图标
        self._attr_icon = "mdi:plus-circle"
        # 确保按钮始终可用，不随网关在线状态变灰
        self._attr_available = True
    
    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息 - 与网关关联"""
        return DeviceInfo(
            identifiers={(DOMAIN, self.gateway_sn)},
            name=self.gateway_name,
            manufacturer=MANUFACTURER,
            model=MODEL
        )
    
    async def async_press(self) -> None:
        """按下按键，触发配对模式"""
        try:
            # P1 修复：使用 mqtt_handler.pairing_timeout_handle 统一管理配对超时，
            # 与 __init__.py 的 handle_start_pairing 服务共享同一个句柄。
            if self.mqtt_handler.pairing_timeout_handle:
                self.mqtt_handler.pairing_timeout_handle.cancel()
                self.mqtt_handler.pairing_timeout_handle = None

            # 使用命令管理器发送，统一处理命令ID、连接检查等
            success = await self.mqtt_handler.send_command(self.gateway_sn, "start_pairing")
            if not success:
                # v1.6.9：配对按钮失败如实上抛（此前 error+return=假成功，
                # 与 services.start_pairing 同族——HA 卡片点击报"启动配对失败"）
                # v1.6.10（审计 B2）：抛前先清上次配对残留（旧定时器已在
                # 本 try 开头被 cancel，不清则永久卡「配对中」）
                _LOGGER.error("发送配对命令失败")
                self.mqtt_handler.abort_pairing_if_active()
                raise HomeAssistantError("启动配对失败：命令未送达（网关离线）")
            
            # 更新配对状态
            self.mqtt_handler.pairing_active = True
            self.mqtt_handler._notify_status_change()
            
            # 更新网关状态
            self.hass.async_create_task(
                self.device_manager.update_gateway_status("pairing")
            )
            
            _LOGGER.info("配对命令已发送，持续时间: %d秒", GATEWAY_PAIRING_TIMEOUT)
            _LOGGER.info("已触发网关 %s 的配对模式", self.gateway_sn)
            
            # 设置定时器，在配对超时后恢复状态
            def pairing_timeout():
                self.mqtt_handler.pairing_timeout_handle = None
                self.mqtt_handler.pairing_active = False
                self.mqtt_handler._notify_status_change()
                self.hass.async_create_task(
                    self.device_manager.update_gateway_status("online" if self.mqtt_handler.connected else "offline")
                )
                _LOGGER.info("配对模式已超时，恢复正常状态")
            
            # 延迟执行超时回调
            self.mqtt_handler.pairing_timeout_handle = self.hass.loop.call_later(GATEWAY_PAIRING_TIMEOUT, pairing_timeout)
        except HomeAssistantError:
            # v1.6.9：命令未送达的假成功根治——不链式穿透下面的兜底 except
            raise
        except Exception as e:
            # v1.6.10（审计 B2）：发送成功后、置位/挂定时器前抛错（如
            # create_task/call_later 异常）同样会留 pairing_active 残留——
            # 幂等清理助手，未处于配对中时为 no-op
            self.mqtt_handler.abort_pairing_if_active()
            _LOGGER.error("触发网关配对模式失败: %s", e)
            raise HomeAssistantError(f"启动配对失败：{e}") from e

class GatewayDeviceRemoveButton(ButtonEntity):
    """网关设备删除按键"""
    
    _attr_has_entity_name = True
    
    def __init__(
        self,
        hass: HomeAssistant,
        device_manager,
        mqtt_handler,
        gateway_sn: str,
        gateway_name: str,
        device_sn: str,
        device_name: str,
        entry_id: str = None
    ):
        """初始化网关设备删除按键"""
        self.hass = hass
        self.device_manager = device_manager
        self.mqtt_handler = mqtt_handler
        self.gateway_sn = gateway_sn
        self.gateway_name = gateway_name
        self.device_sn = device_sn
        self.device_name = device_name
        self._entry_id = entry_id
        self._attr_name = f"移除 {device_sn[-4:]}"
        # unique_id基于网关SN和设备SN，确保同一网关的同一设备只有一个删除按钮
        self._attr_unique_id = f"{gateway_sn}_remove_{device_sn}"
        # 添加图标
        self._attr_icon = "mdi:delete"
        # 确保按钮始终可用，不随网关在线状态变灰
        self._attr_available = True
        # 设为配置类，使按钮出现在配置区域
        self._attr_entity_category = EntityCategory.CONFIG
    
    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息 - 与网关关联，显示在网关控制栏中"""
        return DeviceInfo(
            identifiers={(DOMAIN, self.gateway_sn)},
            name=self.gateway_name,
            manufacturer=MANUFACTURER,
            model=MODEL
        )
    
    async def async_press(self) -> None:
        """按下按键，删除设备"""
        # MQTT 解绑为尽力而为：网关离线/broker 断开时发送会失败，
        # 但设备删除的本地部分（注册表/映射/实体）是纯本地操作，
        # 不应被网关在线状态阻断——即使解绑命令未发出也继续删除。
        try:
            await self.mqtt_handler.unbind_device(self.device_sn)
            _LOGGER.info("已发送解绑命令，设备SN: %s", self.device_sn)
        except Exception as e:
            _LOGGER.warning("发送解绑命令失败（将继续本地删除设备）: %s", e)

        # 等待1秒，确保网关有足够时间处理解绑命令
        await asyncio.sleep(GATEWAY_READY_DELAY)

        # v1.7.33（全量审计）：sleep 是让出点——期间该条目可能被 reload（另一
        # 会话改令牌/选项即触发）或被删除。旧 manager 在 unload 时被 cleanup
        # 清空，拿构造期引用 remove_device 会整体 no-op（不登记手动删除名单、
        # 不清映射），新 manager 又按残留映射回填 → 幽灵设备复活（v1.6.19
        # A-MED2 在 WS 通道修过同案，按钮路径漏改）。按 (entry_id → data)
        # 重解析；条目仍在而 data 未就绪=重载中，如实拒绝让用户重试。
        manager_now = self.device_manager
        data_now = None
        try:
            data_now = (self.hass.data.get(DOMAIN) or {}).get(self._entry_id)
        except Exception:  # noqa: BLE001 - 宿主数据面异常按"判定不了"处理
            data_now = None
        if isinstance(data_now, dict) and data_now.get("device_manager") is not None:
            manager_now = data_now["device_manager"]
        elif data_now is None and self._entry_id and _entry_still_configured(
                self.hass, self._entry_id):
            _LOGGER.warning(
                "移除按钮：条目 %s 正在重载，设备 %s 的本地删除未执行——请稍后重试",
                self._entry_id, self.device_sn)
            return

        # 从设备管理器中删除设备（本地操作，不受网关状态影响）
        try:
            await manager_now.remove_device(self.device_sn)
            _LOGGER.info("已从系统中删除设备: %s", self.device_sn)
        except Exception as e:
            _LOGGER.error("从系统中删除设备失败: %s", e)
            return

        # 从实体注册表中删除自身（删除按钮）
        try:
            from homeassistant.helpers.entity_registry import async_get
            entity_registry = async_get(self.hass)
            # 兼容新旧 HA 的 entity 查找（新版 async_get_entity_id 为 async 方法——
            # 2026-08-28 修复 'RegistryEntry' object can't be awaited）
            from .utils import async_get_entity_id as _aget_eid
            from .utils import call_registry_method as _call_reg
            entity_id = await _aget_eid(self.hass, "button", self._attr_unique_id)
            if entity_id:
                await _call_reg(entity_registry.async_remove, entity_id)
                _LOGGER.info("已从实体注册表中删除删除按钮: %s", entity_id)
            else:
                _LOGGER.debug("删除按钮实体未找到，可能已经被删除: %s", self._attr_unique_id)
        except Exception as e:
            _LOGGER.error("从实体注册表中删除删除按钮失败: %s", e)