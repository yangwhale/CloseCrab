# Cluster 合并 Batch 模板

每 cluster 4 步固定流程。**遵循 invariants.md** — 尤其是 invariant #1（内容完整复制）+ tool 用法（Write 前 Read）。

---

## Step 1: Read 所有 sub-file

```bash
cd ~/.claude/projects/-home-chrisya/memory/
for f in feedback_<prefix>-*.md; do
  echo "═══ $f ($(wc -l<$f) lines) ═══"
  cat "$f"
  echo
done > /tmp/cluster-source.txt
wc -l /tmp/cluster-source.txt  # 估算 cluster file 大小
```

**Why**：一次性拿全主题内容，便于设计 sections。

---

## Step 2: 设计 cluster file 结构

按主题逻辑分 sections，不按文件名字典序：

```
# <Cluster Name> 教训汇总

一句话总体描述（架构 / 用途 / 适用范围）

---

## A. <主题 1>

### A1. <具体规则 1>
### A2. <具体规则 2>

## B. <主题 2>

### B1. ...
```

**例**（openclaw cluster）：
```
## A. Gateway 架构
   A1. 共享 Gateway 验证
   A2. Gateway 没 watchdog
   A3. Gateway hot-reload
   A4. Tool event opaque
## B. Model 配置
   B1. 单源真理
   B2. Session sticky
## C. Worker 运行行为
   C1. Retry path parity
   C2. Daily reset + workspace
```

---

## Step 3: Write cluster file（含完整原始数据）

**关键**：覆盖已存在文件前**必须先 Read**（CC tool state），否则 Write 会失败。

```python
# 伪代码
if cluster_file.exists():
    Read(cluster_file)  # 必须，CC tool 状态要求
Write(cluster_file, content)
```

**内容原则**（遵守 invariant #1）：
- 每个 section 完整复制对应 sub-file 内容
- 保留所有数值 / 命令 / 行号 / commit hash / 时间戳
- frontmatter 写：
  ```yaml
  ---
  name: feedback_<cluster-name>
  description: <主题> 全部教训汇总 — section 列表
  metadata:
    type: feedback
  ---
  ```

---

## Step 4: Edit MEMORY.md（N 行 link → 1 行 cluster index）

```bash
# 找到原 N 行 sub-file links
grep -n "feedback_<prefix>-" ~/.claude/projects/-home-chrisya/memory/MEMORY.md
```

替换成 1 行 cluster hook（≤150 字符 + 触发关键词）：

```markdown
- [<Cluster 名> 全部教训](feedback_<cluster-name>.md) — A. <主题 1> (<关键词>) / B. <主题 2> (<关键词>) / C. <主题 3>
```

**hook 写法**：用斜杠分段列 sections + 关键触发词（不是摘要）。

---

## Step 5: Delete 原 sub-file

```bash
rm feedback_<prefix>-*.md
```

**Verify**：
```bash
ls feedback_<prefix>-* 2>/dev/null  # 应该 No such file
wc -l ~/.claude/projects/-home-chrisya/memory/MEMORY.md  # 应该减了 N-1 行
```

---

## Step 6（可选）: 跑 audit 验证

```bash
python3 ~/CloseCrab/scripts/memory-audit.py --action-only
```

**期望输出**：`🟢 actionable=0, 无需 cleanup`

如果出现 dead_index → MEMORY.md 改链时漏了；orphan 多了 → cluster file 文件名错。立即修。

---

## 全 cluster batch 模板（伪代码）

```bash
for cluster in openclaw kilo gbrain wiki bot botcore memory cold tpu; do
  echo "═══ Cluster: $cluster ═══"
  # 1. Read source
  for f in feedback_${cluster}-*.md; do cat "$f"; done > /tmp/src.txt
  # 2-3. Design + Write cluster file (manual review)
  # 4. Edit MEMORY.md (manual)
  # 5. Delete originals
  rm feedback_${cluster}-*.md
  # 6. Audit
  python3 ~/CloseCrab/scripts/memory-audit.py --action-only
done
```

**注意**：上面伪代码省略了 manual review 步骤。实际操作时**不能纯自动化** — Step 2 (设计 sections) 和 Step 3 (Write) 必须 LLM judgment，不是 mechanical replace。

---

## 失败模式 + 救援

### 失败 1: Write cluster file 但 Edit MEMORY.md 失败

**症状**：cluster file 存在，但 MEMORY.md 仍 link 原 sub-file。

**救援**：删 cluster file（避免 dup），重新做 Step 4。

### 失败 2: 删 sub-file 后才发现 cluster file 漏内容

**症状**：cluster file Read 找不到本该在的数据。

**救援**：从 git 恢复 — `cd ~/my-private && git show <last-commit>:claude-code/memory/feedback_<old>.md > /tmp/recover.md`

### 失败 3: MEMORY.md > 200 行

**症状**：合并后行数没减反增。

**救援**：检查是否有重复 link（同一 cluster file 被 link 多次）。Cross-surface link ≤2 个 ok，>2 需要重新评估。

### 失败 5: Write 报 "File has not been read yet"

**症状**：覆盖已存在文件时 CC tool 报错 `File has not been read yet. Read it first before writing to it.`

**根因**：CC tool state 要求 — 覆盖任何 disk 上已存在的文件前必须先 Read（让 tool state 记录文件指纹）。这次战役至少 3 次踩到（cert-expiry / pptx-text-in-shape / openclaw bunny shared/gcp-infra.md），每次都让 sub-file 内容暂时丢失。

**救援步骤**：
```python
# 1. Read 让 tool state 记录
Read(cluster_file)

# 2. Write 重试，这次成功
Write(cluster_file, content)

# 3. 验证内容已更新（可选）
Read(cluster_file)  # 看新内容
```

**预防模板**（写 cluster file 前显式 Read）：
```python
if cluster_file.exists():
    Read(cluster_file)  # MANDATORY before Write to existing file
Write(cluster_file, new_content)
```

**特别注意**：sync 后的 shared/X.md 也会触发这个 — 即使 file 是从 openclaw bot workspace rsync 来的，Write 前仍要 Read。

### 失败 4: 小爱回归测试 FAIL

**症状**：bot 答不出 / 答案错 / 找不到 cluster file。

**根因**：
- 索引引导失败 → MEMORY.md hook 关键词没覆盖 query
- 内容丢失 → invariant #1 违反，回 Step 3 补内容
- bot 凭脑子答 → cluster file 没真覆盖该主题，需要 sub-file 独立保留

**救援**：补 hook 关键词，或回滚 cluster 合并保留 sub-file 独立。
