"""开窗器网关数值调节平台 - Number 实体（HA 界面滑动条）

通过 004 协议发送 rwp_winact_* 参数（0-100）控制命令，
并解析 005 上报的同名属性回显当前值。
当前支持：
- 速度（rwp_winact_speed）
- 力度（rwp_winact_strength）
"""
import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.components.number import NumberEntity, NumberMode

from .base_entity import WindowControllerBaseEntity
from .const import (
    DOMAIN,
    CONF_GATEWAY_SN,
    DEVICE_TYPE_WINDOW_OPENER,
    MANUFACTURER,
    COMMAND_SET_SPEED,
    COMMAND_SET_STRENGTH,
    SPEED_MIN,
    SPEED_MAX,
    DEVICE_SETPOINTS,
    SLIDER_DEBOUNCE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

from .utils import get_entity_registry


class WindowControllerRangeNumber(WindowControllerBaseEntity, NumberEntity):
    """开窗器数值调节滑动条基类（rwp_winact_*，0-100%）

    子类需定义：
    - _entity_suffix: unique_id 后缀（如 speed）
    - _entity_label: 实体名称（如 速度）
    - _entity_icon: 图标
    - _command: 发送命令（COMMAND_SET_SPEED / COMMAND_SET_STRENGTH）
    - _param_key: send_command 的参数字段（speed / strength）
    """

    _entity_suffix = ""
    _entity_label = ""
    _entity_icon = ""
    _command = ""
    _param_key = ""

    def __init__(
        self,
        hass: HomeAssistant,
        device_manager,
        mqtt_handler,
        gateway_sn: str,
        device_sn: str,
        device_name: str,
        entry_id: str = None
    ):
        """初始化滑动条实体"""
        super().__init__(
            hass=hass,
            device_manager=device_manager,
            mqtt_handler=mqtt_handler,
            gateway_sn=gateway_sn,
            device_sn=device_sn,
            device_name=device_name
        )

        self._attr_name = self._entity_label
        self._attr_unique_id = f"{gateway_sn}_{device_sn}_{self._entity_suffix}"
        self._attr_icon = self._entity_icon
        self._attr_native_min_value = float(SPEED_MIN)
        self._attr_native_max_value = float(SPEED_MAX)
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "%"
        self._attr_mode = NumberMode.SLIDER
        self._attr_entity_category = EntityCategory.CONFIG
        # 始终可用，防止变灰（与按钮/Cover 行为一致）
        self._attr_available = True
        self._entry_id = entry_id
        # 防抖：拖动过程中只更新显示，停止 SLIDER_DEBOUNCE_SECONDS 秒后才发送命令
        self._debounce_handle = None
        self._pending_value = None

        # 初始化状态（若有已保存的设定值）
        self._update_state()

    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息"""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_sn)},
            name=self.device_name,
            manufacturer=MANUFACTURER,
            model="开窗器",
            serial_number=self.device_sn
        )

    def _read_setpoint(self):
        """读取设备参数设定值（上次用户设定的位置）"""
        # 守卫：实体被移除后 self.hass 可能为 None（HA 清空引用），
        # 残留轮询/回调会触发 'NoneType' object has no attribute 'data'（2026-08-28 实测）
        if self.hass is None:
            return None
        setpoints = self.hass.data.get(DOMAIN, {}).get(DEVICE_SETPOINTS, {})
        return setpoints.get(self.device_sn, {}).get(self._param_key)

    def _update_state(self):
        """同步实体状态：显示上次设定的值（setpoint）

        注意：不读取 005 上报的运行时值——网关空闲时上报 0，
        若以运行时值回显，重新进入界面后滑块会跳回 0。
        HA 侧显示 setpoint、小程序侧显示设备回传值（winact_speed/strength），这是刻意分叉。
        """
        value = self._read_setpoint()
        if value is not None:
            try:
                value = int(value)
                self._attr_native_value = float(max(SPEED_MIN, min(SPEED_MAX, value)))
            except (ValueError, TypeError, OverflowError):
                # 审计 2026-09-30 A-6：补捕 OverflowError——JSON `1e999`/`Infinity`
                # 解析成 float('inf') 本身不抛错，而 `int(inf)` 抛的是
                # OverflowError，旧的 (ValueError, TypeError) 元组接不住；实体
                # __init__（:90）在 number 启动循环（无 try）里抛出 ⇒ 该条目所有
                # 设备的速度/力度滑块一起消失，每次 reload 复发（一坏俱坏，与
                # persist B-MED2 修的是同一后果，只换了维度）。口径对齐
                # ws_gateway._as_int（v1.6.19 A-HIGH2 同条理由）。
                _LOGGER.debug("设备 %s %s设定值无效: %r", self.device_sn, self._entity_label, value)

    async def async_update(self) -> None:
        """定期更新状态（HA 轮询）"""
        # 守卫：实体被移除后 hass 为 None，残留轮询直接返回（2026-08-28 实测崩溃点）
        if self.hass is None:
            return
        if self._pending_value is not None:
            # 防抖等待中：保持用户正在设定的值，避免轮询把滑块拉回旧值
            self._attr_native_value = float(self._pending_value)
        else:
            self._update_state()
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """滑动条拖动回调：立即更新显示，防抖后只发送最终值"""
        # v1.7.65（第四轮复核 N4，同族第 7 处）：入参来自自动化 YAML / 服务调用，
        # `.inf`、`.nan` 都是合法 float 字面量——`int(inf)` 抛 OverflowError、
        # `int(nan)` 抛 ValueError，旧代码无闸 ⇒ 服务调用当场炸穿。同文件 :122 的
        # 设定值回显早在 v1.7.61 A-6 就接住了这两个，入参侧是同族漏网。
        # 口径照 cover.async_set_cover_position：非法值**如实拒**，不回退默认档
        # （回退＝把"想设 150"静默执行成别的档位，v1.6.19 B-LOW11 同判）。
        try:
            value_int = int(value)
        except (ValueError, TypeError, OverflowError) as err:
            raise HomeAssistantError(
                f"设置{self._entity_label}失败：无效的值 {value!r}") from err
        value_int = max(SPEED_MIN, min(SPEED_MAX, value_int))
        # 立即更新界面显示（拖动过程中滑块跟随，不阻塞交互）
        self._attr_native_value = float(value_int)
        self.async_write_ha_state()
        # 记录待发送的最终值并重置防抖定时器：
        # 连续拖动只重置定时器，停止 SLIDER_DEBOUNCE_SECONDS 秒后才真正发送。
        self._pending_value = value_int
        if self._debounce_handle:
            self._debounce_handle.cancel()
        self._debounce_handle = self.hass.loop.call_later(
            SLIDER_DEBOUNCE_SECONDS, self._on_debounce_fired
        )

    def _on_debounce_fired(self):
        """防抖窗口结束：仅发送最后一次设定的值"""
        self._debounce_handle = None
        # 守卫：实体已被移除（hass=None）时不再创建发送任务（v1.6.3）
        if self.hass is None:
            return
        value = self._pending_value
        self._pending_value = None
        if value is None:
            return
        self.hass.async_create_task(self._send_value(value))

    async def _send_value(self, value: int) -> None:
        """发送 004 控制命令；成功后持久化设定值，失败则回退显示"""
        # 守卫：任务已 create_task 出去后实体可能被删除（拖滑块后立即删设备），
        # 此路径不受 async_will_remove_from_hass 的定时器取消保护（v1.6.3）
        if self.hass is None:
            return
        try:
            success = await self._get_mqtt_handler().send_command(
                self.device_sn, self._command, {self._param_key: value}
            )
            if success:
                # v1.6.4：await 期间实体可能已被移除（TOCTOU）——hass 摘除后
                # 再访问 self.hass.data 会 AttributeError 落入 except 分支，
                # 把"命令已成功"误记为"设置失败"日志并空跑回退。命令已送达，
                # 静默跳过本地持久化即可。
                if self.hass is None:
                    return
                # v1.6.10（审计 N9）：命令已送达=成功。本地簿记（setpoints 写入/
                # 持久化 create_task——如 hass 关闭期抛 RuntimeError）失败此前会
                # 落入外层 except 触发 _revert_to_saved，把已生效的滑块错误回弹。
                # 独立 try 降级为告警，不回退
                try:
                    # 本地记录设定值：重新进入界面/HA 重启后仍回显上次设定位置
                    setpoints = self.hass.data.setdefault(DOMAIN, {}).setdefault(DEVICE_SETPOINTS, {})
                    setpoints.setdefault(self.device_sn, {})[self._param_key] = value
                    # 触发持久化保存（save_persistent_data 内部有防抖，连续操作只落盘一次）
                    from .persist import save_persistent_data
                    self.hass.async_create_task(save_persistent_data(self.hass))
                except Exception as pe:
                    _LOGGER.warning(
                        "设备 %s %s设定值本地保存失败（命令已送达，不回退显示）: %s",
                        self.device_sn, self._entity_label, pe,
                    )
                _LOGGER.info("设备 %s %s设置为 %d%%", self.device_sn, self._entity_label, value)
            else:
                _LOGGER.warning("设备 %s %s设置命令发送失败，不保存设定值", self.device_sn, self._entity_label)
                self._revert_to_saved()
        except Exception as e:
            _LOGGER.error("设置设备 %s %s失败: %s", self.device_sn, self._entity_label, e)
            self._revert_to_saved()

    def _revert_to_saved(self):
        """发送失败：滑块回退到已保存的设定值（无保存则回到未设定状态）"""
        # 守卫：实体已移除时 async_write_ha_state 会崩溃（v1.6.3）
        if self.hass is None:
            return
        self._attr_native_value = None
        self._update_state()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """实体移除时取消挂起的防抖定时器，避免回调在实体销毁后触发"""
        if self._debounce_handle:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        await super().async_will_remove_from_hass()


class WindowControllerSpeedNumber(WindowControllerRangeNumber):
    """开窗速度滑动条（rwp_winact_speed，0-100%）"""

    _entity_suffix = "speed"
    _entity_label = "速度"
    _entity_icon = "mdi:speedometer"
    _command = COMMAND_SET_SPEED
    _param_key = "speed"


class WindowControllerStrengthNumber(WindowControllerRangeNumber):
    """开窗力度滑动条（rwp_winact_strength，0-100%）"""

    _entity_suffix = "strength"
    _entity_label = "力度"
    _entity_icon = "mdi:arm-flex"
    _command = COMMAND_SET_STRENGTH
    _param_key = "strength"


# 所有数值调节实体类（新增 rwp_winact_* 参数时在此追加）
_NUMBER_ENTITY_CLASSES = (WindowControllerSpeedNumber, WindowControllerStrengthNumber)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """设置数值调节（Number）平台"""
    _LOGGER.info("设置数值调节平台: %s", entry.entry_id)

    domain_data = hass.data[DOMAIN]
    entry_data = domain_data.get(entry.entry_id)

    if not entry_data:
        _LOGGER.error("配置条目数据未找到: %s", entry.entry_id)
        return

    device_manager = entry_data.get("device_manager")
    mqtt_handler = entry_data.get("mqtt_handler")

    if not device_manager or not mqtt_handler:
        _LOGGER.error("设备管理器或MQTT处理器未找到")
        return

    gateway_sn = entry.data[CONF_GATEWAY_SN]

    # 设备SN -> {实体后缀: 实体实例}，用于移除时清理
    created_numbers = {}

    async def on_device_added(device_sn: str, device_name: str, device_type: str):
        """设备添加回调，自动创建滑动条实体"""
        if device_type == DEVICE_TYPE_WINDOW_OPENER:
            from .utils import async_get_entity_id as _aget_eid
            entities_to_add = []
            created = {}
            for cls in _NUMBER_ENTITY_CLASSES:
                # 会话内幂等短路（v1.6.3）：设备重同步会重复触发本回调，
                # 重复创建会叠加 add_status_callback（旧实例回调无人摘除 → 泄漏）。
                # 注册表查重存在 async_add_entities 尚未落库的窗口，故先查会话字典。
                if cls._entity_suffix in created_numbers.get(device_sn, {}):
                    _LOGGER.debug("%s实体本会话已创建，跳过: %s", cls._entity_label, device_sn)
                    continue
                unique_id = f"{gateway_sn}_{device_sn}_{cls._entity_suffix}"
                # 兼容新旧 HA 的 entity 查找（跨会话残留实体查注册表）
                if await _aget_eid(hass, "number", unique_id) is not None:
                    _LOGGER.debug("%s实体已存在，跳过创建: %s", cls._entity_label, device_sn)
                    continue
                number = cls(
                    hass, device_manager, mqtt_handler,
                    gateway_sn, device_sn, device_name, str(entry.entry_id)
                )
                entities_to_add.append(number)
                created[cls._entity_suffix] = number
                # 注册状态更新回调，005 上报时即时刷新
                mqtt_handler.add_status_callback(device_sn, number.async_update)
                _LOGGER.info("自动为设备 %s 添加%s滑动条", device_name, cls._entity_label)
            if entities_to_add:
                async_add_entities(entities_to_add)
            if created:
                created_numbers.setdefault(device_sn, {}).update(created)

    async def on_device_removed(device_sn: str, device_name: str, device_type: str):
        """设备移除回调，清理滑动条实体"""
        if device_type == DEVICE_TYPE_WINDOW_OPENER and device_sn in created_numbers:
            numbers = created_numbers.pop(device_sn)
            try:
                from .utils import call_registry_method as _call_reg
                from .utils import async_get_entity_id as _aget_eid
                entity_registry = get_entity_registry(hass)
                for number in numbers.values():
                    mqtt_handler.remove_status_callback(device_sn, number.async_update)
                    # v1.6.19（第六轮审计 B-LOW6）：unique_id 优先（button.py
                    # v1.6.3 定案同款）——动态配对后秒级解绑时 async_add_entities
                    # 可能尚未给实体分配 entity_id，原 `if number.entity_id:`
                    # 落空即注册表条目悬挂；重新配对时查重命中悬挂条目又跳过
                    # 创建 → 该设备永久缺 number 实体（直到重启）。
                    _eid = await _aget_eid(hass, "number", number._attr_unique_id)
                    if _eid:
                        await _call_reg(entity_registry.async_remove, _eid)
                    elif number.entity_id:
                        await _call_reg(entity_registry.async_remove, number.entity_id)
                    else:
                        _LOGGER.warning("滑动条实体定位失败（unique_id=%s 双路径均未命中）: %s",
                                        number._attr_unique_id, device_name)
                _LOGGER.info("已移除设备 %s 的滑动条实体", device_name)
            except Exception as e:
                _LOGGER.error("移除设备 %s 的滑动条实体失败: %s", device_name, e)

    device_manager.set_device_added_callback(on_device_added)
    device_manager.set_device_removed_callback(on_device_removed)

    # 启动循环无条件创建实体（与 button/sensor 一致）：
    # 注册表条目跨重启/重载持久保留，用注册表查重会导致重启后实体无平台实例。
    entities = []
    for device in device_manager.get_all_devices():
        if device.get("type") == DEVICE_TYPE_WINDOW_OPENER:
            device_sn = device["sn"]
            created_numbers[device_sn] = {}
            for cls in _NUMBER_ENTITY_CLASSES:
                number = cls(
                    hass, device_manager, mqtt_handler,
                    gateway_sn, device_sn, device["name"], str(entry.entry_id)
                )
                entities.append(number)
                created_numbers[device_sn][cls._entity_suffix] = number
                mqtt_handler.add_status_callback(device_sn, number.async_update)

    if entities:
        async_add_entities(entities)
        _LOGGER.info("已添加 %d 个滑动条实体", len(entities))
