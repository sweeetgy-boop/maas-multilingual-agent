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


def resolve_place(text: str | None) -> str | None:
    """자유문 지명 → 표준 ID. 모든 도구의 선행 단계."""
    if not text:
        return None
    t = text.strip().casefold()
    if t in PLACE_ALIASES:
        return PLACE_ALIASES[t]
    for alias, pid in PLACE_ALIASES.items():
        if alias in t or t in alias:
            return pid
    return None


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


def _stamp(payload: dict, source: str) -> dict:
    payload["data_source"] = source
    payload["retrieved_at"] = datetime.now().isoformat(timespec="seconds")
    payload["disclaimer"] = "실시간 운행정보는 참고용이며, 최종 확인은 운영기관 공식 채널을 이용하세요."
    return payload


# ─────────────────────────────────────────────────────────
RAIL_TABLE = {
    ("SEOUL", "BUSAN"): [("KTX 101", 158, 59800), ("KTX 105", 165, 59800), ("KTX-이음 213", 178, 47900)],
    ("SEOUL", "DAEJEON"): [("KTX 103", 62, 23700), ("KTX 107", 68, 23700), ("무궁화 1201", 108, 11800)],
    ("SEOUL", "DONGDAEGU"): [("KTX 111", 112, 43500), ("KTX 115", 119, 43500)],
    ("SUSEO", "BUSAN"): [("SRT 301", 152, 52600), ("SRT 305", 159, 52600)],
    ("SUSEO", "DAEJEON"): [("SRT 303", 61, 21100), ("SRT 307", 66, 21100)],
    ("DAEJEON", "BUSAN"): [("KTX 121", 95, 36900), ("무궁화 1305", 168, 17300)],
}


def search_rail(origin: str | None, destination: str | None,
                datetime_hint: str | None = None, pax: int | None = None) -> dict:
    o, d = resolve_place(origin), resolve_place(destination)
    if not o or not d:
        return _stamp({"found": False,
                       "reason": "unresolved_place",
                       "unresolved": [x for x, r in ((origin, o), (destination, d)) if not r]},
                      "KORAIL/SR OpenAPI (mock)")

    rows = RAIL_TABLE.get((o, d)) or RAIL_TABLE.get((d, o))
    if not rows:
        return _stamp({"found": False, "reason": "no_direct_service",
                       "origin": PLACE_NAMES.get(o), "destination": PLACE_NAMES.get(d)},
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
    return _stamp({"found": True, "origin": PLACE_NAMES.get(o),
                   "destination": PLACE_NAMES.get(d), "pax": n, "trains": trains},
                  "KORAIL/SR OpenAPI (mock)")


BUS_TABLE = {
    ("DONGSEOUL", "SOKCHO"): [("동서울-속초 우등", 145, 22400), ("동서울-속초 일반", 160, 15200)],
    ("SEOUL", "SEOSAN"): [("서울-서산 우등", 118, 14300), ("서울-서산 일반", 130, 9700)],
    ("BUSAN", "PUS"): [("부산역-김해공항 리무진", 55, 7500)],
    ("ICN", "SEOUL"): [("공항리무진 6001", 70, 18000), ("공항리무진 6015", 85, 18000)],
}


def search_bus(origin=None, destination=None, datetime_hint=None, pax=None) -> dict:
    o, d = resolve_place(origin), resolve_place(destination)
    if not o or not d:
        return _stamp({"found": False, "reason": "unresolved_place"}, "TAGO 버스 API (mock)")
    rows = BUS_TABLE.get((o, d)) or BUS_TABLE.get((d, o))
    if not rows:
        return _stamp({"found": False, "reason": "no_direct_service"}, "TAGO 버스 API (mock)")
    base = _base_date(datetime_hint)
    n = pax or 1
    return _stamp({"found": True, "origin": PLACE_NAMES.get(o), "destination": PLACE_NAMES.get(d),
                   "pax": n,
                   "buses": [{"route": r, "departure": (base + timedelta(minutes=40 * i)).strftime("%Y-%m-%d %H:%M"),
                              "duration_min": m, "fare_krw": f * n}
                             for i, (r, m, f) in enumerate(rows)]},
                  "TAGO 버스 API (mock)")


FLIGHT_TABLE = {
    ("ICN", "CJU"): [("KE1201", 70, 89000), ("OZ8905", 75, 82000), ("7C101", 70, 54000)],
    ("GMP", "CJU"): [("KE1231", 65, 78000), ("LJ301", 70, 49000)],
    ("GMP", "PUS"): [("KE1401", 55, 71000), ("BX8801", 60, 45000)],
}


def search_flight(origin=None, destination=None, datetime_hint=None, pax=None) -> dict:
    o, d = resolve_place(origin), resolve_place(destination)
    if not o or not d:
        return _stamp({"found": False, "reason": "unresolved_place"}, "한국공항공사 API (mock)")
    rows = FLIGHT_TABLE.get((o, d)) or FLIGHT_TABLE.get((d, o))
    if not rows:
        return _stamp({"found": False, "reason": "no_direct_service"}, "한국공항공사 API (mock)")
    base = _base_date(datetime_hint)
    n = pax or 1
    return _stamp({"found": True, "origin": PLACE_NAMES.get(o), "destination": PLACE_NAMES.get(d),
                   "pax": n,
                   "flights": [{"flight_no": f, "departure": (base + timedelta(minutes=90 * i)).strftime("%Y-%m-%d %H:%M"),
                                "duration_min": m, "fare_krw": p * n}
                               for i, (f, m, p) in enumerate(rows)]},
                  "한국공항공사 API (mock)")


def search_lodging(destination=None, datetime_hint=None, pax=None, origin=None) -> dict:
    d = resolve_place(destination) or resolve_place(origin)
    if not d:
        return _stamp({"found": False, "reason": "unresolved_place"}, "한국관광공사 TourAPI (mock)")
    name = PLACE_NAMES.get(d, destination)
    return _stamp({"found": True, "near": name,
                   "hotels": [
                       {"name": f"{name} 스테이션 호텔", "distance_m": 220, "price_krw": 98000, "rating": 4.2},
                       {"name": f"{name} 비즈니스 인", "distance_m": 450, "price_krw": 72000, "rating": 3.9},
                       {"name": f"{name} 게스트하우스", "distance_m": 610, "price_krw": 38000, "rating": 4.0},
                   ]}, "한국관광공사 TourAPI (mock)")


def search_share_mobility(origin=None, destination=None, **_) -> dict:
    # "강남역 근처 따릉이" 처럼 기준점이 destination 으로 들어오는 경우가 많다
    o = resolve_place(origin) or resolve_place(destination)
    if not o:
        return _stamp({"found": False, "reason": "unresolved_place"}, "GBFS (mock)")
    name = PLACE_NAMES.get(o, origin)
    return _stamp({"found": True, "near": name,
                   "bike_stations": [
                       {"station": f"{name} 1번 출구", "distance_m": 80, "bikes_available": 7, "docks_free": 12},
                       {"station": f"{name} 앞 광장", "distance_m": 150, "bikes_available": 3, "docks_free": 18},
                   ],
                   "car_share": [
                       {"operator": "카셰어A", "spot": f"{name} 공영주차장", "distance_m": 320,
                        "car": "경차", "price_per_10min_krw": 1200},
                   ]}, "GBFS + 제휴 API (mock)")


def get_realtime_status(**_) -> dict:
    return _stamp({"found": True,
                   "lines": [
                       {"line": "수도권 1호선", "status": "정상운행", "delay_min": 0},
                       {"line": "경부선 KTX", "status": "지연", "delay_min": 8,
                        "cause": "선행열차 지연"},
                   ]}, "GTFS-RT (mock)")


def plan_journey(origin=None, destination=None, datetime_hint=None, pax=None) -> dict:
    o, d = resolve_place(origin), resolve_place(destination)
    if not o or not d:
        return _stamp({"found": False, "reason": "unresolved_place"}, "OpenTripPlanner (mock)")
    base = _base_date(datetime_hint)
    return _stamp({"found": True, "origin": PLACE_NAMES.get(o), "destination": PLACE_NAMES.get(d),
                   "itineraries": [{
                       "total_min": 195, "total_fare_krw": 62800, "transfers": 1,
                       "legs": [
                           {"mode": "WALK", "from": PLACE_NAMES.get(o), "to": f"{PLACE_NAMES.get(o)} 승강장",
                            "duration_min": 6, "fare_krw": 0},
                           {"mode": "RAIL", "from": PLACE_NAMES.get(o), "to": PLACE_NAMES.get(d),
                            "service": "KTX 101", "duration_min": 158, "fare_krw": 59800,
                            "departure": base.strftime("%Y-%m-%d %H:%M")},
                           {"mode": "SUBWAY", "from": PLACE_NAMES.get(d), "to": "목적지 인근",
                            "duration_min": 31, "fare_krw": 3000},
                       ]}]},
                  "OpenTripPlanner (mock)")


# 게이트가 반환하는 intent → 도구 매핑
TOOL_MAP = {
    "search_rail": search_rail,
    "search_bus": search_bus,
    "search_flight": search_flight,
    "search_lodging": lambda origin=None, destination=None, datetime_hint=None, pax=None:
        search_lodging(destination, datetime_hint, pax, origin),
    "share_mobility": search_share_mobility,
    "plan_journey": plan_journey,
    "get_realtime_status": get_realtime_status,
    "fare_policy": search_rail,
}


def call_tool(intent: str, slots: dict) -> dict:
    fn = TOOL_MAP.get(intent)
    if fn is None:
        return {"found": False, "reason": "no_tool_for_intent", "intent": intent}
    return fn(origin=slots.get("origin"), destination=slots.get("destination"),
              datetime_hint=slots.get("datetime"), pax=slots.get("pax"))
