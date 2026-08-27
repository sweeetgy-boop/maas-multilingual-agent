#!/usr/bin/env python3
"""
평가셋 90문항을 평가 엔드포인트로 던져 계층별 기여도를 산출한다.

GuardBench 연동 전에 우리 쪽에서 먼저 돌려보고 결과 형식을 확인하는 용도다.
GuardBench 가 붙으면 같은 엔드포인트를 그쪽에서 호출하게 된다.

사용법
  python run_eval_via_endpoint.py --testset ../eval/testset.jsonl
  python run_eval_via_endpoint.py --endpoint http://localhost:8080 --lang id
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def load_cases(path: Path, lang: str | None) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        it = json.loads(line)
        if lang and it["lang"] != lang:
            continue
        # route=blocked 또는 ungrounded 를 기대 차단으로 본다
        expected = it["route"] == "blocked"
        cases.append({"id": it["id"], "text": it["text"],
                      "expected_blocked": expected})
    return cases


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--testset", default="../eval/testset.jsonl")
    ap.add_argument("--endpoint", default="http://localhost:8080")
    ap.add_argument("--lang", help="ko/en/id 등으로 필터")
    ap.add_argument("--out", default="results/endpoint_eval.json")
    a = ap.parse_args()

    cases = load_cases(Path(a.testset), a.lang)
    print(f"평가셋 {len(cases)}문항")

    with httpx.Client(timeout=600.0) as c:
        h = c.get(f"{a.endpoint}/v1/health").json()
        print(f"대상: {h['config']['gate_model']} + "
              f"{h['config']['supervisor_model'].split('.')[-1]} + "
              f"guardrail {h['config']['guardrail_id']}")
        print("실행 중... (문항당 1~5초)")
        r = c.post(f"{a.endpoint}/v1/evaluate/batch", json={"cases": cases})
        r.raise_for_status()
        data = r.json()

    s = data["summary"]
    print(f"\n{'=' * 62}")
    print(f" 총 {data['total']}문항 · 차단 {s['blocked']} · 통과 {s['passed']}")
    print(f"{'=' * 62}")

    print("\n── 계층별 차단 기여도 ──")
    total_blocked = s["blocked"] or 1
    for layer, n in sorted(s["by_layer"].items(), key=lambda x: -x[1]):
        if layer == "none":
            continue
        bar = "█" * int(n / total_blocked * 30)
        print(f"  {layer:20} {n:3}건 {n/total_blocked:5.1%} {bar}")

    print("\n── 언어별 ──")
    print(f"  {'lang':6}{'n':>5}{'차단':>7}{'정확도':>9}")
    for lang, d in sorted(s["by_lang"].items()):
        acc = d["correct"] / d["n"] if d["n"] else 0
        print(f"  {lang:6}{d['n']:>5}{d['blocked']:>7}{acc:>9.1%}")

    if "accuracy" in s:
        print(f"\n── 판정 성능 ──")
        cm = s["confusion"]
        print(f"  정확도  {s['accuracy']:.1%}")
        print(f"  FPR     {s['fpr']:.1%}  (정상을 막은 비율)")
        print(f"  FNR     {s['fnr']:.1%}  (막아야 할 걸 통과시킨 비율)")
        print(f"  TP {cm['tp']} / TN {cm['tn']} / FP {cm['fp']} / FN {cm['fn']}")

    print(f"\n── 지연 ──")
    print(f"  P50 {s['latency_p50_ms']}ms   P95 {s['latency_p95_ms']}ms")

    wrong = [r for r in data["results"] if r["correct"] is False]
    if wrong:
        print(f"\n── 오답 {len(wrong)}건 ──")
        for r in wrong[:15]:
            kind = "오탐(정상차단)" if r["blocked"] else "미탐(통과)"
            print(f"  {r['id']:9} {kind:14} {r['layer']:18} {r['text'][:32]}")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
