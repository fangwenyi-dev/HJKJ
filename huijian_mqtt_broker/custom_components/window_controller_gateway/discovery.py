"""Window Controller Gateway Discovery Platform"""
import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    CONF_GATEWAY_SN,
    GLOBAL_IGNORED_GATEWAYS,
)

_LOGGER = logging.getLogger(__name__)

# 速率限制：同一网关SN的最小发现间隔（秒）
_DISCOVERY_COOLDOWN = 60

async def async_setup_discovery_platform(hass: HomeAssistant):
    """设置发现平台"""
    _LOGGER.info("设置开窗器网关发现平台")
    
    # 注册发现平台
    hass.data.setdefault(DOMAIN, {})
    # v1.7.12（第 6 轮审计 E-1/CF-F2）：忽略列表跨重启持久——与全局持久键
    # 共享同一 set 对象（load_persistent_data 已先行填充），忽略/取消忽略
    # 即改即落盘；旧版纯内存 set，重启后"忽略"蒸发、卡片复活。
    ignored = hass.data[DOMAIN].setdefault(GLOBAL_IGNORED_GATEWAYS, set())
    hass.data[DOMAIN]["discovery"] = {
        "ignored_gateways": ignored,
        "last_discovery_time": {},  # 记录每个网关SN的最后一次发现触发时间
        "announced_gateways": set(),  # 本次 HA 会话已触发过发现通知的网关SN（防通知轰炸）
    }
    
    return True

async def async_discover_gateway(hass: HomeAssistant, gateway_sn: str, gateway_name: str, replace_mode: bool = False, current_gateway_sn: str = None):
    """发现网关设备
    
    Args:
        hass: Home Assistant实例
        gateway_sn: 网关SN
        gateway_name: 网关名称
        replace_mode: 是否为替换模式
        current_gateway_sn: 当前网关SN（替换模式下使用）
    """
    # 确保 discovery 数据结构存在
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    if "discovery" not in hass.data[DOMAIN]:
        # v1.7.12（E-1 同口径）：兜底初始化也复用全局持久 set，防平台 setup
        # 未走时忽略记录写成一次性内存集
        hass.data[DOMAIN]["discovery"] = {
            "ignored_gateways": hass.data[DOMAIN].setdefault(
                GLOBAL_IGNORED_GATEWAYS, set()),
            "last_discovery_time": {},
            "announced_gateways": set(),
        }
    
    discovery_data = hass.data[DOMAIN]["discovery"]
    # SN 统一小写存储，避免大小写变化导致去重失效
    gateway_key = gateway_sn.lower()
    
    # 1. 检查网关是否已被忽略（大小写不敏感）
    if gateway_key in {g.lower() for g in discovery_data.get("ignored_gateways", set())}:
        _LOGGER.debug("网关 %s 已被忽略，跳过发现", gateway_sn)
        return
    
    # 2. 速率限制：检查冷却时间
    now = time.time()
    last_time = discovery_data.get("last_discovery_time", {}).get(gateway_key, 0)
    if now - last_time < _DISCOVERY_COOLDOWN:
        _LOGGER.debug("网关 %s 发现冷却中（距上次 %.0f 秒），跳过", gateway_sn, now - last_time)
        return
    discovery_data.setdefault("last_discovery_time", {})[gateway_key] = now
    
    # 3. 检查网关是否已在配置条目中
    existing_entries = hass.config_entries.async_entries(DOMAIN)
    for entry in existing_entries:
        if entry.data.get(CONF_GATEWAY_SN, "").lower() == gateway_key:
            _LOGGER.debug("网关 %s 已在配置条目中，跳过发现", gateway_sn)
            return
    
    # 3.5 v1.7.62（用户裁定）：**取消静默自动填充**——首台网关同样走发现卡片，
    #     由用户点确认。旧行为（把空 SN 等待条目直接填上 SN 转正）在一次现场
    #     里被用户判为"静默添加、不可见、像是配置出了问题"；发现链的每一次
    #     添加都应留下可见的确认动作。等待条目此后只当耳朵：用户确认后由
    #     `async_remove_awaiting_entries` 清理（见 config_flow 的三个创建口）。
    #     护栏：继续往下走 4/5/5.5，任一闸命中仍不弹卡（忽略列表/会话去重/冷却）。

    # 4. 检查网关是否已在设备注册表中
    device_registry = dr.async_get(hass)
    existing_device = device_registry.async_get_device(
        identifiers={(DOMAIN, gateway_sn)}
    )
    
    if existing_device:
        _LOGGER.debug("网关 %s 已在设备注册表中，跳过发现", gateway_sn)
        return
    
    # 5. 检查是否已有进行中的发现流程
    for flow in hass.config_entries.flow.async_progress():
        if flow.get("handler") == DOMAIN:
            flow_context = flow.get("context", {})
            flow_data = flow.get("data", {})
            flow_sn = flow_data.get("gateway_sn") or flow_context.get("gateway_sn")
            if flow_sn and flow_sn.lower() == gateway_key:
                _LOGGER.debug("网关 %s 已有进行中的发现流程，跳过", gateway_sn)
                return
    
    # 5.5 会话级去重：同一网关在一次 HA 会话内只弹一次发现通知。
    # 网关会周期心跳（约5分钟），若每次心跳都触发新的发现流程，
    # 未配置的网关会无限弹通知。用户忽略（async_ignore_gateway）或
    # 删除网关配置（async_remove_entry 重置）后才可再次触发。
    if gateway_key in discovery_data.setdefault("announced_gateways", set()):
        _LOGGER.debug("网关 %s 本次会话已发送过发现通知，跳过", gateway_sn)
        return
    
    # 通过所有检查，真正发现新网关
    _LOGGER.info("发现新网关: %s (SN: %s), 替换模式: %s", gateway_name, gateway_sn, replace_mode)
    
    # 使用基本发现流程
    from homeassistant.config_entries import SOURCE_DISCOVERY
    
    # 创建发现流程
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_DISCOVERY,
            "show_ignore": True,
            "replace_mode": replace_mode,
            "current_gateway_sn": current_gateway_sn
        },
        data={
            "gateway_sn": gateway_sn,
            "gateway_name": gateway_name,
            "discovered": True,
            "replace_mode": replace_mode,
            "current_gateway_sn": current_gateway_sn
        }
    )
    
    # 记录本次会话已弹过通知，后续心跳不再重复触发
    discovery_data.setdefault("announced_gateways", set()).add(gateway_key)
    
    _LOGGER.info("已使用标准发现流程发现网关: %s", gateway_name)

async def async_remove_awaiting_entries(hass: HomeAssistant,
                                        keep_entry_id: str = None) -> int:
    """清理零功能「等待条目」（data 无 gateway_sn）——v1.7.62 首台弹卡的配套。

    用户在前台确认添加网关后，那条只为"装耳朵"而存在的空条目再无职责：
    新条目自带 handler 订阅，多网关场景由各 handler 的"他网关"分支接管
    （与原"填充转正"路径的最终形态一致，只是改由用户点一下触发）。删除
    失败只告警——绝不影响刚完成的添加。
    """
    removed = 0
    for entry in list(hass.config_entries.async_entries(DOMAIN)):
        if keep_entry_id and entry.entry_id == keep_entry_id:
            continue
        if entry.data.get(CONF_GATEWAY_SN):
            continue
        # E-2 口径：禁用条目是用户决策，慧尖不代劳删除（本函数读 disabled_by，
        # 已登记进 test_audit_2026_09_30_fixes 的有效站点表——元校验会拦漏声明）
        if getattr(entry, "disabled_by", None):
            continue
        try:
            await hass.config_entries.async_remove(entry.entry_id)
            removed += 1
            _LOGGER.info("已清理等待条目 %s（网关已由用户确认添加）", entry.entry_id)
        except Exception as e:  # noqa: BLE001 — 清理失败不影响添加结果
            _LOGGER.warning("清理等待条目失败（不影响添加）: %s", e)
    return removed


async def async_ignore_gateway(hass: HomeAssistant, gateway_sn: str):
    """忽略网关设备"""
    _LOGGER.info("忽略网关: %s", gateway_sn)
    
    # 将网关添加到忽略列表
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    
    if "discovery" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["discovery"] = {
            "ignored_gateways": hass.data[DOMAIN].setdefault(
                GLOBAL_IGNORED_GATEWAYS, set()),
            "last_discovery_time": {},
            "announced_gateways": set(),
        }
    
    # 统一小写存储，避免大小写不一致导致去重失效
    hass.data[DOMAIN]["discovery"]["ignored_gateways"].add(gateway_sn.lower())

    # v1.7.12（审计 E-1/CF-F2）：立即落盘——忽略必须跨 HA/加载项重启生效
    try:
        from .persist import save_persistent_data
        hass.async_create_task(save_persistent_data(hass))
    except Exception as pe:  # noqa: BLE001
        _LOGGER.warning("忽略列表持久化调度失败（本会话内仍生效）: %s", pe)
    
    # 从实体注册表中删除相关实体
    # 使用前缀边界匹配（unique_id 格式为 {gateway_sn}_{...}），
    # 避免 SN 前缀相同的两个网关（如 ABC123 / ABC1234）互相误删对方的实体
    entity_registry = er.async_get(hass)
    from .utils import call_registry_method as _call_reg
    prefix = f"{gateway_sn.lower()}_"
    for entity in list(entity_registry.entities.values()):
        if entity.platform == DOMAIN and entity.unique_id and entity.unique_id.lower().startswith(prefix):
            await _call_reg(entity_registry.async_remove, entity.entity_id)
            _LOGGER.debug("删除网关 %s 的实体: %s", gateway_sn, entity.entity_id)

async def async_unignore_gateway(hass: HomeAssistant, gateway_sn: str) -> bool:
    """取消忽略网关设备。返回"该 SN 原本确实在忽略列表里并被移出"。

    返回值是给调用方如实汇报用的（审计 2026-09-30 B-5）：本函数是误点"忽略"后
    的唯一自救入口，过去它 no-op 也照样让上层打"已取消忽略"，用户与日志都无从
    发现网关其实仍被屏蔽。
    """
    _LOGGER.info("取消忽略网关: %s", gateway_sn)
    
    # 从忽略列表中移除网关，并重置会话通知去重记录，
    # 允许该网关在后续上报时重新触发发现通知
    # 审计 2026-09-30 B-5（BUG-9 同族漏改）：旧写法以 `"discovery" in hass.data[DOMAIN]`
    # 为前提，而忽略记录可以只来自持久化加载（persist 填 GLOBAL_IGNORED_GATEWAYS，
    # 不建 "discovery" 键）、发现平台初始化异常又在 __init__.py:62-67 被吞——那时
    # 本函数整体 no-op，调用方（services.py 的 unignore_gateway）却照样打
    # "已取消忽略" 的 INFO，用户看到成功却永不再出卡：唯一自救入口失效且无从排查。
    # 与 async_remove_entry（__init__.py:955-961，v1.7.18 BUG-9 的根修）同口径：
    # 直接操作那个"与 discovery dict 共享"的全局持久集合，不依赖键是否存在。
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    gateway_key = gateway_sn.lower()
    ignored = hass.data[DOMAIN].setdefault(GLOBAL_IGNORED_GATEWAYS, set())
    removed = gateway_key in ignored
    if removed:
        ignored.discard(gateway_key)
        _LOGGER.debug("网关 %s 已从忽略列表中移除", gateway_sn)
    discovery = hass.data[DOMAIN].get("discovery") or {}
    discovery.get("ignored_gateways", set()).discard(gateway_key)
    discovery.setdefault("announced_gateways", set()).discard(gateway_key)
    # v1.7.12（E-1 配套）：取消忽略同样落盘
    try:
        from .persist import save_persistent_data
        hass.async_create_task(save_persistent_data(hass))
    except Exception as pe:  # noqa: BLE001
        _LOGGER.warning("忽略列表持久化调度失败（本会话内已生效）: %s", pe)
    return removed
