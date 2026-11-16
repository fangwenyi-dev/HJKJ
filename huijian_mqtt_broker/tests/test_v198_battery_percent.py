# -*- coding: utf-8 -*-
"""v1.9.8 钉桩：面板电池显示改百分比（用户定案 10.5V=0% / 12.6V=100%）。

用户 2026-11-16 原话：「我感觉 web 界面中现在显示的开窗器子设备为电压数值，应该改成
百分比。如 10.5v 对应着 0%，12.6v 以上对应 100%，这样以百分比的形式显示剩余电池电量，
更加容易说明电压状态。」

本文件钉四件事：

1) **换算本身**（node 真跑 `batteryPercent`）：锚点、区间内线性、两端钳制。
   钳制是硬要求而不是好看：满电涓流会到 13V+、欠压会跌破 10V，线性外推会给出
   -14% / 119% 这种自相矛盾的读数。
2) **非数值不得变成 0%**：`null` / `''` / `undefined` 走 `Number()` 全是 0，若不
   先挡，就会把「本轮没取到数据」渲染成「电池已耗尽」——这是会让客户报障的假告警。
   `"abc"`→NaN、`"1e999"`→Infinity（与后端 A-HIGH2 同族畸形输入）一律回 null。
3) **单一展示出口**：`batteryTextOf` 必须同时覆盖电池实体与 `cover.attributes.voltage`
   兜底；只改主路径会把原始伏数从兜底那半条漏回用户眼前。渲染两处（首屏占位
   `renderDevice` 与刷新 `loadDeviceState`）都必须走这个出口，不许各拼一份。
4) **反钉**：用户可见文案里不许再出现「电压: 」，且面板 JS 里不许再有
   `unit_of_measurement || 'V'` 这种把伏数直接拼进状态串的写法。

只改展示口径，**不动 HA 实体语义**（sensor 仍是 device_class=voltage 的诊断实体），
这一点也钉住——防止后来者为了面板文案去改 sensor 的 unit。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
SENSOR = (ROOT / "custom_components" / "window_controller_gateway" / "sensor.py").read_text(encoding="utf-8")


def _js_body(name):
    """按名抽函数体；0 处或多处都报错（重名等于后者覆盖前者，钉会验到死码）。"""
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
    raise AssertionError("函数 %s 花括号不配平" % name)


def _const(name):
    """取模块级数值常量（换算锚点必须从实现取，测试里不再抄一份）。"""
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*([-\d.]+)\s*;", JS)
    assert m, "huijian.js 里找不到常量 %s" % name
    return float(m.group(1))


def _run_node(script):
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑换算")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "battery.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0, "node 真跑不符：\n%s%s" % (r.stdout, r.stderr)


def _harness(extra):
    return ("const BATTERY_MIN_V = %r;\nconst BATTERY_MAX_V = %r;\n"
            % (_const("BATTERY_MIN_V"), _const("BATTERY_MAX_V"))
            + _js_body("batteryPercent") + "\n" + _js_body("batteryTextOf") + "\n" + extra)


# ══════════════════ 1) 换算：锚点 / 线性 / 钳制 ══════════════════
def test_anchor_voltages_map_to_0_and_100_percent():
    """用户给的两个锚点必须**逐值**成立（10.5V→0%，12.6V→100%）。"""
    _run_node(_harness("""
if (batteryPercent(10.5) !== 0) { console.log('FAIL 10.5V 应为 0%, 实得 ' + batteryPercent(10.5)); process.exit(1) }
if (batteryPercent(12.6) !== 100) { console.log('FAIL 12.6V 应为 100%, 实得 ' + batteryPercent(12.6)); process.exit(1) }
console.log('OK');
"""))


def test_linear_inside_the_window():
    """区间内线性，且必须四舍五入而不是截断。

    精确中点 11.55V 走浮点会落在 49.9999…，取整后仍是 50——这条正是"别在边界
    上做精确相等"的提醒，所以另外两条用**明确偏上/偏下**的值验取整方向：
    11.57V=50.95% 必须给 51（截断会给 50）、11.03V=25.24% 必须给 25。
    """
    _run_node(_harness("""
const mid = batteryPercent(11.55);
if (mid !== 50) { console.log('FAIL 11.55V 应为 50%, 实得 ' + mid); process.exit(1) }
const up = batteryPercent(11.57);      // 50.95% ⇒ 51（截断实现会给 50）
if (up !== 51) { console.log('FAIL 11.57V 应向取整到 51%, 实得 ' + up); process.exit(1) }
const down = batteryPercent(11.03);    // 25.24% ⇒ 25
if (down !== 25) { console.log('FAIL 11.03V 应为 25%, 实得 ' + down); process.exit(1) }
console.log('OK');
"""))


def test_out_of_range_clamps_instead_of_going_negative_or_over_100():
    """13V+ 涓流与 10V 以下欠压都必须贴边——负电量/超 100% 是会让客户报障的读数。"""
    _run_node(_harness("""
const cases = [[13.0, 100], [13.4, 100], [14.0, 100], [10.0, 0], [9.5, 0], [0, 0], [-5, 0]];
for (const [v, want] of cases) {
  const got = batteryPercent(v);
  if (got !== want) { console.log('FAIL ' + v + 'V 应为 ' + want + '%, 实得 ' + got); process.exit(1) }
}
console.log('OK');
"""))


def test_missing_or_malformed_voltage_yields_no_percent_at_all():
    """"读不到"不许渲染成 0%（会把没数据说成电池耗尽），畸形值不许拼出 NaN%。

    这一条是本文件最要紧的一半：`Number(null)===0`、`Number('')===0` 是 JS 的
    既有语义，漏挡就是一条静默的假告警。
    """
    _run_node(_harness("""
const bad = [null, undefined, '', 'abc', '1e999', '-1e999', NaN, Infinity, -Infinity];
for (const v of bad) {
  const got = batteryPercent(v);
  if (got !== null) { console.log('FAIL ' + String(v) + ' 应回 null, 实得 ' + got); process.exit(1) }
}
// 字符串数字（HA /states 里的 state 恒为字符串）必须照常换算
if (batteryPercent('12.3') !== 86) { console.log('FAIL "12.3" 应为 86%, 实得 ' + batteryPercent('12.3')); process.exit(1) }
console.log('OK');
"""))


# ══════════════════ 2) 单一展示出口：实体优先 + cover 兜底 ══════════════════
def test_display_exit_converts_both_the_entity_and_the_cover_fallback():
    """两条取值路径都必须过换算。

    兜底那条（`cover.attributes.voltage`）是旧版第二个"电压: xV"写点，只改主路径
    的话，没装电池传感器实体的机型会继续看到伏数——改口径漏掉半条等于没改。
    """
    _run_node(_harness("""
const ent = (v) => ({ state: v, attributes: { unit_of_measurement: 'V' } });
const cover = (v) => ({ attributes: { voltage: v } });
if (batteryTextOf(ent('12.6'), null) !== '电量: 100%') {
  console.log('FAIL 实体主路径: ' + batteryTextOf(ent('12.6'), null)); process.exit(1) }
if (batteryTextOf(null, cover(10.5)) !== '电量: 0%') {
  console.log('FAIL cover 兜底: ' + batteryTextOf(null, cover(10.5))); process.exit(1) }
// 实体在但脏（unknown/unavailable）⇒ 回落 cover，而不是显示 0%
if (batteryTextOf(ent('unknown'), cover('12.3')) !== '电量: 86%') {
  console.log('FAIL 脏实体应落回 cover: ' + batteryTextOf(ent('unknown'), cover('12.3'))); process.exit(1) }
// 两边都读不到 ⇒ 空串（调用点据此**整段不拼**，而不是拼一个"电量: --"）
if (batteryTextOf(ent('unavailable'), cover(undefined)) !== '') {
  console.log('FAIL 读不到应为空串: ' + batteryTextOf(ent('unavailable'), cover(undefined))); process.exit(1) }
if (batteryTextOf(null, null) !== '') { console.log('FAIL 双 null 应为空串'); process.exit(1) }
console.log('OK');
"""))


# ══════════════════ 3) 接线：两处渲染都走同一出口 ══════════════════
def test_render_device_uses_the_shared_exit():
    body = _js_body("renderDevice")
    assert "batteryTextOf(" in body, "renderDevice 首屏占位没走统一出口"
    assert "unit_of_measurement || 'V'" not in body, \
        "renderDevice 还在自己拼伏数（改口径漏掉这一处＝首屏与刷新后两种文案并存）"


def test_load_device_state_uses_the_shared_exit():
    body = _js_body("loadDeviceState")
    assert "batteryTextOf(" in body, "loadDeviceState 刷新出口没走统一出口"
    assert "unit_of_measurement || 'V'" not in body, \
        "loadDeviceState 还在自己拼伏数"
    # 兜底分支的判据必须留在出口里，不能被"顺手简化"删掉：没电池实体的机型
    # 只能靠 cover 属性，删了它们就永远看不到电量。
    assert "coverEntity.attributes.voltage" in _js_body("batteryTextOf"), \
        "batteryTextOf 丢了 cover.attributes.voltage 兜底（无电池实体的机型会没电量）"


def _js_code_only(src):
    """去掉注释，只留代码：注释里描述"旧版长这样"是有效信息，不该被反钉打死。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


def test_no_raw_voltage_wording_left_in_the_panel():
    """反钉：面板**拼给用户的字符串**里不许再有「电压」那套伏数文案。

    判据只看代码区、且只看字符串字面量：源码注释里说明实体来源或旧行为是在
    留证据，扫全文会逼人删注释（同 test_audit_2026_10_20 的窄名单思路）。
    """
    code = _js_code_only(JS)
    quoted = [s for _, s in re.findall(r"""(['"])(.*?)\1""", code, re.S)]
    offenders = [s for s in quoted if "电压: " in s or "电压：" in s]
    assert not offenders, "面板仍在给用户看电压伏数（用户定案改成百分比）：%s" % offenders[:3]
    # `'V'` 作为展示单位不许再拼到状态串上（换算口径只走 batteryTextOf）
    leftovers = [ln for ln in code.splitlines()
                 if re.search(r"unit_of_measurement\s*\|\|\s*'V'", ln)]
    assert not leftovers, "还有把伏数当展示值的残留：%s" % leftovers[:3]
    # 正钉：用户要看到的是「电量: N%」
    assert "'电量: ' + pct + '%'" in JS, "面板没有输出「电量: N%」这套口径"


# ══════════════════ 4) 只改展示，不改实体语义 ══════════════════
def test_ha_sensor_entity_still_reports_volts():
    """面板换百分比**不是**让 sensor 改语义：诊断/自动化仍要伏数。

    这条是给后来者的路标——把 sensor 的 unit 改成 % 会让 HA 历史曲线、
    device_class=voltage 和既有测试全部失配，且用户并没有要求动实体。
    """
    assert "SensorDeviceClass.VOLTAGE" in SENSOR, \
        "sensor.py 的电池实体不再声明 voltage 设备类（面板文案需求不该改实体语义）"
    assert 'VOLTAGE' in SENSOR
