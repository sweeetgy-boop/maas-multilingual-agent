#!/usr/bin/env python3
"""
TAGO(국토교통부) 고속버스정보 API 어댑터

  https://apis.data.go.kr/1613000/ExpBusInfo/GetStrtpntAlocFndExpbusInfo

작업 0 실측 결과 (2026-08-31 기준)
  - 요금(charge)·등급(gradeNm)은 실제로 채워져 온다.
  - 잔여석 정보는 응답에 없다. seats_available 필드는 채우지 않는다.
  - depPlandTime(YYYYMMDD) 조회는 당일부터 +2일까지만 결과가 나온다.
    그 이후 날짜는 노선이 없어서가 아니라 API 지평선 밖이라 totalCount=0
    이 오지만, 응답만 보면 "그 날 운행 없음"과 구별이 안 된다 — 이 모듈은
    있는 그대로(found: False, no_direct_service)만 돌려주고 지어내지 않는다.
  - gradeId 파라미터로 서버 측 등급 필터링은 되지 않는다(무시됨).
    등급 필터는 클라이언트에서 gradeNm 문자열로 거른다.
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
    (build_bus_terminals.py) 의 도시 매칭 결과로 "이 도시에 다른 터미널이
    더 있나"를 넓혀서 찾는 폴백에 쓴다.

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
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import httpx

HERE = Path(__file__).parent
CACHE_PATH = HERE / "bus_terminals.json"

BASE = "https://apis.data.go.kr/1613000/ExpBusInfo"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))
TIMEOUT = 10.0
MAX_RESULTS = 5
FETCH_ROWS = 100          # 등급 필터·정렬 후 5건을 안정적으로 뽑기 위한 여유분
MAX_CANDIDATES = 4        # 지명 하나당 시도할 최대 터미널 후보 수 (콜 수 상한 = 이 값의 제곱)

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

_SUFFIXES = ("고속버스터미널", "종합버스터미널", "시외버스터미널", "종합터미널",
             "버스터미널", "터미널", "역")


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


def _fetch(dep_id: str, arr_id: str, date: str) -> list[dict] | None:
    """None = 호출 실패. [] = 그 조합에 노선 없음(다음 후보 조합 시도)."""
    try:
        r = httpx.get(f"{BASE}/GetStrtpntAlocFndExpbusInfo", params={
            "serviceKey": KEY, "_type": "json", "numOfRows": FETCH_ROWS,
            "depTerminalId": dep_id, "arrTerminalId": arr_id, "depPlandTime": date,
        }, timeout=TIMEOUT)
        r.raise_for_status()
        body = r.json()["response"]["body"]
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError):
        return None
    return _normalize_items(body)


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

    raw_items: list[dict] = []
    chosen_dep, chosen_arr = dep_candidates[0], arr_candidates[0]
    call_failed = False
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

    if not raw_items:
        if call_failed:
            return None
        return {"found": False, "reason": "no_direct_service",
                "origin": chosen_dep["name"], "destination": chosen_arr["name"], "date": date}

    items = raw_items
    if grade:
        g = grade.strip().casefold()
        items = [it for it in items if g in (it.get("gradeNm") or "").casefold()]
    if not items:
        return {"found": False, "reason": "no_direct_service",
                "origin": chosen_dep["name"], "destination": chosen_arr["name"], "date": date}

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
            "date": date, "buses": buses}


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
