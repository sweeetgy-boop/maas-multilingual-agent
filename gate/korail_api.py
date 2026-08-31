#!/usr/bin/env python3
"""
한국철도공사 열차운행정보 API 어댑터

  https://apis.data.go.kr/B551457/run/v2

작업 0 실측 결과 (2026-08-31 기준)
  - **cond 파라미터가 동작한다.** 형식은 `cond[필드명::연산자]` 다.
    이전에 runDt/runYmd/depStnCd 등으로 시도해 전부 무시됐던 것은
    파라미터 이름이 아니라 형식이 틀렸기 때문이다.
      cond[dptre_stn_nm::EQ]=서울 & cond[arvl_stn_nm::EQ]=부산
        → totalCount 70,198 → 5,987 (다중 조건은 AND 결합)
  - 동작 연산자: EQ, GT, GTE, LT, LTE, LIKE.
    NE, BETWEEN 은 응답 자체가 깨진다(사용 금지).
    날짜 범위는 GTE + LTE 조합으로 조회한다.
  - LIKE 는 한글 역명에서 부분일치로 정상 동작한다.
    cond[value::LIKE]=서울 → 서울/공항서울/지하서울/서울대 …
  - RunInfo2 도 RunPlan2 와 같은 cond 형식을 받는다(실측):
      필터 없음                                   → 806,230
      run_ymd::EQ=20260830                        →   9,128
      + stn_nm::EQ=서울                            →     333
      + stop_se_cd::EQ=01                         →     140
  - cond 키의 대괄호·콜론은 httpx 가 %5B / %3A%3A 로 퍼센트 인코딩해
    보내고 서버가 그대로 받는다. URL 을 문자열로 직접 조립할 필요가 없다.
    (serviceKey 만 unquote 로 이중 인코딩을 피하면 된다.)
  - **미래 데이터가 없다.** 이것이 이 모듈의 용도를 결정한다.
      RunPlan2 cond[run_ymd::EQ]=20260830  →  814
      RunPlan2 cond[run_ymd::EQ]=20260831  →    0   (오늘)
      RunInfo2 cond[run_ymd::GTE]=20260831 →    0
    보유 범위는 이분 탐색으로 확정: **20260531 ~ 20260830, 92일 연속**
    (RunPlan2 70,198건 ≈ 771편/일, RunInfo2 806,230건 ≈ 8,859건/일).
    두 오퍼레이션의 범위가 같고, 7일 간격 14개 표본에 결측일이 없다 —
    산발적 과거 날짜가 아니라 약 3개월 롤링 윈도우로 연속 적재된다.
    즉 이 API 는 시간표가 아니라 **과거 실적 데이터**다.
    따라서 search_rail(시간표 조회)에는 쓸 수 없고, 쓰면 안 된다.
    과거 운행을 미래 시간표처럼 제시하는 것이기 때문이다.
  - **열차종별(KTX/무궁화) 코드가 없다.** codes2 의 type 을 전수
    조사한 결과 13종뿐이고 종별 코드는 없다:
      stn_cd, sbwy_stn_cd, stor_stn_cd, mrnt_cd, sbwy_ln_cd,
      stop_se_cd, uppln_dn_se_cd, tmwd_se_cd, hdqt_cd,
      frg_se_cd, item_lclsf_cd, item_mclsf_cd, item_sclsf_cd
    열차번호 → 종별 매핑은 여전히 불가능하다. 종별을 지어내지 않는다.
  - 운임 정보도 없다(종전 조사와 동일).
  - codes2 는 cond 없이 호출하면 totalCount=0 이다. 반드시 조건을 준다.
  - **RunInfo2 에는 실제 운행시각이 있다.** 20260830 시발역 출발시각을
    RunPlan2 계획과 대조한 결과 129편 중 53편이 1~24분 차이를 보였다.
    계획 대비 실제 차이로 지연 산출이 가능하다 — 이 모듈이 실제로
    제공하는 값은 이것 하나다.

용도 (사용자 확인 완료: 판단 분기 ii)
  시간표 조회에는 쓰지 않는다. get_realtime_status 의 철도 지연 이력
  용도로만 제한 사용한다. 응답에는 반드시 기준일(data_date)을 실어
  호출부가 "실시간"이 아니라 "최신 실적일 기준"임을 밝힐 수 있게 한다.

인증키는 unquote 해서 쓴다 (expbus_api.py 와 동일 이유). 포털이 주는
키는 Encoding 형태(%2F 등)라 httpx params 가 다시 인코딩하면 이중
인코딩(%252F)이 되어 403 이 난다.

키 없음/조회 실패 시 예외를 던지지 않고 None 을 돌려준다. 호출부
(tools.py)는 None 을 "실데이터 없음"으로 해석해 목 데이터로 폴백한다.

사용법
  python korail_api.py --stations                       역 코드 캐시 생성
  python korail_api.py --from 서울 --to 부산 --date 20260830
  python korail_api.py --delays                         최신 실적일 지연 이력
  python korail_api.py --delays --station 서울
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx

BASE = "https://apis.data.go.kr/B551457/run/v2"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))

STATIONS_PATH = Path(__file__).with_name("rail_stations.json")

CACHE_TTL_SEC = 600          # 시간표·실적은 자주 바뀌지 않는다
_cache: dict[str, tuple[float, object]] = {}

# 이 API 가 보유한 최신 데이터를 찾을 때 오늘부터 거슬러 올라가는 최대 일수.
# 실측상 최신일은 어제였지만, 적재가 며칠 밀릴 수 있으므로 여유를 둔다.
MAX_LOOKBACK_DAYS = 14


# ─────────────────────────────────────────────────────────
def _items(body: dict) -> list[dict]:
    """items.item 을 항상 리스트로 정규화한다."""
    items = (body.get("items") or {}).get("item") or []
    if isinstance(items, dict):      # 1건이면 배열이 아니라 객체로 오는 공공데이터포털 특유의 문제
        return [items]
    return items if isinstance(items, list) else []


def _get(endpoint: str, cond: dict[str, str], rows: int = 100,
         page: int = 1) -> tuple[int, list[dict]] | None:
    """cond 조회. (totalCount, items) 또는 실패 시 None.

    cond 는 {"run_ymd::EQ": "20260830"} 형태로 받아 cond[...] 로 감싼다."""
    if not KEY:
        return None
    params = {"serviceKey": KEY, "pageNo": page,
              "numOfRows": rows, "returnType": "JSON"}
    params.update({f"cond[{k}]": v for k, v in cond.items()})

    ck = f"{endpoint}|{sorted(cond.items())}|{rows}|{page}"
    hit = _cache.get(ck)
    if hit is not None and time.time() - hit[0] < CACHE_TTL_SEC:
        return hit[1]                                    # type: ignore[return-value]

    try:
        r = httpx.get(f"{BASE}/{endpoint}", params=params, timeout=20.0)
        r.raise_for_status()
        body = r.json()["response"]["body"]
        out = (int(body.get("totalCount") or 0), _items(body))
    except (httpx.HTTPError, KeyError, ValueError, TypeError,
            json.JSONDecodeError):
        return None

    _cache[ck] = (time.time(), out)
    return out


def _fmt(dt: str | None) -> str | None:
    """'2026-08-30 05:13:00.0' → '2026-08-30 05:13'"""
    return dt[:16] if dt else None


def _parse(dt: str | None) -> datetime | None:
    if not dt:
        return None
    try:
        return datetime.strptime(dt[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _minutes(dep: str | None, arr: str | None) -> int | None:
    """소요시간(분). 도착이 출발보다 이르면 자정을 넘긴 것으로 본다."""
    d, a = _parse(dep), _parse(arr)
    if not d or not a:
        return None
    if a < d:                       # 자정 넘김
        a += timedelta(days=1)
    return int((a - d).total_seconds() // 60)


def _delta_min(planned: str | None, actual: str | None) -> int | None:
    """계획 대비 실제의 부호 있는 차이(분). 양수가 지연, 음수가 조기 출발.

    _minutes 를 쓰면 안 된다 — 1분 조기 출발을 자정 넘김으로 오인해
    1439 분 지연으로 만든다(실측에서 실제로 발생). 지연은 길어야 몇
    시간이므로, 12시간을 넘는 차이만 날짜 경계로 보고 되돌린다."""
    p, a = _parse(planned), _parse(actual)
    if not p or not a:
        return None
    diff = int((a - p).total_seconds() // 60)
    if diff > 720:
        diff -= 1440
    elif diff < -720:
        diff += 1440
    return diff


# ─────────────────────────────────────────────────────────
def latest_data_date(today: str | None = None) -> str | None:
    """API 가 보유한 가장 최신 run_ymd 를 찾는다.

    미래 데이터가 없으므로 오늘부터 거슬러 올라가며 첫 히트를 쓴다.
    GTE 로 한 번에 판정하지 않는 이유는 GTE 가 '그 날 이후 전부'라
    어느 날짜인지는 알려주지 않기 때문이다."""
    base = datetime.strptime(today, "%Y%m%d") if today else datetime.now()
    for back in range(MAX_LOOKBACK_DAYS + 1):
        ymd = (base - timedelta(days=back)).strftime("%Y%m%d")
        got = _get("travelerTrainRunPlan2", {"run_ymd::EQ": ymd}, rows=1)
        if got is None:
            return None
        if got[0] > 0:
            return ymd
    return None


def _load_cache() -> dict:
    if not STATIONS_PATH.exists():
        return {}
    try:
        return json.loads(STATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_stations() -> dict[str, str]:
    """역명 → 역코드. 캐시 파일이 없으면 빈 dict."""
    return _load_cache().get("stations", {})


def load_lines() -> dict[str, str]:
    """주운행선명 → 노선코드 (mrnt_cd). 캐시 파일이 없으면 빈 dict."""
    return _load_cache().get("lines", {})


def _fetch_codes(code_type: str) -> dict[str, str]:
    """codes2 의 한 type 을 전부 받아 {코드명: 코드값} 으로 만든다.
    codes2 는 cond 없이 호출하면 0건이므로 type::EQ 를 반드시 준다."""
    out: dict[str, str] = {}
    page = 1
    while page <= 20:
        got = _get("codes2", {"type::EQ": code_type}, rows=1000, page=page)
        if got is None:
            raise SystemExit(f"codes2 조회 실패 (type={code_type})")
        total, items = got
        if not items:
            break
        for it in items:
            name, code = it.get("value"), it.get("code")
            # '*'(기타) 같은 특수 코드는 실제 역·노선이 아니다.
            if name and code and code != "*":
                out.setdefault(name, code)
        if page * 1000 >= total:
            break
        page += 1
    return out


def sync_stations() -> tuple[int, int]:
    """codes2 에서 역 코드와 주운행선 코드를 받아 rail_stations.json 에 캐시한다.

    노선까지 받는 이유: "지금 경부선 지연돼?" 처럼 역이 아니라 노선을
    가리키는 질의를 실데이터로 답하려면 노선명 목록이 필요하다."""
    if not KEY:
        raise SystemExit("DATA_GO_KR_KEY_ENC 환경변수가 필요합니다 (.env 참조)")

    stations = {n: c for n, c in _fetch_codes("stn_cd").items() if c.isdigit()}
    lines = _fetch_codes("mrnt_cd")

    STATIONS_PATH.write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "count": len(stations), "line_count": len(lines),
         "stations": stations, "lines": lines},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return len(stations), len(lines)


# ─────────────────────────────────────────────────────────
def search_schedule(origin: str, destination: str, run_ymd: str,
                    after_hhmm: str | None = None, limit: int = 5) -> dict | None:
    """구간 운행실적 조회 (직통만).

    ⚠ 이 API 에는 미래 데이터가 없다. 반환값은 시간표가 아니라 run_ymd
    당일의 실적이다. 시간표 안내(search_rail)에 쓰면 안 된다 — 이 함수는
    지연 이력 계산과 CLI 검증용이다.

    운임·열차종별은 API 가 주지 않으므로 필드를 넣지 않는다."""
    cond = {"run_ymd::EQ": run_ymd,
            "dptre_stn_nm::EQ": origin,
            "arvl_stn_nm::EQ": destination}
    got = _get("travelerTrainRunPlan2", cond, rows=200)
    if got is None:
        return None

    _, items = got
    rows = sorted(items, key=lambda r: r.get("trn_plan_dptre_dt") or "")
    if after_hhmm:
        rows = [r for r in rows
                if (_fmt(r.get("trn_plan_dptre_dt")) or "")[11:] >= after_hhmm]

    if not rows:
        # 그 날짜에 데이터 자체가 없는 것과 그 구간에 직통이 없는 것은 다르다.
        # 미래 날짜는 항상 전자다(이 API 에 미래 데이터가 없다) — 구별하지
        # 않으면 "그 날 운행 없음"으로 잘못 읽힌다.
        day = _get("travelerTrainRunPlan2", {"run_ymd::EQ": run_ymd}, rows=1)
        if day is not None and day[0] == 0:
            return {"found": False, "reason": "date_not_available",
                    "origin": origin, "destination": destination, "date": run_ymd,
                    "available_until": latest_data_date(),
                    "note": "이 API 는 과거 운행실적만 제공하며 미래 시간표가 없습니다"}
        return {"found": False, "reason": "no_direct_service",
                "origin": origin, "destination": destination, "date": run_ymd}

    trains = [{
        "train_no": r.get("trn_no"),
        "departure": _fmt(r.get("trn_plan_dptre_dt")),
        "arrival": _fmt(r.get("trn_plan_arvl_dt")),
        "duration_min": _minutes(r.get("trn_plan_dptre_dt"), r.get("trn_plan_arvl_dt")),
    } for r in rows[:limit]]

    return {"found": True, "origin": origin, "destination": destination,
            "date": run_ymd, "trains": trains}


def delay_history(run_ymd: str | None = None, station: str | None = None,
                  line: str | None = None, limit: int = 5) -> dict | None:
    """계획 대비 실제 출발시각 차이로 지연을 산출한다.

    RunPlan2(계획)와 RunInfo2(실제)를 열차번호로 조인한다. RunInfo2 의
    시발역(stop_se_cd=01) 레코드를 실제 출발로 본다.

    station 을 주면 그 역을 시발로 하는 열차만, line(주운행선명, 예 "경부선")
    을 주면 그 노선 열차만 본다. 노선은 RunInfo2 에만 있는 필드라
    RunPlan2 쪽에는 걸 수 없다 — 조인한 뒤 거른다.

    지연의 중앙값은 1분이다(실측). 대부분은 사실상 정시이므로 호출부가
    "지연 n건"을 그대로 내보내면 과장이 된다.
    반환에 data_date 를 실어 호출부가 기준일을 밝힐 수 있게 한다."""
    ymd = run_ymd or latest_data_date()
    if not ymd:
        return None

    pcond = {"run_ymd::EQ": ymd}
    if station:
        pcond["dptre_stn_nm::EQ"] = station
    plans = _get("travelerTrainRunPlan2", pcond, rows=1000)
    if plans is None:
        return None
    plan_by_no = {p.get("trn_no"): p for p in plans[1] if p.get("trn_no")}
    if not plan_by_no:
        return {"found": False, "reason": "no_data", "data_date": ymd}

    icond = {"run_ymd::EQ": ymd, "stop_se_cd::EQ": "01"}     # 시발
    if station:
        icond["stn_nm::EQ"] = station
    if line:
        icond["mrnt_nm::EQ"] = line
    infos = _get("travelerTrainRunInfo2", icond, rows=1000)
    if infos is None:
        return None

    delayed, ontime = [], 0
    for r in infos[1]:
        plan = plan_by_no.get(r.get("trn_no"))
        if not plan:
            continue
        diff = _delta_min(plan.get("trn_plan_dptre_dt"), r.get("trn_dptre_dt"))
        if diff is None:
            continue
        if diff <= 0:
            ontime += 1
            continue
        delayed.append({
            "train_no": r.get("trn_no"),
            "origin": plan.get("dptre_stn_nm"),
            "destination": plan.get("arvl_stn_nm"),
            "scheduled": _fmt(plan.get("trn_plan_dptre_dt")),
            "actual": _fmt(r.get("trn_dptre_dt")),
            "delay_min": diff,
            **({"line": r["mrnt_nm"]} if r.get("mrnt_nm") else {}),
        })

    if not delayed and not ontime:
        return {"found": False, "reason": "no_data", "data_date": ymd}

    delayed.sort(key=lambda x: -x["delay_min"])
    return {"found": True, "data_date": ymd,
            "compared": len(delayed) + ontime,
            "delayed_count": len(delayed),
            "on_time_count": ontime,
            "trains": delayed[:limit],
            **({"station": station} if station else {}),
            **({"line": line} if line else {})}



def get_station_history(station: str, line: str | None = None,
                        run_ymd: str | None = None, limit: int = 10) -> dict | None:
    """역별 운행 이력 (RunInfo2). 그 역을 지나간 열차의 실제 시각이다.

    RunPlan2 와 구조가 다르다. RunPlan2 는 열차 1건 단위라 출발·도착역
    필터로 구간 조회가 되지만, RunInfo2 는 정차역 1건 단위이고 역·노선
    필터만 있어 구간 조회가 안 된다. 그래서 이 함수는 "이 역의 이력"만
    답한다.

    delay_min 은 그 역이 **시발역인 열차에만** 채운다. RunPlan2 가 주는
    계획 시각은 시발 출발과 종착 도착 두 개뿐이라, 중간 정차역의 계획
    시각은 어디에도 없어 지연을 계산할 수 없기 때문이다. 없는 값을
    추정해 채우지 않는다.

    행선지(origin/destination)는 RunInfo2 에 없어 RunPlan2 를 열차번호로
    조인해 붙인다(실측 매칭률 100%)."""
    ymd = run_ymd or latest_data_date()
    if not ymd:
        return None

    icond = {"run_ymd::EQ": ymd, "stn_nm::EQ": station}
    if line:
        icond["mrnt_nm::EQ"] = line
    infos = _get("travelerTrainRunInfo2", icond, rows=1000)
    if infos is None:
        return None
    if not infos[1]:
        return {"found": False, "reason": "no_data", "station": station,
                "data_date": ymd, **({"line": line} if line else {})}

    plans = _get("travelerTrainRunPlan2", {"run_ymd::EQ": ymd}, rows=1000)
    plan_by_no = {p.get("trn_no"): p for p in (plans[1] if plans else [])}

    rows = []
    for r in sorted(infos[1], key=lambda x: x.get("trn_dptre_dt")
                    or x.get("trn_arvl_dt") or ""):
        plan = plan_by_no.get(r.get("trn_no"))
        row = {"train_no": r.get("trn_no")}
        if plan:
            row["origin"] = plan.get("dptre_stn_nm")
            row["destination"] = plan.get("arvl_stn_nm")
        if r.get("trn_arvl_dt"):
            row["arrival"] = _fmt(r["trn_arvl_dt"])
        if r.get("trn_dptre_dt"):
            row["departure"] = _fmt(r["trn_dptre_dt"])
        if r.get("mrnt_nm"):
            row["line"] = r["mrnt_nm"]
        if r.get("stop_se_nm"):
            row["stop_type"] = r["stop_se_nm"]
        # 시발역일 때만 계획과 대조할 수 있다.
        if r.get("stop_se_cd") == "01" and plan:
            d = _delta_min(plan.get("trn_plan_dptre_dt"), r.get("trn_dptre_dt"))
            if d is not None:
                row["delay_min"] = d
        rows.append(row)

    return {"found": True, "station": station, "data_date": ymd,
            "total": len(rows), "trains": rows[:limit],
            **({"line": line} if line else {})}


# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stations", action="store_true", help="역 코드 캐시 생성")
    ap.add_argument("--from", dest="origin")
    ap.add_argument("--to", dest="destination")
    ap.add_argument("--date")
    ap.add_argument("--after", help="HH:MM 이후 출발")
    ap.add_argument("--delays", action="store_true", help="지연 이력")
    ap.add_argument("--station", help="역별 운행 이력 (--delays 와 함께 쓰면 시발역 필터)")
    ap.add_argument("--line", help="주운행선명 (예 경부선)")
    ap.add_argument("--latest", action="store_true", help="보유 최신 데이터 날짜")
    a = ap.parse_args()

    def out(x):
        print(json.dumps(x, ensure_ascii=False, indent=2))

    if a.stations:
        ns, nl = sync_stations()
        print(f"역 코드 {ns}개 / 노선 {nl}개 → {STATIONS_PATH}", file=sys.stderr)
    elif a.latest:
        print(latest_data_date() or "(없음)")
    elif a.delays:
        out(delay_history(a.date, a.station, a.line))
    elif a.station:
        out(get_station_history(a.station, a.line, a.date))
    elif a.origin and a.destination:
        ymd = a.date or latest_data_date()
        if not ymd:
            raise SystemExit("보유 데이터 날짜를 찾지 못했습니다")
        out(search_schedule(a.origin, a.destination, ymd, a.after))
    else:
        ap.print_help()
