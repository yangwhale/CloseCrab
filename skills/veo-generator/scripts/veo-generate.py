#!/usr/bin/env python3
"""Veo 3.1 — Text-to-video generation via Google GenAI SDK (Vertex AI)."""

import argparse
import base64
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


MODEL_TIERS = {
    "standard": "veo-3.1-generate-001",
    "fast": "veo-3.1-fast-generate-001",
    "lite": "veo-3.1-lite-generate-001",
}


def get_project():
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        try:
            project = subprocess.check_output(
                ["gcloud", "config", "get-value", "project"],
                stderr=subprocess.DEVNULL, text=True
            ).strip()
        except Exception:
            pass
    if not project:
        print("Error: No GCP project. Set GOOGLE_CLOUD_PROJECT or run 'gcloud config set project <id>'", file=sys.stderr)
        sys.exit(1)
    return project


def generate_video(args):
    from google import genai
    from google.genai import types

    project = get_project()
    client = genai.Client(vertexai=True, project=project, location="us-central1")

    model_id = MODEL_TIERS.get(args.model, args.model)

    # Build source
    source = types.GenerateVideosSource(prompt=args.prompt)

    # Image-to-video: input image as first frame
    if args.image:
        image_path = Path(args.image)
        if not image_path.exists():
            print(f"Error: Image file not found: {args.image}", file=sys.stderr)
            sys.exit(1)
        source.image = types.Image.from_file(str(image_path))

    # Build config
    config = types.GenerateVideosConfig(
        number_of_videos=args.count,
        aspect_ratio=args.aspect,
        duration_seconds=args.duration,
        resolution=args.resolution,
        person_generation="allow_adult",
        enhance_prompt=args.enhance,
    )

    if args.negative:
        config.negative_prompt = args.negative

    # Reference images
    if args.ref:
        if len(args.ref) > 3:
            print("Error: Maximum 3 reference images allowed.", file=sys.stderr)
            sys.exit(1)
        ref_images = []
        for ref_path in args.ref:
            if not Path(ref_path).exists():
                print(f"Error: Reference image not found: {ref_path}", file=sys.stderr)
                sys.exit(1)
            ref_images.append(types.VideoGenerationReferenceImage(
                image=types.Image.from_file(ref_path),
                reference_type="asset",
            ))
        config.reference_images = ref_images

    # Last frame for frame interpolation
    if args.last_frame:
        if not Path(args.last_frame).exists():
            print(f"Error: Last frame image not found: {args.last_frame}", file=sys.stderr)
            sys.exit(1)
        config.last_frame = types.Image.from_file(args.last_frame)

    # Submit generation
    print("Submitting video generation request...", file=sys.stderr)
    operation = client.models.generate_videos(
        model=model_id,
        source=source,
        config=config,
    )
    print(f"Operation: {operation.name}", file=sys.stderr)
    print(f"Polling for completion (interval: {args.poll_interval}s, timeout: {args.timeout}s)...", file=sys.stderr)

    # Poll for completion
    elapsed = 0
    while not operation.done:
        if elapsed >= args.timeout:
            print(f"Error: Timeout after {args.timeout}s waiting for video generation.", file=sys.stderr)
            print(f"Operation: {operation.name}", file=sys.stderr)
            sys.exit(1)
        time.sleep(args.poll_interval)
        elapsed += args.poll_interval
        operation = client.operations.get(operation)
        print(f"  Still generating... ({elapsed}s)", file=sys.stderr)

    print(f"Video generation complete! ({elapsed}s)", file=sys.stderr)

    # Check for errors
    if operation.error:
        print(f"Error: {operation.error}", file=sys.stderr)
        sys.exit(1)

    return operation.result


def main():
    parser = argparse.ArgumentParser(description="Generate videos with Veo 3.1 on Vertex AI")
    parser.add_argument("prompt", help="Text prompt for video generation")
    parser.add_argument("--aspect", default="16:9",
                        help="Aspect ratio: 16:9 (default), 9:16")
    parser.add_argument("--count", type=int, default=1,
                        help="Number of videos to generate, 1-4 (default: 1)")
    parser.add_argument("--model", default="standard",
                        help="Model tier: standard (default, best quality), fast, lite")
    parser.add_argument("--duration", type=int, default=8,
                        help="Video duration in seconds: 4, 6, or 8 (default: 8)")
    parser.add_argument("--resolution", default="720p",
                        help="Video resolution: 720p (default), 1080p, 4k")
    parser.add_argument("--negative", default="",
                        help="Negative prompt (content to avoid)")
    parser.add_argument("--output", dest="output_name", default="",
                        help="Custom output filename (without extension)")
    parser.add_argument("--image", default="",
                        help="Input image for image-to-video (used as first frame)")
    parser.add_argument("--ref", action="append", default=[],
                        help="Reference image for visual guidance (up to 3)")
    parser.add_argument("--last-frame", default="",
                        help="Last frame image for frame interpolation")
    parser.add_argument("--no-rewrite", dest="enhance", action="store_false", default=True,
                        help="Disable built-in prompt rewriter")
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Polling interval in seconds (default: 10)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Max wait time in seconds (default: 300)")
    args = parser.parse_args()

    web_root = os.environ.get("CC_PAGES_WEB_ROOT")
    url_prefix = os.environ.get("CC_PAGES_URL_PREFIX")
    if not web_root or not url_prefix:
        print("Error: Set CC_PAGES_WEB_ROOT and CC_PAGES_URL_PREFIX env vars", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.join(web_root, "assets", "veo")
    base_url = f"{url_prefix}/assets/veo"
    os.makedirs(output_dir, exist_ok=True)

    # Generate output name if not specified
    output_name = args.output_name
    if not output_name:
        sanitized = re.sub(r"[^a-zA-Z0-9]+", "-", args.prompt)[:30].rstrip("-")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_name = f"{sanitized}-{timestamp}"

    # Generate videos
    result = generate_video(args)

    if not result or not result.generated_videos:
        print("Error: No videos in response.", file=sys.stderr)
        sys.exit(1)

    # Save videos
    saved = 0
    for i, gen_video in enumerate(result.generated_videos):
        video = gen_video.video
        suffix = f"-{i+1}" if len(result.generated_videos) > 1 else ""
        filename = f"{output_name}{suffix}.mp4"
        filepath = os.path.join(output_dir, filename)

        if video.video_bytes:
            with open(filepath, "wb") as f:
                f.write(video.video_bytes)
        elif video.uri:
            # GCS URI — download with gsutil
            subprocess.run(["gsutil", "cp", video.uri, filepath],
                           capture_output=True, check=True)
        else:
            print(f"Warning: video {i} has no content", file=sys.stderr)
            continue

        url = f"{base_url}/{filename}"
        print(url)
        saved += 1

    if saved == 0:
        rai_count = getattr(result, "rai_media_filtered_count", 0)
        if rai_count:
            print(f"Warning: {rai_count} video(s) filtered by safety checks.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
