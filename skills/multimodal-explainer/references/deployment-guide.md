# Deployment Guide — CC Pages Upload

How to deploy the multimodal HTML doc + voice files to CC Pages.

## IAP vs Public Decision Tree

**Default to IAP-only** (`/pages/`) if ANY of these apply:
- Mentions internal Google teammates by name (Alex, Andy, Hengtao, Junjie, Rich, etc.)
- Mentions a customer company name (Tencent, Alibaba, ByteDance, ...)
- Mentions unreleased product features, internal roadmap items, ETAs
- Mentions internal code names, internal project names
- Cites internal documents (go/ links, g3doc URLs, internal Slack threads)
- Contains internal performance data, internal benchmark results, internal pricing
- References an internal team's pushback / objection / risk assessment

**OK to make public** (`/assets/`) only if ALL are true:
- Pure technical concepts (e.g., "what is MoE", "what is PPO") — no internal context
- No customer names anywhere
- No internal teammate names anywhere
- Data cited is from public sources (papers, public benchmarks, vendor docs)
- User explicitly says it's safe to publish

When unsure, default to IAP. Easier to publish later than to retract.

## GCS Buckets

- **IAP-only**: `gs://chris-pgp-host-asia/cc-pages/pages/...` → `https://cc.higcp.com/pages/...`
- **Public**: `gs://chris-pgp-host-asia/cc-pages/assets/...` → `https://cc.higcp.com/assets/...`

Voice files live in a `voice/` subdirectory under either:
- IAP voice: `https://cc.higcp.com/pages/voice/{seg}.ogg`
- Public voice: `https://cc.higcp.com/assets/voice/{seg}.ogg`

In the HTML, `<audio src="voice/{seg}.ogg">` (relative path) — works for both because the HTML and audio share the same parent directory under both layouts.

## Upload Method: Python SDK (NOT gcloud)

`gcloud storage cp` and `gsutil cp` BOTH fail on cc-tw because of Google's Context-Aware Access (CAA) policy — they require an Endpoint Verification cert which a headless GCE VM cannot provide.

Use `google-cloud-storage` Python SDK instead, which uses the Service Account credentials directly:

```python
from google.cloud import storage
client = storage.Client(project='chris-pgp-host')
bucket = client.bucket('chris-pgp-host-asia')

# Upload HTML
html_blob = bucket.blob('cc-pages/pages/my-doc.html')
html_blob.upload_from_filename('/path/to/my-doc.html')

# Upload voice files
for ogg in os.listdir('/path/to/voice/'):
    blob = bucket.blob(f'cc-pages/pages/voice/{ogg}')
    blob.upload_from_filename(f'/path/to/voice/{ogg}')
    print(f'  ✅ {ogg}: {blob.size} bytes')
```

This always works because the SDK uses Application Default Credentials (the SA key already configured on cc-tw via the Firestore client setup).

## URL Verification

After upload, verify with `curl`:

```bash
# IAP page: expect 302 redirect to Google login
curl -sI "https://cc.higcp.com/pages/my-doc.html" | head -3
# HTTP/2 302
# location: https://accounts.google.com/o/oauth2/v2/auth?...

# Public page: expect 200 with content
curl -sI "https://cc.higcp.com/assets/my-doc.html" | head -3
# HTTP/2 200
# content-type: text/html
# content-length: 12345

# Voice file (same logic)
curl -sI "https://cc.higcp.com/pages/voice/01-overview.ogg" | head -3
# HTTP/2 302  (IAP) or HTTP/2 200 (public)
```

A 302 to `accounts.google.com` means IAP is working correctly. Logged-in Chrome with corp account will pass through.

## Common Pitfalls

1. **gcsfuse mount is unreliable** — don't rely on writing to `~/gcs-mount/cc-pages/...` propagating to GCS quickly. Always use the SDK to upload explicitly.

2. **Voice URL must match HTML URL location** — if HTML is at `/pages/foo.html` and voice at `/assets/voice/`, the `<audio src="voice/...">` (relative) will resolve wrong. Keep them under the same parent.

3. **IAP cookie scope** — IAP grants access at the load balancer level for the whole `cc.higcp.com` domain. Same-domain audio fetch works seamlessly. Cross-domain audio would NOT work due to IAP cookie boundaries.

4. **`preload="none"` is intentional** — without it, opening the HTML triggers immediate download of all 9 OGG files (~8MB total). With `preload="none"`, browser only loads when user clicks play.

5. **Test on actual deployment** — local file:// URL doesn't reproduce IAP behavior. Always open the deployed URL in a real browser to confirm audio plays.

## Example deploy script invocation

```bash
~/CloseCrab/skills/multimodal-explainer/scripts/deploy-multimodal-doc.sh \
    /tmp/my-explainer/index.html \
    /tmp/my-explainer/voice/

# Or for public:
~/CloseCrab/skills/multimodal-explainer/scripts/deploy-multimodal-doc.sh \
    /tmp/my-explainer/index.html \
    /tmp/my-explainer/voice/ \
    --public
```
