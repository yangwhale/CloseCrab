#!/usr/bin/env python3
"""Parse Morgan Stanley 'Your Alphabet Stock Statement' CSV and compute
GSU geographic source apportionment for Hong Kong salaries tax.

Usage:
    python3 parse_ms_statement.py --csv stock.csv --transfer-date 2023-08-01 [--output out.json]

Input:  Morgan Stanley CSV (All available history, USD)
Output: JSON with per-tax-year summaries and per-lot details
"""
import argparse, csv, json, re, sys
from collections import defaultdict
from datetime import date

MONTHS = {
    '一月': 1, '二月': 2, '三月': 3, '四月': 4, '五月': 5, '六月': 6,
    '七月': 7, '八月': 8, '九月': 9, '十月': 10, '十一月': 11, '十二月': 12,
    'January': 1, 'February': 2, 'March': 3, 'April': 4, 'May': 5, 'June': 6,
    'July': 7, 'August': 8, 'September': 9, 'October': 10, 'November': 11, 'December': 12,
}

# IRD Salaries Tax average buying rates (USD->HKD) by (year, month).
# Source: https://www.ird.gov.hk/chi/tax/ind_stp.htm
# Key = (calendar_year, month), Value = cumulative avg buying rate from tax year start to that month-end.
IRD_RATES = {
    (2023,4):7.8247,(2023,5):7.8180,(2023,6):7.8144,(2023,7):7.8087,(2023,8):7.8071,
    (2023,9):7.8067,(2023,10):7.8056,(2023,11):7.8027,(2023,12):7.8006,
    (2024,1):7.7997,(2024,2):7.7992,(2024,3):7.7989,
    (2024,4):7.8039,(2024,5):7.7939,(2024,6):7.7901,(2024,7):7.7880,(2024,8):7.7842,
    (2024,9):7.7808,(2024,10):7.7753,(2024,11):7.7725,(2024,12):7.7695,
    (2025,1):7.7682,(2025,2):7.7671,(2025,3):7.7653,
    (2025,4):7.7350,(2025,5):7.7565,(2025,6):7.7781,(2025,7):7.7891,(2025,8):7.7913,
    (2025,9):7.7857,(2025,10):7.7802,(2025,11):7.7765,(2025,12):7.7740,
    (2026,1):7.7737,(2026,2):7.7751,(2026,3):7.7772,
}
FALLBACK_RATE = 7.80


def parse_date(s):
    s = s.strip()
    m = re.match(r'(\d+)-(\S+?)-(\d+)', s)
    if not m:
        return None
    day, mon_str, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = MONTHS.get(mon_str)
    if month is None:
        try:
            month = int(mon_str)
        except ValueError:
            return None
    return date(year, month, day)


def parse_money(s):
    return float(s.replace('$', '').replace(',', '').replace('"', '').strip() or '0')


def hk_tax_year(d):
    return d.year - 1 if d.month < 4 else d.year


def get_rate(d):
    return IRD_RATES.get((d.year, d.month), FALLBACK_RATE)


def main():
    parser = argparse.ArgumentParser(description='Parse MS Stock Statement for HK tax apportionment')
    parser.add_argument('--csv', required=True, help='Path to Morgan Stanley CSV')
    parser.add_argument('--transfer-date', required=True, help='Date of transfer to HK (YYYY-MM-DD)')
    parser.add_argument('--output', default=None, help='Output JSON path (default: stdout)')
    args = parser.parse_args()

    T = date.fromisoformat(args.transfer_date)

    with open(args.csv) as f:
        lines = f.readlines()

    reader = csv.reader(lines[1:])
    header = next(reader)
    rows = [r for r in reader if len(r) >= 18 and r[0].strip()]

    lots = defaultdict(list)
    for r in rows:
        lots[r[0]].append(r)

    agg = defaultdict(lambda: {'total_usd': 0, 'hk_usd': 0, 'cn_usd': 0,
                                'hk_hkd': 0, 'cn_hkd': 0, 'lots': 0})
    details = []

    for purno, rs in lots.items():
        tot = parse_money(rs[0][7])
        g = parse_date(rs[0][2])
        v = parse_date(rs[0][3])
        if not g or not v:
            continue
        if v < T:
            continue

        if len(rs) == 2:
            hk = parse_money(rs[1][8])
            cn = parse_money(rs[0][8])
        elif g >= T:
            hk = tot
            cn = 0.0
        else:
            hk = 0.0
            cn = tot

        rt = get_rate(v)
        hy = hk_tax_year(v)
        ty = f"{hy}/{(hy+1)%100:02d}"

        a = agg[ty]
        a['total_usd'] += tot
        a['hk_usd'] += hk
        a['cn_usd'] += cn
        a['hk_hkd'] += hk * rt
        a['cn_hkd'] += cn * rt
        a['lots'] += 1

        cat = 'post_transfer' if g >= T else 'cross_border'
        details.append({
            'tax_year': ty,
            'award': rs[0][16],
            'grant_date': g.isoformat(),
            'vest_date': v.isoformat(),
            'total_usd': round(tot, 2),
            'hk_usd': round(hk, 2),
            'cn_usd': round(cn, 2),
            'rate': rt,
            'hk_hkd': round(hk * rt, 0),
            'cn_hkd': round(cn * rt, 0),
            'category': cat,
        })

    for ty in agg:
        for k in ['total_usd', 'hk_usd', 'cn_usd', 'hk_hkd', 'cn_hkd']:
            agg[ty][k] = round(agg[ty][k], 0)

    result = {
        'transfer_date': T.isoformat(),
        'generated': date.today().isoformat(),
        'total_lots_processed': len(details),
        'tax_years': dict(sorted(agg.items())),
        'details': sorted(details, key=lambda x: (x['tax_year'], x['vest_date'])),
    }

    out = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(out)
        print(f"Written to {args.output} ({len(details)} lots, {len(agg)} tax years)", file=sys.stderr)
    else:
        print(out)


if __name__ == '__main__':
    main()
