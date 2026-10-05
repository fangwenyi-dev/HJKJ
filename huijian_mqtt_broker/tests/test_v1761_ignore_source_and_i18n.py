"""v1.7.61 现场三连（.184 实锤）：ignore 源 MQTT 条目 / zh-Hans 翻译缺失。

1. `.184` 的 HA 里那条 MQTT 条目是 `source=ignore`（用户点过"忽略"的发现条目，
   **永不加载**）：bootstrap 只滤 `disabled_by`，把它当有效配置 → 走"已配置"
   分支 → 删标记（自称成功）→ 真正的 MQTT 条目永不创建 → `is_mqtt_loaded` 恒假
   → 心跳耳无限干等 → **首台网关自动添加不成链**（devices 表为空 + 13:58 那条
   "MQTT 集成仍未就绪"）。
2. 集成只带 `translations/zh-CN.json`，而 HA 简体中文的语言码是 `zh-Hans`
   ⇒ 找不到文件 → 回退 en（也没有）⇒ 选项菜单/错误卡全空白或裸键。
"""
import asyncio
import json
import types
from pathlib import Path
from types import SimpleNamespace

import custom_components.window_controller_gateway.mqtt_bootstrap as mb

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"


# ============ 一、ignore 源条目不是"有效 MQTT 配置" ============

class _EnsureHass:
    def __init__(self, marker_path, mqtt_entries, flow_results, mqtt_loaded=False):
        self._marker_path = marker_path
        entries = list(mqtt_entries)
        self.data = {"mqtt": object()} if mqtt_loaded else {}
        self.config = types.SimpleNamespace(
            path=lambda name: str(self._marker_path) if name == mb.BOOTSTRAP_FILENAME
            else f"/config/{name}")
        self.flow = types.SimpleNamespace(calls=[], _results=list(flow_results))

        async def _init(domain, context=None):
            self.flow.calls.append(("init", domain))
            return self.flow._results.pop(0)

        async def _configure(flow_id, user_input=None):
            self.flow.calls.append(("configure", flow_id, user_input))
            return self.flow._results.pop(0)

        async def _abort(flow_id):
            self.flow.calls.append(("abort", flow_id))

        self.flow.async_init = _init
        self.flow.async_configure = _configure
        self.flow.async_abort = _abort
        self.reload_calls = []
        self.updated = []
        self.removed = []

        async def _reload(eid):
            self.reload_calls.append(eid)

        def _update(entry, data=None, **kw):
            self.updated.append(dict(data or {}))

        async def _remove(eid):
            self.removed.append(eid)

        self.config_entries = types.SimpleNamespace(
            async_entries=lambda dom: entries if dom == "mqtt" else [],
            flow=self.flow, async_reload=_reload, async_update_entry=_update,
            async_remove=_remove)

    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


def _marker(tmp_path):
    p = tmp_path / mb.BOOTSTRAP_FILENAME
    p.write_text(json.dumps({"broker": "127.0.0.1", "port": 2022,
                             "username": "ha_mqtt", "password": "pw"}),
                 encoding="utf-8")
    return p


def _mk_entry(source, disabled_by=None, broker="127.0.0.1", port=2022,
              username="ha_mqtt", password="pw", eid="E1"):
    return SimpleNamespace(data={"broker": broker, "port": port,
                                 "username": username, "password": password},
                           disabled_by=disabled_by, source=source, entry_id=eid)


def test_ignore_source_entry_is_not_taken_over(tmp_path):
    """被忽略的条目**不许**被当成有效配置、更不许被"接管"改写。"""
    marker = _marker(tmp_path)
    hass = _EnsureHass(marker, [_mk_entry("ignore")],
                       flow_results=[{"type": "abort",
                                      "reason": "single_instance_allowed"}])
    asyncio.run(mb.ensure_mqtt_connection(hass))
    assert hass.updated == [], "不得改写被忽略条目的数据（它不是我们的配置）"
    assert hass.reload_calls == [], "不得 reload 一个永不加载的条目"


def test_ignore_only_keeps_marker_and_warns(tmp_path, caplog):
    """只有忽略条目 + 建条被 single_instance 拦 ⇒ **标记必须保留**（旧实现删标记
    自称"视为已有配置"，随后再无人重建 → 现场形态）。"""
    import logging
    marker = _marker(tmp_path)
    hass = _EnsureHass(marker, [_mk_entry("ignore")],
                       flow_results=[{"type": "abort",
                                      "reason": "single_instance_allowed"}])
    with caplog.at_level(logging.WARNING,
                         logger="custom_components.window_controller_gateway"):
        result = asyncio.run(mb.ensure_mqtt_connection(hass))
    assert marker.exists(), "建条未落地 ⇒ 标记必须保留（下轮自愈）"
    assert result is False
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "忽略" in text, "必须点名'被忽略的 MQTT 条目'这一根因"


def test_valid_entry_still_matches_and_lands(tmp_path):
    """反向臂：正常启用条目（数据一致）照旧走匹配分支并删标记。"""
    marker = _marker(tmp_path)
    hass = _EnsureHass(marker, [_mk_entry("user")], flow_results=[],
                       mqtt_loaded=True)
    asyncio.run(mb.ensure_mqtt_connection(hass))
    assert not marker.exists(), "匹配就落地（既有语义不许被改坏）"


def test_verify_ignored_only_is_named(tmp_path):
    """通道核验：只有被忽略条目 ⇒ 专门的 `ignored_only` 判词（卡片给对归因）。"""
    (tmp_path / mb.ENDPOINT_FILENAME).write_text(
        json.dumps({"broker": "127.0.0.1", "port": 2022}), encoding="utf-8")
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(path=lambda *p: str(tmp_path / p[0])),
        config_entries=SimpleNamespace(
            async_entries=lambda dom: [_mk_entry("ignore")]),
    )
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "ignored_only"


def test_verify_valid_entry_still_ok(tmp_path):
    """反向臂：忽略条目 + 一条正常条目 ⇒ 以正常条目为准判 ok。"""
    (tmp_path / mb.ENDPOINT_FILENAME).write_text(
        json.dumps({"broker": "127.0.0.1", "port": 2022}), encoding="utf-8")
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(path=lambda *p: str(tmp_path / p[0])),
        config_entries=SimpleNamespace(
            async_entries=lambda dom: [_mk_entry("ignore"),
                                       _mk_entry("user", eid="E2")]),
    )
    assert asyncio.run(mb.verify_builtin_channel(hass)) == "ok"


# ============ 二、翻译：HA 简体中文要的是 zh-Hans ============

def test_hans_translation_shipped_and_in_sync():
    """HA 简体中文语言码 = zh-Hans（不是 zh-CN）：只带 zh-CN.json 时前端
    拿不到任何组件翻译 ⇒ 选项菜单空白、错误卡显示裸键（现场截图实锤）。
    两份必须同时存在且逐字一致（防只更新一份）。"""
    cn = json.loads((PKG / "translations" / "zh-CN.json").read_text(encoding="utf-8"))
    hans_path = PKG / "translations" / "zh-Hans.json"
    assert hans_path.exists(), "必须随包提供 translations/zh-Hans.json"
    hans = json.loads(hans_path.read_text(encoding="utf-8"))
    assert hans == cn, "zh-Hans 与 zh-CN 必须逐字一致（同一份中文）"


def test_menu_and_error_keys_present_in_both():
    """这份截图点的具体键：options.step.init 的菜单标签 + broker_not_ready。"""
    for rel in ("strings.json", "translations/zh-CN.json", "translations/zh-Hans.json"):
        d = json.loads((PKG / rel).read_text(encoding="utf-8"))
        menu = d["options"]["step"]["init"]["menu_options"]
        assert menu.get("add_gateway") and menu.get("options"), rel
        assert d["config"]["error"]["broker_not_ready"], rel
