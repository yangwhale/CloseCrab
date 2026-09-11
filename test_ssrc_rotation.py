"""SSRC 换号清理补丁的单测。

为什么值得单测：这段代码的**危险面比收益面大**。清理做错了不是「少清一点」，
是每说一句话就把自己的解码器掐掉一次 —— 症状比原来的换号残留更严重。所以
下面每个「该清」的用例都配了一个「不该清」的反例。

**测的是真补丁，不是复刻。** 第一版这里自己抄了一遍「old is not None and
old != ssrc」的判据，那等于测试自己给自己打分 —— 改坏 sidecar 里那份，测试
照样全绿。现在改成真的调 `_install_ssrc_rotation_patch()` 去打 discord 的
`VoiceClient`，再拿替身对象走它的未绑定方法。
"""
from __future__ import annotations

import pytest

from discord.voice import VoiceClient

import closecrab.voice.discord_voice_sidecar as sc


class _Router:
    def __init__(self, boom: bool = False):
        self.destroyed: list[int] = []
        self.boom = boom

        self.assigned: list[tuple[int, int]] = []

    def destroy_decoder(self, ssrc: int) -> None:
        if self.boom:
            raise RuntimeError("decoder already gone")
        self.destroyed.append(ssrc)

    # 原版 _add_ssrc 收尾会调它，替身少一个方法就是 AttributeError
    def set_user_id(self, ssrc: int, user_id: int) -> None:
        self.assigned.append((ssrc, user_id))


class _Timer:
    def __init__(self, boom: bool = False):
        self.dropped: list[int] = []
        self.boom = boom

    def drop_ssrc(self, ssrc: int) -> None:
        if self.boom:
            raise RuntimeError("no such ssrc")
        self.dropped.append(ssrc)


class _Reader:
    def __init__(self, boom_router=False, boom_timer=False):
        self.packet_router = _Router(boom_router)
        self.speaking_timer = _Timer(boom_timer)


class _VC:
    """够用的 VoiceClient 替身：`_add_ssrc` 只碰这三个属性。

    不是 VoiceClient 的子类 —— 真造一个要 gateway/socket/guild 一整套。方法用
    未绑定的形式调 (`VoiceClient._add_ssrc(vc, ...)`)，Python 不检查 self 类型。
    """

    def __init__(self, reader=None):
        self._id_to_ssrc: dict[int, int] = {}
        self._ssrc_to_id: dict[int, int] = {}
        self._reader = reader


@pytest.fixture(scope="module", autouse=True)
def _patched():
    """把真补丁打到真 VoiceClient 上，跑完还原。"""
    orig = VoiceClient._add_ssrc
    sc._ssrc_rotate_patched = False
    if hasattr(VoiceClient, "_cc_ssrc_rotate"):
        del VoiceClient._cc_ssrc_rotate
    sc._install_ssrc_rotation_patch()
    assert VoiceClient._add_ssrc is not orig, "补丁没挂上，下面的测试全是空转"
    yield
    VoiceClient._add_ssrc = orig
    sc._ssrc_rotate_patched = False


def _add_ssrc_rotating(vc: _VC, user_id: int, ssrc: int) -> None:
    """走真补丁。"""
    VoiceClient._add_ssrc(vc, user_id, ssrc)


def _seed(vc: _VC, user_id: int, ssrc: int) -> None:
    """直接摆好初始映射，不经过补丁 —— 避免把准备工作也算成一次换号。"""
    vc._ssrc_to_id[ssrc] = user_id
    vc._id_to_ssrc[user_id] = ssrc


@pytest.fixture(autouse=True)
def _reset_counter():
    sc._ssrc_rotations = 0
    yield


# ── 该清的 ────────────────────────────────────────────────────────────────

def test_rotation_destroys_old_decoder():
    r = _Reader()
    vc = _VC(r)
    _seed(vc, 42, 10664)
    _add_ssrc_rotating(vc, 42, 10769)

    assert r.packet_router.destroyed == [10664]
    assert r.speaking_timer.dropped == [10664]


def test_rotation_clears_stale_reverse_entry():
    """py-cord 原版的洞：正向表被覆盖，反向表的旧号永远留着。"""
    vc = _VC(_Reader())
    _seed(vc, 42, 10664)
    _add_ssrc_rotating(vc, 42, 10769)

    assert 10664 not in vc._ssrc_to_id
    assert vc._ssrc_to_id == {10769: 42}
    assert vc._id_to_ssrc == {42: 10769}


def test_rotation_counter_increments():
    vc = _VC(_Reader())
    _seed(vc, 42, 1)
    _add_ssrc_rotating(vc, 42, 2)
    _add_ssrc_rotating(vc, 42, 3)
    assert sc._ssrc_rotations == 2


# ── 不该清的（反例）────────────────────────────────────────────────────────

def test_same_ssrc_reannounced_is_not_a_rotation():
    """每次开口都推一条 speaking，号没变。清了就等于每句话掐一次自己。"""
    r = _Reader()
    vc = _VC(r)
    _seed(vc, 42, 10664)
    for _ in range(5):
        _add_ssrc_rotating(vc, 42, 10664)

    assert r.packet_router.destroyed == []
    assert r.speaking_timer.dropped == []
    assert sc._ssrc_rotations == 0


def test_first_ever_ssrc_is_not_a_rotation():
    r = _Reader()
    vc = _VC(r)
    _add_ssrc_rotating(vc, 42, 10664)

    assert r.packet_router.destroyed == []
    assert sc._ssrc_rotations == 0


def test_other_user_ssrc_is_untouched():
    """两个人各自的号互不影响 —— 别把在场另一个人的解码器顺手清了。"""
    r = _Reader()
    vc = _VC(r)
    _seed(vc, 42, 10664)
    _seed(vc, 99, 10647)
    _add_ssrc_rotating(vc, 42, 10769)

    assert r.packet_router.destroyed == [10664]
    assert vc._ssrc_to_id[10647] == 99


# ── 清理失败不能挡住新号写入 ───────────────────────────────────────────────

def test_reader_absent_does_not_raise():
    vc = _VC(None)
    _seed(vc, 42, 10664)
    _add_ssrc_rotating(vc, 42, 10769)
    assert vc._id_to_ssrc == {42: 10769}


def test_decoder_destroy_failure_still_writes_new_ssrc():
    """清理是尽力而为。销毁抛了也得让新号进表，否则从『卡』变成『全聋』。"""
    r = _Reader(boom_router=True)
    vc = _VC(r)
    _seed(vc, 42, 10664)
    _add_ssrc_rotating(vc, 42, 10769)

    assert vc._id_to_ssrc == {42: 10769}
    assert vc._ssrc_to_id == {10769: 42}
    assert r.speaking_timer.dropped == [10664]   # 前一步炸了，后一步照做


def test_timer_drop_failure_still_writes_new_ssrc():
    r = _Reader(boom_timer=True)
    vc = _VC(r)
    _seed(vc, 42, 10664)
    _add_ssrc_rotating(vc, 42, 10769)

    assert vc._id_to_ssrc == {42: 10769}
    assert r.packet_router.destroyed == [10664]
