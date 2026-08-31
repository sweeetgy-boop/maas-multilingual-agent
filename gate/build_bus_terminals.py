#!/usr/bin/env python3
"""
TAGO 고속버스정보 터미널·도시코드·등급코드 캐시 빌드 스크립트.

  GetCtyCodeList        전국 도시코드
  GetExpBusTrminlList    터미널 목록 (terminalNm 없이 호출해도 전량(453건, 2026-08
                         기준) 이 한 페이지로 온다 — 도시별 순회나 한 글자씩 전수
                         조회는 불필요함을 실측으로 확인했다)
  GetExpBusGradList      버스등급 코드

제약
  - GetExpBusTrminlList 응답에는 cityCode/cityName 필드가 없다. 터미널명에
    도시명이 접두로 들어가는 관례(예: "대전복합" → "대전")를 이용해
    GetCtyCodeList 도시명과 부분 일치시켜 city_code/city_name 을 추정한다.
    매칭 안 되면 null 로 둔다 — 지어내지 않는다.
  - 터미널 좌표는 API 로 제공되지 않는다. gate/transit_nodes.json 의
    bus_terminal 노드와 이름/별칭 부분 일치로 좌표를 보완하고, 안 되면
    lat/lon 은 null 로 둔다.
  - 인증키는 unquote 해서 쓴다 (%2F 등 이중 인코딩 방지, korail_api.py 와 동일).

사용법: python build_bus_terminals.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote

import httpx

HERE = Path(__file__).parent
OUT_PATH = HERE / "bus_terminals.json"
TRANSIT_NODES_PATH = HERE / "transit_nodes.json"

BASE = "https://apis.data.go.kr/1613000/ExpBusInfo"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))
TIMEOUT = 15.0

CITY_SUFFIXES = ("특별자치시", "특별자치도", "광역시", "특별시", "시", "군", "구")


def _call(client: httpx.Client, op: str, **params) -> list[dict]:
    r = client.get(f"{BASE}/{op}", params={
        "serviceKey": KEY, "_type": "json", "numOfRows": 1000, **params}, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()["response"]["body"]
    items = body.get("items", {}).get("item") or []
    if isinstance(items, dict):          # 1건이면 배열이 아니라 객체로 오는 공공데이터포털 특유의 문제
        items = [items]
    return items


def _strip_city_suffix(name: str) -> str:
    for suf in CITY_SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


def _match_city(terminal_nm: str, cities: list[dict]) -> tuple[str | None, str | None]:
    """터미널명에 도시명이 포함돼 있으면 그 도시로 본다. 여러 도시명이 부분
    일치하면(예: "성남" 이 "성남" 과 "성남시" 둘 다에 걸치는 경우는 없지만
    혼동 방지 차원) 가장 긴 이름이 이긴다."""
    best: tuple[int, str, str] | None = None
    for c in cities:
        short = _strip_city_suffix(c["cityName"])
        if short and short in terminal_nm:
            if best is None or len(short) > best[0]:
                best = (len(short), c["cityCode"], c["cityName"])
    if best is None:
        return None, None
    return best[1], best[2]


def _load_transit_bus_nodes() -> list[dict]:
    if not TRANSIT_NODES_PATH.exists():
        return []
    nodes = json.loads(TRANSIT_NODES_PATH.read_text(encoding="utf-8"))
    return [n for n in nodes if n["type"] == "bus_terminal"]


def _match_coords(terminal_nm: str, bus_nodes: list[dict]) -> tuple[float | None, float | None]:
    t = terminal_nm.casefold()
    candidates = [n for n in bus_nodes
                  if t in n["name"].casefold() or n["name"].casefold() in t
                  or any(t in a.casefold() or a.casefold() in t for a in n["aliases"])]
    if not candidates:
        return None, None
    candidates.sort(key=lambda n: len(n["name"]))
    return candidates[0]["lat"], candidates[0]["lon"]


def build() -> dict:
    if not KEY:
        raise SystemExit("DATA_GO_KR_KEY_ENC 환경변수가 필요합니다 (.env 참조)")

    with httpx.Client() as client:
        cities_raw = _call(client, "GetCtyCodeList")
        terminals_raw = _call(client, "GetExpBusTrminlList")
        grades_raw = _call(client, "GetExpBusGradList")

    cities = [{"code": c["cityCode"], "name": c["cityName"]} for c in cities_raw]
    grades = [{"id": g["gradeId"], "name": g["gradeNm"]} for g in grades_raw]

    bus_nodes = _load_transit_bus_nodes()
    terminals = []
    for t in terminals_raw:
        name = t["terminalNm"]
        city_code, city_name = _match_city(name, cities_raw)
        lat, lon = _match_coords(name, bus_nodes)
        terminals.append({
            "id": t["terminalId"], "name": name,
            "city_code": city_code, "city_name": city_name,
            "lat": lat, "lon": lon,
        })

    return {"terminals": terminals, "cities": cities, "grades": grades}


def main() -> None:
    data = build()
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    total = len(data["terminals"])
    no_coords = [t["name"] for t in data["terminals"] if t["lat"] is None]
    no_city = [t["name"] for t in data["terminals"] if t["city_code"] is None]
    print(f"터미널 {total}개, 도시 {len(data['cities'])}개, 등급 {len(data['grades'])}개 "
          f"→ {OUT_PATH}", file=sys.stderr)
    # 좌표·도시 보완은 best-effort 다. 실패 건수를 명시적으로 남겨야 캐시를
    # 다시 만들 때 품질이 나빠졌는지(예: transit_nodes.json 이 줄었는지)
    # 눈에 띈다. 좌표 미보완은 정상이다 — transit_nodes 에는 주요 터미널만 있다.
    print(f"  좌표 보완 실패 {len(no_coords)}/{total}건 "
          f"(예: {', '.join(no_coords[:5])}{' …' if len(no_coords) > 5 else ''})",
          file=sys.stderr)
    print(f"  도시 매칭 실패 {len(no_city)}/{total}건 "
          f"(예: {', '.join(no_city[:5])}{' …' if len(no_city) > 5 else ''})",
          file=sys.stderr)


if __name__ == "__main__":
    main()
