"""模型分档的测试（design §6.5，用例编号 T241-T246）。

不碰 Firestore、不起真 agent：db() 和 subprocess.run 都打桩，跑完是毫秒级。
"""
import argparse
import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("watch_task", _ROOT / "scripts" / "watch-task.py")
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)


# ── 打桩 ────────────────────────────────────────────────────────────────────

class _FakeDoc:
    def __init__(self, sink):
        self.sink = sink

    def set(self, doc):
        self.sink.update(doc)


class _FakeColl:
    def __init__(self, sink):
        self.sink = sink

    def document(self, _name):
        return _FakeDoc(self.sink)


def _fake_db(sink):
    db = types.SimpleNamespace()
    db.collection = lambda _c: _FakeColl(sink)
    return db


def _create(argv_extra):
    """跑一次真实的 create 命令行，返回落库的 doc。"""
    sink = {}
    argv = ["watch-task.py", "create", "--name", "t", "--prompt", "p",
            "--notify-bot", "jarvis"] + argv_extra
    with mock.patch.object(wt, "db", lambda: _fake_db(sink)), \
         mock.patch.object(sys, "argv", argv):
        wt.main()
    return sink


# ── T241 / T245：默认档 + 向后兼容 ──────────────────────────────────────────

def test_t241_default_is_cheapest_tier():
    assert _create([])["model"] == "haiku"
    assert wt.DEFAULT_MODEL == "haiku"


def _tick_with(task: dict) -> dict:
    """跑真实的 cmd_tick，返回探针收到的参数。

    只打桩 _claim（并发抢占另有测试）和 run_probe 本体 —— 中间那句
    `task.get("model") or DEFAULT_MODEL` 是真代码，这才是被测对象。
    """
    seen, updated = {}, {}

    class _Ref:
        def update(self, upd):
            updated.update(upd)

    class _Q:
        def stream(self):
            return [types.SimpleNamespace(to_dict=lambda: task)]

    class _C:
        def where(self, *a):
            return _Q()

        def document(self, _n):
            return _Ref()

    d = types.SimpleNamespace(collection=lambda _c: _C())

    def fake_probe(prompt, prev, skips, model):
        seen.update(prompt=prompt, model=model)
        return "SKIP", ""

    with mock.patch.object(wt, "db", lambda: d), \
         mock.patch.object(wt, "_claim", lambda *a: task), \
         mock.patch.object(wt, "run_probe", fake_probe):
        wt.cmd_tick(argparse.Namespace(dry_run=False))
    return seen


def _base_task(**over):
    t = {"name": "t", "prompt": "p", "last_report": "", "consecutive_skips": 0,
         "interval_sec": 120, "notify_bot": "jarvis", "status": "active",
         "created_at": wt.NOW(), "next_fire_at": wt.NOW() - wt.timedelta(seconds=1),
         "max_age_sec": 0}
    t.update(over)
    return t


def test_t245_task_without_model_field_still_runs():
    """老任务库里没有 model 字段，按默认档跑，不要求数据迁移。"""
    assert _tick_with(_base_task())["model"] == "haiku"


def test_t245b_task_model_is_honored_by_tick():
    """反过来：写了档位就必须按那个档跑，不能被默认值盖掉。"""
    assert _tick_with(_base_task(model="opus"))["model"] == "opus"


# ── T242：档位真的传到命令行 ────────────────────────────────────────────────

@pytest.mark.parametrize("tier", wt.MODEL_TIERS)
def test_t242_tier_reaches_cli(tier):
    assert _create(["--model", tier])["model"] == tier

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env") or {}
        return types.SimpleNamespace(stdout="SKIP")

    with mock.patch.object(wt.subprocess, "run", fake_run):
        wt.run_probe("prompt", "", 0, tier)

    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == tier


# ── T243 / T246：非法档位必须报错，不能静默退回默认 ─────────────────────────

def test_t243_unknown_tier_rejected():
    with pytest.raises(SystemExit) as e:
        _create(["--model", "gpt-4"])
    assert e.value.code != 0


def test_t246_none_tier_points_at_cron_tool():
    """none 档在 watch 里是退化形态（每轮直投 = cron job），要给出去处。"""
    with pytest.raises(argparse.ArgumentTypeError) as e:
        wt._model_tier("none")
    assert "cron-tool" in str(e.value)


def test_tier_is_case_insensitive():
    assert wt._model_tier("HAIKU") == "haiku"
    assert wt._model_tier(" Opus ") == "opus"


# ── T244：ANTHROPIC_BETAS 必须被剥掉 ────────────────────────────────────────

def test_t244_anthropic_betas_stripped():
    """本机设了 context-1m 的 beta 头，haiku 不支持，带着调直接 400。

    这条只在默认档（haiku）上才有防护价值 —— opus/sonnet 带着都正常，
    所以漏了不会立刻炸，只会让最常跑的那一档静默失败。
    """
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env") or {}
        return types.SimpleNamespace(stdout="SKIP")

    with mock.patch.dict("os.environ", {"ANTHROPIC_BETAS": "context-1m-2025-08-07",
                                        "KEEP_ME": "1"}), \
         mock.patch.object(wt.subprocess, "run", fake_run):
        wt.run_probe("prompt", "", 0, "haiku")

    assert "ANTHROPIC_BETAS" not in captured["env"], "剥离被改掉了，haiku 档会 400"
    assert captured["env"].get("KEEP_ME") == "1", "只该剥这一个变量，别把环境清空"


# ── 契约解析：模型不守规矩时不能把进展吞掉 ──────────────────────────────────

@pytest.mark.parametrize("stdout,want_verdict", [
    ("SKIP", "SKIP"),
    ("REPORT\n第 3 层跑完了", "REPORT"),
    ("DONE: 训练结束，MFU 42%", "DONE"),
    ("我觉得应该还在跑", "REPORT"),          # 没守契约 → 当成有话说，不吞
    ("", "SKIP"),                            # 空输出 → 静默
])
def test_probe_contract_parsing(stdout, want_verdict):
    with mock.patch.object(wt.subprocess, "run",
                           lambda cmd, **kw: types.SimpleNamespace(stdout=stdout)):
        verdict, _ = wt.run_probe("p", "", 0, "haiku")
    assert verdict == want_verdict


def test_probe_timeout_degrades_to_skip():
    """探针超时不能当成有进展去播报，也不能崩掉整个 tick。"""
    def boom(cmd, **kw):
        raise wt.subprocess.TimeoutExpired(cmd, 1)

    with mock.patch.object(wt.subprocess, "run", boom):
        assert wt.run_probe("p", "", 0, "haiku") == ("SKIP", "")
