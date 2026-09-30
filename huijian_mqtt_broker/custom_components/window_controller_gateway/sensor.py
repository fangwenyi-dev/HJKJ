"""开窗器网关传感器平台"""
import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .const import (
    DOMAIN,
    CONF_GATEWAY_SN,
    MANUFACTURER,
    SENSOR_TIMEOUT_MINUTES,
)
from .base_entity import WindowControllerBaseEntity

_LOGGER = logging.getLogger(__name__)

from .utils import get_entity_registry


class WindowControllerBatterySensor(WindowControllerBaseEntity, SensorEntity):
    """开窗器电池电压传感器"""
    
    def __init__(
        self,
        hass: HomeAssistant,
        device_manager,
        gateway_sn: str,
        device_sn: str,
        device_name: str,
        entry_id: str = None
    ):
        """初始化电池电压传感器"""
        # 调用基类初始化，mqtt_handler参数传入None
        super().__init__(
            hass=hass,
            device_manager=device_manager,
            mqtt_handler=None,
            gateway_sn=gateway_sn,
            device_sn=device_sn,
            device_name=device_name
        )
        
        self._attr_name = "电池电压"
        # unique_id 基于设备SN，与v1.1.8保持一致
        self._attr_unique_id = f"{gateway_sn}_{device_sn}_battery"
        self._attr_device_class = SensorDeviceClass.VOLTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        # v1.6.12（第五轮审计 #7）：移除 last_update_time——本地刷新时效戳
        # 是失效机制永不生效的根因，时效统一以设备缓存 last_update 为准
        self._entry_id = entry_id
        # 添加图标
        self._attr_icon = "mdi:battery"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        
        # 初始化状态
        self._update_state()
        
        # 注册状态更新回调
        # 注意：这里需要从hass.data中获取mqtt_handler
        # 由于初始化时可能还未设置，这里暂时不注册
        # 回调注册将在async_add_entities后通过其他方式处理
    
    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息"""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_sn)},
            name=self.device_name,
            manufacturer=MANUFACTURER,
            model="开窗器"
        )
    
    def _update_state(self):
        """从设备管理器更新状态

        v1.6.12（第五轮审计 #7）：时效判定改读设备缓存 last_update（真实上报
        时刻，update_device_status 维护的墙钟）——此前每次从缓存读到值就把
        本地 last_update_time 重置为 now，紧随其后的超时判定恒为假，
        SENSOR_TIMEOUT_MINUTES 契约从未生效（网关离线数小时仍显示离线前电压）。
        """
        device = self.device_manager.get_device(self.device_sn)
        if not device:
            return
        # 审计 2026-09-30 B-1 配套（复核指出我只补了一半）：时效闸必须排在
        # "取不到 voltage 就早退"**之前**。force 迁移会把 attributes 清空，
        # 旧顺序下属性一空就直接 return ⇒ _attr_native_value 永不更新，
        # 补了 last_update 也照样冻结在迁移前的值、失联不转 unknown。
        # 与同文件状态传感器（:170 起的"先做陈旧判定"）同口径。
        last_update = device.get("last_update")
        if last_update is not None and \
                (time.time() - last_update) > SENSOR_TIMEOUT_MINUTES * 60:
            self._attr_native_value = None
            _LOGGER.debug("设备 %s 电池电压数据超时（超过 %d 分钟无上报）",
                          self.device_sn, SENSOR_TIMEOUT_MINUTES)
            return
        attributes = device.get("attributes", {})
        voltage = attributes.get("voltage")
        if voltage is None:
            return
        self._attr_native_value = voltage
        _LOGGER.debug("设备 %s 电池电压更新: %.1fV", self.device_sn, voltage)
    
    @property
    def native_unit_of_measurement(self):
        """返回单位 - 确保即使状态为None时也返回正确的单位"""
        return "V"
    
    async def async_update(self) -> None:
        """更新实体状态"""
        # 守卫：实体被移除后 hass 为 None，残留轮询直接返回（2026-08-28 实测崩溃点）
        if self.hass is None:
            return
        self._update_state()
        self.async_write_ha_state()


class WindowControllerStatusSensor(WindowControllerBaseEntity, SensorEntity):
    """开窗器状态传感器"""
    
    def __init__(
        self,
        hass: HomeAssistant,
        device_manager,
        gateway_sn: str,
        device_sn: str,
        device_name: str,
        entry_id: str = None
    ):
        """初始化状态传感器"""
        super().__init__(
            hass=hass,
            device_manager=device_manager,
            mqtt_handler=None,
            gateway_sn=gateway_sn,
            device_sn=device_sn,
            device_name=device_name
        )
        self._entry_id = entry_id
        self._attr_name = "状态"
        # unique_id 基于设备SN，与v1.1.8保持一致
        self._attr_unique_id = f"{gateway_sn}_{device_sn}_status"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = ["closed", "open"]
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        # v1.6.12（第五轮审计 #7）：移除 last_update_time——本地刷新时效戳
        # 是失效机制永不生效的根因，时效统一以设备缓存 last_update 为准
        
        # 初始化状态
        self._update_state()
    
    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息"""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_sn)},
            name=self.device_name,
            manufacturer=MANUFACTURER,
            model="开窗器"
        )
    
    def _update_state(self):
        """从设备管理器更新状态

        v1.6.12（第五轮审计 #7）：同电池传感器——时效改读设备缓存
        last_update（真实上报时刻），本地重置时效戳令超时分支恒不可达的
        缺陷根治；网关长时间离线后状态如实转 unknown。
        """
        device = self.device_manager.get_device(self.device_sn)
        if not device:
            return
        # v1.6.12：先做陈旧判定——陈旧值（status/r_travel 都是缓存）不再回写
        last_update = device.get("last_update")
        if last_update is not None and \
                (time.time() - last_update) > SENSOR_TIMEOUT_MINUTES * 60:
            self._attr_native_value = None
            _LOGGER.debug("设备 %s 状态数据超时（超过 %d 分钟无上报）",
                          self.device_sn, SENSOR_TIMEOUT_MINUTES)
            return
        # 优先使用设备状态
        status = device.get("status")
        if status in ["closed", "open"]:
            self._attr_native_value = status
            _LOGGER.debug("设备 %s 状态更新为: %s", self.device_sn, status)
        else:
            # 如果没有状态，使用r_travel判断
            # v1.7.33（全量审计）：旧判定 `r_travel == 0` 是严格比较——字符串
            # "0"/"0.0"（005 上报原样入库形态，test_cover_state 夹具实证存在）
            # 与 255（未校准/离线标记）都落 "open"，关着的窗显示"打开"；而
            # v1.7.31 起 Web 设备卡圆点的主判据正是本传感器。改数值归一 +
            # 0..100 值域：越界/非法一律 unknown，绝不用位置反推状态。
            attributes = device.get("attributes", {})
            r_travel = attributes.get("r_travel")
            if r_travel is not None:
                raw = None
                if not isinstance(r_travel, bool):
                    try:
                        raw = int(float(r_travel))
                    except (ValueError, TypeError, OverflowError):
                        raw = None
                if raw is None or not 0 <= raw <= 100:
                    self._attr_native_value = None
                    _LOGGER.debug("设备 %s r_travel 非法或未校准(%r)，状态转 unknown",
                                  self.device_sn, r_travel)
                else:
                    new_status = "closed" if raw == 0 else "open"
                    self._attr_native_value = new_status
                    _LOGGER.debug("设备 %s 状态根据r_travel更新为: %s",
                                  self.device_sn, new_status)

    async def async_update(self) -> None:
        """更新实体状态"""
        # 守卫：实体被移除后 hass 为 None，残留轮询直接返回（2026-08-28 实测崩溃点）
        if self.hass is None:
            return
        self._update_state()
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """设置传感器实体"""
    _LOGGER.info("设置传感器平台: %s, entry_id: %s", entry.entry_id, entry.entry_id)
    # 从设备管理器获取设备
    domain_data = hass.data[DOMAIN]
    entry_data = domain_data.get(entry.entry_id)
    
    _LOGGER.info("传感器平台: 获取到 domain_data, keys: %s", list(domain_data.keys()))
    
    if not entry_data:
        _LOGGER.error("配置条目数据未找到: %s, domain_data keys: %s", entry.entry_id, list(domain_data.keys()))
        return
        
    device_manager = entry_data.get("device_manager")
    
    if not device_manager:
        _LOGGER.error("设备管理器未找到")
        return
    
    gateway_sn = entry.data[CONF_GATEWAY_SN]
    
    # 跟踪创建的传感器实体
    created_sensors = {}
    
    # 定义设备添加回调函数
    async def on_device_added(device_sn: str, device_name: str, device_type: str):
        """设备添加回调，自动创建传感器实体"""
        _LOGGER.info("收到设备添加回调: %s - %s", device_name, device_sn)
        
        # 检查实体是否已存在（避免重复创建；兼容新旧 HA entity 查找）
        from .utils import async_get_entity_id as _aget_eid
        
        battery_unique_id = f"{gateway_sn}_{device_sn}_battery"
        status_unique_id = f"{gateway_sn}_{device_sn}_status"
        
        # 会话内幂等短路（v1.6.3）：设备重同步会重复触发本回调并叠加
        # add_status_callback（旧实例回调无人摘除 → 状态多播 + 内存泄漏）；
        # async_add_entities 到注册表落库存在窗口，注册表查重挡不住连续事件
        tracked = created_sensors.get(device_sn, {})
        battery_exists = "battery" in tracked or await _aget_eid(hass, "sensor", battery_unique_id) is not None
        status_exists = "status" in tracked or await _aget_eid(hass, "sensor", status_unique_id) is not None
        
        if battery_exists and status_exists:
            _LOGGER.info("传感器实体已存在，跳过创建: %s", device_sn)
            return
        
        entities_to_add = []
        sensors_to_track = {}
        
        # 检查并创建电池电压传感器
        if not battery_exists:
            battery_sensor = WindowControllerBatterySensor(
                hass,
                device_manager,
                gateway_sn,
                device_sn,
                device_name
            )
            entities_to_add.append(battery_sensor)
            sensors_to_track["battery"] = battery_sensor
            _LOGGER.debug("为设备 %s 添加电池传感器", device_name)
        
        # 检查并创建状态传感器
        if not status_exists:
            status_sensor = WindowControllerStatusSensor(
                hass,
                device_manager,
                gateway_sn,
                device_sn,
                device_name
            )
            entities_to_add.append(status_sensor)
            sensors_to_track["status"] = status_sensor
            _LOGGER.debug("为设备 %s 添加状态传感器", device_name)
        
        # 调用async_add_entities添加实体
        if entities_to_add:
            async_add_entities(entities_to_add)
            _LOGGER.info("为新设备 %s 添加了传感器实体", device_name)
            
            # 合并到 created_sensors（而非整体替换）：
            # 若启动循环/先前回调已跟踪了其他传感器（如 battery），
            # 整体替换会丢失该记录，导致删除设备时残留孤儿实体
            created_sensors.setdefault(device_sn, {}).update(sensors_to_track)
            
            # 注册状态更新回调
            # 注意：这里需要从hass.data中获取mqtt_handler
            domain_data = hass.data.get(DOMAIN, {})
            entry_data = domain_data.get(entry.entry_id)
            if entry_data:
                mqtt_handler = entry_data.get("mqtt_handler")
                if mqtt_handler:
                    # 为电池传感器注册回调
                    if "battery" in sensors_to_track:
                        mqtt_handler.add_status_callback(device_sn, sensors_to_track["battery"].async_update)
                    # 为状态传感器注册回调
                    if "status" in sensors_to_track:
                        mqtt_handler.add_status_callback(device_sn, sensors_to_track["status"].async_update)
                    _LOGGER.debug("为设备 %s 注册了状态更新回调", device_sn)
                else:
                    _LOGGER.warning("mqtt_handler 不可用，设备 %s 的传感器回调暂未注册", device_sn)
            else:
                _LOGGER.warning("entry_data (%s) 不可用，设备 %s 的传感器回调暂未注册", entry.entry_id, device_sn)

    # 定义设备移除回调函数
    async def on_device_removed(device_sn: str, device_name: str, device_type: str):
        """设备移除回调，清理相关传感器"""
        _LOGGER.info("收到设备移除回调: %s - %s", device_name, device_sn)
        if device_sn in created_sensors:
            # 获取传感器实体
            sensors = created_sensors[device_sn]
            # 从跟踪字典中删除
            del created_sensors[device_sn]
            _LOGGER.info("已清理设备 %s 的传感器实体跟踪", device_name)
            
            # 移除状态更新回调
            try:
                domain_data = hass.data.get(DOMAIN, {})
                entry_data = domain_data.get(entry.entry_id)
                if entry_data:
                    mqtt_handler = entry_data.get("mqtt_handler")
                    if mqtt_handler:
                        # 移除电池传感器回调
                        if "battery" in sensors:
                            battery_entity = sensors["battery"]
                            mqtt_handler.remove_status_callback(device_sn, battery_entity.async_update)
                        # 移除状态传感器回调
                        if "status" in sensors:
                            status_entity = sensors["status"]
                            mqtt_handler.remove_status_callback(device_sn, status_entity.async_update)
                        _LOGGER.debug("已移除设备 %s 的状态更新回调", device_sn)
            except Exception as e:
                _LOGGER.error("移除设备 %s 的状态更新回调失败: %s", device_name, e)
            
            # 尝试从实体注册表中删除实体（兼容新旧 HA registry 同步/异步 API）
            try:
                from .utils import call_registry_method as _call_reg
                from .utils import async_get_entity_id as _aget_eid
                entity_registry = get_entity_registry(hass)
                # v1.6.19（第六轮审计 B-LOW6）：unique_id 优先（button.py
                # v1.6.3 定案同款）——配对后秒级解绑时实体可能尚未获派
                # entity_id，原单路径落空即注册表悬挂、重配对永久缺传感器。
                async def _drop(entity, label):
                    _eid = await _aget_eid(hass, "sensor", entity._attr_unique_id)
                    if _eid:
                        await _call_reg(entity_registry.async_remove, _eid)
                    elif entity.entity_id:
                        await _call_reg(entity_registry.async_remove, entity.entity_id)
                    else:
                        _LOGGER.warning("%s实体定位失败（unique_id=%s 双路径均未命中）: %s",
                                        label, entity._attr_unique_id, device_name)
                # 删除电池传感器
                if "battery" in sensors:
                    battery_entity = sensors["battery"]
                    await _drop(battery_entity, "电池传感器")
                    _LOGGER.info("已从实体注册表中删除设备 %s 的电池传感器", device_name)
                # 删除状态传感器
                if "status" in sensors:
                    status_entity = sensors["status"]
                    await _drop(status_entity, "状态传感器")
                    _LOGGER.info("已从实体注册表中删除设备 %s 的状态传感器", device_name)
            except Exception as e:
                _LOGGER.error("从实体注册表中删除设备 %s 的传感器失败: %s", device_name, e)

    # 设置设备添加回调
    device_manager.set_device_added_callback(on_device_added)
    # 设置设备移除回调
    device_manager.set_device_removed_callback(on_device_removed)
    _LOGGER.info("已设置设备回调")
    
    # 为每个设备创建传感器（直接创建所有传感器，不检查实体是否存在）
    entities = []
    
    try:
        devices = device_manager.get_all_devices()
        _LOGGER.info("传感器平台: 获取到 %d 个设备", len(devices))
        
        for device in devices:
            device_sn = device.get("sn")
            device_name = device.get("name")
            
            if device_sn and device_name:
                # 启动循环无条件创建传感器：
                # 注册表条目跨重启/重载持久保留，用注册表查重会导致重启后
                # 实体只有注册表条目、没有平台实例（不可用）。
                # 重复添加由 HA 按 unique_id 自动去重（替换更新）。
                battery_sensor = WindowControllerBatterySensor(
                    hass,
                    device_manager,
                    gateway_sn,
                    device_sn,
                    device_name
                )
                entities.append(battery_sensor)
                created_sensors.setdefault(device_sn, {})["battery"] = battery_sensor
                
                status_sensor = WindowControllerStatusSensor(
                    hass,
                    device_manager,
                    gateway_sn,
                    device_sn,
                    device_name
                )
                entities.append(status_sensor)
                created_sensors.setdefault(device_sn, {})["status"] = status_sensor
                
    except Exception as e:
        _LOGGER.error("传感器平台: 创建传感器实体时发生错误: %s", e, exc_info=True)
    
    _LOGGER.info("传感器平台: 检查 entities 数量: %d", len(entities))
    _LOGGER.info("传感器平台: entities bool: %s", bool(entities))
    
    if entities:
        _LOGGER.info("传感器平台: 进入 if entities 块, 数量=%d", len(entities))
        
        # 直接调用 async_add_entities（与 button.py 保持一致）
        async_add_entities(entities)
        _LOGGER.info("传感器平台: async_add_entities 调用完成")
        _LOGGER.info("已添加 %d 个传感器实体", len(entities))
        
        # 注册回调
        try:
            domain_data = hass.data.get(DOMAIN, {})
            entry_data = domain_data.get(entry.entry_id)
            if entry_data:
                mqtt_handler = entry_data.get("mqtt_handler")
                if mqtt_handler:
                    for device_sn, sensors in created_sensors.items():
                        if "battery" in sensors:
                            battery_sensor = sensors["battery"]
                            mqtt_handler.add_status_callback(device_sn, battery_sensor.async_update)
                        if "status" in sensors:
                            status_sensor = sensors["status"]
                            mqtt_handler.add_status_callback(device_sn, status_sensor.async_update)
                    _LOGGER.info("传感器回调注册完成")
                else:
                    _LOGGER.warning("mqtt_handler 不可用，传感器回调暂未注册")
            else:
                _LOGGER.warning("entry_data (%s) 不可用，传感器回调暂未注册", entry.entry_id)
        except Exception as e:
            _LOGGER.error("传感器平台: 注册回调失败: %s", e, exc_info=True)
    else:
        _LOGGER.info("当前没有设备，等待设备添加")
