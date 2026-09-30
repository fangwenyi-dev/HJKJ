"""REST API bridge exposing the device registry to the add-on Web UI.

Home Assistant Core only exposes the device registry over WebSocket
(``config/device_registry/list``), not REST. The add-on Web UI (ingress) can
only use REST via the Supervisor proxy (``/api/ha/`` -> ``http://supervisor/core/api/``).
This view serializes the device registry from inside HA so the Web UI can list
gateway (parent) and child devices for a given config entry.
"""
from __future__ import annotations

import logging

from homeassistant.components import http
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, HUB_DATA_KEY
from .utils import iter_devices

_LOGGER = logging.getLogger(__name__)


def async_setup_api(hass: HomeAssistant) -> None:
    """Register the device-list REST view."""
    hass.http.register_view(WindowGatewayDevicesView())
    hass.http.register_view(WindowGatewaySecurityView())
    hass.http.register_view(WindowGatewayHubView())
    hass.http.register_view(WindowGatewayHubBindCodeView())
    hass.http.register_view(WindowGatewayHubMembersView())
    hass.http.register_view(WindowGatewayHubMemberRemoveView())
    hass.http.register_view(WindowGatewayHubMemberRenameView())


def _member_op_view(client, **flags) -> dict:
    """成员写操作（踢人/改名）的回包＝完整状态视图 + 操作结果位。

    为什么不给一个窄包（只回 members）：面板唯一的渲染出口 applyHubStatus 吃的是
    完整视图，喂窄包会让它把 `connected`/`bindCode`/`memberCode` 当成缺失——
    用户点完「移除」，面板就在一张刚还在倒计时的有效码旁边显示"未连接 / ------"，
    直到下次手动刷新。
    """
    view = client.status_view()
    view["enabled"] = True
    view.update(flags)
    return view


class WindowGatewaySecurityView(http.HomeAssistantView):
    """v1.6.21: 凭据健康只读视图——Web UI 用它提示"仍是默认小程序令牌"。

    默认 WS 令牌与小程序内置值同串（const.DEFAULT_WS_GATEWAY_TOKEN），
    知道 SN + 在内网即可连；提示改密是安全兜底，**绝不自动改**——
    自动轮换会造成小程序侧永久 401（令牌必须两侧同步是既定契约）。
    仅暴露布尔值，不回显任何令牌/密码明文。
    """

    url = "/api/window_controller_gateway/security"
    name = "api:window_controller_gateway:security"

    async def get(self, request):
        """任一**有效**网关注项仍是默认令牌 → true；无有效条目 → None（无从判定）。

        审计 2026-09-30 E-2：按本仓单一真源口径（utils.entry_state_for_sn 自过滤
        disabled_by，"被禁用的条目不算有效配置"）先滤掉禁用条目再判定。不禁用条目
        的令牌并不在监听面上（ws_gateway_wanted 已跳过），把它算进来会让面板就一个
        根本没跑的网关报"仍是默认令牌"——警告不可执行即噪声。
        """
        hass = request.app["hass"]
        entries = [e for e in hass.config_entries.async_entries(DOMAIN)
                   if not getattr(e, "disabled_by", None)]
        if not entries:
            return self.json({"ws_token_is_default": None, "gateway_entries": 0})
        from .const import CONF_WS_GATEWAY_TOKEN, DEFAULT_WS_GATEWAY_TOKEN
        is_default = any(
            entry.options.get(CONF_WS_GATEWAY_TOKEN, DEFAULT_WS_GATEWAY_TOKEN)
            == DEFAULT_WS_GATEWAY_TOKEN
            for entry in entries
        )
        return self.json({"ws_token_is_default": is_default, "gateway_entries": len(entries)})


class WindowGatewayDevicesView(http.HomeAssistantView):
    """Return device registry devices (parent + via children) for a config entry."""

    url = "/api/window_controller_gateway/devices"
    name = "api:window_controller_gateway:devices"

    async def get(self, request):
        """Return devices belonging to the given config_entry_id (parent + via children).

        每个设备附带其下的精确实体列表（entity_id/domain/unique_id），
        供 Web UI 直接定位实体，避免用 SN 字符串模糊匹配 entity_id
        （设备显示名只含 SN 后 4 位，模糊匹配后 6 位永远失败——2026-08-28 实测）。
        """
        hass = request.app["hass"]
        registry = dr.async_get(hass)
        entity_registry = er.async_get(hass)
        config_entry_id = request.query.get("config_entry_id")

        # 网关在线状态：直接读 mqtt_handler.connected —— 它由"收到网关上报"
        # 置 True（handle_gateway_response），网关超时置 False。语义即
        # "网关上报过 = 在线"，不依赖 binary_sensor 实体（实体可能未创建，
        # 且 v1.5.5 前 Web UI 因匹配失败一直显示"未知"——2026-08-28 实测）。
        gateway_online: bool | None = None
        if config_entry_id and DOMAIN in hass.data:
            entry_data = hass.data[DOMAIN].get(config_entry_id)
            if isinstance(entry_data, dict):
                mqtt_handler = entry_data.get("mqtt_handler")
                if mqtt_handler is not None:
                    gateway_online = bool(getattr(mqtt_handler, "connected", False))

        # 按 device_id 聚合实体（一次遍历完成，避免为每个设备再扫全表）
        entities_by_device: dict = {}
        for entity_entry in entity_registry.entities.values():  # EntityRegistry 无 async_entries（E2E 实锤），且 entities 未被弃用
            did = entity_entry.device_id
            if not did:
                continue
            entities_by_device.setdefault(did, []).append({
                "entity_id": entity_entry.entity_id,
                "domain": entity_entry.domain,
                "unique_id": entity_entry.unique_id,
            })

        all_devices = []
        for device in iter_devices(registry):  # v1.7.28：双形态兼容遍历（iter_devices 单一出口）
            # 兼容新旧 HA：config_entries (set) 取代旧版 config_entry_id (str)
            entry_ids = set()
            ce = getattr(device, "config_entries", None)
            if ce:
                entry_ids.update(ce)
            ce_id = getattr(device, "config_entry_id", None)
            if ce_id:
                entry_ids.add(ce_id)
            all_devices.append({
                "id": device.id,
                "name": device.name_by_user or device.name or "",
                "name_by_user": device.name_by_user,
                "via_device_id": getattr(device, "via_device_id", None),
                "identifiers": [[i[0], i[1]] for i in (device.identifiers or [])],
                "config_entries": list(entry_ids),
                "entities": entities_by_device.get(device.id, []),
                "gateway_online": gateway_online,
            })

        if not config_entry_id:
            # v1.7.12（第 6 轮审计 L-11）：缺参此前返回全设备注册表——本视图
            # 语义是"本集成的设备"，把他集成的设备/实体明细整体泄给 Web 调用
            # 面毫无必要。收紧为仅带本集成 identifiers 的设备（网关+其子设备），
            # 返回结构不变，对现有消费者零破坏。
            own = [d for d in all_devices
                   if any(i[0] == DOMAIN for i in d["identifiers"])]
            return self.json(own)

        parent_ids = {
            d["id"] for d in all_devices if config_entry_id in d["config_entries"]
        }
        result = [d for d in all_devices if d["id"] in parent_ids]
        for d in all_devices:
            vid = d.get("via_device_id")
            if vid and vid in parent_ids and d["id"] not in parent_ids:
                result.append(d)
        return self.json(result)


def _hub_client(hass):
    """取安装级 hub 单例（**不是**"遍历条目取第一个"——那正是只看到一台网关的根因）。"""
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    return domain_data.get(HUB_DATA_KEY)


class WindowGatewayHubView(http.HomeAssistantView):
    """v0.1(P0): 慧尖云 hub 绑定状态只读视图——插件页展示"扫码绑定"用。

    只回 instanceId / 绑定码 / 连接状态；**绝不回显 secret**（hub 长连凭据）。
    绑定码本身就是给用户看的（扫一次完成"HA 实例 ↔ 微信账号"绑定），
    但日志侧只记摘要（见 hub_client.cred_brief）。
    """

    url = "/api/window_controller_gateway/hub"
    name = "api:window_controller_gateway:hub"

    async def get(self, request):
        """返回安装级 hub 的状态；没长连（无网关/未起）如实回 enabled=False。

        顺带（服务端节流地）刷一次家庭成员：面板只调本路由与两条 POST，从不调只读的
        /hub/members ⇒ 不刷就永远显示"只有你一人"，看不到也踢不了已有家人。
        """
        hass = request.app["hass"]
        client = _hub_client(hass)
        if client is None:
            return self.json({"enabled": False})
        await client.maybe_refresh_members()
        view = client.status_view()
        view["enabled"] = True
        return self.json(view)


class WindowGatewayHubBindCodeView(http.HomeAssistantView):
    """v1.7.41: 换一个新绑定码（面板点二维码/刷新走这条）。

    为什么必须是 POST：hub 侧轮换会**当场作废旧码**（一次性语义不变），不能让
    "看一眼状态"这种读操作顺手把用户手抄到一半的码弄失效——只有用户显式点刷新才换。
    """

    url = "/api/window_controller_gateway/hub/bindcode"
    name = "api:window_controller_gateway:hub:bindcode"

    async def post(self, request):
        """换码并回新状态；集成里没有 hub 客户端（或换码失败）时如实回 refreshOk=False。

        v1.7.47：body 可带 `{"kind":"member"}` 换**成员码**（面板「添加家人」）。
        老面板不带 body ⇒ 按 owner 处理（向后兼容）；未知 kind 一律回落 owner，
        不把面板传来的字符串直接透传给 hub。
        """
        hass = request.app["hass"]
        client = _hub_client(hass)
        if client is None:
            return self.json({"enabled": False, "refreshOk": False})
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 无体/坏体一律按 owner 码处理
            payload = {}
        kind = "member" if isinstance(payload, dict) and payload.get("kind") == "member" else "owner"
        ok = await client.refresh_bind_code(kind)
        if kind == "member":
            await client.list_members()      # 点「添加家人」后顺手刷新成员列表，省一次往返
        view = client.status_view()
        view["enabled"] = True
        view["refreshOk"] = bool(ok)
        return self.json(view)


class WindowGatewayHubMembersView(http.HomeAssistantView):
    """v1.7.47: 家庭成员列表（掩码 openid + mid 句柄）。

    只读，但**每次都经 hub 取**：成员关系的真相在云端，本地不留副本（留了就会与 hub 分叉）。
    老 hub 没有 /agent/members ⇒ 回 membersSupported=False，面板据此**禁用**成员区，
    而不是显示"读取失败"（那会让人以为云通道坏了）。
    """

    url = "/api/window_controller_gateway/hub/members"
    name = "api:window_controller_gateway:hub:members"

    async def get(self, request):
        hass = request.app["hass"]
        client = _hub_client(hass)
        if client is None:
            return self.json({"enabled": False, "ok": False, "members": [], "membersSupported": False})
        ok = await client.list_members()
        view = client.status_view()
        return self.json({
            "enabled": True,
            "ok": bool(ok),
            "ownerMasked": view.get("ownerMasked"),
            "members": view.get("members") or [],
            "membersCount": view.get("membersCount"),
            "membersMax": view.get("membersMax"),
            "membersSupported": bool(view.get("membersSupported")),
            "lastError": view.get("lastError"),
            "lastOpError": view.get("lastOpError"),
        })


class WindowGatewayHubMemberRemoveView(http.HomeAssistantView):
    """v1.7.47: 移除一个家庭成员（按 mid）。

    必须是 POST：这是有副作用的写操作（被踢的人立刻失去控制权），不能被"看一眼状态"顺带触发。
    没有 mid 就不发请求——空 mid 会被 hub 判 unknown_member，白打一次云端端点。
    """

    url = "/api/window_controller_gateway/hub/members/remove"
    name = "api:window_controller_gateway:hub:members:remove"

    async def post(self, request):
        hass = request.app["hass"]
        client = _hub_client(hass)
        if client is None:
            return self.json({"enabled": False, "removedOk": False})
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        # 审计 2026-09-30 A-3：`(payload or {}).get(...)` 只挡假值——`request.json()`
        # 会把 body 原样解成任意 JSON 形态，`[1]`/`"abc"`/`5` 这类**真值非 dict**
        # 直接让 .get 抛 AttributeError ⇒ aiohttp 500（无 JSON 体），面板拿不到
        # removedOk 语义。同文件 :219 的换码视图写法是对的（isinstance 判），
        # 本处与下方改名视图是同一函数的判据分叉。
        mid = str(payload.get("mid") or "") if isinstance(payload, dict) else ""
        removed = bool(mid) and await client.remove_member(mid)
        if mid:
            await client.list_members()      # 踢完刷新，否则面板还显示被踢的人
        return self.json(_member_op_view(client, removedOk=bool(removed)))


class WindowGatewayHubMemberRenameView(http.HomeAssistantView):
    """v1.7.54: 给一位家庭成员起本机称呼（面板「改名」）。

    只写本机文件，**不打云端**：hub 的成员记录没有名称字段，所以这条路由不产生
    任何对 hub 的调用（不占限流、不在付费端点上多打一下）。
    名称随 mid 存：人被踢掉再扫回来还是同一个微信账号 ⇒ 称呼对得上人。
    留空＝清掉称呼、恢复显示云端掩码（不是错误，回 renameOk=True）。
    """

    url = "/api/window_controller_gateway/hub/members/rename"
    name = "api:window_controller_gateway:hub:members:rename"

    async def post(self, request):
        hass = request.app["hass"]
        client = _hub_client(hass)
        if client is None:
            return self.json({"enabled": False, "renameOk": False})
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        # 审计 2026-09-30 A-3 同族：判据与上方移除视图对齐（isinstance，非 `or {}`）
        body = payload if isinstance(payload, dict) else {}
        mid = str(body.get("mid") or "")
        name = body.get("name")
        ok = await client.set_member_alias(mid, name)
        return self.json(_member_op_view(client, renameOk=bool(ok)))
