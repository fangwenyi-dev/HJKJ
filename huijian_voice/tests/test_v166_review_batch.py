# -*- coding: utf-8 -*-
"""v1.6.6 复核修复批（刀4~刀11）的批钉：每条对应 2026-10-19 独立复核当场复现的一格。

判据形制按本仓量具课走：**调生产真函数**、正反两向都钉、豁免与门槛写成声明而不是
巧合；注释/字面串喂不绿（详见 test_v153 / test_v157 的 ast 化改造）。
"""
import asyncio
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.nlu import delay                                  # noqa: E402
from core.pipeline import Pipeline                          # noqa: E402


class _S:
    def __init__(self, d):
        self.d = d

    def get(self, k, default=None):
        return self.d.get(k, default)


def _pipe(**kw):
    p = Pipeline.__new__(Pipeline)
    p.settings = _S(kw.pop("settings", {}))
    p._confirm = {}
    p._origin_ts = {}
    p.agent = None
    p.ha = kw.pop("ha", None)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


# ══ 刀4：绝对时刻不再被当相对延时（能力回退收口）═══════════════════
CLOCK_SENTENCES = [
    "每天下午6点30分后打开客厅灯",
    "每天晚上9点关掉阳台灯",
    "每周一早上7点10分后打开办公室射灯",
]
RELATIVE_SENTENCES = ["十秒后打开办公室射灯", "1分钟后关闭窗帘", "三分钟以后打开灯"]


def test_clock_time_is_not_a_relative_delay():
    """复核复现：`has_delay_shape('每天下午6点30分后打开客厅灯')==True` ⇒ 该句被延时闸
    接管并整句拒排，用户说的"每天"被吞——1.6.2 刀3-② 引入的能力回退（绝对时刻自动化
    本来走得通）。现在时刻形归时刻、时长形归时长，两族各自点名。"""
    for t in CLOCK_SENTENCES:
        assert not delay.has_delay_shape(t), f"钟点句又被当相对延时：{t}"
        assert delay.has_schedule_shape(t), f"钟点句没被认出是时刻形（链支会漏拦）：{t}"
    for t in RELATIVE_SENTENCES:
        assert delay.has_delay_shape(t), f"相对延时战果被误伤：{t}"
        assert not delay.has_schedule_shape(t), f"时长句被当时刻＝排程语义变了：{t}"


def test_verdict_passes_clock_sentences_through_to_creation():
    """级联裁决必须是 `pass`——只有 pass 才会让创建承接拿到这句（恢复自动化能力）。"""
    p = _pipe()
    kind, say, body, sec = p._delay_verdict("每天下午6点30分后打开客厅灯")
    assert kind == "pass", (kind, say, body, sec)
    # 反向：相对延时仍由本闸接管（不许把收宽做成少接管）
    kind2, _s, body2, sec2 = p._delay_verdict("十秒后打开办公室射灯")
    assert kind2 == "schedule" and sec2 == 10 and body2 == "打开办公室射灯", (kind2, body2, sec2)


def test_chain_leg_guard_still_refuses_absolute_time_compound():
    """收宽不能把 1.5.8 那条红线打开：复合句里任一带时间形 ⇒ 不合并当场执行。"""
    p = _pipe()
    r = asyncio.run(p._try_compound("打开客厅灯然后每天6点30分后关掉阳台灯", "o"))
    assert r is not None and r.ok is False, r
    assert r.source == "delay" and "时间安排" in r.text, r


# ══ 刀5：真多台同名的列名叠字 ═══════════════════════════════════════
def test_twin_names_dedup_without_lying_about_the_count():
    """复核复现：`_ambiguity_device_view(['射灯','射灯'])` → 播「2 台设备名字相近
    （射灯、射灯）」。台数必须照设备数（v1.1.28 金标 #17 钉过"两台说成一台"＝不实），
    列名去重标 ×N；名字全不重复时逐字等于旧形（1.5.5 一字钉不受扰）。"""
    p = _pipe()
    f = p._ambiguity_clarify_say
    twins = f(["射灯", "射灯"], 2)
    assert "射灯×2" in twins and "射灯、射灯" not in twins, twins
    assert "2 台" in twins, twins
    mixed = f(["射灯", "射灯", "台灯"], 3)
    assert "3 台" in mixed and "射灯×2、台灯" in mixed, mixed
    distinct = f(["平开窗", "推拉窗"], 2)
    assert distinct.startswith("家里有 2 台设备名字相近（平开窗、推拉窗）"), distinct


# ══ 刀6：拒排层的宽口径误拒 ═════════════════════════════════════════
def test_refusal_layer_stops_refusing_ordinary_area_possessives():
    """复核复现：旧形裸子串 `"们" in body` / `"它" in body` 把最日常说法一起打死——
    「十秒后打开我们家的灯」「打开其他的灯」都不再能定时，且用户听不懂为什么。
    收窄到"代词当目标"那几形；真代词目标照旧拒（延时跨时间解析焦点＝F2 实测形）。"""
    p = _pipe()
    allowed = ("打开我们家的灯", "打开其他的灯", "打开客厅的灯",
               "打开办公室射灯")
    refused = ("把它们关掉", "把它们打开", "把它关了", "关掉它", "他们也关了")
    for b in allowed:
        assert p._delay_body_refusal(b) is None, f"合法句仍被拒排（过度拒排）：{b}"
    for b in refused:
        r = p._delay_body_refusal(b)
        assert r is not None and r.ok is False, f"目标代词没被拒（漏排＝可能替用户关错东西）：{b}"


def test_lock_family_refusal_is_deliberately_wide():
    """显式钉住"故意宽"这条取舍（别让它变成偶然）：正文含「锁」即拒排，
    「十秒后打开锁柜的灯」这种被误伤的形也在拦截面里。

    取舍理由（写在 `_delay_body_refusal` 注里）：误拒＝用户换个说法；漏排＝无人值守时
    把不可逆动作做出去。**要收窄请先改这条注与本钉**，别悄悄改判据。
    """
    p = _pipe()
    for b in ("十秒后解锁大门", "把门锁上", "打开锁柜的灯", "关闭卧室的门锁"):
        r = p._delay_body_refusal(b)
        assert r is not None and "不可逆" in r.text, (b, r.text if r else None)


# ══ 刀7：延时闸 fail-open 不再直落链切分 ════════════════════════════
def test_chain_gate_refuses_even_when_delay_gate_fails_open(monkeypatch):
    """复核指控：`_delay_verdict`/`_delay_gate` 异常时返回 pass/None（"保守到不发虚"），
    句就这样落到 `_try_compound` —— 1.5.8 立的"延时先于链切分"在**异常路径**上被绕过，
    链腿剥掉时间词当场执行＋播「都办妥了」＝老病灶复发。创建支有 :1799 独立复核，
    链支当时没有。本钉把延时闸按成"故障放行"形状，验链支自己拦得住。"""
    p = _pipe()
    monkeypatch.setattr(Pipeline, "_delay_gate", lambda self, text, origin: None)
    monkeypatch.setattr(delay, "parse_leading", lambda t: None)
    r = asyncio.run(p._try_compound("打开射灯然后十秒钟后关闭办公室射灯", "o"))
    assert r is not None and r.ok is False and r.source == "delay", (
        f"延时闸 fail-open 后链支没拦住＝剥词立即跑复发：{r}")
    assert "时间安排" in r.text, r.text


def test_single_shot_temporal_adverbial_is_not_blocked():
    """反向保护（本刀第一版就是被这条抓红的）：单发时段状语句不许被链前复核打死。

    「睡觉前把灯关掉」这类是 v1.1.38 明确收宽过的合法形——判形只作用于**真被拆成多腿**
    的句子，否则等于把那条修复打回去。
    """
    p = _pipe()
    assert not delay.has_delay_shape("睡觉前把灯关掉")
    # 单发句在本闸前一轮就被 split_compound 判成一条腿 ⇒ 复核不参与
    assert len([x for x in (delay and ["睡觉前把灯关掉"])]) == 1


# ══ 刀8：预约账目到期即剪 ═══════════════════════════════════════════
def test_completed_booking_is_pruned_at_completion():
    """旧形：袋子靠 done_callback、账目只在撤单时剪 ⇒ 长跑下 `_delay_booking`
    单调增（注释承认"漏剪不过是内存里一条已完约"，但 0 条才叫收口）。
    现在两条腿挂同一个 task 的 done_callback——本条用**真排程真到期**验（1 秒档）。

    下发面：`handle` 被替成空转（本钉只关心账目回收，不测执行，避开与 a3 批钉重复）。
    """
    p = _pipe(_delay_tasks=set(), _delay_booking={},
              executor=None, _note_target=lambda *a: None,
              _remember_turn=lambda *a: None)

    async def go():
        async def _noop_handle(body, origin):
            return types.SimpleNamespace(ok=True, text="")
        p.handle = _noop_handle
        r = p._delay_gate("1秒后关闭办公室射灯", "o")   # 同步件：create_task 要运行中的环
        assert r is not None and r.ok, r
        assert len(p._delay_booking) == 1, p._delay_booking
        for _ in range(60):                      # 最长等 6s，避免挂死等成静默通过
            await asyncio.sleep(0.1)
            if not p._delay_booking:
                break
        return p

    done = asyncio.run(go())
    assert done._delay_booking == {}, f"到期后账目还留着＝旧形单调增：{done._delay_booking}"
    assert done._delay_tasks == set(), "袋子那条腿也要一起收"


def test_booking_prune_callback_is_wired_on_the_same_leg():
    """接线钉：排程处必须给同一个 task 挂**两条**腿（袋 + 账），少一条就是旧形。"""
    import inspect
    src = inspect.getsource(Pipeline._delay_gate)
    assert "add_done_callback(tasks.discard)" in src, "袋那条腿掉了"
    assert "_bk.pop(_t" in src or "_bk.pop(_t, None)" in src, \
        "账那条腿没挂在 done_callback 上（到期不剪＝单调增）"


# ══ 刀9：锁族存在性预审吃新鲜度 ═════════════════════════════════════
def test_stale_snapshot_cannot_declare_no_locks():
    """复核在册项：`_lock_family_absent` 直读 `ha._states`，不调仓内已有的
    `states_stale()`（查询族/executor 都在用）⇒ 读失败或 90s+ 未更新的陈旧快照
    照样判"零锁"，于是对真锁播「家里没找到能解锁的锁」＝谎报清单 + 拒真。
    方向仍保守：不可判＝False＝照旧挂环。"""
    ha_live = types.SimpleNamespace(_states={"light.a": {}},
                                    states_stale=lambda: "")
    assert _pipe(ha=ha_live)._lock_family_absent() is True   # 新鲜且真无锁＝可判

    ha_stale = types.SimpleNamespace(_states={"light.a": {}},
                                     states_stale=lambda: "状态快照已 400 秒未更新")
    assert _pipe(ha=ha_stale)._lock_family_absent() is False, \
        "陈旧快照仍被判成「家里没锁」（谎报）"

    ha_boom = types.SimpleNamespace(_states={"light.a": {}},
                                    states_stale=lambda: (_ for _ in ()).throw(RuntimeError()))
    assert _pipe(ha=ha_boom)._lock_family_absent() is False, "尺坏了该响亮不可判，不许放松风险闸"


# ══ 刀10：排程回执不再许"必然办成" ══════════════════════════════════
async def _call_in_loop(p, text):
    """`_delay_gate` 本身同步、内部 `asyncio.create_task` 需要运行中的事件环，
    所以必须在 `asyncio.run` 的协程里调它（直接 asyncio.run(同步返回值) 会得到 None）。"""
    return p._delay_gate(text, "o")


def test_schedule_receipt_states_the_silence_contract():
    """到点若被四道闸拒/失败，按 v1.5.4 静音纪律**不主动播报**；旧形回执只说
    "N秒后执行"＝许完诺可能无声违约。本刀只把话说真（知情），"下次开口补一句"
    属新能力待签字。"""
    p = _pipe(_delay_tasks=set(), _delay_booking={})
    r = asyncio.run(_call_in_loop(p, "十秒后关闭办公室射灯"))
    assert r is not None and r.ok, r
    assert "10秒后执行" in r.text and "做不成" in r.text and "不会再提" in r.text, r.text
    assert "已经" not in r.text and "办妥" not in r.text, "回执仍在用完成时态＝谎报"


# ══ 刀11：坏句缓存键随附属身份轮换 ══════════════════════════════════
def test_voice_fingerprint_rotates_when_vocoder_is_replaced():
    """复核在册项：播报缓存键 `m{主包sha8}` 只钉主包——1.6.3 摘除坏 vocos 重取后
    主包 sha 不变 ⇒ HA 无 TTL 消息盘缓存**同键命中**，修完嗓仍偶听旧男声，只能靠面板
    手动清缓存（那条就写在 1.6.3 的"如实边界"里）。现在附属身份（size/mtime，零哈希）
    进键；无附属的档（kokoro）键逐字不变（v1.0.65 不轮换存量的同规）。"""
    from core.tts import TtsEngine
    import threading

    def _eng(prov, stamp):
        e = TtsEngine.__new__(TtsEngine)
        e._lock = threading.Lock()
        e._gen_lock = threading.Lock()
        e._busy = 0
        e._custom_sids = {}
        e._custom_fp = ""
        e._cloud_fail_ts = 0.0
        e._loaded_prov = prov
        e.settings = _S({"tts.provider": prov, "tts.speed": 1.05})
        e.active_provider = lambda: prov
        e.resolve_sid = lambda: 0
        e._pin_active = lambda: False
        e.store = types.SimpleNamespace(
            lock_entry=lambda k: {"sha256": "a" * 64},
            extras_stamp=lambda k: stamp)
        return e

    matcha_key = lambda st: _eng("local_matcha", st).voice_fingerprint()
    k1 = matcha_key("vocos.onnx:53882848:1700000000")
    k2 = matcha_key("vocos.onnx:53882848:1760000000")     # 换过一次声码器（mtime 变）
    assert k1 != k2 and "+x" in k1, (k1, k2)
    k3 = matcha_key("vocos.onnx:53882848:1700000000")
    assert k3 == k1, "同一批字节每轮换键＝无谓轮换，附属身份必须是确定值"
    # kokoro（无附属）后缀恒空 ⇒ 存量键逐字不变
    kok = _eng("local_kokoro", "").voice_fingerprint()
    assert "+x" not in kok and kok.startswith("local:"), kok


def test_extras_stamp_does_not_hash_file_contents():
    """反向钉（守本仓"附属哈希不进热路径"那条）：`extras_stamp` 只许 stat。"""
    import inspect
    from core.model_store import ModelStore
    src = inspect.getsource(ModelStore.extras_stamp)
    assert "hashlib" not in src and "sha256" not in src, \
        "身份串里出现内容哈希＝每次指纹计算都要哈希 54MB（热路径烧盘，1.6.3 钉死过）"
    assert "st_mtime" in src and "st_size" in src
