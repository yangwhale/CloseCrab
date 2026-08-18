#!/usr/bin/env python3
"""Multimodal inbound debounce unit tests."""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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


def test_should_debounce_multimodal_types():
    channel = FeishuChannel.__new__(FeishuChannel)
    
    # Text
    t_msg = MockP2Data("m1", "c1", "u1", "text", json.dumps({"text": "hello"}))
    assert channel._should_debounce_msg(t_msg) is True
    
    # Image
    img_msg = MockP2Data("m2", "c1", "u1", "image", json.dumps({"image_key": "img_1"}))
    assert channel._should_debounce_msg(img_msg) is True

    # Media / Video
    vid_msg = MockP2Data("m3", "c1", "u1", "media", json.dumps({"file_key": "vid_1"}))
    assert channel._should_debounce_msg(vid_msg) is True

    # Audio
    aud_msg = MockP2Data("m4", "c1", "u1", "audio", json.dumps({"file_key": "aud_1"}))
    assert channel._should_debounce_msg(aud_msg) is True

    # File
    file_msg = MockP2Data("m5", "c1", "u1", "file", json.dumps({"file_key": "doc_1"}))
    assert channel._should_debounce_msg(file_msg) is True

    # Post
    post_msg = MockP2Data("m6", "c1", "u1", "post", json.dumps({"zh_cn": {"title": "t", "content": []}}))
    assert channel._should_debounce_msg(post_msg) is True

    # App bot sender should not debounce
    app_msg = MockP2Data("m7", "c1", "bot1", "text", json.dumps({"text": "bot msg"}), sender_type="app")
    assert channel._should_debounce_msg(app_msg) is False

    # Control slash command should not debounce
    slash_msg = MockP2Data("m8", "c1", "u1", "text", json.dumps({"text": "/restart"}))
    assert channel._should_debounce_msg(slash_msg) is False

    # Emergency stop should not debounce
    stop_msg = MockP2Data("m9", "c1", "u1", "text", json.dumps({"text": "停止"}))
    assert channel._should_debounce_msg(stop_msg) is False


def test_multimodal_aggregation_debouncer_flow():
    async def run():
        flushes = []
        channel = FeishuChannel.__new__(FeishuChannel)
        channel._app_id = "test_app"
        
        d = InboundDebouncer(
            debounce_s=0.1,
            build_key=channel._build_debounce_key,
            should_debounce=channel._should_debounce_msg,
            on_flush=lambda items: flushes.append(items) or asyncio.sleep(0),
        )

        # 1. Text + Image
        msg1 = MockP2Data("m1", "c1", "u1", "text", json.dumps({"text": "这是什么图片？"}))
        msg2 = MockP2Data("m2", "c1", "u1", "image", json.dumps({"image_key": "img_123"}))
        await d.enqueue(msg1)
        await asyncio.sleep(0.02)
        await d.enqueue(msg2)
        await asyncio.sleep(0.2)

        assert len(flushes) == 1
        assert len(flushes[0]) == 2
        assert flushes[0][0].event.message.message_type == "text"
        assert flushes[0][1].event.message.message_type == "image"

        # 2. Audio + Document + Text
        flushes.clear()
        msg_a = MockP2Data("m3", "c1", "u1", "audio", json.dumps({"file_key": "aud_1"}))
        msg_d = MockP2Data("m4", "c1", "u1", "file", json.dumps({"file_key": "doc_1"}))
        msg_t = MockP2Data("m5", "c1", "u1", "text", json.dumps({"text": "顺便总结第三章"}))
        await d.enqueue(msg_a)
        await asyncio.sleep(0.02)
        await d.enqueue(msg_d)
        await asyncio.sleep(0.02)
        await d.enqueue(msg_t)
        await asyncio.sleep(0.2)

        assert len(flushes) == 1
        assert len(flushes[0]) == 3
        assert [x.event.message.message_type for x in flushes[0]] == ["audio", "file", "text"]

        await d.close()

    asyncio.run(run())


def test_parse_single_message_content():
    async def run():
        channel = FeishuChannel.__new__(FeishuChannel)
        channel._core = MagicMock(_worker_type="dsh")
        channel._bot_open_id = "bot_self"
        channel._async_send_text = AsyncMock()

        # Download attachment mock
        async def mock_download(msg):
            if msg.message_type == "image":
                return ("/tmp/feishu_img_1.jpg", "image.jpg")
            elif msg.message_type == "media":
                return ("/tmp/feishu_vid_1.mp4", "video.mp4")
            elif msg.message_type == "audio":
                return ("/tmp/feishu_aud_1.ogg", "voice.ogg")
            elif msg.message_type == "file":
                return ("/tmp/feishu_doc_1.pdf", "report.pdf")
            return None

        channel._download_attachment = mock_download

        # Text
        t_msg = MockMessage("m1", "c1", "text", json.dumps({"text": "测试文本"}))
        res_t = await channel._parse_single_message_content(t_msg)
        assert res_t == "测试文本"

        # Image
        img_msg = MockMessage("m2", "c1", "image", json.dumps({"image_key": "k1"}))
        res_img = await channel._parse_single_message_content(img_msg)
        assert res_img == "[Attached file: image.jpg (saved at /tmp/feishu_img_1.jpg)]"

        # Video
        vid_msg = MockMessage("m3", "c1", "media", json.dumps({"file_key": "k2"}))
        res_vid = await channel._parse_single_message_content(vid_msg)
        assert res_vid == "[Attached file: video.mp4 (saved at /tmp/feishu_vid_1.mp4)]"

        # Audio on DSH
        aud_msg = MockMessage("m4", "c1", "audio", json.dumps({"file_key": "k3"}))
        res_aud = await channel._parse_single_message_content(aud_msg)
        assert res_aud == "[Attached file: voice.ogg (saved at /tmp/feishu_aud_1.ogg)]"

        # Audio on non-DSH (classic STT)
        channel._core._worker_type = "claude"
        channel._process_audio = AsyncMock(return_value="这是识别出的语音")
        res_aud_stt = await channel._parse_single_message_content(aud_msg)
        assert res_aud_stt == "这是识别出的语音"

        # File
        file_msg = MockMessage("m5", "c1", "file", json.dumps({"file_key": "k4"}))
        res_file = await channel._parse_single_message_content(file_msg)
        assert res_file == "[Attached file: report.pdf (saved at /tmp/feishu_doc_1.pdf)]"

    asyncio.run(run())
