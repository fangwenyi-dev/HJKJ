"""开窗器网关集成"""
import logging
import asyncio
from datetime import timedelta
from typing import Any, Dict, Final, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform, EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    DOMAIN, 
    CONF_GATEWAY_SN, 
    CONF_GATEWAY_NAME,
    DEFAULT_GATEWAY_NAME,
    SCAN_INTERVAL,
    DEVICE_TO_GATEWAY_MAPPING,
    GLOBAL_MANUALLY_REMOVED_DEVICES,
    GLOBAL_IGNORED_GATEWAYS,
    DEVICE_SETPOINTS,
    RESTART_DELAY,
    CONF_AUTO_DISCOVERY,
    CONF_DEBUG_LOGGING,
)
from .persist import load_persistent_data, save_persistent_data
from .services import register_services
from .api import async_setup_api
from .hub_client import (HUB_DEFAULT_BASE, HUB_DEFAULT_INSTALL_KEY, HubClient,
                         resolve_hub_base)
from .utils import (is_mqtt_loaded, iter_devices, async_write_instance_uuid_file,
                    log_throttled)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SENSOR, Platform.COVER]

# 发现平台名称
DISCOVERY_PLATFORM = "window_controller_gateway"

# 记录已开启 debug_logging 的配置条目。
# 模块 logger 由多个 entry 共享，直接 setLevel 会互相覆盖且在卸载后不恢复，
# 因此用引用计数：任一 entry 开启则 DEBUG，全部关闭/卸载后恢复 NOTSET（继承 HA logger 配置）。
_debug_logging_entries: set = set()



async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
    """设置集成 - Home Assistant调用此函数加载集成"""
    _LOGGER.info("=== 开窗器网关集成初始化 ===")
    hass.data.setdefault(DOMAIN, {})
    
    # 初始化全局设备到网关映射表
    hass.data[DOMAIN].setdefault(DEVICE_TO_GATEWAY_MAPPING, {})
    hass.data[DOMAIN].setdefault(GLOBAL_MANUALLY_REMOVED_DEVICES, set())
    hass.data[DOMAIN].setdefault(DEVICE_SETPOINTS, {})
    
    # 加载持久化数据
    await load_persistent_data(hass)
    
    # 设置发现平台
    try:
        from .discovery import async_setup_discovery_platform
        await async_setup_discovery_platform(hass)
        _LOGGER.info("开窗器网关发现平台设置成功")
    except Exception as e:
        _LOGGER.error("设置开窗器网关发现平台失败: %s", e)
    

    if not register_services(hass):
        return False

    # 注册供插件 Web UI 调用的设备列表 REST 端点。
    # 背景：HA Core 仅通过 WebSocket 暴露 device_registry（config/device_registry/list），
    # 不提供 REST 端点；而插件 Web UI（ingress）只能经 Supervisor 代理走 REST
    # （/api/ha/ -> http://supervisor/core/api/）。此视图在 HA 内部序列化设备注册表，
    # 使 Web UI 能列出某配置条目下的网关（父）与子设备。
    async_setup_api(hass)

    return True

DISCOVERY_INTERVAL_MIN_SECONDS: Final = 60
DISCOVERY_INTERVAL_MAX_SECONDS: Final = 3600


def _clamp_discovery_interval(raw) -> int:
    """发现间隔归一与钳制（v1.7.33 全量审计）。

    该值是 mqtt_handler.check_connection() 的唯一节拍源（网关离线回收只在
    这一拍里跑）：options 里的原值可能被用户调到 3600（离线回收滞后 1 小时）
    或被 .storage 手改成字符串（timedelta(seconds="300") → TypeError → 条目
    setup 直接失败、集成起不来且无自愈）。此处统一 int 归一 + 钳到
    [60, 3600]，越界留 WARNING 且不抛。
    """
    if isinstance(raw, timedelta):
        raw = raw.total_seconds()
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        seconds = int(SCAN_INTERVAL)
    if seconds < DISCOVERY_INTERVAL_MIN_SECONDS or seconds > DISCOVERY_INTERVAL_MAX_SECONDS:
        _LOGGER.warning(
            "发现间隔 %s 超出允许范围（%d-%d 秒），已钳制——该值同时是网关"
            "离线回收的唯一节拍源", seconds,
            DISCOVERY_INTERVAL_MIN_SECONDS, DISCOVERY_INTERVAL_MAX_SECONDS,
        )
        seconds = min(max(seconds, DISCOVERY_INTERVAL_MIN_SECONDS),
                      DISCOVERY_INTERVAL_MAX_SECONDS)
    return seconds


def _apply_debug_logging(entry_id: str, enabled: bool) -> None:
    """按引用计数设置/恢复模块 logger 级别（v1.8.5 G-6 抽出的共用出口）。

    完整设置分支与 awaiting 分支共用本函数。抽出的直接原因：awaiting 分支原先
    对 entry.options 零读取，而"网关还没被发现"恰是最需要 DEBUG 的时刻——选项页
    对等待条目可达，保存 debug_logging 却零效果且无声。

    引用计数语义不变：只有集合清空才把级别恢复为 NOTSET（继续继承 HA logger
    配置），避免多条目互相覆盖、卸载后不恢复（P1 修复的原始动机）。
    """
    if enabled:
        _debug_logging_entries.add(entry_id)
        _LOGGER.setLevel(logging.DEBUG)
        _LOGGER.info("调试日志已启用")
    else:
        _debug_logging_entries.discard(entry_id)
        if not _debug_logging_entries:
            _LOGGER.setLevel(logging.NOTSET)  # 恢复为继承 HA logger 配置
            _LOGGER.info("调试日志已关闭（模块日志级别恢复为继承设置）")


def _auto_discovery_enabled(entry) -> bool:
    """awaiting 分支的 auto_discovery 门控（v1.8.5 G-6）。

    与 mqtt_handler._lifecycle._auto_discovery_enabled 同语义、同默认值：字段缺失、
    取不到配置、任何异常都保持 True（历史行为，绝不能把用户已有网关的自动发现
    因为一次读取失败而静默关掉）。awaiting 条目没有 device_manager，那条路径的
    消费方触不到，故此处按 entry.options 直读。
    """
    try:
        options = getattr(entry, "options", None) or {}
        return bool(options.get(CONF_AUTO_DISCOVERY, True))
    except Exception:
        return True


def _make_hub_control(hass: HomeAssistant):
    """hub 下行命令 → 本仓 004 控制路径。

    语义与 LAN WS 网关 `_cmd_control` 一致：ok = QoS1 已发布到 broker，
    不代表设备已执行（执行实据靠状态上报）；命令不重发。仅向"设备所属条目"
    发布（映射命中即止），避免多条目广播造成重复控制。
    """

    async def _control(dev_sn: str, attribute: str, value: str) -> bool:
        for data in list(hass.data.get(DOMAIN, {}).values()):
            if not isinstance(data, dict):
                continue
            manager = data.get("device_manager")
            handler = data.get("mqtt_handler")
            if manager is None or handler is None:
                continue
            if dev_sn in getattr(manager, "devices", {}):
                return bool(await handler.send_ws_raw_004(dev_sn, attribute, value))
        return False
    return _control


# 慧尖云 hub 出站长连：**一个 HA 安装一个实例**（不是每网关条目一个）。
# 归属放在 DOMAIN 级，是因为身份文件本来就在全局 config_dir——每条目各建实例会
# 互相覆盖同一份身份，HA 重启后全部用同一个 instanceId 去连，hub 的 onAgent 把
# 前一条顶掉 ⇒ N 台网关抢一条长连，且小程序只看到其中一台（用户报障原形）。
from .const import HUB_DATA_KEY, HUB_STOP_LISTENER_KEY, HUB_STOPPED_KEY  # noqa: E402


def _hub_managers(hass: HomeAssistant) -> List[Any]:
    """当前已完成设置的全部条目的 device_manager（聚合口径同 WS 网关）。"""
    out: List[Any] = []
    for data in list(hass.data.get(DOMAIN, {}).values()):
        if not isinstance(data, dict) or not data.get("_setup_complete"):
            continue
        manager = data.get("device_manager")
        if manager is not None:
            out.append(manager)
    return out


def _hub_expected_manager_count(hass: HomeAssistant) -> int:
    """**本该**挂上几个 manager（v1.8.5 审计 G-2）。

    判据＝当前启用的、带网关 SN 的条目数（与 _hub_managers 的聚合口径同源）：
    · 条目**真被删除** ⇒ 它已不在 async_entries 里 ⇒ 期望数同步下降
      ⇒ 缺的那台本就该被 hub 淘汰，快照仍可权威（保住 v1.7.59 幽灵设备修复）；
    · 条目 **reload 中／冷启动分步 setup** ⇒ 条目还在，只是 `_setup_complete`
      尚未落地 ⇒ 期望数 > 已挂数 ⇒ 快照判不权威、本轮不上行。

    awaiting（等待态）条目 `data={}` 无 SN，天然不计入——它本来也没有设备。
    取不到 config_entries（测试桩/极早期）时回 0＝无缺口，退回旧行为。
    """
    try:
        entries = list(hass.config_entries.async_entries(DOMAIN))
    except Exception:  # noqa: BLE001 - 无 config_entries（测试桩）时按无缺口
        return 0
    n = 0
    for ent in entries:
        if getattr(ent, "disabled_by", None):
            continue
        try:
            if (ent.data or {}).get(CONF_GATEWAY_SN):
                n += 1
        except Exception:  # noqa: BLE001 - 单条目异常不影响计数口径
            continue
    return n


def _hub_option(hass: HomeAssistant, key: str) -> str:
    """取"任一条目里非空的那个覆盖值"（只有一个实例，不存在改了不生效）。

    审计 2026-09-30 E-2：只看**有效**条目（自过滤 disabled_by，与本仓
    utils.entry_state_for_sn 的单一真源口径同判）。禁用条目的 options 不会被
    _hub_managers 那条路采纳（条目未 setup 就没有 manager），却会在这里被读成
    hub 地址/密钥——于是"禁用旧网关后云端长连仍指向旧 hub"。
    """
    try:
        entries = list(hass.config_entries.async_entries(DOMAIN))
    except Exception:  # noqa: BLE001 - 无 config_entries（测试桩）时退回内置默认
        entries = []
    for ent in entries:
        if getattr(ent, "disabled_by", None):
            continue
        try:
            val = (ent.options or {}).get(key)
        except Exception:  # noqa: BLE001
            val = None
        if val:
            return str(val)
    return ""


async def async_ensure_hub_client(hass: HomeAssistant) -> None:
    """按当前条目集合聚合 hub 长连：拉起 / 换挂 manager / 无网关则停。

    幂等：在任一 config entry setup/unload 尾部与 HA STOP 时调用（与
    async_ensure_ws_gateway 同一批调用点）。失败只记日志——远程控制通道
    不得影响本地功能（对齐 WS 网关既定语义）。
    """
    if DOMAIN not in hass.data:
        return
    domain_data = hass.data[DOMAIN]
    managers = _hub_managers(hass)
    current = domain_data.get(HUB_DATA_KEY)

    if not managers:
        await async_stop_hub_client(hass)
        return

    # 审计 2026-09-30 C-2：停机闩锁。STOP 之后条目还会逐个 unload，每条 unload
    # 尾部都调本函数；没有这把锁就会在关机过程中把刚停掉的长连**重新拉起**
    # （HubClient.async_start 里是 ensure_future(_run_forever)），而它的 STOP
    # 监听注册时事件已派发过 ⇒ 任务与 aiohttp 会话无人回收，云端看到一次
    # instance 抖动。同形状的 WS 网关早已定案必须加锁（ws_gateway.py:977-980
    # 的注释："若无此闩锁会把刚停掉的服务器在关机过程中重新拉起"）。
    if domain_data.get(HUB_STOPPED_KEY):
        return

    # v1.8.6（审计 G-10）：attach 支此前**只挂集合、不比对端点与密钥**。base/install_key
    # 是构造期绑死的（hub_client.py:378-379，长连与重连全读 self.base），于是
    # "端点可用 entry.options 覆盖"（本文件下方注释的自述）在多条目下等于空头承诺：
    # A 卸载尾部调本函数时 managers=[B] 非空 ⇒ 只 attach ⇒ 新配置永不生效，而
    # "端点被覆盖"那行日志只在创建支打 ⇒ 连"用的是旧端点"都看不见。同形态在 ws_gateway
    # 早已定案"比对 + 必要时热同步"（ws_gateway.py:1096-1102）——两条通道同判据是本仓纪律。
    # 判据在**分支之前**算一次：创建支与重建路用同一份值，也不给本函数添第二个调用点
    # （test_v1744 有条钉在数全仓 ensure 调用点，递归会把它顶成 4）。
    base = resolve_hub_base(_hub_option(hass, "hub_base"))
    key = _hub_option(hass, "hub_install_key") or HUB_DEFAULT_INSTALL_KEY
    if current is not None and (current.base != base or current.install_key != key):
        if domain_data.get(HUB_STOPPED_KEY) or getattr(hass, "is_stopping", False):
            # 停机窗内绝不重建：新实例的 STOP 监听注册时事件已派发过 ⇒ 任务与 aiohttp
            # 会话无人回收（C-2 那条实锤教训，见 _register_hub_stop_listener）。
            # 此刻端口配置改不改都无所谓——进程马上要退了，交给下一个 ensure。
            _LOGGER.debug("hub 端点/密钥与实例不一致，但处于停机窗，本次不重建")
            current.attach_managers(managers)
            return
        # 密钥不回显（本仓口径：只说变了，不说变成什么）
        _LOGGER.warning(
            "hub 端点/安装密钥与在跑的长连不一致（端点 %s → %s；密钥%s）"
            "——重建长连让配置生效（旧实例继续打旧端点会表现为"
            "\"改了不生效、面板却显示已保存\"）",
            current.base, base,
            "已变更" if current.install_key != key else "未变")
        await async_stop_hub_client(hass)
        current = None

    if current is None:
        if base != HUB_DEFAULT_BASE:
            # 非内置默认＝有覆盖（entry.options 或 HUIJIAN_HUB_BASE 环境变量，后者是
            # 真栈 e2e 的黑洞杠杆）——显式记一行，覆盖永不静默（否则误设环境变量把生产
            # HA 指到别处时无从察觉）。
            _LOGGER.info("慧尖云 hub 端点被覆盖为 %s（非内置默认）", base)
        client = HubClient(
            managers,
            config_dir=hass.config.config_dir,
            base=base,
            install_key=key,
            control_fn=_make_hub_control(hass),
            # v1.8.5（审计 G-2）：把"该有几个 manager"的判据注入长连。缺了回调时
            # build_state_snapshot 退回旧行为（只防零 manager）——见该方法 docstring。
            expected_managers_fn=lambda: _hub_expected_manager_count(hass),
        )
        domain_data[HUB_DATA_KEY] = client
        try:
            await client.async_start()
        except Exception as e:  # noqa: BLE001 - 起不来就别留半个注册
            _LOGGER.error("慧尖云 hub 长连启动失败（不影响本地功能）: %s", e, exc_info=True)
            if domain_data.get(HUB_DATA_KEY) is client:
                domain_data.pop(HUB_DATA_KEY, None)
            try:
                await client.async_stop()
            except Exception:  # noqa: BLE001
                pass
            return
        _register_hub_stop_listener(hass, domain_data)
        return

    current.attach_managers(managers)


def _register_hub_stop_listener(hass: HomeAssistant, domain_data: Dict[str, Any]) -> None:
    """STOP 监听只注册一次（照抄 v1.7.33 对 ws_gateway"句柄不存不摘"那条教训）。"""
    if domain_data.get(HUB_STOP_LISTENER_KEY):
        return
    # 审计 2026-09-30 C-2 复核补口：ensure 若在 async_start 的让出点里被 STOP
    # 抢先（闩锁已置、事件已派发），走到这里再注册的 listen_once 永不触发，
    # 句柄还要等下一个人来摘——直接不注册（hass.is_stopping 是仓内既有用法）。
    if domain_data.get(HUB_STOPPED_KEY) or getattr(hass, "is_stopping", False):
        return
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    async def _on_ha_stop(_event) -> None:
        # 审计 2026-09-30 C-2：先置停机闩锁、再停通道——与 ws_gateway 的
        # WS_GATEWAY_STOPPED_KEY 完全同构（那边的注释已是定案："STOP 后 entry
        # 逐个 unload 也会调本函数，若无此闩锁会把刚停掉的服务器在关机过程中
        # 重新拉起"）。hub 侧此前只有"监听只注册一次"，没有锁：≥2 条目停机时
        # STOP 回调是并发任务，hub 的 _on_ha_stop 先跑完并 pop 掉 HUB_DATA_KEY，
        # 另一条目 unload 尾部再 ensure ⇒ 关机中拉起新实例，而它的 STOP 监听
        # 注册时事件已派发过 ⇒ 任务与 aiohttp 会话无人回收，云端看到一次
        # instance 抖动。摘除侧也一并交给 from_stop（见 async_stop_hub_client）。
        # 置停机闩锁在 async_stop_hub_client(from_stop=True) 里做，本回调只负责
        # 把"这是 STOP 派发"这件事告诉它（一处置锁，避免两条路径口径分叉）。
        await async_stop_hub_client(hass, from_stop=True)

    try:
        domain_data[HUB_STOP_LISTENER_KEY] = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    except Exception:  # noqa: BLE001 - 无 bus 环境（测试桩）不应阻断通道
        domain_data[HUB_STOP_LISTENER_KEY] = None


async def async_stop_hub_client(hass: HomeAssistant, *, from_stop: bool = False) -> None:
    """停掉安装级长连并摘键；幂等，二次调用不炸。

    审计 2026-09-30 C-4：`from_stop` 只在 HA STOP 派发路径为真（见
    _register_hub_stop_listener 的回调）。async_listen_once 的一次性监听器
    **在事件派发时就被总线摘掉了**，此时再 unsub() 会让 HA core 打
    "Unable to remove unknown listener" —— 同一形态在 :1140-1147（v1.7.31 F-A）
    已被现场实锤修过（"0918 现场每次停机每条目一条，日志噪声盖真故障"），
    本处是照抄注册侧、漏抄摘除侧。条目 unload 路径（非 STOP）里监听器还在，
    该摘还是要摘，所以不能无条件跳过。
    """
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return
    if from_stop:
        # 审计 2026-09-30 C-2：闩锁在停机侧置位（async_ensure_hub_client 据此
        # 拒绝在关机过程中重新拉起）。
        domain_data[HUB_STOPPED_KEY] = True
    current = domain_data.get(HUB_DATA_KEY)
    unsub = domain_data.pop(HUB_STOP_LISTENER_KEY, None)
    if callable(unsub) and not from_stop:
        try:
            unsub()
        except Exception:  # noqa: BLE001
            pass
    if current is None:
        return
    try:
        await current.async_stop()
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("停止 hub 客户端失败: %s", e)
    finally:
        # 判等再 pop：async_stop 有真实让出点，期间并发 ensure 可能已登记新实例
        if domain_data.get(HUB_DATA_KEY) is current:
            domain_data.pop(HUB_DATA_KEY, None)



async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """设置配置条目

    支持两种模式：
    - 有 gateway_sn：完整设置（MQTT + 设备管理器）
    - 无 gateway_sn：最小设置（仅注册平台），等待用户通过选项页或自动发现添加网关
    """
    gateway_sn = entry.data.get(CONF_GATEWAY_SN, "")
    _LOGGER.info("=== 开始设置配置条目: %s, gateway: %s ===", entry.entry_id, gateway_sn or "(待配置)")

    # 审计 2026-09-30 C-1：域级服务的**重注册**落点。注销有位置
    # （async_remove_entry 在最后一个条目删除时按名 async_remove），重注册却没有——
    # register_services 只在 DOMAIN 级 async_setup(:70) 调过一次，而组件一旦进了
    # hass.config.components，HA 就不会再跑 async_setup，此后新建条目只走
    # async_setup_entry。于是"删掉全部条目 → 再加一台网关"这条正常操作路径上，
    # 7 个域级服务永久缺席（面板 start_pairing / check_gateway_status /
    # rename_device 直接 Service not found，且没有任何自愈入口，直到重启 HA）。
    # 原 :930 注释"register_services 每次 setup 全覆盖注册"把 DOMAIN setup 当成了
    # 条目 setup，前提本身是错的。async_register 是覆盖式的，逐条目重入无害；
    # 注册失败仍按原语义让本条目 setup 失败（与 :70 同一判据）。
    if not register_services(hass):
        return False
    
    try:
        from .device_manager import WindowControllerDeviceManager
        from .mqtt_handler import WindowControllerMQTTHandler
    except ImportError as e:
        _LOGGER.critical("导入核心模块失败: %s", e)
        return False

    # v1.7.33（全量审计）：先清掉上一次生命周期遗留的**状态类**键——
    # async_unload_entry 失败时不 pop 该条目字典，下次 setup 的 previous.update()
    # 会把 _platforms_forwarded=True / _bg_tasks / unsub_listeners 原样继承：
    # 门禁据此对从未 forward 的平台调 async_unload_platforms（每平台一条
    # "Config entry was never loaded!" ERROR），旧任务/监听器列表也持续累积
    # （已无引用可取消）。平台附加键（created_*）不受影响，仍在 update 中保留。
    #
    # 审计 2026-09-30 H-3 复核（本批行为钉抓出）：清理原先只写在**完整设置**
    # 分支里，awaiting 分支（无 SN）不经过 ⇒ 残留 _bg_tasks 会被
    # setdefault(...).append() 追加进旧列表（每次失败卸载累积一条死任务引用）。
    # 提到分支分流之前，两分支同口径。
    _prev_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(_prev_data, dict):
        for _stale in ("_platforms_forwarded", "_bg_tasks", "unsub_listeners"):
            _prev_data.pop(_stale, None)

    # v1.7.60：实例指纹落盘（HA 配置目录，跨容器可见）。容器侧发现代理在
    # "HA 的 MQTT 客户端收不到任何上报"时用同一指纹做 001 兜底应答；写在
    # 两分支之前——等待条目与已配置条目都要保证该文件存在。
    await async_write_instance_uuid_file(hass)

    # ---- 无网关 SN：最小设置，等待后续配置 ----
    if not gateway_sn:
        _LOGGER.info("网关 SN 未配置，进入等待模式（可通过选项页或自动发现添加）")
        hass.data[DOMAIN].setdefault(entry.entry_id, {})
        hass.data[DOMAIN][entry.entry_id]["gateway_sn"] = ""
        hass.data[DOMAIN][entry.entry_id]["_awaiting_gateway"] = True
        # v1.8.5 G-6：本分支此前对 entry.options 零读取。选项页对等待条目是可达的
        # （config_flow async_step_init 的 E-1 菜单），但等待条目既没有 mqtt_handler
        # 也没有 device_manager——debug_logging 与 auto_discovery 那两个消费方一个
        # 都触不到，用户保存后零效果、且无声。而"网关还没被发现"恰是最需要 DEBUG
        # 的时刻（发现链断裂、心跳耳异常都只在这段可见）。
        #
        # 此处消费 debug_logging，与完整设置分支共用同一引用计数出口
        # （_apply_debug_logging）。只在本次要开启、或本条目已登记在案时才调用：
        # 默认 False 且未登记时保持不动，避免每条 awaiting 条目 setup 都白打一行
        # "调试日志已关闭"。auto_discovery 的消费点在心跳耳内（发现卡调用前）。
        _awaiting_debug = bool((entry.options or {}).get(CONF_DEBUG_LOGGING, False))
        if _awaiting_debug or entry.entry_id in _debug_logging_entries:
            _apply_debug_logging(entry.entry_id, _awaiting_debug)
        # discovery_interval 在本分支无消费方（它的唯一节拍源 mqtt_handler.
        # check_connection() 要等网关配置后才存在），故此处不读；用户在此页改它
        # 会在条目转正后的完整分支生效。
        # 无网关 SN：不 forward 任何平台实体。
        # 历史实现 forward 了 4 个空平台，但各平台 async_setup_entry 在
        # device_manager 缺失时会打 error 日志（"设备管理器未找到"），
        # 且与完整 PLATFORMS 的卸载集合不一致。config entry 的加载状态
        # 由 async_setup_entry 返回值决定，与是否 forward 平台无关，
        # 因此 forward 空列表即可。
        # v1.7.12（第 6 轮审计 E-6）注释订正：旧尾句"卸载时 PLATFORMS 对未
        # 加载平台是安全 no-op"已被 v1.7.11 真栈证伪——HA≥2024 平台组件对
        # never-loaded 条目 async_unload_entry 抛 ValueError "Config entry
        # was never loaded!"（ERROR 风暴），卸载必须按 _platforms_forwarded
        # 实际转发记录门禁（见 :356 定义与卸载分支），勿回退。

        # 轻量级心跳监听器：订阅 gateway/rpt_rsp，发现新网关时自动触发发现流程
        # 这让"先装集成、后上电网关"的自动发现流程成为可能
        try:
            from .const import TOPIC_GATEWAY_RSP
            from .discovery import async_discover_gateway

            _unsub_heartbeat = None

            async def _heartbeat_listener(msg):
                """监听网关心跳，触发自动发现"""
                try:
                    import json
                    # v1.7.33（全量审计）：与 _protocol 耳同款入站尺寸闸。
                    # 旧实现只在 handler 侧有闸，等待态条目的心跳耳（干净主机
                    # 首配形态下**唯一**的应答者）直接 json.loads 全量 payload
                    # ——mosquitto 默认不限 message_size_limit，一条 50-100MB
                    # 报文就在事件循环线程卡死整个 HA。留痕走节流（防刷盘）。
                    from .utils import inbound_payload_ok, log_throttled
                    if not inbound_payload_ok(msg):
                        log_throttled(hass, "_hb_oversize_logged", "rpt_rsp",
                                      600.0, _LOGGER.warning,
                                      "心跳耳收到超大 MQTT 报文（%d 字节），拒收处理",
                                      len(msg.payload))
                        return
                    payload = json.loads(msg.payload)
                    if "head" not in payload or "ctype" not in payload:
                        return
                    response_sn = payload.get("sn")
                    if not response_sn or not isinstance(response_sn, (str, int, float)):
                        return
                    if isinstance(response_sn, bool):
                        return
                    response_sn = str(response_sn)
                    import re
                    if not re.match(r"^[a-zA-Z0-9]{10,}$", response_sn):
                        return

                    # 检查是否已配置（v1.7.31 A-3 三态门 → v1.7.34 四态：
                    # 禁用条目不算已配置——BUG-5 统一口径；未加载条目同样不算
                    # ——正式 handler 没挂订阅时耳朵必须顶上。仅"在飞/已加载"
                    # 才整体静默让位 handler）
                    from .utils import entry_state_for_sn, log_throttled
                    _st = entry_state_for_sn(hass, response_sn)
                    if _st == "configured":
                        return
                    if _st == "disabled":
                        log_throttled(
                            hass, "_hb_disabled_logged", response_sn.lower(), 600.0,
                            _LOGGER.warning,
                            "网关 %s 的条目处于禁用状态但仍上报——继续代答 001 止血"
                            "（风暴不停在禁用侧无解），但不弹发现卡（尊重禁用决策）；"
                            "如需恢复使用请到 设置→设备与服务 启用该条目。每 SN 10 分钟去重",
                            response_sn)
                    if _st == "not_loaded":
                        # v1.7.34：旧实现此形态返回 "configured" ⇒ 两耳一起静默
                        # 让位一个不存在的 handler，网关 001 无人应答且日志零留痕。
                        log_throttled(
                            hass, "_hb_unloaded_logged", response_sn.lower(), 600.0,
                            _LOGGER.warning,
                            "网关 %s 的条目已配置但未加载（setup 失败或等待重试），"
                            "正式 handler 未挂订阅——本耳继续代答 001 止血，但不弹"
                            "发现卡（条目已在列表里）。根因请到 设置→设备与服务 查看"
                            "该条目的错误提示，或检索日志中本集成的 setup 异常。"
                            "每 SN 10 分钟去重",
                            response_sn)

                    # v1.7.26 用户裁定 A / v1.7.27 格式定稿 / v1.7.30 仲裁收口：
                    # 未配置网关首报 001 耳朵级代答——固件每 5s 重发直到收到应答，
                    # 旧链条在条目转正前无人应答，转正链一断即无限重试风暴。代答
                    # 与正式 handler 应答完全同形（含 uuid）。v1.7.30 ②：改走统一
                    # 仲裁入口（台架实锤 1 请求 2~3 答，倍数=应答者数），并补齐
                    # data 归一（v1.7.30 审计收编：001 带 data:null 时 _protocol
                    # 耳归一后照答、本耳谓词见非 dict 拒答——干净主机只有本耳，
                    # 该形态下风暴不止血）。
                    from .utils import (should_ear_ack_001,
                                        async_ear_ack_001_arbitrated)
                    _ear_data = payload.get("data")
                    if not isinstance(_ear_data, dict):
                        _ear_data = {}
                    if should_ear_ack_001(payload.get("ctype"), _ear_data):
                        if await async_ear_ack_001_arbitrated(
                                hass, response_sn, payload.get("id", 0)):
                            _LOGGER.info("耳朵已代答 001 绑定应答（未配置网关）: %s",
                                         response_sn)

                    if _st in ("disabled", "not_loaded"):
                        # 代答已做；发现卡对"用户主动禁用"是打扰（A-3 裁定），
                        # 对"条目已在列表里只是没加载起来"更是纯噪音
                        # （async_discover_gateway 第 3 步命中同 SN 条目本就早退）。
                        return
                    gateway_name = f"慧尖网关 {response_sn[-4:]}"
                    # v1.8.5 G-6：本调用此前不看 options 的 auto_discovery。选项页
                    # 对 awaiting 条目可达（config_flow E-1 菜单），但等待条目没有
                    # mqtt_handler，`_auto_discovery_enabled` 那个唯一消费方触不到
                    # ——用户取消勾选后发现卡照弹。门控加在调用侧。
                    if not _auto_discovery_enabled(entry):
                        _LOGGER.info(
                            "心跳监听器发现新网关 %s，但 auto_discovery 已关闭"
                            "——不弹发现卡（可在选项页重新开启）", response_sn)
                        return
                    _LOGGER.info("心跳监听器发现新网关: %s (SN: %s)", gateway_name, response_sn)
                    await async_discover_gateway(hass, response_sn, gateway_name)
                except Exception as e:
                    # v1.7.31（A-4）：兜底从 DEBUG 升 WARNING+节流——0917 取证
                    # 铁律"归因行必须 WARNING"，DEBUG 形态下代答/发现/注册表
                    # 任一异常默认级别零可见，发现链断裂无迹可寻（v1.7.30 ③
                    # 自己立的规矩，本监听器是全链唯一残留 DEBUG 的面）。
                    # 节流防 5s 风暴刷屏；exc_info 留栈定位根因。
                    from .utils import log_throttled
                    log_throttled(
                        hass, "_hb_err_logged", repr(e)[:200], 600.0,
                        _LOGGER.warning,
                        "心跳监听器处理消息出错（每形态 10 分钟去重，发现链可能"
                        "静默断裂——持续出现请查 MQTT 通道与网关上报格式）: %s",
                        e, exc_info=True)

            # v1.7.11：awaiting 条目自身也要驱动 MQTT bootstrap——客户可能
            # 从未走过 config_flow（快速发现代理建的正是这种零交互等待条目），
            # 干净主机上 mqtt 条目不存在时心跳监听器会等 120s 超时失效，
            # 整条自动发现链静默断掉。语义与 config_flow 空 SN 分支同款：
            # 尽力而为，失败不阻塞（稍后就绪即可，武装任务会等到）。
            try:
                from .mqtt_bootstrap import ensure_mqtt_connection
                await ensure_mqtt_connection(hass)
            except ConfigEntryNotReady:
                pass  # broker 稍后就绪（加载项启动竞态窗口），武装任务兜底
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("等待模式 MQTT 引导异常（不阻塞，后台武装兜底）: %s", e)
            # v1.7.29 A / v1.7.30 ④：bootstrap 持久自愈（每 hass 单实例幂等）
            # ——标记未落地时重试 ensure（v1.7.30 起 300s 指数退避封顶 1h），
            # 失败升修复条目；不再"错过 setup 即静默等重启"
            from .mqtt_bootstrap import async_start_bootstrap_healer
            async_start_bootstrap_healer(hass)

            _subscribed_now = False
            if is_mqtt_loaded(hass):
                from homeassistant.components import mqtt as mqtt_comp
                # v1.7.12（第 6 轮审计 CF-F4）：即时订阅单独兜异常——旧版
                # subscribe 抛错直接落最外层 except，else 分支的后台武装被
                # 整体跳过，本条目生命周期内自动发现静默死亡。失败转入武装
                # 重试路径（同下方 A-2 设计）。
                try:
                    _unsub_heartbeat = await mqtt_comp.async_subscribe(
                        hass, TOPIC_GATEWAY_RSP, _heartbeat_listener, 1
                    )
                    hass.data[DOMAIN][entry.entry_id]["_unsub_heartbeat"] = _unsub_heartbeat
                    # v1.7.60：就绪判据拆两态——订阅只依赖"集成已加载"，但
                    # "能听见"还要求客户端真连上 broker。旧日志把两者混为一谈
                    # （恒打"已启动…等待网关上电"），现场"一条都收不到"时
                    # 日志全绿、零归因。未连接时改打带修复方向的节流 WARNING。
                    from .utils import is_mqtt_connected
                    if is_mqtt_connected(hass):
                        _LOGGER.info("已启动网关心跳监听器，等待网关上电...")
                    else:
                        log_throttled(
                            hass, "_hb_not_connected_logged", entry.entry_id,
                            600.0, _LOGGER.warning,
                            "MQTT 集成已加载但客户端未连上 Broker——心跳订阅已挂"
                            "（连接恢复后自动生效），此前收不到任何网关上报。"
                            "若长时间如此：到 设置→设备与服务→MQTT 核对该条目"
                            "指向与凭据（应然 127.0.0.1:2022）。每 10 分钟去重"
                        )
                    _subscribed_now = True
                except Exception as sub_e:  # noqa: BLE001
                    _LOGGER.warning(
                        "心跳即时订阅失败（%s），转入后台武装重试", sub_e)

            if not _subscribed_now:
                # v1.6.26（第八轮审计 A-2）：旧实现只武装一次——加载项首启的
                # 典型时序里 MQTT 条目由本集成的 bootstrap 稍后异步创建，
                # is_mqtt_loaded 此刻为假即永久放弃，自动发现整链静默失效。
                # 改为后台任务等待 MQTT 就绪后再订阅（进 _bg_tasks，卸载/
                # 重载时统一取消；订阅前后双重检查条目数据仍在，防悬挂资源）。
                async def _arm_heartbeat_when_mqtt_ready():
                    from homeassistant.components import mqtt as mqtt_comp
                    from .utils import async_wait_mqtt_loaded
                    # v1.7.28（现场实锤"等待 MQTT 集成 120s 仍未就绪，心跳
                    # 监听器未武装"）：旧实现一轮 120s 即弃——加载项重启/首配
                    # 窗口里 MQTT 恢复常晚于 120s，耳朵从此永久失聪直到条目
                    # reload/HA 重启。改为无限期耐心武装（每 120s 一条节流留痕）；
                    # 任务在 _bg_tasks，卸载/reload 统一取消，data_now 双检兜底。
                    _waited = 0
                    _ensure_after = 0   # 到达该累计秒数即催一轮（首轮立刻催）
                    _gap = 120          # 每催一轮后翻倍，封顶 3600s（v1.7.63 C-8）
                    while not await async_wait_mqtt_loaded(hass, timeout=120.0):
                        _waited += 120
                        if hass.data.get(DOMAIN, {}).get(entry.entry_id) is None:
                            return  # 条目已卸载/重载，放弃武装
                        # v1.7.61（10-05 现场）：等待期间**顺带重试引导**。
                        # 旧实现只等不催——若 MQTT 条目没建成/被删（典型：只有一条
                        # source=ignore 的"被忽略"条目，永不加载），本循环会无限
                        # 干等，首台网关的自动添加永不成链。ensure 幂等（模块级锁 +
                        # 双重检查），标记不在时是 no-op；标记在位时这是除 healer
                        # 之外唯一的自愈点（healer 无标记只核验不重建）。
                        # 对抗复核 C2-c/C-8：重试带指数退避（120→240→…封顶 3600s），
                        # 与 v1.7.30"接管破坏面不得恒频施压"的纪律同向。
                        if _waited >= _ensure_after:
                            # v1.7.63（对抗复核 C-8）：旧式 max(120,x)*2 无界翻倍
                            # （注释承诺的 3600s 封顶并不存在）——改"绝对时刻 +
                            # 间隔封顶"：120→240→…→3600，之后每 3600s 催一次。
                            _ensure_after = _waited + _gap
                            _gap = min(_gap * 2, 3600)
                            try:
                                from .mqtt_bootstrap import ensure_mqtt_connection
                                await ensure_mqtt_connection(hass)
                            except Exception as _boot_e:  # noqa: BLE001 — 下轮再试
                                _LOGGER.debug(
                                    "等待就绪期间的引导重试失败（下轮再试）: %s",
                                    _boot_e)
                        _LOGGER.warning(
                            "MQTT 集成仍未就绪（累计 %ds），心跳武装持续等待——"
                            "请检查 MQTT 集成能否连上 broker（慧尖内置为 2022）",
                            _waited)
                    data_now = hass.data.get(DOMAIN, {}).get(entry.entry_id)
                    if data_now is None:
                        return  # 条目已卸载/重载，放弃武装
                    # v1.7.12（审计 CF-F4）：武装协程内订阅同样兜异常——旧版
                    # subscribe 抛错=task 未检索异常静默放弃，本条目再无人监听
                    # v1.7.33（全量审计）：失败不再一次即弃——MQTT 已就绪但订阅
                    # 瞬时失败（broker 重启竞态/换代期）会让本条目在剩余生命周期
                    # 内永久失聪（与 v1.7.28 修掉的"120s 即弃"同族，只换了触发点）。
                    # 改为 60s 退避无限重试（任务登记在 _bg_tasks，卸载即取消；
                    # 每轮条目存活双检）。留痕走节流防刷屏。
                    unsub = None
                    while unsub is None:
                        try:
                            unsub = await mqtt_comp.async_subscribe(
                                hass, TOPIC_GATEWAY_RSP, _heartbeat_listener, 1
                            )
                        except Exception as sub_e:  # noqa: BLE001
                            from .utils import log_throttled as _log_throttled
                            _log_throttled(
                                hass, "_hb_arm_fail_logged", entry.entry_id, 600.0,
                                _LOGGER.warning,
                                "心跳武装订阅失败，60s 后自动重试（无需手动干预）: %s",
                                sub_e)
                            await asyncio.sleep(60)
                            if hass.data.get(DOMAIN, {}).get(entry.entry_id) is None:
                                return  # 条目已卸载/重载，放弃武装
                    data_now = hass.data.get(DOMAIN, {}).get(entry.entry_id)
                    if data_now is None:
                        if unsub:
                            unsub()
                        return
                    if unsub:
                        data_now["_unsub_heartbeat"] = unsub
                        # v1.7.60：同首装分支的诚实化——订阅挂上 ≠ 能听见
                        from .utils import is_mqtt_connected
                        if is_mqtt_connected(hass):
                            _LOGGER.info("MQTT 就绪，已补装网关心跳监听器，等待网关上电...")
                        else:
                            _LOGGER.warning(
                                "MQTT 集成已加载但客户端未连上 Broker——心跳监听器"
                                "已补装，连接恢复前收不到任何网关上报；请核对"
                                "设置→设备与服务→MQTT 条目指向（应然 "
                                "127.0.0.1:2022）"
                            )

                _arm_task = hass.async_create_task(
                    _arm_heartbeat_when_mqtt_ready(),
                    name=f"{DOMAIN}_heartbeat_arm_{entry.entry_id}",
                )
                hass.data[DOMAIN][entry.entry_id].setdefault("_bg_tasks", []).append(_arm_task)
                _LOGGER.info("MQTT 尚未就绪，心跳监听器转入后台等待武装")
        except Exception as e:
            _LOGGER.warning("启动心跳监听器失败: %s（不影响手动添加）", e)

        # v1.6.26（第八轮审计 B-1）：awaiting 条目的唯一"转正"入口是
        # config_flow「添加网关」的 async_update_entry(data=+SN)——此前
        # update listener 只在完整设置分支注册，awaiting 条目改 data 后无人
        # 触发重载，配置静默不生效直至 HA 重启（v1.6.19 删显式 reload 时
        # 注释的"listener 已覆盖"前提对该分支为假）。按完整分支同款注册；
        # async_update_options 仅调 async_reload，awaiting 期重载是安全的。
        entry.async_on_unload(entry.add_update_listener(async_update_options))

        # v1.6.26（第八轮审计 A-3）：v1.6.16「半开口径」——条目存在即应监听
        # 9001（小程序可连、列表为空属正常）。awaiting-only 安装此前从不
        # 启动 WS 单例，小程序 mDNS 发现后恒 Connection refused。
        try:
            from .ws_gateway import async_ensure_ws_gateway
            await async_ensure_ws_gateway(hass)
        except Exception as e:
            _LOGGER.error("小程序 WS 网关检查失败（不影响其余功能）: %s", e, exc_info=True)

        # hub 长连**不在这里** ensure：awaiting 条目没有 device_manager，
        # `_hub_managers()` 对本条目恒为空，在此调用的净效果只剩"把别的条目
        # 已经拉起来的安装级长连停掉"。拉起/收拢只发生在完整设置分支与
        # unload/remove 三处（v1.7.44 修的就是这条落点）。
        return True

    # ---- 有网关 SN：完整设置 ----
    gateway_name = entry.data.get(CONF_GATEWAY_NAME, f"{DEFAULT_GATEWAY_NAME} {gateway_sn[-4:]}")
    
    device_manager = None
    mqtt_handler = None
    unsub_listeners = []

    try:
        # 先存储一个占位数据，确保平台设置时能够访问到基础数据
        hass.data[DOMAIN].setdefault(entry.entry_id, {})
        hass.data[DOMAIN][entry.entry_id]["gateway_sn"] = gateway_sn
        hass.data[DOMAIN][entry.entry_id]["gateway_name"] = gateway_name
        # v1.7.12（第 6 轮审计改进项）：删除死键 "_setup_in_progress"——
        # 全仓无任何读取方（含历史版本），纯占位误导维护者以为有防重入语义

        # 一体化插件：确保 MQTT 集成已建立连接（需要时按引导标记自动创建条目）。
        # 必须在创建 MQTT 处理器之前完成，否则订阅会因 MQTT 未就绪而失败。
        from .mqtt_bootstrap import ensure_mqtt_connection, async_start_bootstrap_healer
        await ensure_mqtt_connection(hass)
        # v1.7.29 A：同上——引导未落地时后台周期自愈，可见修复条目兜底
        async_start_bootstrap_healer(hass)

        # 创建设备管理器
        _LOGGER.debug("正在创建设备管理器...")
        device_manager = WindowControllerDeviceManager(hass, entry)

        # 快速注册网关设备（立即返回，给用户即时反馈）
        _LOGGER.debug("正在注册网关设备实体...")
        await device_manager.register_gateway_device()

        # 创建MQTT处理器（快速初始化，不等待连接）
        _LOGGER.debug("正在创建MQTT处理器...")
        mqtt_handler = WindowControllerMQTTHandler(hass, gateway_sn, device_manager)
        mqtt_setup_ok = await mqtt_handler.setup()
        if not mqtt_setup_ok:
            _LOGGER.error("MQTT处理器初始化失败，MQTT集成可能未启用")
            raise ConfigEntryNotReady("MQTT集成未启用，请先在Home Assistant中启用MQTT集成")
        
        # 预先将 device_manager 和 mqtt_handler 存储到 entry_data
        # 确保在设备加载回调触发时，平台可以访问到这些对象
        hass.data[DOMAIN][entry.entry_id]["device_manager"] = device_manager
        hass.data[DOMAIN][entry.entry_id]["mqtt_handler"] = mqtt_handler
        hass.data[DOMAIN][entry.entry_id]["gateway_sn"] = gateway_sn
        hass.data[DOMAIN][entry.entry_id]["gateway_name"] = gateway_name

        # 立即加载设备（在平台设置之前）
        _LOGGER.info("正在加载已存在的设备: %s, entry_id: %s", gateway_sn, entry.entry_id)
        try:
            await device_manager.setup()
        except Exception as e:
            _LOGGER.error("加载设备失败: %s", e)
            import traceback
            _LOGGER.error("堆栈跟踪: %s", traceback.format_exc())
            # P1 修复：抛出 ConfigEntryNotReady 让 HA 知道 setup 失败并自动重试，
            # 而不是静默继续（集成"看似在线实则无设备"，永不重试）
            raise ConfigEntryNotReady(f"设备加载失败: {e}") from e
        
        # 检查设备加载结果
        devices = device_manager.get_all_devices()
        _LOGGER.info("设备加载完成，共 %d 个设备: %s", len(devices), [d.get("sn") for d in devices])

        # 获取配置选项
        options = entry.options
        # v1.7.33（全量审计）：读取端补类型归一与值域钳制。该值是
        # mqtt_handler.check_connection() 的唯一节拍源（网关离线回收只在这里
        # 跑）：旧实现直接把 options 原值喂给 timedelta——用户调成 3600 则离线
        # 回收滞后 1 小时（实体假在线），.storage 手改成字符串则
        # timedelta(seconds="300") TypeError 落进 except 使条目 setup 直接失败
        # （集成起不来且无自愈）。钳到 [60, 3600] 与表单 schema 同界。
        discovery_interval = _clamp_discovery_interval(
            options.get("discovery_interval", SCAN_INTERVAL))
        debug_logging = options.get("debug_logging", False)
        
        # P1 修复：启用/禁用调试日志时使用引用计数控制模块 logger 级别。
        # 不再无条件 setLevel，避免多网关互相覆盖、卸载后不恢复。
        # v1.8.5 G-6：逻辑抽到 _apply_debug_logging，与 awaiting 分支共用。
        _apply_debug_logging(entry.entry_id, bool(debug_logging))

        # 设置状态定期更新（取消定时设备发现，只保留连接检查）
        async def periodic_update(_now):
            """定期检查连接状态"""
            try:
                await mqtt_handler.check_connection()
            except Exception as e:
                _LOGGER.warning("定期连接检查时出错: %s", e)

        # v1.7.33：seconds 已在上方归一/钳制（int 秒），此处不再做 timedelta 分支
        remove_interval = async_track_time_interval(hass, periodic_update, timedelta(seconds=discovery_interval))
        unsub_listeners.append(remove_interval)

        # 更新完整运行数据
        entry_data = {
            "gateway_sn": gateway_sn,
            "gateway_name": gateway_name,
            "device_manager": device_manager,
            "mqtt_handler": mqtt_handler,
            "unsub_listeners": unsub_listeners,
            "_setup_complete": True
        }
        # 合并已有数据（保留平台可能附加的键，如 created_remove_buttons）；
        # 状态类键的清理已在函数入口统一做（H-3 复核：两分支同口径）。
        previous = hass.data[DOMAIN].get(entry.entry_id, {})
        previous.update(entry_data)
        hass.data[DOMAIN][entry.entry_id] = previous

        # 恢复被 HA 自动禁用的实体（disabled_by="integration"）。
        # 背景：实体注册表中同一 unique_id 的平台/配置变迁（如旧版本按钮由其他
        # 平台创建、或升级后 domain 变化）会导致 HA 自动禁用实体，前端显示为
        # "已禁用"灰色。用户手动禁用的（disabled_by="user"）不做处理。
        # 必须在平台 forward 之前恢复，实体创建时即处于启用状态。
        try:
            entity_registry = er.async_get(hass)
            from .utils import call_registry_method as _call_reg
            restored_count = 0
            for entity_entry in list(entity_registry.entities.values()):
                if (entity_entry.platform == DOMAIN
                        and entity_entry.config_entry_id == entry.entry_id
                        and entity_entry.disabled_by is not None
                        and entity_entry.disabled_by != "user"):
                    await _call_reg(
                        entity_registry.async_update_entity,
                        entity_entry.entity_id, disabled_by=None
                    )
                    restored_count += 1
            if restored_count:
                _LOGGER.info("已恢复 %d 个被自动禁用的实体", restored_count)
        except Exception as e:
            _LOGGER.debug("恢复自动禁用实体失败（可忽略）: %s", e)

        # 设置平台（快速返回，不等待实体创建完成）
        _LOGGER.debug("正在设置前端平台组件...")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # v1.7.11：记录「本条目真的 forward 过平台」——async_unload_entry /
        # _cleanup_partial_setup 以此决定要不要按 PLATFORMS 卸载。awaiting
        # 条目从不 forward，硬编码卸载会让 HA≥2024 的平台组件对 never-loaded
        # 条目抛 ValueError "Config entry was never loaded!"，每个平台打一条
        # ERROR traceback（真栈实锤：代理自动填充触发的首个 reload 刷屏）。
        hass.data[DOMAIN][entry.entry_id]["_platforms_forwarded"] = True

        # 监听HA停止事件
        hass.data[DOMAIN][entry.entry_id]["_stop_unsub"] = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _make_shutdown_handler(hass, entry))

        # P0 修复 Bug #1：注册选项更新监听器，使配置选项变更即时生效
        entry.async_on_unload(entry.add_update_listener(async_update_options))

        # 创建后台任务，延迟触发发现；任务引用存入 _bg_tasks 供卸载时取消，
        # 避免任务在条目卸载/重载后继续存活并访问已清理的对象
        _bg_task = hass.async_create_task(
            _background_initialization(hass, entry.entry_id, mqtt_handler),
            eager_start=True,
            name=f"{DOMAIN}_background_init_{entry.entry_id}",
        )
        hass.data[DOMAIN][entry.entry_id].setdefault("_bg_tasks", []).append(_bg_task)

        # ============ 自动设备迁移（替换网关流程）暂禁用 ============
        # 迁移功能先不使用：即使 entry.data 中带 migration_info（替换网关流程
        # 创建的 entry），也不再自动触发设备迁移。重新启用时取消下面注释。
        # _LOGGER.info("检查是否需要执行设备迁移，entry.data: %s", entry.data)
        # migration_info = entry.data.get("migration_info")
        # if migration_info:
        #     old_gateway_sn = migration_info.get("old_gateway_sn")
        #     remove_old_gateway = migration_info.get("remove_old_gateway", False)
        #     if old_gateway_sn and old_gateway_sn.lower() != gateway_sn.lower():
        #         hass.async_create_task(_migrate_devices_async(hass, old_gateway_sn, gateway_sn, remove_old_gateway), name=f"{DOMAIN}_migrate_{entry.entry_id}")

        # v1.6.15：小程序局域网 WS 网关——任一 entry 选项开启即启动单例，
        # 失败只记日志（不阻断集成其余功能）
        try:
            from .ws_gateway import async_ensure_ws_gateway
            await async_ensure_ws_gateway(hass)
        except Exception as e:
            _LOGGER.error("小程序 WS 网关检查失败（不影响其余功能）: %s", e, exc_info=True)

        # 慧尖云 hub 出站长连（P0，客户零配置的远程控制通道）：**一个 HA 安装一条**，
        # 本条目只是"聚合触发点"（见 async_ensure_hub_client 的归属说明）。启动失败
        # 只降级重连、不影响本地功能（对齐 WS 网关"启动失败只记 error"的既定语义）。
        # 端点/密钥可用 entry.options 的 hub_base / hub_install_key 覆盖（P1 再进
        # config_flow 表单）。落点必须在**本分支**：awaiting 条目无 manager，
        # 挂在那边等于远程控制永不启动（v1.7.43 的实发回归，v1.7.44 修回）。
        try:
            await async_ensure_hub_client(hass)
        except Exception as e:  # noqa: BLE001 - 云通道故障不得拖累本地与 WS
            _LOGGER.error("慧尖云 hub 通道启动失败（不影响本地功能）: %s", e, exc_info=True)

        _LOGGER.info("开窗器网关 [%s] 设置完成", gateway_name)
        return True

    except ConfigEntryNotReady:
        # 不二次包装：保留原始 ConfigEntryNotReady 的可重试提示信息（HA 会展示给用户）
        # 清理 debug_logging 引用计数，避免失败 entry 永久占用 DEBUG 级别
        _debug_logging_entries.discard(entry.entry_id)
        if not _debug_logging_entries:
            _LOGGER.setLevel(logging.NOTSET)
        await _cleanup_partial_setup(mqtt_handler, device_manager, unsub_listeners,
                                     hass=hass, entry=entry)
        # 清理残留的 entry 数据，避免重试时读到脏状态
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.warning("设置网关 [%s] 失败（可重试），已清理部分初始化资源", gateway_name)
        raise
    except Exception as e:
        _LOGGER.error("设置网关 [%s] 过程中失败: %s", gateway_name, e, exc_info=True)
        # 清理 debug_logging 引用计数，避免失败 entry 永久占用 DEBUG 级别
        _debug_logging_entries.discard(entry.entry_id)
        if not _debug_logging_entries:
            _LOGGER.setLevel(logging.NOTSET)
        await _cleanup_partial_setup(mqtt_handler, device_manager, unsub_listeners,
                                     hass=hass, entry=entry)
        # 清理残留的 entry 数据，避免后续操作读到已清理的 manager 引用
        hass.data[DOMAIN].pop(entry.entry_id, None)
        return False

async def _cleanup_partial_setup(mqtt_handler, device_manager, unsub_listeners,
                                 hass=None, entry=None) -> None:
    """清理 async_setup_entry 中途失败时已创建的部分资源（幂等，各步骤独立容错）"""
    if mqtt_handler:
        try:
            await mqtt_handler.cleanup()
        except Exception as e:
            _LOGGER.debug("清理MQTT处理器异常: %s", e)
    if device_manager and hasattr(device_manager, 'cleanup'):
        try:
            await device_manager.cleanup()
        except Exception as e:
            _LOGGER.debug("清理设备管理器异常: %s", e)
    for unsub in (unsub_listeners or []):
        try:
            unsub()
        except Exception as e:
            _LOGGER.debug("取消监听器异常: %s", e)
    # v1.6.26（第八轮审计 A-1B）：forward 之后（:286 起）才抛异常的失败路径，
    # 5 个平台已加载——不清则僵尸实体持已 cleanup 的 manager/handler 引用，
    # "存在但永不更新"。v1.7.11 起以 _platforms_forwarded 为门禁：forward
    # 之前的失败（含 awaiting 分支复用清理）平台从未加载，HA 平台组件对
    # never-loaded 条目抛 ValueError 刷 ERROR traceback，必须跳过。
    if hass is not None and entry is not None:
        _rt = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
        if _rt.get("_platforms_forwarded"):
            try:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            except Exception as e:
                _LOGGER.debug("失败清理时卸载平台异常: %s", e)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载配置条目"""
    entry_id = entry.entry_id
    _LOGGER.info("正在卸载配置条目: %s", entry_id)

    if DOMAIN not in hass.data or entry_id not in hass.data[DOMAIN]:
        _LOGGER.debug("要卸载的条目 %s 未在数据中找到（可能已被清理），视为卸载成功", entry_id)
        return True

    data = hass.data[DOMAIN][entry_id]
    unload_successful = True

    # 0. 保存持久化数据（在清理之前）
    await save_persistent_data(hass)

    # 1. 取消停止事件监听器
    stop_unsub = data.get("_stop_unsub")
    if stop_unsub:
        try:
            stop_unsub()
        except Exception as e:
            _LOGGER.debug("取消停止监听器时出错: %s", e)

    # 1.5 先取消后台任务（_bg_tasks），避免任务在卸载后继续执行
    # 审计 2026-09-30 C-5：这一步**必须早于**心跳退订。旧顺序是 1.1 读
    # _unsub_heartbeat → 1.5 取消任务，而 step 0 的 `await save_persistent_data`
    # 就是让出点：等待态条目的"MQTT 未就绪→后台武装"任务（_arm_heartbeat_when_mqtt_ready，
    # :443-493）可以在这两步之间完成 async_subscribe 并把句柄写进 data。
    # 于是 1.1 已读过（拿不到）、1.5 对已结束的任务 cancel 无效 ⇒ gateway/rpt_rsp
    # 上留下一只挂在已卸载条目上的耳朵，继续代答 001；reload 一轮多一只（仲裁只压
    # 重复发布、不压订阅）。它自带的 `data_now is None` 双检也拦不住——条目数据要到
    # 本函数结尾才 pop，卸载进行中该判据恒不成立。
    # v1.7.64（对抗复核 A-3）：C-6 不变量——卸载/清理链上任何地方都不许就地吞掉取消。
    # 旧写法逐条 `await bg_task` + `except CancelledError: debug` 正好违反：HA 取消
    # async_unload_entry 时，CancelledError 从 `await bg_task` 抛出，被当成"子任务的
    # 取消"记一条 debug 就过去了 ⇒ 取消传不出去，卸载被硬跑到结尾并返回 True
    # （HA 以为干净卸载）。gather(return_exceptions=True) 把两种取消分开：子任务取消
    # 作为结果值返回（不转抛），本协程被取消时 gather 当场抛出（正确向上传）。
    # 快照一次——v1.6.11 #3 同因：任务完成回调会原地收缩这张列表。
    _bg = [t for t in list(data.get("_bg_tasks", [])) if t is not None]
    for bg_task in _bg:
        if not bg_task.done():
            bg_task.cancel()
    if _bg:
        try:
            results = await asyncio.gather(*_bg, return_exceptions=True)
        except Exception as e:  # noqa: BLE001 - 收尾自身异常不许把卸载吃掉（取消不在这格）
            _LOGGER.warning("取消后台任务时出错: %s", e)
        else:
            for res in results:
                # CancelledError 自 3.8 起继承 BaseException，isinstance(..., Exception)
                # 天然把它排除在外——这里只剩"后台任务自己异常退出"需要留痕
                if isinstance(res, Exception):
                    _LOGGER.debug("后台任务异常: %s", res)

    # 1.1 取消心跳监听器（无 SN 模式下的自动发现）——在任务收尾**之后**读，
    # 才能接住任务在让出点里刚补上的那只耳朵（见上方 C-5）。
    heartbeat_unsub = data.get("_unsub_heartbeat")
    if heartbeat_unsub:
        try:
            heartbeat_unsub()
            _LOGGER.debug("心跳监听器已取消")
        except Exception as e:
            _LOGGER.debug("取消心跳监听器时出错: %s", e)
        data["_unsub_heartbeat"] = None

    # 2. 先停止所有定时任务和监听器
    for unsub in data.get("unsub_listeners", []):
        try:
            unsub()
        except Exception as e:
            _LOGGER.warning("取消监听器时出错: %s", e)
            unload_successful = False

    # 2. 停止后台检查任务
    if "mqtt_handler" in data and data["mqtt_handler"]:
        if hasattr(data["mqtt_handler"], '_check_task') and data["mqtt_handler"]._check_task:
            try:
                _ck = data["mqtt_handler"]._check_task
                _ck.cancel()
                # v1.7.64（对抗复核 A-3 同形）：旧写法 `except CancelledError: debug`
                # 会把"本协程被取消"和"子任务被我们取消"混为一谈、就地吞掉。
                # gather 的 return_exceptions 只把后者收成结果值，前者照样向上传。
                for res in await asyncio.gather(_ck, return_exceptions=True):
                    if isinstance(res, Exception):
                        _LOGGER.debug("MQTT检查任务异常: %s", res)
                _LOGGER.info("已停止MQTT后台检查任务")
            except Exception as e:
                _LOGGER.warning("停止MQTT后台检查任务时出错: %s", e)
                unload_successful = False

    # 3. 卸载平台实体（v1.7.11：仅对真 forward 过平台的条目执行——
    # awaiting 条目从未加载任何平台，强卸 PLATFORMS 会被 HA 平台组件对
    # never-loaded 条目抛 ValueError，每平台一条 ERROR traceback）
    if data.get("_platforms_forwarded"):
        try:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            _LOGGER.info("平台实体卸载完成")
        except Exception as e:
            _LOGGER.error("卸载平台时出错: %s", e)
            unload_successful = False
    else:
        _LOGGER.debug("本条目未 forward 平台（awaiting），跳过平台卸载")

    # 4. 清理MQTT处理器
    try:
        if "mqtt_handler" in data and data["mqtt_handler"]:
            await data["mqtt_handler"].cleanup()
            _LOGGER.info("MQTT处理器清理完成")
    except Exception as e:
        _LOGGER.error("清理MQTT处理器时出错: %s", e)
        unload_successful = False

    # 5. 清理设备管理器
    try:
        if "device_manager" in data and data["device_manager"]:
            await data["device_manager"].cleanup()
            _LOGGER.info("设备管理器清理完成")
    except Exception as e:
        _LOGGER.error("清理设备管理器时出错: %s", e)
        unload_successful = False

    # 6. 最后移除数据
    # 恢复调试日志引用计数：卸载的 entry 不再占用 DEBUG 级别
    if entry_id in _debug_logging_entries:
        _debug_logging_entries.discard(entry_id)
        if not _debug_logging_entries:
            _LOGGER.setLevel(logging.NOTSET)
    if unload_successful:
        hass.data[DOMAIN].pop(entry_id, None)
        _LOGGER.info("配置条目 %s 卸载成功", entry_id)
    else:
        _LOGGER.warning("配置条目 %s 卸载完成，但部分清理操作遇到问题", entry_id)

    # v1.6.15：本 entry 离场后重新聚合 WS 网关（全部关闭则停止单例；
    # 幂等，HA STOP 路径复用）
    try:
        from .ws_gateway import async_ensure_ws_gateway
        await async_ensure_ws_gateway(hass)
    except Exception as e:
        _LOGGER.warning("小程序 WS 网关状态同步失败: %s", e)

    try:
        await async_ensure_hub_client(hass)
    except Exception as e:  # noqa: BLE001 - 云通道故障不得拖累本地与 WS
        _LOGGER.warning("慧尖云 hub 通道状态同步失败: %s", e)

    # v1.7.34：服务注销**不在此处**（v1.7.33 曾误放这里）——reload 也走
    # unload，而 reload 时条目仍留在 config_entries 里（"剩余条目为空"恒真），
    # 每次 reload 都会把域级服务摘掉；若随后的 setup 失败
    # （ConfigEntryNotReady/异常），服务就长期空着（调用得裸 KeyError）。
    # 正确落点是 async_remove_entry（条目确已从列表移除后才回调）。

    return unload_successful

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """更新配置选项"""
    _LOGGER.info("更新配置选项: %s", entry.entry_id)
    
    # 重新加载配置条目
    await hass.config_entries.async_reload(entry.entry_id)

async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """删除配置条目"""
    gateway_sn = entry.data.get(CONF_GATEWAY_SN, "unknown")
    _LOGGER.info("从配置中永久移除开窗器网关: %s", gateway_sn)

    # v1.7.34：最后一个条目被**删除**时注销域级服务（服务是 DOMAIN 级注册，
    # register_services 每次 setup 全覆盖注册；不注销则集成整体卸载后句柄仍
    # 留在 services 注册表，再调用得裸 KeyError/500）。落点必须在 remove 而非
    # unload——reload 同样走 unload 且条目仍在列表里，放 unload 会每次 reload
    # 摘一遍服务（v1.7.33 的实装位置，已由 test_v1733_guards 反钉）。
    try:
        _remaining = [e for e in hass.config_entries.async_entries(DOMAIN)
                      if e.entry_id != entry.entry_id]
        if not _remaining:
            for _svc_name in (hass.data.get(DOMAIN, {}) or {}).get(
                    "_registered_services", []) or []:
                try:
                    hass.services.async_remove(DOMAIN, _svc_name)
                except Exception as _svc_err:  # noqa: BLE001
                    _LOGGER.debug("注销服务 %s 失败（可能未注册）: %s",
                                  _svc_name, _svc_err)
            _LOGGER.info("全部条目已删除，域级服务已注销")
            # v1.7.63（C-10）：全部条目被删 ⇒ 诊断卡一并清掉（healer 此后不再
            # 运行，僵尸卡无人清）。v1.8.5（审计 G-3）：改走域级唯一清理出口——
            # 此前手写清单只列了 channel+mdns，漏掉 takeover（第三张卡），
            # 卡面"系统自动重试"的承诺在 healer 退出后无人执行。
            try:
                from .mqtt_bootstrap import _clear_huijian_issues
                _clear_huijian_issues(hass)
            except Exception as _ir_err:  # noqa: BLE001 — 可见性面失败不影响删除
                _LOGGER.debug("清理诊断卡失败（不影响删除结果）: %s", _ir_err)
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("服务注销检查失败（不影响删除结果）: %s", e)

    
    # 重置该网关的发现去重/忽略记录，使删除后的网关可被再次自动发现。
    # 否则 announced_gateways 中残留的"已通知"记录会永久屏蔽该网关。
    try:
        gateway_key = gateway_sn.lower()
        # v1.7.18（第 7 轮审计 BUG-9）：直接清全局持久忽略集——旧实现在
        # get("discovery", {}) 上操作，发现平台初始化失败时（异常在上方
        # 被吞、"discovery" 键不存在）discard 全部落在一次性临时 dict 上，
        # 持久忽略（GLOBAL_IGNORED_GATEWAYS，与 discovery dict 同一集合
        # 对象）纹丝不动 → 删条目后网关永不再被自动发现且无从排查。
        hass.data[DOMAIN].setdefault(GLOBAL_IGNORED_GATEWAYS, set()).discard(
            gateway_key
        )
        discovery = hass.data[DOMAIN].get("discovery") or {}
        discovery.get("announced_gateways", set()).discard(gateway_key)
        if "ignored_gateways" in discovery:
            discovery["ignored_gateways"].discard(gateway_key)
        discovery.get("last_discovery_time", {}).pop(gateway_key, None)
    except Exception as e:
        _LOGGER.debug("重置网关 %s 的发现记录失败（可忽略）: %s", gateway_sn, e)
    
    # 保存当前的持久化数据
    await save_persistent_data(hass)
    
    # 清理设备到网关映射表中属于该网关的映射关系
    # 否则这些设备会被永久锁死在已删除的网关上，无法被新网关发现和添加
    if DOMAIN in hass.data and DEVICE_TO_GATEWAY_MAPPING in hass.data[DOMAIN]:
        device_to_gateway_mapping = hass.data[DOMAIN][DEVICE_TO_GATEWAY_MAPPING]
        devices_to_remove = []
        
        # 找出所有映射到该网关的设备（大小写不敏感）
        for device_sn, mapped_gateway_sn in list(device_to_gateway_mapping.items()):
            if mapped_gateway_sn.lower() == gateway_sn.lower():
                devices_to_remove.append(device_sn)
                del device_to_gateway_mapping[device_sn]
        
        _LOGGER.info("已清理 %d 个设备的网关映射关系（网关 %s 已删除）", len(devices_to_remove), gateway_sn)
        
        # v1.7.12（第 6 轮审计 E-9）：这些子设备的速度/力度设定值同步清除——
        # 旧版残留 hass.data 与持久 JSON，同 SN 设备重配到其他网关时
        # number 实体回显陈旧设定值、误导用户以为已生效
        try:
            sp = hass.data[DOMAIN].get(DEVICE_SETPOINTS) or {}
            for dsn in devices_to_remove:
                sp.pop(dsn, None)
                for k in [k for k in sp if str(k).lower() == str(dsn).lower() and k != dsn]:
                    sp.pop(k, None)
        except Exception as spe:  # noqa: BLE001
            _LOGGER.warning("清理设备设定值失败（不影响删除流程）: %s", spe)
        
        # 保存更新后的持久化数据
        await save_persistent_data(hass)
    
    # 清理设备注册表中该网关的设备条目（含其下实体）。
    # 若残留，async_discover_gateway 的"已在设备注册表中"检查会永久屏蔽该网关，
    # 导致删除后的网关再也无法被自动发现，只能手动添加。
    gateway_device_id = None  # v1.6.12：子设备匹配用（删除前捕获）
    try:
        device_registry = dr.async_get(hass)
        gateway_device = device_registry.async_get_device(
            identifiers={(DOMAIN, gateway_sn)}
        )
        if gateway_device:
            gateway_device_id = gateway_device.id
            # 仅当该设备只关联到当前（被删除的）配置条目时才整删。
            # 若被其他 entry 共享（罕见：同 SN 多 entry），整删会误伤另一网关。
            # v1.6.12（第五轮审计 #6）：原读一个不存在的复数属性名——DeviceEntry 上
            # 从未有过它（正确为 config_entries/旧版 config_entry_id），
            # getattr 恒 None 使共享保护形同虚设，统一走 utils 双读兼容
            from .utils import get_device_config_entry_ids
            entry_ids = get_device_config_entry_ids(gateway_device)
            if entry_ids and entry_ids != {entry.entry_id}:
                _LOGGER.info(
                    "网关设备 %s 同时关联其他配置条目（%s），仅清理映射、保留设备注册表条目",
                    gateway_sn, sorted(entry_ids - {entry.entry_id}),
                )
            else:
                # 先删除该网关设备下的所有实体，避免留下孤儿实体
                from .utils import call_registry_method as _call_reg
                entity_registry = er.async_get(hass)
                for entity_entry in list(entity_registry.entities.values()):
                    if entity_entry.device_id == gateway_device.id:
                        await _call_reg(entity_registry.async_remove, entity_entry.entity_id)
                # 再删除网关设备条目本身
                await _call_reg(device_registry.async_remove_device, gateway_device.id)
                _LOGGER.info("已删除网关 %s 的设备注册表条目（含其下实体）", gateway_sn)
    except Exception as e:
        _LOGGER.error("删除网关 %s 的设备注册表条目失败: %s", gateway_sn, e)

    # 清理该网关的子设备注册表条目（via_device_id 指向该网关）。
    # 否则子设备条目残留为孤儿设备（config_entry 已删，无法被管理，脏数据）。
    # v1.6.12（第五轮审计 #6）：原读一个不存在的设备属性（hasattr/getattr 恒
    # None——测试钉桩见 tests/test_audit_round5.py 的静态扫描）
    # 不存在、恒 None，且旧值形态也非 (DOMAIN, sn) 元组，本段"意图 100% 落空"
    # 从未清理过任何子设备。改为按父设备 id 匹配（网关设备 id 已在上一步捕获，
    # 即便其注册表条目已被删，子设备 via_device_id 仍指向该 id，字符串可比）
    try:
        from .utils import call_registry_method as _call_reg
        from .utils import get_via_device_id
        device_registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        for device in iter_devices(device_registry):  # v1.7.28：双形态兼容遍历
            via_id = get_via_device_id(device)
            if gateway_device_id and via_id == gateway_device_id:
                # 先删除该子设备下的实体（仅限属于被删除网关 entry 的实体），
                # 再删除设备条目本身
                for entity_entry in list(entity_registry.entities.values()):
                    if (entity_entry.device_id == device.id
                            and entity_entry.config_entry_id == entry.entry_id):
                        await _call_reg(entity_registry.async_remove, entity_entry.entity_id)
                await _call_reg(device_registry.async_remove_device, device.id)
                _LOGGER.info("已删除网关 %s 的子设备注册表条目: %s", gateway_sn, device.id)
    except Exception as e:
        _LOGGER.error("删除网关 %s 的子设备注册表条目失败: %s", gateway_sn, e)

    # v1.6.15：entry 此时已不在 config_entries 表中，重新聚合 WS 网关——
    # 删除最后一个（或唯一开启 WS 的）entry 后服务器必须停止，
    # 否则 9001 监听面在无任何网关时空转残留（unload 时机做不到：
    # 彼时本 entry 仍在表内，wanted 判定恒为开）
    try:
        from .ws_gateway import async_ensure_ws_gateway
        await async_ensure_ws_gateway(hass)
    except Exception as e:
        _LOGGER.warning("小程序 WS 网关状态同步失败（删除条目后）: %s", e)

    try:
        await async_ensure_hub_client(hass)
    except Exception as e:  # noqa: BLE001 - 云通道故障不得拖累本地与 WS
        _LOGGER.warning("慧尖云 hub 通道状态同步失败（删除条目后）: %s", e)


async def _background_initialization(hass, entry_id, mqtt_handler):
    """后台初始化任务，不阻塞主流程"""
    try:
        await asyncio.sleep(0.5)
        # P0 守卫：条目已被卸载/重载（hass.data 中已无该条目数据）时
        # 直接返回，不访问已清理的 mqtt_handler 等对象。
        if DOMAIN not in hass.data or entry_id not in hass.data[DOMAIN]:
            _LOGGER.debug("后台初始化任务：条目 %s 已卸载，跳过初始化", entry_id)
            return
        _LOGGER.debug("后台任务：正在触发快速设备发现...")
        await mqtt_handler.fast_discovery()
        _LOGGER.debug("后台任务：初始化完成")
    except Exception as e:
        _LOGGER.warning("后台初始化任务出错: %s", e)


async def _migrate_devices_async(hass, old_gateway_sn, gateway_sn, remove_old_gateway):
    """异步执行设备迁移"""
    try:
        _LOGGER.info("开始异步设备迁移，旧网关: %s, 新网关: %s", old_gateway_sn, gateway_sn)
        await asyncio.sleep(RESTART_DELAY)

        # 执行迁移前清除 migration_info：防止迁移执行中（服务内会 reload）
        # 再次触发迁移形成循环。注意：async_update_entry 会经 add_update_listener
        # 触发 async_reload（异步任务），此处不可再显式 async_reload（会与
        # listener 的 reload 并发竞态），改为轮询等待该 entry 完成 reload。
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_GATEWAY_SN, "").lower() == gateway_sn.lower() and entry.data.get("migration_info"):
                new_data = {k: v for k, v in entry.data.items() if k != "migration_info"}
                hass.config_entries.async_update_entry(entry, data=new_data)
                _LOGGER.info("已清除 migration_info，等待重载完成")
                # 轮询等待 reload 完成（setup 完成后会写入 _setup_complete），
                # 避免与 listener 触发的 reload 并发；最多等待 5 秒
                for _ in range(25):
                    await asyncio.sleep(0.2)
                    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
                    if entry_data.get("_setup_complete"):
                        break
                break

        _LOGGER.info("调用迁移服务...")
        await hass.services.async_call(
            DOMAIN,
            "migrate_devices",
            {
                "old_gateway_sn": old_gateway_sn,
                "new_gateway_sn": gateway_sn,
                "remove_old_gateway": remove_old_gateway
            },
            blocking=True
        )
        _LOGGER.info("设备迁移任务已提交并完成")
    except Exception as e:
        _LOGGER.error("异步执行设备迁移失败: %s", e, exc_info=True)


def _make_shutdown_handler(hass, entry):
    """创建HA停止事件回调"""
    async def async_shutdown(event):
        # v1.7.31（现场实锤 F-A）：async_listen_once 的一次性监听器在 STOP
        # 派发时即被总线消费摘除，本回调随后自调 async_unload_entry 若在
        # :526 对它再 unsub，HA core 会打 "Unable to remove unknown job
        # listener" ERROR（0918 现场每次停机每条目一条，日志噪声盖真故障）。
        # 监听器已死，先清句柄摘除这段双重移除面。
        _data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if _data is not None:
            _data.pop("_stop_unsub", None)
        _LOGGER.info("Home Assistant停止，保存持久化数据...")
        await save_persistent_data(hass)
        _LOGGER.info("Home Assistant停止，清理网关资源...")
        await async_unload_entry(hass, entry)
    return async_shutdown
