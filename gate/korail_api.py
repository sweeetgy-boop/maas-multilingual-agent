#!/usr/bin/env python3
"""
한국철도공사 열차운행정보 API 어댑터

공공데이터포털 API 는 조회 파라미터 필터가 동작하지 않아
하루치 전체를 받아 SQLite 로 캐시한 뒤 로컬 조회한다.

  - 운행계획 약 6.9만 건 / 일  → 1,000건씩 70회 호출
  - 일일 트래픽 한도 10,000 건 대비 0.7% 사용
  - 조회 응답이 밀리초 단위로 빨라진다

제약 (2026-08 기준)
  - 이 API 에는 **운임 정보가 없다**. 요금은 안내하지 않는다.
  - 열차종별(KTX/무궁화 등) 정보도 없다. 열차번호만 제공된다.
  - 향후 코레일 정식 경로로 운임·종별 표를 확보하면 조인 로직만 추가하면 된다.

사용법
  python korail_api.py --sync                    # 오늘 데이터 수집
  python korail_api.py --sync --date 20260827    # 특정일 수집
  python korail_api.py --search 서울 부산          # 조회 테스트
  python korail_api.py --stations                # 역 목록 확인
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

BASE = "https://apis.data.go.kr/B551457/run/v2"
from urllib.parse import unquote
# 포털이 주는 키는 Encoding 형태(%2F 등)다. httpx params 가 다시 인코딩해
# 이중 인코딩(%252F)이 되므로 디코딩해서 넘긴다.
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))
DB = Path(os.environ.get("KORAIL_DB", Path(__file__).with_name("korail.db")))

PAGE_SIZE = 1000
MAX_PAGES = 120          # 안전 상한 (6.9만 건이면 70페이지)


# ─────────────────────────────────────────────────────────
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS run_plan (
        run_ymd       TEXT NOT NULL,
        trn_no        TEXT NOT NULL,
        dptre_stn_cd  TEXT,
        dptre_stn_nm  TEXT,
        arvl_stn_cd   TEXT,
        arvl_stn_nm   TEXT,
        plan_dptre_dt TEXT,
        plan_arvl_dt  TEXT,
        PRIMARY KEY (run_ymd, trn_no)
    );
    CREATE INDEX IF NOT EXISTS idx_plan_od
        ON run_plan(run_ymd, dptre_stn_nm, arvl_stn_nm);

    CREATE TABLE IF NOT EXISTS run_stop (
        run_ymd     TEXT NOT NULL,
        trn_no      TEXT NOT NULL,
        trn_run_sn  INTEGER NOT NULL,
        stn_cd      TEXT,
        stn_nm      TEXT,
        mrnt_nm     TEXT,
        stop_se_nm  TEXT,
        arvl_dt     TEXT,
        dptre_dt    TEXT,
        updn_cd     TEXT,
        PRIMARY KEY (run_ymd, trn_no, trn_run_sn)
    );
    CREATE INDEX IF NOT EXISTS idx_stop_stn
        ON run_stop(run_ymd, stn_nm);

    CREATE TABLE IF NOT EXISTS sync_log (
        run_ymd    TEXT PRIMARY KEY,
        endpoint   TEXT,
        rows       INTEGER,
        synced_at  TEXT
    );
    """)
    conn.commit()


def fetch_pages(client: httpx.Client, endpoint: str, run_ymd: str) -> list[dict]:
    """페이징으로 전체를 받아온다. API 가 날짜 필터를 무시하므로 응답에서 직접 거른다."""
    rows, page = [], 1
    while page <= MAX_PAGES:
        r = client.get(f"{BASE}/{endpoint}", params={
            "serviceKey": KEY, "pageNo": page,
            "numOfRows": PAGE_SIZE, "_type": "json"}, timeout=30.0)
        r.raise_for_status()
        body = r.json()["response"]["body"]
        items = body.get("items", {}).get("item") or []
        if not items:
            break
        rows.extend(items)
        total = body.get("totalCount", 0)
        print(f"\r  {endpoint}: {len(rows)}/{total}", end="", file=sys.stderr, flush=True)
        if len(rows) >= total:
            break
        page += 1
        time.sleep(0.1)          # 과도한 호출 방지
    print(file=sys.stderr)
    return rows


def sync(run_ymd: str) -> None:
    if not KEY:
        raise SystemExit("DATA_GO_KR_KEY_ENC 환경변수가 필요합니다 (.env 참조)")

    conn = sqlite3.connect(DB)
    init_db(conn)

    with httpx.Client() as client:
        # ── 운행계획 ──
        plans = fetch_pages(client, "travelerTrainRunPlan2", run_ymd)
        kept = [p for p in plans if p.get("run_ymd") == run_ymd]
        print(f"  운행계획: 수신 {len(plans)} / {run_ymd} 해당 {len(kept)}", file=sys.stderr)

        conn.executemany(
            "INSERT OR REPLACE INTO run_plan VALUES (?,?,?,?,?,?,?,?)",
            [(p["run_ymd"], p["trn_no"].lstrip("0") or "0",
              p.get("dptre_stn_cd"), p.get("dptre_stn_nm"),
              p.get("arvl_stn_cd"), p.get("arvl_stn_nm"),
              p.get("trn_plan_dptre_dt"), p.get("trn_plan_arvl_dt")) for p in kept])

        # ── 정차역 (연계 경로·경유역 확인용) ──
        stops = fetch_pages(client, "travelerTrainRunInfo2", run_ymd)
        kept_s = [s for s in stops if s.get("run_ymd") == run_ymd]
        print(f"  정차정보: 수신 {len(stops)} / {run_ymd} 해당 {len(kept_s)}", file=sys.stderr)

        conn.executemany(
            "INSERT OR REPLACE INTO run_stop VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(s["run_ymd"], s["trn_no"].lstrip("0") or "0", int(s.get("trn_run_sn") or 0),
              s.get("stn_cd"), s.get("stn_nm"), s.get("mrnt_nm"), s.get("stop_se_nm"),
              s.get("trn_arvl_dt"), s.get("trn_dptre_dt"), s.get("uppln_dn_se_cd"))
             for s in kept_s])

    conn.execute("INSERT OR REPLACE INTO sync_log VALUES (?,?,?,?)",
                 (run_ymd, "both", len(kept) + len(kept_s),
                  datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    print(f"동기화 완료: {run_ymd}", file=sys.stderr)


# ─────────────────────────────────────────────────────────
def _fmt(dt: str | None) -> str | None:
    """'2026-08-26 05:13:00.0' → '2026-08-26 05:13'"""
    return dt[:16] if dt else None


def _minutes(dep: str | None, arr: str | None) -> int | None:
    if not dep or not arr:
        return None
    f = "%Y-%m-%d %H:%M:%S.%f"
    try:
        d, a = datetime.strptime(dep, f), datetime.strptime(arr, f)
        if a < d:                       # 자정 넘김
            a += timedelta(days=1)
        return int((a - d).total_seconds() // 60)
    except ValueError:
        return None


def search_schedule(origin: str, destination: str, run_ymd: str,
                    after_hhmm: str | None = None, limit: int = 5) -> dict:
    """
    구간 시간표 조회. 직통 열차만 반환한다.

    운임 정보는 이 API 에 없으므로 반환하지 않는다.
    도구 응답에 없는 값은 Supervisor 가 언급하지 않도록 설계돼 있다.
    """
    if not DB.exists():
        return {"found": False, "reason": "cache_not_synced",
                "hint": "python korail_api.py --sync 를 먼저 실행하세요"}

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    sql = """
      SELECT trn_no, dptre_stn_nm, arvl_stn_nm, plan_dptre_dt, plan_arvl_dt
        FROM run_plan
       WHERE run_ymd = ? AND dptre_stn_nm = ? AND arvl_stn_nm = ?
    """
    params = [run_ymd, origin, destination]
    if after_hhmm:
        sql += " AND substr(plan_dptre_dt, 12, 5) >= ?"
        params.append(after_hhmm)
    sql += " ORDER BY plan_dptre_dt LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        return {"found": False, "reason": "no_direct_service",
                "origin": origin, "destination": destination, "date": run_ymd}

    trains = []
    for r in rows:
        trains.append({
            "train_no": r["trn_no"],
            "departure": _fmt(r["plan_dptre_dt"]),
            "arrival": _fmt(r["plan_arvl_dt"]),
            "duration_min": _minutes(r["plan_dptre_dt"], r["plan_arvl_dt"]),
        })

    return {
        "found": True,
        "origin": origin, "destination": destination, "date": run_ymd,
        "trains": trains,
        "fare_note": "이 데이터에는 운임 정보가 포함되어 있지 않습니다. "
                     "요금과 좌석 예매는 코레일 공식 홈페이지에서 확인하세요.",
        "data_source": "한국철도공사 열차운행정보 (공공데이터포털)",
        "retrieved_at": datetime.now().isoformat(timespec="seconds"),
        "disclaimer": "운행정보는 참고용이며, 최종 확인은 운영기관 공식 채널을 이용하세요.",
    }


def list_stations(run_ymd: str | None = None, limit: int = 100) -> list[str]:
    if not DB.exists():
        return []
    conn = sqlite3.connect(DB)
    if run_ymd:
        rows = conn.execute(
            "SELECT DISTINCT dptre_stn_nm FROM run_plan WHERE run_ymd=? "
            "UNION SELECT DISTINCT arvl_stn_nm FROM run_plan WHERE run_ymd=? "
            "ORDER BY 1 LIMIT ?", (run_ymd, run_ymd, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT dptre_stn_nm FROM run_plan "
            "UNION SELECT DISTINCT arvl_stn_nm FROM run_plan "
            "ORDER BY 1 LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]


# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--search", nargs=2, metavar=("ORIGIN", "DEST"))
    ap.add_argument("--after", help="HH:MM 이후 출발")
    ap.add_argument("--stations", action="store_true")
    a = ap.parse_args()

    if a.sync:
        sync(a.date)
    elif a.stations:
        st = list_stations(a.date)
        print(f"역 {len(st)}개")
        for i in range(0, len(st), 8):
            print("  " + "  ".join(f"{s:8}" for s in st[i:i + 8]))
    elif a.search:
        import json
        r = search_schedule(a.search[0], a.search[1], a.date, a.after)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        ap.print_help()
