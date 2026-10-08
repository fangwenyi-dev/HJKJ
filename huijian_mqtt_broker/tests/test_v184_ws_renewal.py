"""v1.8.4 审计 B-2：WS 空闲续期只认**已识别的业务命令**。

v1.7.33 那次修复只堵了一半：续期点在解析与分派**之前**，于是任意 1 字节非空 TEXT
（`"x"`）与当年那条 BINARY 漏洞完全等价——默认令牌公开在本仓，同网段主机每 299s
发一帧就能永久占满 WS_MAX_CLIENTS=4，把真小程序挤成 503。

本文件全部走**真分派**（不桩 handle_json_message）：错误包照旧要回（协议面），
只是畸形/未知命令不再能续命。
"""
import asyncio
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.ws_gateway as wg

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "window_controller_gateway"


class _FakeWS:
    """按脚本吐帧的假连接（与 test_v1733_ws_hardening 同形制，但这里不桩处理器）。"""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.closed = False
        self.sent = []

    async def receive(self):
        await asyncio.sleep(0.005)
        item = self._script[self._i % len(self._script)]
        self._i += 1
        return item

    async def send_str(self, text):
        self.sent.append(text)

    async def close(self):
        self.closed = True


def _msg(type_, data=None):
    return SimpleNamespace(type=type_, data=data)


def _server(monkeypatch, timeout=0.06):
    monkeypatch.setattr(wg, "WS_RECV_TIMEOUT_SECONDS", timeout)
    import custom_components.window_controller_gateway.const as c
    hass = SimpleNamespace(
        data={c.DOMAIN: {}}, loop=None,
        config_entries=SimpleNamespace(async_entries=lambda d: []),
        async_create_task=lambda coro, **kw: asyncio.ensure_future(coro),
    )
    srv = wg.WsGatewayServer(hass, host="127.0.0.1", port=9999, token="tok12345")
    return srv, timeout


async def _survives(srv, ws, timeout, factor=4):
    """True = 会话还活着（续期成立）；False = deadline 到点自己退出（没被续期）。"""
    try:
        await asyncio.wait_for(srv._session(ws), timeout=timeout * factor)
        return False
    except asyncio.TimeoutError:
        return True


# ==================== 两臂：什么帧能续命 ====================

@pytest.mark.asyncio
async def test_known_business_command_renews(monkeypatch):
    srv, timeout = _server(monkeypatch)
    ws = _FakeWS([_msg(wg.WSMsgType.TEXT, '{"cmd":"ping"}')])
    assert await _survives(srv, ws, timeout), "合法业务命令必须续期（小程序 60s 心跳的保活通道）"
    assert any("pong" in s for s in ws.sent), ws.sent


@pytest.mark.asyncio
async def test_one_byte_text_does_not_renew(monkeypatch):
    """B-2 的正身：1 字节非空 TEXT 过去与 BINARY 等价占槽。"""
    srv, timeout = _server(monkeypatch)
    ws = _FakeWS([_msg(wg.WSMsgType.TEXT, "x")])
    assert not await _survives(srv, ws, timeout, factor=6), \
        "垃圾 TEXT 仍在续期 ⇒ 占槽面没堵（v1.7.33 只堵了 BINARY 那一半）"
    assert any("missing cmd" in s for s in ws.sent), \
        f"畸形帧的错误包不得丢（协议面必须与改前一致）：{ws.sent}"


@pytest.mark.asyncio
async def test_unknown_command_does_not_renew(monkeypatch):
    """`{"cmd":"x"}` 只有 12 字节，用"能解析成对象"当判据关不住这种占槽法。"""
    srv, timeout = _server(monkeypatch)
    ws = _FakeWS([_msg(wg.WSMsgType.TEXT, '{"cmd":"definitely_not_a_cmd"}')])
    assert not await _survives(srv, ws, timeout, factor=6), "未知命令不得续期"
    assert any("unknown command" in s for s in ws.sent), f"错误包丢了：{ws.sent}"


@pytest.mark.asyncio
async def test_json_scalar_and_array_frames_do_not_renew(monkeypatch):
    """合法 JSON 但不是对象：同样不算业务活动，且照旧回 missing cmd。"""
    for frame in ('"123"', "[1,2,3]", "123", '{"nomd":"ping"}'):
        srv, timeout = _server(monkeypatch)
        ws = _FakeWS([_msg(wg.WSMsgType.TEXT, frame)])
        assert not await _survives(srv, ws, timeout, factor=6), f"{frame} 竟被当成业务帧"


@pytest.mark.asyncio
async def test_empty_text_still_ignored(monkeypatch):
    """改前的"空帧忽略"语义不许被这刀顺手改掉。"""
    srv, timeout = _server(monkeypatch)
    ws = _FakeWS([_msg(wg.WSMsgType.TEXT, "")])
    assert not await _survives(srv, ws, timeout, factor=6)
    assert ws.sent == [], f"空帧不该回包：{ws.sent}"


def test_predicate_accepts_every_command_in_the_table():
    """命令集里每条都必须被认成业务帧——判据与表脱节时，那个功能的客户端会被 300s
    静默踢线（这里只判谓词，会话臂用 ping 走真分派，避免把设备管理器的在途状态卷进来）。
    """
    assert wg.BUSINESS_CMDS, "命令集空了＝所有小程序客户端都会被周期性踢线"
    for cmd in sorted(wg.BUSINESS_CMDS):
        assert wg.frame_is_business(json.dumps({"cmd": cmd})), f"{cmd} 未被认成业务帧"
    for junk in ("x", "123", "", "null", "[]", "{}", '{"cmd":""}', '{"cmd":123}',
                 '{"cmd":null}'):
        assert not wg.frame_is_business(junk), f"{junk!r} 竟被认成业务帧"


# ==================== 表与分派链不许各自漂移 ====================

def _dispatched_cmds():
    """从 handle_json_message 的源码里抽它自己判的 cmd 字面量。"""
    src = (PKG / "ws_gateway.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
               and n.name == "WsGatewayServer")
    fn = next(f for f in cls.body
              if isinstance(f, ast.AsyncFunctionDef) and f.name == "handle_json_message")
    found = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.left, ast.Name) and node.left.id == "cmd"
                and isinstance(node.comparators[0], ast.Constant)
                and isinstance(node.comparators[0].value, str)):
            found.add(node.comparators[0].value)
    return found


def test_business_cmd_table_matches_the_dispatch_chain():
    """双向对账：分派链加了 cmd 而表里没加 ⇒ 那条命令的客户端会被 300s 静默踢线；
    表里有而分派链没有 ⇒ 名单空转、B-2 的判据被稀释。两种都必须当场红。"""
    chain = _dispatched_cmds()
    assert chain, "分派链锚丢失——判据本身失效了，这条必须先修"
    assert chain - wg.BUSINESS_CMDS == set(), \
        f"分派链里有命令没进 BUSINESS_CMDS（会被静默踢线）: {sorted(chain - wg.BUSINESS_CMDS)}"
    assert wg.BUSINESS_CMDS - chain == set(), \
        f"BUSINESS_CMDS 里有分派链不认的幽灵项: {sorted(wg.BUSINESS_CMDS - chain)}"


def test_renewal_point_is_after_dispatch():
    """形状守卫（AST，不吃排版）：**循环体内每一处** `deadline = loop.time()` 都必须
    被 `if business:` 罩住，且落在分派调用之后。

    第一版这条守卫只判"有一处受保护的续期在分派之后"——于是"在分派前再补一行裸续期"
    的变异照样绿（本文件实测它没红）。不变量必须是集合相等，不是存在性。
    """
    tree = ast.parse((PKG / "ws_gateway.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
               and n.name == "WsGatewayServer")
    session = next(f for f in cls.body
                   if isinstance(f, ast.AsyncFunctionDef) and f.name == "_session")

    def _is_deadline_assign(node):
        return (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "deadline" for t in node.targets)
                and "loop.time" in ast.unparse(node.value))

    loop = next((n for n in ast.walk(session) if isinstance(n, ast.While)), None)
    assert loop is not None, "_session 的读取循环锚丢失"
    inside = {n.lineno for n in ast.walk(loop) if _is_deadline_assign(n)}
    assert inside, "循环里一处续期赋值都没有——B-2 的修法整个消失了"

    guarded = set()
    for node in ast.walk(loop):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == "business"):
            guarded |= {n.lineno for n in ast.walk(node) if _is_deadline_assign(n)}
    assert inside == guarded, \
        f"循环里存在不受业务帧判据罩住的续期点：{sorted(inside - guarded)}（B-2 复发形态）"

    handle_line = min((n.lineno for n in ast.walk(session)
                       if isinstance(n, ast.Await)
                       and "handle_json_message" in ast.unparse(n.value)),
                      default=None)
    assert handle_line, "分派调用锚丢失——_session 的形状变了，本条要跟着更新"
    assert min(guarded) > handle_line, \
        f"续期在第 {min(guarded)} 行、分派在第 {handle_line} 行 ⇒ 又回到解析前续命"
