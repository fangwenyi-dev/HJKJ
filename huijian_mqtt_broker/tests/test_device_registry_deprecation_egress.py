"""v1.9.2 设备注册表弃用族的双形态出口（现网实锤，非未来雷）。

一手证据（2026-10-28 09:55 .91 `/api/hassio/core/logs`，跑的就是 v1.9.1）：

    WARNING [homeassistant.helpers.frame] Detected that custom integration
      'window_controller_gateway' calls `device_registry.async_get_device`, …
      breaks_in_ha_version="2027.8.0"
    WARNING [homeassistant.helpers.frame] Detected that custom integration
      'window_controller_gateway' accesses `DeviceEntry.config_entries`, …
      breaks_in_ha_version="2027.10.0"

⇒ 这两面**今天就在用户日志里**（不是"2027 年才会出问题"），2027 年是停摆年。
上游真签名（gh api 取的 device_registry.py 两份，逐字核过）：
  · 2026.10.4:2091 `async_get_device_by_identifier(identifier, config_entry_id)`
    ＋ :2132 `async_get_devices(*, identifiers, connections, config_entry_id)`
  · 2024.12.0（manifest 声明下限）里 `async_get_device_by_identifier` **零命中**
    ⇒ 只能"探测分形"，禁止无条件换新 API（旧 HA 直接 AttributeError 打死设备
      查找，比弃用告警严重一级）——与 utils.via_device_kwargs /
      call_registry_method 同族兼容哲学。
  · 2024.12.0 的 DeviceEntry **没有** `config_entry_id` 字段（只有
    `config_entries: set[str]`）；2026.10.4 反过来 `config_entries` 是会被
    报告的兼容垫片，其值恒等于 `{config_entry_id}`，**只有"迁移前复合设备"**
    （restored composite）才在它上面给出并集且不告警（源码 :429/:435/:557）。
    ⇒ 读取顺序必须"先 config_entry_id，复合设备再并回 config_entries"，
      两种版本都拿到同一份真值且一次都不触弃用面。

判据分三层（缺一层就是假绿）：
  ①行为：替身注册表分别扮新 HA / 2024.12，点名"走了哪条出口、弃用面调用次数=0"；
  ②类面：AST 全树扫调用点，除双形态出口自身之外禁止直调被禁形态；
  ③阳性对照：把被禁形态写进合成源码，扫描器必须点名它（扫描器自己坏掉时
    ①②会一起静默变绿）。
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.window_controller_gateway.const import DOMAIN
from custom_components.window_controller_gateway.utils import (
    get_device_by_sn, get_device_config_entry_ids)

_HERE = Path(__file__).resolve()
_PKG_DIR = _HERE.parents[1] / "custom_components" / "window_controller_gateway"

SN = "100122501203"


# ==================== 替身：两代注册表 + 两代 DeviceEntry ====================

class _Device:
    """DeviceEntry 最小替身。

    entries=None ⇒ 压根没有 config_entries 这个可读面（碰它就炸），用来证明
    新形态下**一次都没触**弃用垫片；要扮旧版就显式传集合。
    """

    def __init__(self, did, entry_id=None, entries=None, composite=False,
                 composite_id=None):
        self.id = did
        if entry_id is not None:
            self.config_entry_id = entry_id
        self._entries = entries
        self.is_composite_device = composite
        self.composite_device_id = composite_id

    @property
    def config_entries(self):
        if self._entries is None:
            raise AssertionError(
                "触到了弃用的 DeviceEntry.config_entries（2027.10.0 停摆面）")
        return self._entries


class _NewHaRegistry:
    """新 HA 替身：两条非弃用出口都在，弃用的那条留痕但不许被走到。"""

    def __init__(self, owned=None, cross=(), deprecated_hit=None):
        self._owned = owned or {}        # (sn, entry_id) -> device
        self._cross = list(cross)        # async_get_devices 的返回（跨条目）
        self._deprecated_hit = deprecated_hit
        self.identifier_calls = []
        self.list_calls = []
        self.deprecated_calls = []

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.identifier_calls.append((identifier, config_entry_id))
        return self._owned.get((identifier[1], config_entry_id))

    def async_get_devices(self, *, identifiers=None, connections=None,
                          config_entry_id=None):
        self.list_calls.append(frozenset(identifiers or ()))
        return list(self._cross)

    def async_get_device(self, identifiers=None, connections=None):
        self.deprecated_calls.append(frozenset(identifiers or ()))
        return self._deprecated_hit


class _OldHaRegistry:
    """HA 2024.12.0（manifest 声明下限）替身：只有 async_get_device。

    刻意**不定义** async_get_device_by_identifier / async_get_devices——与上游
    该版本源码一致（dr_2024.12.0.py 里这两个名字零命中）；helper 若盲调新 API
    当场 AttributeError。
    """

    def __init__(self, found=None):
        self.found = found
        self.calls = []

    def async_get_device(self, identifiers=None, connections=None):
        self.calls.append(frozenset(identifiers or ()))
        return self.found
    # 刻意不定义新 API：实现若盲调 async_get_device_by_identifier 会 AttributeError
    # 当场红（getattr 探测形态则由 calls 断言抓住"没走旧出口"）。


# ==================== ①行为：get_device_by_sn 双形态 ====================

class TestGetDeviceBySn:
    def test_new_ha_uses_identifier_api_only(self):
        dev = _Device("D1", entry_id="E1")
        reg = _NewHaRegistry(owned={(SN, "E1"): dev})
        got = get_device_by_sn(reg, SN, config_entry_id="E1")
        assert got is dev
        assert reg.identifier_calls == [((DOMAIN, SN), "E1")], \
            "新 HA 必须走 async_get_device_by_identifier((DOMAIN, sn), 条目 id)"
        assert reg.deprecated_calls == [], "2027.8.0 停摆面一次都不许触"

    def test_entry_scoped_miss_falls_back_to_cross_entry(self):
        """迁移/接管现场：设备此刻还挂在**别的**条目上。条目内查不到时必须回退
        跨条目查询——否则查得到却报 None，迁移链静默断（比弃用告警严重一级）。"""
        dev = _Device("D2", entry_id="E-old")
        reg = _NewHaRegistry(owned={}, cross=[dev])
        got = get_device_by_sn(reg, SN, config_entry_id="E-new")
        assert got is dev, "条目作用域未命中不得当成『设备不存在』（迁移链会静默断）"
        assert reg.list_calls == [frozenset({(DOMAIN, SN)})]
        assert reg.deprecated_calls == []

    def test_old_ha_keeps_identifiers_lookup(self):
        dev = _Device("D3", entries={"E1"})
        reg = _OldHaRegistry(found=dev)
        got = get_device_by_sn(reg, SN)
        assert got is dev
        assert reg.calls == [frozenset({(DOMAIN, SN)})], \
            "下限版本唯一形态：async_get_device(identifiers={(DOMAIN, sn)})"

    def test_no_entry_id_still_uses_new_apis_on_new_ha(self):
        """调用点拿不到条目 id（如 utils.resolve_via_device_id）也要走非弃用面。"""
        dev = _Device("D4", entry_id="E1")
        reg = _NewHaRegistry(cross=[dev])
        assert get_device_by_sn(reg, SN) is dev
        assert reg.deprecated_calls == []

    def test_composite_splits_keep_old_lookup(self):
        """一条标识符对应"迁移前复合设备"的多个 split 时，上游用复合视图回答
        （其 id 是存量自动化/实体引用指向的那个）。这里**保留**旧调用而不是把
        id 换成某个 split——那会把老引用指错设备。代价：这一种形态仍有一条
        弃用告警，是有意的等价性取舍，不是漏迁。"""
        s1 = _Device("S1", entry_id="Ea", composite=True, composite_id="CID")
        s2 = _Device("S2", entry_id="Eb", composite=True, composite_id="CID")
        comp = _Device("CID", entry_id="Ea", composite=True)
        reg = _NewHaRegistry(cross=[s1, s2], deprecated_hit=comp)
        assert get_device_by_sn(reg, SN) is comp
        assert len(reg.deprecated_calls) == 1

    def test_independent_matches_take_first(self):
        """独立设备共用一条标识符（非复合）：取第一个——与上游兜底同形。"""
        a = _Device("X1", entry_id="Ea")
        b = _Device("X2", entry_id="Eb")
        reg = _NewHaRegistry(cross=[a, b])
        assert get_device_by_sn(reg, SN) is a

    def test_not_found_returns_none_on_both_forms(self):
        assert get_device_by_sn(_NewHaRegistry(), SN) is None
        assert get_device_by_sn(_OldHaRegistry(), SN) is None, \
            "查不到回 None 的契约两代一致（调用方靠 None 分支决策）"


# ==================== ①行为：get_device_config_entry_ids 不触弃用面 =========

class TestDeviceConfigEntryIds:
    def test_new_ha_reads_only_config_entry_id(self):
        dev = _Device("D", entry_id="E1", entries=None)   # 碰 config_entries 即炸
        assert get_device_config_entry_ids(dev) == {"E1"}

    def test_old_ha_reads_config_entries(self):
        dev = _Device("D", entries={"E1", "E2"})          # 2024.12：无 config_entry_id
        assert get_device_config_entry_ids(dev) == {"E1", "E2"}

    def test_composite_device_merges_entries(self):
        """复合设备（迁移前一条标识符属多条目）的并集只存在于 config_entries，
        且读它不告警——必须并回，否则宿主归属判定漏掉其它条目。"""
        dev = _Device("D", entry_id="E1", entries={"E1", "E2"}, composite=True)
        assert get_device_config_entry_ids(dev) == {"E1", "E2"}

    def test_entry_without_ids_returns_empty_set(self):
        """两代"真身可能给出空"的形态 + 仓内 SimpleNamespace 替身（既无
        config_entry_id 也无 config_entries）都要平稳回空集，不许 AttributeError
        ——设备视图/共享保护判定都靠空集短路。"""
        assert get_device_config_entry_ids(_Device("D", entries=set())) == set()
        assert get_device_config_entry_ids(SimpleNamespace(id="D")) == set()


# ==================== ②类面：AST 全树扫调用点 ====================

def _allow_ranges(tree, func_name):
    """双形态出口自身所在的行区间——被禁形态只允许出现在它函数体内。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            out.append((node.lineno, node.end_lineno or node.lineno))
    return out


def _get_device_offenders(tree, path, helper_name="get_device_by_sn"):
    """点名 `x.async_get_device(...)` 真调用面（属性名全等，不用子串——
    `async_get_device_by_identifier` 含同名前缀，子串判会自我误伤）。"""
    allow = _allow_ranges(tree, helper_name)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "async_get_device":
            if not any(a <= node.lineno <= b for a, b in allow):
                offenders.append(f"{path.name}:{node.lineno}")
    return offenders


def _reads_manager_expr(seg):
    """`hass.config_entries` 是 ConfigEntry 管理器（正常 API），不是设备属性。"""
    return (seg == "hass" or seg.endswith(".hass")
            or seg.endswith("['hass']") or seg.endswith('["hass"]'))


def _config_entries_offenders(tree, path, helper_name="get_device_config_entry_ids"):
    """禁直读 DeviceEntry.config_entries——**两种写法都算**。

    getattr 名字面量形态必须一起抓：本仓的原始缺陷正是
    `getattr(device, "config_entries", None)`，只盯 `ast.Attribute` 的扫描器
    对它完全瞎（同名串在常量里，不在属性面上）。
    """
    allow = _allow_ranges(tree, helper_name)
    offenders = []
    for node in ast.walk(tree):
        is_attr = (isinstance(node, ast.Attribute)
                   and node.attr == "config_entries")
        is_getattr = (isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name)
                      and node.func.id == "getattr" and len(node.args) >= 2
                      and isinstance(node.args[1], ast.Constant)
                      and node.args[1].value == "config_entries")
        if not (is_attr or is_getattr):
            continue
        seg = ast.unparse(node.value if is_attr else node.args[0])
        if _reads_manager_expr(seg):
            continue
        if any(a <= node.lineno <= b for a, b in allow):
            continue
        offenders.append(f"{path.name}:{node.lineno}")
    return offenders


def _scan(scanner):
    """整包递归扫描＋宇宙自检：文件数由盘上真清单决定，解析失败响亮报错。"""
    files = sorted(_PKG_DIR.rglob("*.py"))
    assert len(files) >= 20, f"扫描宇宙异常（只找到 {len(files)} 个 .py）——判据已失效"
    out, parsed = [], 0
    for py in files:
        if "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        parsed += 1
        out.extend(scanner(tree, py))
    assert parsed == len([p for p in files if "__pycache__" not in p.parts])
    return out


class TestNoDeprecatedDeviceRegistrySurface:
    def test_no_direct_async_get_device_outside_helper(self):
        offenders = _scan(_get_device_offenders)
        assert not offenders, (
            f"发现直调弃用的 device_registry.async_get_device（2027.8.0 停摆）：{offenders}"
            "——一律改走 utils.get_device_by_sn 双形态出口")

    def test_no_device_config_entries_read_outside_helper(self):
        offenders = _scan(_config_entries_offenders)
        assert not offenders, (
            f"发现直读弃用的 DeviceEntry.config_entries（2027.10.0 停摆）：{offenders}"
            "——一律改走 utils.get_device_config_entry_ids")

    def test_helper_sites_counted_per_file(self):
        """盘上实际交付形状：出口收口后的**调用点**计数（AST 数 Call，不用子串
        ——`def get_device_by_sn(` 那行本身含同名子串，文本数会自己喂绿自己）。
        计数变了说明有人删/加了一处设备查找：删＝功能静默丢失，类面钉看不见。"""
        expect = {"device_manager.py": 14, "discovery.py": 1, "utils.py": 1,
                  "__init__.py": 1}
        for name, n in expect.items():
            tree = ast.parse((_PKG_DIR / name).read_text(encoding="utf-8"))
            calls = [x.lineno for x in ast.walk(tree)
                     if isinstance(x, ast.Call)
                     and ((isinstance(x.func, ast.Name) and x.func.id == "get_device_by_sn")
                          or (isinstance(x.func, ast.Attribute)
                              and x.func.attr == "get_device_by_sn"))]
            assert len(calls) == n, \
                f"{name} 出口调用点须恰为 {n}（实得 {len(calls)}，行 {calls}）"

    def test_registry_view_uses_the_helper(self):
        """api.py 的设备视图此前自己写了一遍"两代都读"的兼容逻辑（三处副本之
        一）。收口后它必须经出口，而不是留一份会漂的抄本。"""
        src = (_PKG_DIR / "api.py").read_text(encoding="utf-8")
        assert "get_device_config_entry_ids(" in src


# ==================== ③阳性对照：扫描器坏了必须当场露出来 ====================

class TestScannersHaveTeeth:
    """没有这一组，②的两条类面钉在扫描器自身坏掉时会**静默变绿**
    （宇宙空转、attr 名写错、except 提前 return 都属此类）。"""

    def test_async_get_device_form_is_caught(self):
        tree = ast.parse(
            "def f(device_registry):\n"
            "    return device_registry.async_get_device(identifiers={('a', 'b')})\n")
        got = _get_device_offenders(tree, Path("synthetic.py"))
        assert got == ["synthetic.py:2"], f"扫描器抓不到被禁形态＝判据是空气：{got}"

    def test_helper_body_is_exempt_but_rename_is_not(self):
        tree = ast.parse(
            "def get_device_by_sn(device_registry):\n"
            "    return device_registry.async_get_device(identifiers={('a', 'b')})\n"
            "def other(device_registry):\n"
            "    return device_registry.async_get_device(identifiers={('a', 'b')})\n")
        assert _get_device_offenders(tree, Path("synthetic.py")) == ["synthetic.py:4"]

    def test_identifier_api_is_not_flagged(self):
        """反向半条：新 API 名字含 async_get_device 前缀，扫描器若用子串判会把
        正确实现判红（也就会诱导人把钉改成"睁一只眼"）。"""
        tree = ast.parse(
            "def f(device_registry):\n"
            "    return device_registry.async_get_device_by_identifier(('a', 'b'), 'e')\n")
        assert _get_device_offenders(tree, Path("synthetic.py")) == []

    def test_config_entries_device_read_is_caught(self):
        tree = ast.parse(
            "def f(device):\n"
            "    return device.config_entries\n")
        got = _config_entries_offenders(tree, Path("synthetic.py"))
        assert got == ["synthetic.py:2"], f"扫描器抓不到被禁形态＝判据是空气：{got}"

    def test_getattr_name_form_is_caught(self):
        """本仓原始缺陷就是这个写法：属性名藏在字符串常量里，只盯 Attribute
        节点的扫描器对它全瞎——阳性对照必须单独跑这一形态。"""
        tree = ast.parse(
            "def f(device):\n"
            "    return getattr(device, 'config_entries', None)\n")
        got = _config_entries_offenders(tree, Path("synthetic.py"))
        assert got == ["synthetic.py:2"], f"getattr 名字面量形态没被抓到：{got}"

    def test_hass_manager_read_is_not_flagged(self):
        """反向半条：hass.config_entries / self.hass.config_entries /
        request.app["hass"].config_entries 都是正常 API，误伤会让人绕开判据。"""
        tree = ast.parse(
            "def f(hass, self, request):\n"
            "    a = hass.config_entries.async_entries('d')\n"
            "    b = self.hass.config_entries.async_entries('d')\n"
            "    c = request.app['hass'].config_entries.async_entries('d')\n")
        assert _config_entries_offenders(tree, Path("synthetic.py")) == []

    def test_dict_key_and_subscript_are_not_reads(self):
        """视图里 `"config_entries": list(ids)` 与 `d["config_entries"]` 是自有
        字典键（对外契约），不是 DeviceEntry 属性面。"""
        tree = ast.parse(
            "def f(d):\n"
            "    out = {'config_entries': [1]}\n"
            "    return d['config_entries']\n")
        assert _config_entries_offenders(tree, Path("synthetic.py")) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
