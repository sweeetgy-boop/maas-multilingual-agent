#!/usr/bin/env python3
"""
KRIC 도시철도 노선·역 캐시 빌드 스크립트.

  trainUseInfo / subwayRouteInfo   도시철도 전체노선정보

목적은 **서울 121장소 밖 도시철도 질의에 역 실재·노선·환승 여부를 답하는 것**
하나다. 시각표가 아니다 — 이 스크립트도, 이 스크립트가 만드는 캐시도 시각
정보를 담지 않는다(아래 "범위" 참조).

범위 (2026-09-01 실측으로 확정)
  현재 인증키는 subwayRouteInfo **하나만** 승인돼 있다. 같은 서비스ID
  아래 subwayTimetable / subwayTimetableExp / subwayEnvironmental 은 전부
  resultCode 30("등록되지 않은 서비스키입니다")으로 막힌다. 없는 오퍼레이션은
  30 이 아니라 12("해당 오픈 API 서비스가 없거나 폐기되었습니다")를 주므로,
  30 은 "오퍼레이션은 있으나 이 키에 권한이 없다"는 뜻이다 — 키 문제가
  아니다(같은 순간 subwayRouteInfo 는 00).

  따라서 이 캐시에는 도착시각·출발시각·열차번호가 없다. subwayTimetable
  승인이 나기 전까지 호출부는 시간표를 만들어내면 안 된다.

API 제약 (실측)
  - 오류도 HTTP 200 으로 온다. status_code 로 판정하면 안 되고
    header.resultCode 를 봐야 한다.
  - **railOprIsttCd 는 필터로 동작하지 않는다.** 이 파라미터만 주면 0건이고,
    lnCd 와 같이 줘도 무시된다(railOprIsttCd=BS&lnCd=1 → 219건으로
    lnCd=1 단독과 동일). mreaWideCd 도 필터로는 무효다.
    **유효한 필터는 lnCd 뿐**이라 노선코드를 순회하고 기관별 분리는
    받아온 뒤 클라이언트에서 한다.
  - 인증키($ 와 / 포함)는 원본·단일 퍼센트인코딩 둘 다 통과하고 이중
    인코딩만 실패한다. httpx params= 가 단일 인코딩을 하므로 **미리 인코딩
    하거나 unquote 하면 안 된다**(형제 스크립트들이 DATA_GO_KR_KEY_ENC 를
    unquote 하는 것과 다른 점이다 — 그쪽 키는 애초에 인코딩된 값이다).
  - 응답 필드는 8개뿐이고 **좌표가 없다**:
      mreaWideCd routCd routNm railOprIsttCd lnCd stinCd stinNm stinConsOrdr
  - DNS·연결이 간헐적으로 실패한다(첫 호출 실패 후 재시도하면 성공).
    호출마다 재시도한다.

코드 체계 주의
  - stinCd 포맷이 기관마다 다르다. BS·GJ 3자리("119") / DG·DJ 4자리
    제로패딩("0130") / KR 하이픈("100-3","K118") / S1 3~4자리.
    **문자열로 다뤄야 하며 정수로 바꾸면 안 된다.**
  - 숫자 lnCd 는 도시 간 재사용된다. lnCd=1 하나에 서울·부산·대구·광주·
    대전 1호선이 모두 들어 있다. 노선 식별에는 (region, operator, line)
    셋이 다 필요하다.
  - 역명도 도시 간 충돌한다. "시청"은 서울(S1)·부산(BS)·대전(DJ)에 모두
    있다. 역명만으로 매핑하면 안 된다.

사용법
  export KRIC_SERVICE_KEY='...'          # 따옴표 필수 ($ 가 들어 있다)
  python build_subway_stations.py
  python build_subway_stations.py --sweep      # 노선코드 재탐색(느림)
  python build_subway_stations.py --geocode    # 좌표 보완(카카오 호출)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

BASE = "https://openapi.kric.go.kr/openapi"
SERVICE = "trainUseInfo"
OPERATION = "subwayRouteInfo"

KEY = os.environ.get("KRIC_SERVICE_KEY", "")

OUT_PATH = Path(__file__).with_name("subway_stations.json")

TIMEOUT = 20.0
RETRIES = 3
SLEEP_BETWEEN = 0.2

# 2026-09-01 전수 스윕(숫자 0~39 + 영문 1~2자 전조합)으로 확인된 노선코드 25개.
# 필터가 lnCd 뿐이라 이 목록을 순회하는 것이 전량 수집 경로다. 신설 노선이
# 생기면 --sweep 으로 다시 찾는다(스윕은 약 1000회 호출이라 기본값이 아니다).
KNOWN_LINE_CODES = [
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "A", "A1", "B1", "D1", "E1", "I1", "I2",
    "K1", "K2", "K5", "K6", "K7", "L1", "UI", "U1", "WS",
]

# mreaWideCd → 권역명. API 가 코드만 주므로 이름은 소속 역으로 역산했다
# (02 는 BS 전 노선, 03 은 DG, 04 는 GJ, 05 는 DJ 가 들어 있다).
# API 가 준 값이 아니라 **파생 라벨**이다.
REGION_NAMES = {
    "01": "수도권",
    "02": "부산·울산권",
    "03": "대구권",
    "04": "광주권",
    "05": "대전권",
}

# railOprIsttCd → 운영기관명. **API 응답에 기관명이 없어 별도로 채운 표다.**
# 확신할 수 없는 코드는 지어내지 않고 None 으로 둔다(호출부는 코드로 폴백).
# NU(진접선·별내선 구간)와 GU(별내선 구리 구간)는 노선 소유·운영 주체가
# 위탁 관계로 갈려 단정할 수 없어 비워 둔다.
OPERATOR_NAMES = {
    "S1": "서울교통공사",
    "S9": "서울시메트로9호선",
    "KR": "한국철도공사",
    "IC": "인천교통공사",
    "BS": "부산교통공사",
    "DG": "대구교통공사",
    "DJ": "대전교통공사",
    "GJ": "광주교통공사",
    "BG": "부산김해경전철",
    "AR": "공항철도",
    "DX": "신분당선",
    "EV": "용인경전철",
    "UI": "우이신설경전철",
    "UL": "의정부경전철",
    "SL": "남서울경전철",
    "SW": "서해철도",
    "SR": "에스알",
    "GX": None,
    "NU": None,
    "GU": None,
}


def fetch_line(client: httpx.Client, ln_cd: str) -> list[dict]:
    """한 노선코드의 역 목록. 실패·데이터없음이면 빈 리스트.

    resultCode 는 00(정상) / 03(데이터 없음) / 12(없는 오퍼레이션) /
    30(미승인 키) 를 관찰했다. 03 은 정상적인 '해당 없음'이므로 조용히
    빈 리스트를 준다. 30·12 는 설정 문제이니 소리내어 죽는다."""
    params = {"serviceKey": KEY, "format": "json", "lnCd": ln_cd}
    last: Exception | None = None

    for attempt in range(RETRIES):
        try:
            r = client.get(f"{BASE}/{SERVICE}/{OPERATION}", params=params, timeout=TIMEOUT)
            body = r.json()
        except Exception as e:                      # DNS·연결·JSON 파싱
            last = e
            time.sleep(1.0 * (attempt + 1))
            continue

        header = body.get("header") or {}
        code = header.get("resultCode")

        if code == "00":
            return body.get("body") or []
        if code == "03":
            return []
        if code in ("30", "12"):
            raise SystemExit(
                f"lnCd={ln_cd}: resultCode={code} {header.get('resultMsg')}\n"
                f"  30 이면 KRIC_SERVICE_KEY 가 {OPERATION} 오퍼레이션에 승인돼 있지 않습니다.\n"
                f"  12 면 오퍼레이션 경로가 바뀐 것입니다.")
        # 그 밖의 코드는 일시적일 수 있으니 재시도
        last = RuntimeError(f"resultCode={code} {header.get('resultMsg')}")
        time.sleep(1.0 * (attempt + 1))

    print(f"  ! lnCd={ln_cd} 조회 실패 ({last}) — 건너뜁니다", file=sys.stderr)
    return []


def sweep_line_codes(client: httpx.Client) -> list[str]:
    """노선코드 전수 탐색. 필터가 lnCd 뿐이고 코드 목록을 주는 오퍼레이션이
    없어서, 후보를 직접 두드려 보는 것 외에 방법이 없다. 호출이 많으므로
    기본 경로가 아니다 — KNOWN_LINE_CODES 갱신용이다."""
    import string

    cands = [str(i) for i in range(0, 40)]
    for a in string.ascii_uppercase:
        cands.append(a)
        cands.extend(a + b for b in string.ascii_uppercase + string.digits)

    found = []
    for i, c in enumerate(cands, 1):
        if i % 100 == 0:
            print(f"  스윕 {i}/{len(cands)} … 발견 {len(found)}", file=sys.stderr)
        if fetch_line(client, c):
            found.append(c)
            print(f"  + lnCd={c}", file=sys.stderr)
        time.sleep(SLEEP_BETWEEN)
    return found


# 권역별 대략 경계상자 (lat_min, lat_max, lon_min, lon_max).
# 지오코딩 결과가 그 역이 속한 권역 밖으로 나오면 **버린다**. 틀린 좌표를
# 채우는 것이 비워 두는 것보다 훨씬 나쁘기 때문이다(실측: "서면역"이 검색에
# 실패한 뒤 "서면"으로 재조회되면 충남의 행정구역 서면에 걸려 부산에서
# 250km 떨어진 좌표가 들어왔다).
REGION_BBOX = {
    "01": (36.8, 38.3, 126.3, 127.8),
    "02": (34.9, 35.8, 128.7, 129.5),
    "03": (35.6, 36.2, 128.3, 129.0),
    "04": (35.0, 35.3, 126.6, 127.0),
    "05": (36.1, 36.7, 127.2, 127.6),
}


def fill_coords(stations: list[dict]) -> int:
    """좌표 보완. API 가 좌표를 주지 않아 별도 경로로 채운다.

    **수율이 매우 낮다(실측 8건 중 1건).** geocode() 가 최종적으로 부르는
    카카오 엔드포인트는 주소 검색이라 "서면역" 같은 POI 를 찾지 못한다.
    역 좌표를 제대로 채우려면 키워드(장소) 검색 경로가 따로 필요한데 그건
    이 스크립트의 범위 밖이다. 그래서 --geocode 는 기본값이 아니고, 실패한
    역은 null 로 남는다.

    조회는 "역"을 붙인 형태로만 한다. 맨이름으로 재조회하면 동명의
    행정구역에 걸려 엉뚱한 좌표가 들어온다 — 위 REGION_BBOX 주석 참조.
    경계상자를 벗어난 결과도 버린다."""
    try:
        from geocode import geocode
    except Exception as e:
        print(f"  ! geocode 모듈을 불러올 수 없습니다 ({e}) — 좌표를 건너뜁니다",
              file=sys.stderr)
        return 0

    filled = 0
    seen: dict[str, dict | None] = {}
    for i, s in enumerate(stations, 1):
        if i % 100 == 0:
            print(f"  지오코딩 {i}/{len(stations)} … 성공 {filled}", file=sys.stderr)

        # 같은 역명이 여러 노선에 걸리면(환승역) 한 번만 조회한다.
        key = f"{s['region']}|{s['name']}"
        if key not in seen:
            # 괄호 병기는 지오코딩을 방해한다: "청량리(서울시립대입구)" → "청량리"
            base = s["name"].split("(")[0].strip()
            hit = geocode(base if base.endswith("역") else f"{base}역")

            # 권역 밖 좌표는 다른 지명에 잘못 걸린 것이다. 버린다.
            box = REGION_BBOX.get(s["region"])
            if hit and box:
                lo_lat, hi_lat, lo_lon, hi_lon = box
                if not (lo_lat <= hit["lat"] <= hi_lat and lo_lon <= hit["lon"] <= hi_lon):
                    print(f"  ! {s['name']} 좌표가 {s['region_name']} 밖 "
                          f"({hit['lat']:.4f},{hit['lon']:.4f}) — 버립니다", file=sys.stderr)
                    hit = None
            seen[key] = hit

        hit = seen[key]
        if hit:
            s["lat"], s["lon"] = hit["lat"], hit["lon"]
            filled += 1
    return filled


def build(sweep: bool = False, do_geocode: bool = False) -> dict:
    if not KEY:
        raise SystemExit("KRIC_SERVICE_KEY 환경변수가 필요합니다 (.env 참조). "
                         "값에 $ 가 있으니 export 시 작은따옴표로 감싸세요.")

    with httpx.Client() as client:
        codes = sweep_line_codes(client) if sweep else KNOWN_LINE_CODES
        if sweep:
            print(f"스윕 결과 노선코드 {len(codes)}개: {codes}", file=sys.stderr)

        rows: list[dict] = []
        for c in codes:
            got = fetch_line(client, c)
            print(f"  lnCd={c:4} {len(got):4}건", file=sys.stderr)
            rows.extend(got)
            time.sleep(SLEEP_BETWEEN)

    if not rows:
        raise SystemExit("수집된 역이 없습니다. 인증키·네트워크를 확인하세요.")

    operators: dict[str, dict] = {}
    lines: dict[tuple, dict] = {}
    stations: list[dict] = []

    for b in rows:
        op = b["railOprIsttCd"]
        region = b["mreaWideCd"]
        ln = b["lnCd"]

        # 기관과 권역은 1:1 이 아니다 — KR(한국철도공사)만 해도 수도권·동해선(부산)·
        # 대경선(대구) 셋에 걸친다. 기관에 권역을 하나만 달면 먼저 만난 권역으로
        # 잘못 굳는다. 권역은 목록으로 들고, 단일 권역 판정은 호출부가 한다.
        o = operators.setdefault(op, {
            "code": op,
            # 이름을 모르는 기관은 코드를 그대로 노출한다 — 지어내지 않는다.
            "name": OPERATOR_NAMES.get(op) or op,
            "name_known": OPERATOR_NAMES.get(op) is not None,
            "regions": [],
        })
        if region not in o["regions"]:
            o["regions"].append(region)

        lines.setdefault((op, ln), {
            "operator": op,
            "code": ln,
            "name": b["routNm"],
            "region": region,
            "region_name": REGION_NAMES.get(region, region),
        })

        stations.append({
            "operator": op,
            "line": ln,
            "code": b["stinCd"],          # 문자열 유지 — 기관마다 포맷이 다르다
            "name": b["stinNm"],
            "region": region,
            "region_name": REGION_NAMES.get(region, region),
            "order": b.get("stinConsOrdr"),
            "lat": None,                  # API 에 좌표가 없다
            "lon": None,
        })

    filled = fill_coords(stations) if do_geocode else 0

    stations.sort(key=lambda s: (s["region"], s["operator"], s["line"],
                                 s["order"] if s["order"] is not None else 0))

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": f"KRIC {SERVICE}/{OPERATION}",
        # 이 캐시에 시각표가 없다는 사실을 데이터 안에 남긴다. 호출부가
        # 이 파일만 보고도 시간표를 줄 수 없음을 알 수 있어야 한다.
        "has_timetable": False,
        "timetable_note": ("subwayTimetable 오퍼레이션이 현재 인증키에 승인돼 있지 않아 "
                           "도착·출발시각 정보가 없습니다. 노선·역 정보 전용 캐시입니다."),
        "count": len(stations),
        "operator_count": len(operators),
        "line_count": len(lines),
        "coords_filled": filled,
        "operators": sorted(operators.values(),
                            key=lambda o: (min(o["regions"]), o["code"])),
        "lines": sorted(lines.values(), key=lambda l: (l["region"], l["operator"], l["code"])),
        "stations": stations,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="KRIC 도시철도 노선·역 캐시 빌드")
    ap.add_argument("--sweep", action="store_true",
                    help="노선코드를 전수 탐색한다 (약 1000회 호출, 느리다)")
    ap.add_argument("--geocode", action="store_true",
                    help="좌표를 지오코딩으로 보완한다 (카카오 API 호출 발생)")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    data = build(sweep=args.sweep, do_geocode=args.geocode)
    Path(args.out).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{args.out}")
    print(f"  역 {data['count']}개 / 노선 {data['line_count']}개 / 기관 {data['operator_count']}개")
    for o in data["operators"]:
        n = sum(1 for s in data["stations"] if s["operator"] == o["code"])
        regions = "·".join(REGION_NAMES.get(r, r) for r in o["regions"])
        print(f"    {o['code']:3} {o['name']:12} {n:4}역  {regions}")
    print("  권역별:")
    for r in sorted({s["region"] for s in data["stations"]}):
        n = sum(1 for s in data["stations"] if s["region"] == r)
        print(f"    {r} {REGION_NAMES.get(r, r):10} {n:4}역")
    if data["coords_filled"]:
        print(f"  좌표 보완 {data['coords_filled']}/{data['count']}")


if __name__ == "__main__":
    main()
