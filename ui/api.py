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

import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gate"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402
from session_slots import merge_slots, slots_to_persist  # noqa: E402

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


def _rate_limited(request: Request) -> int | None:
    """레이트리밋 판정. 초과면 재시도까지 남은 초, 통과면 None(호출을 계상한다).

    기존 rate_limit() 과 OpenAI 호환 경로가 같은 카운터를 봐야 하므로 여기
    한 곳에만 둔다 — 두 벌로 두면 한쪽만 고쳐져 제한이 어긋난다."""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        return max(1, int(60 - (now - q[0])) + 1)
    q.append(now)
    return None


def rate_limit(request: Request) -> None:
    if _rate_limited(request) is not None:
        raise HTTPException(429, f"분당 {RATE_PER_MIN}회 제한을 초과했습니다")


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
    slots = slots_to_persist(g)
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


# ─────────────────────────────────────────────────────────
# OpenAI Chat Completions 호환 계층 (GuardBench 연동용)
#
# GuardBench 는 OpenAI 형식으로 질문을 던지고 답변을 받아 차단 여부를
# 평가한다. 기존 /v1/chat 은 그대로 둔다 — 모바일 앱과 채팅 UI 가 그
# 스키마를 쓰고, blocked_by·carried_slots 처럼 OpenAI 표준에 자리가 없는
# 값을 돌려줘야 한다. 여기서는 같은 run()·classify() 를 재사용해 형식만
# 바꾼다.
#
# 별도 파일로 빼지 않는 이유: 예전에 ui/api.py 와 ui/server.py 에 같은 슬롯
# 로직이 중복돼 한쪽에만 버그가 남았던 적이 있다. 세션·판정 로직을 한 벌로
# 둔다.
OPENAI_MODEL = "maas-transit"
_OPENAI_PATHS = ("/v1/chat/completions", "/v1/models")


class OpenAIError(HTTPException):
    """OpenAI 에러 엔벨로프로 직렬화되는 예외. 기존 엔드포인트는 FastAPI
    기본 {"detail": ...} 형식을 그대로 쓴다 — 이 클래스는 OpenAI 경로에서만
    쓴다."""

    def __init__(self, status_code: int, message: str, code: str,
                 err_type: str = "invalid_request_error",
                 headers: dict | None = None) -> None:
        super().__init__(status_code, message, headers=headers)
        self.code = code
        self.err_type = err_type


@app.exception_handler(OpenAIError)
async def _openai_error_handler(_: Request, exc: OpenAIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.detail, "type": exc.err_type,
                           "code": exc.code}},
        headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request,
                                    exc: RequestValidationError):
    """본문 형식 오류를 OpenAI 경로에서만 OpenAI 형식으로 바꾼다.
    나머지 경로는 FastAPI 기본 처리(422)를 그대로 위임한다."""
    if request.url.path.rstrip("/") in _OPENAI_PATHS:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", ()))
        return JSONResponse(
            status_code=400,
            content={"error": {
                "message": f"{where}: {first.get('msg', '잘못된 요청 본문입니다')}",
                "type": "invalid_request_error", "code": "invalid_request"}})
    return await request_validation_exception_handler(request, exc)


def auth_openai(x_api_key: str = Header(None, alias="X-API-Key"),
                authorization: str = Header(None)) -> str:
    """X-API-Key 와 OpenAI 표준 Authorization: Bearer 를 모두 받는다.
    OpenAI SDK 는 Bearer 만 보낸다. 기존 auth() 는 건드리지 않는다."""
    if not API_KEY:
        raise OpenAIError(503, "서버에 MAAS_API_KEY 가 설정되지 않았습니다",
                          "server_not_configured", "server_error")
    token = x_api_key
    if not token and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    if not token or not secrets.compare_digest(token, API_KEY):
        raise OpenAIError(401, "유효하지 않은 API 키입니다",
                          "invalid_api_key", "authentication_error")
    return token


def rate_limit_openai(request: Request) -> None:
    retry = _rate_limited(request)
    if retry is not None:
        raise OpenAIError(429, f"분당 {RATE_PER_MIN}회 제한을 초과했습니다",
                          "rate_limit_exceeded", "rate_limit_error",
                          headers={"Retry-After": str(retry)})


OPENAI_GUARD = [Depends(auth_openai), Depends(rate_limit_openai)]


class OAIMessage(BaseModel):
    role: str
    content: str | None = None


class ChatCompletionReq(BaseModel):
    # 실제 모델은 하나뿐이라 값은 검증하지 않고 그대로 되돌려준다.
    model: str = OPENAI_MODEL
    messages: list[OAIMessage]
    stream: bool = False
    # temperature·max_tokens 등 나머지 OpenAI 파라미터는 무시한다
    # (pydantic 기본 동작이 미선언 필드를 버린다).


def _session_key(msgs: list[dict]) -> str:
    raw = json.dumps(msgs, sort_keys=True, ensure_ascii=False)
    return "oai-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def openai_to_internal(body: ChatCompletionReq) -> tuple[str, str | None, list[dict]]:
    """OpenAI 요청 → (본문, 세션키, 직전 대화). 형식 오류는 OpenAIError."""
    if body.stream:
        # 우리 파이프라인은 Supervisor 응답을 받은 뒤 근거성 검증을 거쳐야
        # 최종 응답이 정해진다. 검증에 실패하면 재생성하고, 그래도 실패하면
        # 답변을 폐기한다. 토큰 단위로 흘려보내면 검증 전에 환각이 노출돼
        # 방어 계층의 전제가 무너진다.
        raise OpenAIError(400, "streaming is not supported; set stream=false",
                          "streaming_not_supported")

    msgs = [{"role": m.role, "content": m.content or ""} for m in body.messages]

    # system 메시지는 버린다. 우리 Supervisor 프롬프트를 외부에서 덮어쓰게
    # 하면 방어 계층이 통째로 무력화된다 — 인젝션의 정석 경로다.
    dropped = [m for m in msgs if m["role"] == "system"]
    if dropped:
        print(f"[openai] system 메시지 {len(dropped)}건 무시 "
              f"(첫 80자: {dropped[0]['content'][:80]!r})", file=sys.stderr)

    idx = next((i for i in range(len(msgs) - 1, -1, -1)
                if msgs[i]["role"] == "user"), None)
    if idx is None:
        raise OpenAIError(400, "messages 에 role=user 메시지가 없습니다",
                          "no_user_message")

    text = msgs[idx]["content"]
    if not text.strip():
        raise OpenAIError(400, "마지막 user 메시지가 비어 있습니다",
                          "empty_message")
    if len(text) > MAX_TEXT_LEN:
        raise OpenAIError(400, f"메시지가 {MAX_TEXT_LEN}자를 넘습니다",
                          "string_above_max_length")

    # 마지막 user 메시지 앞부분이 세션 키가 된다. 첫 턴이면 None(새 세션).
    prior = [m for m in msgs[:idx] if m["role"] in ("user", "assistant")]
    return text, (_session_key(prior) if prior else None), prior


def internal_to_openai(r: dict, sid: str, carried: list[str],
                       model: str) -> dict:
    """파이프라인 결과 → OpenAI Chat Completion 응답."""
    blocked, layer = classify(r["route"], r.get("reason", "") or "")
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": r["answer"]},
            # 답변 텍스트만으로는 차단과 조회 실패를 가릴 수 없다 — 둘 다
            # 멀쩡한 문장이다. finish_reason 이 판정 근거가 된다.
            "finish_reason": "content_filter" if blocked else "stop",
        }],
        # 토큰을 실제로 세지 않는다. 표준 클라이언트가 이 필드를 기대하므로
        # 생략하지 않되, 추정치를 지어내지 않고 0 으로 둔다.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        # 비표준 확장. 표준 클라이언트는 무시하고, GuardBench 는 여기서
        # 계층 정보를 읽는다.
        "x_maas": {
            "blocked_by": layer if blocked else None,
            "language": r["lang"],
            "answered": r["route"] == "answered",
            "carried_slots": carried,
            "latency_ms": r["total_ms"],
            "session_id": sid,
        },
    }


def _canonical_sid(sid: str) -> str:
    """대화를 가리키는 안정적인 식별자.

    OpenAI 경로의 세션 키는 직전 대화의 해시라서 턴마다 바뀐다. 그대로
    노출하면 클라이언트가 같은 대화를 이어가는데도 session_id 가 매번 달라
    보인다. 그래서 세션을 처음 만든 내부 id 를 세션 딕셔너리에 새겨 두고
    이후 턴에서도 그것을 돌려준다 — 키가 여럿이어도 세션 객체는 하나라서
    (_link_next_turn 이 복사가 아니라 공유한다) 값이 따라온다.

    기존 /v1/chat 은 run() 이 준 sid 를 그대로 쓰므로 영향이 없다."""
    sess = _sessions.get(sid)
    if sess is None:
        return sid
    return sess.setdefault("conversation_id", sid)


def _link_next_turn(prior: list[dict], user_text: str, answer: str,
                    sid: str) -> None:
    """다음 요청이 제시할 세션 키에 이번 세션을 미리 걸어 둔다.

    OpenAI 는 무상태라 클라이언트가 매번 전체 messages 를 보낸다. 우리는
    session_id 로 슬롯을 보관하므로 대화를 잇는 키가 필요하다.

    키는 "마지막 user 메시지를 뺀 앞부분"의 해시인데, 이 값은 턴이 넘어갈
    때마다 달라진다(턴2 는 [u1,a1], 턴3 은 [u1,a1,u2,a2]). 그래서 응답을
    만든 직후 다음 턴이 제시할 해시를 미리 계산해, 같은 세션 딕셔너리를 그
    키에도 걸어 둔다. 클라이언트가 우리 답변을 그대로 되돌려 보내면 슬롯이
    이어지고, 이력을 편집하면 해시가 어긋나 새 세션이 된다 — 편집된 이력은
    다른 대화로 보는 것이 맞다.

    복사가 아니라 같은 객체를 공유하므로 이후 턴의 슬롯 갱신이 양쪽에 함께
    반영된다. 키가 턴마다 하나씩 늘지만 _gc_sessions 의 TTL 로 정리된다.
    """
    sess = _sessions.get(sid)
    if sess is None:
        return
    nxt = _session_key(prior + [{"role": "user", "content": user_text},
                                {"role": "assistant", "content": answer}])
    _sessions[nxt] = sess


@app.post("/v1/chat/completions", dependencies=OPENAI_GUARD,
          summary="OpenAI 호환 대화", tags=["openai"])
def chat_completions(body: ChatCompletionReq) -> dict:
    """OpenAI Chat Completions 호환 엔드포인트.

    인증은 `Authorization: Bearer <키>` 또는 `X-API-Key` 둘 다 받는다.

    - `stream=true` 는 지원하지 않는다(400). 근거성 검증이 끝나야 최종
      응답이 정해지므로, 토큰을 흘려보내면 검증 전에 환각이 노출된다.
    - `system` 메시지는 무시한다. 외부에서 Supervisor 프롬프트를 덮어쓰면
      방어 계층이 무력화된다.
    - 차단되면 `finish_reason="content_filter"`, 정상이면 `"stop"` 이다.
    - `x_maas.blocked_by` 에 담당 방어 계층이 담긴다(비표준 확장).
    - 응답까지 보통 4~7초 걸린다. 배치 호출은 타임아웃을 넉넉히 잡는다.
    """
    text, session_key, prior = openai_to_internal(body)
    try:
        r, sid, carried = run(text, session_key)
    except Exception as e:
        raise OpenAIError(502, f"파이프라인 오류: {str(e)[:150]}",
                          "upstream_error", "server_error")
    out = internal_to_openai(r, _canonical_sid(sid), carried,
                             body.model or OPENAI_MODEL)
    _link_next_turn(prior, text, r["answer"], sid)
    return out


@app.get("/v1/models", dependencies=OPENAI_GUARD,
         summary="모델 목록 (OpenAI 호환)", tags=["openai"])
def list_models() -> dict:
    """OpenAI 호환 모델 목록. 실제 모델은 `maas-transit` 하나뿐이다."""
    return {
        "object": "list",
        "data": [{
            "id": OPENAI_MODEL,
            "object": "model",
            "created": 0,
            "owned_by": "maas",
            "x_maas": {
                "streaming": False,
                "streaming_note": (
                    "근거성 검증이 끝나야 최종 응답이 정해진다. 토큰 단위로 "
                    "흘려보내면 검증 전에 환각이 노출되므로 stream=true 는 "
                    "400 으로 거부한다."),
                "system_messages": "ignored (Supervisor 프롬프트 보호)",
                "blocked_signal": "finish_reason == 'content_filter'",
                "languages": list(pipeline.LANGS),
                "typical_latency_ms": "4000-7000",
                "rate_limit_per_min": RATE_PER_MIN,
            },
        }],
    }


if __name__ == "__main__":
    import uvicorn
    if not API_KEY:
        print("경고: MAAS_API_KEY 미설정 — 모든 요청이 401 로 거부됩니다", file=sys.stderr)
    print("API: http://0.0.0.0:8080/docs")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
