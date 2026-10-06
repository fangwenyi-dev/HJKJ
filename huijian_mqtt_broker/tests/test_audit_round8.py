"""第八轮全量审计（v1.6.26 修复批）回归钉桩。

审计流程：5 路独立只读审计（MQTT 核心 / 实体与配置流 / Web UI / 基础设施 /
文档版本面）+ 母节点逐条实证复核（其中 2 条审计结论被复核**推翻**不修：
"机械 diff 门禁不实注释"grep 零命中；"Gitee 落后 19 提交"系本地跟踪引用过期）。

本文件覆盖两类：
1. **行为测试**（A-1 发现链实参 / D-4 大小写自纠 / D-1 空令牌握手 /
   B-2 位置持久化恢复 / C-1 幽灵设备清理）——A-1 类"静默失效面"必须断言
   实参（CLAUDE.md 教训：v1.6.0 "entity" 与 v1.6.25 from-import 深度都骗过
   了全绿套件）；
2. **结构/文本钉桩**（awaiting 分支 listener 注册、run.sh 权限与脱敏、
   ci.yaml 门禁形态、版本一致性与"历史注释防漂移"）。
"""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.discovery as disc_mod
import custom_components.window_controller_gateway.mqtt_handler as mh_mod
from custom_components.window_controller_gateway import const as c
from custom_components.window_controller_gateway.device_manager import (
    WindowControllerDeviceManager,
)
from custom_components.window_controller_gateway.mqtt_handler import (
    WindowControllerMQTTHandler,
)

ROOT = Path(__file__).resolve().parents[1]
GW_SN = "100122501203"
OTHER_SN = "100199999999"


# ==================== 假件 ====================

class _Entries:
    def __init__(self, entries=None, progress=None):
        self._entries = entries or []
        self.flow = SimpleNamespace(async_progress=lambda: progress or [])

    def async_entries(self, domain=None):
        return self._entries

    def async_get_entry(self, entry_id):
        # v1.7.31 C-2 前置门兼容：测试代用条目恒"存活"（拒绝路径由
        # test_v1731_field_fixes 专项覆盖）
        return SimpleNamespace(entry_id=entry_id)


class _Hass:
    """带 config_entries 与运行中 loop 的假 hass（A-1 分支必需面）。"""

    def __init__(self, entries=None, progress=None):
        self.data = {c.DOMAIN: {}}
        self.config = SimpleNamespace(config_dir=".")
        self.config_entries = _Entries(entries, progress)
        self.loop = asyncio.get_running_loop()
        self.tasks = []

    def async_create_task(self, coro, **kwargs):
        t = asyncio.ensure_future(coro)
        self.tasks.append(t)
        return t

    def add_job(self, job, *args):
        if callable(job):
            return job(*args)
        return None


class _MockDM:
    def __init__(self):
        self.devices = {}
        self._manually_removed_devices = set()
        self.last_update_call = None

    def get_device(self, sn):
        return self.devices.get(sn)

    def _notify_status_listeners(self, sn):
        pass

    def is_device_manually_removed(self, sn):
        return sn in self._manually_removed_devices

    async def add_device(self, sn, name, typ=None, force=False,
                         is_manual_pairing=False):
        self.devices[sn] = {"sn": sn, "name": name, "status": "connected",
                            "attributes": {}, "last_update": 0}

    async def update_gateway_status(self, status):
        pass

    async def update_device_status(self, sn, status, attributes=None):
        self.last_update_call = (sn, status, attributes)

    def get_gateway_info(self):
        return {"name": "GW", "status": "online"}


class _Publisher:
    def __init__(self):
        self.published = []

    async def __call__(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload)))

    def by_ctype(self, ctype):
        return [p for _, p in self.published if p.get("ctype") == ctype]


def _mk(monkeypatch, gateway_sn=GW_SN, progress=None):
    pub = _Publisher()
    monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
    handler = WindowControllerMQTTHandler(_Hass(progress=progress),
                                          gateway_sn, _MockDM())
    return handler, pub


async def _capture_rsp_cb(handler, monkeypatch):
    """handle_gateway_response 是 _subscribe_topics 内的闭包（生产入口）——
    经打补丁的 async_subscribe 捕获回调，测试走真实注册路径而非旁路。"""
    holder = {}

    async def fake_sub(hass, topic, callback, qos=0):
        holder[topic] = callback
        return lambda: None

    monkeypatch.setattr(mh_mod.mqtt, "async_subscribe", fake_sub)
    await handler._subscribe_topics()
    return holder[c.TOPIC_GATEWAY_RSP]


def _envelope(ctype, msg_id, sn, data):
    return {"head": c.PROTOCOL_HEAD, "ctype": ctype, "id": msg_id,
            "sn": sn, "data": data}


def _msg(ctype, msg_id, sn, data):
    body = json.dumps(_envelope(ctype, msg_id, sn, data))
    return SimpleNamespace(payload=body.encode("utf-8"))


# ==================== A-1：异网关上报 → 发现链实参断言 ====================

class TestGatewayDiscoveryChain:
    """v1.6.25 拆包回归（阻断级）的**行为**守护：from ..discovery 惰性导入
    一旦回潮/挪层，ModuleNotFoundError 被外层 except 吞成一行日志——只有
    对分支断言实参才能拦住（314 全绿时代该分支零覆盖）。"""

    @pytest.mark.asyncio
    async def test_foreign_sn_triggers_discovery_with_exact_args(self, monkeypatch):
        calls = []

        async def fake_discover(hass, sn, name, replace_mode, current_sn):
            calls.append((sn, name, replace_mode, current_sn))

        monkeypatch.setattr(disc_mod, "async_discover_gateway", fake_discover)
        handler, _ = _mk(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        cb(_msg("002", 7, OTHER_SN, {}))
        await asyncio.sleep(0.02)
        assert calls == [(OTHER_SN, f"网关 {OTHER_SN[-4:]}", False, GW_SN)], \
            "发现链必须带 (异SN, 展示名, replace=False, 当前SN) 恰好触发一次"

    @pytest.mark.asyncio
    async def test_replace_mode_context_propagates(self, monkeypatch):
        calls = []

        async def fake_discover(hass, sn, name, replace_mode, current_sn):
            calls.append(replace_mode)

        monkeypatch.setattr(disc_mod, "async_discover_gateway", fake_discover)
        handler, _ = _mk(monkeypatch, progress=[
            {"handler": c.DOMAIN,
             "context": {"source": "replace_gateway"}}])
        cb = await _capture_rsp_cb(handler, monkeypatch)
        cb(_msg("002", 8, OTHER_SN, {}))
        await asyncio.sleep(0.02)
        assert calls == [True], "replace_gateway 流程上下文必须透传 replace_mode"

    @pytest.mark.asyncio
    async def test_malformed_foreign_sn_rejected(self, monkeypatch):
        called = []

        async def fake_discover(*a):
            called.append(a)

        monkeypatch.setattr(disc_mod, "async_discover_gateway", fake_discover)
        handler, _ = _mk(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        # 短 SN / 注入字符：正则闸必须拦截，不得进发现流程
        cb(_msg("002", 9, "bad sn!", {}))
        cb(_msg("002", 9, "123456789", {}))
        await asyncio.sleep(0)
        assert called == []


# ==================== D-4：SN 大小写内存自纠 ====================

class TestSnCaseCorrection:
    @pytest.mark.asyncio
    async def test_matched_report_corrects_case_of_publish_identity(self, monkeypatch):
        """入站匹配大小写不敏感、MQTT 主题敏感：用户录入 "aBc…" 形态、网关
        上报 "ABC…" 形态时，处理链内存订正 gateway_sn，后续 ack/指令全部
        按上报形态发布（否则症状="在线但指令全无反应"）。"""
        stored = "abc123456789"
        reported = "ABC123456789"
        handler, pub = _mk(monkeypatch, gateway_sn=stored)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        cb(_msg("002", 11, reported, {}))
        await asyncio.sleep(0.02)
        assert handler.gateway_sn == reported
        acks = pub.by_ctype("002")
        assert len(acks) == 1
        assert acks[0]["sn"] == reported, "ack payload sn 用订正后形态"
        topic = pub.published[0][0]
        assert topic == f"gateway/{reported}/req", "req 主题用订正后形态"

    @pytest.mark.asyncio
    async def test_exact_case_report_noop(self, monkeypatch):
        handler, pub = _mk(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        cb(_msg("002", 12, GW_SN, {}))
        await asyncio.sleep(0.02)
        assert handler.gateway_sn == GW_SN


# ==================== D-1：空令牌（免认证形态）握手回归 ====================

class TestEmptyTokenHandshake:
    """config_flow 明文支持"空串=不认证"，但 aiohttp≥3.9 对
    WebSocketResponse(protocols=None) 收到带子协议头的请求在 _handshake
    抛 TypeError → 500（微信 connectSocket 恒带子协议=免认证直连整体
    不可用）。aiohttp 3.13.5 活体 A/B 复现后修复为 ()，本测试钉死。"""

    @pytest.mark.asyncio
    async def test_no_token_accepts_subprotocol_and_plain(self):
        from aiohttp import ClientSession

        from test_ws_gateway import FakeDM, FakeHandler, FakeHass
        from custom_components.window_controller_gateway.ws_gateway import (
            WsGatewayServer,
        )

        dm = FakeDM(devices={}, gateway_sn="GW1")
        handler = FakeHandler("GW1")
        data = {"entry_GW1": {"_setup_complete": True, "gateway_sn": "GW1",
                              "mqtt_handler": handler, "device_manager": dm}}

        class RunHass(FakeHass):
            def async_create_task(self, coro, **kwargs):
                return asyncio.ensure_future(coro)

        hass = RunHass(data)
        import socket as _sock
        probe = _sock.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        server = WsGatewayServer(hass, host="127.0.0.1", port=port, token="")
        await server.async_start()
        server._attach_listeners([dm])
        try:
            url = f"ws://127.0.0.1:{port}/ws"
            async with ClientSession() as sess:
                # 带任意子协议（固件/微信形态）→ 必须 101 且不回显
                async with sess.ws_connect(url, protocols=["wx-anything"]) as ws:
                    assert ws._response.headers.get(
                        "Sec-WebSocket-Protocol") is None, \
                        "无认证时不回显子协议（esp_http_server 同款）"
                    await ws.send_str('{"cmd":"get_gateways"}')
                    msg = await ws.receive(timeout=5)
                    assert json.loads(msg.data)["type"] == "gateway_list"
                # 不带子协议 → 同样放行
                async with sess.ws_connect(url) as ws2:
                    await ws2.send_str('{"cmd":"get_gateways"}')
                    msg = await ws2.receive(timeout=5)
                    assert json.loads(msg.data)["gateways"] == [
                        {"sn": "GW1", "online": True}]
        finally:
            await server.async_stop()


# ==================== B-2：未校准 255 的持久化/恢复语义 ====================

class TestPositionRawPersistence:
    def _cover(self, attributes, status=None):
        from custom_components.window_controller_gateway.cover import (
            WindowControllerCover,
        )
        dm = _MockDM()
        dev = {"sn": "5005X", "attributes": attributes}
        if status:
            dev["status"] = status
        dm.devices["5005X"] = dev
        cov = WindowControllerCover(
            hass=None, device_manager=dm, mqtt_handler=None,
            gateway_sn="GW1", device_sn="5005X", device_name="窗")
        return cov, dm

    def _cover_restorable(self):
        # 恢复守卫：仅 status 空/UNKNOWN/CONNECTED 才回填（缓存无实时数据）
        return self._cover({}, status=c.DEVICE_STATUS_CONNECTED)

    def test_uncalibrated_255_persists_raw(self):
        cov, _ = self._cover({"r_travel": 255}, status=c.DEVICE_STATUS_OPEN)
        attrs = cov.extra_state_attributes
        assert attrs["position"] == 100
        assert attrs["r_travel_raw"] == 255, \
            "钳制值之外必须持久化原始值，否则 255 语义一存即丢"

    def test_normal_position_persists_raw_too(self):
        cov, _ = self._cover({"r_travel": "60"}, status=c.DEVICE_STATUS_OPEN)
        attrs = cov.extra_state_attributes
        assert attrs["position"] == 60 and attrs["r_travel_raw"] == 60

    @pytest.mark.asyncio
    async def test_restore_255_keeps_position_unknown(self):
        cov, dm = self._cover_restorable()
        cov.async_get_last_state = _state_fn(
            "open", {"position": 100, "r_travel_raw": 255})
        await cov.async_added_to_hass()
        sn, status, attrs = dm.last_update_call
        assert status == c.DEVICE_STATUS_OPEN, "开关状态照常恢复"
        assert not (attrs or {}) or "r_travel" not in attrs, \
            "255 标记不得回填成 100%——位置保持未知"

    @pytest.mark.asyncio
    async def test_restore_in_range_writes_back(self):
        cov, dm = self._cover_restorable()
        cov.async_get_last_state = _state_fn(
            "open", {"position": 42, "r_travel_raw": 42})
        await cov.async_added_to_hass()
        assert dm.last_update_call[2] == {"r_travel": 42}

    @pytest.mark.asyncio
    async def test_restore_legacy_row_without_raw(self):
        """v1.6.25 旧持久化数据（无 r_travel_raw）按钳制值回填——向后兼容。"""
        cov, dm = self._cover_restorable()
        cov.async_get_last_state = _state_fn("closed", {"position": 0})
        await cov.async_added_to_hass()
        sn, status, attrs = dm.last_update_call
        assert status == c.DEVICE_STATUS_CLOSED
        assert attrs == {"r_travel": 0}


def _state_fn(state, attributes):
    def get():
        async def _c():
            return SimpleNamespace(state=state, attributes=attributes)
        return _c()
    return get


# ==================== C-1：remove_device 缓存无关清理 ====================

class _Reg:
    def __init__(self, device=None):
        self._device = device
        self.removed = []

    def async_get_device(self, identifiers=None):
        return self._device

    def async_remove_device(self, device_id):
        self.removed.append(device_id)


class _DMHass:
    def __init__(self):
        self.data = {}
        self.config = SimpleNamespace(config_dir=".")
        self.coros = []
        # v1.7.31（C-2 前置门）：add_device 首行查条目存活——测试代用条目
        # 恒存活（拒绝路径由 test_v1731_field_fixes 专项双臂覆盖）
        self.config_entries = SimpleNamespace(
            async_get_entry=lambda eid: SimpleNamespace(entry_id=eid))

    def async_create_task(self, coro, **kwargs):
        # 持久化写盘协程在测试里直接丢弃（无 IO），任务面语义不在此钉
        self.coros.append(coro)
        coro.close()
        return None


def _make_dm():
    hass = _DMHass()
    entry = SimpleNamespace(entry_id="e1",
                            data={c.CONF_GATEWAY_SN: GW_SN,
                                  c.CONF_GATEWAY_NAME: "慧尖网关"})
    dm = WindowControllerDeviceManager(hass, entry)
    # 文件 IO 收口 monkeypoint：持久化保存全部 no-op
    dm._save_device_to_gateway_mapping = lambda: None
    dm._trigger_persistent_save = lambda: None
    return hass, dm


class TestRemoveDeviceGhost:
    @pytest.mark.asyncio
    async def test_cache_miss_still_cleans_everything(self):
        """幽灵设备形态：设备不在缓存但映射/名单/注册表/回调面残留——
        旧实现整体静默 no-op，现必须全部清掉并如实返回。"""
        hass, dm = _make_dm()
        ghost = "50022E019999"
        hass.data[c.DOMAIN][c.DEVICE_TO_GATEWAY_MAPPING] = {ghost: GW_SN}
        sp = {ghost: {"speed": 5}}
        dm.get_device_setpoints = lambda: sp
        reg = _Reg(device=SimpleNamespace(id="dev-x"))
        async def fake_reg():
            return reg
        dm._get_device_registry = fake_reg
        notified = []

        async def cb(sn, name, typ):
            notified.append((sn, name, typ))

        dm._device_removed_callbacks = [cb]

        assert await dm.remove_device(ghost) is True
        assert ghost not in hass.data[c.DOMAIN][c.DEVICE_TO_GATEWAY_MAPPING], \
            "映射残留=下次 002 复活"
        assert ghost not in sp, "setpoints 残留脏数据"
        assert ghost in hass.data[c.DOMAIN][c.GLOBAL_MANUALLY_REMOVED_DEVICES]
        assert reg.removed == ["dev-x"], "设备注册表条目必须删除"
        assert notified == [(ghost, ghost, c.DEVICE_TYPE_WINDOW_OPENER)], \
            "缓存缺失时 name 兜底 SN、type 兜底开窗器（消费方按类型过滤）"

    @pytest.mark.asyncio
    async def test_cached_path_unchanged(self):
        hass, dm = _make_dm()
        sn = "50022E010603"
        await dm.add_device(sn, "客厅窗", c.DEVICE_TYPE_WINDOW_OPENER)
        assert sn in dm.devices
        reg = _Reg(device=None)
        async def fake_reg():
            return reg
        dm._get_device_registry = fake_reg
        assert await dm.remove_device(sn) is True
        assert sn not in dm.devices

    @pytest.mark.asyncio
    async def test_nowhere_is_idempotent_true(self):
        hass, dm = _make_dm()
        async def fake_reg():
            return _Reg(None)
        dm._get_device_registry = fake_reg
        assert await dm.remove_device("50022E000000") is True


# ==================== 结构/文本钉桩（run.sh、CI、Web、版本面） ====================

RUN_SH = (ROOT / "run.sh").read_text(encoding="utf-8")
CI = (ROOT.parent / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
INDEX = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
INIT = (ROOT / "custom_components" / "window_controller_gateway" /
        "__init__.py").read_text(encoding="utf-8")


class TestHardeningPins:
    def test_ingress_conf_chmod(self):
        i = RUN_SH.find("\nNGINXEOF")
        assert i >= 0
        tail = RUN_SH[i:i + 600]
        assert re.search(r"chmod 600\s+/etc/nginx/http\.d/ingress\.conf", tail), \
            "含明文 SUPERVISOR_TOKEN 的 ingress.conf 必须 600（W-1）"

    def test_mosquitto_conf_chmod_on_bridge_write(self):
        seg = RUN_SH[RUN_SH.find("_bridge_on()"):RUN_SH.find("_bridge_off()")]
        assert 'chmod 600 "${MOSQ_CONF}"' in seg, \
            "桥段（含对端凭据）写入后 mosquitto.conf 收紧 600（D-5）"

    def test_bridge_on_chmod_before_kill(self):
        seg = RUN_SH[RUN_SH.find("_bridge_on()"):RUN_SH.find("_bridge_off()")]
        assert seg.find("chmod 600") < seg.find("kill -TERM"), \
            "chmod 必须先于计划内重启——重启窗口里 conf 不得处于可读态"

    def test_diagnostic_redaction(self):
        assert "REDACTED" in RUN_SH
        assert re.search(
            r"sed -E 's/\^.*password\|remote_password\|username\|remote_username",
            RUN_SH), "崩溃诊断打印 mosquitto.conf 必须脱敏（D-4）"
        # 诊断段不得再用裸 cat 输出 conf
        diag = RUN_SH[RUN_SH.find("mosquitto.conf 内容"):]
        assert not re.match(r"^[^\n]*cat /etc/mosquitto/mosquitto\.conf", diag), \
            "诊断行不得回退为裸 cat"


class TestCiPins:
    def test_lint_uses_compileall(self):
        assert "compileall -q huijian_mqtt_broker/custom_components" in CI, \
            "语法门必须递归子包（A-2：*.py glob 曾整体漏掉 mqtt_handler/）"
        assert "py_compile huijian_mqtt_broker/custom_components/window_controller_gateway/*.py" not in CI

    def test_changelog_extractor_keeps_header(self):
        assert '{ found=1; print; next }' in CI, \
            "Release 正文 awk 必须输出 ## [版本] 标题行（E-3）"

    def test_warm_mirror_repo_derived(self):
        assert '"${ARCH}-${IMAGE_NAME}"' in CI, \
            "warm-mirrors 仓名必须由 image 字段派生（E-10）"
        assert 'REPO="${ARCH}-huijian-mqtt-broker"' not in CI, \
            "硬编码回潮即失败（image 改名后预热静默失效）"

    def test_hard_gate_still_hard(self):
        # continue-on-error 全文件唯一合法出现= warm-mirrors（best-effort
        # 预热）；e2e 自 v1.6.22 起是发布硬门禁，不得被顺手加回
        #
        # 审计 2026-09-30 H-7/A4 改钉：旧写法只数全文出现次数 + 比一个字节偏移
        # （`0 <= CI.find("warm-mirrors:") <= keys[0]`）。实测变异——把这行
        # continue-on-error 从 warm-mirrors **挪到 e2e job**（真栈 E2E 由发布硬
        # 门禁降级为软失败，manifest/release 照发）——计数仍是 1、偏移仍满足，
        # 本文件与 test_v1753 一起 40 条全绿。判据必须绑到"它落在 warm-mirrors
        # 这个 job 的正文区间里"，而不是"在文件里某处、且在某个字符串之后"。
        keys = [m.start() for m in re.finditer(r"^\s*continue-on-error:", CI, re.M)]
        assert len(keys) == 1, f"continue-on-error 应仅剩 warm-mirrors 一处: {len(keys)}"
        wm = re.search(r"^  warm-mirrors:\n", CI, re.M)
        assert wm, "warm-mirrors job 锚丢失（改名了？本钉需重写）"
        nxt = re.search(r"^  [a-z][\w-]*:\n", CI[wm.end():], re.M)
        wm_end = wm.end() + (nxt.start() if nxt else len(CI))
        assert wm.start() <= keys[0] < wm_end, (
            "唯一一处 continue-on-error 必须位于 warm-mirrors job 正文区间内——"
            "挪进 e2e/manifest/release 就是把发布硬门禁降级为软失败"
            "（实际偏移 %s，合法区间 [%s, %s)）" % (keys[0], wm.start(), wm_end))


class TestWebPins:
    def test_footer_placeholder(self):
        assert '<span id="footerVersion"></span>' in INDEX, \
            "页脚版本必须空占位、由 JS 回填（W-6）"
        assert not re.search(
            r'id="footerVersion">v?\d+\.\d+', INDEX)

    def test_current_version_unique_declare(self):
        assert INDEX.count("CURRENT_VERSION = '") == 1


class TestAwaitingSetupBranch:
    """B-1/A-2/A-3 的结构面：awaiting 分支三个补齐点必须在位。"""

    def _seg(self):
        start = INIT.find("    if not gateway_sn:")
        end = INIT.find("# ---- 有网关 SN：完整设置 ----")
        assert 0 <= start < end, "awaiting 分支锚丢失"
        return INIT[start:end]

    def test_update_listener_registered(self):
        assert "entry.async_on_unload(entry.add_update_listener(async_update_options))" in self._seg(), \
            "awaiting 条目必须注册 update listener（B-1：否则添加网关后永不重载）"

    def test_heartbeat_deferred_arm(self):
        seg = self._seg()
        assert "_arm_heartbeat_when_mqtt_ready" in seg, \
            "MQTT 未就绪时心跳监听器必须转后台等待武装（A-2）"
        assert "async_wait_mqtt_loaded" in seg

    def test_ws_gateway_ensured(self):
        assert "async_ensure_ws_gateway" in self._seg(), \
            "awaiting-only 安装也应监听 9001（A-3：半开口径定案）"


class TestCleanupUnloadsPlatforms:
    def test_cleanup_partial_unloads(self):
        start = INIT.find("async def _cleanup_partial_setup")
        seg = INIT[start:start + 3200]
        assert "async_unload_platforms(entry, PLATFORMS)" in seg, \
            "setup 失败清理必须卸载已 forward 平台（B-A1：僵尸实体）"


class TestVersionFields:
    """五处字段一致 + 历史注释防 bump 漂移（MEMORY 铁律）。

    want 从 config.yaml 动态读（v1.6.25「测试动态版本锚根治」定案）——
    否则每次升版都要回来改这个字面量，与"版本单一真源"自相矛盾。
    """

    def test_fields(self):
        cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
        want = re.search(r'^version: "([\d.]+)"', cfg, re.M).group(1)
        mf = json.loads((ROOT / "custom_components/window_controller_gateway/manifest.json").read_text(encoding="utf-8"))
        vj = json.loads((ROOT / "www/version.json").read_text(encoding="utf-8"))
        assert want, "config.yaml 未解析出版本"
        assert mf["version"] == want
        assert vj["addon_version"] == want == vj["integration_version"]
        assert f"CURRENT_VERSION = '{want}'" in INDEX
        # 第 5 张：README 版本徽章。迁仓到 HJKJ 后本仓一条 release 命名空间被两个
        # 加载项共用（实测 /releases/latest 现在是语音的 vo-1.2.1），"latest 型"徽章
        # 会把网关卡片显示成语音版本号 ⇒ 徽章改成静态钉死版本号，漏 bump 当场红。
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert f"badge/version-v{want}-blue" in readme, \
            "README 版本徽章未随 bump 更新（仍写着别的版本＝网关卡片显示错版本号）"

    def test_asset_cache_bust_query(self):
        """v1.7.1 实锤教训：现场容器已是 1.7.0（update 实体实证），但用户
        浏览器仍呈现 ≤1.6.29 旧资源——nginx no-store 挡不住一切客户端缓存
        形态（Service Worker/国产壳）。静态引用必须挂 ?v=版本，每次发布
        URL 变化强制穿透任何缓存。此测试钉死：三资源带 query 且与
        config.yaml 权威版本一致（bump 忘改 query 当场红）。"""
        cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
        want = re.search(r'^version: "([\d.]+)"', cfg, re.M).group(1)
        for res in ("css/huijian.css", "js/starsky.js", "js/huijian.js"):
            assert f'{res}?v={want}"' in INDEX, f"{res} 缺 ?v={want} cache-bust"
        bare = re.findall(r'(?:href|src)="((?:css|js)/[\w.]+)"', INDEX)
        assert not bare, f"裸引用（无版本 query）会被客户端缓存钉死: {bare}"

    def test_changelog_contains_current_version_section(self):
        """动态锚：CHANGELOG 必须已含 config.yaml 同版本段（发布铁律的
        机器化——忘写 CHANGELOG 就升版当场红）。"""
        cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
        want = re.search(r'^version: "([\d.]+)"', cfg, re.M).group(1)
        chg = (ROOT.parent / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## [{want}]" in chg, f"CHANGELOG 缺 [{want}] 段"

    def test_historical_comments_not_drifted(self):
        # 拆包/共存桥/三文件化的历史注释必须仍指认真实引入版本
        cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
        assert "v1.6.24 引入" in cfg, "共存桥归属注释不得被 bump sed 漂移"
        assert "v1.6.25 Web UI 三文件化" in INDEX
        chg = (ROOT.parent / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "## [1.6.25] - 2026-09-06" in chg
        assert "## [1.6.26] - 2026-09-07" in chg
        assert "特性公开归并" in chg, "E-2：1.6.24 未发布，桥特性须并入 1.6.26 公开段"

    def test_claude_md_synced(self):
        claude = (ROOT.parent / "CLAUDE.md").read_text(encoding="utf-8")
        assert "compileall" in claude
        assert "双源并集取版本号" in claude.replace("最大者", ""), \
            "W-2：更新检查记载必须与 huijian.js 双源实现一致"
        assert "py_compile custom_components" not in claude
