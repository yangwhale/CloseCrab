---
name: multimodal-explainer
description: Generate a comprehensive multimodal HTML explainer document with embedded Gemini TTS voice narration segments, structured sections, callouts, diagrams, and tables. Optimized for explaining complex technical topics (PoC briefs, new domain concepts, architecture deep-dives) to first-time learners who need both written reference AND spoken walkthrough. Use when the user says "做个讲解文档"、"多模态讲解"、"配语音讲解的文档"、"科普文档加语音"、"explain this with voice"、"technical walkthrough"、"语音讲解 HTML"、"多模态科普", OR when a topic is complex enough that an audio walkthrough materially helps comprehension (50+ concepts, multi-stakeholder, first-time exposure).
---

# Multimodal Technical Explainer Documentation

## Purpose

Generate a single-page HTML document combining:
1. **Written reference** — Material Design sections, term definitions, tables, callouts, ASCII flow diagrams
2. **Audio walkthrough** — Per-section Gemini TTS narration (Chinese default, voice-mode口语化 style with emotion tags)
3. **Global navigation** — Sticky sidenav, voice-overview index with durations, scroll anchors

The combination lets first-time learners (a) listen on the go, (b) refer back in text, (c) jump to specific concepts via nav.

## Workflow

Follow these steps in order. **Always confirm scope with user before starting** if topic, audience, depth, or deployment visibility is ambiguous.

### Step 1: Clarify Inputs

Ask the user (concise, max 4 questions):
1. **Source material** — Doc URL / pasted text / topic name? Read it fully before drafting.
2. **Audience** — First-time learner? Expert refresher? Decision-maker (exec brief)?
3. **Voice language + style** — Chinese (default, follows `references/script-style-guide.md` voice-mode rules) or English?
4. **Deployment visibility** — Internal sensitive (IAP-only `/pages/`) or public OK (`/assets/`)? Default to IAP if any internal name, customer name, or unreleased product is mentioned.

### Step 2: Plan Section Structure

Design 5–9 sections covering:
- §0: A "全景图" / "Overview" with an ASCII flow diagram showing the topic's lifecycle
- §1..N-1: Individual concept clusters, each cohesive enough for a 2–4 minute audio segment
- §N (final): A "客户场景串讲" / "Worked Example" that uses every prior concept in a concrete scenario, plus 3–5 self-test questions

Each section gets its own `<audio>` segment. Sections that exceed ~2000 Chinese characters of narration should be split into 2 sub-segments (see `references/audio-segmentation-guide.md`).

### Step 3: Draft the HTML

Start from `assets/html-template.html` (Material Design, Google Sans, Roboto Mono, sidenav, voice-player CSS).

Each section card uses this pattern:
- `<h2>` section title
- `<div class="card-desc">` one-line summary
- `<div class="voice-player">` audio player (added in Step 6)
- Content: `<h3>` subsections, `<div class="term">` for term-name + term-en + term-def + term-example, `<div class="callout tip|warn|fact">`, `<div class="analogy">` for purple类比, `<div class="flow-diagram">` for ASCII art, tables for comparisons

Component reference: `references/html-template-design.md`.

### Step 4: Write Voice Narration Scripts

For each section, write a separate `.txt` file with the spoken script. Follow `references/script-style-guide.md`:
- Lead with `[casually]` or similar Gemini emotion tag
- Switch tag every 1–3 paragraphs to match content emotion
- Short sentences (25–50 chars), no markdown, no emojis
- Spell out symbols (`/` → "斜杠", `=` → "等于"); hyphens in model names break TTS — replace with spaces (`Qwen3.5-397B` → `Qwen3.5 397B`)
- **Maximum ~2000 Chinese characters per segment** (Gemini TTS silently fails above this, outputs ~1s silence)
- End each segment with a one-sentence recap: "听完这段你应该能..."

### Step 5: Generate Voice Files

Run `scripts/generate-voice-segments.sh <input_dir> <output_dir> [--voice orus]`. It:
- Reads all `.txt` files in input dir
- Parallel-invokes `~/CloseCrab/skills/tts-generator/scripts/tts-generate.py`
- Auto-detects failed segments (file < 50KB) and lists them
- Default voice: `orus` (sober male). Override per user preference.

For failed segments: split the script in half at a natural breakpoint, regenerate as `01a-foo.txt` + `01b-foo.txt`.

### Step 6: Embed Audio in HTML

For each section's `<h2>` + `<div class="card-desc">`, insert immediately after:
```html
<div class="voice-player">
  <div class="voice-label">
    <span class="voice-icon">🎧</span>
    <span class="voice-title">语音讲解 · {section name}</span>
    <span class="voice-duration">{duration}</span>
  </div>
  <audio controls preload="none">
    <source src="voice/{filename}.ogg" type="audio/ogg">
  </audio>
</div>
```

Get accurate durations:
```bash
ffprobe -v error -show_entries format=duration -of csv=p=0 file.ogg
```

In the hero section, add a global `<div class="voice-overview">` listing all segments + total duration so users can scan and pick. Pattern shown in `assets/html-template.html`.

### Step 7: Deploy

Run `scripts/deploy-multimodal-doc.sh <html_path> <voice_dir> [--public]`. It:
- Defaults to IAP-only (`gs://chris-pgp-host-asia/cc-pages/pages/` and `pages/voice/`)
- With `--public`: uploads to `assets/` instead
- Uses google-cloud-storage Python SDK (bypasses gcloud's CAA wall on cc-tw)
- Verifies URLs after upload

URL pattern:
- IAP: `https://cc.higcp.com/pages/{filename}.html` + `https://cc.higcp.com/pages/voice/{seg}.ogg`
- Public: `https://cc.higcp.com/assets/{filename}.html` + `https://cc.higcp.com/assets/voice/{seg}.ogg`

See `references/deployment-guide.md` for the IAP-vs-public decision tree.

## Critical Rules

1. **Always confirm visibility before deploy.** Documents mentioning internal Google teammates, customer names, or unreleased products MUST go IAP-only. Default to IAP if unsure.
2. **Always estimate before committing.** Per section: ~150 chars/minute of audio. 9 segments × 2.5 min avg ≈ 22 min total. Tell the user expected total runtime upfront.
3. **Always test one segment first.** Before parallel-generating 8+ segments, test 1 to confirm voice, pace, and emotion tags work as intended.
4. **Always include the worked-example final section.** The section that re-uses every prior term is what makes the document stick — it's the comprehension test.
5. **Never skip the global voice overview.** Even with sidenav, users need to see total duration + segment breakdown upfront to decide listening strategy ("if time-pressed listen X+Y").

## Reference Files

- **references/script-style-guide.md** — Voice-mode口语化 rules, Gemini emotion tag catalog, character normalization, length limits, failure modes
- **references/html-template-design.md** — Material Design CSS components (term box, callout, voice-player, sidenav, flow-diagram, color variables)
- **references/audio-segmentation-guide.md** — How to split long sections, what to do when TTS silently fails, duration estimation formula
- **references/deployment-guide.md** — IAP vs public decision tree, GCS upload via Python SDK (CAA workaround), URL verification commands

## Assets

- **assets/html-template.html** — Complete starter template with sidenav, hero, voice-overview, sample section cards, all CSS components ready to clone

## Scripts

- **scripts/generate-voice-segments.sh** — Parallel TTS generation with failure detection
- **scripts/deploy-multimodal-doc.sh** — GCS upload + URL verification (uses Python SDK to bypass CAA)
