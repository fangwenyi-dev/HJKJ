#!/usr/bin/env python3
"""单文件隔离体检（仓库工具，不进镜像，无第三方依赖）。

背景（v1.6.8 之后自查实锤）：`test_v1210_area_bulk.py` 有 6 条集成侧钉**单跑必红**、
全量 3332 却绿——绿色来自 `test_audit4_fixes` 早一步装进 `sys.modules` 的 HA 权威替身，
不是这条链自带。这类"靠隔壁文件漏下来的全局态才绿"的钉，CI 永远看不见过期：
全量按字母序跑，谁先谁后一换就集体红，而红相是"实现坏了"。

用法：

    python huijian_voice/tools/test_isolation_sweep.py            # 全部 test_*.py
    python huijian_voice/tools/test_isolation_sweep.py v1210 v1125  # 只跑名字含这些片段的

判读：`RED` 的行末就是该文件单独跑的末行。有 RED 时整体 exit 1。
耗时：约 200 个文件 × 数秒 = 十几到几十分钟（串行；并行会让计时型钉互相干扰，
"同一轮别跑两套计时测量"是这仓的台架纪律）。

Windows 需要 opus 真库：`HUIJIAN_OPUS_DLL_DIR=<含 opus.dll 的目录>` 会原样透传给子进程
（仓旁 `_winlibs/` 在位时 conftest 自己会找到，不用设）。
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                      # 仓库根（pytest 的 cwd，与 CI 同口径）
TESTS = ROOT / "huijian_voice" / "tests"


def main(argv):
    files = sorted(TESTS.glob("test_*.py"))
    if argv:
        files = [f for f in files if any(a in f.name for a in argv)]
    if not files:
        print("没有匹配的测试文件——参数写错了？", file=sys.stderr)
        return 2

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    reds = []
    for f in files:
        r = subprocess.run([sys.executable, "-m", "pytest",
                            str(f.relative_to(ROOT)), "-q", "--no-header",
                            "-p", "no:cacheprovider"],
                           cwd=str(ROOT), capture_output=True, env=env)
        text = (r.stdout + r.stderr).decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines()
                 if " passed" in ln or " failed" in ln or "error" in ln.lower()]
        verdict = lines[-1] if lines else "(没有末行计数＝跑错了目录或被中断，不算验证)"
        print(f"{'RED ' if r.returncode else 'OK  '}{f.name:54s} | {verdict}")
        if r.returncode:
            reds.append(f.name)
    print(f"\n单跑体检：{len(files)} 个文件 / 红 {len(reds)} 个")
    for n in reds:
        print("  RED", n)
    return 1 if reds else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
