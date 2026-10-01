# -*- coding: utf-8 -*-
"""2026-10-01 复核批行为钉：条目**渲染形态**变了必须整建，卡片不许停在禁用态。

缺陷（v1.7.56 的 B-7 修复自己引入的回退，本批复核抓出）：B-7 用模块级
`DISABLED_ENTRIES` 拦住了"禁用条目长出 8 颗恒 4xx 按钮"，但那份集合只在
`loadGateways` 整建时清，而 `silentRefresh` 的整建判据（v1.6.9 起）**只比
entry_id 集合**。于是两种真实形态都不改变 id 集合 ⇒ 永不整建：

  ① HA 里把条目禁用、再重新启用；
  ② 启动期条目还在 `setup_retry`/`not_loaded` 时打开面板，之后变 `loaded`。

用户可见后果：卡片恒停在「条目未启用，暂不可用」零按钮，只有点「⟳ 刷新」或
手动 F5 才纠正——与 v1.7.55 修掉的那条 P0 同族（面板不自愈）。v1.7.54 反而
能在同一轮渲染里自愈，因为那时第二个循环无闸门地把所有条目都喂进
`loadGatewayDevices`（那正是 B-7 要修的 bug，它意外充当了回收通道）。

补口：`ENTRY_SIG` 记下"这张卡是按哪个形态渲染的"（disabled_by|state|gateway_sn
三项，取自一份 `entryRenderSig`，记与比同源），`silentRefresh` 在 id 集合相同
时再比签名，不一致即升级为整建。同批把三张渲染态映射的清理提到「空清单」早退
之前（条目全删的那一轮原先留着上一轮的键）。

为什么既有守卫抓不到它：
  * `test_v1755_silent_refresh_behavior.py` 的场景 C 是在桩里**手动**
    `DISABLED_ENTRIES[ENTRY] = 1` / `delete`，从不驱动"形态变化 ⇒ 回收"，
    所以本缺陷全程 1321 passed；
  * `node --check` 只查语法，"映射不回收"是运行时行为。

所以本文件把**真实函数体**按大括号配平抽出来（`entryRenderSig`/`renderGateway`/
`renderGatewayDisabled`/`loadGateways`/`silentRefresh` 逐字，不 eval 整个文件），
配一个按真标记（`id="…"`）解析的假 DOM，用 node 真跑六个场景，其中一条是
**反向半条**（形态不变时不得每轮都整建——否则这条修复会被写成"永远重建"那种
伪修复）。另配两条自变异核验：摘掉"比签名"或摘掉"记签名"，回收场景必须变红。
变异只发生在临时目录，仓库源码不动。

⚠️ 假 DOM 的契约依赖（踩过一次就别再踩）：两种渲染都必须产出
`id="devices-<entryId>"` 容器与 `class="gateway-item" id="gw-<entryId>"` 卡片，
标记形制一变本文件的解析就失效——由
`test_fake_dom_contract_markers_still_hold` 钉住，漂移时当场红而不是假绿。
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "www" / "js" / "huijian.js"
SRC = JS_PATH.read_text(encoding="utf-8")

EXTRACTED = ["entryRenderSig", "escapeHtml", "jsQuote", "jsAttr", "renderGateway",
             "renderGatewayDisabled", "loadGateways", "silentRefresh"]


def _node():
    """node 可执行文件：优先 NODE_BIN，其次 PATH（与 v1755 那份同规矩）。"""
    for cand in (os.environ.get("NODE_BIN"), shutil.which("node"), shutil.which("node.exe")):
        if cand:
            return cand
    raise AssertionError("node 不可用，无法真跑面板刷新（装 node 或设 NODE_BIN）")


def _extract(name, src=None):
    """按大括号配平抽出唯一那个 `function name(` 的定义（含 `async`）。"""
    src = SRC if src is None else src
    pat = re.compile(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(")
    hits = [m.start() for m in pat.finditer(src)]
    assert len(hits) == 1, \
        "函数 %s 出现 %d 次（应为 1；重名＝后者覆盖前者，钉会验到死码）" % (name, len(hits))
    i = hits[0]
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("函数 %s 花括号不配平（抽取锚点失效）" % name)


def _body(name, src=None):
    return _extract(name, src)


# ═══════════════ 假 DOM + 桩 + 六个场景（全部逐字抽取生产函数）═══════════════
HARNESS_HEAD = """
// ── 假 DOM：innerHTML 一写就按真标记重解析 id ──
const ENTRY = 'cfg-1';
const SUB_IDS = ['sub_dev_1', 'sub_dev_2'];
const GW_SN = 'GW-SN-0001';
const NEW_SN = 'GW-SN-0002';

const _els = {};
function mkEl(id) {
  if (_els[id]) return _els[id];
  return (_els[id] = { id: id, innerHTML: '', title: '', textContent: '',
                        className: '', classList: { contains: () => false } });
}
const container = mkEl('gatewayContainer');
let _containerHtml = '';
let _items = [];
Object.defineProperty(container, 'innerHTML', {
  get() { return _containerHtml; },
  set(v) {
    _containerHtml = String(v);
    probe.innerHTMLWrites++;
    for (const m of _containerHtml.matchAll(/id="([^"]+)"/g)) { mkEl(m[1]); }
    _items = Array.from(
      _containerHtml.matchAll(/class="gateway-item" id="gw-([^"]+)"/g), m => m[1]);
  }
});
container.querySelectorAll = (sel) =>
  (sel === '.gateway-item' ? _items.map(id => ({ id: 'gw-' + id })) : []);

const document = {
  getElementById: (id) => (_els[id] ? _els[id] : null),
  querySelector: () => null
};

// ── 探针 ──
const probe = { innerHTMLWrites: 0, loadGatewayDevices: [], updateGatewayDevices: [],
                haApi: [] };
function resetProbe() {
  probe.loadGatewayDevices.length = 0;
  probe.updateGatewayDevices.length = 0;
  probe.haApi.length = 0;
}
function writes() { return probe.innerHTMLWrites; }

// ── 服务端 config_entries（每场景重设）──
let ENTRIES = [];
function entryObj(extra) {
  const base = { entry_id: ENTRY, title: '慧尖网关', state: 'loaded',
                 disabled_by: null, data: { gateway_sn: GW_SN } };
  return Object.assign(base, extra || {});
}

// ── 桩：HA API ──
async function haApi(path) {
  probe.haApi.push(path);
  if (path.indexOf('/config/config_entries/entry') === 0) {
    return { ok: true, status: 200, json: async () => ENTRIES };
  }
  return { ok: true, status: 200, json: async () => [] };
}
async function checkServiceStatus() {}
async function loadRemoteControl() {}

// 桩：真实现会按 id 覆写 devices-<entryId>（/devices 读设备注册表，条目禁用
// 不影响注册表 ⇒ 必有数据），这正是 B-7 的缺陷形态，所以桩也照真实现覆写。
async function loadGatewayDevices(entryId, sn) {
  probe.loadGatewayDevices.push({ entryId: entryId, sn: sn });
  const el = document.getElementById('devices-' + entryId);
  if (el) {
    el.innerHTML = SUB_IDS.map(r => '<div class="device-item" id="dev-' + r + '"></div>').join('');
  }
}
// 无感刷新路径（本文件不测它内部，测的是"该不该升级到整建"）
async function updateGatewayDevices(entryId, sn) {
  probe.updateGatewayDevices.push({ entryId: entryId, sn: sn });
}

// ── 生产模块级全局（被测函数按它们判形态；改名会 ReferenceError，不会假绿）──
const DOMAIN = 'window_controller_gateway';
let _silentRefreshing = false;
const PAIRING_UNTIL = {};
const GATEWAY_SN_BY_ENTRY = {};
const DISABLED_ENTRIES = {};
const ENTRY_SIG = {};
"""

HARNESS_TAIL = """
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++; } }
// 假 DOM 只做"注册元素"这一层：innerHTML 重写时把标记里的 id 变成元素，
// 但不给嵌套子元素回填内容。所以"这一轮渲染的是占位"判在容器整段 html 上，
// "设备区真的建出了设备行"判在桩写过的那个子元素上——两者合起来才是
// "用户看到的是占位 / 用户看到的是设备"。
function containerHas(text) { return _containerHtml.indexOf(text) >= 0; }
function rowsShown(id) {
  const el = document.getElementById('devices-' + id);
  return !!el && (el.innerHTML || '').indexOf('device-item') >= 0;
}
// loadGateways 的 catch 会把错误文案写进容器（生产语义）。桩缺依赖时走的也是这条
// 路 ⇒ "发生过一次 innerHTML 写入"会被一次**失败的**写入满足，那正是假绿。每条
// 场景都显式否认走进 catch：缺依赖当场红，而不是让回收断言莫名其妙地失败。
function catchShown() {
  return containerHas('无法连接 HA API') || containerHas('面板无权直接读取')
      || containerHas('HA 响应超时') || containerHas('使用说明');
}

(async function () {
  // ── D1：禁用条目首屏＝占位、零设备渲染、签名已记 ──
  ENTRIES = [entryObj({ disabled_by: 'user' })];
  resetProbe();
  await loadGateways();
  want(!catchShown(), 'D1: loadGateways 走进了 catch（多半是桩缺依赖），本场景已失真');
  want(containerHas('条目未启用，暂不可用') && !rowsShown(ENTRY),
       'D1: 禁用条目必须渲染「条目未启用，暂不可用」占位且无设备行（回收场景的前提）');
  want(probe.loadGatewayDevices.length === 0,
       'D1: 禁用条目不得喂进设备渲染（B-7 原判据）: ' + JSON.stringify(probe.loadGatewayDevices));
  want(DISABLED_ENTRIES[ENTRY] === true, 'D1: 渲染时必须把条目记进 DISABLED_ENTRIES');
  want(ENTRY_SIG[ENTRY] === entryRenderSig(ENTRIES[0]),
       'D1: 渲染时必须记下形态签名: ' + JSON.stringify(ENTRY_SIG));

  // ── D2：HA 里重新启用 ⇒ 下一轮静默刷新必须整建并把设备区建回来 ──
  ENTRIES = [entryObj({ disabled_by: null })];
  resetProbe();
  const w2 = writes();
  await silentRefresh();
  want(writes() > w2, 'D2: 禁用→启用 必须升级为整建（id 集合没变，只比 id 的旧判据抓不到）');
  want(!catchShown(), 'D2: 整建走进了 catch（桩缺依赖），本场景已失真');
  want(!DISABLED_ENTRIES[ENTRY], 'D2: 回收后禁用判定必须被清掉');
  want(probe.loadGatewayDevices.length === 1,
       'D2: 回收后必须真的重建设备区: ' + JSON.stringify(probe.loadGatewayDevices));
  want(rowsShown(ENTRY), 'D2: 用户可见的恢复——设备区要有设备行，不能还是占位');
  want(GATEWAY_SN_BY_ENTRY[ENTRY] === GW_SN,
       'D2: 回收后 SN 映射要重新填上: ' + JSON.stringify(GATEWAY_SN_BY_ENTRY));

  // ── D3：启动期 setup_retry（未 loaded）→ loaded，同样必须回收 ──
  ENTRIES = [entryObj({ state: 'setup_retry' })];
  resetProbe();
  await loadGateways();
  want(!catchShown(), 'D3: 首屏整建走进了 catch（桩缺依赖），本场景已失真');
  want(containerHas('状态: setup_retry') && probe.loadGatewayDevices.length === 0,
       'D3: setup_retry 条目按禁用态渲染（首屏）: ' + JSON.stringify(probe.loadGatewayDevices));
  ENTRIES = [entryObj({ state: 'loaded' })];
  resetProbe();
  const w3 = writes();
  await silentRefresh();
  want(writes() > w3, 'D3: setup_retry→loaded 必须整建（这才是"打开面板时 HA 还在启动"的常态）');
  want(!catchShown(), 'D3: 回收整建走进了 catch（桩缺依赖），本场景已失真');
  want(rowsShown(ENTRY), 'D3: 设备区必须恢复出设备行');

  // ── D4：条目换了网关 SN ⇒ 整建（旧 SN 会让静默刷新拿错 SN 发控制）──
  ENTRIES = [entryObj({ data: { gateway_sn: NEW_SN } })];
  resetProbe();
  const w4 = writes();
  await silentRefresh();
  want(writes() > w4, 'D4: gateway_sn 变化必须整建');
  want(!catchShown(), 'D4: 整建走进了 catch（桩缺依赖），本场景已失真');
  want(GATEWAY_SN_BY_ENTRY[ENTRY] === NEW_SN,
       'D4: SN 映射必须刷成新值: ' + JSON.stringify(GATEWAY_SN_BY_ENTRY));

  // ── D5（反向半条）：形态不变时**不得**每轮都整建，必须走无感路径 ──
  for (let round = 1; round <= 3; round++) {
    resetProbe();
    const w5 = writes();
    await silentRefresh();
    want(writes() === w5,
         'D5: 形态没变就不该整建（否则"修复"写成了每 30s 全量重建）第 ' + round + ' 轮');
    want(probe.updateGatewayDevices.length === 1 && probe.updateGatewayDevices[0].sn === NEW_SN,
         'D5: 第 ' + round + ' 轮要真的走无感刷新: ' + JSON.stringify(probe.updateGatewayDevices));
    want(probe.loadGatewayDevices.length === 0,
         'D5: 第 ' + round + ' 轮不该重建设备区: ' + JSON.stringify(probe.loadGatewayDevices));
  }

  // ── D6：签名缺失（卡片不是 loadGateways 建的）按不一致处理，宁可多重建一次 ──
  delete ENTRY_SIG[ENTRY];
  resetProbe();
  const w6 = writes();
  await silentRefresh();
  want(writes() > w6, 'D6: 签名缺失必须按"形态变了"处理（不许静默不回收）');
  want(!catchShown(), 'D6: 整建走进了 catch（桩缺依赖），本场景已失真');

  if (bad) { console.log('条目形态失效判定真跑: ' + bad + ' 处不符'); process.exit(1); }
  console.log('OK');
})();
"""


def _script(extra_head="", silent_body=None, loadg_body=None):
    parts = [_body(n) for n in EXTRACTED]
    if silent_body is not None:
        parts[EXTRACTED.index("silentRefresh")] = silent_body
    if loadg_body is not None:
        parts[EXTRACTED.index("loadGateways")] = loadg_body
    return HARNESS_HEAD + extra_head + "\n".join(parts) + "\n" + HARNESS_TAIL


def _run(script):
    node = _node()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "entry_invalidation.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run([node, str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


# ── 元钉：抽取与假 DOM 契约不许静默假绿 ────────────────────────────
def test_extracted_functions_are_real():
    """判据必须还长在抽出来的函数里，否则下面的行为钉毫无意义。"""
    sig = _extract("entryRenderSig")
    for field in ("disabled_by", "state", "gateway_sn"):
        assert field in sig, \
            "entryRenderSig 不再看 %s——形态签名退化，回收会漏掉这一维，必须重写" % field

    loadg = _extract("loadGateways")
    assert "ENTRY_SIG[entry.entry_id] = entryRenderSig(entry);" in loadg, \
        "loadGateways 不再记录形态签名（比而不记＝恒判不一致＝每轮整建，或整条失效）"
    assert "delete ENTRY_SIG[k]" in loadg, "loadGateways 不再清形态签名映射"

    silent = _extract("silentRefresh")
    assert "ENTRY_SIG[e.entry_id] !== entryRenderSig(e)" in silent, \
        "silentRefresh 不再比形态签名——本文件的回收场景全部失去被测对象，必须重写"


def test_maps_cleared_before_empty_list_early_return():
    """三张渲染态映射必须在「空清单」早退**之前**清（条目全删的那一轮也要清）。"""
    loadg = _extract("loadGateways")
    early = loadg.index("if (!entries || entries.length === 0)")
    for name in ("GATEWAY_SN_BY_ENTRY", "DISABLED_ENTRIES", "ENTRY_SIG"):
        line = loadg.index("for (const k of Object.keys(%s)) delete %s[k];" % (name, name))
        assert line < early, \
            "%s 的清理仍在空清单早退之后：条目被全删时映射留着上一轮的键" % name


def test_fake_dom_contract_markers_still_hold():
    """假 DOM 靠两种渲染里的 id 标记工作；标记形制一变解析就失效。"""
    for fn in ("renderGateway", "renderGatewayDisabled"):
        body = _extract(fn)
        assert 'class="gateway-item" id="gw-' in body, \
            "%s 不再产出 .gateway-item 卡片，假 DOM 的卡片清单解析会失效" % fn
        assert 'id="devices-' in body, \
            "%s 不再建 devices-<entryId> 容器，回收断言无从检查" % fn


# ── 行为钉（本文件的主断言）────────────────────────────────────────
def test_state_change_rebuilds_and_recovers_the_card():
    """六个场景真跑：禁用→启用 / setup_retry→loaded / 换 SN 都要回收，形态不变不许重建。"""
    rc, out, err = _run(_script())
    assert rc == 0 and "OK" in out, \
        "条目形态失效判定真跑失败（卡片会停在禁用态，用户要手动刷新才恢复）：\n%s%s" % (out, err)


# ── 自变异核验：钉子自己证明自己不是假绿钉 ─────────────────────────
def test_probe_catches_the_missing_signature_comparison():
    """摘掉 silentRefresh 里"比签名"那半步 ⇒ 回收场景必须变红。"""
    silent = _extract("silentRefresh")
    mutant = silent.replace(
        "if (same) for (const e of entries) {\n"
        "                        if (ENTRY_SIG[e.entry_id] !== entryRenderSig(e)) "
        "{ same = false; break; }\n"
        "                    }", "")
    assert mutant != silent, \
        "变异没生效：silentRefresh 里已无签名比对那一段——补口被改名/挪走，钉与修复一起失效，必须重写"
    rc, out, err = _run(_script(silent_body=mutant))
    assert not (rc == 0 and "OK" in out), \
        "摘掉签名比对后回收场景仍通过：这条钉是假绿，必须重写探针\n%s" % out


def test_probe_catches_the_missing_signature_recording():
    """摘掉 loadGateways 里"记签名"那一行 ⇒ 必须变红（记不到就恒判不一致，D5 会红）。"""
    loadg = _extract("loadGateways")
    mutant = loadg.replace("ENTRY_SIG[entry.entry_id] = entryRenderSig(entry);", "")
    assert mutant != loadg, \
        "变异没生效：loadGateways 里已无记录签名那一行，必须重写这条核验"
    rc, out, err = _run(_script(loadg_body=mutant))
    assert not (rc == 0 and "OK" in out), \
        "摘掉签名记录后仍通过：探针没真跑重建判定，必须重写\n%s" % out
