#!/usr/bin/env python3
"""
전국 교통 접근점(철도역·버스터미널·공항) 좌표 빌드 스크립트.

실제 소스를 조사한 결과 (2026-08-27 기준)

  a) 공공데이터포털 "한국철도공사_철도운영정보_노선정보"
     → 노선/역코드는 있으나 위도·경도가 없다. 좌표용으로 못 쓴다.

  b) TAGO 시외버스 터미널 목록 API
     → 서비스ID를 추정해 호출했더니 (SuburbsBusTrminlInfoService1)
       NO_OPENAPI_SERVICE_ERROR(코드 12, "해당 오픈API 서비스가 없거나 폐기됨").
       공공데이터포털에서 실제 존재를 확인한 TAGO 서비스는 버스정류소정보/
       버스도착정보/버스노선정보/버스위치정보/고속버스정보뿐이며, 터미널
       좌표를 직접 주는 API를 찾지 못했다.

  c) 한국공항공사 공항 목록
     → 항공정보포털(에어포탈) API가 있으나 이번 조사에서 안정적으로
       접근 가능한 엔드포인트를 확인하지 못했다.

  d) "한국철도공사_역 위치 정보_20240401" (공공데이터포털, 파일데이터)
     → 202개 역의 역명/위도/경도를 담고 있는 것으로 확인했다
       (data.go.kr/data/15127532/fileData.do). 다만 파일데이터라 서비스키로
       바로 REST 호출이 안 되고 포털 다운로드 세션이 필요해, 이 스크립트에서
       자동으로 받아올 수 없었다.

결론: 좌표를 프로그래밍적으로 확보하지 못했다. 지시서의 폴백 경로대로
transit_nodes_seed.json 에 KTX/SRT 주요역·광역시도 시외/고속버스터미널·
국내공항을 실좌표로 하드코딩해 두고, 이 스크립트는 그 seed 를 읽어
gate/transit_nodes.json 을 만든다.

향후 실제 API/파일을 확보하면 fetch_from_*() 자리에 파서를 채우고
merge_sources() 에 연결하면 된다 — seed 는 그대로 최후 폴백으로 남긴다.

사용법: python build_transit_nodes.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
SEED_PATH = HERE / "transit_nodes_seed.json"
OUT_PATH = HERE / "transit_nodes.json"

REQUIRED_FIELDS = {"id", "name", "aliases", "type", "lat", "lon", "operator"}
VALID_TYPES = {"rail", "bus_terminal", "airport", "subway"}


def fetch_from_korail_station_api() -> list[dict]:
    """실제 API 확보 시 이 자리에 채운다. 지금은 항상 빈 리스트."""
    return []


def fetch_from_tago_terminal_api() -> list[dict]:
    """실제 API 확보 시 이 자리에 채운다. 지금은 항상 빈 리스트."""
    return []


def fetch_from_airport_api() -> list[dict]:
    """실제 API 확보 시 이 자리에 채운다. 지금은 항상 빈 리스트."""
    return []


def load_seed() -> list[dict]:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def merge_sources() -> list[dict]:
    """id 기준으로 병합한다. 나중 소스가 먼저 것을 덮어쓴다.
    지금은 seed 뿐이지만, 실 API 가 채워지면 seed 는 최후 폴백이 되도록
    seed 를 먼저 넣고 실 API 결과를 나중에 얹는다."""
    by_id: dict[str, dict] = {}
    for row in load_seed():
        by_id[row["id"]] = row
    for row in (fetch_from_korail_station_api() +
                fetch_from_tago_terminal_api() +
                fetch_from_airport_api()):
        by_id[row["id"]] = row
    return list(by_id.values())


def validate(rows: list[dict]) -> None:
    seen_ids = set()
    for r in rows:
        missing = REQUIRED_FIELDS - r.keys()
        if missing:
            raise ValueError(f"{r.get('id')}: 필드 누락 {missing}")
        if r["type"] not in VALID_TYPES:
            raise ValueError(f"{r['id']}: 알 수 없는 type {r['type']!r}")
        if not (-90 <= r["lat"] <= 90 and -180 <= r["lon"] <= 180):
            raise ValueError(f"{r['id']}: 좌표 범위 이상 lat={r['lat']} lon={r['lon']}")
        if r["id"] in seen_ids:
            raise ValueError(f"id 중복: {r['id']}")
        seen_ids.add(r["id"])


def main() -> None:
    rows = merge_sources()
    validate(rows)
    rows.sort(key=lambda r: r["id"])
    OUT_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    print(f"{len(rows)}개 접근점 → {OUT_PATH}")
    for t, n in sorted(by_type.items()):
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
