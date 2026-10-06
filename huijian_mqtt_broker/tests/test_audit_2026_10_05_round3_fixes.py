# -*- coding: utf-8 -*-
"""第三轮只读复核（docs/bug-audit-2026-10-05-round3.md）残余条目：逐条判决 + 修复回归。

报告 §三/§3.2 共 6 条待决，本文件按"真则修、假则留证"处置：

  真（已修，本文件各有回归）
    #3  cover.py 数值转换只捕 (ValueError, TypeError)——大整数进 float() 抛
        OverflowError 会炸穿实体属性；同族漏网 hub_client.py 的 bindCodeAt。
        收口口径：全仓按类扫描 + 机器验证步（下面第一条测试）。
    #5  set_member_alias 的读-改-写快照做在锁外 ⇒ 并发改名丢更新。
    #6  _http 不验 200 的体形状 ⇒ 调用方在 try 之外 `.get()`，AttributeError
        逃出 list_members（面板 500 / 保活 task 死）。
    #8  device_manager.cleanup 逐条 await + `except CancelledError: pass` 吞掉
        **本协程自己的**取消——违反本仓 C-6 不变量（既有 test_c6_* 只扫"调用点"，
        cleanup 本体是作用面漏洞，同批把钉扩到本体）。
    #9  面板滑块防覆写只认 document.activeElement——iOS Safari 触摸拖动不给非
        文本控件移焦，手机上等于没修。改用「原生 input 事件时间戳」为第一凭据
        （鼠标/触摸/键盘三种拖动都逐帧派发），**不新增 touch/pointer 监听**，
        v1.7.22「防误触纯 CSS、JS 不劫持滑块」的约定不破。

  假（不改，留证）
    §3.1 C-2「窄竞态」：报告建议的 `consumed` 标志与现实现**逐帧等价**——
        今日 pop 之后同 id 再来一帧已走"解除失聪"分支，加 consumed 后同样在
        第二帧解除；回声真丢时两者都是"第一帧被当回声、下一帧恢复"。窗口闭不上，
        故不改（要闭它得在载荷里带自答标记＝改协议，代价大于收益）。
        该语义已由 tests/mutation_matrix.py 的 c2/f1 两臂双向守着。
    §4.1「注释滞后」：mqtt_bootstrap.py 那条 return 的注释当时就已写明
        "宿主停机 / 启用条目已清空"两条路径，指控不成立。
"""
import ast
import asyncio
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import types

import pytest

from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway import \
    ws_gateway as wsg  # noqa: F401  卸载路径的 monkeypatch 目标
import custom_components.window_controller_gateway as pkg
from custom_components.window_controller_gateway.cover import WindowControllerCover
from custom_components.window_controller_gateway.device_manager import (
    WindowControllerDeviceManager,
)
from custom_components.window_controller_gateway.const import (
    DEVICE_STATUS_OPEN,
    DOMAIN,
)
# 判据本体从它所在的那个文件 import——不在这里再抄一份形状规则（抄的那份会与实现漂移）
from test_audit_2026_09_30_fixes import _c6_offenders_in_tree

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "window_controller_gateway"
JS = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")

MID_A = "a" * 12
MID_B = "b" * 12
HUGE = 10 ** 400          # JSON 允许的任意精度整数：进 float() 抛 OverflowError


# ═════════════════ #3 · 数值转换溢出类：全类机器验证步 ═════════════════

def _arg_is_text(call):
    """转换实参按构造就是 str：`int("42")` 只会抛 ValueError/TypeError。
    OverflowError 只在实参**本身已是数字**（大整数/inf）时才发生。"""
    args = call.args
    if len(args) != 1:
        return False
    a = args[0]
    if not isinstance(a, ast.Call):
        return False
    if isinstance(a.func, ast.Attribute) and a.func.attr in ("strip", "lstrip", "rstrip"):
        return True
    return isinstance(a.func, ast.Name) and a.func.id == "str"


def _overflow_offenders(root, pkg):
    """扫全仓：Try 体里有直接 int()/float() 转换、接住了 ValueError/TypeError
    却没接 OverflowError 的，全是漏网。判据来自 10-05 审计 #3 的同类收敛。"""
    offenders = []
    for p in sorted(list(root.glob("*.py")) + list(pkg.rglob("*.py"))):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for t in ast.walk(tree):
            if not isinstance(t, ast.Try):
                continue
            conv = [c for stmt in t.body for c in ast.walk(stmt)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id in ("int", "float") and not _arg_is_text(c)]
            if not conv:
                continue
            caught, bare = set(), False
            for h in t.handlers:
                if h.type is None:
                    bare = True
                    continue
                for nm in ast.walk(h.type):
                    if isinstance(nm, ast.Name):
                        caught.add(nm.id)
                    elif isinstance(nm, ast.Attribute):
                        caught.add(nm.attr)
            if bare:
                continue
            if caught & {"ValueError", "TypeError"} and "OverflowError" not in caught:
                offenders.append("%s:%d" % (p.name, t.lineno))
    return offenders


def test_no_numeric_conversion_left_without_overflow_gate():
    offenders = _overflow_offenders(ROOT, PKG)
    assert not offenders, \
        "数值转换点漏捕 OverflowError（大整数/inf 会把异常炸出实体属性或调用面）: %s" % offenders


def test_overflow_gate_itself_is_not_tautological():
    """判据自证：把 cover.py 的一处 OverflowError 摘掉，本钉必须变红。
    不做这一步，"全类扫过"只是话术（本仓纪律：加固也要变异）。"""
    src = (PKG / "cover.py").read_text(encoding="utf-8")
    mutated = src.replace("except (ValueError, TypeError, OverflowError):",
                          "except (ValueError, TypeError):")
    assert mutated != src, "变异锚点没命中（cover.py 的 except 写法变了？判据要跟着改）"
    with tempfile.TemporaryDirectory() as d:
        fake_root = pathlib.Path(d)
        fake_pkg = fake_root / "pkg"
        shutil.copytree(PKG, fake_pkg)
        (fake_pkg / "cover.py").write_text(mutated, encoding="utf-8")
        offenders = _overflow_offenders(fake_root, fake_pkg)
        assert any("cover.py" in o for o in offenders), \
            "摘掉 OverflowError 后判据仍然绿——这条钉是假绿"


# ═════════════════ #3 · cover.py 四条落点的行为验证 ═════════════════

class _FakeDM:
    def __init__(self, status=None, attributes=None, last_update=None):
        dev = None
        if status is not None or attributes is not None:
            dev = {"sn": "5005X", "status": status, "attributes": attributes or {}}
            if last_update:
                dev["last_update"] = last_update
        self._device = dev

    def get_device(self, device_sn):
        return self._device

    async def update_device_status(self, device_sn, status, attributes=None):
        self.updated = (device_sn, status, attributes)


def _cover(attributes, status=DEVICE_STATUS_OPEN):
    c = WindowControllerCover(
        hass=None, device_manager=_FakeDM(status, attributes), mqtt_handler=None,
        gateway_sn="GW1", device_sn="5005X", device_name="窗")
    c._position_capable = True
    return c


@pytest.mark.parametrize("junk", [HUGE, float("inf"), float("-inf"), float("nan")])
def test_is_closed_does_not_raise_or_claim_open_on_non_finite(junk):
    """垃圾位置不许被静默解释成"打开"，更不许把异常炸出实体属性。
    旧实现：HUGE → OverflowError 逃逸；inf/nan → `<= 0` 为 False ⇒ state=open。"""
    assert _cover({"r_travel": junk}, status=None).is_closed is None


@pytest.mark.parametrize("junk", [HUGE, float("inf"), float("-inf"), float("nan")])
def test_current_position_falls_back_to_state_endpoints_on_junk(junk):
    c = _cover({"r_travel": junk}, status=DEVICE_STATUS_OPEN)
    assert c.current_cover_position == 100, "非有限/越界值必须落到开/关端点，不谎报数值"


def test_extra_state_attributes_survive_non_finite():
    attrs = _cover({"r_travel": float("inf")}).extra_state_attributes
    assert "position" not in attrs, "inf 不许以合法数字形态进状态属性"


def test_real_positions_still_work():
    """正向半条：修复不许把正常值一起挡掉（v1.6.11 的 0.5=打开 语义保留）。
    status=None 才走 r_travel 推导分支——有 status 时按状态优先，属既有语义。"""
    assert _cover({"r_travel": 0}, status=None).is_closed is True
    assert _cover({"r_travel": 0.5}, status=None).is_closed is False
    assert _cover({"r_travel": 65}, status=None).is_closed is False
    assert _cover({"r_travel": "abc"}, status=None).is_closed is None
    assert _cover({"r_travel": 65}).current_cover_position == 65
    assert _cover({"r_travel": 255}).current_cover_position == 100  # 未校准走端点


# ═════════════════ #5 · 并发改名的读-改-写必须整体在锁内 ═════════════════

def _alias_client(tmp_path):
    c = hc.HubClient([], config_dir=str(tmp_path), session=object())
    c.members = [{"mid": MID_A, "openidMasked": "oeh…zS", "at": 1},
                 {"mid": MID_B, "openidMasked": "oFa…02", "at": 2}]
    return c


def test_concurrent_renames_keep_both_names(tmp_path, monkeypatch):
    """两行「改名」并发：旧实现把 `dict(self.member_aliases)` 快照做在锁外，
    两边从同一份快照出发、各自整份覆盖落盘 ⇒ 先完成那次改名静默丢失。"""
    client = _alias_client(tmp_path)
    real = hc.save_member_aliases
    writes = []

    def slow_save(config_dir, mapping):
        writes.append(dict(mapping))
        time.sleep(0.05)          # 磁盘慢一点，竞态窗口就张开
        real(config_dir, mapping)

    monkeypatch.setattr(hc, "save_member_aliases", slow_save)

    async def both():
        await asyncio.gather(client.set_member_alias(MID_A, "爸爸"),
                             client.set_member_alias(MID_B, "妈妈"))

    asyncio.run(both())
    assert len(writes) == 2
    assert hc.load_member_aliases(str(tmp_path)) == {MID_A: "爸爸", MID_B: "妈妈"}, \
        "两次并发改名只剩后写的那条——锁外快照的丢更新回潮"
    assert client.member_aliases == {MID_A: "爸爸", MID_B: "妈妈"}


def test_second_writer_still_sees_first_commit(tmp_path, monkeypatch):
    """反向半条：串行两次改同一行，第二次必须基于第一次的结果（不是初始空表）。"""
    client = _alias_client(tmp_path)
    monkeypatch.setattr(hc, "save_member_aliases",
                        lambda config_dir, mapping: None)   # 只验内存口径
    asyncio.run(client.set_member_alias(MID_A, "爸爸"))
    asyncio.run(client.set_member_alias(MID_B, "妈妈"))
    assert client.member_aliases == {MID_A: "爸爸", MID_B: "妈妈"}


def test_failed_save_does_not_commit_memory(tmp_path, monkeypatch):
    """写盘失败不许在内存里假装记住了（旧口径保持）。"""
    client = _alias_client(tmp_path)

    def boom(config_dir, mapping):
        raise OSError("disk full")

    monkeypatch.setattr(hc, "save_member_aliases", boom)
    assert asyncio.run(client.set_member_alias(MID_A, "爸爸")) is False
    assert client.member_aliases == {}


# ═════════════════ #6 · 200 响应体的形状闸 ═════════════════

class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body


class _Session:
    def __init__(self, body, status=200):
        self.closed = False
        self._body, self._status = body, status

    def post(self, url, json=None, **kwargs):
        return _Resp(self._status, self._body)


@pytest.mark.parametrize("body", [None, [1, 2], "ok", 42])
def test_http_rejects_non_object_200_body(body):
    """云函数返回空时云托管照样给 200 + `null`。旧实现原样 return ⇒ 调用方在
    try 之外做 `data.get("ok")`，AttributeError 逃出方法。"""
    c = hc.HubClient([], config_dir="x", base="http://127.0.0.1:1", session=_Session(body))
    with pytest.raises(hc.HubHttpError) as e:
        asyncio.run(c._http("/agent/members", {}))
    assert e.value.status == 200
    assert e.value.err == "bad_body", "错误码要可辨识，不能混进网络类失败"


def test_http_passes_object_body_through():
    c = hc.HubClient([], config_dir="x", base="http://127.0.0.1:1",
                     session=_Session({"ok": True, "members": []}))
    assert asyncio.run(c._http("/agent/members", {})) == {"ok": True, "members": []}


def test_list_members_degrades_instead_of_raising_on_null_body(tmp_path):
    """落点验证：面板调 list_members 时非 dict 体必须走"读取失败"操作槽，
    而不是把 AttributeError 抛给路由（那会让面板 500）。"""
    c = hc.HubClient([], config_dir=str(tmp_path), base="http://127.0.0.1:1",
                     session=_Session(None))
    c.instance_id, c._secret = "inst-1", "sec-1"
    assert asyncio.run(c.list_members()) is False


# ═════════════════ #8 · cleanup 的取消语义（正反两向真跑） ═════════════════

def _bare_dm():
    dm = WindowControllerDeviceManager.__new__(WindowControllerDeviceManager)
    dm._background_tasks = []
    dm.devices = {"x": 1}
    dm._device_registry_cache = None
    dm._device_added_callbacks = []
    dm._device_removed_callbacks = []
    dm._device_update_callbacks = {}
    return dm


@pytest.mark.asyncio
async def test_cleanup_cancellation_propagates_outward():
    """cleanup 正 await 后台任务时上层 cancel ⇒ CancelledError 必须穿出来。
    旧写法 `except asyncio.CancelledError: pass` 就地吃掉，HA 停机/重载只能等超时。

    后台任务故意做成"取消不掉的"（接住 CancelledError 后继续干活）：这样 cleanup
    一定还钉在 await 那一格上，取消只能落在 cleanup 自己身上——正是旧写法吞掉的那一格。
    套 wait_for：若取消被吞，cleanup 会一直等那只任务到 3600s ⇒ 本钉挂死而不是红
    （异步挂死=静默失败，看门狗必须自带）。"""
    dm = _bare_dm()
    seen = {"cancel_hit": False}

    async def stubborn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancel_hit"] = True      # 取消到了子任务，子任务却不死
            await asyncio.sleep(3600)

    slow = asyncio.ensure_future(stubborn())
    dm._background_tasks.append(slow)
    await asyncio.sleep(0)

    task = asyncio.ensure_future(dm.cleanup())
    await asyncio.sleep(0.05)
    assert seen["cancel_hit"], "cleanup 没走到 await 后台任务那一格 ⇒ 本钉在测空气"
    assert not task.done(), "后台任务没卡住 cleanup，取消落不到 await 上"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    slow.cancel()   # 收尾：别留一只没人管的 3600s 任务（销毁时会有 Task was destroyed）


@pytest.mark.asyncio
async def test_child_cancellation_does_not_escape_cleanup():
    """反向半条：子任务被取消是 cleanup 的正常产物，不许转抛给上层
    （否则后续清理不跑，devices/回调列表留在脏状态）。"""
    dm = _bare_dm()
    kids = [asyncio.ensure_future(asyncio.sleep(3600)) for _ in range(3)]
    dm._background_tasks.extend(kids)
    await asyncio.sleep(0)

    await asyncio.wait_for(dm.cleanup(), timeout=5)   # 不抛
    assert all(t.done() for t in kids)
    assert dm._background_tasks == [] and dm.devices == {}


@pytest.mark.asyncio
async def test_cleanup_waits_for_every_task_despite_gather(monkeypatch):
    """gather 化不许把"每个任务都被 await 过"这条保证弄丢（v1.6.11 #3 的原缺陷）。"""
    dm = _bare_dm()
    state = {"ran": 0}

    async def worker():
        state["ran"] += 1
        await asyncio.sleep(3600)

    kids = [asyncio.ensure_future(worker()) for _ in range(4)]
    dm._background_tasks.extend(kids)
    for _ in range(4):
        await asyncio.sleep(0)          # 让 4 只都起步
    await asyncio.wait_for(dm.cleanup(), timeout=5)
    assert state["ran"] == 4 and all(t.cancelled() for t in kids)


# ═════════════════ #9 · 面板滑块防覆写：触摸凭据（node 真跑） ═════════════════

def _js_body(name):
    hits = [m.start() for m in re.finditer(r"function\s+" + re.escape(name) + r"\s*\(", JS)]
    assert len(hits) == 1, "函数 %s 出现 %d 次（重名＝后者覆盖前者，钉会验到死码）" % (name, len(hits))
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


def _js_consts():
    out = []
    for pat in (r"const USER_INPUT_AT = new WeakMap\(\);",
                r"const USER_INPUT_HOLD_MS = \d+;"):
        m = re.search(pat, JS)
        assert m, "huijian.js 缺常量 %s（改名了？harness 要跟着改）" % pat
        out.append(m.group(0))
    return "\n".join(out)


HARNESS = _js_consts() + "\n" + _js_body("markUserInput") + "\n" + _js_body("userInteracting") + """
// ── 假 DOM：只有一个"当前焦点元素"，触摸拖动不会改变它（iOS Safari 语义）──
const document = { activeElement: null };
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++ } }

const slider = { value: 30 };
const other = { value: 60 };

want(userInteracting(slider) === false, '冷启动没交互过，不该算交互（否则永远不回写）');
want(userInteracting(null) === false, 'null 元素不许抛');

// 场景 1：桌面/键盘——焦点即交互（v1.7.18 原语义保持）
document.activeElement = slider;
want(userInteracting(slider) === true, '焦点在滑块上必须算交互');
want(userInteracting(other) === false, '别的滑块不受影响');

// 场景 2：触摸拖动——焦点没动（activeElement 仍是 body），只有原生 input 事件
document.activeElement = null;
slider.value = 65;
markUserInput(slider);
want(userInteracting(slider) === true,
     '手指拖动中（input 已派发、焦点没跟上）必须算交互——#9 的正身');
want(userInteracting(other) === false, '另一条滑块不该被连带冻结');

// 场景 3：保持窗过后恢复回写（不许一次触摸就永久不回写）
USER_INPUT_AT.set(slider, Date.now() - (USER_INPUT_HOLD_MS + 1));
want(userInteracting(slider) === false, '过了保持窗必须让 30s 无感刷新同步真值');

if (bad) { console.log('userInteracting 真跑: ' + bad + ' 处不符'); process.exit(1) }
console.log('OK');
"""


def _node(script):
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "t.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0 and "OK" in r.stdout, \
            "userInteracting 真跑失败：\n%s%s" % (r.stdout, r.stderr)


def test_user_interacting_recognises_touch_drag_in_node():
    _node(HARNESS)


def test_every_rendered_slider_is_marked_and_guarded():
    """覆盖率钉：renderDevice 里每条 `<input type="range">` 的 oninput 都必须先
    打交互标记，且三处回写都受 userInteracting 保护——漏一条就等于那条滑块
    仍是"拖动中被覆写"的老形态（v1.7.18 在手机上的原缺陷）。"""
    body = _js_body("renderDevice")
    sliders = len(re.findall(r'<input type="range"', body))
    marked = len(re.findall(r'markUserInput\(this\);', body))
    assert sliders >= 3, "renderDevice 里的滑块少于 3 条？判据要跟着改"
    assert marked == sliders, \
        "滑块 %d 条 / 交互标记 %d 处——有滑块没接凭据" % (sliders, marked)
    guarded = len(re.findall(r'!userInteracting\(', JS))
    assert guarded == 3, "回写点的守卫变成 %d 处（应为 3：位置/速度/力度）" % guarded


def test_no_touch_or_pointer_hijacking_added():
    """反向半条：本批靠原生 input 事件拿凭据，不得新增 touch/pointer 监听
    （v1.7.22 防误触批的约定：JS 不劫持滑块默认行为）。"""
    assert "touchstart" not in JS and "pointerdown" not in JS, \
        "JS 里出现 touch/pointer 事件劫持——v1.7.22 的 CSS-only 约定被破"


# ═════════════ #6 的下游自伤：新错误码必须在面板有文案（node 真跑） ═════════════

def test_bad_body_code_renders_copy_in_node():
    """`_http` 新产生的 `bad_body` 会经调用方的 `e.err or "…"` 落进操作槽，而面板对
    未知码走 `default: return ''`（那是 conn/op 拆分钉刻意要求的行为）⇒ 不映射就等于
    用户点完「添加家人」毫无反应。本钉用产品源码在 node 里真跑映射出口。"""
    _node(_js_body("hubOpErrorText") + """
const document = {};
let bad = 0;
function want(cond, msg) { if (!cond) { console.log('FAIL ' + msg); bad++ } }

want(hubOpErrorText('bad_body').length > 0,
     'bad_body 必须有文案（未知码走 default 回空串＝点了没反应）');
want(/对象|看不懂/.test(hubOpErrorText('bad_body')),
     '文案要说清"响应体不是对象"，不能只说"稍后再试": ' + hubOpErrorText('bad_body'));
want(hubOpErrorText('zzz_not_a_code') === '',
     '未知码仍须回空串（conn/op 拆分钉的口径不许因此翻掉）');

if (bad) { console.log('hubOpErrorText 真跑: ' + bad + ' 处不符'); process.exit(1) }
console.log('OK');
""")


# ══════════ 发版前对抗复核（A-1…A-4）：确证缺陷的收口与判据自证 ══════════

@pytest.mark.parametrize("junk", [HUGE, 10 ** 999, float("inf")])
def test_positive_int_rejects_float_unrepresentable(junk):
    """A-1 源头闸：`_positive_int` 的产物全都要进浮点算术（`ms/1000.0`、
    `ttl-(now-at)`）。旧实现 `int()` 不抛就原样放行——JSON 允许任意精度整数，
    `10**400` 实测一路带到 status_view 才炸（面板 500）。"""
    assert hc._positive_int(junk) == 0, "过大/非有限的云端数值一律按不可用（0＝回落兜底）"


def test_ttl_from_expire_ms_survives_huge_epoch_ms():
    """A-1 第一落点：`_ttl_from_expire_ms(10**400)` 旧实现抛
    `OverflowError: int too large to convert to float`，而它的唯一调用点在
    `_ensure_registered` 里没有兜底 ⇒ 从长连主循环逃出，每 5s 重启且永远修不好。"""
    assert hc._ttl_from_expire_ms(HUGE) == 0
    assert hc._ttl_from_expire_ms(10 ** 999) == 0
    assert hc._ttl_from_expire_ms(1e999) == 0, "inf 形态也不能算出个假 TTL"


@pytest.mark.parametrize("junk", [HUGE, float("inf"), float("nan")])
def test_member_code_expires_in_matches_owner_sibling_guard(junk):
    """A-1 第二落点（视图层双保险）：`bind_code_expires_in` 早在 v1.7.61 S1 就有
    出口守卫，成员码这条是同族漏网——同一个 junk 值过去返回 -1，这里旧实现抛。"""
    c = hc.HubClient([], config_dir="x", session=object())
    c.member_code, c._member_code_at, c._member_code_ttl_s = "123456", 1.0, junk
    assert c.member_code_expires_in() == -1


def test_status_view_survives_absurd_cloud_numbers(tmp_path):
    """端到端落点：`api.py` 的 status_view 调用不包异常，抛出去就是面板 500。
    两层都验：① 入界闸过后的正常回落；② 绕过入界闸（历史/异常路径）时视图层自守。"""
    c = hc.HubClient([], config_dir=str(tmp_path), session=object())
    c.instance_id, c._secret = "inst-1", "sec-1"
    c.member_code, c._member_code_at = "123456", time.time()
    c.bind_code, c._bind_code_at = "654321", time.time()

    c._member_code_ttl_s = hc._positive_int(HUGE)      # 过闸 ⇒ 0 ⇒ 回落常量
    c._bind_code_ttl_s = hc._positive_int(HUGE)
    assert isinstance(c.status_view(), dict)

    c._member_code_ttl_s = HUGE                        # 绕过入界闸，直塞视图层
    assert isinstance(c.status_view(), dict), "视图层没有自守就又被上游一个数打穿"


def test_concurrent_renames_survive_cold_alias_file_read(tmp_path, monkeypatch):
    """A-2：`_load_aliases()` 的读盘做在锁外——并发改名时两边各自发起一次读盘，
    慢的那次回来把 `self.member_aliases` 整个覆盖成"第一次落盘之前"的磁盘快照 ⇒
    先完成那次改名在内存与磁盘上一起丢。冷启动窗（flag 还没置上）实测可达。

    调度是**显式**的，不靠 gather 的运气：先让 A 进到读盘（已提交线程池），再让 B
    进到读盘（此刻磁盘仍是空表），B 的读盘慢半拍返回那份旧快照。修后 B 拿锁时
    `_aliases_loaded` 已为真、直接复用，第二次读盘根本不会发生。

    v1.7.65（矩阵整跑复验）：末条断言是**与线程池调度无关**的判据，别删。此前只靠
    「落盘少了一条改名」判红，那是计时依赖的——第二次的线程要被负载拖到第一次落盘之后
    才真读盘，它拿到的就是新快照，去掉锁的实现照样全绿；整跑时 `r3_alias_load_outside_lock`
    就是这么假绿了一次（同一臂单跑 10/10 必红）。B 一定在 A 置起 `_aliases_loaded`
    之前跑到判 flag 那一格，所以「变异后必 2 次读盘」不看时序。
    """
    client = _alias_client(tmp_path)
    real_load = hc.load_member_aliases
    reads = []

    def stale_second_read(config_dir):
        snap = real_load(config_dir)        # 读盘就在调用这一刻（磁盘还是空表）
        reads.append(1)
        if len(reads) == 2:
            time.sleep(0.12)               # 第二个人晚一步，带回的是旧快照
        return snap

    monkeypatch.setattr(hc, "load_member_aliases", stale_second_read)

    async def drive():
        ta = asyncio.ensure_future(client.set_member_alias(MID_A, "爸爸"))
        await asyncio.sleep(0)              # A 进到 _load_aliases 的 await
        tb = asyncio.ensure_future(client.set_member_alias(MID_B, "妈妈"))
        await asyncio.sleep(0)              # B 也进到读盘
        await asyncio.wait_for(asyncio.gather(ta, tb), timeout=5)

    asyncio.run(drive())
    assert real_load(str(tmp_path)) == {MID_A: "爸爸", MID_B: "妈妈"}, \
        "锁外的第二次读盘把先完成那次改名抹掉了（A-2 回潮）"
    assert client.member_aliases == {MID_A: "爸爸", MID_B: "妈妈"}
    assert len(reads) == 1, \
        "第二次读盘发生了：锁内二次判没生效，读盘与赋 flag 不再是同一原子段（A-2 回潮）"


def _unload_harness(tmp_path, monkeypatch, runtime):
    """照抄 test_audit_2026_09_30_fixes 的 async_unload_entry 假件口径。"""
    async def noop(_hass):
        return None

    monkeypatch.setattr(pkg, "save_persistent_data", noop)
    monkeypatch.setattr(wsg, "async_ensure_ws_gateway", noop)
    monkeypatch.setattr(pkg, "async_ensure_hub_client", noop)
    hass = types.SimpleNamespace(
        data={DOMAIN: {"E1": runtime}},
        config=types.SimpleNamespace(config_dir=str(tmp_path)),
        config_entries=types.SimpleNamespace(async_entries=lambda d: []))
    return hass, types.SimpleNamespace(entry_id="E1", data={})


@pytest.mark.asyncio
async def test_unload_entry_cancellation_propagates(tmp_path, monkeypatch):
    """A-3 真跑：`async_unload_entry` 里两处同形吞取消（`_bg_tasks` 逐条 await、
    `_check_task` await）。旧写法把"本协程被取消"当成"子任务被我们取消"记一条
    debug 就过去 ⇒ 取消传不出去，卸载被硬跑到结尾返回 True，HA 以为干净卸载。"""
    seen = {"cancel_hit": False}

    async def stubborn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen["cancel_hit"] = True          # 取消到了子任务，子任务却不死
            await asyncio.sleep(3600)

    runtime = {"_bg_tasks": [asyncio.ensure_future(stubborn())]}
    hass, entry = _unload_harness(tmp_path, monkeypatch, runtime)

    task = asyncio.ensure_future(pkg.async_unload_entry(hass, entry))
    await asyncio.sleep(0.2)
    assert seen["cancel_hit"], "卸载没走到后台任务收尾那一格 ⇒ 本钉在测空气"
    assert not task.done(), "子任务没卡住卸载，取消落不到 await 上"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_unload_entry_absorbs_child_cancellation_and_returns_true(tmp_path, monkeypatch):
    """反向半条：子任务被我们取消是卸载的正常产物，不许转抛（否则后续清理不跑、
    平台卸载被跳过，条目状态留在半卸载形态）。"""
    kid = asyncio.ensure_future(asyncio.sleep(3600))
    runtime = {"_bg_tasks": [kid]}
    hass, entry = _unload_harness(tmp_path, monkeypatch, runtime)

    assert await asyncio.wait_for(pkg.async_unload_entry(hass, entry), 5) is True
    assert kid.done()


@pytest.mark.parametrize("src,expect_hit", [
    # ① 禁形态：捕了不传
    ("async def cleanup(self):\n    try:\n        await t\n"
     "    except asyncio.CancelledError:\n        pass\n", True),
    # ② 允许的收口形态：条件再抛（A-4 假阳性——旧判据只查 handler 直接语句）
    ("async def cleanup(self):\n    try:\n        await t\n"
     "    except asyncio.CancelledError:\n        if not self.devices:\n            raise\n", False),
    # ③ async with suppress（A-4 盲区：旧判据只扫 ast.With）
    ("async def cleanup(self):\n    async with contextlib.suppress(asyncio.CancelledError):\n"
     "        await t\n", True),
    # ④ except*（A-4 盲区：TryStar 不是 Try 节点）
    ("async def cleanup(self):\n    try:\n        await t\n"
     "    except* asyncio.CancelledError:\n        pass\n", True),
    # ⑤ 与取消无关的 suppress 不该被牵连
    ("async def cleanup(self):\n    async with contextlib.suppress(ValueError):\n"
     "        await t\n", False),
])
def test_c6_detector_shapes(src, expect_hit):
    """判据自身的形状自证（合成源码四臂）：扩面不能只靠"产品码恰好写对了"来绿。"""
    hits = _c6_offenders_in_tree(ast.parse(src), set())
    assert bool(hits) is expect_hit, \
        "形态 %r 判据给 %s，期望 %s" % (src[:48], hits, "报" if expect_hit else "不报")
