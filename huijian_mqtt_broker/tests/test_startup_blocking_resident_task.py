"""常驻任务不得用 eager 面创建——否则顶穿 HA 启动收尾（2026-10-27 .91 现网实锤）。

现场一手证据（HA Core 日志，版本 2026.10.4）：
    17:08:07 [homeassistant.bootstrap] Setup timed out for bootstrap waiting on
             {<Task name='window_controller_gateway_bootstrap_healer'
               coro=<async_start_bootstrap_healer.<locals>._healer()
                    running at .../mqtt_bootstrap.py:982>>} - moving forward
    17:08:22 [homeassistant.core] Something is blocking Home Assistant from
             wrapping up the start up phase ... waiting for tasks: {同一个 task}
⇒ core_state=NOT_RUNNING 持续 8 分 55 秒。那 9 分钟里 Supervisor 不放 /core/api，
加项面板 /api/ha/* 全 502、发现代理每 30s 打一条「引导失败（entries query failed）」。

判据形态取自 **HA 2026.10.4 homeassistant/core.py 实物**（非推测）：
  * `async_create_task(target, name=None, eager_start=True)` → task 进 `self._tasks`
  * `async_create_background_task(target, name, eager_start=True)`（name 必需）
        → task 进 `self._background_tasks`，文档三句承诺：
          "Will not block startup / Will be automatically cancelled on shutdown /
           Calls to async_block_till_done will not wait for completion"
  * `async_block_till_done(wait_background_tasks=False)` 只 gather `self._tasks`
  * 停机路径把 `self._background_tasks` 逐个 `task.cancel("Home Assistant is stopping")`
        （core.py:1177）

⇒ 永不出栈的巡查任务必须由 background 面创建；用 eager 面创建的后果不是"升级那一次
慢"，而是**每一次 HA 启动/reload 都要付 bootstrap 超时**。

类的判据（第二类测试）不止钉本次改的两处：凡"交给任务创建器的常驻循环协程"都必须
走 background 面。常驻的定义是 **`while True`（无界，与睡多久无关）或循环里有 ≥60s
的等待**——只按秒数判会被 `while True: sleep(30)` 这种形状骗过去（语音侧就是这个形状，
本次审计实测到的假阴性，写成钉见 test_unbounded_loop_is_resident_even_with_short_sleep）。
比它短的有界一次性任务（本集成最热的是 0.5s 首配让位、30s 耳促升观察单发）不算常驻：
等它们的代价远小于把永不出栈的任务留在启动收尾里。
"""
import ast
import asyncio
import pathlib

import pytest
from types import SimpleNamespace

import custom_components.window_controller_gateway.mqtt_bootstrap as mb

ROOT = pathlib.Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "window_controller_gateway"

#: 常驻判据：while 体内出现 ≥ 该秒数的等待 ⇒ 判为"不会在短时间内结束"
RESIDENT_SLEEP_MIN = 60.0
EAGER_CREATOR = "async_create_task"
BACKGROUND_CREATOR = "async_create_background_task"
# 对抗复核（2026-10-27）抓出的两处判据盲，扩到实物口径：
#   · `hass.async_add_job(coro)` / `async_add_hass_job(..., background=False)` 默认落
#     `self._tasks`（上游实物 core.py:768 与最低支持版 2024.12.0:782 同一行逻辑）⇒ 同样顶穿启动
#   · `hass.async_create_task(self._loop(hass))` 这种 Attribute 实参以前被直接 continue 掉
EAGER_CREATORS = (EAGER_CREATOR, "async_add_job", "async_add_hass_job")
BACKGROUND_CREATORS = (BACKGROUND_CREATOR,)
CREATORS = EAGER_CREATORS + BACKGROUND_CREATORS


# --------------------------------------------------------------------------
# 判据本体（同一份代码既扫生产，也吃合成源码——自证臂才有效）
# --------------------------------------------------------------------------

def _module_floats(tree):
    """模块级 NAME -> float 常量表（睡多久通常写成 CHANNEL_VERIFY_INTERVAL = 1800.0）。"""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, (int, float)) \
                and not isinstance(node.value.value, bool):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = float(node.value.value)
    return out


def _await_seconds(node, consts):
    """Await 里可能等待的秒数：数值字面量 + 模块常量解引用（跳过 hass/self 首位）。"""
    call = node.value
    if not isinstance(call, ast.Call):
        return []
    args = list(call.args)
    if args and isinstance(args[0], ast.Name) and args[0].id in ("hass", "self"):
        args = args[1:]
    out = []
    for a in args + [kw.value for kw in call.keywords]:
        if isinstance(a, ast.Constant) and isinstance(a.value, (int, float)) \
                and not isinstance(a.value, bool):
            out.append(float(a.value))
        elif isinstance(a, ast.Name) and a.id in consts:
            out.append(consts[a.id])
    return out


def _test_read_names(test_node):
    """循环条件里**读**到的变量名（含 self._stopping 这种 Attribute 全串）。"""
    out = set()
    for n in ast.walk(test_node):
        if isinstance(n, ast.Attribute):
            parts, cur = [], n
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            out.add(".".join(reversed(parts)))
        elif isinstance(n, ast.Name):
            out.add(n.id)
    return {x for x in out if x not in ("True", "False", "None")}


def _body_assigned_names(body):
    out = set()
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        out.add(t.id)
                    elif isinstance(t, ast.Attribute):
                        parts, cur = [], t
                        while isinstance(cur, ast.Attribute):
                            parts.append(cur.attr)
                            cur = cur.value
                        if isinstance(cur, ast.Name):
                            parts.append(cur.id)
                        out.add(".".join(reversed(parts)))
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                tgt = getattr(n, "target", None) or getattr(n, "optional_vars", None)
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id)
    return out


def _is_resident_loop(funcdef, consts):
    """常驻＝三种实物形态任一：
      1. `while True`（无界，与睡多久无关）
      2. while 体内有 ≥60s 的等待
      3. while 的条件变量在体内**从不被赋值**且无 break/return ⇒ 没有自然出口

    第 3 条为什么要收成这样而不是"没有 break/return 就算常驻"：我第一版这么写，
    当场把 `persist.save_persistent_data` 的防抖圈（`while _save_pending:` 体内
    `_save_pending = False`，有自然出口）误判成缺陷。真实形态的对照是
    `hub_client._run_forever` 的 `while not self._stopping:`——那个标志只由外部
    `async_stop` 置位，循环内不赋值 ⇒ 才是真无出口。
    """
    for node in ast.walk(funcdef):
        if not isinstance(node, ast.While):
            continue
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            return True
        cond_names = _test_read_names(node.test)
        if cond_names and not (cond_names & _body_assigned_names(node.body)) and \
                not any(isinstance(x, (ast.Break, ast.Return))
                        for stmt in node.body for x in ast.walk(stmt)):
            return True
        for inner in ast.walk(node):
            if isinstance(inner, ast.Await) and \
                    any(sec >= RESIDENT_SLEEP_MIN for sec in _await_seconds(inner, consts)):
                return True
    return False


def _coro_func_name(call_node, funcs):
    """从 `X.loop(hass)` / `X._loop(hass)` 里取出被调协程名（Attribute 形态不再放过）。"""
    f = call_node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _site_creator(node):
    """把创建点归成 'eager' / 'background'（add_job 系显式 background=True 时不算 eager）。"""
    attr = node.func.attr
    if attr in BACKGROUND_CREATORS:
        return BACKGROUND_CREATOR
    for kw in node.keywords:
        if kw.arg == "background" and isinstance(kw.value, ast.Constant) \
                and kw.value.value is True:
            return BACKGROUND_CREATOR
    return attr


def resident_task_sites(source, filename="<src>"):
    """返回 [(行号, 协程名, 创建器)]：常驻循环协程被交给了哪个任务创建器。

    宇宙来源＝"被交给 CREATORS 的协程"本身，与被校验对象同构；改名/挪文件不会让它
    静默变空集，空集由 test_resident_loop_universe_is_not_silently_empty 判红。
    """
    tree = ast.parse(source, filename=filename)
    consts = _module_floats(tree)
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            funcs.setdefault(node.name, node)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in CREATORS:
            continue
        # 实参位不固定：hass.async_create_task(coro, name=…) 与
        # entry.async_create_background_task(hass, coro, name) 两种排布都要吃
        for arg in node.args:
            if not isinstance(arg, ast.Call):
                continue
            fn = funcs.get(_coro_func_name(arg, funcs) or "")
            if fn is None or not _is_resident_loop(fn, consts):
                continue
            sites.append((node.lineno, fn.name, _site_creator(node)))
            break
    return sorted(sites)


def integration_scan():
    """扫全集成：返回 (违规清单, 全部常驻创建点, 扫到的文件相对路径)。

    宇宙必须**递归**：对抗复核实测旧写法 `glob("*.py")` 只吃顶层，`mqtt_handler/`
    整包在宇宙之外（注入一条 eager 常驻循环进去，8 条判据照绿）。
    """
    bad, seen, files = [], [], []
    for path in sorted(INTEGRATION.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        files.append(path.relative_to(INTEGRATION).as_posix())
        for lineno, name, creator in resident_task_sites(
                path.read_text(encoding="utf-8"), path.name):
            seen.append((path.relative_to(INTEGRATION).as_posix(), name, creator))
            if creator in EAGER_CREATORS:
                bad.append("%s:%d %s()" % (
                    path.relative_to(INTEGRATION).as_posix(), lineno, name))
    return bad, seen, files


class _FakeHA:
    """分桶正确的 hass 替身：eager 桶被 block_till_done 等，background 桶不被等。

    与真 HA 的差别只在不复刻 eager_start 的"创建即跑第一步"（这里用
    loop.create_task 延迟启动）——判据看的是**哪个桶**，不受影响。
    """

    def __init__(self, entries=None):
        self.data = {}
        self._tasks = set()
        self._background_tasks = set()
        self.is_stopping = False
        self.config = SimpleNamespace(config_dir=".")
        self.config_entries = SimpleNamespace(
            async_entries=lambda dom: list(entries or []))

    def _track(self, task, bucket):
        bucket.add(task)
        task.add_done_callback(bucket.remove)   # 与 HA 同形：完成即从桶里摘掉

    def async_create_task(self, coro, name=None, eager_start=True):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._track(task, self._tasks)
        return task

    def async_create_background_task(self, coro, name, eager_start=True):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._track(task, self._background_tasks)
        return task

    async def async_block_till_done(self, wait_background_tasks=False):
        await asyncio.sleep(0)
        while True:
            pool = self._tasks | self._background_tasks if wait_background_tasks \
                else self._tasks
            pending = [t for t in pool if not t.done() and t is not
                       asyncio.current_task()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)


async def _no_marker(hass):
    """has_bootstrap_marker 替身：标记不在＝引导已落地 ⇒ 走常驻核验分支。"""
    return False


async def _verdict_ok(hass):
    return "ok"


# --------------------------------------------------------------------------
# 一、行为臂：启动收尾不再等常驻 healer，而 healer 确实还活着
# --------------------------------------------------------------------------

def test_resident_healer_does_not_block_startup_block_till_done(monkeypatch):
    """healer 永不结束（挂在睡里）时，async_block_till_done() 必须在有界时间内返回。

    红＝缺陷复现：HA 等它等到 bootstrap 超时，core 停在 NOT_RUNNING（实测 8 分 55 秒）。
    """
    park = asyncio.Event()      # 只创建、绝不 set ⇒ healer 真·永不出栈

    async def fake_sleep(hass, delay):
        await park.wait()
        return True

    async def fake_mdns(hass):
        return None

    monkeypatch.setattr(mb, "has_bootstrap_marker", _no_marker)
    monkeypatch.setattr(mb, "verify_builtin_channel", _verdict_ok)
    monkeypatch.setattr(mb, "_interruptible_sleep", fake_sleep)
    monkeypatch.setattr(mb, "_mdns_guard", fake_mdns)

    hass = _FakeHA(entries=[SimpleNamespace(entry_id="E1")])

    async def scenario():
        mb.async_start_bootstrap_healer(hass)
        healer = hass.data[mb.DOMAIN]["_bootstrap_healer"]
        assert healer.get_name() == f"{mb.DOMAIN}_bootstrap_healer", (
            "句柄名变了＝本判据盯的任务被换掉了，请同步判据而不是放过")
        await asyncio.sleep(0)      # 让 healer 走到 park 上
        try:
            await asyncio.wait_for(hass.async_block_till_done(), timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail(
                "async_block_till_done() 未在有界时间内返回＝常驻任务被算进 HA 启动"
                "收尾（现网形态：bootstrap 超时 WARNING + core_state=NOT_RUNNING "
                "8 分 55 秒 ⇒ 面板 /api/ha/ 全 502、发现代理刷『引导失败』）。"
                "改法＝常驻任务用 hass.async_create_background_task 创建")
        assert not healer.done(), "healer 必须仍在常驻（判据不许靠'它正好结束了'蒙绿）"
        assert healer in hass._background_tasks, "常驻 healer 应落在 background 桶"
        # 与真 HA 停机同形（core.py:1177 逐个 cancel）：取消必须干净收尾并交回句柄
        healer.cancel()
        await asyncio.gather(healer, return_exceptions=True)
        return healer

    scenario_result = asyncio.run(scenario())
    assert scenario_result.cancelled()
    assert hass.data[mb.DOMAIN]["_bootstrap_healer"] is None, (
        "取消后不清句柄＝下次 setup 以为 healer 还活着，永不再拉起")


# --------------------------------------------------------------------------
# 二、类级判据：全集成内"常驻循环交给 eager 面"一处都不许留
# --------------------------------------------------------------------------

def test_no_resident_loop_is_created_on_the_eager_api():
    bad, _seen, _files = integration_scan()
    assert not bad, (
        "常驻循环任务用 eager 面（%s）创建 ⇒ 会被 HA 的 async_block_till_done 等：\n  %s\n"
        "现网后果＝每次 HA 启动多花约 9 分钟 NOT_RUNNING"
        % ("/".join(EAGER_CREATORS), "\n  ".join(bad)))


def test_universe_is_recursive_not_just_top_level():
    """宇宙自检第二半：扫描必须覆盖子包（`mqtt_handler/` 6 个文件）。

    旧写法 glob("*.py") 只吃顶层 ⇒ 往子包里塞一条 eager 常驻循环，全仓零反应。
    """
    _bad, _seen, files = integration_scan()
    assert any("/" in f for f in files), \
        "扫描宇宙里没有子包文件＝glob 漏递归，子包整体在判据之外"
    assert "mqtt_handler/_lifecycle.py" in files, \
        "mqtt_handler/ 不在宇宙里（MQTT 生命周期那包恰恰是最容易起常驻任务的地方）"


def test_resident_loop_universe_is_not_silently_empty():
    """宇宙下限自检：扫描必须至少认出已知的两处常驻循环（改名/挪走≠没有）。

    判据若因重构变成空集，前面那条类级判据会"零命中通过"——那正是本仓踩过的假绿形态。
    """
    _bad, seen, _files = integration_scan()
    names = {(f, n) for (f, n, _c) in seen}
    assert len(names) >= 2, (
        "只扫到 %d 处常驻循环创建点，少于已知的 2 处 ⇒ 扫描宇宙缩小了，"
        "先修判据再谈通过：%s" % (len(names), sorted(names)))
    assert any(n == "_healer" for _f, n, _c in seen), \
        "healer 创建点不在扫描宇宙里＝假绿"
    assert any(n == "_arm_heartbeat_when_mqtt_ready" for _f, n, _c in seen), \
        "心跳武装任务创建点不在扫描宇宙里＝假绿"


def test_background_sites_keep_their_own_cancel_handle():
    """接线半条：换 background 面不等于放弃现有的收尾记账口径。

    * 心跳武装任务：句柄必须继续进 `_bg_tasks`（条目卸载的 A-3 gather 取消口径）；
    * healer：它的退出面**本来就不是**"谁 cancel 它"，而是循环顶部自察
      "停机 / 启用条目清零"（盘上核实：全集成无任何地方按 `_bootstrap_healer`
      句柄取消它）。换 API 后这条自退路径不变，但幂等判活仍要靠句柄在位，
      所以钉"创建点仍是 background 面 + 句柄仍写进 runtime"。
    """
    init_src = (INTEGRATION / "__init__.py").read_text(encoding="utf-8")
    at = init_src.index("_arm_heartbeat_when_mqtt_ready(),")
    window = init_src[at - 400:at + 600]
    assert BACKGROUND_CREATOR in window, "心跳武装任务创建点不是 background 面"
    assert '"_bg_tasks"' in window and "append" in window, (
        "武装任务句柄不再进 _bg_tasks＝条目卸载时没人取消它")

    mb_src = (INTEGRATION / "mqtt_bootstrap.py").read_text(encoding="utf-8")
    at2 = mb_src.index('runtime["_bootstrap_healer"]')
    assert BACKGROUND_CREATOR in mb_src[at2:at2 + 300], \
        "healer 创建点不再是 background 面"


# --------------------------------------------------------------------------
# 三、判据自证臂：证明第二类判据真会咬（不是"字样还在就绿"）
# --------------------------------------------------------------------------

_RESIDENT_EAGER = '''
import asyncio

CHANNEL_VERIFY_INTERVAL = 1800.0


async def _loop(hass):
    while True:
        await _interruptible_sleep(hass, CHANNEL_VERIFY_INTERVAL)


def start(hass):
    return hass.async_create_task(_loop(), name="loop")
'''

_RESIDENT_BACKGROUND = _RESIDENT_EAGER.replace(
    "async_create_task(", "async_create_background_task(")

_SHORT_LOOP = '''
import asyncio

EAR_PROMOTION_WATCH_SECONDS = 30.0


async def _watch(hass):
    while True:
        await asyncio.sleep(EAR_PROMOTION_WATCH_SECONDS)


def start(hass):
    return hass.async_create_task(_watch(), name="watch")
'''

_BOUNDED_FOR_LOOP = '''
import asyncio

CHANNEL_VERIFY_INTERVAL = 1800.0


async def _finite(hass):
    for _ in range(2):
        await _interruptible_sleep(hass, CHANNEL_VERIFY_INTERVAL)


def start(hass):
    return hass.async_create_task(_finite(), name="finite")
'''

_LONG_SLEEP_WITHOUT_LOOP = '''
async def _one_shot(hass):
    await _interruptible_sleep(hass, 1800.0)


def start(hass):
    return hass.async_create_task(_one_shot(), name="one")
'''


def test_judge_bites_on_synthetic_eager_resident_loop():
    """反向半边：合成源码"常驻循环 + eager 面"必须被扫出来，否则判据是空的。"""
    sites = resident_task_sites(_RESIDENT_EAGER, "synth.py")
    assert [(n, c) for (_l, n, c) in sites] == [("_loop", EAGER_CREATOR)], sites


def test_judge_passes_equivalent_background_form():
    """等价半边：只换创建器、其余一字不动 ⇒ 不得报红（别把合规实现钉死）。"""
    sites = resident_task_sites(_RESIDENT_BACKGROUND, "synth.py")
    assert [(n, c) for (_l, n, c) in sites] == [("_loop", BACKGROUND_CREATOR)], sites


def test_unbounded_loop_is_resident_even_with_short_sleep():
    """无界循环这一条不依赖秒数：`while True` + 30s 也必须算常驻。

    为什么单独钉：语音侧的常驻面正是这个形状（while True + sleep 30/55）。
    判据若只按"睡够 60s"算，同形缺陷会在两个加载项里一起被放过——本次审计
    实测到的假阴性形态，写成钉才不会退回阈值版。
    """
    sites = resident_task_sites(_SHORT_LOOP, "s.py")
    assert [(n, c) for (_l, n, c) in sites] == [("_watch", EAGER_CREATOR)], \
        "while True 即使只睡 30s 也算常驻（eager 面创建＝启动期不定时炸弹）"


def test_judge_shape_criteria():
    """形状半边：有界 `for` 里的 1800s 长睡、以及不在循环里的长睡，都不算常驻。"""
    assert resident_task_sites(_BOUNDED_FOR_LOOP, "s.py") == [], \
        "有界 for 不是常驻循环（等得起，别把一次性任务钉成缺陷）"
    assert resident_task_sites(_LONG_SLEEP_WITHOUT_LOOP, "s.py") == [], \
        "没有循环的单发长睡不是常驻"


# ── 对抗复核抓出的三个形态盲，逐个补成自证臂（这三条红＝判据又变窄了）──

_ATTRIBUTE_FORM = '''
import asyncio


class Holder:
    async def _loop(self, hass):
        while True:
            await asyncio.sleep(120.0)

    def start(self, hass):
        return hass.async_create_task(self._loop(hass), name="m")
'''

_ADD_JOB_FORM = '''
import asyncio


async def _loop(hass):
    while True:
        await asyncio.sleep(120.0)


def start(hass):
    return hass.async_add_job(_loop(hass))
'''

_ADD_JOB_EXPLICIT_BACKGROUND = _ADD_JOB_FORM.replace(
    "async_add_job(_loop(hass))", "async_add_hass_job(_loop(hass), background=True)")

_NO_EXIT_WHILE = '''
import asyncio


class Holder:
    async def _run_forever(self):
        while not self._stopping:
            await asyncio.sleep(1.0)

    def start(self, hass):
        return hass.async_create_task(self._run_forever(), name="f")
'''

_DEBOUNCE_WHILE = '''
import asyncio

_pending = True


async def _save(hass):
    global _pending
    while _pending:
        _pending = False
        await _do_save(hass)


def start(hass):
    return hass.async_create_task(_save(hass), name="s")
'''

_ENTRY_SCOPED_ARGS = '''
import asyncio


async def _loop(hass):
    while True:
        await asyncio.sleep(120.0)


def start(entry, hass):
    return entry.async_create_background_task(hass, _loop(hass), "n")
'''


def test_judge_catches_attribute_method_form():
    """对抗复核实测盲点①：`hass.async_create_task(self._loop(hass))` 以前被 continue 掉。"""
    sites = resident_task_sites(_ATTRIBUTE_FORM, "a.py")
    assert [(n, c) for (_l, n, c) in sites] == [("_loop", EAGER_CREATOR)], sites


def test_judge_catches_add_job_default_eager():
    """盲点②：async_add_job 默认落 _tasks（上游 core.py:768 / 2024.12.0:782 实物）。"""
    sites = resident_task_sites(_ADD_JOB_FORM, "j.py")
    assert [(n, c) for (_l, n, c) in sites] == [("_loop", "async_add_job")], sites
    assert sites[0][2] in EAGER_CREATORS


def test_add_job_with_explicit_background_is_not_a_violation():
    """等价半边：显式 background=True 的 add_hass_job 不顶启动 ⇒ 不得报红。"""
    sites = resident_task_sites(_ADD_JOB_EXPLICIT_BACKGROUND, "j.py")
    assert [(n, c) for (_l, n, c) in sites] == [("_loop", BACKGROUND_CREATOR)], sites


def test_judge_catches_while_without_natural_exit():
    """盲点③：`while not self._stopping` 既不是 while True 也没有长睡，以前算"不常驻"。"""
    sites = resident_task_sites(_NO_EXIT_WHILE, "w.py")
    assert [(n, c) for (_l, n, c) in sites] == [("_run_forever", EAGER_CREATOR)], sites


def test_debounce_while_is_not_resident():
    """第 3 条的**反向**半边：条件变量在体内被赋值的防抖圈有自然出口，不得算常驻。

    这条不是凑数——我第一版把"体内无 break/return"一律当常驻，当场把生产里
    `persist.save_persistent_data` 的 `while _pending: _pending = False` 误报成缺陷。
    没有这条反向臂，判据会靠"把合法实现钉红"来假装覆盖广。
    """
    assert resident_task_sites(_DEBOUNCE_WHILE, "d.py") == [], \
        "体内会置False的防抖圈有自然出口＝一次性任务，不是常驻"


def test_judge_reads_entry_scoped_argument_order():
    """实参位不固定：entry.async_create_background_task(hass, coro, name) 也要认得。"""
    sites = resident_task_sites(_ENTRY_SCOPED_ARGS, "e.py")
    assert [(n, c) for (_l, n, c) in sites] == [("_loop", BACKGROUND_CREATOR)], sites
