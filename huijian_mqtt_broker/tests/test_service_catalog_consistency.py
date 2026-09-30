"""服务目录三方一致守卫（v1.7.32 全量审计：服务真值源漂移）。

三方各自维护同一份服务目录，彼此无对账：
- `services.yaml`（HA 开发者工具「服务」页的表单与说明）
- `strings.json` / `translations/zh-CN.json`（服务名与描述的本地化源）
- `services.py` 的实际注册集

实测缺陷（本守卫立前）：`unignore_gateway` 在 yaml 有、两语言 strings **零
条目**（HA 服务页裸显 key，用户无从知道它干什么——而它正是「误点忽略后
唯一自救出口」）；反向 `migrate_devices` 在两语言 strings 里有，而注册
代码已整段注释禁用、yaml 也无该项，属孤儿文案。

判定口径：
1. 双语键集合必须相等（任一侧漏键即红）；
2. `services.yaml` 每个服务必须在双语 strings 都有条目（yaml 是对用户的
   承诺面，缺文案即半成品）；
3. strings 里比 yaml 多出来的键必须恰好等于显式登记的「已禁用但有文案」
   白名单——加一个例外就必须改一次这个常量，防孤儿文案重新长回来。
"""
import json
import re
import types
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"
# 加载项根（www/ 在这里）：PKG 的上两层，不是 parents[0]（那是 custom_components）
ROOT = PKG.parents[1]

# 显式例外：处理器仍在（test_services_failfast 直接调 handle_migrate_devices），
# 但注册被注释禁用（services.py「若需重新启用，取消注释即可」）。重新启用时
# 必须同步 services.yaml，届时把本集合清空即可。
DISABLED_BUT_DOCUMENTED = {"migrate_devices"}


def _locales() -> dict:
    out = {}
    for name in ("strings.json", "translations/zh-CN.json"):
        out[name] = set(json.loads(
            (PKG / name).read_text(encoding="utf-8")).get("services", {}))
    return out


def _yaml_services() -> set:
    return set(re.findall(
        r"^([a-z_]+):\s*$", (PKG / "services.yaml").read_text(encoding="utf-8"), re.M))


class TestServiceCatalogTriple:
    def test_locales_symmetric(self):
        loc = _locales()
        assert len(set(map(frozenset, loc.values()))) == 1, (
            f"双语服务键集合不等：{ {k: sorted(v) for k, v in loc.items()} }"
        )

    def test_every_yaml_service_has_bilingual_text(self):
        loc = _locales()
        yaml_keys = _yaml_services()
        assert yaml_keys, "services.yaml 解析为空（守卫会瞎）"
        for name, keys in loc.items():
            missing = yaml_keys - keys
            assert not missing, (
                f"{name} 缺服务文案 {sorted(missing)}——HA 服务页会裸显 key"
            )

    def test_extra_text_is_only_explicit_whitelist(self):
        loc = _locales()
        extras = set.intersection(*loc.values()) - _yaml_services()
        assert extras == DISABLED_BUT_DOCUMENTED, (
            f"strings 里有未登记的服务文案 {sorted(extras - DISABLED_BUT_DOCUMENTED)}，"
            f"或白名单已过期 {sorted(DISABLED_BUT_DOCUMENTED - extras)}——"
            "孤儿文案与缺文案同罪"
        )

    def test_unignore_gateway_present_in_all_three(self):
        """自救出口必须三方在场（它是「误点忽略」后唯一恢复路径）。"""
        assert "unignore_gateway" in _yaml_services()
        for name, keys in _locales().items():
            assert "unignore_gateway" in keys, f"{name} 缺 unignore_gateway 文案"


# ══════════════════════════════════════════════════════════════════
# 第四条腿（审计 2026-09-30 H-1）：文件头一直承诺"三方一致含 services.py 的
# 实际注册集"，但上面三个测试**从不读 services.py**——只比 services.yaml 与两份
# strings。实测变异：把 services.py 的注册名改成 check_gw_status 并同步改掉它的
# 自登记清单（绕开 test_v1733_guards 的自对账），本文件照绿，而 yaml 仍向用户
# 广告 check_gateway_status ⇒ 调用得 ServiceNotFound。同时全仓
# `grep services/window_controller_gateway tests/` 零命中，面板那 4 条服务调用
# 与注册名之间也没有任何钉。下面把这两条腿补成**真跑**。
# ══════════════════════════════════════════════════════════════════


def _registered_names(tmp_path) -> set:
    """真跑 register_services，取实际注册进服务表的名字。"""
    from homeassistant.core import FakeServices

    from custom_components.window_controller_gateway.services import register_services

    hass = types.SimpleNamespace(
        data={}, services=FakeServices(),
        config=types.SimpleNamespace(config_dir=str(tmp_path)))
    assert register_services(hass) is True, "register_services 返回 False（注册中途抛错）"
    return {name for (_domain, name) in hass.services.registered}


def _panel_called_names() -> set:
    """面板 huijian.js 里真调的服务名（REST /services/<domain>/<service>）。"""
    js = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
    return set(re.findall(r"/services/%s/([a-z_]+)" % "window_controller_gateway", js))


class TestServiceCatalogFourthLeg:
    def test_every_registered_service_is_advertised_in_yaml(self, tmp_path):
        """注册了却没在 yaml 里 → HA 服务页无表单无说明，等于半成品。"""
        registered = _registered_names(tmp_path)
        assert registered, "实际注册集为空（守卫会瞎）"
        missing = registered - _yaml_services()
        assert not missing, f"已注册但 services.yaml 未广告: {sorted(missing)}"

    def test_every_advertised_service_is_registered(self, tmp_path):
        """yaml 广告了却没注册 → 用户照着服务页调用得 ServiceNotFound（H-1 的实测形态）。"""
        registered = _registered_names(tmp_path)
        ghost = _yaml_services() - registered - DISABLED_BUT_DOCUMENTED
        assert not ghost, (
            f"services.yaml 广告了未注册的服务 {sorted(ghost)}——要么注册、"
            "要么进 DISABLED_BUT_DOCUMENTED 并说明为何留着文案"
        )

    def test_panel_calls_resolve_to_registered_services(self, tmp_path):
        """面板调用的每个服务名都必须在注册集里（此前两仓之间零条钉）。"""
        registered = _registered_names(tmp_path)
        called = _panel_called_names()
        assert called, "面板服务调用解析为空（提取锚失效）"
        dangling = called - registered
        assert not dangling, f"面板调用未注册的服务 {sorted(dangling)}（ServiceNotFound）"
