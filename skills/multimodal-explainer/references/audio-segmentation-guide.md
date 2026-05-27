# Audio Segmentation Guide

How to plan, split, and validate audio segments for the multimodal explainer.

## Section → Segment Mapping

Default: one audio segment per HTML section.

**Exception**: if a single section's narration exceeds ~2000 Chinese characters, split it into 2 sub-segments labeled `Na` and `Nb`. The HTML section keeps one card but contains two `<audio>` players.

## Length Estimation Formula

For Chinese narration with orus voice:
- **150 characters per minute** of audio (roughly — Gemini TTS pacing)
- **2000 characters → ~3m20s** (still safe)
- **2500 characters → ~4m10s** (risky — has failed in tests)
- **3000+ characters → guaranteed silent failure**

Reverse calculation when designing:
- Want a 2-minute segment? Write ~300 characters of script.
- Want a 4-minute segment? Write ~600 characters. Maximum advisable per single segment.

Worth noting: TTS speed is NOT user-configurable from this skill. If you want shorter audio, write less script.

## Detecting TTS Failure

After running TTS, check each OGG file:

```bash
for f in *.ogg; do
  size=$(stat -c%s "$f")
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
  status="OK"
  [ "$size" -lt 51200 ] && status="❌ FAILED (size=$size bytes, dur=${dur}s)"
  printf "%-40s %s\n" "$f" "$status"
done
```

**Failure signature**: OGG file is ~6KB containing ~1s of silence. Gemini TTS doesn't return an error — it silently truncates input that exceeds its internal limit.

## Splitting a Failed Segment

### Find a natural breakpoint

Look for transition phrases in the original script:
- "接下来讲..." / "然后讲..." / "再讲..."
- New `[emotion-tag]` paragraph break
- New conceptual cluster (e.g. from "algorithms" to "reward design")

Split at that point. Don't split mid-paragraph.

### File naming convention

If original was `04-rl.txt`, split into:
- `04a-rl-algorithms.txt` (first half)
- `04b-rl-reward-metrics.txt` (second half)

The `a` / `b` suffix preserves ordering for the HTML voice-overview index.

### Add transition phrases

Part A should end with: "这是 RL 第一小节，下一节讲..."
Part B should start with: "RL 第二小节，讲..."

This keeps the listener oriented when audio jumps between files.

### Update HTML

In the `<div class="card">` for §3, place TWO `<div class="voice-player">` blocks, one after the other. Number them in the title:

```html
<div class="voice-player">
  <div class="voice-label">
    <span class="voice-title">语音讲解 3a · RL 算法流派</span>
    <span class="voice-duration">2m34s</span>
  </div>
  <audio controls preload="none"><source src="voice/04a-rl-algorithms.ogg" type="audio/ogg"></audio>
</div>

<div class="voice-player">
  <div class="voice-label">
    <span class="voice-title">语音讲解 3b · RL Reward + 监控</span>
    <span class="voice-duration">2m27s</span>
  </div>
  <audio controls preload="none"><source src="voice/04b-rl-reward-metrics.ogg" type="audio/ogg"></audio>
</div>
```

And in the global voice-overview at the top:
```html
<div class="ov-item"><span class="ov-num">3a</span><a href="#sec-3" class="ov-name">RL 算法流派</a><span class="ov-dur">2m34s</span></div>
<div class="ov-item"><span class="ov-num">3b</span><a href="#sec-3" class="ov-name">RL Reward + 监控</a><span class="ov-dur">2m27s</span></div>
```

## Recommended Section Structure (9-segment template)

For a typical complex topic, this segment plan works well:

| § | Topic | Target chars | Target duration |
|---|---|---|---|
| 0 | Overview / 全景图 | ~250 | 1m30s-2m |
| 1 | Foundational concepts (terminology, naming) | ~600 | 2m30s-3m |
| 2 | Core process 1 (e.g. PT + SFT) | ~400 | 2m-2m30s |
| 3a | Core process 2a (e.g. RL algorithms) | ~500 | 2m30s |
| 3b | Core process 2b (e.g. RL reward + monitoring) | ~450 | 2m20s |
| 4 | Special considerations / edge cases | ~500 | 2m40s |
| 5 | Data formats / interface specs | ~380 | 2m |
| 6 | Tool stack / ecosystem | ~600 | 3m |
| 8 | Worked example tying it all together | ~700 | 4m30s |

Total: ~4380 chars × ~150 = ~22 minutes of audio. Comfortable single sitting.

## Duration Extraction

Always use `ffprobe` for the audio-tag's duration display, never estimate from file size (compression varies):

```bash
ffprobe -v error -show_entries format=duration -of csv=p=0 file.ogg
# Returns: 113.166500
```

Format as `MmSSs`:
```bash
dur_sec=$(ffprobe -v error -show_entries format=duration -of csv=p=0 file.ogg | cut -d. -f1)
mins=$((dur_sec / 60))
secs=$((dur_sec % 60))
printf "%dm%02ds\n" "$mins" "$secs"
```
