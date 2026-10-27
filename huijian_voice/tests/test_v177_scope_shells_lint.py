"""v1.7.7 批：anyio cancel scope 横跨 yield 的**类级**收口 ＋ 语音线 undefined-name 闸。

判决来源（2026-10-27 外部报告 P-3／BUG-2 逐条复现）：
  · P-3 点的是 `stt_transport.await_message`——形状属实，但它是兼容壳（新链路走
    `recognize`，全仓无生产调用者）。真问题同类共六处：tts（v1.0.69 根因①）、
    llm（v1.1.27-r2）、conversation 消费侧（v1.1.27）三处**生产在跑**的早已收口，
    剩下 stt / ws_transport 两处壳没收 ⇒ 本批按「同类不许留第三份形状」收，
    不按「现网故障」排。
  · BUG-2 点的是 `core/main.py` 用了没 import 的 `Optional`（ruff F821 真阳），
    根因是语音线 CI 完全没有 lint 步（网关线有）。运行时不炸**仅因为**文件顶部
    `from __future__ import annotations`——本批补 import 并给该线装上窄闸。

扫描型守卫自带：正向对照（旧形状必须被抓到）＋宇宙元自检（扫描面缩窄即红），
避免「0 命中」是空跑假绿。
"""
import ast
import asyncio
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("anyio", reason="需真 anyio（cancel scope 任务仿射语义前提）")
import anyio  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SCOPE_FUNCS = {"fail_after", "move_on_after"}
PROD_DIRS = ("custom_components", "core")


def _yields_in_block(body):
    """body 内、且不属更内层函数/类/推导式的 yield。"""
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                          ast.Lambda, ast.ListComp, ast.SetComp,
                          ast.GeneratorExp, ast.DictComp)):
            continue
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            return True
        stack.extend(ast.iter_child_nodes(n))
    return False


def _scope_name(item):
    ce = item.context_expr
    if isinstance(ce, ast.Call) and isinstance(ce.func, ast.Attribute):
        f = ce.func
        if isinstance(f.value, ast.Name) and f.value.id == "anyio" and f.attr in SCOPE_FUNCS:
            return "anyio." + f.attr
    return None


def scan_scope_across_yield(source, label="<src>"):
    """返回 [(行号, 函数名, scope)]——异步生成器里把 anyio scope 包住了 yield。"""
    out = []
    tree = ast.parse(source)
    for fn in [x for x in ast.walk(tree) if isinstance(x, ast.AsyncFunctionDef)]:
        if not _yields_in_block(fn.body):
            continue
        for w in [x for x in ast.walk(fn) if isinstance(x, ast.With)]:
            for item in w.items:
                nm = _scope_name(item)
                if nm and _yields_in_scope(w):
                    out.append((w.lineno, fn.name, nm))
    del label
    return out


def _yields_in_scope(with_node):
    return _yields_in_block(with_node.body)


def production_hits():
    hits, files, gens = [], 0, 0
    for d in PROD_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            if p.name == "__pycache__" or "__pycache__" in p.parts:
                continue
            files += 1
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for fn in [x for x in ast.walk(tree) if isinstance(x, ast.AsyncFunctionDef)]:
                if not _yields_in_block(fn.body):
                    continue
                gens += 1
                for (lineno, name, scope) in scan_scope_across_yield(
                        p.read_text(encoding="utf-8")):
                    hits.append("%s:%d %s() 的 %s 包住了 yield"
                                % (p.relative_to(ROOT), lineno, name, scope))
    return hits, files, gens


# ── ①　正向对照：扫描器必须抓得住旧形状（不然 0 命中＝假绿）────────────
def test_scanner_catches_old_shell_shape_positive_control():
    old_shape = (
        "import anyio\n"
        "class T:\n"
        "    async def await_message(self, timeout=60):\n"
        "        try:\n"
        "            with anyio.fail_after(timeout):\n"
        "                async for data in self._recv_reader:\n"
        "                    yield data\n"
        "        except TimeoutError:\n"
        "            yield {'error': 'x'}\n"
    )
    got = scan_scope_across_yield(old_shape)
    assert len(got) == 1, "正向对照失效：扫描器抓不到 scope 横跨 yield 的旧形状"
    assert got[0][1] == "await_message" and got[0][2] == "anyio.fail_after"

    move_on_shape = old_shape.replace("fail_after", "move_on_after")
    assert len(scan_scope_across_yield(move_on_shape)) == 1, \
        "move_on_after 同形未纳管（只钉 fail_after 会漏掉一半）"

    ok_shape = (
        "import anyio\n"
        "class T:\n"
        "    async def await_message(self, timeout=60):\n"
        "        while True:\n"
        "            with anyio.fail_after(1):\n"
        "                data = await self._recv_reader.__anext__()\n"
        "            yield data\n"
    )
    assert scan_scope_across_yield(ok_shape) == [], \
        "修复形状被误判（扫描器失去区分力＝任何版本都绿）"


# ── ②　类级不变量：生产宇宙零命中，且扫描面不许静默缩窄────────────────
def test_no_anyio_scope_across_yield_in_production():
    hits, files, gens = production_hits()
    assert not hits, "同类第 N 处又长回来：\n    " + "\n    ".join(hits)
    assert files >= 105, "扫描面缩窄（生产模块数 %d）——本钉会因此假绿" % files
    assert gens >= 17, "异步生成器宇宙缩窄（%d 个）——路径或识别式失效" % gens


# ── ③　行为验证：从真源码摘出两处壳，跨任务收口不得炸────────────────
def _extract_method(relpath, cls_name, meth_name, extra_ns=None):
    p = ROOT.joinpath(*relpath.split("/"))
    tree = ast.parse(p.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == cls_name)
    fn = next(n for n in cls.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == meth_name)
    ns = {"anyio": anyio, "time": time, "Dict": dict,
          "asyncio": asyncio, "_LOGGER": logging.getLogger("test.v177")}
    ns.update(extra_ns or {})
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(p), "exec"), ns)
    return ns[meth_name]


class _RecLogger:
    """stt 的壳里有一句 `except RuntimeError: self.logger.info(...)`——它会把
    "cancel scope in a different task" **吞成一条日志**。所以只判"异常没逃出
    生成器"的臂对该缺陷是瞎的（10-27 变异臂 A3 实抓：注入缺陷后行为臂照样绿）。
    本夹具把日志收下，判据同时看"没炸"与"没被吞"。"""

    def __init__(self):
        self.records = []

    def info(self, msg, *a, **k):
        self.records.append(str(msg))

    def warning(self, msg, *a, **k):
        self.records.append(str(msg))

    def debug(self, msg, *a, **k):
        pass


def _swallowed(records):
    return [r for r in records if "cancel scope" in r]


def _cross_task_close(meth, tr):
    """任务A 驱动到首块 yield、任务B aclose——现场报错的触发形。"""
    async def scenario():
        send, recv = anyio.create_memory_object_stream(1)
        tr._recv_reader = recv
        gen = meth(tr, 30)
        await send.send(SimpleNamespace(type="stt"))
        got = await gen.__anext__()
        err = "NOT-RUN"

        async def closer():
            nonlocal err
            try:
                await gen.aclose()
                err = None
            except BaseException as e:  # noqa: BLE001
                err = e

        await asyncio.create_task(closer())
        await send.aclose()
        return got, err, list(getattr(tr.logger, "records", []))

    return asyncio.run(scenario())


def test_stt_shell_survives_cross_task_close():
    meth = _extract_method("custom_components/huijian_ai/huijian/stt_transport.py",
                           "SttTransport", "await_message")
    log = _RecLogger()
    got, err, records = _cross_task_close(meth, SimpleNamespace(logger=log))
    assert err is None, "兼容壳仍把 scope 横跨 yield：任务B 收口炸 %r" % (err,)
    assert not _swallowed(records), \
        "异常没逃出生成器只是被本文件那句 except RuntimeError 吞掉了（吞＝整轮照样作废，" \
        "只是不留痕）：吞掉的内容 %s" % _swallowed(records)
    assert got.type == "stt"


def test_ws_shell_survives_cross_task_close():
    meth = _extract_method("custom_components/huijian_ai/huijian/ws_transport.py",
                           "WsTransport", "await_message")
    got, err, _rec = _cross_task_close(meth, SimpleNamespace(logger=_RecLogger()))
    assert err is None, "基类壳仍把 scope 横跨 yield：任务B 收口炸 %r" % (err,)
    assert got.type == "stt"


# ── ④　语义不劣化：超时→error 帧；流先关→安静收口（两臂同判）──────────
def _two_frames_then_close(meth, tr_ns, types=("tts", "stt")):
    async def scenario():
        send, recv = anyio.create_memory_object_stream(4)
        tr_ns._recv_reader = recv
        gen = meth(tr_ns, 30)
        for t in types:
            await send.send(SimpleNamespace(type=t))
        out = [await gen.__anext__()]
        try:
            out.append(await gen.__anext__())
        except StopAsyncIteration:
            out.append("ENDED")
        await send.aclose()
        return out

    return asyncio.run(scenario())


def test_stt_shell_skips_non_matching_frame_then_yields():
    meth = _extract_method("custom_components/huijian_ai/huijian/stt_transport.py",
                           "SttTransport", "await_message")
    # 过滤表是 ["stt","tts"]——要验"跳过不匹配的帧"，第一帧必须用表外类型
    # （旧形 `break`、新形 `return`：都是收够一帧就收口，不会产第二帧）。
    out = _two_frames_then_close(meth, SimpleNamespace(logger=logging.getLogger("t")),
                                 types=("text", "stt"))
    assert [o.type for o in out if hasattr(o, "type")] == ["stt"], \
        "旧形只认 ['stt','tts'] 两型——过滤语义被改动"
    assert "ENDED" in out, "壳在交付一帧后未收口（旧形 yield 后即 break）"


def test_shells_timeout_still_yields_error_frame():
    for rel, cls in (("custom_components/huijian_ai/huijian/stt_transport.py", "SttTransport"),
                     ("custom_components/huijian_ai/huijian/ws_transport.py", "WsTransport")):
        meth = _extract_method(rel, cls, "await_message")

        async def scenario():
            send, recv = anyio.create_memory_object_stream(1)
            tr = SimpleNamespace(_recv_reader=recv, logger=logging.getLogger("t"))
            gen = meth(tr, 1)
            got = await gen.__anext__()
            await send.aclose()
            return got

        got = asyncio.run(scenario())
        assert isinstance(got, dict) and got.get("error") == "Response timeout", \
            "%s 超时语义漂移（旧形超时回 error 帧）" % cls


def test_shells_stream_closed_ends_without_frame():
    for rel, cls in (("custom_components/huijian_ai/huijian/stt_transport.py", "SttTransport"),
                     ("custom_components/huijian_ai/huijian/ws_transport.py", "WsTransport")):
        meth = _extract_method(rel, cls, "await_message")

        async def scenario():
            send, recv = anyio.create_memory_object_stream(1)
            tr = SimpleNamespace(_recv_reader=recv, logger=logging.getLogger("t"))
            gen = meth(tr, 30)
            await send.aclose()          # 流先关：旧形是 async for 直接结束（不产帧）
            try:
                got = await gen.__anext__()
            except StopAsyncIteration:
                got = "ENDED"
            return got

        got = asyncio.run(scenario())
        assert got == "ENDED", "%s 在流关闭处多产了帧（语义改动）: %r" % (cls, got)


# ── ⑤　undefined-name 闸：闸必须存在、必须圈到本线、必须与 pytest 同步 ──
def test_ci_voice_has_ruff_gate_covering_voice_line():
    wf = (REPO / ".github" / "workflows" / "ci-voice.yaml").read_text(encoding="utf-8")
    # 只认**执行行**：`# ruff check ...` 是注释，用 "ruff check" in ln 会把被注释掉的
    # 哑门读成"闸在"（10-27 变异臂 A6 实抓：注释掉那一行，本钉照样绿）。
    blocks = [ln for ln in wf.splitlines() if ln.strip().startswith("ruff check")]
    commented = [ln for ln in wf.splitlines() if re.match(r"^\s*#\s*ruff check", ln)]
    assert blocks, ("语音线 CI 无**在跑的** lint 步——F821 这类形状缺陷就是从这里出厂的"
                    "（注释掉的门不算门：%s）" % (commented[:1] or "无"))
    line = blocks[0]
    assert "--select" in line and "F821" in line, "lint 步未纳管 undefined-name: %s" % line.strip()
    assert "E9" in line, "lint 步未纳管语法错误(E9): %s" % line.strip()
    assert "huijian_voice/" in line, "lint 步宇宙没圈到语音线: %s" % line.strip()
    assert "async with anyio.fail_after" not in line, "lint 命令行被写坏"
    i_ruff, i_pytest = wf.find("ruff check"), wf.find("python3 -m pytest huijian_voice/tests")
    assert i_pytest != -1, "语音线 CI 的 pytest 步不见了（闸存在但没人跑量具）"


def test_main_py_optional_name_is_bound():
    src = (ROOT / "core" / "main.py").read_text(encoding="utf-8")
    assert "from typing import Optional" in src.split("class ")[0], \
        "main.py 未把 Optional 引到模块作用域（F821 复发）"
    assert "Optional[" in src, "Optional 的使用点不见了——上一条会因空宇宙假绿"


def test_custom_llm_api_er_visible_to_static_check_only():
    src = (ROOT / "custom_components" / "huijian_ai" / "custom_llm_api.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    tc = [n for n in tree.body
          if isinstance(n, ast.If) and getattr(n.test, "id", "") == "TYPE_CHECKING"]
    assert tc, "er 的 TYPE_CHECKING 块不见了：静态检查会重新报 F821"
    names = {a.asname or a.name for s in tc[0].body
             if isinstance(s, (ast.Import, ast.ImportFrom)) for a in s.names}
    assert "er" in names, "TYPE_CHECKING 块里没有 er 别名"
    assert "from __future__ import annotations" in src.split("import logging")[0], \
        "可导入性前提（future annotations）被拔掉：整包 import 即 NameError"
