#!/usr/bin/env python3
"""DIPN 38 partial-exemption computation for HK salaries tax share awards.

Reads taxpayer config (YAML or JSON) + a vests list (CSV or JSON), applies the
section 8(1A)(a) / DIPN 38 paragraphs 43-46 apportionment, and emits a JSON
result that gen_pdf.py consumes.

Usage as CLI:
    python compute.py --config config.yaml --vests vests.csv --output result.json

Usage as library:
    from compute import compute_all, Vest, TaxpayerConfig
    rows = compute_all(config, vests)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

try:
    import yaml  # type: ignore  # pyyaml; optional, falls back to JSON if missing
except ImportError:
    yaml = None  # type: ignore


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TaxpayerConfig:
    """Per-filing context. Everything here ends up in the PDF header / metadata."""
    name: str                       # e.g. "MR DOE, JOHN"
    file_no: str                    # IRD file number, e.g. "6N1-FXXXXXXX"
    year_of_assessment: str         # e.g. "2025/26"
    ya_start: date                  # 1 Apr YYYY
    ya_end: date                    # 31 Mar YYYY+1
    hk_employment_start: date       # First day at HK employer (e.g. 2024-06-03)
    hk_employer: str                # Current HK employer legal name
    prior_employer: str = ""        # Optional, for itinerary appendix

    @classmethod
    def from_dict(cls, d: dict) -> "TaxpayerConfig":
        def to_date(v) -> date:
            if isinstance(v, date):
                return v
            return date.fromisoformat(str(v))
        return cls(
            name=d["name"],
            file_no=d["file_no"],
            year_of_assessment=d["year_of_assessment"],
            ya_start=to_date(d["ya_start"]),
            ya_end=to_date(d["ya_end"]),
            hk_employment_start=to_date(d["hk_employment_start"]),
            hk_employer=d["hk_employer"],
            prior_employer=d.get("prior_employer", ""),
        )


@dataclass
class Vest:
    """One vesting event from the stock statement (Hong Kong jurisdiction row)."""
    grant_date: date
    vest_date: date
    shares: float
    fmv_usd: float
    fx_rate: float

    @classmethod
    def from_dict(cls, d: dict) -> "Vest":
        def to_date(v) -> date:
            if isinstance(v, date):
                return v
            return date.fromisoformat(str(v))
        return cls(
            grant_date=to_date(d["grant_date"]),
            vest_date=to_date(d["vest_date"]),
            shares=float(d["shares"]),
            fmv_usd=float(d["fmv_usd"]),
            fx_rate=float(d["fx_rate"]),
        )


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def compute_one(v: Vest, cfg: TaxpayerConfig) -> dict:
    """Apply DIPN 38 apportionment to a single vest.

    Returns a dict with all the derived fields the PDF needs.
    """
    usd = v.shares * v.fmv_usd
    hkd = usd * v.fx_rate

    # Total days in vesting period: grant_date -> vest_date - 1 inclusive.
    # Equivalent to (vest_date - grant_date).days in Python — verified against
    # multiple years of IRD-accepted filings.
    total = (v.vest_date - v.grant_date).days

    # Outside-HK days: grant_date -> (hk_start - 1) inclusive, when grant is
    # before HK employment.
    # (hk_start - grant).days gives the inclusive count.
    if v.grant_date < cfg.hk_employment_start:
        outside = (cfg.hk_employment_start - v.grant_date).days
        hk_days = total - outside
        assessable = hkd * hk_days / total
        apportioned = True
    else:
        # Grant made after HK employment started — fully assessable, no apportionment.
        outside = 0
        hk_days = total
        assessable = hkd
        apportioned = False

    return {
        "grant_date": v.grant_date.isoformat(),
        "vest_date": v.vest_date.isoformat(),
        "shares": v.shares,
        "fmv_usd": v.fmv_usd,
        "usd": usd,
        "fx_rate": v.fx_rate,
        "hkd": hkd,
        "total_days": total,
        "outside_days": outside,
        "hk_days": hk_days,
        "assessable_hkd": assessable,
        "apportioned": apportioned,
    }


def note_label(i: int) -> str:
    """0->a, 1->b, ..., 25->z, 26->aa, 27->ab, ...

    Matches the labelling convention used in IRD-filed APPENDIX-EQUITY pages.
    """
    if i < 26:
        return chr(ord("a") + i)
    return "a" + chr(ord("a") + (i - 26))


def compute_all(cfg: TaxpayerConfig, vests: Iterable[Vest]) -> dict:
    """Compute all vests and assemble totals + sanity checks.

    Returns a dict with:
      - taxpayer: config echo
      - rows: list of per-vest results, sorted by (vest_date desc, grant_date desc)
              to match the conventional table layout
      - totals: aggregated shares / hkd / assessable
      - sanity: dict of checks (HK days consistency per vest_date, etc.)
    """
    vests = list(vests)

    # Sort: vest_date desc (latest first), then grant_date desc (newest grant first)
    vests.sort(key=lambda v: (v.vest_date, v.grant_date), reverse=True)

    rows = []
    note_idx = 0
    for v in vests:
        if not (cfg.ya_start <= v.vest_date <= cfg.ya_end):
            raise ValueError(
                f"Vest on {v.vest_date} falls outside year of assessment "
                f"{cfg.ya_start} – {cfg.ya_end}"
            )
        r = compute_one(v, cfg)
        if r["apportioned"]:
            r["note"] = "(1" + note_label(note_idx) + ")"
            note_idx += 1
        else:
            r["note"] = ""
        rows.append(r)

    total_shares = sum(r["shares"] for r in rows)
    total_hkd = sum(r["hkd"] for r in rows)
    total_assessable = sum(r["assessable_hkd"] for r in rows)
    excluded = total_hkd - total_assessable

    # Sanity: same vest_date + pre-HK grant should all have the same hk_days
    by_vest = {}
    for r in rows:
        if r["outside_days"] > 0:
            by_vest.setdefault(r["vest_date"], []).append(r["hk_days"])
    hk_days_consistent = all(len(set(v)) == 1 for v in by_vest.values())

    return {
        "taxpayer": {
            "name": cfg.name,
            "file_no": cfg.file_no,
            "year_of_assessment": cfg.year_of_assessment,
            "ya_start": cfg.ya_start.isoformat(),
            "ya_end": cfg.ya_end.isoformat(),
            "hk_employment_start": cfg.hk_employment_start.isoformat(),
            "hk_employer": cfg.hk_employer,
            "prior_employer": cfg.prior_employer,
        },
        "rows": rows,
        "totals": {
            "shares": total_shares,
            "net_amount_hkd": total_hkd,
            "net_assessable_hkd": total_assessable,
            "excluded_hkd": excluded,
            "net_amount_hkd_int": int(total_hkd),       # floor for reporting
            "net_assessable_hkd_int": int(total_assessable),
            "excluded_hkd_int": int(total_hkd) - int(total_assessable),
            "vest_count": len(rows),
            "apportioned_count": sum(1 for r in rows if r["apportioned"]),
        },
        "sanity": {
            "hk_days_consistent": hk_days_consistent,
            "hk_days_by_vest_date": {
                k.isoformat() if isinstance(k, date) else str(k): list(set(v))
                for k, v in by_vest.items()
            },
        },
    }


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def load_config(path: Path) -> TaxpayerConfig:
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        if yaml is None:
            raise RuntimeError("PyYAML required for YAML config; pip install pyyaml")
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return TaxpayerConfig.from_dict(data)


def load_vests(path: Path) -> list[Vest]:
    if path.suffix.lower() == ".csv":
        with path.open() as f:
            return [Vest.from_dict(row) for row in csv.DictReader(f)]
    text = path.read_text()
    data = json.loads(text)
    if isinstance(data, dict) and "vests" in data:
        data = data["vests"]
    return [Vest.from_dict(d) for d in data]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True,
                   help="Taxpayer config (YAML or JSON)")
    p.add_argument("--vests", type=Path, required=True,
                   help="Vests data (CSV or JSON)")
    p.add_argument("--output", type=Path, default=None,
                   help="Output JSON path (default: stdout)")
    args = p.parse_args()

    cfg = load_config(args.config)
    vests = load_vests(args.vests)
    result = compute_all(cfg, vests)

    out = json.dumps(result, indent=2, default=str)
    if args.output:
        args.output.write_text(out)
        # Brief summary to stderr so user sees something
        t = result["totals"]
        print(
            f"Wrote {args.output}\n"
            f"  Vests: {t['vest_count']} ({t['apportioned_count']} apportioned)\n"
            f"  Net amount:     HK$ {t['net_amount_hkd_int']:,}\n"
            f"  Net assessable: HK$ {t['net_assessable_hkd_int']:,}\n"
            f"  Excluded:       HK$ {t['excluded_hkd_int']:,}",
            file=sys.stderr,
        )
    else:
        print(out)

    if not result["sanity"]["hk_days_consistent"]:
        print(
            "WARNING: HK days inconsistent across vests sharing a vest_date — "
            "investigate before trusting this output.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
