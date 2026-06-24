# DIPN No. 38 — Share-Based Benefits (key paragraphs)

Source: IRD Departmental Interpretation and Practice Notes No. 38 (Revised), <https://www.ird.gov.hk/eng/pdf/dipn38.pdf>.

This reference quotes the paragraphs most relevant to the cross-border share-award partial-exemption claim that this skill implements. Read the full DIPN if your case has unusual facts (early-vesting, departure from HK mid-vesting, share options vs share awards distinction, etc.).

---

## Paragraph 43 — Non-Hong Kong employment at the time of grant

> Where a person has a non-Hong Kong employment at the time of grant, the gain will have a non-Hong Kong source and will not be chargeable to Salaries Tax unless it comes within the scope of section 8(1A)(a) i.e. if it is derived from services rendered in Hong Kong. In this regard, the Department will generally accept that no liability to Salaries Tax arises where a right is granted on an unconditional basis (or on completion of a vesting period of a conditional grant) prior to a person rendering any services in Hong Kong, notwithstanding that the right may be exercised after the person commences to render such services.

**Key takeaway**: the source of a share award gain is determined at **grant time**, not vest time. If the grant was made under a non-HK employment, the gain is non-HK source — and only the portion attributable to HK services becomes assessable.

---

## Paragraph 44 — Vesting period spans the transfer

> The more complex situation is the one where a person with a non-Hong Kong employment is granted the right subject to a vesting period during which services are rendered both in and outside Hong Kong. In such a situation, it is considered that the gain on the subsequent exercise etc. of the right should not be fully assessable in Hong Kong as it can partly be attributed to services rendered outside Hong Kong. On the other hand, because the gain can be partly attributed to services in Hong Kong, the benefit should to some extent be chargeable to Salaries Tax.

**This is the standard "transfer during vesting" case** that this skill handles.

---

## Paragraphs 45–46 — The apportionment formula

> In considering the appropriate proportion, if any, of the gain to be treated as derived from services rendered in Hong Kong... the Department will generally accept that it is equitable to have regard to the number of days in Hong Kong plus leave days attributable to services in Hong Kong during the period from the date of conditional grant to the date the employee became unconditionally entitled to exercise the right (i.e. the vesting period) to the total number of days in the period... In other words, the assessable amount of the gain can generally be arrived at by applying the following formula:

```
                                Days in Hong Kong plus
                                attributable leave during
Gain calculated in section ×          vesting period
9(1)(d) and 9(4)                Total number of days in
                                    the vesting period
```

> For example, if the vesting period was two years (i.e. 730 days) and the person's days in Hong Kong plus leave days attributable to service in Hong Kong were 292 days during the period, 40% of the gain would be assessable in the year of exercise of the right (i.e. 292/730 = 40%).

**Application in this skill**: for each individual vesting tranche, count days from grant date to vest date (the "vesting period"). The "Days in HK during vesting period" is `vest_date − hk_employment_start` (days from start of HK employment to the vest). The "Outside HK days" is `hk_employment_start − grant_date` (days before HK employment within the vesting period).

---

## Paragraph 47 — Section 8(1A)(b)(ii) interaction

> The provisions of section 8(1A)(b)(ii), as read with section 8(1B), may be relevant in relation to the calculation of the proportion referred to above. By virtue of section 8(1A)(b)(ii), chargeable income does not include... [income from services rendered outside HK if visits to HK in the year of assessment do not exceed 60 days].

**Edge case**: if during any year of the vesting period the taxpayer was in HK for ≤60 days, those days are not counted as "HK days" in the numerator. This skill currently does NOT model this rule because it's rare for full-time employees transferring to HK; if your case is affected, do the calculation manually for that year.

---

## Relationship to BIR60 Appendix Section 4

When filing BIR60, the partial exemption claim goes in:

- **BIR60 main form Part 4.1 Box 30**: gross income (including the full share award amount as reported on IR56B)
- **BIR60 main form Part 4.1 Box 34**: amount to be excluded (the difference = non-HK source portion)
- **BIR60 Appendix Section 4**: "Application for Full / Partial Exemption of Income"
  - Grounds: section 8(1A)(a) — Non-Hong Kong employment
  - Supporting documents: detailed computation (the PDF this skill generates) + itinerary

The "Non-Hong Kong employment" ground is applied to the share award portion specifically; the employment at vest is still HK (and the salary portion remains fully HK-source taxable). This subtlety is explained in the methodology footnote on the PDF's first page so the IRD reviewer doesn't misinterpret the claim as a whole-employment exemption.

---

## What this skill does NOT cover (consult a tax professional)

- **Share options** (as opposed to share awards/RSUs/GSUs) — section 9(4) computation differs; vesting period definition may differ
- **Conditional vesting** based on performance metrics (not just time)
- **Permanent departure from HK** during a vesting period (DIPN 38 paragraphs 71–74 — "deemed vesting" rules)
- **Section 8(1A)(c) tax credit claims** (where tax was paid in the non-HK jurisdiction on the same income)
- **Personal Assessment** elections that interact with the share award income
