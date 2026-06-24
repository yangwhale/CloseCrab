# Extracting vest data from Alphabet / Google stock statements

Alphabet (Google) employees receive a "GSU Stock Statement" PDF (hosted by Morgan Stanley) listing every vesting event. This is the input to `scripts/compute.py`.

## Statement layout

Typical Alphabet stock statement is 5–10 pages, one wide table per page. Columns:

| Column | Description | Use? |
|---|---|---|
| Purno | Internal Morgan Stanley ID | No |
| **Jurisdiction** | Country tag — `Hong Kong` or `China` (or `US`, etc.) | **Yes — keep only Hong Kong rows** |
| **Original Award Date** | When the grant was conditionally awarded | **Yes — this is `grant_date`** |
| **Vesting Date** | When this tranche vested | **Yes — this is `vest_date`** |
| **GSUs Vested** | Number of shares vested in this event | **Yes — this is `shares`** |
| Shares Deposited | Net shares after withholding | No (we want gross) |
| **Fair Market Value** | USD per share on vest date | **Yes — this is `fmv_usd`** |
| Total Income of GSUs at Vest | `GSUs Vested × FMV` in USD | Useful for cross-check |
| Prorated Income | Employer-internal China/HK split | **NO — do not use** |
| Payroll Reportable Amount | Same as Prorated | **NO** |
| ... | ... | |
| **FX Rate** | USD→HKD on the HK row | **Yes — this is `fx_rate`** |
| Currency | Always USD | No |
| Release Type | `WCF` (whole-share, cash-refund) or `STC` (sell-to-cover) | No |
| Award Number | Internal grant ID (e.g. C1051837) | Optional — useful for cross-referencing the grant |

## Critical pitfalls

### 1. Same vest event = TWO rows (China + HK)

For pre-transfer grants, each vest appears **twice** — one row tagged `China` (or whatever the prior jurisdiction was) and one row tagged `Hong Kong`. They have the same shares, same FMV, but **different FX rates** (China row uses PBOC rate ~7.1, HK row uses HKMA rate ~7.7).

**Always use the Hong Kong row** for `fx_rate`. The China FX rate is for Chinese payroll reporting and produces materially different (lower) HKD figures that don't match what IRD expects.

### 2. Post-transfer grants only have HK row

For grants made after HK transfer (e.g. the user's first refresh grant under HK employment), only the HK row exists — there's no China-side reporting because the grant has no China connection.

### 3. Total Income vs Prorated Income

The PDF has two USD amount columns per row:
- **Total Income of GSUs at Vest** = `shares × FMV` (the full gross)
- **Prorated Income** = the employer's internal split between jurisdictions

For HK tax purposes use **Total Income** (the gross). Prorated Income is the company's payroll-internal allocation, useful for their China payroll filing but irrelevant for your HK tax claim — IRD wants the gross figure and applies its own DIPN 38 apportionment.

In `scripts/compute.py` we recompute `Total Income = shares × FMV` so this column isn't strictly needed for input — but it's a useful sanity check (if your `shares × FMV` doesn't match the statement's Total Income column to within $0.01, something's wrong).

## Extraction workflow

When the user gives you an Alphabet stock statement PDF:

1. **Read the PDF** in chunks using Read tool's `pages` parameter (5–10 pages, often 1.5–2 MB).
2. **Find the rows** in the year of assessment (vest_date ∈ [YA_start, YA_end]).
3. **For each vest event** (identified by Vesting Date + Original Award Date + Shares):
   - Take the **Hong Kong** jurisdiction row
   - Extract `grant_date`, `vest_date`, `shares` (GSUs Vested), `fmv_usd` (Fair Market Value), `fx_rate` (FX Rate from HK row)
4. **Skip** China rows for the same vest event (they have a different FX rate).
5. **Output to CSV** or JSON for `compute.py`.

## CSV output format

```csv
grant_date,vest_date,shares,fmv_usd,fx_rate
2024-03-06,2025-04-25,9.042,161.47,7.758540
2023-03-08,2025-04-25,15.069,161.47,7.758540
2021-10-06,2025-04-25,20.091,161.47,7.758540
2025-03-05,2025-04-25,6.008,161.47,7.758540
```

(One row per vest; dates in ISO format; floats with up to 6 dp where the PDF has 6.)

## Cross-check before generating PDF

Always verify against the previous year's tax return (if user provided one):

1. Same skill, same algorithm, run on **last year's** data.
2. Compare each row's `assessable_hkd` to the previous return's "Net Assessable income" column.
3. They should match within HK$ 0.01. If any row differs by > HK$ 0.05, **stop** and investigate — likely an FX rate or date typo.

This catches almost all errors before they reach IRD.
