#!/usr/bin/env python3
"""香港实时巴士到站查询 — KMB + Citybus 开放 API。"""
import argparse, json, sys
from datetime import datetime, timezone, timedelta
import requests

HKT = timezone(timedelta(hours=8))
KMB_BASE = "https://data.etabus.gov.hk/v1/transport/kmb"
CTB_BASE = "https://rt.data.gov.hk/v2/transport/citybus"

FAVORITE_STOPS = {
    "113": {"stop": "9DBBC71CB1578A87", "name": "喇沙利道", "seq": 12, "bound": "outbound", "company": "KMB"},
    "182": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 17, "bound": "outbound", "company": "KMB"},
    "170": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 13, "bound": "outbound", "company": "KMB"},
    "103": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 10, "bound": "outbound", "company": "KMB"},
}

def get_kmb_eta(route: str, stop_id: str, service_type: str = "1"):
    url = f"{KMB_BASE}/eta/{stop_id}/{route}/{service_type}"
    r = requests.get(url, timeout=5)
    return r.json().get("data", [])

def get_ctb_eta(route: str, stop_id: str):
    url = f"{CTB_BASE}/eta/CTB/{stop_id}/{route}"
    r = requests.get(url, timeout=5)
    return r.json().get("data", [])

def get_route_info(route: str):
    kmb = requests.get(f"{KMB_BASE}/route/{route}/outbound/1", timeout=5).json().get("data", {})
    return kmb

def format_eta(eta_str: str) -> str:
    if not eta_str:
        return "N/A"
    try:
        eta_time = datetime.fromisoformat(eta_str)
        now = datetime.now(HKT)
        diff = (eta_time - now).total_seconds()
        mins = int(diff // 60)
        if mins <= 0:
            return "即将到站"
        elif mins < 60:
            return f"{mins}分钟后 ({eta_time.strftime('%H:%M:%S')})"
        else:
            return f"{mins}分钟后 ({eta_time.strftime('%H:%M')})"
    except Exception:
        return eta_str

def query_route(route: str, stop_override: str = None):
    route = route.upper()
    info = get_route_info(route)

    fav = FAVORITE_STOPS.get(route)
    if stop_override:
        stop_id = stop_override
        stop_name = stop_override
    elif fav:
        stop_id = fav["stop"]
        stop_name = fav["name"]
    else:
        print(f"路线 {route} 不在常用列表中，请指定 --stop")
        sys.exit(1)

    print(f"🚌 {route} ({info.get('orig_tc', '')} → {info.get('dest_tc', '')})")
    print(f"📍 查询站: {stop_name}")
    print(f"⏰ 查询时间: {datetime.now(HKT).strftime('%H:%M:%S')}")
    print()

    etas = get_kmb_eta(route, stop_id)
    if not etas or not any(e.get("eta") for e in etas):
        etas = get_ctb_eta(route, stop_id.replace("KMB", ""))

    valid = [e for e in etas if e.get("eta")]
    if not valid:
        print("  ❌ 目前没有到站信息（可能已收车）")
        return

    for e in valid[:5]:
        eta_str = format_eta(e["eta"])
        rmk = e.get("rmk_tc", "")
        seq = e.get("eta_seq", "")
        flag = " ⚠️ " + rmk if rmk else ""
        print(f"  🕐 第{seq}班: {eta_str}{flag}")

def list_routes():
    print("常用路线（九龙城附近上车）：")
    print()
    for route, info in FAVORITE_STOPS.items():
        ri = get_route_info(route)
        print(f"  {route}: {ri.get('orig_tc','')} → {ri.get('dest_tc','')} | 上车站: {info['name']}")

def main():
    p = argparse.ArgumentParser(description="香港实时巴士到站查询")
    p.add_argument("route", nargs="?", help="路线号 (如 113)")
    p.add_argument("--stop", help="指定站点 ID")
    p.add_argument("--list", action="store_true", help="列出常用路线")
    p.add_argument("--all", action="store_true", help="查询所有常用路线")
    args = p.parse_args()

    if args.list:
        list_routes()
        return
    if args.all:
        for route in FAVORITE_STOPS:
            query_route(route)
            print()
        return
    if not args.route:
        p.print_help()
        return
    query_route(args.route, args.stop)

if __name__ == "__main__":
    main()
