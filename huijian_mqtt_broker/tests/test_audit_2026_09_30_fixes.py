# -*- coding: utf-8 -*-
"""审计 2026-09-30 修复批的行为钉（docs/bug-audit-2026-09-30.md）。

三条纪律（本仓反复踩过的坑，逐条落到下面的用例）：
  1. 行为优先于文本：能真跑的就调真的函数体。`assert 某串 in src` 这类结构钉
     骗过上一批 P0（const 重赋值被内层 catch 静默吞掉），只在"运行时不可达"
     或"必须防形态回潮"时使用，且都写明为什么。
  2. 单向不变量必配反向：每条「加了闸」的钉配一条「闸不得误伤正常路径」的半条，
     否则「什么都不做」也能让钉通过。
  3. 桩不得窄于真实现：下面自带的假件都按真签名接 kwargs、返回真返回类型。

B-7 / G-1 / G-2（面板）的行为钉在 test_v1755_silent_refresh_behavior.py：
场景 C + 自变异核验 2（node 真跑，摘掉守卫必须变红）。
"""
import ast
import asyncio
import inspect
import os
import shutil
import tempfile
import json
import re
import subprocess
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "window_controller_gateway"
REPO = ROOT.parent
RUN_SH = (ROOT / "run.sh").read_text(encoding="utf-8")

import custom_components.window_controller_gateway as pkg
from custom_components.window_controller_gateway import api
from custom_components.window_controller_gateway import config_flow as cf_mod
from custom_components.window_controller_gateway import const as c
from custom_components.window_controller_gateway import discovery
from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway import services
from custom_components.window_controller_gateway import ws_gateway as wsg
from custom_components.window_controller_gateway.const import DOMAIN
from custom_components.window_controller_gateway.device_manager import (
    WindowControllerDeviceManager,
)
from custom_components.window_controller_gateway.mqtt_handler import (
    WindowControllerMQTTHandler,
)
from custom_components.window_controller_gateway.number import (
    WindowControllerSpeedNumber,
)


class _QuietLogger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _task_shim(coro, **kw):
    """hass.async_create_task 的等宽替身（真签名带 name= 等 kwargs）。

    桩若只接一个位置参数，调用点带上 name= 就会 TypeError——协程对象在**调用侧**
    已经创建、却没被交接出去，于是留下一条 "coroutine was never awaited" 噪声，
    而真正的失败被 except 吞掉。本仓纪律：桩不得窄于真实现。
    done() 也不许恒 True：生产侧通篇 `if not task.done(): task.cancel()`，
    假件下不进去就等于把"卸载会不会真取消任务"整类判据作废（conftest 同条）。
    """
    if asyncio.iscoroutine(coro):
        coro.close()
    return _ShimTask()


class _ShimTask:
    def __init__(self):
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        return True

    def cancelled(self):
        return self._cancelled

    def done(self):
        return self._cancelled

    def __await__(self):
        if False:
            yield
        return None


def _code_lines(text):
    """整行注释与空行剔除后的文本。

    文本型判据一律先过这道：本仓"钉语法不钉裸标识符"的教训——行首加个 #
    就能让"某串在场"的断言继续绿（对抗复核 2026-09-30 对 F-1/F-2/C-7 实测）。
    """
    return "\n".join(l for l in text.splitlines()
                     if l.strip() and not l.strip().startswith("#"))


def _has_directive(block, directive):
    """块内**生效行**里存在该指令（行首即指令，允许行尾注释）。

    比裸子串严：`# proxy_ssl_verify on;`（整行注释）与
    `proxy_ssl_verify off; # proxy_ssl_verify on;`（真指令+尾注释）都不算数。
    """
    pat = re.compile(r"^\s*" + re.escape(directive) + r"\s*(?:#.*)?$", re.M)
    return bool(pat.search(block))


def _runsh_function(name):
    """抽出 run.sh 顶层函数的真实现，套上 set -e/pipefail 供行为钉真跑。

    为什么连 set -e 一起带：F-2 复核缺口 B 只在 set -e 下才成立
    （`X=$(awk …)` 失败 → 整个看门狗子 shell 被静默杀死），不还原这个
    开关就等于没测那条缺陷。路径类杠杆（NGINX_PROC_*）由用例注入。
    """
    src = (ROOT / "run.sh").read_text(encoding="utf-8")
    m = re.search(r"^%s\(\) \{\n(.*?)\n\}" % re.escape(name), src, re.M | re.S)
    assert m, "run.sh 里找不到函数 %s（结构变了，本钉需同步）" % name
    return "set -e\nset -o pipefail\n%s() {\n%s\n}\n" % (name, m.group(1))


def _bash_run(script, env_extra=None):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("本用例需要 bash（Windows 走 Git Bash）")
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([bash, "-c", script], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, env=env)


# ═══════════════════════════════════ A-1 WS 握手 ═══════════════════════════════════
# hmac.compare_digest 对含非 ASCII 的 str 直接抛 TypeError；aiohttp 两条解析器臂
# 都把非 ASCII 头部字节原样交付（本次实测），而握手调用点在 try 之外。


def test_a1_handshake_never_raises_on_arbitrary_bytes():
    for junk in ("é", "中文", "\udcff", " ok", "a,b", "\u00e9good"):
        assert wsg.handshake_token_ok(junk, "goodtoken") is False, \
            "非 ASCII 候选应判不匹配，实得异常或 True：%r" % junk


def test_a1_ascii_candidate_after_non_ascii_still_matches():
    """反向半条：正确令牌排在非 ASCII 候选之后仍要放行。

    没有这半条，「整头非 ASCII 一律拒」也能过上一条——而那是缺陷的第二半
    （合法客户端被一起拒），微信 connectSocket 恒带子协议，不能再犯。
    """
    assert wsg.handshake_token_ok("é, goodtoken", "goodtoken") is True
    assert wsg.handshake_token_ok("中文,goodtoken", "goodtoken") is True
    assert wsg.handshake_token_ok("goodtoken", "goodtoken") is True


def test_a1_handshake_still_uses_constant_time_compare():
    """改法不许把 compare_digest 换成 in/==（时序侧信道是 v1.7.33 立的判据）。"""
    for fn in (wsg.handshake_token_ok, wsg.validate_new_token):
        src = inspect.getsource(fn)
        assert "hmac.compare_digest" in src, "%s 退回短路比较＝时序预言机回潮" % fn.__name__
        assert not re.search(r"(cand|token|old_token)\s*==\s*(token|cand|current_token)", src), \
            "%s 里出现了裸 == 令牌比较（时序侧信道）" % fn.__name__


def test_a1_non_ascii_server_token_denies_instead_of_crashing():
    """storage 被手改成非 ASCII 令牌：如实全员 401，而不是每个握手打 500。"""
    assert wsg.handshake_token_ok("goodtoken", "déf") is False


def test_a1_empty_token_still_means_no_auth():
    """反向半条：空令牌 = 不认证放行（D-1 定案），不许被新闸改成拒绝。"""
    assert wsg.handshake_token_ok("é", "") is True
    assert wsg.handshake_token_ok(None, "") is True


def test_a1_validate_old_token_non_ascii_returns_mismatch():
    """set_token 的 oldToken 不许抛（抛了小程序收不到任何 set_token_ack）。"""
    err = wsg.validate_new_token("validtoken8", "中文", "goodtoken")
    assert err == wsg._MSG_OLD_MISMATCH, \
        "非 ASCII oldToken 应判 oldToken 不匹配，实得 %r" % err


def test_a1_validate_correct_old_token_still_passes():
    """反向半条：正常 oldToken 必须照常通过校验链。"""
    assert wsg.validate_new_token("validtoken8", "goodtoken", "goodtoken") is None


# ═══════════════════════════════ A-2 云通道 params ═══════════════════════════════


def _hub_client(tmp_path):
    client = hc.HubClient([], config_dir=str(tmp_path), base="https://hub.invalid")
    client._logger = _QuietLogger()
    return client


@pytest.mark.parametrize("bad_params", [[], "x", 123, [1, 2], [{"a": 1}], True])
def test_a2_non_dict_params_returns_invalid_without_raising(tmp_path, bad_params):
    """真值非 dict 的 params 必须回 invalid_params，而不是把整条长连打掉。"""
    client = _hub_client(tmp_path)
    touched = []

    async def control(sn, attr, value):
        touched.append((sn, attr, value))
        return True

    client.control_fn = control
    res = asyncio.run(client._handle_cmd(
        {"action": "control", "sn": "A1", "params": bad_params}))
    assert res == {"ok": False, "err": "invalid_params"}, res
    assert touched == [], "非法 params 不得触到控制面"


def test_a2_well_formed_params_still_executes(tmp_path):
    """反向半条：合法 params 照常执行（否则「一律 invalid」也能过上一条）。"""
    client = _hub_client(tmp_path)
    touched = []

    async def control(sn, attr, value):
        touched.append((sn, attr, value))
        return True

    client.control_fn = control
    res = asyncio.run(client._handle_cmd({
        "action": "control", "sn": "A1",
        "params": {"attribute": "position", "value": "100"}}))
    assert res["ok"] is True, res
    assert touched == [("A1", "position", "100")]


def test_a2_receive_loop_wraps_handler_so_one_poison_frame_cannot_drop_link():
    """判据分叉反向核验：接收循环里对 _handle_cmd 的调用要么有闸、要么有 try。"""
    src = inspect.getsource(hc.HubClient._session_once)
    assert "await self._handle_cmd(data)" in src
    gated = ("isinstance(msg.get(\"params\")" in inspect.getsource(hc.HubClient._handle_cmd)
             or "isinstance(params, dict)" in inspect.getsource(hc.HubClient._handle_cmd))
    assert gated, "云通道 params 必须有类型闸（LAN 通道同款入口一直有）"


# ═══════════════════════════════ A-3 / B-4 api body ═══════════════════════════════


class _Req:
    """aiohttp Request 的等宽替身：只暴露被测代码用到的两条面。"""

    def __init__(self, body, hass):
        self.app = {"hass": hass}
        self._body = body

    async def json(self, loads=None, content_type="application/json", **kw):
        return self._body


class _MemberClient:
    def __init__(self):
        self.calls = []

    async def remove_member(self, mid):
        self.calls.append(("remove", mid))
        return True

    async def set_member_alias(self, mid, name):
        self.calls.append(("rename", mid, name))
        return True

    async def list_members(self):
        self.calls.append(("list",))
        return True

    def status_view(self):
        return {"members": [], "membersCount": 0, "membersMax": 8,
                "membersSupported": True, "ownerMasked": None,
                "lastError": None, "lastOpError": None}


@pytest.fixture()
def _patched_client(monkeypatch):
    client = _MemberClient()
    hass = types.SimpleNamespace(data={DOMAIN: {c.HUB_DATA_KEY: client}})
    monkeypatch.setattr(api, "_hub_client", lambda h: client)
    return client, hass


@pytest.mark.parametrize("view_attr", [
    "WindowGatewayHubMemberRemoveView", "WindowGatewayHubMemberRenameView"])
@pytest.mark.parametrize("body", [[1], "abc", 5, None, {}, True, {"mid": "x"}])
def test_a3_views_never_500_on_arbitrary_body(_patched_client, view_attr, body):
    """body 是任意 JSON 形态都不得抛 AttributeError（抛了 = 500 且无 JSON 体）。"""
    client, hass = _patched_client
    view = getattr(api, view_attr)()
    out = {}
    view.json = lambda payload, **kw: out.setdefault("p", payload)
    asyncio.run(view.post(_Req(body, hass)))
    assert "p" in out, "视图没回 JSON"
    assert out["p"]["enabled"] is True


def test_a3_non_dict_body_sends_no_cloud_call(_patched_client):
    """非 dict body ⇒ mid 为空 ⇒ 不发 remove 调用（空 mid 会被 hub 判 unknown_member）。"""
    client, hass = _patched_client
    view = api.WindowGatewayHubMemberRemoveView()
    out = {}
    view.json = lambda payload, **kw: out.setdefault("p", payload)
    asyncio.run(view.post(_Req([1], hass)))
    assert ("remove", "x") not in client.calls
    assert not any(k == "remove" for k, *_ in client.calls), client.calls
    assert out["p"]["removedOk"] is False


def test_a3_valid_body_still_calls_through(_patched_client):
    """反向半条：合法 mid 必须照常发出调用（闸不许把正常路径一起拦掉）。"""
    client, hass = _patched_client
    view = api.WindowGatewayHubMemberRemoveView()
    view.json = lambda payload, **kw: None
    asyncio.run(view.post(_Req({"mid": "a" * 12}, hass)))
    assert ("remove", "a" * 12) in client.calls, client.calls


# ═══════════════════════════════ A-4 容器型命令 id ═══════════════════════════════


def _mk_handler(tmp_path):
    dm = types.SimpleNamespace(devices={}, entry=types.SimpleNamespace(options={}))
    hass = types.SimpleNamespace(
        data={DOMAIN: {}}, loop=None,
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        async_create_task=_task_shim, async_add_job=lambda job, *a: None)
    handler = WindowControllerMQTTHandler(hass, "1001GW", dm)
    handler.gateway_sn = "1001GW"
    return handler


@pytest.mark.parametrize("raw", [[1], {"a": 1}, [1, 2], {1, 2}, bytearray(b"1")])
def test_a4_unhashable_id_maps_to_none(raw):
    """不可哈希 id 归 None（pop(None) 是 miss），不许把 TypeError 留给调用方。"""
    out = _mk_handler(Path(""))._norm_cmd_id(raw)
    assert out is None, "%r 归一成 %r——docstring 承诺未识别类型落 miss/旁路分支" % (raw, out)
    hash(out)


def test_a4_known_id_forms_unchanged():
    """反向半条：合法/已知形态的归一结果一律不得被新闸改掉。"""
    h = _mk_handler(Path(""))
    assert h._norm_cmd_id(42) == 42
    assert h._norm_cmd_id("42") == 42
    assert h._norm_cmd_id(42.0) == 42
    assert h._norm_cmd_id(True) is None
    assert h._norm_cmd_id(None) is None
    assert h._norm_cmd_id("abc") == "abc"


def test_a4_bind_ops_pop_cannot_receive_unhashable(tmp_path):
    """端到端半条：_bind_ops 的键写入与 pop 必须同走归一化后的可哈希值。"""
    h = _mk_handler(tmp_path)
    h._bind_ops = {5: ("bind", "50051")}
    for raw in ([1], {"a": 1}, 5, "5", None, True):
        key = h._norm_cmd_id(raw)
        rec = h._bind_ops.pop(key, None)          # 这一行就是缺陷现场
        assert rec is None or isinstance(rec, tuple)


# ═══════════════════════════════ A-5 设备名类型 ═══════════════════════════════


def _mgr(tmp_path, entry_id="e1", gw="1001GW", sn=None):
    entry = types.SimpleNamespace(
        entry_id=entry_id, data={c.CONF_GATEWAY_SN: sn or gw}, options={})
    store = {DOMAIN: {}}
    hass = types.SimpleNamespace(
        data=store, loop=None,
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        async_create_task=_task_shim, async_add_job=lambda job, *a: None,
        config_entries=types.SimpleNamespace(
            async_get_entry=lambda eid: entry, async_entries=lambda domain: [entry]))
    return WindowControllerDeviceManager(hass, entry)


@pytest.mark.parametrize("bad_name", [None, 123, ["x"], 1.5])
def test_a5_add_device_survives_non_str_name(tmp_path, bad_name):
    """消费侧兜底：名字非 str 不得在 .lower() 处 AttributeError（异常只落通用日志）。"""
    mgr = _mgr(tmp_path)
    res = asyncio.run(mgr.add_device("50051", bad_name))
    assert res is None or isinstance(res, str)


def test_a5_legacy_branch_guards_name_like_sn():
    """生产侧结构钉（运行时不可达的形态，防回潮）：legacy 分支对 name 也要有闸。

    为什么用结构判据：legacy device_discovery 在 handle_gateway_response 的闭包
    里，真跑要整条 MQTT 订阅链；本仓对 SN 的同类守卫当初也是靠结构钉立住的。
    对抗复核二轮升级：旧版按"1600 字符窗口里出现 isinstance(device_name, str)"
    判——把守卫换成一行同字样的注释照样绿。现在按 AST 判**结构位**：
    `device_name = …get(ATTR_DEVICE_NAME)` 这个赋值必须处在某个
    `isinstance(device_name, str)` 守卫的 if 体内。
    """
    tree = ast.parse((PKG / "mqtt_handler" / "_protocol.py").read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and ast.unparse(node.targets[0]) == "device_name":
            target = node
    assert target is not None, "legacy 分支的 device_name 赋值形态变了，本钉需重写"
    guarded = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If)
                and "isinstance(device_name, str)" in ast.unparse(node.test)):
            continue
        if any(sub is target for sub in ast.walk(node)):
            guarded = True
    assert guarded, \
        "device_name 的取值不在 isinstance(device_name, str) 守卫之内（A-5 回潮："\
        "键存在值为 null 时 .get 返回 None，.lower() 即 AttributeError）"


# ═══════════════════════════════ A-6 非有限设定值 ═══════════════════════════════


def _speed_entity():
    ent = object.__new__(WindowControllerSpeedNumber)
    ent.device_sn = "50051"
    ent._entity_label = "速度"
    ent._param_key = "speed"
    ent._attr_native_value = None
    return ent


@pytest.mark.parametrize("junk", [float("inf"), float("-inf"), float("nan")])
def test_a6_non_finite_setpoint_does_not_raise(junk):
    """int(inf) 抛 OverflowError，旧的 (ValueError, TypeError) 接不住 ⇒ 一坏俱坏。"""
    ent = _speed_entity()
    ent._read_setpoint = lambda: junk
    ent._update_state()                       # 不许抛
    assert ent._attr_native_value is None, "非法设定值不得写进 native_value"


def test_a6_finite_setpoint_still_applies():
    """反向半条：正常整数设定值必须照常落值。"""
    ent = _speed_entity()
    ent._read_setpoint = lambda: 60
    ent._update_state()
    assert ent._attr_native_value == 60.0


def test_a6_catch_tuple_contains_overflow():
    """判据来自本仓定案（ws_gateway._as_int 的 A-HIGH2 同条理由）。"""
    src = inspect.getsource(WindowControllerSpeedNumber.__mro__[1]._update_state) \
        if hasattr(WindowControllerSpeedNumber, "_update_state") else ""
    assert "OverflowError" in src, "number 的兜底没捕 OverflowError（A-6 回潮）"


# ═══════════════════════════════ B-1 force 迁移 last_update ═══════════════════════════════


def _force_branch():
    tree = ast.parse((PKG / "device_manager.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "add_device":
            for sub in ast.walk(node):
                if isinstance(sub, ast.If) and ast.unparse(sub.test) == "force":
                    for assign in ast.walk(sub):
                        if (isinstance(assign, ast.Assign)
                                and ast.unparse(assign.targets[0]).endswith(
                                    "self.devices[device_sn]")):
                            return assign
    return None


def test_b1_force_rebuild_dict_carries_last_update():
    """AST 判据（不是文本扫描）：force 分支重建的字典必须有 last_update。"""
    branch = _force_branch()
    assert branch is not None, "找不到 force 分支的设备字典重建（结构变了，本钉要重写）"
    assert isinstance(branch.value, ast.Dict), "重建不再是字典字面量，本钉要重写"
    keys = {}
    for k, v in zip(branch.value.keys, branch.value.values):
        keys[ast.literal_eval(ast.unparse(k))] = ast.unparse(v)
    assert "last_update" in keys, \
        "force 分支又丢了 last_update（DM-F1 同族第四次），实得键：%s" % sorted(keys)
    assert "time.time()" in keys["last_update"], "last_update 必须是当前时刻"


def test_b1_consumers_treat_missing_last_update_as_fresh():
    """前提核验：消费端仍是 None=新鲜 语义——若这条变了，B-1 的影响面要重估。"""
    src = (PKG / "cover.py").read_text(encoding="utf-8")
    assert re.search(r"if\s+_lu\s+and", src), \
        "cover 的时效判据形态变了（`_lu and ...` 是 None=新鲜 的成因），本钉需重写"


def _battery_sensor(device):
    from custom_components.window_controller_gateway.sensor import (
        WindowControllerBatterySensor,
    )
    ent = object.__new__(WindowControllerBatterySensor)
    ent.device_sn = "50051"
    ent._attr_native_value = None
    ent.device_manager = types.SimpleNamespace(get_device=lambda sn: device)
    return ent


def test_b1_stale_voltage_sensor_goes_unknown_even_with_empty_attributes():
    """行为钉（复核缺口）：时效闸必须排在"取不到 voltage 就早退"**之前**。

    force 迁移会把 attributes 清空——旧顺序下属性一空就直接 return，
    `_attr_native_value` 永不更新，网关彻底失联也冻结在迁移前的电压值。
    实测把顺序改回去，全量 1290 条照绿（零守护），所以按行为判。
    """
    stale = time.time() - (c.SENSOR_TIMEOUT_MINUTES + 1) * 60
    ent = _battery_sensor({"attributes": {}, "last_update": stale})
    ent._attr_native_value = 12.5
    ent._update_state()
    assert ent._attr_native_value is None, \
        "陈旧判据被'attributes 为空'的早退挡住（B-1 复合后果：永久冻结）"


def test_b1_fresh_voltage_sensor_still_updates():
    """反向半条：新鲜数据照常更新（闸不许做成永远 unknown）。"""
    ent = _battery_sensor({"attributes": {"voltage": 12.5},
                           "last_update": time.time()})
    ent._update_state()
    assert ent._attr_native_value == 12.5


# ═══════════════════════════════ B-3 迁移类型校验 ═══════════════════════════════


def _two_managers(tmp_path):
    new_entry = types.SimpleNamespace(
        entry_id="e-new", data={c.CONF_GATEWAY_SN: "1001NEW"}, options={})
    old_entry = types.SimpleNamespace(
        entry_id="e-old", data={c.CONF_GATEWAY_SN: "1001OLD"}, options={})

    def _hass(ent):
        store = {DOMAIN: {}}
        return store, types.SimpleNamespace(
            data=store, loop=None,
            config=types.SimpleNamespace(config_dir=str(tmp_path)),
            async_create_task=_task_shim, async_add_job=lambda job, *a: None,
            config_entries=types.SimpleNamespace(
                async_get_entry=lambda eid: ent, async_entries=lambda domain: [ent]))

    store_new, hass_new = _hass(new_entry)
    new_mgr = WindowControllerDeviceManager(hass_new, new_entry)
    old_mgr = WindowControllerDeviceManager(_hass(old_entry)[1], old_entry)
    # 把两个 manager 放进同一个 DOMAIN 视图（真 HA 里就是这个形状）
    store_new[DOMAIN]["e-new"] = {"device_manager": new_mgr}
    store_new[DOMAIN]["e-old"] = {"device_manager": old_mgr}
    new_mgr.hass.data = store_new
    return new_mgr, old_mgr


def test_b3_type_check_is_live_across_managers(tmp_path):
    """待迁设备在旧网关缓存里也必须被校验到（此前 self.devices 恒 None ⇒ 死分支）。"""
    new_mgr, old_mgr = _two_managers(tmp_path)
    old_mgr.devices["50051"] = {"sn": "50051", "name": "非开窗器",
                                "type": "some_other_type"}
    assert new_mgr.devices.get("50051") is None, "前置：新 manager 缓存里没有它"
    rec = new_mgr._find_device_record("50051")
    assert rec is not None and rec["type"] == "some_other_type"
    res = asyncio.run(new_mgr._validate_migration(["50051"], "1001NEW"))
    assert res["valid"] is False, "类型不兼容要真的拦下来"
    assert any("仅支持开窗器" in e for e in res["errors"]), res


def test_b3_window_opener_device_not_blocked(tmp_path):
    """反向半条：开窗器类型的待迁设备不得被新逻辑误拦。"""
    new_mgr, old_mgr = _two_managers(tmp_path)
    old_mgr.devices["50051"] = {"sn": "50051", "name": "开窗器",
                                "type": c.DEVICE_TYPE_WINDOW_OPENER}
    res = asyncio.run(new_mgr._validate_migration(["50051"], "1001NEW"))
    assert all("类型不兼容" not in e for e in res["errors"]), res


def test_b3_absent_record_is_permissive_not_fatal(tmp_path):
    """反向半条 2：查不到记录（旧条目已卸载）必须放行，不能新拦掉本来能成的迁移。"""
    new_mgr, _old = _two_managers(tmp_path)
    res = asyncio.run(new_mgr._validate_migration(["50099"], "1001NEW"))
    assert all("类型不兼容" not in e for e in res["errors"]), res


# ═══════════════════════════════ B-5 取消忽略 ═══════════════════════════════


def _disc_hass(ignored):
    return types.SimpleNamespace(
        data={DOMAIN: {c.GLOBAL_IGNORED_GATEWAYS: set(ignored)}},
        async_create_task=_task_shim)


def test_b5_unignore_without_discovery_key_still_works():
    """忽略记录可只来自持久化加载（无 discovery 键）——那时取消也必须真生效。"""
    hass = _disc_hass({"gw001"})
    assert "discovery" not in hass.data[DOMAIN], "前置：没有 discovery 键"
    assert asyncio.run(discovery.async_unignore_gateway(hass, "GW001")) is True
    assert "gw001" not in hass.data[DOMAIN][c.GLOBAL_IGNORED_GATEWAYS]


def test_b5_unignore_absent_record_returns_false():
    """反向半条：本就没被忽略的 SN 必须回 False，上层据此不能说「已取消」。"""
    hass = _disc_hass(set())
    assert asyncio.run(discovery.async_unignore_gateway(hass, "GW999")) is False


def test_b5_service_consumes_the_result():
    """服务层不得在 no-op 时打「已取消忽略」（假成功痕迹）。"""
    src = inspect.getsource(services.handle_unignore_gateway)
    assert re.search(r"^\s+unignored\s*=\s*await async_unignore_gateway", src, re.M), \
        "取消忽略的返回值必须被接住（B-5）"
    assert "if unignored" in src, "必须按结果分支留痕"


def test_b5_service_reports_both_outcomes(monkeypatch):
    """真跑服务：命中/未命中两条分支各留一条正确的日志。"""
    async def fake_unignore(hass, sn):
        return sn == "GW001"

    monkeypatch.setattr(discovery, "async_unignore_gateway", fake_unignore)
    call = types.SimpleNamespace(data={"gateway_sn": "GW001"})
    hass = types.SimpleNamespace(
        logger=None, config=types.SimpleNamespace(config_dir="."))
    caplog_msgs = []

    class _L:
        def info(self, msg, *a):
            caplog_msgs.append(("info", msg % a if a else msg))

        def warning(self, msg, *a):
            caplog_msgs.append(("warning", msg % a if a else msg))

        def error(self, msg, *a):
            caplog_msgs.append(("error", msg % a if a else msg))

    monkeypatch.setattr(services, "_LOGGER", _L())
    asyncio.run(services.handle_unignore_gateway(hass, call))
    assert any(lvl == "info" and "已取消忽略" in m for lvl, m in caplog_msgs), caplog_msgs

    caplog_msgs.clear()
    call2 = types.SimpleNamespace(data={"gateway_sn": "GW404"})
    asyncio.run(services.handle_unignore_gateway(hass, call2))
    assert not any("已取消忽略" in m for _lvl, m in caplog_msgs), \
        "未命中时仍报「已取消忽略」＝假成功回潮"


# ═══════════════════════════════ B-6 文案可见性 ═══════════════════════════════


def _div_span(html, opening_idx):
    """从元素起始处返回该 div 的完整文本区间。"""
    start = html.rindex("<", 0, opening_idx)
    depth, i, end = 0, start, None
    while i < len(html):
        if html.startswith("<div", i):
            depth += 1
            i += 4
            continue
        if html.startswith("</div>", i):
            depth -= 1
            if depth == 0:
                end = i + len("</div>")
                break
        i += 1
    assert end, "div 不配平，本钉需重写"
    return html[start:end]


def test_b6_member_expiry_hint_is_not_inside_the_hidden_wrapper():
    """hubMemberExp 不能是 #hubMemberQr 的后代——那块会被 hidden 整块藏掉。"""
    html = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    wrapper = _div_span(html, html.index('id="hubMemberQr"'))
    assert "hubMemberExp" not in wrapper, \
        "指路文案又回到 hidden 容器里（B-6 回潮）：用户看不到「已过期，点添加家人换新码」"
    assert "hubMemberExp" in html, "文案元素本体不许被删掉"


def test_b6_owner_block_still_the_reference_shape():
    """对照物核验：绑定码那块（hubCodeExp 在 #hubQr 之外）是本钉的依据，不得被改坏。"""
    html = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    bind_wrapper = _div_span(html, html.index('id="hubQr"'))
    assert "hubCodeExp" not in bind_wrapper
    assert "hubCodeExp" in html


def test_b6_js_still_writes_the_hint():
    """JS 侧仍要写 hubMemberExp（只删文案不算修复，那是掩盖）。"""
    js = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
    assert "hubMemberExp" in js


# ═══════════════════════════════ C-1 服务重注册 ═══════════════════════════════


def _setup_hass(tmp_path, entry, store):
    from homeassistant.core import FakeServices

    async def _true(*a, **k):
        return True

    return types.SimpleNamespace(
        data=store, services=FakeServices(), loop=None, is_stopping=False,
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        async_create_task=_task_shim, async_add_job=lambda job, *a: None,
        bus=types.SimpleNamespace(async_listen_once=lambda *a, **k: (lambda: None)),
        config_entries=types.SimpleNamespace(
            async_entries=lambda domain: [entry],
            async_get_entry=lambda eid: entry,
            async_forward_entry_setups=_true,
            async_forward_entry_unload=_true))


def test_c1_services_removed_then_re_registered_on_next_entry(tmp_path, monkeypatch):
    """删完最后一个条目 → 注销；再加一个条目 → 必须回来（此前要重启 HA）。"""
    from homeassistant.core import FakeServices

    async def _true(*a, **k):
        return True

    entry = types.SimpleNamespace(
        entry_id="e1", data={}, options={}, state="loaded", disabled_by=None,
        async_on_unload=lambda cb: None, add_update_listener=lambda cb: None)
    hass = types.SimpleNamespace(
        data={DOMAIN: {}}, services=FakeServices(), loop=None, is_stopping=False,
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        async_create_task=_task_shim, async_add_job=lambda job, *a: None,
        bus=types.SimpleNamespace(async_listen_once=lambda *a, **k: (lambda: None)),
        config_entries=types.SimpleNamespace(
            async_entries=lambda domain: [entry],
            async_get_entry=lambda eid: entry,
            async_forward_entry_setups=_true,
            async_forward_entry_unload=_true))
    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", _true)
    monkeypatch.setattr(pkg, "async_ensure_hub_client", _true)

    assert asyncio.run(pkg.async_setup_entry(hass, entry)) is True
    assert hass.services.has_service(DOMAIN, "start_pairing"), \
        "条目 setup 必须把域级服务补回来（C-1 的落点）"

    asyncio.run(pkg.async_remove_entry(hass, entry))
    assert not hass.services.has_service(DOMAIN, "start_pairing"), \
        "最后一个条目删除后应注销服务"

    entry2 = types.SimpleNamespace(
        entry_id="e2", data={}, options={}, state="loaded", disabled_by=None,
        async_on_unload=lambda cb: None, add_update_listener=lambda cb: None)
    hass.config_entries.async_entries = lambda domain: [entry2]
    hass.config_entries.async_get_entry = lambda eid: entry2
    assert asyncio.run(pkg.async_setup_entry(hass, entry2)) is True
    for name in ("start_pairing", "check_gateway_status", "rename_device",
                 "set_position", "refresh_devices", "transfer_device",
                 "unignore_gateway"):
        assert hass.services.has_service(DOMAIN, name), \
            "重新加条目后服务 %s 没回来（C-1 回潮：要重启 HA 才有）" % name


def test_h3_setup_clears_stale_runtime_keys_before_merge(tmp_path, monkeypatch):
    """行为钉（H-3）：卸载失败残留的状态类键不得被 previous.update() 继承。

    旧守卫（test_v1733_guards）只断言源码里出现那行字面量——对抗复核实锤：
    把它改成 pop 一个 throwaway 副本（字面量仍在场、行为归零）全量照绿。
    这里真跑一次 setup：预置三只残留键，断言旧 _bg_tasks 没被继承
    （残留被继承 ⇒ 旧任务列表持续累积、已无引用可取消）。
    """
    async def _true(*a, **k):
        return True

    entry = types.SimpleNamespace(
        entry_id="e1", data={}, options={}, state="loaded", disabled_by=None,
        async_on_unload=lambda cb: None, add_update_listener=lambda cb: None)
    stale_bg = ["stale-task"]
    stale_unsub = [lambda: None]
    store = {DOMAIN: {"e1": {"_platforms_forwarded": True,
                             "_bg_tasks": stale_bg,
                             "unsub_listeners": stale_unsub}}}
    hass = _setup_hass(tmp_path, entry, store)
    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", _true)
    monkeypatch.setattr(pkg, "async_ensure_hub_client", _true)

    assert asyncio.run(pkg.async_setup_entry(hass, entry)) is True
    data = store[DOMAIN]["e1"]
    assert data.get("_bg_tasks") is not stale_bg, \
        "旧任务列表被继承（H-3 形态：pop 了 throwaway 副本，行为归零）"
    assert "stale-task" not in (data.get("_bg_tasks") or []), \
        "残留任务混进新的 _bg_tasks（已无引用可取消，只累积）"


# ═══════════════════════════════ C-2 hub 停机闩锁 ═══════════════════════════════


def test_c2_ensure_refuses_to_respawn_after_stop(tmp_path, monkeypatch):
    """STOP 后条目逐个 unload 也会调 ensure；没有闩锁就把刚停的长连再拉起来。"""
    made = []
    monkeypatch.setattr(pkg, "HubClient", lambda *a, **k: made.append(object()))

    def _hass():
        store = {DOMAIN: {}}
        # _hub_managers 的口径：只认带 _setup_complete 的条目运行时
        store[DOMAIN]["e1"] = {"_setup_complete": True,
                               "device_manager": types.SimpleNamespace(
                                   gateway_sn="1001GW", devices={})}
        return store, types.SimpleNamespace(
            data=store, config=types.SimpleNamespace(config_dir=str(tmp_path)),
            config_entries=types.SimpleNamespace(
                async_entries=lambda d: [], async_get_entry=lambda e: None))

    store, hass = _hass()
    asyncio.run(pkg.async_ensure_hub_client(hass))
    assert made, "前置：正常态必须拉起一次"

    made.clear()
    store[DOMAIN][c.HUB_STOPPED_KEY] = True
    asyncio.run(pkg.async_ensure_hub_client(hass))
    assert not made, "停机闩锁失效：关机过程中又新建了一个 HubClient（C-2）"


def test_c2_stop_listener_sets_the_latch(tmp_path):
    """闩锁必须由 STOP 回调置位，否则上一条钉只是死码。"""
    captured = {}
    bus = types.SimpleNamespace(
        async_listen_once=lambda evt, cb: (captured.setdefault("cb", cb), (lambda: None))[1])
    store = {DOMAIN: {}}
    hass = types.SimpleNamespace(
        data=store, bus=bus, config=types.SimpleNamespace(config_dir=str(tmp_path)),
        config_entries=types.SimpleNamespace(async_entries=lambda d: []))
    pkg._register_hub_stop_listener(hass, store[DOMAIN])
    assert "cb" in captured, "STOP 监听没注册成功"
    asyncio.run(captured["cb"](None))
    assert store[DOMAIN].get(c.HUB_STOPPED_KEY) is True, "STOP 回调未置闩锁"


def test_c2_no_stop_listener_registration_after_latch(tmp_path):
    """复核补口行为钉：STOP 已派发（闩锁置位 / hass 正在停机）后不得再注册。

    交错：ensure 越过闩锁检查、卡在 async_start 的让出点里被 STOP 抢先，
    走到注册点时事件已派发过——再挂一只 listen_once 永不触发、句柄无人摘。
    """
    registered = []
    bus = types.SimpleNamespace(
        async_listen_once=lambda evt, cb: (registered.append(cb), (lambda: None))[1])

    def _hass(store, stopping):
        return types.SimpleNamespace(
            data=store, bus=bus, is_stopping=stopping,
            config=types.SimpleNamespace(config_dir=str(tmp_path)),
            config_entries=types.SimpleNamespace(async_entries=lambda d: []))

    latched = {DOMAIN: {c.HUB_STOPPED_KEY: True}}
    pkg._register_hub_stop_listener(_hass(latched, False), latched[DOMAIN])
    assert registered == [], "闩锁已置仍注册 STOP 监听（C-2 复核补口）"
    stopping = {DOMAIN: {}}
    pkg._register_hub_stop_listener(_hass(stopping, True), stopping[DOMAIN])
    assert registered == [], "hass 正在停机仍注册 STOP 监听（C-2 复核补口）"
    # 反向半条：正常态必须照常注册（闸不许做成永不注册）
    normal = {DOMAIN: {}}
    pkg._register_hub_stop_listener(_hass(normal, False), normal[DOMAIN])
    assert registered, "正常态没注册 STOP 监听（反向半条）"


# ═══════════════════════════════ C-4 once-listener ═══════════════════════════════


class _StopClient:
    def __init__(self):
        self.stopped = False

    async def async_stop(self):
        self.stopped = True


def _stop_hass(client, unsub_spy):
    store = {DOMAIN: {c.HUB_DATA_KEY: client, c.HUB_STOP_LISTENER_KEY: unsub_spy}}
    return types.SimpleNamespace(data=store)


def test_c4_stop_path_does_not_unsub_the_consumed_listener(tmp_path):
    """async_listen_once 在派发时已被总线摘除，再 unsub() 让 HA 打 ERROR（F-A 同族）。"""
    calls = []
    client = _StopClient()
    asyncio.run(pkg.async_stop_hub_client(_stop_hass(client, lambda: calls.append("x")),
                                          from_stop=True))
    assert calls == [], "STOP 派发路径又去摘已消费的 once 监听器"
    assert client.stopped and client is not None


def test_c4_unload_path_still_unsubs():
    """反向半条：条目 unload 路径里监听器还活着，该摘必须摘。"""
    calls = []
    client = _StopClient()
    asyncio.run(pkg.async_stop_hub_client(_stop_hass(client, lambda: calls.append("x"))))
    assert calls == ["x"], "非 STOP 路径没摘监听器——句柄不存不摘的老账"


def test_c4_listener_handle_is_popped_either_way():
    """两条路径都必须把句柄从 domain_data 摘掉（防二次执行）。"""
    for from_stop in (True, False):
        client = _StopClient()
        hass = _stop_hass(client, lambda: None)
        asyncio.run(pkg.async_stop_hub_client(hass, from_stop=from_stop))
        assert c.HUB_STOP_LISTENER_KEY not in hass.data[DOMAIN]


# ═══════════════════════════════ C-5 卸载顺序 ═══════════════════════════════


def test_c5_bg_tasks_cancelled_before_heartbeat_unsub():
    """心跳退订必须晚于 _bg_tasks 取消，否则卸载期补装的耳朵永久无人退订。

    用 AST 语句行号比顺序（不是字符串 index——那种钉会被注释满足）。
    """
    tree = ast.parse((PKG / "__init__.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_unload_entry")
    lines = {"bg": None, "hb": None}
    for sub in ast.walk(fn):
        # data.get("_bg_tasks") 里那是 ast.Constant（字符串字面量），不是 Attribute
        if isinstance(sub, ast.Constant) and sub.value == "_bg_tasks":
            lines["bg"] = min(x for x in (lines["bg"], sub.lineno) if x)
        if isinstance(sub, ast.Constant) and sub.value == "_unsub_heartbeat":
            lines["hb"] = min(x for x in (lines["hb"], sub.lineno) if x)
    assert lines["bg"] and lines["hb"], "两处清理之一不在 async_unload_entry 里，本钉需重写"
    assert lines["bg"] < lines["hb"], \
        "心跳退订（行 %s）又跑回后台任务取消（行 %s）之前了（C-5 回潮）" % (
            lines["hb"], lines["bg"])


@pytest.mark.asyncio
async def test_c5_heartbeat_unsub_is_actually_called(tmp_path, monkeypatch):
    """顺序对了还得**真的调用**：金牌复测实测 `if heartbeat_unsub:` → `if False:`
    时 1286 条全绿——上面那条 AST 顺序钉只看两处的行号，看不出闸被焊死。

    场景按 C-5 的原缺陷铺：卸载 step 0 的 await 是让出点，等待态条目的"MQTT 未就绪→
    后台武装"任务在这期间跑完并把句柄写进 data。清理排在任务收尾**之后**才读得到它，
    读到之后必须调用并置 None（防二次调用）。
    """
    armed = []
    runtime = {}

    async def _arming():
        await asyncio.sleep(0)                # 真让出：模拟武装任务在卸载期完成
        runtime["_unsub_heartbeat"] = lambda: armed.append("late")

    task = asyncio.ensure_future(_arming())
    runtime["_bg_tasks"] = [task]

    async def slow_save(_hass):
        await asyncio.sleep(0.05)             # step 0 的让出点（真实现是存盘）

    async def noop_ensure(_hass):
        return None

    monkeypatch.setattr(pkg, "save_persistent_data", slow_save)
    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", noop_ensure)
    monkeypatch.setattr(pkg, "async_ensure_hub_client", noop_ensure)

    hass = types.SimpleNamespace(
        data={DOMAIN: {"E1": runtime}},
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        config_entries=types.SimpleNamespace(async_entries=lambda d: []))
    entry = types.SimpleNamespace(entry_id="E1", data={})
    assert await pkg.async_unload_entry(hass, entry) is True

    assert armed == ["late"], \
        "补装的心跳耳朵没人退订：卸载后仍挂在已卸载条目上代答 001（C-5 行为面）"
    assert runtime.get("_unsub_heartbeat") is None, "句柄不清 ⇒ 同一只耳朵可能被摘两次"


# ═══════════════════════════════ C-6 取消信号 ═══════════════════════════════


def _cleanup_node():
    tree = ast.parse((PKG / "mqtt_handler" / "_lifecycle.py").read_text(encoding="utf-8"))
    cleanup = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "cleanup"]
    assert len(cleanup) == 1, "cleanup 有 %d 个同名定义，本钉会验到死码" % len(cleanup)
    return cleanup[0]


def test_c6_no_merged_cancelled_except_in_cleanup():
    """禁形态回潮（**两种**形态）：合并 `except (CancelledError, Exception)`，
    或把 CancelledError 单列一个吞异常的 except——两者都会吞掉本协程自己的取消。

    hub_client.py:493-497 的注释点名过这条反例，本仓 cleanup 曾三处一致、一处漏。
    对抗复核实测：只查合并写法拦不住"拆成两个 except"，故按 AST 判"cleanup 里
    任何捕 CancelledError 的 except 都不许存在"。
    """
    src = (PKG / "mqtt_handler" / "_lifecycle.py").read_text(encoding="utf-8")
    assert not re.search(r"except\s*\(\s*asyncio\.CancelledError\s*,\s*Exception\s*\)", src), \
        "合并 except 回潮（C-6）"
    for node in ast.walk(_cleanup_node()):
        if isinstance(node, ast.ExceptHandler) and node.type is not None \
                and "CancelledError" in ast.unparse(node.type):
            raise AssertionError(
                "cleanup 里出现捕 CancelledError 的 except（吞自身取消，C-6 反例）："
                "第 %s 行 %s" % (node.lineno, ast.unparse(node.type)))


def test_c6_child_cancellation_absorbed_via_gather():
    """反向半条：子任务取消不得转抛给上层（否则 cleanup 中途炸废、后续清理不跑）。

    按 AST 数，不拿文本计数充数：_lifecycle.py:416 的注释里就写着
    `gather(return_exceptions=True)`，旧写法"真调用 4 处 + 注释 1 处、阈值 ≥3"
    等于**摘掉两处仍绿**（复核点名的稀释）。这里数的是 gather 调用节点本身；
    对抗复核补充：不 await 也能让计数钉绿，故四处都必须真的在 await 表达式里。
    """
    cleanup = _cleanup_node()
    gathers = [n for n in ast.walk(cleanup)
               if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("gather")]
    assert len(gathers) == 4, \
        "cleanup 里的 gather 变成 %d 处了：先弄清是哪四处（取消收尾 _pending / check / "\
        "reconnect / 二次 check），再决定改判据还是改代码" % len(gathers)
    awaited = {id(a.value) for a in ast.walk(cleanup) if isinstance(a, ast.Await)}
    for g in gathers:
        kw = {k.arg: ast.unparse(k.value) for k in g.keywords}
        assert kw.get("return_exceptions") == "True", \
            "有一处 gather 没带 return_exceptions=True，子任务取消会转抛上层：%s" \
            % ast.unparse(g)[:100]
        assert id(g) in awaited, \
            "有一处 gather 没被 await（复核：不 await 也能让计数钉绿）：%s" % ast.unparse(g)[:100]


@pytest.mark.asyncio
async def test_c6_cleanup_propagates_its_own_cancellation():
    """行为半条：cleanup 停在等待子任务时被外部取消，CancelledError 必须传出。

    对抗复核实测：只留结构钉时，"gather 外包 try/except CancelledError: pass"
    或"干脆不 await"都能让它们全绿——"取消信号被吞"这类事故只有真跑拦得住。
    """
    obj = object.__new__(WindowControllerMQTTHandler)
    obj._closing = False
    obj._unsub_rsp = None
    obj._dispatch_tasks = []
    obj.pairing_timeout_handle = None
    obj._bind_ops = {}
    obj._reconnect_task = None
    obj._status_callbacks = set()

    release = asyncio.Event()

    async def stubborn():
        # 抗一次取消的子任务：把 cleanup 钉在等待点，外部取消才有确定落点
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()

    obj._check_task = asyncio.ensure_future(stubborn())
    task = asyncio.ensure_future(WindowControllerMQTTHandler.cleanup(obj))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not task.done(), "前置：cleanup 应停在等待被取消的子任务上"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    obj._check_task.cancel()
    try:
        await obj._check_task
    except BaseException:  # noqa: BLE001 - 收尾测试自己的假件任务
        pass


# ═══════════════════════════════ C-7 pkill ═══════════════════════════════


def _cleanup_mdns_code():
    body = RUN_SH[RUN_SH.index("cleanup_mdns() {"):]
    return _code_lines(body[:body.index("\n}") + 2])


def test_c7_run_sh_does_not_depend_on_pkill():
    assert not re.search(r"^\s*pkill\b", _code_lines(RUN_SH), re.M), \
        "run.sh 又用回 pkill：base 镜像没有 procps，清理会静默失效（C-7）"


def test_c7_cleanup_mdns_kills_both_the_subshell_and_python():
    """注释不算数（对抗复核实测：整段换成含全部字样的注释，3 条钉仍全绿）。"""
    body = _cleanup_mdns_code()
    assert 'kill "${MDNS_PID}"' in body, "看门狗子 shell 那条 kill 不许删"
    assert "/proc/" in body and "mdns_publisher" in body, \
        "python 那一条要按 /proc cmdline 真杀掉（与 §7b 判活同源）"


def test_c7_cleanup_skips_own_pid():
    """反向半条：扫描必须跳过自身，否则自杀式清理。"""
    assert '"$$"' in _cleanup_mdns_code()


# ═══════════════════ D-1 身份落盘失败的三处调用点（复核点名"修了没钉"）═══════════════════
# hub_client 里 `await self._save_identity()` 共三处：换码（api 面板直调）、清档
# （401 熔断）、注册（长连首跳）。审计前全部裸奔，本批逐条包了闸，但两条既有测试
# （test_refresh_bind_code_persists_and_degrades / test_invalidate_identity_writes_
# cleared_file）走的都是**落盘成功**那条路——也就是说把三个 try 全删掉，1270 条照绿。
# 下面每处一条真跑钉 + 一条反向半条，外加一条"不许再有裸调用点"的全类 AST 钉。


def _hub_client(tmp_path):
    return hc.HubClient([], config_dir=str(tmp_path), base="https://hub.invalid")


def _raising_save(*_a, **_kw):
    async def _boom():
        raise OSError(28, "No space left on device")
    return _boom


def test_d1_bindcode_persist_failure_returns_true_and_says_so(tmp_path, monkeypatch):
    c = _hub_client(tmp_path)
    c.instance_id, c._secret = "inst-1", "sec-1"

    async def ok_http(path, payload, timeout_s=None):
        return {"ok": True, "bindCode": "222222", "expiresInSec": 600}

    monkeypatch.setattr(c, "_http", ok_http)
    monkeypatch.setattr(c, "_save_identity", _raising_save())
    # 新码是云端当前认的那张：落盘失败也不许回滚成旧码，更不许把异常抛给面板（500）
    assert asyncio.run(c.refresh_bind_code()) is True
    assert c.bind_code == "222222", "落盘失败却回滚了内存码＝把云端已作废的旧码留给用户扫"
    assert c.last_op_error == "bindcode_persist_failed", \
        "标了错才谈得上「不假装没发生」，面板据此提示重启后需重新换码"

    # 反向半条：落盘成功 ⇒ 不许留错误槽，且码真进了磁盘
    c2 = _hub_client(tmp_path)
    c2.instance_id, c2._secret = "inst-1", "sec-1"
    monkeypatch.setattr(c2, "_http", ok_http)
    assert asyncio.run(c2.refresh_bind_code()) is True
    assert c2.last_op_error is None
    assert hc.load_identity(str(tmp_path))["bindCode"] == "222222"


def test_d1_invalidate_clears_memory_even_when_persist_fails(tmp_path, monkeypatch):
    c = _hub_client(tmp_path)
    c.instance_id, c._secret, c.bind_code = "inst-1", "sec-1", "111111"
    c.member_code, c.members, c.owner_masked = "222222", [{"openid": "o"}], "o***"
    monkeypatch.setattr(c, "_save_identity", _raising_save())
    asyncio.run(c._invalidate_identity("测试"))
    # 半途而废的旧形态：异常一抛，下面两行不执行 ⇒ 死身份留在磁盘被读回、熔断计数不涨
    assert c.instance_id is None and c._secret is None
    assert c._identity_rejected is True, "清档失败就没了指望重注册的闸，HA 重启也读回死身份"
    assert c._rereg_streak == 1, "熔断计数被落盘异常吞掉 ⇒ 三次熔断永不成立，无限重连"
    assert c.last_error == "identity_rejected"
    assert c.member_code is None and c.members == [], \
        "D-2 半边：成员码/成员表属于同一个 instance，清档时必须一起抹掉"

    c2 = _hub_client(tmp_path)
    c2.instance_id, c2._secret = "inst-1", "sec-1"
    asyncio.run(c2._invalidate_identity("测试"))          # 反向半条：正常清档
    assert c2._rereg_streak == 1 and c2._identity_rejected is True
    assert not hc.load_identity(str(tmp_path)).get("instanceId"), \
        "正常路径必须真的把身份从磁盘抹掉（load_identity 无文件回空 dict）"


def test_d1_register_persist_failure_keeps_the_issued_identity(tmp_path, monkeypatch):
    c = _hub_client(tmp_path)

    async def ok_register(path, payload, timeout_s=None):
        return {"ok": True, "instanceId": "inst-9", "secret": "sec-9",
                "bindCode": "333333", "expiresInSec": 600}

    monkeypatch.setattr(c, "_http", ok_register)
    monkeypatch.setattr(c, "_save_identity", _raising_save())
    asyncio.run(c._ensure_registered())          # 旧形态：这里直接抛穿到 _run_forever
    assert c.instance_id == "inst-9" and c._secret == "sec-9"
    assert c._identity_rejected is False
    assert (c.last_error or "").startswith("identity_persist_failed"), \
        "落盘失败必须留痕：否则用户直到 HA 重启（重新注册、作废绑定码、留孤儿实例）才知道"

    # 反向半条：下一轮不许重复注册（内存身份可用＝"可继续工作"的那半句成立）
    calls = []

    async def counting_http(path, payload, timeout_s=None):
        calls.append(path)
        return {"ok": True, "instanceId": "x", "secret": "y"}

    monkeypatch.setattr(c, "_http", counting_http)
    asyncio.run(c._ensure_registered())
    assert calls == [], "内存身份被落盘失败连带作废 ⇒ 每轮都重注册，hub 侧堆孤儿实例"

    # 反向半条 2：落盘成功时不许留错误槽
    c2 = _hub_client(tmp_path)
    monkeypatch.setattr(c2, "_http", ok_register)
    asyncio.run(c2._ensure_registered())
    assert c2.last_error is None
    assert hc.load_identity(str(tmp_path))["instanceId"] == "inst-9"


def test_d1_no_naked_save_identity_call_site_remains():
    """全类钉：任何 `await self._save_identity()` 都必须在 try 体内（新增调用点同样受管）。"""
    seen = 0
    for p in sorted(PKG.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        in_try = set()
        for t in ast.walk(tree):
            if isinstance(t, ast.Try):
                for stmt in t.body:
                    for sub in ast.walk(stmt):
                        in_try.add(id(sub))
        for n in ast.walk(tree):
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call) \
                    and isinstance(n.value.func, ast.Attribute) \
                    and n.value.func.attr == "_save_identity":
                seen += 1
                assert id(n) in in_try, \
                    "%s:%d 裸 await _save_identity（盘满/OSError 会原样抛穿）" % (p.name, n.lineno)
    assert seen >= 3, "只扫到 %d 处调用点：扫描锚或调用点形态变了，本钉需重写" % seen


# ═══════════════════════════════ F-1 nginx 上游证书校验 ═══════════════════════════════

LOC_RE = re.compile(r"location /api/(?:github|gitee)/ \{(.*?)\n    \}", re.S)


def test_f1_generated_conf_verifies_upstream():
    m = re.search(r"cat > /etc/nginx/http\.d/ingress\.conf <<NGINXEOF\n(.*?)\nNGINXEOF",
                  RUN_SH, re.S)
    assert m, "run.sh 内 ingress heredoc 锚丢失"
    blocks = LOC_RE.findall(m.group(1))
    assert len(blocks) == 2, "应有 github/gitee 两条 https 反代，实得 %d" % len(blocks)
    for b in blocks:
        # 判据按"生效行"取：对抗复核 2026-09-30 实测三行 verify 指令全部行首加 #
        # （字样保留）5 条钉仍全绿——裸子串 `in` 会被自己的注释喂饱。
        assert _has_directive(b, "proxy_ssl_verify on;"), \
            "https 反代未校验上游证书（F-1）"
        assert _has_directive(
            b, "proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;")
        assert _has_directive(b, "proxy_ssl_server_name on;"), \
            "SNI 必须一起开，否则校验必然失败"
        assert _has_directive(b, "proxy_ssl_verify_depth 2;"), \
            "深度回到 nginx 默认的 1：gitee 实测两层中间 CA，会判超限 502"


def test_f1_ca_bundle_is_a_build_time_guarantee_not_a_luck():
    """F-1 的可用性反面：`proxy_ssl_verify on` 指向的 CA 文件必须真的在镜像里。

    文件缺失时 nginx -t 是 [emerg] 不能加载证书——整个 Web UI 起不来，比徽章取不到
    release JSON 严重一个量级。base 镜像"恰好带"不能当保证，所以本钉要求 Dockerfile
    显式列装，且列的包与两处 conf 引用的路径同源（alpine 的 ca-certificates 提供
    /etc/ssl/certs/ca-certificates.crt）。
    """
    dk = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"RUN apk add --no-cache(.*?)(?=\n[A-Z]|\ncopy|\nCOPY|\Z)", dk, re.S)
    assert m, "Dockerfile 的 apk 安装块锚丢失，本钉需重写"
    pkgs = [p.strip().rstrip("\\").strip() for p in m.group(1).splitlines() if p.strip()]
    assert "ca-certificates" in pkgs, \
        "Dockerfile 不再显式装 ca-certificates，而 conf 里的 proxy_ssl_verify on 依赖它"
    # 路径同源：conf 引用的束路径必须是该包在 alpine 里提供的那一份
    static = (ROOT / "ingress.conf").read_text(encoding="utf-8")
    for text, label in ((RUN_SH, "run.sh"), (static, "ingress.conf")):
        assert "/etc/ssl/certs/ca-certificates.crt" in text, \
            "%s 里的 CA 路径变了，与包提供的不是同一份，本钉需同步" % label


def test_f1_static_template_matches():
    conf = (ROOT / "ingress.conf").read_text(encoding="utf-8")
    blocks = LOC_RE.findall(conf)
    assert len(blocks) == 2
    for b in blocks:
        assert _has_directive(b, "proxy_ssl_verify on;") and \
            _has_directive(
                b, "proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;"), \
            "静态模板少了生效的 verify 指令（注释不算——复核实测的绕过形态）"


def test_f1_two_copies_stay_in_sync_after_the_fix():
    """两份 conf 的 https 段必须逐字同形（本仓此前"三次漂移全靠人工"）。"""
    gen = re.search(r"cat > /etc/nginx/http\.d/ingress\.conf <<NGINXEOF\n(.*?)\nNGINXEOF",
                    RUN_SH, re.S).group(1)
    # 注释行不参与比对：两份 conf 的说明文字本就可以不同，本仓那条同步钉
    # （test_v1712_audit::TestIngressMechanicalDiff）也是按"去注释后逐行等"判的。
    def norm(text):
        return [[l.strip() for l in blk.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
                for blk in LOC_RE.findall(text)]

    assert norm(gen) == norm((ROOT / "ingress.conf").read_text(encoding="utf-8"))


# ═══════════════════════════════ F-2 nginx 看门狗 ═══════════════════════════════


def _watchdog_seg():
    start = RUN_SH.index("# ---------- 3a. nginx 存活看门狗")
    return RUN_SH[start:RUN_SH.index("# ---------- 3b.")]


def test_f2_nginx_watchdog_probes_both_port_and_process():
    """看门狗存在性 + 双判据（端口 + 本容器 nginx 进程）。

    对抗复核 2026-09-30 实测两条绕过：①整块换成"含全部字样"的注释，4 条钉
    全绿；②只看端口——host_network 下 /proc/net/tcp 是**宿主**的表，
    "端口被宿主其它进程占用"会被判成健康（审计 F-2 的第二场景）。现在
    注释行剔除后再判，且两个探针与"有监听无进程"诊断都必须在场。
    """
    seg = _code_lines(_watchdog_seg())
    assert "nginx_probe_listen" in seg and "nginx_probe_procs" in seg, \
        "看门狗必须同时用端口探针与进程探针（缺进程侧⇒宿主抢端口时判健康）"
    assert ":2AF6$" in seg, "端口探针判据必须是 10998 的 LISTEN（大写十六进制 2AF6）"
    assert re.search(r"^\s*nginx\s*$|nginx \|\|", seg, re.M), "拉起动作缺失"
    assert "有监听但容器内无 nginx 进程" in seg, \
        "端口被他人占用（有监听无进程）必须打诊断，不许静默当健康"


def test_f2_watchdog_has_bounded_restarts():
    seg = _code_lines(_watchdog_seg())
    assert "MAX_NGINX_RESTARTS" in seg and "-ge" in seg, "缺连续重启上限（会刷屏无限拉起）"


def test_f2_probe_listen_counts_listen_sockets_and_survives_read_errors(tmp_path):
    """端口探针行为钉（抽 run.sh 真实现、喂假 /proc 真跑）。

    四臂：①命中 2AF6+0A 计数 1；②st 非 0A 不计；③**tcp6 缺失不得吞掉 tcp 的
    命中**（两文件一起喂 awk 时缺失的那个是致命错误——初版踩过，判 0 会误拉起
    nginx）；④**读取异常必须在 set -e 下仍以 0 成功退出**——复核缺口 B：旧写法
    `X=$(awk …)` 的非零退出会把整个看门狗子 shell 静默杀死。
    """
    fn = _runsh_function("nginx_probe_listen")
    tcp = tmp_path / "tcp"
    tcp.write_text(
        "  sl  local_address rem_address   st\n"
        "   0: 00000000:2AF6 00000000:0000 0A 00000000:00000000 00:00000000\n"
        "   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000\n"
        "   2: 0100007F:2AF6 00000000:0000 01 00000000:00000000 00:00000000\n",
        encoding="utf-8")
    tcp6 = tmp_path / "tcp6"
    tcp6.write_text("  sl  local_address rem_address   st\n", encoding="utf-8")
    r = _bash_run(fn + "\nnginx_probe_listen\n",
                  env_extra={"NGINX_PROC_TCP": tcp.as_posix(),
                             "NGINX_PROC_TCP6": tcp6.as_posix()})
    assert r.returncode == 0, "探针异常退出：%s" % r.stderr
    assert r.stdout.strip() == "1", "应只数到 1 条 2AF6+0A，实得 %r" % r.stdout
    r_missing6 = _bash_run(fn + "\nnginx_probe_listen\n",
                           env_extra={"NGINX_PROC_TCP": tcp.as_posix(),
                                      "NGINX_PROC_TCP6": "/nonexistent-tcp6"})
    assert r_missing6.stdout.strip() == "1", \
        "tcp6 缺失吞掉了 tcp 的命中（判 0 ⇒ 误拉起）：%r" % r_missing6.stdout
    r2 = _bash_run(fn + "\nnginx_probe_listen\n",
                   env_extra={"NGINX_PROC_TCP": "/nonexistent-a",
                              "NGINX_PROC_TCP6": "/nonexistent-b"})
    assert r2.returncode == 0, \
        "读取异常时探针非零退出（set -e 会静默杀死看门狗，F-2 复核缺口 B）：%s" % r2.stderr
    assert r2.stdout.strip() == "0", "读取异常必须以 0 计，实得 %r" % r2.stdout


def test_f2_probe_procs_counts_nginx_like_a_container_scan(tmp_path):
    """进程探针行为钉：只数本容器的 nginx；/proc 里的非数字条目必须跳过。"""
    fn = _runsh_function("nginx_probe_procs")
    root = tmp_path / "proc"
    (root / "1234").mkdir(parents=True)
    (root / "1234" / "comm").write_text("nginx\n", encoding="utf-8")
    (root / "77").mkdir()
    (root / "77" / "comm").write_text("bash\n", encoding="utf-8")
    (root / "self").mkdir()          # /proc 里真实存在的非数字条目
    (root / "uptime").write_text("x", encoding="utf-8")
    r = _bash_run(fn + "\nexport NGINX_PROC_ROOT='%s'\nnginx_probe_procs\n" % root.as_posix())
    assert r.returncode == 0, "进程探针异常退出：%s" % r.stderr
    assert r.stdout.strip() == "1", "应只数到 1 个 nginx 进程，实得 %r" % r.stdout


def test_f2_healthy_requires_both_port_and_process():
    """健康判据行为钉：抽出 run.sh 里那条**真判据行**原文来跑（不重抄判据）。

    复核补口后的判据 = "端口有监听 AND 本容器 nginx 在"：只看端口时
    (LISTEN=1, PROCS=0) 会被判成 HEALTHY——宿主抢走 10998 后看门狗就
    永远不再拉起自己。
    """
    seg = _code_lines(_watchdog_seg())
    m = re.search(r'^        if (\[ "\$\{NGINX_LISTEN\}"[^\n]*); then$', seg, re.M)
    assert m, "健康判据行锚丢失（run.sh 结构变了，本钉需同步）"
    cond = m.group(1)
    for listen, procs, healthy in (("1", "1", True), ("1", "0", False),
                                   ("0", "1", False), ("0", "0", False)):
        script = ("set -e\nNGINX_LISTEN=%s\nNGINX_PROCS=%s\n"
                  "if %s; then echo HEALTHY; else echo SICK; fi\n" % (listen, procs, cond))
        r = _bash_run(script)
        want = "HEALTHY" if healthy else "SICK"
        assert r.stdout.strip() == want, \
            "LISTEN=%s PROCS=%s 判成 %r（应 %s）" % (listen, procs, r.stdout.strip(), want)


def test_f2_empty_probe_output_is_not_treated_as_alive():
    """看门狗禁假默认：探针把读取失败归一成 0，判据做数值比较（不是 `:-` 兜底）。"""
    seg = _code_lines(_watchdog_seg())
    assert re.search(r'\$\{NGINX_LISTEN\}" -gt 0', seg), \
        "健康判据必须对探针输出做数值比较（空/垃圾不得当健康）"
    assert '|| _c=""' in _code_lines(_runsh_function("nginx_probe_listen")), \
        "读取异常必须被探针吞掉（裸 $(awk) 的非零退出在 set -e 下杀死看门狗）"


def test_f2_restart_count_resets_on_healthy_probe():
    """反向半条："连续"语义必须清零，否则长跑后偶发一次崩溃也凑够上限直接放弃。"""
    seg = _code_lines(_watchdog_seg())
    assert "NGINX_RESTART_COUNT=0" in seg


# ═══════════════════════════════ F-3 heredoc $SYS ═══════════════════════════════


def _bridge_heredoc():
    m = re.search(r'cat >> "\$\{MOSQ_CONF\}" <<EOF\n(.*?)\nEOF\n', RUN_SH, re.S)
    assert m, "桥段 heredoc 锚丢失"
    return m.group(1)


def test_f3_sys_reference_is_escaped_in_the_unquoted_heredoc():
    body = _bridge_heredoc()
    assert r"gateway/test/\$SYS/" in body, \
        "$SYS 未转义 ⇒ 生成的 mosquitto.conf 注释里被展开为空（F-3）"


def test_f3_rendered_comment_keeps_the_red_line_text():
    """真渲染一次：落盘文本必须仍是 gateway/test/$SYS/。"""
    script = ('BRIDGE_MARKER=M\nBRIDGE_CREDS=""\nBRIDGE_TOPICS_EXTRA="topic a/# in 1"\n'
              'cat <<EOF\n' + _bridge_heredoc() + "\nEOF\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, timeout=60,
                       encoding="utf-8", errors="replace")
    assert "gateway/test/$SYS/" in (r.stdout or ""), \
        "渲染结果里 $SYS 仍被吞掉（修复未生效）"
    assert "gateway/test//\u5168\u5339\u914d" not in (r.stdout or "")


# ═══════════════════════════ C-3 / H-2 WS 停机路径可观测 ═══════════════════════════
# H-2：`ws_gateway.py` 的 STOP 注册包在 `except Exception  # 无 bus 环境（测试桩）`
# 里，而全部 ws 测试的 hass 替身**没有 bus** ⇒ 那条注册在测试里恒为死码（把
# `if not domain_data.get(...)` 改成 `if False:` 全量仍绿）。这里给它配真 bus，
# 把 STOP 路径从"不可观测"变成"真跑"。


class _FakeServer:
    """WsGatewayServer 的等宽替身：只替掉真 bind 端口那一段。"""

    instances = 0

    def __init__(self, hass, *, host="0.0.0.0", port, token):
        self.port, self.token = port, token
        self.host = host
        self._runner = "running"
        self._stopping = False
        self.stops = 0
        self.started = 0
        _FakeServer.instances += 1

    async def async_start(self):
        self.started += 1

    async def async_stop(self):
        self.stops += 1
        self._stopping = True
        self._runner = None

    def _attach_listeners(self, managers):
        self.attached = managers

    def _entries_data(self):
        return []


def _ws_hass(tmp_path, listeners):
    entry = types.SimpleNamespace(entry_id="e1", options={}, data={}, disabled_by=None)
    store = {DOMAIN: {}}
    hass = types.SimpleNamespace(
        data=store,
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        config_entries=types.SimpleNamespace(async_entries=lambda d: [entry]),
        bus=types.SimpleNamespace(
            async_listen_once=lambda evt, cb: (listeners.append(cb), (lambda: None))[1]))
    return store, hass


@pytest.mark.asyncio
async def test_c3_ws_stop_listener_registered_once_and_stops_server(tmp_path, monkeypatch):
    listeners = []
    monkeypatch.setattr(wsg, "WsGatewayServer", _FakeServer)
    _FakeServer.instances = 0
    store, hass = _ws_hass(tmp_path, listeners)

    await wsg.async_ensure_ws_gateway(hass)
    await wsg.async_ensure_ws_gateway(hass)     # 改端口/改令牌式二次进入
    assert len(listeners) == 1, \
        "STOP 监听注册了 %d 次（v1.7.33 的「单例注册」回潮＝停机按次数重复执行）" % len(listeners)
    assert store[DOMAIN].get(wsg.WS_GATEWAY_STOP_LISTENER_KEY) is not None

    server = store[DOMAIN][wsg.WS_GATEWAY_DATA_KEY]
    await listeners[0](None)                    # 真跑 STOP 回调
    assert server.stops == 1, "STOP 回调没停服务器"
    assert store[DOMAIN].get(wsg.WS_GATEWAY_STOPPED_KEY) is True, "STOP 未置闩锁"
    assert wsg.WS_GATEWAY_DATA_KEY not in store[DOMAIN]


@pytest.mark.asyncio
async def test_c3_ensure_refuses_to_respawn_after_stop_latched(tmp_path, monkeypatch):
    """闩锁置上后 ensure 不得再拉起（否则关机过程中 9001 又被打开）。"""
    listeners = []
    monkeypatch.setattr(wsg, "WsGatewayServer", _FakeServer)
    _FakeServer.instances = 0
    store, hass = _ws_hass(tmp_path, listeners)
    store[DOMAIN][wsg.WS_GATEWAY_STOPPED_KEY] = True
    await wsg.async_ensure_ws_gateway(hass)
    assert _FakeServer.instances == 0, "停机闩锁失效：ensure 又建了一个服务器实例"


@pytest.mark.asyncio
async def test_c3_late_registration_is_self_closed_by_recheck(tmp_path, monkeypatch):
    """登记后复检：闩锁在 async_start 让出点期间被置上，本实例必须自我让位。"""
    listeners = []
    store, hass = _ws_hass(tmp_path, listeners)
    made = []

    class _RacingServer(_FakeServer):
        def __init__(self, hass, *, host="0.0.0.0", port, token):
            super().__init__(hass, host=host, port=port, token=token)
            made.append(self)

        async def async_start(self):
            # 模拟并发 STOP：本实例启动让出期间，STOP 侧把闩锁置上
            store[DOMAIN][wsg.WS_GATEWAY_STOPPED_KEY] = True
            await super().async_start()

    monkeypatch.setattr(wsg, "WsGatewayServer", _RacingServer)
    await wsg.async_ensure_ws_gateway(hass)
    assert made, "前置：确实走到了「启动中被并发置锁」的形态"
    assert wsg.WS_GATEWAY_DATA_KEY not in store[DOMAIN], \
        "迟到登记的实例没被复检收回（整个关机过程 9001 继续监听，C-3 主形态）"
    assert made[0].stops == 1, "收回必须真把刚起的服务器停掉"


@pytest.mark.asyncio
async def test_c3_second_async_stop_waits_for_the_first_to_release_the_port(tmp_path):
    """`_stopping` 短路不得再"立刻返回"——端口此时并未释放。"""
    srv = wsg.WsGatewayServer(hass=None, port=9001, token="t")
    order = []

    class _Runner:
        async def cleanup(self):
            await asyncio.sleep(0.05)
            order.append("first-released-port")

    srv._runner = _Runner()
    first = asyncio.ensure_future(srv.async_stop())
    await asyncio.sleep(0.005)                  # 让首路真正进入 cleanup
    second = asyncio.ensure_future(srv.async_stop())
    await asyncio.wait_for(second, timeout=1.0)
    order.append("second-returned")
    await first
    assert order == ["first-released-port", "second-returned"], \
        "第二路在端口释放前就返回了（并发 ensure 因此撞 OSError 端口仍被占）：%s" % order


@pytest.mark.asyncio
async def test_c3b_stop_branch_pop_is_identity_gated(tmp_path, monkeypatch):
    """wanted=None 分支的 pop 必须判等——金牌复测实测：把它改成无条件 pop，1286 条全绿。

    真实现里 `await current.async_stop()` 有秒级让出点（客户端 close 最长 ~10s），期间
    并发 ensure 完全可能已经拉起并登记了新实例；无条件 pop 删掉的是**他人**的注册，
    于是新监听器持旧令牌再也关不掉（v1.7.18 BUG-6 注释自陈的那道判等）。
    """
    gate = asyncio.Event()

    class _ParkedServer(_FakeServer):
        slow = True

        async def async_stop(self):
            self.stops += 1
            self._stopping = True
            if _ParkedServer.slow:
                _ParkedServer.slow = False
                await gate.wait()         # 停在"刚要收尾"这一刻，把让出点做实
            self._runner = None

    monkeypatch.setattr(wsg, "WsGatewayServer", _ParkedServer)
    _ParkedServer.slow = True
    entries = []

    def _disabled():
        return types.SimpleNamespace(
            entry_id="e1", options={c.CONF_WS_GATEWAY_ENABLED: False},
            data={}, disabled_by=None)

    entries.append(_disabled())
    store = {DOMAIN: {}}
    listeners = []
    hass = types.SimpleNamespace(
        data=store,
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        config_entries=types.SimpleNamespace(async_entries=lambda d: list(entries)),
        bus=types.SimpleNamespace(
            async_listen_once=lambda evt, cb: (listeners.append(cb), (lambda: None))[1]))

    old = _ParkedServer(hass, port=9001, token="old-token")
    store[DOMAIN][wsg.WS_GATEWAY_DATA_KEY] = old

    first = asyncio.ensure_future(wsg.async_ensure_ws_gateway(hass))
    await asyncio.sleep(0.02)             # 让 first 停在 async_stop 里
    assert old.stops == 1, "前置：停用分支还没真的在停"

    new = _ParkedServer(hass, port=9001, token="new-token")
    store[DOMAIN][wsg.WS_GATEWAY_DATA_KEY] = new     # 模拟并发 ensure 完成的新登记
    gate.set()
    await first

    assert store[DOMAIN].get(wsg.WS_GATEWAY_DATA_KEY) is new, \
        "旧实例收尾时把新登记 pop 掉了（C-3b）：新监听器从此无人持有，旧令牌孤儿监听"

    # 反向半条：没有接管者时 pop 必须真发生（判等不许做成永不删）
    _ParkedServer.slow = True
    store2 = {DOMAIN: {}}
    hass2 = types.SimpleNamespace(
        data=store2,
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        config_entries=types.SimpleNamespace(async_entries=lambda d: [entry_off]),
        bus=types.SimpleNamespace(async_listen_once=lambda e, cb: (lambda: None)))
    entry_off = _disabled()
    stale = _ParkedServer(hass2, port=9001, token="t")
    store2[DOMAIN][wsg.WS_GATEWAY_DATA_KEY] = stale
    await wsg.async_ensure_ws_gateway(hass2)
    assert wsg.WS_GATEWAY_DATA_KEY not in store2[DOMAIN], \
        "判等闸焊死：无人接管时注册也没删（F2 原判据回潮）"


@pytest.mark.asyncio
async def test_c3c_stop_gateway_pop_is_identity_gated(tmp_path, monkeypatch):
    """C-3 的另一半（金牌复测实测出出来的）：`async_stop_ws_gateway` 的 pop 同样判等。

    STOP 先置闩锁再 `await current.async_stop()`，而闩锁复检写在 ensure 的**拉起前**
    （:1041），parking 期间到达的 ensure 仍可能完成一次登记；无条件 pop 删的是那张新
    注册——与 :1022 那道判等同一动机，两处都得钉（矩阵里两处锚点各一行，摘一处红一处）。
    """
    gate = asyncio.Event()

    class _Parked(_FakeServer):
        slow = True

        async def async_stop(self):
            self.stops += 1
            self._stopping = True
            if _Parked.slow:
                _Parked.slow = False
                await gate.wait()
            self._runner = None

    monkeypatch.setattr(wsg, "WsGatewayServer", _Parked)
    _Parked.slow = True
    store, hass = _ws_hass(tmp_path, [])
    old = _Parked(hass, port=9001, token="old")
    store[DOMAIN][wsg.WS_GATEWAY_DATA_KEY] = old

    first = asyncio.ensure_future(wsg.async_stop_ws_gateway(hass))
    await asyncio.sleep(0.02)
    assert old.stops == 1, "前置：STOP 没真的在停服务器"
    new = _Parked(hass, port=9001, token="new")
    store[DOMAIN][wsg.WS_GATEWAY_DATA_KEY] = new
    gate.set()
    await first
    assert store[DOMAIN].get(wsg.WS_GATEWAY_DATA_KEY) is new, \
        "STOP 收尾把停机期间的新登记删了（C-3c）：那个监听器从此无人持有"
    assert store[DOMAIN].get(wsg.WS_GATEWAY_STOPPED_KEY) is True

    # 反向半条：没有接管者时必须真删
    _Parked.slow = True
    store2, hass2 = _ws_hass(tmp_path, [])
    stale = _Parked(hass2, port=9001, token="t")
    store2[DOMAIN][wsg.WS_GATEWAY_DATA_KEY] = stale
    await wsg.async_stop_ws_gateway(hass2)
    assert wsg.WS_GATEWAY_DATA_KEY not in store2[DOMAIN], \
        "判等闸焊死：无人接管时 STOP 也没删注册（关机后端口空转）"


# ═══════════════════════ H-6 假件视图不得窄于真实现（跨文件补钉）═══════════════════════
@pytest.mark.parametrize("mod_name", [
    "test_v1754_member_alias", "test_v1747_hub_members_api", "test_hub_members_refresh"])
def test_h6_fake_status_views_are_no_narrower_than_real(mod_name):
    """假件的 status_view 必须**由真实现派生**，不许再手写 dict。

    缺陷面（审计 H-6 #5）：手写 dict 的键集是抄的人当时看到的那份，真实现加/减
    字段时它静默不跟——于是 test_member_op_views_return_the_full_status_view 这类
    **名字**判的其实是假件自己的键（真 status_view 停发 bindCodeTtlS 照样绿，
    面板倒计时退回硬编兜底）。

    判据为什么是「派生调用在场」而不是「键集相等」：三处假件现在都走
    conftest.full_status_view()，键集相等已被构造保证，再钉一次是**恒真钉**
    （正是本批要消灭的那种守卫）。要防的是"有人新写一个手写 dict 的假件"。
    """
    import importlib
    importlib.import_module(mod_name)                    # 先确认真的导得动

    path = Path(__file__).resolve().parent / ("%s.py" % mod_name)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bodies = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "status_view"]
    assert bodies, "%s 里已无 status_view 假件（本钉需重写）" % mod_name
    for fn in bodies:
        # 认**调用节点**，不认文本：ast.unparse 会把 docstring 一起吐出来，
        # 一句"这里改用 full_status_view()"的注释/文档串就能把文本版钉成恒真。
        called = [c for c in ast.walk(fn)
                  if isinstance(c, ast.Call)
                  and getattr(c.func, "id", None) == "full_status_view"]
        assert called, \
            "%s 的假件视图又是手写 dict（键集会随真实现漂移）：%s" % (
                mod_name, ast.unparse(fn)[:120])


def test_h6_the_deriving_helper_itself_reads_the_real_implementation():
    """反向半条：派生器自己不许退化成第二份手抄清单。

    旧写法是 `"HubClient" in inspect.getsource(full_status_view)`——文本钉：该函数的
    docstring 里本来就写着 HubClient 和 status_view()，把真调用整段换成手写 dict 它
    **照样满足**（复核点名的稀释，与 B-4 那条同一病）。docstring 是 AST 的一部分，
    所以 ast.unparse 也躲不开——这里直接认调用形态：视图必须来自构造真 HubClient。
    """
    from homeassistant.core import full_status_view

    tree = ast.parse((Path(__file__).resolve().parent / "conftest.py")
                     .read_text(encoding="utf-8"))
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
           and n.name == "full_status_view"]
    assert len(fns) == 1, "conftest 里 full_status_view 有 %d 个定义，本钉会验到死码" % len(fns)
    derived = [n for n in ast.walk(fns[0])
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "status_view"
               and isinstance(n.func.value, ast.Call)
               and getattr(n.func.value.func, "id", None) == "HubClient"]
    assert derived, "派生器没在构造真 HubClient 取视图＝又一份手写清单（假绿的新家）"

    keys = set(full_status_view().keys())
    assert {"bindCodeTtlS", "memberCodeTtlS", "memberCodeExpiresIn",
            "memberCodeExpired"} <= keys, \
        "TTL/过期字段不在视图里——面板倒计时会退回硬编兜底，正是 H-6 的实害"
    # 键集必须与真实现逐项相等——这才是"派生"的全部意义：真实现加/减字段，
    # 手写清单会在这里当场分叉（旧钉只看 4 个字段在场，漏了反向那一半）。
    from custom_components.window_controller_gateway.hub_client import HubClient
    assert set(HubClient([], config_dir=".").status_view()) == keys, \
        "假件视图与真 status_view 键集分叉：派生器已退化成手写清单"


# ═══════════════════════════════ F-4 CI 看门狗 ═══════════════════════════════


def test_f4_lint_job_has_timeout_minutes():
    """job 级键的**缩进**本身就是判据的一部分。

    对抗复核实测：把 timeout-minutes 移到 lint 的某个 step 下（YAML 合法），
    旧正则（job 段内任意位置）照样绿——而 step 级上限罩不住同 job 其它用例，
    挂死仍等 Actions 默认 6 小时。job 级键必须是 4 空格缩进的顶层键。
    """
    ci = (REPO / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    m = re.search(r"\n  lint:\n(.*?)(?=\n  \w[\w-]*:\n)", ci, re.S)
    assert m, "lint job 段锚丢失"
    assert re.search(r"\n    timeout-minutes:\s*\d+\s*(?:#.*)?\n", m.group(1)), \
        "lint job 缺 **job 级** timeout-minutes（F-4）：用例挂死要等 6 小时"
    assert not re.search(r"\n\s{6,}timeout-minutes:", m.group(1)), \
        "时间上限被挪到 step 级：罩不住同 job 其它用例（复核实测的假绿形态）"


# ═══════════════════════════════ E-1 选项页可达 ═══════════════════════════════


@pytest.mark.asyncio
async def test_e1_awaiting_entry_gets_a_menu():
    """无 SN 条目此前被无条件分流到 add_gateway ⇒ WS 令牌/端口/开关永无入口。"""
    flow = object.__new__(cf_mod.OptionsFlow)
    flow._config_entry = types.SimpleNamespace(
        data={c.CONF_GATEWAY_SN: ""}, options={}, entry_id="e1")
    shown = {}

    def _menu(**kw):
        shown.update(kw)
        return {"type": "menu", **kw}

    flow.async_show_menu = _menu
    res = await flow.async_step_init()
    assert res["type"] == "menu", "无 SN 条目必须给出菜单（选项步骤否则不可达）"
    assert shown["step_id"] == "init"
    assert set(shown["menu_options"]) == {"add_gateway", "options"}


@pytest.mark.asyncio
async def test_e1_configured_entry_still_goes_straight_to_options():
    """反向半条：已配 SN 的条目不得多一层菜单。"""
    flow = object.__new__(cf_mod.OptionsFlow)
    flow._config_entry = types.SimpleNamespace(
        data={c.CONF_GATEWAY_SN: "1001GW"}, options={}, entry_id="e1")
    hit = []

    async def _opts(user_input=None):
        hit.append(1)
        return {"type": "form"}

    flow.async_step_options = _opts
    res = await flow.async_step_init()
    assert res == {"type": "form"} and hit == [1]


def test_e1_menu_strings_exist_in_both_locales():
    """菜单项文案两份都得有（裸英文 step id 泄漏 UI 是本仓红线）。"""
    for rel in ("strings.json", "translations/zh-CN.json"):
        s = json.loads((PKG / rel).read_text(encoding="utf-8"))
        menu = s["options"]["step"]["init"]["menu_options"]
        assert set(menu) == {"add_gateway", "options"}
        for key, val in menu.items():
            assert any("一" <= ch <= "鿿" for ch in val), "%s 的菜单项 %s 非中文" % (rel, key)


def test_e1_awaiting_entry_would_have_been_listening_on_the_public_default_token():
    """风险前提核验：awaiting 条目（空 options）确实取到公开默认令牌。"""
    entry = types.SimpleNamespace(entry_id="e1", options={}, data={}, disabled_by=None)
    hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_entries=lambda d: [entry]))
    _port, token = wsg.ws_gateway_wanted(hass)
    assert token == c.DEFAULT_WS_GATEWAY_TOKEN, \
        "默认令牌口径变了，E-1 的风险论证需重估"


# ═══════════════════════════════ E-2 禁用条目 ═══════════════════════════════


def _ws_entry(port=None, token=None, enabled=None, disabled=None, eid="e1"):
    options = {}
    if enabled is not None:
        options[c.CONF_WS_GATEWAY_ENABLED] = enabled
    if port is not None:
        options[c.CONF_WS_GATEWAY_PORT] = port
    if token is not None:
        options[c.CONF_WS_GATEWAY_TOKEN] = token
    return types.SimpleNamespace(entry_id=eid, options=options, data={},
                                 disabled_by=disabled)


def _hass_with(entries):
    return types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_entries=lambda d: list(entries)))


def test_e2_disabled_entry_config_is_not_adopted():
    """禁用条目①(9100/tokenA) + 启用条目②(9001/tokenB) ⇒ 必须采纳②。"""
    hass = _hass_with([_ws_entry(port=9100, token="tokenA", disabled="user", eid="e1"),
                       _ws_entry(port=9001, token="goodtoken1", eid="e2")])
    assert wsg.ws_gateway_wanted(hass) == (9001, "goodtoken1"), \
        "采纳了被禁用条目的端口/令牌（E-2）"


def test_e2_all_disabled_stops_the_listener():
    """全部条目禁用 ⇒ wanted 必须是 None（9001 不得空转残留）。"""
    assert wsg.ws_gateway_wanted(_hass_with([_ws_entry(disabled="user")])) is None


def test_e2_enabled_entry_still_wins():
    """反向半条：单个启用条目照常采纳（闸不许做成永拒）。"""
    assert wsg.ws_gateway_wanted(
        _hass_with([_ws_entry(port=9001, token="goodtoken1")])) == (9001, "goodtoken1")


def test_e2_explicitly_disabled_option_still_skipped():
    """反向半条 2：options 里显式关 WS 的条目仍按原判据跳过。"""
    assert wsg.ws_gateway_wanted(
        _hass_with([_ws_entry(enabled=False, port=9001, token="goodtoken1")])) is None


def test_e2_token_persist_skips_disabled_entries():
    """_persist_token 不得把新令牌写进被禁用条目（否则全禁用时误判写入成功、不回滚）。

    本条只作**存在性**辅助钉（常数在场）；真正的判据是下面那条行为钉——
    对抗复核实测：删掉行为、只留一行含 "disabled_by" 的赋值，本条照样绿。
    """
    # getattr(entry, "disabled_by", None) 里 "disabled_by" 是**常量**而非名字，
    # 按 co_names 找会永远找不到（一条恒假的正向判据不如不写）。
    consts = wsg.WsGatewayServer._persist_token.__code__.co_consts
    assert "disabled_by" in consts, "令牌持久化未跳过禁用条目（E-2 同族）"


def test_e2_token_persist_skips_disabled_entries_by_behavior():
    """行为钉（复核点名的"字符串常量在场、行为删光仍绿"）：真跑 _persist_token。

    两臂：①禁用条目不得收到写入、启用条目照写；②全禁用 ⇒ 零命中必须回滚
    内存令牌（否则 HA 重启回退旧令牌、小程序侧永久 401 漂移）。
    """
    updates = []

    def _mk(entry_id, disabled):
        return types.SimpleNamespace(
            entry_id=entry_id, disabled_by=disabled,
            options={c.CONF_WS_GATEWAY_ENABLED: True,
                     c.CONF_WS_GATEWAY_TOKEN: "old"})

    def _hass(entries):
        return types.SimpleNamespace(
            data={DOMAIN: {}},
            config_entries=types.SimpleNamespace(
                async_entries=lambda d: entries,
                async_update_entry=lambda e, options=None: updates.append(
                    (e.entry_id, options))))

    off, on = _mk("e-off", "user"), _mk("e-on", None)
    srv = wsg.WsGatewayServer(_hass([off, on]), port=9001, token="old")
    srv._token = "new"
    asyncio.run(srv._persist_token("new", "old"))
    assert [eid for eid, _ in updates] == ["e-on"], \
        "被禁用条目也被写进了新令牌（E-2 同族；全禁用时还会误判 wrote=True 不回滚）"
    assert srv._token == "new"

    updates.clear()
    srv2 = wsg.WsGatewayServer(_hass([_mk("e-off", "user")]), port=9001, token="old")
    srv2._token = "new"
    asyncio.run(srv2._persist_token("new", "old"))
    assert updates == [] and srv2._token == "old", \
        "全部条目被禁用时未回滚内存令牌（重启回退漂移）"


def test_e2_hub_option_skips_disabled_entries():
    """hub 长连的 hub_base/install_key 也不得取自被禁用条目（同类第三处）。"""
    off = types.SimpleNamespace(options={"hub_base": "http://old-hub.invalid:1/"},
                                disabled_by="user")
    on = types.SimpleNamespace(options={"hub_base": "http://live-hub.invalid:1/"},
                               disabled_by=None)
    assert pkg._hub_option(_hass_with([off, on]), "hub_base") == \
        "http://live-hub.invalid:1/", "采纳了被禁用条目的 hub 地址（E-2 同类）"
    # 反向半条：没有禁用条目时照常取值（闸不许做成永不取）
    assert pkg._hub_option(_hass_with([on]), "hub_base") == "http://live-hub.invalid:1/"
    # 全部禁用 ⇒ 空串，调用方按 env/内置默认解析（不是"沿用旧 hub"）
    assert pkg._hub_option(_hass_with([off]), "hub_base") == ""


def test_e2_security_view_ignores_disabled_entries(monkeypatch):
    """/security 的凭据告警只按**有效**条目判定（同类第四处）。"""
    from homeassistant.components import http as ha_http
    monkeypatch.setattr(ha_http.HomeAssistantView, "json",
                        lambda self, data: data, raising=False)
    view = api.WindowGatewaySecurityView()

    def run(entries):
        return asyncio.run(view.get(
            types.SimpleNamespace(app={"hass": _hass_with(entries)})))

    custom = "custom-x-123456789"
    # 唯一条目被禁用且仍是默认令牌 ⇒ 无从判定（不是"没风险"，是"根本没在跑"）
    r = run([_ws_entry(disabled="user")])
    assert r["ws_token_is_default"] is None and r["gateway_entries"] == 0, \
        "把禁用条目算进了安全判定（E-2 同类）"
    # 反向半条 1：启用条目未改令牌 ⇒ 照报 True（闸不许做成永不告警）
    assert run([_ws_entry()])["ws_token_is_default"] is True
    # 反向半条 2：启用条目已改、只有被禁用条目是默认串 ⇒ 不得告警
    assert run([_ws_entry(token=custom, eid="e2"),
                 _ws_entry(disabled="user")])["ws_token_is_default"] is False, \
        "禁用条目的默认令牌仍在触发告警（E-2 同类）"
    # 取严仍按有效条目：启用的是默认串 ⇒ True，与被禁用条目改没改无关
    assert run([_ws_entry(token=custom, disabled="user"),
                 _ws_entry(eid="e2")])["ws_token_is_default"] is True


def _existence_mgr(tmp_path, entries, current_sn="1001NEW", mapping=None, eid="e-cur"):
    entry = types.SimpleNamespace(
        entry_id=eid, data={c.CONF_GATEWAY_SN: current_sn}, options={})
    store = {DOMAIN: {}}
    if mapping is not None:
        store[DOMAIN][c.DEVICE_TO_GATEWAY_MAPPING] = mapping
    hass = types.SimpleNamespace(
        data=store, loop=None,
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        async_create_task=_task_shim, async_add_job=lambda job, *a: None,
        config_entries=types.SimpleNamespace(
            async_get_entry=lambda wanted: entry if wanted == eid else None,
            async_entries=lambda domain: list(entries) + [entry]))
    mgr = WindowControllerDeviceManager(hass, entry)

    async def _noop(*a, **k):
        return None
    mgr._notify_device_conflict = _noop
    return mgr, store


def test_e2_disabled_old_gateway_still_counts_as_existing(tmp_path):
    """行为钉（复核缺口）：存在性判定不许滤 disabled_by——禁用≠已删除。

    add_device 靠"旧网关条目是否仍在"决定要不要自动转移设备；给这个循环加
    disabled 过滤 ⇒ 禁用旧网关被当成删除、设备被静默转移（E-2 反例）。
    """
    old = types.SimpleNamespace(
        entry_id="e-old", data={c.CONF_GATEWAY_SN: "1001OLD"},
        options={}, disabled_by="user")
    mgr, store = _existence_mgr(tmp_path, [old], mapping={"50051": "1001OLD"})
    res = asyncio.run(mgr.add_device("50051", "开窗器"))
    assert res is None, "禁用旧网关被当作已删除：设备被自动转移（E-2 存在性面）"
    assert store[DOMAIN][c.DEVICE_TO_GATEWAY_MAPPING]["50051"] == "1001OLD"


def test_e2_deleted_old_gateway_still_auto_transfers(tmp_path):
    """反向半条：旧网关真被删除时，自动转移必须照常发生（闸不许做成永不转移）。"""
    mgr, store = _existence_mgr(tmp_path, [], mapping={"50051": "1001OLD"})
    asyncio.run(mgr.add_device("50051", "开窗器"))
    assert store[DOMAIN][c.DEVICE_TO_GATEWAY_MAPPING]["50051"] == "1001NEW", \
        "旧网关真删了却不转移（反向半条）"


def test_e2_entry_still_configured_counts_disabled_entries():
    """gateway._entry_still_configured（reload 让出点判定）：禁用条目仍在列表里。"""
    from custom_components.window_controller_gateway import gateway as gw_mod
    off = types.SimpleNamespace(entry_id="e-off", disabled_by="user")
    hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_entries=lambda d: [off]))
    assert gw_mod._entry_still_configured(hass, "e-off") is True, \
        "禁用条目被当成已删除（E-2 存在性面：reload 让出点会把'正在重载'误判）"
    # 反向半条：真不在列表里的 entry_id 必须回 False（调用方据此退回原行为）
    assert gw_mod._entry_still_configured(hass, "nope") is False


def test_e2_unbind_distinguishes_disabled_from_deleted(monkeypatch):
    """反向那一半：ws_gateway 的 unbind 重解析**不许**过滤禁用条目。

    那里问的是"条目还在不在配置列表里"，不是"是不是有效配置"。照单一真源口径
    一滤，禁用就被读成"已删除"并 ack ok:True——设备还在注册表里，小程序收到假
    成功、下次 get_devices 原样复活，正是 v1.6.17 F1 要消灭的形态。三态各自钉。
    """
    monkeypatch.setattr(wsg, "GATEWAY_READY_DELAY", 0)

    async def _unbind(_dev):
        return None

    handler = types.SimpleNamespace(connected=True, unbind_device=_unbind)
    manager = types.SimpleNamespace(devices={"DEV1": {}})
    first = {"mqtt_handler": handler, "device_manager": manager}
    seq = []
    monkeypatch.setattr(wsg.WsGatewayServer, "_find_entry", lambda self, sn: seq.pop(0))
    server = object.__new__(wsg.WsGatewayServer)

    def run(disabled):
        seq.extend([first, None])
        entries = [] if disabled == "gone" else [
            types.SimpleNamespace(data={c.CONF_GATEWAY_SN: "GW1"},
                                  disabled_by=None if disabled == "reload"
                                  else disabled)]
        server.hass = _hass_with(entries)
        return asyncio.run(server._cmd_unbind({"gwSn": "GW1", "devSn": "DEV1"}))

    r = run("user")
    assert r["ok"] is False and r["msg"] == "entry disabled", \
        "禁用条目被当成 reload 或已删除：小程序会按'稍后重试'死循环或收到假成功"
    r = run("reload")
    assert r["ok"] is False and r["msg"] == "entry reloading, retry later", \
        "reload 分支的既有语义（v1.7.31 B-1）被改动"
    r = run("gone")
    assert r["ok"] is True, "条目真删除时仍 ack 失败：删除随条目收口的原判据丢了"


def test_e2_single_source_of_truth_convention_is_not_widening():
    """口径核验：仓内"禁用条目不算有效配置"的单一真源仍是 utils.entry_state_for_sn。"""
    src = (PKG / "utils.py").read_text(encoding="utf-8")
    assert "disabled_by" in src, "utils 里的单一真源判据丢失（本批多条钉以此为据）"


# ---- E-2 全类清点：每个 async_entries(DOMAIN) 站点都得说清它问的是哪个问题 ----
# 审计只点了 4 处，但"同类"不能只修被点到的那几处就宣称收口。全仓扫下来是两类互斥
# 的问题：①"是不是有效配置"（必须滤 disabled_by，否则禁用条目越权接管运行态）；
# ②"条目还在不在 / 它的 entry_id"（**不许**滤：滤了会把禁用读成已删除，v1.6.17 F1
# 的假成功正从这条路出来）。下面把 23 处调用（去重后 22 个函数）逐个归类并双向核对：
# 新增站点不进清单就红，清单里的站点消失也红——把"这处到底该不该滤"变成每次提交都
# 要回答的问题，而不是一次性的人工结论。
_E2_VALID_SITES = {
    ("__init__.py", "_hub_option"): "hub 地址/密钥只能取自生效条目",
    ("api.py", "WindowGatewaySecurityView.get"): "凭据告警只对在册监听的网关成立",
    ("mqtt_bootstrap.py", "_enabled_huijian_entry_count"): "healer 计数按生效条目",
    ("utils.py", "entry_state_for_sn"): "单一真源本身",
    ("ws_gateway.py", "ws_gateway_wanted"): "端口/令牌采纳——E-2 案发现场",
    ("ws_gateway.py", "WsGatewayServer._persist_token"): "新令牌不得写进禁用条目",
}
_E2_EXISTENCE_SITES = {
    ("__init__.py", "_migrate_devices_async"): "等指定条目 reload 结束",
    ("__init__.py", "async_remove_entry"): "还剩几条才注销域级服务",
    ("config_flow.py", "ConfigFlow.async_step_user"): "同 SN 已占位则 abort（禁用也占位）",
    ("config_flow.py", "ConfigFlow.async_step_confirm_add"): "同上，unique_id 兜底扫描",
    ("config_flow.py", "ConfigFlow.async_step_discovery"): "同上",
    ("config_flow.py", "ConfigFlow.async_step_confirm_migration"): "按 SN 取目标 entry",
    ("config_flow.py", "ConfigFlow.async_step_replace"): "替换选择器（:660 注明无生产入口）",
    ("config_flow.py", "OptionsFlow.async_step_add_gateway"): "同 SN 已配置则报错",
    ("device_manager.py", "WindowControllerDeviceManager.add_device"):
        "旧网关条目是否仍在，决定要不要自动转移",
    ("device_manager.py", "WindowControllerDeviceManager.transfer_device"): "取目标/来源 entry_id",
    ("device_manager.py", "WindowControllerDeviceManager._rollback_migration"): "取旧网关 entry_id 回填",
    ("discovery.py", "async_discover_gateway"): "该 SN 已有条目则不重复自动添加",
    ("gateway.py", "_entry_still_configured"): "reload 让出点：条目还在不在列表",
    ("utils.py", "_watch_ear_promotion._check"): "已有条目则静默退出",
    ("services.py", "handle_migrate_devices.find_gateway_entry"): "迁移按 SN 取 entry",
    ("ws_gateway.py", "WsGatewayServer._cmd_unbind"): "三态判定，滤了⇒假成功",
}


def _fn_by_qualname(tree, qualname):
    """按点号限定名取函数节点（`async_entries` 的调用者可能有同名方法）。"""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        chain, cur = [node.name], node
        par = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        while par.get(cur) is not None:
            cur = par[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                chain.append(cur.name)
        if ".".join(reversed(chain)) == qualname:
            found.append(node)
    return found[0] if found else None


def _domain_entry_sites():
    """AST 取 `async_entries(DOMAIN)` 每个调用点的 (文件, 最内层限定函数名)。"""
    out = set()
    par = {}
    for p in sorted(PKG.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        par = {}
        for node in ast.walk(tree):
            for ch in ast.iter_child_nodes(node):
                par[ch] = node
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "async_entries"):
                continue
            if not n.args or ast.unparse(n.args[0]) != "DOMAIN":
                continue      # async_entries("mqtt") 是另一域，由 mqtt_bootstrap 自滤
            chain, cur = [], n
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    chain.append(cur.name)
                cur = par.get(cur)
            out.add((p.name, ".".join(reversed(chain)) if chain else "<module>"))
    return out


def test_e2_every_domain_entry_site_is_classified():
    found = _domain_entry_sites()
    declared = set(_E2_VALID_SITES) | set(_E2_EXISTENCE_SITES)
    assert len(found) >= 20, \
        "只扫到 %d 个站点：扫描器本身失效了，下面两条会假绿" % len(found)
    new = sorted(found - declared)
    gone = sorted(declared - found)
    assert not new, "新增 async_entries(DOMAIN) 站点未声明口径（E-2）：%s" % new
    assert not gone, "清单里的站点已不在源码中，需同步删除：%s" % gone


def test_e2_classification_matches_what_the_code_actually_does():
    """口径必须是实的：标「有效」的站点必须真读 disabled_by，剩下的必须全在存在性清单。"""
    for (fname, qn), why in _E2_VALID_SITES.items():
        tree = ast.parse((PKG / fname).read_text(encoding="utf-8"))
        node = _fn_by_qualname(tree, qn)
        assert node is not None, "%s::%s 取不到节点，清单需同步" % (fname, qn)
        assert "disabled_by" in ast.unparse(node), \
            "%s::%s 标了「有效」（%s）却没读 disabled_by——口径是假的" % (fname, qn, why)
    assert _domain_entry_sites() - set(_E2_VALID_SITES) == set(_E2_EXISTENCE_SITES), \
        "存在性站点集合漂移（既没被分类、也没被当作新增拦下）"


def test_e2_cmd_unbind_matched_comprehension_does_not_filter_disabled():
    """存在性那一半的形式反证（行为反证见 test_e2_unbind_distinguishes_disabled_from_deleted）。

    必须按 AST 判并用 ast.unparse：本函数为了说明"为何不过滤"，注释里必然出现
    disabled_by——任何文本钉在这里恒真，正是复核批评的那种钉。unparse 会剥掉注释，
    只剩真代码。
    """
    tree = ast.parse((PKG / "ws_gateway.py").read_text(encoding="utf-8"))
    fn = _fn_by_qualname(tree, "WsGatewayServer._cmd_unbind")
    assert fn is not None, "_cmd_unbind 改名/挪走，本钉需重写"
    matched = [t for t in ast.walk(fn)
               if isinstance(t, ast.Assign)
               and getattr(t.targets[0], "id", None) == "matched"]
    assert matched, "matched 这个中间变量不见了：三态判定被改写，本钉需重写"
    assert "disabled_by" not in ast.unparse(matched[0]), \
        "matched 推导式里出现 disabled_by：禁用条目会被算成「条目已消失」并 ack 成功"
    # 反向半条：三态判定本身（剥掉注释后的真代码）必须还在，否则上一条在空函数上恒真
    assert "disabled_by" in ast.unparse(fn), \
        "_cmd_unbind 已无禁用态真代码，只是注释在场——E-2 的反向结论失去守卫"


# ══════════════════════════ B-4 确认步的 unique_id 可达 ══════════════════════════
# 为什么必须按 AST 判：test_audit_round6::test_discovery_branch_sets_unique_id 那种
# `"async_set_unique_id(...)" in seg` 文本钉，对"调用被缩进带跑进死分支"的缺陷版
# **同样满足**（字符串在场、行为为零）。缩进错位只有语法树能看出来。


def _fn_node(name):
    src = (PKG / "config_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def _guard_body_nodes(fn):
    """`if not gateway_sn:` 那个早退守卫的体内全部节点（按 id 比，节点身份唯一）。"""
    for sub in ast.walk(fn):
        if isinstance(sub, ast.If) and ast.unparse(sub.test) == "not gateway_sn":
            return {id(x) for blk in sub.body for x in ast.walk(blk)}, sub
    return set(), None


def _expr_stmts(fn, names):
    """{语句名: 节点}——`await self.X()` 是 Expr(Await(Call))，先剥掉 Await。"""
    out = {}
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Expr):
            continue
        value = sub.value
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                and value.func.attr in names:
            out[value.func.attr] = sub
    return out


def test_b4_set_unique_id_is_not_inside_the_empty_sn_guard():
    fn = _fn_node("async_step_confirm_add")
    stmts = _expr_stmts(fn, {"async_set_unique_id"})
    assert stmts, "confirm_add 里已无 async_set_unique_id（本钉需重写）"
    inside, guard = _guard_body_nodes(fn)
    assert guard is not None, "早退守卫形态变了（本钉需重写）"
    assert id(stmts["async_set_unique_id"]) not in inside, \
        "重设 unique_id 又掉进 `if not gateway_sn` 的死体里（B-4 回潮）——该体先 return"


def test_b4_set_precedes_abort_and_both_are_outside_the_guard():
    """反向半条：两条语句都得在 if 之外，且 set 在 abort **之前**（顺序倒了 abort 判旧 uid）。"""
    fn = _fn_node("async_step_confirm_add")
    where = _expr_stmts(fn, {"async_set_unique_id", "_abort_if_unique_id_configured"})
    assert len(where) == 2, f"两条判据语句之一不见了：{sorted(where)}"
    inside, _guard = _guard_body_nodes(fn)
    for name, node in where.items():
        assert id(node) not in inside, "%s 落在早退守卫的死体里" % name
    assert where["async_set_unique_id"].lineno < where["_abort_if_unique_id_configured"].lineno, \
        "先 abort 后 set：唯一性判的是上一个 uid"


# ══════════════════════ 面板：配对窗口定时器与孤儿引用（G-1/G-2）══════════════════════
# 面板那两条只能在 node 里真跑（定时器句柄与 DOM 重建都是运行时行为）。定时器用
# 假表驱动（不靠真 sleep），断的是"第二次点击之后，第一次的收尾回调不许再拆新窗口"。

_PAIR_JS_HEAD = r"""
const ENTRY = 'cfg-1';
const GATEWAY_SN_BY_ENTRY = { [ENTRY]: 'GW-SN-1' };
const PAIRING_UNTIL = {};
const PAIRING_TIMERS = {};
const DISABLED_ENTRIES = {};
let NOW = 0;
// startPairing 用 Date.now() 算窗口时刻，假表驱动必须把时钟一起接管
Date.now = function () { return NOW; };
const timers = [];
let nextTimerId = 1;
function setTimeout(fn, ms) { const id = nextTimerId++; timers.push({ id, at: NOW + ms, fn }); return id; }
function clearTimeout(id) { const k = timers.findIndex(t => t.id === id); if (k >= 0) timers.splice(k, 1); }
function advanceTo(target) {
  NOW = target;
  for (;;) {
    const due = timers.filter(t => t.at <= NOW).sort((a, b) => a.at - b.at);
    if (!due.length) return;
    const t = due[0];
    const k = timers.indexOf(t);
    if (k < 0) return;
    timers.splice(k, 1);
    t.fn();
  }
}
const paints = [];
const rebuilds = [];
// 假 DOM 必须能区分"节点还在树上"与"容器已被整体重建"：真实现里 innerHTML 重建会让
// 旧引用成孤儿，写回无人看见。只记 id 的话，孤儿与活节点 id 完全相同 ⇒ 假件比真实现
// 窄，G-2 的缺陷形态（用 await 前抓的引用）在 harness 里"成功"（变异矩阵实测 GREEN）。
function _mkNode(id) { return { id: id, live: true }; }
function _rebuild() {
  for (const k of Object.keys(els)) els[k].live = false;
  for (const k of Object.keys(els)) delete els[k];
}
function updateGatewayStatus(el, kind) {
  paints.push({ id: el && el.id, kind, live: !!(el && el.live) });
}
function showToast() {}
function confirm() { return true; }
const els = {};
const document = { getElementById: (id) => (els[id] || (els[id] = _mkNode(id))) };
// tail 里按场景置位，让对应的真调用失败（null＝全部成功，既有场景的行为不变）
let FAIL_MODE = null;
// 第 N 次 haApi 调用时整体重建容器＝"await 期间被重建"的真形态（G-2）
let INVALIDATE_AT = 0;
let apiCalls = 0;
async function haApi(path) {
  apiCalls++;
  if (INVALIDATE_AT && apiCalls === INVALIDATE_AT) _rebuild();
  if (path.indexOf('/window_controller_gateway/devices') === 0) {
    if (FAIL_MODE === 'devices') return { ok: false, status: 500 };
    return { ok: true, status: 200, json: async () => ([{ id: 'gw_1', name: 'GW' }]) };
  }
  if (path.indexOf('/services/window_controller_gateway/start_pairing') === 0) {
    if (FAIL_MODE === 'pairing') return { ok: false, status: 500 };
    return { ok: true, status: 200 };
  }
  return { ok: false, status: 404 };
}
async function loadGatewayDevices(entryId, sn) { rebuilds.push({ at: NOW, entryId }); }
async function updateGatewayDevices(entryId, sn) {}
"""

_PAIR_JS_TAIL = r"""
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++; } }

(async function () {
  NOW = 0;
  await startPairing(ENTRY);
  want(PAIRING_UNTIL[ENTRY] === 65000, '第一次开窗时刻应为 65000: ' + PAIRING_UNTIL[ENTRY]);
  want(timers.length === 2, '第一次应留下 10s/60s 两条定时器: ' + timers.length);

  // G-2 的触发形态：第二次点击的**两次 await 中间**容器被整体重建（30s 静默刷新/
  // needRebuild 就发生在这里），函数进场时抓的 statusEl 从此成孤儿节点。
  INVALIDATE_AT = 3;

  NOW = 30000;
  await startPairing(ENTRY);
  want(PAIRING_UNTIL[ENTRY] === 95000, '第二次窗口应顺延到 95000: ' + PAIRING_UNTIL[ENTRY]);
  want(timers.length === 2,
       '第二次必须先把上一次的收尾句柄清掉（实剩 ' + timers.length + ' 条＝旧回调还在）');
  const last = paints[paints.length - 1];
  want(last && last.kind === 'pairing' && last.id === 'gw-status-' + ENTRY,
       '重建之后徽标必须写在**新**元素上: ' + JSON.stringify(last));
  // 这条才是 G-2 的正身：写在孤儿节点上＝id 一样、页面看不见。旧假件不记存活位，
  // 于是"用 await 前抓的引用"与"按 id 重取"在 harness 里不可区分（变异矩阵实测 GREEN）。
  want(last && last.live === true,
       '徽标写回了孤儿节点（live=false），用户看到的还是重建前那张卡: ' + JSON.stringify(last));

  advanceTo(61000);
  want(PAIRING_UNTIL[ENTRY] === 95000,
       't=61s 旧收尾把新窗口 delete 掉 ⇒ 配对仍生效却显示在线（G-1）: ' + PAIRING_UNTIL[ENTRY]);
  advanceTo(95001);
  want(PAIRING_UNTIL[ENTRY] === undefined, '窗口到期后必须自己收口: ' + PAIRING_UNTIL[ENTRY]);

  if (bad) { console.log('startPairing 真跑: ' + bad + ' 处不符'); process.exit(1); }
  console.log('OK');
})();
"""


def _node_run(script: str):
    node = os.environ.get("NODE_BIN") or shutil.which("node") or shutil.which("node.exe")
    assert node, "node 不可用（装 node 或设 NODE_BIN），面板行为钉无法真跑"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "pairing.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run([node, str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _extract_js_func(name: str) -> str:
    js = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
    hits = [m.start() for m in re.finditer(r"(?:async\s+)?function\s+%s\s*\(" % re.escape(name), js)]
    assert len(hits) == 1, "函数 %s 出现 %d 次（重名＝后者覆盖前者，钉会验到死码）" % (name, len(hits))
    i = hits[0]
    j = js.index("{", i)
    depth = 0
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[i:k + 1]
    raise AssertionError("函数 %s 花括号不配平（抽取锚点失效）" % name)


def test_g1_pairing_timers_really_rearm_on_second_click():
    """node 真跑：30s 后再点一次「配对」，前一次的 60s 收尾不许拆掉新窗口。"""
    body = _extract_js_func("startPairing")
    assert "PAIRING_TIMERS" in body, "startPairing 不再保存定时器句柄（G-1 回潮），本钉需重写"
    rc, out = _node_run(_PAIR_JS_HEAD + "\n" + body + "\n" + _PAIR_JS_TAIL)
    assert rc == 0 and "OK" in out, "配对窗口行为不符：\n%s" % out


def test_g1_probe_catches_the_untracked_timer_regression():
    """自变异核验：摘掉**成功路径**重入时的 clearTimeout 段，场景必须变红。

    锚点必须带上下面的赋值语句：G-1 修完后函数里有两处同样的 `if (PAIRING_TIMERS
    [entryId])` 块（abortPairing 里也有一处），只按名字匹配会删错那一处，探针于是
    "变异没落地"地全绿——正是本仓反复告诫的"钉要绑同作用域+顺序"。
    """
    body = _extract_js_func("startPairing")
    mutant = re.sub(r"if \(PAIRING_TIMERS\[entryId\]\) \{[^}]*\}(\s*)"
                    r"PAIRING_TIMERS\[entryId\] = \{",
                    r"\1PAIRING_TIMERS[entryId] = {", body, count=1)
    assert mutant != body, "变异未生效：重入清理块形态变了，本钉需重写"
    rc, out = _node_run(_PAIR_JS_HEAD + "\n" + mutant + "\n" + _PAIR_JS_TAIL)
    assert not (rc == 0 and "OK" in out), \
        "摘掉定时器清理后仍全绿：这条钉抓不住 G-1，必须重写\n%s" % out


# G-1 的另一半（复核点名"只钉了成功路径"）：两条**失败**出口也得对称收口。
# 旧写法在 !gwDevice 早退与 catch 里都无条件 delete PAIRING_UNTIL——重复点「配对」
# 而这次失败时，删掉的是**上一个仍在服务端生效**的窗口；句柄表则更糟：既不清也不删，
# 到点照样触发收尾回调去重建列表，而徽标早已回到"未知"。

_PAIR_JS_FAIL_TAIL = r"""
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++; } }

(async function () {
  // ① 全新状态下单次失败：flag 与句柄表都不得留下残渣
  NOW = 0; FAIL_MODE = 'devices';
  await startPairing(ENTRY);
  want(PAIRING_UNTIL[ENTRY] === undefined, '无窗口时失败必须不留 flag: ' + PAIRING_UNTIL[ENTRY]);
  want(PAIRING_TIMERS[ENTRY] === undefined, '无窗口时失败必须不留句柄: ' + PAIRING_TIMERS[ENTRY]);
  want(timers.length === 0, '失败路径不许留下定时器: ' + timers.length);
  want(paints[paints.length - 1].kind === 'unknown', '无窗口失败应显示未知');

  // ② 有一个仍在生效的窗口，第二次点击失败 ⇒ 上一个窗口是**真的还在配对**，不许清
  FAIL_MODE = null;
  NOW = 0;
  await startPairing(ENTRY);
  want(timers.length === 2, '前置：成功开窗应留 2 条定时器: ' + timers.length);
  FAIL_MODE = 'devices';
  NOW = 30000;
  await startPairing(ENTRY);
  want(PAIRING_UNTIL[ENTRY] === 65000,
       '失败把仍在生效的上一个窗口删了 ⇒ 配对中却显示在线: ' + PAIRING_UNTIL[ENTRY]);
  want(timers.length === 2, '活窗口的收尾句柄不许被清: ' + timers.length);
  want(paints[paints.length - 1].kind === 'pairing',
       '有活窗口时失败应回到"配对中"，不是"未知": ' + paints[paints.length - 1].kind);

  // ③ 窗口已过期后失败 ⇒ 必须彻底收口（flag + 句柄 + 两条定时器）
  FAIL_MODE = 'pairing';
  NOW = 70000;
  await startPairing(ENTRY);
  want(PAIRING_UNTIL[ENTRY] === undefined, '过期窗口在失败时必须清掉');
  want(PAIRING_TIMERS[ENTRY] === undefined, '过期窗口在失败时必须清掉句柄表');
  want(timers.length === 0,
       '清掉句柄表却漏了真定时器 ⇒ 到点仍会重建列表: ' + timers.length);
  want(paints[paints.length - 1].kind === 'unknown', '过期后失败应显示未知');

  if (bad) { console.log('startPairing 失败路径: ' + bad + ' 处不符'); process.exit(1); }
  console.log('OK');
})();
"""


def test_g1_failure_paths_are_symmetric():
    body = _extract_js_func("startPairing")
    assert "PAIRING_TIMERS" in body and "prevLive" in body, \
        "失败路径的对称收口（abortPairing/prevLive）不在了（G-1 另一半回潮）"
    rc, out = _node_run(_PAIR_JS_HEAD + "\n" + body + "\n" + _PAIR_JS_FAIL_TAIL)
    assert rc == 0 and "OK" in out, "配对失败路径行为不符：\n%s" % out


def test_g1_probe_catches_failure_path_regression():
    """两条反向变异各自必须变红：把清理做成无条件、把句柄清理摘掉。"""
    body = _extract_js_func("startPairing")
    cases = []
    m1 = re.sub(r"if \(prevLive\) \{[^}]*\}", "", body, count=1)
    assert m1 != body, "变异 1 没落地：prevLive 短路块改名了，本钉需重写"
    cases.append(("回到无条件清理（上一个活窗口被删）", m1))
    m2 = re.sub(r"if \(PAIRING_TIMERS\[entryId\]\) \{[^}]*"
                r"delete PAIRING_TIMERS\[entryId\];[^}]*\}", "", body, count=1)
    assert m2 != body, "变异 2 没落地：失败路径的句柄清理改名了，本钉需重写"
    cases.append(("失败路径不清定时器句柄", m2))
    for label, mutant in cases:
        rc, out = _node_run(_PAIR_JS_HEAD + "\n" + mutant + "\n" + _PAIR_JS_FAIL_TAIL)
        assert not (rc == 0 and "OK" in out), \
            "变异「%s」后仍全绿：这条钉抓不住 G-1 的另一半\n%s" % (label, out)


def test_g2_probe_catches_the_orphan_reference():
    """自变异核验：把 paintStatus 改回"用 await 前抓的 statusEl"，真跑钉必须变红。

    这一条是金牌复测直接教出来的：变异矩阵首轮 G-2 = GREEN，原因是假 DOM 只记 id，
    孤儿节点与重建后的活节点 id 相同 ⇒ 假件比真实现窄，两种写法在 harness 里不可区分。
    现在假件带存活位（_rebuild 把旧节点标 live=false），缺陷版必须被抓住。
    """
    body = _extract_js_func("startPairing")
    mutant = body.replace(
        "updateGatewayStatus(document.getElementById('gw-status-' + entryId), kind)",
        "updateGatewayStatus(statusEl, kind)")
    assert mutant != body, "paintStatus 的「按 id 重取」形态变了，本探针需重写"
    rc, out = _node_run(_PAIR_JS_HEAD + "\n" + mutant + "\n" + _PAIR_JS_TAIL)
    assert not (rc == 0 and "OK" in out), \
        "回到孤儿引用仍全绿：假件又变窄了，这条真跑钉失去意义\n%s" % out


def test_g2_no_stale_element_reference_after_awaits():
    """G-2 结构判据：两次 await 之后不得再把 await **之前**抓的 statusEl 写出去。

    为什么不用 node 跑这一条：孤儿引用的本质是"写到了一个已从文档上摘下来的节点"，
    假 DOM 里没有父子关系可查，跑出来只会是"元素 id 对不对"——那种断言恒真。
    按源码位置判更硬：`updateGatewayStatus(statusEl` 只允许出现在首个 await 之前。
    """
    body = _extract_js_func("startPairing")
    first_await = body.index("await haApi(")
    for m in re.finditer(r"updateGatewayStatus\(\s*statusEl", body):
        assert m.start() < first_await, \
            "await 之后仍在用 await 前抓的 statusEl（孤儿节点写回）：偏移 %s > %s" % (
                m.start(), first_await)
    assert "paintStatus" in body or "getElementById" in body[first_await:], \
        "await 之后没有任何按 id 重取的写法——G-2 的修复不在位"


# ══════════════════════ B-7 三个入口各自的真跑钉（复核【必须回炉】#1）══════════════════════
# 复核实锤：本轮只钉了 updateGatewayDevices 那一道闸，把 loadGateways 的 continue 与
# loadGatewayDevices 的入口闸**一起删掉，1270 条全绿**，而"禁用卡长出 8 颗可点按钮"
# 原样回来；唯一像守卫的 test_v1733_guards 判 `"entry.disabled_by" in JS`，由本批之前
# 就在的 :714 满足＝恒真。下面两处各配一条 node 真跑钉。

_B7_HEAD = r"""
const _els = {};
function mkEl(id) {
  return _els[id] = _els[id] || {
    id: id, innerHTML: '', textContent: '', className: '', hidden: false, children: [],
    classList: { contains: () => false },
    querySelector: () => null, querySelectorAll: () => [],
    appendChild(c) { this.children.push(c); }, setAttribute() {}
  };
}
mkEl('gatewayContainer');
// 假 DOM 必须按 id 真给出容器/状态元素：loadGatewayDevices 在 `if (!deviceListEl)
// return;` 处就会退出，桩不给元素＝反向半条永远红（假绿的反面：假红）。
const document = {
  getElementById: (id) => _els[id] || (id.indexOf('gatewayContainer') >= 0 ? mkEl(id) : null),
  querySelector: () => null, querySelectorAll: () => []
};
const GATEWAY_SN_BY_ENTRY = {};
const DISABLED_ENTRIES = {};
const PAIRING_UNTIL = {};
// DOMAIN/INGRESS_BASE 是真函数体里要读的模块级常量：桩不给 ⇒ loadGateways 的
// catch 会吞掉 ReferenceError，测试只在断言处报"条目没被处理"，看不出是假件缺位。
const DOMAIN = 'window_controller_gateway';
const INGRESS_BASE = '/api/hassio/app/entries/';
const loaded = [];
const calls = [];
async function haApi(path) {
  calls.push(path);
  return { ok: true, status: 200, json: async () => ENTRIES };
}
let ENTRIES = [];
function escapeHtml(s) { return String(s); }
function jsAttr(s) { return String(s).replace(/'/g, "\'"); }
function renderGateway(name, sn, entryId) { return '<div class="gateway-item" id="gw-' + entryId + '"></div>'; }
function renderGatewayDisabled(name, sn, entryId) { return '<div class="gateway-item" id="gw-' + entryId + '"></div>'; }
async function loadGatewayDevices(entryId, sn) { loaded.push(entryId); }
function deviceSnOf() { return null; }
function findEntityByUniqueId() { return null; }
function updateGatewayStatus() {}
function loadDeviceState() {}
"""

_B7_TAIL = r"""
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++; } }

(async function () {
  // 一条禁用条目 + 一条正常条目
  ENTRIES = [
    { entry_id: 'e-off', state: 'loaded', disabled_by: 'user', data: { gateway_sn: 'GW1' }, title: 'A' },
    { entry_id: 'e-on', state: 'loaded', disabled_by: null, data: { gateway_sn: 'GW2' }, title: 'B' }
  ];
  loaded.length = 0; calls.length = 0;
  await loadGateways();
  // 假件缺全局时 loadGateways 的 catch 会把它咽下去，于是下面三条断言全以
  // "条目没被处理"的形态失败——先钉住"确实走进了正常路径"，否则红的是假件不是产品。
  want(/无法连接 HA API|无权直接读取|响应超时/.test(_els['gatewayContainer'].innerHTML) === false,
       'loadGateways 走进了 catch（多为假件缺全局），其余断言不可信');
  want(loaded.length === 1 && loaded[0] === 'e-on',
       '禁用条目不得被喂进设备刷新循环（实得 ' + JSON.stringify(loaded) + '）');
  want(DISABLED_ENTRIES['e-off'] === true, '渲染时必须登记禁用集合，后续刷新才拦得住');
  want(DISABLED_ENTRIES['e-on'] === undefined, '正常条目不得被误登记（反向半条）');
  if (bad) { console.log('B-7: ' + bad + ' 处不符'); process.exit(1); }
  console.log('OK');
})();
"""

_B7_GUARD_TAIL = r"""
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++; } }
(async function () {
  const ENTRY = 'e-off';
  mkEl('devices-' + ENTRY); mkEl('gw-status-' + ENTRY);
  DISABLED_ENTRIES[ENTRY] = true;
  calls.length = 0;
  await loadGatewayDevices(ENTRY, 'GW1');
  want(calls.length === 0,
       '禁用条目的设备刷新必须整轮跳过（实打 ' + calls.length + ' 次 HA API）');
  delete DISABLED_ENTRIES[ENTRY];
  calls.length = 0;
  await loadGatewayDevices(ENTRY, 'GW1');
  want(calls.length >= 1, '反向半条：未禁用的条目必须照常打 /devices，否则上一条恒真');
  if (bad) { console.log('B-7b: ' + bad + ' 处不符'); process.exit(1); }
  console.log('OK');
})();
"""


def test_b7_loadgateways_loop_skips_disabled_entries():
    body = _extract_js_func("loadGateways")
    assert "DISABLED_ENTRIES" in body, "loadGateways 的禁用登记/跳过丢了（B-7 回潮）"
    rc, out = _node_run(_B7_HEAD + "\n" + body + "\n" + _B7_TAIL)
    assert rc == 0 and "OK" in out, "loadGateways 真跑不符：\n%s" % out


def test_b7b_loadgatewaydevices_entry_guard():
    body = _extract_js_func("loadGatewayDevices")
    assert "if (DISABLED_ENTRIES[entryId]) return;" in body, \
        "loadGatewayDevices 入口闸丢了——这才是拦住三条刷新路径的那一道（B-7）"
    rc, out = _node_run(_B7_HEAD + "\n" + body + "\n" + _B7_GUARD_TAIL)
    assert rc == 0 and "OK" in out, "loadGatewayDevices 真跑不符：\n%s" % out


def test_b7_probe_catches_the_guard_removal():
    """自变异核验：复核给的那条形变——两道闸**各自**摘掉后，本批钉必须变红。

    写成循环是为了让"哪一道闸失去抓手"直接落在断言消息里，而不是只报一条泛红。
    """
    cases = [
        ("loadGatewayDevices", "if (DISABLED_ENTRIES[entryId]) return;",
         _B7_GUARD_TAIL, "入口闸（:807）"),
        ("loadGateways", "if (DISABLED_ENTRIES[entry.entry_id]) continue;",
         _B7_TAIL, "重建循环的 continue（:737）"),
        ("loadGateways", "DISABLED_ENTRIES[entry.entry_id] = true;",
         _B7_TAIL, "禁用集合的登记（:718）"),
    ]
    for fn, line, tail, label in cases:
        body = _extract_js_func(fn)
        assert line in body, "%s 已不在 %s 里：本探针需同步重写" % (label, fn)
        mutant = body.replace(line, "", 1)
        rc, out = _node_run(_B7_HEAD + "\n" + mutant + "\n" + tail)
        assert not (rc == 0 and "OK" in out), \
            "摘掉%s后仍全绿：B-7 的钉抓不住这一道的回潮\n%s" % (label, out)
