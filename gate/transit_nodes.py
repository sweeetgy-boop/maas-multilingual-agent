"""
교통 접근점(철도역·버스터미널·공항·도시철도) 조회 계층.

gate/transit_nodes.json (build_transit_nodes.py 로 생성) 을 읽어
  1) 이름/별칭으로 접근점 자체를 직접 찾거나 (match_node)
  2) 좌표 기준 반경 내 가까운 접근점을 찾는다 (find_access_points)

find_access_points 는 타입별로 최소 1개씩 포함되도록 보정한다 — 순수
거리순으로만 자르면 철도역만 몰려 있는 지역에서 버스 옵션이 사라진다.

사용법: python transit_nodes.py --near 37.3422,127.9202 --radius 15
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from geo_utils import haversine_m

HERE = Path(__file__).parent
NODES_PATH = HERE / "transit_nodes.json"

NODES: list[dict] = json.loads(NODES_PATH.read_text(encoding="utf-8")) if NODES_PATH.exists() else []
NODE_BY_ID: dict[str, dict] = {n["id"]: n for n in NODES}


def match_node(text: str | None) -> dict | None:
    """자유문 지명이 접근점 자체(이름/별칭)와 일치하면 그 노드를 돌려준다.
    외부 호출 없이 메모리 사전만 조회하므로 가장 빠른 경로다.
    "역"처럼 여러 후보가 겹치면 이름이 가장 짧은(가장 구체적인) 쪽을 고른다."""
    if not text:
        return None
    t = text.strip().casefold()

    exact = [n for n in NODES if n["name"].casefold() == t
             or any(a.casefold() == t for a in n["aliases"])]
    if exact:
        return exact[0]

    candidates = [n for n in NODES
                  if t in n["name"].casefold() or n["name"].casefold() in t
                  or any(t in a.casefold() or a.casefold() in t for a in n["aliases"])]
    if not candidates:
        return None
    candidates.sort(key=lambda n: len(n["name"]))
    return candidates[0]


def _type_rank(t: str) -> int:
    order = {"rail": 0, "bus_terminal": 1, "airport": 2, "subway": 3}
    return order.get(t, 9)


def find_access_points(lat: float, lon: float, radius_km: float = 15, limit: int = 3) -> list[dict]:
    """좌표 기준 반경 내 접근점을 거리순으로 최대 limit개 반환한다.
    반경 내 타입이 여러 개면 각 타입이 최소 1개는 포함되도록 보정한다.
    반경 내에 하나도 없으면 반경을 30km로 늘려 재시도하고, 그래도 없으면 [] 를 반환한다."""
    for r in (radius_km, 30):
        within = []
        for n in NODES:
            d = haversine_m((lat, lon), (n["lat"], n["lon"]))
            if d <= r * 1000:
                within.append((d, n))
        if within:
            within.sort(key=lambda x: x[0])
            return _diversify(within, limit)
    return []


def _diversify(within: list[tuple[float, dict]], limit: int) -> list[dict]:
    """거리순 (거리, 노드) 목록에서 상위 limit개를 뽑되, 반경 안에 존재하는
    타입이 상위 limit개에서 빠졌으면 가장 가까운 그 타입 노드로 바꿔 넣는다."""
    selected = list(within[:limit])
    present_types = {n["type"] for _, n in within}
    selected_types = {n["type"] for _, n in selected}
    missing = present_types - selected_types

    for t in missing:
        closest = next((d, n) for d, n in within if n["type"] == t)
        # 이미 2개 이상 뽑힌 타입 중 가장 먼 항목을 교체한다
        type_counts: dict[str, int] = {}
        for _, n in selected:
            type_counts[n["type"]] = type_counts.get(n["type"], 0) + 1
        swap_idx = None
        for i in range(len(selected) - 1, -1, -1):
            if type_counts[selected[i][1]["type"]] > 1:
                swap_idx = i
                break
        if swap_idx is not None:
            selected[swap_idx] = closest
        else:
            selected.append(closest)

    selected.sort(key=lambda x: x[0])
    return [{"name": n["name"], "type": n["type"], "distance_m": round(d)} for d, n in selected]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--near", required=True, help="lat,lon")
    ap.add_argument("--radius", type=float, default=15)
    ap.add_argument("--limit", type=int, default=3)
    a = ap.parse_args()

    lat_s, lon_s = a.near.split(",")
    result = find_access_points(float(lat_s), float(lon_s), a.radius, a.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
