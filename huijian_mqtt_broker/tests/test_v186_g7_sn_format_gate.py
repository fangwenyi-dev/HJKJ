"""v1.8.5 审计 G-7：入站设备 SN 格式闸（幽灵设备）专项钉。

【缺陷形态（台架实锤 `_goldtest/repro_g7_sn_zero.py`）】
002/003/005 三条入站路径原来的判空都排在 `str()` **之前**，于是固件异常回包
`sn=0` 归一成 `"0"` 后非空，被当成合法设备 SN 一路走到
`add_device` / `update_device_status`（后者对未知 SN 会自动添加），在设备表里
注册出**幽灵设备**。002 只拦住 JSON 数字 0、字符串 `"0"` 漏网；003/005 两形态
全漏。legacy 通道（`_protocol.py` 的 `device_discovery` / `device_status`）同型。

【修复形态】
共用谓词 `utils.is_valid_device_sn`（`^[a-zA-Z0-9]{10,}$`，与本仓三处既有同串
先例同口径：`gateway_discovery_proxy.py` 的 `SN_RE`、`__init__.py` 心跳耳、
`_protocol.py` 异网关分支），在五处入站点归一之后补闸。

【本钉桩清单（每条都是可变异的行为断言，不是源码字符串断言）】
 1. 谓词与既有唯一口径同串，且只判格式不判归属。
 2. 002：`sn ∈ {0,"0",None,"","00"}` 一律不入设备表；**002 必 ack 且恰一次**。
 3. 002：正常 10~12 位 SN 照旧入库（零误伤），含 JSON 数字形态归一。
 4. 003：`sn ∈ {0,"0",None,""}` 一律不入设备表，且**不产生 003 ack**
    （003 是 HA 主动下发命令的回复，协议上不该 ack）。
 5. 003：正常 SN 照旧入库。
 6. 005：`sn ∈ {0,"0",None,""}` 一律不入设备表，且**005 必 ack 且恰一次**
    （ack 由外层 `_handle_ctype_005` 的 finally 保证，闸不得破坏它）。
 7. 005：正常 SN 照旧入库并推状态。
 8. legacy `device_discovery` / `device_status`：同款拒收。
 9. 闸是**格式**闸不是长度闸：5 位内部标识（`device_manager` 合法用法）在
    入站报文里同样被拒——闸只用于入站报文，不用于内部标识。

【为什么钉行为而不是钉源码】
上一轮「判空写在 str() 之前」正是**看着有守卫、实际拦不住**的形态：源码里
`if not device_sn: continue` 一直都在。只有把 `0/"0"` 真喂进处理器、断言设备表
为空，才能区分「写了守卫」与「守卫有效」。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.utils as u
import custom_components.window_controller_gateway.mqtt_handler as mh_mod
from custom_components.window_controller_gateway import const as c
from custom_components.window_controller_gateway.mqtt_handler import (
    WindowControllerMQTTHandler,
)

HERE = Path(__file__).resolve().parent
PKG = HERE.parent / "custom_components" / "window_controller_gateway"

GW_SN = "100122501207"          # 12 位（网关 SN）
DEV_SN = "500500000001"         # 12 位（正常设备 SN）
LEGACY_DEV_SN = "50063420020A"  # 12 位（legacy 夹具，含字母）

# 幽灵设备的全部入站形态：JSON 数字 0 与字符串形态（"00" 是同族短形态）。
ZERO_FORMS = [0, "0", "00", None, ""]


class _DM:
    """设备管理器替身：桩面覆盖生产侧被调用的**全部**方法（不得比真实现窄）。"""

    def __init__(self):
        self.devices = {}
        self.added = []
        self.device_updates = []
        self.gateway_status = []
        self._next_number = 1
        self.entry = SimpleNamespace(options={})

    def get_device(self, sn):
        return self.devices.get(sn)

    def get_all_devices(self):
        return list(self.devices.values())

    def is_device_manually_removed(self, sn):
        return False

    def _notify_status_listeners(self, sn):
        pass

    def allocate_device_number(self):
        n = self._next_number
        self._next_number += 1
        return n

    async def add_device(self, sn, name, typ=None, force=False, is_manual_pairing=False):
        self.added.append(sn)
        self.devices[sn] = {"sn": sn, "name": name}
        return sn

    async def update_gateway_status(self, status):
        self.gateway_status.append(status)

    async def update_device_status(self, sn, status, attributes=None):
        # 生产侧 device_manager 的语义：SN 不存在则**自动添加**（幽灵设备入口）
        if sn not in self.devices:
            self.devices[sn] = {"sn": sn, "name": sn}
            self.added.append(sn)
        self.device_updates.append((sn, status, dict(attributes or {})))


class _Hass:
    def __init__(self, loop=None):
        self.data = {c.DOMAIN: {}}
        self.loop = loop
        self.config = SimpleNamespace(config_dir=".")

    def async_create_task(self, coro):
        if self.loop is not None and self.loop.is_running():
            return self.loop.create_task(coro)
        coro.close()
        return None

    def add_job(self, job, *args):
        return job(*args) if callable(job) else None


class _Publisher:
    def __init__(self):
        self.published = []

    async def __call__(self, hass, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload)))

    def by_ctype(self, ctype):
        return [p for _, p in self.published if p.get("ctype") == ctype]


def _mk(monkeypatch):
    pub = _Publisher()
    monkeypatch.setattr(mh_mod.mqtt, "async_publish", pub)
    dm = _DM()
    handler = WindowControllerMQTTHandler(
        _Hass(loop=asyncio.get_running_loop()), GW_SN, dm)
    handler.gateway_sn = GW_SN
    return handler, dm, pub


async def _settle(rounds: int = 8):
    """让 _schedule_async_task 挂起的派发链跑完（任务→副作用至少 2 跳）。"""
    for _ in range(rounds):
        await asyncio.sleep(0.01)


async def _capture_rsp_cb(handler, monkeypatch):
    """从 _subscribe_topics 抓出生产闭包（handle_gateway_response）。"""
    captured = {}

    async def fake_sub(hass, topic, cb, qos):
        captured["cb"] = cb
        return lambda: None

    monkeypatch.setattr(mh_mod.mqtt, "async_subscribe", fake_sub)
    await handler._subscribe_topics()
    return captured["cb"]


def _envelope_002(devices, msg_id=7):
    return ({"head": c.PROTOCOL_HEAD, "ctype": "002", "id": msg_id, "sn": GW_SN,
             "data": {"devices": devices}}, {"devices": devices})


def _envelope_003(dev_sn, msg_id=7, errcode=0):
    data = {"sn": dev_sn, "errcode": errcode, "devtype": "curtain_ctr"}
    return ({"head": c.PROTOCOL_HEAD, "ctype": "003", "id": msg_id, "sn": GW_SN,
             "data": data}, data)


def _envelope_005(dev_sn, msg_id=7):
    data = {"sn": dev_sn, "status": "open", "position": 50}
    return ({"head": c.PROTOCOL_HEAD, "ctype": "005", "id": msg_id, "sn": GW_SN,
             "data": data}, data)


# ==================== 0. 谓词口径 ====================

class TestPredicate:
    def test_same_literal_as_the_three_existing_sites(self):
        """判据必须与仓内既有唯一口径同串，不得成为第二套规则。"""
        assert u.DEVICE_SN_RE.pattern == r"^[a-zA-Z0-9]{10,}$"
        legacy = (PKG / "mqtt_handler" / "_protocol.py").read_text(encoding="utf-8")
        ear = (PKG / "__init__.py").read_text(encoding="utf-8")
        proxy = (PKG.parent.parent / "gateway_discovery_proxy.py").read_text(
            encoding="utf-8")
        for name, src in (("_protocol.py", legacy), ("__init__.py", ear),
                          ("gateway_discovery_proxy.py", proxy)):
            assert r"^[a-zA-Z0-9]{10,}$" in src, (
                f"{name} 的既有 SN 判据消失或改了口径——格式闸成了第二套规则")

    @pytest.mark.parametrize("bad", ZERO_FORMS)
    def test_zero_forms_rejected(self, bad):
        assert u.is_valid_device_sn(bad) is False, (
            f"{bad!r} 被判成合法设备 SN ⇒ 幽灵设备闸形同虚设")

    @pytest.mark.parametrize("good", [DEV_SN, LEGACY_DEV_SN, "500534380262",
                                      "E2EGW0000001", "aB3dE5fG7h9"])
    def test_legal_forms_accepted(self, good):
        assert u.is_valid_device_sn(good) is True

    def test_format_gate_is_not_a_length_gate(self):
        """5 位内部标识（device_manager 合法用法）在**入站**报文里被拒——
        闸的适用面是报文里的设备 SN，不是内部标识本身。"""
        assert u.is_valid_device_sn("50051") is False
        assert u.is_valid_device_sn("5005X") is False


# ==================== 1. 002（网关状态上报） ====================

class TestCtype002Gate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ZERO_FORMS)
    async def test_002_zero_forms_never_enter_device_table(self, monkeypatch, bad):
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_002([{"sn": bad, "model": "win", "vesion": "1.0"}])
        await handler._handle_ctype_002(payload, "002", data)
        await _settle()
        assert dm.added == [], (
            f"002 上报 sn={bad!r} 注册出了幽灵设备（台架实锤形态）")
        assert dm.device_updates == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ZERO_FORMS)
    async def test_002_ack_still_exactly_once(self, monkeypatch, bad):
        """002 必 ack（协议契约）：闸只丢设备、不得吃掉 ack。"""
        handler, _, pub = _mk(monkeypatch)
        payload, data = _envelope_002([{"sn": bad}], msg_id=11)
        await handler._handle_ctype_002(payload, "002", data)
        acks = pub.by_ctype("002")
        assert len(acks) == 1, "002 必须 ack 一次，否则网关无限重传"
        assert acks[0]["data"]["errcode"] == 0
        assert acks[0]["id"] == 11, "ack 的 id 必须回显请求 id"

    @pytest.mark.asyncio
    async def test_002_legal_sn_still_added(self, monkeypatch):
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_002(
            [{"sn": DEV_SN, "model": "win", "vesion": "1.0", "r_travel": "65"}])
        await handler._handle_ctype_002(payload, "002", data)
        await _settle()
        assert dm.added == [DEV_SN], "正常 SN 被闸误伤"
        assert dm.device_updates[-1][0] == DEV_SN

    @pytest.mark.asyncio
    async def test_002_numeric_legal_sn_normalized(self, monkeypatch):
        """JSON 数字形态的**合法** SN（固件常见）须照常入库。"""
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_002([{"sn": 500534380262, "model": "win",
                                        "vesion": "1.0"}])
        await handler._handle_ctype_002(payload, "002", data)
        await _settle()
        assert dm.added == ["500534380262"]

    @pytest.mark.asyncio
    async def test_002_mixed_batch_keeps_legal_and_drops_ghost(self, monkeypatch):
        """同帧混装：幽灵条目被丢，合法条目照常入库（闸是逐条 continue）。"""
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_002([
            {"sn": 0, "model": "win", "vesion": "1.0"},
            {"sn": DEV_SN, "model": "win", "vesion": "1.0"},
            {"sn": "0", "model": "win", "vesion": "1.0"},
        ])
        await handler._handle_ctype_002(payload, "002", data)
        await _settle()
        assert dm.added == [DEV_SN], f"混装帧结果异常: {dm.added!r}"


# ==================== 2. 003（绑定/解绑回复） ====================

class TestCtype003Gate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ZERO_FORMS)
    async def test_003_zero_forms_never_enter_device_table(self, monkeypatch, bad):
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_003(bad)
        await handler._handle_ctype_003(payload, "003", data)
        assert dm.added == [], f"003 回复 sn={bad!r} 注册出了幽灵设备"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ZERO_FORMS)
    async def test_003_never_acks(self, monkeypatch, bad):
        """003 是 HA 主动下发命令的回复，协议上不做 ack（闸不得引入 ack）。"""
        handler, _, pub = _mk(monkeypatch)
        payload, data = _envelope_003(bad, msg_id=12)
        await handler._handle_ctype_003(payload, "003", data)
        assert pub.published == [], "003 不应产生任何上行 ack"

    @pytest.mark.asyncio
    async def test_003_legal_sn_still_added(self, monkeypatch):
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_003(LEGACY_DEV_SN)
        await handler._handle_ctype_003(payload, "003", data)
        assert dm.added == [LEGACY_DEV_SN], "正常 SN 被闸误伤"

    @pytest.mark.asyncio
    async def test_003_manual_pairing_still_enters_removal_flow(self, monkeypatch):
        """手动配对确认（bind_op=="bind"）路径在合法 SN 下语义不变。"""
        handler, dm, _ = _mk(monkeypatch)
        handler._record_bind_op(21, "bind")
        payload, data = _envelope_003(DEV_SN, msg_id=21)
        await handler._handle_ctype_003(payload, "003", data)
        assert dm.added == [DEV_SN]

    @pytest.mark.asyncio
    async def test_003_zero_sn_takes_the_existing_no_sn_path(self, monkeypatch):
        """置 None 后自然落进「设备操作成功但未返回设备SN」既有无 SN 路径，
        不新增分支语义：errcode=0 且无 SN ⇒ 设备表不动、无异常。"""
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_003("0", errcode=0)
        await handler._handle_ctype_003(payload, "003", data)
        assert dm.added == []
        assert dm.devices == {}


# ==================== 3. 005（设备状态上报） ====================

class TestCtype005Gate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ZERO_FORMS)
    async def test_005_zero_forms_never_enter_device_table(self, monkeypatch, bad):
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_005(bad)
        await handler._handle_ctype_005(payload, "005", data)
        await _settle()
        assert dm.device_updates == [], f"005 上报 sn={bad!r} 更新/添加了幽灵设备"
        assert dm.added == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ZERO_FORMS)
    async def test_005_ack_still_exactly_once(self, monkeypatch, bad):
        """005 必 ack 由外层 finally 保证：闸 early-return 不得破坏它。"""
        handler, _, pub = _mk(monkeypatch)
        payload, data = _envelope_005(bad, msg_id=13)
        await handler._handle_ctype_005(payload, "005", data)
        acks = pub.by_ctype("005")
        assert len(acks) == 1, "005 必须 ack 一次，否则网关无限重传"
        assert acks[0]["id"] == 13

    @pytest.mark.asyncio
    async def test_005_legal_sn_still_updates(self, monkeypatch):
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_005(DEV_SN)
        await handler._handle_ctype_005(payload, "005", data)
        await _settle()
        assert [t[0] for t in dm.device_updates] == [DEV_SN], "正常 SN 被闸误伤"
        assert dm.device_updates[0][1] == "open"
        assert dm.device_updates[0][2]["position"] == 50

    @pytest.mark.asyncio
    async def test_005_numeric_legal_sn_normalized(self, monkeypatch):
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_005(50063420020)
        await handler._handle_ctype_005(payload, "005", data)
        await _settle()
        assert [t[0] for t in dm.device_updates] == ["50063420020"]


# ==================== 4. legacy 通道（无 head/ctype 的旧固件） ====================

class TestLegacyGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ZERO_FORMS)
    async def test_legacy_discovery_zero_forms_rejected(self, monkeypatch, bad):
        handler, dm, _ = _mk(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        msg = {"gateway_sn": GW_SN, "type": "device_discovery",
               "devices": [{"device_sn": bad, "device_name": "窗"}]}
        cb(SimpleNamespace(payload=json.dumps(msg).encode()))
        await _settle()
        assert dm.added == [], f"legacy 发现分支 sn={bad!r} 注册出了幽灵设备"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ZERO_FORMS)
    async def test_legacy_status_zero_forms_rejected(self, monkeypatch, bad):
        handler, dm, _ = _mk(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        msg = {"gateway_sn": GW_SN, "type": "device_status",
               "device_sn": bad, "status": "open"}
        cb(SimpleNamespace(payload=json.dumps(msg).encode()))
        await _settle()
        assert dm.device_updates == [], f"legacy 状态分支 sn={bad!r} 更新了幽灵设备"

    @pytest.mark.asyncio
    async def test_legacy_discovery_legal_sn_still_added(self, monkeypatch):
        handler, dm, _ = _mk(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        msg = {"gateway_sn": GW_SN, "type": "device_discovery",
               "devices": [{"device_sn": LEGACY_DEV_SN, "device_name": "窗"}]}
        cb(SimpleNamespace(payload=json.dumps(msg).encode()))
        await _settle()
        assert dm.added == [LEGACY_DEV_SN], "legacy 正常 SN 被闸误伤"

    @pytest.mark.asyncio
    async def test_legacy_status_legal_sn_still_updates(self, monkeypatch):
        handler, dm, _ = _mk(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        msg = {"gateway_sn": GW_SN, "type": "device_status",
               "device_sn": LEGACY_DEV_SN, "status": "open", "position": 30}
        cb(SimpleNamespace(payload=json.dumps(msg).encode()))
        await _settle()
        assert [t[0] for t in dm.device_updates] == [LEGACY_DEV_SN]

    @pytest.mark.asyncio
    async def test_legacy_numeric_legal_sn_normalized(self, monkeypatch):
        handler, dm, _ = _mk(monkeypatch)
        cb = await _capture_rsp_cb(handler, monkeypatch)
        msg = {"gateway_sn": GW_SN, "type": "device_discovery",
               "devices": [{"device_sn": 500534380262, "device_name": "窗"}]}
        cb(SimpleNamespace(payload=json.dumps(msg).encode()))
        await _settle()
        assert dm.added == ["500534380262"]


# ==================== 5. 闸不得宽到误伤网关自身 ====================

class TestGateScope:
    @pytest.mark.asyncio
    async def test_002_gateway_own_sn_still_filtered_by_1001_rule(self, monkeypatch):
        """网关自身 SN（1001 前缀）本就该被 002 过滤，与格式闸互不影响。"""
        handler, dm, _ = _mk(monkeypatch)
        payload, data = _envelope_002([{"sn": "100122501207", "model": "win",
                                        "vesion": "1.0"}])
        await handler._handle_ctype_002(payload, "002", data)
        await _settle()
        assert dm.added == []

    def test_gate_is_applied_after_normalization_at_every_inbound_site(self):
        """五处入站点的静态形态钉：闸必须排在 str() 归一**之后**（这正是
        G-7 缺陷的根因——旧守卫排在归一之前）。"""
        ctypes_src = (PKG / "mqtt_handler" / "_ctypes.py").read_text(encoding="utf-8")
        proto_src = (PKG / "mqtt_handler" / "_protocol.py").read_text(encoding="utf-8")
        assert ctypes_src.count("is_valid_device_sn(") >= 3, (
            "ctype 层三处入站（002/003/005）未全部接闸")
        assert proto_src.count("is_valid_device_sn(") >= 2, (
            "legacy 两处入站未全部接闸")
        # 002：str() 归一在闸之前
        seg = ctypes_src.split('_LOGGER.warning("002 设备 SN 类型非法，跳过: %r"', 1)[1]
        seg = seg.split('if not is_valid_device_sn(device_sn):', 1)
        assert len(seg) == 2, "002 未接格式闸"
        between = seg[0]
        assert "device_sn = str(device_sn)" in between, (
            "002 格式闸排在 str() 归一之前 ⇒ 数字 0 会绕过闸（G-7 原形）")
