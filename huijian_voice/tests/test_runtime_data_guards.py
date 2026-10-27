"""runtime_data 缺席守卫钉桩（2026-09-08 台架实发回归）。

背景：HA core 的删除条目流程是 async_unload（成功后 core 执行
object.__delattr__(entry, "runtime_data")）→ component.async_remove_entry。
本 fork 的 assist 分支裸访问 entry.runtime_data（huijian/__init__.py
get_entry_data）与 diagnostics.py，在"删除 assist 条目"时必抛
AttributeError: 'ConfigEntry' object has no attribute 'runtime_data'
（台架 logbook 实发 "Error calling entry remove callback"）。
setup 失败的条目同样从未 set 过该属性（类级 annotation 无缺省值）。

本测试用 sys.modules stub 真 import 被测模块，构造无 runtime_data 的
entry 对象直调——即实发故障的最小复现；另附源码级钉桩防回退。
"""

import ast
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

CC = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"


def _load_huijian_pkg():
    """把 custom_components/huijian_ai/huijian/__init__.py 以 stub 依赖载入。"""
    if "huijian_under_test" in sys.modules:
        return sys.modules["huijian_under_test"]

    # stub homeassistant 依赖面（本机无 homeassistant 包，见 CLAUDE.md 测试环境）
    ha = types.ModuleType("homeassistant")
    exc = types.ModuleType("homeassistant.exceptions")

    class ConfigEntryAuthFailed(Exception):
        pass

    exc.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    helpers = types.ModuleType("homeassistant.helpers")
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    iid = types.ModuleType("homeassistant.helpers.instance_id")
    er.async_entries_for_config_entry = lambda *a, **k: []
    er.async_get = lambda *a, **k: None
    iid.async_get = lambda *a, **k: "stub-haid"
    helpers.entity_registry = er
    helpers.instance_id = iid
    ha.exceptions = exc
    ha.helpers = helpers
    for name, mod in {
        "homeassistant": ha,
        "homeassistant.exceptions": exc,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_registry": er,
        "homeassistant.helpers.instance_id": iid,
    }.items():
        sys.modules.setdefault(name, mod)

    # 伪父包 + const 子模块（供 `from ..const import DOMAIN` 相对导入）
    parent = types.ModuleType("cut_parent")
    parent.__path__ = [str(CC)]
    const = types.ModuleType("cut_parent.const")
    const.DOMAIN = "huijian_ai"
    parent.const = const
    sys.modules.setdefault("cut_parent", parent)
    sys.modules.setdefault("cut_parent.const", const)

    pkginit = CC / "huijian" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "cut_parent.huijian", pkginit, submodule_search_locations=[str(pkginit.parent)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cut_parent.huijian"] = mod
    spec.loader.exec_module(mod)
    sys.modules["huijian_under_test"] = mod
    return mod


def test_get_entry_data_assist_survives_missing_runtime_data():
    """最小复现：删除 assist 条目时 core 已 del runtime_data，不得再抛。"""
    m = _load_huijian_pkg()
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(data={"config_type": "assist"})  # 无 runtime_data 属性
    assert m.get_entry_data(hass, entry, "mcp_transport") is None
    assert m.get_entry_data(hass, entry) == {}
    assert m.get_entry_data(hass, entry, "x", pop=True) is None
    assert m.get_entry_data(hass, entry, "x", set_default=7) == 7


def test_get_entry_data_assist_with_runtime_data_roundtrips():
    m = _load_huijian_pkg()
    runtime = {"mcp_transport": "sentinel"}
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(data={"config_type": "assist"}, runtime_data=runtime)
    assert m.get_entry_data(hass, entry, "mcp_transport") == "sentinel"
    assert m.get_entry_data(hass, entry, "mcp_transport", pop=True) == "sentinel"
    assert "mcp_transport" not in runtime


def test_get_entry_data_device_uses_hass_data_unchanged():
    m = _load_huijian_pkg()
    runtime = SimpleNamespace()
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="e1", data={"config_type": "device"},
                            runtime_data=runtime)
    d = m.get_entry_data(hass, entry)
    d["mcp_transport"] = "dev"
    assert hass.data["huijian_ai"]["e1"]["mcp_transport"] == "dev"
    assert m.get_entry_data(hass, entry, "mcp_transport") == "dev"


def _src(rel):
    return (CC / rel).read_text(encoding="utf-8")


def test_source_pins_no_bare_runtime_data_in_remove_family():
    """防回退：remove/diagnostics 路径禁止裸 entry.runtime_data 访问。"""
    hj = _src("huijian/__init__.py")
    assert 'getattr(entry, "runtime_data", None)' in hj, (
        "get_entry_data assist 分支必须保留缺席守卫（2026-09-08 实发回归）"
    )
    diag = _src("diagnostics.py")
    assert 'getattr(config_entry, "runtime_data", None)' in diag
    assert "entry not loaded" in diag, "未加载条目诊断需给降级说明而非抛异常"


def test_source_pin_mcp_closed_at_unload():
    """mcp_transport 关闭时点必须在 unload（assist 的 runtime_data 尚存活），
    remove 回调时 core 已删数据、清理不可达。"""
    init = _src("__init__.py")
    i = init.index("async def async_unload_entry")
    j = init.index("async def async_remove_entry")
    seg = init[i:j]
    assert '"mcp_transport"' in seg, "async_unload_entry 必须一并关闭 mcp_transport"


def test_source_pin_satellite_validation_error_hint():
    """validation-error + 域内无 assist 条目 → HA 日志必须给修复指引
    （定案：不静默补建被用户删除的条目，但两端都要看得见原因）。"""
    sat = _src("assist_satellite.py")
    assert '"validation-error"' in sat and "语音助手" in sat


def test_source_pin_auto_ensure_no_latch():
    """v1.0.16 语义修正：自动补建改幂等检查（有则不碰、缺则补建），
    _AUTO_ASSIST_DONE 一次性闩锁删除——它曾令"用户手删引擎后永不自愈"
    （2026-09-08 台架两轮实发 validation-error 失聪）。"""
    init = _src("__init__.py")
    assert "_AUTO_ASSIST_DONE" not in init, "闩锁字段必须整体移除（不再读也不再写）"
    assert "auto_assist_done" not in init, "entry.data 键字面量同删"
    assert 'CONF_CONFIG_TYPE\n        ) == "assist"' in init or (
        '== "assist"' in init and "async_entries(DOMAIN)" in init
    ), "幂等检查（存在 assist 条目即早退）必须保留"


def test_source_pin_conversation_timeout_sync_cm():
    """conversation.py 超时守卫必须用 anyio 的**同步**上下文管理器——fail_after /
    move_on_after 都是同步 CM，写成 async with 时 huijian_agent 每次意图识别必抛
    TypeError（2026-09-08 台架实发，agent 全瘫）。

    v1.7.7 重指（外部报告复核抓出的"钉被注释喂绿"）：旧钉正向格是
    `"with anyio.fail_after(timeout)" in conv`，而 v1.1.27 把整轮收口改成"单调
    deadline + 逐条 move_on_after"后代码里已无此串——子串只活在函数 docstring
    引的"旧实现"里，于是这条钉从此靠散文满足＝拔钉。改为 AST 取**真函数体**判定：
    原不变量（同步 CM、绝不 async with）照守，并补上"scope 不许横跨 yield"，
    且落点/宇宙两格缺任何一格都点名，不再可能被注释或空宇宙喂绿。
    """
    conv = _src("conversation.py")
    assert "async with anyio." not in conv
    tree = ast.parse(conv)

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_await_message_with_timeout"), None)
    assert fn is not None, "找不到 _await_message_with_timeout（钉失去落点＝假绿）"
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)], \
        "_await_message_with_timeout 体内出现 AsyncWith——anyio 同步 CM 写成 " \
        "async with 时每次意图识别必抛 TypeError（2026-09-08 台架实发）"

    def _yields_in(body):
        stack = list(body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                              ast.Lambda)):
                continue
            if isinstance(n, (ast.Yield, ast.YieldFrom)):
                return True
            stack.extend(ast.iter_child_nodes(n))
        return False

    assert _yields_in(fn.body), \
        "_await_message_with_timeout 体内已无 yield——本钉在守空气（落点换代要重指）"
    scopes = [w for w in ast.walk(fn) if isinstance(w, ast.With)
              and any(_is_anyio_cm(it.context_expr) for it in w.items)]
    assert scopes, "体内没有 anyio 超时 CM 的 With 节点（守卫被整条撤掉）"
    bad = [w.lineno for w in scopes if _yields_in(w.body)]
    assert not bad, ("超时 scope 横跨 yield（第 %s 行）：跨任务收口必炸 "
                     "'cancel scope in a different task'" % bad)


def _is_anyio_cm(expr):
    """anyio.fail_after(...) / anyio.move_on_after(...) 形态判定。"""
    return (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
            and isinstance(expr.func.value, ast.Name)
            and expr.func.value.id == "anyio"
            and expr.func.attr in ("fail_after", "move_on_after"))


def test_source_pin_text_tts_nonblocking_import():
    """text.py 的 edge_tts 惰性导入必须走 import-executor——模块顶层在循环内
    import edge_tts 会级联 certifi load_verify_locations 阻塞调用
    （2026-09-08 台架 2026.8 实发 Detected blocking call 警告）。"""
    txt = _src("text.py")
    assert "import edge_tts" not in txt.split("_play_tts", 1)[1].split("def ", 1)[0] \
        or "async_add_import_executor_job" in txt
    assert "async_add_import_executor_job" in txt
