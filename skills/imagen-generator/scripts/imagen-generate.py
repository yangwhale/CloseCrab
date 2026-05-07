#!/usr/bin/env python3
"""Gemini Image — Text-to-image generation via Google GenAI SDK (Vertex AI)."""

import argparse
import base64
import os
import re
import subprocess
import sys
from datetime import datetime


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


def generate_image(prompt, model, aspect, resolution):
    from google import genai
    from google.genai import types

    project = get_project()
    client = genai.Client(vertexai=True, project=project, location="global")

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect,
                image_size=resolution,
            ),
        ),
    )

    # Extract image data from response
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data

    # No image found — dump text parts for debugging
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            print(f"Model text: {part.text[:200]}", file=sys.stderr)
    print("Error: No image in response", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Generate images with Gemini on Vertex AI")
    parser.add_argument("prompt", help="Text prompt for image generation")
    parser.add_argument("--aspect", default="1:1",
                        help="Aspect ratio (default: 1:1)")
    parser.add_argument("--count", type=int, default=1,
                        help="Number of images to generate, 1-4 (default: 1)")
    parser.add_argument("--resolution", default="1K",
                        help="Output resolution: 512, 1K, 2K, 4K (default: 1K)")
    parser.add_argument("--output", dest="output_name", default="",
                        help="Custom output filename (without extension)")
    parser.add_argument("--model", default="gemini-3-pro-image-preview",
                        help="Model ID (default: gemini-3-pro-image-preview)")
    args = parser.parse_args()

    web_root = os.environ.get("CC_PAGES_WEB_ROOT")
    url_prefix = os.environ.get("CC_PAGES_URL_PREFIX")
    if not web_root or not url_prefix:
        print("Error: Set CC_PAGES_WEB_ROOT and CC_PAGES_URL_PREFIX env vars", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.join(web_root, "assets", "imagen")
    base_url = f"{url_prefix}/assets/imagen"
    os.makedirs(output_dir, exist_ok=True)

    # Generate output name if not specified
    output_name = args.output_name
    if not output_name:
        sanitized = re.sub(r"[^a-zA-Z0-9]+", "-", args.prompt)[:30].rstrip("-")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_name = f"{sanitized}-{timestamp}"

    for i in range(1, args.count + 1):
        img_bytes = generate_image(args.prompt, args.model, args.aspect, args.resolution)

        suffix = f"-{i}" if args.count > 1 else ""
        filename = f"{output_name}{suffix}.png"
        filepath = os.path.join(output_dir, filename)

        import tempfile
        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        with open(tmp_path, "wb") as f:
            f.write(img_bytes)

        # Upload via gcloud to ensure correct Content-Type and bypass gcsfuse delay
        gcs_dest = f"gs://chris-pgp-host-asia/cc-pages/assets/imagen/{filename}"
        try:
            subprocess.run([
                "gcloud", "storage", "cp", tmp_path, gcs_dest,
                "--content-type=image/png"
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Also write to local gcsfuse mount to keep it in sync locally
            with open(filepath, "wb") as f:
                f.write(img_bytes)
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to upload to GCS: {e}", file=sys.stderr)
            # Fallback to just local write
            with open(filepath, "wb") as f:
                f.write(img_bytes)

        url = f"{base_url}/{filename}"
        print(url)


if __name__ == "__main__":
    main()
