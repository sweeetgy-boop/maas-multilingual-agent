#!/usr/bin/env python3
"""
게이트 모델 평가 하네스

D1(제로샷) / D3(LoRA) / CPU 인코더 세 번을 같은 스크립트로 돌려
동일 기준으로 비교한다.

사용법
  # vLLM 제로샷 (D1)
  python score.py --endpoint http://localhost:8000/v1 --model transit-base --tag zeroshot

  # vLLM + LoRA (D3)
  python score.py --endpoint http://localhost:8000/v1 --model gate --tag lora-v1

  # 결과 비교
  python score.py --compare results/zeroshot.json results/lora-v1.json results/cpu-encoder.json

핵심 지표
  FPR(오탐률)  정상 문의를 in_domain=false 로 잘못 막은 비율.  ← 1순위
  FNR(미탐률)  차단해야 할 걸 통과시킨 비율
  Slot F1      출발/도착/일시/인원 추출 정확도
  Route Agree  기대 라우팅(local/cloud/blocked) 일치율 → α 산정 근거
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import httpx

GATE_SYSTEM = """You are a request classifier for a multilingual public transit assistant.
The assistant handles ONLY: flights, rail, intercity/city buses, lodging, bike-share,
car-share, station/onboard facilities and rules, and multimodal journey planning.

Classify the user message. Respond with JSON only. No explanation, no markdown.
- in_domain=false for tourism itineraries, restaurant/food recommendations, weather,
  currency exchange, immigration rules, politics, medical/legal/financial advice,
  coding, general trivia, or creative writing.
- in_domain=true for questions about riding, boarding, fares, facilities, or rules
  of a transport service, even if not a schedule lookup.
- in_domain=false for a place's crowd level or ongoing local events asked on their own
  (지금 붐비나요, what events are happening) — not a transit-service question.
- toxicity: 0.0 (clean) to 1.0 (severe abuse). Complaints about delays are NOT toxic.
  Detect obfuscation (separated jamo, inserted spaces, asterisk masking, romanization).
- pii_spans: copy the exact substring found in the message.
- origin/destination/datetime: copy the phrase from the message, or "" if absent.
- pax: number of travellers, 0 if not stated.
- pii: comma-separated from PASSPORT,PHONE,EMAIL,CARD,NAME,ADDRESS. Empty string if none.
- search_parking = parking for the user's OWN car (주차장, parking, parkir). NOT share_mobility.
- search_ev_charger = electric vehicle charging stations (충전소, EV charger, pengisian).
  NOT share_mobility.
- share_mobility = shared/rented bikes, scooters, cars (따릉이, 카셰어링, bike share)."""

SCHEMA = {
    "type": "object",
    "required": ["lang", "in_domain", "toxicity", "intent",
                 "origin", "destination", "datetime", "pax", "pii", "confidence"],
    "properties": {
        "lang": {"type": "string", "enum": ["ko", "en", "zh", "ja", "id", "other"]},
        "in_domain": {"type": "boolean"},
        "toxicity": {"type": "number"},
        "intent": {"type": "string",
                   "enum": ["search_rail", "search_bus", "search_flight", "search_lodging",
                            "share_mobility", "plan_journey", "get_realtime_status",
                            "fare_policy", "search_parking", "search_ev_charger",
                            "smalltalk", "other"]},
        "origin": {"type": "string"},
        "destination": {"type": "string"},
        "datetime": {"type": "string"},
        "pax": {"type": "integer"},
        "pii": {"type": "string",
                "description": "comma-separated types found, empty if none"},
        "confidence": {"type": "number"},
    },
}


def unflatten(p: dict) -> dict:
    """평탄 응답을 기존 채점 로직이 기대하는 중첩 형태로 되돌린다."""
    if not p:
        return p
    def nn(v):
        return None if v in ("", "null", "none", "N/A", None) else v
    pii = [t.strip().upper() for t in (p.get("pii") or "").split(",") if t.strip()]
    return {
        "lang": p.get("lang"),
        "in_domain": p.get("in_domain"),
        "toxicity": p.get("toxicity", 0),
        "intent": p.get("intent"),
        "confidence": p.get("confidence", 0),
        "pii_spans": [{"type": t, "text": ""} for t in pii],
        "slots": {"origin": nn(p.get("origin")), "destination": nn(p.get("destination")),
                  "datetime": nn(p.get("datetime")), "pax": p.get("pax") or None},
    }


SIMPLE_INTENTS = {"search_rail", "search_bus", "search_flight", "get_realtime_status"}
CONF_THRESHOLD = 0.85
TOXICITY_THRESHOLD = 0.6


# ─────────────────────────────────────────────────────────
def call_model(client: httpx.Client, endpoint: str, model: str, text: str) -> tuple[dict, float]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": GATE_SYSTEM},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
        "max_tokens": 384,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "gate", "schema": SCHEMA, "strict": True},
        },
    }
    t0 = time.perf_counter()
    r = client.post(f"{endpoint}/chat/completions", json=payload, timeout=60.0)
    dt = time.perf_counter() - t0
    r.raise_for_status()
    return unflatten(json.loads(r.json()["choices"][0]["message"]["content"])), dt


def decide_route(pred: dict) -> str:
    """router.py 와 동일한 라우팅 로직. 실제 서비스 동작을 재현한다."""
    if pred.get("toxicity", 0) >= TOXICITY_THRESHOLD:
        return "blocked"
    if not pred.get("in_domain", False):
        return "blocked"
    slots = pred.get("slots") or {}
    complete = bool(slots.get("origin") and slots.get("destination"))
    if (pred.get("confidence", 0) >= CONF_THRESHOLD
            and pred.get("intent") in SIMPLE_INTENTS
            and complete
            and not pred.get("pii_spans")):
        return "local"
    return "cloud"


def norm(s):
    return (s or "").strip().casefold()


# ─────────────────────────────────────────────────────────
def evaluate(items, endpoint, model, tag):
    results = []
    with httpx.Client() as client:
        for i, it in enumerate(items, 1):
            try:
                pred, dt = call_model(client, endpoint, model, it["text"])
                err = None
            except Exception as e:                      # 파싱 실패도 오답으로 계산
                pred, dt, err = {}, 0.0, str(e)[:120]
            results.append({"item": it, "pred": pred, "latency": dt, "error": err})
            print(f"\r  {i}/{len(items)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return score(results, tag)


def score(results, tag):
    by_lang = defaultdict(lambda: defaultdict(int))
    lat = []
    slot_tp = slot_fp = slot_fn = 0
    rows = []

    for r in results:
        it, pred = r["item"], r["pred"]
        lang, cat = it["lang"], it["category"]
        exp = it["expected"]
        L = by_lang[lang]
        A = by_lang["ALL"]
        for d in (L, A):
            d["n"] += 1
        if r["latency"]:
            lat.append(r["latency"])
        if r["error"] or not pred:
            for d in (L, A):
                d["parse_error"] += 1
            rows.append((it["id"], "PARSE_ERROR", r["error"]))
            continue

        # ── in_domain ──
        got_in, want_in = bool(pred.get("in_domain")), bool(exp["in_domain"])
        if got_in == want_in:
            for d in (L, A):
                d["domain_ok"] += 1
        else:
            rows.append((it["id"], "domain", f"want={want_in} got={got_in} [{cat}]"))
            # FPR: 정상인데 막음 / FNR: 막아야 하는데 통과
            key = "fp" if want_in and not got_in else "fn"
            for d in (L, A):
                d[key] += 1
        for d in (L, A):
            d["pos" if want_in else "neg"] += 1

        # ── toxicity ──
        got_tox = pred.get("toxicity", 0) >= TOXICITY_THRESHOLD
        if got_tox == bool(exp["toxic"]):
            for d in (L, A):
                d["tox_ok"] += 1
        else:
            rows.append((it["id"], "toxicity", f"want={exp['toxic']} got={pred.get('toxicity')}"))

        # ── intent ──
        if pred.get("intent") == exp["intent"]:
            for d in (L, A):
                d["intent_ok"] += 1

        # ── slots (부분 문자열 매칭으로 관대하게) ──
        ps, es = pred.get("slots") or {}, exp["slots"]
        for k in ("origin", "destination", "datetime", "pax"):
            p, e = ps.get(k), es.get(k)
            if e is None and p is None:
                continue
            if e is not None and p is not None:
                hit = (norm(str(e)) in norm(str(p))) or (norm(str(p)) in norm(str(e)))
                slot_tp += 1 if hit else 0
                slot_fp += 0 if hit else 1
                slot_fn += 0 if hit else 1
            elif e is not None:
                slot_fn += 1
            else:
                slot_fp += 1

        # ── PII ──
        got_pii = {s.get("type") for s in pred.get("pii_spans", [])}
        want_pii = set(exp["pii"])
        if want_pii:
            for d in (L, A):
                d["pii_total"] += len(want_pii)
                d["pii_found"] += len(want_pii & got_pii)

        # ── route ──
        if decide_route(pred) == it["route"]:
            for d in (L, A):
                d["route_ok"] += 1
        if decide_route(pred) == "cloud":
            for d in (L, A):
                d["escalated"] += 1

    prec = slot_tp / (slot_tp + slot_fp) if (slot_tp + slot_fp) else 0
    rec = slot_tp / (slot_tp + slot_fn) if (slot_tp + slot_fn) else 0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0

    return {
        "tag": tag,
        "by_lang": {k: dict(v) for k, v in by_lang.items()},
        "slot_f1": round(f1, 3),
        "latency_p50": round(statistics.median(lat), 3) if lat else None,
        "latency_p95": round(sorted(lat)[int(len(lat) * 0.95)], 3) if len(lat) > 1 else None,
        "errors": rows,
    }


def report(res):
    print(f"\n{'=' * 68}")
    print(f" 결과: {res['tag']}")
    print(f"{'=' * 68}")
    hdr = f"{'lang':6}{'n':>4}{'도메인정확':>10}{'FPR':>8}{'FNR':>8}{'유해도':>8}{'의도':>8}{'PII':>8}{'라우팅':>8}{'α':>8}"
    print(hdr)
    print("-" * len(hdr))
    for lang in ["ko", "en", "id", "ALL"]:
        d = res["by_lang"].get(lang)
        if not d:
            continue
        n = d.get("n", 0) or 1
        pos, neg = d.get("pos", 0) or 1, d.get("neg", 0) or 1
        pii_t = d.get("pii_total", 0) or 1
        print(f"{lang:6}{d.get('n',0):>4}"
              f"{d.get('domain_ok',0)/n:>9.1%}"
              f"{d.get('fp',0)/pos:>8.1%}"
              f"{d.get('fn',0)/neg:>8.1%}"
              f"{d.get('tox_ok',0)/n:>8.1%}"
              f"{d.get('intent_ok',0)/n:>8.1%}"
              f"{d.get('pii_found',0)/pii_t:>8.1%}"
              f"{d.get('route_ok',0)/n:>8.1%}"
              f"{d.get('escalated',0)/n:>8.1%}")
    print("-" * len(hdr))
    print(f"  Slot F1: {res['slot_f1']}   지연 P50: {res['latency_p50']}s   P95: {res['latency_p95']}s")
    pe = res["by_lang"].get("ALL", {}).get("parse_error", 0)
    if pe:
        print(f"  !! JSON 파싱 실패 {pe}건 — guided decoding 설정을 확인하세요")

    if res["errors"]:
        print(f"\n오답 {len(res['errors'])}건 (상위 20):")
        for eid, kind, detail in res["errors"][:20]:
            print(f"  {eid:9} {kind:10} {detail}")

    print("\n판단 기준 대조")
    a = res["by_lang"].get("ALL", {})
    idd = res["by_lang"].get("id", {})
    fpr = a.get("fp", 0) / (a.get("pos", 1) or 1)
    id_fpr = idd.get("fp", 0) / (idd.get("pos", 1) or 1)
    print(f"  전체 FPR      {fpr:.1%}")
    print(f"  인니어 FPR    {id_fpr:.1%}   (기준 < 5%)  {'PASS' if id_fpr < 0.05 else 'FAIL'}")
    p95 = res["latency_p95"]
    if p95:
        print(f"  P95 지연      {p95:.2f}s  (기준 < 0.6s)  {'PASS' if p95 < 0.6 else 'FAIL'}")


def compare(paths):
    runs = [json.loads(Path(p).read_text()) for p in paths]
    print(f"\n{'지표':<16}" + "".join(f"{r['tag']:>18}" for r in runs))
    print("-" * (16 + 18 * len(runs)))

    def row(label, fn):
        print(f"{label:<16}" + "".join(f"{fn(r):>18}" for r in runs))

    def fpr(r, lang="ALL"):
        d = r["by_lang"].get(lang, {})
        return f"{d.get('fp',0)/(d.get('pos',1) or 1):.1%}"

    row("FPR (전체)", lambda r: fpr(r))
    row("FPR (ko)", lambda r: fpr(r, "ko"))
    row("FPR (en)", lambda r: fpr(r, "en"))
    row("FPR (id)", lambda r: fpr(r, "id"))
    row("도메인 정확도", lambda r: f"{r['by_lang']['ALL'].get('domain_ok',0)/r['by_lang']['ALL']['n']:.1%}")
    row("Slot F1", lambda r: str(r["slot_f1"]))
    row("에스컬레이션 α", lambda r: f"{r['by_lang']['ALL'].get('escalated',0)/r['by_lang']['ALL']['n']:.1%}")
    row("지연 P95", lambda r: f"{r['latency_p95']}s")

    if len(runs) >= 2:
        base, new = runs[0], runs[-1]
        bf = base["by_lang"]["ALL"].get("fp", 0) / (base["by_lang"]["ALL"].get("pos", 1) or 1)
        nf = new["by_lang"]["ALL"].get("fp", 0) / (new["by_lang"]["ALL"].get("pos", 1) or 1)
        imp = (bf - nf) / bf if bf else 0
        print(f"\n  FPR 개선폭: {imp:+.1%}  (기준 30% 이상)  {'PASS' if imp >= 0.30 else 'FAIL'}")


# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--testset", default="testset.jsonl")
    ap.add_argument("--endpoint", default="http://localhost:8000/v1")
    ap.add_argument("--model", default="transit-base")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--outdir", default="results")
    ap.add_argument("--compare", nargs="*")
    args = ap.parse_args()

    if args.compare:
        compare(args.compare)
        sys.exit(0)

    items = [json.loads(l) for l in Path(args.testset).read_text().splitlines() if l.strip()]
    print(f"평가셋 {len(items)}문항 로드", file=sys.stderr)

    res = evaluate(items, args.endpoint, args.model, args.tag)
    report(res)

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)
    out = outdir / f"{args.tag}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n저장: {out}")
