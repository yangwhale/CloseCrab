#!/usr/bin/env python3
"""CC Wiki v2 深层概念树索引生成器（Hierarchical Taxonomy + Mermaid Mindmap）。

手工设计的多层语义骨架（TAXONOMY）+ 递归渲染：
- content/index.md            首页（全库总览脑图 + 10 领域卡片 + 类型计数 + 探索方式）
- content/topics/topic-*.md   每个一级领域一个 hub（领域脑图 + 折叠概念树 + source 聚合 + 返回首页）

设计权衡：
- 骨架（概念层级）= 手工。语义层级（KDA is-a 线性注意力 is-a 注意力）无法从扁平 tag 推断。
- 叶子文档 = 半自动。concept/entity 用精确 slug 归类（语义准确 + 0 死链）；source 文档每领域底部按 tag 自动聚合（保留自我生长）。
- 同概念跨域复现刻意保留（flash-attention 同时在 架构→注意力 和 推理→注意力优化）。

幂等：每次 build 前跑，全量重算。渲染器只 emit 真实存在的 slug，故 0 死链。
未归类的 concept/entity 在 build 时打印审计清单，提醒补 taxonomy。
"""
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CONTENT = REPO / "content"
TOPICS = CONTENT / "topics"

# ---------------------------------------------------------------------------
# 深层概念树（手工骨架）。
# 节点 = dict: {name, [slugs], [children], [tags]}
#   slugs    : 叶子，精确绑定 concept/entity 页（渲染器跳过不存在的，保证 0 死链）
#   children : 子分类，递归
#   tags     : 该节点（通常一级领域）用于在 hub 底部自动聚合 source 文档
# 一级领域额外带 key / emoji / intro / source_tags。
# ---------------------------------------------------------------------------
TAXONOMY = [
    {
        "key": "architecture", "emoji": "🧠", "name": "模型架构 Architecture",
        "intro": "注意力 / FFN / MoE 等结构组件——模型「长什么样」。",
        "source_tags": ["attention", "moe", "architecture", "linear-attention",
                        "hybrid-attention", "mla", "flash-attention", "deltanet"],
        "children": [
            {"name": "注意力 Attention", "children": [
                {"name": "全注意力 Full", "slugs": ["mla", "gated-mla", "flash-attention", "attention-sink", "qk-clip", "rope"]},
                {"name": "稀疏注意力 Sparse", "slugs": ["swa", "sparse-attention", "dsa"]},
                {"name": "线性注意力 Linear", "slugs": ["linear-attention", "deltanet", "gdn", "kda", "fla-library"]},
                {"name": "混合注意力 Hybrid", "slugs": ["hybrid-attention", "pr-2366-hybrid-kv-cache-fix"]},
            ]},
            {"name": "FFN / MoE", "children": [
                {"name": "MoE 核心", "slugs": ["moe", "megablocks"]},
                {"name": "路由与负载均衡", "slugs": ["eplb", "noaux-tc"]},
                {"name": "激活函数", "slugs": ["swiglu"]},
            ]},
            {"name": "解码与预测", "slugs": ["mtp"]},
        ],
    },
    {
        "key": "training", "emoji": "🎯", "name": "训练 Training",
        "intro": "并行策略、优化器、精度量化、后训练、显存优化、Checkpoint。",
        "source_tags": ["training", "parallelism", "fsdp", "precision", "fp8", "fp4",
                        "quantization", "optimizer", "sft", "rl-scaling", "checkpoint"],
        "children": [
            {"name": "并行策略 Parallelism", "children": [
                {"name": "数据并行", "slugs": ["fsdp", "2d-fsdp"]},
                {"name": "模型并行", "slugs": ["tensor-parallelism", "pipeline-parallelism"]},
                {"name": "序列与上下文并行", "slugs": ["sequence-parallelism", "context-parallelism"]},
                {"name": "专家并行", "slugs": ["expert-parallelism", "eplb"]},
                {"name": "综合与拓扑", "slugs": ["tp-pp-cp-ep", "cross-dcn-dp", "topology-aware-distributed", "partition-spec", "all-gather", "spmd-gspmd"]},
            ]},
            {"name": "优化器 Optimizer", "slugs": ["muon-optimizer", "adamw"]},
            {"name": "精度与量化", "children": [
                {"name": "低精度训练", "slugs": ["fp8", "fp4-quantization", "mixed-precision"]},
                {"name": "量化方法", "slugs": ["qat"]},
                {"name": "数值稳定与调试", "slugs": ["loss-scaling", "gradient-clipping", "matmul-precision", "subnormal-flush", "precision-alignment", "tpu-precision-debug", "detect-special-fp"]},
            ]},
            {"name": "后训练与对齐", "slugs": ["cpt-sft", "sft", "dpo-grpo", "lora", "knowledge-distillation", "logit-kl"]},
            {"name": "显存与计算优化", "slugs": ["remat", "host-offload", "gradient-accumulation", "sequence-packing", "scan-layers"]},
            {"name": "Checkpoint", "slugs": ["shape-mismatch", "orbax", "zarr-ocdbt", "grain"]},
        ],
    },
    {
        "key": "inference", "emoji": "⚡", "name": "推理 Inference",
        "intro": "KV Cache、注意力优化（复用架构）、推理引擎、量化推理。",
        "source_tags": ["inference", "vllm", "sglang", "kv-cache", "serving", "decode"],
        "children": [
            {"name": "KV Cache", "slugs": ["kv-cache", "pr-2366-hybrid-kv-cache-fix"]},
            {"name": "注意力优化（复用架构）", "slugs": ["flash-attention", "swa", "dsa", "mla", "gated-mla"]},
            {"name": "推理引擎", "slugs": ["vllm", "sglang"]},
            {"name": "量化推理", "slugs": ["fp4-quantization", "qat"]},
        ],
    },
    {
        "key": "hardware", "emoji": "🔧", "name": "硬件 Hardware",
        "intro": "TPU / GPU 芯片规格、计算内存、网络互联拓扑。",
        "source_tags": ["tpu", "gpu", "nvidia", "networking", "ici-dcn", "3d-torus", "hardware"],
        "children": [
            {"name": "TPU", "children": [
                {"name": "芯片型号", "slugs": ["tpu-v7", "tpu-v6e", "tpu-v5p"]},
                {"name": "计算与内存", "slugs": ["mxu", "hbm-vmem", "tflops", "sparsecore"]},
            ]},
            {"name": "GPU", "slugs": ["a100", "h200", "b200", "rtx-pro-6000"]},
            {"name": "网络互联", "slugs": ["ici-dcn", "3d-torus", "cross-dcn-dp"]},
        ],
    },
    {
        "key": "frameworks", "emoji": "🛠️", "name": "框架与工具 Frameworks",
        "intro": "JAX / XLA / Pallas / Pathways 软件栈、编译运行时、集群工具。",
        "source_tags": ["jax", "xla", "maxtext", "pathways", "pallas", "megatron", "framework", "xpk", "xprof"],
        "children": [
            {"name": "核心框架", "slugs": ["jax", "maxtext", "pathways", "megatron", "pallas"]},
            {"name": "分布式与初始化", "slugs": ["jax-distributed-init", "spmd-gspmd"]},
            {"name": "编译与运行时", "slugs": ["xla", "jit-xla", "hlo", "libtpu"]},
            {"name": "数据与 Checkpoint", "slugs": ["grain", "orbax", "zarr-ocdbt"]},
            {"name": "集群工具", "slugs": ["xpk", "xprof", "maxtext-aot-precompile"]},
        ],
    },
    {
        "key": "platform", "emoji": "☁️", "name": "平台与运维 Platform",
        "intro": "GKE 编排、诊断监控、模型迁移与基础设施。",
        "source_tags": ["gke", "gitops", "infra", "migration", "kueue", "mldiagnostics", "platform"],
        "children": [
            {"name": "GKE / 编排", "slugs": ["gke", "gitops-tpu-deploy"]},
            {"name": "诊断与监控", "slugs": ["mldiagnostics"]},
            {"name": "迁移", "slugs": ["model-porting-methodology"]},
        ],
    },
    {
        "key": "ant-tpu", "emoji": "🐜", "name": "蚂蚁 TPU 项目",
        "intro": "蚂蚁集团 TPU 迁移项目的团队、模型与专项技术。",
        "source_tags": ["ant-tpu", "almodel", "cross-dcn", "ling", "bailing"],
        "children": [
            {"name": "项目与团队", "slugs": ["ant-tpu-project", "ant-group", "almodel"]},
            {"name": "模型", "slugs": ["ling-v3", "ling3-flash", "ling-25", "bailing-moe-v3", "mimo-v2"]},
            {"name": "专项技术", "slugs": ["cross-dcn-dp", "mldiagnostics"]},
        ],
    },
    {
        "key": "models", "emoji": "📦", "name": "模型家族 Model Families",
        "intro": "DeepSeek / Qwen / Kimi 等开源模型家族。",
        "source_tags": ["deepseek", "qwen", "kimi", "gemma", "model-family", "llm"],
        "children": [
            {"name": "DeepSeek", "slugs": ["deepseek-v3", "deepseek-v4"]},
            {"name": "Qwen", "slugs": ["qwen3", "qwen3-next"]},
            {"name": "其它开源", "slugs": ["kimi-k25", "gemma-4", "mimo-v2", "ling-25"]},
        ],
    },
    {
        "key": "diffusion", "emoji": "🎨", "name": "扩散与视频生成",
        "intro": "Diffusion / 视频生成模型与架构（DiT / S3Diff / Wan / Flux）。",
        "source_tags": ["diffusion", "video-generation", "dit", "s3diff", "wan", "flux", "cogvideox"],
        "children": [
            {"name": "架构", "slugs": ["dit"]},
            {"name": "模型", "slugs": ["s3diff", "wan21", "flux", "cogvideox"]},
        ],
    },
    {
        "key": "methodology", "emoji": "📚", "name": "方法论与诊断",
        "intro": "迁移方法论、性能 Profiling、知识管理。",
        "source_tags": ["methodology", "guide", "comparison", "benchmark", "profiling", "debugging", "rag", "memex"],
        "children": [
            {"name": "迁移方法论", "slugs": ["model-porting-methodology", "precision-alignment"]},
            {"name": "性能与 Profiling", "slugs": ["mfu", "tflops", "xprof"]},
            {"name": "知识管理", "slugs": ["knowledge-compounding", "rag", "memex"]},
        ],
    },
]

TYPE_HUBS = [
    ("sources", "Sources 来源摘要"),
    ("entities", "Entities 实体"),
    ("concepts", "Concepts 概念"),
    ("analyses", "Analyses 分析对比"),
]

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Quartz 的 wikilink 正则禁止 alias 含 # | [ ]，否则整条不解析、残留成字面文本。
_ALIAS_TRANS = str.maketrans({"#": "＃", "|": "/", "[": "（", "]": "）"})


def safe_alias(title: str) -> str:
    return title.translate(_ALIAS_TRANS)


# mermaid mindmap 节点文本：含 () / [] " 会被当形状语法 → 包成 id["text"] 并 sanitize。
_MERMAID_TRANS = str.maketrans({
    '"': "'", "(": "（", ")": "）", "[": "（", "]": "）",
    "{": "（", "}": "）", "/": "／", "\\": "／", "#": "＃",
    ";": "；", ":": "：",
})


def mermaid_text(s: str) -> str:
    return s.translate(_MERMAID_TRANS).strip()


def parse_page(path: Path):
    text = path.read_text(encoding="utf-8")
    m = FM_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = [tags]
    tags = [str(t).strip().lower() for t in tags if t]
    desc = (fm.get("description") or "").strip().replace("\n", " ")
    if len(desc) > 64:
        desc = desc[:62] + "…"
    return {
        "slug": path.stem,
        "title": str(fm.get("title") or path.stem).strip(),
        "tags": set(tags),
        "type": (fm.get("type") or "").strip().lower(),
        "desc": desc,
    }


def scan():
    """返回 slug -> page 索引 dict。"""
    index = {}
    for path in CONTENT.rglob("*.md"):
        rel = path.relative_to(CONTENT)
        if rel.parts and rel.parts[0] in ("topics", "tags"):
            continue
        if path.name == "index.md":
            continue
        p = parse_page(path)
        if p:
            index[p["slug"]] = p
    return index


# ---------------------------------------------------------------------------
# 树遍历工具
# ---------------------------------------------------------------------------
def iter_leaf_slugs(node):
    """递归 yield 节点子树下所有 slug（可能含不存在的）。"""
    for s in node.get("slugs", []):
        yield s
    for c in node.get("children", []):
        yield from iter_leaf_slugs(c)


def domain_existing_slugs(domain, index):
    """领域子树下真实存在的去重 slug 集合。"""
    return {s for s in iter_leaf_slugs(domain) if s in index}


# ---------------------------------------------------------------------------
# 概念树渲染（嵌套 Markdown 列表 + 折叠 callout）
# ---------------------------------------------------------------------------
def render_slug_list(slugs, index, depth, lines):
    """渲染叶子 slug 为缩进 Markdown 列表项，跳过不存在的。"""
    indent = "  " * depth
    for s in slugs:
        p = index.get(s)
        if not p:
            continue
        tail = f" — {p['desc']}" if p["desc"] else ""
        lines.append(f"{indent}- [[{s}|{safe_alias(p['title'])}]]{tail}")


def render_subtree(node, index, depth, lines):
    """渲染 callout 内部的子层（L3+）：分类名 + 嵌套列表，递归。"""
    indent = "  " * depth
    name = node["name"]
    children = node.get("children", [])
    slugs = node.get("slugs", [])
    existing = [s for s in slugs if s in index]
    if children:
        lines.append(f"{indent}- **{name}**")
        for c in children:
            render_subtree(c, index, depth + 1, lines)
    elif existing:
        lines.append(f"{indent}- **{name}**")
        render_slug_list(existing, index, depth + 1, lines)


def render_domain_tree(domain, index, lines):
    """领域 hub 主体：L2 子领域用折叠 callout，内部递归。"""
    for sub in domain["children"]:
        # 统计该子领域真实文档数
        count = len(domain_existing_slugs(sub, index))
        if count == 0:
            continue
        lines.append(f"> [!abstract]- {sub['name']} · {count} 篇")
        body = []
        sub_children = sub.get("children", [])
        sub_slugs = [s for s in sub.get("slugs", []) if s in index]
        if sub_children:
            for c in sub_children:
                render_subtree(c, index, 0, body)
        else:
            render_slug_list(sub_slugs, index, 0, body)
        for b in body:
            lines.append(f"> {b}")
        lines.append("")


# ---------------------------------------------------------------------------
# Markmap 脑图生成（横向可折叠树，markmap-lib 解析 markdown 标题 + 列表）
# 输入即 markdown：# 根 → ## 子领域 → - 组件。markmap 自带配色 + 折叠交互。
# ---------------------------------------------------------------------------
def markmap_text(s: str) -> str:
    """脑图节点文本：markmap 按 markdown 渲染，去掉可能干扰的方括号。"""
    return s.replace("[", "（").replace("]", "）").strip()


def domain_mindmap(domain, index, max_depth=3):
    """单领域脑图：领域 → 子领域 → 组件（叶子 slug 不画）。"""
    lines = ["```markmap", f'# {markmap_text(domain["name"])}', ""]
    for sub in domain["children"]:
        if len(domain_existing_slugs(sub, index)) == 0:
            continue
        lines.append(f'## {markmap_text(sub["name"])}')
        for comp in sub.get("children", []):
            if len(domain_existing_slugs(comp, index)) == 0:
                continue
            lines.append(f'- {markmap_text(comp["name"])}')
        lines.append("")
    lines.append("```")
    return "\n".join(lines)


def overview_mindmap(index, max_depth=2):
    """全库总览脑图：CC Wiki → 10 领域 → 二级子领域。"""
    lines = ["```markmap", "# CC Wiki", ""]
    for domain in TAXONOMY:
        if len(domain_existing_slugs(domain, index)) == 0:
            continue
        lines.append(f'## {markmap_text(domain["name"])}')
        for sub in domain["children"]:
            if len(domain_existing_slugs(sub, index)) == 0:
                continue
            lines.append(f'- {markmap_text(sub["name"])}')
        lines.append("")
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Source 文档聚合（自我生长部分）
# ---------------------------------------------------------------------------
def domain_sources(domain, index):
    """按 source_tags 聚合 type=source 的页面，排除已在概念树里的。"""
    tagset = set(domain.get("source_tags", []))
    if not tagset:
        return []
    tree_slugs = set(iter_leaf_slugs(domain))
    matched = [
        p for p in index.values()
        if p["type"] == "source" and (p["tags"] & tagset) and p["slug"] not in tree_slugs
    ]
    matched.sort(key=lambda p: p["title"])
    return matched


# ---------------------------------------------------------------------------
# 页面渲染
# ---------------------------------------------------------------------------
def render_topic_hub(domain, index):
    total = len(domain_existing_slugs(domain, index))
    sources = domain_sources(domain, index)
    lines = [
        "---",
        f'title: "{domain["emoji"]} {domain["name"]}"',
        f'description: "{domain["intro"]} 核心概念 {total} 篇。"',
        "type: hub",
        "tags:",
        "  - hub",
        "  - moc",
        "---",
        "",
        f"> {domain['intro']}（核心概念 **{total}** 篇，同一概念可能跨领域复现）",
        "",
        "## 🧭 领域脑图",
        "",
        domain_mindmap(domain, index),
        "",
        "## 🌲 概念树",
        "",
        "点开折叠块逐层下钻，叶子是具体技术文档。",
        "",
    ]
    render_domain_tree(domain, index, lines)

    if sources:
        lines.append(f"## 📄 相关来源文档 · {len(sources)} 篇")
        lines.append("")
        lines.append("> 按标签自动聚合的文章 / 论文 / 报告（随新 ingest 自动生长）。")
        lines.append("")
        for p in sources:
            tail = f" — {p['desc']}" if p["desc"] else ""
            lines.append(f"- [[{p['slug']}|{safe_alias(p['title'])}]]{tail}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("← 返回 [[index|Wiki 首页]] · 按类型浏览 [[sources/index|Sources]] · "
                 "[[entities/index|Entities]] · [[concepts/index|Concepts]] · [[analyses/index|Analyses]]")
    lines.append("")
    return "\n".join(lines)


def render_moc(index, type_counts):
    lines = [
        "---",
        "title: CC Wiki v2",
        'description: "AI/ML 基础设施、TPU/GPU 训练推理知识库 — 深层概念树索引"',
        "type: hub",
        "tags:",
        "  - wiki",
        "  - hub",
        "  - moc",
        "---",
        "",
        "个人知识 Wiki — AI/ML 基础设施、TPU/GPU 训练推理、模型架构。"
        "下面提供**主题 / 类型 / 标签 / 关系 / 时间**五种方式浏览全库。",
        "",
        "## 🧠 全库总览脑图",
        "",
        "一图看清 10 大领域与二级子领域；点下方卡片逐层下钻到具体技术。",
        "",
        overview_mindmap(index),
        "",
        "## 🗂️ 按主题浏览",
        "",
        "每个领域一张概念树，可逐层展开下钻（领域 → 组件 → 具体技术）。",
        "",
    ]
    for domain in TAXONOMY:
        total = len(domain_existing_slugs(domain, index))
        if total == 0:
            continue
        sub_names = []
        for sub in domain["children"]:
            c = len(domain_existing_slugs(sub, index))
            if c:
                sub_names.append(f"{sub['name']}（{c}）")
        lines.append(f"### {domain['emoji']} [[topic-{domain['key']}|{domain['name']}]] · {total} 篇")
        lines.append("")
        lines.append(domain["intro"])
        if sub_names:
            lines.append("")
            lines.append("子领域：" + " · ".join(sub_names))
        lines.append("")
        lines.append(f"[[topic-{domain['key']}|展开全部 →]]")
        lines.append("")

    lines.append("## 📁 按类型浏览")
    lines.append("")
    for key, label in TYPE_HUBS:
        count = type_counts.get(key, 0)
        lines.append(f"- [[{key}/index|{label}]] — {count} 篇")
    lines.append("")

    lines.append("## 🧭 其它探索方式")
    lines.append("")
    lines.append("- **关系图谱**：右侧 Graph 视图，看页面间 wikilink 连接。")
    lines.append("- **最近更新**：页面底部「最近更新」列最新 ingest 的文档（时间维度）。")
    lines.append("- **目录树**：左侧 Explorer 按文件夹（sources/entities/concepts/…）浏览。")
    lines.append("- **标签**：任意页面顶部 tag 点进 `/tags/<tag>`，自动汇总同标签文档。")
    lines.append("- **全文搜索**：左上角搜索框（BM25 中英混合）。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
def audit(index):
    """打印：(1) 树里引用但不存在的 slug；(2) 未进任何树节点的 concept/entity。"""
    referenced = set()
    missing = []
    for domain in TAXONOMY:
        for s in iter_leaf_slugs(domain):
            referenced.add(s)
            if s not in index:
                missing.append((domain["key"], s))

    if missing:
        print(f"[gen-moc] ⚠ {len(missing)} 个 slug 在 taxonomy 里但无对应文件（已跳过，0 死链）：")
        for key, s in missing:
            print(f"[gen-moc]     {key}: {s}")

    unclassified = sorted(
        p["slug"] for p in index.values()
        if p["type"] in ("concept", "entity")
        and not p["slug"].startswith("person-")
        and p["slug"] not in referenced
    )
    if unclassified:
        print(f"[gen-moc] ⚠ {len(unclassified)} 个 concept/entity 未进概念树（建议补 taxonomy）：")
        print("[gen-moc]     " + ", ".join(unclassified))


def main():
    index = scan()
    TOPICS.mkdir(exist_ok=True)

    written = []
    for domain in TAXONOMY:
        hub = render_topic_hub(domain, index)
        out = TOPICS / f"topic-{domain['key']}.md"
        out.write_text(hub, encoding="utf-8")
        written.append(out)

    type_counts = {}
    for key, _label in TYPE_HUBS:
        d = CONTENT / key
        type_counts[key] = len([p for p in d.glob("*.md") if p.name != "index.md"]) if d.is_dir() else 0

    moc = render_moc(index, type_counts)
    (CONTENT / "index.md").write_text(moc, encoding="utf-8")
    written.append(CONTENT / "index.md")

    print(f"[gen-moc] scanned {len(index)} pages")
    for domain in TAXONOMY:
        print(f"[gen-moc]   {domain['name']}: {len(domain_existing_slugs(domain, index))} 核心概念")
    print(f"[gen-moc] type counts: " + ", ".join(f"{k}={v}" for k, v in type_counts.items()))
    print(f"[gen-moc] wrote {len(written)} files")
    audit(index)


if __name__ == "__main__":
    main()
