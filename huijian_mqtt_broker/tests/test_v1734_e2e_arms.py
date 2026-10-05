"""v1.7.34 钉桩：真栈 E2E driver 新增 K/L 两臂（CI 硬门禁，本地无 docker）。

K 臂：未配置网关首报 001 → `gateway/{sn}/req` 上**恰好一条**同型代答
      （head/ctype/id/sn 回带 + data.errcode:0 + uuid）+ 发现卡挂起。
L 臂：空 SN 等待条目 + 首报 → **不再静默填充**（v1.7.62 起）——必须弹发现卡、
      等待条目不得出现网关设备；REST 走完卡片流后新条目 loaded、子设备真注册、
      等待条目被清理，
      且两耳并存（handler 耳 + 心跳耳）时仍 1 请求 1 答。

本地无 docker（run_local.sh 走 WSL），故两臂的 CI 真栈结果由 e2e job 承担；
本文件守两件事：①两臂不被悄悄删掉/断言被稀释；②两臂的判定助手**本身**
不是空转——把 `_check_single_ack` / `_wait_acks` 从 driver 源文本抽出真跑，
多答/错形/漏 uuid 必须真 die（否则 CI 绿是假的）。
"""
import asyncio
import json
import pathlib
import re
import socket
import threading
import time
from types import SimpleNamespace

import pytest

HERE = pathlib.Path(__file__).resolve().parent
DRIVER = HERE / "e2e" / "ha_e2e_driver.py"
SRC = DRIVER.read_text(encoding="utf-8")
SH = (HERE / "e2e" / "run_e2e.sh").read_text(encoding="utf-8")

NEW_GW = "E2EGW0000002"
AUTO_GW = "E2EGW0000003"
AUTO_DEV = "500700000002"


# ============ ① 两臂存在性与断言强度（静态钉） ============
class TestArmsPresent:
    def test_arm_k_first_report(self):
        assert f'NEW_GW = "{NEW_GW}"' in SRC
        i_k = SRC.index('step("K"')
        assert 'pc.publish("gateway/rpt_rsp", json.dumps(_first_report(NEW_GW, 7101)))' \
            in SRC[i_k:], "K 臂必须真发首报 001（不是只订不发的空转臂）"
        assert "_check_single_ack(_k_acks, NEW_GW, 7101" in SRC[i_k:], \
            "K 臂必须走恰好一答判定"
        assert "_cards_for(NEW_GW" in SRC[i_k:], "K 臂必须验发现卡挂起"

    def test_arm_l_zero_click(self):
        assert f'AUTO_GW = "{AUTO_GW}"' in SRC
        i_l = SRC.index(f'AUTO_GW = "{AUTO_GW}"')   # 从常量块起（含 AUTO_DEV）
        seg = SRC[i_l:]
        assert '{"gateway_sn": "", "gateway_name": ""}' in seg, \
            "L 臂必须用发现代理同款空 SN REST 路径建等待条目"
        assert 'if e.get("entry_id") != entry' in seg, \
            "等待条目必须按 entry_id 甄别（REST 条目表不含 data，见下）"
        assert 'if _aw_state != "loaded":' in seg, \
            "L 臂必须验等待条目真 loaded（心跳耳挂载前提）"
        assert "静默填充回潮" in seg, \
            "L 臂必须钉'等待条目不得被静默填充'（v1.7.62 反向半边）"
        assert "_cards_for(AUTO_GW)" in seg and "必须弹发现卡" in seg, \
            "L 臂必须验发现卡挂起（首台也弹卡的新语义）"
        assert "未被清理" in seg, \
            "L 臂必须验确认添加后等待条目被清理（async_remove_awaiting_entries）"
        assert "config_entry_id={awaiting_id}" in seg and "含网关SN" in seg, \
            "L 臂接管证据必须走 devices 视图（产品级真值面）"
        assert "_check_single_ack(_l_acks, AUTO_GW, 7202" in seg, \
            "L 臂必须验两耳并存时仍恰好一答"

    def test_l_arm_does_not_peek_entry_data(self):
        """反钉（第三轮 CI 假红实锤）：`GET /api/config/config_entries/entry`
        返回的是 `entry.as_json_fragment`，**不含 data/unique_id**（HA
        config_entries.py 源码实证）。拿它读 data.gateway_sn 判"填充成功"会
        恒为假 —— 真栈上表现为"填充明明发生，driver 却报 填充=None"。"""
        assert 'e.get("data")' not in SRC, \
            "不得从 REST 条目表读 data（该响应无此字段）；用 devices 视图判接管"
        assert "as_json_fragment" in SRC, "该实证结论必须写进注释防回退"

    def test_arms_run_before_soak(self):
        """顺序钉：两臂必须在 J 段 500 条 soak 之前——否则 req 主题被洪水
        污染，恰好一答的计数无从判读。"""
        assert SRC.index('step("K"') < SRC.index('step("J"')
        assert SRC.index('step("L"') < SRC.index('step("J"')
        assert SRC.index('step("K"') < SRC.index('step("L"'), \
            "K 臂（单耳）必须先于 L 臂（两耳）——应答者数量是递增的"

    def test_ack_recording_keeps_topic(self):
        """计数按主题甄别：只记 payload 的老形态无法区分代答归属。"""
        assert "ack_msgs.append((msg.topic, raw))" in SRC
        assert "acks.append(raw)" in SRC, "旧 acks 列表不得删（H/H2 段仍用）"

    def test_summary_reports_both_arms(self):
        i = SRC.index("GITHUB_STEP_SUMMARY")
        assert "首报 001 代答" in SRC[i:] and "首台弹卡确认" in SRC[i:]

    def test_l_arm_republishes_distinct_ids(self):
        """周期重发 002（每轮换 id）：既喂卡片流的连接测试窗，也当'不许静默
        填充'的判据面——单发一条落在竞态窗内就白等。同 id 会被 5s 去重层吃掉。"""
        i_l = SRC.index(f'AUTO_GW = "{AUTO_GW}"')
        seg = SRC[i_l:]
        assert "while not _stop.is_set():" in seg, "重发必须是后台周期线程"
        assert "_i += 1" in seg, "重发必须换 id（同 id 会被 5s 去重层吃掉）"


# ============ ①b 在途流查询：WS 而非 405 的 REST 端点 ============
class TestFlowQueryContract:
    """首轮真栈实锤（run 35690049138）：K 臂代答断言通过、卡片断言假红——
    `GET /api/config/config_entries/flow` 在 HA 2026.9.3 是 405（源码
    ConfigManagerFlowIndexView.get 显式 raise HTTPMethodNotAllowed），恒空
    列表既让 K 臂假红，也会让 L 臂的"发现卡 0 张"变成假绿。"""

    def test_uses_ws_progress_command(self):
        assert '"config_entries/flow/progress"' in SRC
        assert '"auth_required"' in SRC and '"access_token"' in SRC, \
            "WS 认证契约（首帧 auth_required → auth+access_token → auth_ok）"
        assert "/api/websocket" in SRC

    def test_never_queries_the_405_rest_index(self):
        """反钉：不得回退到 REST 的流索引（405，恒空 ⇒ 两种断言都失真）。"""
        assert 'call("GET", "/api/config/config_entries/flow")' not in SRC
        assert "HTTPMethodNotAllowed" in SRC and "405" in SRC, \
            "405 的实证结论必须写进注释（防下次再猜同一个端点）"

    def test_query_failure_is_loud(self):
        """strict 路径查询失败必须 die——静默返回空＝"0 张卡"假绿。"""
        i = SRC.index("def _hj_flows(")
        seg = SRC[i:SRC.index("def _wait_acks(")]
        assert "die(" in seg, "strict 查询失败必须显式失败"
        assert "last_err" in seg, "非 strict 路径要把错误带回给最终 die 文案"
        i_l = SRC.index("_cards = _cards_for(AUTO_GW")
        assert "strict=False" not in SRC[i_l:i_l + 60], \
            "L 臂的\"卡必须挂起\"断言必须走 strict（查询失败即红，不得当没卡）"
        i_k = SRC.index("_c = _cards_for(NEW_GW")
        assert "strict=False" in SRC[i_k:i_k + 80] and "last_err=" in SRC[i_k:i_k + 80], \
            "K 臂轮询期容错、最终 die 带出末次错误"


# ============ ② 判定助手真跑（防空转绿） ============
def _load_helpers(ack_msgs=None, die_calls=None):
    """从 driver 源文本抽出两个助手真跑——driver 是脚本（导入即执行 A 段），
    只能按锚切片 exec。切片锚丢失即 KeyError，静态钉会先红。"""
    start = SRC.index("def _wait_acks(")
    end = SRC.index("# ---------- K.")
    ns = {"json": json, "time": time, "ack_msgs": ack_msgs if ack_msgs is not None else []}

    def die(msg):
        if die_calls is not None:
            die_calls.append(msg)
        raise SystemExit(msg)

    ns["die"] = die
    exec(compile(SRC[start:end], str(DRIVER), "exec"), ns)  # noqa: S102
    return ns


def _ack(sn, msg_id, uuid="84cfcfbf-0000-0000-0000-000000000000", ctype="001",
         errcode=0, head="$SH"):
    data = {} if uuid is None else {"errcode": errcode, "uuid": uuid}
    if errcode is None:
        data.pop("errcode", None)
    return (f"gateway/{sn}/req",
            json.dumps({"head": head, "ctype": ctype, "id": msg_id,
                        "sn": sn, "data": data}))


class TestCheckSingleAck:
    def _run(self, seen, sn=NEW_GW, msg_id=7101):
        calls = []
        ns = _load_helpers(die_calls=calls)
        try:
            ns["_check_single_ack"](seen, sn, msg_id, "T")
        except SystemExit:
            pass
        return calls

    def test_one_good_ack_passes(self):
        assert self._run([_ack(NEW_GW, 7101)]) == []

    def test_two_acks_die(self):
        """仲裁失效（v1.7.30 前的 1 请求 2~3 答）必须真红。"""
        calls = self._run([_ack(NEW_GW, 7101), _ack(NEW_GW, 7101)])
        assert len(calls) == 1 and "恰好 1 条" in calls[0]

    def test_zero_ack_dies(self):
        calls = self._run([])
        assert len(calls) == 1 and "实得 0" in calls[0]

    def test_wrong_id_dies(self):
        calls = self._run([_ack(NEW_GW, 9999)])
        assert len(calls) == 1 and "不同形" in calls[0]

    def test_missing_uuid_dies(self):
        calls = self._run([_ack(NEW_GW, 7101, uuid=None)])
        assert len(calls) == 1 and "uuid" in calls[0]

    def test_nonzero_errcode_dies(self):
        calls = self._run([_ack(NEW_GW, 7101, errcode=5)])
        assert len(calls) == 1 and "errcode" in calls[0]

    def test_unrelated_traffic_on_same_topic_ignored(self):
        """同主题的非 001 报文（handler 自身业务）不计入代答数。"""
        assert self._run([_ack(NEW_GW, 7101, ctype="002"),
                          _ack(NEW_GW, 7101)]) == []

    def test_other_topic_not_counted_by_wait(self):
        msgs = [_ack("E2EGW0000009", 7101), _ack(NEW_GW, 7101)]
        ns = _load_helpers(ack_msgs=msgs)
        got = ns["_wait_acks"](f"gateway/{NEW_GW}/req", 0, timeout=1, settle=0)
        assert [t for t, _ in got] == [f"gateway/{NEW_GW}/req"]


class TestWaitAcksSettles:
    def test_late_second_ack_is_caught(self):
        """反假绿：只等到第一条就返回会漏掉晚到的第二个应答者——助手必须
        在凑够 want 条后再静置 settle 秒重新计数（settle=0 即失效）。"""
        msgs = [_ack(NEW_GW, 7101)]
        ns = _load_helpers(ack_msgs=msgs)
        real_sleep = time.sleep

        def fake_sleep(sec):
            if sec == 0.3 and len(msgs) == 1:
                msgs.append(_ack(NEW_GW, 7101))   # settle 期间第二答到达
            real_sleep(0)

        ns["time"] = SimpleNamespace(sleep=fake_sleep, time=time.time)
        got = ns["_wait_acks"](f"gateway/{NEW_GW}/req", 0, want=1,
                               timeout=2, settle=0.3)
        assert len(got) == 2, f"晚到的第二答必须被计入，实得 {got}"

    def test_settle_constant_is_nonzero(self):
        """静置窗不得被调成 0（调 0 = 本臂退化为"只等第一答"的假绿）。"""
        assert "def _wait_acks(topic, since, want=1, timeout=30, settle=2.0):" in SRC


# ============ ③ 真栈取证配置（run_e2e.sh） ============
def _e2e_configuration_yaml():
    """抽出 run_e2e.sh 预置给 HA 容器的 configuration.yaml 正文。"""
    m = re.search(r'cat > "\$CFG/configuration\.yaml" <<\'EOF\'\n(.*?)\nEOF\n',
                  SH, re.S)
    assert m, "run_e2e.sh 不再预置 configuration.yaml（取证锚丢失）"
    return m.group(1)


class TestE2EStackDiagnostics:
    def test_integration_logger_at_info(self):
        """发现链的早退分支全是 DEBUG、成功路径才是 INFO——不开 INFO，K/L 红时
        diag 只能看到 WARNING+，等于再盲跑一轮 CI（首轮 405 假红的教训）。"""
        assert "custom_components.window_controller_gateway: info" in SH

    def test_default_config_kept(self):
        """反钉：预置的 configuration.yaml 必须自带 `default_config:`。镜像原本
        自动生成那份（HA config.py DEFAULT_CONFIG 首行即此），api/auth/
        onboarding/frontend 全靠它拉起；漏掉＝REST API 整个不存在，driver A 段
        就死，且现象是"HA 端口活着但 /api/ 404"，极难归因。"""
        body = _e2e_configuration_yaml()
        assert re.search(r"^default_config:\s*$", body, re.M), \
            "缺 default_config（REST API/onboarding 全不加载）"

    def test_no_dangling_includes(self):
        """反钉：预置正文里不得出现 !include——automations/scripts/scenes/themes
        那些文件不会随之生成，缺文件的 include 会让配置校验失败、HA 起不来。
        （只看正文，注释里谈这条教训是允许且必要的。）"""
        assert "!include" not in _e2e_configuration_yaml()

    def test_local_and_ci_share_the_driver(self):
        """契约同源不得破：本地 harness 与 CI 跑同一份 driver。"""
        rl = (HERE / "e2e" / "run_local.sh").read_text(encoding="utf-8")
        assert "ha_e2e_driver.py" in rl and "ha_e2e_driver.py" in SH


# ============ ④ WS 客户端台架（真 aiohttp 服务端 + 真握手/真帧） ============
# 首轮 CI 假红就出在这段代码的 API 假设上（REST 405），所以它必须有行为级
# 台架：本地起一个最小 HA /api/websocket，按 2026.9.3 源码的 auth 契约应答，
# 真跑 driver 切片出来的 _ws_call/_hj_flows/_cards_for。
try:
    import aiohttp
    from aiohttp import web
    _HAS_AIOHTTP = True
except ImportError:  # pragma: no cover - CI lint 步骤显式装 aiohttp
    _HAS_AIOHTTP = False

_CARD = {"flow_id": "f1", "handler": "window_controller_gateway",
         "context": {"source": "discovery", "unique_id": NEW_GW.lower()},
         "step_id": "user"}
_FOREIGN_CARD = {"flow_id": "f2", "handler": "mqtt",
                 "context": {"source": "user"}}


class _MockHA:
    """最小 HA WS 端：auth_required → auth(access_token) → auth_ok → result。"""

    def __init__(self, flows=(), auth_ok=True, cmd_ok=True):
        self.flows = list(flows)
        self.auth_ok = auth_ok
        self.cmd_ok = cmd_ok
        self.seen = {"auth": None, "cmds": []}
        self.url = None
        self._loop = None
        self._runner = None
        self._thread = None

    async def _handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.9.3"})
        first = await ws.receive_json()
        self.seen["auth"] = first
        if (not self.auth_ok or first.get("type") != "auth"
                or not first.get("access_token")):
            await ws.send_json({"type": "auth_invalid", "message": "令牌无效"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.9.3"})
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            req = json.loads(msg.data)
            self.seen["cmds"].append(req.get("type"))
            ok = self.cmd_ok and req.get("type") == "config_entries/flow/progress"
            body = {"id": req.get("id"), "type": "result", "success": ok}
            if ok:
                body["result"] = self.flows
            else:
                body["error"] = {"code": "unknown_command", "message": "n/a"}
            await ws.send_json(body)
        return ws

    def __enter__(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = web.Application()
        app.router.add_get("/api/websocket", self._handler)
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def serve():
            asyncio.set_event_loop(self._loop)
            self._runner = web.AppRunner(app)
            self._loop.run_until_complete(self._runner.setup())
            self._loop.run_until_complete(
                web.TCPSite(self._runner, "127.0.0.1", port).start())
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        assert ready.wait(10), "台架 WS 服务未起来"
        self.url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc):
        """停 loop → 清 runner → 关 loop。只 stop 不清理会把在途 accept 协程
        丢给 GC，Windows proactor 下刷一屏 "Task was destroyed but it is
        pending!"，把真问题的日志埋掉。"""
        if self._loop is None:
            return False
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if self._runner is not None:
                self._loop.run_until_complete(self._runner.cleanup())
        finally:
            self._loop.close()
        return False


def _load_flow_helpers(ha_url, token="tok-e2e", die_calls=None):
    """切出 driver 的 WS 查询三件套真跑（HA/TOKEN/die 注入）。"""
    start = SRC.index("def _ws_call(")
    end = SRC.index("def _wait_acks(")
    ns = {"json": json, "HA": ha_url, "TOKEN": token}

    def die(msg):
        if die_calls is not None:
            die_calls.append(msg)
        raise SystemExit(msg)

    ns["die"] = die
    exec(compile(SRC[start:end], str(DRIVER), "exec"), ns)  # noqa: S102
    return ns


@pytest.mark.skipif(not _HAS_AIOHTTP, reason="WS 台架需 aiohttp（HA/CI 自带）")
class TestWsFlowQuery:
    def test_progress_query_finds_card(self):
        with _MockHA(flows=[_CARD, _FOREIGN_CARD]) as ha:
            ns = _load_flow_helpers(ha.url)
            flows = ns["_hj_flows"]()
            assert [f["flow_id"] for f in flows] == ["f1"], \
                "只取本集成 handler 的在途流"
            assert len(ns["_cards_for"](NEW_GW)) == 1
            assert ns["_cards_for"](AUTO_GW) == []
            assert ha.seen["auth"] == {"type": "auth", "access_token": "tok-e2e"}
            assert set(ha.seen["cmds"]) == {"config_entries/flow/progress"}, \
                "每次查询一条 WS 命令（连接 per 查询），不得夹带别的命令"
            assert len(ha.seen["cmds"]) == 3, "上面三次查询各开一条连接"

    def test_auth_invalid_dies(self):
        calls = []
        with _MockHA(auth_ok=False) as ha:
            ns = _load_flow_helpers(ha.url, die_calls=calls)
            with pytest.raises(SystemExit):
                ns["_hj_flows"]()
        assert calls and "认证被拒" in calls[0]

    def test_command_failure_dies_in_strict_mode(self):
        """strict 下命令失败必须 die——不得把失败当"0 张卡"。"""
        calls = []
        with _MockHA(cmd_ok=False) as ha:
            ns = _load_flow_helpers(ha.url, die_calls=calls)
            with pytest.raises(SystemExit):
                ns["_hj_flows"]()
        assert calls and "失败" in calls[0]

    def test_command_failure_returns_none_when_lenient(self):
        """轮询期（strict=False）返回 None 并把错误带回，供最终 die 文案引用。"""
        errs = []
        with _MockHA(cmd_ok=False) as ha:
            ns = _load_flow_helpers(ha.url)
            assert ns["_hj_flows"](strict=False, last_err=errs) is None
            assert ns["_cards_for"](AUTO_GW, strict=False, last_err=errs) is None
        assert errs and len(errs) == 2

    def test_unreachable_server_is_loud_not_empty(self):
        """端口不通（HA 重启窗/URL 错）也必须显式失败，不得静默空列表。"""
        calls = []
        ns = _load_flow_helpers("http://127.0.0.1:1", die_calls=calls)
        with pytest.raises(SystemExit):
            ns["_hj_flows"]()
        assert calls and "取在途发现流失败" in calls[0]

    def test_zero_cards_is_a_real_empty_result(self):
        """L 臂"发现卡 0 张"必须建立在查通了的空结果上（与失败区分开）。"""
        with _MockHA(flows=[]) as ha:
            ns = _load_flow_helpers(ha.url)
            assert ns["_hj_flows"]() == []
            assert ns["_cards_for"](AUTO_GW) == []
