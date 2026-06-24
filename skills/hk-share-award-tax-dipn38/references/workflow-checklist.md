# GSU 税务分析工作流 Checklist

## Phase 1: 材料收集

### 必需材料
- [ ] **Morgan Stanley Stock Statement (CSV)**
  - 来源：atwork.morganstanley.com → Activity → Reports → "Your Alphabet Stock Statement"
  - 设置：All available history / USD / CSV
  - 若用户已登录，可通过 Chrome MCP 自行下载

- [ ] **IR56B（各已评税年度）**
  - 来源：用户从 Payday 下载 / 直接发 PDF
  - 关键字段：item 11(k) GSU 金额、Total
  - Google 报全额（不分地域）

- [ ] **IRD 评税通知书**
  - 来源：Chrome MCP 从 itp.etax.ird.gov.hk 拿 / 用户发 PDF
  - 关键字段：本人入息总额

- [ ] **转移日期（内地→香港入职日）**
  - 来源：用户口述 / IR56B 雇佣期起始日 / Vialto 计算表
  - 可从 MS Statement 双行 lot 反推验证（46+ lot 标准差 <1 天 = 可靠）

### 可选材料
- [ ] Vialto 计算表 / objection letter（如有，用于对照验证）
- [ ] 日间联系电话（填 IR831 用）
- [ ] HKID 前 6 位（解密 Vialto 文件用）

## Phase 2: 解析 & 核算

- [ ] 跑 `parse_ms_statement.py --csv ... --transfer-date ... --output analysis.json`
- [ ] 检查输出：各税年 GSU 全额、香港来源、内地豁免
- [ ] 交叉验证：MS 全额 ≈ IR56B GSU 金额？（差异应 <2%）

## Phase 3: 对账 & 诊断

对每个已评税年度做判断：

```
IF IR56B Total = 评税入息
  → ❌ 没 claim 地域豁免（问题年度）
  → 多缴 ≈ 内地豁免 HKD × 17%

IF IR56B Total > 评税入息
  → ✅ 已 claim（差额 ≈ 内地豁免）
  → 验证差额 ≈ 独立核算的内地豁免（差异 <2% = OK）
```

## Phase 4: 生成报告

- [ ] 跑 `gen_tax_report.py` 生成 HTML
- [ ] 用 `publish-cc-page.sh` 发布到 CC Pages
- [ ] 发链接给用户

## Phase 5: 行动指导

### 情况 A — 有年度漏 claim
- [ ] 读 `references/ir831-guide.md`
- [ ] 生成 IR831 填表内容
- [ ] 生成 70A 附信草稿（填入具体金额）
- [ ] 列需附文件清单

### 情况 B — 当年未报税
- [ ] 指导 BIR60 附录第 4 部分填法
- [ ] GSU 只填香港来源金额
- [ ] 附 MS 结单作支持

### 情况 C — 全部已正确 claim
- [ ] 确认无需行动
- [ ] 提醒往后年度继续 claim

## 常见陷阱

1. **Google IR56B 报全额** — 这是最容易踩的坑，用户照抄就多缴了
2. **日期月份在 PDF 中可能丢失** — CSV 格式日期完整（中文月份），优先用 CSV
3. **美元汇率要用 IRD 官方买价** — 不是即期汇率，不是银行汇率，是 IRD 累计平均买价
4. **两行 lot = 跨境分摊** — MS 结单里同一 lot 拆两行，行 1 = 内地段，行 2 = 香港段
5. **转移前 vest 排除** — 那些 vest 时你还在内地，未计入香港税表，不是"多缴"
6. **Vialto 可能已处理** — 先查有没有税务代理帮 claim 过，避免重复
