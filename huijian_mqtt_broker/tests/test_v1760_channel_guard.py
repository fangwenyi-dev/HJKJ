"""v1.7.60 根因修复批：HA 侧"零应答"全链的行为钉。

现场形态（2026-10-05 客户抓包）：网关 1001215011a3 每 5s 发 001 连续 14+ 帧
无人应答；另一台 10012250123f 正常上报却不出卡片。代码层根因三条：
  1. 全部应答者的就绪判据是 is_mqtt_loaded（条目 setup 过）而非
     is_mqtt_connected——"条目 loaded 但一条都收不到"是静默假绿终态；
  2. 自愈完全依赖一次性引导标记，落地即删 ⇒ MQTT 条目被别的 broker 抢走/
     禁用/凭据失配后永不复查、永不告警；
  3. 容器侧发现代理（唯一看得到 broker 真值的组件）从不检查 HA 有没有应答。

本文件钉：核验五态 / healer 常驻与收尾 / 代理兜底应答逐字与停手条件 /
repair 流分流 / 接线不丢。
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import homeassistant.components.mqtt as fake_mqtt
import homeassistant.helpers.issue_registry as fake_ir
import custom_components.window_controller_gateway.mqtt_bootstrap as mb
import custom_components.window_controller_gateway.config_flow as cf_mod
from custom_components.window_controller_gateway.const import DOMAIN

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"

_spec = importlib.util.spec_from_file_location(
    "gateway_discovery_proxy_v1760",
    Path(__file__).resolve().parents[1] / "gateway_discovery_proxy.py",
)
gdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gdp)

GW = "1001215011a3"
UUID = "4bc297c6-308d-4397-b1d6-2ef6ccc329d3"


def _req001(msg_id):
    return json.dumps({"head": "$SH", "id": msg_id, "ctype": "001", "sn": GW,
                       "data": {"vesion": "V3.55.249.075_YGZN_HJ_1b0ca4a",
                                "userid": 100, "model": "YGZN_GW001",
                                "familyid": 100}})


# ============ 一、通道核验五态（verify_builtin_channel） ============

class _VerHass:
    def __init__(self, tmp_path, mqtt_entries, endpoint=True):
        self.data = {}
        self.config_entries = SimpleNamespace(
            async_entries=lambda dom: list(mqtt_entries))
        self.config = SimpleNamespace(
            path=lambda *p: str(tmp_path / p[0]))
        if endpoint:
            (tmp_path / mb.ENDPOINT_FILENAME).write_text(
                json.dumps({"broker": "127.0.0.1", "port": 2022}),
                encoding="utf-8")


def _mqtt_entry(broker="127.0.0.1", port=2022, disabled_by=None):
    return SimpleNamespace(data={"broker": broker, "port": port},
                           disabled_by=disabled_by, entry_id="M1")


def test_verify_no_endpoint_is_no_verdict(tmp_path):
    """无端点文件（HACS 独立安装）= 无判定依据，不误报。"""
    hass = _VerHass(tmp_path, [], endpoint=False)
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "no_endpoint"


def test_verify_ok_requires_connected(tmp_path):
    hass = _VerHass(tmp_path, [_mqtt_entry()])
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "ok"


def test_verify_no_entry(tmp_path):
    hass = _VerHass(tmp_path, [])
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "no_entry"


def test_verify_mismatch_when_pointing_elsewhere(tmp_path):
    """被官方 Mosquitto/EMQX 抢走 → mismatch（现场最常形态）。"""
    hass = _VerHass(tmp_path, [_mqtt_entry(broker="192.168.1.50", port=1883)])
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "mismatch"


def test_verify_disconnected(tmp_path, monkeypatch):
    hass = _VerHass(tmp_path, [_mqtt_entry()])
    monkeypatch.setattr(fake_mqtt, "async_connected", lambda hass: False)
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "disconnected"


def test_verify_disabled_entry_not_counted(tmp_path):
    hass = _VerHass(tmp_path, [_mqtt_entry(disabled_by="user")])
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "no_entry"


# ============ 二、healer 常驻：落地后仍核验，破坏即报，恢复才收尾 ============

class _HealHass:
    def __init__(self, tmp_path, mqtt_entries):
        self.data = {}
        self.config = SimpleNamespace(path=lambda *p: str(tmp_path / p[0]))
        gw = [SimpleNamespace(entry_id="E1")]
        self.config_entries = SimpleNamespace(
            async_entries=lambda dom: (gw if dom == DOMAIN else list(mqtt_entries)))

    def async_create_task(self, coro, name=None):
        return asyncio.ensure_future(coro)


def _run_healer(hass):
    async def main():
        mb.async_start_bootstrap_healer(hass)
        task = hass.data[DOMAIN]["_bootstrap_healer"]
        await asyncio.wait_for(task, timeout=5)
        return task
    return asyncio.run(main())


def test_healer_stays_and_reports_when_channel_broken(tmp_path, monkeypatch):
    """标记已删 + 条目指向别处：不再直接退出（旧行为），出通道修复条目。"""
    from homeassistant.helpers import issue_registry as ir
    ir.ISSUES_CREATED.clear()
    hass = _HealHass(tmp_path, [_mqtt_entry(broker="192.168.1.50", port=1883)])
    (tmp_path / mb.ENDPOINT_FILENAME).write_text(
        json.dumps({"broker": "127.0.0.1", "port": 2022}), encoding="utf-8")

    async def no_marker(h):
        return False

    monkeypatch.setattr(mb, "has_bootstrap_marker", no_marker)
    monkeypatch.setattr(mb, "CHANNEL_VERIFY_INTERVAL", 0.0)
    calls = {"n": 0}

    async def sleep_one_round(hass, delay):
        calls["n"] += 1
        return calls["n"] < 2  # 第二轮后收手（模拟用户修复后下一轮转 ok）

    monkeypatch.setattr(mb, "_interruptible_sleep", sleep_one_round)
    _run_healer(hass)
    assert calls["n"] == 2, "破坏态必须进入低频巡查循环，而不是退出"
    created = [i for i in ir.ISSUES_CREATED if i["issue_id"] == mb.CHANNEL_ISSUE_ID]
    assert created and created[0]["is_fixable"] is True, \
        "通道故障必须出可修复的 HA 修复条目（可见可修）"


def test_healer_exits_only_when_channel_ok(tmp_path, monkeypatch):
    """标记已删 + 不变量成立：清卡片退出（原语义保留）。"""
    from homeassistant.helpers import issue_registry as ir
    ir.ISSUES_DELETED.clear()
    hass = _HealHass(tmp_path, [_mqtt_entry()])
    (tmp_path / mb.ENDPOINT_FILENAME).write_text(
        json.dumps({"broker": "127.0.0.1", "port": 2022}), encoding="utf-8")

    async def no_marker(h):
        return False

    monkeypatch.setattr(mb, "has_bootstrap_marker", no_marker)
    _run_healer(hass)
    assert (DOMAIN, mb.CHANNEL_ISSUE_ID) in ir.ISSUES_DELETED
    assert hass.data[DOMAIN]["_bootstrap_healer"] is None


# ============ 三、代理 001 兜底应答（用户点名：应该回复这条就够了） ============

def _proxy(publish_ack=None, read_uuid=None):
    pubs, logs, acks = [], [], []
    clock = {"t": 1000.0}

    p = gdp.DiscoveryProxy(
        lambda: [{"domain": DOMAIN, "state": "loaded"}],   # 耳朵已在 → 纯观察
        lambda: "exists",
        lambda line: pubs.append(line),
        now=lambda: clock["t"],
        log=logs.append,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        publish_ack=(None if publish_ack is None else
                     (lambda sn, payload: acks.append((sn, payload)) or True)),
        read_uuid=read_uuid,
    )
    return p, pubs, logs, acks, clock


def _req(ctype, msg_id, sn=GW):
    return json.dumps({"head": "$SH", "id": msg_id, "ctype": ctype, "sn": sn,
                       "data": {"vesion": "V3.55.249.075_YGZN_HJ_1b0ca4a",
                                "userid": 100, "model": "YGZN_GW001",
                                "familyid": 100} if ctype == "001" else {}})


def test_ack_guard_fires_after_two_unanswered_frames():
    """HA 连续两轮不答 → 本代理按同一指纹代答（逐字判据）。"""
    p, _, logs, acks, _ = _proxy(publish_ack=True, read_uuid=lambda: UUID)
    p.handle_line(_req001(100))
    assert acks == [], "首帧必须留给 HA（5s 窗口）"
    p.handle_line(_req001(101))
    assert len(acks) == 1
    sn, payload = acks[0]
    assert sn == GW
    assert payload == {"head": "$SH", "ctype": "001", "id": 101, "sn": GW,
                       "data": {"errcode": 0, "uuid": UUID}}, \
        "代答报文必须与正式 handler/耳朵逐字同形（含 uuid 指纹）"
    assert any("零应答" in m for m in logs), "必须留痕指出 HA 侧无人应答"


def test_ack_guard_covers_002_and_005_without_uuid():
    """契约面是 001/002/005：现场实测未配置网关的 002 零应答（a3 id=103）。
    002/005 的 ack 与集成 _send_ack 同形——只有 errcode，不带 uuid。
    判"失聪"后（连续两轮零应答）后续每帧必 ack 的当场代答，不再等一轮。"""
    p, _, logs, acks, _ = _proxy(publish_ack=True, read_uuid=lambda: None)
    p.handle_line(_req("002", 103))
    assert acks == [], "首帧留给 HA（5s 窗口）"
    p.handle_line(_req("002", 104))
    assert len(acks) == 1
    assert acks[0][1] == {"head": "$SH", "ctype": "002", "id": 104, "sn": GW,
                          "data": {"errcode": 0}}, \
        "002 应答逐字：errcode 0，无 uuid（与 _send_ack 同形）"
    p.handle_line(_req("005", 201))
    p.handle_line(_req("005", 202))
    assert [a[1]["ctype"] for a in acks] == ["002", "005", "005"], \
        "判失聪后每帧必 ack 的上报都要当场代答"
    assert any("未配置" in m for m in logs)


def test_ha_evidence_clears_deaf_state():
    """HA 一旦有新应答（下行主题出现），失聪态与请求积压一起清零——代理停手；
    若 HA 再度静默，需重新积累两轮未答才接管（不抖回旧判据）。"""
    p, _, _, acks, _ = _proxy(publish_ack=True, read_uuid=lambda: UUID)
    p.handle_line(_req("005", 1))
    p.handle_line(_req("005", 2))
    assert len(acks) == 1, "先判失聪并代答"
    p.handle_line('gateway/%s/req {"head":"$SH","ctype":"005","id":9,"sn":"%s",'
                  '"data":{"errcode":0}}' % (GW, GW))
    p.handle_line(_req("005", 3))
    assert len(acks) == 1, "HA 应答出现后必须停手"
    p.handle_line(_req("005", 4))
    assert len(acks) == 2, "HA 再度静默：重新积累两轮未答才接管"


def test_ack_guard_counts_distinct_ids_only():
    """同一 id 的固件重发只算一轮——否则单帧重复两次就凑够"两轮"误判。"""
    p, _, _, acks, _ = _proxy(publish_ack=True, read_uuid=lambda: UUID)
    for _ in range(4):
        p.handle_line(_req001(100))
    assert acks == []


def test_ack_guard_reacknowledges_after_reboot_id_reuse():
    """网关重启后 id 从 100 重来：过期的"HA 已答"证据不得把新请求判成已答。"""
    p, _, _, acks, clock = _proxy(publish_ack=True, read_uuid=lambda: UUID)
    p.handle_line('gateway/%s/req {"head":"$SH","ctype":"001","id":100,"sn":"%s",'
                  '"data":{"errcode":0,"uuid":"x"}}' % (GW, GW))
    p.handle_line('gateway/%s/req {"head":"$SH","ctype":"001","id":101,"sn":"%s",'
                  '"data":{"errcode":0,"uuid":"x"}}' % (GW, GW))
    p.handle_line(_req001(100))
    p.handle_line(_req001(101))
    assert acks == [], "HA 在答（证据新鲜）→ 代理停手"
    clock["t"] += 120.0                       # 超过 ACK_EVIDENCE_TTL
    p.handle_line(_req001(100))               # 重启后 id 从 100 重来
    p.handle_line(_req001(101))
    assert len(acks) == 1 and acks[0][1]["id"] == 101, \
        "证据过期后新风暴必须重新兜底（否则重启后永远不止血）"


def test_ack_guard_stands_down_when_ha_answers():
    """HA 的应答出现在 gateway/<sn>/req（含 -v 主题行）→ 绝不代答。"""
    p, _, _, acks, _ = _proxy(publish_ack=True, read_uuid=lambda: UUID)
    p.handle_line('gateway/%s/req {"head":"$SH","ctype":"001","id":100,"sn":"%s",'
                  '"data":{"errcode":0,"uuid":"x"}}' % (GW, GW))
    p.handle_line(_req001(100))
    p.handle_line(_req001(101))
    assert acks == [], "HA 已应答 ⇒ 代理停手（防双答）"


def test_ack_guard_never_answers_gateway_replies():
    """带 errcode 的 001 是网关对我方报文的回复——绝不对答（防回环）。"""
    p, _, _, acks, _ = _proxy(publish_ack=True, read_uuid=lambda: UUID)
    for i in (1, 2, 3):
        p.handle_line(json.dumps({"head": "$SH", "id": i, "ctype": "001",
                                  "sn": GW, "data": {"errcode": 0}}))
    assert acks == []


def test_ack_guard_needs_uuid_file():
    """缺实例指纹文件：001 不代答（两个指纹会让固件认成另一台服务器），且说一次；
    002/005 不依赖指纹，照答。"""
    p, _, logs, acks, _ = _proxy(publish_ack=True, read_uuid=lambda: None)
    p.handle_line(_req001(1))
    p.handle_line(_req001(2))
    assert acks == []
    assert any("指纹" in m for m in logs)
    p.handle_line(_req("005", 9))
    p.handle_line(_req("005", 10))
    assert [a[1]["ctype"] for a in acks] == ["005", "005"], \
        "002/005 不依赖指纹——判失聪后每帧照答"


def test_ack_guard_off_without_injection():
    """未注入 publish_ack（旧形态）：零行为变化。"""
    p, _, _, acks, _ = _proxy()
    p.handle_line(_req001(1))
    p.handle_line(_req001(2))
    p.handle_line(_req("002", 3))
    p.handle_line(_req("002", 4))
    assert acks == []


def test_verbose_topic_lines_still_parse():
    """mosquitto_sub -v 的行("topic payload")由代理剥主题再解析；纯 payload 形态
    （旧测试/旧 argv）继续吃。带主题的整行不能直接喂 parse_report。"""
    p, pubs, _, _, _ = _proxy()
    p.handle_line("gateway/rpt_rsp " + _req001(7))
    assert pubs == []  # 耳朵已在 → 纯观察（且行已被正确剥主题解析，无异常）
    assert gdp.parse_report("gateway/rpt_rsp " + _req001(7)) is None, \
        "带主题的整行不是合法 payload——剥主题是代理的职责"


# ============ 五、假绿终态：等待条目 loaded 但 MQTT 未连接 ============

def test_awaiting_entry_loaded_but_deaf_is_loud(tmp_path, monkeypatch, caplog):
    """现场终态（2026-10-05）：等待条目照样 loaded（HA 里看着正常），但没有
    任何 gateway/rpt_rsp 订阅者 ⇒ 001 无人应答、卡片不出、日志全绿。

    本钉要求：条目仍 loaded（不改变 HA 可见状态）+ 订阅照挂（HA 重连后自动
    生效）+ **必须有一条指名"未连上 Broker"的 WARNING**（旧行为是打
    "已启动网关心跳监听器"的假成功）。
    """
    import logging
    import custom_components.window_controller_gateway as pkg
    from homeassistant.core import FakeServices
    from homeassistant.helpers import entity_registry as er

    subs = []

    async def asub(hass, topic, cb, qos=0):
        subs.append((topic, cb))
        return lambda: None

    monkeypatch.setattr(fake_mqtt, "async_subscribe", asub)
    monkeypatch.setattr(fake_mqtt, "async_publish",
                        lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(fake_mqtt, "async_connected", lambda hass: False)

    async def noop_save(hass):
        return None

    monkeypatch.setattr(pkg, "save_persistent_data", noop_save)
    import custom_components.window_controller_gateway.ws_gateway as wsg
    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", noop_save)
    er.async_get = lambda h: SimpleNamespace(entities={})

    class Entry:
        data, options, entry_id, title = {}, {}, "EA", "慧尖网关"

        def async_on_unload(self, fn):
            pass

        def add_update_listener(self, fn):
            return lambda: None

    entry = Entry()
    hass = SimpleNamespace(
        data={DOMAIN: {}, "mqtt": object()},
        config=SimpleNamespace(config_dir=str(tmp_path),
                               path=lambda *p: str(tmp_path / p[0])),
        loop=None,
        services=FakeServices(),
        async_create_task=lambda coro, name=None, **kw: asyncio.ensure_future(coro),
        async_add_executor_job=None,
        config_entries=SimpleNamespace(
            async_entries=lambda dom: [SimpleNamespace(entry_id="EA", data={},
                                                       options={}, disabled_by=None,
                                                       state="loaded", unique_id=None)],
            async_update_entry=lambda *a, **k: None,
            async_forward_entry_setups=lambda e, p: asyncio.sleep(0),
        ),
        bus=SimpleNamespace(async_listen_once=lambda ev, cb: (lambda: None)),
    )

    async def go():
        hass.loop = asyncio.get_running_loop()
        return await pkg.async_setup_entry(hass, entry)

    with caplog.at_level(logging.WARNING,
                         logger="custom_components.window_controller_gateway"):
        ok = asyncio.run(go())

    assert ok is True, "条目照样加载成功（这正是假绿：HA 里看不出问题）"
    assert subs and subs[0][0] == "gateway/rpt_rsp", "订阅必须照挂（重连后自动生效）"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "未连上" in text or "未连接" in text, \
        "loaded 但未连接必须留下指名归因（旧行为是'已启动心跳监听器'假成功）"


class _FlowStub:
    def __init__(self, context):
        self.context = context
        self.aborted = []
        self.forms = []

    def async_abort(self, reason=None, **kw):
        self.aborted.append(reason)
        return {"type": "abort", "reason": reason}

    def async_show_form(self, step_id=None, errors=None, **kw):
        self.forms.append((step_id, errors))
        return {"type": "form", "errors": errors}


def test_repair_flow_channel_issue_uses_channel_verdict(monkeypatch):
    """通道修复条目：判据必须是 verify_builtin_channel（标记早已被删，
    用标记判据会把"没修好"误报成"已修好"）。"""
    async def fake_ensure(hass):
        return None

    async def fake_marker(hass):
        return False

    async def ok_verdict(hass):
        return "ok"

    monkeypatch.setattr(mb, "ensure_mqtt_connection", fake_ensure)
    monkeypatch.setattr(mb, "has_bootstrap_marker", fake_marker)
    monkeypatch.setattr(mb, "verify_builtin_channel", ok_verdict)
    async def _drive(stub):
        await cf_mod.ConfigFlow.async_step_repair(stub, {"submit": True})

    stub = _FlowStub({"issue_id": mb.CHANNEL_ISSUE_ID})
    stub.hass = SimpleNamespace()
    asyncio.run(_drive(stub))
    assert stub.aborted == ["mqtt_bootstrap_fixed"]

    async def bad_verdict(hass):
        return "mismatch"

    monkeypatch.setattr(mb, "verify_builtin_channel", bad_verdict)
    stub2 = _FlowStub({"issue_id": mb.CHANNEL_ISSUE_ID})
    stub2.hass = SimpleNamespace()
    asyncio.run(_drive(stub2))
    assert stub2.aborted == []
    assert stub2.forms and stub2.forms[0][1] == {"base": "mqtt_bootstrap_still_pending"}


# ============ 六、mDNS 撞名（用户裁定 B：不改名，只响亮） ============

def _load_mdns():
    spec = importlib.util.spec_from_file_location(
        "mdns_publisher_v1760",
        Path(__file__).resolve().parents[1] / "mdns_publisher.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:  # 环境无 zeroconf：脚本 import 即退出，跳过本组
        pytest.skip("环境无 zeroconf，跳过 mDNS 单测")
    return mod


def test_mdns_conflict_is_named_not_blank():
    """撞名＝NonUniqueNameException（**无文本**）：旧实现打出"服务注册失败: "
    空白行 + 10 秒恒频刷屏，零归因。现在必须点名占位者与处置。"""
    m = _load_mdns()
    exc = (m.NonUniqueNameException() if m.NonUniqueNameException
           else Exception(""))
    state, msg = m.describe_failure(exc, "192.168.1.184", 2022,
                                    owner="192.168.1.91:2022")
    if m.NonUniqueNameException is None:
        pytest.skip("zeroconf 版本无 NonUniqueNameException")
    assert state == "name_conflict"
    assert "已被" in msg and "192.168.1.91:2022" in msg \
        and "192.168.1.184:2022" in msg, "必须点名占位者与本机端点"
    assert msg.startswith("NAMECONFLICT"), "留痕前缀便于 grep"


def test_mdns_blank_exception_never_blank():
    """异常无文本（非撞名同族）也要带类型名——杜绝空白错误行。"""
    m = _load_mdns()
    state, msg = m.describe_failure(OSError(), "10.0.0.2", 2022)
    assert state == "error" and "OSError" in msg


def test_mdns_status_file_written(tmp_path, monkeypatch):
    m = _load_mdns()
    monkeypatch.setattr(m, "_status_path",
                        lambda: str(tmp_path / m.STATUS_FILENAME))
    m.write_status("name_conflict", "192.168.1.184", 2022, owner="192.168.1.91:2022")
    data = json.loads((tmp_path / m.STATUS_FILENAME).read_text(encoding="utf-8"))
    assert data["state"] == "name_conflict" and data["owner"] == "192.168.1.91:2022"


def test_mdns_guard_raises_and_clears_issue(tmp_path, monkeypatch):
    """集成侧：状态文件 name_conflict → HA 提示卡（带占位者/本机 IP 占位符）；
    状态回 ok → 清卡。"""
    from homeassistant.helpers import issue_registry as ir
    ir.ISSUES_CREATED.clear()
    ir.ISSUES_DELETED.clear()
    path = tmp_path / mb.MDNS_STATUS_FILENAME
    hass = SimpleNamespace(config=SimpleNamespace(path=lambda *p: str(tmp_path / p[0])))

    path.write_text(json.dumps({"state": "name_conflict",
                                "owner": "192.168.1.91:2022",
                                "local_ip": "192.168.1.184"}),
                    encoding="utf-8")
    asyncio.run(mb._mdns_guard(hass))
    created = [i for i in ir.ISSUES_CREATED if i["issue_id"] == mb.MDNS_ISSUE_ID]
    assert created and created[0]["is_fixable"] is False, \
        "撞名不可由程序修（用户二选一），提示卡必须非可修"
    assert created[0]["translation_key"] == mb.MDNS_ISSUE_ID

    path.write_text(json.dumps({"state": "ok"}), encoding="utf-8")
    asyncio.run(mb._mdns_guard(hass))
    assert (DOMAIN, mb.MDNS_ISSUE_ID) in ir.ISSUES_DELETED


def test_mdns_translation_keys_symmetric():
    for rel in ("strings.json", "translations/zh-CN.json"):
        d = json.loads((PKG / rel).read_text(encoding="utf-8"))
        node = d["issues"][mb.MDNS_ISSUE_ID]
        assert "title" in node and "description" in node
        assert "{owner}" in node["description"] and "{local_ip}" in node["description"], \
            "占位符必须与 _mdns_guard 传的 translation_placeholders 对齐"


def test_healer_calls_mdns_guard():
    src = (PKG / "mqtt_bootstrap.py").read_text(encoding="utf-8")
    assert "await _mdns_guard(hass)" in src, "healer 常驻巡查必须带上 mDNS 撞名提示"


def test_wiring_pins():
    """接线不丢：指纹落盘被 setup 调用 / 代理 main 双主题+注入 / run.sh 端点文件。"""
    init_src = (PKG / "__init__.py").read_text(encoding="utf-8")
    assert "async_write_instance_uuid_file(hass)" in init_src
    proxy_src = (Path(__file__).resolve().parents[1] / "gateway_discovery_proxy.py"
                 ).read_text(encoding="utf-8")
    assert '"gateway/+/req"' in proxy_src and '"-v"' in proxy_src
    assert "publish_ack=_ack_factory(" in proxy_src
    assert "read_uuid=_read_instance_uuid" in proxy_src
    runsh = (Path(__file__).resolve().parents[1] / "run.sh").read_text(encoding="utf-8")
    assert "window_controller_gateway_mqtt_endpoint.json" in runsh
    assert 'BACKOFF=$((10 * (1 << (MDNS_RETRY - 1))))' in runsh, \
        "mDNS 看门狗必须指数退避（旧实现 10 秒恒频，撞名时永久刷屏）"
    assert "名字冲突" in runsh, "撞名（exit 3）必须有人话提示"
    heal_src = (PKG / "mqtt_bootstrap.py").read_text(encoding="utf-8")
    assert heal_src.count("verify_builtin_channel(hass)") >= 1, \
        "healer 常驻核验必须接上 verify_builtin_channel"
    cf_src = (PKG / "config_flow.py").read_text(encoding="utf-8")
    assert "verify_builtin_channel(self.hass)" in cf_src, \
        "通道修复条目的一键修必须用通道核验判据（标记判据会误报已修好）"
