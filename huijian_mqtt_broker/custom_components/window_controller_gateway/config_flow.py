"""Window Controller Gateway Configuration Flow"""
import voluptuous as vol
import re
import logging
import asyncio
from typing import Any, Dict, Optional

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    DOMAIN, 
    CONF_GATEWAY_SN, 
    CONF_GATEWAY_NAME, 
    DEFAULT_GATEWAY_NAME,
    GATEWAY_CONNECT_TIMEOUT,
    CONF_WS_GATEWAY_ENABLED,
    CONF_EXPOSE_COVER_AS_CURTAIN,
    DEFAULT_EXPOSE_COVER_AS_CURTAIN,
    CONF_WS_GATEWAY_PORT,
    CONF_WS_GATEWAY_TOKEN,
    DEFAULT_WS_GATEWAY_ENABLED,
    DEFAULT_WS_GATEWAY_PORT,
    DEFAULT_WS_GATEWAY_TOKEN,
    WS_TOKEN_MAX_LEN,
    WS_TOKEN_MIN_LEN,
    WS_TOKEN_CHARSET,
    WS_RESERVED_PORTS,
)
from .mqtt_handler import WindowControllerMQTTHandler
from .mqtt_bootstrap import ensure_mqtt_connection, has_bootstrap_marker
from .utils import is_mqtt_loaded, async_wait_mqtt_loaded

_LOGGER = logging.getLogger(__name__)

# v1.6.13：MQTT 就绪宽限窗口。ensure_mqtt_connection 创建/更新条目后，
# MQTT 集成 setup 完成（hass.data["mqtt"] 写入）是异步的，立即同步判
# is_mqtt_loaded 会误报（客户现场实锤）。宽限轮询此窗口；超时仍不就绪
# 才按失败形态分流错误码。注意两等待谓词不同（ensure 内 30s 等"客户端
# 连上"，此处 10s 等"hass.data 条目存在"），时序上可串行叠加；当 ensure
# 返回 False（已完整等过 30s 仍未就绪）时门禁跳过本窗口免白等（审计#3）。
MQTT_READY_GRACE_SECONDS = 10.0

def validate_gateway_sn(sn: str) -> bool:
    """Validate gateway serial number format"""
    if not sn or len(sn) < 10:
        return False
    return bool(re.match(r'^[a-zA-Z0-9]+$', sn))

class MockDeviceManager:
    """用于连接测试的模拟设备管理器"""
    def __init__(self):
        self._manually_removed_devices = set()
        # v1.6.26（第八轮审计 D-3）：补齐 handler 侧真实契约面——连接测试
        # 窗口内到达的 003 会走 _save_manually_removed_devices / devices 判定
        # / _notify_status_listeners，_auto_discovery_enabled 读 .entry.options
        # （getattr 已对 None 安全）。同族问题 v1.6.11 #6 已为
        # allocate_device_number 修过一次，勿再留缺口。
        self.devices = {}
        self.entry = None

    def _save_manually_removed_devices(self):
        pass

    def _notify_status_listeners(self, device_sn):
        pass
    
    async def update_gateway_status(self, status):
        pass
    
    async def update_device_status(self, device_sn, status, attributes=None):
        pass
    
    def get_gateway_info(self):
        return {"name": "Test Gateway"}
    
    def get_all_devices(self):
        return []
    
    def get_device(self, device_sn):
        return None
    
    async def add_device(self, device_sn, device_name, device_type=None, force=False, is_manual_pairing=False):
        _LOGGER.debug("模拟添加设备: %s, 名称: %s, force: %s, is_manual_pairing: %s", device_sn, device_name, force, is_manual_pairing)
        return device_sn
    
    def is_device_manually_removed(self, device_sn):
        return device_sn in self._manually_removed_devices

    def allocate_device_number(self):
        # v1.6.11（审计 #6）：连接测试期间 handler 订阅真实生效，测试窗口内
        # 到达的 005 会走 _quick_add_device → allocate_device_number——本 mock
        # 缺该方法的 AttributeError 此前被消息循环兜底 except 吞掉（丢一帧
        # +日志噪音）。补齐契约面（返回值仅测试期占位，条目创建后真实 dm 接管）
        return 1


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configuration flow handler class"""
    VERSION = 1

    def __init__(self):
        """初始化配置流"""
        super().__init__()
        # 连接测试未通过时暂存用户输入，供确认步骤使用
        self._pending_gateway_sn = None
        self._pending_gateway_name = None

    async def async_step_repair(self, user_input=None):
        """B（v1.7.29）：修复条目「MQTT 自动配置未完成」的一键重试。

        重跑 bootstrap 引导；标记消失即落地（清修复条目并中止），
        仍在则如实回显"尚未成功"，让用户结合日志/HA MQTT 条目状态处置。

        v1.7.60：同一入口承接「MQTT 通道失效」修复条目（mqtt_channel_broken）
        ——那种形态下标记早已被删，判据必须换成 verify_builtin_channel 的常驻
        核验（标记判据会误报"已修好"）。
        """
        from .mqtt_bootstrap import (ensure_mqtt_connection,
                                     has_bootstrap_marker,
                                     verify_builtin_channel,
                                     _clear_takeover_issue,
                                     _clear_channel_issue,
                                     CHANNEL_ISSUE_ID)
        issue_id = str((self.context or {}).get("issue_id") or "")
        if user_input is not None:
            try:
                await ensure_mqtt_connection(self.hass)
            except Exception:  # noqa: BLE001 — ConfigEntryNotReady 等统一按判定结果走
                pass
            if issue_id == CHANNEL_ISSUE_ID:
                verdict = await verify_builtin_channel(self.hass)
                if verdict in ("ok", "no_endpoint"):
                    _clear_channel_issue(self.hass)
                    return self.async_abort(reason="mqtt_bootstrap_fixed")
                return self.async_show_form(
                    step_id="repair",
                    errors={"base": "mqtt_bootstrap_still_pending"})
            if not await has_bootstrap_marker(self.hass):
                _clear_takeover_issue(self.hass)
                return self.async_abort(reason="mqtt_bootstrap_fixed")
            return self.async_show_form(
                step_id="repair", errors={"base": "mqtt_bootstrap_still_pending"})
        return self.async_show_form(step_id="repair")

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle user step

        网关 SN 为可选项：用户可以先安装集成（点"下一步"），
        之后通过选项页面添加网关，或等待 MQTT 自动发现。
        """
        errors = {}

        # 从上下文中获取网关SN和名称（如果是从发现流程进入）
        gateway_sn_from_context = self.context.get("gateway_sn")
        gateway_name_from_context = self.context.get("gateway_name")

        if user_input is not None:
            raw_sn = user_input.get(CONF_GATEWAY_SN, "").strip()
            raw_name = user_input.get(CONF_GATEWAY_NAME, "").strip()

            if raw_sn:
                # ---- 有 SN：走原有的验证+连接测试流程 ----
                gateway_sn = raw_sn
                gateway_name = raw_name or f"{DEFAULT_GATEWAY_NAME} {gateway_sn[-4:]}"

                if not validate_gateway_sn(gateway_sn):
                    errors[CONF_GATEWAY_SN] = "invalid_sn_format"
                else:
                    # unique_id 统一小写，避免大小写不同导致重复添加
                    # v1.7.33（全量审计）：raise_on_progress=False——默认 True 时同 SN
                    # 有在途发现流（发现卡未点）会直接抛异常，用户侧表现为
                    # 流莫名消失；本流紧随其后的 already_configured 扫描成死码。
                    # 与本文件 :208/:420/:437 既有写法对齐。
                    await self.async_set_unique_id(gateway_sn.lower(), raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                    for entry in self.hass.config_entries.async_entries(DOMAIN):
                        if entry.data.get(CONF_GATEWAY_SN, "").lower() == gateway_sn.lower():
                            return self.async_abort(reason="already_configured")

                    try:
                        mqtt_already_waited = False
                        try:
                            mqtt_already_waited = (
                                await ensure_mqtt_connection(self.hass)
                            ) is False
                        except ConfigEntryNotReady as mqtt_exc:
                            # v1.6.13：该异常语义是"暂时未就绪、可稍后重试"，
                            # 不再直接判定失败形态——交给统一就绪门禁分流
                            # （旧版在此直接报 mqtt_not_available，与真实原因
                            # "内置 broker 未就绪/凭据被拒"不符，客户无从下手）
                            _LOGGER.info("MQTT 引导未确认完成，转交就绪门禁: %s", mqtt_exc)

                        if await self._async_gate_mqtt_ready(
                            errors, already_waited=mqtt_already_waited
                        ):
                            connected = await self._test_gateway_connectivity(gateway_sn)
                            if not connected:
                                self._pending_gateway_sn = gateway_sn
                                self._pending_gateway_name = gateway_name
                                return await self.async_step_confirm_add()
                    except Exception:
                        # v1.7.12（第 6 轮审计 E-11）：意外异常（AttributeError/
                        # 编码错等）也统一显示"无法连接"会误导排障方向——
                        # 文案保持（strings 依赖此键）但打完整堆栈留痕定位
                        _LOGGER.exception(
                            "网关连接测试流程出现未预期异常（界面按连接失败呈现）")
                        errors["base"] = "cannot_connect"

                    if not errors:
                        return self.async_create_entry(
                            title=gateway_name,
                            data={
                                CONF_GATEWAY_SN: gateway_sn,
                                CONF_GATEWAY_NAME: gateway_name
                            }
                        )
            else:
                # ---- 无 SN：直接完成安装，网关待后续添加 ----
                # v1.7.12（第 6 轮审计 CF-F1）：清除流实例上可能已设置的
                # unique_id——用户输入 SN→测试失败→confirm_add"返回修改"→
                # 清空 SN 提交时，:135 设置的 _unique_id 一直存活，create
                # 出的 data={} 条目会继承该 SN 的 unique_id 成幽灵占坑：
                # 该 SN 后续发现/手动添加永久 already_configured，界面上
                # 却只是"待配置"。本分支的语义是无 SN 引导条目，uid 必须为空。
                await self.async_set_unique_id(None, raise_on_progress=False)
                # v1.6.19（第六轮审计 B-LOW8）：查重——本分支不设 unique_id
                # 也没有扫描，原实现可连点 N 次"下一步"造出 N 个"待配置"
                # 条目（各带心跳监听）。引导性空条目全局只允许一个，
                # 添加真实网关走「集成选项 → 添加网关」。
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if not entry.data.get(CONF_GATEWAY_SN):
                        return self.async_abort(reason="already_configured")
                try:
                    await ensure_mqtt_connection(self.hass)
                except ConfigEntryNotReady:
                    pass  # MQTT 稍后就绪即可，不阻塞安装
                except Exception as e:  # noqa: BLE001
                    # v1.6.19（第六轮审计 B-LOW9）：原只捕 ConfigEntryNotReady，
                    # ensure 的意外异常直接冒泡打穿"不阻塞安装"的自我承诺
                    _LOGGER.warning("MQTT 引导异常（无 SN 安装不阻塞，继续创建）: %s", e)

                return self.async_create_entry(
                    title="慧尖网关",
                    data={}
                )

        # ---- 表单 ----
        # v1.7.18（第 7 轮审计 BUG-10）：回填优先级翻转——用户已提交过
        # （user_input 存在，含被校验拒绝后重显）时以用户输入为准；旧实现
        # context 值恒优先，用户输入非法被拒后表单"吞掉"刚填的内容，表现
        # 为没保存。仅初次渲染（无 user_input）沿用发现 context 预填。
        _ui_sn = user_input.get(CONF_GATEWAY_SN, "") if user_input else ""
        _ui_name = user_input.get(CONF_GATEWAY_NAME, "") if user_input else ""
        default_sn = _ui_sn or gateway_sn_from_context or ""
        if default_sn:
            default_name = (
                _ui_name
                or gateway_name_from_context
                or f"{DEFAULT_GATEWAY_NAME} {default_sn[-4:]}"
            )
        else:
            default_name = _ui_name or gateway_name_from_context or DEFAULT_GATEWAY_NAME

        data_schema = vol.Schema({
            vol.Optional(
                CONF_GATEWAY_SN,
                description={"suggested_value": default_sn},
                default=default_sn
            ): str,
            vol.Optional(
                CONF_GATEWAY_NAME,
                description={"suggested_value": default_name},
                default=default_name
            ): str,
        })

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "example_sn": "100121501186",
                "min_length": "10"
            }
        )

    async def async_step_confirm_add(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """连接测试未通过时的确认步骤：允许用户仍然添加网关

        本集成无法主动探测网关（协议规定网关只能主动上报），
        连接测试只是"在窗口内等一次上报"。网关未上电/未连 MQTT/上报间隔较长
        都会导致测试失败，不应因此阻止用户添加。
        """
        if user_input is not None:
            if user_input.get("confirm", False):
                gateway_sn = self._pending_gateway_sn
                gateway_name = self._pending_gateway_name
                if not gateway_sn:
                    return self.async_abort(reason="invalid_input")
                # 再次检查唯一性（确认期间可能已被其他流程配置；unique_id 统一小写）
                # v1.7.33（全量审计）：raise_on_progress=False——默认 True 时同 SN
                # 有在途发现流（发现卡未点）会直接抛异常，用户侧表现为
                # 流莫名消失；本流紧随其后的 already_configured 扫描成死码。
                # 与本文件 :208/:420/:437 既有写法对齐。
                # 审计 2026-09-30 B-4：上面那段注释的续行缩进比语句深，把下一行
                # async_set_unique_id 一起带进了 `if not gateway_sn:` 体内（AST 实测：
                # 该调用落在 line 285 的 if 分支里），而 :286 已经 return ⇒ 重设
                # unique_id 从未发生，本步只剩 :161 遗留的 _unique_id 在兜。
                # 注释不影响缩进、代码影响——续行注释必须与语句同级或更浅。
                await self.async_set_unique_id(gateway_sn.lower(), raise_on_progress=False)
                self._abort_if_unique_id_configured()
                # 兜底：兼容历史大小写原样的 entry unique_id
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if entry.data.get(CONF_GATEWAY_SN, "").lower() == gateway_sn.lower():
                        return self.async_abort(reason="already_configured")
                return self.async_create_entry(
                    title=gateway_name,
                    data={
                        CONF_GATEWAY_SN: gateway_sn,
                        CONF_GATEWAY_NAME: gateway_name
                    }
                )
            # 用户选择返回修改：把已输入的值带回表单
            self.context["gateway_sn"] = self._pending_gateway_sn
            self.context["gateway_name"] = self._pending_gateway_name
            return await self.async_step_user()

        return self.async_show_form(
            step_id="confirm_add",
            data_schema=vol.Schema({
                vol.Required("confirm", default=False): bool,
            }),
            description_placeholders={
                "gateway_sn": self._pending_gateway_sn or "",
            },
        )

    async def async_step_replace_gateway(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle gateway replacement flow - 暂不支持，提示用户"""
        return self.async_abort(
            reason="migration_not_supported",
            description_placeholders={"error": "迁移功能暂未启用，请手动删除旧网关后重新添加"}
        )

    async def async_step_discovery(self, discovery_info: Dict[str, Any]) -> FlowResult:
        """Handle discovery step"""
        _LOGGER.info("处理网关发现: %s", discovery_info)
        
        gateway_sn = discovery_info.get("gateway_sn")
        # 防御：gateway_sn 缺失时避免 `gateway_sn[-4:]` 直接 TypeError
        gateway_name = discovery_info.get("gateway_name")
        if not gateway_name:
            gateway_name = f"慧尖网关 {gateway_sn[-4:]}" if gateway_sn else DEFAULT_GATEWAY_NAME
        replace_mode = discovery_info.get("replace_mode", False)
        current_gateway_sn = discovery_info.get("current_gateway_sn")
        
        # 防御校验：SN 缺失或格式非法时直接中止发现流程
        # （避免 async_set_unique_id(None) 及后续用非法 SN 生成 MQTT 主题）
        if not isinstance(gateway_sn, str) or not validate_gateway_sn(gateway_sn):
            _LOGGER.warning("发现流程收到非法网关SN，中止: %r", gateway_sn)
            return self.async_abort(reason="invalid_sn_format")
        
                # 检查是否已配置（unique_id 统一小写，避免大小写不同导致重复添加）
        # v1.7.33（全量审计）：raise_on_progress=False——默认 True 时同 SN
        # 有在途发现流（发现卡未点）会直接抛异常，用户侧表现为
        # 流莫名消失；本流紧随其后的 already_configured 扫描成死码。
        # 与本文件 :208/:420/:437 既有写法对齐。
        await self.async_set_unique_id(gateway_sn.lower(), raise_on_progress=False)
        self._abort_if_unique_id_configured()
        
        # 检查是否已存在配置的网关
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)

        # 迁移/替换功能暂禁用：即使已配置网关也不再强制进入替换流程，
        # 新发现的网关直接走正常添加流程（支持多网关并存）
        if replace_mode:
            # 替换模式（当前无流程设置该标志，保留分支结构备用）
            _LOGGER.info("检测到替换模式，进入替换流程")
            
            # 获取第一个已配置的网关信息或使用current_gateway_sn
            existing_gateway_sn = current_gateway_sn
            if not existing_gateway_sn and existing_entries:
                if len(existing_entries) > 1:
                    # 多个网关时，让用户选择替换哪个网关
                    _LOGGER.info("存在多个已配置的网关（%d个），让用户选择替换哪个", len(existing_entries))
                    self.context.update({
                        "gateway_sn": gateway_sn,
                        "gateway_name": gateway_name,
                        "title_placeholders": {"name": gateway_name},
                        "description_placeholders": {
                            "name": gateway_name,
                            "sn": gateway_sn
                        },
                        "suggested_display_name": gateway_name,
                        "source": "discovery",
                        "replace_mode": replace_mode
                    })
                    return await self.async_step_replace()
                existing_entry = existing_entries[0]
                existing_gateway_sn = existing_entry.data.get(CONF_GATEWAY_SN)
            
            # 设置上下文信息
            # device_id应该是旧网关的SN（已配置的网关）
            # old_gateway_sn是旧网关的SN（已配置的网关）
            self.context.update({
                "gateway_sn": gateway_sn,  # 新网关的SN
                "gateway_name": gateway_name,
                "device_id": existing_gateway_sn,  # 旧网关的SN
                "old_gateway_sn": existing_gateway_sn,  # 旧网关的SN
                "new_gateway_sn": gateway_sn,  # 新网关的SN
                "title_placeholders": {
                    "name": gateway_name
                },
                "description_placeholders": {
                    "name": gateway_name,
                    "sn": gateway_sn
                },
                "suggested_display_name": gateway_name,
                "source": "discovery",
                "replace_mode": replace_mode
            })
            
            # 进入替换流程
            return await self.async_step_confirm_migration()
        else:
            # 没有已配置的网关，进入添加流程
            _LOGGER.info("没有已配置的网关，进入添加流程")
            
            # 设置上下文信息，确保Home Assistant能够显示带有"忽略"按钮的发现卡片
            self.context.update({
                "gateway_sn": gateway_sn,
                "gateway_name": gateway_name,
                "title_placeholders": {
                    "name": gateway_name
                },
                "description_placeholders": {
                    "name": gateway_name,
                    "sn": gateway_sn
                },
                "suggested_display_name": gateway_name,
                "source": "discovery"
            })
            
            # 对于发现的设备，Home Assistant会自动显示"忽略"按钮
            # v1.6.19（第六轮审计 B-MED1）：必须给发现流设 unique_id——HA 的
            # "忽略"按钮走 config_entries/ignore_flow 命令，它【另起新流】并只
            # 把原流 context 的 unique_id 塞进新流 user_input；不设这里，ignore
            # 步骤拿不到 SN（旧实现读 self.context，新流 context 里根本没有
            # gateway_sn）→ 忽略整体空转，重启后卡片复活。
            await self.async_set_unique_id(gateway_sn.lower(), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_user()
    
    async def async_step_ignore(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle ignore step"""
        # v1.6.19（第六轮审计 B-MED1）：SN 来源 = 新 ignore 流的 user_input
        # （HA core 塞 {"unique_id": 原流context.unique_id, "title": ...}），
        # 兼容保留 context 直读（同流续办形态）。旧写法只读 self.context →
        # 恒 None → async_ignore_gateway 永不执行，"忽略"形同虚设。
        gateway_sn = (
            (user_input or {}).get("unique_id")
            or (user_input or {}).get("gateway_sn")
            or self.context.get("gateway_sn")
        )
        _LOGGER.info("忽略网关: %s", gateway_sn)
        if gateway_sn:
            await self.async_set_unique_id(gateway_sn.lower(), raise_on_progress=False)
            from .discovery import async_ignore_gateway
            await async_ignore_gateway(self.hass, gateway_sn)
        
        # 中止流程
        return self.async_abort(reason="ignored")

    async def async_step_confirm_migration(self, user_input=None):
        """确认迁移"""
        if user_input is not None:
            if user_input.get("confirm"):
                # 保存迁移信息到数据中，以便在配置条目设置完成后使用
                migration_info = {
                    "old_gateway_sn": self.context.get("old_gateway_sn", ""),
                    "remove_old_gateway": user_input.get("remove_old", False)
                }
                
                _LOGGER.info("保存迁移信息到数据: %s", migration_info)
                
                new_gateway_sn = self.context.get("new_gateway_sn", "")
                # 防御校验：即使从发现流程绕过 replace 步骤进入，
                # SN 也必须满足格式（长度 + 字符集），避免畸形 SN 生成非法 MQTT 主题
                if not new_gateway_sn or not validate_gateway_sn(new_gateway_sn):
                    _LOGGER.error("新网关SN格式无效，取消迁移: %r", new_gateway_sn)
                    return self.async_abort(reason="invalid_sn_format")
                existing_entries = self.hass.config_entries.async_entries(DOMAIN)
                existing_entry = None
                
                for entry in existing_entries:
                    if entry.data.get(CONF_GATEWAY_SN, "").lower() == new_gateway_sn.lower():
                        existing_entry = entry
                        break
                
                if existing_entry:
                    # 如果存在，更新该条目
                    _LOGGER.info("更新现有网关配置条目: %s", existing_entry.entry_id)
                    self.hass.config_entries.async_update_entry(
                        existing_entry,
                        data={
                            **existing_entry.data,
                            "migration_info": migration_info  # 将迁移信息保存到 data 中
                        }
                    )
                    # 重新加载配置条目
                    await self.hass.config_entries.async_reload(existing_entry.entry_id)
                    return self.async_abort(reason="updated_existing_gateway")
                else:
                    # 如果不存在，创建新条目
                    _LOGGER.info("创建新网关配置条目: %s", new_gateway_sn)
                    return self.async_create_entry(
                        title=self.context.get("gateway_name", f"慧尖网关 {new_gateway_sn[-4:]}"),
                        data={
                            CONF_GATEWAY_SN: new_gateway_sn,
                            CONF_GATEWAY_NAME: self.context.get("gateway_name", f"慧尖网关 {new_gateway_sn[-4:]}"),
                            "migration_info": migration_info  # 将迁移信息保存到 data 中
                        }
                    )
        
        # 显示确认表单
        return self.async_show_form(
            step_id="confirm_migration",
            data_schema=vol.Schema({
                vol.Required("confirm", default=False): bool,
                vol.Optional("remove_old", default=False): bool,
            }),
            description_placeholders={
                "old_gateway": self.context.get("old_gateway_name", self.context.get("old_gateway_sn", "未知")),
                "new_gateway": self.context.get("new_gateway_name", self.context.get("new_gateway_sn", "未知")),
                "device_count": self.context.get("device_count", "未知")
            }
        )

    async def _async_gate_mqtt_ready(
        self, errors: Dict[str, str], already_waited: bool = False
    ) -> bool:
        """MQTT 就绪门禁（v1.6.13，客户现场误诊根治）。

        返回 True 表示可安全进入连接测试。返回 False 时已向 errors["base"]
        写入恰当错误码：
        - 从未加载且无引导线索（无 MQTT 条目、无标记）→ mqtt_not_available
          （真正"没启用 MQTT 集成"，提示用户去启用）
        - 有引导意图/已有条目但宽限窗口内始终未就绪 → broker_not_ready
          （内置 broker 未起或凭据被拒，提示等加载项就绪后重试）

        关键点：ensure_mqtt_connection 创建/更新条目后 MQTT setup 是异步完成的，
        故先宽限轮询再判定，避免把"正在连接"误判成"不可用"。
        already_waited=True（ensure 已完整消耗过 30s 连接等待仍未就绪，见
        ensure 返回值契约）时跳过本窗口——同一时段内不可能凭空就绪（审计#3）。
        """
        if is_mqtt_loaded(self.hass):
            return True

        has_entries = bool(self.hass.config_entries.async_entries("mqtt"))
        marker_pending = (not has_entries) and await has_bootstrap_marker(self.hass)

        # 完全没有 MQTT 条目、也没有引导标记 → 用户确实尚未启用/配置 MQTT，
        # 无需空等宽限窗口，直接给 mqtt_not_available。
        if not has_entries and not marker_pending:
            errors["base"] = "mqtt_not_available"
            return False

        if already_waited:
            # v1.7.18（第 7 轮审计 BUG-11，审计#3 精化）：ensure 消耗满 30s
            # 通常不必再宽限（同一时段难凭空就绪，审计#3 原意）——但条目
            # **确凿仍在排队**（setup_in_progress/not_loaded 且未禁用）时，
            # 31s 落地窗口白报一次 broker_not_ready 会误导用户。故仅当至少
            # 一个条目处于"活跃排队"形态才放行到下方宽限；终态失败
            # （setup_error/setup_retry/禁用）或状态未知/无条目一律保持快败。
            pending_states = ("setup_in_progress", "not_loaded")

            def _entry_state(e):
                s = getattr(e, "state", None)
                return getattr(s, "value", s)

            positively_pending = any(
                not getattr(e, "disabled_by", None)
                and _entry_state(e) in pending_states
                for e in self.hass.config_entries.async_entries("mqtt")
            )
            if not positively_pending:
                errors["base"] = "broker_not_ready"
                return False

        # 存在引导线索（已有 MQTT 条目，或有标记待消费）→ 给异步 setup 一次宽限窗口
        if await async_wait_mqtt_loaded(self.hass, MQTT_READY_GRACE_SECONDS):
            return True

        errors["base"] = "broker_not_ready"
        return False

    async def _test_gateway_connectivity(self, gateway_sn: str) -> bool:
        """Test gateway connectivity"""
        _LOGGER.info("Testing gateway connectivity for SN: %s", gateway_sn)

        mqtt_handler = None
        try:
            # Check if MQTT integration is available
            if not is_mqtt_loaded(self.hass):
                _LOGGER.error("MQTT integration not available")
                return False

            mock_device_manager = MockDeviceManager()
            mqtt_handler = WindowControllerMQTTHandler(self.hass, gateway_sn, mock_device_manager)

            # Setup MQTT handler
            if not await mqtt_handler.setup():
                _LOGGER.error("Failed to setup MQTT handler")
                return False

            # 协议说明：002 是网关主动发起的上报，HA 发送 002 网关不会响应。
            # 连接验证改为：只检查 MQTT 订阅是否成功（setup 返回 True 即代表订阅成功），
            # 然后等待网关主动上报（网关在线时会主动发送 001/002 消息，
            # handle_gateway_response 会设置 connected=True）。
            # 不再发送无效的 002 发现命令。
            # await mqtt_handler.check_connection()  # 旧逻辑：发送 002，网关不响应

            # 轮询等待网关主动上报：一旦收到立即通过，最多等待 GATEWAY_CONNECT_TIMEOUT 秒。
            # 固定 sleep 会漏掉上报频率较低（心跳间隔长）但实际在线的网关。
            waited = 0.0
            poll_interval = 0.5
            while waited < GATEWAY_CONNECT_TIMEOUT and not mqtt_handler.connected:
                await asyncio.sleep(poll_interval)
                waited += poll_interval

            # 检查网关是否主动上报了消息（connected 由 handle_gateway_response 设置）
            connected = mqtt_handler.connected

            if connected:
                _LOGGER.info("Gateway connectivity test passed")
            else:
                _LOGGER.warning(
                    "Gateway connectivity test failed (no response within %.0f s)",
                    GATEWAY_CONNECT_TIMEOUT,
                )

            return connected

        except Exception as e:
            _LOGGER.error("Error testing gateway connectivity: %s", e)
            return False
        finally:
            if mqtt_handler:
                try:
                    await mqtt_handler.cleanup()
                except Exception as cleanup_e:
                    _LOGGER.debug("MQTT handler cleanup error: %s", cleanup_e)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create options flow"""
        return OptionsFlow(config_entry)

    async def async_step_replace(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle gateway replacement step"""
        errors = {}
        
        # 从context中获取网关信息
        gateway_sn = self.context.get("gateway_sn")
        gateway_name = self.context.get("gateway_name", f"慧尖网关 {gateway_sn[-4:]}" if gateway_sn else "慧尖网关")
        old_gateway_sn_from_context = self.context.get("old_gateway_sn")
        replace_mode = self.context.get("replace_mode", False)

        # 获取所有已配置的网关
        # v1.7.12（第 6 轮审计 E-5/CF-F5）：无 SN 引导条目（data={}）会让
        # 旧的 entry.data[CONF_GATEWAY_SN] 直取 KeyError 打穿本步骤——先取
        # 后滤（本步骤现无生产入口，属拆雷，防未来接回替换流程即炸）
        existing_entries = self.hass.config_entries.async_entries(DOMAIN)
        gateway_options = {
            sn: entry.data.get(CONF_GATEWAY_NAME, f"慧尖网关 {sn[-4:]}")
            for entry in existing_entries
            if (sn := entry.data.get(CONF_GATEWAY_SN))
        }

        # 如果只有1个网关，自动选中
        if len(gateway_options) == 1 and not old_gateway_sn_from_context:
            old_gateway_sn_from_context = list(gateway_options.keys())[0]
            _LOGGER.info("自动选中唯一网关: %s", old_gateway_sn_from_context)

        if user_input is not None:
            old_gateway_sn = user_input.get("old_gateway_sn")
            
            # 在替换模式下，新网关SN总是从上下文获取，不允许修改
            if replace_mode:
                new_gateway_sn = gateway_sn
            else:
                new_gateway_sn = user_input.get("new_gateway_sn", gateway_sn)  # 使用默认值（当前发现的网关）

            if not old_gateway_sn or not validate_gateway_sn(old_gateway_sn):
                errors["old_gateway_sn"] = "invalid_sn_format"
            elif not replace_mode and (not new_gateway_sn or not validate_gateway_sn(new_gateway_sn)):
                errors["new_gateway_sn"] = "invalid_sn_format"
            else:
                # 保存到上下文
                self.context["old_gateway_sn"] = old_gateway_sn
                self.context["new_gateway_sn"] = new_gateway_sn
                self.context["old_gateway_name"] = gateway_options.get(old_gateway_sn, f"慧尖网关 {old_gateway_sn[-4:]}")
                self.context["new_gateway_name"] = gateway_name
                
                # 进入确认迁移步骤
                return await self.async_step_confirm_migration()

        # 构建数据模式，确保替换时必须输入旧网关和新网关SN
        data_schema = vol.Schema({})
        
        # 添加旧网关SN字段，使用下拉选择器
        if gateway_options:
            data_schema = data_schema.extend({
                vol.Required(
                    "old_gateway_sn",
                    default=user_input.get("old_gateway_sn", old_gateway_sn_from_context or list(gateway_options.keys())[0]) if user_input else old_gateway_sn_from_context or list(gateway_options.keys())[0]
                ): vol.In(gateway_options),
            })
        else:
            data_schema = data_schema.extend({
                vol.Required(
                    "old_gateway_sn",
                    default=user_input.get("old_gateway_sn", old_gateway_sn_from_context or "") if user_input else old_gateway_sn_from_context or ""
                ): str,
            })
        
        # 如果不是替换模式，允许用户输入新网关SN
        if not replace_mode:
            data_schema = data_schema.extend({
                vol.Required(
                    "new_gateway_sn",
                    default=user_input.get("new_gateway_sn", gateway_sn) if user_input else gateway_sn
                ): str,
            })

        return self.async_show_form(
            step_id="replace",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "gateway_name": gateway_name,
                "new_gateway_sn": gateway_sn
            }
        )

class OptionsFlow(config_entries.OptionsFlow):
    """Options flow handler class"""
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow"""
        self._config_entry = config_entry

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Manage options — 首次进入时根据是否已配置网关分流"""
        current_sn = self._config_entry.data.get(CONF_GATEWAY_SN, "")
        if not current_sn:
            # 审计 2026-09-30 E-1：旧写法无条件分流到 add_gateway，于是"还没配网关"
            # 的条目**永远进不到** async_step_options —— WS 端口/令牌/开关、
            # debug_logging、discovery_interval、auto_discovery 一个入口都没有；
            # 而 awaiting 分支早已按"半开口径"起了 9001 监听（__init__.py:515-517），
            # ws_gateway_wanted 取的是 DEFAULT_WS_GATEWAY_TOKEN（const.py 里那个
            # 小程序内置的公开值）⇒ 用户既看不到也改不掉。v1.7.33 专门修过
            # "自定义令牌被静默改回默认"，那是同一风险的另一头。
            # 改为菜单：两个步骤都可达（选中的 step id 由 HA 自动路由到
            # async_step_add_gateway / async_step_options，本步不需要处理 user_input）。
            return self.async_show_menu(
                step_id="init",
                menu_options=["add_gateway", "options"],
            )
        # 已有网关：进入常规选项
        return await self.async_step_options()

    async def async_step_add_gateway(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """添加网关 SN（首次配置或无网关时）"""
        errors = {}
        if user_input is not None:
            gateway_sn = user_input.get(CONF_GATEWAY_SN, "").strip()
            gateway_name = user_input.get(CONF_GATEWAY_NAME, "").strip()

            if not gateway_sn:
                errors[CONF_GATEWAY_SN] = "required"
            elif not validate_gateway_sn(gateway_sn):
                errors[CONF_GATEWAY_SN] = "invalid_sn_format"
            else:
                # 检查是否已配置该网关
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if entry.data.get(CONF_GATEWAY_SN, "").lower() == gateway_sn.lower():
                        errors[CONF_GATEWAY_SN] = "already_configured"
                        break

                # v1.6.26（第八轮审计 D-2）：HA 2026.x 的 async_update_entry
                # 对 unique_id 撞车**不抛异常**——只 error 日志后照写
                # （config_entries.py 源码实证），下方 except ValueError 兜底
                # 永不触发。判重前置：另一条目已占该 uid（其 data 无 SN 的
                # 极端形态会被上方扫描漏掉）即回显 already_configured。
                if not errors:
                    clash = self.hass.config_entries.async_entry_for_domain_unique_id(
                        DOMAIN, gateway_sn.lower()
                    )
                    if clash is not None and clash.entry_id != self._config_entry.entry_id:
                        errors[CONF_GATEWAY_SN] = "already_configured"

                if not errors:
                    if not gateway_name:
                        gateway_name = f"{DEFAULT_GATEWAY_NAME} {gateway_sn[-4:]}"

                    # 更新 config entry DATA（不是 options）
                    # v1.6.19（第六轮审计 B-LOW7）三处纠偏：
                    # ① 顺手写 unique_id（引导性空条目原本无 uid，补上让
                    #   HA 原生查重/忽略对这条生效）；真查重在上方
                    #   async_entry_for_domain_unique_id 前置（v1.6.26 D-2），
                    #   此处 except ValueError 仅留作老 HA 时代保险丝——
                    #   2026.x 撞车不抛异常、只 error 日志后照写（源码实证）；
                    # ② 删除显式 async_reload——条目 setup 时注册了 update
                    #   listener，async_update_entry 本身就会触发整条目重载，
                    #   原双路径 = 双重载（MQTT 重订阅、实体瞬断两次）；
                    # ③ create_entry(data={}) 会把条目 options 整体清空
                    #   （options 流的 create_entry 写入的就是 options！），
                    #   用户在选项页配过的 WS 端口/令牌被抹掉——改为原样保
                    #   存当前 options。
                    new_data = {
                        **self._config_entry.data,
                        CONF_GATEWAY_SN: gateway_sn,
                        CONF_GATEWAY_NAME: gateway_name,
                    }
                    try:
                        self.hass.config_entries.async_update_entry(
                            self._config_entry,
                            data=new_data,
                            unique_id=gateway_sn.lower(),
                        )
                    except ValueError:
                        errors[CONF_GATEWAY_SN] = "already_configured"
                    else:
                        return self.async_create_entry(
                            title="", data={**self._config_entry.options}
                        )

        return self.async_show_form(
            step_id="add_gateway",
            data_schema=vol.Schema({
                vol.Required(CONF_GATEWAY_SN): str,
                vol.Optional(
                    CONF_GATEWAY_NAME,
                    description={"suggested_value": "慧尖网关"}
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "example_sn": "100121501186",
            },
        )

    async def async_step_options(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """常规选项（已有网关时显示）

        v1.6.12（第五轮审计 #9）：移除 gateway_sn 表单字段——setup 只读
        entry.data，写入 options 无任何消费方，是"看起来能用实则静默无效"
        的死控件（改 SN 的正确入口是「替换网关」流程）。auto_discovery
        保留并已真实接线（mqtt_handler._auto_discovery_enabled 门控 002
        自动添加）；三个字段与 strings/zh-CN 的 options.step.options 对齐。

        v1.6.15 新增「小程序局域网直连」三项；v1.6.16 起 enabled 默认**开**
        （对齐固件"配网完成即常听"，令牌握手 401 门禁保留，可显式关闭）；
        令牌按固件握手规则
        预校验（B4 教训：含空格/逗号的令牌会拆散子协议造成永久自锁），
        空串 = 不认证并如实提示。
        """
        errors = {}
        if user_input is not None:
            token = user_input.get(CONF_WS_GATEWAY_TOKEN, DEFAULT_WS_GATEWAY_TOKEN)
            if not isinstance(token, str):
                token = ""
            token = token.strip()
            if token and (
                not (WS_TOKEN_MIN_LEN <= len(token) < WS_TOKEN_MAX_LEN)
                or any(ch not in WS_TOKEN_CHARSET for ch in token)
            ):
                errors[CONF_WS_GATEWAY_TOKEN] = "invalid_ws_token"
            elif user_input.get(CONF_WS_GATEWAY_PORT) in WS_RESERVED_PORTS:
                # v1.6.19（第六轮审计 B-LOW10）：本栈保留口——2022=内置
                # Mosquitto、10998=Web UI nginx ingress、8123=HA core、
                # 1883=外部 broker 惯用口。撞上后 bind 失败只进日志，
                # 小程序侧恒 Connection refused 静默失联（与"改端口=失联"
                # 同族坑），在表单源头拒绝。
                errors[CONF_WS_GATEWAY_PORT] = "ws_port_reserved"
            else:
                user_input = {**user_input, CONF_WS_GATEWAY_TOKEN: token}
                # v1.7.61（10-05 审计 #11）：整表覆盖会把**表单外的** options
                # 静默清空——hub_base / hub_install_key 是 __init__._hub_option
                # 会读的受支持覆盖项（hub_client.py:54 明写），却不在本表单里。
                # 用户进选项页保存一次（哪怕只改一个开关），自建 hub 端点/安装
                # 密钥即被清空、云通道回落内置默认且界面无提示。改为"旧表打底、
                # 表单字段覆盖"——与 async_step_add_gateway 那侧保留 options 同口径。
                merged = {**self._config_entry.options, **user_input}
                return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema({
                vol.Optional(
                    "discovery_interval",
                    default=self._config_entry.options.get("discovery_interval", 300)
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                vol.Optional(
                    "auto_discovery",
                    default=self._config_entry.options.get("auto_discovery", True)
                ): bool,
                vol.Optional(
                    "debug_logging",
                    default=self._config_entry.options.get("debug_logging", False)
                ): bool,
                vol.Optional(
                    CONF_EXPOSE_COVER_AS_CURTAIN,
                    default=self._config_entry.options.get(
                        CONF_EXPOSE_COVER_AS_CURTAIN,
                        DEFAULT_EXPOSE_COVER_AS_CURTAIN)
                ): bool,
                vol.Optional(
                    CONF_WS_GATEWAY_ENABLED,
                    default=self._config_entry.options.get(
                        CONF_WS_GATEWAY_ENABLED, DEFAULT_WS_GATEWAY_ENABLED)
                ): bool,
                vol.Optional(
                    CONF_WS_GATEWAY_PORT,
                    default=self._config_entry.options.get(
                        CONF_WS_GATEWAY_PORT, DEFAULT_WS_GATEWAY_PORT)
                ): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
                vol.Optional(
                    CONF_WS_GATEWAY_TOKEN,
                    description={
                        "suggested_value": self._config_entry.options.get(
                            CONF_WS_GATEWAY_TOKEN, DEFAULT_WS_GATEWAY_TOKEN)
                    },
                    # v1.7.33（全量审计）：default 取**当前存储值**——旧实现写死
                    # 公开的 DEFAULT_WS_GATEWAY_TOKEN，与同组其余 6 个字段
                    # （一律以存储值作 default）真值源不一致：客户端提交时漏带
                    # 该键（HA 前端对未改动字段可能不落 key）即把用户自定义令牌
                    # 静默改回默认——小程序侧永久 401，同时 9001 变成人人可连。
                    default=self._config_entry.options.get(
                        CONF_WS_GATEWAY_TOKEN, DEFAULT_WS_GATEWAY_TOKEN),
                ): str,
            }),
            errors=errors,
        )

