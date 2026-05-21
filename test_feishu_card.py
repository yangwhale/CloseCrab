#!/usr/bin/env python3
"""FeishuChannel card rendering tests (pure data, no async).

Tests _build_ask_question_card multi/single dispatch.
Run: python3 test_feishu_card.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from closecrab.channels.feishu import FeishuChannel

RESULTS = []


def record(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append((name, passed, detail))
    marker = "✅" if passed else "❌"
    print(f"{marker} [{status}] {name}: {detail}")


def _tags(card: dict) -> list[str]:
    return [e.get("tag") for e in card.get("elements", [])]


def test_single_select_renders_buttons():
    """Single-select (no multiSelect or multiSelect=False) renders action buttons."""
    inp = {"questions": [{
        "question": "同意吗",
        "options": [{"label": "同意"}, {"label": "拒绝"}],
    }]}
    card = FeishuChannel._build_ask_question_card(None, inp)
    tags = _tags(card)
    has_action = "action" in tags
    hint = card["elements"][-1]["elements"][0]["content"]
    ok = has_action and "点击按钮" in hint
    record(
        "single_select_renders_buttons",
        ok,
        f"tags={tags} hint={hint!r}",
    )


def test_multi_select_no_buttons():
    """multiSelect=True suppresses action buttons, lists options as text."""
    inp = {"questions": [{
        "question": "选哪些",
        "multiSelect": True,
        "options": [
            {"label": "A", "description": "选项 A 描述"},
            {"label": "B"},
            {"label": "C", "description": "选项 C"},
        ],
    }]}
    card = FeishuChannel._build_ask_question_card(None, inp)
    tags = _tags(card)
    has_action = "action" in tags
    hint = card["elements"][-1]["elements"][0]["content"]
    # 选项列表 div 应该是 elements[1]（elements[0] 是 question 标题 div）
    opts_content = card["elements"][1]["text"]["content"]
    ok = (
        not has_action
        and "多选" in hint
        and "1." in opts_content
        and "2." in opts_content
        and "3." in opts_content
        and "A" in opts_content
        and "B" in opts_content
        and "C" in opts_content
    )
    record(
        "multi_select_no_buttons",
        ok,
        f"tags={tags} hint_ok={'多选' in hint} opts_ok={'1.' in opts_content and '3.' in opts_content}",
    )


def test_multi_select_title_marker():
    """multi-select question title gets [多选] prefix; single doesn't."""
    inp = {"questions": [
        {"question": "Q1", "multiSelect": True, "options": [{"label": "x"}]},
        {"question": "Q2", "options": [{"label": "y"}]},
    ]}
    card = FeishuChannel._build_ask_question_card(None, inp)
    # elements: [Q1 title div, Q1 options div, Q2 title div, Q2 action, hr, note]
    q1_title = card["elements"][0]["text"]["content"]
    q2_title = card["elements"][2]["text"]["content"]
    ok = "多选" in q1_title and "多选" not in q2_title
    record(
        "multi_select_title_marker",
        ok,
        f"q1={q1_title!r} q2={q2_title!r}",
    )


def test_mixed_questions_hint_is_multi():
    """Any multiSelect=True question in batch → hint uses multi wording."""
    inp = {"questions": [
        {"question": "single", "options": [{"label": "a"}]},
        {"question": "multi", "multiSelect": True, "options": [{"label": "b"}]},
    ]}
    card = FeishuChannel._build_ask_question_card(None, inp)
    hint = card["elements"][-1]["elements"][0]["content"]
    ok = "多选" in hint
    record(
        "mixed_questions_hint_is_multi",
        ok,
        f"hint={hint!r}",
    )


def test_empty_options_no_action_no_crash():
    """Question with no options: don't render action, don't crash."""
    inp = {"questions": [{"question": "Open-ended?", "options": []}]}
    card = FeishuChannel._build_ask_question_card(None, inp)
    tags = _tags(card)
    ok = "action" not in tags and tags[0] == "div"
    record(
        "empty_options_no_action_no_crash",
        ok,
        f"tags={tags}",
    )


def test_multiselect_default_false():
    """multiSelect field omitted → defaults to single (button mode)."""
    inp = {"questions": [{
        "question": "缺省字段",
        "options": [{"label": "a"}, {"label": "b"}],
    }]}
    card = FeishuChannel._build_ask_question_card(None, inp)
    tags = _tags(card)
    ok = "action" in tags
    record(
        "multiselect_default_false",
        ok,
        f"tags={tags}",
    )


def test_card_header_and_template():
    """Card always has orange header with confirmation title."""
    inp = {"questions": [{"question": "Q", "options": [{"label": "x"}]}]}
    card = FeishuChannel._build_ask_question_card(None, inp)
    header = card.get("header", {})
    template = header.get("template")
    title = header.get("title", {}).get("content", "")
    ok = template == "orange" and "确认" in title
    record(
        "card_header_and_template",
        ok,
        f"template={template} title={title!r}",
    )


def main():
    tests = [
        test_single_select_renders_buttons,
        test_multi_select_no_buttons,
        test_multi_select_title_marker,
        test_mixed_questions_hint_is_multi,
        test_empty_options_no_action_no_crash,
        test_multiselect_default_false,
        test_card_header_and_template,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            record(t.__name__, False, f"exception: {e!r}")

    passed = sum(1 for _, p, _ in RESULTS if p)
    total = len(RESULTS)
    print(f"\n{'='*60}\n{passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
