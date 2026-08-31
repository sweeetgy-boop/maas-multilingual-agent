"""
교통 도구 계층 — 3일 PoC용 목 구현

실제 운영에서는 각 함수를 TAGO / 코레일 / 한국공항공사 / TourAPI /
GBFS / OpenTripPlanner 호출로 교체한다. 반환 스키마는 그대로 유지하면
Supervisor 프롬프트를 고칠 필요가 없다.

핵심 설계 원칙
  - 도구는 결정론적이다. LLM 이 시각·요금을 만들어내지 않는다.
  - 반환 JSON 의 값만 렌더링하고, 모델이 숫자를 재작성하지 못하게 한다.
  - 모든 응답에 data_source 와 retrieved_at 을 포함해 검증 계층이 대조한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import citydata_api
import expbus_api
import korail_api
import odsay_api
from geocode import geocode
from transit_nodes import NODE_BY_ID, find_access_points

COVERED_AREA_LABEL = "서울 주요 121개 장소"

# 서울 전용 실시간 데이터 도구가 121장소 밖 지명을 받았을 때 쓰는 서비스별 안내 문구.
SERVICE_NOTE = {
    "share_mobility": "따릉이는 서울시 공공자전거로 서울 지역에서만 운영됩니다",
    "search_parking": "실시간 주차정보는 서울시 주요 지점에서만 제공됩니다",
    "search_ev_charger": "실시간 충전소 정보는 서울시 주요 지점에서만 제공됩니다",
    "get_realtime_status": "실시간 지하철 도착정보는 서울시 주요 역에서만 제공됩니다",
}


def _not_covered(requested: str | None, service: str) -> dict:
    """121장소 밖 지명에 대해 목 데이터로 폴백하지 않고 커버리지 밖임을 명시한다.
    목 데이터가 실데이터처럼 보이면 사용자가 현장에 가서 아무것도 없는 상황이 된다."""
    return {"found": False, "reason": "location_not_covered",
            "requested": requested, "covered_area": COVERED_AREA_LABEL,
            "service_note": SERVICE_NOTE[service]}

# ── 지명 정규화 사전 (다국어 별칭 → 표준 ID) ──
# 실제로는 임베딩 + FAISS 로 처리하지만, PoC 는 사전으로 충분하다.
PLACE_ALIASES = {
    "서울역": "SEOUL", "seoul station": "SEOUL", "首尔站": "SEOUL",
    "ソウル駅": "SEOUL", "stasiun seoul": "SEOUL", "seoul": "SEOUL",
    "부산역": "BUSAN", "busan station": "BUSAN", "釜山站": "BUSAN",
    "釜山駅": "BUSAN", "stasiun busan": "BUSAN", "busan": "BUSAN", "부산": "BUSAN",
    "대전역": "DAEJEON", "daejeon station": "DAEJEON", "대전": "DAEJEON",
    "동대구역": "DONGDAEGU", "대구": "DONGDAEGU",
    "광주송정역": "GWANGJU", "광주": "GWANGJU",
    "인천공항": "ICN", "incheon airport": "ICN", "incheon": "ICN",
    "仁川空港": "ICN", "仁川国际机场": "ICN", "bandara incheon": "ICN",
    "김포공항": "GMP", "gimpo airport": "GMP",
    "제주": "CJU", "jeju": "CJU", "제주공항": "CJU", "济州": "CJU",
    "김해공항": "PUS", "gimhae airport": "PUS",
    "동서울터미널": "DONGSEOUL", "dong seoul terminal": "DONGSEOUL",
    "속초": "SOKCHO", "sokcho": "SOKCHO",
    "서산": "SEOSAN", "seosan": "SEOSAN",
    "강남역": "GANGNAM", "gangnam station": "GANGNAM", "강남": "GANGNAM",
    "홍대": "HONGDAE", "hongdae": "HONGDAE",
    "여의도": "YEOUIDO", "yeouido": "YEOUIDO",
    "광명역": "GWANGMYEONG", "gwangmyeong station": "GWANGMYEONG",
    "수서역": "SUSEO", "suseo": "SUSEO",
}

PLACE_NAMES = {
    "SEOUL": "서울역", "BUSAN": "부산역", "DAEJEON": "대전역",
    "DONGDAEGU": "동대구역", "GWANGJU": "광주송정역", "ICN": "인천국제공항",
    "GMP": "김포공항", "CJU": "제주국제공항", "PUS": "김해국제공항",
    "DONGSEOUL": "동서울종합터미널", "SOKCHO": "속초시외버스터미널",
    "SEOSAN": "서산공용버스터미널", "GANGNAM": "강남역",
    "HONGDAE": "홍대입구역", "YEOUIDO": "여의도역",
    "GWANGMYEONG": "광명역", "SUSEO": "수서역",
}


def _place_from_id(pid: str, fallback_name: str) -> dict:
    """레거시 PLACE_ALIASES 표준 ID를 신규 반환 구조로 감싼다.
    transit_nodes.json 에 같은 id 로 등록된 노드가 있으면(17개 레거시
    허브는 그렇게 등록해 뒀다) 좌표까지 채운다."""
    node = NODE_BY_ID.get(pid)
    if node is not None:
        return {"id": pid, "name": node["name"], "lat": node["lat"], "lon": node["lon"],
                "access_points": [{"name": node["name"], "type": node["type"], "distance_m": 0,
                                   "lat": node["lat"], "lon": node["lon"]}]}
    return {"id": pid, "name": fallback_name, "lat": None, "lon": None,
            "access_points": [{"name": fallback_name, "type": "unknown", "distance_m": 0}]}


def resolve_place(text: str | None) -> dict | None:
    """자유문 지명 → {id, name, lat, lon, access_points}. 모든 도구의 선행 단계.

    순서:
      1. 기존 PLACE_ALIASES **정확히 일치**하는 경우만 즉시 반환 (하위 호환,
         17개 철도역/공항/터미널 — 외부 조회 없이 가장 빠르다).
      2. geocode(): transit_nodes.json 직접 매칭 → admin_areas.json → (선택)
         외부 API 순으로 시도한다. transit_node 매칭이면 그 자체가 접근점,
         아니면 좌표 기준 find_access_points() 로 접근점을 채운다.
      3. 그래도 못 찾으면 PLACE_ALIASES **부분 문자열** 매칭을 최후 폴백으로
         쓴다. 이걸 2단계보다 먼저 하면 "부산 광안리"가 "부산"에 부분
         일치해 부산역으로 잘못 해소되는 문제가 재발한다 — 반드시 이 순서.
    """
    if not text:
        return None
    t = text.strip().casefold()

    if t in PLACE_ALIASES:
        pid = PLACE_ALIASES[t]
        return _place_from_id(pid, PLACE_NAMES.get(pid, text.strip()))

    g = geocode(text)
    if g is not None:
        if g["source"] == "transit_node":
            return {"id": g["id"], "name": g["name"], "lat": g["lat"], "lon": g["lon"],
                    "access_points": [{"name": g["name"], "type": g["type"], "distance_m": 0,
                                       "lat": g["lat"], "lon": g["lon"]}]}
        return {"id": g["id"], "name": g["name"], "lat": g["lat"], "lon": g["lon"],
                "access_points": find_access_points(g["lat"], g["lon"])}

    for alias, pid in PLACE_ALIASES.items():
        if alias in t or t in alias:
            return _place_from_id(pid, PLACE_NAMES.get(pid, text.strip()))

    return None


def resolve_place_coords(lat: float, lon: float, name: str | None = None) -> dict:
    """좌표를 직접 받아 지오코딩을 건너뛰고 접근점만 채운다 (resolve_place 와
    같은 반환 모양). ui/api.py 가 origin_coords/destination_coords 를 받았을
    때 쓴다 — "내 위치" 처럼 텍스트 지명이 없는 경우."""
    return {"id": f"COORD-{lat:.5f},{lon:.5f}", "name": name or "현재 위치",
            "lat": lat, "lon": lon, "access_points": find_access_points(lat, lon)}


def _base_date(dt_hint: str | None) -> datetime:
    now = datetime.now().replace(second=0, microsecond=0)
    h = (dt_hint or "").casefold()
    if any(k in h for k in ("내일", "tomorrow", "besok", "明日", "明天")):
        now += timedelta(days=1)
    if any(k in h for k in ("오후", "afternoon", "sore", "午後")):
        return now.replace(hour=13, minute=0)
    if any(k in h for k in ("아침", "오전", "morning", "pagi", "朝")):
        return now.replace(hour=7, minute=0)
    if any(k in h for k in ("밤", "night", "malam", "夜")):
        return now.replace(hour=21, minute=0)
    return now


def _stamp(payload: dict, source: str, disclaimer: str | None = None) -> dict:
    """disclaimer 를 넘기면 기본 문구 대신 쓴다. Supervisor 규칙 6 이
    disclaimer 를 반드시 출력하므로, 꼭 전달돼야 하는 단서(예: 실시간이
    아니라 과거 실적 기준이라는 사실)를 여기 실으면 누락되지 않는다."""
    payload["data_source"] = source
    payload["retrieved_at"] = datetime.now().isoformat(timespec="seconds")
    payload["disclaimer"] = disclaimer or (
        "실시간 운행정보는 참고용이며, 최종 확인은 운영기관 공식 채널을 이용하세요.")
    return payload


def _add_context(result: dict, destination_name: str | None) -> None:
    """경로·시간표 조회가 성공했고 목적지가 서울 121장소에 해당하면 혼잡도/
    사고통제/문화행사 부가정보를 붙인다. 121장소 밖이거나 데이터가 없으면
    아무 것도 하지 않는다 — context 키 자체를 넣지 않는다."""
    if not destination_name:
        return
    area = citydata_api.resolve_area(destination_name)
    if not area:
        return
    ctx = citydata_api.get_context(area)
    if ctx:
        result["context"] = ctx


# ─────────────────────────────────────────────────────────
RAIL_TABLE = {
    ("SEOUL", "BUSAN"): [("KTX 101", 158, 59800), ("KTX 105", 165, 59800), ("KTX-이음 213", 178, 47900)],
    ("SEOUL", "DAEJEON"): [("KTX 103", 62, 23700), ("KTX 107", 68, 23700), ("무궁화 1201", 108, 11800)],
    ("SEOUL", "DONGDAEGU"): [("KTX 111", 112, 43500), ("KTX 115", 119, 43500)],
    ("SUSEO", "BUSAN"): [("SRT 301", 152, 52600), ("SRT 305", 159, 52600)],
    ("SUSEO", "DAEJEON"): [("SRT 303", 61, 21100), ("SRT 307", 66, 21100)],
    ("DAEJEON", "BUSAN"): [("KTX 121", 95, 36900), ("무궁화 1305", 168, 17300)],
}


def _unresolved(origin, o, destination, d, source: str) -> dict:
    unresolved = [x for x, r in ((origin, o), (destination, d)) if x and not r]
    return _stamp({"found": False, "reason": "unresolved_place",
                   "unresolved": unresolved,
                   "hint": "입력하신 지명을 인식하지 못했습니다. 정확한 역명, 터미널명, "
                           "공항명 또는 시/군/구 명을 입력해 주세요."},
                  source)


def pick_anchor(origin: str | None, destination: str | None, carried=()) -> str | None:
    """"<장소> 근처 X" 류 도구의 기준점 선택.

    버그: 이전 턴에서 승계된 슬롯은 이번 턴 사용자가 실제로 말한 장소가
    아니다. "서울역→부산역 KTX" 다음에 "광명역 근처 호텔"이라고 하면
    게이트는 origin=광명역만 채우고 destination 은 비워서 돌려주는데,
    비어 있던 destination 이 직전 턴의 "부산역"을 승계해버리면 광명역
    근처를 물었는데 부산역 호텔이 나가는 사고가 난다.

    원칙: 이번 턴에 사용자가 실제로 말한(승계되지 않은) 값만 기준점이
    될 수 있다. destination 우선 → origin 순, 둘 다 승계값이면(예: "그럼
    거기서 대중교통은?" 처럼 이번 턴에 새 장소가 아예 없는 경우) 그대로
    destination → origin 순으로 승계값을 쓴다."""
    fresh = [v for k, v in (("destination", destination), ("origin", origin))
             if v and k not in carried]
    if fresh:
        return fresh[0]
    return destination or origin


def search_rail(origin: str | None, destination: str | None,
                datetime_hint: str | None = None, pax: int | None = None, **_) -> dict:
    o, d = resolve_place(origin), resolve_place(destination)
    if not o or not d:
        return _unresolved(origin, o, destination, d, "KORAIL/SR OpenAPI (mock)")

    rows = RAIL_TABLE.get((o["id"], d["id"])) or RAIL_TABLE.get((d["id"], o["id"]))
    if not rows:
        return _stamp({"found": False, "reason": "no_direct_service",
                       "origin": o["name"], "destination": d["name"]},
                      "KORAIL/SR OpenAPI (mock)")

    base = _base_date(datetime_hint)
    n = pax or 1
    trains = []
    for i, (name, mins, fare) in enumerate(rows):
        dep = base + timedelta(minutes=25 * i)
        trains.append({
            "train": name,
            "departure": dep.strftime("%Y-%m-%d %H:%M"),
            "arrival": (dep + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M"),
            "duration_min": mins,
            "fare_krw": fare * n,
            "seats_available": True,
        })
    result = {"found": True, "origin": o["name"],
              "destination": d["name"], "pax": n, "trains": trains}
    _add_context(result, d["name"])
    return _stamp(result, "KORAIL/SR OpenAPI (mock)")


# TAGO 실데이터가 없을 때(키 미설정·쿼터 소진·API 장애)만 쓰는 목값.
# 실서비스 요금대에 맞춰 두되, data_source 에 (mock) 이 찍히므로 사용자와
# 평가기 모두 실데이터와 구별할 수 있다. 주요 도시간 조합을 비워 두면
# 폴백이 "노선 없음"으로 답해 버려서, 실제로 있는 노선을 없다고 말하게 된다.
BUS_TABLE = {
    ("DONGSEOUL", "SOKCHO"): [("동서울-속초 우등", 145, 22400), ("동서울-속초 일반", 160, 15200)],
    ("SEOUL", "SEOSAN"): [("서울-서산 우등", 118, 14300), ("서울-서산 일반", 130, 9700)],
    ("SEOUL", "BUSAN"): [("서울경부-부산 우등", 250, 39700), ("서울경부-부산 고속", 260, 26700)],
    ("SEOUL", "DAEJEON"): [("서울경부-대전복합 우등", 120, 16600), ("서울경부-대전복합 고속", 125, 11400)],
    ("DONGSEOUL", "RAIL-강릉"): [("동서울-강릉 우등", 150, 22300), ("동서울-강릉 고속", 160, 15100)],
    ("BUSAN", "PUS"): [("부산역-김해공항 리무진", 55, 7500)],
    ("ICN", "SEOUL"): [("공항리무진 6001", 70, 18000), ("공항리무진 6015", 85, 18000)],
}


def _bus_result_from_expbus(real: dict, pax: int | None) -> dict:
    """expbus_api.search() 의 반환(내부 전용 필드 date 포함)을 search_bus
    의 확정 스키마로 변환한다. seats_available 은 TAGO 응답에 없으므로
    (작업 0 실측) 채우지 않는다 — 없는 정보는 필드를 아예 넣지 않는다."""
    n = pax or 1
    if not real.get("found"):
        return {"found": False, "reason": real.get("reason", "no_direct_service"),
                "origin": real.get("origin"), "destination": real.get("destination")}
    buses = []
    for b in real.get("buses", []):
        row = {"route": b["route"], "departure": b["departure"]}
        if "duration_min" in b:
            row["duration_min"] = b["duration_min"]
        row["fare_krw"] = b["fare_krw"] * n
        if "grade" in b:
            row["grade"] = b["grade"]
        buses.append(row)
    return {"found": True, "origin": real["origin"], "destination": real["destination"],
            "pax": n, "buses": buses}


def search_bus(origin=None, destination=None, datetime_hint=None, pax=None, carried=(), **_) -> dict:
    """도시 간 이동(출발/도착이 서로 다른 장소로 둘 다 해소됨)이면 TAGO
    고속버스정보를 우선 조회한다. 실패하거나(터미널 없음·API 오류) 근거리
    질의("<장소> 근처 버스정류장" 류, 장소가 하나뿐)면 서울 121장소 안일
    때 실시간 버스정류소 위치로 폴백하고, 그마저 없으면 목 데이터를 쓴다."""
    o, d = resolve_place(origin), resolve_place(destination)

    if o and d and o["id"] != d["id"]:
        date_str = _base_date(datetime_hint).strftime("%Y%m%d")
        real = expbus_api.search(origin, destination, date_str)
        if real is not None:
            result = _bus_result_from_expbus(real, pax)
            if result["found"]:
                _add_context(result, d["name"])
            # 고속버스 시간표는 D+2 까지만 공개된다. 더 뒤 날짜를 물으면
            # 조회 가능한 마지막 날로 당겨 조회되므로, 요청한 날짜의
            # 시간표가 아니라는 사실을 disclaimer 에 실어 보낸다
            # (Supervisor 규칙 6 이 disclaimer 를 반드시 출력한다).
            note = None
            if real.get("date_clamped"):
                shown = datetime.strptime(real["date"], "%Y%m%d").strftime("%Y-%m-%d")
                note = (f"고속버스 시간표는 오늘부터 2일 뒤까지만 공개됩니다. "
                        f"문의하신 날짜 대신 조회 가능한 마지막 날짜({shown}) 기준으로 "
                        f"안내합니다. 이후 날짜는 예매 개시 후 확인하세요.")
            return _stamp(result, "TAGO 고속버스정보 (국토교통부, 공공데이터포털)", note)

        rows = BUS_TABLE.get((o["id"], d["id"])) or BUS_TABLE.get((d["id"], o["id"]))
        if rows:
            base = _base_date(datetime_hint)
            n = pax or 1
            result = {"found": True, "origin": o["name"], "destination": d["name"],
                      "pax": n,
                      "buses": [{"route": r, "departure": (base + timedelta(minutes=40 * i)).strftime("%Y-%m-%d %H:%M"),
                                 "duration_min": m, "fare_krw": f * n}
                                for i, (r, m, f) in enumerate(rows)]}
            _add_context(result, d["name"])
            return _stamp(result, "TAGO 버스 API (mock)")
        return _stamp({"found": False, "reason": "no_direct_service",
                       "origin": o["name"], "destination": d["name"]}, "TAGO 버스 API (mock)")

    anchor = pick_anchor(origin, destination, carried)
    area = citydata_api.resolve_area(anchor) if anchor else None
    if area:
        stops = citydata_api.get_bus_stops(area)
        if stops:
            return _stamp({"found": True, "near": area, "stops": stops},
                          "서울시 실시간 도시데이터 (버스정류소)")

    if not o or not d:
        return _unresolved(origin, o, destination, d, "TAGO 버스 API (mock)")
    return _stamp({"found": False, "reason": "no_direct_service",
                   "origin": o["name"], "destination": d["name"]}, "TAGO 버스 API (mock)")


FLIGHT_TABLE = {
    ("ICN", "CJU"): [("KE1201", 70, 89000), ("OZ8905", 75, 82000), ("7C101", 70, 54000)],
    ("GMP", "CJU"): [("KE1231", 65, 78000), ("LJ301", 70, 49000)],
    ("GMP", "PUS"): [("KE1401", 55, 71000), ("BX8801", 60, 45000)],
}


def search_flight(origin=None, destination=None, datetime_hint=None, pax=None, **_) -> dict:
    o, d = resolve_place(origin), resolve_place(destination)
    if not o or not d:
        return _unresolved(origin, o, destination, d, "한국공항공사 API (mock)")
    rows = FLIGHT_TABLE.get((o["id"], d["id"])) or FLIGHT_TABLE.get((d["id"], o["id"]))
    if not rows:
        return _stamp({"found": False, "reason": "no_direct_service"}, "한국공항공사 API (mock)")
    base = _base_date(datetime_hint)
    n = pax or 1
    result = {"found": True, "origin": o["name"], "destination": d["name"],
              "pax": n,
              "flights": [{"flight_no": f, "departure": (base + timedelta(minutes=90 * i)).strftime("%Y-%m-%d %H:%M"),
                           "duration_min": m, "fare_krw": p * n}
                          for i, (f, m, p) in enumerate(rows)]}
    _add_context(result, d["name"])
    return _stamp(result, "한국공항공사 API (mock)")


def search_lodging(origin=None, destination=None, datetime_hint=None,
                   pax=None, carried=(), **_) -> dict:
    anchor = pick_anchor(origin, destination, carried)
    d = resolve_place(anchor)
    if not d:
        return _stamp({"found": False, "reason": "unresolved_place",
                       "unresolved": [x for x in (destination, origin) if x],
                       "hint": "입력하신 지명을 인식하지 못했습니다. 정확한 역명, 터미널명, "
                               "공항명 또는 시/군/구 명을 입력해 주세요."},
                      "한국관광공사 TourAPI (mock)")
    name = d["name"]
    return _stamp({"found": True, "near": name,
                   "hotels": [
                       {"name": f"{name} 스테이션 호텔", "distance_m": 220, "price_krw": 98000, "rating": 4.2},
                       {"name": f"{name} 비즈니스 인", "distance_m": 450, "price_krw": 72000, "rating": 3.9},
                       {"name": f"{name} 게스트하우스", "distance_m": 610, "price_krw": 38000, "rating": 4.0},
                   ]}, "한국관광공사 TourAPI (mock)")


def search_share_mobility(origin=None, destination=None, pax=None, carried=(), **_) -> dict:
    """따릉이는 서울시 공공자전거로 서울에서만 운영된다. 121장소 밖 지명이면
    목 데이터로 폴백하지 않고 커버리지 밖임을 명시적으로 반환한다."""
    anchor = pick_anchor(origin, destination, carried)
    area = citydata_api.resolve_area(anchor) if anchor else None
    if not area:
        return _not_covered(anchor, "share_mobility")

    bikes = citydata_api.get_bike(area)
    if bikes:
        return _stamp({"found": True, "near": area,
                       "bike_stations": bikes,
                       "car_share": [
                           {"operator": "카셰어A", "spot": f"{area} 공영주차장", "distance_m": 320,
                            "car": "경차", "price_per_10min_krw": 1200},
                       ]}, "서울시 실시간 도시데이터(따릉이) + 제휴 API(카셰어링, mock)")

    # 키 미설정·캐시 미동기화 등으로 실시간 데이터를 못 받아도 121장소 안이면
    # 목 데이터로 폴백한다 — API 실패가 곧 서비스 중단이 되면 안 된다.
    return _stamp({"found": True, "near": area,
                   "bike_stations": [
                       {"station": f"{area} 1번 출구", "distance_m": 80, "bikes_available": 7, "docks_free": 12},
                       {"station": f"{area} 앞 광장", "distance_m": 150, "bikes_available": 3, "docks_free": 18},
                   ],
                   "car_share": [
                       {"operator": "카셰어A", "spot": f"{area} 공영주차장", "distance_m": 320,
                        "car": "경차", "price_per_10min_krw": 1200},
                   ]}, "GBFS (mock)")


_RAIL_MOCK_LINES = [
    {"line": "수도권 1호선", "status": "정상운행", "delay_min": 0},
    {"line": "경부선 KTX", "status": "지연", "delay_min": 8, "cause": "선행열차 지연"},
]


def _rail_station_name(place: dict | None) -> str | None:
    """resolve_place 결과가 간선철도역이면 코레일이 쓰는 역명을 돌려준다.
    코레일 데이터의 역명에는 '역'이 붙지 않는다(서울역 → 서울)."""
    if not place:
        return None
    node = NODE_BY_ID.get(place.get("id"))
    if not node or node.get("type") != "rail":
        return None
    name = (node.get("name") or "").strip()
    return name[:-1] if name.endswith("역") else name or None


def _rail_line_name(text: str | None) -> str | None:
    """질의에 코레일 주운행선명이 들어 있으면 그 노선명을 돌려준다.
    "경부선 KTX", "지금 경부선" 처럼 수식어가 붙어도 잡히게 부분일치로 본다.
    긴 이름부터 보는 이유: "경부선"과 "경부선고속"이 둘 다 있으면 더 구체적인
    쪽이 맞다."""
    if not text:
        return None
    names = korail_api.load_lines()
    if not names:
        return None
    for name in sorted(names, key=len, reverse=True):
        if len(name) >= 3 and name in text:
            return name
    return None


def _rail_delay_status(station: str | None = None,
                       line: str | None = None) -> dict | None:
    """코레일 운행실적으로 지연 현황을 만든다. 실패 시 None.
    station 은 시발역, line 은 주운행선명으로 거른다.

    ⚠ 이 API 에는 당일·미래 데이터가 없다(korail_api 작업 0 실측). 최신
    실적일(실측상 어제) 기준이므로 "실시간"이라고 말하면 안 된다. 기준일은
    disclaimer 에 실어 보낸다 — Supervisor 규칙 6 이 disclaimer 를 반드시
    출력하므로 이 단서가 누락되지 않는다.

    lines 항목 스키마는 목 데이터와 동일하게 line/status/delay_min 만 쓴다.
    지연 사유(cause)는 API 가 주지 않으므로 필드를 넣지 않는다 — 넣지
    않으면 Supervisor 가 언급하지 않고, 지어내면 검증 계층이 차단한다."""
    hist = korail_api.delay_history(station=station, line=line, limit=5)
    if not hist or not hist.get("found"):
        return None

    lines = [{"line": f"{t['origin']}-{t['destination']} {t['train_no']}열차",
              "status": "지연", "delay_min": t["delay_min"]}
             for t in hist.get("trains", [])]
    if not lines:
        # 비교 대상은 있었는데 지연이 한 편도 없었던 경우.
        subject = line or f"{station} 출발 열차"
        lines = [{"line": subject, "status": "정상운행", "delay_min": 0}]

    ymd = hist["data_date"]
    date_label = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    return _stamp(
        {"found": True, "lines": lines},
        "한국철도공사 열차운행정보 (공공데이터포털)",
        f"{date_label} 운행 실적 기준이며 실시간 정보가 아닙니다. "
        f"당일 운행상황은 코레일 공식 채널에서 확인하세요.")


def get_realtime_status(origin=None, destination=None, pax=None, carried=(), **_) -> dict:
    """분기 순서
      1. 서울시 121장소 안 → 실시간 지하철 도착정보(citydata).
      2. 121장소 밖이지만 간선철도역 → 코레일 운행실적 기반 지연(전국 서비스).
      3. 그 외 지명 → 커버리지 밖임을 명시한다. 관계 없는 수도권 노선 목
         데이터를 그 지명의 답인 것처럼 주면 안 된다.
      4. 지명 없음 → 전국 단위 목 데이터.

    2번은 "실시간"이 아니다. 코레일 API 에는 당일·미래 데이터가 없어 최신
    실적일 기준이며, 그 사실은 _rail_delay_status 가 disclaimer 에 싣는다."""
    anchor = pick_anchor(origin, destination, carried)
    area = citydata_api.resolve_area(anchor) if anchor else None

    if area:
        rows = citydata_api.get_subway(area)
        if rows:
            lines = [{"line": f"{r['station']} {r['line']}" if r.get("line") else r["station"],
                      **({"status": r["message"]} if r.get("message") else {}),
                      **({"direction": r["direction"]} if r.get("direction") else {})}
                     for r in rows]
            return _stamp({"found": True, "lines": lines}, "서울시 실시간 도시데이터 (지하철 도착정보)")
        # 121장소 안인데 키 미설정 등으로 실시간 데이터를 못 받으면 목 데이터로
        # 폴백한다. 코레일 실적으로 대체하지 않는다 — 이 분기가 답해야 하는
        # 것은 그 지역 지하철 도착정보이고, 간선철도 지연은 그 답이 아니다.
        return _stamp({"found": True, "lines": [dict(x) for x in _RAIL_MOCK_LINES]},
                      "GTFS-RT (mock)")

    if anchor:
        # 서울 121장소 밖이라도 간선철도역이면 코레일 실적으로 답할 수 있다.
        # 철도는 전국 서비스라 location_not_covered 가 맞지 않는다.
        station = _rail_station_name(resolve_place(anchor))
        # "경부선" 처럼 역이 아니라 노선을 가리킨 경우도 철도 질의다.
        line = None if station else _rail_line_name(anchor)
        if station or line:
            rail = _rail_delay_status(station=station, line=line)
            if rail:
                return rail
            # 철도는 전국 서비스다. 키 미설정·조회 실패로 실데이터를 못 받아도
            # 커버리지 밖이 아니므로 목 데이터로 폴백한다(서울 전용 도구들과
            # 다른 점이다).
            return _stamp({"found": True, "lines": [dict(x) for x in _RAIL_MOCK_LINES]},
                          "GTFS-RT (mock)")
        return _not_covered(anchor, "get_realtime_status")

    # 지명 없이 일반적인 지연 여부를 물은 경우("1호선 지금 지연돼?").
    # 여기서 코레일 간선 실적을 주면 안 된다 — 지하철 1호선을 물었는데
    # 경부선 지연으로 답하게 된다. 전국 단위 실시간 지하철 피드가 없으므로
    # 이 분기는 목 데이터로 남긴다.
    return _stamp({"found": True, "lines": [dict(x) for x in _RAIL_MOCK_LINES]},
                  "GTFS-RT (mock)")


def search_parking(origin=None, destination=None, pax=None, carried=(), **_) -> dict:
    anchor = pick_anchor(origin, destination, carried)
    area = citydata_api.resolve_area(anchor) if anchor else None
    if not area:
        return _not_covered(anchor, "search_parking")

    lots = citydata_api.get_parking(area)
    if lots:
        return _stamp({"found": True, "near": area, "parking_lots": lots},
                      "서울시 실시간 도시데이터 (주차장)")
    # 키 미설정·캐시 미동기화 등으로 실시간 데이터를 못 받아도 121장소 안이면
    # 목 데이터로 폴백한다 — API 실패가 곧 서비스 중단이 되면 안 된다.
    return _stamp({"found": True, "near": area,
                   "parking_lots": [
                       {"name": f"{area} 공영주차장", "capacity": 80, "available": 23,
                        "base_fee_krw": 1000, "base_minutes": 30, "distance_m": 200},
                   ]}, "주차장 안내 (mock)")


def search_ev_charger(origin=None, destination=None, pax=None, carried=(), **_) -> dict:
    anchor = pick_anchor(origin, destination, carried)
    area = citydata_api.resolve_area(anchor) if anchor else None
    if not area:
        return _not_covered(anchor, "search_ev_charger")

    chargers = citydata_api.get_ev_charger(area)
    if chargers:
        return _stamp({"found": True, "near": area, "chargers": chargers},
                      "서울시 실시간 도시데이터 (전기차 충전소)")
    return _stamp({"found": True, "near": area,
                   "chargers": [
                       {"station": f"{area} 공영충전소", "type": "DC콤보", "available_count": 2,
                        "total_count": 4, "output_kw": 100},
                   ]}, "전기차 충전소 안내 (mock)")


# 접근점 타입별 대표 이동수단·소요시간·요금 (mock). 실 서비스에서는
# OpenTripPlanner 가 실제 경로 탐색으로 계산해 채운다.
_HUB_MODE = {"rail": ("RAIL", 120, 45000), "bus_terminal": ("BUS", 150, 22000),
             "airport": ("AIR", 70, 65000), "subway": ("SUBWAY", 25, 1500),
             "unknown": ("RAIL", 120, 45000)}


def _local_note(from_name: str, to_name: str) -> dict:
    return {"mode": "LOCAL", "from": from_name, "to": to_name,
            "note": f"{from_name}에서 {to_name}까지는 시내 대중교통 또는 택시 이용"}


def _connector_leg(from_name: str, from_lat, from_lon, to_name: str, to_lat, to_lon,
                    same_point: bool) -> dict:
    """출발지<->출발 접근점, 또는 도착 접근점<->목적지 구간을 ODsay 로 계산한다.

    same_point 면(접근점 자체가 곧 그 지점) 구간이 없는 것이므로 빈 legs.
    좌표가 없거나(레거시 허브 등) ODsay 가 실패하면(키 없음/호출 오류/좌표
    범위 밖) 기존 안내 문구로 폴백한다 — 이 폴백이 파이프라인이 죽지 않게
    하는 최소한의 안전망이다.

    반환: {"legs": [...], "total_min": int|None, "total_fare_krw": int|None,
           "used_real": bool}. same_point 면 구간 자체가 없으므로 0(값을
    "모르는" None 이 아니라 "쓸 것이 없어 0"이다). 반대로 ODsay 가 필요한데
    실패했으면 None — 호출부가 이를 0으로 채우면 안 된다(모르는 값과 무료를
    혼동하게 된다)."""
    if same_point:
        return {"legs": [], "total_min": 0, "total_fare_krw": 0, "used_real": False}

    if None in (from_lat, from_lon, to_lat, to_lon):
        return {"legs": [_local_note(from_name, to_name)],
                "total_min": None, "total_fare_krw": None, "used_real": False}

    route = odsay_api.search_route(from_lon, from_lat, to_lon, to_lat,
                                    origin_name=from_name, destination_name=to_name)
    if route is None:
        return {"legs": [_local_note(from_name, to_name)],
                "total_min": None, "total_fare_krw": None, "used_real": False}

    return {"legs": route["legs"], "total_min": route.get("total_min"),
            "total_fare_krw": route.get("total_fare_krw"), "used_real": True}


def plan_journey(origin=None, destination=None, datetime_hint=None, pax=None,
                 origin_coords: dict | None = None, destination_coords: dict | None = None,
                 **_) -> dict:
    o = (resolve_place_coords(origin_coords["lat"], origin_coords["lon"], origin)
         if origin_coords else resolve_place(origin))
    d = (resolve_place_coords(destination_coords["lat"], destination_coords["lon"], destination)
         if destination_coords else resolve_place(destination))
    if not o or not d:
        return _unresolved(origin, o, destination, d, "OpenTripPlanner (mock)")

    if not o["access_points"]:
        return _stamp({"found": False, "reason": "no_transit_access", "destination": o["name"],
                       "hint": f"{o['name']}까지 연결되는 철도·버스 노선을 찾지 못했습니다"},
                      "OpenTripPlanner (mock)")
    if not d["access_points"]:
        return _stamp({"found": False, "reason": "no_transit_access", "destination": d["name"],
                       "hint": f"{d['name']}까지 연결되는 철도·버스 노선을 찾지 못했습니다"},
                      "OpenTripPlanner (mock)")

    o_hub, d_hub = o["access_points"][0], d["access_points"][0]
    base = _base_date(datetime_hint)
    n = pax or 1

    # 출발/도착이 둘 다 지하철역 자체로 곧바로 해소되면(같은 도시 내 단거리)
    # 허브 경유 모델 대신 ODsay 로 출발지<->목적지를 한 번에 계산한다.
    # _HUB_MODE["subway"] 는 임의 지하철역 쌍에 고정 25분/1500원을 매기는
    # 조악한 근사치라, 실제 경로가 있는데 지어낸 값을 낼 이유가 없다.
    if (o_hub["type"] == "subway" and o_hub["distance_m"] == 0
            and d_hub["type"] == "subway" and d_hub["distance_m"] == 0
            and o["lat"] is not None and d["lat"] is not None):
        route = odsay_api.search_route(o["lon"], o["lat"], d["lon"], d["lat"],
                                        origin_name=o["name"], destination_name=d["name"])
        if route is not None:
            itinerary = {"transfers": route["transfers"], "legs": route["legs"]}
            if route.get("total_min") is not None:
                itinerary["total_min"] = route["total_min"]
            if route.get("total_fare_krw") is not None:
                itinerary["total_fare_krw"] = route["total_fare_krw"]
            result = {"found": True, "origin": o["name"], "destination": d["name"], "pax": n,
                      "itineraries": [itinerary]}
            _add_context(result, d["name"])
            return _stamp(result, "ODsay LAB 대중교통 경로탐색")
        # ODsay 실패 시 아래 허브 경유 모델로 폴백한다 (같은 지점이라 첫/
        # 마지막 구간은 생략되고, 중간 구간이 _HUB_MODE 목값으로 채워진다)

    mode, mins, fare = _HUB_MODE.get(d_hub["type"], _HUB_MODE["unknown"])

    used_real_bus = False
    if d_hub["type"] == "bus_terminal":
        # 사용자가 쓴 원문 지명("서울")을 넘긴다. o["name"] 은 해소된 접근점
        # 이름("서울역")이라 고속터미널로 매핑되지 않아(철도역명은 의도적으로
        # 매핑하지 않는다) 실요금을 놓친다. search_bus 도 원문을 넘긴다.
        dep_text = origin or o["name"]
        real = expbus_api.search(dep_text, d_hub["name"], base.strftime("%Y%m%d"))
        if real and real.get("found") and real.get("buses"):
            first = real["buses"][0]
            if "duration_min" in first:
                mins = first["duration_min"]
            fare = first["fare_krw"]
            used_real_bus = True

    # ① 출발지 -> 출발 접근점. ③ 도착 접근점 -> 목적지. 같은 지점이면 생략,
    # 아니면 ODsay 로 계산하고 실패 시 안내 문구로 폴백한다.
    first_leg = _connector_leg(o["name"], o.get("lat"), o.get("lon"),
                               o_hub["name"], o_hub.get("lat"), o_hub.get("lon"),
                               same_point=(o_hub["distance_m"] == 0))
    last_leg = _connector_leg(d_hub["name"], d_hub.get("lat"), d_hub.get("lon"),
                              d["name"], d.get("lat"), d.get("lon"),
                              same_point=(d_hub["distance_m"] == 0))

    mid_leg = {"mode": mode, "from": o_hub["name"], "to": d_hub["name"],
               "duration_min": mins, "fare_krw": fare * n,
               "departure": base.strftime("%Y-%m-%d %H:%M")}
    legs = [*first_leg["legs"], mid_leg, *last_leg["legs"]]

    # 세 구간 합계는 값이 없는 구간(ODsay 실패로 안내 문구만 있는 경우)이
    # 있으면 아예 넣지 않는다 - 없는 값을 0으로 채우면 안 된다.
    min_parts = [first_leg["total_min"], mins, last_leg["total_min"]]
    fare_parts = [first_leg["total_fare_krw"], fare * n, last_leg["total_fare_krw"]]

    itinerary = {"transfers": len(legs) - 1, "legs": legs}
    if all(v is not None for v in min_parts):
        itinerary["total_min"] = sum(min_parts)
    if all(v is not None for v in fare_parts):
        itinerary["total_fare_krw"] = sum(fare_parts)

    result = {"found": True, "origin": o["name"], "destination": d["name"], "pax": n,
              "itineraries": [itinerary]}

    if len(d["access_points"]) > 1:
        alts = ", ".join(f"{ap['name']}({round(ap['distance_m'] / 1000, 1)}km)"
                         for ap in d["access_points"])
        result["destination_access_note"] = f"{d['name']} 인근 접근점: {alts}"

    _add_context(result, d["name"])
    sources = ["OpenTripPlanner (mock)"]
    if used_real_bus:
        sources.append("TAGO 고속버스정보 (국토교통부)")
    if first_leg["used_real"] or last_leg["used_real"]:
        sources.append("ODsay LAB 대중교통 경로탐색")
    return _stamp(result, " + ".join(sources))


# 게이트가 반환하는 intent → 도구 매핑
TOOL_MAP = {
    "search_rail": search_rail,
    "search_bus": search_bus,
    "search_flight": search_flight,
    "search_lodging": search_lodging,
    "share_mobility": search_share_mobility,
    "plan_journey": plan_journey,
    "get_realtime_status": get_realtime_status,
    "fare_policy": search_rail,
    "search_parking": search_parking,
    "search_ev_charger": search_ev_charger,
}


def call_tool(intent: str, slots: dict, carried=()) -> dict:
    """carried: 이번 턴 값이 아니라 직전 턴에서 승계된 슬롯 키 목록.
    "<장소> 근처 X" 류 도구가 기준점을 고를 때(pick_anchor) 승계된 슬롯을
    걸러내는 데 쓴다. 기본값 빈 튜플이라 이 인자를 안 넘기는 기존 호출부
    (CLI 등)도 그대로 동작한다.

    origin_coords/destination_coords: 게이트를 우회해 도구로 직접 전달되는
    좌표(선택). 현재는 plan_journey 만 사용한다 — 다른 도구는 **_ 로
    무시하므로 안전하게 항상 전달해도 된다."""
    fn = TOOL_MAP.get(intent)
    if fn is None:
        return {"found": False, "reason": "no_tool_for_intent", "intent": intent}
    return fn(origin=slots.get("origin"), destination=slots.get("destination"),
              datetime_hint=slots.get("datetime"), pax=slots.get("pax"), carried=carried,
              origin_coords=slots.get("origin_coords"),
              destination_coords=slots.get("destination_coords"))
