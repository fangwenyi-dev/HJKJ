"""网关发布闸的结构钉：任何"会写产物"的 job 都必须挂 publish 条件。

背景（2026-10-06 迁仓后实测）：本仓一条 main 由网关与语音两个加载项共用，而网关
workflow 的触发是 `push: branches:[main]` 且无 paths 过滤 ⇒ 语音侧每推一次，网关链
就重跑一轮，并把**已发布的版本号**原地重新 build、覆盖 ghcr（实测 v1.8.1 的 index
digest 被换过）。prepare 里那道发布闸用 `publish` 输出把六个写产物的 job 全关掉。

这里钉的是"闸还在、且没漏挂"。判据一律走 YAML 解析，不用 grep 源码字符串——
否则一句注释里写满 `publish == 'true'` 就能把它喂绿。
"""

import re
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yaml"
GATE_IF = "needs.prepare.outputs.publish == 'true'"
# prepare 自身与两个"只读/算参数"的 job 不需要闸：lint 不写产物、
# prepare 是闸的宿主、init 只算 matrix。其余凡 needs 里含 prepare 的一律算"发布型"。
UNGATED_OK = {"lint", "prepare", "init"}


def _doc():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _publishing_jobs(doc):
    return {
        name for name, job in doc["jobs"].items()
        if name not in UNGATED_OK
        and "prepare" in (job.get("needs") or ([job["needs"]] if isinstance(job.get("needs"), str) else []))
    }


def test_every_publishing_job_is_gated():
    """反向判：漏挂闸的新 job 会红，而不是只盯我现在列出的这六个。"""
    doc = _doc()
    jobs = _publishing_jobs(doc)
    assert jobs, "解析不到任何依赖 prepare 的 job——判据失效，先确认 workflow 结构没被改形"
    missing = sorted(j for j in jobs if doc["jobs"][j].get("if") != GATE_IF)
    assert not missing, (
        f"这些会写产物的 job 没挂发布闸（会覆盖已发布镜像/Release）：{missing}"
    )


def test_init_and_lint_stay_ungated():
    """正向半边：闸不许把不写产物的 job 也关掉，否则 lint 形同没跑。"""
    doc = _doc()
    for j in ("lint", "prepare"):
        assert doc["jobs"][j].get("if") is None, f"{j} 不该挂 publish 条件（它跑不跑与发版无关）"
    assert doc["jobs"]["init"].get("if") is None, "init 只算 matrix，挂闸会让 build 永远拿不到 matrix"


def test_prepare_publishes_gate_output():
    doc = _doc()
    assert doc["jobs"]["prepare"]["outputs"].get("publish") == "${{ steps.gate.outputs.publish }}", \
        "prepare 没把闸的结论透出去，六个 job 的 if 全是空比较"


def test_gate_step_semantics_is_skip_not_fail():
    """闸的语义必须是"响亮跳过"，不是 exit 1 拦停。

    语音侧那道闸拦停是对的（那边"版本号没涨就推 main"是人在犯错）；这边触发者是
    对端的正常发版，每推一次红一次会把红变成噪音，反而没人看红。
    """
    doc = _doc()
    steps = doc["jobs"]["prepare"]["steps"]
    gate = [s for s in steps if s.get("id") == "gate"]
    assert len(gate) == 1, f"gate 步数量={len(gate)}（应恰好 1）"
    run = gate[0]["run"]
    assert "exit 1" not in run, "闸里出现 exit 1＝把跳过改成了拦停，语音侧每次 push 都会红一条网关链"
    assert run.count("publish=false") == 1, "写 publish=false 的分支必须唯一，否则某条路径静默漏发"
    assert "::warning title=本轮跳过网关发布::" in run, "跳过必须响亮（::warning::），静默 skip 等于没人知道没发"
    assert "|| true" in run, "ls-remote 必须容错：闸口不得比它守的事更脆（网络抖动会把正常发版也打死）"
    # 降级路径必须是放行而不是跳过——判据：无 REFS 时写的是 publish=true
    degrade = re.search(r"if \[ -z \"\$REFS\" \]; then(.*?)fi", run, re.S)
    assert degrade and "publish=true" in degrade.group(1), \
        "ls-remote 无输出时应降级放行；改成跳过会让网络抖动悄悄吃掉一次真发版"


def test_force_republish_input_declared():
    doc = _doc()
    inputs = doc[True]["workflow_dispatch"].get("inputs") or {}   # YAML 里 `on:` 解析成 True
    assert "force_republish" in inputs, "没有 force_republish 输入＝故意重发已发布版本没有出口"
    assert inputs["force_republish"]["type"] == "boolean"
