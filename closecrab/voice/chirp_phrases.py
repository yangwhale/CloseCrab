# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Phrase boost vocabulary for ChirpSTT speech adaptation.

These phrases get higher recognition probability when fed to Cloud Speech v2
via `RecognitionConfig.adaptation`. Tuned for the kinds of terms that come
up in CloseCrab voice chats: AI products, ML infrastructure, the bot's own
domain names, and a few Chinese terms STT routinely flubs.

Cloud Speech v2 adaptation caps:
  - 1,200 phrases per inline PhraseSet
  - 100 characters per phrase
  - 100,000 total characters per request
  - Boost in (0, 20]

We aim for ~450 phrases — well under the 1,200 ceiling, leaving headroom
for per-bot custom additions, but big enough to cover the AI/infra terms
that come up routinely in voice chats with this user.

Boost values: 18 = critical proper nouns (Higcp, CloseCrab, Opus —
absolutely cannot be misheard), 16 = strong (DeepSeek, Gemini, Claude),
unset = PhraseSet default (set to 10 in chirp_stt.py). Going above 16 for
common words causes false positives where unrelated audio gets transcribed
as the boosted phrase.
"""

from __future__ import annotations

# Phrases grouped by category for maintainability. Order doesn't matter to
# the API — Cloud Speech treats them as a flat set. Duplicates are deduped
# in default_phrases() so it's OK if a word appears in two groups.

# --- AI products & models (international) -----------------------------------
_PRODUCTS_AI_INTL = [
    # Google / DeepMind
    "Gemini", "Gemini Pro", "Gemini Flash", "Gemini Live", "Gemini Ultra",
    "Gemini 2", "Gemini 2.5", "Gemini 3", "Gemini 3 Pro", "Gemini 3 Flash",
    "Vertex AI", "Vertex", "Gemma", "Gemma 4", "Imagen", "NotebookLM",
    "Veo 3", "Hunyuan Video",
    # Anthropic
    "Claude", "Claude Code", "Claude Opus", "Claude Sonnet", "Claude Haiku",
    "Claude Opus 4.7", "Claude Sonnet 4.6", "Claude Haiku 4.5",
    "Anthropic", "Opus", "Sonnet", "Haiku", "Artifacts",
    "Computer Use", "Constitutional AI",
    # OpenAI
    "OpenAI", "ChatGPT", "GPT-4", "GPT-4o", "GPT-4o realtime", "GPT-4 Turbo",
    "GPT-5", "o1", "o3", "o3-mini", "o4", "Codex", "Whisper", "Sora",
    "realtime API", "advanced voice mode",
    # Meta
    "Llama", "Llama 3", "Llama 3.1", "Llama 4", "Llama Nemotron",
    # Mistral / xAI
    "Mistral", "Mixtral", "Cohere",
    "Grok", "Grok 4", "xAI",
    # Image / video / audio
    "Stable Diffusion", "SD3", "Midjourney", "Flux", "FLUX.1",
    "Runway", "Pika", "Luma", "Kling", "可灵",
    "ElevenLabs", "Eleven Labs", "Suno", "Udio", "voice clone",
    # Coding agents (2025-2026 hot category)
    # Note: Cline removed — too confusable with "client" even with high boost.
    "Devin", "Aider", "Bolt", "Replit Agent",
    "Cursor", "Cursor Composer", "Windsurf",
    # Others
    "Perplexity", "Mamba 2",
]

# --- AI products & models (中国) -------------------------------------------
_PRODUCTS_AI_CN = [
    # DeepSeek
    "DeepSeek", "DeepSeek V2", "DeepSeek V3", "DeepSeek V4",
    "DeepSeek R1", "DeepSeek R2", "DeepSeek-Coder", "DeepSeek-Math",
    # Moonshot / Kimi
    "Moonshot", "月之暗面", "Kimi", "Kimi K2", "Kimi K2.5", "Kimi 探索版",
    # StepFun
    "StepFun", "阶跃星辰", "Step-2", "Step-1V",
    # MiniMax
    "MiniMax", "MiniMax M1", "海螺", "海螺 AI", "MiniMax-Text-01",
    # 智谱 / GLM
    "智谱", "智谱清言", "GLM-4", "GLM-4.5", "GLM-4.6", "GLM 4.6", "ChatGLM",
    # Alibaba / Qwen
    "通义千问", "Qwen", "Qwen2", "Qwen3", "Qwen3 Max", "Qwen Max", "Qwen Plus",
    "QwQ", "Qwen-VL", "通义万相", "Wan 2.1",
    # ByteDance / Doubao
    "豆包", "Doubao", "Doubao 1.5 Pro", "Doubao 1.6", "Seed", "Seed-1.5",
    # Tencent
    "Hunyuan", "混元",
    # Baidu
    "文心一言", "文心", "ERNIE", "ERNIE 4.5",
    # Tencent
    "混元", "Hunyuan", "Hunyuan-T1",
    # 商汤 / 第四范式 / 百川 / 零一万物 / 面壁 / 阶跃
    "商汤", "SenseTime", "日日新",
    "百川", "Baichuan",
    "零一万物", "Yi 1.5", "Yi-Lightning",
    "面壁智能", "MiniCPM",
    # Other CN models
    "百灵", "ALModel", "Ling 2.5", "Wan 2.1", "万象",
    "Skywork", "Skywork-MoE",
]

# --- AI labs / 公司 ---------------------------------------------------------
# Removed: Inflection/Adept/Character AI (defunct/marginal), Snowflake
# (common English word), JD (too short ambiguous), Lenovo (rare in voice),
# HiSilicon (rare), OPPO/vivo (rare in tech context).
_COMPANIES = [
    # Big tech
    "Google", "Microsoft", "Amazon", "Apple", "Meta", "NVIDIA", "Nvidia",
    "AMD", "Intel", "ARM", "Qualcomm",
    # AI labs
    "OpenAI", "Anthropic", "DeepMind", "Mistral AI", "Cohere", "xAI",
    "Stability AI",
    # Cloud / 工具
    "GCP", "Google Cloud", "AWS", "Azure", "Cloudflare", "Vercel", "Netlify",
    "Hugging Face", "Databricks", "Replicate",
    # 中国
    "字节", "字节跳动", "ByteDance",
    "阿里", "阿里巴巴", "Alibaba",
    "腾讯", "Tencent",
    "百度", "Baidu",
    "美团", "Meituan",
    "华为", "Huawei",
    "小米", "Xiaomi",
    "中科院", "清华大学", "北大",
]

# --- 人名 (大佬 / 用户常提) -----------------------------------------------
_PEOPLE = [
    # AI 圈
    "Karpathy", "Andrej Karpathy", "Andrej",
    "Sam Altman", "Greg Brockman", "Ilya Sutskever", "Mira Murati",
    "Dario Amodei", "Daniela Amodei",
    "Demis Hassabis", "Jeff Dean", "Sundar Pichai",
    "Yann LeCun", "Geoffrey Hinton", "Yoshua Bengio",
    "Andrew Ng", "Fei-Fei Li", "李飞飞",
    # Hardware
    "Jensen Huang", "黄仁勋", "Lisa Su", "苏姿丰",
    # CN founders
    "梁文锋", "Liang Wenfeng",
    "杨植麟", "Yang Zhilin",
    "王小川", "Wang Xiaochuan",
    "李彦宏",
    "马云", "马斯克", "Elon Musk",
    "扎克伯格", "Mark Zuckerberg",
]

# --- 开发工具 / IDE / CLI ---------------------------------------------------
_DEV_TOOLS = [
    # VCS
    "git", "GitHub", "GitLab", "Bitbucket", "PR", "MR", "commit", "rebase",
    "merge", "branch", "fork", "pull request",
    # IDE / editor
    "VS Code", "Cursor", "Neovim", "Vim", "JetBrains", "PyCharm", "IntelliJ",
    "Sublime", "Zed",
    # Package managers
    "npm", "pnpm", "yarn", "Bun", "pip", "uv", "conda", "poetry",
    # JS runtimes / build tools (2025-2026 hot)
    "Deno", "Tauri", "Electron", "Vite", "Turbopack",
    # Python formatters / linters
    "Ruff", "Black", "isort", "mypy", "pyright",
    # Container / orchestration
    "Docker", "Kubernetes", "k8s", "Helm", "Istio", "Envoy",
    # Compute / scheduling
    "Slurm", "Ray", "DeepSpeed", "Accelerate",
    # OS
    "Linux", "Ubuntu", "Debian", "gLinux", "macOS", "Mac", "Windows", "WSL",
    # Languages / runtimes
    "Python", "Rust", "Go", "TypeScript", "JavaScript", "Java", "C++",
    "Node.js", "Deno",
    # CLI tools
    "tmux", "ssh", "curl", "ripgrep", "fzf", "jq",
]

# --- 协议 / 标准 / 网络 ----------------------------------------------------
_PROTOCOLS = [
    "HTTP", "HTTPS", "WebSocket", "gRPC", "REST", "GraphQL",
    "OAuth", "JWT", "SSL", "TLS",
    "TCP", "UDP", "IP", "DNS", "CIDR",
    "SDP", "RTC", "SIP", "SRTP",
    "JSON", "YAML", "TOML", "Protobuf",
    "S3", "GCS", "HDFS",
    # MCP / ACP (CloseCrab 内部)
    "MCP", "ACP",
]

# --- 云 / 基础设施 ---------------------------------------------------------
_CLOUD_INFRA = [
    # GCP
    "GCP", "Google Cloud", "Vertex AI", "Cloud Run", "Cloud Build",
    "Cloud Storage", "GCS", "BigQuery", "Firestore", "Pub/Sub",
    "GKE", "GCE", "Compute Engine", "Kubernetes Engine",
    "IAM", "Workload Identity", "Service Account",
    # AWS
    "AWS", "EC2", "S3", "Lambda", "DynamoDB", "RDS",
    # Azure
    "Azure",
    # CDN / 边缘
    "Cloudflare", "Fastly", "Vercel", "Netlify",
    # 监控 / 日志
    "Grafana", "Prometheus", "Datadog", "Sentry",
    # 数据库
    "PostgreSQL", "MySQL", "Redis", "MongoDB",
    "Pinecone", "Qdrant", "Weaviate", "pgvector",
]

# --- 通讯 / 平台 ------------------------------------------------------------
_PLATFORMS = [
    "飞书", "Lark",
    "钉钉", "DingTalk",
    "微信", "WeChat",
    "QQ", "Telegram", "Slack", "Discord", "WhatsApp", "Signal",
    "Twitter", "X", "Bluesky", "Mastodon",
    "公众号", "小程序",
    "Notion", "Obsidian", "Linear", "Jira",
    "Zoom", "Google Meet", "Teams",
]

# --- AI / ML 框架 + 系统 ---------------------------------------------------
_PRODUCTS_INFRA = [
    # Speech / voice
    "Chirp", "Chirp 3", "Chirp 2", "TTS", "STT",
    "LiveKit", "WebRTC", "Silero", "VAD",
    # Cloud
    "Firestore", "BigQuery", "Cloud Run", "GKE", "GCS",
    "Cloud Storage", "Kubernetes", "Pub/Sub",
    # Training frameworks
    "MaxText", "JAX", "PyTorch", "TensorFlow", "Keras",
    "Megatron", "Megatron-LM", "DeepSpeed", "Ray", "FairScale",
    "Pathways", "Pallas", "XLA", "HLO",
    # Inference / serving
    "vLLM", "SGLang", "Triton", "TensorRT", "TensorRT-LLM",
    "llama.cpp", "Ollama", "LMDeploy",
    # Inference techniques (2025-2026 hot)
    "speculative decoding", "continuous batching", "prefix caching",
    "PagedAttention", "Paged Attention", "EAGLE", "Medusa",
    # GPU clouds
    "Modal Labs", "Together AI", "Fireworks", "Anyscale",
    "Lambda Labs", "RunPod", "Vast.ai",
    # Storage / IO
    "Orbax", "Grain", "Zarr3", "Zarr", "OCDBT", "Parquet", "Arrow",
    "Hugging Face", "Datasets",
    # Tools / utilities
    "Argus", "XProf", "xpk",
    # Editor / agent frameworks (skipped Aider/Continue/Cline — common English)
    "LangChain", "LlamaIndex",
    "Cursor",
    # CloseCrab specific
    "CloseCrab", "Higcp", "OpenClaw", "Kilo", "GBrain",
    "GeminiSTT", "GeminiTTS", "GeminiACP", "ClaudeCodeWorker",
    # Bot names
    "天猫精灵", "小爱同学",
    "jarvis", "Jarvis", "hulk", "Hulk", "tommy", "Tommy",
    "bunny", "Bunny", "tianmaojingling",
]

# --- 硬件 ---------------------------------------------------------------
_HARDWARE = [
    # 加速器
    "TPU", "GPU", "NVIDIA", "Nvidia", "AMD", "Instinct",
    "TPU v4", "TPU v5e", "TPU v5p", "TPU v6e", "TPU v7",
    "Trillium", "Ironwood", "SparseCore",
    "A100", "H100", "H200", "B100", "B200", "GB200", "GH200",
    "L40", "L40S", "RTX PRO 6000", "RTX 4090", "RTX 5090",
    "MI300", "MI325", "MI350",
    # 内存
    "HBM", "HBM2", "HBM2e", "HBM3", "HBM3e", "HBM4",
    "VMEM", "MXU", "TensorCore",
    # 互联
    "RDMA", "NCCL", "InfiniBand", "NVLink", "NVSwitch", "PCIe",
    "ICI", "DCN", "RoCE", "Ethernet",
    # CPU (skipped "Grace" — common English name)
    "Xeon", "EPYC", "ARM", "Graviton",
    # Architecture codenames (2025-2026)
    "Hopper", "Blackwell", "Rubin", "Vera Rubin",
    "MI300", "MI300X", "MI325", "MI350", "MI355X",
    "GB200 NVL72",
]

# --- ML 术语 ------------------------------------------------------------
_ML_JARGON = [
    # Reasoning / 2025-2026 hot
    "test time compute", "test-time compute", "inference time scaling",
    "chain of thought", "CoT", "majority voting", "self consistency",
    "reasoning effort", "thinking budget",
    "DualPipe", "RLVR",
    # Parallelism
    "MoE", "FSDP", "ZeRO",
    "Tensor Parallelism", "Pipeline Parallelism",
    "Expert Parallelism", "Sequence Parallelism", "Context Parallelism",
    "Data Parallelism",
    "all-reduce", "all-gather", "reduce-scatter", "broadcast",
    "TP", "PP", "EP", "CP", "DP", "SP", "SPMD", "GSPMD",
    "ring all-reduce", "tree all-reduce",
    # Architecture
    "Attention", "Flash Attention", "Splash Attention",
    "Sliding Window Attention", "SWA",
    "RoPE", "MLA", "GQA", "MQA", "KV Cache", "KV cache",
    "Sparse Attention", "DSA", "Hybrid Attention", "GDN",
    "Mixture of Depths",
    "fine grained expert", "coarse grained expert",
    "细粒度", "粗粒度", "细粒度路由", "粗粒度路由",
    "Ring Attention", "state space model", "SSM",
    "Transformer", "DiT", "Diffusion", "U-Net",
    "SwiGLU", "GELU", "ReLU", "Softmax", "LayerNorm", "RMSNorm",
    # Precision / quantization
    "bf16", "fp16", "fp32", "fp8", "fp4", "int8", "int4",
    "Mixed Precision", "Quantization", "QAT", "GPTQ", "AWQ",
    # microscaling removed: boost caused ON to translate to "微缩放" Chinese.
    "SmoothQuant", "MXFP4", "NVFP4", "GGUF",
    "FP8 量化", "INT4 量化", "AWQ 量化",
    # Training
    "REMAT", "Gradient Checkpointing", "Gradient Accumulation",
    "Gradient Clipping", "Loss Scaling",
    # Optimizers (skipped Lion/Shampoo — common English words)
    "AdamW", "Adam", "Muon",
    "LoRA", "QLoRA", "Prefix Tuning",
    "RLHF", "DPO", "GRPO", "PPO", "SFT", "CPT",
    "Knowledge Distillation", "Teacher", "Student",
    "Pre-training", "Post-training", "Fine-tuning", "Alignment",
    # Inference
    "MFU", "TFLOPS", "TOPS", "prefill", "decode",
    "context window", "context length", "speculative decoding",
    "continuous batching", "PagedAttention",
    "prompt cache", "KV cache offload",
    # Eval / benchmarks
    "MMLU", "HumanEval", "SWE-bench", "SWE bench", "SWE bench verified",
    "GPQA", "GPQA Diamond", "AIME", "AIME 2025", "MATH",
    "Aider polyglot", "RULER", "needle in a haystack",
    "benchmark", "eval", "leaderboard",
    # Agent / tool
    "agent", "tool use", "function call", "reasoning",
    "Sycophancy",
    # RAG / retrieval
    "RAG", "retrieval", "embedding", "vector db",
    "BGE M3", "BGE-M3", "ColBERT", "GraphRAG", "BM25",
    "dense retrieval", "hybrid search", "late interaction",
    # Tokenization
    "tokenizer", "token", "BPE", "Tiktoken", "SentencePiece",
    # Compilation
    "JIT", "AOT", "kernel fusion", "graph compilation",
]

# --- 中文易错词 + 用户常用词 ----------------------------------------------
_COMMON_CN = [
    # Words STT flubbed in earlier tests + likely future flubs
    "粤海街道", "腾讯滨海大厦", "深圳南山区", "南山区",
    "公众号", "小程序",
    "回话", "排查", "整不会了", "卧槽",
    # 项目 / 工作流相关
    "部署", "上线", "回滚", "灰度", "压测",
    "健康检查", "烟测", "smoke test",
    "定时任务", "Cron", "调度",
    "鉴权", "白名单", "限流", "熔断",
    # AI 相关中文
    "大模型", "小模型", "推理", "训练", "微调", "对齐",
    "上下文", "上下文窗口", "提示词", "系统提示",
    "幻觉", "涌现", "思维链", "工具调用",
    # User domain
    "cc.higcp.com", "higcp.com", "wiki.higcp.com",
    # CloseCrab 配置 / 操作相关
    "Firestore", "LiveKit", "ACP", "MCP",
]

# Phrase → boost override. Anything not in this dict falls back to the
# PhraseSet default (set in chirp_stt.py). Override here when you want a
# proper noun to win against a homophone (e.g. "Opus" vs common English).
_PER_PHRASE_BOOST: dict[str, float] = {
    # CloseCrab proper nouns — absolutely cannot be misheard
    "Higcp": 18.0,
    "CloseCrab": 18.0,
    "OpenClaw": 17.0,
    "GBrain": 17.0,
    "cc.higcp.com": 18.0,
    "higcp.com": 18.0,
    "wiki.higcp.com": 17.0,
    # AI models / companies user references constantly
    "Opus": 18.0,
    "Claude Opus": 17.0,
    "Sonnet": 16.0,
    "Haiku": 16.0,
    "Gemini": 16.0,
    "Claude": 16.0,
    "Anthropic": 16.0,
    "DeepSeek": 20.0,         # max boost — was getting parsed as "第四个" in fast Chinese speech
    "DeepSeek V3": 20.0,
    "DeepSeek V4": 20.0,
    "DeepSeek R1": 20.0,
    "DeepSeek R2": 20.0,
    "Kimi": 16.0,
    "Moonshot": 16.0,
    "MiniMax": 15.0,
    # 中文地标 (homophone-prone)
    "粤海街道": 16.0,
    # Voice infra
    "LiveKit": 16.0,
    "Firestore": 15.0,
    "Chirp": 15.0,
    # People (user mentions Karpathy a lot)
    "Karpathy": 16.0,
    "梁文锋": 15.0,
    "杨植麟": 15.0,
    "黄仁勋": 15.0,
    # Words easily hijacked by similar-sounding English
    "llama.cpp": 17.0,        # was getting "拉玛点 CPP"
    "QwQ": 17.0,              # was getting "QQQ"
    "Devin": 16.0,            # was getting "Devon"
    "Bun": 18.0,              # was getting "半" — needs high boost
    "Tauri": 15.0,            # was getting "Tory"
    "Ruff": 15.0,             # was getting "Rough"
    "computer use": 15.0,
    "DualPipe": 16.0,
    "Wan 2.1": 15.0,          # 万象 model
    "Hunyuan": 15.0,
    "Veo 3": 16.0,
    "ElevenLabs": 16.0,       # was getting "11 Labs"
    "Modal Labs": 15.0,
    "Bolt": 14.0,
}


def default_phrases() -> list[tuple[str, float | None]]:
    """Return the built-in phrase list as (value, boost_override) tuples.

    boost_override is None for phrases that use the PhraseSet-level default.
    Duplicates across categories are deduped (first-occurrence wins).
    """
    seen: set[str] = set()
    out: list[tuple[str, float | None]] = []
    for group in (
        _PRODUCTS_AI_INTL,
        _PRODUCTS_AI_CN,
        _COMPANIES,
        _PEOPLE,
        _DEV_TOOLS,
        _PROTOCOLS,
        _CLOUD_INFRA,
        _PLATFORMS,
        _PRODUCTS_INFRA,
        _HARDWARE,
        _ML_JARGON,
        _COMMON_CN,
    ):
        for p in group:
            if p in seen:
                continue
            seen.add(p)
            out.append((p, _PER_PHRASE_BOOST.get(p)))
    return out
