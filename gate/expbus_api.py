#!/usr/bin/env python3
"""
TAGO(국토교통부) 고속버스정보 API 어댑터

  https://apis.data.go.kr/1613000/ExpBusInfo/GetStrtpntAlocFndExpbusInfo

작업 0 실측 결과 (2026-08-31 기준)
  - 요금(charge)·등급(gradeNm)은 실제로 채워져 온다.
  - 잔여석 정보는 응답에 없다. seats_available 필드는 채우지 않는다.
  - depPlandTime(YYYYMMDD) 조회는 당일부터 +2일까지만 결과가 나온다.
    그 이후 날짜는 노선이 없어서가 아니라 API 지평선 밖이라 totalCount=0
    이 오는데, resultCode 는 00(정상)이라 응답만 보면 "그 날 운행 없음"과
    구별되지 않는다. 그래서 지평선 밖 날짜는 조회 가능한 마지막 날(D+2)로
    당겨서 조회하고, 그 사실을 date_requested/date_clamped 로 표시한다.
    호출부는 이를 disclaimer 에 실어 사용자에게 알린다 — 요청한 날짜의
    시간표인 것처럼 보이게 두지 않는다.
  - 등급 필터 파라미터명은 gradeId 가 아니라 **busGradeId** 다. busGradeId=7
    을 주면 서버가 프리미엄만 10건으로 걸러 준다(실측). 다만 이 모듈은
    클라이언트에서 gradeNm 부분일치로 거른다 — "우등"으로 물으면 우등과
    심야우등을 함께 보여주는 편이 사용자 기대에 가깝고, 등급ID 매핑 표를
    따로 유지하지 않아도 되기 때문이다.
  - **같은 이름의 터미널이 ID 여러 개로 중복 등록돼 있고, 노선 데이터는
    그중 한 ID에만 걸려 있다** (실측으로 발견). 예: "동서울"은 NAEK030/
    031/032/035 네 개가 있는데 동서울→강릉 노선은 NAEK032 에만 있고
    나머지는 전부 totalCount=0. "동대구"도 7개 중 NAEK801 하나만 서울
    노선을 갖고 있었다. 터미널명 하나당 ID 하나만 시도하면 실제로 있는
    노선도 no_direct_service 로 잘못 나온다 — 그래서 이름이 같은 ID를
    모두 후보로 갖고 순서대로 시도한다.
  - 서울은 물리적으로 여러 터미널 단지로 나뉘어 있고(경부선은 서울경부,
    호남선은 센트럴시티가 주력), "대표 터미널 하나"로 고정하면 호남선
    계열 노선(광주 등)을 못 찾는다. "서울"처럼 여러 단지를 아우르는
    지명은 CANONICAL_TERMINAL 에 후보 이름을 여러 개 순서대로 둔다.
  - GetExpBusTrminlList 는 도시코드를 안 준다. gate/bus_terminals.json
    (build_bus_terminals.py) 이 터미널명↔도시명 부분일치로 city_code 를
    추정해 넣어 두지만, 453건 중 208건은 매칭이 안 돼 null 이다. 그래서
    이 모듈의 지명 해소는 도시코드를 쓰지 않고 터미널명(CANONICAL_TERMINAL
    → 완전일치 → 부분일치)만으로 한다.

인증키는 unquote 해서 쓴다 (제약 3, korail_api.py 와 동일 이유).
키 없음/조회 실패/터미널 미해소 시 예외를 던지지 않고 None 을 돌려준다.
호출부(tools.py)는 None 을 "실시간 데이터 없음"으로 해석해 목 데이터로
폴백해야 한다.

사용법
  python expbus_api.py --from 서울 --to 부산 --date 20260901
  python expbus_api.py --from 동서울 --to 강릉 --date 20260901 --grade 우등
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
CACHE_PATH = HERE / "bus_terminals.json"

BASE = "https://apis.data.go.kr/1613000/ExpBusInfo"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))
TIMEOUT = 10.0
MAX_RESULTS = 5
FETCH_ROWS = 100          # 등급 필터·정렬 후 5건을 안정적으로 뽑기 위한 여유분
MAX_CANDIDATES = 4        # 지명 하나당 시도할 최대 터미널 후보 수 (콜 수 상한 = 이 값의 제곱)
HORIZON_DAYS = 2          # 조회 가능한 마지막 날 = 오늘 + 2 (실측)
TTL_SECONDS = 600         # 응답 캐시 10분

# 폴백 대상(일시적·환경 문제): 재시도해도 소용없으니 조용히 목 데이터로 넘긴다.
FALLBACK_CODES = {"22": "LIMITED_NUMBER_OF_SERVICE_REQUESTS",
                  "32": "UNREGISTERED_IP_ERROR"}
# 설정 오류(사람이 고쳐야 함): 조용히 넘기면 원인을 못 찾으므로 명확히 남긴다.
CONFIG_ERROR_CODES = {"30": "SERVICE_KEY_IS_NOT_REGISTERED",
                      "31": "DEADLINE_HAS_EXPIRED"}

# 주요 도시/지명 → 시도할 터미널명 순서(우선순위). 값은 TAGO terminalNm
# 원문과 정확히 일치해야 한다. "서울"은 실제로 서로 다른 터미널 단지가
# 여러 개라 후보를 여러 개 둔다. 그 외는 대부분 1개면 충분하다(3일 PoC
# 범위 — tools.py PLACE_ALIASES 와 같은 방식의 소규모 사전).
CANONICAL_TERMINAL: dict[str, list[str]] = {
    "서울": ["서울경부", "센트럴시티(서울)", "동서울", "서울남부"],
    "서울고속버스터미널": ["서울경부"], "센트럴시티": ["센트럴시티(서울)"],
    "동서울": ["동서울"], "부산": ["부산", "부산시외", "부산사상"],
    "대구": ["동대구"], "동대구": ["동대구"], "광주": ["광주(유·스퀘어)"],
    "대전": ["대전복합"], "인천": ["인천"], "강릉": ["강릉"], "속초": ["속초"],
    "춘천": ["춘천"], "원주": ["원주"], "전주": ["전주"], "울산": ["울산"],
    "포항": ["포항"], "여수": ["여수"], "순천": ["순천"], "목포": ["목포"],
    "안동": ["안동"],
    # 제주는 섬이라 TAGO 고속버스 네트워크에 터미널이 없다(항공/여객선 권역).
    # 사전에 넣지 않는다 — 있는 것처럼 넣으면 오히려 잘못된 검색을 유도한다.
}

# "역"은 접미사로 벗기지 않는다. 벗기면 "서울역"→"서울"→서울경부로 매핑돼
# 철도역 질의가 고속버스로 새고("서울역"은 고속터미널이 아니다), "강남역"도
# 부분일치로 "강남마을" 터미널에 잘못 붙는다. 철도역명은 고속버스 터미널로
# 해소하지 않고 후보 없음(→ 호출부가 목 데이터/다른 갈래로 폴백)으로 둔다.
_SUFFIXES = ("고속버스터미널", "종합버스터미널", "시외버스터미널", "종합터미널",
             "버스터미널", "터미널")


def _strip_suffix(text: str) -> str:
    for suf in _SUFFIXES:
        if text.endswith(suf) and len(text) > len(suf):
            return text[: -len(suf)]
    return text


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {"terminals": [], "cities": [], "grades": []}
    return json.loads(CACHE_PATH.read_text(encoding="utf-8"))


_CACHE = _load_cache()
_TERMINALS = _CACHE.get("terminals", [])

_BY_NAME: dict[str, list[dict]] = {}
for _t in _TERMINALS:
    _BY_NAME.setdefault(_t["name"], []).append(_t)
for _ids in _BY_NAME.values():
    _ids.sort(key=lambda x: x["id"])


def _ids_for_name(name: str) -> list[dict]:
    return [{"id": t["id"], "name": t["name"]} for t in _BY_NAME.get(name, [])]


def resolve_terminal_candidates(text: str | None) -> list[dict]:
    """자유문 지명 → 시도할 {"id","name"} 후보 목록(우선순위 순, 최대
    MAX_CANDIDATES개). 동명 터미널의 중복 ID와, 서울처럼 여러 단지로
    나뉜 지명을 모두 이 목록으로 흡수한다."""
    if not text or not _TERMINALS:
        return []
    t = text.strip().casefold()
    stripped = _strip_suffix(text.strip()).casefold()
    name_by_cf = {n.casefold(): n for n in _BY_NAME}

    for candidate in (t, stripped):
        names = CANONICAL_TERMINAL.get(candidate)
        if names:
            out: list[dict] = []
            for n in names:
                out.extend(_ids_for_name(n))
            if out:
                return out[:MAX_CANDIDATES]

    for candidate in (t, stripped):
        n = name_by_cf.get(candidate)
        if n:
            out = _ids_for_name(n)
            if out:
                return out[:MAX_CANDIDATES]

    # "…역"은 부분일치를 적용하지 않는다. 적용하면 "부산역"→"부산"(고속터미널),
    # "강남역"→"강남마을"처럼 철도역 질의가 엉뚱한 고속버스 터미널로 샌다.
    # 다만 터미널 목록에는 오송역·천안아산역처럼 실제로 "역"으로 끝나는
    # 터미널이 9개 있으므로 이름을 통째로 막지는 않고, 위의 완전일치까지만
    # 허용해서 그 9개는 정상 해소되게 둔다.
    if text.strip().endswith("역"):
        return []

    for candidate in (t, stripped):
        matched_names = [n for n in _BY_NAME if candidate in n.casefold() or n.casefold() in candidate]
        if matched_names:
            matched_names.sort(key=len)
            out = []
            for n in matched_names:
                out.extend(_ids_for_name(n))
                if len(out) >= MAX_CANDIDATES:
                    break
            if out:
                return out[:MAX_CANDIDATES]

    return []


def _parse_ts(v: int | str) -> datetime | None:
    try:
        return datetime.strptime(str(v), "%Y%m%d%H%M")
    except ValueError:
        return None


def _normalize_items(body: dict) -> list[dict]:
    items = body.get("items", {}).get("item") or []
    if isinstance(items, dict):      # 1건이면 배열이 아니라 객체로 오는 공공데이터포털 특유의 문제
        items = [items]
    return items


def _error_code(payload: dict) -> tuple[str, str] | None:
    """정상 응답이면 None, 오류면 (코드, 메시지).

    포털은 오류를 정상과 **다른 봉투**로 돌려준다. 정상은
    response.header.resultCode 지만 키 오류 등은 최상위가 통째로
    OpenAPI_ServiceResponse.cmmMsgHeader.returnReasonCode 다(실측). 뒤쪽을
    보지 않으면 KeyError 로 뭉개져 30/31 이 조용히 사라진다."""
    cmm = payload.get("OpenAPI_ServiceResponse", {}).get("cmmMsgHeader")
    if isinstance(cmm, dict):
        return (str(cmm.get("returnReasonCode", "99")),
                str(cmm.get("errMsg") or cmm.get("returnAuthMsg") or ""))
    header = payload.get("response", {}).get("header", {})
    code = str(header.get("resultCode", "")).lstrip("0") or "0"
    if code != "0":
        return str(header.get("resultCode")), str(header.get("resultMsg", ""))
    return None


class _FatalApiError(Exception):
    """이 호출 이후 어떤 후보 조합을 더 시도해도 결과가 같은 오류.
    키 문제(30/31)·요청제한(22)·미등록 IP(32) 가 여기 해당한다. 후보를
    12개씩 재시도하면 로그만 도배되고 일일 트래픽만 축낸다."""


_RESPONSE_CACHE: dict[tuple, tuple[float, list[dict] | None]] = {}


def _fetch(dep_id: str, arr_id: str, date: str) -> list[dict] | None:
    """None = 이 조합만 실패. [] = 그 조합에 노선 없음(다음 후보 조합 시도).
    후보를 더 시도해도 소용없는 오류는 _FatalApiError 를 던진다.
    같은 (출발,도착,날짜) 는 TTL_SECONDS 동안 캐시한다."""
    key = (dep_id, arr_id, date)
    hit = _RESPONSE_CACHE.get(key)
    if hit and time.time() - hit[0] < TTL_SECONDS:
        return hit[1]

    try:
        r = httpx.get(f"{BASE}/GetStrtpntAlocFndExpbusInfo", params={
            "serviceKey": KEY, "_type": "json", "numOfRows": FETCH_ROWS,
            "depTerminalId": dep_id, "arrTerminalId": arr_id, "depPlandTime": date,
        }, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        log.warning("TAGO 고속버스 호출 실패 (%s→%s %s): %s", dep_id, arr_id, date, exc)
        return None

    # 포털은 키 오류를 HTTP 403 + 오류코드가 담긴 본문으로 돌려준다(실측).
    # raise_for_status() 를 먼저 부르면 그 본문을 못 읽어 30/31 이 그냥
    # "403" 으로 뭉개진다. 상태코드와 무관하게 본문부터 해석한다.
    try:
        payload = r.json()
    except (ValueError, json.JSONDecodeError):
        log.warning("TAGO 고속버스 응답 파싱 불가 (%s→%s %s): HTTP %s %s",
                    dep_id, arr_id, date, r.status_code, r.text[:200])
        return None

    err = _error_code(payload)
    if err:
        code, msg = err
        if code in CONFIG_ERROR_CODES:
            # 설정 오류는 폴백돼도 계속 재발한다. 원인이 드러나게 남긴다.
            log.error("TAGO 고속버스 설정 오류 %s(%s): %s — DATA_GO_KR_KEY_ENC 확인 필요",
                      code, CONFIG_ERROR_CODES[code], msg)
            raise _FatalApiError(code)
        if code in FALLBACK_CODES:
            log.warning("TAGO 고속버스 %s(%s): %s — 목 데이터로 폴백",
                        code, FALLBACK_CODES[code], msg)
            raise _FatalApiError(code)
        log.warning("TAGO 고속버스 오류 %s: %s", code, msg)
        return None

    if r.status_code >= 400:
        log.warning("TAGO 고속버스 HTTP %s (%s→%s %s)", r.status_code, dep_id, arr_id, date)
        return None

    try:
        result = _normalize_items(payload["response"]["body"])
    except (KeyError, AttributeError, TypeError) as exc:
        log.warning("TAGO 고속버스 응답 형식 예상 밖 (%s→%s %s): %s", dep_id, arr_id, date, exc)
        return None

    _RESPONSE_CACHE[key] = (time.time(), result)
    return result


def clamp_date(date: str) -> tuple[str, bool]:
    """(조회에 쓸 날짜, 당겨졌는지). API 는 오늘~D+2 만 시간표를 준다.
    그 밖의 날짜를 그대로 물으면 totalCount=0 이 정상 응답으로 와서 '운행
    없음'과 구별되지 않으므로, 조회 가능한 마지막 날로 당겨 실데이터를
    보여주고 당겨졌다는 사실을 호출부에 알린다."""
    try:
        want = datetime.strptime(date, "%Y%m%d").date()
    except (TypeError, ValueError):
        return date, False
    horizon = (datetime.now() + timedelta(days=HORIZON_DAYS)).date()
    if want > horizon:
        return horizon.strftime("%Y%m%d"), True
    return date, False


def search(dep_terminal: str | None, arr_terminal: str | None,
           date: str, grade: str | None = None) -> dict | None:
    """dep_terminal/arr_terminal 은 자유문 지명, date 는 YYYYMMDD.
    조회 실패·터미널 미해소·키 없음이면 None. 결과가 있으면(0건 포함)
    {"found": bool, ...} 딕셔너리."""
    if not KEY:
        return None

    dep_candidates = resolve_terminal_candidates(dep_terminal)
    arr_candidates = resolve_terminal_candidates(arr_terminal)
    if not dep_candidates or not arr_candidates:
        return None

    requested_date = date
    date, clamped = clamp_date(date)

    raw_items: list[dict] = []
    chosen_dep, chosen_arr = dep_candidates[0], arr_candidates[0]
    call_failed = False
    try:
        for dep in dep_candidates:
            found_pair = False
            for arr in arr_candidates:
                items = _fetch(dep["id"], arr["id"], date)
                if items is None:
                    call_failed = True
                    continue
                if items:
                    raw_items, chosen_dep, chosen_arr = items, dep, arr
                    found_pair = True
                    break
            if found_pair:
                break
    except _FatalApiError:
        return None      # 키·쿼터·IP 문제. 남은 후보를 더 시도해도 같다.

    if not raw_items:
        if call_failed:
            return None
        return {"found": False, "reason": "no_direct_service",
                "origin": chosen_dep["name"], "destination": chosen_arr["name"],
                "date": date, "date_requested": requested_date, "date_clamped": clamped}

    items = raw_items
    if grade:
        g = grade.strip().casefold()
        items = [it for it in items if g in (it.get("gradeNm") or "").casefold()]
    if not items:
        return {"found": False, "reason": "no_direct_service",
                "origin": chosen_dep["name"], "destination": chosen_arr["name"],
                "date": date, "date_requested": requested_date, "date_clamped": clamped}

    parsed = []
    for it in items:
        dep_dt = _parse_ts(it.get("depPlandTime"))
        arr_dt = _parse_ts(it.get("arrPlandTime"))
        if dep_dt is None:
            continue
        row = {
            "route": f"{it.get('depPlaceNm', chosen_dep['name'])}-{it.get('arrPlaceNm', chosen_arr['name'])}",
            "departure": dep_dt.strftime("%Y-%m-%d %H:%M"),
            "fare_krw": it.get("charge"),
            "grade": it.get("gradeNm"),
            "_sort": dep_dt,
        }
        if arr_dt is not None:
            row["duration_min"] = int((arr_dt - dep_dt).total_seconds() // 60)
        parsed.append(row)

    parsed.sort(key=lambda x: x["_sort"])
    buses = [{k: v for k, v in row.items() if k != "_sort" and v is not None}
             for row in parsed[:MAX_RESULTS]]

    return {"found": True, "origin": chosen_dep["name"], "destination": chosen_arr["name"],
            "date": date, "date_requested": requested_date, "date_clamped": clamped,
            "buses": buses}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="origin", required=True)
    ap.add_argument("--to", dest="destination", required=True)
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--grade")
    a = ap.parse_args()

    if not KEY:
        print("경고: DATA_GO_KR_KEY_ENC 미설정 — None 이 반환됩니다", file=sys.stderr)

    result = search(a.origin, a.destination, a.date, a.grade)
    print(json.dumps(result, ensure_ascii=False, indent=2))
