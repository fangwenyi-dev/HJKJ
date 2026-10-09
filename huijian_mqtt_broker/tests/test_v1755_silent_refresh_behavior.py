# -*- coding: utf-8 -*-
"""v1.7.55 行为钉：无感刷新 `updateGatewayDevices` 必须**真的**把设备状态刷上去。

背景（P0，已由审计确证并实测）：`www/js/huijian.js` 的 `updateGatewayDevices`
把 `deviceListEl`/`statusEl` 声明成了 `const`，而函数在第二个 `await` 之后
按 id **重新取值**（防容器被整体重建后写回孤儿节点）——赋值即抛
`TypeError: Assignment to constant variable.`，被同函数内层的 `catch` 静默吞掉
⇒ 网关徽标 与 逐设备 `loadDeviceState` 更新**永久执行不到**。
同一段逻辑在 `loadGatewayDevices`（:780-781）用的是 `let`，本处是漏改。

三条调用路径全中：:528 每 30s silentRefresh、:1327 控制命令后 2s、:1216
「状态」按钮检查后 2s。用户可见后果：首屏正常，之后开/关、拖滑块、在 HA 里
改状态，面板一律停在初始值，直到手动 F5，且无报错无提示。

为什么既有守卫抓不到它：
  * `node --check` 只查语法，const 重赋值是**运行时**错误；
  * `test_mobile_v175.py` 对这个函数只做源码文本扫描（判段内有无
    `loadGatewayDevices`），是结构钉不是行为钉——const 版本照样扫得到。

所以本文件：把**真实函数体**按大括号配平从源码里抽出来（不 eval 整个文件），
配一个最小假 DOM，用 node **真跑**它，探针记录 `loadDeviceState` 被调用次数。
无感刷新真的刷新了状态，这个数就必须 ≥ 1。

并配一条**自变异核验**（`test_probe_catches_the_const_regression`）：把抽出来的
函数体里那两行 `let` 改回 `const` 再跑一遍，必须变红——钉子自己证明自己不是
又一个假绿钉。变异只发生在临时目录，不碰仓库。

⚠️ 维护提醒：假 DOM 的两个坑踩过一次就别再踩——
  ① `getElementById('dev-<id>')` 必须返回**真实元素对象**，否则 `if (devEl)`
     本就跳过，测试假绿；
  ② 服务端 subDevices 的 id 集合必须与 `querySelectorAll('.device-item')` 返回
     的渲染集合**逐字相等**，否则集合比对不等会走 `loadGatewayDevices` 提前
     return，根本到不了被测的那两行重取（测试假绿）。
     这两条都由 `probe.rebuild === 0` + `loadDeviceState` 计数共同兜住。
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")

FUNC = "updateGatewayDevices"
_DEF_RE = re.compile(r"(?:async\s+)?function\s+" + re.escape(FUNC) + r"\s*\(")


def _node():
    """node 可执行文件：优先 NODE_BIN，其次 PATH。"""
    for cand in (os.environ.get("NODE_BIN"), shutil.which("node"), shutil.which("node.exe")):
        if cand:
            return cand
    raise AssertionError("node 不可用，无法真跑无感刷新（装 node 或设 NODE_BIN）")


def _extract(src=None):
    """按大括号配平抽出唯一那个 updateGatewayDevices 的函数体（含 `async`）。"""
    src = JS if src is None else src
    hits = [m.start() for m in _DEF_RE.finditer(src)]
    assert len(hits) == 1, \
        "函数 %s 出现 %d 次（应为 1；重名＝后者覆盖前者，钉会验到死码）" % (FUNC, len(hits))
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
    raise AssertionError("函数 %s 花括号不配平（抽取锚点失效）" % FUNC)


# ══════════════════ 假 DOM + 探针（全部依赖打桩）══════════════════
HARNESS_HEAD = """
// ── 最小假 DOM ──
const ENTRY = 'cfg-1';
const SUB_IDS = ['sub_dev_1', 'sub_dev_2'];
const GW_SN = 'GW-SN-0001';
const GW_ONLINE_ENTITY = 'binary_sensor.gw_online';

const _els = {};
function mkEl(id) {
  if (_els[id]) return _els[id];
  return (_els[id] = {
    id: id, textContent: '', className: '', innerHTML: '', hidden: false, title: '',
    children: [], classList: { contains: () => false },
    appendChild(c) { this.children.push(c); },
    querySelector: () => null,
    querySelectorAll: () => []
  });
}

// 页面已渲染的设备行（id 形如 dev-<rawId>）。坑②：id 必须与服务端 subDevices
// 的 id 逐字相等，否则集合比对不等 → 升级重建提前 return，测不到目标行。
const RENDERED = SUB_IDS.map(function (raw) { return mkEl('dev-' + raw); });
const devicesEl = mkEl('devices-' + ENTRY);
devicesEl.querySelectorAll = (sel) => (sel === '.device-item' ? RENDERED : []);
devicesEl.querySelector = (sel) => (sel === '.device-item' ? (RENDERED[0] || null) : null);
mkEl('gw-status-' + ENTRY);

// 坑①：dev-* 必须查得到真实元素对象，否则 if (devEl) 本就跳过。
const KNOWN = {};
KNOWN['devices-' + ENTRY] = 1;
KNOWN['gw-status-' + ENTRY] = 1;
for (const e of RENDERED) KNOWN[e.id] = 1;

const document = {
  getElementById: (id) => (KNOWN[id] ? mkEl(id) : null),
  querySelector: (sel) => (sel === '#gw-' + ENTRY + ' .gateway-sn' ? mkEl('gw-sn-el') : null)
};

// ── 探针 ──
const probe = { haApi: [], status: [], loadDeviceState: [], rebuild: 0 };
function resetProbe() {
  probe.haApi.length = 0; probe.status.length = 0; probe.loadDeviceState.length = 0;
  probe.rebuild = 0;
}

// ── 服务端数据（每场景重设）──
let DEVICES = [];
let STATES = [];

// ── 桩：HA API ──
async function haApi(path) {
  probe.haApi.push(path);
  if (path.indexOf('/window_controller_gateway/devices') === 0) {
    return { ok: true, status: 200, json: async () => DEVICES };
  }
  if (path === '/states') {
    return { ok: true, status: 200, json: async () => STATES };
  }
  return { ok: false, status: 404, json: async () => ({}) };
}

// ── 桩：渲染/更新 ──
function updateGatewayStatus(el, status) {
  probe.status.push({ el: el && el.id, status: status });
}
async function loadGatewayDevices(entryId, sn) {
  probe.rebuild++;   // 走到这里说明 id 集合对不上，本场景已失真
}
function loadDeviceState(dev, states) {
  probe.loadDeviceState.push({ id: dev.id, n: Array.isArray(states) ? states.length : -1 });
}

// ── 桩：工具函数 ──
function escapeHtml(text) {
  if (text === null || text === undefined) return '';
  return String(text);
}
function deviceSnOf(dev) {
  if (!dev || !Array.isArray(dev.identifiers)) return null;
  for (const it of dev.identifiers) {
    if (Array.isArray(it) && it.length > 1 && it[0] === 'window_controller_gateway') return it[1];
  }
  return null;
}
function findEntityByUniqueId(dev, domain, suffix) {
  if (!dev || domain !== 'binary_sensor' || suffix !== 'online') return null;
  return { domain: 'binary_sensor', unique_id: GW_SN + '_online', entity_id: GW_ONLINE_ENTITY };
}
const GATEWAY_SN_BY_ENTRY = {};
// 审计 2026-09-30 B-7：生产代码里的模块级全局，被测函数按它判断"条目被禁用"。
// 抽函数跑的假 DOM 必须把模块作用域的依赖一起补上，否则 ReferenceError——
// 这不是被测代码的错，是桩缺面（本仓纪律：桩不得窄于真实现）。
const DISABLED_ENTRIES = {};
// v1.8.5（审计 G-4）：同款模块级全局——降级渲染登记、无感刷新消费的整建标记。
// 漏补即 ReferenceError 被 catch 吞掉，场景 A/B/C 会集体假红（桩缺面，不是缺陷）。
const PENDING_REBUILD = {};
"""

HARNESS_TAIL = """
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++; } }
function subs() {
  return SUB_IDS.map(function (raw, i) {
    return { id: raw, name: 'sub' + i, via_device_id: 'gw_1', entities: [] };
  });
}

(async function () {
  // ── 场景 A：新 API（网关带 gateway_online 布尔值）——每 30s 无感刷新的常态 ──
  DEVICES = [{ id: 'gw_1', name: 'GW', identifiers: [['window_controller_gateway', GW_SN]],
               entities: [], gateway_online: true }].concat(subs());
  STATES = [{ entity_id: GW_ONLINE_ENTITY, state: 'on' },
            { entity_id: 'cover.sub_dev_1_cover', state: 'open' }];
  resetProbe();
  await updateGatewayDevices(ENTRY, GW_SN);

  want(probe.loadDeviceState.length >= 1,
       'A: 无感刷新必须至少更新 1 个设备状态（实测 0 ⇒ await 后重取那两行抛了错被吞）: '
       + JSON.stringify(probe.loadDeviceState));
  want(probe.loadDeviceState.length === SUB_IDS.length,
       'A: 每个子设备都要更新一次（应 ' + SUB_IDS.length + ' 次）: '
       + JSON.stringify(probe.loadDeviceState));
  want(probe.loadDeviceState.every(c => c.n === STATES.length),
       'A: 必须拿 /states 的真数据更新（n 应等于 ' + STATES.length + '）: '
       + JSON.stringify(probe.loadDeviceState));
  want(probe.status.length >= 1 && probe.status[0].status === 'online',
       'A: 网关徽标要刷新为在线: ' + JSON.stringify(probe.status));
  want(probe.status.length >= 1 && probe.status[0].el === 'gw-status-' + ENTRY,
       'A: 徽标必须写在网关状态元素上: ' + JSON.stringify(probe.status));
  want(probe.rebuild === 0,
       'A: 设备集合没变就不该升级完整重建（rebuild=' + probe.rebuild
       + '；非 0 ⇒ 桩的 id 集合与服务端不一致，本场景已失真）');
  want(probe.haApi.indexOf('/states') >= 0, 'A: 必须真的拉了 /states: ' + JSON.stringify(probe.haApi));

  // ── 场景 B：兼容旧 API（网关没有 gateway_online，回退 binary_sensor 实体）──
  // 这一条更狠：const 版在 catch 分支里因 gateway_online 非布尔而**连徽标都不写**，
  // probe.status 会是空的——不只是设备状态不更新。
  DEVICES = [{ id: 'gw_1', name: 'GW', identifiers: [['window_controller_gateway', GW_SN]],
               entities: [] }].concat(subs());
  STATES = [{ entity_id: GW_ONLINE_ENTITY, state: 'on' },
            { entity_id: 'cover.sub_dev_1_cover', state: 'closed' }];
  resetProbe();
  await updateGatewayDevices(ENTRY, GW_SN);

  want(probe.loadDeviceState.length === SUB_IDS.length,
       'B: 旧 API 路径也要逐设备更新（应 ' + SUB_IDS.length + ' 次）: '
       + JSON.stringify(probe.loadDeviceState));
  want(probe.status.length === 1 && probe.status[0].status === 'online',
       'B: 徽标要由 online 实体推出在线（全空＝走了 catch 降级分支＝缺陷还在）: '
       + JSON.stringify(probe.status));
  want(probe.rebuild === 0, 'B: 不该升级完整重建（rebuild=' + probe.rebuild + '）');

  // ── 场景 C：条目被禁用时本轮必须整体跳过（审计 2026-09-30 B-7）──
  // 缺陷形态：renderGatewayDisabled 建的"条目未启用，暂不可用"容器会被后面的
  // 刷新覆写成 8 颗可点按钮（点下去恒 4xx），同一张卡灰徽标配活按钮。
  // 判据是模块级 DISABLED_ENTRIES，所以桩里必须真设真清。
  resetProbe();
  DISABLED_ENTRIES[ENTRY] = 1;
  await updateGatewayDevices(ENTRY, GW_SN);
  want(probe.haApi.length === 0,
       'C: 禁用条目不得再打任何 HA API（白打 /devices + /states）: ' + JSON.stringify(probe.haApi));
  want(probe.loadDeviceState.length === 0,
       'C: 禁用条目不得更新设备状态: ' + JSON.stringify(probe.loadDeviceState));
  want(probe.rebuild === 0,
       'C: 禁用条目不得升级完整重建（那会把占位文案换成可点按钮）: ' + probe.rebuild);
  want(probe.status.length === 0,
       'C: 禁用条目不得写徽标（它压根没有 gw-status-* 元素）: ' + JSON.stringify(probe.status));
  delete DISABLED_ENTRIES[ENTRY];
  // 反向半条：清掉标记后必须**照常**刷新（否则"C 通过"只是因为整条函数没跑）
  resetProbe();
  await updateGatewayDevices(ENTRY, GW_SN);
  want(probe.loadDeviceState.length === SUB_IDS.length,
       'C2: 未禁用的条目仍要逐设备更新（反向半条，防 C 靠"什么都不做"蒙过）: '
       + JSON.stringify(probe.loadDeviceState));

  if (bad) { console.log('updateGatewayDevices 真跑: ' + bad + ' 处不符'); process.exit(1); }
  console.log('OK');
})();
"""


def _script(body):
    return HARNESS_HEAD + "\n" + body + "\n" + HARNESS_TAIL


def _run(script):
    node = _node()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "silent_refresh.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run([node, str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


# ── 元钉：抽取本身不许假绿 ──────────────────────────────────────────
def test_extracted_function_is_real():
    """源码结构一变、抽到 0 个函数时，下面那条行为钉会静默假绿——这里先钉住抽取。"""
    body = _extract()
    assert body.strip(), "抽到的函数体为空（守卫会静默假绿）"
    assert "async function " + FUNC in body, \
        "抽取锚点漂移：函数体里没有 `async function %s`" % FUNC
    assert "loadDeviceState(" in body, \
        "%s 不再调用 loadDeviceState——本文件的探针已无意义，必须重写" % FUNC
    assert "document.getElementById('devices-' + entryId)" in body, \
        "%s 不再按 id 重取容器——探针目标消失，必须重写" % FUNC


# ── 行为钉（本文件的主断言）────────────────────────────────────────
def test_silent_refresh_really_updates_device_states():
    """无感刷新跑完，loadDeviceState 必须被调用（≥1 次），否则状态冻结到手动 F5。"""
    rc, out, err = _run(_script(_extract()))
    assert rc == 0 and "OK" in out, \
        "updateGatewayDevices 真跑失败（设备状态没被刷新）：\n%s%s" % (out, err)


# ── 自变异核验：钉子自己证明自己不是假绿钉 ─────────────────────────
def test_probe_catches_the_const_regression():
    """把那两行 `let` 改回 `const` 再跑一遍——必须变红。

    只在临时目录里变异抽出来的函数体，仓库源码不动。若这条哪天绿了（变异后
    仍通过），说明探针已经失真，行为钉必须重写。
    """
    body = _extract()
    mutant = body.replace("let deviceListEl = document.getElementById",
                          "const deviceListEl = document.getElementById")
    mutant = mutant.replace("let statusEl = document.getElementById",
                            "const statusEl = document.getElementById")
    assert mutant != body, \
        "变异没生效（变量改名/重构了？）——自变异核验已失效，必须重写"
    rc, out, err = _run(_script(mutant))
    assert not (rc == 0 and "OK" in out), \
        "const 版居然跑过了：本测试抓不住这个缺陷（又一个假绿钉），必须重写探针"


def test_probe_catches_the_disabled_entry_leak():
    """自变异核验 2：删掉 DISABLED_ENTRIES 那道闸，场景 C 必须变红。

    与 const 那条同构——钉子要自己证明自己抓得住。若这条哪天绿了，说明场景 C
    的探针已经失真（比如 DISABLED_ENTRIES 变成了生产里根本不读的摆设）。
    """
    body = _extract()
    mutant = body.replace("if (DISABLED_ENTRIES[entryId]) return;", "")
    assert mutant != body, \
        "变异没生效：updateGatewayDevices 里已无 `if (DISABLED_ENTRIES[entryId]) return;`" \
        "——闸被改名/挪走/删掉了，B-7 的守卫与这条钉一起失效，必须重写"
    rc, out, err = _run(_script(mutant))
    assert not (rc == 0 and "OK" in out), \
        "摘掉禁用条目守卫后场景 C 仍通过：这条钉是假绿，必须重写探针"
