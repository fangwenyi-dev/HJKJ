"""v1.7.33 全量审计修复批（F/G/H/I 组）守卫：生命周期 / 表单 / 容器脚本 / Web UI。

本文件把"结构型改动"转成可回归断言。结构钉（读源码）与行为钉（驱动函数）
各自标注；结构钉一律避开自己的 docstring（历史教训：扫描型守卫别扫到自己）。
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway as gw
from custom_components.window_controller_gateway import const as c

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "window_controller_gateway"


def _code_only(text: str) -> str:
    """去注释与 docstring 后的源码（防守卫扫到自己的说明文字）。"""
    no_block = re.sub(r'""".*?"""', "", text, flags=re.S)
    no_block = re.sub(r"'''.*?'''", "", no_block, flags=re.S)
    return "\n".join(ln for ln in no_block.splitlines()
                     if not ln.strip().startswith("#"))


# ==================== F：生命周期与表单 ====================

class TestDiscoveryIntervalClamp:
    """该值是网关离线回收的唯一节拍源，必须可测地归一与钳制。"""

    @pytest.mark.parametrize("raw,expect", [
        (300, 300), (60, 60), (3600, 3600),
        ("300", 300), (300.0, 300),
        (10, 60), (100000, 3600),         # 越界 → 钳制
        ("abc", 300), (None, 300), ({}, 300),
        (float("inf"), 300),
    ])
    def test_clamp_table(self, raw, expect):
        assert gw._clamp_discovery_interval(raw) == expect

    def test_timedelta_accepted(self):
        from datetime import timedelta
        assert gw._clamp_discovery_interval(timedelta(seconds=420)) == 420


class TestSetupDoesNotInheritStaleRuntime:
    def test_setup_clears_state_keys_before_merge(self):
        src = _code_only((PKG / "__init__.py").read_text(encoding="utf-8"))
        assert 'for _stale in ("_platforms_forwarded", "_bg_tasks", "unsub_listeners")' in src, \
            "setup 必须先清状态类键——卸载失败残留会被 previous.update 原样继承"


class TestRaiseOnProgressFalse:
    def test_all_set_unique_id_calls_pass_flag(self):
        src = _code_only((PKG / "config_flow.py").read_text(encoding="utf-8"))
        # 逐行扫（调用一律单行；用正则到行尾，避免被 gateway_sn.lower() 的
        # 右括号截断——首版守卫就被这个坑咬过一次）
        calls = [ln.strip() for ln in src.splitlines()
                 if "self.async_set_unique_id(" in ln]
        assert len(calls) >= 4, f"set_unique_id 调用点缩水: {calls}"
        missing = [x for x in calls if "raise_on_progress=False" not in x]
        assert not missing, f"未显式 raise_on_progress=False 的入口: {missing}"


class TestViaDeviceOmitWhenUnresolved:
    def test_unresolved_gateway_omits_kwarg(self):
        """父网关未注册时必须**省略**参数（传 None = 清空归属，语义不同）。"""
        from custom_components.window_controller_gateway.utils import via_device_kwargs

        class _Reg:
            def async_get_or_create(self, *, via_device_id=None):
                return None

        reg = _Reg()
        assert via_device_kwargs(reg, "100122501207") == {}, (
            "解析不到父设备时必须省略 via_device_id，而不是显式传 None"
        )

    def test_resolved_gateway_passes_id(self, monkeypatch):
        from custom_components.window_controller_gateway import utils as u

        class _Reg:
            def async_get_or_create(self, *, via_device_id=None):
                return None

        monkeypatch.setattr(u, "resolve_via_device_id", lambda reg, sn: "dev-id-1")
        assert u.via_device_kwargs(_Reg(), "SN") == {"via_device_id": "dev-id-1"}


class TestServiceCatalogRegistration:
    def test_registered_names_recorded_for_unload(self):
        from custom_components.window_controller_gateway import services as svc
        registered = []

        class _Services:
            def async_register(self, domain, name, handler, schema=None):
                registered.append(name)

            def async_remove(self, domain, name):
                pass

        hass = SimpleNamespace(services=_Services(), data={c.DOMAIN: {}})
        assert svc.register_services(hass) is True
        assert "unignore_gateway" in registered
        names = hass.data[c.DOMAIN].get("_registered_services")
        assert names, "注册集未登记——卸载时无法按名注销"
        assert sorted(names) == sorted(set(names)) and len(names) == len(registered)

    def test_service_unregister_lives_in_remove_entry_not_unload(self):
        """reload 也走 unload——注销逻辑放 unload 会把域级服务在每次重载时摘掉，
        而重载的 setup 若失败（ConfigEntryNotReady）服务就长期空着。"""
        src = _code_only((PKG / "__init__.py").read_text(encoding="utf-8"))
        unload_body = src.split("async def async_unload_entry(", 1)[1]
        unload_body = unload_body.split("async def async_update_options(", 1)[0]
        assert "hass.services.async_remove" not in unload_body, \
            "服务注销不得写在 async_unload_entry（reload 会误触发）"
        remove_body = src.split("async def async_remove_entry(", 1)[1]
        remove_body = remove_body.split("async def ", 1)[0]
        assert "hass.services.async_remove" in remove_body, \
            "最后一个条目被删除时须注销域级服务（防句柄指向旧闭包）"


# ==================== G：容器脚本（结构钉） ====================

class TestRunShHardening:
    def setup_method(self):
        self.src = (ROOT / "run.sh").read_text(encoding="utf-8")

    def test_pipefail_present(self):
        assert "\nset -o pipefail\n" in self.src, "缺 pipefail（管道左端失败会被右端成功掩盖）"

    def test_integration_swap_is_atomic(self):
        assert "_STAGE=" in self.src, "集成目录换防未走临时名 + mv 原子换名"
        assert 'mv "${_STAGE}" "${INTEGRATION_DST}"' in self.src
        # 旧的非原子两步不得回潮（rm -rf 紧邻 cp -r）
        assert not re.search(r'rm -rf "\$\{INTEGRATION_DST\}"\n\s*cp -r "\$\{INTEGRATION_SRC\}"',
                             self.src), "回潮：rm -rf + cp -r 非原子换防"

    def test_allowlist_residual_risk_is_documented(self):
        assert "172.30.32.0/24" in self.src
        assert "收紧前需真机确认 ingress 源 IP" in self.src, (
            "跨加载项暴露面的取证指引必须在场（本批不平猜收紧，避免整站 403）"
        )


class TestMosquittoQueueSettings:
    def test_queue_and_autosave(self):
        conf = (ROOT / "mosquitto.conf").read_text(encoding="utf-8")
        assert "max_queued_messages 1000" in conf, "QoS1 离线窗口 100 条即静默丢弃"
        # v1.8.5（审计 G-5 纠偏）：v1.7.33 误把 on_changes=true 当"变化即落盘"，
        # 实为 mosquitto 语义反面——true 使 interval 变变更计数阈值并**停掉**
        # 秒级定时落盘（man mosquitto.conf(5) 与 src/loop.c 同口径）。正确形态
        # 是 false + 定时 1800s。反向臂防回潮：true 复活即红（正是当初钉住错值的形态）。
        assert "autosave_on_changes false" in conf, "定时落盘被 on_changes=true 停走（G-5）"
        assert "autosave_on_changes true" not in conf, (
            "G-5 回潮：true 会让 autosave_interval 变计数阈值、崩溃窗口重开"
        )
        assert "autosave_interval 1800" in conf, "落盘周期不得无声漂移"


class TestDiscoveryProxyStderr:
    def test_stderr_captured(self):
        src = _code_only((ROOT / "gateway_discovery_proxy.py").read_text(encoding="utf-8"))
        assert "stderr=subprocess.STDOUT" in src, "stderr 被吞则认证/拒连根因不可见"


# ==================== H：Web UI（结构钉 + 语义钉） ====================

class TestWebUiHardening:
    def setup_method(self):
        self.js = (ROOT / "www" / "js" / "huijian.js").read_text(encoding="utf-8")
        self.html = (ROOT / "www" / "index.html").read_text(encoding="utf-8")

    def test_no_undefined_css_var(self):
        css = (ROOT / "www" / "css" / "huijian.css").read_text(encoding="utf-8")
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", self.js + self.html))
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
        assert used <= defined, f"使用了未定义的 CSS 变量: {sorted(used - defined)}"

    def test_release_link_protocol_whitelist(self):
        assert "function safeReleaseUrl" in self.js
        assert "safeReleaseUrl(latestRelease.html_url)" in self.js, \
            "Release 链接必须过协议白名单（escapeHtml 挡不住 javascript:）"
        assert 'rel="noopener noreferrer"' in self.js

    def test_disabled_entry_filter_and_renderer(self):
        assert "function renderGatewayDisabled" in self.js
        assert "entry.disabled_by" in self.js, "禁用条目必须被过滤（与 MQTT 条目同口径）"

    def test_gateway_devices_requeries_nodes_after_await(self):
        body = self.js.split("async function loadGatewayDevices", 1)[1]
        body = body.split("async function ", 1)[0]
        requeries = body.count("deviceListEl = document.getElementById('devices-' + entryId)")
        assert requeries >= 3, (
            f"await 后重取容器节点次数不足（{requeries}）——写回会落在孤儿节点上"
        )
        assert "let deviceListEl" in self.js and "let statusEl" in self.js

    def test_position_capable_consumed(self):
        assert "coverEntity.attributes.position_capable" in self.js or \
            "attributes.position_capable" in self.js, "位置滑块必须按机型能力渲染"
        assert "不支持百分比定位" in self.js

    def test_logo_cache_buster(self):
        assert 'src="img/logo.png?v=' in self.html
