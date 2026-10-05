#!/usr/bin/env python3
"""App 的文字消息（LiveKit `lk.chat`）→ bot 自己的对话（`livekit_out._deliver_chat`）。

2026-10-05 分工：文字交给 bot（bunny / jarvis），语音照旧走语音助手。
app 的 `session.send(text:)`（聊天框和锁屏快捷回复都走它）发的是 `lk.chat` 文本流；
bot 进程里以 `<bot>-speaker` 进房的那张嘴收下，过滤后用飞书频道的
`inject_synthetic_text` 注入一条主人身份的私聊消息。

要钉住的：
  1. 只收 token 服务给人签的 identity（`voice_assistant_user_<uuid>`）；
     bot 的嘴 `*-speaker`、语音助手 `agent-*`、别的 bot、近似但不精确的都丢
  2. 去空白；空的丢；> 2000 字丢（不截断）；恰好 2000 收
  3. 按 stream id 去重（10 分钟），被拒的不占 id，投递失败把 id 还回去
  4. 注入参数：open_id / chat_id 原样、content 四行格式、source="closecrab-app"
  5. 不是飞书频道 / 桥没接 / 飞书 loop 还没建 / 没有主人私聊 ⇒ 丢弃并 warning
  6. 处理器注册在 `lk.chat` 上、跟播放 RPC 同一处；main.py 接了桥；
     inject_synthetic_text 带 source 参数且默认仍是 zello-stt（Zello 不用改）

跑法：`python3 -m pytest -q test_app_chat.py` 或 `python3 test_app_chat.py`。
不起 LiveKit、不连飞书：读流的 reader 和飞书频道都是假的，飞书 loop 是真 asyncio loop（跑在线程里）。
"""
import asyncio
import inspect
import json  # noqa: F401
import logging
import re
import sys
import threading
import time
import types

sys.path.insert(0, ".")
from closecrab.voice import livekit_out as M  # noqa: E402

GOOD = "voice_assistant_user_0f8fad5b-d9cb-469f-a165-70867728950e"


class FakeFeishu:
    def __init__(self):
        self.calls = []

    async def inject_synthetic_text(self, open_id, chat_id, text, source="zello-stt"):
        self.calls.append((open_id, chat_id, text, source))


class FakeReader:
    _n = 0

    def __init__(self, text, sid=None):
        FakeReader._n += 1
        self._text = text
        self.info = types.SimpleNamespace(stream_id=sid or f"TS_{FakeReader._n}")
        self.read = False

    async def read_all(self):
        self.read = True
        return self._text


class Bridge:
    """起一条真 loop 跑在后台线程里当飞书的 loop，并把桥接到 livekit_out。"""

    def __init__(self, open_id="ou_chris", chat_id="oc_p2p"):
        self.feishu = FakeFeishu()
        self.loop = asyncio.new_event_loop()
        self.th = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.th.start()
        self.open_id, self.chat_id = open_id, chat_id
        M.set_chat_bridge(lambda: (self.feishu, self.loop, self.open_id, self.chat_id))

    def wait(self, n=1):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if len(self.feishu.calls) >= n:
                return True
            time.sleep(0.01)
        return len(self.feishu.calls) >= n

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.th.join(timeout=2)
        self.loop.close()


def reset():
    M.set_chat_bridge(None)
    M._chat_seen = None


def deliver(text, sender=GOOD, sid=None):
    r = FakeReader(text, sid)
    ok = asyncio.run(M._deliver_chat(r, sender))
    return ok, r


# ── 1. 发送方 ──────────────────────────────────────────────────────────

def test_sender_identity_exact_match():
    assert M._app_sender_ok(GOOD)
    for bad in [
        "bunny-speaker", "jarvis-speaker",                     # bot 自己的嘴
        "agent-AJ_x8Kd2", "agent-" + GOOD,                     # 语音助手
        "jarvis", "tommy-voice",                               # 别的 bot
        "voice_assistant_user_1234",                           # 上游旧格式（随机 4 位数）
        GOOD.upper(), GOOD + "-speaker", " " + GOOD, GOOD + "\n",
        "voice_assistant_user_0f8fad5b-d9cb-469f-a165-70867728950",  # 少一位
        "", None, 123,
    ]:
        assert not M._app_sender_ok(bad), bad


def test_identity_rule_matches_token_service():
    """跟 token 服务那行对齐：前端改了 identity 规则，这条该先红。"""
    src = open("infra/livekit/frontend/app/api/token/route.ts", encoding="utf-8").read()
    assert "`voice_assistant_user_${crypto.randomUUID()}`" in src


def test_non_app_senders_dropped_but_stream_drained():
    reset()
    b = Bridge()
    try:
        for s in ["bunny-speaker", "agent-AJ_abc", "jarvis", ""]:
            ok, r = deliver("好", sender=s)
            assert ok is False and r.read, s       # 也要读完，别让 SDK 的 reader 挂着
        time.sleep(0.05)
        assert b.feishu.calls == []
    finally:
        b.close()


# ── 2. 文本 ────────────────────────────────────────────────────────────

def test_text_strip_and_length():
    reset()
    b = Bridge()
    try:
        for t in ["", "   \n\t ", "x" * 2001, "  " + "x" * 2001 + "  "]:
            assert deliver(t)[0] is False, len(t)
        assert b.feishu.calls == []
        assert deliver("x" * 2000)[0] is True                  # 边界：恰好 2000 收
        assert deliver("  " + "y" * 2000 + "\n")[0] is True    # 去空白后算
        assert b.wait(2)
        assert b.feishu.calls[1][2].endswith("\n" + "y" * 2000)
    finally:
        b.close()


# ── 3. 去重 ────────────────────────────────────────────────────────────

def test_stream_id_dedup():
    reset()
    b = Bridge()
    try:
        assert deliver("没问题，请继续", sid="S1")[0] is True
        assert deliver("没问题，请继续", sid="S1")[0] is False
        assert deliver("没问题，请继续", sid="S2")[0] is True   # 同一句话、不同的流 ⇒ 照收
        assert b.wait(2)
        time.sleep(0.05)
        assert len(b.feishu.calls) == 2
    finally:
        b.close()


def test_seen_ids_ttl_boundaries():
    g = M._SeenIds(ttl=600)
    assert g.admit("a", 1000.0)
    assert not g.admit("a", 1599.9)
    assert g.admit("a", 1600.0)        # 到点 ⇒ 过期，可再用
    assert g.admit("b", 5000.0)
    assert g.admit("b", 10.0)          # 时钟往回 ⇒ 旧记录作废
    assert M._CHAT_DEDUP_TTL == 600.0


def test_rejected_does_not_burn_stream_id():
    reset()
    b = Bridge()
    try:
        assert deliver("", sid="K")[0] is False
        assert deliver("好", sender="bunny-speaker", sid="K")[0] is False
        assert deliver("好", sid="K")[0] is True
        assert b.wait(1)
    finally:
        b.close()


def test_delivery_failure_returns_stream_id():
    reset()
    b = Bridge()
    b.close()                                     # 飞书 loop 已关 ⇒ 投递失败
    assert deliver("好", sid="F")[0] is False
    assert "F" not in M._chat_seen._seen
    b2 = Bridge()
    try:
        assert deliver("好", sid="F")[0] is True
        assert b2.wait(1)
    finally:
        b2.close()


# ── 4. 注入参数 ────────────────────────────────────────────────────────

def test_inject_arguments_and_content():
    reset()
    b = Bridge(open_id="ou_owner_1", chat_id="oc_dm_2")
    try:
        assert deliver("  按照你的想法来 \n")[0] is True
        assert b.wait(1)
        open_id, chat_id, content, source = b.feishu.calls[0]
        assert (open_id, chat_id, source) == ("ou_owner_1", "oc_dm_2", "closecrab-app")
        lines = content.split("\n")
        assert lines[0] == "[channel: text]"
        assert re.fullmatch(r"\[当前时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2} HKT\]", lines[1]), lines[1]
        assert lines[2] == "[from: CloseCrab App]"
        assert lines[3:] == ["按照你的想法来"]
    finally:
        b.close()


def test_multiline_text_kept():
    reset()
    b = Bridge()
    try:
        assert deliver("第一行\n第二行")[0] is True
        assert b.wait(1)
        assert b.feishu.calls[0][2].endswith("[from: CloseCrab App]\n第一行\n第二行")
    finally:
        b.close()


def test_content_pure():
    assert M._chat_content("先别", "2026-10-05 21:30 HKT") == (
        "[channel: text]\n[当前时间: 2026-10-05 21:30 HKT]\n[from: CloseCrab App]\n先别")


# ── 5. 桥不可用 ────────────────────────────────────────────────────────

def test_not_feishu_dropped_with_warning(caplog):
    reset()
    with caplog.at_level(logging.WARNING, logger="closecrab.voice.livekit_out"):
        assert deliver("好", sid="Q")[0] is False
    assert any("不是飞书频道" in r.getMessage() for r in caplog.records)
    # 没占 id：桥接上之后同一条流能进来
    b = Bridge()
    try:
        assert deliver("好", sid="Q")[0] is True
    finally:
        b.close()


def test_bridge_parts_missing():
    reset()
    feishu = FakeFeishu()
    loop = asyncio.new_event_loop()
    try:
        for t in [None, (None, loop, "ou", "oc"), (feishu, None, "ou", "oc"),
                  (feishu, loop, "", "oc"), (feishu, loop, "ou", "")]:
            M.set_chat_bridge(lambda t=t: t)
            assert deliver("好")[0] is False, t
        M.set_chat_bridge(lambda: 1 / 0)          # 桥自己抛 ⇒ 丢弃不崩
        assert deliver("好")[0] is False
        assert feishu.calls == []
    finally:
        loop.close()
        reset()


# ── 6. 接线 ────────────────────────────────────────────────────────────

def test_registered_on_lk_chat():
    class FakeRoom:
        def __init__(self):
            self.handlers = {}

        def register_text_stream_handler(self, topic, fn):
            if topic in self.handlers:
                raise ValueError("already set")
            self.handlers[topic] = fn

    room = FakeRoom()
    M._register_chat_stream(room)
    assert room.handlers == {"lk.chat": M._on_chat_stream}
    M._register_chat_stream(room)                   # 重复注册不抛出来
    src = open("closecrab/voice/livekit_out.py", encoding="utf-8").read()
    assert "    _register_playback_rpc(room)\n    _register_chat_stream(room)\n" in src


def test_on_chat_stream_spawns_reader_task():
    reset()
    b = Bridge()

    async def run():
        r = FakeReader("好")
        M._on_chat_stream(r, GOOD)
        assert len(M._chat_tasks) == 1
        await asyncio.gather(*list(M._chat_tasks))
        return r

    try:
        r = asyncio.run(run())
        assert r.read and b.wait(1)
    finally:
        b.close()


def test_main_wires_bridge():
    src = open("closecrab/main.py", encoding="utf-8").read()
    assert "from .voice.livekit_out import set_chat_bridge" in src
    # 不借 Zello 的桥
    seg = src[src.index("def _feishu_chat_target"):src.index("set_chat_bridge(_feishu_chat_target)")]
    assert "zello" not in seg.lower()


def test_main_bridge_resolver_logic():
    """把 main.py 里那个 resolver 抠出来跑：主人取 allowed 第一个、否则最近活跃；chat_id 现取。"""
    src = open("closecrab/main.py", encoding="utf-8").read()
    m = re.search(r"( +)def _feishu_chat_target\(ch=channel\):\n(.*?)\n\1    return [^\n]+\n", src, re.S)
    assert m
    body = "\n".join(line[len(m.group(1)):] for line in m.group(0).splitlines())
    ch = types.SimpleNamespace(_user_chats={}, _allowed_open_ids={"ou_owner"}, _loop="L")
    ns = {"channel": ch}
    exec(body, ns)
    f = ns["_feishu_chat_target"]
    assert f() == (ch, "L", "ou_owner", "")             # 还没私聊过
    ch._user_chats["ou_owner"] = "oc_dm"
    assert f() == (ch, "L", "ou_owner", "oc_dm")         # 之后出现了 ⇒ 现取得到
    ch2 = types.SimpleNamespace(_user_chats={"ou_a": "oc_a", "ou_b": "oc_b"}, _allowed_open_ids=set(), _loop=None)
    ns2 = {"channel": ch2}
    exec(body, ns2)
    assert ns2["_feishu_chat_target"]() == (ch2, None, "ou_b", "oc_b")   # 没 allowed ⇒ 最近活跃


def test_inject_synthetic_text_has_source_param():
    src = open("closecrab/channels/feishu.py", encoding="utf-8").read()
    m = re.search(r"async def inject_synthetic_text\(self, open_id: str, chat_id: str, text: str,\s*"
                  r"source: str = \"zello-stt\"\):", src)
    assert m, "inject_synthetic_text 要带 source 参数，默认 zello-stt（Zello 调用方不改）"
    body = src[m.end():m.end() + 2500]
    assert 'synthetic_id = f"{source}-' in body
    assert "Zello inject" not in body


def test_contract_doc():
    doc = open("docs/livekit-cross-end-contract.md", encoding="utf-8").read()
    assert "`lk.chat`" in doc


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main(["-q", __file__]))
