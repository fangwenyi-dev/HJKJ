# -*- coding: utf-8 -*-
"""v1.7.47 api 层：bindcode 收 `kind`、家庭成员列表/移除两条新路由。

假 HA 包树里的 `HomeAssistantView` 是**空类**（没有 `json()`），所以行为测试在实例上补一个
`json` 来捕获返回载荷——这样测的是**视图的真方法体**，而不是把被测逻辑换成计数桩
（本仓踩过：把 `refreshRemote` 换成 `calls++` 就等于把要判的东西判掉了）。
"""
import asyncio

from homeassistant.core import full_status_view

from custom_components.window_controller_gateway import api
from custom_components.window_controller_gateway.const import DOMAIN, HUB_DATA_KEY

_NO_CLIENT = object()
_NO_BODY = object()


class _FakeClient:
    def __init__(self, remove_ok=True, supported=True):
        self.calls = []
        self.remove_ok = remove_ok
        self.supported = supported
        self.members = [{"mid": "a" * 12, "openidMasked": "oFa…02", "at": 1}]

    async def list_members(self):
        self.calls.append(("list_members",))
        return True

    async def remove_member(self, mid):
        self.calls.append(("remove_member", mid))
        return self.remove_ok

    async def refresh_bind_code(self, kind="owner"):
        self.calls.append(("refresh_bind_code", kind))
        return True

    def status_view(self):
        # 审计 2026-09-30 H-6：键集由**真实现**单向决定（手写 dict 会随真实现演进
        # 悄悄变窄，而"回包必须是完整视图"这类名字判的其实是假件自己的键）。
        return full_status_view(
            connected=True, instanceId="inst-1", bindCode="111111",
            bindCodeExpiresIn=300, bindCodeExpired=False, gatewaySn="GW1",
            gateways=[{"sn": "GW1", "deviceCount": 2}], hub="https://hub",
            lastError=None, memberCode="654321", memberCodeExpiresIn=300,
            memberCodeExpired=False,
            members=self.members, membersCount=len(self.members), membersMax=8,
            membersSupported=self.supported, ownerMasked="oFa…01")


class _FakeHass:
    def __init__(self, client=_NO_CLIENT):
        self.data = {DOMAIN: ({} if client is _NO_CLIENT else {HUB_DATA_KEY: client})}
        self.registered = []
        self.http = self                       # hass.http.register_view(...)

    def register_view(self, view):
        self.registered.append(view)


class _FakeRequest:
    def __init__(self, hass, payload=_NO_BODY):
        self.app = {"hass": hass}
        self._payload = payload

    async def json(self):
        if self._payload is _NO_BODY:
            raise ValueError("no body")        # 老面板就是不带 body 的 GET/POST
        return self._payload


def _view(cls, captured):
    v = cls()
    v.json = lambda payload: (captured.update(payload), payload)[1]
    return v


# ── 成员列表 ────────────────────────────────────────────────────────
def test_members_view_returns_hub_shape():
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMembersView, out).get(_FakeRequest(_FakeHass(client))))
    assert out["enabled"] is True and out["ok"] is True
    assert out["membersCount"] == 1 and out["membersMax"] == 8
    assert out["members"][0]["openidMasked"] == "oFa…02"
    assert out["ownerMasked"] == "oFa…01"
    assert out["membersSupported"] is True
    assert client.calls == [("list_members",)], "每次读都要经 hub 取（本地不留副本，留了就会与 hub 分叉）"
    assert "sec" not in str(out).lower() or "secret" not in str(out).lower()


def test_members_view_without_client_reports_disabled_not_error():
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMembersView, out).get(_FakeRequest(_FakeHass())))
    assert out == {"enabled": False, "ok": False, "members": [], "membersSupported": False}, out


def test_members_view_passes_through_old_hub_flag():
    """老 hub 没有 /agent/members ⇒ membersSupported=False，面板据此**禁用**成员区，
    而不是显示"读取失败"（那会让人以为云通道坏了）。"""
    client = _FakeClient(supported=False)
    client.members = []
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMembersView, out).get(_FakeRequest(_FakeHass(client))))
    assert out["membersSupported"] is False


# ── 移除成员 ────────────────────────────────────────────────────────
def test_remove_view_passes_mid_and_refreshes_after():
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMemberRemoveView, out).post(
        _FakeRequest(_FakeHass(client), {"mid": "a" * 12})))
    assert client.calls == [("remove_member", "a" * 12), ("list_members",)], \
        "踢完必须刷新列表，否则面板还显示被踢的人: %s" % client.calls
    assert out["removedOk"] is True and out["enabled"] is True


def test_remove_view_without_mid_never_calls_hub():
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMemberRemoveView, out).post(_FakeRequest(_FakeHass(client), {})))
    assert out["removedOk"] is False
    assert ("remove_member", "") not in client.calls and not any(c[0] == "remove_member" for c in client.calls), \
        "没有 mid 就不该发请求（空 mid 会被 hub 判 unknown_member，白打一次付费端点）"


def test_remove_view_reports_hub_rejection():
    client = _FakeClient(remove_ok=False)
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMemberRemoveView, out).post(
        _FakeRequest(_FakeHass(client), {"mid": "b" * 12})))
    assert out["removedOk"] is False, "hub 拒了必须如实回 False，不能假装成功"


def test_remove_view_without_client():
    out = {}
    asyncio.run(_view(api.WindowGatewayHubMemberRemoveView, out).post(_FakeRequest(_FakeHass(), {"mid": "a"})))
    assert out == {"enabled": False, "removedOk": False}, out


# ── bindcode 收 kind ────────────────────────────────────────────────
def test_bindcode_view_passes_kind_member():
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubBindCodeView, out).post(
        _FakeRequest(_FakeHass(client), {"kind": "member"})))
    assert ("refresh_bind_code", "member") in client.calls, client.calls
    assert ("list_members",) in client.calls, "点「添加家人」后顺手刷新成员列表，省一次往返"
    assert out["refreshOk"] is True and out["enabled"] is True


def test_bindcode_view_defaults_to_owner():
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubBindCodeView, out).post(_FakeRequest(_FakeHass(client))))
    assert ("refresh_bind_code", "owner") in client.calls, "老面板不带 body ⇒ 必须按 owner 处理"
    assert ("list_members",) not in client.calls, "owner 码那条路不该顺手拉成员列表"


def test_bindcode_view_rejects_unknown_kind_silently_as_owner():
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubBindCodeView, out).post(
        _FakeRequest(_FakeHass(client), {"kind": "root"})))
    assert ("refresh_bind_code", "owner") in client.calls, "未知 kind 必须回落 owner，不得透传给 hub"


# ── 接线 ────────────────────────────────────────────────────────────
def test_all_hub_routes_are_registered():
    """只定义不注册＝没接线（v1.7.44 的同型教训：注释与定义都不是证据）。"""
    hass = _FakeHass()
    api.async_setup_api(hass)
    urls = {getattr(v, "url", None) for v in hass.registered}
    for want in ("/api/window_controller_gateway/hub",
                 "/api/window_controller_gateway/hub/bindcode",
                 "/api/window_controller_gateway/hub/members",
                 "/api/window_controller_gateway/hub/members/remove"):
        assert want in urls, "路由 %s 没被注册（面板点了就是 404）；已注册=%s" % (want, sorted(urls))


def test_member_views_read_the_singleton_not_entries():
    """成员视图必须走 DOMAIN 级单例（`_hub_client`），不得遍历条目取第一个——
    那正是"只看到一台网关"的老根因（v1.7.43 修过，这里防复活）。"""
    src = api.WindowGatewayHubMembersView.get.__doc__ or ""
    assert "entries" not in src
    for cls in (api.WindowGatewayHubMembersView, api.WindowGatewayHubMemberRemoveView):
        code = cls.get.__code__.co_names if hasattr(cls, "get") else ()
        post_code = cls.post.__code__.co_names if hasattr(cls, "post") else ()
        names = set(code) | set(post_code)
        assert "_hub_client" in names, "%s 没走 _hub_client 单例取值" % cls.__name__
        assert "async_entries" not in names, "%s 不得遍历条目" % cls.__name__
