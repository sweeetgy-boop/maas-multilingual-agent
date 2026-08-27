"""
서울시 실시간 도시데이터 (서울열린데이터광장) 연동 계층

  http://openapi.seoul.go.kr:8088/{KEY}/xml/citydata/1/5/{장소명}

한 번에 장소 1개만 조회 가능하고, 서울 주요 121개 장소만 지원한다
(gate/seoul_areas.json — build_areas.py 로 공식 xlsx/shapefile 에서 생성).
응답은 장소당 약 200KB 이므로, 이 모듈은 XML 에서 필요한 필드만 뽑아
얕은 리스트/딕트로 돌려준다. 원본 엘리먼트나 전체 서브트리를 반환하지
않는다 (그대로 넘기면 Supervisor 프롬프트가 터진다).

인증키가 없거나 호출이 실패하면 예외를 던지지 않고 None 을 돌려준다.
호출부(tools.py)는 None 을 "실시간 데이터 없음"으로 해석해 목 데이터로
폴백해야 한다 — 캐시 미동기화나 키 미설정으로 파이프라인 전체가 죽으면 안 된다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

API_KEY_ENV = "SEOUL_OPEN_API_KEY"
BASE_URL = "http://openapi.seoul.go.kr:8088"
CACHE_TTL_SEC = 60
REQUEST_TIMEOUT = 10.0

AREAS: list[dict] = json.loads((Path(__file__).parent / "seoul_areas.json").read_text(encoding="utf-8"))
_AREA_BY_NAME = {a["name"]: a for a in AREAS}

# 여러 장소가 같은 구어체 표현에 겹치는 경우 (예: "잠실"은 관광특구/역/한강공원 등
# 5곳과 겹친다) 일반 규칙만으로는 엉뚱한 곳을 고를 수 있어 자주 쓰는 것만 수동 지정한다.
CURATED_ALIASES = {
    "홍대": "홍대입구역(2호선)", "hongdae": "홍대입구역(2호선)",
    "광화문": "광화문·덕수궁", "gwanghwamun": "광화문·덕수궁",
}


def resolve_area(text: str | None) -> str | None:
    """자유문 지명 → 121개 장소 중 하나의 공식 장소명(citydata 조회에 쓰는 그대로). 없으면 None.
    "강남" 처럼 여러 장소에 겹치는 경우 이름이 가장 짧은(가장 구체적인) 쪽을 고른다."""
    if not text:
        return None
    t = text.strip().casefold()

    if t in CURATED_ALIASES:
        return CURATED_ALIASES[t]

    exact = [a for a in AREAS if a["name"].casefold() == t or (a["eng"] or "").casefold() == t]
    if exact:
        return exact[0]["name"]

    candidates = [a for a in AREAS
                  if t in a["name"].casefold() or a["name"].casefold() in t
                  or t in (a["eng"] or "").casefold() or (a["eng"] or "").casefold() in t]
    if not candidates:
        return None
    candidates.sort(key=lambda a: len(a["name"]))
    return candidates[0]["name"]


def _area_center(area_name: str) -> tuple[float, float] | None:
    a = _AREA_BY_NAME.get(area_name)
    if a is None or a["lat"] is None:
        return None
    return a["lat"], a["lon"]


def _haversine_m(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    r = 6371000.0
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _nearest(area_name: str, rows: list[dict], limit: int) -> list[dict]:
    """lat/lon 이 있는 항목은 장소 중심좌표 기준 가까운 순으로, 없으면 원래 순서로 자른다."""
    center = _area_center(area_name)
    if center is None:
        return rows[:limit]

    def dist(r):
        if r.get("lat") is None or r.get("lon") is None:
            return float("inf")
        return _haversine_m(center, (r["lat"], r["lon"]))

    return sorted(rows, key=dist)[:limit]


# ─────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, ET.Element | None]] = {}


def _fetch_root(area: str) -> ET.Element | None:
    now = time.time()
    cached = _cache.get(area)
    if cached is not None and now - cached[0] < CACHE_TTL_SEC:
        return cached[1]
    root = _call_api(area)
    _cache[area] = (now, root)
    return root


def _call_api(area: str) -> ET.Element | None:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        return None
    url = f"{BASE_URL}/{key}/xml/citydata/1/5/{urllib.parse.quote(area)}"
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            data = resp.read()
        root = ET.fromstring(data)
    except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError):
        return None
    # 오류 응답은 바깥 래퍼 없이 <RESULT><CODE>...가 루트로 오지만, 성공 응답은
    # <SeoulRtd.citydata><RESULT><RESULT.CODE>...(필드명에 점이 붙는다!) 형태다.
    result = root if root.tag == "RESULT" else root.find("RESULT")
    code = ""
    if result is not None:
        code = (result.findtext("CODE") or result.findtext("RESULT.CODE") or "").strip()
    if not code.startswith("INFO-000"):
        return None
    return root


# ─────────────────────────────────────────────────────────
def _text(elem: ET.Element, tag: str) -> str | None:
    v = elem.findtext(tag)
    return v.strip() if v and v.strip() else None


def _num(v: str | None, cast=int):
    if v is None:
        return None
    try:
        return cast(v)
    except ValueError:
        return None


def _latlon(elem: ET.Element, x_tag: str, y_tag: str) -> tuple[float | None, float | None]:
    return _num(_text(elem, x_tag), float), _num(_text(elem, y_tag), float)


# ─────────────────────────────────────────────────────────
def get_subway(area: str, limit: int = 2) -> list[dict] | None:
    """SUB_STTS 는 역별로 SUB_DETAIL(방향별 도착정보) 리스트를 갖는다.
    가장 임박한 것부터: 전역도착/전역출발 > 초단위 카운트다운 > 그 외(운행중 등)."""
    root = _fetch_root(area)
    if root is None:
        return None
    stations = root.findall("CITYDATA/SUB_STTS/SUB_STTS")

    def priority(d: ET.Element) -> tuple[int, int]:
        info = _text(d, "SUB_ARVINFO") or ""
        secs = _num(_text(d, "SUB_ARVTIME")) or 0
        if info in ("전역도착", "전역출발", "출발", "도착"):
            return (0, 0)
        if secs > 0:
            return (1, secs)
        return (2, 0)

    rows = []
    for st in stations:
        station = _text(st, "SUB_STN_NM")
        line = _text(st, "SUB_STN_LINE")
        for d in st.findall("SUB_DETAIL/SUB_DETAIL"):
            rows.append({
                "_pri": priority(d),
                "station": station,
                "line": (f"{line}호선" if line else None),
                "direction": _text(d, "SUB_ROUTE_NM"),
                "message": _text(d, "SUB_ARMG1"),
            })
    rows.sort(key=lambda r: r["_pri"])
    out = []
    for r in rows[:limit]:
        row = {k: v for k, v in r.items() if k != "_pri" and v is not None}
        if row:
            out.append(row)
    return out


def get_elevators(area: str, limit: int = 3) -> list[dict] | None:
    """SUB_FACIINFO 는 역(SUB_STTS) 안에 중첩되어 온다."""
    root = _fetch_root(area)
    if root is None:
        return None
    out = []
    for st in root.findall("CITYDATA/SUB_STTS/SUB_STTS"):
        station = _text(st, "SUB_STN_NM")
        for f in st.findall("SUB_FACIINFO/SUB_FACIINFO"):
            row = {"station": station, "name": _text(f, "ELVTR_NM"),
                   "section": _text(f, "OPR_SEC"), "location": _text(f, "INSTL_PSTN"),
                   "status": _text(f, "USE_YN")}
            out.append({k: v for k, v in row.items() if v is not None})
            if len(out) >= limit:
                return out
    return out


def get_bus_stops(area: str, limit: int = 3) -> list[dict] | None:
    root = _fetch_root(area)
    if root is None:
        return None
    rows = []
    for it in root.findall("CITYDATA/BUS_STN_STTS/BUS_STN_STTS"):
        lon, lat = _latlon(it, "BUS_STN_X", "BUS_STN_Y")
        stop = _text(it, "BUS_STN_NM")
        if not stop:
            continue
        rows.append({"stop": stop, "ars_id": _text(it, "BUS_ARS_ID"), "lat": lat, "lon": lon})
    nearest = _nearest(area, rows, limit)
    return [{k: v for k, v in r.items() if k not in ("lat", "lon") and v is not None} for r in nearest]


def get_bike(area: str, limit: int = 3) -> list[dict] | None:
    root = _fetch_root(area)
    if root is None:
        return None
    rows = []
    for it in root.findall("CITYDATA/SBIKE_STTS/SBIKE_STTS"):
        lon, lat = _latlon(it, "SBIKE_X", "SBIKE_Y")
        station = _text(it, "SBIKE_SPOT_NM")
        if not station:
            continue
        rows.append({"station": station,
                     "bikes_available": _num(_text(it, "SBIKE_PARKING_CNT")),
                     "docks_free": _num(_text(it, "SBIKE_RACK_CNT")),
                     "lat": lat, "lon": lon})
    nearest = _nearest(area, rows, limit)
    return [{k: v for k, v in r.items() if k not in ("lat", "lon") and v is not None} for r in nearest]


def get_parking(area: str, limit: int = 3) -> list[dict] | None:
    """CUR_PRK_YN 이 'N' 이면 실시간 잔여면수를 제공하지 않는 주차장이므로
    available 필드를 아예 비운다 (호출부가 없는 정보를 지어내지 않도록)."""
    root = _fetch_root(area)
    if root is None:
        return None
    rows = []
    for it in root.findall("CITYDATA/PRK_STTS/PRK_STTS"):
        name = _text(it, "PRK_NM")
        if not name:
            continue
        row = {"name": name, "capacity": _num(_text(it, "CPCTY")),
               "lat": _num(_text(it, "LAT"), float), "lon": _num(_text(it, "LNG"), float),
               "base_fee_krw": _num(_text(it, "RATES")), "base_minutes": _num(_text(it, "TIME_RATES"))}
        if _text(it, "CUR_PRK_YN") == "Y":
            row["available"] = _num(_text(it, "CUR_PRK_CNT"))
        rows.append(row)
    nearest = _nearest(area, rows, limit)
    return [{k: v for k, v in r.items() if k not in ("lat", "lon") and v is not None} for r in nearest]


def get_ev_charger(area: str, limit: int = 3) -> list[dict] | None:
    """CHARGER_STAT 이 '사용가능' 인 충전기만 available_count 에 센다.
    '충전중', '상태미확인', '점검중' 등은 total_count 에는 포함하되 가용 대수로는 세지 않는다.
    usetime/limit_detail 은 충전기가 아니라 충전소(STAT_*) 단위 정보라 각 타입 행에 그대로 복제한다."""
    root = _fetch_root(area)
    if root is None:
        return None
    stations = root.findall("CITYDATA/CHARGER_STTS/CHARGER_STTS")

    def station_rows(st: ET.Element) -> list[dict]:
        name = _text(st, "STAT_NM")
        if not name:
            return []
        usetime = _text(st, "STAT_USETIME")
        limit_detail = _text(st, "STAT_LIMITDETAIL") if _text(st, "STAT_LIMITYN") == "Y" else None
        by_type: dict[str, dict] = {}
        for u in st.findall("CHARGER_DETAILS/CHARGER_DETAILS"):
            ctype = _text(u, "CHARGER_TYPE") or "미상"
            status = _text(u, "CHARGER_STAT") or ""
            kw = _num(_text(u, "OUTPUT"))
            bucket = by_type.setdefault(ctype, {
                "station": name, "type": ctype, "available_count": 0, "total_count": 0,
                "lat": _num(_text(st, "STAT_Y"), float), "lon": _num(_text(st, "STAT_X"), float),
            })
            bucket["total_count"] += 1
            if status == "사용가능":
                bucket["available_count"] += 1
            if kw is not None and "output_kw" not in bucket:
                bucket["output_kw"] = kw
            if usetime and "usetime" not in bucket:
                bucket["usetime"] = usetime
            if limit_detail and "limit_detail" not in bucket:
                bucket["limit_detail"] = limit_detail
        return list(by_type.values())

    all_rows = [r for st in stations for r in station_rows(st)]
    # 충전소(station) 단위로 가까운 순 정렬한 뒤, 뽑힌 충전소의 타입 행을 모두 붙인다.
    by_station: dict[str, list[dict]] = {}
    for r in all_rows:
        by_station.setdefault(r["station"], []).append(r)
    reps = [rows[0] for rows in by_station.values()]
    nearest_stations = {r["station"] for r in _nearest(area, reps, limit)}
    out = [r for r in all_rows if r["station"] in nearest_stations]
    return [{k: v for k, v in r.items() if k not in ("lat", "lon")} for r in out]


def get_context(area: str) -> dict | None:
    """부가정보: 혼잡도 / 가장 가까운 사고통제 1건 / 가장 가까운 문화행사 1건. 있는 것만 채운다."""
    root = _fetch_root(area)
    if root is None:
        return None
    ctx: dict = {}

    congestion = root.findtext("CITYDATA/LIVE_PPLTN_STTS/LIVE_PPLTN_STTS/AREA_CONGEST_LVL")
    if congestion and congestion.strip():
        ctx["congestion"] = congestion.strip()

    accidents = []
    for a in root.findall("CITYDATA/ACDNT_CNTRL_STTS/ACDNT_CNTRL_STTS"):
        info = _text(a, "ACDNT_INFO")
        if info:
            lon, lat = _latlon(a, "ACDNT_X", "ACDNT_Y")
            accidents.append({"info": info, "lat": lat, "lon": lon})
    nearest_acc = _nearest(area, accidents, 1)
    if nearest_acc:
        ctx["alert"] = nearest_acc[0]["info"]

    events = []
    for e in root.findall("CITYDATA/EVENT_STTS/EVENT_STTS"):
        name = _text(e, "EVENT_NM")
        if name:
            lon, lat = _latlon(e, "EVENT_X", "EVENT_Y")
            events.append({"name": name, "period": _text(e, "EVENT_PERIOD"), "lat": lat, "lon": lon})
    nearest_ev = _nearest(area, events, 1)
    if nearest_ev:
        ev = nearest_ev[0]
        ctx["event"] = f"{ev['name']} ({ev['period']})" if ev.get("period") else ev["name"]

    return ctx or None


SECTION_FUNCS = {
    "subway": get_subway,
    "elevators": get_elevators,
    "bus": get_bus_stops,
    "bike": get_bike,
    "parking": get_parking,
    "ev_charger": get_ev_charger,
    "context": get_context,
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", required=True)
    ap.add_argument("--section", choices=sorted(SECTION_FUNCS), default="subway")
    a = ap.parse_args()

    if not os.environ.get(API_KEY_ENV):
        print(f"경고: {API_KEY_ENV} 미설정 — None 이 반환됩니다", file=sys.stderr)

    resolved = resolve_area(a.area) or a.area
    result = SECTION_FUNCS[a.section](resolved)
    print(json.dumps({"area": resolved, "section": a.section, "result": result},
                      ensure_ascii=False, indent=2))
