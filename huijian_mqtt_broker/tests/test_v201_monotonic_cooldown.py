# -*- coding: utf-8 -*-
"""v2.0.1（第七轮审计 R-1/R-2）：本地超时/节流一律 monotonic + None 哨兵。

两条真实 bug（均为 v1.8.2 B-1 的同族漏网，10-08 审计后唯一遗留）：
  R-1  discovery.py 发现冷却用墙钟：NTP 校时/RTC 漂移把钟往回拨后
       now-last_time 恒为负 ⇒ 本会话内该网关的发现卡不再弹（重启才自愈）。
  R-2  hub_client.py 两处**本地节流**（成员刷新 / 绑定码自动补发）用墙钟：
       回拨 ⇒ 节流击穿、对付费端点连击；前跳 ⇒ 补发被憋死。

修法纪律（本仓 10-08 审计 B-1 修复建议里点名的坑，**不许只换钟不换哨兵**）：
  Linux 的 monotonic≈开机秒数，缺省哨兵若仍是 0，开机 60s/120s 内的首次
  发现/刷新会被误判"冷却/节流中"而吞掉。故缺省一律 None＝从未发生过，直接放行。

刻意不改的墙钟（勿顺手"修"）：
  * hub_client 的 _bind_code_at/_member_code_at/members_loaded_at 等 TTL——
    它们对的是**云端绝对到期时间**，必须墙钟；
  * utils.device_is_stale 的墙钟回退路径——mono 孪生优先，墙钟只兜历史数据。

每条行为钉都配了 mutation_matrix.py 里的反向臂：把钟换回去/把哨兵换回 0/
摘掉节流，必须有钉当场红。
"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import custom_components.window_controller_gateway.discovery as disc_mod
from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway.const import DOMAIN, CONF_GATEWAY_SN

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"


# ── R-1：发现冷却 ─────────────────────────────────────────────────────
def _hass(entries=()):
    """与 test_v1762 同款最小夹具：空注册表、无进行中流。"""
    flows = []

    async def fake_init(domain, context=None, data=None):
        flows.append({"context": context, "data": data})

    hass = SimpleNamespace(
        data={DOMAIN: {"discovery": {"ignored_gateways": set(),
                                     "last_discovery_time": {},
                                     "announced_gateways": set()}}},
        config_entries=SimpleNamespace(
            async_entries=lambda domain: list(entries),
            flow=SimpleNamespace(async_progress=lambda: [], async_init=fake_init),
        ),
    )
    return hass, flows


def _no_registry():
    disc_mod.dr = SimpleNamespace(async_get=lambda h: SimpleNamespace(
        async_get_device=lambda identifiers=None: None))


def _discover(hass, sn="1001215011a3"):
    return disc_mod.async_discover_gateway(hass, sn, "慧尖网关")


def _patch_clock(monkeypatch, module, wall, mono):
    """只换**被测模块**的 time 引用，不碰全局 time 模块。
    教训（本臂首跑失守的根因）：monkeypatch.setattr(disc_mod.time, ...) 改的是
    共享的 time 模块本体——asyncio 事件循环内部也调 time.monotonic()，拿到的
    假值会污染 loop 自己的调度时钟，行为钉就测不准了。换成模块级 SimpleNamespace
    后，产品代码读假钟、asyncio 读真钟，互不干扰。"""
    monkeypatch.setattr(module, "time",
                        SimpleNamespace(time=lambda: wall[0],
                                        monotonic=lambda: mono[0]))


def test_wall_clock_rollback_does_not_freeze_discovery(monkeypatch):
    """R-1 行为钉：冷却基准若是墙钟，回拨 1 小时后差值恒负 ⇒ 同一网关在
    用户取消忽略后的再次发现被永久吞掉（直到重启）。monotonic 不受墙钟影响。
    用**同一 SN** 走 unignore 清会话去重（announced）但保留冷却戳的真实路径，
    让第二次发现必须过 step 2 冷却闸——不同 SN 各有独立基准，测不到本闸。"""
    _no_registry()
    hass, flows = _hass()
    wall = [1_000_000.0]
    mono = [500.0]
    _patch_clock(monkeypatch, disc_mod, wall, mono)

    asyncio.run(_discover(hass))
    assert len(flows) == 1

    # NTP 校时把墙钟往回拨 1 小时；单调钟只走了 61s（> 60s 冷却窗）
    wall[0] -= 3600.0
    mono[0] += 61.0
    # 用户取消忽略：清 announced（会话去重），冷却戳按设计保留
    dd = hass.data[DOMAIN]["discovery"]
    dd["announced_gateways"].clear()
    asyncio.run(_discover(hass))
    assert len(flows) == 2, \
        "墙钟回拨把发现冷却冻死＝取消忽略后网关永不弹卡（R-1 回潮：冷却基准必须 monotonic）"


def test_cooldown_still_blocks_within_window(monkeypatch):
    """反向半条：冷却语义本身不许丢——窗内同 SN 再报照旧拦。
    注意冷却是**按 SN** 的（防同一网关重复触发），不同 SN 各有自己的基准；
    且冷却戳在 step 2 就写（早于注册表/会话去重闸），所以用"已配置条目"
    让第一次在 step 3 早退、只留下冷却戳，第二次同 SN 必须被 step 2 拦。"""
    _no_registry()
    configured = SimpleNamespace(entry_id="E1",
                                 data={CONF_GATEWAY_SN: "1001215011a3"}, title="x")
    hass, flows = _hass([configured])
    mono = [500.0]
    _patch_clock(monkeypatch, disc_mod, [1_000_000.0], mono)

    asyncio.run(_discover(hass))         # step 2 写戳，step 3 已配置早退
    assert flows == []
    mono[0] += 30.0                      # 仍在 60s 窗内
    asyncio.run(_discover(hass))
    assert flows == [], "冷却窗内同 SN 必须拦截（节流语义不许随换钟一起丢）"
    # 窗过之后同 SN 放行到 step 3（仍因已配置早退，但证明冷却不再拦）
    mono[0] += 31.0
    asyncio.run(_discover(hass))
    assert flows == []


def test_first_discovery_passes_at_boot(monkeypatch):
    """哨兵钉：Linux monotonic≈开机秒数。开机 10s 时首次发现，若缺省哨兵
    仍是 0，now-0=10<60 会被误判冷却中——首台网关的卡片在开机一分钟内静默丢失。"""
    _no_registry()
    hass, flows = _hass()
    monkeypatch.setattr(disc_mod, "time", SimpleNamespace(monotonic=lambda: 10.0, time=time.time))
    asyncio.run(_discover(hass))
    assert len(flows) == 1, \
        "开机初期首次发现被 0 哨兵误判冷却（不许只换钟不换哨兵）"


def test_cooldown_source_is_monotonic_with_none_sentinel():
    """源码钉（配合行为钉）：discovery 的冷却段不许回潮墙钟/0 哨兵。"""
    src = (PKG / "discovery.py").read_text(encoding="utf-8")
    seg = src[src.index("# 2. 速率限制"):src.index("# 3. 检查网关是否已在配置条目中")]
    assert "time.monotonic()" in seg, "发现冷却回潮墙钟（R-1）"
    assert "time.time()" not in seg, "发现冷却回潮墙钟（R-1）"
    assert ".get(gateway_key)" in seg and ".get(gateway_key, 0)" not in seg, \
        "缺省哨兵回潮 0＝开机 60s 内首次发现被吞"


# ── R-2：hub 本地节流 ─────────────────────────────────────────────────
def _client(tmp_path):
    c = hc.HubClient([], config_dir=str(tmp_path), session=object())
    c.instance_id, c._secret = "inst-1", "sec-1"
    c.connected = True
    c.members_supported = True
    calls = []

    async def fake_http(path, payload, timeout_s=None):
        calls.append(path)
        return {"ok": True, "ownerMasked": "oFa…01", "membersMax": 8,
                "members": [{"mid": "a" * 12, "openidMasked": "oFa…02", "at": 1}]}

    c._http = fake_http
    return c, calls


def test_members_throttle_survives_wall_rollback(tmp_path, monkeypatch):
    """R-2 行为钉①：成员刷新节流若是墙钟，回拨后 now-last 恒负 ⇒ 每次 GET /hub
    都打云端（节流击穿）。monotonic 基准下窗内必须只打一次。"""
    client, calls = _client(tmp_path)
    wall = [1_000_000.0]
    mono = [500.0]
    _patch_clock(monkeypatch, hc, wall, mono)

    asyncio.run(client.maybe_refresh_members())
    wall[0] -= 3600.0                    # NTP 回拨
    mono[0] += 5.0                       # 单调钟只走 5s，仍在 120s 窗内
    for _ in range(3):
        asyncio.run(client.maybe_refresh_members())
    assert len(calls) == 1, \
        "墙钟回拨击穿成员节流＝面板每 30s 把云端打爆（R-2 回潮：节流必须 monotonic）"


def test_members_first_refresh_passes_at_boot(tmp_path, monkeypatch):
    """哨兵钉①：开机 10s 首次刷新，0 哨兵会误判节流中 ⇒ 面板开机两分钟
    看不到家人。None 哨兵必须放行。"""
    client, calls = _client(tmp_path)
    monkeypatch.setattr(hc, "time", SimpleNamespace(monotonic=lambda: 10.0, time=time.time))
    asyncio.run(client.maybe_refresh_members())
    assert calls == ["/agent/members"], "开机初期首次成员刷新被 0 哨兵吞掉"


def test_renew_throttle_survives_wall_rollback(tmp_path, monkeypatch):
    """R-2 行为钉②：绑定码自动补发节流若是墙钟，回拨后对付费端点连击
    （真机曾 32s 内 8 条告警）。monotonic 基准下窗内只许一次。"""
    client, calls = _client(tmp_path)
    client.bind_code, client._bind_code_at = "111111", 0   # 死码 ⇒ 每次都想补发
    wall = [1_000_000.0]
    mono = [500.0]
    _patch_clock(monkeypatch, hc, wall, mono)

    async def failing(path, payload, timeout_s=None):
        calls.append(path)
        raise RuntimeError("hub /agent/bindcode -> 502")

    client._http = failing
    asyncio.run(client._renew_bind_code_if_stale())
    wall[0] -= 3600.0
    mono[0] += 5.0
    asyncio.run(client._renew_bind_code_if_stale())
    asyncio.run(client._renew_bind_code_if_stale())
    assert len(calls) == 1, \
        "墙钟回拨击穿换码节流＝对付费端点连击（R-2 回潮：节流必须 monotonic）"


def test_renew_first_attempt_passes_at_boot(tmp_path, monkeypatch):
    """哨兵钉②：开机 10s 首次自动补发，0 哨兵会误判节流中。None 必须放行。"""
    client, calls = _client(tmp_path)
    client.bind_code, client._bind_code_at = "111111", 0
    monkeypatch.setattr(hc, "time", SimpleNamespace(monotonic=lambda: 10.0, time=time.time))
    asyncio.run(client._renew_bind_code_if_stale())
    assert calls == ["/agent/bindcode"], "开机初期首次自动补发被 0 哨兵吞掉"


def test_reconnect_reset_is_none_not_zero(tmp_path):
    """重连归零的哨兵形态：monotonic 下 0 是合法开机时刻，归零必须置 None
    （test_hub_client 的同名钉断言 is None，这里钉源码不许写回 0.0）。"""
    src = (PKG / "hub_client.py").read_text(encoding="utf-8")
    assert "self._members_refresh_at = None" in src, "重连归零回潮 0.0"
    assert "self._members_refresh_at = 0.0" not in src, "重连归零回潮 0.0"


def test_throttle_source_is_monotonic():
    """源码钉：两处本地节流的读写不许回潮墙钟；TTL 墙钟不在本钉范围。"""
    src = (PKG / "hub_client.py").read_text(encoding="utf-8")
    seg_m = src[src.index("async def maybe_refresh_members"):src.index("async def _renew_bind_code_if_stale")]
    assert "time.monotonic()" in seg_m and "time.time()" not in seg_m, \
        "成员刷新节流回潮墙钟（R-2）"
    seg_b = src[src.index("async def _renew_bind_code_if_stale"):src.index("def _ws_url")]
    assert "time.monotonic()" in seg_b, "换码节流回潮墙钟（R-2）"
    assert "time.time() - self._bind_renew_at" not in seg_b, "换码节流回潮墙钟（R-2）"
    assert "self._bind_renew_at = time.monotonic()" in src, "换码尝试戳回潮墙钟（R-2）"