# -*- coding: utf-8 -*-
"""CI 侧跨仓契约钉入口的行为钉 + workflow 结构钉（B3）。

要治的病：110 条跨仓契约钉在 CI 里**从没跑过**——CI 拿不到私有的 hub / 小程序仓，
脚本 `exit 3`、pytest 记 skipped、job 照绿，而"CI 9/9 success"这句话把"没跑"说成了
"跑过并通过"。跳过本身没错（私有仓在公共 runner 上就是不可见），**错的是跳过与执行
成功在同一个绿灯下不可区分**。

判据都走真跑 / 真解析，不做全文 grep（v1.7.51 复审批刚实证过"注释满足断言"的假绿）：
  ① 对端仓不可见 ⇒ 入口退出 0（跳过不该阻断发版）但必须响亮：::warning:: + 条数下限 +
     缺哪个变量，且**不得**打印对账汇总行（那在日志里就是"跑过了"的形态）；
  ② 对端仓可见 ⇒ 真跑对账，并自己核对 PASS 计数 ≥ 下限，脚本被改瘪要 rc!=0；
  ③ ci.yaml 按 YAML 字段路径解析后必须真调用这个入口、且带两个对端仓的只读 deploy key。
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]                 # huijian_mqtt_broker/
REPO = ROOT.parent                                        # 加载项仓根
ENTRY = ROOT / "tests" / "e2e" / "ci_contract_pins.sh"
CI = REPO / ".github" / "workflows" / "ci.yaml"
HUB_REPO = os.environ.get("HUB_REPO", r"E:\AI\huijian-cloud-hub")
MP_REPO = os.environ.get("MINIPROGRAM_REPO", r"E:\AI\ha-yy\weichat-huijian-hz")
FLOOR = 110          # 与 cross_repo_contract.sh 的自证下限同源；改条数必须两边一起改


def _run(env_extra, timeout=300):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(["bash", str(ENTRY)], capture_output=True, timeout=timeout, env=env)


def _text(r):
    return (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")


def _yaml_or_skip():
    try:
        import yaml
    except ImportError:
        pytest.skip("无 pyyaml，无法按字段解析 workflow")
    return yaml


def test_entrypoint_exists_and_delegates_to_the_real_pins():
    assert ENTRY.exists(), "CI 契约钉入口不存在（B3 未落地）"
    src = ENTRY.read_text(encoding="utf-8")
    assert src.startswith("#!"), "入口必须是可执行脚本（CI 用 bash 调它）"
    # 审计 2026-09-30 H-4/A5 改钉：旧写法 `"cross_repo_contract.sh" in src` 会被
    # 本脚本**第 4 行头注释**单独满足——真调用在 :38，完全不在判据射程内。
    # 把 :38 那行 bash 调用整段删掉，这条"必须真调对账脚本"的钉照绿，而 CI 从此
    # 只跑自己那份聚合计数。钉"真调"就必须锚在**非注释行的调用形态**上。
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert re.search(r"\bbash\b[^\n]*cross_repo_contract\.sh", code), \
        "入口没有真调对账脚本（只剩注释提到它的名字也算假绿）"


def test_skip_is_loud_when_peer_repos_invisible(tmp_path):
    bad_hub = str(tmp_path / "nope-hub")
    bad_mp = str(tmp_path / "nope-mp")
    r = _run({"HUB_REPO": bad_hub, "MINIPROGRAM_REPO": bad_mp})
    out = _text(r)
    assert r.returncode == 0, "缺仓时应 0 退出（跳过不是失败），实得 %d：%s" % (r.returncode, out[-300:])
    assert re.search(r"::warning( title=[^:]*)?::", out), \
        "缺仓必须打 GitHub warning 注解，光 print 一行不算响亮：\n%s" % out[-400:]
    assert "未执行" in out, "warning 要说清这批钉没跑，而不是含糊的 skip：\n%s" % out[-400:]
    assert re.search(r"%d\s*条" % FLOOR, out), "要报出条数下限，好核对跳过了多少：\n%s" % out[-400:]
    assert "跨仓契约: " not in out, "跳过路径不许打印对账汇总行——那是「跑过了」的形态"
    assert "HUB_REPO" in out and "MINIPROGRAM_REPO" in out, "要点名缺哪两个变量，否则没人知道怎么补"


def test_floor_is_enforced_when_pins_run():
    hub_ok = (Path(HUB_REPO) / "src" / "server.js").exists()
    mp_ok = (Path(MP_REPO) / "miniprogram" / "utils" / "cloud-gw.js").exists()
    if not (hub_ok and mp_ok):
        pytest.skip("对端仓不可见（本机跑这条要先 export HUB_REPO / MINIPROGRAM_REPO）")
    r = _run({"HUB_REPO": HUB_REPO, "MINIPROGRAM_REPO": MP_REPO})
    out = _text(r)
    assert r.returncode == 0, "对账应通过：\n%s" % out[-600:]
    assert "::warning::" not in out, "仓可见时不许走跳过分支"
    m = re.search(r"跨仓契约: (\d+) passed", out)
    assert m, "没抓到对账汇总行（脚本输出形态变了，本条钉的判据要同步）：\n%s" % out[-300:]
    n = int(m.group(1))
    assert n >= FLOOR, "条数掉到 %d < %d：对账脚本被改瘪了" % (n, FLOOR)
    # 入口自己必须复核过计数，而不是只透传子脚本的 rc
    assert re.search(r"PASS 计数 %d（下限 %d）" % (n, FLOOR), out), \
        "入口要把「我核过条数」打在输出里：\n%s" % out[-300:]


def test_entry_fails_when_pin_count_drops(tmp_path):
    """把对账脚本改瘪（只留几条）⇒ 入口必须 rc!=0。这条才是"下限有用"的证明。"""
    hub_ok = (Path(HUB_REPO) / "src" / "server.js").exists()
    mp_ok = (Path(MP_REPO) / "miniprogram" / "utils" / "cloud-gw.js").exists()
    if not (hub_ok and mp_ok):
        pytest.skip("对端仓不可见")
    fake = tmp_path / "e2e"
    fake.mkdir()
    # 造一个"永远只报 3 条 PASS、rc=0"的空壳脚本，并造齐两个仓的探路文件
    (fake / "cross_repo_contract.sh").write_text(
        'echo "跨仓契约: 3 passed, 0 failed"\nexit 0\n', encoding="utf-8")
    for d in ("hub", "mp"):
        (tmp_path / d / "src").mkdir(parents=True, exist_ok=True) if d == "hub" else \
            (tmp_path / d / "miniprogram" / "utils").mkdir(parents=True, exist_ok=True)
    (tmp_path / "hub" / "src" / "server.js").write_text("// x\n", encoding="utf-8")
    (tmp_path / "mp" / "miniprogram" / "utils" / "cloud-gw.js").write_text("// x\n", encoding="utf-8")
    entry = (fake / "ci_contract_pins.sh")
    entry.write_text(
        ENTRY.read_text(encoding="utf-8").replace(
            ' "$(dirname "$0")/cross_repo_contract.sh"', ' "%s/cross_repo_contract.sh"' % fake),
        encoding="utf-8")
    env_extra = {"HUB_REPO": str(tmp_path / "hub"), "MINIPROGRAM_REPO": str(tmp_path / "mp")}
    env = dict(os.environ)
    env.update(env_extra)
    r = subprocess.run(["bash", str(entry)], capture_output=True, timeout=120, env=env)
    out = _text(r)
    assert r.returncode != 0, "对账被改瘪到 3 条时入口必须判失败，实得 rc=0：\n%s" % out[-400:]
    assert "下限" in out, "失败要说是条数不够，而不是含糊报错：\n%s" % out[-300:]


def test_ci_yaml_wires_the_entrypoint():
    yaml = _yaml_or_skip()
    assert CI.exists(), "找不到 ci.yaml"
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    runs, envs = [], []
    for job in (doc.get("jobs") or {}).values():
        for st in job.get("steps") or []:
            if isinstance(st.get("run"), str):
                runs.append(st["run"])
            if isinstance(st.get("env"), dict):
                envs.extend(str(v) for v in st["env"].values())
    joined = "\n".join(runs)
    assert "ci_contract_pins.sh" in joined, "没有哪个 step 真调用契约钉入口"
    secrets = "\n".join(envs) + joined
    assert "HUB_REPO_DEPLOY_KEY" in secrets and "MINIPROGRAM_REPO_DEPLOY_KEY" in secrets, \
        "两个对端仓的只读 deploy key 没进 workflow（不配它 CI 永远只能跳过）"
    assert re.search(r"git clone", joined), "workflow 里没有任何 clone 对端仓的步骤"


def test_ci_installs_pyyaml_so_the_yaml_pins_cannot_silently_skip():
    """上面两条 workflow 结构钉在缺 pyyaml 时是 pytest.skip —— 而"在 CI 里静默跳过"就是
    B3 要治的那个病本身。所以 pyyaml 必须是**跑 pytest 的那个 job**在**跑 pytest 之前**
    装上的依赖。判据按 job 与 step 顺序取（YAML 已解析）：跨 job 命中不算，pytest 之后
    才装也不算——第一版写成"全文任一 pip 行含 pyyaml"，实测会把别处的安装当成本 job 的。
    """
    yaml = _yaml_or_skip()
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    target = None
    for jname, job in (doc.get("jobs") or {}).items():
        for st in job.get("steps") or []:
            if isinstance(st.get("run"), str) and "pytest huijian_mqtt_broker/tests" in st["run"]:
                target = (jname, job)
                break
        if target:
            break
    assert target, "CI 里找不到跑 pytest 的 job（结构变了，本条判据要同步）"
    jname, job = target
    runs = [st["run"] for st in (job.get("steps") or []) if isinstance(st.get("run"), str)]
    pytest_at = next(i for i, r in enumerate(runs) if "pytest huijian_mqtt_broker/tests" in r)
    before = "\n".join(runs[:pytest_at])
    assert re.search(r"pip install[^\n]*\bpyyaml\b", before, re.I), \
        "job「%s」里 pyyaml 必须在 pytest 之前装，否则 workflow 结构钉在 CI 里走 skip = 又一个假绿" % jname


def test_ci_yaml_skip_path_is_visible():
    """YAML 侧要保证的不是"它也打 warning"（warning 出自入口脚本），而是**这一步不许被吞**：
    没有 continue-on-error、没有 `|| true`、没有 `timeout-minutes: 0` 之类的静音开关。
    判错对象是因为断言对象而错，不是放宽——按原写法去改 YAML 只会得到一个假绿。"""
    yaml = _yaml_or_skip()
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = [st for job in (doc.get("jobs") or {}).values() for st in (job.get("steps") or [])
             if isinstance(st.get("run"), str) and "ci_contract_pins.sh" in st["run"]]
    assert steps, "没有 step 调用契约钉入口"
    for st in steps:
        assert st.get("continue-on-error") is not True, \
            "契约钉那一步不许 continue-on-error（红会被压成绿）：%s" % st["run"][:160]
        assert "|| true" not in st["run"] and "|| exit 0" not in st["run"], \
            "契约钉那一步不许把失败吞成 0：%s" % st["run"][:160]
    # job 级也不许挂 continue-on-error
    for jname, job in (doc.get("jobs") or {}).items():
        if any(isinstance(st.get("run"), str) and "ci_contract_pins.sh" in st["run"]
               for st in (job.get("steps") or [])):
            assert job.get("continue-on-error") is not True, "job %s 挂了 continue-on-error" % jname


def test_ci_pytest_step_gets_peer_repo_paths():
    """H-4 复核：主 pytest 步也必须拿到对端仓路径。

    不注入的话 `test_v1747_cross_repo_contract.py` 在 CI 里必然 skip——那一步
    是唯一在 pytest 内跑跨仓钉的入口，"CI 9/9 success"又会把"没跑"说成"跑过"。
    判据按 job 内**顺序**取：peer-sync（clone 对端仓）必须排在 pytest 之前，
    否则 `steps.peer-sync.outputs.*` 恒为空 ⇒ 注入等于没注入。
    """
    yaml = _yaml_or_skip()
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job_steps, idx, target = None, None, None
    for job in (doc.get("jobs") or {}).values():
        steps = job.get("steps") or []
        for i, st in enumerate(steps):
            if isinstance(st.get("run"), str) \
                    and "pytest huijian_mqtt_broker/tests" in st["run"]:
                target, idx, job_steps = st, i, steps
                break
        if target:
            break
    assert target is not None, "CI 里找不到跑 pytest 的 step（结构变了，本条判据要同步）"
    env = target.get("env") or {}
    assert "HUB_REPO" in env and "MINIPROGRAM_REPO" in env, \
        "pytest 步没注入对端仓路径 ⇒ 跨仓契约钉在 CI 里永远 skip（H-4 回潮）"
    peers = [i for i, st in enumerate(job_steps)
             if isinstance(st.get("run"), str) and "git clone" in st["run"]]
    assert peers and min(peers) < idx, \
        "clone 对端仓的 step 必须排在 pytest 之前（否则注入的路径恒为空）"
