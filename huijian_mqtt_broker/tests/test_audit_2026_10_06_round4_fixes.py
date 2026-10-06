# -*- coding: utf-8 -*-
"""第四轮独立复核（docs/verify-2026-10-06-v1764.md）四条遗留项的收口回归。

那份复核判"本批 14/14 真修、新增产品缺陷 0"，另登记 4 条"非本批引入"的遗留项。
我逐条自证后**全部为真**，其中 N4 两条是真能炸的产品缺陷（只是早于本批存在）：

  N4a `number.async_set_native_value` 的 `int(value)` 无任何闸——自动化 YAML 写
      `.inf` 抛 OverflowError、`.nan` 抛 ValueError ⇒ 服务调用当场炸穿。同文件
      :122 的设定值回显早在 v1.7.61 A-6 就接住了这两个，入参侧是同族漏网（第 7 处）。
      修法：如实拒（`HomeAssistantError`），不回退默认档（v1.6.19 B-LOW11 同判）。
  N4b `verify_builtin_channel` 的 `int(endpoint["port"])` 在 try 之外——端点文件被写
      成 `1e999`（JSON 合法→inf）时抛 OverflowError 逃出判定面。同函数 :312 的**条目
      侧**早在 v1.7.61 S2 就接住了，端点侧漏（第 8 处）。现状会被 healer/repairs 的
      宽兜底折成 probe_error/仍坏——**不崩但判词不准**（真坏的是端点文件）。
      修法：按 C-7 口径归 `endpoint_broken`（不清卡，卡片能指到端点文件）。
  N1  C-6 判据放过裸 `except:`（`h.type is None: continue`）——那是最宽的吞；
      CI 的 ruff 参数（F,E9,B）不含 E722 拦不住 ⇒ 潜在盲区。修法：裸 except 与
      `except BaseException` 一并纳入命中，配形状臂。
  N2  `_set_op_error` 体内兜底常量 `op_failed` 既不在派生宇宙也不在面板码表——
      将来某调用点传空就是一次"点了没反应"。修法：派生宇宙收函数体兜底常量
      （并给该分支加"必须真的贡献码"的死分支自证——上一版把它挂在 `continue`
      之后，加了等于没加还全绿），面板补文案。
"""
import ast
import asyncio
import pathlib
import re
import shutil
import subprocess
import tempfile
import types

import pytest

from homeassistant.exceptions import HomeAssistantError  # conftest 装的假件

from custom_components.window_controller_gateway import mqtt_bootstrap as mb
from custom_components.window_controller_gateway import number as num
from custom_components.window_controller_gateway.const import SPEED_MAX, SPEED_MIN

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")

# ═════════════════════════ N4a · number 入参闸 ═════════════════════════

class _NullDM:
    def get_device(self, sn):
        return None


def _number():
    return num.WindowControllerSpeedNumber(
        hass=None, device_manager=_NullDM(), mqtt_handler=None,
        gateway_sn="GW1", device_sn="50063420020A", device_name="窗")


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", [float("inf"), float("-inf"), float("nan"), "abc", None])
async def test_set_native_value_rejects_junk_instead_of_crashing(junk):
    """旧形态：`int(inf)` 抛 OverflowError、`int(nan)`/`int("abc")` 抛 ValueError，
    全部原样逃给服务调用方。修后必须是 HomeAssistantError（HA 会转成服务报错），
    而且异常链保留原因（`from err`）。"""
    ent = _number()
    with pytest.raises(HomeAssistantError) as e:
        await ent.async_set_native_value(junk)
    assert not isinstance(e.value.__cause__, (KeyError, AttributeError))
    assert e.value.__cause__ is not None, "异常链要留住原始原因（排障要看得到是 inf 还是 abc）"


@pytest.mark.asyncio
async def test_set_native_value_still_clamps_valid_input():
    """反向半条：闸不许把正常路径一起挡掉。150 仍钳到 SPEED_MAX、-5 钳到 SPEED_MIN。"""
    ent = _number()
    ent.async_write_ha_state = lambda: None
    ent.hass = types.SimpleNamespace(loop=asyncio.get_running_loop(),
                                     async_create_task=lambda f: None)
    try:
        await ent.async_set_native_value(150)
        assert ent._pending_value == SPEED_MAX
        await ent.async_set_native_value(-5)
        assert ent._pending_value == SPEED_MIN
        await ent.async_set_native_value(30)
        assert ent._pending_value == 30
    finally:
        if ent._debounce_handle:
            ent._debounce_handle.cancel()


# ═════════════════════════ N4b · 端点文件 port 闸 ═════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_port", [float("inf"), float("nan"), "abc", None, {}, "1e999"])
async def test_endpoint_bad_port_is_endpoint_broken_not_escape(bad_port, monkeypatch):
    """端点文件 port 不可解析 ⇒ 归 endpoint_broken（不清卡、判词指到端点文件）。
    旧实现：`int(inf)` 抛 OverflowError 逃出本函数，被 healer 的宽兜底折成
    probe_error、被 repairs 折成"仍坏"——不崩，但把"端点文件坏了"说成"没探到"。"""
    async def fake_probe(_hass):
        return "ok", {"broker": "127.0.0.1", "port": bad_port}

    monkeypatch.setattr(mb, "_probe_endpoint", fake_probe)
    hass = types.SimpleNamespace(data={}, config_entries=types.SimpleNamespace(
        async_entries=lambda d: []))
    assert await mb.verify_builtin_channel(hass) == "endpoint_broken"


@pytest.mark.asyncio
async def test_endpoint_valid_port_still_proceeds(monkeypatch):
    """反向半条：正常 port 不许被闸误伤成 endpoint_broken（那会误报"端点坏"）。"""
    async def fake_probe(_hass):
        return "ok", {"broker": "127.0.0.1", "port": "2022"}

    # 产品侧是**同步**调用 async_entries（HA 的这个名字有历史误导性）
    monkeypatch.setattr(mb, "_probe_endpoint", fake_probe)
    hass = types.SimpleNamespace(data={}, config_entries=types.SimpleNamespace(
        async_entries=lambda d: []))
    assert await mb.verify_builtin_channel(hass) == "no_entry", \
        "port 正常时必须继续往下走（判到条目面），不能被新闸截住"


# ═════════════════════ N1 · C-6 判据形状臂（含裸 except / BaseException） ═════════════════

def _c6_hits(src):
    from test_audit_2026_09_30_fixes import _c6_offenders_in_tree
    return _c6_offenders_in_tree(ast.parse(src), set())


@pytest.mark.parametrize("src,expect_hit", [
    # 吞取消（既有四臂之外的新面）：裸 except 是最宽的吞
    ("async def cleanup(self):\n    try:\n        await t\n    except:\n        pass\n", True),
    # BaseException 同样能捕到 CancelledError（3.8+ 它继承 BaseException）
    ("async def cleanup(self):\n    try:\n        await t\n"
     "    except BaseException:\n        pass\n", True),
    # 捕了再 raise（含条件再抛）＝合法收口
    ("async def cleanup(self):\n    try:\n        await t\n"
     "    except BaseException:\n        raise\n", False),
    ("async def cleanup(self):\n    try:\n        await t\n"
     "    except:\n        if not ok:\n            raise\n", False),
    # 与取消无关的窄捕不许被牵连
    ("async def cleanup(self):\n    try:\n        await t\n"
     "    except ValueError:\n        pass\n", False),
])
def test_c6_detector_covers_bare_and_base_exception_forms(src, expect_hit):
    hits = _c6_hits(src)
    assert bool(hits) is expect_hit, \
        "形态 %r 判据给 %s，期望 %s" % (src[:52], hits, "报" if expect_hit else "不报")


def test_c6_bare_except_arm_is_not_vacuous():
    """裸 except 那条臂不许是"看起来红"：命中描述必须点名是裸 except，
    而不是靠别的 handler 蒙对。"""
    hits = _c6_hits("async def cleanup(self):\n    try:\n        await t\n"
                    "    except:\n        pass\n")
    assert len(hits) == 1 and "裸 except" in hits[0], hits


# ═════════════════════ N2 · op_failed 面板文案（node 真跑） ═════════════════

def _js_body(name):
    hits = [m.start() for m in re.finditer(r"function\s+" + re.escape(name) + r"\s*\(", JS)]
    assert len(hits) == 1, "函数 %s 出现 %d 次" % (name, len(hits))
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
    raise AssertionError("花括号不配平")


def test_op_failed_code_renders_copy_in_node():
    """`op_failed` 是 `_set_op_error` 的兜底值：它天生"没原因"，所以文案必须承认
    这点并把人指到 HA 日志；未知码仍须回空串（conn/op 拆分钉的口径不破）。"""
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑映射出口")
    script = _js_body("hubOpErrorText") + """
const document = {};
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++ } }
const t = hubOpErrorText('op_failed');
want(t.length > 0, 'op_failed 必须有文案（未知码走 default 回空串＝点了没反应）');
want(/原因|日志/.test(t), '文案要承认"没给出原因"并指到日志，不能假称网络问题: ' + t);
want(hubOpErrorText('bad_body').length > 0, 'bad_body 文案不得被本批改坏');
want(hubOpErrorText('zzz_nope') === '', '未知码仍须回空串');
if (bad) { console.log('hubOpErrorText 真跑: ' + bad + ' 处不符'); process.exit(1) }
console.log('OK');
"""
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "t.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0 and "OK" in r.stdout, \
            "op_failed 真跑失败：\n%s%s" % (r.stdout, r.stderr)
