"""v1.1.5 ACR 发布脚本钉：Registry.request 重试/401 换 token、push_blob 换 session。

背景（三连 run 实证的三种死法，全部要有本地钉——不许再拿 CI 当测试机）：
- run 35626978134/35675256894：块写满超时（TimeoutError/SSLEOF）→ request 级重试；
- run 35676700243：layer4 推到 97% 被掐后，**缓存 token 已过期**，新 session 的
  POST 全 401 → 每次重试必须 fresh=True 重取 token；
- session 被服务端掐掉后复用旧 Location 秒败 → push_blob 整块级重启（新 session
  从 0 重传，最多 3 轮）。
"""
import ast
import re
import sys
import textwrap
import time
import urllib.error
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent / "scripts"))

import acr_transcode as ac  # noqa: E402

ac.CHUNK = 4   # 本文件全部上传用例按 4B 块走 2 块节奏（模块级一次性，勿在用例内改）

import pytest  # noqa: E402

REAL_SLEEP = time.sleep     # 必须在夹具短路之前留一份真时钟（`ac.time` 与 `time` 同一模块对象）

AC_SRC = _ROOT.parent / "scripts" / "acr_transcode.py"


def _func_src(name):
    """按函数/方法名抽**块内**源码并去 `#` 注释行。

    本仓纪律（v1.1.40 起写进 CLAUDE 级习惯）：扫描型守卫不许全文件 grep——
    讲病灶的注释/docstring 里正当引用着旧写法，裸标识符匹配会被自己的注释满足。
    """
    src = AC_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == name), None)
    assert node is not None, f"acr_transcode.py 里找不到 {name}（钉桩失去对象）"
    body = textwrap.dedent(ast.get_source_segment(src, node))
    return "\n".join(ln.split("#")[0] for ln in body.splitlines())


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """退避 sleep 在测试里全部短路（穷尽用例真睡要 155s）。"""
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)


class FakeResp:
    def __init__(self, status, body=b"", loc=None):
        self.status, self._body, self._loc = status, body, loc

    def read(self):
        return self._body

    def getheader(self, name, default=None):
        return self._loc if name == "Location" else default


class FakeConn:
    """按脚本队列吐响应；'EIO' 项表示该次 request 抛连接级异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.reqs = []

    def request(self, method, path, body=None, headers=None):
        self.reqs.append((method, path, dict(headers or {})))
        if self.script and self.script[0] == "EIO":
            self.script.pop(0)
            raise TimeoutError("The write operation timed out")

    def getresponse(self):
        return self.script.pop(0)

    def close(self):
        pass


def _reg(script):
    r = ac.Registry("cr.example")
    r._realm, r._service = "https://cr.example/token", "cr.example"
    r._tokens = {}
    fresh_calls = []

    def fake_token(repo, actions, fresh=False):
        fresh_calls.append(fresh)
        return "TOK%d" % len(fresh_calls)
    r.token = fake_token
    conn = FakeConn(script)
    r._conn = lambda timeout=300: conn
    return r, conn, fresh_calls


# ── request：重试与 token 刷新 ─────────────────────────────────

def test_request_success_first_try_uses_cached_token_path():
    r, conn, fresh = _reg([FakeResp(202, loc="/uploads/abc")])
    st, loc, _ = r.request("POST", "/v2/x/blobs/uploads/", "x", b"",
                           headers={"Content-Length": "0"})
    assert (st, loc) == (202, "/uploads/abc")
    assert fresh == [False], "首试不必重取 token"
    assert conn.reqs[0][2]["Authorization"] == "Bearer TOK1"


def test_request_io_error_retry_refreshes_token_and_succeeds():
    r, conn, fresh = _reg(["EIO", FakeResp(202, loc="/u/2")])
    st, loc, _ = r.request("PATCH", "/u/2?stage=1", "x", b"chunk",
                           headers={"Content-Type": "application/octet-stream"})
    assert (st, loc) == (202, "/u/2")
    assert fresh == [False, True], "第二次必须 fresh=True 重取"
    assert conn.reqs[-1][2]["Authorization"] == "Bearer TOK2"


def test_request_401_then_refresh_succeeds():
    # run 35678680808 主签名：POST 新 session 拿 401（token 过期）→ 重取后成功
    r, conn, fresh = _reg([FakeResp(401, b'{"errors":[{"code":"UNAUTHORIZED"}]}'),
                           FakeResp(202, loc="/u/new")])
    st, loc, _ = r.request("POST", "/v2/x/blobs/uploads/", "x", b"")
    assert (st, loc) == (202, "/u/new")
    assert fresh == [False, True]


def test_request_persistent_401_raises_not_silent():
    r, _, fresh = _reg([FakeResp(401, b"nope"), FakeResp(401, b"nope"),
                        FakeResp(401, b"nope"), FakeResp(401, b"nope"),
                        FakeResp(401, b"nope"), FakeResp(401, b"nope")])
    try:
        r.request("POST", "/v2/x/blobs/uploads/", "x", b"")
        raise AssertionError("持续 401 必须抛")
    except RuntimeError as e:
        assert "401" in str(e)


def test_request_status_error_no_retry():
    r, conn, _ = _reg([FakeResp(404, b"missing")])
    try:
        r.request("GET", "/v2/x/whatever", "x", b"")
        raise AssertionError("非 401 状态错必须抛")
    except RuntimeError as e:
        assert "404" in str(e)
    assert len(conn.reqs) == 1, "语义错不得重试"


def test_request_exhausts_and_raises():
    r, _, _ = _reg(["EIO"] * 6)
    try:
        r.request("PATCH", "/u/x", "x", b"z")
        raise AssertionError("重试穷尽必须抛")
    except RuntimeError as e:
        assert "重试 6 次" in str(e)


# ── Registry.get：401 刷新重试（run 35681170094 签名）──────────

def _http_get_script(codes):
    """返回 (callable, calls)：按队列吐 200/抛 HTTPError(code)。"""
    calls = []

    def fake(url, headers=None, timeout=60, binary=False):
        calls.append(dict(headers or {}))
        c = codes.pop(0)
        if c == 200:
            return b"body", {}
        raise urllib.error.HTTPError(url, c, "e", {}, None)
    return fake, calls


def test_get_401_refreshes_token_and_succeeds(monkeypatch):
    r, _, fresh = _reg([])
    fake, calls = _http_get_script([401, 200])
    monkeypatch.setattr(ac, "http_get", fake)
    body, _ = r.get("/v2/x/blobs/sha", "x")
    assert body == b"body"
    assert fresh == [False, True], "401 后必须 fresh 重取"
    assert calls[1]["Authorization"] == "Bearer TOK2"


def test_get_non401_raises_without_retry(monkeypatch):
    r, _, fresh = _reg([])
    fake, calls = _http_get_script([404])
    monkeypatch.setattr(ac, "http_get", fake)
    try:
        r.get("/v2/x/blobs/sha", "x")
        raise AssertionError("404 必须原样抛（秒传分支语义）")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    assert len(calls) == 1, "非 401 不得重试"


def test_push_blob_instant_skip_after_401_recovery(monkeypatch):
    # 集成钉（第四轮审计 P3 改真 HEAD）：探测用 urlopen(HEAD)——401→fresh 重取
    # →200 命中即跳过上传（不再走 POST）。桩面同步从 http_get 换成 urlopen。
    class _Ok:
        def __init__(self, code):
            self._code = code

        def read(self):
            return b""

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((getattr(req, "method", None), req.full_url,
                      dict(getattr(req, "headers", {}) or {})))
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "unauth", {}, None)
        return _Ok(200)

    monkeypatch.setattr(ac.urllib.request, "urlopen", fake_urlopen)
    r, _conn, fresh = _reg([])
    ac.push_blob(r, "x", "sha256:d", b"data", "layer7")
    assert [c[0] for c in calls] == ["HEAD", "HEAD"], "必须真 HEAD 探测"
    assert fresh == [False, True], "401 后必须 fresh 重取 token"


# ── push_blob：秒传 / 换 session 重启 ──────────────────────────

class _Store:
    def __init__(self, get_raises=None):
        self.get_raises = get_raises
        self.calls = []
        self.conn = None

    def make(self, script):
        self.conn = FakeConn(script)
        return self.conn

    def request(self, method, path, repo, body, headers=None,
                actions=("pull", "push"), status_ok=(200, 201, 202, 204),
                attempts=6):
        self.calls.append((method, path))
        if self.conn.script and self.conn.script[0] == "EIO":
            self.conn.script.pop(0)
            raise RuntimeError("PATCH /u 重试 6 次仍失败：boom")
        r = self.conn.script.pop(0)
        if r.status not in status_ok:
            raise RuntimeError(f"{method} {path} → {r.status}")
        return r.status, r.getheader("Location"), r.read()


def test_push_blob_instant_skip_no_upload():
    s = _Store()
    s.head = lambda path, repo, actions=("pull",), **kw: (b"", {})
    ac.push_blob(s, "x", "sha256:d", b"data", "layer0")
    assert s.calls == [], "秒传命中不得走上传"


def test_push_blob_instant_skip_only_on_404():
    s = _Store()
    s.head = lambda path, repo, actions=("pull",), **kw: (_ for _ in ()).throw(
        urllib.error.HTTPError(path, 500, "boom", {}, None))
    try:
        ac.push_blob(s, "x", "sha256:d", b"data", "layer0")
        raise AssertionError("非 404 的 HEAD 异常必须原样抛")
    except urllib.error.HTTPError as e:
        assert e.code == 500


def test_push_blob_happy_path_chunk_then_finalize():
    s = _Store()
    s.head = lambda path, repo, actions=("pull",), **kw: False   # 404=未在盘
    s.conn = FakeConn([
        FakeResp(202, loc="/u/1"),                       # POST session
        FakeResp(202, loc="/u/1"), FakeResp(202, loc="/u/1"),  # 2×PATCH(8B/4B)
        FakeResp(201),                                    # PUT finalize
    ])
    ac.push_blob(s, "x", "sha256:d", b"abcdefgh", "layer9")
    assert [m for m, _ in s.calls] == ["POST", "PATCH", "PATCH", "PUT"]
    assert "digest=sha256:d" in s.calls[-1][1]


def test_push_blob_session_death_restarts_with_new_session():
    s = _Store()
    s.head = lambda path, repo, actions=("pull",), **kw: False   # 404=未在盘
    # 第一轮：POST ok → PATCH ok → 第二轮 PATCH 死（重试穷尽）；
    # 重启轮：POST 新 session → 2×PATCH → PUT 成功。
    s.conn = FakeConn([
        FakeResp(202, loc="/u/old"), FakeResp(202, loc="/u/old"), "EIO",
        FakeResp(202, loc="/u/new"), FakeResp(202, loc="/u/new"),
        FakeResp(202, loc="/u/new"), FakeResp(201),
    ])
    ac.push_blob(s, "x", "sha256:d", b"abcdefgh", "layer4")
    posts = [p for m, p in s.calls if m == "POST"]
    assert len(posts) == 2, "整块失败必须重开 session"
    patches = [p for m, p in s.calls if m == "PATCH"]
    assert all("/u/new" in p for p in patches[2:]), "重启轮 PATCH 走新 session"
    assert s.calls[-1][0] == "PUT"


# ── v1.1.17 收口：墙钟预算自洽（单块卡死不得耗死整个发版）──────────────
def test_chain_deadline_is_within_job_budget():
    """预算关系钉：链时钟必须显著小于 CI job 预算，且小于"单块卡死"的最坏代价
    （3 轮 × 6 次 × 300s socket 超时 = 5400s）——否则就是 v1.1.14 那轮的形状：
    90.1min 被 CI 硬杀、日志停在 layer6 54.5/76.5MB。

    v1.1.41 改名 `PUSH_DEADLINE_S` → `CHAIN_DEADLINE_S`：这条时钟**覆盖整条链**
    （下载+推送），旧名谎报成只管推送，于是下载腿自建 900s×20 块的独立预算，
    最坏 5h ≫ job 90min——闸护不到 job（复验 §1.2）。
    """
    assert 0 < ac.CHAIN_DEADLINE_S <= 75 * 60, ac.CHAIN_DEADLINE_S
    assert 3 * 6 * 300 > ac.CHAIN_DEADLINE_S, "预算够不到最坏单块代价＝截断不了卡死"


def test_deadline_expired_aborts_before_any_attempt():
    """行为钉：预算已耗尽 ⇒ 一次请求都不发就抛具名 RuntimeError（不再无限重试）。"""
    ac.chain_deadline_reset(0.0)
    try:
        r, conn, _ = _reg(["EIO"] * 40)
        with pytest.raises(RuntimeError, match="墙钟预算"):
            r.request("PATCH", "/u/1?stage=1", "x", b"chunk", headers={})
        assert conn.reqs == [], "预算耗尽后仍发了请求"
    finally:
        ac.chain_deadline_clear()


def test_deadline_ample_keeps_old_retry_behaviour():
    """反向钉：预算充足 ⇒ 旧重试语义一字不动（穷尽 attempts 后抛原错误，不抛预算错）。"""
    ac.chain_deadline_reset(3600)
    try:
        r, conn, _ = _reg(["EIO"] * 40)
        with pytest.raises(Exception) as ei:      # noqa: PT011 具体类型由旧语义决定
            r.request("PATCH", "/u/1?stage=1", "x", b"chunk", headers={})
        assert "墙钟预算" not in str(ei.value)
        assert len(conn.reqs) == 6, "attempts 语义被改（旧 6 次）"
    finally:
        ac.chain_deadline_clear()


def test_deadline_accounts_for_both_transcodes_in_one_job():
    """预算算术要按**同一 job 内串行两次 transcode**（双架构）算，且与 ci.yaml 的
    job 上限对账——v1.1.17 首版写 60min 只按单链算，两档都慢时第二档照样被 CI 硬杀
    （1.1.16 的 run 就是这么死的：90min 整点 cancel ⇒ Release 被 skip）。"""
    import re
    from pathlib import Path
    ci = (Path(__file__).resolve().parents[2] / ".github/workflows/ci-voice.yaml").read_text(
        encoding="utf-8")
    m = re.search(r"push-acr:.*?timeout-minutes:\s*(\d+)", ci, re.S)
    assert m, "找不到 push-acr job 的 timeout-minutes"
    job_min = int(m.group(1))
    two_chains_min = 2 * ac.CHAIN_DEADLINE_S / 60
    assert two_chains_min + 10 <= job_min, \
        f"两档预算 {two_chains_min:.0f}min + 10min 余量 > job {job_min}min ⇒ 仍会被硬杀"
    # 复验 §1.2：`PUSH`→`CHAIN` 只是名义，**实质要求是下载腿也查这条时钟**——
    # 否则整链最坏＝blob 数 × FETCH_DEADLINE_S（19 层 + config ≈ 5h）≫ job 90min，
    # 闸护不到 CI job。"逐块之和 > job"是**允许**的，前提是它被链时钟夹住；
    # 夹不住（旧形）时这里判红，同时行为面由 test_get_blob_honors_chain_deadline 兜。
    blob_count = 20                     # 19 层 + config（本次发版的真形）
    assert blob_count * ac.FETCH_DEADLINE_S > job_min * 60, \
        "靶子变了：逐块预算之和已能盖进 job，链时钟 clamp 这条要按新数值重算"
    assert ac.CHAIN_DEADLINE_S < job_min * 60 / 2, \
        "链时钟必须小于 job 的一半，否则两档串行照样被硬杀"
    gb = _func_src("get_blob").replace(" ", "").replace("\n", "").replace("\r", "")
    assert "if_deadline_at:end=min(end,_deadline_at)" in gb, \
        "下载腿不查链时钟 ⇒ 整链最坏 = blob 数 × FETCH_DEADLINE_S，闸护不到 CI job"
    body = _func_src("cmd_transcode")
    assert "chain_deadline_reset()" in body and body.index("chain_deadline_reset()") \
        < body.index("get_blob("), "链时钟必须在**第一个 blob 之前**起算（config 也得在闸内）"


# ══ v1.1.39：下载腿必须有墙钟总时限（本机代推两次 595s 零层完成的复盘）══════
class _BResp:
    """只喂数据的假响应。

    ⚠ 它**不建模阻塞**：真 `HTTPResponse.read(n)` 是 BufferedReader 语义（攒满 n 才
    返回），这里每调用一次给一块。所以"低速滴流下墙钟闸是否可达"不能用本替身判——
    那正是复验点名的"假响应把被测语义替换掉了"。阻塞语义另立一钉
    （`test_wall_clock_fires_while_read_would_block`，用 `_DripResp`）。
    """

    def __init__(self, status, chunks):
        self.status = status
        self._it = iter(chunks)
        self.closed = 0

    def read(self, n=-1):
        try:
            v = next(self._it)
        except StopIteration:
            return b""
        if isinstance(v, Exception):
            raise v
        return v

    def read1(self, n=-1):          # 生产走 read1；本替身两者同义（喂数据用）
        return self.read(n)

    def close(self):
        self.closed += 1


class _Env(list):
    """`_blob_env` 的返回：本身是 urlopen 收到的 req 列表（旧钉按 `calls[0]` 用），
    附带 `.resps`（响应替身，查 close 计数）与 `.fresh`（token fresh 序列）。"""

    def __init__(self):
        super().__init__()
        self.resps = []
        self.fresh = []


def _blob_env(monkeypatch, script):
    """script 项：`(status, [chunks])` 正常响应，或 `(异常, None)` 让 urlopen 抛。"""
    env = _Env()
    seq = list(script)

    def fake_open(req, timeout=None):
        env.append(req)
        status, chunks = seq.pop(0)
        if isinstance(status, Exception):
            raise status
        resp = _BResp(status, chunks)
        env.resps.append(resp)
        return resp

    def fake_token(self, repo, actions, fresh=False):
        env.fresh.append(bool(fresh))
        return "T"

    monkeypatch.setattr(ac.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(ac.Registry, "token", fake_token)
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    return env


GOOD = "sha256:" + __import__("hashlib").sha256(b"ABCDEF").hexdigest()


def test_get_blob_resumes_with_range_when_server_honors_206(monkeypatch):
    calls = _blob_env(monkeypatch, [
        (200, [b"ABC", OSError("滴流中断")]),
        (206, [b"DEF"]),
    ])
    reg = ac.Registry("example.invalid")
    raw, _ = reg.get_blob("/v2/ns/repo/blobs/x", "ns/repo", label="layer0",
                          size=6, expect_digest=GOOD, chunk=4, deadline_s=30)
    assert raw == b"ABCDEF", raw
    assert calls[0].get_header("Range") is None
    assert calls[1].get_header("Range") == "bytes=3-",         "续传必须带 Range，否则每次断流都从头再来"


def test_get_blob_discards_partial_when_source_ignores_range(monkeypatch):
    """源对 ranged 请求回 200 全量（代理常见）⇒ 已收字节必须丢弃重头取，
    否则 200 的全量体会拼在半截后面＝静默损坏后推上 ACR。"""
    _blob_env(monkeypatch, [
        (200, [b"ABC", OSError("断")]),
        (200, [b"ABCDEF"]),
    ])
    reg = ac.Registry("example.invalid")
    raw, _ = reg.get_blob("/v2/ns/repo/blobs/x", "ns/repo", label="layer1",
                          size=6, expect_digest=GOOD, chunk=8, deadline_s=30)
    assert raw == b"ABCDEF", raw


def test_get_blob_wall_clock_is_named_not_silent(monkeypatch):
    """滴流源：每块都有字节、永不 EOF——正是把发布钉死的那种链路。"""
    class Never:
        status = 200

        def read(self, n=-1):
            return b"x"

        read1 = read

        def close(self):
            pass

    monkeypatch.setattr(ac.urllib.request, "urlopen", lambda req, timeout=None: Never())
    monkeypatch.setattr(ac.Registry, "token",
                        lambda self, repo, actions, fresh=False: "T")
    reg = ac.Registry("example.invalid")
    try:
        reg.get_blob("/v2/ns/repo/blobs/x", "ns/repo", label="layer9",
                     chunk=1, deadline_s=0.2)
    except RuntimeError as e:
        assert "墙钟" in str(e) and "layer9" in str(e), e
    else:
        raise AssertionError("无墙钟＝滴流源可把发布钉死（v1.1.39 实锤）")


# ══ 复验 §1.1（P1）/ §1.2 / §2.1-§2.4：本批判红的那几条 ═════════════════
class _DripResp:
    """按**真语义**建模两条读法（ACR 下载腿复验 §1.1 的核心）。

    真 `HTTPResponse.read(n)` 会攒满 n 才返回 ⇒ 滴流不断时一次调用可堵 `chunk/速率`
    秒，而墙钟复检在循环顶部 ⇒ **永不可达**。本桩给一个 `block_s` 堵头（真链路上
    这里是"永远"，测试不许真挂住），到点按 EOF 收——于是"闸没响"这条错误路径仍可断言。
    """

    def __init__(self, drip=b"x", every=0.002, block_s=1.5, max_reads=2000):
        self.drip, self.every, self.block_s = drip, every, block_s
        self.max_reads = max_reads        # 读次上限：没有它，删掉复检的变异体会把套件挂死
        self.n_reads = 0
        self.t0 = time.monotonic()
        self.closed = 0
        self.status = 200

    def read1(self, n=-1):
        self.n_reads += 1
        if self.n_reads > self.max_reads:
            return b""                    # 模拟 EOF：让"复检被删"的变异体报错而不是挂死
        REAL_SLEEP(self.every)            # 一次底层读 = 一滴
        return self.drip

    def read(self, n=-1):
        want = n if n and n > 0 else (1 << 20)
        acc = bytearray()
        while len(acc) < want:
            if (time.monotonic() - self.t0 >= self.block_s
                    or len(acc) >= self.max_reads):
                return b""                # 堵头：模拟成"源到这儿就结束了"
            REAL_SLEEP(self.every)
            acc += self.drip
        return bytes(acc)

    def close(self):
        self.closed += 1


def _drip_env(monkeypatch):
    """autouse 夹具把 `time.sleep` 短路了（穷尽用例真睡要 155s），而 `ac.time` 就是
    同一个模块对象——所以这里**不能**取 `time.sleep`（那取到的是已被短路的假货），
    要用模块导入时留下的 `REAL_SLEEP`。否则滴流桩一秒攒满一块，被测语义又被替换掉了。"""
    monkeypatch.setattr(ac.time, "sleep", REAL_SLEEP)
    monkeypatch.setattr(ac.Registry, "token",
                        lambda self, repo, actions, fresh=False: "T")


def test_wall_clock_fires_while_read_would_block(monkeypatch):
    """P1 主钉：预算 0.2s，滴流 500B/s（chunk=1MB ⇒ read 要攒 2000s）。
    用 `read(chunk)` 时复检永不可达（本桩到 1.5s 堵头按 EOF 收，报错变成"零字节"）；
    用 `read1(chunk)` 时 0.2s 当场抛具名预算错。真 socket 同形见
    `_goldtest/adv_verify_acr_drip.py`（read ⇒ 6s 看门狗 0 次复检 / read1 ⇒ 2.0s 响）。"""
    _drip_env(monkeypatch)
    monkeypatch.setattr(ac.urllib.request, "urlopen",
                        lambda req, timeout=None: _DripResp(drip=b"x", every=0.002))
    t0 = time.monotonic()
    with pytest.raises(RuntimeError) as ei:
        ac.Registry("example.invalid").get_blob(
            "/p", "r", label="layerD", chunk=1 << 20, deadline_s=0.2)
    spent = time.monotonic() - t0
    assert "墙钟预算" in str(ei.value), f"没报具名预算错（旧形 read 会掉这里）：{ei.value}"
    assert spent < 1.0, f"闸比预算晚到 {spent:.2f}s（堵头 1.5s）＝复检被 read 的阻塞吞了"


def test_get_blob_uses_read1_not_read():
    """反向源形状钉：`read(chunk)` 不许回到读流（写回旧形时上一钉要能红，
    而这条给"改了读法但语义仍不对"的中间态兜底）。"""
    gb = _func_src("get_blob")
    assert "resp.read1(chunk)" in gb, "读流没走 read1"
    assert not re.search(r"resp\.read\(", gb), "read(chunk) 回到读流＝复检永不可达"


def test_get_blob_honors_chain_deadline(monkeypatch):
    """链时钟到点时**下载腿**也要当场停下并报"链"（旧形只护推送腿）。"""
    _blob_env(monkeypatch, [(200, [b"x"] * 5000)])
    ac.chain_deadline_reset(0.0)
    try:
        with pytest.raises(RuntimeError) as ei:
            ac.Registry("example.invalid").get_blob(
                "/p", "r", label="layer3", chunk=1, deadline_s=30)
        msg = str(ei.value)
        assert "墙钟预算" in msg and "下载" in msg, f"没点名卡在下载腿：{msg}"
        assert "字节数不符" not in msg and "零字节" not in msg, msg
    finally:
        ac.chain_deadline_clear()


def test_416_with_full_bytes_goes_to_validation(monkeypatch):
    """字节已收满、只差一个干净 EOF ⇒ 源对 `bytes=6-` 回 416 是**正确**答复。
    旧形裸 raise 让 HTTPError 直穿调用方＝手里明明有完整数据却连校验机会都没有。"""
    err416 = urllib.error.HTTPError("https://h/p", 416, "range not satisfiable", None, None)
    env = _blob_env(monkeypatch, [(200, [b"ABCDEF", OSError("读流断了，没给 EOF")]),
                                  (err416, None)])
    raw, _ = ac.Registry("example.invalid").get_blob(
        "/p", "r", label="layer4", size=6, expect_digest=GOOD, chunk=8, deadline_s=30)
    assert raw == b"ABCDEF", f"416 后没走到校验：{raw!r}"
    assert env[1].get_header("Range") == "bytes=6-", "续传请求应按已收字节偏移"


def test_416_with_partial_bytes_raises_named(monkeypatch):
    """数据没收满就 416：不许静默当完整体推上去，要抛具名错（size 不符即可判）。"""
    err416 = urllib.error.HTTPError("https://h/p", 416, "range not satisfiable", None, None)
    _blob_env(monkeypatch, [(200, [b"AB", OSError("断")])] + [(err416, None)] * 6)
    with pytest.raises(RuntimeError) as ei:
        ac.Registry("example.invalid").get_blob("/p", "r", label="layer5", size=6,
                                                chunk=4, deadline_s=30)
    assert "字节数不符" in str(ei.value), ei.value


def test_non_401_error_does_not_double_request(monkeypatch):
    """内层只为 401 换 fresh token 再打一次；500 这类语义错重发不会变好。
    旧形所有错误都走内层 ⇒ 6 次 attempt 打 **12** 次（实测），日志同一"第N次"两行。"""
    err500 = urllib.error.HTTPError("https://h/p", 500, "boom", None, None)
    env = _blob_env(monkeypatch, [(err500, None)] * 12)
    with pytest.raises(RuntimeError):
        ac.Registry("example.invalid").get_blob("/p", "r", label="layer6",
                                                chunk=4, deadline_s=30)
    assert len(env) == 6, f"每次 attempt 打了 {len(env) / 6:.1f} 遍（错误流量翻倍）"
    assert not any(env.fresh), f"非 401 也去重取 token：{env.fresh}"


def test_401_still_retries_once_with_fresh_token(monkeypatch):
    """反向不变量：401 就地换 fresh token 重打一次的旧语义不许被一起砍掉。"""
    err401 = urllib.error.HTTPError("https://h/p", 401, "expired", None, None)
    env = _blob_env(monkeypatch, [(err401, None), (200, [b"ABCDEF"])])
    sleeps = []
    monkeypatch.setattr(ac.time, "sleep", lambda s: sleeps.append(s))
    raw, _ = ac.Registry("example.invalid").get_blob("/p", "r", label="layer7",
                                                     size=6, expect_digest=GOOD,
                                                     chunk=8, deadline_s=30)
    assert raw == b"ABCDEF"
    assert len(env) == 2 and env.fresh == [False, True], env.fresh
    assert sleeps == [], f"401 就地换 token 重打不该退避（旧形也不退）：{sleeps}"


def test_backoff_is_once_between_attempts(monkeypatch):
    """轮与轮之间**恰好**退避一次：既不许像旧形那样睡两遍，也不许被改造顺手删掉
    （本批自己差点犯后者——把内层 sleep 删干净后 6 轮请求会变成背靠背爆发）。"""
    sleeps = []
    err500 = urllib.error.HTTPError("https://h/p", 500, "boom", None, None)
    env = _blob_env(monkeypatch, [(err500, None)] * 12)
    monkeypatch.setattr(ac.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(RuntimeError):
        ac.Registry("example.invalid").get_blob("/p", "r", label="layerC",
                                                chunk=4, deadline_s=30)
    assert len(env) == 6, f"每轮打了 {len(env) / 6:.1f} 遍"
    assert len(sleeps) == 5, f"退避 {len(sleeps)} 次 / 6 轮（轮间应恰 5 次）"
    assert sleeps == sorted(sleeps) and sleeps[0] > 0, sleeps


def test_every_response_is_closed(monkeypatch):
    """下载腿也要关响应（push 腿早有 `finally: c.close()`，这一腿实测 0 次）。"""
    env = _blob_env(monkeypatch, [(200, [b"ABC", OSError("断")]), (206, [b"DEF"])])
    ac.Registry("example.invalid").get_blob("/p", "r", label="layer8", size=6,
                                            expect_digest=GOOD, chunk=4, deadline_s=30)
    assert len(env.resps) == 2
    assert all(r.closed >= 1 for r in env.resps), \
        f"响应没关：{[r.closed for r in env.resps]}（套接字只能等 GC）"


def test_budget_exhausted_at_attempt_start_names_budget(monkeypatch):
    """同一个病不许三种报错：本轮起点就已到点 ⇒ 必须报具名预算错，
    而不是掉进「零字节」/「字节数不符」让人以为是源坏了（复验 §2.4）。"""
    _blob_env(monkeypatch, [(200, [b"AB", OSError("断")])])
    ac.chain_deadline_clear()            # 隔离：上一钉不该把链时钟留给这一钉
    reg = ac.Registry("example.invalid")
    with pytest.raises(RuntimeError) as ei:
        # 第 2 轮起点必然超期：预算给到 0（attempt 循环顶部就 break）
        reg.get_blob("/p", "r", label="layerB", size=6, chunk=4, deadline_s=0.0)
    assert "墙钟预算" in str(ei.value), f"报错口径又不一致：{ei.value}"


def test_get_blob_rejects_corrupt_reassembly(monkeypatch):
    _blob_env(monkeypatch, [(200, [b"ABC", OSError("断")]), (206, [b"XXX"])])
    reg = ac.Registry("example.invalid")
    try:
        reg.get_blob("/v2/ns/repo/blobs/x", "ns/repo", label="layer2",
                     size=6, expect_digest=GOOD, chunk=4, deadline_s=30)
    except RuntimeError as e:
        assert "sha256 不符" in str(e), e
    else:
        raise AssertionError("拼接损坏的层被当合法层推上去＝用户刷完变砖")


def test_transcode_download_leg_uses_bounded_fetch():
    body = _func_src("cmd_transcode")
    assert "get_blob(" in body, "配置/层下载退回一把吞的 get(binary=True)"
    # 按**调用点**判，不禁字面量：`binary=True` 在别的调用（如将来取小文件）里合法，
    # 全函数禁一遍会把钉本身变成将来动不得的墙（复验 §2.6）。要禁的是"下载腿走
    # 无墙钟的整块读"，那就只许出现在 blob 之外的调用上。
    assert not re.search(r"\.get\([^)]*binary=True", body), \
        "cmd_transcode 里出现无墙钟的整块 GET（下载腿必须走 get_blob）"
    assert "chain_deadline_reset()" in body, "整条链没有起算时钟（旧形只在推送阶段起算）"
    assert "push_deadline_reset()" not in body, "时钟旧名残留＝下载腿又不查它了"
    # 链尾也必须在闸内（复验 ②b：旧形 `chain_deadline_clear()` 在层循环的 finally，
    # config push 与 manifest PUT 全程"闸外写"——预算已尽时被 CI 掐成半条 manifest
    # 挂在 ACR 上，比不发布更坏）。
    assert body.index("push_manifest(") < body.index("chain_deadline_clear()"), \
        "manifest PUT 落在链时钟之外＝闸外写"


LOCAL_PUSHER = _ROOT.parent / "_acr_local_push.py"


def test_blob_stream_goes_through_the_proxy_seam():
    """取流必须走 `stream_open` 这道 seam——本机代推靠钩它把 ghcr 数据面重写到国内代理
    （直连 ghcr 本机实测 ≈37KB/s＝不可用，代理 ≈2MB/s）。

    发版前对抗复核抓出的**本批新引入回归**：blob 下载从 `get(binary=True)`（走
    `http_get`，被钩）换成 `get_blob` 流式读（直调 `urllib.request.urlopen`）之后，
    20 个 blob 会绕开钩子变回直连——正是这批要治的那条慢链路。
    """
    gb = _func_src("get_blob")
    assert "stream_open(req, timeout=sock_timeout)" in gb, "取流没走 seam"
    assert not re.search(r"urllib\.request\.urlopen", gb), \
        "get_blob 里直连 urlopen＝绕开本机代推的代理钩子（发版会退回直连慢链路）"
    if LOCAL_PUSHER.exists():      # gitignored 本地工装：在位才双钉（同 yyjicheng 先例）
        lp = LOCAL_PUSHER.read_text(encoding="utf-8")
        assert "at.stream_open = patched_stream" in lp, \
            "_acr_local_push.py 只钩 http_get 已护不住流式读，必须同时钩 stream_open"


def test_deadline_arithmetic_includes_overshoot():
    """预算要和**超冲**一起与 job 对账：旧钉只算 `2×40+10 ≤ 90`＝刚好卡线，
    把在途读写超时与退避算进去就漏了（复验 C 段；合成实测预算 2s 实耗 8.03s）。"""
    ci = (_ROOT.parent / ".github/workflows/ci-voice.yaml").read_text(encoding="utf-8")
    m = re.search(r"push-acr:.*?timeout-minutes:\s*(\d+)", ci, re.S)
    assert m, "找不到 push-acr job 的 timeout-minutes"
    job_s = int(m.group(1)) * 60
    per_chain = ac.CHAIN_DEADLINE_S + ac.OVERSHOOT_S
    assert 2 * per_chain + 300 <= job_s, (
        f"两链最坏 {2 * per_chain / 60:.1f}min + 5min 其它步骤 > job {job_s / 60:.0f}min"
        " ⇒ 闸还没到点 CI 先硬杀，等于没闸")
    assert ac.OVERSHOOT_S >= 300 + 60, "超冲必须盖住一次在途写超时与一次在途读超时"


def test_budget_error_prints_the_budget_actually_armed():
    """报错里的秒数必须是**本次起算值**，不是模块默认（复验 C 段小瑕：reset(20)
    却报「预算耗尽（2400s）」＝把人往错的方向带）。"""
    ac.chain_deadline_reset(0.0)
    try:
        r, _, _ = _reg(["EIO"] * 4)
        with pytest.raises(RuntimeError) as ei:
            r.request("PATCH", "/u/1?stage=1", "x", b"chunk", headers={})
        msg = str(ei.value)
        assert "墙钟预算耗尽" in msg and "推送" in msg, msg
        assert f"{ac.CHAIN_DEADLINE_S:.0f}s" not in msg, \
            f"报错谎报了实际起算的预算：{msg}"
    finally:
        ac.chain_deadline_clear()
