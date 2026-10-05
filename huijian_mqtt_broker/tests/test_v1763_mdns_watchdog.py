"""v1.7.63 对抗复核 N-1：mDNS 看门狗退避的整数溢出。

`BACKOFF=$((10 * (1 << (MDNS_RETRY - 1))))` 在 MDNS_RETRY≥61 时按 64 位有符号
回绕成负数/0（本机 bash 实测：61→-6917529027641081856、64→0），`-gt 600` 对
负数不成立 ⇒ 不封顶 ⇒ `sleep 负数` 报错后立刻进下一轮 ⇒ 忙循环刷 stderr
（mDNS 段落在 set +e，子 shell 不继承 errexit，所以不是"看门狗退出"）。
"""
import subprocess
from pathlib import Path

RUNSH = (Path(__file__).resolve().parents[1] / "run.sh").read_text(encoding="utf-8")

GUARD = 'if [ "${MDNS_RETRY}" -ge 7 ]; then'
BARE = 'BACKOFF=$((10 * (1 << (MDNS_RETRY - 1))))'


def test_backoff_expression_runs_in_range():
    """把 run.sh 里那段**逐字抽出真跑**（与生产同一份代码），喂溢出档位：任何
    MDNS_RETRY 下 BACKOFF 都必须落在 [10,600]。"""
    i = RUNSH.index(GUARD)
    # 抽到本 if 的收尾（外层 if 的 fi）——用缩进对齐的行边界取块
    k = RUNSH.index("\n            fi\n", i) + len("\n            fi")
    block = RUNSH[i:k]
    script = ('for MDNS_RETRY in 1 6 7 8 61 64 200; do '
              + block + '; echo "$MDNS_RETRY:$BACKOFF"; done')
    out = subprocess.run(["bash", "-c", script], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    pairs = [ln.split(":") for ln in out.stdout.strip().splitlines()]
    assert len(pairs) == 7
    for retry, backoff in pairs:
        assert 10 <= int(backoff) <= 600, \
            f"retry={retry} ⇒ BACKOFF={backoff}（溢出/未封顶都会出格）"


def test_bare_shift_only_inside_guard():
    """防回退：裸移位表达式只许出现在守卫之后（旧形态是它单独出现且不封顶）。"""
    i = RUNSH.index(GUARD)
    assert RUNSH.index(BARE) > i, "裸移位必须在 -ge 7 守卫之后（先封顶再算）"
