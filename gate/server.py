"""
다국어 교통 챗봇 UI 서버

  FastAPI 백엔드 + 단일 HTML 프런트
  로컬 전용 (127.0.0.1:7860)

기능
  - 멀티턴: 이전 턴의 슬롯을 이어받아 "그럼 다음 열차는?" 같은 후속 질문 처리
  - 디버그 패널: 게이트 판정 / 도구 / 검증 결과 실시간 표시
  - 지표: 게이트 지연, 전체 지연, 토큰, 누적 비용 추정
  - 예시 질문: 5개 언어 프리셋

실행
  python server.py
  → 윈도우 브라우저에서 http://localhost:7860
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import pipeline

app = FastAPI(title="MaaS Transit Chatbot")

# 세션별 상태 (로컬 단일 사용자 전제. 운영에서는 DynamoDB + TTL)
SESSIONS: dict[str, dict] = {}

# Haiku 요금 추정 (USD/1M tokens). 실제 청구와 다를 수 있으니 참고용.
PRICE_IN, PRICE_OUT = 0.25, 1.25


class ChatIn(BaseModel):
    text: str
    session_id: str | None = None


def get_session(sid: str | None) -> tuple[str, dict]:
    if not sid or sid not in SESSIONS:
        sid = str(uuid.uuid4())[:8]
        SESSIONS[sid] = {"turns": [], "last_slots": {}, "cost_usd": 0.0,
                         "tokens_in": 0, "tokens_out": 0}
    return sid, SESSIONS[sid]


def merge_slots(new: dict, prev: dict) -> tuple[dict, list[str]]:
    """
    멀티턴 슬롯 승계.
    "그럼 다음 열차는?" 처럼 슬롯이 비면 직전 턴 값을 물려받는다.
    단 datetime 은 승계하지 않는다 — 시점이 바뀌었을 가능성이 높아 위험하다.
    """
    carried = []
    merged = dict(new)
    for k in ("origin", "destination", "pax"):
        if not merged.get(k) and prev.get(k):
            merged[k] = prev[k]
            carried.append(k)
    return merged, carried


@app.post("/api/chat")
def chat(req: ChatIn):
    sid, sess = get_session(req.session_id)
    t0 = time.perf_counter()

    # 파이프라인의 handle() 을 쓰되, 슬롯 승계를 위해 게이트 결과를 가로챈다
    orig_call_tool = pipeline.tools.call_tool
    carried: list[str] = []

    def patched_call_tool(intent, slots, **kw):
        # **kw 로 받아 그대로 넘긴다. pipeline 이 call_tool 에 인자를
        # 더해도(현재는 원문 text) 이 래퍼를 고치지 않아도 되게 한다.
        nonlocal carried
        merged, carried = merge_slots(slots, sess["last_slots"])
        return orig_call_tool(intent, merged, **kw)

    pipeline.tools.call_tool = patched_call_tool
    try:
        r = pipeline.handle(req.text)
    except Exception as e:
        pipeline.tools.call_tool = orig_call_tool
        return {"session_id": sid, "route": "error", "answer": f"오류: {e}",
                "trace": {}, "metrics": {}}
    finally:
        pipeline.tools.call_tool = orig_call_tool

    # 슬롯 저장 (다음 턴 승계용)
    g = r["trace"].get("gate", {})
    slots = {"origin": g.get("origin") or None, "destination": g.get("destination") or None,
             "pax": g.get("pax") or None}
    merged, _ = merge_slots(slots, sess["last_slots"])
    if merged.get("origin") or merged.get("destination"):
        sess["last_slots"] = merged

    # 비용 추정 (Supervisor 호출이 있었던 경우만)
    est_in, est_out = 0, 0
    if r["route"] in ("answered", "ungrounded"):
        est_in, est_out = 900, 320
        if r["trace"].get("grounded_retry") is not None:
            est_in, est_out = est_in * 2, est_out * 2
    sess["tokens_in"] += est_in
    sess["tokens_out"] += est_out
    sess["cost_usd"] += est_in / 1e6 * PRICE_IN + est_out / 1e6 * PRICE_OUT

    sess["turns"].append({"user": req.text, "bot": r["answer"], "route": r["route"]})

    return {
        "session_id": sid,
        "route": r["route"],
        "reason": r.get("reason", ""),
        "lang": r["lang"],
        "answer": r["answer"],
        "trace": r["trace"],
        "carried_slots": carried,
        "metrics": {
            "gate_ms": r["trace"].get("gate_ms"),
            "total_ms": r["total_ms"],
            "turn_tokens": est_in + est_out,
            "session_cost_usd": round(sess["cost_usd"], 5),
            "session_krw": round(sess["cost_usd"] * 1400, 1),
            "turns": len(sess["turns"]),
        },
    }


@app.post("/api/reset")
def reset(req: ChatIn):
    if req.session_id in SESSIONS:
        del SESSIONS[req.session_id]
    return {"ok": True}


@app.get("/api/health")
def health():
    import httpx
    try:
        with httpx.Client(timeout=3.0) as c:
            m = c.get(f"{pipeline.VLLM_URL}/models").json()["data"][0]["root"]
        gate = m
    except Exception as e:
        gate = f"연결 실패: {str(e)[:60]}"
    return {"gate_model": gate,
            "supervisor": pipeline.CLAUDE_MODEL,
            "guardrail": pipeline.GUARDRAIL_ID or "(미설정)"}


@app.get("/", response_class=HTMLResponse)
def index():
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    print("http://localhost:7860 에서 접속하세요")
    uvicorn.run(app, host="127.0.0.1", port=7860, log_level="warning")
