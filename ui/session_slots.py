"""
멀티턴 세션 슬롯 관리 — ui/server.py 와 ui/api.py 가 공유한다.

과거 이 로직이 두 파일에 중복돼 있었고, 한쪽만 고치고 다른 쪽을 방치해
사고가 난 적이 있다(커밋 832f422 — ui/api.py 에만 멀티턴 슬롯 버그가
남아 있었음). 새로 손볼 일이 생기면 반드시 이 파일 하나만 고친다.
"""

from __future__ import annotations

# "<장소> 근처 X" 류 의도. 그 장소는 근접 검색의 기준점(anchor)이지 여정의
# 출발지·목적지가 아니다. 세션에 origin/destination 으로 남기면 다음 턴의
# 구간 조회를 오염시킨다 — 예: "광명역 근처 호텔" 다음 "인천공항에서는?"
# 이 "인천공항 → 부산역" 이 아니라 "인천공항 → 광명역"으로 샌다.
PROXIMITY_INTENTS = {"search_lodging", "share_mobility", "search_parking", "search_ev_charger"}

# 출발지/목적지 개념이 실제로 있는 구간 의도. 이 의도에서만 origin/destination
# 을 세션에 저장한다.
ROUTE_INTENTS = {"search_rail", "search_bus", "search_flight", "plan_journey"}


def merge_slots(new: dict, prev: dict) -> tuple[dict, list[str]]:
    """멀티턴 슬롯 승계. datetime 은 시점이 바뀌었을 수 있어 승계하지 않는다."""
    carried, merged = [], dict(new)
    for k in ("origin", "destination", "pax"):
        if not merged.get(k) and prev.get(k):
            merged[k] = prev[k]
            carried.append(k)
    return merged, carried


def slots_to_persist(gate: dict) -> dict:
    """도구 호출 후 다음 턴을 위해 세션에 남길 슬롯을 고른다.

    근접 검색 의도(PROXIMITY_INTENTS)의 장소는 여정의 출발지/목적지가
    아니므로 origin/destination 을 저장하지 않는다 — 세션에 남기면 이후
    턴의 구간 조회가 그 장소로 오염된다. 구간 의도(ROUTE_INTENTS)에서만
    origin/destination 을 저장한다. pax 는 여정 전반에 유효하므로 의도와
    무관하게 항상 저장 후보에 넣는다."""
    intent = gate.get("intent")
    slots = {"origin": None, "destination": None, "pax": gate.get("pax") or None}
    if intent in ROUTE_INTENTS:
        slots["origin"] = gate.get("origin") or None
        slots["destination"] = gate.get("destination") or None
    return slots
