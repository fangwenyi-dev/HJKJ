"""v1.7.18 第 7 轮全量审计修复钉桩（dsh-review-loop 5 路审计 + 父级独立核验）。

编号=最终报告 BUG-1~23。回归时禁止改测试迁就实现——若确需变更形态，
先确认原缺陷仍被防住（参考 test_v1712_audit.py 同款约定）。

功能钉桩：BUG-1(setup 订阅失败拒载/巡检重试)、BUG-2(003 数值 SN)、
BUG-3(unignore 服务)、BUG-5(bootstrap 禁用条目)、BUG-6(WS 竞态 pop/重试)、
BUG-7(保留端口回退)、BUG-9(remove_entry 清持久忽略)、BUG-11(门禁终态细分)、
BUG-14(_norm_cmd_id bool)、BUG-16(WS 脏数值拒绝)。
静态钉桩：BUG-8(cap 顺序)、BUG-12/13/20/21/22/23 形态锚。
"""
import asyncio
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]          # huijian_mqtt_broker/
PKG = ROOT / "custom_components" / "window_controller_gateway"
MW = PKG / "mqtt_handler"

import custom_components.window_controller_gateway as pkg_init  # noqa: E402
import custom_components.window_controller_gateway.mqtt_handler as mh_mod  # noqa: E402
import custom_components.window_controller_gateway.mqtt_handler._lifecycle as lc_mod  # noqa: E402
import custom_components.window_controller_gateway.ws_gateway as wg  # noqa: E402
from custom_components.window_controller_gateway.mqtt_handler import (  # noqa: E402
    WindowControllerMQTTHandler,
)
from custom_components.window_controller_gateway import services as svc_mod  # noqa: E402
from custom_components.window_controller_gateway import mqtt_bootstrap as mb_mod  # noqa: E402
from custom_components.window_controller_gateway.const import (  # noqa: E402
    DOMAIN,
    CONF_GATEWAY_SN,
    GLOBAL_IGNORED_GATEWAYS,
    WS_RESERVED_PORTS,
    DEFAULT_WS_GATEWAY_TOKEN,
)

WS_GATEWAY_DATA_KEY = wg.WS_GATEWAY_DATA_KEY

GW_SN = "100122501203"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class _Hass:
    def __init__(self):
        self.data = {DOMAIN: {}}
        self.config = SimpleNamespace(config_dir=".")

    def async_create_task(self, coro):
        coro.close()
        return None


class _MockDM:
    def __init__(self, devices=None):
        self.devices = devices or {}
        self.added = []

    def get_device(self, sn):
        return self.devices.get(sn)

    def is_device_manually_removed(self, sn):
        return False

    def _notify_status_listeners(self, sn):
        pass

    def allocate_device_number(self):
        return 1

    async def add_device(self, sn, name, typ=None, force=False,
                         is_manual_pairing=False):
        self.added.append(sn)

    async def update_device_status(self, sn, status, attributes=None):
        pass

    async def update_gateway_status(self, status):
        pass


def _mk(monkeypatch, dm=None):
    async def _pub(hass, topic, payload, qos=0, retain=False):
        pass

    monkeypatch.setattr(mh_mod.mqtt, "async_publish", _pub)
    handler = WindowControllerMQTTHandler(_Hass(), GW_SN, dm or _MockDM())
    return handler


# ==================== BUG-1：订阅失败拒载 + 巡检重试链 ====================

class TestSetupSubscribeFailure:

    @pytest.mark.asyncio
    async def test_setup_returns_false_when_subscribe_fails(self, monkeypatch):
        """setup 丢弃 _subscribe_topics 返回值 = 条目"已加载"但入站永久丢单。
        修复形态：False → 调用方 __init__ 抛 ConfigEntryNotReady（HA 原生重试）。"""
        handler = _mk(monkeypatch)
        monkeypatch.setattr(lc_mod, "is_mqtt_loaded", lambda h: True)

        async def sub_fail():
            return False

        handler._subscribe_topics = sub_fail
        assert await handler.setup() is False, \
            "订阅失败 setup 必须返回 False（回潮即静默失聪面复发）"
        assert handler._check_task is None, "失败路径不得再挂巡检任务"

    @pytest.mark.asyncio
    async def test_setup_success_starts_watchdog(self, monkeypatch):
        handler = _mk(monkeypatch)
        monkeypatch.setattr(lc_mod, "is_mqtt_loaded", lambda h: True)

        async def sub_ok():
            return True

        handler._subscribe_topics = sub_ok
        assert await handler.setup() is True
        assert handler._check_task is not None
        handler._check_task.cancel()
        try:
            await handler._check_task
        except asyncio.CancelledError:
            pass


# ==================== BUG-2：003 嵌套 SN 类型归一 ====================

class TestBindReplySnNormalization:

    @pytest.mark.asyncio
    async def test_numeric_sn_normalized_to_str(self, monkeypatch):
        dm = _MockDM()
        handler = _mk(monkeypatch, dm)
        payload = {"head": "$SH", "ctype": "003", "id": 7, "sn": GW_SN}
        data = {"errcode": 0, "sn": 500534380262, "bind": 1}  # JSON 数字形态
        await handler._handle_ctype_003(payload, "003", data)
        assert dm.added == ["500534380262"], \
            "数值 SN 须归一成 str 入库（与 002/005 B-5 同型）"

    @pytest.mark.asyncio
    async def test_bool_or_dict_sn_rejected_not_crash(self, monkeypatch):
        for bad in (True, {"a": 1}):
            dm = _MockDM()
            handler = _mk(monkeypatch, dm)
            payload = {"head": "$SH", "ctype": "003", "id": 8, "sn": GW_SN}
            await handler._handle_ctype_003(payload, "003",
                                            {"errcode": 0, "sn": bad, "bind": 1})
            assert dm.added == [], f"{bad!r} 形态不得入库"

    def test_legacy_branch_has_gates_and_online(self):
        """BUG-15：legacy 分支补 auto_discovery 门禁与在线记账（静态形态）。"""
        src = _read(MW / "_protocol.py")
        legacy = src.split("处理原有格式的响应", 1)[1].split("except json.JSONDecodeError", 1)[0]
        assert "self.last_gateway_report_time = time.monotonic()" in legacy, \
            "legacy 上报必须刷新在线口径（恒离线与消息事实矛盾）"
        assert "_auto_discovery_enabled()" in legacy, "legacy 通道须过自动发现门禁"
        assert "device_sn = str(device_sn)" in legacy, "legacy 状态分支 SN 须归一"


# ==================== BUG-14：_norm_cmd_id bool 显式旁路 ====================

class TestNormCmdId:

    def test_bool_returns_none(self):
        # True 原样返回会命中 _bind_ops 键 1（dict 里 True==1 同哈希）
        assert WindowControllerMQTTHandler._norm_cmd_id(True) is None
        assert WindowControllerMQTTHandler._norm_cmd_id(False) is None

    def test_numeric_forms_unchanged(self):
        assert WindowControllerMQTTHandler._norm_cmd_id(42) == 42
        assert WindowControllerMQTTHandler._norm_cmd_id("42") == 42
        assert WindowControllerMQTTHandler._norm_cmd_id(42.0) == 42
        assert WindowControllerMQTTHandler._norm_cmd_id("0") == 0


# ==================== BUG-3：unignore_gateway 服务（忽略自救出口） ====================

class TestUnignoreService:

    def _hass(self):
        reg = {}
        hass = SimpleNamespace(
            data={DOMAIN: {}},
            config=SimpleNamespace(config_dir="."),
            services=SimpleNamespace(
                async_register=lambda d, n, f, schema=None: reg.__setitem__(n, f)),
            async_create_task=lambda coro: coro.close(),
        )
        return hass, reg

    def test_registered(self):
        hass, reg = self._hass()
        assert svc_mod.register_services(hass) is True
        assert "unignore_gateway" in reg, \
            "async_unignore_gateway 必须有生产调用方（BUG-3：误点忽略无自救）"

    @pytest.mark.asyncio
    async def test_call_clears_persistent_ignore(self):
        hass, reg = self._hass()
        svc_mod.register_services(hass)
        ignored = {"100122501186"}
        hass.data[DOMAIN][GLOBAL_IGNORED_GATEWAYS] = ignored
        hass.data[DOMAIN]["discovery"] = {
            "ignored_gateways": ignored,
            "announced_gateways": {"100122501186"},
            "last_discovery_time": {"100122501186": 1.0},
        }
        await reg["unignore_gateway"](
            SimpleNamespace(data={"gateway_sn": "100122501186"}))
        assert ignored == set()
        assert hass.data[DOMAIN]["discovery"]["announced_gateways"] == set()

    @pytest.mark.asyncio
    async def test_empty_sn_raises(self):
        from custom_components.window_controller_gateway.services import (
            ServiceValidationError,
        )
        hass, _ = self._hass()
        with pytest.raises(ServiceValidationError):
            await svc_mod.handle_unignore_gateway(
                hass, SimpleNamespace(data={"gateway_sn": "  "}))


# ==================== BUG-5：bootstrap 不接管禁用条目 ====================

class _BootHass:
    def __init__(self, entries, cfg_dir):
        self.config = SimpleNamespace(
            config_dir=str(cfg_dir),
            path=lambda name: str(Path(cfg_dir) / name),
        )
        self.data = {}
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: (
                list(entries) if domain == "mqtt" else []))

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class TestBootstrapDisabled:

    async def _marker(self, tmp_path):
        marker = tmp_path / mb_mod.BOOTSTRAP_FILENAME
        marker.write_text(json.dumps({
            "broker": "127.0.0.1", "port": 2022,
            "username": "ha_mqtt", "password": "x",
        }), encoding="utf-8")
        return marker

    @pytest.mark.asyncio
    async def test_all_disabled_returns_false_keeps_marker(self, tmp_path, monkeypatch):
        marker = await self._marker(tmp_path)
        disabled = SimpleNamespace(entry_id="m1", disabled_by="user",
                                   data={"broker": "10.0.0.9", "port": 1883},
                                   source="user")
        hass = _BootHass([disabled], tmp_path)
        # 禁用的 hassio 判定路径不应触达
        assert await mb_mod.ensure_mqtt_connection(hass) is False
        assert marker.exists(), "全禁用形态不得删标记（删了=自愈凭据蒸发）"

    @pytest.mark.asyncio
    async def test_disabled_plus_enabled_adopts_enabled(self, tmp_path):
        marker = await self._marker(tmp_path)
        dead = SimpleNamespace(entry_id="m1", disabled_by="user",
                               data={"broker": "x", "port": 1}, source="user")
        alive = SimpleNamespace(entry_id="m2", disabled_by=None,
                                data={"broker": "127.0.0.1", "port": 2022,
                                      "username": "ha_mqtt", "password": "x"},
                                source="user")
        hass = _BootHass([dead, alive], tmp_path)
        assert await mb_mod.ensure_mqtt_connection(hass) is not False
        assert not marker.exists(), "启用条目已匹配内置 Broker → 正常消费标记"

    def test_header_describes_takeover(self):
        src = _read(PKG / "mqtt_bootstrap.py")
        assert "强制接管" in src.split('"""', 2)[1], "头注释须描述真实接管行为（BUG-12）"
        assert src.count("if not broker:") == 1, "入口统一熔断外的不可达死块已删"


# ==================== BUG-6/7：WS 生命周期竞态与保留端口 ====================

class _CE:
    def __init__(self, entries):
        self._e = entries

    def async_entries(self, domain=None):
        return self._e


class TestWsLifecycle:

    @pytest.mark.asyncio
    async def test_start_failure_retries_once(self, monkeypatch):
        attempts = []

        async def boom(self):
            attempts.append(1)
            raise OSError("port in use")

        slept = []

        async def fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(wg.WsGatewayServer, "async_start", boom)
        monkeypatch.setattr(wg.asyncio, "sleep", fake_sleep)
        hass = SimpleNamespace(
            data={DOMAIN: {}},
            config_entries=_CE([SimpleNamespace(entry_id="e0", options={})]))
        await wg.async_ensure_ws_gateway(hass)
        assert attempts == [1, 1], "一次撞口即放弃=『已保存开启』冻结成永不监听（BUG-6）"
        assert slept == [1.0]
        assert WS_GATEWAY_DATA_KEY not in hass.data[DOMAIN]

    @pytest.mark.asyncio
    async def test_wanted_none_pop_is_identity_guarded(self):
        """wanted-None 分支无条件 pop 会删掉 stop 让出点窗口内并发 ensure
        成功方的注册（F2 对称残余，BUG-6）。"""
        class _Cur:
            async def async_stop(self):
                holder[WS_GATEWAY_DATA_KEY] = "NEW-SERVER"

        holder = {WS_GATEWAY_DATA_KEY: _Cur()}
        hass = SimpleNamespace(data={DOMAIN: holder}, config_entries=_CE([]))
        await wg.async_ensure_ws_gateway(hass)
        assert holder.get(WS_GATEWAY_DATA_KEY) == "NEW-SERVER", \
            "注册表被误删=孤儿监听器持旧令牌关不掉"

    def test_reserved_port_runtime_fallback(self):
        from custom_components.window_controller_gateway.const import (
            CONF_WS_GATEWAY_ENABLED,
            CONF_WS_GATEWAY_PORT,
        )
        # BUG-7：非表单路径写入 2022/10998/8123/1883 须回退默认口
        for p in sorted(WS_RESERVED_PORTS):
            hass = SimpleNamespace(
                config_entries=_CE([SimpleNamespace(entry_id="e0", options={
                    CONF_WS_GATEWAY_ENABLED: True, CONF_WS_GATEWAY_PORT: p})]))
            assert wg.ws_gateway_wanted(hass) == (9001, DEFAULT_WS_GATEWAY_TOKEN), \
                f"保留端口 {p} 未回退"

    @pytest.mark.asyncio
    async def test_control_rejects_nonfinite_numeric(self):
        # BUG-16：inf/nan/1e+308 str() 成设备不可解析线值还回 ok=true 假成功
        s = wg.WsGatewayServer.__new__(wg.WsGatewayServer)
        s._device_gateway = lambda sn: None
        # v1.7.33：零条目广播已在审计批改为**如实** ok:false（空集假成功面），
        # 本用例只验线值白名单（BUG-16），故给一个可发布条目保持原断言语义；
        # 空集新语义由 tests/test_v1733_honest_ack.py 单独钉桩。
        class _H:
            async def send_ws_raw_004(self, dev, attr, val):
                return True

        s._entries_data = lambda: [("G", {"mqtt_handler": _H()})]
        # v1.7.65 起本用例的属性名从占位的 "position" 换成真属性 "w_travel"：
        # 域表落地后 position 不再是本协议的出站属性，用它会因"未知属性"被拒——
        # 那样断言照样绿，但验的就不再是这道**格式**闸了（判据要对着它声称的东西）。
        for bad in (math.inf, -math.inf, math.nan, 1e308):
            out = await s._cmd_control({"gwSn": "G", "devSn": "D",
                                        "attribute": "w_travel", "value": bad})
            assert out == {"type": "control_ack", "ok": False, "msg": "invalid value",
                           "attribute": "w_travel"}, f"{bad!r} 未被拒绝"
        out = await s._cmd_control({"gwSn": "G", "devSn": "D",
                                    "attribute": "w_travel", "value": 50})
        assert out["ok"] is True, "正常 int 线值不受影响"
        # ── v1.7.52 改判：取消 str 豁免 ─────────────────────────────
        # 本批原先在这里断言 `attribute:"state", value:"open"` 必须放行（"str 值维持
        # F6 透传语义"）。核过真实协议面后判定那条是**虚构例子钉住了真实漏洞**：
        #   · 004 的入站属性全集（_ctypes.py）= voltage / r_travel / rwp_wind_lock_mode
        #     / rwp_winact_speed / rwp_winact_strength，出站 w_travel —— 全部数值型；
        #   · 合法值域（_commands.py 的 send_ws_raw_004 docstring 原文）=
        #     "w_travel 的 100/0/101/200/0-100、rwp_wind_lock_mode 0/1 等"，也是全数值；
        #   · `state` 根本不是本协议的属性名，"open" 更不是任何合法线值。
        # 而豁免放过的正是 'NaN'：小程序云通道送的是 String(value)，NaN → 'NaN'
        # 恰好从豁免缝穿到固件并拿到 ok:true 假成功。所以现在一律过十进制格式闸。
        out = await s._cmd_control({"gwSn": "G", "devSn": "D",
                                    "attribute": "w_travel", "value": "open"})
        assert out["ok"] is False and out["msg"] == "invalid value", \
            "非十进制线值必须拒（'NaN'/'open' 这类从 str 豁免缝里穿过去）"
        # v1.7.65：属性名占位从 "state" 换成真属性 "w_travel"——域表落地后 state 属
        # "未知属性"，会被两道闸各拒一次，那条断言就不再专验格式闸了。
        # 未知属性本身由 tests/test_control_attr_domain.py 逐条钉。
        out = await s._cmd_control({"gwSn": "G", "devSn": "D",
                                    "attribute": "w_travel", "value": "NaN"})
        assert out["ok"] is False and out["msg"] == "invalid value", \
            "'NaN' 不得再靠 str 豁免透传 004 并回 ok:true"
        # 合法字符串线值（小程序实际下发的就是 String() 形态）不受影响
        for good in ("0", "100", "101", "200", "50"):
            out = await s._cmd_control({"gwSn": "G", "devSn": "D",
                                        "attribute": "w_travel", "value": good})
            assert out["ok"] is True, f"合法十进制串 {good!r} 被误拒"
        # 本批把 "-1" 从"合法十进制串"改判成**越界**：-1 是上报侧的"未知"哨兵
        # （device_ws_view 用它表示没收到过 r_travel），从来不是任何下行命令的合法值；
        # 原样透传给设备只会得到"窗不动却回 ok:true"。改判由域表负责，此处显式钉住，
        # 免得下个版本把它当成回归。
        out = await s._cmd_control({"gwSn": "G", "devSn": "D",
                                    "attribute": "w_travel", "value": "-1"})
        assert out["ok"] is False and out["msg"] == "invalid value", \
            "-1 已改判为越界（上报侧哨兵当命令下发就是假成功）"


# ==================== BUG-9：remove_entry 清持久忽略 ====================

class TestRemoveEntryIgnoreClear:

    @pytest.mark.asyncio
    async def test_clears_global_ignored_without_discovery_dict(self, monkeypatch):
        """"discovery" 键不存在（发现平台初始化失败被吞）时，旧实现把
        discard 落在一次性临时 dict 上——持久忽略纹丝不动。"""
        async def _nosave(h):
            return None

        monkeypatch.setattr(pkg_init, "save_persistent_data", _nosave)
        ignored = {"100121501186"}
        hass = _Hass()
        hass.data[DOMAIN][GLOBAL_IGNORED_GATEWAYS] = ignored
        entry = SimpleNamespace(entry_id="e1",
                                data={CONF_GATEWAY_SN: "100121501186"})
        await pkg_init.async_remove_entry(hass, entry)
        assert ignored == set(), "删条目必须清持久忽略（删后应可再被发现）"


# ==================== BUG-8：容量闸在存在性检查之后 ====================

class TestCapacityGateOrder:

    def test_gate_after_existence(self):
        body = _read(PKG / "device_manager.py").split("async def add_device", 1)[1]
        body = body.split("\n    async def ")[0]
        i_existed = body.index("device_existed = device_sn in self.devices")
        i_cap = body.index("len(self.devices) >= MAX_DEVICES_PER_GATEWAY")
        assert i_existed < i_cap, "容量闸先于存在性检查=满载网关拒绝既有设备自愈（BUG-8）"
        assert "not device_existed and not force" in body


# ==================== BUG-10/13：形态锚 ====================

class TestFormPins:

    def test_form_refill_prefers_user_input(self):
        src = _read(PKG / "config_flow.py")
        assert "default_sn = _ui_sn or gateway_sn_from_context" in src, \
            "被拒后回填须优先用户输入（context 恒优先=表单吞输入，BUG-10）"

    def test_no_percent_d_left(self):
        assert "错误码: %d" not in _read(MW / "_ctypes.py"), "B-11 同族 %d 回潮（BUG-13）"


# ==================== BUG-20/21/22：run.sh 与 config.yaml 形态锚 ====================

class TestRunShPins:

    def test_octal_defense_implemented(self):
        sh = _read(ROOT / "run.sh")
        assert "LAST_TS=$((10#$LAST_TS))" in sh, \
            "『0 开头一律归 0』注释承诺的实现（旧版只有 case 挡不住 078，BUG-20）"

    def test_whitelist_before_restore_and_no_raw_echo(self):
        sh = _read(ROOT / "run.sh")
        i_case = sh.index("拒绝启动")
        i_restore = sh.index('FIRMWARE_MQTT_USER="huijian"')
        assert i_case < i_restore, \
            "白名单校验须先于凭据恢复块（旧顺序下校验不可达，BUG-21）"
        assert "连字符）: '${USERNAME}'" not in sh, "报错不得回显未校验原值（日志注入面）"

    def test_bridge_initial_log_accurate(self):
        sh = _read(ROOT / "run.sh")
        assert "自动写入桥接（broker 重启/首次启动时加载生效）" in sh, \
            "初启路径无重启可言，措辞已纠偏（桥日志簇）"

    def test_config_yaml_proxy_wording(self):
        cfg = _read(ROOT / "config.yaml")
        assert "绝不自动建条目" not in cfg, \
            "与实现矛盾的授权边界描述（BUG-22）；准确口径见下行新文案"
        assert "等待条目" in cfg


# ==================== BUG-12（版本字段一致性由既有动态锚覆盖） =========
