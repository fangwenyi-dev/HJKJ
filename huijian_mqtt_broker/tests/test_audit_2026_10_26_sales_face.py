# -*- coding: utf-8 -*-
"""2026-10-26 销售面批判据（网关 v1.8.8）。

体检出处（逐条在盘上复现后才立）：
  F5 面板连接栏写「查看/修改」，而 `run.sh` 启动即把内置凭据恢复回去 ⇒ 同页另一行
     早已写「无需修改…改动会被启动时自动恢复」——同一页两处口径相反，客户照前者改
     只会把自家网关挡在门外。
  F3 `DOCS.md`/插件 README 让人走「慧尖 → 选项 → 高级选项」，而 `config_flow.py` 按
     `CONF_GATEWAY_SN` 分流：**已配好网关的条目直接进选项页**，那层菜单只在还没配
     网关的条目上出现 ⇒ 正常客户找不到文档说的入口。
  F8 直连监听的端口/令牌只取**第一个已启用条目**（代码注释与 README 已披露），
     但用户在表单里看到的字段说明没有这一句 ⇒ 两台网关时改第二台不生效且无从解释。
  F6 `remove_device` 文档承诺 True＝幂等成功，内部三类**改了状态**的失败只 error 日志
     后照样 `return True`，而两处调用方只 catch 异常 ⇒ 谎报删除成功（幽灵设备燃料）。
  F7 reload 让出窗内点「删除」：后端 warning 后静默 return，`button.press` 回 200，
     面板弹**绿色**「删除命令已发送」。

形制：文案判"承载结论的那一行"（全文子串会被同文件散文喂绿）；F6/F7 一律**行为臂**
（构造真失败注入真函数），并各带一条阳性对照防把闸做太宽。
"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.const as c
from custom_components.window_controller_gateway.device_manager import (
    WindowControllerDeviceManager)
from custom_components.window_controller_gateway.gateway import (
    GatewayDeviceRemoveButton)
from homeassistant.exceptions import HomeAssistantError

HERE = Path(__file__).resolve().parent
BROKER = HERE.parent
REPO_ROOT = BROKER.parent
GW_DIR = BROKER / "custom_components" / "window_controller_gateway"
JS = (BROKER / "www" / "js" / "huijian.js").read_text(encoding="utf-8")

GW_SN = "50022E00AABBCC"
DEV_SN = "50022E019999"


# ══════════════════ F5：面板不许再邀请客户改内置凭据 ══════════════════
def _lines_containing(path, needle):
    text = path.read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if needle in ln]


def test_panel_credential_lines_say_view_only():
    """承载结论的两行（连接栏 + 说明框）：必须出现"勿改/自动恢复"，且不得再出现"查看/修改"。

    判据只吃**写着这条指令的那两行**（锚 `标签页中查看`）——把相邻的 `MQTT 凭据` 标签行
    也算进来会钉出一条与本刀无关的红（第一版就踩了：标签行当然不含"勿改"）。"""
    hits = _lines_containing(BROKER / "www" / "index.html", "标签页中查看")
    assert len(hits) == 2, f"面板凭据指令行应为 2 条，实见 {len(hits)}——判据失去对象"
    for ln in hits:
        assert "查看/修改" not in ln, f"仍在邀请修改内置凭据：{ln.strip()[:90]}"
        assert ("勿改" in ln) or ("请勿修改" in ln) or ("自动恢复" in ln), \
            f"凭据行必须交代「改动会被启动时自动恢复」：{ln.strip()[:90]}"


# ══════════════════ F3：文档指的入口必须是真能走到的那条 ══════════════════
def test_docs_do_not_point_at_an_unreachable_options_menu():
    for rel in ("DOCS.md", "README.md"):
        lines = (BROKER / rel).read_text(encoding="utf-8").splitlines()
        bad = [ln for ln in lines if "选项 → 高级选项" in ln]
        assert not bad, f"huijian_mqtt_broker/{rel} 仍让人走『选项 → 高级选项』" \
                        f"（已配网关的条目没有这层菜单）：{[b.strip()[:60] for b in bad]}"
        good = [ln for ln in lines if "⋮" in ln and "配置" in ln]
        assert good, f"huijian_mqtt_broker/{rel}: 没有一行写明可达入口「条目 ⋮ → 配置」"


# ══════════════════ F8：字段说明要披露"第一个已启用条目" ══════════════════
def test_ws_field_help_discloses_first_enabled_entry():
    """四份（strings + 三份翻译）× 两键（port/token）都得有这句——少一处就是漏那一处。
    判据用**运行态的真判据**（`ws_gateway_wanted` 跳过 disabled 与关了直连的条目，
    ws_gateway.py:1036-1040），不是"已启用条目"这种半句；令牌那半还要说清
    表单只写一条、小程序侧写全部（:764-768）——这两处口径不同是有意的，不写就是误导。"""
    needles = {"zh": "已启用且开着直连", "en": "first enabled entry"}
    files = [("strings.json", "zh"),
             ("translations/zh-CN.json", "zh"),
             ("translations/zh-Hans.json", "zh"),
             ("translations/en.json", "en")]
    for rel, lang in files:
        data = json.loads(GW_DIR.joinpath(*rel.split("/")).read_text(encoding="utf-8"))
        # 定位承载这两个键的那层（不猜形状：找到含这俩键的 dict）
        holder = _find_holder(data, ("ws_gateway_port", "ws_gateway_token"))
        assert holder is not None, f"{rel}: 找不到 ws_gateway_port/token 的说明层"
        for key in ("ws_gateway_port", "ws_gateway_token"):
            desc = holder.get(key) or ""
            assert needles[lang] in desc, \
                f"{rel}:{key} 未披露「只采纳第一个已启用且开着直连的条目」：{desc[:80]}"
        tok = holder["ws_gateway_token"]
        both = (("全部" in tok and "只写" in tok) if lang == "zh"
                else ("only the entry you edit" in tok and "every" in tok))
        assert both, f"{rel}:ws_gateway_token 没说清「表单只写这一条 / 小程序侧写全部」：{tok[:110]}"


def _find_holder(node, keys):
    if isinstance(node, dict):
        if all(k in node for k in keys):
            return node
        for v in node.values():
            got = _find_holder(v, keys)
            if got is not None:
                return got
    elif isinstance(node, list):
        for v in node:
            got = _find_holder(v, keys)
            if got is not None:
                return got
    return None


# ══════════════════ C9：网关链的正文截断闸（语音线 1.2.1 就立过，网关线一直没有）══════════════════
def test_gateway_release_body_not_truncated():
    """本版 CHANGELOG 整段必须落在 GitHub 侧 awk 的 `head -N` 之内。

    实锤过的形状：本链**两处载体口径不一**——GitHub 侧 awk 有 `head -N`，
    gitee-release job 却自己按半区取**整段不截**（`log[i:i + 1 + j]`）；
    1.8.7 那版整段 44 行 < 上限 50 侥幸两源相同，下一版正文一长就静默"Gitee 全文、GitHub 半篇"。
    本钉把"侥幸"换成判据：行数超限当场红，且**上限被调小也当场红**（变异自证 M7）。"""
    wf = (REPO_ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    caps = [int(m) for m in re.findall(r"CHANGELOG\.md \| head -(\d+)", wf)]
    assert caps, "ci.yaml 里找不到 CHANGELOG 提取链的 head 上限——提取形状变了，本钉失去对象"
    # 前提也要钉：Gitee 侧仍是"整段不截"。它哪天改成截断，本钉的口径就得跟着重写，
    # 不能留着一句"两源同正文"的假话。
    # ⚠ 判整行形状，不判裸标识符：ci.yaml 里那句话现在**也出现在我自己加的注释**里，
    # 只 `in wf` 会被散文喂绿（变异臂 M8 实发：把代码改成 `log[i:i + 40]` 本钉照样绿）。
    gitee_lines = [l.strip() for l in wf.splitlines() if l.strip().startswith("body = log[")]
    assert len(gitee_lines) == 1, \
        f"Gitee 侧正文取值语句应有且只有一处，实见 {len(gitee_lines)}：{gitee_lines}"
    assert gitee_lines[0] == "body = log[i:i + 1 + j].strip()", (
        f"gitee-release 侧不再取整段（现为 {gitee_lines[0]!r}）——"
        "两源正文口径变了，请同步重写本钉与 ci.yaml 的注释，别留着假话")

    cfg = (BROKER / "config.yaml").read_text(encoding="utf-8")
    m = re.search(r'^version:\s*"([^"]+)"', cfg, re.M)      # 与 CI 同口径（grep '^version:' + 取引号内）
    assert m, "config.yaml 的 ^version: 行格式漂移——本钉取不到本版号"
    ver = m.group(1)
    lines = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    banner = next((i for i, l in enumerate(lines) if l.startswith("# 慧尖HA语音插件 变更日志")),
                  len(lines))
    start = next((i for i in range(banner) if lines[i].startswith(f"## [{ver}]")), None)
    assert start is not None, \
        f"CHANGELOG 网关区没有 [{ver}] 段——Release 正文会静默回落成 git log 流水"
    end = next((i for i in range(start + 1, banner) if lines[i].startswith("## [")), banner)
    n = end - start
    cap = min(caps)
    assert n <= cap, (
        f"本版网关正文 {n} 行 > GitHub 侧 head -{cap} ⇒ Release 会静默截到第 {cap} 行，"
        f"而 Gitee 侧取整段＝两源正文不等长；抬上限或压缩正文，二选一")


# ══════════════════ G1：持久化写盘失败要入账，但不许上抛 ══════════════════
@pytest.mark.asyncio
async def test_persist_failure_is_recorded_not_raised(tmp_path):
    """落盘失败**记账不上抛**（2026-10-26 复核实发后的定形）。

    为什么不是 `raise`：`__init__.py` 有四处 `await save_persistent_data(hass)`，
    其中三处在条目卸载/清理链上——一抛就把"这次没存住"升级成"这条集成卸不掉"。
    为什么不能像过去那样只 `_LOGGER.error`：`device_manager._log_save_result`
    （"本次内存变更未落盘"）因此永远是死码，调用方收到的都是"已保存"，
    盘满/只读时内存改了盘上没改 ⇒ 重启后 002 把设备建回来＝幽灵设备。"""
    from custom_components.window_controller_gateway import persist

    hass = SimpleNamespace(
        config=SimpleNamespace(config_dir=str(tmp_path / "不存在" / "更深")),  # 必失败
        data={},
    )
    persist.take_last_save_error()                      # 清场
    await persist.save_persistent_data(hass)            # 不许抛
    err = persist.take_last_save_error()
    assert err, "写盘失败必须留下可点名的账（本钉就是那条死码的复活证明）"
    assert persist.take_last_save_error() is None, "账要取走即清，否则下一次失败会被旧账掩盖"


def test_removal_paths_adopt_the_accounting_helper():
    """按类扫：裸 `async_create_task(save_persistent_data(...))` 不留一处——
    没接住账的调度点，就是那条死码的另一半。"""
    src = (GW_DIR / "discovery.py").read_text(encoding="utf-8")
    assert "hass.async_create_task(save_persistent_data(hass))" not in src, \
        "discovery 里又出现裸 create_task：失败无人接"
    assert src.count("schedule_persistent_save(hass)") >= 2, "两处调度点应都走公用入口"


# ══════════════════ F6：remove_device 三类失败必须响亮 ══════════════════
class _Reg:
    def __init__(self, device=None, boom=False):
        self._device = device
        self.boom = boom
        self.removed = []

    def async_get_device(self, identifiers=None):
        return self._device

    def async_remove_device(self, device_id):
        if self.boom:
            raise OSError("disk full")
        self.removed.append(device_id)


class _DMHass:
    def __init__(self):
        self.data = {}
        self.config = SimpleNamespace(config_dir=".")
        self.coros = []
        self.config_entries = SimpleNamespace(
            async_get_entry=lambda eid: SimpleNamespace(entry_id=eid))

    def async_create_task(self, coro, **kwargs):
        self.coros.append(coro)
        coro.close()
        return None


def _make_dm():
    """夹具与 test_audit_round8 同形：持久化三处收口成 no-op（本钉不管落盘）。"""
    hass = _DMHass()
    entry = SimpleNamespace(entry_id="e1",
                            data={c.CONF_GATEWAY_SN: GW_SN, c.CONF_GATEWAY_NAME: "慧尖网关"})
    dm = WindowControllerDeviceManager(hass, entry)
    dm._save_device_to_gateway_mapping = lambda: None
    dm._save_manually_removed_devices = lambda: None
    dm._trigger_persistent_save = lambda: None
    return hass, dm


def _wire_clean(dm, reg):
    async def fake_reg():
        return reg
    dm._get_device_registry = fake_reg
    dm._notify_status_listeners = lambda sn: None


@pytest.mark.asyncio
async def test_remove_device_raises_when_registry_removal_fails():
    """注册表没删净 ⇒ 抛，且拒因点名是哪一步。旧实装：error 日志后 `return True`。"""
    hass, dm = _make_dm()
    _wire_clean(dm, _Reg(device=SimpleNamespace(id="dev-x"), boom=True))
    with pytest.raises(HomeAssistantError) as ei:
        await dm.remove_device(DEV_SN)
    assert "注册表" in str(ei.value), f"拒因未点名注册表：{str(ei.value)!r}"


@pytest.mark.asyncio
async def test_remove_device_raises_when_a_removed_callback_fails():
    hass, dm = _make_dm()
    _wire_clean(dm, _Reg(device=None))

    async def bad_cb(sn, name, dtype):
        raise RuntimeError("listener blew up")

    dm._device_removed_callbacks = [bad_cb]
    with pytest.raises(HomeAssistantError) as ei:
        await dm.remove_device(DEV_SN)
    assert "回调" in str(ei.value), f"拒因未点名回调：{str(ei.value)!r}"


@pytest.mark.asyncio
async def test_remove_device_raises_when_mqtt_handler_notify_fails():
    """P0 复活守卫没执行＝晚到的 003 仍能把设备建回来，不得算成功。"""
    hass, dm = _make_dm()
    _wire_clean(dm, _Reg(device=None))

    class _Boom:
        def _clear_bind_ops_for_device(self, sn):
            raise RuntimeError("handler gone")

    hass.data[c.DOMAIN][dm.entry.entry_id] = {"mqtt_handler": _Boom()}
    with pytest.raises(HomeAssistantError) as ei:
        await dm.remove_device(DEV_SN)
    assert "MQTT" in str(ei.value), f"拒因未点名 MQTT 通知：{str(ei.value)!r}"


@pytest.mark.asyncio
async def test_remove_device_idempotent_absent_still_returns_true():
    """阳性对照（防把闸做太宽）：彻底不存在的幂等成功照旧回 True、不抛。"""
    hass, dm = _make_dm()
    _wire_clean(dm, _Reg(device=None))
    assert await dm.remove_device("NEVER-EXISTED-9") is True


@pytest.mark.asyncio
async def test_remove_button_propagates_removal_failure():
    """调用方义务：按钮侧旧实装 catch 后静默 `return` ⇒ press 回 200 ⇒ 面板弹绿色成功。"""
    class _FailingDM:
        def __init__(self):
            self.removed = []

        async def remove_device(self, device_sn, is_manual=True):
            self.removed.append(device_sn)
            raise HomeAssistantError("HA 设备注册表未删净（OSError）")

    class _Handler:
        async def unbind_device(self, sn):
            return None

    entry = SimpleNamespace(entry_id="e1")
    dm = _FailingDM()
    hass = SimpleNamespace(
        data={c.DOMAIN: {"e1": {"device_manager": dm}}},
        config_entries=SimpleNamespace(async_entries=lambda d: [entry]),
    )
    btn = GatewayDeviceRemoveButton(
        hass=hass, device_manager=dm, mqtt_handler=_Handler(), gateway_sn=GW_SN,
        gateway_name="LoRa 网关", device_sn=DEV_SN, device_name="窗", entry_id="e1")
    _orig_sleep = asyncio.sleep

    async def _no_sleep(*a, **k):
        return None
    asyncio.sleep = _no_sleep
    try:
        with pytest.raises(HomeAssistantError) as ei:
            await btn.async_press()
    finally:
        asyncio.sleep = _orig_sleep
    assert "注册表" in str(ei.value), f"真因要传到用户面前：{str(ei.value)!r}"


# ══════════════════ F7：面板把 HA 的拒绝摆出来，不再无脑绿 ══════════════════
def _async_body(name):
    """按名抽函数体，**保留 async**（`_body` 从 `function` 起切会丢 async）。"""
    m = re.search(r"async\s+function\s+" + re.escape(name) + r"\s*\(", JS)
    assert m, f"huijian.js 里找不到 async function {name}"
    i = m.start()
    j = JS.index("{", i)
    depth = 0
    for k in range(j, len(JS)):
        if JS[k] == "{":
            depth += 1
        elif JS[k] == "}":
            depth -= 1
            if depth == 0:
                return JS[i:k + 1]
    raise AssertionError(f"{name} 花括号不配平")


def _run_node(script):
    if shutil.which("node") is None:
        raise AssertionError("node 不可用，无法真跑面板出口")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "panel.js"
        p.write_text(script, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        assert r.returncode == 0 and "OK" in r.stdout, \
            "controlDevice 真跑失败：\n%s%s" % (r.stdout, r.stderr)


def test_remove_button_renders_ha_rejection_instead_of_green_toast():
    """node 真跑 `controlDevice(...,'remove')`：
       ①后端 400＋message（条目重载/删除没做成）⇒ 必须是 err 且带真话，不得是绿「已发送」；
       ②200 ⇒ 照旧绿「删除命令已发送」（阳性对照，不许把成功也判红）。"""
    script = """
const toasts = [];
function showToast(msg, kind) { toasts.push([msg, kind]); }
function confirm() { return true; }
function findEntityByUniqueId(dev, kind, suffix) { return null; }
function findRemoveButtonEntity(dev, devices) { return { entity_id: 'button.huijian_remove_x' }; }
function updateGatewayDevices() {}
const GATEWAY_SN_BY_ENTRY = {};
const setTimeout = () => {};            // 本轮不验 2s 后的静默刷新
let scenario = [];
async function haApi(path, method, body) {
  const n = scenario.shift();
  if (!n) throw new Error('harness: 场景用完');
  return { ok: n.ok, status: n.status, json: async () => n.body };
}
""" + _async_body("haErrDetail") + _async_body("controlDevice") + """
(async () => {
  // ① 400 + 真话
  scenario = [
    { ok: true, status: 200, body: [] },
    { ok: false, status: 400, body: { message: '这条网关正在重载，删除没有执行——请等几秒后再点一次' } }
  ];
  await controlDevice('dev1', 'remove', 'e1', null);
  let last = toasts[toasts.length - 1];
  if (last[1] !== 'err') { console.log('FAIL kind=' + last.join('|')); process.exit(1); }
  if (last[0].indexOf('正在重载') < 0 || last[0].indexOf('没有执行') < 0) {
    console.log('FAIL msg=' + last[0]); process.exit(1);
  }
  // ② 200 ⇒ 绿色"已发送"仍要在（成功不许被顺手改红）
  toasts.length = 0;
  scenario = [{ ok: true, status: 200, body: [] }, { ok: true, status: 200, body: {} }];
  await controlDevice('dev1', 'remove', 'e1', null);
  last = toasts[toasts.length - 1];
  if (last[1] !== 'ok' || last[0].indexOf('删除命令已发送') < 0) {
    console.log('FAIL ok=' + last.join('|')); process.exit(1);
  }
  console.log('OK');
})();
"""
    _run_node(script)
