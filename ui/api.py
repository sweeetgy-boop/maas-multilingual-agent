#!/usr/bin/env python3
"""
다국어 교통 연계 AI 에이전트 — 공개 HTTP API

외부에서 챗봇을 호출하고 방어 계층을 검증할 수 있는 엔드포인트.
EC2 인스턴스에서 직접 실행하면 vLLM 이 같은 호스트라 터널이 불필요하다.

엔드포인트
  POST /v1/chat              대화 (멀티턴 지원)
  POST /v1/evaluate          단건 평가 — 차단 여부와 담당 계층 반환
  POST /v1/evaluate/batch    배치 평가 — 계층별 기여도 집계
  GET  /v1/health            상태 및 구성
  GET  /v1/spec              방어 계층 정의
  GET  /docs                 OpenAPI 문서

인증
  X-API-Key 헤더 필수. MAAS_API_KEY 환경변수로 설정한다.

실행
  export MAAS_API_KEY=$(openssl rand -hex 24)
  export VLLM_URL=http://localhost:8000/v1
  export GUARDRAIL_ID=... GUARDRAIL_VERSION=1
  export CLAUDE_MODEL=anthropic.claude-3-haiku-20240307-v1:0
  uvicorn api:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
import secrets
import sys
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gate"))
import pipeline  # noqa: E402

API_KEY = os.environ.get("MAAS_API_KEY", "")
RATE_PER_MIN = int(os.environ.get("MAAS_RATE_PER_MIN", "60"))
MAX_TEXT_LEN = 2000
SESSION_TTL = 3600

app = FastAPI(
    title="다국어 교통 연계 AI 에이전트 API",
    version="1.0.0",
    description=(
        "항공·철도·버스·숙박·공유모빌리티 조회와 연계 경로 안내를 "
        "5개 언어(ko/en/zh/ja/id)로 제공하는 에이전트.\n\n"
        "**방어 계층 6종**을 통과한 응답만 반환하며, 각 요청이 어느 계층에서 "
        "차단됐는지 `layer` 필드로 확인할 수 있다.\n\n"
        "인증: 모든 요청에 `X-API-Key` 헤더가 필요하다."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["POST", "GET"], allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────
_hits: dict[str, deque] = defaultdict(deque)
_sessions: dict[str, dict] = {}


def auth(x_api_key: str = Header(None, alias="X-API-Key")) -> str:
    if not API_KEY:
        raise HTTPException(503, "서버에 MAAS_API_KEY 가 설정되지 않았습니다")
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "유효하지 않은 API 키입니다")
    return x_api_key


def rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        raise HTTPException(429, f"분당 {RATE_PER_MIN}회 제한을 초과했습니다")
    q.append(now)


GUARD = [Depends(auth), Depends(rate_limit)]


def _gc_sessions() -> None:
    now = time.time()
    for sid in [s for s, v in _sessions.items() if now - v["at"] > SESSION_TTL]:
        del _sessions[sid]


# ─────────────────────────────────────────────────────────
LAYERS = ["local_gate", "blocklist", "pii_regex",
          "bedrock_guardrail", "grounding_check", "scope_policy"]

ROUTE_MAP: dict[str, tuple[bool, str]] = {
    "answered": (False, "none"),
    "blocked": (True, "varies"),
    "ungrounded": (True, "grounding_check"),
    "tool_empty": (False, "none"),
    "booking_unsupported": (True, "scope_policy"),
}
REASON_LAYER: dict[str, str] = {
    "out_of_domain": "local_gate",
    "toxicity": "local_gate",
    "guardrail_input": "bedrock_guardrail",
    "guardrail_output": "bedrock_guardrail",
    "hallucinated_numbers": "grounding_check",
    "booking_request": "scope_policy",
}


def classify(route: str, reason: str) -> tuple[bool, str]:
    blocked, layer = ROUTE_MAP.get(route, (False, "unknown"))
    if layer == "varies":
        layer = "blocklist" if reason.startswith("blocklist") \
            else REASON_LAYER.get(reason, "unknown")
    return blocked, layer


def merge_slots(new: dict, prev: dict) -> tuple[dict, list[str]]:
    """멀티턴 슬롯 승계. datetime 은 시점이 바뀌었을 수 있어 승계하지 않는다."""
    carried, merged = [], dict(new)
    for k in ("origin", "destination", "pax"):
        if not merged.get(k) and prev.get(k):
            merged[k] = prev[k]
            carried.append(k)
    return merged, carried


def run(text: str, session_id: str | None = None,
        origin_coords: dict | None = None, destination_coords: dict | None = None
        ) -> tuple[dict, str, list[str]]:
    """origin_coords/destination_coords: 게이트 슬롯 추출보다 우선하는 좌표
    (선택). 게이트 스키마는 건드리지 않는다 — 좌표는 게이트를 거치지 않고
    call_tool 로 직접 전달된다. 현재는 plan_journey 만 이를 사용한다."""
    _gc_sessions()
    sid = session_id if session_id in _sessions else str(uuid.uuid4())[:12]
    sess = _sessions.setdefault(sid, {"slots": {}, "at": time.time()})
    sess["at"] = time.time()

    orig = pipeline.tools.call_tool
    carried: list[str] = []

    def patched(intent, slots):
        nonlocal carried
        merged, carried = merge_slots(slots, sess["slots"])
        if origin_coords:
            merged["origin_coords"] = origin_coords
        if destination_coords:
            merged["destination_coords"] = destination_coords
        return orig(intent, merged, carried=carried)

    pipeline.tools.call_tool = patched
    try:
        r = pipeline.handle(text)
    finally:
        pipeline.tools.call_tool = orig

    g = r["trace"].get("gate", {})
    slots = {"origin": g.get("origin") or None,
             "destination": g.get("destination") or None,
             "pax": g.get("pax") or None}
    merged, _ = merge_slots(slots, sess["slots"])
    if merged.get("origin") or merged.get("destination"):
        sess["slots"] = merged
    return r, sid, carried


# ─────────────────────────────────────────────────────────
class Coords(BaseModel):
    lat: float
    lon: float


class ChatReq(BaseModel):
    message: str = Field(..., max_length=MAX_TEXT_LEN,
                         examples=["서울역에서 부산역 가는 KTX 오늘 오후"])
    session_id: str | None = Field(None, description="멀티턴 유지용. 생략 시 새 세션")
    origin_coords: Coords | None = Field(
        None, description="출발지 좌표(예: 내 위치). 주어지면 게이트의 origin 텍스트 해소보다 우선한다")
    destination_coords: Coords | None = Field(
        None, description="목적지 좌표. 주어지면 게이트의 destination 텍스트 해소보다 우선한다")


class ChatRes(BaseModel):
    session_id: str
    reply: str
    language: str = Field(description="감지된 언어 (ko/en/zh/ja/id)")
    answered: bool = Field(description="정상 답변 여부")
    blocked_by: str | None = Field(description="차단된 경우 담당 계층")
    carried_slots: list[str] = Field(description="이전 턴에서 승계된 슬롯")
    latency_ms: int


class EvalReq(BaseModel):
    text: str = Field(..., max_length=MAX_TEXT_LEN)
    id: str | None = None
    expected_blocked: bool | None = Field(
        None, description="기대 차단 여부. 주면 correct 가 채워진다")


class EvalRes(BaseModel):
    id: str
    blocked: bool
    layer: str = Field(description=f"차단 계층. 통과 시 'none'. 가능값: {LAYERS}")
    reason: str
    language: str
    latency_ms: int
    expected_blocked: bool | None = None
    correct: bool | None = None
    detail: dict


class BatchReq(BaseModel):
    cases: list[EvalReq] = Field(..., max_length=200)


# ─────────────────────────────────────────────────────────
@app.post("/v1/chat", response_model=ChatRes, dependencies=GUARD,
          summary="대화", tags=["agent"])
def chat(req: ChatReq) -> ChatRes:
    """
    교통 안내 대화. 5개 언어를 자동 감지해 같은 언어로 응답한다.

    도메인 밖 질문, 유해 표현, 예약 요청은 차단되며 `blocked_by` 에 계층이 표시된다.
    """
    try:
        oc = req.origin_coords.model_dump() if req.origin_coords else None
        dc = req.destination_coords.model_dump() if req.destination_coords else None
        r, sid, carried = run(req.message, req.session_id, oc, dc)
    except Exception as e:
        raise HTTPException(502, f"파이프라인 오류: {str(e)[:150]}")
    blocked, layer = classify(r["route"], r.get("reason", "") or "")
    return ChatRes(
        session_id=sid, reply=r["answer"], language=r["lang"],
        answered=(r["route"] == "answered"),
        blocked_by=layer if blocked else None,
        carried_slots=carried, latency_ms=r["total_ms"])


@app.post("/v1/evaluate", response_model=EvalRes, dependencies=GUARD,
          summary="단건 평가", tags=["evaluation"])
def evaluate(req: EvalReq) -> EvalRes:
    """
    방어 계층 평가. 어느 계층이 차단했는지 반환한다.

    Bedrock Guardrails 단독 평가와 달리, 로컬 게이트·금지어 사전·PII 정규식·
    근거성 검증까지 포함한 전체 스택을 측정한다.
    """
    cid = req.id or str(uuid.uuid4())[:8]
    try:
        r, _, _ = run(req.text)
    except Exception as e:
        raise HTTPException(502, f"파이프라인 오류: {str(e)[:150]}")

    reason = r.get("reason", "") or ""
    blocked, layer = classify(r["route"], reason)
    trace, g = r.get("trace", {}), r.get("trace", {}).get("gate", {})
    correct = None if req.expected_blocked is None else (blocked == req.expected_blocked)

    return EvalRes(
        id=cid, blocked=blocked, layer=layer, reason=reason or r["route"],
        language=r["lang"], latency_ms=r["total_ms"],
        expected_blocked=req.expected_blocked, correct=correct,
        detail={"route": r["route"], "in_domain": g.get("in_domain"),
                "toxicity": g.get("toxicity"), "intent": g.get("intent"),
                "pii_masked": trace.get("pii_masked", []),
                "grounded": trace.get("grounded_retry", trace.get("grounded")),
                "gate_ms": trace.get("gate_ms")})


@app.post("/v1/evaluate/batch", dependencies=GUARD,
          summary="배치 평가", tags=["evaluation"])
def evaluate_batch(body: BatchReq) -> dict:
    """최대 200건. 계층별 차단 기여도와 FPR/FNR 을 함께 반환한다."""
    results = [evaluate(c) for c in body.cases]

    by_layer: dict[str, int] = defaultdict(int)
    by_lang: dict[str, dict] = {}
    tp = tn = fp = fn = scored = 0
    for r in results:
        by_layer[r.layer] += 1
        d = by_lang.setdefault(r.language, {"n": 0, "blocked": 0, "correct": 0})
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
                fp += 1
            else:
                fn += 1

    lat = sorted(r.latency_ms for r in results)
    summary = {
        "blocked": sum(r.blocked for r in results),
        "passed": sum(not r.blocked for r in results),
        "by_layer": dict(by_layer), "by_lang": by_lang,
        "latency_p50_ms": lat[len(lat) // 2] if lat else None,
        "latency_p95_ms": lat[int(len(lat) * 0.95)] if len(lat) > 1 else None,
    }
    if scored:
        summary |= {
            "scored": scored, "accuracy": round((tp + tn) / scored, 4),
            "fpr": round(fp / (fp + tn), 4) if (fp + tn) else 0.0,
            "fnr": round(fn / (fn + tp), 4) if (fn + tp) else 0.0,
            "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        }
    return {"total": len(results), "summary": summary,
            "results": [r.model_dump() for r in results]}


@app.get("/v1/health", tags=["meta"], summary="상태 및 구성")
def health() -> dict:
    """인증 불필요. 서비스 가동 여부와 구성 정보를 반환한다."""
    import httpx
    gate = "unavailable"
    try:
        with httpx.Client(timeout=3.0) as c:
            gate = c.get(f"{pipeline.VLLM_URL}/models").json()["data"][0]["root"]
    except Exception:
        pass
    return {
        "status": "ok" if gate != "unavailable" else "degraded",
        "service": "maas-multilingual-transit-agent",
        "version": "1.0.0",
        "gate_model": gate,
        "supervisor_model": pipeline.CLAUDE_MODEL,
        "guardrail_id": pipeline.GUARDRAIL_ID or None,
        "languages": list(pipeline.LANGS),
        "layers": LAYERS,
        "auth_required": bool(API_KEY),
        "rate_limit_per_min": RATE_PER_MIN,
    }


@app.get("/v1/spec", tags=["meta"], summary="방어 계층 정의")
def spec() -> dict:
    return {
        "layers": {
            "local_gate": "Qwen3-4B 제로샷 게이트. 언어·도메인·유해도 판정",
            "blocklist": "다국어 금지어 사전. 자모분리·별표마스킹 우회 탐지",
            "pii_regex": "PII 정규식. 한글 조사 대응 lookaround 사용",
            "bedrock_guardrail": "Bedrock Guardrails Standard tier + cross-region",
            "grounding_check": "답변 숫자 ↔ 도구 응답 대조. 실패 시 재생성 후 폐기",
            "scope_policy": "예약·결제 요청 차단 (조회 전용 서비스)",
        },
        "notes": [
            "Bedrock Guardrails 는 word filter 와 contextual grounding 에서 "
            "ko/zh/ja/id 를 지원하지 않는다. blocklist 와 grounding_check 가 그 공백을 담당한다.",
            "Guardrails 를 Classic tier 로 생성하면 한국어 입력이 전혀 걸러지지 않는다. "
            "Standard tier + cross-region inference 가 필수다.",
            "교통 데이터는 현재 목(mock) 데이터다. 시각·요금은 실제 운행정보가 아니다.",
        ],
        "scope": {
            "supported": ["항공·철도·버스 시간표 조회", "숙박 검색",
                          "공유 자전거·차량 조회", "연계 경로 안내",
                          "역·차내 시설 및 규정 문의"],
            "not_supported": ["예약·결제·취소", "관광 일정", "맛집 추천",
                              "날씨", "환전", "의료·법률·투자 자문"],
        },
    }


if __name__ == "__main__":
    import uvicorn
    if not API_KEY:
        print("경고: MAAS_API_KEY 미설정 — 모든 요청이 401 로 거부됩니다", file=sys.stderr)
    print("API: http://0.0.0.0:8080/docs")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
