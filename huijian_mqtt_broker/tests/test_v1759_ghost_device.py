# -*- coding: utf-8 -*-
"""v1.7.59 行为钉：删掉的子设备必须从云端消失（用户报"其他用户小程序里还留着已删除的设备"）。

根因链两段，本文件各钉一段（hub 侧的淘汰属 hub 仓，本仓只钉自己这半）：

1. **删除不标脏**：设备从 `device_manager.devices` 消失不产生任何状态变化事件，云端要等
   网关下一次 002 上报或 5 分钟保活才重推 ⇒ 删除在云端延迟到分钟级。
2. **空快照被丢弃**：`_flush_loop` 老写 `if not items: continue`——把"家里真的一台设备都
   不剩了"当"没变化"不上行。hub 按批次合并（merge-only），全量快照一旦不再上报，已删 sn
   就永久留在状态表里，而它返回给小程序的正是那张表。

第 2 段的代价是承诺反转：快照一旦按"全量"上行，hub 就有理由照它淘汰，**漏一台就少一台**。
所以本文件同时钉住"残缺快照不得当全量交出去"（零 manager / 视图彻底构造不出来两种形态），
以及"设备还在缓存里就不能因为一次构造失败而从快照里消失"（回退上一轮视图）。

判据纪律（本仓老账）：输入形态必须生产可达——设备经真实 `add_device`/`remove_device` 或
真实缓存增删进入，不手塞字典形状；上行链路走真入口（`_session_once`），不走私有函数捷径。
"""
import asyncio
import inspect
import logging
from types import SimpleNamespace

import test_audit_round8 as r8
import test_hub_client as th
from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway.ws_gateway import WsGatewayServer

c = r8.c
DEV = "50022E010777"


def _record():
    """与 add_device 入库那条同形的记录（键与默认值照 device_manager.py:690 那段）。"""
    return {"sn": DEV, "name": "客厅窗 (%s)" % DEV, "type": c.DEVICE_TYPE_WINDOW_OPENER,
            "status": "connected", "attributes": {}, "last_update": 0}


def _dm_with_device():
    """真 DeviceManager + 一台在册设备 + 注册表打桩（持久化写盘在 _make_dm 里已 no-op）。"""
    hass, dm = r8._make_dm()
    dm.devices[DEV] = _record()

    async def fake_reg():
        return r8._Reg(device=None)

    dm._get_device_registry = fake_reg
    return hass, dm


class _LanServer:
    """借用真 `_device_update_payload`，只把"条目查找"接上上面那个 manager。"""
    _device_update_payload = WsGatewayServer._device_update_payload

    def __init__(self, dm):
        self._dm = dm

    def _find_entry(self, gateway_sn):
        return {"device_manager": self._dm}


# ══════════════ 1. 删除要把云端标脏（device_manager 侧）══════════════
def test_remove_device_notifies_status_listeners_after_the_cache_drop():
    """核心行为：删除要经状态监听器，而且**回调时设备必须已经不在缓存里**。

    顺序是要点不是巧合：hub 的上行是"回调 → 0.3s 后重扫全量缓存"，回调若早于缓存删除
    跑到，重扫到的还是旧名单，那台已删设备会原样再推一次。
    """
    hass, dm = _dm_with_device()
    seen = []
    dm.add_status_listener(lambda gw, sn: seen.append((gw, sn, sn in dm.devices)))

    assert asyncio.run(dm.remove_device(DEV)) is True
    assert seen == [(dm.gateway_sn, DEV, False)], (
        "删除没经状态监听（或回调早于缓存删除）＝云端快照仍带着这台已删设备: %s" % seen)


def test_removal_notify_stays_silent_on_the_lan_channel():
    """反向半条：这次通知不得给 LAN 造出"设备已删除"的推送——固件协议只定义
    device_update（七键），而设备已从缓存消失，载荷构造必须返回 None（空转）。
    同一条目在删除前必须能构造出载荷，否则这个 None 只是假绿。"""
    _, dm = _dm_with_device()
    server = _LanServer(dm)
    gw_sn = dm.gateway_sn

    live = server._device_update_payload(gw_sn, DEV)
    assert live is not None and live["devSn"] == DEV, "前置：在用的设备要能构造出 device_update"

    asyncio.run(dm.remove_device(DEV))
    assert server._device_update_payload(gw_sn, DEV) is None, (
        "删除后 LAN 仍在推 device_update＝凭空造了固件没有的消息类型")


def test_race_rollback_pop_also_marks_dirty():
    """同族第二处：add_device 在注册表 await 窗口里被用户的删除命中（v1.7.12 DM-F3），
    出口复检把并发第二次添加写回的缓存再 pop 掉——那一次 pop 同样必须标脏。

    不标脏的后果是具体过的：删除的通知先推走（不含这台），并发添加又让全量快照把它带上
    一次，复检 pop 之后没有任何事件 ⇒ 云端停在"这台还在"，最长到 5 分钟保活才自愈。
    """
    hass, dm = _dm_with_device()
    dm.devices.clear()                       # 起点：这台还没入库（复检代码只在新建分支上）
    seen = []
    dm.add_status_listener(lambda gw, sn: seen.append((gw, sn, sn in dm.devices)))

    class _RegRace:
        def __init__(self):
            self.removed = []

        def async_get_device(self, identifiers=None):
            return None

        def async_remove_device(self, device_id):
            self.removed.append(device_id)

        async def async_get_or_create(self, **_kw):
            # 复刻真实交错：删除在这次 await 期间落地（名单加、缓存删），
            # 而并发的第二次添加已经把缓存又填上（同 device_manager.py:703 那次入库）。
            dm._manually_removed_devices.add(DEV)
            dm.devices.pop(DEV, None)
            dm.devices[DEV] = _record()
            return SimpleNamespace(id="dev-race", config_entries=[dm.entry.entry_id])

    async def fake_reg():
        return _RegRace()

    dm._get_device_registry = fake_reg
    got = asyncio.run(dm.add_device(DEV, "客厅窗", c.DEVICE_TYPE_WINDOW_OPENER))

    assert got is None, "复检命中时应回滚本次添加并返回 None"
    assert DEV not in dm.devices, "前置：缓存必须真的被复检 pop 掉，否则无从判"
    assert seen == [(dm.gateway_sn, DEV, False)], (
        "竞态回滚那次 pop 没标脏＝云端停在含这台设备的快照: %s" % seen)


# ══════════════ 2. 空快照要上行、残缺快照不能上行（hub_client 侧）══════════════
def _state_frames(session):
    return [m for m in session.ws.sent if m.get("t") == "state"]


def test_last_device_deleted_still_pushes_an_empty_snapshot(tmp_path, monkeypatch):
    """真入口行为：网关条目在、设备零台 ⇒ 必须上行 `{"t":"state","items":[]}`。

    这是"删掉最后一台"唯一的自愈路径。老写法 `if not items: continue` 把它当没变化丢掉，
    hub 的状态表就永久留着那台，而 `/state` 返回给小程序的正是那张表。
    """
    monkeypatch.setattr(hc, "HUB_STATE_DEBOUNCE_S", 0.01)
    client, _, session = th.make_client(
        tmp_path, manager=th.FakeManager(devices={}),
        session=th.FakeSession(ws=th.FakeWS(hold=0.3)))

    async def run():
        client._stopping = False
        client._state_dirty = asyncio.Event()
        done = await client._session_once()
        await asyncio.sleep(0.05)                                   # 让 flush 有机会跑
        return done, _state_frames(session)

    done, frames = asyncio.run(run())
    assert done is True
    assert frames == [{"t": "state", "items": []}], (
        "零设备的全量快照没上行＝hub 无从淘汰最后删掉的那台: %s" % frames)


def test_zero_manager_batch_is_not_pushed(tmp_path, monkeypatch, caplog):
    """反向半条：一个 manager 都没挂上（条目全在卸载中／启动没完成）时，"空"不代表
    "家里没有设备"。把它当全量交出去就是让云端把全部设备判成已删除——比幽灵设备更糟。

    这里必须同时钉住"这一轮真的跑过"（警告落日志），否则"没发帧"可能只是因为
    上行协程还没轮到——那是假绿。
    """
    monkeypatch.setattr(hc, "HUB_STATE_DEBOUNCE_S", 0.01)
    client, _, session = th.make_client(
        tmp_path, managers=[], session=th.FakeSession(ws=th.FakeWS(hold=0.3)))

    async def run():
        client._stopping = False
        client._state_dirty = asyncio.Event()
        with caplog.at_level(logging.WARNING):
            await client._session_once()
            await asyncio.sleep(0.05)
        return _state_frames(session), caplog.text

    frames, text = asyncio.run(run())
    assert frames == [], "零 manager 的空快照当成全量上行＝云端整表被清空"
    assert "状态快照不完整" in text, "上行协程这一轮根本没跑，'没发帧'是假绿"


def test_degraded_batch_warns_once_and_resumes_after_recovery(tmp_path, monkeypatch, caplog):
    """残缺快照不上行，但必须让人看得见；同一种残缺只播报一次（本项目刚为日志噪声做过
    根修），而恢复后必须继续上行——残缺一次就把云端冻死是不可接受的。"""
    monkeypatch.setattr(hc, "HUB_STATE_DEBOUNCE_S", 0.01)
    good = th.FakeManager(devices={"A1B2": {"attributes": {}}})
    client, _, _ = th.make_client(tmp_path, managers=[],
                                  view_builder=lambda sn, gw, dev: {"sn": sn, "gwSn": gw})
    ws = th.FakeWS()

    with caplog.at_level(logging.WARNING):
        async def run():
            client._stopping = False
            client._state_dirty = asyncio.Event()
            task = asyncio.ensure_future(client._flush_loop(ws))
            for _ in range(3):                                      # 三轮残缺
                client._state_dirty.set()
                await asyncio.sleep(0.06)
            client.attach_managers([good])                          # 恢复：挂上网关条目
            client._state_dirty.set()
            await asyncio.sleep(0.06)
            sent = list(ws.sent)
            client._stopping = True
            client._state_dirty.set()
            await asyncio.gather(task, return_exceptions=True)
            return sent

        sent = asyncio.run(run())

    assert caplog.text.count("状态快照不完整") == 1, "刷屏或一次都不报都不对: %r" % caplog.text
    assert sent == [{"t": "state", "items": [{"sn": "A1B2", "gwSn": "GW1",
                                               "windLockMode": -1}]}], (
        "恢复后没接着上行: %s" % sent)


# ══════════════ 3. 全量承诺怎么兑现（视图构造失败）══════════════
def test_live_device_survives_one_failed_view_build(tmp_path):
    """设备确实还在缓存里，一次视图构造失败不能让它从全量快照里消失。"""
    calls = {"n": 0}

    def builder(sn, gw, dev):
        calls["n"] += 1
        if calls["n"] == 2:                                         # 第二轮构造炸了
            raise ValueError("视图构造异常")
        return {"sn": sn, "gwSn": gw, "position": 10}

    client, _, _ = th.make_client(tmp_path, view_builder=builder,
                                  manager=th.FakeManager(devices={"A1B2": {"attributes": {}}}))
    first, auth1 = client.build_state_snapshot()
    assert auth1 is True and [i["sn"] for i in first] == ["A1B2"], "前置：第一轮正常"

    second, auth2 = client.build_state_snapshot()
    assert [i["sn"] for i in second] == ["A1B2"], "构造失败一次就让在用的设备从全量快照消失"
    assert auth2 is True, "有回退位还判不权威＝整批停发，比旧行为更差"


def test_unbuildable_from_the_very_first_round_is_not_authoritative(tmp_path):
    """首轮就构造失败（没有上一轮视图可回退）⇒ 这批确实缺了一台，必须判不权威。"""
    def builder(sn, gw, dev):
        raise ValueError("这台永远构造不出来")

    client, _, _ = th.make_client(tmp_path, view_builder=builder,
                                  manager=th.FakeManager(devices={"A1B2": {"attributes": {}}}))
    items, auth = client.build_state_snapshot()
    assert items == []
    assert auth is False, "缺了设备还当全量交出去＝hub 把这台判成已删除"


def test_fallback_cache_never_resurrects_a_deleted_device(tmp_path):
    """回退位的边界：设备从缓存消失后，它的上一轮视图不许再进快照——否则这条加固本身
    就变成幽灵设备的制造者（正好是本批要修的东西）。"""
    manager = th.FakeManager(devices={"A1B2": {"attributes": {}}})
    client, _, _ = th.make_client(tmp_path, manager=manager,
                                  view_builder=lambda sn, gw, dev: {"sn": sn, "gwSn": gw})
    built, auth = client.build_state_snapshot()
    assert [i["sn"] for i in built] == ["A1B2"] and auth is True, "前置：先让它进过快照"

    manager.devices.pop("A1B2")                                     # 用户删掉它
    items, auth2 = client.build_state_snapshot()
    assert items == [], "已删设备被上一轮视图回续成幽灵"
    assert auth2 is True, "删干净后的空快照必须仍然权威，否则永远修不好幽灵"


# ══════════════ 4. 交给 hub 的输入形制（跨仓契约的本仓半边）══════════════
def test_state_uplink_is_always_the_array_form_with_sn_and_gwsn(tmp_path):
    """hub 侧的淘汰按"这一批是 items 数组"判（单条兼容形不触发淘汰）。所以本仓只能发
    数组形，且每项必须带 `sn`（淘汰的键）与 `gwSn`（分桶的键）——少一个键，hub 的淘汰
    就退化成不动，或者误删整台网关的设备。"""
    client, _, _ = th.make_client(tmp_path, managers=[
        th.FakeManager(gateway_sn="GW1", devices={"A1B2": {"attributes": {}}}),
        th.FakeManager(gateway_sn="GW2", devices={"C3D4": {"attributes": {}}}),
    ])
    items, auth = client.build_state_snapshot()
    assert auth is True
    assert {(i["sn"], i["gwSn"]) for i in items} == {("A1B2", "GW1"), ("C3D4", "GW2")}
    src = inspect.getsource(hc.HubClient._flush_loop)
    assert src.count('"t": "state"') == 1, "状态上行只许一处发帧"
    assert '"items": items' in src, "发的必须是数组形（单条形不触发 hub 淘汰）"
    assert "collect_state_items" not in src, "别绕开权威快照判据直接取条目"
