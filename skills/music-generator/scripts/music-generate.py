#!/usr/bin/env python3
"""Generate music using Google Lyria 3 (DeepMind) via Gemini API."""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime


MODELS = {
    "clip": "lyria-3-clip-preview",
    "pro": "lyria-3-pro-preview",
}

GCS_BUCKET = "gs://chris-pgp-host-asia/cc-pages/assets/music"


def build_prompt(base_prompt, style=None, key=None, tempo=None, vocal=False):
    parts = [base_prompt.rstrip(". ")]
    if style:
        parts.append(f"Style: {style}.")
    if key:
        parts.append(f"Key: {key}.")
    if tempo:
        parts.append(f"Tempo: {tempo} BPM.")
    if not vocal:
        parts.append("Instrumental only. NO vocals. NO singing. NO humming.")
    return " ".join(parts)


def generate(prompt, model_id):
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    print(f"Model: {model_id}", file=sys.stderr)
    print(f"Prompt: {prompt[:120]}{'...' if len(prompt) > 120 else ''}", file=sys.stderr)

    response = client.models.generate_content(model=model_id, contents=prompt)

    if hasattr(response, "prompt_feedback") and response.prompt_feedback:
        fb = response.prompt_feedback
        if hasattr(fb, "block_reason") and fb.block_reason:
            print(f"Error: Prompt blocked — {fb.block_reason}", file=sys.stderr)
            print("Tip: Remove any specific song/artist/composition names from your prompt.", file=sys.stderr)
            sys.exit(1)

    if not response.candidates:
        print("Error: No candidates in response", file=sys.stderr)
        sys.exit(1)

    audio_data = None
    text_parts = []
    for part in response.candidates[0].content.parts:
        if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
            mime = getattr(part.inline_data, "mime_type", "")
            if "audio" in mime or not audio_data:
                audio_data = part.inline_data.data
        elif hasattr(part, "text") and part.text:
            text_parts.append(part.text)

    if text_parts:
        print(f"Model notes: {' '.join(text_parts)[:200]}", file=sys.stderr)

    if not audio_data:
        print("Error: No audio data in response", file=sys.stderr)
        sys.exit(1)

    return audio_data


def save_and_publish(audio_bytes, output_name, ext="mp3"):
    web_root = os.environ.get("CC_PAGES_WEB_ROOT")
    url_prefix = os.environ.get("CC_PAGES_URL_PREFIX")
    if not web_root or not url_prefix:
        print("Error: Set CC_PAGES_WEB_ROOT and CC_PAGES_URL_PREFIX env vars", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.join(web_root, "assets", "music")
    os.makedirs(output_dir, exist_ok=True)

    filename = f"{output_name}.{ext}"
    filepath = os.path.join(output_dir, filename)

    import tempfile
    tmp_path = os.path.join(tempfile.gettempdir(), filename)
    with open(tmp_path, "wb") as f:
        f.write(audio_bytes)

    mime_type = "audio/mpeg" if ext == "mp3" else f"audio/{ext}"
    gcs_dest = f"{GCS_BUCKET}/{filename}"
    try:
        subprocess.run(
            ["gcloud", "storage", "cp", tmp_path, gcs_dest, f"--content-type={mime_type}"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(filepath, "wb") as f:
            f.write(audio_bytes)
    except subprocess.CalledProcessError as e:
        print(f"Warning: GCS upload failed: {e}", file=sys.stderr)
        with open(filepath, "wb") as f:
            f.write(audio_bytes)

    url = f"{url_prefix}/assets/music/{filename}"
    size_kb = len(audio_bytes) / 1024
    print(f"Saved: {filename} ({size_kb:.0f} KB)", file=sys.stderr)
    return url


def main():
    parser = argparse.ArgumentParser(description="Generate music with Google Lyria 3")
    parser.add_argument("prompt", help="Text prompt describing the music to generate")
    parser.add_argument("--duration", choices=["clip", "full"], default="clip",
                        help="clip = 30s preview (default), full = 1-3 min song")
    parser.add_argument("--style", help="Music style (e.g. piano, jazz, orchestral, ambient)")
    parser.add_argument("--key", help="Musical key (e.g. 'C minor', 'Bb major')")
    parser.add_argument("--tempo", type=int, help="Tempo in BPM")
    parser.add_argument("--vocal", action="store_true", help="Allow vocals (default: instrumental only)")
    parser.add_argument("--output", dest="output_name", default="",
                        help="Custom output filename (without extension)")
    parser.add_argument("--model", help="Override model ID (default: auto by duration)")
    args = parser.parse_args()

    model_id = args.model or MODELS[args.duration]
    prompt = build_prompt(args.prompt, style=args.style, key=args.key, tempo=args.tempo, vocal=args.vocal)

    output_name = args.output_name
    if not output_name:
        sanitized = re.sub(r"[^a-zA-Z0-9]+", "-", args.prompt)[:30].rstrip("-")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_name = f"{sanitized}-{timestamp}"

    audio_bytes = generate(prompt, model_id)
    url = save_and_publish(audio_bytes, output_name)
    print(url)


if __name__ == "__main__":
    main()
