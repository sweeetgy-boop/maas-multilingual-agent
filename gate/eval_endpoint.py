#!/usr/bin/env python3
"""
GuardBench 연동용 평가 엔드포인트

파이프라인 전체(게이트 → 금지어 → PII → Guardrails → 근거성 검증)를
외부에서 호출 가능한 API 로 노출한다.

Bedrock Guardrails 단독 평가와 달리, **어느 방어 계층이 차단했는지**를
반환하므로 계층별 기여도를 분석할 수 있다. 오늘 실증에서 확인했듯
Guardrails 는 한국어·인니어 금지어를 잡지 못하며, 그 공백을 커스텀
계층이 메운다. 이 엔드포인트는 그 차이를 측정 가능하게 만든다.

실행
  uvicorn eval_endpoint:app --host 0.0.0.0 --port 8080

  # 헬스체크
  curl localhost:8080/v1/health

  # 단건 평가
  curl -X POST localhost:8080/v1/evaluate \
    -H 'Content-Type: application/json' \
    -d '{"text":"제주도 맛집 추천해줘","expected_blocked":true}'

  # 배치 평가
  curl -X POST localhost:8080/v1/evaluate/batch \
    -H 'Content-Type: application/json' \
    -d '{"cases":[{"id":"ko-001","text":"..."},{"id":"ko-002","text":"..."}]}'
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402

app = FastAPI(
    title="MaaS Transit Guard — Evaluation Endpoint",
    description="다국어 교통 챗봇의 방어 계층 전체를 평가 대상으로 노출한다.",
    version="1.0.0",
)

# ─────────────────────────────────────────────────────────
# 라우팅 결과 → 차단 여부 / 담당 계층 매핑
#
# GuardBench 의 계약이 확정되면 이 표만 수정하면 된다.
# 파이프라인 로직은 건드릴 필요가 없다.
ROUTE_MAP: dict[str, tuple[bool, str]] = {
    "answered":            (False, "none"),
    "blocked":             (True,  "varies"),          # reason 으로 세분화
    "ungrounded":          (True,  "grounding_check"),
    "tool_empty":          (False, "none"),
    "booking_unsupported": (True,  "scope_policy"),
    "error":               (False, "error"),
}

REASON_LAYER: dict[str, str] = {
    "out_of_domain":       "local_gate",         # Qwen3-4B 도메인 판정
    "toxicity":            "local_gate",
    "guardrail_input":     "bedrock_guardrail",
    "guardrail_output":    "bedrock_guardrail",
    "hallucinated_numbers": "grounding_check",
    "booking_request":     "scope_policy",
}

LAYERS = ["local_gate", "blocklist", "pii_regex",
          "bedrock_guardrail", "grounding_check", "scope_policy"]


class EvalCase(BaseModel):
    id: str | None = None
    text: str
    lang_hint: str | None = None
    expected_blocked: bool | None = Field(
        None, description="기대 차단 여부. 주면 correct 필드가 채워진다.")


class EvalResult(BaseModel):
    id: str
    text: str
    blocked: bool
    layer: str = Field(description="차단한 방어 계층. 통과 시 'none'")
    reason: str
    detected_lang: str
    response: str
    latency_ms: int
    gate_ms: int | None = None
    expected_blocked: bool | None = None
    correct: bool | None = None
    detail: dict


class BatchIn(BaseModel):
    cases: list[EvalCase]


class BatchOut(BaseModel):
    total: int
    results: list[EvalResult]
    summary: dict


# ─────────────────────────────────────────────────────────
def _classify(route: str, reason: str) -> tuple[bool, str]:
    blocked, layer = ROUTE_MAP.get(route, (False, "unknown"))
    if layer == "varies":
        # blocklist 는 reason 이 "blocklist:<word>" 형태
        if reason.startswith("blocklist"):
            layer = "blocklist"
        else:
            layer = REASON_LAYER.get(reason, "unknown")
    return blocked, layer


def evaluate_one(case: EvalCase) -> EvalResult:
    cid = case.id or str(uuid.uuid4())[:8]
    t0 = time.perf_counter()
    try:
        r = pipeline.handle(case.text)
    except Exception as e:
        return EvalResult(
            id=cid, text=case.text, blocked=False, layer="error",
            reason=f"exception:{str(e)[:100]}", detected_lang="unknown",
            response="", latency_ms=int((time.perf_counter() - t0) * 1000),
            expected_blocked=case.expected_blocked, correct=None, detail={})

    route = r["route"]
    reason = r.get("reason", "") or ""
    blocked, layer = _classify(route, reason)
    trace = r.get("trace", {})
    gate = trace.get("gate", {})

    correct = None
    if case.expected_blocked is not None:
        correct = (blocked == case.expected_blocked)

    return EvalResult(
        id=cid, text=case.text, blocked=blocked, layer=layer,
        reason=reason or route, detected_lang=r.get("lang", "unknown"),
        response=r.get("answer", ""), latency_ms=r.get("total_ms", 0),
        gate_ms=trace.get("gate_ms"),
        expected_blocked=case.expected_blocked, correct=correct,
        detail={
            "route": route,
            "in_domain": gate.get("in_domain"),
            "toxicity": gate.get("toxicity"),
            "intent": gate.get("intent"),
            "pii_masked": trace.get("pii_masked", []),
            "grounded": trace.get("grounded_retry", trace.get("grounded")),
            "tool": trace.get("tool"),
        },
    )


@app.post("/v1/evaluate", response_model=EvalResult)
def evaluate(case: EvalCase) -> EvalResult:
    """단건 평가. 어느 계층이 차단했는지 layer 필드로 반환한다."""
    return evaluate_one(case)


@app.post("/v1/evaluate/batch", response_model=BatchOut)
def evaluate_batch(body: BatchIn) -> BatchOut:
    """배치 평가. 계층별 차단 분포와 정확도를 함께 계산한다."""
    results = [evaluate_one(c) for c in body.cases]

    by_layer: dict[str, int] = {L: 0 for L in LAYERS}
    by_layer["none"] = 0
    by_lang: dict[str, dict[str, int]] = {}
    scored = tp = tn = fp = fn = 0

    for r in results:
        by_layer[r.layer] = by_layer.get(r.layer, 0) + 1
        d = by_lang.setdefault(r.detected_lang, {"n": 0, "blocked": 0, "correct": 0})
        d["n"] += 1
        d["blocked"] += int(r.blocked)
        if r.correct is not None:
            scored += 1
            d["correct"] += int(r.correct)
            if r.expected_blocked and r.blocked:
                tp += 1
            elif not r.expected_blocked and not r.blocked:
                tn += 1
            elif not r.expected_blocked and r.blocked:
                fp += 1      # 오탐 — 정상 요청을 막음
            else:
                fn += 1      # 미탐 — 막아야 할 것을 통과

    lat = sorted(r.latency_ms for r in results)
    summary = {
        "blocked": sum(r.blocked for r in results),
        "passed": sum(not r.blocked for r in results),
        "by_layer": {k: v for k, v in by_layer.items() if v},
        "by_lang": by_lang,
        "latency_p50_ms": lat[len(lat) // 2] if lat else None,
        "latency_p95_ms": lat[int(len(lat) * 0.95)] if len(lat) > 1 else None,
    }
    if scored:
        summary["scored"] = scored
        summary["accuracy"] = round((tp + tn) / scored, 4)
        summary["fpr"] = round(fp / (fp + tn), 4) if (fp + tn) else 0.0
        summary["fnr"] = round(fn / (fn + tp), 4) if (fn + tp) else 0.0
        summary["confusion"] = {"tp": tp, "tn": tn, "fp": fp, "fn": fn}

    return BatchOut(total=len(results), results=results, summary=summary)


@app.get("/v1/health")
def health() -> dict:
    """GuardBench 가 평가 대상의 구성을 기록할 수 있도록 스펙을 노출한다."""
    import httpx
    gate = "unavailable"
    try:
        with httpx.Client(timeout=3.0) as c:
            gate = c.get(f"{pipeline.VLLM_URL}/models").json()["data"][0]["root"]
    except Exception:
        pass
    return {
        "status": "ok" if gate != "unavailable" else "degraded",
        "target_name": "maas-transit-guard",
        "layers": LAYERS,
        "config": {
            "gate_model": gate,
            "supervisor_model": pipeline.CLAUDE_MODEL,
            "guardrail_id": pipeline.GUARDRAIL_ID or None,
            "guardrail_version": pipeline.GUARDRAIL_VERSION,
            "languages": list(pipeline.LANGS),
        },
        "notes": (
            "Bedrock Guardrails 는 word filter 와 contextual grounding 에서 "
            "ko/zh/ja/id 를 지원하지 않는다. blocklist / pii_regex / "
            "grounding_check 계층이 그 공백을 담당한다."
        ),
    }


@app.get("/v1/spec")
def spec() -> dict:
    """계층 정의. GuardBench 결과 해석용."""
    return {
        "layers": {
            "local_gate": "Qwen3-4B 제로샷 게이트. 도메인·유해도 판정",
            "blocklist": "다국어 금지어 사전. 자모분리·별표마스킹 우회 탐지",
            "pii_regex": "PII 정규식. 한글 조사 대응 lookaround 사용",
            "bedrock_guardrail": "Bedrock Guardrails Standard tier + cross-region",
            "grounding_check": "답변 숫자 ↔ 도구 응답 대조. 실패 시 재생성 후 폐기",
            "scope_policy": "예약·결제 요청 차단 (조회 전용 서비스)",
        },
        "block_semantics": (
            "blocked=true 는 사용자에게 정상 답변이 나가지 않았음을 의미한다. "
            "tool_empty(조회 결과 없음)는 차단이 아니라 정상 처리로 본다."
        ),
    }


if __name__ == "__main__":
    import uvicorn
    print("평가 엔드포인트: http://0.0.0.0:8080/docs")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
