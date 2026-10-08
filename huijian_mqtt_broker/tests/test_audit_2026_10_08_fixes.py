"""2026-10-08 网关审计 · A 档收口钉（B-4 / B-5 / B-6 / B-8 / B-9）。

共同口径取自本仓既有纪律：机制型静态钉只保形状 ⇒ 每条都配"抽出真实现真跑"的
行为臂（B-8 把 CI 的 python 块抽出来在假树里执行、B-6 注入假 zeroconf 加载真
脚本、B-4 走真 MRO 装父类探针），字样比对只在 B-5 当辅助判据用。
"""
import ast
import contextlib
import importlib.util
import io
import json
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "window_controller_gateway"
REPO = ROOT.parent
CI_YAML = REPO / ".github" / "workflows" / "ci.yaml"


# ============ B-8：CI 的 YAML 语法门必须真覆盖 workflow 自己 ============

def _ci_yaml_gate_source() -> str:
    """抽出 ci.yaml 里 "Check YAML files syntax" 那段真 python（不在测试里重抄
    判据——重抄的那份永远绿，门怎么漏的都不知道）。"""
    import yaml
    wf = yaml.safe_load(CI_YAML.read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["lint"]["steps"]
                if s.get("name") == "Check YAML files syntax")
    body = step["run"].split("python3 -c '")[1].rsplit("'", 1)[0]
    assert "'" not in body, "python 块里的裸单引号会截断 shell 的 '...'"
    return body


def _run_gate(src, workdir, monkeypatch):
    monkeypatch.chdir(workdir)
    buf = io.StringIO()
    rc = 0
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(src, "ci-yaml-gate", "exec"), {"__name__": "__main__"})
    except SystemExit as exc:  # 门用 sys.exit(1) 报失败
        rc = exc.code or 0
    return rc, buf.getvalue()


def _mk(workdir, rel, text):
    p = workdir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_ci_yaml_gate_covers_dotgithub_workflows(tmp_path, monkeypatch):
    """Python glob 的 `*` 不匹配以点开头的目录 ⇒ 旧写法的门从没碰过 workflow
    自己（本机实证：12 个文件、0 个来自 .github）。判据只认真跑的输出。"""
    _mk(tmp_path, ".github/workflows/ci.yaml", "jobs:\n  lint:\n    steps: []\n")
    _mk(tmp_path, "repository.yaml", "repositories: []\n")
    rc, out = _run_gate(_ci_yaml_gate_source(), tmp_path, monkeypatch)
    assert rc == 0, out
    assert ".github/workflows/ci.yaml" in out, "workflow 自身未受校验（B-8 复发）"
    assert "repository.yaml" in out


def test_ci_yaml_gate_parses_not_just_lists(tmp_path, monkeypatch):
    """只把文件名列进门、不真解析＝假绿：坏 YAML 必须让门非零退出。"""
    _mk(tmp_path, ".github/workflows/broken.yaml", "key: [unclosed\n")
    rc, out = _run_gate(_ci_yaml_gate_source(), tmp_path, monkeypatch)
    assert rc != 0, out
    assert "❌" in out


def test_ci_yaml_gate_is_loud_when_narrowed(tmp_path, monkeypatch):
    """门变窄必须响亮失败，而不是安静少校验两个文件。

    变异臂：把 files 的两路 glob 改回旧实现那一路。同一份假树里未变异的门必须
    过、变异后的门必须红——证明"自校验"那段不是装饰。
    """
    _mk(tmp_path, ".github/workflows/ci.yaml", "jobs: {}\n")
    src = _ci_yaml_gate_source()
    narrowed = src.replace('for pat in ("**/*.yaml", ".github/**/*.yaml")',
                           'for pat in ("**/*.yaml",)')
    assert narrowed != src, "变异点没落在盘上实际交付的形状上（钉已失效）"
    rc, out = _run_gate(narrowed, tmp_path, monkeypatch)
    assert rc != 0, "门被改窄却无人报——旧写法的静默形态"
    assert "漏校验" in out


def test_real_repo_passes_the_yaml_gate(monkeypatch):
    """本仓现状必须过这道门。ed641cf 事后改过 ci-voice.yaml 而当时无门可把它——
    这条就是那个缺口的存证：workflow 写坏，本地 test 就该红。

    判据是"workflow 自身与集成树都在覆盖面里"，**不是全仓 yaml 个数**：
    变异矩阵的 `_copy()` 只带 huijian_mqtt_broker/.github/docs＋四个根文件，不带
    huijian_voice/ ⇒ 上一版钉 `>= 14` 在副本里必然假红（本轮整跑实发）。
    """
    rc, out = _run_gate(_ci_yaml_gate_source(), REPO, monkeypatch)
    assert rc == 0, out
    assert ".github/workflows/ci.yaml" in out, out
    assert "huijian_mqtt_broker/config.yaml" in out, out


# ============ B-4：生命周期钩子必须链补 await super() ============

def _hook_overrides(hook):
    """(文件, 类名, 函数节点) —— 集成树内该钩子的全部覆写，含将来新增的。"""
    found = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for fn in cls.body:
                if isinstance(fn, ast.AsyncFunctionDef) and fn.name == hook:
                    found.append((path.name, cls.name, fn))
    return found


def _chains_super(fn, hook):
    for node in ast.walk(fn):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        inner = call.func.value
        if (call.func.attr == hook and isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name) and inner.func.id == "super"):
            return True
    return False


@pytest.mark.parametrize("hook", ["async_added_to_hass", "async_will_remove_from_hass"])
def test_lifecycle_hook_overrides_chain_super(hook):
    """base_entity v1.6.9 立的规：断链会静默跳过挂在 MRO 上的 mixin 钩子。
    钉语法不钉字样——注释里提一句方法名就"过检"的那类假绿不再发生。"""
    sites = _hook_overrides(hook)
    assert sites, f"集成里没有任何 {hook} 覆写？扫描面本身出问题了"
    broken = [f"{f}::{c}.{fn.name}" for f, c, fn in sites if not _chains_super(fn, hook)]
    assert not broken, f"断链（未来 HA 重构/新 mixin 会被静默跳过）: {broken}"


@pytest.mark.asyncio
async def test_gateway_sensor_remove_hook_actually_calls_super(monkeypatch):
    """行为臂：真 MRO 上装一个记录用的父类实现，等一次移除 ⇒ 必须被链到。"""
    import custom_components.window_controller_gateway.gateway as gateway_mod
    from homeassistant.components.binary_sensor import BinarySensorEntity

    calls = []

    async def _record(self):
        calls.append("super")

    monkeypatch.setattr(BinarySensorEntity, "async_will_remove_from_hass",
                        _record, raising=False)
    removed = []
    sensor = object.__new__(gateway_mod.GatewayOnlineSensor)
    sensor.mqtt_handler = SimpleNamespace(
        remove_status_callback=lambda cb: removed.append(cb))
    sensor._on_status_change = "callback-sentinel"

    await sensor.async_will_remove_from_hass()

    assert removed == ["callback-sentinel"], "回调摘除是本钩子的本职，不得丢"
    assert calls == ["super"], "async_will_remove_from_hass 未 await super()"


# ============ B-5：不得留"声明了却没人用"的锁 ============

def test_device_manager_carries_no_unused_migration_lock():
    """迁移全程无锁是**已知现状**（本轮裁决定的是删声明，不是接锁：真加锁要给
    async_reload + 映射整表改写那一段设计锁面，锁错即死锁，而并发入口未复现）。
    留着声明才是缺陷——后人会以为有保护。"""
    src = (PKG / "device_manager.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next((n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef)
                and n.name == "WindowControllerDeviceManager"), None)
    assert cls is not None, "WindowControllerDeviceManager 类锚丢失"
    init = next((fn for fn in cls.body
                 if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and fn.name == "__init__"), None)
    assert init is not None, "__init__ 锚丢失"
    assigned = {t.attr for t in ast.walk(init)
                if isinstance(t, ast.Attribute) and isinstance(t.ctx, ast.Store)}
    for dead in ("_migration_lock", "_is_migrating"):
        assert dead not in assigned, f"{dead} 又被声明了"
        assert dead not in src, f"{dead} 字样回到 device_manager.py"


# ============ B-6：IP 变化重注册后必须回写状态文件 ============

def _load_mdns_with_stub():
    """注入假 zeroconf 再加载真脚本。环境装没装 zeroconf 都要能跑——装了真库
    才跑的写法会在 CI 上静默 SKIP（＝这条钉从没执行过却报绿）。"""
    class _StubInfo:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    module = types.ModuleType("zeroconf")
    module.ServiceInfo = _StubInfo
    module.Zeroconf = lambda *a, **kw: None
    old = sys.modules.get("zeroconf")
    sys.modules["zeroconf"] = module
    try:
        spec = importlib.util.spec_from_file_location(
            "mdns_publisher_b6_probe", ROOT / "mdns_publisher.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            sys.modules.pop("zeroconf", None)
        else:
            sys.modules["zeroconf"] = old
    return mod


class _FakeZeroconf:
    def __init__(self, register_raises=None):
        self.register_raises = register_raises
        self.registered = []
        self.unregistered = []

    def register_service(self, info):
        if self.register_raises is not None:
            raise self.register_raises
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)


def test_ip_change_reregister_writes_status_with_new_ip(tmp_path, monkeypatch):
    """旧实现只 print ⇒ 状态文件永远停在首次注册的旧 IP，而集成侧提示卡读的
    正是文件里的 local_ip（mqtt_bootstrap 只取 state 与 local_ip 两格）。"""
    mod = _load_mdns_with_stub()
    status = tmp_path / mod.STATUS_FILENAME
    monkeypatch.setattr(mod, "_status_path", lambda: str(status))
    zc = _FakeZeroconf()
    old_info = object()

    info = mod.reregister_service(zc, old_info, "10.0.0.9", 2022)

    data = json.loads(status.read_text(encoding="utf-8"))
    assert data["state"] == "ok" and data["local_ip"] == "10.0.0.9", data
    assert info.kwargs["addresses"] == [socket.inet_aton("10.0.0.9")]
    assert zc.unregistered == [old_info] and zc.registered == [info]


def test_ip_change_failure_does_not_claim_ok(tmp_path, monkeypatch):
    """注册失败必须向上抛且**不回写**：撞名/失败分支若把新 IP 写成 ok，提示卡
    会在广播根本没生效时被清掉（sys.exit 归因链一并失效）。"""
    mod = _load_mdns_with_stub()
    status = tmp_path / mod.STATUS_FILENAME
    monkeypatch.setattr(mod, "_status_path", lambda: str(status))
    zc = _FakeZeroconf(register_raises=RuntimeError("register failed"))

    with pytest.raises(RuntimeError):
        mod.reregister_service(zc, object(), "10.0.0.9", 2022)

    assert not status.exists(), "注册失败却写了状态文件"


def test_main_ip_change_branch_is_wired():
    """接线钉：`reregister_service` 有测试不等于 main 在用例走的那条路调它。
    只收口 IP 变化那一支——首次注册留在 main 里是既有形状，不算漏。"""
    tree = ast.parse((ROOT / "mdns_publisher.py").read_text(encoding="utf-8"))
    main = next(fn for fn in tree.body
                if isinstance(fn, ast.FunctionDef) and fn.name == "main")
    branch = None
    for node in ast.walk(main):
        test = ast.unparse(node.test) if isinstance(node, ast.If) else ""
        if "current" in test and "local_ip" in test:
            branch = node
            break
    assert branch is not None, "IP 变化分支锚丢失（`current != local_ip` 判据被改写？）"
    body_src = ast.unparse(branch)
    assert body_src.count("reregister_service(") == 1, body_src
    for dead in ("zeroconf.register_service(", "zeroconf.unregister_service("):
        assert dead not in body_src, f"IP 变化分支里仍有内联 {dead}——没走收口函数"
    assert "zeroconf.register_service(" in ast.unparse(main), \
        "首次注册点消失了——那意味着收口改到了不该改的地方"


# ============ B-9：HA 的英文面只认 translations/en.json ============


def _walk(obj, prefix=""):
    out = []
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        out.extend(_walk(value, path) if isinstance(value, dict) else [(path, value)])
    return out


@pytest.fixture(scope="module")
def en_and_src():
    src = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))
    en_path = PKG / "translations" / "en.json"
    assert en_path.is_file(), (
        "缺 translations/en.json：HA 2024.12 helpers/translation.py 只按 "
        "translations/{lang}.json 取文件，英文没有 strings.json 回落，找不到就"
        "原样回吐 translation key")
    return json.loads(en_path.read_text(encoding="utf-8")), src


def test_en_json_is_english_not_a_copy_of_the_chinese_source(en_and_src):
    """本仓 strings.json 装的是中文（违 HA 约定，但不是本轮的事）。加载项面的
    translations/en.yaml 是真英文（0 汉字）并由 test_v1715 把键面钉住——集成面
    的 en.json 同口径。把 strings.json 复制成 en.json 会被这条打红。"""
    en, _src = en_and_src
    text = json.dumps(en, ensure_ascii=False)
    assert not any("一" <= ch <= "鿿" for ch in text), "en.json 里出现汉字"


def test_en_json_key_paths_match_strings_json(en_and_src):
    en, src = en_and_src
    en_paths = [p for p, _ in _walk(en)]
    src_paths = [p for p, _ in _walk(src)]
    assert set(src_paths) - set(en_paths) == set(), \
        f"en.json 漏键: {sorted(set(src_paths) - set(en_paths))}"
    assert set(en_paths) - set(src_paths) == set(), \
        f"en.json 幽灵键: {sorted(set(en_paths) - set(src_paths))}"
    assert en_paths == src_paths, "en.json 与 strings.json 键序漂移（diff 噪音）"


def test_en_json_placeholders_match_per_key(en_and_src):
    """占位符比文案容易翻车：少一个 {gateway_sn} 是渲染期 KeyError，不是显示问题。"""
    import re
    en, src = en_and_src
    en_leaves, src_leaves = dict(_walk(en)), dict(_walk(src))
    for path, zh in src_leaves.items():
        want = set(re.findall(r"\{(\w+)\}", str(zh)))
        got = set(re.findall(r"\{(\w+)\}", str(en_leaves[path])))
        assert want == got, f"{path} 占位符漂移 src={want} en={got}"


def test_en_json_covers_the_keys_the_ui_actually_reads(en_and_src):
    """把既有中文钉的三个具体键在 en 面同样钉上（选项菜单 + broker_not_ready +
    fix_flow.confirm），否则"文件在、内容空"也算过。"""
    en, _src = en_and_src
    menu = en["options"]["step"]["init"]["menu_options"]
    assert menu.get("add_gateway") and menu.get("options")
    assert en["config"]["error"]["broker_not_ready"]
    for issue in ("mqtt_channel_broken", "mqtt_bootstrap_pending"):
        node = en["issues"][issue]
        assert node["fix_flow"]["step"]["confirm"]["description"], issue
        assert node["fix_flow"]["error"]["still_broken"], issue


# ============ 版本第五源：README 徽章的文案与链接必须一起 bump ============

def test_readme_badge_text_and_link_bump_together():
    """版本"五源"里的第五源是 README 静态徽章（迁仓后 `/releases/latest` 被语音
    占用，"latest 型"徽章会把网关卡片显示成语音版本号，所以只能钉死版本号）。
    既有钉只比 `badge/version-v<ver>-blue` 的**文案**，没比包着它的
    `releases/tag/v<ver>` **链接** ⇒ 文案 bump 而链接停在上一版，点徽章跳到旧
    Release（本轮 bump 实发）。两半必须同源同值。
    """
    import re
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    want = re.search(r'^version:\s*"([\d.]+)"', cfg, re.M).group(1)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"badge/version-v([\d.]+)-blue\)\]\(\S*?/releases/tag/v([\d.]+)\)",
                  readme)
    assert m, "README 徽章形制变了——文案与链接的正向锚都找不到"
    assert m.group(1) == want == m.group(2), \
        f"徽章文案 v{m.group(1)} / 链接 v{m.group(2)} / config.yaml {want} 三者必须一致"
