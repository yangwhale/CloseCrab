#!/usr/bin/env python3
"""Generate Material Design HTML report for GSU HK tax apportionment analysis.

Usage:
    python3 gen_tax_report.py \
      --analysis /tmp/gsu_analysis.json \
      --ir56b '{"2024/25":{"gsu":600000,"total":3300000}}' \
      --assessment '{"2024/25":3300000}' \
      --taxpayer "MR DOE, JOHN" \
      --file-no "6N1-FXXXXXXX" \
      --output /tmp/report.html
"""
import argparse, json, secrets, sys
from datetime import date


def hkd(x): return f"HK${x:,.0f}"
def usd(x): return f"${x:,.0f}"


def build_crosscheck_rows(analysis, ir56b, assessment):
    rows_html = ""
    for ty in sorted(set(list(ir56b.keys()) + list(assessment.keys()))):
        ir = ir56b.get(ty, {})
        ir_total = ir.get('total', 0)
        ir_gsu = ir.get('gsu', 0)
        assess = assessment.get(ty, 0)
        ana = analysis.get('tax_years', {}).get(ty, {})
        diff = ir_total - assess
        if assess > 0 and diff == 0:
            cls = 'bad'
            status = "&#10060; &#27809; claim"
        elif assess > 0 and diff > 0:
            cls = 'good'
            status = "&#9989; &#24050; claim"
        else:
            cls = ''
            status = "&#8212;"
        reporter = "&#8212;"
        rows_html += f'<tr class="{cls}"><td>{ty}</td><td>{reporter}</td>'
        rows_html += f'<td class=num>{ir_gsu:,}</td><td class=num>{ir_total:,}</td>'
        rows_html += f'<td class=num>{assess:,}</td><td class=num>{diff:,}</td>'
        rows_html += f'<td>{status}</td></tr>\n'
    return rows_html


def build_detail_table(details, ty):
    rows = [d for d in details if d['tax_year'] == ty]
    if not rows:
        return "<p class=note>No GSU lots for this tax year.</p>"
    h = '<table><thead><tr><th>Award</th><th>Grant</th><th>Vest</th>'
    h += '<th class=num>Total US$</th><th class="num hk">HK Source HK$</th>'
    h += '<th class="num cn">CN Exempt HK$</th><th>Rate</th><th>Type</th></tr></thead><tbody>\n'
    tot_usd = hk_hkd = cn_hkd = 0
    for d in rows:
        cat = '纯港' if d['category'] == 'post_transfer' else '跨境'
        h += f'<tr><td>{d["award"]}</td><td>{d["grant_date"]}</td><td>{d["vest_date"]}</td>'
        h += f'<td class=num>{usd(d["total_usd"])}</td><td class="num hk">{hkd(d["hk_hkd"])}</td>'
        h += f'<td class="num cn">{hkd(d["cn_hkd"])}</td><td>{d["rate"]:.4f}</td>'
        h += f'<td><span class=tag>{cat}</span></td></tr>\n'
        tot_usd += d['total_usd']
        hk_hkd += d['hk_hkd']
        cn_hkd += d['cn_hkd']
    h += f'<tr class=tot><td colspan=3><b>Total</b></td><td class=num><b>{usd(tot_usd)}</b></td>'
    h += f'<td class="num hk"><b>{hkd(hk_hkd)}</b></td><td class="num cn"><b>{hkd(cn_hkd)}</b></td>'
    h += '<td></td><td></td></tr>\n'
    h += '</tbody></table>'
    return h


CSS = """*{box-sizing:border-box}body{margin:0;font-family:'Google Sans',Roboto,'PingFang HK','Microsoft JhengHei',sans-serif;color:#202124;background:#f8f9fa;line-height:1.65}
.wrap{max-width:1020px;margin:0 auto;padding:0 20px 80px}
header{background:linear-gradient(135deg,#1a73e8,#174ea6);color:#fff;padding:44px 20px 36px}
h1{font-size:25px;margin:0 0 8px;font-weight:500}.sub{opacity:.92;font-size:14px}
.card{background:#fff;border:1px solid #dadce0;border-radius:12px;padding:24px 28px;margin:22px 0;box-shadow:0 1px 2px rgba(60,64,67,.1)}
h2{font-size:20px;font-weight:500;margin:0 0 14px;color:#174ea6;border-bottom:2px solid #1a73e8;padding-bottom:8px;display:inline-block}
h3{font-size:15px;font-weight:500;margin:16px 0 8px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px}
th,td{padding:7px 8px;text-align:left;border-bottom:1px solid #dadce0}
th{background:#f1f3f4;font-weight:500;color:#5f6368;font-size:11.5px}
td.num{text-align:right;font-variant-numeric:tabular-nums;font-family:'Roboto Mono',monospace}
td.cn,th.cn{color:#d93025}td.hk,th.hk{color:#1a73e8}
tr.tot{background:#e8f0fe}tr.tot td{font-weight:600}
tr.bad{background:#fce8e6}tr.good{background:#e6f4ea}
.tag{font-size:10px;padding:2px 6px;border-radius:10px;background:#e8eaed;color:#5f6368}
.good-box{background:#e6f4ea;border:1px solid #a8dab5;border-radius:10px;padding:14px 18px;font-size:14px;color:#0d652d;margin:16px 0}
.bad-box{background:#fce8e6;border:1px solid #f5c6cb;border-radius:10px;padding:14px 18px;font-size:14px;color:#c5221f;margin:16px 0}
.warn-box{background:#fef7e0;border:1px solid #fdd663;border-radius:10px;padding:14px 18px;font-size:13px;color:#5f4b00;margin:14px 0}
.formula{background:#f1f3f4;border-radius:8px;padding:14px 18px;font-family:'Roboto Mono',monospace;font-size:12px;white-space:pre-wrap;margin:10px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:16px 0}
.kpi{background:#fff;border:1px solid #dadce0;border-left:4px solid #1a73e8;border-radius:8px;padding:14px}
.kpi .v{font-size:21px;font-weight:600;color:#174ea6}.kpi .l{font-size:12px;color:#5f6368;margin-top:4px}
.kpi.cn{border-left-color:#d93025}.kpi.cn .v{color:#d93025}
a{color:#1a73e8}.law li{margin:7px 0}.note{font-size:12.5px;color:#5f6368}
ul.act li{margin:8px 0}footer{text-align:center;color:#5f6368;font-size:12px;margin-top:28px}
.legend{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;vertical-align:middle}"""


def main():
    parser = argparse.ArgumentParser(description='Generate GSU tax apportionment HTML report')
    parser.add_argument('--analysis', required=True, help='Path to analysis JSON from parse_ms_statement.py')
    parser.add_argument('--ir56b', required=True, help='JSON string: {"YYYY/YY": {"gsu": N, "total": N}, ...}')
    parser.add_argument('--assessment', required=True, help='JSON string: {"YYYY/YY": N, ...} (assessed income per year)')
    parser.add_argument('--taxpayer', default='Taxpayer', help='Taxpayer name')
    parser.add_argument('--file-no', default='', help='IRD file number')
    parser.add_argument('--output', required=True, help='Output HTML path')
    args = parser.parse_args()

    analysis = json.load(open(args.analysis))
    ir56b = json.loads(args.ir56b)
    assessment = json.loads(args.assessment)
    tok = secrets.token_hex(8)
    T = analysis['transfer_date']
    today = date.today().isoformat()

    crosscheck = build_crosscheck_rows(analysis, ir56b, assessment)

    problem_years = []
    ok_years = []
    for ty in sorted(assessment.keys()):
        ir_total = ir56b.get(ty, {}).get('total', 0)
        assess = assessment[ty]
        if ir_total > 0 and ir_total == assess:
            cn = analysis.get('tax_years', {}).get(ty, {}).get('cn_hkd', 0)
            problem_years.append((ty, cn))
        elif ir_total > assess:
            ok_years.append(ty)

    not_filed = [ty for ty in analysis.get('tax_years', {}) if ty not in assessment]

    summary_html = ""
    if problem_years:
        yrs = ", ".join(f"<b>{ty}</b> (exempt {hkd(cn)}, refund ~{hkd(cn*0.17)})" for ty, cn in problem_years)
        summary_html += f'<div class=bad-box>&#9888;&#65039; <b>Problem found:</b> {yrs} — GSU reported at full amount, no geographic exemption claimed. Overpaid tax can be recovered via Section 70A.</div>\n'
    if ok_years:
        summary_html += f'<div class=good-box>&#9989; {", ".join(ok_years)} — GSU geographic exemption correctly claimed.</div>\n'

    detail_sections = ""
    all_years = sorted(set(list(analysis.get('tax_years', {}).keys())))
    for ty in all_years:
        a = analysis['tax_years'].get(ty, {})
        status = ""
        for pty, cn in problem_years:
            if pty == ty:
                status = " &#10060; NOT CLAIMED"
        for oty in ok_years:
            if oty == ty:
                status = " &#9989; CLAIMED"
        if ty in [nf for nf in not_filed]:
            status = " &#8212; Not yet filed"
        detail_sections += f'<div class=card><h3>{ty}{status}</h3>\n'
        detail_sections += f'<div class=kpis><div class=kpi><div class=v>{hkd(a.get("hk_hkd",0))}</div><div class=l>HK Source</div></div>'
        detail_sections += f'<div class="kpi cn"><div class=v>{hkd(a.get("cn_hkd",0))}</div><div class=l>CN Exempt</div></div></div>\n'
        detail_sections += build_detail_table(analysis['details'], ty)
        detail_sections += '</div>\n'

    action_items = "<ul class=act>\n"
    for ty, cn in problem_years:
        action_items += f'<li><b>{ty} (Retrospective 70A)</b>: File IR831 citing Section 70A to reduce assessable income by {hkd(cn)}. Estimated refund ~{hkd(cn*0.17)}. Deadline: {int(ty[:4])+7}-03-31.</li>\n'
    for ty in not_filed:
        a = analysis['tax_years'].get(ty, {})
        if a.get('cn_hkd', 0) > 0:
            action_items += f'<li><b>{ty} (Current filing)</b>: Report GSU at HK source only ({hkd(a["hk_hkd"])}), not IR56B full amount. Claim exemption in BIR60 Appendix Part 4. Saves ~{hkd(a["cn_hkd"]*0.17)}.</li>\n'
    action_items += "</ul>"

    html = f"""<!DOCTYPE html><html lang=zh-Hant><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content="noindex,nofollow">
<title>GSU HK Tax Apportionment Report</title><style>{CSS}</style></head><body>
<header><div class=wrap><h1>GSU Hong Kong Salaries Tax &middot; Geographic Source Apportionment Report</h1>
<div class=sub>{args.taxpayer} &middot; File No. {args.file_no} &middot; Transfer date {T} &middot; Generated {today} &middot; ID {tok}</div></div></header>
<div class=wrap>
<div class=warn-box>&#128274; Private document (IAP protected). Not professional tax advice &mdash; consult a licensed tax advisor before filing.</div>
{summary_html}
<div class=card>
<h2>Cross-Check: IR56B vs Assessment vs Independent Calculation</h2>
<p>Google IR56B reports GSU at <b>full amount</b> (no geographic split). Each year you must actively claim the exemption.</p>
<table><thead><tr><th>Tax Year</th><th>Filed by</th><th class=num>IR56B GSU</th><th class=num>IR56B Total</th><th class=num>Assessed Income</th><th class=num>Difference</th><th>Status</th></tr></thead>
<tbody>{crosscheck}</tbody></table>
<p class=note>Difference = IR56B Total - Assessed Income &asymp; GSU CN-source exempted. Zero difference = full amount assessed = no exemption claimed.</p>
</div>
<div class=card>
<h2>Apportionment Method</h2>
<div class=formula>HK Taxable = Total &times; (HK days in vesting period &divide; total vesting days)
CN Exempt  = Total - HK Taxable
Split point T = {T} (start of HK employment)
Exchange rate: IRD Salaries Tax average buying rate (USD) by vest month</div>
</div>
<div class=card><h2>Per-Year Detail</h2>
{detail_sections}
</div>
<div class=card><h2>Recommended Actions</h2>{action_items}</div>
<div class=card><h2>Legal References</h2>
<ul class=law>
<li><b>IRO s.8(1A)(c)</b> &mdash; Exemption for services rendered outside HK. <a href="https://www.elegislation.gov.hk/hk/cap112!zh-Hant-HK/s8">Link</a></li>
<li><b>IRO s.70A</b> &mdash; Correction of errors/omissions (6-year limit). <a href="https://www.elegislation.gov.hk/hk/cap112!zh-Hant-HK/s70A">Link</a></li>
<li><b>IRO s.79</b> &mdash; Refund of overpaid tax. <a href="https://www.elegislation.gov.hk/hk/cap112!zh-Hant-HK/s79">Link</a></li>
<li><b>DIPN 38</b> &mdash; Share awards taxation &amp; time apportionment. <a href="https://www.ird.gov.hk/eng/pdf/dipn38.pdf">PDF</a></li>
<li><b>IRD Exchange Rates</b> &mdash; Salaries tax buying rates. <a href="https://www.ird.gov.hk/chi/tax/ind_stp.htm">Link</a></li>
</ul></div>
<footer>Generated by hk-tax-gsu skill &middot; ID {tok}</footer>
</div></body></html>"""

    with open(args.output, 'w') as f:
        f.write(html)
    print(f"Report written to {args.output}", file=sys.stderr)
    if problem_years:
        for ty, cn in problem_years:
            print(f"PROBLEM: {ty} overpaid ~{hkd(cn*0.17)}", file=sys.stderr)


if __name__ == '__main__':
    main()
