"""看门狗报警前的分类 —— 纯逻辑。

背景：tommy 2026-09-20 连着两次被「大概率有 worker 挂了」叫起来，两次都得手动
跑 registry + 翻日志才能排除，两次都是「活干完了没发 done」。

⚠️ **假警报的代价不是浪费一次诊断，是把真警报变得不可信。**
所以这组测试的重点是：两种情况**必须**产出不一样的文案。
"""
from closecrab.core.fleet_health import (advice, classify_silence, headline,
                                         DEFAULT_STALE_AFTER_SEC)


def test_all_alive_is_dangling_not_dead():
    """全员心跳新鲜 → 这是「没闭环」，不是「挂了」。"""
    v = classify_silence({"tommy": 30.0, "bunny": 12.0})
    assert v.kind == "dangling"
    assert not v.needs_rescue
    assert v.alive == ["bunny", "tommy"]


def test_one_stale_is_dead():
    v = classify_silence({"tommy": 30.0, "hulk": 9999.0})
    assert v.kind == "dead"
    assert v.needs_rescue
    assert v.stale == ["hulk"] and v.alive == ["tommy"]


def test_missing_heartbeat_is_its_own_bucket():
    """⚠️ registry 里查不到**不算挂了**。查不到就是查不到 ——
    报成挂了是在编，而这个报警的全部价值就在于它说的是真的。"""
    v = classify_silence({"newbot": None, "tommy": 10.0})
    assert v.kind == "unknown"
    assert v.unknown == ["newbot"]
    assert v.stale == []


def test_stale_wins_over_unknown():
    """既有不动的又有查不到的 → 按最坏的算，先去救那个不动的。"""
    v = classify_silence({"a": None, "b": 9999.0})
    assert v.kind == "dead"


def test_boundary_is_inclusive():
    """正好卡在阈值上算活着 —— 边界摇摆会让同一个 bot 一会儿死一会儿活。"""
    assert classify_silence({"a": DEFAULT_STALE_AFTER_SEC}).kind == "dangling"
    assert classify_silence({"a": DEFAULT_STALE_AFTER_SEC + 0.1}).kind == "dead"


def test_headlines_are_visibly_different():
    """**这条是整件事的重点。** 两种情况的第一行必须一眼分得开 ——
    长得一样的话，人还是得每次都手动查一遍，等于没修。"""
    ok = headline(classify_silence({"a": 5.0}), 1, 10)
    bad = headline(classify_silence({"a": 9999.0}), 1, 10)
    assert ok != bad
    assert "挂" not in ok, f"没挂的时候不该说挂: {ok}"
    assert "挂" in bad or "不动" in bad, bad


def test_dangling_advice_hands_you_the_command():
    """没闭环那一支要**直接把命令写出来**，别让人再去翻文档 ——
    翻文档的成本正是这个假警报最贵的地方。"""
    v = classify_silence({"tommy": 5.0})
    text = "\n".join(advice(v, ["901519f6"]))
    assert "--phase done" in text
    assert "901519f6" in text
    assert "没有挂" in text


def test_dead_advice_names_who_to_check_first():
    v = classify_silence({"tommy": 5.0, "hulk": 9999.0})
    text = "\n".join(advice(v, ["x"]))
    assert "hulk" in text and "先查" in text
    # 活着的也要列出来，否则人会把三个都查一遍
    assert "tommy" in text


def test_empty_input_does_not_claim_death():
    """一个 worker 都没有时不许报「挂了」。"""
    v = classify_silence({})
    assert v.kind == "dangling" and not v.needs_rescue
