# Code Wiki — Authority Boundary

Read this before citing Code Wiki content in any answer to the user.

## What Code Wiki is

An auto-generated documentation site at `codewiki.google`. Backed by Gemini Flash running over the public README + source files of selected open-source repos. Output is structured as TOC + section pages + chat. Updates on PR merge.

## What it does well

| Strength | Why |
|---|---|
| Module decomposition | Gemini reads the directory tree and groups files into coherent sections; matches a senior engineer's first-pass mental model |
| Code-pinned references | Every claim has a `github.com/{org}/{repo}/blob/{SHA}/{path}#L{line}` link, commit-pinned. Saves grep time. |
| Architecture diagrams | Auto-generated from import graph + naming patterns. Decent for "what calls what" overview. |
| Honest "don't know" | When asked something not in the docs, it says so explicitly. Does not hallucinate fake APIs. |

## What it does poorly — DO NOT cite it for these

| Weakness | Failure mode |
|---|---|
| Runtime behavior | Cannot describe what happens at execution time — e.g., "how does FSDP release all-gathered weight memory" → it answers "Trust the compiler" (a slogan, not a mechanism). |
| Optimization / performance tuning | No knowledge of profiling data, MFU, throughput characteristics. Cannot suggest flag combinations. |
| Why a design decision was made | No access to PR discussions, design docs, or maintainer reasoning. Will speculate or punt. |
| Recent unmerged work | Updates lag PR merges; nothing about open PRs or branches other than main. |
| Cross-repo interactions | Each repo is a silo; cannot reason about how MaxText + JAX + XLA interact at runtime. |
| Bugs and known issues | Doesn't read issue tracker; cannot tell you "this is broken in v0.4.2". |
| Non-public repos | Only covers selected OSS repos. No private/internal coverage. |

## Empirical evidence

Tested against Ant↔Google MaxText QA tracker (see `~/my-wiki-v2/content/sources/ant-maxtext-qa-tracker.md`):

| Q | Code Wiki answer quality |
|---|---|
| Q1: non-scan FSDP HBM release | ❌ Generic "Trust the compiler", missed the `jax.custom_vjp` solution Ran Ran gave |
| Q2: Muon 5 all-reduce overlap | ❌ Would not know (runtime optimizer behavior, no docs) |
| Q4: 100B MoE 24% MFU diagnosis | ❌ Cannot diagnose (needs xprof + flag-level knowledge) |
| Q5: Memory Viewer metrics | ❌ Cannot teach tool usage |
| (Hypothetical) Architecture overview of MaxText | ✅ Would nail this — sections, supported models, multimodal flow, etc. |

## Behavioral rule

When using cache content from this skill:

1. **For "what's in the codebase" / "how is X organized" questions** — cite freely, link to the cache + the Code Wiki page.
2. **For "how does X actually work at runtime" / "how do I fix Y" questions** — drop the cache. `Read` the actual source files. The cache is a starting point, not the answer.
3. **Never paraphrase Code Wiki as if it were an expert opinion.** Attribute: "according to Code Wiki's auto-generated docs, ..." or "the Gemini-generated wiki summarizes this as ...".
4. **If the user is about to act on a Code Wiki claim** (run a command, edit code, file a bug) — verify in source first.

The authority warning in every cache file is a contract. Honor it.
