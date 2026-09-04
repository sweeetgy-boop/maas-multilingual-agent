#!/usr/bin/env python3
"""
공항 실시간 운항현황 어댑터 — 인천공항공사 + 한국공항공사

공항에 따라 API 가 갈린다. 하나로 덮는 서비스가 없다.
airports.json 의 operator 로 판정한다.

  IIAC  인천국제공항공사  https://apis.data.go.kr/B551177/statusOfAllFltDeOdp
        ICN 전용. 일일 500회.
  KAC   한국공항공사      https://apis.data.go.kr/B551178/flight-status
        그 외 국내 14개 공항. 오퍼레이션당 일일 5,000회.

두 응답을 공통 형태로 정규화해 호출부가 어느 공항인지 신경 쓰지 않게 한다.

작업 0 실측 (2026-09-04)

  인천 — statusOfAllFltDeOdp
  - **items 에 `item` 래퍼가 없다.** `body.items[]` 로 바로 리스트다.
    TAGO·KAC 의 `body.items.item[]` 과 모양이 다르다. numOfRows 를
    1~1200 으로 바꿔가며 확인했고 전부 리스트였다(제약 4).
  - **searchDate 를 안 주면 오늘이 아니다.** 필터 없이 부르면 여러 날이
    섞여 11,000건 넘게 온다. 항상 searchDate 를 준다.
  - 조회 범위는 **D-3 ~ D+6** 이다(D-4·D+7 은 0건). 포털 설명과 일치한다.
  - **도착 응답에만 carousel(수하물수취대)·exitNumber(출구)가 있다.**
    도착 100편 표본에서 둘 다 100% 채워져 있었다. 반대로 chkinRange
    (체크인카운터)는 **출발 응답에만** 있다. 두 오퍼레이션의 필드 집합이
    다르므로 한쪽 파서로 뭉뚱그리면 값을 흘린다.
  - 코드셰어 Slave 가 54%(1,200편 중 649편)다. Slave 의 masterFlightId 가
    같은 응답에 없는 경우는 0건이라, Slave 를 지워도 항공편이 사라지지 않는다.
  - 화물기가 하루 90편 있다. passengerOrCargo=P 로 서버가 걸러준다.
  - 국내선(typeOfFlight=D)은 하루 32편뿐이고 김해 25·대구 6·**제주 1**이다.
  - terminalId 는 문서에 없는 **P03 이 최다**다(1,200편 중 748).
    P01 272 · P02 180 · 화물은 C01.
  - estimatedDatetime 은 null 이 하나도 없다. 지연 계산이 항상 가능하다.

  한국공항공사 — flight-status **(오퍼레이션마다 성격이 다르다)**
  - 지난 조사에서 "키 미등록"으로 본 것은 **호스트를 잘못 짚은 탓**이었다.
    openapi.airport.co.kr 은 실재하는 다른 게이트웨이라 그럴듯한 오류를
    돌려줬다. 포털 값 apis.data.go.kr/B551178 로는 5종 모두 정상이다.
  - **/info** 실시간 운항정보
      · schAirCode/schIOType 로 거른다(camelCase).
      · **searchday 를 무시한다.** D-3·D+0·D+6 모두 같은 216건이 온다 —
        오늘 하루짜리 피드다.
      · **gate 가 100% 채워져 있다.** 탑승구를 주는 유일한 목록 오퍼레이션.
      · rmkKor 도 채워진다. 대신 codeshare·masterflightid 가 없다.
  - **/depart · /arrival** 지연·결항
      · airport_code/searchday/flight_id 로 거른다(snake_case).
      · 조회 범위 **D-3 ~ D+6**(D-4·D+7 은 0건).
      · codeshare(Y/N)·masterflightid 가 있다. 오늘 기준 Y 가 35%(75/216).
      · **gate 필드가 아예 없다.**
      · 미래 날짜는 rmkKor 이 거의 비어 있다(D+1 에서 722/723 이 null)
        — 상태는 오늘 것만 쓸 수 있다.
      · **/depart 는 arrvAirportCode, /arrival 은 arrAirportCode** 다.
        같은 서비스인데 필드명이 갈린다.
  - **/detail** 상세 현황
      · **요청 파라미터가 serviceKey·pageNo·numOfRows·type 뿐이다.**
        공항·날짜·편명 필터가 하나도 없어 전량(4,778건, 48페이지)을 받아
        로컬에서 걸러야 한다.
      · **BAGGAGE_CLAIM(수하물 수취대)을 주는 유일한 곳**이다.
        도착편 2,383건 중 1,302건(55%)이 채워져 있고 출발편은 전부 비었다.
      · FLIGHT_DATE 범위가 D-1 ~ D+1 사흘치다.
  - **numOfRows 상한은 100** 이다. 200 이상은 HTTP_ERROR(04).
  - 커버리지 14개 공항(하루 723편):
      CJU 247 · GMP 175 · PUS 164 · CJJ 53 · TAE 29 · KWJ 20 · RSU 7 ·
      HIN 7 · USN 6 · ICN 5 · KPO 3 · KUV 3 · YNY 2 · WJU 2
  - 여객/화물 구분 필드가 다섯 오퍼레이션 어디에도 없다.

오퍼레이션 선택 (실측에 따른 결론)
  오늘 + 실시간·탑승구  → /info      (gate 를 주는 유일한 목록)
  그 밖의 날짜          → /depart · /arrival  (D-3~D+6, 코드셰어 포함)
  도착 수하물           → /detail    (BAGGAGE_CLAIM 을 주는 유일한 곳)
  하나로는 부족해서 셋을 나눠 쓴다.

캐시 (제약 7 — 인천 일일 500회)
  오늘   TTL   60초  실시간이다. 길게 잡으면 지연 정보가 낡는다.
  미래   TTL   30분  스케줄은 자주 바뀌지 않는다.
  과거   TTL    6시간 확정된 기록이라 갱신될 일이 없다.
  numOfRows 를 크게 잡아 하루치를 한 번에 받고 로컬에서 거른다.
  편명별로 호출하면 하루치 쿼터를 금방 소진한다.

CLI: python gate/airport_status_api.py --airport GMP --io O
     python gate/airport_status_api.py --airport ICN --io I
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
KAC_BASE = "https://apis.data.go.kr/B551178/flight-status"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))

# 캐시 3단 (제약 7). 날짜에 따라 갱신 필요성이 다르다.
TTL_TODAY = 60               # 실시간. 길면 지연 정보가 낡는다
TTL_FUTURE = 30 * 60         # 스케줄은 자주 바뀌지 않는다
TTL_PAST = 6 * 3600          # 확정된 기록이다
_cache: dict[str, tuple[float, object]] = {}

# 인천은 하루치가 1,200편대라 한 번에 받는다. KAC 는 상한이 100 이라
# 페이징한다(실측: 200 이상 HTTP_ERROR 04).
ICN_ROWS = 2000
KAC_ROWS = 100
KAC_MAX_PAGES = 12           # 하루 약 720편이면 8페이지면 끝난다
KAC_DETAIL_MAX_PAGES = 60    # 전량 4,778건 = 48페이지

TIMEOUT = 30.0

# 평가를 반복해서 돌릴 때 인천 호출을 끄는 스위치 (기본 꺼짐).
#
# 필요한지 먼저 재봤다: 인천 질의 40회를 연달아 넣어도 실제 API 호출은
# **2회**였다(io 별로 하루치를 통째로 받아 60초 캐시). 평가셋 513문항 중
# 인천 언급은 15개뿐이고 한 번 도는 데 몇 분이면 20~40회 수준이라, 일일
# 500회 한도에는 한참 못 미친다. 그래서 기본값은 꺼둔다.
# 하루에 평가를 수십 번 돌리는 상황을 위한 안전판일 뿐이다.
# 켜면 인천 경로가 None 을 돌려주고 호출부가 목 데이터로 폴백한다.
SKIP_IIAC = os.environ.get("SKIP_IIAC_API", "").strip().lower() in ("1", "true", "yes")

# 두 API 의 조회 범위. 밖을 물으면 호출하지 않고 바로 알린다 —
# 헛호출로 쿼터를 쓰지 않는다.
RANGE_BACK_DAYS = 3          # D-3
RANGE_FWD_DAYS = 6           # D+6

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


def _ttl_for(ymd: str) -> int:
    """조회 날짜에 따라 캐시 수명을 고른다. 오늘 것만 자주 갱신하면 된다."""
    today = date.today().strftime("%Y%m%d")
    if ymd == today:
        return TTL_TODAY
    return TTL_FUTURE if ymd > today else TTL_PAST


def _cached(key: str, ttl: int):
    hit = _cache.get(key)
    if hit is not None and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _in_range(ymd: str) -> bool:
    """두 API 모두 D-3 ~ D+6 만 준다(실측). 밖이면 호출하지 않는다."""
    try:
        d = datetime.strptime(ymd, "%Y%m%d").date()
    except ValueError:
        return False
    return -RANGE_BACK_DAYS <= (d - date.today()).days <= RANGE_FWD_DAYS


# ── 인천국제공항공사 ──────────────────────────────────────
def _items(body: dict) -> list[dict]:
    """items 정규화 (제약 4). 세 API 의 모양이 전부 다르다.

      TAGO · KAC  body.items.item[]   ← dict 로 한 겹 감싼다
      인천         body.items[]        ← 래퍼 없이 바로 리스트

    거기에 공공데이터포털은 1건일 때 리스트가 아니라 단일 객체를 주는
    경우가 있다. 세 경우를 모두 리스트로 만든다. 실측에서는 어느 API 도
    단일 객체를 주지 않았지만, 한 줄로 막을 수 있는 위험을 남길 이유가 없다."""
    it = body.get("items")
    if isinstance(it, dict):
        it = it.get("item")
    if it is None:
        return []
    if isinstance(it, dict):
        return [it]
    return it if isinstance(it, list) else []


def _icn_fetch(io: str, ymd: str, passenger_only: bool = True) -> list[dict] | None:
    """인천 하루치를 통째로 받는다. 편명·상대공항 필터는 호출부가 로컬에서
    한다 — 인천은 일일 500회라(제약 7) 편명별 호출은 감당이 안 된다."""
    if not KEY:
        return None
    op = "getFltDeparturesDeOdp" if io == "O" else "getFltArrivalsDeOdp"
    # 편명·상대공항으로 좁히지 않고 하루치를 통째로 받는다. 인천은 일일
    # 500회라(제약 7) 편명별로 부르면 금방 소진된다. 필터는 로컬에서 한다.
    params = {"serviceKey": KEY, "type": "json", "numOfRows": ICN_ROWS,
              "pageNo": 1, "searchDate": ymd}
    if passenger_only:
        params["passengerOrCargo"] = "P"

    ck = f"icn|{op}|{ymd}|{passenger_only}"
    if (c := _cached(ck, _ttl_for(ymd))) is not None:
        return c                                          # type: ignore[return-value]
    try:
        r = httpx.get(f"{ICN_BASE}/{op}", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        out = _items(r.json()["response"]["body"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return None
    _cache[ck] = (time.time(), out)
    return out


def _icn_norm(x: dict, io: str) -> dict:
    """인천 응답 정규화.

    **출발과 도착의 필드 집합이 다르다.** 도착에만 carousel(수하물수취대)·
    exitNumber(출구)가 있고, 출발에만 chkinRange(체크인카운터)가 있다.
    없는 쪽은 None 이 되므로 한 함수로 받아도 값이 섞이지 않는다."""
    sched = _parse_dt(x.get("scheduleDatetime"))
    est = _parse_dt(x.get("estimatedDatetime"))
    term = (x.get("terminalId") or "").strip() or None
    return _finish({
        "flight_no": (x.get("flightId") or "").strip() or None,
        "airline": (x.get("airline") or "").strip() or None,
        "counterpart": (x.get("airport") or "").strip() or None,
        "counterpart_code": (x.get("airportCode") or "").strip() or None,
        "scheduled": sched.strftime("%Y-%m-%d %H:%M") if sched else None,
        "estimated": est.strftime("%Y-%m-%d %H:%M") if est else None,
        "status": (x.get("remark") or "").strip() or None,
        "gate": (x.get("gateNumber") or "").strip() or None,
        "terminal": TERMINAL_NAMES.get(term, term),
        "checkin": _clean_checkin(x.get("chkinRange")),      # 출발 응답에만 있다
        "carousel": (x.get("carousel") or "").strip() or None,      # 도착 전용
        "exit": (x.get("exitNumber") or "").strip() or None,        # 도착 전용
        "is_domestic": x.get("typeOfFlight") == "D",
        "is_cargo": x.get("passengerOrCargo") == "Cargo",
        "codeshare": (x.get("codeshare") or "").strip() or None,
        "io": io,
        "_master_flight_no": (x.get("masterFlightId") or "").strip() or None,
    })


# ── 한국공항공사 ─────────────────────────────────────────
def _kac_get(op: str, params: dict, ymd: str,
             max_pages: int = KAC_MAX_PAGES) -> list[dict] | None:
    """KAC 오퍼레이션을 페이징해 전량 받는다. 실패하면 None.

    numOfRows 상한이 100 이라 페이징이 필수다. 한 페이지라도 실패하면
    부분 결과를 내놓지 않고 None 을 돌려준다 — 잘린 목록을 "이게 전부"인
    것처럼 보여주는 것이 조용히 틀리는 길이다."""
    if not KEY:
        return None
    ck = f"kac|{op}|{sorted((k, str(v)) for k, v in params.items())}"
    ttl = _ttl_for(ymd)
    if (c := _cached(ck, ttl)) is not None:
        return c                                          # type: ignore[return-value]

    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        try:
            r = httpx.get(f"{KAC_BASE}/{op}",
                          params={"serviceKey": KEY, "numOfRows": KAC_ROWS,
                                  "pageNo": page, "type": "json", **params},
                          timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json()["response"]["body"]
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return None
        rows += _items(body)
        if len(rows) >= int(body.get("totalCount") or 0):
            break

    _cache[ck] = (time.time(), rows)
    return rows


def _kac_baggage(ymd: str) -> dict[tuple[str, str, str, str], dict] | None:
    """/detail 전량을 받아 (기준공항, 편명, 날짜) → 상세로 색인한다.

    **BAGGAGE_CLAIM 을 주는 유일한 오퍼레이션**인데 필터 파라미터가 하나도
    없어서(serviceKey·pageNo·numOfRows·type 뿐) 4,778건 48페이지를 통째로
    받아야 한다. 호출이 비싸므로 도착 조회일 때만 부르고, 캐시 수명도
    넉넉히 잡는다 — 수취대 배정은 분 단위로 바뀌지 않는다.

    보유 범위가 D-1 ~ D+1 사흘치라 그 밖의 날짜는 부르지 않는다.

    색인 키에 **IO 를 반드시 넣는다.** 같은 공항·같은 편명·같은 날짜가
    출발과 도착 양쪽에 있어서, IO 없이 묶으면 4,778건이 4,489건으로
    줄면서 289건이 서로를 덮어쓴다(실측). 출발 행의 빈 BAGGAGE_CLAIM 이
    도착 행의 값을 지우는 셈이다."""
    today = date.today()
    try:
        gap = (datetime.strptime(ymd, "%Y%m%d").date() - today).days
    except ValueError:
        return None
    if abs(gap) > 1:
        return None

    rows = _kac_get("detail", {}, ymd, max_pages=KAC_DETAIL_MAX_PAGES)
    if rows is None:
        return None
    return {((x.get("AIRPORT") or "").strip(),
             (x.get("AIR_FLN") or "").strip(),
             (x.get("FLIGHT_DATE") or "").strip(),
             (x.get("IO") or "").strip().upper()): x
            for x in rows}


def _kac_codeshare(x: dict) -> tuple[str | None, str | None]:
    """KAC 행에서 (코드셰어 구분, 마스터 편명) 을 뽑는다.

    **codeshare 플래그로 판정하면 안 된다.** Y 는 "이 편이 코드셰어에
    엮여 있다"는 뜻이지 "이 편이 중복"이라는 뜻이 아니다. 실제로 운항하는
    편도 Y 로 온다 — 김포 오늘 Y 75건 중 36건이 masterflightid 가 자기
    자신이었다(BX8025 → BX8025, 에어부산이 실제 운항). Y 를 그대로 Slave 로
    보면 그 36편이 목록에서 사라진다.

    판정은 **masterflightid 와 flightid 를 견줘서** 한다.
      같으면      Master (실제 운항편)
      다르면      Slave  (판매 편명)
    김포·제주 양쪽에서 슬레이브의 마스터가 같은 응답에 없는 경우는
    0건이라, Slave 를 지워도 항공편 자체가 사라지지 않는다."""
    fid = (x.get("flightid") or "").strip()
    master = (x.get("masterflightid") or "").strip()
    if not master:
        return ("Master" if (x.get("codeshare") or "").upper() != "Y" else None), None
    return ("Master" if master == fid else "Slave"), master


def _kac_legs(iata: str, io: str, ymd: str) -> list[dict] | None:
    op = "arrival" if io == "I" else "depart"
    return _kac_get(op, {"airport_code": iata, "searchday": ymd}, ymd)


def _kac_fetch(iata: str, io: str, ymd: str) -> tuple[list[dict], str] | None:
    """KAC 에서 그 공항·그 날의 운항 목록을 받는다.
    반환: (원본 행들, 어느 오퍼레이션을 썼는지)

    오퍼레이션이 셋인데 주는 것이 서로 달라 질의에 맞춰 고른다.
      오늘   → /info    gate 와 rmkKor 을 주는 유일한 목록이다.
      그 외  → /depart · /arrival   D-3~D+6 을 덮고 코드셰어도 준다.
    /info 는 searchday 를 무시하는 **오늘 전용 피드**라(D-3·D+6 모두 같은
    216건) 다른 날짜에 쓸 수 없다."""
    if ymd == date.today().strftime("%Y%m%d"):
        rows = _kac_get("info", {"schAirCode": iata, "schIOType": io}, ymd)
        if rows is not None:
            return rows, "info"
        # /info 가 실패해도 /depart 로 되짚어 볼 값어치가 있다.
    rows = _kac_legs(iata, io, ymd)
    op = "arrival" if io == "I" else "depart"
    return (rows, op) if rows is not None else None


def _kac_add_codeshare(flights: list[dict], iata: str, io: str, ymd: str) -> None:
    """/info 로 받은 목록에 코드셰어 여부를 덧댄다 (기능 8).

    오퍼레이션 둘의 강점이 엇갈린다 — /info 만 gate 를 주고,
    /depart·/arrival 만 codeshare 를 준다. 오늘 조회에서 /info 만 쓰면
    김포에서 코드셰어 중복이 그대로 남는다(실측: 오늘 216편 중 Y 가 75편,
    35%). 그래서 오늘은 둘을 다 받아 편명+시각으로 잇는다.
    KAC 는 오퍼레이션당 일일 5,000회라 한 공항 하루치 3페이지를 더 받는
    비용이 문제 되지 않는다.

    편명만으로 잇지 않는 이유는 같은 편명이 하루에 두 번 뜰 수 있어서다.
    예정 시각(HHMM)을 함께 키로 쓴다."""
    legs = _kac_legs(iata, io, ymd)
    if not legs:
        return
    idx: dict[tuple[str, str], dict] = {}
    for x in legs:
        fn = (x.get("flightid") or "").strip()
        hm = str(x.get("scheduledatetime") or "")[8:12]
        if fn and hm:
            idx[(fn, hm)] = x
    for f in flights:
        hm = (f.get("scheduled") or "")[-5:].replace(":", "")
        x = idx.get((f.get("flight_no") or "", hm))
        if not x:
            continue
        f["codeshare"], f["_master_flight_no"] = _kac_codeshare(x)


def _kac_norm_info(x: dict, iata: str, io: str, ymd: str) -> dict:
    """/info 행 정규화. std/etd 가 "0600" 처럼 HHMM 네 자리라 날짜를 붙인다.
    io 는 응답의 값을 믿는다 — 요청 필터와 어긋날 일은 없지만 응답이 진실이다."""
    day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    io = (x.get("io") or io).strip().upper()[:1] or io
    # 도착이면 상대는 출발지, 출발이면 도착지다.
    ko = x.get("boardingKor") if io == "I" else x.get("arrivedKor")
    en = x.get("boardingEng") if io == "I" else x.get("arrivedEng")
    return _finish({
        "flight_no": (x.get("airFln") or "").strip() or None,
        "airline": (x.get("airlineKorean") or "").strip() or None,
        "counterpart": (ko or "").strip() or (en or "").strip() or None,
        "counterpart_code": (x.get("city") or "").strip() or None,
        "scheduled": _hhmm(x.get("std"), day),
        "estimated": _hhmm(x.get("etd"), day),
        "status": (x.get("rmkKor") or "").strip() or None,
        "gate": (x.get("gate") or "").strip() or None,
        "terminal": None,        # KAC 응답에 터미널 필드가 없다
        "checkin": None,
        "carousel": None, "exit": None,
        "is_domestic": (x.get("line") or "").startswith("국내"),
        "is_cargo": False,       # KAC 는 여객/화물 구분 필드가 없다
        "codeshare": None,       # /info 에는 코드셰어 필드가 없다
        "io": io,
        "_master_flight_no": None,
    })


def _kac_norm_leg(x: dict, iata: str, io: str) -> dict:
    """/depart · /arrival 행 정규화. 시각이 YYYYMMDDHHMM 이라 그대로 파싱된다.

    **두 오퍼레이션의 도착공항 코드 필드명이 다르다** — /depart 는
    arrvAirportCode(v 가 있다), /arrival 은 arrAirportCode. 양쪽을 다 본다."""
    io = (x.get("io") or io).strip().upper()[:1] or io
    if io == "I":
        ko, code = x.get("depAirport"), x.get("depAirportCode")
    else:
        ko = x.get("arrAirport")
        code = x.get("arrvAirportCode") or x.get("arrAirportCode")
    sched = _parse_dt(x.get("scheduledatetime"))
    est = _parse_dt(x.get("estimateddatetime"))
    return _finish({
        "flight_no": (x.get("flightid") or "").strip() or None,
        "airline": (x.get("airline") or "").strip() or None,
        "counterpart": (ko or "").strip() or None,
        "counterpart_code": (code or "").strip() or None,
        "scheduled": sched.strftime("%Y-%m-%d %H:%M") if sched else None,
        "estimated": est.strftime("%Y-%m-%d %H:%M") if est else None,
        "status": (x.get("rmkKor") or "").strip() or None,
        "gate": None,            # /depart·/arrival 에는 gate 필드가 없다
        "terminal": None,
        "checkin": None,
        "carousel": None, "exit": None,
        "is_domestic": (x.get("line") or "").startswith("국내"),
        "is_cargo": False,
        # 인천의 Master/Slave 와 어휘를 맞춘다. 판정 근거는 _kac_codeshare 참고.
        "codeshare": _kac_codeshare(x)[0],
        "io": io,
        "_master_flight_no": _kac_codeshare(x)[1],
    })


def _kac_add_baggage(flights: list[dict], iata: str, io: str, ymd: str) -> None:
    """도착편에 수하물수취대와 탑승구를 덧댄다 (기능 11).

    /detail 이 BAGGAGE_CLAIM 을 주는 유일한 곳이라 도착 조회일 때만
    부른다. 값이 없으면 필드를 넣지 않는다 — 수취대 번호를 지어내면
    승객이 엉뚱한 곳에서 기다린다."""
    if io != "I":
        return
    idx = _kac_baggage(ymd)
    if not idx:
        return
    for f in flights:
        d = idx.get((iata, f.get("flight_no") or "", ymd, io))
        if not d:
            continue
        if (bc := (d.get("BAGGAGE_CLAIM") or "").strip()):
            f["carousel"] = bc
        if not f.get("gate") and (g := (d.get("GATE") or "").strip()):
            f["gate"] = g


# ── 공통 ────────────────────────────────────────────────
def _finish(out: dict) -> dict:
    """정규화 마무리 — 지연을 계산해 붙인다. 세 파서가 공통으로 부른다."""
    _set_delay(out,
               _parse_hm(out.get("scheduled")), _parse_hm(out.get("estimated")))
    return out


def _parse_hm(v: str | None) -> datetime | None:
    try:
        return datetime.strptime(v or "", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


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
    flight_no 편명으로 좁힌다 ("인천공항 KE081 탑승구 어디야?")
    domestic  True 국내선만 / False 국제선만 / None 전부 (기능 6)
    include_codeshare
              None 이면 편명 지정 시에만 코드셰어를 남긴다 (기능 8)

    화물기는 항상 제외한다 (기능 7). 사용자가 탈 수 없는 항공편이다.
    도착이면 수하물수취대·출구를 함께 채운다 (기능 11).

    **하루치를 통째로 받아 로컬에서 거른다.** 인천은 일일 500회라(제약 7)
    편명별로 호출하면 금방 소진된다. 캐시가 있으면 같은 날 다른 편명
    질의는 호출 없이 답한다.

    반환
      성공           {"found": True, "flights": [...], ...}
      미지원·범위 밖   {"found": False, "reason": ...}
      API 실패·키없음  None   ← 호출부가 목 데이터로 폴백한다
    """
    port = flight_api.resolve_airport(airport)
    if not port:
        return {"found": False, "reason": "unresolved_airport",
                "unresolved": [airport]}
    operator = port.get("operator")
    if not operator:
        # 해외 공항의 실시간 정보를 주는 API 가 없다. 지어내지 않는다.
        return {"found": False, "reason": "overseas_not_supported",
                "airport": port["name_ko"]}

    io = "I" if str(io).upper().startswith("I") else "O"
    day = ymd or date.today().strftime("%Y%m%d")
    if not _in_range(day):
        # 두 API 모두 D-3~D+6 만 준다. 헛호출로 쿼터를 쓰지 않는다.
        return {"found": False, "reason": "date_out_of_range",
                "airport": port["name_ko"], "date": day,
                "note": "실시간 운항정보는 3일 전부터 6일 후까지만 조회됩니다"}

    if operator == "IIAC":
        if SKIP_IIAC:
            return None            # 목 데이터로 폴백한다(제약 5)
        rows = _icn_fetch(io, day)
        if rows is None:
            return None
        flights = [_icn_norm(x, io) for x in rows]
        source = "인천국제공항공사 항공기 운항 현황"
    else:
        got = _kac_fetch(port["iata"], io, day)
        if got is None:
            return None
        rows, op = got
        flights = [(_kac_norm_info(x, port["iata"], io, day) if op == "info"
                    else _kac_norm_leg(x, port["iata"], io)) for x in rows]
        if op == "info":
            _kac_add_codeshare(flights, port["iata"], io, day)
        _kac_add_baggage(flights, port["iata"], io, day)
        source = f"한국공항공사 실시간 항공기 운항정보 ({op})"

    fetched = len(flights)

    # 기능 7 — 화물기 제외. 인천은 요청 파라미터로 이미 걸렀지만 응답에서
    # 한 번 더 본다. 서버 필터를 믿고 끝내지 않는다.
    flights = [f for f in flights if not f["is_cargo"]]
    cargo_excluded = fetched - len(flights)

    # 편명·상대공항 필터는 로컬에서 한다(위 주석 참고 — 쿼터 절약).
    if flight_no:
        fn = flight_no.replace(" ", "").casefold()
        flights = [f for f in flights
                   if (f["flight_no"] or "").replace(" ", "").casefold() == fn]
    if counterpart:
        cp = counterpart.strip().casefold()
        flights = [f for f in flights
                   if (f["counterpart_code"] or "").casefold() == cp
                   or cp in (f["counterpart"] or "").casefold()]

    # 기능 6 — 국내선/국제선 구분
    if domestic is not None:
        flights = [f for f in flights if f["is_domestic"] is domestic]

    # 기능 8 — 코드셰어 중복 제거. 같은 비행기가 여러 항공사 편명으로
    # 중복 노출되면 5편을 보여줘도 실제 선택지가 2편으로 줄어든다.
    # 인천 실측: 1,200편 중 Slave 649편(54%). Slave 의 masterFlightId 가
    # 같은 응답에 없는 경우는 0건이라 지워도 항공편이 사라지지 않는다.
    # KAC 는 codeshare Y/N 를 Slave/Master 로 옮겨 같은 규칙을 쓴다.
    # 다만 KAC 미래 날짜는 전부 'N' 으로 와서(D+1 에서 723/723) 이 필터가
    # 사실상 동작하지 않는다 — 데이터가 없는 것이지 중복이 없는 것이 아니다.
    #
    # 편명을 지정해 물으면 그 편명이 Slave 일 수 있으므로 남긴다.
    keep_cs = include_codeshare if include_codeshare is not None else bool(flight_no)
    codeshare_removed = 0
    if not keep_cs:
        before = len(flights)
        flights = [f for f in flights if f["codeshare"] != "Slave"]
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
    out["_rows"] = flights
    return out


def delay_summary(airport: str, io: str = "O") -> dict | None:
    """그 공항에 지금 지연·결항이 얼마나 있는가 (기능 3).

    항공편 안내에 부가정보로 한 줄 붙이는 데 쓴다 — citydata 에서 혼잡도를
    붙인 것과 같은 방식이다. 지연·결항이 없으면 None 을 돌려 아무 것도
    붙이지 않는다. 없는 걱정을 만들지 않는다.

    remark 는 미래 편에서 null 이다(실측 1,200편 중 814편). 값이 있는 편만
    센다. 지연 판정은 remark 문자열이 아니라 delay_min 으로 한다 —
    remark='출발'인데 실제로는 87분 늦은 편이 있었다."""
    st = get_status(airport, io=io, limit=0)
    if not st or not st.get("found") and not st.get("total_flights"):
        return None
    rows = st.get("_rows") or []
    delayed = [f for f in rows if (f.get("delay_min") or 0) >= 15]
    cancelled = [f for f in rows
                 if any(w in (f.get("status") or "") for w in ("결항", "취소"))]
    if not delayed and not cancelled:
        return None

    label = f"{st['airport']} {'출발편' if st['io'] == 'O' else '도착편'}"
    parts = []
    if delayed:
        parts.append(f"{len(delayed)}편 지연")
    if cancelled:
        parts.append(f"{len(cancelled)}편 결항")
    out = {"airport_status": f"{label} {', '.join(parts)}"}
    if delayed:
        out["max_delay_min"] = max(f["delay_min"] for f in delayed)
    return out


def _public(obj):
    """밑줄로 시작하는 내부 키를 걷어낸다. _rows 는 집계용이라 밖으로
    나갈 이유가 없고, 나가면 응답만 커진다."""
    if isinstance(obj, dict):
        return {k: _public(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_public(x) for x in obj]
    return obj


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
    print(json.dumps(_public(r), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
