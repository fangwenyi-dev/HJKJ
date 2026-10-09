"""69 条 ASR 热词纠错表钉桩（口径裁定：061701 代码版=严格超集，58 条为移植基线；
2026-09-16 现场增补「平×窗」插音族 3 条；2026-09 开窗器名称优化增补 开窗器/开合器
近音族 5 条；2026-09-27 现场日志（09-14）增补 平盖窗/平改窗 2 条；2026-09-30 现场
日志（展厅「催拉窗」）增补 催拉窗 1 条——根治另见 targets._generic_rescue 音节级
近音救援，计数以本钉为准。
2026-10-09 第七轮审计 A7 **删表 6 条**（计数 70→64）：`我室→卧室`（代词开头＝属格
"我这间"，折成在册区域是凭空猜房间）、`管灯→关灯`/`开登→开灯`/`大看→打开`/
`关币→关闭`/`赞停→暂停`（值是控制动词＝改的不是"哪台设备"而是"这句要做什么"，
实测「把管灯打开」被折成"关灯"= 要开却下发关，「打开登录页面」被折出
TurnDeviceOn name=灯）。同批在 `corrector` 落两道**禁入族闸**（代词开头的键、
值是动作词的键一律不改写），用户扩展表 corrections_extra 同受此闸保护——所以
条数钉的意义是"这张表不再含这两族"，而不只是一个数字。"""
from core.nlu import corrector


def test_table_has_64_entries():
    assert len(corrector.BASE_CORRECTIONS) == 64


def test_pronoun_and_action_value_keys_are_gone():
    """A7 两族禁入：表内不再有代词开头的键，也不再有"值=控制动词"的键。

    反向钉（断"不存在"）+ 正向闸（断 extra 也被拦）两条一起才叫钉住：只删表不闸，
    下一个人从现场工单里再补一条 `XX→关灯` 就回到同一个事故。
    """
    assert not [w for w in corrector.BASE_CORRECTIONS
                if w[:1] in corrector._PRONOUN_HEADS], "代词开头的键回到了表里"
    assert not [w for w, v in corrector.BASE_CORRECTIONS.items()
                if v in corrector._ACTION_VALUES], "值=控制动词的键回到了表里"
    # 真听岔的救济必须还在（删键删的是族，不是整张表）
    for wrong, right in [("沃室", "卧室"), ("条案", "调暗"), ("条亮", "调亮"),
                         ("开创器", "开窗器"), ("催拉窗", "推拉窗")]:
        assert corrector.BASE_CORRECTIONS[wrong] == right, wrong


def test_a7_no_polarity_flip_and_no_room_guess():
    """四条现场形态逐字钉：不改写、不反极性、不猜房间。"""
    for raw in ("打开我室的灯", "把管灯打开", "打开登录页面", "帮我室温",
                "我室设备全部打开"):
        assert corrector.apply(raw) == raw, raw
    # extra 想加回这两族 ⇒ 闸拦住，不改写（护栏面向未来，不是面向历史）
    assert corrector.apply("把管灯打开", {"管灯": "关灯"}) == "把管灯打开"
    assert corrector.apply("打开我室的灯", {"我室": "卧室"}) == "打开我室的灯"
    # 正常 extra 照旧生效（闸不是把整张 extra 关掉）
    assert corrector.apply("打开沃室的灯", {"登带": "灯带"}) == "打开卧室的灯"


def test_pingchuang_window_char_replaced_entirely():
    """2026-09-27 办公实锤：连"窗"都被听成"商"，泛称字不在表内 ⇒ 救援够不到，只能入表。"""
    raw = "关闭办公室平台商打开办公室射灯"
    assert corrector.apply(raw) == "关闭办公室平开窗打开办公室射灯"
    # 与既有 平×窗 族互不截胡（长键优先，同长不重叠）
    assert corrector.apply("关闭办公室平台窗") == "关闭办公室平开窗"
    assert corrector.apply("关闭办公室平盖窗") == "关闭办公室平开窗"
    # 已知代价（与「平台窗」同口径：表内注释已声明，出口是 corrections_extra）：
    # 三字整词键会命中"平台商城"这类正常词。控窗语音域里不会出现该词；
    # 本断言是把取舍钉在案上，不是藏起来。
    assert corrector.apply("平台商城打开") == "平开窗城打开"


def test_opener_near_sound_pairs():
    """开窗器/开合器近音对（用户令优化第①项配套）。"""
    for wrong, right in [("开创器", "开窗器"), ("开窗气", "开窗器"),
                         ("开床器", "开窗器"), ("开和器", "开合器"),
                         ("开合气", "开合器")]:
        assert corrector.BASE_CORRECTIONS[wrong] == right
    # 不伤真词：近音键是三字整词，普通句不误触
    assert corrector.apply("打开开创的设备") == "打开开创的设备"


def test_core_pairs():
    for wrong, right in [("内导", "内倒"), ("站厅", "展厅"), ("统灯", "筒灯"),
                         ("办公系统", "办公室"), ("办公事", "办公室"), ("平推车", "平推窗"),
                         ("放量大一点", "风量大一点"), ("汇江", "慧尖"),
                         ("平台窗", "平开窗"), ("平抬窗", "平开窗"), ("平胎窗", "平开窗")]:
        assert corrector.BASE_CORRECTIONS[wrong] == right


def test_pingchuang_insertion_realworld():
    """现场日志实锤句（2026-09-16）：复合句里的「平台窗」必须纠回「平开窗」。"""
    raw = "关闭办公室空调打开办公室平台窗"
    out = corrector.apply(raw)
    assert out == "关闭办公室空调打开办公室平开窗"
    # 不伤及邻词：真·平推窗句不被新键误触
    assert corrector.apply("把平推窗关上") == "把平推窗关上"
    assert corrector.apply("羊台的窗") == "阳台的窗"


def test_apply_longest_first():
    # 「办公系统」必须先于短键生效（原 dict 序隐患的钉桩）
    assert corrector.apply("打开办公系统的灯") == "打开办公室的灯"
    assert corrector.apply("把站厅的统灯打开") == "把展厅的筒灯打开"
    # 二次套用不回退
    assert corrector.apply(corrector.apply("内导展厅窗")) == "内倒展厅窗"


def test_apply_extra_merges_without_override():
    out = corrector.apply("把智能面板打开", extra={"智能面板": "场景面板"})
    assert "场景面板" in out
    # 用户表同名键不覆盖基础表（基础表经回归验证）
    out2 = corrector.apply("内导", extra={"内导": "歪斜"})
    assert out2 == "内倒"
