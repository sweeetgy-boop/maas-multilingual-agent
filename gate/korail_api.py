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
    미래 시간표로 그대로 제시하면 안 된다.
  - **열차 다이어는 요일 단위로 반복된다(실측).** 서울→부산 직통 편수는
    월 08-24/08-17/08-10/08-03 이 60/62/63/63 편으로 안정적인 반면,
    토 08-29 는 72편, 일 08-30 은 71편이다. 평일과 주말 다이어가 실제로
    다르므로 "가장 최근 날짜"가 아니라 **같은 요일의 최근 날짜**를 써야
    한다. 5개 구간(서울-부산/대전/동대구, 용산-목포, 청량리-안동) 모두
    4주 전까지 같은 요일 데이터가 빠짐없이 있었다.
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

  - **구간 조회는 RunInfo2(정차역 단위)로 한다.** RunPlan2 의
    dptre_stn_nm/arvl_stn_nm 은 열차의 시종착역이라 중간 정차역 쌍이
    잡히지 않는다: 서울→광명·천안아산·오송, 청량리→원주가 전부 0건인데
    실제로는 수십 편이 선다. RunInfo2 는 정차 1건 단위에 순번(trn_run_sn)
    이 있어 두 역을 trn_no 로 조인하고 순번을 비교하면 구간이 나온다.
      서울→광명  RunPlan2 0편 → RunInfo2 86편 (실측)
    실측으로 확인한 조인 안전성:
      · 한 역에서 같은 trn_no 가 두 번 나오는 경우 0건(서울 333·광명 227)
        → trn_no 단독 키로 조인해도 안전하다.
      · sn 비교로 판정한 방향이 uppln_dn_se_cd(D/U)와 175편 전부 일치
        → 상하행 코드를 따로 보지 않아도 방향이 갈린다.
      · trn_run_sn 은 **문자열**("2")로 온다. int 로 바꿔 비교해야 한다.
      · 시발역은 trn_arvl_dt 가, 종착역은 trn_dptre_dt 가 null 이다.
    역별 하루 정차는 40~333건이라 1페이지(1000건)로 끝난다 — 두 역이면
    호출 2회다. 하루 전체는 9,128건이라 통째로 받는 방식(10회)도 가능하나
    첫 조회가 3.9초로 느려 역별 2회 조회를 택했다(캐시 적중 시 0ms).

용도
  1. search_schedule — **요일 매칭 참고 시간표**. target_date 와 같은
     요일의 가장 최근 보유일을 기준일로 삼아, 그 날 운행 기록의 시각을
     target_date 로 옮겨 보여준다. 지어낸 목 데이터("KTX 101, 13:00,
     59,800원" — 존재하지 않는 편명)보다 실제 운행 기록이 낫다는 판단이다.
     반환에는 reference_date/reference_note/is_reference 를 반드시 실어
     호출부와 Supervisor 가 "확정 시간표가 아니라 과거 운행 기록"임을
     밝히게 한다. 확정 시간표로 제시하면 안 된다.
  2. delay_history / get_station_history — 지연 이력. 최신 실적일 기준.

  요금과 열차종별(KTX/무궁화)은 API 가 주지 않는다. 두 필드 모두 채우지
  않는다 — 지어내면 근거성 검증 계층이 차단한다.

인증키는 unquote 해서 쓴다 (expbus_api.py 와 동일 이유). 포털이 주는
키는 Encoding 형태(%2F 등)라 httpx params 가 다시 인코딩하면 이중
인코딩(%252F)이 되어 403 이 난다.

키 없음/조회 실패 시 예외를 던지지 않고 None 을 돌려준다. 호출부
(tools.py)는 None 을 "실데이터 없음"으로 해석해 목 데이터로 폴백한다.

사용법
  python korail_api.py --stations                       역 코드 캐시 생성
  python korail_api.py --from 서울 --to 부산 --date 20260901
  python korail_api.py --delays                         최신 실적일 지연 이력
  python korail_api.py --delays --station 서울
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx

BASE = "https://apis.data.go.kr/B551457/run/v2"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))

STATIONS_PATH = Path(__file__).with_name("rail_stations.json")

CACHE_TTL_SEC = 3600         # 과거 실적은 갱신되지 않는다. 1시간이면 충분하다
_cache: dict[str, tuple[float, object]] = {}

# 이 API 가 보유한 최신 데이터를 찾을 때 오늘부터 거슬러 올라가는 최대 일수.
# 실측상 최신일은 어제였지만, 적재가 며칠 밀릴 수 있으므로 여유를 둔다.
MAX_LOOKBACK_DAYS = 14

# 요일 매칭에서 거슬러 올라가는 최대 주 수. 보유 범위가 약 3개월(92일)
# 이므로 4주는 항상 범위 안이다.
MAX_REFERENCE_WEEKS = 4

_WEEKDAY_KO = "월화수목금토일"


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


_has_data_cache: dict[str, bool] = {}


def _has_data(ymd: str) -> bool | None:
    """그 날짜에 운행 데이터가 있는가. None = 조회 실패.

    과거 실적은 한 번 확정되면 바뀌지 않으므로 프로세스 수명 동안 캐시한다
    (같은 요일 조회가 반복되면 같은 날짜를 계속 되묻게 된다).

    시간표 경로가 RunInfo2 로 통일됐으므로 판정도 RunInfo2 로 한다. 두
    오퍼레이션의 보유 날짜는 동일하다(8개 표본에서 전부 일치, 실측)."""
    if ymd in _has_data_cache:
        return _has_data_cache[ymd]
    got = _get("travelerTrainRunInfo2", {"run_ymd::EQ": ymd}, rows=1)
    if got is None:
        return None
    _has_data_cache[ymd] = got[0] > 0
    return _has_data_cache[ymd]


def find_reference_date(target_date: date) -> date | None:
    """target_date 와 같은 요일의 가장 최근 데이터 보유일.

    열차 다이어는 요일 단위로 반복되므로(평일/주말이 다르다), "가장 최근
    날짜"가 아니라 같은 요일을 찾아야 한다. 7일 전 → 14 → 21 → 28일 전
    순으로 시도하고 4주 안에 없으면 None.

    기준은 오늘이 아니라 **target_date** 다. 사용자가 "내일"을 물으면
    내일과 같은 요일을 찾아야 한다.

    target_date 자체에 데이터가 있으면(과거 날짜를 물은 경우) 그 날을
    그대로 쓴다 — 같은 요일의 다른 날로 대체할 이유가 없다. 오늘·미래는
    항상 데이터가 없으므로 이 검사는 건너뛴다(불필요한 호출 방지)."""
    if target_date < date.today():
        if _has_data(target_date.strftime("%Y%m%d")):
            return target_date

    for weeks in range(1, MAX_REFERENCE_WEEKS + 1):
        cand = target_date - timedelta(days=7 * weeks)
        has = _has_data(cand.strftime("%Y%m%d"))
        if has is None:
            return None
        if has:
            return cand
    return None


def _reference_note(ref: date) -> str:
    return f"{ref.isoformat()}({_WEEKDAY_KO[ref.weekday()]}) 운행 기록 기준"


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
def _shift(dt: str | None, delta: timedelta) -> str | None:
    """'2026-08-24 05:13:00.0' 을 delta 만큼 옮겨 '2026-08-31 05:13' 로.

    도착 시각도 같은 delta 로 옮긴다. 자정을 넘는 열차는 도착 날짜가
    출발보다 하루 뒤인데, 같은 delta 를 더하면 그 관계가 그대로 보존된다."""
    parsed = _parse(dt)
    return (parsed + delta).strftime("%Y-%m-%d %H:%M") if parsed else None


def _station_stops(station: str, ymd: str) -> dict[str, dict] | None:
    """그 날 그 역의 정차 기록을 {trn_no: 레코드} 로. None = 조회 실패.

    한 역의 하루 정차는 40~333건이라(실측) 1페이지(1000건)로 충분하다.
    같은 역을 같은 날 두 번 지나는 열차는 없어(서울 333건·광명 227건에서
    중복 trn_no 0건, 실측) trn_no 단독 키가 안전하다."""
    got = _get("travelerTrainRunInfo2",
               {"run_ymd::GTE": ymd, "run_ymd::LTE": ymd,
                "stn_nm::EQ": station}, rows=1000)
    if got is None:
        return None
    return {r["trn_no"]: r for r in got[1] if r.get("trn_no")}


def search_via_stops(origin: str, destination: str, ref_ymd: str,
                     limit: int = 5) -> list[dict] | None:
    """정차역 단위(RunInfo2)로 구간 시간표를 만든다. None = 조회 실패.

    두 역의 정차 목록을 trn_no 로 조인하고, 출발역의 정차 순번(trn_run_sn)
    이 도착역보다 작은 열차만 남긴다. 이 비교 하나로 방향이 갈린다 —
    서울·광명 공통 175편을 sn 으로 나눈 결과가 uppln_dn_se_cd(상/하행)와
    175편 전부 일치했다(실측). 그래서 상하행 코드를 따로 보지 않는다.

    trn_run_sn 은 문자열("2")로 오므로 반드시 int 로 바꿔 비교한다.
    문자열 그대로 비교하면 "10" < "2" 가 되어 순서가 뒤집힌다."""
    o_stops = _station_stops(origin, ref_ymd)
    if o_stops is None:
        return None
    d_stops = _station_stops(destination, ref_ymd)
    if d_stops is None:
        return None

    rows = []
    for trn_no, o in o_stops.items():
        d = d_stops.get(trn_no)
        if d is None:
            continue
        try:
            if int(o["trn_run_sn"]) >= int(d["trn_run_sn"]):
                continue                      # 역방향이거나 같은 역
        except (KeyError, TypeError, ValueError):
            continue
        # 출발역이 그 열차의 종착이면 출발시각이, 도착역이 시발이면
        # 도착시각이 없다. 없는 값을 추정해 채우지 않고 건너뛴다.
        dep, arr = o.get("trn_dptre_dt"), d.get("trn_arvl_dt")
        if not dep or not arr:
            continue
        rows.append({"trn_no": trn_no, "dep": dep, "arr": arr})

    rows.sort(key=lambda r: r["dep"])
    return rows


def search_schedule(origin: str, destination: str, target_date: str | date,
                    after_hhmm: str | None = None, before_hhmm: str | None = None,
                    limit: int = 5) -> dict | None:
    """요일 매칭 참고 시간표.

    이 API 에는 미래 데이터가 없으므로, target_date 와 같은 요일의 가장
    최근 보유일을 찾아 그 날 운행 기록의 시각을 target_date 로 옮겨
    돌려준다. 반환값은 **확정 시간표가 아니라 과거 운행 기록**이며,
    is_reference/reference_date/reference_note 가 그 사실을 나른다.
    호출부는 이 셋을 반드시 사용자에게 전달해야 한다.

    구간 조회는 RunInfo2(정차역 단위)로 한다. RunPlan2 는 열차의
    출발역→종착역 한 쌍만 기록해서 중간 정차역이 잡히지 않았다 —
    서울→광명·천안아산·오송, 청량리→원주가 전부 0건이었다. 정차역 단위로
    바꾸면서 서울→광명이 0편에서 86편이 됐다(실측).

    시각은 전부 RunInfo2 의 **실제 운행 기록**이다. RunPlan2 의 계획
    시각과 몇 분 차이가 나므로(00001 부산 도착: 계획 07:50 / 실제 07:54)
    섞지 않고 한쪽으로 통일했다.

    운임·열차종별은 API 가 주지 않으므로 필드를 넣지 않는다(지어내지 않는다).

    after_hhmm/before_hhmm 은 출발 시각대 필터("05:00" 형식, 반개구간
    [after, before)). 게이트가 뽑은 "오늘 오후" 같은 시간대를 호출부가
    범위로 옮겨 넘긴다. 필터는 limit 로 자르기 **전에** 걸어야 한다 —
    나중에 거르면 하루의 첫 5편만 받아 놓고 그중에서 고르게 된다.

    키 없음·조회 실패·기준일 없음·구간 0건이면 None (호출부가 목 데이터로
    폴백). 시간대 필터 때문에 0건이 된 경우도 None 이라, 호출부가 필터 없이
    다시 부를지 판단한다. 0건을 "운행 없음"으로 단정하지 않는다."""
    if not KEY:
        return None

    target = (datetime.strptime(target_date, "%Y%m%d").date()
              if isinstance(target_date, str) else target_date)

    ref = find_reference_date(target)
    if ref is None:
        return None

    rows = search_via_stops(origin, destination, ref.strftime("%Y%m%d"), limit)
    if rows is None:
        return None

    delta = target - ref
    trains = []
    for r in rows:
        dep = _shift(r["dep"], delta)
        if dep is None:
            continue
        if after_hhmm and dep[11:] < after_hhmm:
            continue
        if before_hhmm and dep[11:] >= before_hhmm:
            continue
        trains.append({
            "train_no": r["trn_no"],
            "departure": dep,
            # 도착에도 같은 delta 를 더한다. 자정을 넘는 열차는 도착 날짜가
            # 출발보다 하루 뒤인데, 같은 delta 면 그 관계가 보존된다.
            "arrival": _shift(r["arr"], delta),
            "duration_min": _minutes(r["dep"], r["arr"]),
        })

    if not trains:
        return None

    return {"found": True, "origin": origin, "destination": destination,
            "date": target.isoformat(),
            "reference_date": ref.isoformat(),
            "reference_note": _reference_note(ref),
            "is_reference": True,
            "lookup": "RunInfo2/stops",     # 어느 경로로 얻었는지 (디버깅용)
            "trains": trains[:limit]}


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
        # --date 는 조회할 날짜(오늘·내일 등)다. 어느 날 기록을 기준으로
        # 삼을지는 find_reference_date 가 요일을 맞춰 고른다.
        ymd = a.date or datetime.now().strftime("%Y%m%d")
        out(search_schedule(a.origin, a.destination, ymd, a.after))
    else:
        ap.print_help()
