---
name: hk-share-award-tax-dipn38
description: "Compute Hong Kong salaries tax partial exemption for share awards (GSU / RSU / stock options) under DIPN No. 38 paragraphs 43-46 and section 8(1A)(a) of the Inland Revenue Ordinance. Full lifecycle: (1) auto-extract vest data from Morgan Stanley / IRD / IR56B via Chrome MCP, (2) compute DIPN 38 time apportionment, (3) cross-check IR56B vs IRD assessment to detect unclaimed exemptions, (4) generate APPENDIX-EQUITY PDF for eTAX submission, (5) generate HTML audit report for personal review, (6) guide eTAX BIR60 filing or 70A retrospective claim. Triggers: HK salaries tax, BIR60, IR56B, share award apportionment, DIPN 38, 'partial exemption', cross-border GSU/RSU vest, '报税分析', '股票减税', 'GSU 豁免', '从内地转来香港', '70A', 'time apportionment', '股票报税'."
---

# Hong Kong Share Award Tax — DIPN 38 Partial Exemption

When a Hong Kong taxpayer transfers from a non-HK employer to a HK employer mid-vesting, share awards granted under the non-HK employment are only partially taxable in HK. This skill produces:

1. An **IR56B vs Assessment cross-check** — detect which tax years have unclaimed exemptions
2. A **DIPN 38 computation PDF** in the IRD-accepted "APPENDIX-EQUITY" format
3. An **HTML audit report** for personal review (CC Pages)
4. The **eTAX BIR60 Appendix Section 4** filing guidance
5. An **IR831 / Section 70A** retrospective claim package (for years where exemption was missed)

---

## When to trigger

Strong triggers:
- User mentions HK salaries tax + share awards / GSU / RSU / stock options
- User mentions "DIPN 38" / "partial exemption" / "section 8(1A)(a)"
- User mentions transfer between non-HK and HK entities (Google Shanghai → Google HK, etc.)
- User uploads a stock statement / IR56B / IRD assessment and asks about HK tax
- Chinese: "报税分析", "股票减税", "GSU 豁免", "地域来源", "从内地转来香港", "70A", "股票报税"

Do NOT use for:
- Non-HK tax jurisdictions
- Pure HK employment with no cross-border element
- Carried Interest (BIRSP4 — different form)

---

## Core algorithm

Legal basis: **DIPN No. 38 ¶43–46** + **section 8(1A)(a) IRO**.

```
                                    Days in HK during vesting period
Net Assessable HKD = Net Amount × ─────────────────────────────────────
                                    Total days in vesting period
```

Where:
- Total days = `vest_date − grant_date` (Python `.days`)
- Outside-HK days = `hk_employment_start − grant_date` (when grant < hk_start)
- HK days = total − outside

**Decision rules**:
| Grant date | Treatment |
|---|---|
| Before HK employment start | Apportion (this skill's main work) |
| On or after HK employment start | 100% assessable (no apportionment) |

**Critical data rules** (verified against IRD-accepted filings):
- Use **`Total Income of GSUs at Vest`**, NOT `Prorated Income` (Prorated is employer-internal)
- Use **HK jurisdiction row's FX rate** (~7.7), NOT China row's PBOC rate (~7.1)
- Use **floor()** (truncate) for final integer, not round() — IRD convention
- Google IR56B reports GSU at **full amount** (no geographic split) — taxpayer must self-claim

---

## Workflow

### Phase 1: Gather inputs

Collect via dialogue; auto-fetch where possible:

| Material | Source | Required |
|---|---|---|
| Morgan Stanley Stock Statement | User sends PDF/CSV, or Chrome MCP from atwork.morganstanley.com | **Yes** |
| IR56B (each tax year) | User sends PDF, or Payday system | **Yes** |
| IRD Assessment notices | Chrome MCP from itp.etax.ird.gov.hk, or user sends PDF | **Yes** |
| HK employment start date | User states / IR56B period / Vialto docs | **Yes** |
| Taxpayer name & IRD file no. | IR56B / assessment notice | **Yes** |
| Phone number | User states (for IR831) | Only for 70A |
| Previous year's tax return | User / Vialto | Optional (algorithm verification) |

**Auto-fetch via Chrome MCP** (if user has logged in):
- Morgan Stanley: Activity → Reports → "Your Alphabet Stock Statement" → All history / USD / CSV → Run Report → fetch download link
- IRD: itp.etax.ird.gov.hk → 税務狀況 → 評税 → 檢視 → presign URL → fetch PDF

### Phase 2: Extract vest data from stock statement

For Alphabet/Google statements, see [references/extracting_alphabet_statement.md](references/extracting_alphabet_statement.md).

Key rules:
- Take **Hong Kong jurisdiction rows only** (skip China rows — different FX rate)
- Extract: `grant_date`, `vest_date`, `shares` (GSUs Vested), `fmv_usd` (Fair Market Value), `fx_rate`
- Filter to the target year of assessment (vest_date ∈ [YA_start, YA_end])
- Output CSV for `scripts/compute.py`

### Phase 3: Cross-check IR56B vs Assessment (detect unclaimed exemptions)

**This is the diagnostic step** — determines whether to file a new claim or a retrospective 70A correction.

For each assessed tax year:
```
IF IR56B Total = Assessment income → ❌ No exemption claimed (problem year)
IF IR56B Total > Assessment income → ✅ Exemption was claimed (OK year)
   Verify: difference ≈ independently computed CN-source amount (within 2%)
```

### Phase 4: Verify algorithm against prior year (optional but recommended)

If user provided a previous year's return with APPENDIX-EQUITY:
1. Re-run `compute.py` on last year's data
2. Compare each row's `assessable_hkd` to the filed figure
3. Must match within HK$ 0.01 — if not, stop and investigate

### Phase 5: Run computation

Build config YAML + vests CSV, then:

```bash
python3 scripts/compute.py --config config.yaml --vests vests.csv --output result.json
```

Validates all vests fall within YA window, computes per-vest apportionment, aggregates totals, runs sanity checks.

### Phase 6: Generate outputs

**APPENDIX-EQUITY PDF** (for eTAX submission):
```bash
python3 scripts/gen_pdf.py --result result.json --output appendix_equity.pdf
```
Multi-page PDF: main table → DIPN 38 detail blocks → Itinerary appendix.

**HTML audit report** (for personal review):
```bash
python3 scripts/gen_tax_report.py \
  --analysis analysis.json \
  --ir56b '{"2024/25":{"gsu":604498,"total":3305748}}' \
  --assessment '{"2024/25":3305748}' \
  --taxpayer "NAME" --file-no "FILE_NO" \
  --output report.html
```
Publish via `publish-cc-page.sh`.

### Phase 7: Action guidance

**Situation A — Current year filing (not yet filed)**:
- Guide eTAX BIR60 filing per [references/etax_filing.md](references/etax_filing.md)
- Enable "Apply for partial exemption" in Step 2 → Box 30 = IR56B total → Box 34 = excluded amount → Section 4 attach PDF

**Situation B — Past year where exemption was missed (70A retrospective)**:
- Guide IR831 + Section 70A letter per [references/ir831-guide.md](references/ir831-guide.md)
- IR831 Item 2 + Item 25, attach APPENDIX-EQUITY PDF + IR56B + employment contract
- Deadline: 6 years after end of tax year

**Situation C — All years correctly claimed**:
- Confirm no action needed; remind to continue claiming until all pre-transfer grants vest

---

## Reference files

- [references/dipn38.md](references/dipn38.md) — DIPN 38 ¶43–46 verbatim + interpretation
- [references/etax_filing.md](references/etax_filing.md) — Full eTAX BIR60 filing walkthrough (Box-by-Box)
- [references/ir831-guide.md](references/ir831-guide.md) — IR831 filling guide + 70A letter template
- [references/extracting_alphabet_statement.md](references/extracting_alphabet_statement.md) — Morgan Stanley statement parsing
- [references/workflow-checklist.md](references/workflow-checklist.md) — Material checklist + decision tree

## Scripts

- `scripts/compute.py` — Core DIPN 38 calculation engine (YAML+CSV → JSON)
- `scripts/gen_pdf.py` — APPENDIX-EQUITY PDF generator (reportlab)
- `scripts/gen_tax_report.py` — HTML audit report generator (Material Design)
- `scripts/parse_ms_statement.py` — Morgan Stanley CSV auto-parser (bulk extraction)
- `scripts/example/` — Working example with anonymized data

## Common pitfalls

| Pitfall | Fix |
|---|---|
| Used `Prorated Income` instead of `Total Income` | Always use `Total Income of GSUs at Vest` (gross) |
| Used China row's FX rate (~7.1) | Use HK row's FX rate (~7.7) |
| Used round() instead of floor() | Use `int()` (truncate) — IRD convention |
| Filed BIR-SP4 form | Wrong form — use BIR60 Appendix Section 4 |
| Selected wrong eTAX grounds | Always pick "Non-Hong Kong employment" section 8(1A)(a) |
| Forgot to enable Box 34 in eTAX | Must tick "Apply for partial exemption" in Step 2 first |
| Forgot to attach PDF | The PDF is the proof; without it IRD may reject |
| Assumed Google IR56B splits by geography | Google reports full GSU amount — you must self-claim |
| Assumed Prorated = what to report | Prorated is employer-internal; IRD wants gross + your own apportionment |
| Didn't check if a tax agent already claimed | Check Vialto/PwC/Deloitte first — avoid duplication |
