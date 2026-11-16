# -*- coding: utf-8 -*-
"""v1.7.47 加载项侧的家庭成员链路。

钉四件事：
  1) 成员码签发：载荷必须带 `kind`（否则 hub 会当 owner 码轮换，把用户正在扫的码作废）；
     成员码落 `member_code`，**绝不动** `bind_code`；
  2) 老 hub 降级：老 hub 忽略 `kind` ⇒ 它轮换的其实是 owner 码，判据只能是响应里的
     `kind` 回显；缺回显必须**丢弃返回值**并置 `members_supported=False`（面板据此禁用
     「添加家人」，而不是把 owner 码当成员码显示——那样家人扫到的是"成为主人"的码）；
  3) 成员码**不进自动补发**：自动轮换只服务 owner 码（成员码是主人的显式意图，
     自动轮换会让已截图发出去的码失效）；面板显式请求则**不受节流**；
  4) 成员列表/踢人走**实例凭据**（加载项没有 openid），失败只降级不断连、不回显凭据。
"""
import asyncio
import time

import pytest

from custom_components.window_controller_gateway import hub_client as hc


@pytest.fixture
def client(tmp_path):
    c = hc.HubClient([], config_dir=str(tmp_path), session=object())
    c.instance_id = "inst-1"
    c._secret = "sec-1"
    c.bind_code = "111111"
    c._bind_code_at = time.time()
    return c


def _stub_http(client, replies):
    """把 _http 换成按路径回预置结果的假实现；记录每次调用的 (path, payload)。"""
    calls = []

    async def fake(path, payload, timeout_s=None):
        calls.append((path, payload))
        item = replies.get(path)
        if isinstance(item, Exception):
            raise item
        return dict(item) if item is not None else {"ok": False, "err": "not_found"}

    client._http = fake
    return calls


# ── 1) 成员码签发 ────────────────────────────────────────────────────
def test_member_code_request_carries_kind_and_lands_in_member_fields(client):
    calls = _stub_http(client, {"/agent/bindcode": {"ok": True, "bindCode": "654321", "kind": "member"}})

    assert asyncio.run(client.refresh_bind_code("member")) is True

    assert calls[0][1]["kind"] == "member", "载荷必须带 kind，否则 hub 会当成 owner 码轮换"
    assert client.member_code == "654321"
    assert client.bind_code == "111111", "签成员码不得动 owner 码（用户可能正在扫）"
    assert client.member_code_expires_in() > 0
    # 成员码短命且属显式操作：**不落身份文件**（落了就会与 hub 真相分叉）
    saved = hc.load_identity(client.config_dir)
    assert "memberCode" not in saved, "成员码不该写进身份文件"


def test_owner_code_path_unchanged(client):
    calls = _stub_http(client, {"/agent/bindcode": {"ok": True, "bindCode": "222222", "kind": "owner"}})

    assert asyncio.run(client.refresh_bind_code()) is True          # 缺省＝owner

    assert calls[0][1]["kind"] == "owner"
    assert client.bind_code == "222222" and client.member_code is None
    assert hc.load_identity(client.config_dir)["bindCode"] == "222222", "owner 码仍要落盘（重启复用）"


# ── 2) 老 hub 降级 ───────────────────────────────────────────────────
def test_old_hub_without_kind_echo_degrades_and_is_not_used_as_member_code(client):
    _stub_http(client, {"/agent/bindcode": {"ok": True, "bindCode": "999999"}})

    assert asyncio.run(client.refresh_bind_code("member")) is False

    assert client.member_code is None, "不得把老 hub 回的 owner 码当成员码显示"
    assert client.last_op_error == "hub_too_old_for_member_code"
    assert client.members_supported is False


# ── 3) 自动补发与节流 ────────────────────────────────────────────────
def test_member_code_never_auto_renews(client):
    """反钉：自动补发只服务 owner 码。"""
    calls = _stub_http(client, {"/agent/bindcode": {"ok": True, "bindCode": "123123", "kind": "owner"}})
    client.member_code = "654321"
    client._member_code_at = time.time() - hc.BIND_CODE_TTL_S - 10      # 成员码已过期
    client._bind_code_at = time.time()                                  # owner 码还新鲜

    asyncio.run(client._renew_bind_code_if_stale())

    assert calls == [], "owner 码没过期就不该发请求；成员码过期也不该自动换"
    assert client.member_code == "654321"


def test_member_code_panel_path_is_not_throttled(client):
    """面板显式点「添加家人」不受 BIND_CODE_RENEW_MIN_INTERVAL_S 节流（与点二维码同口径）。"""
    _stub_http(client, {"/agent/bindcode": {"ok": True, "bindCode": "123123", "kind": "member"}})
    client._bind_renew_at = time.monotonic()                            # 刚刚才尝试过（v2.0.1 起节流基准 monotonic）

    assert asyncio.run(client.refresh_bind_code("member")) is True, "显式请求不得被节流挡掉"


# ── 4) 成员列表与踢人 ────────────────────────────────────────────────
def test_list_members_fills_view(client):
    _stub_http(client, {"/agent/members": {
        "ok": True, "ownerMasked": "oFa…01", "membersMax": 8,
        "members": [{"mid": "a" * 12, "openidMasked": "oFa…02", "at": 1}]}})

    assert asyncio.run(client.list_members()) is True

    view = client.status_view()
    assert view["membersSupported"] is True
    assert view["membersCount"] == 1 and view["membersMax"] == hc.HUB_MEMBERS_MAX == 8
    assert view["members"][0]["openidMasked"] == "oFa…02"
    assert view["ownerMasked"] == "oFa…01"
    assert "sec-1" not in str(view), "视图不得回显 secret"


def test_list_members_on_old_hub_degrades_without_breaking_link(client):
    """只有 hub 明确"没有这个端点"（404 且体里没给应用级 err）才是老 hub。"""
    _stub_http(client, {"/agent/members": hc.HubHttpError("/agent/members", 404)})

    assert asyncio.run(client.list_members()) is False

    assert client.members_supported is False, "老 hub 无此端点 ⇒ 面板要禁用成员区，而不是显示'读取失败'"
    assert client.last_op_error == "members_unsupported"


def test_list_members_network_failure_is_not_blamed_on_hub_version(client):
    """瞬时网络失败不得判成"云端版本过旧"：那会让人以为升级云端就能修好一个网络问题。

    旧实现把任何异常都置 members_supported=False ⇒ 面板显示"云端版本过旧，暂不支持"
    并禁用按钮，而真因只是超时/5xx/断网。
    """
    for boom in (RuntimeError("net down"),
                 hc.HubHttpError("/agent/members", 500),
                 hc.HubHttpError("/agent/members", 502),
                 hc.HubHttpError("/agent/members", 404, "unknown_instance")):
        client.members_supported = True
        _stub_http(client, {"/agent/members": boom})
        assert asyncio.run(client.list_members()) is False
        assert client.members_supported is True, "%r 竟被判成老 hub" % (boom,)
    assert client.last_op_error == "unknown_instance", "hub 的真实 err 必须原样留着，别压成笼统值"

    client.members_supported = True
    _stub_http(client, {"/agent/members": RuntimeError("timed out")})
    asyncio.run(client.list_members())
    assert client.last_op_error == "members_unavailable"


def test_remove_member_sends_mid_and_instance_credentials(client):
    calls = _stub_http(client, {
        "/agent/unbind": {"ok": True, "removed": "oFa…02", "remaining": 0},
        "/agent/members": {"ok": True, "ownerMasked": "oFa…01", "membersMax": 8, "members": []}})

    assert asyncio.run(client.remove_member("a" * 12)) is True

    assert calls[0][0] == "/agent/unbind"
    assert calls[0][1]["mid"] == "a" * 12
    assert calls[0][1]["instanceId"] == "inst-1" and calls[0][1]["secret"] == "sec-1"
    assert client.status_view()["membersCount"] == 0, "踢完要顺手刷新列表"


def test_remove_member_failure_does_not_echo_secret(client):
    _stub_http(client, {"/agent/unbind": RuntimeError("hub /agent/unbind -> 403 sec-1")})

    assert asyncio.run(client.remove_member("a" * 12)) is False

    assert client.last_op_error == "member_remove_failed"
    assert "sec-1" not in str(client.last_op_error), "错误槽不得回显凭据"


def test_remove_member_requires_mid(client):
    calls = _stub_http(client, {"/agent/unbind": {"ok": True}})
    assert asyncio.run(client.remove_member("")) is False
    assert calls == [], "没有 mid 就不该发请求"


# ── 结构：视图键与 api 路由（api 的行为钉在 test_v1747_hub_members_api.py）──
def test_status_view_exposes_member_keys(client):
    view = client.status_view()
    for key in ("memberCode", "memberCodeExpiresIn", "memberCodeExpired",
                "members", "membersCount", "membersMax", "membersSupported", "ownerMasked"):
        assert key in view, "status_view 缺键 %s（面板拿不到就不会渲染）" % key
    # 既有键一个都不能少（面板与 api 都在用）
    for key in ("connected", "instanceId", "bindCode", "bindCodeExpiresIn",
                "bindCodeExpired", "gatewaySn", "gateways", "hub", "lastError",
                "lastOpError", "bindCodeTtlS", "memberCodeTtlS"):
        assert key in view, "status_view 丢了既有键 %s" % key


def test_member_code_expiry_semantics(client):
    assert client.member_code_expires_in() == -1, "无码时必须回 -1（未知），不得当'刚过期'渲染"
    client.member_code = "654321"
    assert client.member_code_expires_in() == -1, "有码但签发时刻未知也要回 -1"
    client._member_code_at = time.time()
    assert 0 < client.member_code_expires_in() <= hc.BIND_CODE_TTL_S
    client._member_code_at = time.time() - hc.BIND_CODE_TTL_S - 1
    assert client.member_code_expires_in() < 0
    assert client.status_view()["memberCodeExpired"] is True
