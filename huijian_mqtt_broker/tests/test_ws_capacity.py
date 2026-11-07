# -*- coding: utf-8 -*-
"""局域网直连的并发槽位：与云端成员上限对齐（8），并让这条关系由机器看守。

缺陷形态（2026-11-04 现网实锤，192.168.1.4）：`WS_MAX_CLIENTS` 从 v1.6.15 起抄的是网关
固件 `app_ws_gateway.c` 的 4，而那是 ESP32 侧 `int fds[4]` 这类静态数组的**资源**约束，
属容量不属协议；加载项这侧是 aiohttp，多一个槽只是多一条 socket。于是出现
"云端允许绑 8 位家人、局域网只让 4 台手机同时用"——那台当时是 主人＋4 位家人＝5 个人
都够格打开小程序，日志里 08:06–08:08 连续 7 条 `WS 连接数已满（4），拒绝新连接`
就是第 5 个人被拒。被拒的一方只看到"连不上网关"，唯一说得出原因的那句躺在 HA 日志里。

本文件钉四件事：
  ① 容量契约：`WS_MAX_CLIENTS == HUB_MEMBERS_MAX`（两侧各自漂移才撞出这个洞）；
  ② 行为：满员时新握手 503，且**被拒不占槽**（不占 `_clients` 也不占 `_pending_handshakes`）；
  ③ 行为：在途握手计入同一道闸（v1.6.17 的原子性定式），且未满时闸确实放行；
  ④ 形状：闸必须读常量而不是硬编数字；散文里不许再写死 `WS_MAX_CLIENTS=4`。
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.window_controller_gateway import hub_client as hc
from custom_components.window_controller_gateway import ws_gateway as wg

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "window_controller_gateway"
TOKEN = "tok12345"


def _mk_server(monkeypatch, timeout=0.06):
    monkeypatch.setattr(wg, "WS_RECV_TIMEOUT_SECONDS", timeout)

    def _create_task(coro, **kwargs):
        return asyncio.ensure_future(coro)

    hass = SimpleNamespace(
        data={}, loop=None,
        config_entries=SimpleNamespace(async_entries=lambda d: []),
        async_create_task=_create_task,
    )
    srv = wg.WsGatewayServer(hass, host="127.0.0.1", port=9999, token=TOKEN)

    async def _handle(_data):
        return None

    monkeypatch.setattr(srv, "handle_json_message", _handle)
    return srv


def _req(remote="1.2.3.4", proto=TOKEN):
    return SimpleNamespace(headers={"Sec-WebSocket-Protocol": proto} if proto else {},
                           remote=remote)


# ── ① 容量契约 ────────────────────────────────────────────────────────
def test_ws_capacity_equals_the_cloud_member_cap():
    """局域网槽位数必须等于云端成员上限——否则"绑得进来、连不上"。

    反向半条也在这里：哪天 hub 把成员上限改成 6，本钉要红，逼两侧一起动，
    而不是各改各的再撞一次今天这种形状。
    """
    assert wg.WS_MAX_CLIENTS == hc.HUB_MEMBERS_MAX, (
        "局域网直连槽位 %d 与云端成员上限 %d 不等＝第 %d 位家人被 503 拒"
        % (wg.WS_MAX_CLIENTS, hc.HUB_MEMBERS_MAX, min(wg.WS_MAX_CLIENTS, hc.HUB_MEMBERS_MAX) + 1))
    assert wg.WS_MAX_CLIENTS >= 8, "槽位不得低于 8（本批的裁定就是与 8 人家庭对齐）"


def test_no_prose_hardcodes_the_old_four():
    """散文里不许再出现 `WS_MAX_CLIENTS=4` 这种把数字写死的句子。

    为什么钉这个：数字一旦进注释，改了常量注释不会跟着改，下一个人就会拿注释当事实
    （本次要收口的四处正是这样留下的）。要么引用常量名，要么写"上限"。
    """
    import re
    for rel in ("const.py", "ws_gateway.py"):
        src = (PKG / rel).read_text(encoding="utf-8")
        assert not re.search(r"WS_MAX_CLIENTS\s*=\s*4\b", src), \
            "%s 里仍有把槽位数写死成 4 的散文" % rel


def test_gate_reads_the_constant_not_a_number():
    """准入闸必须拿 WS_MAX_CLIENTS 比较——硬编数字就是"下次改不动"的那个洞。"""
    src = (PKG / "ws_gateway.py").read_text(encoding="utf-8")
    assert "len(self._clients) + self._pending_handshakes >= WS_MAX_CLIENTS" in src, \
        "容量闸的形状变了：必须是在册客户端 + 在途握手 与常量比较"


# ── ② 满员行为 ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_full_slots_reject_and_do_not_consume_a_slot(monkeypatch):
    srv = _mk_server(monkeypatch)
    srv._clients = {object() for _ in range(wg.WS_MAX_CLIENTS)}
    before = len(srv._clients)
    resp = await srv._handle_ws(_req())
    assert getattr(resp, "status", None) == 503, "满员必须 503（与固件同款拒绝握手）"
    assert len(srv._clients) == before, "被拒的连接不许占走一个槽"
    assert srv._pending_handshakes == 0, "被拒的连接不许留下预约计数（否则攒满就永久 503）"


@pytest.mark.asyncio
async def test_capacity_is_eight_so_a_five_person_family_fits(monkeypatch):
    """本批的产品判据：第 5 台手机必须连得上（改之前它会被拒）。"""
    srv = _mk_server(monkeypatch)
    assert wg.WS_MAX_CLIENTS >= 5, "槽位低于 5 就仍然复现 5 口之家第 5 人连不上"
    srv._clients = {object() for _ in range(4)}
    resp = await srv._handle_ws(_req())
    assert getattr(resp, "status", None) != 503, \
        "4 个在册时第 5 个不该再被拒: %s" % getattr(resp, "status", None)


# ──  在途握手计入同一道闸 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pending_handshakes_count_toward_the_gate(monkeypatch):
    """并发握手不能同时通过容量检查（v1.6.17：固件把"查满→入册"做成原子，这里靠预约数逼近）。"""
    srv = _mk_server(monkeypatch)
    srv._clients = {object() for _ in range(wg.WS_MAX_CLIENTS - 1)}
    srv._pending_handshakes = 1
    resp = await srv._handle_ws(_req())
    assert getattr(resp, "status", None) == 503, "在途握手没计入闸＝并发下会瞬时超上限"
    assert srv._pending_handshakes == 1, "被拒的一方不得改动预约计数"


@pytest.mark.asyncio
async def test_gate_open_when_below_capacity(monkeypatch):
    """未满时闸要放行：走完整握手后因假请求失败回 400——关键是**不是 503**，
    且失败后预约计数必须退回 0（v1.6.19 那条"只增不减"的教训）。"""
    srv = _mk_server(monkeypatch)
    srv._clients = set()
    resp = await srv._handle_ws(_req())
    assert getattr(resp, "status", None) != 503, "空槽时被误拒: %s" % getattr(resp, "status", None)
    assert srv._pending_handshakes == 0, "握手失败后预约计数没归还"


@pytest.mark.asyncio
async def test_bad_token_is_rejected_before_the_capacity_gate(monkeypatch):
    """令牌错的请求必须在容量闸**之前**被 401 挡掉——否则满员时未认证方还能刷出 503 噪声。"""
    srv = _mk_server(monkeypatch)
    srv._clients = {object() for _ in range(wg.WS_MAX_CLIENTS)}
    resp = await srv._handle_ws(_req(proto="wrongtoken"))
    assert getattr(resp, "status", None) == 401, \
        "顺序错了：认证闸必须在容量闸前面（实得 %s）" % getattr(resp, "status", None)
