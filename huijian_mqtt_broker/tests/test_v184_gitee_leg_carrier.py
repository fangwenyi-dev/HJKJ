"""v1.8.4：Gitee Release 腿的载体与有界重试——把整段脚本抽出来用假 curl **真跑**。

为什么这里必须有行为级判据：v1.8.3 发版时这条腿连红三次（python urllib 打 gitee 的 TLS
握手 30s 超时，而**同一个 job** 里前置的"等 sha 落 Gitee"步用 curl 2.3 秒返回 200），
最后那条 Release 是手工补的（id=1189310）。而当时守这条腿的钉全是字样级（判"某串在不在
场"）——"字样在、行为已被阉"正是本仓反复复发的假绿形态（见 test_v1621 里
`test_gitee_sha_wait_gate_fails_loudly_not_silently` 的对抗复核记录）。换 curl 之后
`subprocess` 可以桩掉，旧注释里那句"行为级要真发 API，留给 CI"的前提已经不成立 ⇒ 补齐。
"""
import contextlib
import io
import json
import pathlib
import sys
import types

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = ROOT.parent
CI = REPO / ".github" / "workflows" / "ci.yaml"
TOKEN = "FAKE-GITEE-TOKEN-0123456789"
FAKE_SHA = "caad663fef2f6eee473f062228f5f8d45cf58f1a"


class _Resp:
    """subprocess.CompletedProcess 的替身（只带脚本用到的三个字段）。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _http(code, payload="null"):
    """curl --write-out 的产物形态：正文 + 换行 + HTTP 状态码。

    payload=None ⇒ 不写正文（curl 只吐状态码行）；payload="null" 才是 Gitee
    "不存在的 Release" 的真返回（200 + body null，2026-10-06 实探针）。
    """
    text = "null" if payload == "null" else ("" if payload is None else json.dumps(payload))
    return _Resp(0, f"{text}\n{code}")


def _leg_source():
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = wf["jobs"]["gitee-release"]["steps"]
    step = next(s for s in steps if "Create or sync" in s.get("name", ""))
    src = step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    compile(src, "gitee-leg", "exec")
    return src


def _run_leg(monkeypatch, tmp_path, script, src=None, version="9.9.9"):
    """在假 cwd（无 CHANGELOG ⇒ 正文回落 PREPARED，判据可控）里真跑整段脚本。"""
    src = src or _leg_source()
    fake = _FakeCurl(script)
    sleeps = []

    sub = types.ModuleType("subprocess")
    sub.run = fake.run
    tm = types.ModuleType("time")
    tm.sleep = lambda s: sleeps.append(s)
    # 桩全局态必须还原——monkeypatch.setitem 在每条测试结束自动复原
    monkeypatch.setitem(sys.modules, "subprocess", sub)
    monkeypatch.setitem(sys.modules, "time", tm)
    monkeypatch.setenv("VERSION", version)
    monkeypatch.setenv("COMMIT", FAKE_SHA)
    monkeypatch.setenv("GITEE_TOKEN", TOKEN)
    monkeypatch.setenv("GITEE_REPO", "someone/Repo")
    monkeypatch.setenv("PREPARED", "##  prepared body 导语")
    monkeypatch.chdir(tmp_path)

    out, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(src, "gitee-leg", "exec"), {"__name__": "__main__"})
    except SystemExit as exc:
        code = exc.code or 0
    return code, out.getvalue(), fake.calls, sleeps


class _FakeCurl:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def run(self, args, **_kw):
        self.calls.append(list(args))
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return item


def _body_of(argv):
    for i, a in enumerate(argv):
        if a == "--data-binary":
            return json.loads(argv[i + 1])
    return None


# ============ 载体本身：只认非注释行 ============

def test_leg_calls_gitee_via_curl_not_urllib():
    """载体定案的存证。判据必须**剥掉注释行**再比——否则一句"以前用 urllib"的说明
    就能把它喂绿（本仓实发过：我自己写的新注释正好满足了另一条旧钉的字样）。
    """
    src = _leg_source()
    code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    joined = "\n".join(code_lines)
    for dead in ("urllib.request", "urllib.error", "urlopen("):
        assert dead not in joined, f"腿又回到 urllib 载体（{dead}）——v1.8.3 三次红的就是它"
    assert 'args = ["curl"' in joined, "外呼必须走 curl（同 job 的等待步已证 curl 可用）"


# ============ 三条分支的行为 ============

def test_absent_release_creates_with_sha(tmp_path, monkeypatch):
    calls = [_http(200), _http(201, {"id": 555, "tag_name": "v9.9.9"})]
    code, out, argvs, sleeps = _run_leg(monkeypatch, tmp_path, calls)
    assert code == 0, out
    assert "已创建" in out, out
    assert len(argvs) == 2, f"首次就成不该重试：{len(argvs)} 次"
    assert sleeps == [], "成功路径不得有退避睡眠"
    posted = _body_of(argvs[1])
    assert posted["target_commitish"] == FAKE_SHA, "target_commitish 必须显式指本次提交"
    assert posted["tag_name"] == posted["name"] == "v9.9.9"
    assert "prepared body" in posted["body"], "缺 CHANGELOG 时要回落 prepare 产物，不能发空正文"


def test_existing_release_syncs_by_patch_not_second_post(tmp_path, monkeypatch):
    calls = [_http(200, {"id": 123}), _http(200, {"id": 123})]
    code, out, argvs, _s = _run_leg(monkeypatch, tmp_path, calls)
    assert code == 0, out
    assert "已同步正文" in out, out
    assert "--request" in argvs[1] and "PATCH" in argvs[1], "已存在必须 PATCH（PUT 必 405）"
    patched = _body_of(argvs[1])
    # PATCH 只发 body 会被 Gitee 打回 400「tag_name is missing」，正文还会被清空
    assert patched["tag_name"] == patched["name"] == "v9.9.9"
    assert patched["body"]


def test_existing_release_patch_failure_is_loud(tmp_path, monkeypatch):
    calls = [_http(200, {"id": 123}), _http(400, {"message": "body is missing"})]
    code, out, _a, _s = _run_leg(monkeypatch, tmp_path, calls)
    assert code == 1, f"PATCH 回 400 却被放行：{out}"
    assert "::error" in out and "400" in out, out


# ============ 非 200/404 不吞 + 有界重试 ============

def test_server_error_retries_bounded_then_fails_loud(tmp_path, monkeypatch):
    """v1.8.3 实发形态的等价物：瞬时失败不能一次就打断整条腿，但重试必须有界，
    且最终失败必须响亮（哑红等于没建）。
    """
    calls = [_http(500), _http(502), _http(503)]
    code, out, argvs, sleeps = _run_leg(monkeypatch, tmp_path, calls)
    assert code == 1, out
    assert len(argvs) == 3, f"重试必须有界（3 次上限），实得 {len(argvs)}"
    assert sleeps == [10, 20], f"退避序列应为 10/20，实得 {sleeps}"
    assert "::error" in out and "三次重试仍失败" in out, out


def test_transient_tls_error_then_success_is_visible(tmp_path, monkeypatch):
    """今天那条超时（curl rc=35）的现场：第 1 次失败、第 2 次成 ⇒ 必须重试后继续，
    并把"重试过"这件事打进日志（否则静默自愈=没人知道链路在抖）。
    """
    calls = [_Resp(35, "", "error:1409F10B:SSL routines:ssl3_get_record:wrong version number"),
             _http(200), _http(201, {"id": 9, "tag_name": "v9.9.9"})]
    code, out, argvs, sleeps = _run_leg(monkeypatch, tmp_path, calls)
    assert code == 0, out
    assert "第 1 次失败" in out, out
    assert "已创建" in out and "外呼尝试 3 次" in out, out
    assert len(argvs) == 3 and sleeps == [10], (len(argvs), sleeps)


def test_404_is_not_retried(tmp_path, monkeypatch):
    """404 是确定答案（走创建分支），拿它重试只是白等 30s 退避。"""
    calls = [_http(404), _http(201, {"id": 7, "tag_name": "v9.9.9"})]
    code, out, argvs, sleeps = _run_leg(monkeypatch, tmp_path, calls)
    assert code == 0 and len(argvs) == 2 and sleeps == [], (code, len(argvs), sleeps)


# ============ 凭据纪律 ============

def test_token_never_reaches_the_log(tmp_path, monkeypatch):
    """token 在 URL 查询里，日志只准打去掉查询串的路径——这条不是风格问题：
    CI 日志是公开的，token 进日志＝任何人都能写别人的 Release。
    """
    for script in (
        [_http(200), _http(201, {"id": 1, "tag_name": "v9.9.9"})],
        [_http(500), _http(500), _http(500)],
        [_Resp(35, "", f"curl: (35) handshake timeout for {TOKEN}")],
    ):
        code, out, _a, _s = _run_leg(monkeypatch, tmp_path, script)
        assert TOKEN not in out, f"token 漏进日志：{out[:200]}"
        assert "access_token" not in out, f"URL 查询串漏进日志：{out[:200]}"


# ============ 反向臂：假修必须红 ============

def test_non_retryable_status_blocks_creation(tmp_path, monkeypatch):
    """403 不是瞬时错：既不重试，也**绝不落到创建分支**——守卫吃的是这个行为。"""
    code, out, argvs, _s = _run_leg(monkeypatch, tmp_path,
                                    [_http(403, {"message": "forbidden"})])
    assert code == 1, out
    assert "::error" in out and "HTTP 403" in out, out
    assert len(argvs) == 1, f"4xx 不该重试，实得 {len(argvs)} 次"


def test_gutting_the_status_guard_changes_behavior(tmp_path, monkeypatch):
    """反向臂：把"非 200/404 必须失败"的守卫改成永假（其余原样）⇒ 同一条 403
    会静默走到创建。两条一起才证明守卫是承重的，不是装饰。
    """
    src = _leg_source()
    gutted = src.replace("if code not in (200, 404):", "if False:")
    assert gutted != src, "守卫锚丢失——判据已随形制漂移，本条要跟着更新而不是删掉"
    code, out, argvs, _s = _run_leg(
        monkeypatch, tmp_path,
        [_http(403, {"message": "forbidden"}), _http(201, {"id": 1, "tag_name": "v9.9.9"})],
        src=gutted)
    assert code == 0 and len(argvs) == 2, (code, out[:200])
    assert "已创建" in out, "被阉后确实静默建了 Release（这就是当年那条钉要防的形态）"
