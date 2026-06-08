#!/usr/bin/env python3
"""香港实时巴士到站查询 — KMB + Citybus 开放 API。"""
import argparse, json, sys
from datetime import datetime, timezone, timedelta
import requests

HKT = timezone(timedelta(hours=8))
KMB_BASE = "https://data.etabus.gov.hk/v1/transport/kmb"
CTB_BASE = "https://rt.data.gov.hk/v2/transport/citybus"

FAVORITE_STOPS = {
    # 去公司方向 (outbound)
    "113": {"stop": "9DBBC71CB1578A87", "name": "喇沙利道", "seq": 12, "bound": "outbound", "company": "KMB"},
    "182": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 17, "bound": "outbound", "company": "KMB"},
    "170": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 13, "bound": "outbound", "company": "KMB"},
    "103": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 10, "bound": "outbound", "company": "KMB"},
    # 回家方向 (inbound) — 从公司附近上车
    "113回": {"stop": "5E580B75E8AF99F3", "name": "堅拿道西(銅鑼灣)", "seq": 17, "bound": "inbound", "company": "KMB", "route": "113"},
    "182回": {"stop": "4A626ACDA2618AC3", "name": "堅拿道西(灣仔)", "seq": 10, "bound": "inbound", "company": "KMB", "route": "182"},
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
    actual_route = route
    if stop_override:
        stop_id = stop_override
        stop_name = stop_override
    elif fav:
        stop_id = fav["stop"]
        stop_name = fav["name"]
        actual_route = fav.get("route", route)
    else:
        print(f"路线 {route} 不在常用列表中，请指定 --stop")
        sys.exit(1)

    if actual_route != route:
        info = get_route_info(actual_route)
    direction = "回家" if fav and fav.get("bound") == "inbound" else "去公司"
    print(f"🚌 {actual_route} ({info.get('orig_tc', '')} → {info.get('dest_tc', '')}) [{direction}]")
    print(f"📍 查询站: {stop_name}")
    print(f"⏰ 查询时间: {datetime.now(HKT).strftime('%H:%M:%S')}")
    print()

    kmb_etas = get_kmb_eta(actual_route, stop_id)
    ctb_etas = []
    try:
        ctb_etas = get_ctb_eta(actual_route, stop_id)
    except Exception:
        pass
    etas = kmb_etas + ctb_etas

    valid = [e for e in etas if e.get("eta")]
    if not valid:
        print("  ❌ 目前没有到站信息（可能已收车）")
        return

    seen = set()
    sorted_etas = sorted(valid, key=lambda e: e.get("eta", ""))
    idx = 0
    for e in sorted_etas:
        eta_val = e["eta"]
        if eta_val in seen:
            continue
        seen.add(eta_val)
        idx += 1
        eta_str = format_eta(eta_val)
        rmk = e.get("rmk_tc", "")
        co = e.get("co", "")
        company_tag = f" [{co}]" if co else ""
        flag = f" ⚠️ {rmk}" if rmk else ""
        print(f"  🕐 第{idx}班: {eta_str}{company_tag}{flag}")
        if idx >= 5:
            break

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
