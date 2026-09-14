#!/usr/bin/env python3
"""TTS 合成缓存的回收：只删长期没被命中的，命中过的要续命。

判据全是**文件系统实况**（谁还在、mtime 是多少），不看返回值 —— 返回值对
而文件没删掉，正是这类 GC 最典型的坏法。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GEMINI_API_KEY", "x")

import closecrab.voice.discord_voice_sidecar as D  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def put(text, voice, age_days):
    """造一个缓存条目：mtime 往前拨 age_days 天，**atime 故意留在当下**。

    这不是图省事，是照着线上实况造的：2026-09-14 实测生产目录里 7180 个文件
    的 atime 全落在 7 天内，mtime 却横跨三个月 —— 有东西整目录扫读过一遍。
    两个时间一起往回拨的话，「判据用 atime」这个变异跟正确实现表现完全一样，
    测试会全绿地放它过去（第一版就是这么漏的）。
    """
    D._cache_save_pcm(text, voice, b"\x01\x02" * 100)
    p = os.path.join(D._TTS_CACHE_DIR,
                     f"{D._cache_key_for_batch(text, voice)}.pcm")
    os.utime(p, (time.time(), time.time() - age_days * 86400))
    return p


def run():
    tmp = tempfile.mkdtemp(prefix="tts-cache-test-")
    saved_dir, saved_last = D._TTS_CACHE_DIR, D._last_tts_cache_gc[0]
    D._TTS_CACHE_DIR = tmp
    try:
        old = put("很久没人用的一句", "orus", 45)
        fresh = put("昨天刚用过的一句", "orus", 1)
        edge = put("正好卡在边界上那句", "orus", 29)

        # 正例 1：命中就续命 —— 一个 45 天没动的条目，读一次之后不该再被删。
        revived = put("老条目但刚被命中", "orus", 45)
        before = os.path.getmtime(revived)
        got = D._cache_get_pcm("老条目但刚被命中", "orus")
        after = os.path.getmtime(revived)
        check("正例 1：命中返回数据且 mtime 被刷新",
              got == b"\x01\x02" * 100 and after - before > 40 * 86400,
              f"mtime 前进了 {(after - before) / 86400:.1f} 天")

        D._last_tts_cache_gc[0] = 0.0
        D._maybe_gc_tts_cache()

        # 正例 2：真正过期的被删。
        check("正例 2：45 天没命中的被删", not os.path.exists(old))
        # 反例 1：近期用过的不能动。
        check("反例 1：1 天前的还在", os.path.exists(fresh))
        # 反例 2：边界内的不能删。29 < 30，差一天就误删是最常见的 off-by-one。
        check("反例 2：29 天的还在（保留期 30 天，别差一天就杀）",
              os.path.exists(edge))
        # 反例 3：刚被命中续过命的必须活下来 —— 这条要是挂了，
        # 「命中续命」就是写了个寂寞，天天在用的条目照样 30 天一到就没。
        check("反例 3：命中续过命的老条目活下来了", os.path.exists(revived))

        # 反例 4：一小时节流。刚跑过就再叫一次，不该再扫目录。
        put("节流测试用的一句", "orus", 99)
        D._maybe_gc_tts_cache()          # 紧接着再调，应被节流挡下
        check("反例 4：一小时内不重复扫（99 天的这次没被删）",
              os.path.exists(os.path.join(
                  D._TTS_CACHE_DIR,
                  f"{D._cache_key_for_batch('节流测试用的一句', 'orus')}.pcm")))

        # 反例 5：超长文本根本不进缓存 —— 存不进来，自然也不用回收。
        long_text = "这是一句超过三十个字的很长很长的文本用来验证它不会被写进缓存目录里去"
        assert len(long_text) > D._CACHE_MAX_CHARS
        n_before = len(os.listdir(tmp))
        D._cache_save_pcm(long_text, "orus", b"\x03" * 100)
        check("反例 5：超过 30 字的不落缓存",
              len(os.listdir(tmp)) == n_before,
              f"目录里 {n_before} → {len(os.listdir(tmp))} 个文件")
    finally:
        D._TTS_CACHE_DIR, D._last_tts_cache_gc[0] = saved_dir, saved_last
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过"
          + ("" if not bad else f" —— 失败: {bad}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
