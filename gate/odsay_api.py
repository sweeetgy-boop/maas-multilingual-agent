#!/usr/bin/env python3
"""
ODsay LAB 대중교통 경로탐색 API 어댑터

  https://api.odsay.com/v1/api/searchPubTransPathT

⚠️ 작업 0 실측 미완료 (2026-08-31 세션 기준): ODSAY_API_KEY 가 설정돼 있지
않아 실제 호출로 응답 스키마를 검증하지 못했다. 아래 구현은 공식 문서
(lab.odsay.com/guide/releaseReference)만을 근거로 작성했다 —
korail_api.py/expbus_api.py 처럼 curl 실측으로 확정한 스키마가 아니다.
키가 생기면 CLI(`python odsay_api.py --from ... --to ...`)로 실제 응답을
찍어보고 이 주석과 _normalize() 를 대조/수정할 것.

문서 기준 스키마
  요청: apiKey, SX/SY(출발 경도/위도), EX/EY(도착 경도/위도), OPT, output=json
  응답: response.result.path[] — OPT=0(추천경로)이면 보통 1개, 첫 번째를 쓴다.
        path[].info.totalTime(분) / payment(원) /
               busTransitCount / subwayTransitCount
        path[].subPath[] — trafficType: 1=지하철 2=버스 3=도보
          공통: sectionTime(분), startName, endName
          도보: distance(m)
          버스/지하철: stationCount, lane[0].busNo 또는 lane[0].name
  에러: 문서에 정확한 JSON 에러 엔벨로프가 명시돼 있지 않다(코드-메시지
  표만 제공, 예: -99=검색결과없음, 3=출발지정류장없음). result 키가 없거나
  path 가 비어 있거나 구조가 예상과 다르면(KeyError/TypeError/IndexError)
  전부 실패로 간주해 None 을 돌려준다 — 모르는 에러 형태라도 예외 없이
  안전하게 처리하는 것이 이 함수의 계약이다.

인증키는 unquote 해서 쓴다 (korail_api.py/expbus_api.py 와 동일 이유 —
ODsay 키도 URL 인코딩된 형태로 발급되므로, httpx params= 로 다시 인코딩하기
전에 한 번 풀어야 %252F 류 이중 인코딩을 피할 수 있다).

캐시: 경로는 자주 안 바뀌므로 좌표를 소수점 4자리(약 11m 오차)로 반올림한
키로 TTL 5분 캐싱한다. 실패(None)는 캐싱하지 않는다 — 일시적 네트워크
오류까지 5분간 강제로 폴백시키면 안 된다.

키 없음/호출 실패/응답 스키마 이상이면 예외를 던지지 않고 None 을 돌려준다.
호출부(tools.py)는 None 을 "실시간 데이터 없음"으로 해석해 기존 안내
문구("시내 대중교통 또는 택시 이용")로 폴백해야 한다.

사용법
  python odsay_api.py --from 126.9707,37.5547 --to 127.0276,37.4979
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import unquote

import httpx

BASE = "https://api.odsay.com/v1/api/searchPubTransPathT"
KEY = unquote(os.environ.get("ODSAY_API_KEY", ""))
TIMEOUT = 10.0
CACHE_TTL = 300  # 경로는 자주 안 바뀐다. 5분 캐시

_TRAFFIC_MODE = {1: "SUBWAY", 2: "BUS", 3: "WALK"}

_cache: dict[tuple, tuple[float, dict]] = {}


def _cache_key(sx: float, sy: float, ex: float, ey: float, opt: int) -> tuple:
    return (round(sx, 4), round(sy, 4), round(ex, 4), round(ey, 4), opt)


def _lane_label(sub: dict) -> str | None:
    lanes = sub.get("lane") or []
    if not lanes:
        return None
    lane = lanes[0]
    return lane.get("busNo") or lane.get("name")


def _normalize_leg(sub: dict, origin_name: str, destination_name: str,
                    is_first: bool, is_last: bool) -> dict | None:
    """ODsay subPath 1건 -> tools.py 의 legs 항목. trafficType 을 모르면
    None (호출부가 건너뛴다). startName/endName 이 비어 있으면(원좌표
    구간은 이름이 없는 경우가 있다) 첫/마지막 구간에 한해 origin_name/
    destination_name 으로 채운다."""
    mode = _TRAFFIC_MODE.get(sub.get("trafficType"))
    if mode is None:
        return None

    frm = sub.get("startName") or (origin_name if is_first else None)
    to = sub.get("endName") or (destination_name if is_last else None)
    if not frm or not to:
        return None

    leg: dict = {"mode": mode, "from": frm, "to": to}
    if sub.get("sectionTime") is not None:
        leg["duration_min"] = sub["sectionTime"]

    if mode == "WALK":
        if sub.get("distance") is not None:
            leg["distance_m"] = sub["distance"]
    else:
        service = _lane_label(sub)
        if service is not None:
            leg["service"] = service
        if sub.get("stationCount") is not None:
            leg["stations"] = sub["stationCount"]

    return leg


def _normalize(data: dict, origin_name: str, destination_name: str) -> dict | None:
    """ODsay 원본 응답 -> {"legs":[...], "total_min":, "total_fare_krw":,
    "transfers":} 정규화 스키마. 구조가 예상과 다르면 None."""
    try:
        paths = data["result"]["path"]
        if not paths:
            return None
        path = paths[0]
        info = path.get("info", {})
        sub_paths = path.get("subPath", [])

        legs = []
        for i, sub in enumerate(sub_paths):
            leg = _normalize_leg(sub, origin_name, destination_name,
                                  is_first=(i == 0), is_last=(i == len(sub_paths) - 1))
            if leg is not None:
                legs.append(leg)
        if not legs:
            return None

        result: dict = {"legs": legs}
        if info.get("totalTime") is not None:
            result["total_min"] = info["totalTime"]
        if info.get("payment") is not None:
            result["total_fare_krw"] = info["payment"]
        result["transfers"] = (info.get("busTransitCount") or 0) + (info.get("subwayTransitCount") or 0)
        return result
    except (KeyError, TypeError, IndexError):
        return None


def search_route(sx: float, sy: float, ex: float, ey: float, opt: int = 0,
                  origin_name: str = "출발지", destination_name: str = "목적지") -> dict | None:
    """좌표 기반 대중교통 경로.

    Args:
        sx, sy: 출발지 경도/위도. ex, ey: 도착지 경도/위도.
        opt: 0=추천경로(기본), 1=타입별정렬.
        origin_name, destination_name: 첫/마지막 구간의 이름이 ODsay
            응답에 비어 있을 때만 대체로 쓰인다 (원좌표는 정류장명이 없다).

    Returns:
        {"legs": [...], "total_min": int, "total_fare_krw": int, "transfers": int}
        또는 실패 시 None (키 없음/호출 오류/좌표 범위 밖/응답 구조 이상).
    """
    if not KEY:
        return None

    key = _cache_key(sx, sy, ex, ey, opt)
    cached = _cache.get(key)
    now = time.time()
    if cached is not None and now - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        r = httpx.get(BASE, params={
            "apiKey": KEY, "SX": sx, "SY": sy, "EX": ex, "EY": ey,
            "OPT": opt, "SearchPathType": 0, "output": "json",
        }, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return None

    result = _normalize(data, origin_name, destination_name)
    if result is not None:
        _cache[key] = (now, result)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="origin", required=True, help="경도,위도")
    ap.add_argument("--to", dest="destination", required=True, help="경도,위도")
    ap.add_argument("--opt", type=int, default=0)
    a = ap.parse_args()

    if not KEY:
        print("경고: ODSAY_API_KEY 미설정 — None 이 반환됩니다", file=sys.stderr)

    sx, sy = (float(v) for v in a.origin.split(","))
    ex, ey = (float(v) for v in a.destination.split(","))
    result = search_route(sx, sy, ex, ey, a.opt)
    print(json.dumps(result, ensure_ascii=False, indent=2))
