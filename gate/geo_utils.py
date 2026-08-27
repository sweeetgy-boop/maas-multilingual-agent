"""
공통 지리 유틸 — haversine 거리 계산.

citydata_api.py, transit_nodes.py, geocode.py 가 모두 이 함수를 쓴다.
원래 citydata_api.py 안에 _haversine_m 으로 중복 구현돼 있던 것을 빼냈다.
"""

from __future__ import annotations

import math


def haversine_m(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """두 (lat, lon) 좌표 사이의 대권거리(미터)."""
    r = 6371000.0
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
