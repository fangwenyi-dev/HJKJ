"""开窗器网关Cover平台 - 供LLM等使用Cover语义控制开窗器"""
import logging
import math
import time

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverEntity,
    CoverEntityFeature,
    CoverDeviceClass,
)

from .base_entity import WindowControllerBaseEntity
from .const import (
    DOMAIN,
    CONF_GATEWAY_SN,
    DEVICE_TYPE_WINDOW_OPENER,
    MANUFACTURER,
    COMMAND_OPEN,
    COMMAND_CLOSE,
    COMMAND_STOP,
    COMMAND_SET_POSITION,
    POSITION_CAPABLE_SN_PREFIXES,
    POSITION_COALESCE_SECONDS,
    DEVICE_STATUS_OPEN,
    DEVICE_STATUS_CLOSED,
    DEVICE_STATUS_UNKNOWN,
    DEVICE_STATUS_CONNECTED,
    CONF_EXPOSE_COVER_AS_CURTAIN,
    DEFAULT_EXPOSE_COVER_AS_CURTAIN,
)

_LOGGER = logging.getLogger(__name__)


from .utils import device_is_stale, get_entity_registry


class WindowControllerCover(WindowControllerBaseEntity, RestoreEntity, CoverEntity):
    """开窗器Cover实体 - 供LLM等使用Cover语义控制"""

    def __init__(
        self,
        hass: HomeAssistant,
        device_manager,
        mqtt_handler,
        gateway_sn: str,
        device_sn: str,
        device_name: str,
        entry_id: str = None,
        as_curtain: bool = False
    ):
        """初始化开窗器Cover实体"""
        super().__init__(
            hass=hass,
            device_manager=device_manager,
            mqtt_handler=mqtt_handler,
            gateway_sn=gateway_sn,
            device_sn=device_sn,
            device_name=device_name
        )

        self._attr_unique_id = f"{gateway_sn}_{device_sn}_cover"
        # v1.6.23：兼容 vivohomebridge——其 cover 枚举仅放行 device_class
        # ==curtain（源码 vbridge.py 实证）；options 开启后以窗帘身份
        # 暴露（cover 控制语义 open/close/stop/position 两侧协议一致）
        self._attr_device_class = (
            CoverDeviceClass.CURTAIN if as_curtain else CoverDeviceClass.WINDOW
        )
        self._attr_name = "开窗器"
        self._entry_id = entry_id
        # v1.7.21（机型能力分流）：SN 前四位即机型码，百分比能力逐机型不同
        # （5001/5003/5005/5006/5007 支持；5002 平开窗暂不支持——见 const.py
        # 权威矩阵）。未知前缀一律按"不支持"兜底：宁可退化为三态（功能完整、
        # 不会假动作），也不能在无百分比硬件上声明 SET_POSITION。
        self._position_capable = str(device_sn)[:4] in POSITION_CAPABLE_SN_PREFIXES
        self._attr_supported_features = (
            CoverEntityFeature.OPEN |
            CoverEntityFeature.CLOSE |
            CoverEntityFeature.STOP |
            # v1.7.20/1.7.21（HomeKit Window 映射根治）：上游
            # homekit/accessories.py 决策树实证——window + SET_POSITION →
            # Window 服务（位置磁贴）；缺该位 → WindowCoveringBasic（Apple
            # 自己做三段式：>70 开 / <30 关 / 中间停=暂停）。所以这一位既
            # 决定"是否显示为窗户"，也决定"有没有暂停"——只对真支持百分比
            # 的机型声明，两种形态各自诚实。位置端点不会重新引入置灰：
            # v1.6.16 的防线是 assumed_state=True（前端 canOpen/canClose
            # 公式被其短路，见下方注释）。
            (CoverEntityFeature.SET_POSITION if self._position_capable else 0)
        )
        # v1.7.21 位置命令合并（机制二）运行时状态：pending 值 + 定时器 +
        # 上次真实下发时刻（monotonic）。仅位置命令走合并，开/关/停是离散
        # 用户动作，逐次下发。
        self._pending_position = None
        self._coalesce_handle = None
        self._last_position_send = 0.0
        # 始终可用，防止变灰
        self._attr_available = True
        # v1.6.16（用户定案：原生卡片开/关/停三键任何状态下必须可点）：
        # 置灰判据实锤于 home-assistant/frontend src/data/cover.ts——
        #   canOpen  = assumed_state || (!isFullyOpen  && !isOpening)
        #   canClose = assumed_state || (!isFullyClosed && !isClosing)
        #   canStop  = 仅排除 unavailable（从不受开/闭状态影响）
        # 即 state=open 时「开」被禁、closed 时「关」被禁——用户所见"灰色"。
        # assumed_state=True 短路前两式 → 三键恒可下发；HA 状态机
        # （CoverEntity.state 由 is_closed 计算）完全不受该属性影响，
        # v1.6.8 定案的真实 open/closed（历史曲线/自动化/LLM 语义）原样保留。
        # 语义诚实性同样成立：协议规定网关只能被动上报（002/005），HA 无法
        # 回查实际窗位，手动拉绳等旁路移动不会即时反映——本就不是可回读态。
        self._attr_assumed_state = True
        self._last_state_update = None

    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息"""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_sn)},
            name=self.device_name,
            manufacturer=MANUFACTURER,
            model="开窗器",
            serial_number=self.device_sn,
            sw_version="1.0"
        )

    @property
    def is_closed(self):
        """v1.6.8：由网关上报缓存推导真实开/闭。

        v1.0.1 起这里写死 return None（本意是防原生卡片按钮变灰），
        副作用是 HA 标准 state 计算（is_closed=None → state=None）
        使 cover.state **永远输出 unknown**——历史曲线、自动化触发条件、
        LLM 语义控制与 Web 管理面板状态行全部失效。现恢复真实语义：
        窗闭合时原生卡片「关」按钮置灰属 HA 正常行为；Web 面板按钮为
        自定义控件不受影响。位置端点（0/100）连带置灰的风险由 v1.6.16
        assumed_state 统一防线接管；v1.7.20 起 current_cover_position
        暴露真实位置供 HomeKit Window。
        """
        device = self.device_manager.get_device(self.device_sn)
        if device:
            # v1.6.19（第六轮审计 B-MED3）：与 sensor 的 15 分钟时效同判据
            # （SENSOR_TIMEOUT_MINUTES，v1.6.12 #7 定案）。网关长期失联时
            # sensor/binary_sensor 已转 unknown，此处不设闸则 cover.state
            # 永久冻结在最后已知值——同设备两实体矛盾显示、自动化按陈旧
            # "open" 持续动作。恢复值（async_added_to_hass）在重启时刻获得
            # 时间戳，语义=「信任关机快照 15 分钟」，与 v1.6.8 恢复设计
            # 自洽；无时间戳的设备（历史形态/测试夹具）视为新鲜。
            if device_is_stale(device):
                return None
            status = device.get("status")
            if status == DEVICE_STATUS_CLOSED:
                return True
            if status == DEVICE_STATUS_OPEN:
                return False
            # v1.6.8（用户定案：状态与位置同步——r_travel 0=关，>0=开）：
            # 防御性兜底。现行链路里 r_travel 总是与推导出的 status 同时写入
            # （002 _update_device_attributes 与 005 attrs 分支均如此），此分支
            # 正常不触发；保留是防固件将来「只推位置不带状态字段」时出现
            # 「待上报 + 位置 65%」的矛盾显示
            r_travel = (device.get("attributes") or {}).get("r_travel")
            try:
                if r_travel is not None:
                    # v1.6.11（外部审计 #2）：int() 截断会把 0.5 判成"关"
                    # （int(0.5)=0 ≤0），违反用户定案的">0=打开"语义。协议
                    # 规定整数 0-100，但 JSON 可携浮点——用 float 直比，
                    # 非数值串由既有 except (ValueError, TypeError) 落 None
                    # v1.7.64（第三轮复核 #3 同类收口）：这条转换还漏两种死法——
                    # ① JSON 大整数进 float() 抛的是 OverflowError，旧元组接不住，
                    #   异常从实体属性直接炸穿状态机；
                    # ② inf/nan 转换不抛错，而 `inf<=0`、`nan<=0` 皆 False ⇒ 垃圾值
                    #   被静默解释成"打开"。非有限数一律落 None（未知），与
                    #   ws_gateway._as_int / hub_client._attr_int 同口径。
                    position = float(r_travel)
                    if math.isfinite(position):
                        return position <= 0
            except (ValueError, TypeError, OverflowError):
                pass
        return None

    async def async_added_to_hass(self):
        """启动时把上次重启前的开/关状态与位置回填设备缓存（v1.6.8）。

        协议规定网关只能主动推送（002/005），HA 无法主动查询，而
        device_manager 缓存不跨重启——修复前每次 HA 重启后所有子设备
        状态都要等下一次网关上报才有值（unknown 窗口最长可达上报周期）。
        恢复值仅在缓存尚无实时数据时写入；真实上报到达后自然覆盖。
        """
        await super().async_added_to_hass()
        try:
            last_state = await self.async_get_last_state()
        except Exception as e:
            _LOGGER.debug("获取 %s 历史状态失败: %s", self.device_sn, e)
            return
        if not last_state or last_state.state not in ("open", "closed"):
            return
        device = self.device_manager.get_device(self.device_sn)
        if device is None:
            return
        if device.get("status") not in (None, DEVICE_STATUS_UNKNOWN, DEVICE_STATUS_CONNECTED):
            return  # 本会话已有实时上报，不覆盖
        attributes = {}
        last_attrs = last_state.attributes or {}
        # v1.6.26（第八轮审计 B-2）：优先恢复**原始** r_travel——旧实现只存
        # 钳制后 position，未校准标记 255 被洗成 100，重启后"未校准"语义
        # 永久丢失（固件口径 255 直接丢弃，WS 视图应维持 -1）。raw 越界时
        # 只回填开关状态、位置保持未知；无 raw 字段的 v1.6.25 旧数据按
        # 钳制值回填（向后兼容）。
        raw = last_attrs.get("r_travel_raw")
        try:
            if raw is not None:
                raw = int(raw)
                if 0 <= raw <= 100:
                    attributes["r_travel"] = raw
            else:
                pos = last_attrs.get("position")
                if pos is not None:
                    attributes["r_travel"] = max(0, min(100, int(pos)))
        except (ValueError, TypeError, OverflowError):
            # v1.7.64（#3 同类）：restore 状态里的 r_travel_raw/position 是 HA 自己
            # 落盘的历史值，形态不可控——inf/极大浮点进 int() 抛的是 OverflowError，
            # 旧元组接不住 ⇒ async_added_to_hass 整段恢复失败（重启后状态永远回填不上）
            pass
        status = DEVICE_STATUS_OPEN if last_state.state == "open" else DEVICE_STATUS_CLOSED
        _LOGGER.info(
            "恢复设备 %s 重启前状态: %s%s", self.device_sn, status,
            "（位置 %s%%）" % attributes["r_travel"] if "r_travel" in attributes else ""
        )
        await self.device_manager.update_device_status(self.device_sn, status, attributes or None)

    @property
    def is_closing(self):
        """始终返回False，确保关闭按钮不会变灰"""
        return False

    @property
    def is_opening(self):
        """始终返回False，确保打开按钮不会变灰"""
        return False

    @property
    def current_cover_position(self):
        """v1.7.20：暴露真实位置（HomeKit Window/LLM/卡片滑块的输入面）。

        优先级：
        1. 设备缓存 r_travel ∈ [0,100] → 如实返回（校准后电机上报）；
        2. 未校准（255）/非法/缺失 → 按开/关状态端点兜底（用户拍板方案 a，
           与固件"open=全开、close=全关"物理端点一致；部分行程后停机且
           未校准时会有偏差，校准后自动恢复精确）；
        3. 状态超时（SENSOR_TIMEOUT_MINUTES 同 is_closed 判据）/无缓存 →
           None（HomeKit 保持上次值，不谎报）。

        历史注记：v1.0.1~v1.6.19 此属性恒 None 是"防原生卡片按钮置灰"的
        双保险；v1.6.16 起防置灰正解已是 assumed_state=True 短路前端
        canOpen/canClose 判据（见 __init__ 注释），本属性暴露不再影响三键
        常亮——而 HomeKit Window accessory 恰恰**必须**位置能力
        （上游 type_covers.py 实证）。

        v1.7.21：无百分比能力的机型（5002 平开窗等）恒返 None——它走
        WindowCoveringBasic 形态，位置由 Apple 依 state 自行映射 0/100，
        我们谎报数值只会让"假滑块"更有迷惑性。
        """
        if not self._position_capable:
            return None
        device = self.device_manager.get_device(self.device_sn)
        if not device:
            return None
        # 与 is_closed 同款时效闸：网关长期失联时不输出陈旧位置
        if device_is_stale(device):
            return None
        r_travel = (device.get("attributes") or {}).get("r_travel")
        try:
            raw = int(r_travel)
            if 0 <= raw <= 100:
                return raw
        except (ValueError, TypeError, OverflowError):
            # v1.7.64（#3 同类）：inf/极大浮点进 int() 抛的是 OverflowError，旧元组
            # 接不住会直接从属性炸出去；接住它就落到下方的开/关端点兜底
            pass
        status = device.get("status")
        if status == DEVICE_STATUS_OPEN:
            return 100
        if status == DEVICE_STATUS_CLOSED:
            return 0
        return None

    @property
    def extra_state_attributes(self):
        """返回额外状态属性，供用户查看设备实际位置和状态"""
        attrs = {}
        # v1.7.21：机型百分比能力（Web 面板/诊断可见；HomeKit 侧由
        # supported_features 的 SET_POSITION 位体现）
        attrs["position_capable"] = self._position_capable
        device = self.device_manager.get_device(self.device_sn)
        if device:
            # v1.7.33（全量审计）：位置/状态属性此前不受时效闸约束——
            # current_cover_position（上方）有 15 分钟闸，extra_state_attributes
            # 无闸，失联设备于是出现「圆点灰未知 + 状态:打开 + 位置 65%」并存，
            # 且 Web 面板滑块仍可拖。同判据收敛（None=新鲜的历史形态语义不变，
            # 见 is_closed 处的 v1.6.19 注释）。
            if device_is_stale(device):
                attrs["device_status"] = DEVICE_STATUS_UNKNOWN
                attrs["position_stale"] = True
                return attrs
            status = device.get("status")
            if status:
                attrs["device_status"] = status
            attributes = device.get("attributes", {})
            r_travel = attributes.get("r_travel")
            if r_travel is not None:
                try:
                    raw = int(r_travel)
                    attrs["position"] = max(0, min(100, raw))
                    # v1.6.26（第八轮审计 B-2）：一并持久化原始值，
                    # 恢复路径据此区分"真 100%"与"未校准 255 被钳成 100"
                    attrs["r_travel_raw"] = raw
                except (ValueError, TypeError, OverflowError):
                    # v1.7.64（#3 同类）：属性字典在 HA 状态机里被逐轮构造，这里
                    # 逃逸的异常会让实体整轮状态写不出去（Web 面板停在旧值）
                    pass
        return attrs

    async def async_update(self) -> None:
        """定期更新状态，防止实体被HA标记为unavailable"""
        # 守卫：实体被移除后 hass 为 None，残留轮询直接返回（2026-08-28 实测崩溃点）
        if self.hass is None:
            return
        self._attr_available = True
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs) -> None:
        """打开开窗器（v1.6.9：失败如实上抛，此前吞异常+不查返回值=假成功）"""
        try:
            success = await self._get_mqtt_handler().send_command(self.device_sn, COMMAND_OPEN)
        except Exception as e:
            _LOGGER.error("Cover打开失败 %s: %s", self.device_sn, e)
            raise HomeAssistantError(f"打开失败：{e}") from e
        if not success:
            raise HomeAssistantError("打开失败：命令未送达（网关或设备离线）")
        _LOGGER.info("Cover打开: %s", self.device_sn)

    async def async_close_cover(self, **kwargs) -> None:
        """关闭开窗器（v1.6.9：失败如实上抛）"""
        try:
            success = await self._get_mqtt_handler().send_command(self.device_sn, COMMAND_CLOSE)
        except Exception as e:
            _LOGGER.error("Cover关闭失败 %s: %s", self.device_sn, e)
            raise HomeAssistantError(f"关闭失败：{e}") from e
        if not success:
            raise HomeAssistantError("关闭失败：命令未送达（网关或设备离线）")
        _LOGGER.info("Cover关闭: %s", self.device_sn)

    async def async_stop_cover(self, **kwargs) -> None:
        """停止开窗器（v1.6.9：失败如实上抛）"""
        try:
            success = await self._get_mqtt_handler().send_command(self.device_sn, COMMAND_STOP)
        except Exception as e:
            _LOGGER.error("Cover停止失败 %s: %s", self.device_sn, e)
            raise HomeAssistantError(f"停止失败：{e}") from e
        if not success:
            raise HomeAssistantError("停止失败：命令未送达（网关或设备离线）")
        _LOGGER.info("Cover停止: %s", self.device_sn)

    async def async_set_cover_position(self, **kwargs) -> None:
        """定位到指定开度 0-100（v1.7.20：HomeKit Window 滑块/
        cover.set_cover_position 服务入口）。

        v1.7.21 命令合并（机制二：首发立即 + 窗口内只发最终值）：
        Apple 窗子磁贴拖动期**持续写** TargetPosition（实机日志 34→46→47
        间隔约 250ms），旧实现逐条直发 004 → 一次拖动十几条报文全压到 LoRa
        空口。现行为：
          - 距上次真实下发 ≥ POSITION_COALESCE_SECONDS 的首次调用：立即下发
            （保住 v1.6.9 failfast：未送达仍同步抛 HomeAssistantError）；
          - 窗口内后续调用：只更新 pending 并重置定时器（界面回显由 Apple
            自行乐观吸附，不受影响）；
          - 静默 POSITION_COALESCE_SECONDS 后：补发最后一条（此时已无调用方
            可抛错，失败只落日志——这是机制二的契约边界，用户已拍板）。
        越界/非法值仍在**合并之前**即拒（v1.6.19 B-LOW11 口径）。
        """
        position = kwargs.get(ATTR_POSITION)
        try:
            position_int = int(position)
        except (ValueError, TypeError, OverflowError) as err:
            # v1.7.33：raise ... from err——异常链保留原始原因（排障时能看出
            # 是 "abc"/None 还是 inf 溢出），HA 日志逐层可读
            raise HomeAssistantError(
                f"设置位置失败：无效的位置值 {position!r}") from err
        if not 0 <= position_int <= 100:
            raise HomeAssistantError(f"设置位置失败：位置超出范围(0-100): {position_int}")
        if not self._position_capable:
            # 该机型无百分比硬件（5002 等）：HA 核心服务层本已按
            # supported_features 拦截（ServiceValidationError），此处兜底
            # 防内部误用——绝不把无效的 w_travel 指令打到 LoRa 空口上。
            raise HomeAssistantError(
                f"设置位置失败：机型 {str(self.device_sn)[:4]} 不支持百分比定位"
            )
        # 首发立即：无 pending 且在合并窗口之外
        if self._pending_position is None and (
            time.monotonic() - self._last_position_send
        ) >= POSITION_COALESCE_SECONDS:
            await self._send_position(position_int)
            return
        # 窗口内：只记最后值，静默窗口结束后补发
        self._pending_position = position_int
        if self._coalesce_handle is not None:
            self._coalesce_handle.cancel()
        if self.hass is None:
            # 实体已被移除（拖滑块后立即删设备）：无法建定时器则直接放弃，
            # 不留悬挂 pending
            self._pending_position = None
            return
        self._coalesce_handle = self.hass.loop.call_later(
            POSITION_COALESCE_SECONDS, self._on_coalesce_fired
        )

    def _on_coalesce_fired(self):
        """合并窗口结束：仅补发最后一次设定的位置（机制二 trailing 段）"""
        self._coalesce_handle = None
        value = self._pending_position
        self._pending_position = None
        # 守卫（number 实体 v1.6.3 同款）：实体已移除时不再创建发送任务
        if self.hass is None or value is None:
            return
        self.hass.async_create_task(self._send_position_deferred(value))

    async def _send_position(self, position_int: int, raise_on_failure: bool = True) -> bool:
        """真实下发 004 set_position。

        raise_on_failure=False（合并补发路径）时不抛错：那一路已无调用方
        在等，失败只能落日志，由调用方法自行告警。
        """
        try:
            success = await self._get_mqtt_handler().send_command(
                self.device_sn, COMMAND_SET_POSITION, {"position": position_int})
        except Exception as e:
            if raise_on_failure:
                _LOGGER.error("Cover设置位置失败 %s: %s", self.device_sn, e)
                raise HomeAssistantError(f"设置位置失败：{e}") from e
            _LOGGER.error("Cover设置位置(合并补发)异常 %s: %s", self.device_sn, e)
            return False
        if not success:
            if raise_on_failure:
                raise HomeAssistantError("设置位置失败：命令未送达（网关或设备离线）")
            return False
        # 仅成功才记窗口起点：失败后应立即允许重试，不被合并窗口拖延
        self._last_position_send = time.monotonic()
        _LOGGER.info("Cover设置位置: %s → %d%%", self.device_sn, position_int)
        return True

    async def _send_position_deferred(self, position_int: int) -> None:
        """补发（trailing）路径：无调用方在等，失败只落日志并标注当时值。

        v1.6.3/v1.6.4/v1.6.10 三条教训照抄：create_task 出去后实体可能已被
        删除（TOCTOU）——每个 await 前重新确认 self.hass，且后台任务不得
        抛出未处理异常。
        """
        if self.hass is None:
            return
        try:
            success = await self._send_position(position_int, raise_on_failure=False)
        except Exception as e:  # noqa: BLE001 —— 后台任务兜底
            _LOGGER.error("Cover设置位置(合并补发)异常 %s: %s", self.device_sn, e)
            return
        if not success:
            _LOGGER.warning(
                "Cover设置位置(合并补发)未送达 %s → %d%%（调用方已返回，无法回抛）",
                self.device_sn, position_int,
            )

    async def async_will_remove_from_hass(self) -> None:
        """实体移除：取消合并定时器并清空 pending（number 实体 v1.6.3 同款）"""
        if self._coalesce_handle is not None:
            self._coalesce_handle.cancel()
            self._coalesce_handle = None
        self._pending_position = None
        await super().async_will_remove_from_hass()

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """设置Cover实体"""
    _LOGGER.info("设置Cover平台: %s", entry.entry_id)

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

    created_covers = {}

    async def on_device_added(device_sn: str, device_name: str, device_type: str):
        """设备添加回调，自动创建Cover实体"""
        if device_type == DEVICE_TYPE_WINDOW_OPENER:
            # 会话内幂等短路（v1.6.3）：设备重同步会重复触发本回调；
            # async_add_entities 到注册表落库存在窗口，注册表查重挡不住连续事件
            if device_sn in created_covers:
                _LOGGER.debug("Cover实体本会话已创建，跳过: %s", device_sn)
                return

            from .utils import async_get_entity_id as _aget_eid

            cover_unique_id = f"{gateway_sn}_{device_sn}_cover"
            # 兼容新旧 HA 的 entity 查找（新版 async_get_entity_id 为 async 方法——2026-08-28 修复）
            cover_exists = await _aget_eid(hass, "cover", cover_unique_id) is not None

            if cover_exists:
                _LOGGER.debug("Cover实体已存在，跳过创建: %s", device_sn)
                return

            cover = WindowControllerCover(
                hass,
                device_manager,
                mqtt_handler,
                gateway_sn,
                device_sn,
                device_name,
                str(entry.entry_id),
                as_curtain=bool(entry.options.get(
                    CONF_EXPOSE_COVER_AS_CURTAIN,
                    DEFAULT_EXPOSE_COVER_AS_CURTAIN))
            )
            async_add_entities([cover])
            created_covers[device_sn] = cover
            # v1.6.12（第五轮审计 #5）：注册设备状态回调（对齐 number/sensor）——
            # 此前 cover 只靠 HA 轮询（默认 5 分钟），005 上报到达后传感器/滑块
            # 即时刷新而 cover 卡片滞后，违背 v1.6.8「cover.state 可驱动历史曲线、
            # 自动化触发条件」的定案
            mqtt_handler.add_status_callback(device_sn, cover.async_update)
            _LOGGER.info("自动为设备 %s 添加Cover实体", device_name)

    async def on_device_removed(device_sn: str, device_name: str, device_type: str):
        """设备移除回调，清理相关Cover实体"""
        if device_type == DEVICE_TYPE_WINDOW_OPENER:
            if device_sn in created_covers:
                cover = created_covers[device_sn]
                del created_covers[device_sn]
                # v1.6.12：摘除本会话注册的设备状态回调（与 number 移除路径对称）
                mqtt_handler.remove_status_callback(device_sn, cover.async_update)

                try:
                    from .utils import call_registry_method as _call_reg
                    from .utils import async_get_entity_id as _aget_eid
                    entity_registry = get_entity_registry(hass)
                    # v1.6.19（第六轮审计 B-LOW6）：unique_id 优先（button.py
                    # v1.6.3 定案同款）——配对后秒级解绑时实体可能尚未获派
                    # entity_id，原单路径落空即注册表悬挂、重配对永久缺 cover。
                    _eid = await _aget_eid(hass, "cover", cover._attr_unique_id)
                    if _eid:
                        await _call_reg(entity_registry.async_remove, _eid)
                        _LOGGER.info("已移除设备 %s 的Cover实体", device_name)
                    elif cover.entity_id:
                        await _call_reg(entity_registry.async_remove, cover.entity_id)
                        _LOGGER.info("已移除设备 %s 的Cover实体", device_name)
                    else:
                        _LOGGER.warning("Cover实体定位失败（unique_id=%s 双路径均未命中）: %s",
                                        cover._attr_unique_id, device_name)
                except Exception as e:
                    _LOGGER.error("移除Cover实体失败 %s: %s", device_name, e)

    device_manager.set_device_added_callback(on_device_added)
    device_manager.set_device_removed_callback(on_device_removed)

    entities = []
    devices = device_manager.get_all_devices()
    for device in devices:
        if device.get("type") == DEVICE_TYPE_WINDOW_OPENER:
            device_sn = device["sn"]
            device_name = device["name"]

            # 启动循环无条件创建 Cover：
            # 注册表条目跨重启/重载持久保留，用注册表查重会导致重启后
            # 实体只有注册表条目、没有平台实例（不可用）。
            # 重复添加由 HA 按 unique_id 自动去重（替换更新）。
            cover = WindowControllerCover(
                hass,
                device_manager,
                mqtt_handler,
                gateway_sn,
                device_sn,
                device_name,
                str(entry.entry_id),
                as_curtain=bool(entry.options.get(
                    CONF_EXPOSE_COVER_AS_CURTAIN,
                    DEFAULT_EXPOSE_COVER_AS_CURTAIN))
            )
            entities.append(cover)
            created_covers[device_sn] = cover
            # v1.6.12（第五轮审计 #5）：启动循环同样注册状态回调，
            # 005 上报即时刷新 cover（此前只有轮询路径）
            mqtt_handler.add_status_callback(device_sn, cover.async_update)

    if entities:
        async_add_entities(entities)
        _LOGGER.info("已添加 %d 个Cover实体", len(entities))
