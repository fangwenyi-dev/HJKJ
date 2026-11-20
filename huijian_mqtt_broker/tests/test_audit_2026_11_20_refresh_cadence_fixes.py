"""2026-11-20 用户裁定只修 ①②③…里的 ①③——刷新节奏可感缺陷。

用户原话（只读审计后的处置令）：
  ① 运动过程中刷新明显跟不上（用户可感）
  ③ 从后台标签切回来没有立即刷新

两条都是**同一根轴**上的病：面板只有一条 30s 的浏览器轮询，物理行程却只有
10~15s ⇒ 命令过去那一次 +2s 采样之后，位置要等最多 30s 才更新（①）；后台
标签被 `document.hidden` 跳过的那几轮也不会补，切回来看见的是上次数分钟前的
快照，而它和实时数据长得一模一样（③）。

本文件钉住修复的**可感面**，不是钉实现细节：
  · ①命令后开"落定期"快刷窗口：窗口内按 SETTLE_STEP_MS 取位置，窗口一到
    **自动停摆**（不许把 30s 基准换成永远 2s —— 那等于把后台空转纪律废掉）；
  · ①两条控制路径都要武装（`controlDevice` 与 `controlDevicePosition`；后
    者修复前**一次刷新都没有**），删除命令不武装（设备接下来要消失）；
  · ③切回前台补一轮 + 先把"更新于 hh:mm／已过期"摆出来，别让陈旧进度冒充现状。

行为钉真跑：node 里把 6 个新函数抽出来执行，用可控时钟与假 setTimeout 走完
整个落定期，核对"每 2s 一刷 → 到点停 → 后台不空转 → 切回接得上"。
"""
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
INDEX = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "www" / "css" / "huijian.css").read_text(encoding="utf-8")

NEW_FUNCS = ["armSettleWindow", "settleTick", "markPanelSynced",
             "renderSyncTip", "kickOnReturn"]


def _node():
    """node 可执行文件：优先 NODE_BIN，其次 PATH（与 v1755 那份同规矩）。"""
    import os
    import shutil
    for cand in (os.environ.get("NODE_BIN"), shutil.which("node"), shutil.which("node.exe")):
        if cand:
            return cand
    raise AssertionError("node 不可用，无法真跑落定期快刷（装 node 或设 NODE_BIN）")


def _body(name):
    """按大括号配平抽出唯一那个 `function name(` 的定义。"""
    hits = [m.start() for m in re.finditer(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", JS)]
    assert len(hits) == 1, \
        "函数 %s 出现 %d 次（应为 1；重名＝后者覆盖前者，钉会验到死码）" % (name, len(hits))
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
    raise AssertionError("函数 %s 花括号不配平（解析锚点失效）" % name)


def _decls():
    """把落定期/新鲜度那几个状态声明**按原文**抽出来（`let` 不可改成 const）。"""
    pats = [
        r"const SETTLE_STEP_MS = \d+;",
        r"const SETTLE_SPAN_MS = \d+;",
        r"const SETTLE_FAST_UNTIL = \{\};",
        r"let _settleTimer = null;",
        r"let _panelSyncedAt = 0;",
        r"let _lastKickAt = 0;",
    ]
    out = []
    for p in pats:
        m = re.search(p, JS)
        assert m, "落定期/新鲜度状态声明缺失或改形（%s）——桩拼不起来，钉等于没钉" % p
        out.append(m.group(0))
    return "\n".join(out)


def _run_node(script):
    node = _node()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settle_window.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run([node, str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, "node 真跑失败：\nSTDOUT%s\nSTDERR%s" % (r.stdout, r.stderr)
    assert "OK" in r.stdout, "node 真跑没走到通过标记：\n%s%s" % (r.stdout, r.stderr)
    return r.stdout


# ══ 0) 元钉：钉与被测对象都还在 ══════════════════════════════════════
def test_extracted_functions_are_real_and_unique():
    """5 个新函数都必须是 `function name(` 单行定义（v1.7.46 抽取正则的形状）。"""
    for name in NEW_FUNCS:
        assert re.search(r"^[ \t]*function[ \t]+" + re.escape(name) + r"[ \t]*\(", JS, re.M), \
            "%s 不是行首单行函数定义——面板抽取正则会漏掉它（重名/死码钉随之假绿）" % name


def test_settle_cadence_is_meaningfully_faster_than_the_baseline():
    """窗口内步长短于 30s 基准，且窗口长度装得下多次采样（否则"修复"等于没修）。"""
    step = int(re.search(r"const SETTLE_STEP_MS = (\d+);", JS).group(1))
    span = int(re.search(r"const SETTLE_SPAN_MS = (\d+);", JS).group(1))
    assert step < 30000, "落定期步长 %dms 不比 30s 基准快——①的「跟不上」没被修" % step
    assert step >= 500, "步长 %dms 太密，会把 HA 打成每秒若干次全量 /states" % step
    assert span > step * 3, "窗口 %dms 只够 %d 次采样，行程走完之前就停摆了" % (span, span // step)


def test_no_new_setinterval_is_introduced():
    """不许用 setInterval 做落定期：自续 setTimeout 才能在窗口到点后**真的停**。"""
    assert JS.count("setInterval(") == 2, \
        "setInterval 数量变了（应仍是 init 内 30min 更新检查 + 30s 轮询两条）：%d" % JS.count("setInterval(")
    for name in NEW_FUNCS:
        assert "setInterval" not in _body(name), \
            "%s 里出现 setInterval——一个永不停摆的 2s 定时器会废掉隐藏页不空转的纪律" % name


# ══ 1) ① 两个武装点 ════════════════════════════════════════════════
def test_settle_window_is_armed_on_both_control_paths():
    ctl = _body("controlDevice")
    pos = _body("controlDevicePosition")
    assert "armSettleWindow(entryId)" in ctl, \
        "controlDevice 下发命令后不武装落定期——开/关/停之后位置要等 30s 才动（①原样）"
    assert "armSettleWindow(entryId)" in pos, \
        "controlDevicePosition 不武装落定期——一条定位命令下发后页面完全不动（修复前它连一次刷新都没有）"


def test_remove_command_does_not_arm_the_settle_window():
    """删除命令不武装：设备接下来要消失，没有行程值得以 2s 粒度看。"""
    ctl = _body("controlDevice")
    assert "if (command !== 'remove') armSettleWindow(entryId);" in ctl, \
        "删除命令也去武装落定期了——那 24s 里每 2s 刷一台正在被删的设备，纯空转"


def test_settle_path_never_rebuilds_the_dom():
    """反半条：落定期只走无感更新，绝不整建（否则每次命令都闪一下整页）。"""
    for name in ("armSettleWindow", "settleTick"):
        body = _body(name)
        for forbidden in ("loadGateways", "loadGatewayDevices", "innerHTML"):
            assert forbidden not in body, \
                "%s 里出现 %s——落定期整建会把 2s 一刷变成 2s 一闪（v1.7.5 手机端闪屏教训）" % (name, forbidden)
    assert "updateGatewayDevices(" in _body("settleTick"), \
        "settleTick 不走无感更新出口，那它到底刷了什么？"


def test_settle_tick_skips_hidden_tabs_and_does_not_self_perpetuate():
    """两条纪律同时钉：隐藏页不空转；窗口到点后**不再自续**。"""
    tick = _body("settleTick")
    assert "document.hidden" in tick, "settleTick 不判 document.hidden——后台标签每 2s 空转"
    assert "SETTLE_FAST_UNTIL[id] > now" in tick, \
        "settleTick 丢了窗口过期判定——那它就是个永不停摆的 2s 轮询"

    # 真跑：窗口到点后队列必须清空（不是无限自续）
    _run_node(_decls() + """
const timers = [];
function setTimeout(fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; }
let _hidden = false;
const els = { panelSyncTip: { textContent: '', className: '' } };
const document = { get hidden() { return _hidden; },
                   getElementById: (id) => els[id] || null };
const updates = [];
function updateGatewayDevices(entryId, sn) { updates.push([entryId, sn]); }
const GATEWAY_SN_BY_ENTRY = { e1: 'SN-1' };
let now = 1000000;
Date.now = () => now;
let _silentRefreshing = false;
""" + "\n".join(_body(n) for n in NEW_FUNCS) + """
let bad = [];
function want(cond, msg) { if (!cond) bad.push(msg); }
function drain(limit) {
  let n = 0;
  while (timers.length && n < limit) {
    const t = timers.shift();
    now += t.ms;
    n++;
    t.fn();
  }
  return n;
}

armSettleWindow('e1');
want(timers.length === 1, '武装后应恰有一个待触发的 tick: ' + timers.length);
want(timers[0].ms === SETTLE_STEP_MS, '首次 tick 间隔不是 SETTLE_STEP_MS: ' + timers[0].ms);
drain(500);
want(updates.length >= 3, '整个落定期只刷了 ' + updates.length + ' 次（2s 粒度没生效）');
want(updates.every(u => u[0] === 'e1'), '落定期刷到了没武装的条目: ' + JSON.stringify(updates));
want(timers.length === 0, '窗口到点后仍在自续——这是个永不停摆的 2s 轮询');
const settled = updates.length;
drain(500);
want(updates.length === settled, '窗口结束后又刷了：' + (updates.length - settled));

// 后台标签：不空转，也不自续
updates.length = 0;
timers.length = 0;
now += 100000;
armSettleWindow('e1');
_hidden = true;
drain(500);
want(updates.length === 0, '隐藏页仍在落定期刷设备（' + updates.length + ' 次）——后台空转');
want(timers.length === 0, '隐藏页仍在自续 tick');

// 切回前台：把停掉的链接回来（否则运动中途切走再切回，这段行程又退回 30s 一格）
_hidden = false;
let refreshed = 0;
function silentRefresh() { refreshed++; }
now += 1000;
kickOnReturn();
want(refreshed === 1, '切回前台没有补一轮刷新: ' + refreshed);
want(timers.length === 1, '切回前台没把落定期链接回来: ' + timers.length);
drain(500);
want(updates.length >= 1, '接回来的落定期不刷设备');

if (bad.length) { console.log('落定期真跑不符: ' + bad.join(' / ')); process.exit(1); }
console.log('OK');
""")


# ══ 2) ③ 切回前台 + 新鲜度提示 ═════════════════════════════════════
def test_returning_to_foreground_registers_both_listeners():
    """visibilitychange 与 focus 都要挂（有的环境只发其中一个），且只挂一次。"""
    assert JS.count("document.addEventListener('visibilitychange', kickOnReturn);") == 1, \
        "面板没监听 visibilitychange——从后台标签切回来不会立刻刷新（③原样）"
    assert JS.count("window.addEventListener('focus', kickOnReturn);") == 1, \
        "没监听 focus——切回应用（未触发 visibilitychange 的环境）仍不刷新"


def test_kick_on_return_dedupes_and_shows_staleness_first():
    """切回时先摆"刚才那段是旧的"再刷；visibilitychange+focus 成对到达只补一轮。"""
    body = _body("kickOnReturn")
    assert "renderSyncTip()" in body, "切回时没先重画新鲜度提示——用户第一眼仍看到假实时"
    assert "silentRefresh()" in body, "切回时不补刷新（③的核心行为缺失）"
    assert "_lastKickAt" in body, "切回缺少去重——两个监听各补一轮，等于连拉两次全量 /states"

    _run_node(_decls() + """
const timers = [];
function setTimeout(fn, ms) { timers.push({ fn: fn, ms: ms }); return timers.length; }
let _hidden = false;
const els = { panelSyncTip: { textContent: '', className: '' } };
const document = { get hidden() { return _hidden; },
                   getElementById: (id) => els[id] || null };
function updateGatewayDevices() {}
const GATEWAY_SN_BY_ENTRY = {};
let now = 5000000;
Date.now = () => now;
""" + "\n".join(_body(n) for n in NEW_FUNCS) + """
let bad = [];
function want(cond, msg) { if (!cond) bad.push(msg); }
let refreshed = 0;
function silentRefresh() { refreshed++; }

kickOnReturn();
kickOnReturn();          // 成对到达的第二个监听
want(refreshed === 1, 'visibilitychange+focus 成对到达却补了 ' + refreshed + ' 轮（应去重成 1 轮）');

now += 5000;
kickOnReturn();
want(refreshed === 2, '第二次真的切回没有补刷新: ' + refreshed);

_hidden = true;
now += 5000;
kickOnReturn();
want(refreshed === 2, '隐藏状态下也补刷了（document.hidden 判定丢了）');

if (bad.length) { console.log('切回补刷不符: ' + bad.join(' / ')); process.exit(1); }
console.log('OK');
""")


def test_sync_tip_marks_stale_data_as_expired():
    """时间戳不前进＝陈旧：必须出现「已过期」与 expired 类，而不是照旧显示更新时间。"""
    _run_node(_decls() + """
const els = { panelSyncTip: { textContent: '', className: '' } };
const document = { hidden: false, getElementById: (id) => els[id] || null };
let now = 1700000000000;
Date.now = () => now;
""" + "\n".join(_body(n) for n in NEW_FUNCS) + """
let bad = [];
function want(cond, msg) { if (!cond) bad.push(msg); }
const el = els.panelSyncTip;

renderSyncTip();
want(el.textContent === '', '从未刷新过时不该显示时间戳: ' + JSON.stringify(el.textContent));

markPanelSynced();
want(el.textContent.indexOf('更新于') >= 0, '成功刷新后没显示「更新于 hh:mm」: ' + JSON.stringify(el.textContent));
want(el.className.indexOf('expired') < 0, '刚刷新完就被标成过期: ' + el.className);

now += 30000;
renderSyncTip();
want(el.className.indexOf('expired') < 0, '30s（还没到阈值）就标过期，提示会一直红着: ' + el.className);

now += 300000;
renderSyncTip();
want(el.textContent.indexOf('已过期') >= 0, '5 分钟没刷新却不提示过期: ' + JSON.stringify(el.textContent));
want(el.className.indexOf('expired') >= 0, '过期文案出来了但没挂 expired 类（配色不生效）: ' + el.className);

// 重新刷新一次必须回到"新鲜"（否则提示只会上红不会消退）
markPanelSynced();
want(el.className.indexOf('expired') < 0, '刷新成功后 expired 没有退掉: ' + el.className);

if (bad.length) { console.log('新鲜度提示不符: ' + bad.join(' / ')); process.exit(1); }
console.log('OK');
""")


def test_sync_tip_dom_and_css_surfaces_exist():
    """提示要真挂在「网关与子设备」标题里，且类名有 CSS 规则（无规则＝裸文本）。"""
    assert '<span id="panelSyncTip" class="panel-sync-tip"></span>' in INDEX, \
        "index.html 的「网关与子设备」标题里没有新鲜度提示的挂载点——JS 写了也没处显示"
    assert re.search(r"\.panel-sync-tip\b", CSS), "缺 .panel-sync-tip 样式规则"
    assert re.search(r"\.panel-sync-tip\.expired\b", CSS), \
        "缺 .panel-sync-tip.expired 规则——「已过期」与普通时间戳长得一样，提示就白加了"
    assert "getElementById('panelSyncTip')" in JS, "JS 没去写那个挂载点（死 DOM）"


def test_panel_timestamp_advances_only_after_data_landed():
    """时间戳只能在数据真落地之后推进：失败时前进＝把陈旧数据伪装成刚更新。"""
    body = _body("silentRefresh")
    marks = [m.start() for m in re.finditer(r"markPanelSynced\(\);", body)]
    assert len(marks) == 1, \
        "silentRefresh 里 markPanelSynced() 出现 %d 次（应恰 1 处：成功路径末尾）" % len(marks)
    landed = body.index("await updateGatewayDevices(entryId, gatewaySn);")
    assert marks[0] > landed, \
        "markPanelSynced() 在设备更新之前就推进时间戳了——HA 拉不动时也会显示「刚更新过」"
    started = body.index("renderSyncTip();")
    assert started < landed, \
        "本轮开头的 renderSyncTip() 不在前段——失败时用户看不到「这屏是旧的」"


# ══ 3) 反死码：新东西全都要有活着的消费者 ════════════════════════════
def test_every_new_helper_and_state_has_a_live_consumer():
    """声明了没人用＝死码（本仓既有纪律：declared but never used = red）。

    注意 `settleTick` 是被**引用**传递的（`setTimeout(settleTick, …)`），不是带
    括号调用 ⇒ 判据数"定义之外的每一次出现"，而不是数 `name(`。
    """
    for name in NEW_FUNCS:
        uses = len(re.findall(r"(?<!function )\b" + re.escape(name) + r"\b", JS))
        assert uses >= 1, \
            "%s 除了定义之外一次都没被用到——加了个死函数" % name
    for state in ("SETTLE_FAST_UNTIL", "_settleTimer", "_panelSyncedAt", "_lastKickAt"):
        assert JS.count(state) >= 3, \
            "%s 出现 %d 次——声明了但没被真正读写，状态是死的" % (state, JS.count(state))


def test_settle_window_still_leaves_the_30s_baseline_alone():
    """①是"命令后收紧"，不是"全局改快"：30s 那条必须原样还在（隐藏页跳过也在）。"""
    tail = JS[JS.index("init();"):]
    m = re.search(r"setInterval\(\(\)\s*=>\s*\{(.*?)\},\s*30000\)", tail, re.S)
    assert m, "文件尾部的 30s 轮询不见了"
    assert "document.hidden" in m.group(1) and "silentRefresh()" in m.group(1), \
        "30s 轮询的隐藏页跳过语义被改动（后台空转纪律）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
