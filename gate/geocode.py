"""
지명 → 좌표 지오코딩 계층. 3단계 폴백, 위에서부터 시도하고 성공하면 멈춘다.

  1단계: transit_nodes.json 직접 매칭 ("원주역", "서울역")
         외부 호출 없음. transit_nodes.match_node() 재사용.
  2단계: admin_areas.json 행정구역/랜드마크 좌표표 ("강원도 원주", "일산", "종로")
         외부 호출 없음.
  3단계: 카카오 로컬 API (KAKAO_REST_API_KEY 있을 때만). 1·2단계 실패 시에만 호출한다.

gate/admin_areas.json 은 행정안전부 API를 이번 세션에서 프로그래밍적으로
확보하지 못해 주요 시/군/구·랜드마크를 수동으로 채운 표다(transit_nodes_seed.json
과 같은 이유의 폴백). 실좌표이며, 나중에 행안부 좌표계 API로 갱신 가능한
구조로 남겨 둔다(레코드 형태만 유지하면 됨).

결과는 인메모리 LRU 캐시에 저장해 같은 지명 반복 조회 시 재계산하지 않는다.

사용법: python geocode.py --place "강원도 원주"
"""

from __future__ import annotations

import argparse
import json
import os
from functools import lru_cache
from pathlib import Path

import httpx

from transit_nodes import match_node

HERE = Path(__file__).parent
ADMIN_AREAS_PATH = HERE / "admin_areas.json"

ADMIN_AREAS: list[dict] = (json.loads(ADMIN_AREAS_PATH.read_text(encoding="utf-8"))
                            if ADMIN_AREAS_PATH.exists() else [])

KAKAO_KEY_ENV = "KAKAO_REST_API_KEY"
KAKAO_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_TIMEOUT = 5.0


def _match_admin_area(text: str) -> dict | None:
    """행정구역/랜드마크 별칭 매칭. 여러 후보가 겹치면(예: "제주 성산일출봉"이
    "제주"와 "성산일출봉" 모두를 포함) 더 구체적인(별칭이 더 긴) 쪽을 고른다."""
    t = text.strip().casefold()

    exact = [a for a in ADMIN_AREAS if any(al.casefold() == t for al in a["aliases"])]
    if exact:
        return exact[0]

    candidates = []
    for a in ADMIN_AREAS:
        for al in a["aliases"]:
            al_cf = al.casefold()
            if al_cf in t or t in al_cf:
                candidates.append((len(al_cf), a))
                break
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


def _call_kakao(text: str) -> dict | None:
    key = os.environ.get(KAKAO_KEY_ENV)
    if not key:
        return None
    try:
        r = httpx.get(KAKAO_URL, params={"query": text},
                      headers={"Authorization": f"KakaoAK {key}"}, timeout=KAKAO_TIMEOUT)
        r.raise_for_status()
        docs = r.json().get("documents") or []
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    if not docs:
        return None
    d = docs[0]
    return {"lat": float(d["y"]), "lon": float(d["x"]), "name": d.get("address_name", text)}


def _geocode_uncached(text: str) -> dict | None:
    # 1단계: 접근점 자체 이름
    node = match_node(text)
    if node is not None:
        return {"id": node["id"], "name": node["name"], "lat": node["lat"], "lon": node["lon"],
                "type": node["type"], "source": "transit_node"}

    # 2단계: 행정구역/랜드마크
    area = _match_admin_area(text)
    if area is not None:
        return {"id": f"ADMIN-{area['name']}", "name": area["name"],
                "lat": area["lat"], "lon": area["lon"], "source": "admin_area"}

    # 3단계: 외부 API (키 없으면 건너뜀)
    kakao = _call_kakao(text)
    if kakao is not None:
        return {"id": f"GEO-{text.strip()}", "name": kakao["name"],
                "lat": kakao["lat"], "lon": kakao["lon"], "source": "kakao"}

    return None


@lru_cache(maxsize=2048)
def _geocode_cached(text: str) -> str | None:
    """lru_cache 는 dict 를 캐시하면 호출부가 실수로 값을 변형할 위험이 있어
    JSON 문자열로 캐시하고 호출부에서 매번 새 dict 로 역직렬화한다."""
    result = _geocode_uncached(text)
    return json.dumps(result, ensure_ascii=False) if result is not None else None


def geocode(text: str | None) -> dict | None:
    if not text or not text.strip():
        return None
    cached = _geocode_cached(text.strip())
    return json.loads(cached) if cached is not None else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--place", required=True)
    a = ap.parse_args()

    if not os.environ.get(KAKAO_KEY_ENV):
        print("참고: KAKAO_REST_API_KEY 미설정 — 3단계(외부 API)는 건너뜁니다")

    result = geocode(a.place)
    print(json.dumps(result, ensure_ascii=False, indent=2))
