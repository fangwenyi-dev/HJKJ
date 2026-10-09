# -*- coding: utf-8 -*-
"""v1.7.44 钉桩：hub 安装级单例**接在哪个调用点上**——v1.7.43 的实发回归。

v1.7.43 把 HubClient 改成 DOMAIN 级单例（归属层，对的），但 `async_ensure_hub_client`
只挂在 `async_setup_entry` 的 **awaiting（无 SN）分支**（还重复两次），生产真实路径的
**完整设置分支一次都没调** ⇒ HA 重启后云长连永不启动：面板 `/hub` 回 `enabled:false`
（没码可扫），小程序那侧 `agentsOnline:0`，远程控制整条断——比 v1.7.42（按条目建、
至少能连上一台）更差。

为什么 925 条单测 + 真栈 e2e 18/18 全绿却没抓到：那 46 条 hub 测试**全部直接调
`async_ensure_hub_client(hass)`**，e2e 驱动**直接 `HubClient(...)`**——接线层（谁在
什么时机调它）零覆盖；而"反钉 setup 里不得按条目建 client"只判**不存在**，是单侧钉，
正好挡不住"该在的地方没了"。

⇒ 本文件两条腿都要：
  ① 行为：真跑 `pkg.async_setup_entry`（完整设置分支），断言单例真被建起来；
  ② 结构：按 `if not gateway_sn:` 把 setup 切成两支，**每支**的 ensure 计数分别判
     （完整支必须 1、awaiting 支必须 0），并用同款 WS 单例的计数当"解析锚点存活"证据。
"""
import asyncio
import inspect
import logging
import re
import types

import pytest

import custom_components.window_controller_gateway as pkg
import custom_components.window_controller_gateway.device_manager as dm_mod
import custom_components.window_controller_gateway.mqtt_bootstrap as mb
import custom_components.window_controller_gateway.mqtt_handler as mh_mod
import custom_components.window_controller_gateway.ws_gateway as wsg
from homeassistant.core import FakeServices
from custom_components.window_controller_gateway import HUB_DATA_KEY
from custom_components.window_controller_gateway.const import (
    CONF_GATEWAY_SN, DOMAIN)

GW1 = "1001999900000001"
GW2 = "1001999900000002"


class FakeManager:
    """真签名：WindowControllerDeviceManager(hass, entry)（桩不得窄于真实现）。"""

    def __init__(self, hass, entry):
        self.gateway_sn = entry.data.get(CONF_GATEWAY_SN, "")
        self.devices = {}
        self.listeners = []

    async def register_gateway_device(self):
        return None

    async def setup(self):
        return None

    def get_all_devices(self):
        return []

    def add_status_listener(self, cb):
        if cb not in self.listeners:
            self.listeners.append(cb)

    def remove_status_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)


class FakeHub:
    """hub 客户端替身：只记"被怎么调用"，不碰网络。"""

    made = []

    def __init__(self, managers=None, **kw):
        self.managers = list(managers or [])
        self.kw = kw
        # v1.8.6（审计 G-10）：与 test_v1743 的替身同款等宽——真 HubClient 的
        # base/install_key 是构造期绑死的实例属性（hub_client.py:378-379），
        # ensure 现在要读它们做端点比对；替身不带上这面就只能以 AttributeError 收场。
        self.base = kw.get("base", "")
        self.install_key = kw.get("install_key", "")
        self.started = 0
        self.stopped = 0
        FakeHub.made.append(self)

    def attach_managers(self, managers):
        self.managers = list(managers)

    async def async_start(self):
        self.started += 1

    async def async_stop(self):
        self.stopped += 1

    def status_view(self):
        return {"connected": True, "gateways": [
            {"sn": m.gateway_sn, "deviceCount": 0} for m in self.managers]}


class FakeHandler:
    def __init__(self, *_a, **_k):
        pass

    async def setup(self):
        return True

    async def check_connection(self):
        return None

    async def send_ws_raw_004(self, *_a):
        return True


def _entry(entry_id, gateway_sn=""):
    data = {CONF_GATEWAY_SN: gateway_sn} if gateway_sn else {}

    class _E:
        pass

    e = _E()
    e.entry_id = entry_id
    e.data = data
    e.options = {}
    e.async_on_unload = lambda cb: None
    e.add_update_listener = lambda cb: None
    return e


class _Hass:
    """够用的 hass 替身：完整设置分支要的 config_entries / bus / task 全都有。"""

    def __init__(self, tmp_path):
        self.data = {DOMAIN: {}}
        self.config = types.SimpleNamespace(config_dir=str(tmp_path))
        # 审计 2026-09-30 C-1 配套：真 HA 恒有 hass.services（域级服务注册面），
        # 本替身此前没有这一面。async_setup_entry 补上"服务重注册"后，桩比
        # 真实现窄直接把条目 setup 判成失败——补桩而不是弱化生产判据。
        self.services = FakeServices()
        self.bus = types.SimpleNamespace(
            async_listen_once=lambda *a, **k: (lambda: None))
        self.config_entries = types.SimpleNamespace(
            async_entries=lambda domain: [],
            async_forward_entry_setups=lambda entry, platforms: _done(),
            async_forward_entry_unload=lambda entry, platform: _done(True))

    def async_create_task(self, coro, **kw):
        if inspect.iscoroutine(coro):
            coro.close()          # 别留 "never awaited" 噪声
        return None

    def add_job(self, job, *args):
        return None


async def _done(value=None):
    return value


def _harness(monkeypatch, tmp_path):
    """把完整设置分支的外部依赖全打桩，**唯独不打桩 hub 接线**（被测对象本身）。"""
    FakeHub.made = []
    monkeypatch.setattr(pkg, "HubClient", FakeHub)
    monkeypatch.setattr(dm_mod, "WindowControllerDeviceManager", FakeManager)
    monkeypatch.setattr(mh_mod, "WindowControllerMQTTHandler", FakeHandler)
    monkeypatch.setattr(mb, "ensure_mqtt_connection", lambda h: _done())
    monkeypatch.setattr(mb, "async_start_bootstrap_healer", lambda h: None)
    monkeypatch.setattr(pkg, "async_track_time_interval",
                        lambda *a, **k: (lambda: None))
    monkeypatch.setattr(pkg, "_background_initialization", lambda *a, **k: _done())

    async def noop_ws(hass):
        return None

    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", noop_ws)
    return _Hass(tmp_path)


# ============ ① 行为：真跑 setup，单例必须起来 ============
def test_full_setup_starts_the_install_singleton(monkeypatch, tmp_path):
    """生产真实路径（条目带 SN）setup 完 → 安装级长连必须已建并已 start。

    这条就是 v1.7.43 缺的那条：当时 setup 完 hass.data[DOMAIN] 里根本没有单例键，
    面板读到 enabled:false，用户看到的正是"云端连接 未启用 / 纳管网关 —"。
    """
    hass = _harness(monkeypatch, tmp_path)
    assert asyncio.run(pkg.async_setup_entry(hass, _entry("e1", GW1))) is True
    assert HUB_DATA_KEY in hass.data[DOMAIN], \
        "setup 完成却没有安装级 hub 单例＝远程控制永不启动（v1.7.43 实发回归形态）"
    hub = hass.data[DOMAIN][HUB_DATA_KEY]
    assert len(FakeHub.made) == 1 and hub.started == 1
    assert [m.gateway_sn for m in hub.managers] == [GW1]


def test_second_gateway_entry_shares_the_same_client(monkeypatch, tmp_path):
    """第二台网关注册：同一个实例换挂两条 manager，**不得**再建第二个实例。

    两个条目各建一个实例＝各拿一个绑定码 + 抢同一份 huijian_hub_identity.json，
    正是"小程序只看到一台"的原形。
    """
    hass = _harness(monkeypatch, tmp_path)
    asyncio.run(pkg.async_setup_entry(hass, _entry("e1", GW1)))
    asyncio.run(pkg.async_setup_entry(hass, _entry("e2", GW2)))
    assert len(FakeHub.made) == 1, "两条目建了两个实例＝粒度回潮"
    hub = hass.data[DOMAIN][HUB_DATA_KEY]
    assert hub.started == 1, "复用实例时不得重复 start"
    assert sorted(m.gateway_sn for m in hub.managers) == sorted([GW1, GW2])


def test_setup_failure_does_not_leave_a_started_client(monkeypatch, tmp_path):
    """hub 起不来只降级：不得留半个注册（否则面板显示一个从未注册成功的码）。"""
    hass = _harness(monkeypatch, tmp_path)

    class Boom(FakeHub):
        async def async_start(self):
            raise OSError("hub down")

    monkeypatch.setattr(pkg, "HubClient", Boom)
    assert asyncio.run(pkg.async_setup_entry(hass, _entry("e1", GW1))) is True
    assert HUB_DATA_KEY not in hass.data[DOMAIN]


# ============ ② 结构：落点必须"每支各判"，别只判不存在 ============
def _split_setup_branches():
    """把真 `async_setup_entry` 切成 awaiting 支与完整设置支。

    锚点：`if not gateway_sn:` 之后**第一处缩进 8 的 `return True`** 即 awaiting 支收尾
    （不能用非贪婪匹配整函数，否则被支内嵌套 try/except 截断——本仓吃过这类空判）。
    """
    src = inspect.getsource(pkg.async_setup_entry)
    k = src.index("if not gateway_sn:")
    m = re.search(r"\n        return True\n", src[k:])
    assert m, "找不到 awaiting 支的 return True＝解析锚点漂移，这两条钉等于空判"
    return src[k:k + m.end()], src[k + m.end():]


@pytest.mark.parametrize("branch,expected_hub", [("awaiting", 0), ("full", 1)])
def test_hub_ensure_landing_points_are_branch_aware(branch, expected_hub):
    """完整设置支必须**恰好一处** ensure（多了＝重复注册/作废活码，少了＝通道永不启动）；
    awaiting 支必须**零处**（该支 managers 恒空，在此调用的净效果只剩停掉别的条目
    已经拉起来的长连）。"""
    head, tail = _split_setup_branches()
    seg = head if branch == "awaiting" else tail
    got = len(re.findall(r"await async_ensure_hub_client\(hass\)", seg))
    assert got == expected_hub, "%s 支 ensure_hub 调用数=%d（应为 %d）" % (
        branch, got, expected_hub)


def test_splitter_anchor_is_alive():
    """元钉：同款 WS 单例在两支各一处——两支都能被正确切分，上面的计数才不是空判。
    锚点一漂移（比如有人把 awaiting 分支整个搬走）这条先红，而不是静默放行。"""
    head, tail = _split_setup_branches()
    for name, seg in (("awaiting", head), ("full", tail)):
        n = len(re.findall(r"await async_ensure_ws_gateway\(hass\)", seg))
        assert n == 1, "%s 支 WS 单例调用数=%d，切分锚点已漂移" % (name, n)


def test_no_stale_comment_claiming_hub_starts_in_full_branch():
    """注释不是证据（v1.7.42 同型教训）：完整设置支里「hub 长连在此启动」的说明
    必须紧挨真调用，不得再出现「只有注释没有代码」的形态。"""
    _, tail = _split_setup_branches()
    for m in re.finditer(r"hub 出站长连", tail):
        after = tail[m.start():m.start() + 900]
        assert "async_ensure_hub_client(hass)" in after, \
            "完整设置支出现「提到 hub 长连却没有 ensure 调用」的注释——正是 v1.7.43 的形态"


def test_guard_itself_is_not_dead_code():
    """计数型钉的存活证据：真源里 ensure_hub 调用点总数必须等于 4
    （完整 setup + unload + remove + 定义处除外）。数字漂了说明有调用点被静默增删。"""
    src = inspect.getsource(pkg)
    calls = len(re.findall(r"await async_ensure_hub_client\(hass\)", src))
    assert calls == 3, "全仓 ensure_hub 调用点=%d（setup/unload/remove 各一）" % calls


@pytest.fixture(autouse=True)
def _quiet():
    # 必须还原原级别：本 fixture 曾把整个集成 logger 永久设成 CRITICAL 却不 teardown，
    # 于是任何**在本文件之后**跑、又靠 caplog 抓 ERROR/WARNING 的用例（如
    # test_hub_client 的重注册熔断钉）会被静默过滤——全量按字母序时 test_v1744 排在
    # test_hub_client 之后侥幸不炸，但任何"先跑 v1744 再跑 hub_client"的子集/分片必红。
    log = logging.getLogger("custom_components.window_controller_gateway")
    prev = log.level
    log.setLevel(logging.CRITICAL)
    yield
    log.setLevel(prev)
