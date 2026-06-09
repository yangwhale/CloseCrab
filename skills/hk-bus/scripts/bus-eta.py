#!/usr/bin/env python3
"""香港智能通勤助手 — Google Maps 路线 + KMB/Citybus 实时到站融合。"""
import argparse, json, sys, os, math
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

HKT = timezone(timedelta(hours=8))
KMB_BASE = "https://data.etabus.gov.hk/v1/transport/kmb"
CTB_BASE = "https://rt.data.gov.hk/v2/transport/citybus"
MAPS_URL = "https://maps.googleapis.com/maps/api/directions/json"
DEFAULT_DEST = "Times Square, Causeway Bay, Hong Kong"

FAVORITE_STOPS = {
    "113": {"stop": "9DBBC71CB1578A87", "name": "喇沙利道", "seq": 12, "bound": "outbound", "company": "KMB"},
    "182": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 17, "bound": "outbound", "company": "KMB"},
    "170": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 13, "bound": "outbound", "company": "KMB"},
    "103": {"stop": "3821D64D2BF1BB66", "name": "九龍醫院", "seq": 10, "bound": "outbound", "company": "KMB"},
    "113回": {"stop": "5E580B75E8AF99F3", "name": "堅拿道西(銅鑼灣)", "seq": 17, "bound": "inbound", "company": "KMB", "route": "113"},
    "182回": {"stop": "4A626ACDA2618AC3", "name": "堅拿道西(灣仔)", "seq": 10, "bound": "inbound", "company": "KMB", "route": "182"},
}


# ── Google Maps ──────────────────────────

def get_maps_key():
    p = "/tmp/maps-api-key.txt"
    if os.path.exists(p):
        with open(p) as f:
            key = f.read().strip()
            if key:
                return key
    try:
        from google.cloud import firestore
        db = firestore.Client(project="chris-pgp-host", database="closecrab")
        doc = db.document("config/global").get()
        return doc.to_dict().get("google_maps_api_key", "")
    except Exception:
        return ""


def get_transit_directions(origin, dest, key):
    r = requests.get(MAPS_URL, params={
        "origin": origin, "destination": dest,
        "mode": "transit", "language": "zh-TW",
        "departure_time": "now", "alternatives": "true", "key": key,
    }, timeout=10)
    data = r.json()
    if data.get("status") != "OK":
        return []
    return data.get("routes", [])


# ── KMB / Citybus API ───────────────────

def get_kmb_eta(route, stop_id, service_type="1"):
    r = requests.get(f"{KMB_BASE}/eta/{stop_id}/{route}/{service_type}", timeout=5)
    return r.json().get("data", [])


def get_ctb_eta(route, stop_id):
    r = requests.get(f"{CTB_BASE}/eta/CTB/{stop_id}/{route}", timeout=5)
    return r.json().get("data", [])


def get_route_info(route):
    return requests.get(f"{KMB_BASE}/route/{route}/outbound/1", timeout=5).json().get("data", {})


def _stop_detail_kmb(stop_id):
    return requests.get(f"{KMB_BASE}/stop/{stop_id}", timeout=5).json().get("data", {})


def _stop_detail_ctb(stop_id):
    return requests.get(f"{CTB_BASE}/stop/{stop_id}", timeout=5).json().get("data", {})


def _route_stops_kmb(route, direction):
    r = requests.get(f"{KMB_BASE}/route-stop/{route}/{direction}/1", timeout=5)
    return r.json().get("data", [])


def _route_stops_ctb(route, direction):
    r = requests.get(f"{CTB_BASE}/route-stop/CTB/{route}/{direction}", timeout=5)
    return r.json().get("data", [])


# ── Stop Resolution ─────────────────────

def _haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def resolve_bus_stop(route_num, dep_lat, dep_lng):
    """Find the KMB/CTB stop closest to departure coordinates for a given route."""
    combos = []
    for direction in ["outbound", "inbound"]:
        for company, getter in [("KMB", _route_stops_kmb), ("CTB", _route_stops_ctb)]:
            try:
                stops = getter(route_num, direction)
                if stops:
                    combos.append((company, direction, stops))
            except Exception:
                pass

    if not combos:
        return None

    all_ids = {}
    for company, _, stops in combos:
        for s in stops:
            all_ids[(s["stop"], company)] = None

    detail_fn = {"KMB": _stop_detail_kmb, "CTB": _stop_detail_ctb}
    with ThreadPoolExecutor(max_workers=15) as ex:
        futures = {ex.submit(detail_fn[co], sid): (sid, co) for (sid, co) in all_ids}
        for f in as_completed(futures):
            try:
                all_ids[futures[f]] = f.result()
            except Exception:
                pass

    best = None
    for company, direction, stops in combos:
        for s in stops:
            detail = all_ids.get((s["stop"], company))
            if not detail:
                continue
            try:
                slat, slng = float(detail["lat"]), float(detail["long"])
            except (KeyError, ValueError, TypeError):
                continue
            dist = _haversine(dep_lat, dep_lng, slat, slng)
            if dist < 500 and (best is None or dist < best["distance_m"]):
                best = {
                    "stop_id": s["stop"], "name": detail.get("name_tc", ""),
                    "seq": s.get("seq"), "direction": direction,
                    "company": company, "distance_m": dist,
                }
    return best


def get_combined_eta(route_num, stop_id):
    """Merge ETAs from KMB + CTB, deduplicated and sorted."""
    etas = []
    for fn in [get_kmb_eta, get_ctb_eta]:
        try:
            etas.extend(fn(route_num, stop_id))
        except Exception:
            pass
    valid = [e for e in etas if e.get("eta")]
    seen, unique = set(), []
    for e in sorted(valid, key=lambda x: x["eta"]):
        if e["eta"] not in seen:
            seen.add(e["eta"])
            unique.append(e)
    return unique


def _eta_minutes(eta_str):
    if not eta_str:
        return None
    try:
        return (datetime.fromisoformat(eta_str) - datetime.now(HKT)).total_seconds() / 60
    except Exception:
        return None


# ── Smart Commute ────────────────────────

def smart_commute(origin, dest=DEFAULT_DEST):
    key = get_maps_key()
    if not key:
        print("❌ 无法获取 Google Maps API key")
        sys.exit(1)

    print(f"🗺️ {origin} → {dest}")
    print(f"⏰ {datetime.now(HKT).strftime('%H:%M:%S')}")

    routes = get_transit_directions(origin, dest, key)
    if not routes:
        print("\n❌ Google Maps 未返回路线（可能已收车或地址无法识别）")
        return

    print(f"\n找到 {len(routes)} 条路线，正在查询实时到站...\n")

    for i, route_data in enumerate(routes[:5]):
        leg = route_data["legs"][0]
        total_dur = leg["duration"]["text"]
        arr_time = leg.get("arrival_time", {}).get("text", "")

        print(f"━━━ 路线 {i + 1} (全程约 {total_dur}) ━━━")

        walk_to_stop_mins = 0
        is_first_transit = True

        for step in leg["steps"]:
            mode = step["travel_mode"]

            if mode == "WALKING":
                walk_to_stop_mins = step["duration"]["value"] / 60
                print(f"🚶 步行 {step['duration']['text']} ({step['distance']['text']})")

            elif mode == "TRANSIT":
                td = step["transit_details"]
                line = td["line"]
                vtype = line.get("vehicle", {}).get("type", "")
                line_name = line.get("short_name", line.get("name", ""))
                dep_stop = td["departure_stop"]["name"]
                arr_stop = td["arrival_stop"]["name"]
                num_stops = td.get("num_stops", "?")

                if vtype == "BUS":
                    dep_loc = td["departure_stop"]["location"]
                    stop_info = resolve_bus_stop(line_name, dep_loc["lat"], dep_loc["lng"])
                    print(f"🚌 {line_name} {dep_stop} → {arr_stop} ({num_stops}站)")

                    if stop_info:
                        etas = get_combined_eta(line_name, stop_info["stop_id"])
                        if etas:
                            for j, e in enumerate(etas[:3]):
                                mins = _eta_minutes(e["eta"])
                                if mins is None:
                                    continue
                                eta_t = datetime.fromisoformat(e["eta"])
                                co = e.get("co", "")
                                mi = int(mins)

                                if mins <= 0:
                                    tag, catch = "即将到站", "❌ 来不及" if is_first_transit else ""
                                elif is_first_transit and mins > walk_to_stop_mins + 2:
                                    tag = f"{mi}分钟后 ({eta_t.strftime('%H:%M')})"
                                    catch = f"✅ 赶得上，余{int(mins - walk_to_stop_mins)}分钟"
                                elif is_first_transit:
                                    tag = f"{mi}分钟后 ({eta_t.strftime('%H:%M')})"
                                    catch = "❌ 来不及"
                                else:
                                    tag = f"{mi}分钟后 ({eta_t.strftime('%H:%M')})"
                                    catch = ""

                                label = "下一班" if j == 0 else f"第{j + 1}班"
                                line_parts = [f"   {label}: {tag}"]
                                if co:
                                    line_parts.append(f"[{co}]")
                                if catch:
                                    line_parts.append(f"— {catch}")
                                print(" ".join(line_parts))
                        else:
                            print("   ⚠️ 暂无实时到站信息")
                    else:
                        print("   ⚠️ 无法匹配实时站点")
                else:
                    emoji = "🚇" if vtype in ("SUBWAY", "HEAVY_RAIL") else "🚆"
                    print(f"{emoji} {line_name} {dep_stop} → {arr_stop} ({step['duration']['text']}, {num_stops}站)")

                is_first_transit = False
                walk_to_stop_mins = 0

        if arr_time:
            print(f"⏱️ 预计 {arr_time} 到达")
        print()


# ── Legacy Mode ──────────────────────────

def query_route(route, stop_override=None):
    route_key = route.upper()
    info = get_route_info(route_key)
    fav = FAVORITE_STOPS.get(route_key) or FAVORITE_STOPS.get(route)
    actual_route = route_key

    if stop_override:
        stop_id, stop_name = stop_override, stop_override
    elif fav:
        stop_id, stop_name = fav["stop"], fav["name"]
        actual_route = fav.get("route", route_key)
    else:
        print(f"路线 {route} 不在常用列表中，请指定 --stop 或使用 --origin")
        sys.exit(1)

    if actual_route != route_key:
        info = get_route_info(actual_route)
    direction = "回家" if fav and fav.get("bound") == "inbound" else "去公司"
    print(f"🚌 {actual_route} ({info.get('orig_tc', '')} → {info.get('dest_tc', '')}) [{direction}]")
    print(f"📍 查询站: {stop_name}")
    print(f"⏰ {datetime.now(HKT).strftime('%H:%M:%S')}")
    print()

    etas = get_combined_eta(actual_route, stop_id)
    if not etas:
        print("  ❌ 目前没有到站信息（可能已收车）")
        return

    for idx, e in enumerate(etas[:5]):
        mins = _eta_minutes(e["eta"])
        if mins is not None and mins <= 0:
            formatted = "即将到站"
        elif mins is not None:
            eta_t = datetime.fromisoformat(e["eta"])
            formatted = f"{int(mins)}分钟后 ({eta_t.strftime('%H:%M:%S')})"
        else:
            formatted = e["eta"]
        co = e.get("co", "")
        rmk = e.get("rmk_tc", "")
        flag = f" ⚠️ {rmk}" if rmk else ""
        company_tag = f" [{co}]" if co else ""
        print(f"  🕐 第{idx + 1}班: {formatted}{company_tag}{flag}")


def list_routes():
    print("常用路线：\n")
    for route, info in FAVORITE_STOPS.items():
        ri = get_route_info(info.get("route", route))
        direction = "回家" if info.get("bound") == "inbound" else "去公司"
        print(f"  {route}: {ri.get('orig_tc', '')} → {ri.get('dest_tc', '')} | {info['name']} [{direction}]")


def main():
    p = argparse.ArgumentParser(description="香港智能通勤助手")
    p.add_argument("route", nargs="?", help="路线号 (如 113)")
    p.add_argument("--origin", help="出发地 (地址或 lat,lng)")
    p.add_argument("--dest", default=DEFAULT_DEST, help="目的地")
    p.add_argument("--stop", help="指定站点 ID")
    p.add_argument("--list", action="store_true", help="列出常用路线")
    p.add_argument("--all", action="store_true", help="查询所有常用路线")
    args = p.parse_args()

    if args.origin:
        smart_commute(args.origin, args.dest)
    elif args.list:
        list_routes()
    elif args.all:
        for route in FAVORITE_STOPS:
            query_route(route)
            print()
    elif args.route:
        query_route(args.route, args.stop)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
