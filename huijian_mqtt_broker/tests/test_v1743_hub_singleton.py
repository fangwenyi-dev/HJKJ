# -*- coding: utf-8 -*-
"""v1.7.43 钉桩：hub 出站长连是「每个 HA 安装一个」，不是「每个网关条目一个」。

为什么单独一个文件：这条改的是**归属层**，最容易以两种形态回潮——
  ① 有人图省事把 HubClient 挪回 entry 的 setup 里（于是 N 台网关 = N 条长连，
     抢同一份 /config/huijian_hub_identity.json，HA 重启后全部用同一个 instanceId
     去连 → hub 的 onAgent 把前一条顶掉 → 重连战争，且小程序只看到一个网关）；
  ② 单例接线漏了某个调用点（条目增删/重载/HA 关机），留下没人停的孤儿任务。
所以这里既钉行为（幂等、attach、pop 判等），也钉源码形态（反钉 ①）。

范式照抄本仓已修过两轮并发坑的 ws_gateway 单例（v1.7.12 F2 / v1.7.18 BUG-6）：
DOMAIN 级键 + 任一 setup/unload/STOP 尾部幂等 ensure + 停后**判等再 pop**。
"""
import asyncio
import inspect
import re
import types

from custom_components.window_controller_gateway import HUB_DATA_KEY
from custom_components.window_controller_gateway import api as api_mod
from custom_components.window_controller_gateway import const as _const
import custom_components.window_controller_gateway as pkg

DOMAIN = _const.DOMAIN


class FakeManager:
    def __init__(self, gateway_sn="GW1"):
        self.gateway_sn = gateway_sn
        self.devices = {}
        self.listeners = []

    def add_status_listener(self, cb):
        if cb not in self.listeners:
            self.listeners.append(cb)

    def remove_status_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)


class FakeHub:
    """替身：只记"被怎么调用"，不碰网络。"""

    made = []

    def __init__(self, managers=None, **kw):
        self.managers = list(managers or [])
        self.kw = kw
        # v1.8.6（审计 G-10）：真 HubClient 在构造期就把 base/install_key 绑成实例属性
        # （hub_client.py:378-379，长连与重连全读 self.base），而 ensure 现在要读它们
        # 判断"端点换没换"。替身缺这一面不是逻辑变了，是**桩窄于真实现**
        # （本仓第六次踩同坑），只会以 AttributeError 冒充成缺陷。
        self.base = kw.get("base", "")
        self.install_key = kw.get("install_key", "")
        self.attached = []
        self.started = 0
        self.stopped = 0
        FakeHub.made.append(self)

    def attach_managers(self, managers):
        self.attached.append(list(managers))
        self.managers = list(managers)

    async def async_start(self):
        self.started += 1

    async def async_stop(self):
        self.stopped += 1

    def status_view(self):
        return {"connected": True, "instanceId": "i1", "bindCode": "123456",
                "gateways": [{"sn": m.gateway_sn, "deviceCount": 0} for m in self.managers],
                "gatewaySn": (self.managers[0].gateway_sn if self.managers else "")}


class FakeHass:
    """够用的 hass 替身：data / config_dir / bus / config_entries（单例三样都要）。"""

    def __init__(self, tmp_path):
        self.data = {}
        self.config = types.SimpleNamespace(config_dir=str(tmp_path))
        self.bus = types.SimpleNamespace(
            async_listen_once=lambda *a, **k: (lambda: None))
        self.config_entries = types.SimpleNamespace(
            async_entries=lambda domain: [
                types.SimpleNamespace(options={}) for _ in self.data.get(domain, {})
            ])


def _entry(hass, entry_id, gw):
    hass.data.setdefault(DOMAIN, {})[entry_id] = {
        "gateway_sn": gw, "device_manager": FakeManager(gw), "_setup_complete": True,
        "mqtt_handler": object(),
    }


def _setup_two(monkeypatch):
    FakeHub.made = []
    monkeypatch.setattr(pkg, "HubClient", FakeHub)
    hass = FakeHass("/tmp/irrelevant")
    _entry(hass, "e1", "GW1")
    _entry(hass, "e2", "GW2")
    return hass


def test_one_client_for_all_gateway_entries(monkeypatch):
    """两条网关条目 → 只一个实例，且两条的 manager 都聚合进去。"""
    hass = _setup_two(monkeypatch)
    asyncio.run(pkg.async_ensure_hub_client(hass))
    assert len(FakeHub.made) == 1, "建了 %d 个实例＝回到每条目一实例（重连战争 + 小程序只看到一台）" % len(FakeHub.made)
    hub = hass.data[DOMAIN][HUB_DATA_KEY]
    assert sorted(m.gateway_sn for m in hub.managers) == ["GW1", "GW2"]


def test_ensure_is_idempotent(monkeypatch):
    """条目 reload / 多处调用点重复触发时不得重建实例（重建＝重新注册＝作废用户手上的码）。"""
    hass = _setup_two(monkeypatch)
    asyncio.run(pkg.async_ensure_hub_client(hass))
    first = hass.data[DOMAIN][HUB_DATA_KEY]
    asyncio.run(pkg.async_ensure_hub_client(hass))
    asyncio.run(pkg.async_ensure_hub_client(hass))
    assert len(FakeHub.made) == 1 and first.started == 1
    assert hass.data[DOMAIN][HUB_DATA_KEY] is first


def test_singleton_key_not_mistaken_for_an_entry(monkeypatch):
    """单例键与条目键共用 hass.data[DOMAIN]（沿用 ws_gateway 既有约定）——聚合必须
    只认条目里的 device_manager，不能被 _hub_client 那个值冒充成"第三台网关"。"""
    hass = _setup_two(monkeypatch)
    asyncio.run(pkg.async_ensure_hub_client(hass))
    assert HUB_DATA_KEY in hass.data[DOMAIN], "单例确实存在该命名空间下，这条钉才有意义"
    assert len(pkg._hub_managers(hass)) == 2, "聚合数不对＝单例键被当成条目读进来了"


def test_entry_removal_reattaches_without_rebuilding(monkeypatch):
    """卸载一条网关：同一实例换挂剩下的 manager（不是停掉重建）。"""
    hass = _setup_two(monkeypatch)
    asyncio.run(pkg.async_ensure_hub_client(hass))
    hub = hass.data[DOMAIN][HUB_DATA_KEY]
    hass.data[DOMAIN].pop("e2")
    asyncio.run(pkg.async_ensure_hub_client(hass))
    assert hass.data[DOMAIN][HUB_DATA_KEY] is hub
    assert len(FakeHub.made) == 1
    assert [m.gateway_sn for m in hub.managers] == ["GW1"]


def test_last_entry_gone_stops_and_pops(monkeypatch):
    """最后一条也没了 → 停实例并摘键；判等再 pop（照抄 v1.7.18 那条并发教训）。"""
    hass = _setup_two(monkeypatch)
    asyncio.run(pkg.async_ensure_hub_client(hass))
    hub = hass.data[DOMAIN][HUB_DATA_KEY]
    # 真实形态：HA 逐条 pop entry 键（单例键与条目键同在 hass.data[DOMAIN] 下，
    # 但不会被当成条目——见下面那条聚合数钉）
    hass.data[DOMAIN].pop("e1")
    hass.data[DOMAIN].pop("e2")
    asyncio.run(pkg.async_ensure_hub_client(hass))
    assert hub.stopped == 1, "条目全撤了却没停实例＝孤儿长连继续占着 hub 的一个 instance"
    assert HUB_DATA_KEY not in hass.data[DOMAIN]


def test_explicit_stop_helper(monkeypatch):
    hass = _setup_two(monkeypatch)
    asyncio.run(pkg.async_ensure_hub_client(hass))
    hub = hass.data[DOMAIN][HUB_DATA_KEY]
    asyncio.run(pkg.async_stop_hub_client(hass))
    assert hub.stopped == 1 and HUB_DATA_KEY not in hass.data[DOMAIN]
    asyncio.run(pkg.async_stop_hub_client(hass))      # 幂等：二次停不炸


def test_ensure_survives_internal_failure(monkeypatch):
    """启动失败只降级（对齐 WS 网关"失败只记 error"语义），绝不能把 entry setup 带崩。"""
    FakeHub.made = []
    boom = monkeypatch.setattr

    class Bad(FakeHub):
        async def async_start(self):
            raise OSError("hub down")

    boom(pkg, "HubClient", Bad)
    hass = FakeHass("/tmp/irrelevant")
    _entry(hass, "e1", "GW1")
    asyncio.run(pkg.async_ensure_hub_client(hass))    # 不抛即通过


def test_no_per_entry_client_left_in_setup_source():
    """反钉 ①：setup 里不得再按条目建/存 HubClient。"""
    src = inspect.getsource(pkg)
    assert 'data["hub_client"]' not in src and "entry.entry_id][\"hub_client\"]" not in src, \
        "__init__.py 又出现按条目存 hub_client＝粒度回潮，N 台网关会抢同一份身份文件"
    assert "HubClient(" in src, "ensure 里必须真的构造实例（锚点防被整段删空）"


def test_api_views_read_the_singleton():
    """api 两个视图必须读 DOMAIN 级单例，不能"遍历条目取第一个"（那正是只看到一台的根因）。"""
    src = inspect.getsource(api_mod)
    assert src.count("HUB_DATA_KEY") >= 2, "两个 hub 视图没都改读单例"
    assert 'data.get("hub_client")' not in src, "api 仍从条目字典取 hub_client"


# ── 面板网关文案：真跑（node 执行抽出来的函数），不做字符串钉 ─────────
def _run_js(cases):
    import json
    import subprocess
    import tempfile
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
    i = src.index("function hubGatewayText(info) {")
    depth = 0
    end = None
    for n, ch in enumerate(src[i:], start=i):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = n + 1
                break
    assert end, "hubGatewayText 花括号没配平＝抽取器失效"
    body = src[i:end]
    script = (body + "\nconsole.log(JSON.stringify([" +
              ",".join("hubGatewayText(%s)" % json.dumps(c) for c in cases) + "]));\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, "node 跑挂了：" + (r.stderr or "")[:300]
        return json.loads(r.stdout.strip())
    finally:
        import os
        os.unlink(path)


def test_panel_gateway_text_is_actually_computed():
    got = _run_js([
        {"gateways": [{"sn": "GW1", "deviceCount": 3}, {"sn": "GW2", "deviceCount": 2}]},
        {"gateways": [{"sn": "GW1", "deviceCount": 3}]},
        {"gatewaySn": "GW9"},                      # 老集成只回单值
        {},                                        # 什么都没有
        {"gateways": [{"sn": "GW1"}, {"sn": "GW2"}]},   # deviceCount 缺失不得出 NaN
    ])
    assert got[0] == "2 台 · 5 个子设备（GW1、GW2）", got[0]
    assert got[1] == "GW1 · 3 个子设备", got[1]
    assert got[2] == "GW9", "老集成兼容路径失效: %s" % got[2]
    assert got[3] == "—", got[3]
    assert "NaN" not in got[4] and got[4].startswith("2 台 · 0 个子设备"), got[4]


def test_panel_no_longer_claims_a_single_local_gateway():
    """标签必须从「本机网关」改掉：远程控制管的是整个安装，标签写"本机"配单个 SN 是误导。
    只判 label 元素——正文那句"也能控制本机网关的子设备"语义正确，不该被这条钉误伤。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "www" / "index.html").read_text(encoding="utf-8")
    labels = re.findall(r'<span class="label">([^<]*)</span>', html)
    assert labels, "label 元素锚点丢了，这条钉等于空判"
    assert not [l for l in labels if "本机网关" in l], "仍有标签写「本机网关」: %s" % labels
    assert "纳管网关" in labels, "远程控制卡的网关标签没换成计数口径"


# ── 命令下行的归属：必须只发给"设备所属条目"───────────────────────
class _Handler:
    def __init__(self, gw):
        self.gw = gw
        self.sent = []

    async def send_ws_raw_004(self, dev_sn, attribute, value):
        self.sent.append((dev_sn, attribute, value))
        return True


class _Mgr:
    def __init__(self, gw, devices):
        self.gateway_sn = gw
        self.devices = dict.fromkeys(devices, {})


def test_hub_control_routes_to_the_owning_entry_only():
    """一个实例收全部命令，但 004 只能由**设备所属**那条目的 handler 发——
    广播到全部条目＝同一台子设备收到两条重复控制（本仓历史上吃过这类亏）。"""
    h1, h2 = _Handler("GW1"), _Handler("GW2")
    hass = FakeHass("/tmp/irrelevant")
    hass.data[DOMAIN] = {
        "e1": {"_setup_complete": True, "device_manager": _Mgr("GW1", ["A1", "A2"]), "mqtt_handler": h1},
        "e2": {"_setup_complete": True, "device_manager": _Mgr("GW2", ["B1"]), "mqtt_handler": h2},
        HUB_DATA_KEY: object(),          # 单例值混在同一个命名空间里，不能被当成条目
    }
    control = pkg._make_hub_control(hass)
    assert asyncio.run(control("B1", "position", "100")) is True
    assert h2.sent == [("B1", "position", "100")] and h1.sent == [], "路由错了或广播了"
    assert asyncio.run(control("ZZ", "position", "100")) is False, "不存在的设备应回 False"
