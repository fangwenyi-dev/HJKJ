"""v1.7.31 修复批守卫（0918 真机测试 + 五路审计全部实锤项）。

现场抓双修：F-A（STOP one-time 监听器双重移除，台架 A/B 实锤）、
F-B（via_device 写参面弃用漏迁 → utils.via_device_kwargs 双形态出口，
真签名核验旧 HA 2026.1.3 无 via_device_id 形参——探测分形非拍脑袋）。
审计判死批：C-1（create_issue 真签名 is_fixable，臆造参 TypeError 实锤）、
A-1（healer 切片睡眠）、A-3（三态门 entry_state_for_sn，BUG-5 统一口径）、
A-4（心跳兜底 WARNING+节流）、A-5（ignored 看守豁免，复现转正）、
A-6（派发任务 cleanup 收口）、B-1（unbind reload/删除甄别）、
B-4（运行态令牌 charset 闸）、C-2（add_device 条目存活前置门）、
C-3（_pairing 常量单一真源）、D-1/2/3/4（前端真值源/降级回填/注释/域过滤）、
Gitee 徽章 direction=desc 排序窗口盲区。各条目的"判死证据"见 commit 正文。
"""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import custom_components.window_controller_gateway as pkg
from custom_components.window_controller_gateway.const import (
    DOMAIN, DEVICE_TO_GATEWAY_MAPPING)
import custom_components.window_controller_gateway.device_manager as dm
from custom_components.window_controller_gateway.utils import (
    resolve_via_device_id)

_HERE = Path(__file__).resolve()
_PKG_DIR = _HERE.parents[1] / "custom_components" / "window_controller_gateway"
_WWW_DIR = _HERE.parents[1] / "www"


# ==================== F-A：STOP 监听器双重移除 ====================

class _UnsubRecorder:
    """模拟 HA core 语义：one-time 监听器被消费后再 remove → core 打
    "Unable to remove unknown job listener" ERROR。这里只记调用次数，
    行为测试断言 0（修复前=1）。"""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def _mk_runtime_hass():
    runtime = {"_stop_unsub": _UnsubRecorder()}
    hass = SimpleNamespace(
        data={DOMAIN: {"E1": runtime}},
        config_entries=SimpleNamespace(
            async_get_entry=lambda eid: None,
            async_unload_platforms=None,  # runtime 无 _platforms_forwarded 不会被调
        ),
    )
    entry = SimpleNamespace(entry_id="E1", title="慧尖网关", data={})
    return hass, entry, runtime


class TestStopHandlerNoDoubleUnsub:
    def test_handler_does_not_unsub_consumed_listener(self, monkeypatch):
        """STOP 派发形态：handler 自调 unload 不得再触碰已消费的 _stop_unsub。"""
        async def noop_save(hass):
            return None
        monkeypatch.setattr(pkg, "save_persistent_data", noop_save)
        hass, entry, runtime = _mk_runtime_hass()
        rec = runtime["_stop_unsub"]  # 先捕获引用：unload 收尾会清掉 runtime
        handler = pkg._make_shutdown_handler(hass, entry)
        asyncio.run(handler(SimpleNamespace(event_type="x")))
        assert rec.calls == 0, (
            "F-A 回归：STOP 派发时总线已消费该 one-time 监听器，"
            "unload 再 unsub 必打 'Unable to remove unknown job listener' ERROR")
        # handler 内的 unload 应真实执行（runtime 被清 = 走到 unload 收口）
        assert "E1" not in hass.data[DOMAIN], "handler 必须完成自卸载"

    def test_reload_path_still_unsubs(self, monkeypatch):
        """非 STOP（reload/手动卸载）形态：监听器仍在总线上，unload 步骤 1
        必须照常 unsub——反钉修复不越界把正常退订也删掉。"""
        async def noop_save(hass):
            return None
        monkeypatch.setattr(pkg, "save_persistent_data", noop_save)
        hass, entry, runtime = _mk_runtime_hass()
        ok = asyncio.run(pkg.async_unload_entry(hass, entry))
        assert ok is True
        assert runtime["_stop_unsub"].calls == 1, \
            "reload 路径 unload 必须退订 STOP 监听器（漏退订=条目重建后双监听器）"


# ==================== F-B：via_device 写参数面迁移 ====================

class _GatewayRegistry:
    """记录 async_get_or_create 实参的假设备注册表（实参断言，非源码钉）。

    form="new"：模拟现场 HA 2026.9.2 实态（via_device_id 存在、via_device 弃用）
    form="old"：模拟台架 HA 2026.1.3 真签名核验实态（仅 via_device）
    ——签名均显式声明参数名（非 **kwargs 吞），供 utils 的 inspect 探测分形。
    """

    def __init__(self, gateway_id="GW-REG-ID", gateway_exists=True, form="new"):
        self._gateway_id = gateway_id
        self._gateway_exists = gateway_exists
        self.kwargs = None
        if form == "new":
            async def _new(self_, *, config_entry_id=None, identifiers=None,
                           name=None, manufacturer=None, model=None,
                           via_device_id=None, **rest):
                self_.kwargs = {"config_entry_id": config_entry_id,
                                "identifiers": identifiers, "name": name,
                                "manufacturer": manufacturer, "model": model,
                                "via_device_id": via_device_id}
                return SimpleNamespace(id="DEV-1", config_entries={"E1"})
            self.async_get_or_create = _new.__get__(self)
        else:
            async def _old(self_, *, config_entry_id=None, identifiers=None,
                           name=None, manufacturer=None, model=None,
                           via_device=None, **rest):
                self_.kwargs = {"config_entry_id": config_entry_id,
                                "identifiers": identifiers, "name": name,
                                "manufacturer": manufacturer, "model": model,
                                "via_device": via_device}
                return SimpleNamespace(id="DEV-1", config_entries={"E1"})
            self.async_get_or_create = _old.__get__(self)

    def async_get_device(self, identifiers=None):
        if not self._gateway_exists:
            return None
        return SimpleNamespace(id=self._gateway_id)


def _fake_self(reg):
    async def _get_registry():
        return reg
    return SimpleNamespace(
        gateway_sn="10012250123f",
        entry=SimpleNamespace(entry_id="E1"),
        hass=SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_entry=lambda eid: object())),
        _get_device_registry=_get_registry,
    )


class TestResolveViaDeviceId:
    def test_hit_returns_device_id(self):
        reg = _GatewayRegistry(gateway_id="ABC123")
        assert resolve_via_device_id(reg, "10012250123f") == "ABC123"

    def test_miss_returns_none(self):
        reg = _GatewayRegistry(gateway_exists=False)
        assert resolve_via_device_id(reg, "10012250123f") is None

    def test_registry_error_returns_none(self):
        class _Boom:
            def async_get_device(self, **k):
                raise RuntimeError("registry 读面异常")
        assert resolve_via_device_id(_Boom(), "x") is None, \
            "宿主解析失败宁缺归属、不断子设备注册主链"

    def test_kwargs_probe_two_forms(self):
        """双形态出口按运行时签名分形（manifest 支持 2024.12 起全区间）。"""
        from custom_components.window_controller_gateway.utils import (
            via_device_kwargs)
        from custom_components.window_controller_gateway.const import DOMAIN as D
        new = _GatewayRegistry(gateway_id="NID", form="new")
        assert via_device_kwargs(new, "sn1") == {"via_device_id": "NID"}
        old = _GatewayRegistry(form="old")
        # 旧 HA 无 via_device_id：保持原 via_device=(DOMAIN, sn) 形态——
        # 无条件新写法会在旧版 TypeError 打死设备注册（台架 2026.1.3 实证）
        assert via_device_kwargs(old, "sn1") == {"via_device": (D, "sn1")}


class TestFastRegisterRealArgs:
    def test_new_ha_receives_via_device_id(self):
        reg = _GatewayRegistry(gateway_id="GW-REG-ID", form="new")
        asyncio.run(dm.WindowControllerDeviceManager
                    ._async_fast_register_device(_fake_self(reg),
                                                 "50063420020A", "开窗器"))
        assert reg.kwargs is not None, "注册调用未发生"
        assert "via_device" not in reg.kwargs, \
            "F-B 回归：新 HA 上 via_device=(DOMAIN, sn) 已弃用（2027.8 停摆）"
        assert reg.kwargs.get("via_device_id") == "GW-REG-ID", \
            "宿主归属必须传父设备注册表 id（实参断言）"

    def test_old_ha_keeps_via_device_tuple(self):
        reg = _GatewayRegistry(form="old")
        asyncio.run(dm.WindowControllerDeviceManager
                    ._async_fast_register_device(_fake_self(reg),
                                                 "50063420020A", "开窗器"))
        from custom_components.window_controller_gateway.const import DOMAIN as D
        assert reg.kwargs.get("via_device") == (D, "10012250123f"), \
            "旧 HA 兼容形态不得丢宿主归属（manifest 下限 2024.12）"
        assert "via_device_id" not in reg.kwargs

    def test_missing_gateway_omits_via(self):
        reg = _GatewayRegistry(gateway_exists=False, form="new")
        asyncio.run(dm.WindowControllerDeviceManager
                    ._async_fast_register_device(_fake_self(reg),
                                                 "50063420020A", "开窗器"))
        assert reg.kwargs.get("via_device_id") is None
        assert "via_device" not in reg.kwargs


class TestNoDeprecatedViaDeviceCall:
    """AST 守卫：全集成运行时代码禁止以 ``via_device=`` 作调用实参。

    用 AST 而非源码文本扫描——docstring/注释里提及 ``via_device=(DOMAIN, sn)``
    的历史说明（utils.get_via_device_id 等）不得误伤；只禁真实调用面。
    旧形态仅允许经 via_device_kwargs 的 dict 键出口（兼容旧 HA）。
    """

    def test_ast_forbid_via_device_kwarg(self):
        offenders = []
        for py in _PKG_DIR.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "via_device":
                            offenders.append(f"{py.name}:{node.lineno}")
        assert not offenders, (
            f"发现弃用 via_device= 入参调用（HA 2027.8 停摆面）：{offenders}"
            "——一律改走 utils.via_device_kwargs 双形态出口")

    def test_six_migration_points_present(self):
        src = (_PKG_DIR / "device_manager.py").read_text(encoding="utf-8")
        n = src.count("**via_device_kwargs(device_registry, ")
        assert n == 6, \
            f"via_device_kwargs 迁移点须恰为 6（实得 {n}）：注册×3/更新×1/迁移×2"


# ==================== C-1：issue_registry 真签名调用 ====================

class TestIssueRegistryTrueSignature:
    def test_report_takeover_issue_passes_real_signature(self, caplog):
        """conftest 复制品=2026.1.3 真签名逐字（is_fixable 必填 keyword-only）。
        臆造 is_fix_flow 会在复制品上直接 TypeError——本测当场抓获。"""
        import homeassistant.helpers.issue_registry as ir
        import custom_components.window_controller_gateway.mqtt_bootstrap as mb
        ir.ISSUES_CREATED.clear()
        ir.ISSUES_DELETED.clear()
        caplog.set_level(logging.DEBUG)
        mb._report_takeover_issue(SimpleNamespace())
        assert len(ir.ISSUES_CREATED) == 1, \
            "C-1 回归：真签名调用必须真实出卡（旧 is_fix_flow 必 TypeError 被吞）"
        made = ir.ISSUES_CREATED[0]
        assert made["is_fixable"] is True
        assert made["issue_id"] == "mqtt_bootstrap_pending"
        assert made["domain"] == "window_controller_gateway"
        assert not [r for r in caplog.records
                    if "修复条目失败" in r.getMessage()], "不得再走 except 吞没路径"

    def test_clear_takeover_issue_signature(self):
        import homeassistant.helpers.issue_registry as ir
        import custom_components.window_controller_gateway.mqtt_bootstrap as mb
        ir.ISSUES_DELETED.clear()
        mb._clear_takeover_issue(SimpleNamespace())
        assert ("window_controller_gateway", "mqtt_bootstrap_pending") \
            in ir.ISSUES_DELETED


# ==================== A-3：三态门 ====================

class TestEntryStateForSn:
    @staticmethod
    def _hass(entries):
        return SimpleNamespace(config_entries=SimpleNamespace(
            async_entries=lambda d: entries))

    def _entry(self, sn, disabled=None):
        return SimpleNamespace(data={"gateway_sn": sn}, disabled_by=disabled)

    def test_three_states(self):
        from custom_components.window_controller_gateway.utils import (
            entry_state_for_sn as f)
        assert f(self._hass([self._entry("ABC123456")]), "abc123456") \
            == "configured"
        assert f(self._hass([self._entry("ABC123456", "user")]), "abc123456") \
            == "disabled", "禁用条目不得再算已配置（BUG-5 统一口径）"
        assert f(self._hass([]), "nope123456") == "none"
        # 混存：同 SN 有 loaded 条目即 configured（禁用条目不遮蔽启用者）
        assert f(self._hass([self._entry("X123456789", "user"),
                             self._entry("X123456789")]), "x123456789") \
            == "configured"

    def test_boom_falls_to_none(self):
        from custom_components.window_controller_gateway.utils import (
            entry_state_for_sn)
        class _Bad:
            def async_entries(self, d):
                raise RuntimeError("炸")
        assert entry_state_for_sn(SimpleNamespace(config_entries=_Bad()), "s") \
            == "none", "判定面故障不得阻断止血代答"

    def test_healer_exit_gate_uses_enabled_count(self):
        src = (_PKG_DIR / "mqtt_bootstrap.py").read_text(encoding="utf-8")
        assert "_enabled_huijian_entry_count(hass) == 0" in src, \
            "A-3：healer 出口不得再被禁用条目骗成永续巡查"
        assert "not hass.config_entries.async_entries(DOMAIN)" not in src, \
            "旧默认调用（含 disabled）反钉"

    def test_no_raw_async_entries_gates_remain(self):
        """A-3 收口：耳朵/协议的 SN 归属判定一律经三态门。"""
        for name in ("__init__.py", "mqtt_handler/_protocol.py"):
            src = (_PKG_DIR / name).read_text(encoding="utf-8")
            assert "entry_state_for_sn" in src, f"{name} 必须走三态门"


# ==================== A-1：healer 切片睡眠 ====================

class TestHealerInterruptibleSleep:
    def test_stopping_mid_sleep_exits(self, monkeypatch):
        import custom_components.window_controller_gateway.mqtt_bootstrap as mb
        monkeypatch.setattr(mb, "HEALER_SLEEP_CHUNK", 0.05)
        flags = {"stopping": False}
        hass = SimpleNamespace(
            is_stopping=False,
            config_entries=SimpleNamespace(
                async_entries=lambda d: [SimpleNamespace(entry_id="E1")]),
        )

        async def watch():
            await asyncio.sleep(0.12)
            flags["stopping"] = True
            hass.is_stopping = True

        async def main():
            w = asyncio.ensure_future(watch())
            # delay=100s：切片下应在 is_stopping 翻位后 ≤1 片内返回 False
            r = await mb._interruptible_sleep(hass, 100.0)
            await w
            return r
        assert asyncio.run(main()) is False, \
            "A-1：切片复检必须让停机在 CHUNK 内打断长睡"

    def test_full_sleep_returns_true(self, monkeypatch):
        import custom_components.window_controller_gateway.mqtt_bootstrap as mb
        monkeypatch.setattr(mb, "HEALER_SLEEP_CHUNK", 0.02)
        hass = SimpleNamespace(
            is_stopping=False,
            config_entries=SimpleNamespace(
                async_entries=lambda d: [SimpleNamespace(entry_id="E1")]),
        )
        assert asyncio.run(mb._interruptible_sleep(hass, 0.06)) is True

    def test_healer_no_single_long_sleep(self):
        src = (_PKG_DIR / "mqtt_bootstrap.py").read_text(encoding="utf-8")
        healer_seg = src[src.index("async def _healer"):]
        healer_seg = healer_seg.split("runtime[", 1)[0]
        assert "await asyncio.sleep(delay)" not in healer_seg, \
            "单发长睡回潮=停机预算重烧"
        assert "_interruptible_sleep(hass, delay)" in healer_seg


# ==================== A-4/A-5：留痕与豁免 ====================

class TestLogThrottled:
    def test_first_pass_dedup_expiry(self):
        from custom_components.window_controller_gateway.utils import (
            log_throttled)
        import time as _t
        hass = SimpleNamespace(data={})
        got = []
        fn = lambda m, *a, **k: got.append(m)  # noqa: E731
        log_throttled(hass, "bkt", "k1", 600.0, fn, "第一条")
        log_throttled(hass, "bkt", "k1", 600.0, fn, "被抑制")
        log_throttled(hass, "bkt", "k2", 600.0, fn, "另一 key")
        assert got == ["第一条", "另一 key"]
        # 过期放行：手动把时间戳拨旧
        hass.data[DOMAIN]["bkt"]["k1"] = _t.monotonic() - 700
        log_throttled(hass, "bkt", "k1", 600.0, fn, "过期后重来")
        assert got[-1] == "过期后重来"

    def test_heartbeat_fallback_never_debug(self):
        src = (_PKG_DIR / "__init__.py").read_text(encoding="utf-8")
        assert '_LOGGER.debug("心跳监听器处理消息出错' not in src, \
            "A-4 回归：兜底回落 DEBUG=发现链断裂零可见（0917 取证铁律）"
        assert '_hb_err_logged' in src and "exc_info=True" in src

    def test_promotion_watch_ignores_ignored_gateway(self):
        """A-5 复现转正：ignored 态与"卡片挂起"同权静默退场。"""
        import custom_components.window_controller_gateway.utils as u
        from custom_components.window_controller_gateway.const import (
            GLOBAL_IGNORED_GATEWAYS)
        sn = "10012250123f"
        ignored = {sn}
        hass = SimpleNamespace(
            data={DOMAIN: {GLOBAL_IGNORED_GATEWAYS: ignored,
                           "discovery": {"ignored_gateways": ignored}}},
            config_entries=SimpleNamespace(
                async_entries=lambda d: [],
                flow=SimpleNamespace(async_progress=lambda: [])),
            async_create_task=lambda coro, name=None: asyncio.ensure_future(coro),
        )
        records = []

        class _Cap(logging.Handler):
            def emit(self, rec):
                records.append(rec.getMessage())

        lg = logging.getLogger(u.__name__)
        lg.addHandler(_Cap())
        old = u.EAR_PROMOTION_WATCH_SECONDS
        u.EAR_PROMOTION_WATCH_SECONDS = 0.05
        try:
            async def main():
                u._watch_ear_promotion(hass, sn)
                await asyncio.sleep(0.4)
            asyncio.run(main())
        finally:
            u.EAR_PROMOTION_WATCH_SECONDS = old
            lg.removeHandler(lg.handlers[-1])
        warns = [m for m in records if "未转为配置条目" in m]
        assert not warns, \
            f"A-5 回归：用户已忽略的网关不得打误导告警，实得 {warns}"


# ==================== B-1：unbind reload/删除甄别 ====================

class TestUnbindReloadDiscrimination:
    @staticmethod
    def _mk(monkeypatch, gw, dev, entries_after):
        import custom_components.window_controller_gateway.ws_gateway as wsg
        monkeypatch.setattr(wsg, "GATEWAY_READY_DELAY", 0.0)

        class _CE:
            def async_entries(self, d=None):
                return entries_after()
        hass = SimpleNamespace(data={DOMAIN: {}}, config_entries=_CE())
        server = wsg.WsGatewayServer(hass, host="127.0.0.1", port=9998, token="t")

        class _H:
            connected = True
            gateway_sn = gw
            async def unbind_device(self, sn):
                return None
        class _DM:
            devices = {dev: {}}
        hass.data[DOMAIN] = {"e1": {
            "gateway_sn": gw, "_setup_complete": True,
            "mqtt_handler": _H(), "device_manager": _DM()}}
        orig_find = server._find_entry
        n = {"i": 0}
        def find(s):
            n["i"] += 1
            return orig_find(s) if n["i"] == 1 else None  # sleep 后翻脸
        server._find_entry = find
        return server

    def test_reload_window_returns_honest_false(self, monkeypatch):
        gw, dev = "10012250123f", "50063420020A"
        server = self._mk(monkeypatch, gw, dev, lambda: [
            SimpleNamespace(entry_id="e1", data={"gateway_sn": gw})])
        res = asyncio.run(server._cmd_unbind({"gwSn": gw, "devSn": dev}))
        assert res["ok"] is False and "reload" in res.get("msg", ""), \
            "reload 途中不得假成功 ack（B-1 幽灵设备链起点）"

    def test_deleted_entry_still_true(self, monkeypatch):
        gw, dev = "10012250123f", "50063420020A"
        server = self._mk(monkeypatch, gw, dev, lambda: [])
        res = asyncio.run(server._cmd_unbind({"gwSn": gw, "devSn": dev}))
        assert res["ok"] is True, "条目真删除=删除随条目收口，保持原语义"


# ==================== B-4：运行态令牌 charset 闸 ====================

class TestRuntimeTokenGate:
    @staticmethod
    def _hass(options):
        return SimpleNamespace(config_entries=SimpleNamespace(
            async_entries=lambda d: [SimpleNamespace(entry_id="e1",
                                                     options=options)]))

    def test_dirty_token_falls_back(self):
        from custom_components.window_controller_gateway.ws_gateway import (
            ws_gateway_wanted)
        from custom_components.window_controller_gateway.const import (
            CONF_WS_GATEWAY_ENABLED, CONF_WS_GATEWAY_TOKEN,
            DEFAULT_WS_GATEWAY_TOKEN)
        w = ws_gateway_wanted(self._hass({
            CONF_WS_GATEWAY_ENABLED: True,
            CONF_WS_GATEWAY_TOKEN: "has space"}))
        assert w[1] == DEFAULT_WS_GATEWAY_TOKEN, \
            "含空白令牌=不可满足握手永久 401，运行时必须回退"
        w = ws_gateway_wanted(self._hass({
            CONF_WS_GATEWAY_ENABLED: True,
            CONF_WS_GATEWAY_TOKEN: "a" * 80}))
        assert w[1] == DEFAULT_WS_GATEWAY_TOKEN, "超长令牌（固件判式上限）同样回退"

    def test_empty_token_legal_stays(self):
        from custom_components.window_controller_gateway.ws_gateway import (
            ws_gateway_wanted)
        from custom_components.window_controller_gateway.const import (
            CONF_WS_GATEWAY_ENABLED, CONF_WS_GATEWAY_TOKEN)
        w = ws_gateway_wanted(self._hass({
            CONF_WS_GATEWAY_ENABLED: True, CONF_WS_GATEWAY_TOKEN: ""}))
        assert w[1] == "", "空串=不认证是 D-1 合法形态，不得回退"

    def test_form_layer_charset_shared_with_runtime(self):
        """B-4 补面（v1.7.32 全量审计）：表单层字符集不得自留第二份字面量。

        运行时闸（ws_gateway.py）用 const.WS_TOKEN_CHARSET，而 config_flow
        的 options 校验曾内联同一串 64 字符——改常量则两层判据静默分叉，
        与 B-4「运行态防线」同威胁模型。此处双向判：常量必须在场 + 字面量
        不得回潮。
        """
        from custom_components.window_controller_gateway import const as _c
        cf = (Path(pkg.__file__).parent / "config_flow.py").read_text(encoding="utf-8")
        assert "WS_TOKEN_CHARSET" in cf, "表单层未引用 const.WS_TOKEN_CHARSET"
        stale = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        assert stale not in cf, (
            "config_flow.py 回潮了内联字面量字符集——改常量将只改到一半"
        )
        assert stale == "".join(ch for ch in stale if ch in _c.WS_TOKEN_CHARSET), (
            "const.WS_TOKEN_CHARSET 已变更，本守卫钉的旧字面量需同步复核"
        )


# ==================== C-2：add_device 条目存活前置门 ====================

class TestAddDeviceEntryGate:
    def test_retired_manager_rejected(self):
        from custom_components.window_controller_gateway.device_manager import (
            WindowControllerDeviceManager)
        hass = SimpleNamespace(
            data={DOMAIN: {}},
            config=SimpleNamespace(config_dir="."),
            config_entries=SimpleNamespace(
                async_get_entry=lambda eid: None,
                async_entries=lambda d: []),
        )
        entry = SimpleNamespace(entry_id="e-dead",
                                data={c_key(): "100129999900"})
        dm = WindowControllerDeviceManager(hass, entry)
        assert asyncio.run(dm.add_device("50022E010603", "迟到设备")) is None
        assert dm.devices == {}, "条目已消失仍写缓存=幽灵燃料"
        assert "50022E010603" not in hass.data[DOMAIN].get(
            DEVICE_TO_GATEWAY_MAPPING, {}), "映射更不得写（persist 复活源）"


def c_key():
    from custom_components.window_controller_gateway.const import (
        CONF_GATEWAY_SN)
    return CONF_GATEWAY_SN


# ==================== A-6：cleanup 收口派发任务 ====================

class TestDispatchTaskCleanup:
    def test_cleanup_cancels_inflight(self):
        from custom_components.window_controller_gateway.mqtt_handler \
            import _lifecycle as lc

        async def main():
            loop = asyncio.get_running_loop()
            self = SimpleNamespace(
                hass=SimpleNamespace(
                    loop=loop,
                    async_create_task=lambda c, **k: asyncio.ensure_future(c)),
                gateway_sn="GW", _closing=False, _dispatch_tasks=set(),
                pairing_timeout_handle=None, _bind_ops={}, _check_task=None,
                _reconnect_task=None, _unsub_rsp=None, _status_callbacks={},
            )
            import types as _t
            self._register_dispatch = _t.MethodType(
                lc._LifecycleMixin._register_dispatch, self)
            self._schedule_async_task = _t.MethodType(
                lc._LifecycleMixin._schedule_async_task, self)
            self.cleanup = _t.MethodType(lc._LifecycleMixin.cleanup, self)
            inflight = []

            async def slow():
                await asyncio.sleep(30)

            for _ in range(2):
                before = len(self._dispatch_tasks)
                self._schedule_async_task(slow())
                assert len(self._dispatch_tasks) == before + 1, \
                    "派发句柄必须登记（A-6 核心）"
                inflight.append(list(self._dispatch_tasks)[-1])
            await asyncio.sleep(0)  # 起跑
            await self.cleanup()
            assert all(t.cancelled() for t in inflight), \
                "cleanup 后在途派发任务不得存活到访问已清空状态"
            assert self._dispatch_tasks == set()
        asyncio.run(main())


# ==================== C-3 + D 前端钉 ====================

class TestConstAndWebuiPins:
    def test_pairing_suffix_single_source(self):
        import custom_components.window_controller_gateway.const as c
        assert c.ENTITY_PAIRING_BUTTON_SUFFIX == "_pairing", \
            "C-3：值订正为实态 _pairing（输出逐字不变，无实体迁移）"
        gsrc = (_PKG_DIR / "gateway.py").read_text(encoding="utf-8")
        assert "ENTITY_PAIRING_BUTTON_SUFFIX}" in gsrc, "构造点必须引常量"
        assert 'f"{gateway_sn}_pairing"' not in gsrc
        assert 'f"{gateway_sn}_online"' not in gsrc, "同族 online 一并收口"

    def test_gitee_desc_fix(self):
        js = (_WWW_DIR / "js" / "huijian.js").read_text(
            encoding="utf-8")
        assert "releases?per_page=100&direction=desc" in js, \
            "Gitee API 升序盲区回归钉：徽章必须显式 desc 取最新页"

    def test_mqtt_channel_domain_filter(self):
        js = (_WWW_DIR / "js" / "huijian.js").read_text(
            encoding="utf-8")
        assert "config/config_entries/entry?domain=mqtt" in js

    def test_dev_dot_unknown_states(self):
        js = (_WWW_DIR / "js" / "huijian.js").read_text(
            encoding="utf-8")
        css = (_WWW_DIR / "css" / "huijian.css").read_text(
            encoding="utf-8")
        assert ".dot-unknown" in css, "D-1：未知灰点样式在位"
        assert js.count("dot-unknown") >= 2, "初态+刷新态两处未知点"
        assert "findEntityState(dev, 'sensor', 'status', states)" in js, \
            "D-1：圆点真值源=status 传感器（上报驱动+时效）"
        assert "coverEntity.state === 'unavailable'" not in js, \
            "D-1 反钉：cover 钉死 available，unavailable 假判据不得回潮"

    def test_degraded_rebuild_loads_state(self):
        js = (_WWW_DIR / "js" / "huijian.js").read_text(
            encoding="utf-8")
        # v1.8.5（审计 G-4）：旧钉只断字面量 `loadDeviceState(dev, []);`——而那句
        # 是**同步**调用（注释宣称的"异步补拉"不实），光有它降级态永不自愈。
        # 判据升级为三件套：降级登记整建标记、恢复路径消费标记、成功整建清标记。
        assert "loadDeviceState(dev, []);" in js, \
            "D-2：/states 失败降级分支仍要回填既有元素"
        assert "PENDING_REBUILD[entryId] = true;" in js, \
            "G-4：降级渲染必须登记整建标记，否则假能力声明与滑块丢失永不自愈"
        assert "PENDING_REBUILD[entryId]" in js.split("async function updateGatewayDevices")[1], \
            "G-4：silentRefresh 必须消费整建标记（id 集合未变也要重建）"
        assert "delete PENDING_REBUILD[entryId];" in js, \
            "G-4：整建成功必须清标记，否则每轮都重建（性能回退）"

    def test_position_rendering_distinguishes_unreadable_from_unsupported(self):
        """G-4 第二条：\"读不到\"不得渲染成能力判词（旧实现两者同形）。"""
        js = (_WWW_DIR / "js" / "huijian.js").read_text(encoding="utf-8")
        assert "const statesEmpty = !states || states.length === 0;" in js, \
            "G-4：必须显式区分空 states（读不到）与 position_capable=false（不支持）"
        assert "读取中…" in js, "G-4：读不到时应给占位文案而非能力判词"
        # 反向臂：状态文本那半行必须带 posCapable 前置（否则无能力机型并排矛盾）
        idx = js.index("statusText += ' | 位置: '")
        head = js[max(0, idx - 200):idx]
        assert "if (posCapable &&" in head, \
            "G-4 回潮：『不支持百分比定位』＋『位置: N%』并排矛盾又回来了"
