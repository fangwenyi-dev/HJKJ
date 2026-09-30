"""v1.7.33 全量审计修复批（E 组）：小程序 WS 网关加固。

钉桩清单：
1. 握手/旧令牌比较改 hmac.compare_digest（时序侧信道）；
2. 子协议切分只认 ',' 与 ' '（旧实现按任意空白拆，比固件与 aiohttp 都宽
   →"过闸但 101 不回显"的静默分歧）；
3. 运行态令牌闸补 WS_TOKEN_MIN_LEN（storage 手改短令牌可绕过表单下限）；
4. 空闲计时只由业务 TEXT 帧续期（BINARY/空帧不再保活——默认令牌公开在
   本仓，4 个槽位可被二进制帧永久占满）；
5. 停机窗口（_stopping）拒绝新握手；
6. 广播失败连接显式 close（旧实现只丢引用，socket 还能活 300s）；
7. `_persist_token` 写全部 enabled 条目（旧版只写第一个即 break，删条目后
   重启静默回退旧令牌 → 小程序永久 401）；
8. STOP 监听单例注册。
"""
import asyncio
from types import SimpleNamespace

import pytest

import custom_components.window_controller_gateway.ws_gateway as wg
from custom_components.window_controller_gateway import const as c


# ==================== 1/2. 纯函数：时序安全与切分口径 ====================

class TestHandshakeCryptoSafety:
    def test_exact_hit_across_separators(self):
        assert wg.handshake_token_ok("tok12345, other", "tok12345") is True
        assert wg.handshake_token_ok("other tok12345", "tok12345") is True
        assert wg.handshake_token_ok("tok12345", "tok12345") is True

    def test_arbitrary_whitespace_is_not_a_separator(self):
        """\\t/\\n 不是固件与 aiohttp 的分隔符，插件不得比它们宽一档。"""
        assert wg.handshake_token_ok("tok12345\tx", "tok12345") is False
        assert wg.handshake_token_ok("tok12345\nx", "tok12345") is False

    def test_no_plain_equality_on_secret(self):
        """时序判据必须是**语法树级**的，不是"源码里出现过这个词"。

        对抗复核 2026-09-30 实锤：本函数上方新写的注释里就含
        `hmac.compare_digest` 字样，`inspect.getsource` 把它一起读进来 ⇒
        把实现退回 `return token in offered_subprotocols(header_value)`
        （TypeError 与时序预言机双双回潮）两条文本断言都不红。
        现在按 AST 判：①必须有 `compare_digest(...)` 的**调用节点**；
        ②不得出现把涉密名字用 == 或 in 直接比对的节点。
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(wg.handshake_token_ok))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        last = fn.body[-1]
        assert isinstance(last, ast.Return) and last.value is not None, \
            "handshake_token_ok 末句不再是 return（本钉需重写）"
        gens = [n for n in ast.walk(last.value) if isinstance(n, ast.GeneratorExp)]
        assert gens, "末句 return 必须是 any(生成器) 形态（A-1 判据位）"
        elt_calls = [c for g in gens for c in ast.walk(g.elt) if isinstance(c, ast.Call)]
        assert any("compare_digest" in ast.unparse(c) for c in elt_calls), \
            "生成器元素必须真调 compare_digest（改名+死代码不再能绕：2026-09-30 复核第二轮）"
        secrets = {"token", "cand", "old_token", "current_token"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and \
                    any(isinstance(op, (ast.Eq, ast.In)) for op in node.ops):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert not (names & secrets), \
                    "涉密值出现 ==/in 短路比对（时序预言机）：%s" % ast.unparse(node)

    def test_old_token_compare_is_constant_time(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(wg.validate_new_token))
        conds = [node.test for node in ast.walk(tree)
                 if isinstance(node, ast.If)
                 and "_MSG_OLD_MISMATCH" in "".join(ast.unparse(x) for x in node.body)]
        assert conds, "旧令牌不匹配分支形态变了（本钉需重写）"
        blob = " ".join(ast.unparse(x) for x in conds)
        assert "compare_digest" in blob, \
            "oldToken 比较必须真调 compare_digest（文本版被注释即可满足，已废）"
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and \
                    any(isinstance(op, (ast.Eq, ast.In)) for op in node.ops):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                assert not (names & {"old_token", "current_token"}), \
                    "oldToken 出现 ==/in 短路比对：%s" % ast.unparse(node)

    def test_validate_still_works(self):
        assert wg.validate_new_token("newtoken1", "oldtoken1", "oldtoken1") is None
        assert wg.validate_new_token("newtoken1", "WRONG", "oldtoken1") == wg._MSG_OLD_MISMATCH
        assert wg.validate_new_token("short", None, "") == wg._MSG_TOO_SHORT


# ==================== 3. 运行态令牌下限 ====================

class TestRuntimeTokenMinLen:
    def _hass(self, token):
        return SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda d: [
            SimpleNamespace(entry_id="e1", options={
                c.CONF_WS_GATEWAY_ENABLED: True, c.CONF_WS_GATEWAY_TOKEN: token})]))

    def test_short_token_falls_back(self):
        port, token = wg.ws_gateway_wanted(self._hass("abc"))
        assert token == c.DEFAULT_WS_GATEWAY_TOKEN, (
            "短令牌可在线枚举（破之即得开窗能力），运行态必须回退默认"
        )

    def test_legal_token_survives(self):
        legal = "Abcd1234_-"
        port, token = wg.ws_gateway_wanted(self._hass(legal))
        assert token == legal

    def test_empty_token_still_means_no_auth(self):
        port, token = wg.ws_gateway_wanted(self._hass(""))
        assert token == "", "空串=不认证（D-1 定案）不得被下限闸误伤"


# ==================== 4. 空闲计时只认业务 TEXT ====================

class _Msg:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = data


class _FakeWS:
    """按脚本持续吐帧的假连接（用于验证"什么帧能续期"）。"""

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


def _mk_server(monkeypatch, timeout=0.06):
    monkeypatch.setattr(wg, "WS_RECV_TIMEOUT_SECONDS", timeout)
    def _create_task(coro, **kwargs):
        return asyncio.ensure_future(coro)

    hass = SimpleNamespace(
        data={c.DOMAIN: {}}, loop=None,
        config_entries=SimpleNamespace(async_entries=lambda d: []),
        async_create_task=_create_task,
    )
    srv = wg.WsGatewayServer(hass, host="127.0.0.1", port=9999, token="tok12345")

    async def _handle(_data):
        return None

    monkeypatch.setattr(srv, "handle_json_message", _handle)
    return srv, timeout


class TestIdleTimerOnlyBusinessText:
    @pytest.mark.asyncio
    async def test_binary_frames_do_not_keep_session_alive(self, monkeypatch):
        srv, timeout = _mk_server(monkeypatch)
        ws = _FakeWS([_Msg(wg.WSMsgType.BINARY, b"\x01")])
        await asyncio.wait_for(srv._session(ws), timeout=timeout * 6)
        # 能返回即为正确：BINARY 未续期，deadline 到期退出

    @pytest.mark.asyncio
    async def test_text_frames_keep_session_alive(self, monkeypatch):
        srv, timeout = _mk_server(monkeypatch)
        ws = _FakeWS([_Msg(wg.WSMsgType.TEXT, '{"cmd":"ping"}')])
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(srv._session(ws), timeout=timeout * 4)
        assert not ws.closed

    @pytest.mark.asyncio
    async def test_stopping_flag_ends_session(self, monkeypatch):
        srv, timeout = _mk_server(monkeypatch)
        ws = _FakeWS([_Msg(wg.WSMsgType.TEXT, '{"cmd":"ping"}')])
        srv._stopping = True
        await asyncio.wait_for(srv._session(ws), timeout=timeout * 2)


# ==================== 5. 停机准入闸 ====================

class TestStoppingAdmission:
    @pytest.mark.asyncio
    async def test_stopping_rejects_new_handshake(self, monkeypatch):
        srv, _t = _mk_server(monkeypatch)
        srv._stopping = True
        resp = await srv._handle_ws(SimpleNamespace(
            headers={"Sec-WebSocket-Protocol": "tok12345"}, remote="1.2.3.4"))
        assert getattr(resp, "status", None) == 503, (
            "停机窗口不得再接受握手（该会话此后无人踢，还拖住 runner.cleanup）"
        )


# ==================== 6. 广播失败显式关连接 ====================

class TestBroadcastClosesDead:
    @pytest.mark.asyncio
    async def test_dead_socket_closed_and_evicted(self, monkeypatch):
        srv, _t = _mk_server(monkeypatch)
        closed = []

        class _Dead:
            closed = False

            async def send_str(self, _t):
                raise ConnectionResetError("client gone")

            async def close(self):
                closed.append(1)

        class _Live:
            closed = False

            async def send_str(self, _t):
                return None

        dead, live = _Dead(), _Live()
        srv._clients = {dead, live}
        await srv._broadcast({"type": "device_update"})
        await asyncio.sleep(0.01)           # 让 close 任务跑完
        assert srv._clients == {live}
        assert closed == [1], "死连接必须显式 close（旧实现只丢引用，socket 再活 300s）"


# ==================== 7. 令牌持久化写全部条目 ====================

class TestPersistTokenAllEntries:
    @pytest.mark.asyncio
    async def test_writes_every_enabled_entry(self, monkeypatch):
        updates = []

        def _mk_entry(eid):
            return SimpleNamespace(entry_id=eid, options={
                c.CONF_WS_GATEWAY_ENABLED: True,
                c.CONF_WS_GATEWAY_TOKEN: c.DEFAULT_WS_GATEWAY_TOKEN})

        class _CE:
            def async_entries(self, _d):
                return [_mk_entry("e1"), _mk_entry("e2")]

            def async_update_entry(self, entry, options=None):
                updates.append((entry.entry_id, (options or {}).get(
                    c.CONF_WS_GATEWAY_TOKEN)))

        hass = SimpleNamespace(data={c.DOMAIN: {}}, config_entries=_CE())
        srv = wg.WsGatewayServer(hass, host="127.0.0.1", port=9999, token="oldtoken1")
        await srv._persist_token("newtoken1", "oldtoken1")
        assert sorted(updates) == [("e1", "newtoken1"), ("e2", "newtoken1")], (
            "令牌必须写入全部 enabled 条目——只写第一个时，删掉该条目即重启回退 401"
        )
        assert srv._token == "newtoken1"


# ==================== 8. STOP 监听单例 ====================

class TestStopListenerSingleton:
    def test_single_registration_marker(self):
        import inspect
        src = inspect.getsource(wg.async_ensure_ws_gateway)
        assert "WS_GATEWAY_STOP_LISTENER_KEY" in src, "STOP 监听未走单例键"
    def test_const_defined(self):
        assert wg.WS_GATEWAY_STOP_LISTENER_KEY.startswith("_ws_gateway")
