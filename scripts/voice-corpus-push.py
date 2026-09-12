#!/usr/bin/env python3
"""录到一句就推一句到飞书，让人当场听回放。

为什么单独一个进程、而不是在 sidecar 的写盘回调里顺手发：
写盘那条路跑在 Discord 的收音线程上，一次飞书上传是好几百毫秒的网络往返，
卡在那儿会直接把后面的 Opus 包顶掉 —— 录音本身比推送重要得多。所以这里
用最笨的办法：另起一个进程轮询目录。它挂了不影响录音。

**判「写完了」看 .json 不看 .wav。** sidecar 是先写 wav 再写同名 json 的，
所以 json 出现 = wav 已经 close 完。只盯 wav 的话会推出半截文件。

跟 feishu-notify.py 一样，身份只认 BOT_NAME / --bot，没有兜底默认值 ——
填错不是「发给别人」，是「以别人的身份发出」。

用法:
  BOT_NAME=bunny scripts/voice-corpus-push.py            # 盯最新一轮录音
  BOT_NAME=bunny scripts/voice-corpus-push.py --max-age 7200
  BOT_NAME=bunny scripts/voice-corpus-push.py --asr gemini,chirp,funasr --gemini-key-from-bot
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
import wave

ROOT = os.path.expanduser("~/voice-regression/audio")


def find_new(root: str, seen: set) -> list:
    """返回已写完、还没推过的 wav，按路径排序。

    「写完」的判据是同名 .json 存在 —— sidecar 先 wav 后 json。
    """
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".wav"):
                continue
            wav = os.path.join(dirpath, name)
            if wav in seen:
                continue
            if not os.path.exists(wav[:-4] + ".json"):
                continue          # 还在写，下一轮再说
            out.append(wav)
    return sorted(out)


# 这里的码率不是为了「听得清」, 是为了**别把自己加的失真也算进去**。
# 语料的用途是让人判断「Discord 那头收到的到底是什么声音」, 我们这一次转码
# 是第二代有损编码, 它加的伪影会被误读成链路问题。
#
# 2026-09-11 实测 (同一段 12.9s 语音, 对齐后分段 SNR, 只统计有声段):
#     24k 15.4 dB | 32k 17.5 dB | 48k 20.2 dB | 64k 22.0 dB | 96k 26.2 dB | 128k 29.1 dB
# 原来写的 32k 只有 17.5 dB —— 这个档位本身就在制造可听的金属味。96k 到 26 dB,
# 文件也才 180 KB 量级, 对一条几十秒的语音消息完全不是问题, 所以取 96k。
#
# 注意**提高码率不会把带宽拉宽**: 源流在 12 kHz 上方就已经只剩 -72 dB 了
# (全天 10 份录音无一例外)。这里换档只影响我们自己叠上去的那一层失真。
#
# 更正 (同日晚些时候): 上一版这里写「被 Opus 限死在 superwideband」, **是错的** ——
# 后来读每个包的 TOC 字节, 发送端声明的是 Hybrid/**FULLBAND 20 kHz** (cfg15)。
# 带宽档位它给足了, 12 kHz 那道坎是别的原因 (比特分配, 或采集链路本身)。
# 留着这段是提醒: 从波形反推「编码器用了什么模式」是猜, TOC 才是发送端自己说的。
_OGG_BITRATE = "96k"


def to_ogg(wav: str) -> str:
    """转成飞书语音消息要的 opus/ogg。转失败返回空串，不抛。"""
    ogg = wav[:-4] + ".ogg"
    if os.path.exists(ogg):
        return ogg
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
         "-c:a", "libopus", "-b:a", _OGG_BITRATE, "-ar", "48000", "-ac", "1", ogg],
        capture_output=True)
    if r.returncode != 0 or not os.path.exists(ogg):
        print(f"[转码失败] {wav}: {r.stderr.decode()[:200]}", file=sys.stderr)
        return ""
    return ogg


def wav_seconds(wav: str) -> float:
    try:
        with wave.open(wav) as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


# ─── ASR 支路：同一份 wav 顺手转成文字 ──────────────────────────────────
#
# 为什么塞进这个进程、而不是再起一个：这条链路的全部价值就在于**跟音频并排看**。
# 「这段我自己听着挺清楚，机器却识别错了」这句判断，只有两样东西贴在同一条
# 消息里才做得出来 —— 分成两个进程推，时间戳一错位就对不上是哪一句了。
#
# 代价是转写的往返会把字幕推迟（Gemini 实测 1.7–11.7s，Chirp 1.1–1.6s），
# 但音频消息是先发的，实时监听那半点不受影响。
#
# **失败一律写进消息正文，不吞。** 跟下面「到点退出要说一声」同源：
# 静悄悄地没有字幕，跟「识别出来就是空的」长得一模一样。
_ASR_ENGINES = ("gemini", "chirp", "funasr")


def _repo_on_path():
    """让脚本能 import closecrab.* —— 复用生产那份 prompt 和词表。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)


def gemini_key_from_bot(bot: str) -> str:
    """从跑着的 bot 进程里取 GEMINI_API_KEY。

    **这不是兜底，是显式换一个来源。** 交互 shell 会从 ~/.claude/settings.json
    继承一把**已经失效**的 key，脚本照着环境变量跑会全程 `API key not valid` ——
    比没有 key 更难查，因为它长得像模型侧的问题。bot 进程手里那把是活的。
    """
    pids = subprocess.run(["pgrep", "-f", f"closecrab --bot {bot}"],
                          capture_output=True, text=True).stdout.split()
    for pid in pids:
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                for line in f.read().split(b"\0"):
                    if line.startswith(b"GEMINI_API_KEY="):
                        return line.split(b"=", 1)[1].decode()
        except OSError:
            continue
    sys.exit(f"--gemini-key-from-bot: 在 {bot} 的进程里没找到 GEMINI_API_KEY")


_genai_client = None


def asr_gemini(wav: str, model: str, api_key: str) -> str:
    global _genai_client
    from google import genai
    from google.genai import types as gt

    _repo_on_path()
    # 用生产 STT 那份 prompt 本体，不手抄 —— 抄一份就等于又开了一路配置，
    # 那边改了这边不知道，量出来的「效果」就不是生产的效果了。
    from closecrab.voice.gemini_stt import _DEFAULT_PROMPT

    with open(wav, "rb") as f:
        data = f.read()
    # Client 必须留住引用。写成 `genai.Client(...).models.generate_content(...)`
    # 那个临时 Client 会在请求发出前被回收，报的是
    # `Cannot send a request, as the client has been closed` —— 长得像网络问题。
    if _genai_client is None:
        _genai_client = genai.Client(api_key=api_key)
    r = _genai_client.models.generate_content(
        model=model,
        contents=[_DEFAULT_PROMPT, gt.Part.from_bytes(data=data, mime_type="audio/wav")],
        config=gt.GenerateContentConfig(
            thinking_config=gt.ThinkingConfig(thinking_level="MINIMAL")),
    )
    return (r.text or "").strip()


# **chirp_3 已经不能用了**，2026-09-12 实测（project gpu-launchpad-playground）：
#     asia-southeast1 chirp_3 → 403 "no longer generally available"（批量与流式同报）
#     us-central1 / global    → 400 "model does not exist in the location"
# 能跑通普通话的只剩 chirp_2（asia-southeast1 与 us-central1 都行，输出一致）。
#
# ⚠️ 这不只是本脚本的事：`chirp_stt.py` 默认 chirp_3、`livekit_io.py` 的
# `STT_MODEL` 默认也是 chirp_3，而 jarvis 的 `livekit.stt_provider` 正是
# `chirp3_stream` —— 那条 STT 现在是**整条失效**，不是「偶尔识别不准」。
# 流式那条单独验过（StreamingRecognize 同一个 403），不是只有批量接口的事。
_CHIRP_MODEL = os.environ.get("CHIRP_MODEL", "chirp_2")


def asr_chirp(wav: str, language: str = "cmn-Hans-CN") -> str:
    from google.api_core import client_options as gapic_options
    from google.cloud import speech_v2
    from google.cloud.speech_v2.types import cloud_speech

    _repo_on_path()
    from closecrab.voice.chirp_phrases import default_phrases

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("chirp 需要 GOOGLE_CLOUD_PROJECT")
    # Chirp 3 + 普通话目前只在 asia-southeast1，且非 global 区必须显式改端点。
    location = os.environ.get("CHIRP_LOCATION", "asia-southeast1")
    client = speech_v2.SpeechClient(client_options=gapic_options.ClientOptions(
        api_endpoint=f"{location}-speech.googleapis.com"))

    with wave.open(wav) as wf:
        pcm = wf.readframes(wf.getnframes())
        rate, ch = wf.getframerate(), wf.getnchannels()

    phrases = [cloud_speech.PhraseSet.Phrase(
        **({"value": v} if b is None else {"value": v, "boost": float(b)}))
        for v, b in default_phrases()]
    cfg = cloud_speech.RecognitionConfig(
        explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
            encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=rate, audio_channel_count=ch),
        language_codes=[language],
        model=_CHIRP_MODEL,
        features=cloud_speech.RecognitionFeatures(enable_automatic_punctuation=True),
        adaptation=cloud_speech.SpeechAdaptation(phrase_sets=[
            cloud_speech.SpeechAdaptation.AdaptationPhraseSet(
                inline_phrase_set=cloud_speech.PhraseSet(phrases=phrases, boost=10.0))]),
    )
    resp = client.recognize(request=cloud_speech.RecognizeRequest(
        recognizer=f"projects/{project}/locations/{location}/recognizers/_",
        config=cfg, content=pcm))
    return " ".join(r.alternatives[0].transcript
                    for r in resp.results if r.alternatives).strip()


# FunASR 是本地那一路（容器里 `funasr-wss-server-2pass`，10095），没有网络往返，
# 所以它在这张对比表里的角色跟另外两个不一样 —— 量的是「自己机器上能做到多好」。
#
# **mode 选 offline 是量出来的，不是抄生产的。** 服务端二进制叫 2pass，
# 意思是它同时有流式那一半（online）和离线重打分那一半（offline）：
#
#     001.wav  online  0.54s 「现在都是tpu最不型号是啥」
#              offline 0.28s 「现在就是tpu最新的型号是啥」
#     007.wav  online  0.28s 「还有gt的天气」
#              offline 0.16s 「还有今天的天气」
#
# 离线那半**又准又快** —— 快是因为 online 要按 600ms 一块喂进去模拟实时，
# 我们手里本来就是整段 wav，没有边说边出字的需求。
# ⚠️ 生产的 `funasr_stt.py` 写死 `mode: "online"`，拿的正是差的那一半。
#    那边是真流式、确实需要 online 的低延迟，但值不值得改成 2pass 另说。
_FUNASR_WS = os.environ.get("FUNASR_WS_URL", "ws://127.0.0.1:10095")
_FUNASR_MODE = os.environ.get("FUNASR_MODE", "offline")


def asr_funasr(wav: str) -> str:
    import audioop
    from websockets.sync.client import connect

    _repo_on_path()
    # 热词跟生产同源：livekit_io 也是把 chirp 那份词表空格拼起来喂给 FunASR 的。
    from closecrab.voice.chirp_phrases import default_phrases
    hot = " ".join(p for p, _ in default_phrases())

    with wave.open(wav) as wf:
        pcm = wf.readframes(wf.getnframes())
        if wf.getnchannels() > 1:
            pcm = audioop.tomono(pcm, 2, 1, 1)
        if wf.getframerate() != 16000:
            pcm, _ = audioop.ratecv(pcm, 2, 1, wf.getframerate(), 16000, None)

    text = ""
    with connect(_FUNASR_WS, subprotocols=["binary"], close_timeout=5) as ws:
        ws.send(json.dumps({"mode": _FUNASR_MODE, "chunk_size": [5, 10, 5],
                            "wav_name": os.path.basename(wav), "is_speaking": True,
                            "chunk_interval": 10, "itn": True, "hotwords": hot}))
        step = 16000 * 2 * 6 // 10  # 600ms
        for i in range(0, len(pcm), step):
            ws.send(pcm[i:i + step])
        ws.send(json.dumps({"is_speaking": False}))
        while True:
            d = json.loads(ws.recv(timeout=15))
            # 最后一条是**整句**不是增量（实测 online/2pass 都如此），所以直接覆盖。
            if d.get("text"):
                text = d["text"]
            if d.get("is_final") or d.get("mode") in ("offline", "2pass-offline"):
                break
    return text.strip()


_ASR_FN = {"gemini": lambda w, m, k: asr_gemini(w, m, k),
           "chirp": lambda w, m, k: asr_chirp(w),
           "funasr": lambda w, m, k: asr_funasr(w)}


def transcribe(wav: str, engines: list, model: str, api_key: str) -> list:
    """返回 [(引擎名, 显示文本, 耗时秒)]；任何一路失败只影响它自己那行。

    三路**并发**跑：串行的话字幕要等 gemini+chirp+funasr 的耗时之和（实测 5–7s），
    并发下只等最慢那个。每一路的耗时是各自计的，所以那几个数字仍然可比 ——
    并发只压缩了墙钟，没有污染指标。
    """
    def one(name):
        t0 = time.monotonic()
        try:
            text = _ASR_FN[name](wav, model, api_key)
            return name, text or "（识别为空）", time.monotonic() - t0
        except Exception as e:
            return name, f"⚠️ 失败 {type(e).__name__}: {e}"[:200], time.monotonic() - t0

    if not engines:
        return []
    with cf.ThreadPoolExecutor(max_workers=len(engines)) as pool:
        return list(pool.map(one, engines))  # map 保序，输出顺序仍按 --asr 写的来


class Sender:
    def __init__(self, bot: str, to: str | None):
        from google.cloud import firestore
        db = firestore.Client(project="chris-pgp-host", database="closecrab")
        doc = db.collection("bots").document(bot).get()
        if not doc.exists:
            sys.exit(f"bot {bot} not found")
        cfg = doc.to_dict().get("channels", {}).get("feishu", {})
        self.target = to or (cfg.get("voice_mode_users") or [None])[0]
        if not self.target:
            sys.exit("no --to and no voice_mode_users in config")
        self.id_type = "open_id" if self.target.startswith("ou_") else "chat_id"
        import lark_oapi as lark
        self.lark = lark
        self.client = lark.Client.builder() \
            .app_id(cfg["app_id"]).app_secret(cfg["app_secret"]) \
            .log_level(lark.LogLevel.ERROR).build()

    def text(self, s: str):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = CreateMessageRequest.builder().receive_id_type(self.id_type).request_body(
            CreateMessageRequestBody.builder().receive_id(self.target).msg_type("text")
            .content(json.dumps({"text": s}, ensure_ascii=False)).build()).build()
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            print(f"[发文本失败] {resp.code} {resp.msg}", file=sys.stderr)

    def audio(self, ogg: str, ms: int):
        from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody,
                                         CreateMessageRequest, CreateMessageRequestBody)
        with open(ogg, "rb") as f:
            fr = CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder().file_type("opus")
                .file_name("voice.ogg").duration(ms).file(f).build()).build()
            up = self.client.im.v1.file.create(fr)
        if not up.success() or not up.data or not up.data.file_key:
            print(f"[上传失败] {up.code} {up.msg}", file=sys.stderr)
            return False
        req = CreateMessageRequest.builder().receive_id_type(self.id_type).request_body(
            CreateMessageRequestBody.builder().receive_id(self.target).msg_type("audio")
            .content(json.dumps({"file_key": up.data.file_key})).build()).build()
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            print(f"[发语音失败] {resp.code} {resp.msg}", file=sys.stderr)
            return False
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--bot", default=os.environ.get("BOT_NAME") or "")
    ap.add_argument("--to")
    ap.add_argument("--interval", type=float, default=1.0)
    # 自带死期：这是个后台常驻轮询，没人会记得关它。
    ap.add_argument("--max-age", type=float, default=4 * 3600, help="跑多久自己退出(秒)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="启动时把已存在的文件标记为已推（避免重启后刷屏）")
    ap.add_argument("--asr", default="",
                    help=f"顺手转写并附在音频后面，逗号分隔：{'/'.join(_ASR_ENGINES)}")
    ap.add_argument("--asr-model", default="gemini-3-flash-preview",
                    help="gemini 那一路用的模型（默认跟生产 GeminiSTT 一致）")
    ap.add_argument("--gemini-key-from-bot", action="store_true",
                    help="从跑着的 bot 进程取 key，绕开 shell 里那把失效的")
    args = ap.parse_args()
    if not args.bot:
        sys.exit("身份必须显式确定：传 --bot 或设 BOT_NAME")

    engines = [e.strip() for e in args.asr.split(",") if e.strip()]
    bad = [e for e in engines if e not in _ASR_ENGINES]
    if bad:
        sys.exit(f"--asr 不认识：{bad}，可选 {list(_ASR_ENGINES)}")
    api_key = ""
    if "gemini" in engines:
        api_key = (gemini_key_from_bot(args.bot) if args.gemini_key_from_bot
                   else os.environ.get("GEMINI_API_KEY", ""))
        if not api_key:
            sys.exit("gemini 转写要 key：设 GEMINI_API_KEY 或加 --gemini-key-from-bot")

    os.makedirs(args.root, exist_ok=True)
    sender = Sender(args.bot, args.to)
    seen = set()
    if args.skip_existing:
        seen = set(find_new(args.root, set()))
        print(f"启动时跳过 {len(seen)} 个已有文件")

    deadline = time.monotonic() + args.max_age
    print(f"盯着 {args.root}，每 {args.interval}s 扫一次，{args.max_age/3600:.1f}h 后自动退出"
          + (f"，转写: {'+'.join(engines)}" if engines else ""))
    while time.monotonic() < deadline:
        for wav in find_new(args.root, seen):
            seen.add(wav)
            sec = wav_seconds(wav)
            ogg = to_ogg(wav)
            rel = os.path.relpath(wav, args.root)
            ok = bool(ogg) and sender.audio(ogg, int(sec * 1000))
            head = (f"⬆️ {rel} · {sec:.1f}s" if ok
                    else f"⚠️ {rel} · {sec:.1f}s 录到了但推送失败，文件在本地")
            # 转写放在音频之后：音频先到，字幕晚几秒跟上，实时监听不受影响。
            lines = [f"{n}({t:.1f}s): {txt}" for n, txt, t in
                     transcribe(wav, engines, args.asr_model, api_key)]
            sender.text("\n".join([head] + lines))
            print(f"pushed {rel} ({sec:.1f}s)" + (f" {lines}" if lines else ""))
        time.sleep(args.interval)

    # **到点退出必须说一声。** 2026-09-11 栽在这上面：默认 4h 的死期正好在一次
    # 对话中间到点，017 推了、018/019 没推，日志里安安静静一句「到点退出」——
    # 用户那头看到的只是「怎么不推了」，跟进程崩了长得一模一样。
    # 自带死期是对的（没人会记得关它），但**静悄悄地死跟崩溃无法区分**，
    # 所以死之前往同一个 channel 里留句话，顺带把重启命令写上。
    sender.text(f"⏹ 语料推送到点退出（跑满 {args.max_age/3600:.1f}h）。"
                f"要接着推：scripts/voice-corpus-push.py --skip-existing --max-age 28800")
    print("到点退出")


if __name__ == "__main__":
    main()
