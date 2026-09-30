# -*- coding: utf-8 -*-
"""v1.7.42 守卫：hub 身份生命周期（抹盘后必须自愈）+ 真栈 e2e 不能被静默阉掉。

背景（用户真机第二条日志）：`[bind] 载荷解析: 命中` → `绑定返回: code_invalid`。
线上取证 `GET /healthz` 回 instances:0 / uptime:104s ⇒ hub 刚重启、注册表空
（`HUB_STORE=/data/store.json` 在云托管容器本地盘，每次部署即抹）。此前加载项抱着
死身份无限重连，面板显示的码是当前 hub 从未签发过的 ⇒ 永不自愈。

这里钉的是"三臂 e2e 与判据不漂移"，行为面本体在 test_hub_client.py（401/403 清身份、
5xx 与裸断不清）——本文件不重复断同一件事，只防两向漂移：
  1) e2e 三臂/文件被删或被稀释成摆设 → 红；
  2) e2e 在拿不到 hub 仓时"静默通过"（skip 不响亮＝门禁不存在）→ 红。
"""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "tests" / "e2e"
DRIVER = E2E / "hub_lifecycle_driver.py"
SH = E2E / "hub_lifecycle_e2e.sh"
HARNESS = E2E / "hub_lifecycle_harness.js"
WRAPPER = E2E / "ci_hub_lifecycle.sh"
CI = ROOT.parent / ".github" / "workflows" / "ci.yaml"

SRC = DRIVER.read_text(encoding="utf-8")
SH_SRC = SH.read_text(encoding="utf-8")


def test_files_exist():
    """三件套防删：driver 单独存在没用，起进程的那半被删就跑不起来。"""
    for p in (DRIVER, SH, HARNESS):
        assert p.exists(), "hub 生命周期真栈缺文件: %s" % p.name


def test_three_arms_are_real_checks():
    """防稀释：按 check( 真调用计数，注释里写"三臂"不算数。"""
    arms = ["A 长连建立并拿到 6 位绑定码", "A2 body.openid 不被当作身份",
            "B 保盘重启后 instanceId 未变",
            "C 抹盘重启后插件自愈并重连", "C 新码真能被 /bind 接受",
            # E 臂（v1.7.47 家庭多人绑定）：owner/member 分流、成员码不作废 owner 码、
            # 鉴权放宽只到 member、踢人立即生效、上限 8、owner 不能退自己
            "E1 owner 码绑定回 role=owner", "E2 签发成员码不作废 owner 码",
            "E3 第二个微信号用成员码绑定", "E4 member 发控制命令鉴权通过",
            "E5 /agent/members 用实例凭据可列成员", "E6 按 mid 踢人成功",
            "E7 前 8 人加入成功、第 9 人 409 members_full",
            "E8 owner 不能退自己"]
    for a in arms:
        assert ('"%s"' % a) in SRC or ("%s" % a) in SRC, "缺臂: %s" % a
    # 37 = 36 个 check( 调用点 + 1 处 def check(。钉的是"臂被砍"，所以取精确下限。
    assert SRC.count("check(") >= 37, "真栈断言只剩 %d 条，疑似被砍" % SRC.count("check(")


def test_cmd_arm_does_not_pass_on_forbidden():
    """/cmd 臂只判 err!=offline 会被 403 forbidden 蒙过（身份没自愈时正是 forbidden）。
    首轮实发踩过这条——断言必须要求"回执真来自长连那侧"。"""
    body = SRC[SRC.index('"/cmd"'):]
    seg = body[:body.index("check(") + 260]
    assert "control_unavailable" in seg, "/cmd 臂退回判 err!=offline＝假绿"


def test_wipe_is_genuine_store_deletion():
    """抹盘必须真删 storeFile——只重启进程＝只测了 B 臂，C 臂会伪装成通过。"""
    assert "STORE.unlink()" in SRC, "没真删注册表文件＝C 臂测的不是线上形态"
    assert "wipe=True" in SRC


def test_skip_is_loud_not_silent():
    """CI 拿不到私有 hub 仓 ⇒ 必须以非 0（3）退出并打印 SKIP，绝不能 exit 0。"""
    assert "sys.exit(3)" in SRC and 'SKIP' in SRC, "driver 的 skip 不响亮"
    assert "exit 3" in SH_SRC, "shell 包装的 skip 不响亮"


def test_skip_path_actually_exits_nonzero():
    """把上一条的叙述变成真执行：不带 HUB_REPO 跑 shell，必须非 0 且话里带 SKIP。"""
    env = dict(os.environ, HUB_REPO="", PYTHONIOENCODING="utf-8")
    r = subprocess.run(["bash", str(SH)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    out = r.stdout + r.stderr
    assert r.returncode == 3, "无 hub 仓时 rc=%s（0＝静默放行）: %s" % (r.returncode, out[:200])
    assert "SKIP" in out, "跳过时没留下可见痕迹"


def test_harness_uses_the_real_hub_source():
    """起的是 hub 仓的 createHub，不是本仓复制的协议影子（复制一份就是一份会漂移的契约）。"""
    js = HARNESS.read_text(encoding="utf-8")
    assert "require(path.join(repo, 'src', 'server.js'))" in js, "harness 不再引用真 hub 源码"
    assert "createHub(" in js


# ---- 审计 2026-09-30 H-4 复核：真栈要在 CI 里被调用，且跳过/真跑两条分支可辨 ----


def test_ci_runs_the_hub_lifecycle_stack():
    """H-4 复核：此前 hub_lifecycle_e2e.sh 无任何 CI step——35 条真栈断言
    "本地全跑、CI 一条不跑"，本组钉在 CI 里等于不存在。

    判据按 YAML 字段路径解析（同 test_v1753 的反假绿口径）：step 的 run 里
    必须真出现入口调用；缺 pyyaml 时响亮 skip（CI 里 pyyaml 有专钉保证先装）。
    """
    try:
        import yaml
    except ImportError:
        import pytest
        pytest.skip("无 pyyaml，无法按字段解析 workflow（CI 侧有专钉保证它先装）")
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = [st for job in (doc.get("jobs") or {}).values()
             for st in (job.get("steps") or [])
             if isinstance(st.get("run"), str) and "ci_hub_lifecycle.sh" in st["run"]]
    assert steps, "没有哪个 step 真调用 hub 生命周期入口（H-4 回潮）"
    for st in steps:
        assert st.get("continue-on-error") is not True, \
            "真栈 step 不许 continue-on-error（红会被压成绿）"
        assert "|| true" not in st["run"] and "|| exit 0" not in st["run"], \
            "真栈 step 不许把失败吞成 0"
        env = st.get("env") or {}
        assert any("HUB_REPO" in str(k) + str(v) for k, v in env.items()), \
            "入口必须拿到 HUB_REPO（否则永远走跳过分支＝又一个假绿）"


def test_ci_wrapper_skip_is_loud_and_nonblocking():
    """行为钉：对端仓不可见 ⇒ exit 0 但必须响亮（::warning + 条数 + 变量名），
    且**不得**打印真栈汇总行——那在日志里就是"跑过了"的形态。"""
    env = dict(os.environ, HUB_REPO="", PYTHONIOENCODING="utf-8")
    r = subprocess.run(["bash", str(WRAPPER)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 0, \
        "缺仓时入口必须 exit 0（跳过不阻断发版），实得 %s：%s" % (r.returncode, out[-300:])
    assert "::warning" in out and "未执行" in out, "跳过必须响亮：%s" % out[-300:]
    assert "HUB_REPO" in out, "要点名缺哪个变量，否则没人知道怎么补"
    assert not re.search(r"真栈: \d+ passed", out), \
        "跳过路径不许打印真栈汇总行（那是「跑过了」的形态）"


def test_ci_wrapper_real_run_branch_is_faithful():
    """结构钉（可见分支）：真跑 + rc 透传 + PASS 计数下限——缺一即假绿。"""
    code = "\n".join(l for l in WRAPPER.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("#"))
    assert re.search(r"\bbash\b[^\n]*hub_lifecycle_e2e\.sh", code), \
        "入口没有真调真栈脚本（只剩注释提到名字也算假绿）"
    assert re.search(r"\bexit\s+\"?\$rc\"?", code), "真跑失败必须透传 rc（不许吞）"
    assert "FLOOR" in code and "-lt" in code, \
        "缺 PASS 计数下限自检（驱程被砍臂会静默绿）"
