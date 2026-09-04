#!/usr/bin/env python3
"""
공항 실시간 운항현황 어댑터 — 인천공항공사 + 한국공항공사

공항에 따라 API 가 갈린다. 하나로 덮는 서비스가 없다.

  ICN        인천국제공항공사  statusOfAllFltDeOdp
             https://apis.data.go.kr/B551177/statusOfAllFltDeOdp
  그 외 국내  한국공항공사      FlightStatusList
             http://openapi.airport.co.kr/service/rest/FlightStatusList

두 응답을 공통 형태로 정규화해 호출부가 어느 공항인지 신경 쓰지 않게 한다.

작업 0 실측 결과 (2026-09-04 기준)

  인천 — 동작 확인
  - **서비스 경로가 가이드 문서와 다르다.** 문서의 서비스 ID `statusOfAllFlt`
    로는 NO_OPENAPI_SERVICE_ERROR(12) 다. 실제 경로는 `statusOfAllFltDeOdp`
    (소문자 s 로 시작한다 — 이 포털의 다른 서비스들과 대소문자 규칙이 다르다).
  - **items 에 `item` 래퍼가 없다.** `body.items[]` 로 바로 리스트다.
    TAGO 의 `body.items.item[]` 과 모양이 다르다. numOfRows 를
    1/2/5/10/100/500/1000/1200 로 바꿔가며 확인했고 전부 리스트였다(제약 4).
  - **searchDate 를 안 주면 오늘이 아니다.** 필터 없이 부르면 여러 날이
    섞여 11,631건이 온다. 항상 searchDate 를 준다.
  - 하루 출발 1,208편 · 도착 1,184편. numOfRows 2000 이면 한 페이지다.
  - **코드셰어 Slave 가 54%(1,200편 중 649편)다.** 중복을 지우지 않으면
    5편을 보여줘도 실제 선택지는 2편으로 줄어든다(기능 8).
  - **화물기가 하루 90편 있다.** passengerOrCargo=C 로 확인했다. 요청
    파라미터 passengerOrCargo=P 로 서버가 걸러준다(기능 7).
  - **국내선(typeOfFlight=D)도 있다.** 하루 32편, 전부 김해(PUS)행이고
    제주(CJU)행은 0편이다.
  - terminalId 는 문서에 없는 **P03 이 최다**다(1,200편 중 616편).
    P01 231 · P02 153 · 화물은 C01. 문서가 예로 든 C02 는 관측되지 않았다.
  - estimatedDatetime 은 null 이 하나도 없다. 지연 계산이 항상 가능하다.
    예정과 다른 편이 269/1,200 이고, 그 중 78편은 **예정보다 빠르다**.
    remark='지연' 27편의 평균 차이는 +42.4분(최대 +150분)이었다.
  - remark 는 미래 편에서 null 이다(1,200편 중 814편). 값이 있는 것만
    상태로 쓴다.

  한국공항공사 — **키가 미등록이다**
  - 엔드포인트는 살아 있다. 경로를 틀리면 "NO OPENAPI SERVICE ERROR",
    맞으면 "SERVICE KEY IS NOT REGISTERED ERROR"(resultCode 99)로 응답이
    갈린다. 즉 경로는 맞고 활용신청이 안 된 상태다.
    data.go.kr 에서 "한국공항공사_실시간 항공기 운항정보" 활용신청이
    승인되면 열린다.
  - 따라서 **아래 파싱 코드는 가이드 문서 기준이고 실측 검증되지 않았다.**
    키가 열리면 반드시 실제 응답과 대조해야 한다. 그때까지 이 경로는
    None 을 돌려주고, 호출부는 목 데이터로 폴백한다(제약 5).
    응답이 예상과 다르면 조용히 None 이 되도록 방어적으로 파싱한다 —
    잘못 읽은 값을 실시간 정보라고 내보내는 것이 최악이다.

캐시는 60초다. 실시간이므로 짧게 잡는다.

CLI: python gate/airport_status_api.py --airport GMP --io O
"""

from __future__ import annotations

import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.parse import unquote

import httpx

import flight_api

ICN_BASE = "https://apis.data.go.kr/B551177/statusOfAllFltDeOdp"
KAC_BASE = "http://openapi.airport.co.kr/service/rest/FlightStatusList"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))

CACHE_TTL_SEC = 60           # 실시간이다. 길게 잡으면 지연 정보가 낡는다
_cache: dict[str, tuple[float, object]] = {}

TIMEOUT = 20.0

# 인천 터미널 코드 → 사람이 읽는 말 (기능 2)
# 실측에서 P01/P02/P03/C01 네 개가 나왔다. 문서가 예로 든 C02 는 없었다.
# 모르는 코드는 원문 그대로 둔다 — 지어내지 않는다.
TERMINAL_NAMES = {
    "P01": "제1여객터미널",
    "P02": "탑승동",
    "P03": "제2여객터미널",
    "C01": "화물터미널",
    "C02": "화물터미널",
}


def _cached(key: str):
    hit = _cache.get(key)
    if hit is not None and time.time() - hit[0] < CACHE_TTL_SEC:
        return hit[1]
    return None


# ── 인천국제공항공사 ──────────────────────────────────────
def _icn_items(body: dict) -> list[dict]:
    """인천은 body.items[] 로 래퍼가 없다. 그래도 dict 래퍼와 단일 객체를
    함께 흡수한다 — 두 API 를 한 함수로 다루면 실수가 준다(제약 4)."""
    it = body.get("items")
    if isinstance(it, dict):
        it = it.get("item")
    if it is None:
        return []
    if isinstance(it, dict):
        return [it]
    return it if isinstance(it, list) else []


def _icn_fetch(io: str, ymd: str, flight_id: str | None = None,
               counterpart: str | None = None,
               passenger_only: bool = True) -> list[dict] | None:
    if not KEY:
        return None
    op = "getFltDeparturesDeOdp" if io == "O" else "getFltArrivalsDeOdp"
    params = {"serviceKey": KEY, "type": "json", "numOfRows": 2000,
              "pageNo": 1, "searchDate": ymd}
    if passenger_only:
        params["passengerOrCargo"] = "P"
    if flight_id:
        params["flightId"] = flight_id
    if counterpart:
        params["airportCode"] = counterpart

    ck = f"icn|{op}|{ymd}|{flight_id}|{counterpart}|{passenger_only}"
    if (c := _cached(ck)) is not None:
        return c                                          # type: ignore[return-value]
    try:
        r = httpx.get(f"{ICN_BASE}/{op}", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        out = _icn_items(r.json()["response"]["body"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return None
    _cache[ck] = (time.time(), out)
    return out


def _icn_norm(x: dict, io: str) -> dict:
    sched = _parse_dt(x.get("scheduleDatetime"))
    est = _parse_dt(x.get("estimatedDatetime"))
    term = (x.get("terminalId") or "").strip() or None
    out = {
        "flight_no": (x.get("flightId") or "").strip() or None,
        "airline": (x.get("airline") or "").strip() or None,
        "counterpart": (x.get("airport") or "").strip() or None,
        "counterpart_code": (x.get("airportCode") or "").strip() or None,
        "scheduled": sched.strftime("%Y-%m-%d %H:%M") if sched else None,
        "estimated": est.strftime("%Y-%m-%d %H:%M") if est else None,
        "status": (x.get("remark") or "").strip() or None,
        "gate": (x.get("gateNumber") or "").strip() or None,
        "terminal": TERMINAL_NAMES.get(term, term),
        "checkin": _clean_checkin(x.get("chkinRange")),
        "is_domestic": x.get("typeOfFlight") == "D",
        "io": io,
        # 필터링(2단계)에 쓰는 원본 값. 정규화 형태를 소비하는 쪽이
        # 원문 필드명을 몰라도 되게 여기서 이름을 통일해 둔다.
        "_cargo": x.get("passengerOrCargo") == "Cargo",
        "_codeshare": (x.get("codeshare") or "").strip() or None,
        "_master_flight_no": (x.get("masterFlightId") or "").strip() or None,
    }
    _set_delay(out, sched, est)
    return out


# ── 한국공항공사 ─────────────────────────────────────────
# 주의: 아래는 가이드 문서 기준이고 실측 검증되지 않았다(키 미등록).
#      키가 열리면 실제 응답과 대조해야 한다.
def _kac_fetch(iata: str, io: str, line_type: str | None = None) -> list[dict] | None:
    if not KEY:
        return None
    params = {"serviceKey": KEY, "schAirCode": iata, "schIOType": io,
              "numOfRows": 500, "pageNo": 1}
    if line_type:
        params["schLineType"] = line_type

    ck = f"kac|{iata}|{io}|{line_type}"
    if (c := _cached(ck)) is not None:
        return c                                          # type: ignore[return-value]
    try:
        r = httpx.get(f"{KAC_BASE}/getFlightStatusList", params=params,
                      timeout=TIMEOUT)
        r.raise_for_status()
        rows = _kac_parse(r.text)
    except (httpx.HTTPError, ET.ParseError, ValueError, TypeError):
        return None
    if rows is None:
        return None
    _cache[ck] = (time.time(), rows)
    return rows


def _kac_parse(text: str) -> list[dict] | None:
    """XML 응답을 파싱한다. 정상(resultCode 00)이 아니면 None 을 돌려
    호출부가 목 데이터로 폴백하게 한다.

    현재 키로는 resultCode 99(SERVICE KEY IS NOT REGISTERED)가 온다.
    이 경로가 조용히 None 을 반환하는 것이 의도한 동작이다."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    code = root.findtext(".//resultCode")
    if code is not None and code.strip() not in ("00", "0"):
        return None
    return [{c.tag: (c.text or "").strip() for c in item}
            for item in root.iter("item")]


def _kac_norm(x: dict, iata: str, io: str) -> dict:
    """std/etd 는 "0005" 처럼 HHMM 네 자리다(문서 기준). 날짜가 없으므로
    오늘로 붙인다. 실시간 조회라 오늘이 맞다."""
    today = date.today().strftime("%Y-%m-%d")
    sched = _hhmm(x.get("std"), today)
    est = _hhmm(x.get("etd"), today)
    # io 가 I(도착)면 상대 공항은 출발지, O(출발)면 도착지다.
    counterpart = x.get("boardingKor") if io == "I" else x.get("arrivedKor")
    out = {
        "flight_no": x.get("airFln") or None,
        "airline": x.get("airlineKorean") or None,
        "counterpart": counterpart or None,
        "counterpart_code": x.get("city") or None,
        "scheduled": sched, "estimated": est,
        "status": x.get("rmkKor") or None,
        "gate": x.get("gate") or None,
        "terminal": None,        # 한국공항공사 응답에 터미널 필드가 없다
        "checkin": None,
        "is_domestic": (x.get("line") or "").startswith("국내"),
        "io": io,
        "_cargo": False,         # 이 API 는 여객/화물 구분 필드가 없다
        "_codeshare": None,      # 코드셰어 구분 필드도 없다
        "_master_flight_no": None,
    }
    try:
        _set_delay(out,
                   datetime.strptime(sched, "%Y-%m-%d %H:%M") if sched else None,
                   datetime.strptime(est, "%Y-%m-%d %H:%M") if est else None)
    except ValueError:
        pass
    return out


# ── 공통 ────────────────────────────────────────────────
def _set_delay(out: dict, sched: datetime | None, est: datetime | None) -> None:
    """예정과 변경 시각의 차이를 분으로 담는다 (기능 4).

    차이가 0 이면 필드를 넣지 않는다 — "지연 0분"은 정보가 아니다.
    **음수는 delay_min 에 넣지 않는다.** 실측상 1,200편 중 78편이 예정보다
    이르다. -10 을 delay_min 에 넣으면 "10분 지연"으로 읽힐 위험이 있어,
    조기 출발은 early_min 이라는 다른 이름으로 담는다."""
    if not (sched and est):
        return
    diff = int((est - sched).total_seconds() // 60)
    if diff > 0:
        out["delay_min"] = diff
    elif diff < 0:
        out["early_min"] = -diff


def _parse_dt(v) -> datetime | None:
    try:
        return datetime.strptime(str(v or ""), "%Y%m%d%H%M")
    except ValueError:
        return None


def _hhmm(v: str | None, day: str) -> str | None:
    s = (v or "").strip()
    if len(s) != 4 or not s.isdigit():
        return None
    return f"{day} {s[:2]}:{s[2:]}"


def _clean_checkin(v) -> str | None:
    """체크인 카운터. 국내선은 "-" 로 온다 — 값이 없다는 뜻이므로 버린다."""
    s = (v or "").strip()
    return s if s and s not in ("-", "0") else None


def get_status(airport: str, io: str = "O", ymd: str | None = None,
               flight_no: str | None = None,
               counterpart: str | None = None,
               domestic: bool | None = None,
               include_codeshare: bool | None = None,
               limit: int = 10) -> dict | None:
    """공항 실시간 운항현황.

    airport   지명·공항명·IATA (airports.json 별칭으로 해소)
    io        "O" 출발 / "I" 도착
    flight_no 편명으로 좁힌다 ("인천공항 KE001 탑승구 어디야?")
    domestic  True 국내선만 / False 국제선만 / None 전부 (기능 6)
    include_codeshare
              None 이면 편명 지정 시에만 코드셰어를 남긴다 (기능 8)

    화물기는 항상 제외한다 (기능 7). 사용자가 탈 수 없는 항공편이다.

    반환
      성공           {"found": True, "flights": [...], ...}
      미지원 공항      {"found": False, "reason": ...}
      API 실패·키없음  None   ← 호출부가 목 데이터로 폴백한다
    """
    port = flight_api.resolve_airport(airport)
    if not port:
        return {"found": False, "reason": "unresolved_airport",
                "unresolved": [airport]}
    if not port.get("domestic"):
        # 해외 공항의 실시간 정보를 주는 API 가 없다. 지어내지 않는다.
        return {"found": False, "reason": "overseas_not_supported",
                "airport": port["name_ko"]}

    io = "I" if str(io).upper().startswith("I") else "O"
    day = ymd or date.today().strftime("%Y%m%d")

    if port["iata"] == "ICN":
        rows = _icn_fetch(io, day, flight_id=flight_no, counterpart=counterpart)
        if rows is None:
            return None
        flights = [_icn_norm(x, io) for x in rows]
        source = "인천국제공항공사 항공기 운항 현황"
    else:
        rows = _kac_fetch(port["iata"], io)
        if rows is None:
            return None
        flights = [_kac_norm(x, port["iata"], io) for x in rows]
        if flight_no:
            fn = flight_no.replace(" ", "").casefold()
            flights = [f for f in flights
                       if (f["flight_no"] or "").replace(" ", "").casefold() == fn]
        source = "한국공항공사 실시간 항공기 운항정보"

    fetched = len(flights)

    # 기능 7 — 화물기 제외. 인천은 요청 파라미터로 이미 걸렀지만, 서버
    # 필터를 믿고 끝내지 않는다. 한국공항공사 응답에는 여객/화물 구분
    # 필드가 아예 없어 여기서 거를 수 있는 것이 없다는 점도 알아둔다.
    flights = [f for f in flights if not f["_cargo"]]
    cargo_excluded = fetched - len(flights)

    # 기능 6 — 국내선/국제선 구분
    if domestic is not None:
        flights = [f for f in flights if f["is_domestic"] is domestic]

    # 기능 8 — 코드셰어 중복 제거. 같은 비행기가 여러 항공사 편명으로
    # 중복 노출되면 5편을 보여줘도 실제 선택지가 2편으로 줄어든다.
    # 실측상 인천 출발 1,200편 중 Slave 가 649편(54%)이고, Slave 의
    # masterFlightId 가 같은 응답에 없는 경우는 0건이었다(공항 코드로
    # 좁혀도 마찬가지). Slave 를 지워도 항공편 자체가 사라지지 않는다.
    #
    # 다만 "대한항공 KE1234" 처럼 편명을 지정해 물으면 그 편명이 Slave 일
    # 수 있다. 그때는 남긴다 — 사용자가 가진 항공권의 편명이다.
    keep_cs = include_codeshare if include_codeshare is not None else bool(flight_no)
    codeshare_removed = 0
    if not keep_cs:
        before = len(flights)
        flights = [f for f in flights if f["_codeshare"] != "Slave"]
        codeshare_removed = before - len(flights)

    flights.sort(key=lambda f: f["scheduled"] or "")
    out = {
        "found": bool(flights),
        "airport": port["name_ko"], "airport_iata": port["iata"],
        "io": io, "date": day,
        "flights": flights[:limit],
        "total_flights": len(flights),
        "shown": min(len(flights), limit),
        "source": source,
        "is_realtime": True,
    }
    if cargo_excluded:
        out["cargo_excluded"] = cargo_excluded
    if codeshare_removed:
        out["codeshare_removed"] = codeshare_removed
    if not flights and fetched:
        # 원본은 있었는데 필터로 다 걸러졌다. "운항이 없다"와 구분해야 한다.
        out["reason"] = "no_flight_after_filter"
    return out


# ── CLI ─────────────────────────────────────────────────
def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="공항 실시간 운항현황 조회")
    ap.add_argument("--airport", required=True, help="GMP / ICN / 김포 …")
    ap.add_argument("--io", default="O", help="O 출발 / I 도착")
    ap.add_argument("--date", default=None, help="YYYYMMDD (기본 오늘)")
    ap.add_argument("--flight", default=None, help="편명 (예 KE001)")
    ap.add_argument("--domestic", action="store_true", help="국내선만")
    ap.add_argument("--international", action="store_true", help="국제선만")
    ap.add_argument("--with-codeshare", action="store_true",
                    help="코드셰어 Slave 도 남긴다")
    ap.add_argument("--limit", type=int, default=5)
    a = ap.parse_args()

    dom = True if a.domestic else (False if a.international else None)
    r = get_status(a.airport, a.io, a.date, flight_no=a.flight, domestic=dom,
                   include_codeshare=a.with_codeshare or None, limit=a.limit)
    if r is None:
        print("API 실패 또는 키 미등록 → 호출부는 목 데이터로 폴백합니다",
              file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
