# -*- coding: utf-8 -*-
"""「家庭成员」列表恒为空（功能半坏）：面板只调 GET /hub 与两条 POST，从不调只读的
GET /hub/members，而 status_view 回的是内存里的 self.members（只在 list_members 里被写）
⇒ HA 每次重启后打开面板，成员区一律"只有你一人"、count=0，看不到也踢不了任何已有家人；
只有点一次「添加家人」（那条 POST 顺带 list_members）列表才突然变准。

修法：GET /hub 在 connected 且 members_supported 时**带服务端节流**地刷一次成员；
面板的 30s 无感刷新也要刷 hub 卡（此前只刷设备 ⇒ 连接点、二维码、码倒计时全会陈旧）。
"""
import asyncio
import re
import time
from pathlib import Path

from homeassistant.core import full_status_view

from custom_components.window_controller_gateway import api
from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway.const import DOMAIN, HUB_DATA_KEY

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")


def _client(tmp_path, connected=True, supported=True):
    c = hc.HubClient([], config_dir=str(tmp_path), session=object())
    c.instance_id, c._secret = "inst-1", "sec-1"
    c.connected = connected
    c.members_supported = supported
    calls = []

    async def fake_http(path, payload, timeout_s=None):
        calls.append(path)
        return {"ok": True, "ownerMasked": "oFa…01", "membersMax": 8,
                "members": [{"mid": "a" * 12, "openidMasked": "oFa…02", "at": 1}]}

    c._http = fake_http
    return c, calls


# ── 服务端节流刷新 ────────────────────────────────────────────────────
def test_get_hub_refresh_triggers_a_member_read(tmp_path):
    client, calls = _client(tmp_path)
    asyncio.run(client.maybe_refresh_members())
    assert calls == ["/agent/members"], "面板不调只读成员路由 ⇒ 这里不刷就永远是'只有你一人'"
    assert client.status_view()["membersCount"] == 1


def test_refresh_is_throttled_server_side(tmp_path, monkeypatch):
    client, calls = _client(tmp_path)
    monkeypatch.setattr(hc, "MEMBERS_REFRESH_MIN_INTERVAL_S", 120.0)
    for _ in range(5):
        asyncio.run(client.maybe_refresh_members())
    assert len(calls) == 1, "没有节流＝面板每 30s 刷一次就把云端调用打爆（%d 次）" % len(calls)

    client._members_refresh_at = time.time() - hc.MEMBERS_REFRESH_MIN_INTERVAL_S - 1
    asyncio.run(client.maybe_refresh_members())
    assert len(calls) == 2, "过了节流窗必须还能再刷（否则一次故障后永久看不到家人）"


def test_failed_refresh_also_counts_as_an_attempt(tmp_path):
    """失败也记一次尝试：云端不可达时不该每 30s 连击（与换码节流同一口径）。"""
    client, calls = _client(tmp_path)

    async def boom(path, payload, timeout_s=None):
        calls.append(path)
        raise RuntimeError("net down")

    client._http = boom
    asyncio.run(client.maybe_refresh_members())
    asyncio.run(client.maybe_refresh_members())
    assert len(calls) == 1, calls


def test_no_refresh_when_disconnected_or_old_hub(tmp_path):
    """没长连时刷不到任何东西；老 hub 没有这个端点 ⇒ 触发只是白拿一个 404。"""
    for kwargs in ({"connected": False}, {"supported": False}, {"connected": False, "supported": False}):
        client, calls = _client(tmp_path, **kwargs)
        asyncio.run(client.maybe_refresh_members())
        assert calls == [], "%r 时不该打云端" % kwargs


def test_refresh_interval_constant_is_named_and_sane():
    assert hc.MEMBERS_REFRESH_MIN_INTERVAL_S == 120.0
    doc = hc.HubClient.maybe_refresh_members.__doc__ or ""
    assert "节流" in doc, "常量语义必须写清（下一个人会以为是缓存过期时间）"


# ── api 接线 ─────────────────────────────────────────────────────────
class _FakeClient:
    def __init__(self):
        self.calls = []

    async def maybe_refresh_members(self):
        self.calls.append(("maybe_refresh_members",))

    async def list_members(self):
        self.calls.append(("list_members",))
        return True

    async def refresh_bind_code(self, kind="owner"):
        self.calls.append(("refresh_bind_code", kind))
        return True

    async def remove_member(self, mid):
        self.calls.append(("remove_member", mid))
        return True

    def status_view(self):
        # 审计 2026-09-30 H-6：键集由真实现单向决定（本假件此前只有 9 个键，
        # 真 status_view 的 20 键里少掉的 11 键正是面板会退回兜底的那批）
        return full_status_view(
            connected=True, lastError=None, lastOpError="no_owner",
            members=[], membersCount=0, membersMax=8, membersSupported=True,
            ownerMasked="oFa…01", bindCodeTtlS=300, memberCodeTtlS=300)


class _FakeHass:
    def __init__(self, client=None):
        self.data = {DOMAIN: ({HUB_DATA_KEY: client} if client is not None else {})}


class _FakeRequest:
    def __init__(self, hass, payload=None):
        self.app = {"hass": hass}
        self._payload = payload

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _view(cls, captured):
    v = cls()
    v.json = lambda payload: (captured.update(payload), payload)[1]
    return v


def test_hub_view_refreshes_members_and_passes_op_error():
    client = _FakeClient()
    out = {}
    asyncio.run(_view(api.WindowGatewayHubView, out).get(_FakeRequest(_FakeHass(client))))
    assert ("maybe_refresh_members",) in client.calls, \
        "GET /hub 没刷成员列表＝面板永远显示'只有你一人'（本条缺陷的主修点）"
    assert out["enabled"] is True
    assert out["lastOpError"] == "no_owner", "三条路由都要透传操作类错误槽"
    assert out["bindCodeTtlS"] == 300, "面板的有效期文案要拿权威 TTL"


def test_member_views_pass_through_op_error():
    for cls, method in ((api.WindowGatewayHubMembersView, "get"),
                        (api.WindowGatewayHubMemberRemoveView, "post")):
        client = _FakeClient()
        out = {}
        payload = None if method == "get" else {"mid": "a" * 12}
        asyncio.run(getattr(_view(cls, out), method)(_FakeRequest(_FakeHass(client), payload)))
        assert out["lastOpError"] == "no_owner", "%s 没透传 lastOpError" % cls.__name__
        assert out["membersMax"] == 8


def test_hub_view_without_client_still_reports_disabled():
    out = {}
    asyncio.run(_view(api.WindowGatewayHubView, out).get(_FakeRequest(_FakeHass())))
    assert out == {"enabled": False}, out


def test_hub_view_reads_the_singleton():
    names = set(api.WindowGatewayHubView.get.__code__.co_names)
    assert "_hub_client" in names and "async_entries" not in names, \
        "必须走 DOMAIN 单例（遍历条目取第一个＝'只看到一台网关'的老根因）"


# ── 面板：30s 无感刷新必须刷 hub 卡 ───────────────────────────────────
def _fn(name):
    hits = [m.start() for m in re.finditer(r"function\s+" + re.escape(name) + r"\s*\(", JS)]
    assert len(hits) == 1, "函数 %s 出现 %d 次（应为 1）" % (name, len(hits))
    i = hits[0]
    j = JS.index("{", i)
    depth = 0
    for k in range(j, len(JS)):
        if JS[k] == "{":
            depth += 1
        elif JS[k] == "}":
            depth -= 1
            if depth == 0:
                return JS[i:k + 1]
    raise AssertionError("函数 %s 未闭合" % name)


def test_silent_refresh_also_refreshes_the_hub_card():
    """连接状态点、二维码、码倒计时都会陈旧——hub 卡必须进 30s 无感刷新。"""
    body = _fn("silentRefresh")
    assert "loadRemoteControl()" in body, "silentRefresh 没刷 hub 卡（码倒计时会一直停在旧值）"
    assert body.index("loadRemoteControl()") < body.index("gatewayContainer"), \
        "hub 卡要放在设备刷新之前：设备那段有多条提前 return，放后面就永远刷不到"


def test_background_throttle_semantics_unchanged():
    """不得新增后台空转：document.hidden 的跳过语义留在 setInterval 那一侧。"""
    body = _fn("silentRefresh")
    # 判语句而不是裸 token：函数里的注释会提到 document.hidden（解释"为什么这里不判"）
    assert "if (document.hidden)" not in body, "跳过语义应在定时器侧，别在函数里再判一次"
    tail = JS[JS.index("init();"):]
    m = re.search(r"setInterval\(\(\)\s*=>\s*\{(.*?)\},\s*30000\)", tail, re.S)
    assert m, "30s 定时器锚点漂移"
    assert "document.hidden" in m.group(1) and "silentRefresh()" in m.group(1), \
        "后台标签跳过 + 30s 无感刷新的接线被改动了"


def test_silent_refresh_still_guards_reentry():
    body = _fn("silentRefresh")
    assert "_silentRefreshing" in body, "防重入标志没了＝慢网络下并发周期会交错写 DOM"


# ── 顺带刷新不许把 HA 的 REST 请求吊住 ──────────────────────────────
class _Resp:
    status = 200

    async def json(self):
        return {"ok": True, "members": [], "membersMax": 8, "ownerMasked": None}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _RecordingSession:
    """记下 session.post 收到的 kwargs：超时这件事只能从这里看（桩 _http 看不到）。"""

    closed = False

    def __init__(self):
        self.seen = []

    def post(self, url, json=None, **kw):  # noqa: A002 - 对齐 aiohttp 的形参名
        self.seen.append(kw)
        return _Resp()


def test_member_read_uses_the_short_panel_timeout(tmp_path):
    """GET /hub 顺带刷成员走**更短**的超时。

    加顺带刷新之前这条路由是纯读内存即回；之后最坏会被一次云端 HTTP 挂住
    HUB_HTTP_TIMEOUT_S=15s（面板表现为刷新转圈）。而 /agent/members 在 hub 侧是
    纯内存读（listMembers 不落盘、不推镜像），正常远快于 1s ⇒ 5s 已很宽松，
    超了就如实显示"读取失败"并在下个节流窗重试。
    """
    assert hc.HUB_PANEL_HTTP_TIMEOUT_S < hc.HUB_HTTP_TIMEOUT_S, \
        "面板超时必须短于会话超时，否则这条改动等于没改"
    c = hc.HubClient([], config_dir=str(tmp_path), session=object())
    c.instance_id, c._secret = "inst-1", "sec-1"
    seen = []

    async def fake(path, payload, timeout_s=None):
        seen.append(timeout_s)
        return {"ok": True, "members": [], "membersMax": 8, "ownerMasked": None}

    c._http = fake
    asyncio.run(c.list_members())
    assert seen == [hc.HUB_PANEL_HTTP_TIMEOUT_S], "成员读取没走更短的面板超时: %r" % (seen,)


def test_http_passes_no_timeout_kwarg_unless_one_is_given(tmp_path):
    """aiohttp 里 `timeout=None` 的语义是**不限时**，不是"用会话默认"。

    所以 `_http` 只在显式给了 timeout_s 时才传 `timeout=`。无脑透传 None 会把
    HUB_HTTP_TIMEOUT_S 那道会话闸整个拆掉 ⇒ 注册/换码/踢人在云端挂起时永久不返回，
    面板与长连一起卡死。这条钉的是那个 footgun 本身。
    """
    sess = _RecordingSession()
    c = hc.HubClient([], config_dir=str(tmp_path), session=sess)
    c.instance_id, c._secret = "inst-1", "sec-1"

    asyncio.run(c._http("/agent/bindcode", {}))
    assert "timeout" not in sess.seen[-1], \
        "无显式超时时不得传 timeout=（None 在 aiohttp 里＝不限时）: %r" % (sess.seen[-1],)

    asyncio.run(c._http("/agent/members", {}, timeout_s=hc.HUB_PANEL_HTTP_TIMEOUT_S))
    kw = sess.seen[-1]
    assert "timeout" in kw and kw["timeout"].total == hc.HUB_PANEL_HTTP_TIMEOUT_S, \
        "显式给的超时没真的传下去: %r" % (kw,)
