#!/usr/bin/env python3
"""
TAGO 국내항공운항정보 API 어댑터 — 국내선 시간표·운임

  https://apis.data.go.kr/1613000/DmstcFlightNvgInfo

작업 0 실측 결과 (2026-09-04 기준)
  - **서비스 URL 이 가이드 문서와 다르다.** 문서의 `DmstcFlightNvgInfoService`
    는 NO_OPENAPI_SERVICE_ERROR(12) 다. 실제 경로는 `DmstcFlightNvgInfo` —
    고속버스가 `ExpBusInfoService` 가 아니라 `ExpBusInfo` 였던 것과 같다.
    오퍼레이션명은 문서대로 대문자 시작(`GetFlightOpratInfoList`)이 맞다.
  - **미래 날짜가 조회된다.** 코레일과 정반대다. 이분 탐색으로 확정한
    보유 범위는 오늘 ~ **+50일(20261024)**, 20261025 부터 전 노선 0건이다.
    20261024 는 IATA 하계 스케줄 마지막 날 — 롤링 윈도우가 아니라
    **현재 시즌 시간표 전체**를 들고 있다. 과거도 -30일까지 조회된다.
    따라서 코레일식 요일 매칭(find_reference_date)이 필요 없다.
    이건 참고값이 아니라 **확정 시간표**이므로 is_reference 를 달지 않는다.
  - **운임은 22.8% 만 채워져 있다.** 6개 노선 × 3개 날짜 975편 전수 조사:
        대한항공 203편 중 65편 / 제주항공 179편 중 91편 /
        아시아나 120편 중 66편 / 나머지 7개 LCC 473편은 **전부 0**
      (이스타·티웨이·진에어·에어부산·에어서울·파라타·에어로K)
    노선 편차도 크다 — 김포→제주 39%, 제주→김포 **0%(111편 전부)**.
    값 자체는 진짜다(61900/51400/49500/87400). 그러니 **있는 편에만 싣고,
    없는 편을 0원이나 "정보없음"으로 채우지 않는다**(제약 6).
    호출부가 이 편차를 사용자에게 한 줄로 설명할 수 있도록
    fare_coverage 를 함께 반환한다.
  - prestigeCharge 는 93.3%(910/975)가 0 이다. 0 이 아닌 65편은 전부
    대한항공이고, economyCharge 가 채워진 대한항공 편과 정확히 일치한다.
    0 은 "비즈니스석 0원"이 아니라 **비즈니스석이 없다**는 뜻이므로
    필드 자체를 넣지 않는다.
  - airlineId 필터가 동작한다(KAL→대한항공 24편, JJA→제주항공 22편).
    기능 9 는 이걸 그대로 쓴다.
  - numOfRows 는 문서의 "최대 4000 byte" 와 달리 넉넉하다. 1,200건까지
    한 페이지로 받힌다. 국내선은 한 노선 하루 최대 119편이라 페이징이
    필요 없다.
  - items 는 `body.items.item[]` 구조이고, 1건일 때도 리스트로 온다
    (0건이면 `{"item": []}`). 인천 API 는 래퍼 없이 `body.items[]` 라
    모양이 다르다 — _items() 가 세 경우를 모두 흡수한다(제약 4).
  - **같은 편명이 5~15분 차이로 두 번 오는 경우가 3.6% 있다.**
    615편 중 22건, 전부 60분 이내였고 60분을 넘는 사례는 0건이다.
    같은 비행기의 데이터 잡음이므로 60분 이내 동일 편명은 하나로 합친다.
  - 인천발 제주행(NAARKSI→NAARKPC)은 0건이다. 노선이 실제로 없다.
    기능 6 이 이 경우를 "항공편 없음"으로 단정하지 않고 김포를 안내한다.

시간대 필터
  tools.time_window() 가 이미 5개 언어의 시간대 표현을 다룬다. 여기서
  다시 구현하지 않고 after_hhmm/before_hhmm 을 받는다 — korail_api.
  search_schedule 과 같은 계약이다. 한쪽만 고쳐 불일치가 생기는 것을
  막으려면 파싱은 한 곳에만 있어야 한다.

인증키는 unquote 해서 쓴다. 포털이 주는 키는 Encoding 형태(%2F 등)라
httpx params 가 다시 인코딩하면 이중 인코딩(%252F)이 되어 403 이 난다
(korail_api.py 에서 겪었다).

CLI: python gate/flight_api.py --from 김포 --to 제주 --date 20260905
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx

BASE = "https://apis.data.go.kr/1613000/DmstcFlightNvgInfo"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))

AIRPORTS_PATH = Path(__file__).with_name("airports.json")

# 시간표는 시즌 단위로 고정이라 자주 바뀌지 않는다. 10분이면 넉넉하다.
CACHE_TTL_SEC = 600
_cache: dict[str, tuple[float, object]] = {}

# 같은 편명이 이 시간 안에 두 번 나오면 한 편으로 본다. 실측상 중복은
# 전부 15분 이내였고 60분을 넘는 사례는 0건이었다.
DUP_WINDOW_MIN = 60

TIMEOUT = 15.0


# ── 공항·항공사 캐시 ──────────────────────────────────────
_data: dict | None = None


def _load() -> dict:
    """airports.json 을 한 번만 읽는다. 없으면 빈 캐시로 동작한다 —
    build_airports.py 를 안 돌렸다고 예외를 던지지는 않는다."""
    global _data
    if _data is None:
        try:
            _data = json.loads(AIRPORTS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _data = {"airports": [], "airlines": []}
    return _data


def resolve_airport(text: str | None) -> dict | None:
    """지명·공항명·IATA·ICAO → 공항 레코드. 못 찾으면 None.

    별칭은 완전 일치를 먼저 본다. "제주"가 "제주국제공항"의 부분열이라
    부분 일치를 먼저 보면 긴 이름이 짧은 이름을 잡아먹는다. 부분 일치는
    완전 일치가 없을 때만, 그리고 **별칭이 입력에 들어 있는** 방향으로만
    본다("김포공항에서" → "김포공항").

    도쿄(NRT/HND)나 베이징(PEK/PKX)처럼 한 도시에 공항이 둘이면 국내
    공항을 먼저, 그 다음 IATA 사전순으로 고른다 — 결정적이어야 같은
    질의에 같은 답이 나온다."""
    if not text:
        return None
    t = text.strip().casefold()
    if not t:
        return None
    ports = _load()["airports"]

    exact = [a for a in ports if any(al.casefold() == t for al in a["aliases"])]
    if exact:
        return min(exact, key=lambda a: (not a["domestic"], a["iata"]))

    # 부분 일치: 별칭이 두 글자 이상일 때만. "제주" 같은 짧은 별칭이
    # 긴 문장 어디에나 걸리는 것은 의도한 동작이지만, 한 글자짜리는
    # 오탐만 만든다.
    part = [a for a in ports
            if any(len(al) >= 2 and al.casefold() in t for al in a["aliases"])]
    if not part:
        return None
    # 가장 긴 별칭이 걸린 공항을 고른다. "김포공항"이 "김포"보다 구체적이다.
    def score(a: dict) -> tuple:
        best = max((len(al) for al in a["aliases"]
                    if len(al) >= 2 and al.casefold() in t), default=0)
        return (-best, not a["domestic"], a["iata"])
    return min(part, key=score)


def resolve_airline(text: str | None) -> dict | None:
    """원문에서 항공사를 찾는다. 게이트가 항공사를 슬롯으로 뽑지 않으므로
    (제약 3 — 게이트 스키마를 건드리지 않는다) 도구가 원문을 보고 맞춘다.

    이름 별칭은 대소문자를 무시하고 부분 일치로 본다.
    **두세 글자 ASCII 코드(IATA/ICAO)는 대문자로 쓰인 경우에만 인정한다.**
    소문자까지 받으면 다른 언어의 흔한 낱말에 걸린다 — 인도네시아어
    "Gimpo **ke** Jeju"(제주 **로**)의 ke 가 대한항공(KE)으로 잡혀 질의가
    대한항공 편만 남는 일이 실제로 있었다. 항공사 코드는 관례상 대문자로
    쓰므로 대문자 조건이 표현력을 깎지 않는다.

    여러 개가 걸리면 가장 긴 별칭이 걸린 항공사를 고른다 —
    "에어부산"이 "에어"보다 구체적이다."""
    if not text:
        return None
    low = text.casefold()
    words = set(re.findall(r"[A-Za-z0-9]+", text))          # 대소문자 그대로
    best, best_len = None, 0
    for a in _load()["airlines"]:
        for al in a["aliases"]:
            if len(al) <= 3 and al.isascii():
                hit = al.isupper() and al in words
            else:
                hit = al.casefold() in low
            if hit and len(al) > best_len:
                best, best_len = a, len(al)
    return best


def alternative_airport(port: dict) -> dict | None:
    """같은 도시권의 다른 공항. 기능 6 이 "인천에서 제주" 에 김포를
    제시하는 데 쓴다. 없으면 None — 대안을 지어내지 않는다."""
    grp = port.get("city_group")
    if not grp:
        return None
    return next((a for a in _load()["airports"]
                 if a.get("city_group") == grp and a["iata"] != port["iata"]), None)


# ── HTTP ────────────────────────────────────────────────
def _items(body: dict) -> list[dict]:
    """items 정규화 (제약 4).

    TAGO 는 `body.items.item[]`, 인천은 `body.items[]` 로 모양이 다르고,
    공공데이터포털은 1건일 때 리스트가 아니라 단일 객체를 주는 경우가
    있다. 세 경우를 모두 리스트로 만든다."""
    it = body.get("items")
    if isinstance(it, dict):
        it = it.get("item")
    if it is None:
        return []
    if isinstance(it, dict):
        return [it]
    return it if isinstance(it, list) else []


def _get(op: str, params: dict) -> list[dict] | None:
    """조회 성공 시 items, 실패·키없음이면 None.

    예외를 밖으로 던지지 않는다 — 호출부는 None 을 받아 목 데이터로
    폴백한다(제약 5)."""
    if not KEY:
        return None
    p = {"serviceKey": KEY, "numOfRows": 300, "pageNo": 1, "_type": "json", **params}
    ck = f"{op}|{sorted((k, str(v)) for k, v in params.items())}"
    hit = _cache.get(ck)
    if hit is not None and time.time() - hit[0] < CACHE_TTL_SEC:
        return hit[1]                                    # type: ignore[return-value]
    try:
        r = httpx.get(f"{BASE}/{op}", params=p, timeout=TIMEOUT)
        r.raise_for_status()
        out = _items(r.json()["response"]["body"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError,
            json.JSONDecodeError):
        return None
    _cache[ck] = (time.time(), out)
    return out


# ── 시각 처리 ────────────────────────────────────────────
def _parse(v) -> datetime | None:
    """202609050820 → datetime. int 로 오므로 str 로 받는다."""
    s = str(v or "")
    try:
        return datetime.strptime(s, "%Y%m%d%H%M")
    except ValueError:
        return None


def _in_window(dt: datetime | None, after: str | None, before: str | None) -> bool:
    if dt is None:
        return True
    hhmm = dt.strftime("%H:%M")
    if after and hhmm < after:
        return False
    if before and hhmm >= before:
        return False
    return True


def _dedup(rows: list[dict]) -> list[dict]:
    """같은 편명이 60분 이내에 두 번 나오면 이른 쪽만 남긴다.
    실측상 3.6%(615편 중 22건)가 이 잡음이고, 60분을 넘는 중복은 0건이다.
    하루에 두 번 뜨는 정상적인 같은 편명(60분 초과)은 그대로 둔다."""
    out: list[dict] = []
    for r in sorted(rows, key=lambda x: str(x.get("depPlandTime") or "")):
        prev = next((o for o in out if o.get("vihicleId") == r.get("vihicleId")), None)
        if prev is not None:
            a, b = _parse(prev.get("depPlandTime")), _parse(r.get("depPlandTime"))
            if a and b and abs((b - a).total_seconds()) <= DUP_WINDOW_MIN * 60:
                continue
        out.append(r)
    return out


# ── 조회 ────────────────────────────────────────────────
def _raw_search(dep_id: str, arr_id: str, ymd: str,
                airline_id: str | None = None) -> list[dict] | None:
    params = {"depAirportId": dep_id, "arrAirportId": arr_id, "depPlandTime": ymd}
    if airline_id:
        params["airlineId"] = airline_id
    return _get("GetFlightOpratInfoList", params)


def search(dep: str, arr: str, date_ymd: str | None = None,
           airline: str | None = None, limit: int = 5,
           after_hhmm: str | None = None,
           before_hhmm: str | None = None) -> dict | None:
    """국내선 시간표 조회.

    dep/arr    지명·공항명·IATA 아무 형태나 (airports.json 별칭으로 해소)
    date_ymd   YYYYMMDD. 없으면 오늘
    airline    원문 그대로 넘겨도 된다 — 여기서 항공사를 찾아 필터한다
    after/before_hhmm  "HH:MM" 출발 시각 범위. tools.time_window() 결과

    반환
      성공          {"found": True, "flights": [...], ...}
      노선 없음      {"found": False, "reason": "no_route", ...}
      시즌 밖       {"found": False, "reason": "beyond_schedule", ...}
      공항 해소 실패  {"found": False, "reason": "unresolved_airport", ...}
      API 실패·키없음 None   ← 호출부가 목 데이터로 폴백한다
    """
    o, d = resolve_airport(dep), resolve_airport(arr)
    if not o or not d:
        return {"found": False, "reason": "unresolved_airport",
                "unresolved": [x for x, r in ((dep, o), (arr, d)) if not r]}
    if not o.get("tago_id") or not d.get("tago_id"):
        # 국제선은 이 API 의 범위 밖이다. 시간표를 지어내지 않는다.
        return {"found": False, "reason": "international_not_supported",
                "dep_airport": o["name_ko"], "arr_airport": d["name_ko"]}
    if o["iata"] == d["iata"]:
        return {"found": False, "reason": "same_airport", "dep_airport": o["name_ko"]}

    ymd = date_ymd or date.today().strftime("%Y%m%d")
    air = resolve_airline(airline) if airline else None
    rows = _raw_search(o["tago_id"], d["tago_id"], ymd,
                       air.get("tago_id") if air else None)
    if rows is None:
        return None                       # API 실패 → 목 데이터 폴백

    # 항공사 필터를 걸었는데 0건이면, 노선 자체가 없는 것인지 그 항공사가
    # 안 다니는 것인지 갈라야 한다. 필터 없이 한 번 더 본다.
    airline_no_service = False
    if not rows and air:
        base = _raw_search(o["tago_id"], d["tago_id"], ymd)
        if base:
            airline_no_service = True
            rows = []

    if not rows and not airline_no_service:
        return _empty_reason(o, d, ymd)

    rows = _dedup(rows)

    flights = []
    for r in rows:
        dt_dep, dt_arr = _parse(r.get("depPlandTime")), _parse(r.get("arrPlandTime"))
        if not _in_window(dt_dep, after_hhmm, before_hhmm):
            continue
        f = {
            "flight_no": r.get("vihicleId"),
            "airline": (r.get("airlineNm") or "").strip(),
            "departure": dt_dep.strftime("%Y-%m-%d %H:%M") if dt_dep else None,
            "arrival": dt_arr.strftime("%Y-%m-%d %H:%M") if dt_arr else None,
        }
        if dt_dep and dt_arr:
            f["duration_min"] = int((dt_arr - dt_dep).total_seconds() // 60)
        # 운임: 있는 편에만 싣는다. 0 은 "0원"이 아니라 "정보 없음"이다.
        eco = _int(r.get("economyCharge"))
        pre = _int(r.get("prestigeCharge"))
        if eco:
            f["fare_krw"] = eco
        if pre:
            f["fare_prestige_krw"] = pre
        flights.append(f)

    flights.sort(key=lambda x: x["departure"] or "")
    fares = [f["fare_krw"] for f in flights if f.get("fare_krw")]

    out = {
        "found": bool(flights),
        "dep_airport": o["name_ko"], "dep_iata": o["iata"],
        "arr_airport": d["name_ko"], "arr_iata": d["iata"],
        "date": ymd,
        "flights": flights[:limit],
        "total_flights": len(flights),
        "shown": min(len(flights), limit),
    }
    if o.get("access_note"):
        out["access_note"] = o["access_note"]
    if air:
        out["airline_filter"] = air["name_ko"]
    if airline_no_service:
        out.update(found=False, reason="airline_no_service")
    if flights:
        # 운임 보유율이 22.8% 라 "요금이 안 나온 편"이 흔하다. 시각 순서를
        # 흐트러뜨리지 않으면서도 요금 질의에 답할 수 있도록, 노선 전체에서
        # 실제로 확인된 운임을 요약해 함께 싣는다. 없는 값을 채우는 게
        # 아니라 받은 값을 모으는 것이다(제약 6).
        out["fare_coverage"] = {"with_fare": len(fares), "of": len(flights)}
        if fares:
            out["fare_krw_range"] = [min(fares), max(fares)]
            by: dict[str, int] = {}
            for f in flights:
                if f.get("fare_krw"):
                    by.setdefault(f["airline"], f["fare_krw"])
            out["fare_by_airline"] = by
        if not fares:
            out["fare_note"] = "이 노선은 공공데이터에 운임이 제공되지 않습니다"
        elif len(fares) < len(flights):
            out["fare_note"] = "운임이 공개된 항공사만 표시합니다"
    elif after_hhmm or before_hhmm:
        out["reason"] = "no_flight_in_window"
    return out


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _empty_reason(o: dict, d: dict, ymd: str) -> dict:
    """0건이 "그 날 그 노선이 없다"인지 "그 날짜가 시즌 밖"인지 가른다.

    **기준 노선으로 판정한다.** 처음에는 같은 노선을 오늘 날짜로 되짚어
    봤는데, 인천→제주가 오늘 1편(7C167)·내일 0편이라 "시즌 밖"으로 잘못
    판정됐다. 주 몇 회만 뜨는 노선이 실제로 있다.
    대신 김포→제주를 요청 날짜로 조회한다. 국내선 최다 노선이라 시즌
    안이면 어느 날이든 110편 넘게 있고, 시즌 밖이면 0건이다. 이 기준이
    개별 노선의 운항 요일에 흔들리지 않는다.

    "노선 없음"을 그 날짜에 한정해 말하는 것도 이 때문이다. 다른 날에는
    뜰 수 있는 노선을 영영 없다고 단정하지 않는다."""
    out = {"found": False, "dep_airport": o["name_ko"],
           "arr_airport": d["name_ko"], "date": ymd}

    if ymd != date.today().strftime("%Y%m%d") and not _in_season(ymd):
        out["reason"] = "beyond_schedule"
        out["note"] = "요청하신 날짜는 아직 시간표가 공개되지 않았습니다"
        return out

    out["reason"] = "no_route"
    alt = alternative_airport(o)
    # 대안 공항에 그 날 실제로 노선이 있는지 확인하고 나서 제시한다.
    # 확인 없이 안내하면 없는 노선을 만드는 것과 같다.
    if alt and alt.get("tago_id") and _raw_search(alt["tago_id"], d["tago_id"], ymd):
        out["alternative_airport"] = alt["name_ko"]
        out["alternative_iata"] = alt["iata"]
        out["note"] = (f"{o['name_ko']}에서 {d['name_ko']}까지 가는 직항이 "
                       f"이 날짜에는 없습니다. {alt['name_ko']}에서 출발하는 "
                       f"항공편이 있습니다")
    return out


# 시즌 판정에 쓰는 기준 노선. 김포–제주는 국내선 최다 노선이라 시즌
# 안이면 어느 날이든 100편이 넘는다. 특정 노선의 운항 요일에 판정이
# 흔들리지 않게 하는 것이 목적이다.
_SEASON_PROBE = ("NAARKSS", "NAARKPC")


def _in_season(ymd: str) -> bool:
    """그 날짜의 시간표가 API 에 올라와 있는가.

    실측 보유 범위는 오늘 ~ +50일(20261024, 하계 스케줄 마지막 날)이다.
    범위를 상수로 박지 않고 매번 확인하는 이유는, 동계 스케줄이 열리면
    범위가 저절로 넓어져야 하기 때문이다."""
    return bool(_raw_search(*_SEASON_PROBE, ymd))


# ── CLI ─────────────────────────────────────────────────
def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="TAGO 국내항공 시간표 조회")
    ap.add_argument("--from", dest="dep", required=True)
    ap.add_argument("--to", dest="arr", required=True)
    ap.add_argument("--date", default=None, help="YYYYMMDD (기본 오늘)")
    ap.add_argument("--airline", default=None)
    ap.add_argument("--time", default=None,
                    help='"오후"·"아침" 등 시간대 표현 (tools.time_window 사용)')
    ap.add_argument("--limit", type=int, default=5)
    a = ap.parse_args()

    after = before = None
    if a.time:
        from tools import time_window          # 순환 임포트를 피해 여기서만 부른다
        w = time_window(a.time)
        if w:
            after, before = w
            print(f"시간대 필터: {after} ~ {before}", file=sys.stderr)

    r = search(a.dep, a.arr, a.date, airline=a.airline, limit=a.limit,
               after_hhmm=after, before_hhmm=before)
    if r is None:
        print("API 실패 또는 DATA_GO_KR_KEY_ENC 없음 → 호출부는 목 데이터로 폴백합니다")
        return 1
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
