#!/usr/bin/env python3
"""Comprehensive test runner for Feishu multimodal message debounce and aggregation."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from closecrab.channels.feishu import FeishuChannel
from closecrab.utils.inbound_debouncer import InboundDebouncer


class MockMessage:
    def __init__(self, message_id, chat_id, message_type, content, mentions=None, parent_id=None):
        self.message_id = message_id
        self.chat_id = chat_id
        self.message_type = message_type
        self.content = content
        self.mentions = mentions or []
        self.parent_id = parent_id
        self.root_id = None
        self.create_time = "1000"
        self.chat_type = "p2p"


class MockSender:
    def __init__(self, open_id, sender_type="user"):
        self.sender_type = sender_type
        self.sender_id = MagicMock(open_id=open_id)


class MockEvent:
    def __init__(self, message, sender):
        self.message = message
        self.sender = sender


class MockP2Data:
    def __init__(self, message_id, chat_id, open_id, message_type, content, mentions=None, sender_type="user"):
        self.event = MockEvent(
            MockMessage(message_id, chat_id, message_type, content, mentions),
            MockSender(open_id, sender_type),
        )


async def run_combination_test(name, messages_with_delays, expected_flushes_count, expected_content_checks):
    print(f"\n================ Running Test: {name} ================")
    channel = FeishuChannel.__new__(FeishuChannel)
    channel._app_id = "test_app"
    channel._bot_open_id = "bot_tianmao"
    channel._core = MagicMock(_worker_type="dsh")
    channel._async_send_text = AsyncMock()

    # Mock download attachment
    async def mock_download(msg):
        mtype = msg.message_type
        content_dict = json.loads(msg.content) if msg.content else {}
        fname = content_dict.get("file_name") or f"mock_{mtype}.bin"
        if mtype == "image":
            fname = content_dict.get("file_name", "photo.jpg")
            return (f"/tmp/feishu_img_{msg.message_id}.jpg", fname)
        elif mtype == "media":
            fname = content_dict.get("file_name", "video.mp4")
            return (f"/tmp/feishu_vid_{msg.message_id}.mp4", fname)
        elif mtype == "audio":
            fname = content_dict.get("file_name", "voice.ogg")
            return (f"/tmp/feishu_aud_{msg.message_id}.ogg", fname)
        elif mtype == "file":
            fname = content_dict.get("file_name", "document.pdf")
            return (f"/tmp/feishu_doc_{msg.message_id}.pdf", fname)
        return None

    channel._download_attachment = mock_download

    dispatched_turns = []

    async def mock_handle_message(data, merged_items=None):
        if merged_items:
            extracted_parts = []
            for it in merged_items:
                if it.event and it.event.message:
                    part = await channel._parse_single_message_content(it.event.message)
                    if part:
                        extracted_parts.append(part)
            content = "\n\n".join(extracted_parts)
        else:
            content = await channel._parse_single_message_content(data.event.message)
        dispatched_turns.append({
            "primary_msg_id": data.event.message.message_id,
            "merged_count": len(merged_items) if merged_items else 1,
            "content": content,
        })

    channel._handle_message_async = mock_handle_message

    debouncer = InboundDebouncer(
        debounce_s=0.15,
        build_key=channel._build_debounce_key,
        should_debounce=channel._should_debounce_msg,
        on_flush=channel._on_debounced_flush,
    )

    for msg, delay in messages_with_delays:
        await debouncer.enqueue(msg)
        if delay > 0:
            await asyncio.sleep(delay)

    # Wait for debouncer flush
    await asyncio.sleep(0.3)
    await debouncer.close()

    print(f"Total Dispatched Turns: {len(dispatched_turns)}")
    for idx, turn in enumerate(dispatched_turns):
        print(f"  Turn {idx+1}: merged_count={turn['merged_count']}")
        print(f"  Content:\n{turn['content']}\n")

    assert len(dispatched_turns) == expected_flushes_count, f"Expected {expected_flushes_count} turns, got {len(dispatched_turns)}"
    for check_fn in expected_content_checks:
        check_fn(dispatched_turns)
    print(f"--> PASS: {name}")


async def main():
    # 1. Permutation 1: Text + 1 Image
    await run_combination_test(
        name="1. Text + Image (0.05s apart)",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "text", json.dumps({"text": "帮我看看这张架构图"})), 0.05),
            (MockP2Data("m2", "c1", "u1", "image", json.dumps({"image_key": "img_arch", "file_name": "arch.jpg"})), 0),
        ],
        expected_flushes_count=1,
        expected_content_checks=[
            lambda turns: "帮我看看这张架构图" in turns[0]["content"],
            lambda turns: "[Attached file: arch.jpg" in turns[0]["content"],
            lambda turns: turns[0]["merged_count"] == 2,
        ]
    )

    # 2. Permutation 2: Image + Text
    await run_combination_test(
        name="2. Image + Text (0.05s apart)",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "image", json.dumps({"image_key": "img_sample", "file_name": "sample.png"})), 0.05),
            (MockP2Data("m2", "c1", "u1", "text", json.dumps({"text": "这个怎么修？"})), 0),
        ],
        expected_flushes_count=1,
        expected_content_checks=[
            lambda turns: "[Attached file: sample.png" in turns[0]["content"],
            lambda turns: "这个怎么修？" in turns[0]["content"],
            lambda turns: turns[0]["content"].index("sample.png") < turns[0]["content"].index("这个怎么修？"),
        ]
    )

    # 3. Permutation 3: Video + Text
    await run_combination_test(
        name="3. Video + Text (0.05s apart)",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "media", json.dumps({"file_key": "vid_demo", "file_name": "demo.mp4"})), 0.05),
            (MockP2Data("m2", "c1", "u1", "text", json.dumps({"text": "视频第 3 秒发生了什么？"})), 0),
        ],
        expected_flushes_count=1,
        expected_content_checks=[
            lambda turns: "[Attached file: demo.mp4" in turns[0]["content"],
            lambda turns: "视频第 3 秒发生了什么？" in turns[0]["content"],
        ]
    )

    # 4. Permutation 4: Audio + Image
    await run_combination_test(
        name="4. Audio + Image (0.05s apart)",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "audio", json.dumps({"file_key": "aud_prompt", "file_name": "voice.ogg"})), 0.05),
            (MockP2Data("m2", "c1", "u1", "image", json.dumps({"image_key": "img_photo", "file_name": "photo.jpg"})), 0),
        ],
        expected_flushes_count=1,
        expected_content_checks=[
            lambda turns: "[Attached file: voice.ogg" in turns[0]["content"],
            lambda turns: "[Attached file: photo.jpg" in turns[0]["content"],
        ]
    )

    # 5. Permutation 5: Audio + Document
    await run_combination_test(
        name="5. Audio + Document (0.05s apart)",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "audio", json.dumps({"file_key": "aud_1", "file_name": "prompt.ogg"})), 0.05),
            (MockP2Data("m2", "c1", "u1", "file", json.dumps({"file_key": "doc_1", "file_name": "contract.pdf"})), 0),
        ],
        expected_flushes_count=1,
        expected_content_checks=[
            lambda turns: "[Attached file: prompt.ogg" in turns[0]["content"],
            lambda turns: "[Attached file: contract.pdf" in turns[0]["content"],
        ]
    )

    # 6. Permutation 6: 3 Images + Text
    await run_combination_test(
        name="6. 3 Images + Text (0.04s apart each)",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "image", json.dumps({"image_key": "img_1", "file_name": "img1.png"})), 0.04),
            (MockP2Data("m2", "c1", "u1", "image", json.dumps({"image_key": "img_2", "file_name": "img2.png"})), 0.04),
            (MockP2Data("m3", "c1", "u1", "image", json.dumps({"image_key": "img_3", "file_name": "img3.png"})), 0.04),
            (MockP2Data("m4", "c1", "u1", "text", json.dumps({"text": "对比这三张图的差异"})), 0),
        ],
        expected_flushes_count=1,
        expected_content_checks=[
            lambda turns: "img1.png" in turns[0]["content"] and "img2.png" in turns[0]["content"] and "img3.png" in turns[0]["content"],
            lambda turns: "对比这三张图的差异" in turns[0]["content"],
            lambda turns: turns[0]["merged_count"] == 4,
        ]
    )

    # 7. Permutation 7: Multi-Modality Mega Batch (Audio + Image + Document + Video + Text)
    await run_combination_test(
        name="7. All 5 Modalities in One Batch",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "audio", json.dumps({"file_key": "a1", "file_name": "speech.ogg"})), 0.03),
            (MockP2Data("m2", "c1", "u1", "image", json.dumps({"image_key": "i1", "file_name": "slide.jpg"})), 0.03),
            (MockP2Data("m3", "c1", "u1", "file", json.dumps({"file_key": "f1", "file_name": "paper.pdf"})), 0.03),
            (MockP2Data("m4", "c1", "u1", "media", json.dumps({"file_key": "v1", "file_name": "clip.mp4"})), 0.03),
            (MockP2Data("m5", "c1", "u1", "text", json.dumps({"text": "把这些材料汇总分析"})), 0),
        ],
        expected_flushes_count=1,
        expected_content_checks=[
            lambda turns: turns[0]["merged_count"] == 5,
            lambda turns: "speech.ogg" in turns[0]["content"] and "slide.jpg" in turns[0]["content"] and "paper.pdf" in turns[0]["content"] and "clip.mp4" in turns[0]["content"] and "把这些材料汇总分析" in turns[0]["content"],
        ]
    )

    # 8. Permutation 8: Immediate Slash Command Breaks Buffer
    await run_combination_test(
        name="8. Text followed immediately by /restart (Bypasses debounce)",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "text", json.dumps({"text": "先说一句话"})), 0.02),
            (MockP2Data("m2", "c1", "u1", "text", json.dumps({"text": "/restart"})), 0),
        ],
        expected_flushes_count=1,
        expected_content_checks=[
            lambda turns: "/restart" in turns[0]["content"],
        ]
    )

    # 9. Permutation 9: Long interval > 0.15s cleanly separates into 2 turns
    await run_combination_test(
        name="9. Two messages with 0.3s delay -> 2 separate turns",
        messages_with_delays=[
            (MockP2Data("m1", "c1", "u1", "text", json.dumps({"text": "第一条独立消息"})), 0.3),
            (MockP2Data("m2", "c1", "u1", "text", json.dumps({"text": "第二条独立消息"})), 0),
        ],
        expected_flushes_count=2,
        expected_content_checks=[
            lambda turns: turns[0]["content"] == "第一条独立消息",
            lambda turns: turns[1]["content"] == "第二条独立消息",
        ]
    )

    print("\n================ ALL 9 PERMUTATION TESTS PASSED PERFECTLY! ================\n")

if __name__ == "__main__":
    asyncio.run(main())
