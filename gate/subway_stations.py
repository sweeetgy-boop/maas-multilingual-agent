"""
도시철도 역 조회 계층 (KRIC subwayRouteInfo 캐시).

gate/subway_stations.json (build_subway_stations.py 로 생성) 을 읽어
역명으로 소속 노선·운영기관·환승 여부를 찾는다. transit_nodes.py 와 같은
구조다 — 외부 호출 없이 메모리 사전만 본다.

**이 계층은 시각표를 다루지 않는다.**
KRIC subwayTimetable 오퍼레이션이 현재 인증키에 승인돼 있지 않아
(2026-09-01 실측: resultCode 30, 같은 순간 subwayRouteInfo 는 00) 캐시에
도착·출발시각이 없다. 따라서 여기서 답할 수 있는 것은 딱 세 가지다.

    이 역이 실재하는가 / 어느 노선인가 / 환승역인가

호출부는 이보다 더 말하면 안 된다. 특히 노선 정보를 준다고 해서 시간표를
주는 것처럼 보이면 안 된다 — 그것이 이 파일이 존재하는 이유다.

한계
  - 역명이 도시 간 충돌한다. "시청"은 서울(S1)·부산(BS)·대전(DJ)에 모두
    있다. 질의에 도시 단서가 있으면 그것으로 좁히고, 없으면 후보를 모두
    돌려주며 ambiguous 로 표시한다 — 임의로 하나를 고르지 않는다.
  - 역명 색인은 한글 전용이다. "Seomyeon"·"西面" 같은 로마자·한자 표기는
    잡히지 않는다. 다국어 역명 매핑은 별도 작업이다(로마자 표기를
    지어내면 없는 역을 있다고 답하게 되므로 추정하지 않는다).

사용법: python subway_stations.py --station 서면
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).parent
CACHE_PATH = HERE / "subway_stations.json"

_CACHE: dict = (json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                if CACHE_PATH.exists() else {})

STATIONS: list[dict] = _CACHE.get("stations", [])
OPERATORS: dict[str, dict] = {o["code"]: o for o in _CACHE.get("operators", [])}
LINES: dict[tuple, dict] = {(l["operator"], l["code"]): l for l in _CACHE.get("lines", [])}

# 캐시에 시각표가 없다는 사실을 코드 쪽에서도 확인할 수 있게 둔다.
HAS_TIMETABLE: bool = bool(_CACHE.get("has_timetable", False))

# 도시 단서 → mreaWideCd 권역코드. 역명 충돌을 좁히는 데만 쓴다.
# 다국어 별칭은 질의가 영어·중국어로 와도 도시만은 좁힐 수 있게 넣었다
# (역명 자체는 한글로 와야 한다 — 위 "한계" 참조).
_REGION_HINTS = {
    "01": ("서울", "인천", "경기", "수원", "성남", "고양", "부천", "안양", "남양주",
           "의정부", "용인", "김포", "seoul", "incheon", "首尔", "仁川"),
    "02": ("부산", "김해", "양산", "울산", "busan", "gimhae", "ulsan", "釜山", "蔚山"),
    "03": ("대구", "경산", "daegu", "大邱"),
    "04": ("광주", "gwangju", "光州"),
    "05": ("대전", "세종", "daejeon", "sejong", "大田"),
}


def _region_hint(text: str | None) -> str | None:
    """질의문에 들어 있는 도시명으로 권역을 좁힌다. 없으면 None."""
    if not text:
        return None
    t = text.casefold()
    for region, hints in _REGION_HINTS.items():
        if any(h in t for h in hints):
            return region
    return None


def is_region_word(token: str | None) -> bool:
    """토큰이 도시 단서 그 자체인가("부산", "대전"). _region_hint 는 문장 전체를
    부분일치로 보지만, 이쪽은 토큰이 정확히 도시명일 때만 참이다."""
    if not token:
        return False
    t = token.casefold()
    return any(t == h for hints in _REGION_HINTS.values() for h in hints)


def normalize(name: str) -> str:
    """역명 정규화. 꼬리의 '역'과 괄호 병기를 떼어낸다.

    "청량리(서울시립대입구)" → "청량리", "서면역" → "서면".
    '역'을 뗀 결과가 비면 떼지 않는다("역곡역"이 "곡"이 되면 안 된다 —
    앞의 '역'까지 지우는 실수를 막으려 꼬리 한 글자만 본다)."""
    n = name.strip().split("(")[0].strip()
    if len(n) > 1 and n.endswith("역"):
        n = n[:-1]
    return n


# 정규화된 역명 → 해당 역 레코드들. 괄호 병기 원본으로도 찾을 수 있게
# 두 형태를 모두 색인한다.
_INDEX: dict[str, list[dict]] = {}
for _s in STATIONS:
    for _key in {normalize(_s["name"]), _s["name"].strip()}:
        _INDEX.setdefault(_key, []).append(_s)


def find_station(text: str | None, region_hint: str | None = None) -> dict | None:
    """역명(자유문)으로 역을 찾는다. 못 찾으면 None.

    text 에는 "부산 서면역" 처럼 도시명이 섞여 있을 수 있다. 도시명은
    권역을 좁히는 데 쓰고, 역명은 그 나머지에서 찾는다."""
    if not text or not STATIONS:
        return None

    hint = region_hint or _region_hint(text)

    # 질의 전체를 역명으로 보고 먼저 시도하고, 안 되면 공백으로 쪼개
    # 토큰 단위로 본다("부산 서면역" → "부산", "서면역").
    #
    # 도시명 토큰을 조심해야 한다. 부산·대전·대구·광주는 그 자체가 역명이라
    # ("부산역", "대전역"…) "부산 서면역"에서 앞 토큰이 먼저 걸리면 서면이
    # 아니라 부산역을 찾아버린다. 그래서 도시 단서인 토큰은 뒤로 미루고,
    # 도시명이 아닌 토큰이 하나라도 맞으면 그쪽을 쓴다. 도시명뿐인 질의
    # ("부산역 가는 법")에서는 미뤄 둔 쪽이 유일한 후보라 그대로 쓰인다.
    keys = [normalize(text), text.strip()]
    keys += sorted((normalize(tok) for tok in text.split()), key=len, reverse=True)

    plain = [k for k in keys if k in _INDEX and not is_region_word(k)]
    city = [k for k in keys if k in _INDEX and is_region_word(k)]
    ordered = plain + city
    if not ordered:
        return None
    matched_key = ordered[0]
    candidates = _INDEX[matched_key]

    regions = sorted({c["region"] for c in candidates})
    if hint and hint in regions:
        candidates = [c for c in candidates if c["region"] == hint]
        regions = [hint]

    name = candidates[0]["name"]
    lines = []
    for c in candidates:
        meta = LINES.get((c["operator"], c["line"]), {})
        op = OPERATORS.get(c["operator"], {})
        lines.append({
            "operator": c["operator"],
            "operator_name": op.get("name") or c["operator"],
            "line": c["line"],
            "line_name": meta.get("name") or c["line"],
            "station_code": c["code"],
            "region": c["region"],
            "region_name": c.get("region_name") or c["region"],
        })

    return {
        "name": name,
        "regions": regions,
        "region_names": sorted({l["region_name"] for l in lines}),
        "lines": lines,
        # 같은 역명이 서로 다른 (기관, 노선) 두 개 이상에 걸치면 환승역이다.
        # 기관이 달라도 환승이다(동대구역 = 대구 1호선 + 대경선).
        "is_transfer": len({(l["operator"], l["line"]) for l in lines}) > 1,
        # 도시 단서가 없어 권역을 못 좁힌 경우. 호출부가 되물어야 한다.
        "ambiguous": len(regions) > 1,
        # 역명이 아니라 도시명 토큰으로만 걸렸는가("부산역" → "부산").
        # 도시명은 간선철도역명이기도 해서, 호출부가 지하철/간선철도 중
        # 어느 쪽 질의인지 가릴 때 이 값을 본다.
        "matched_city_token": is_region_word(matched_key),
        # 질의의 어느 토큰에 맞았는지. 호출부가 간선철도 해소 결과와 견줘
        # 어느 쪽이 더 구체적으로 맞았는지 판단하는 데 쓴다.
        "matched_name": normalize(matched_key),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="도시철도 역 조회 (노선·환승 정보만)")
    ap.add_argument("--station", required=True)
    args = ap.parse_args()

    hit = find_station(args.station)
    if not hit:
        print(f"'{args.station}' — 캐시에 없는 역입니다")
        raise SystemExit(1)
    print(json.dumps(hit, ensure_ascii=False, indent=1))
    if not HAS_TIMETABLE:
        print("\n[주의] 이 캐시에는 시각표가 없습니다 "
              "(subwayTimetable 오퍼레이션 미승인).")


if __name__ == "__main__":
    main()
