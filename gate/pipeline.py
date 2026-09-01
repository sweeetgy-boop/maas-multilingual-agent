"""
다국어 교통 연계 AI 챗봇 — 엔드투엔드 파이프라인

  사용자 입력
      ↓
  ① 게이트 (Qwen3-4B, 로컬)   언어·도메인·유해도·PII·의도·슬롯
      ↓  차단 → 언어별 정형 응답
  ② 금지어 사전 (Aho-Corasick 없이 정규화 매칭)
      ↓
  ③ PII 마스킹 (클라우드 전송 전)
      ↓
  ④ 도구 호출 (결정론적)
      ↓
  ⑤ Supervisor (Bedrock Claude)  도구 JSON → 사용자 언어 답변
      ↓
  ⑥ 검증 (답변 숫자 ↔ 도구 JSON 대조)
      ↓
  ⑦ ApplyGuardrail (로컬 경로도 예외 없음)
      ↓
  응답

사용법
  python pipeline.py --text "서울역에서 부산 가는 KTX 알려줘"
  python pipeline.py --demo          # 5개 언어 시나리오 일괄 실행
  python pipeline.py --serve         # 대화형
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata

import boto3
import httpx

import tools

# ─────────────────────────────────────────────────────────
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000/v1")
GATE_MODEL = os.environ.get("GATE_MODEL", "transit-base")
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "1")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "apac.anthropic.claude-sonnet-4-5-20250929-v1:0")
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

bedrock = boto3.client("bedrock-runtime", region_name=REGION)

LANGS = ("ko", "en", "zh", "ja", "id")

# ── 언어별 정형 응답 (LLM 생성 금지) ──
REFUSAL = {
    "ko": "저는 교통·이동 안내만 도와드릴 수 있어요. 항공·철도·버스 조회, 숙박 검색, 목적지까지의 연계 경로를 물어봐 주세요.",
    "en": "I can only help with transport and travel. Try asking about flights, trains, buses, lodging, or a route to your destination.",
    "zh": "我只能提供交通出行方面的帮助。您可以询问航班、列车、巴士、住宿或到目的地的换乘路线。",
    "ja": "交通・移動のご案内のみ対応しております。航空・鉄道・バスの検索、宿泊、目的地までの乗り継ぎ経路をお尋ねください。",
    "id": "Saya hanya dapat membantu soal transportasi dan perjalanan. Silakan tanyakan penerbangan, kereta, bus, penginapan, atau rute ke tujuan Anda.",
}
WARNING = {
    "ko": "요청하신 내용에는 응답할 수 없습니다. 교통 정보로 다시 질문해 주세요.",
    "en": "I can't respond to that. Please rephrase as a transport question.",
    "zh": "无法回应该内容，请改用交通相关的问题。",
    "ja": "その内容にはお答えできません。交通に関する質問でお願いします。",
    "id": "Saya tidak dapat menanggapi hal itu. Silakan ajukan pertanyaan transportasi.",
}
BOOKING_ONLY = {
    "ko": "저는 조회만 도와드릴 수 있어요. 예매·결제·취소는 각 운영기관 공식 홈페이지나 앱에서 진행해 주세요. 시간표나 요금 조회는 도와드릴 수 있습니다.",
    "en": "I can only look up information. Booking, payment and cancellation must be done on the operator's official website or app. I'm happy to check schedules or fares.",
    "zh": "我只能提供查询服务。订票、支付和退票请在各运营方官方网站或应用程序办理。时刻表和票价查询可以帮您。",
    "ja": "照会のみ対応しております。予約・決済・取消は各運営機関の公式サイトまたはアプリをご利用ください。時刻表や運賃の照会はお手伝いできます。",
    "id": "Saya hanya dapat membantu pencarian informasi. Pemesanan, pembayaran, dan pembatalan harus dilakukan di situs atau aplikasi resmi operator. Saya bisa membantu cek jadwal atau tarif.",
}

BOOKING_WORDS = ("예약", "예매", "결제", "취소", "환불",
                 "book", "reserve", "payment", "cancel", "refund",
                 "pesan", "pemesanan", "bayar", "batal",
                 "予約", "決済", "取消", "订票", "预订", "支付", "退票")

NOT_FOUND = {
    "ko": "요청하신 구간의 정보를 찾지 못했습니다. 출발지와 도착지를 다시 확인해 주세요.",
    "en": "I couldn't find information for that route. Please check the origin and destination.",
    "zh": "未找到该区间的信息，请确认出发地和目的地。",
    "ja": "該当区間の情報が見つかりませんでした。出発地と目的地をご確認ください。",
    "id": "Informasi rute tersebut tidak ditemukan. Mohon periksa asal dan tujuan.",
}


def L(code: str) -> str:
    return code if code in REFUSAL else "en"


# ─────────────────────────────────────────────────────────
# ② 금지어 사전 — Guardrails word filter 가 ko/zh/ja/id 미지원이라 직접 처리
# 운영 시에는 기관 정책 어휘로 교체하고 S3 에서 로드한다.
BLOCKLIST = {
    "ko": ["시발", "씨발", "ㅅㅂ", "ㅆㅂ", "개새끼", "병신", "좆", "지랄"],
    "en": ["fuck", "fucking", "shit", "bitch", "asshole", "bastard"],
    "id": ["anjing", "bangsat", "kontol", "babi", "goblok"],
    "ja": ["くそ", "きさま", "ばか野郎"],
    "zh": ["傻逼", "混蛋", "王八蛋"],
}
_ZW = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_SEP = re.compile(r"[\s\-_.·・~^]+")
_MASKCHAR = re.compile(r"[*#@$%]")
_HOMO = str.maketrans({"о": "o", "а": "a", "е": "e", "і": "i", "ѕ": "s"})
_WILD = "\x00"


def normalize(text: str) -> str:
    """공백·구분자 제거. NFKC 는 호환 자모(ㅅ U+3145)를 조합 자모로 바꾸므로
    사전 어휘에도 반드시 같은 정규화를 적용해야 한다."""
    t = unicodedata.normalize("NFKC", text)
    t = _ZW.sub("", t).casefold().translate(_HOMO)
    t = _MASKCHAR.sub(_WILD, t)          # 별표 마스킹은 와일드카드로 보존
    return _SEP.sub("", t)


# 사전 어휘도 동일 정규화 후 매칭 (자모 분리 우회 대응)
_NORM_BLOCKLIST = {lang: [normalize(w) for w in ws] for lang, ws in BLOCKLIST.items()}
# 마스킹 우회용 패턴: 각 글자가 원문자 또는 와일드카드 어느 쪽이어도 매칭
_MASK_PATTERNS = {
    lang: [re.compile("".join(f"(?:{re.escape(c)}|{_WILD})" for c in w))
           for w in ws if len(w) >= 3]
    for lang, ws in _NORM_BLOCKLIST.items()
}


def blocklist_hit(text: str) -> str | None:
    n = normalize(text)
    for lang, words in _NORM_BLOCKLIST.items():
        for w in words:
            if w and w in n:
                return w
    if _WILD in n:                        # 마스킹이 있을 때만 비싼 검사 수행
        for lang, pats in _MASK_PATTERNS.items():
            for p in pats:
                if p.search(n):
                    return p.pattern[:20]
    return None


# ─────────────────────────────────────────────────────────
# ③ PII — Guardrails 민감정보 필터가 인니어 미지원이라 정규식 백스톱
REGEX_PII = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("CARD", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
    # \b 는 뒤에 한글 조사('로','는')가 붙으면 성립하지 않으므로 lookaround 를 쓴다
    ("PASSPORT", re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d{7,8}(?!\d)")),
    ("PHONE", re.compile(r"(?<!\d)\+?\d[\d\-\s()]{7,15}\d(?!\d)")),
]


def mask_pii(text: str) -> tuple[str, list[str]]:
    found = []
    for label, pat in REGEX_PII:
        text, n = pat.subn(f"[{label}]", text)
        if n:
            found.append(label)
    return text, found


# ─────────────────────────────────────────────────────────
# ① 게이트
GATE_SYSTEM = """You are a request classifier for a multilingual public transit assistant.
The assistant handles ONLY: flights, rail, intercity/city buses, lodging, bike-share,
car-share, station/onboard facilities and rules, and multimodal journey planning.

Respond with JSON only. No explanation, no markdown.
- lang: decide by script, checking in this order.
  1. Any Hangul character present (가-힣) -> ko, even if Hanzi also appears.
  2. Any Kana present (ひらがな/カタカナ) -> ja.
  3. Only Han characters, no Hangul and no Kana -> zh.
  4. Latin script: Indonesian markers (jadwal, dari, ke, saya, apa, berapa,
     bagaimana, tolong, ada, bisa) -> id. Otherwise -> en.
- in_domain=false for tourism itineraries, restaurant/food recommendations, weather,
  currency exchange, immigration rules, politics, medical/legal/financial advice,
  coding, general trivia, or creative writing.
- in_domain=true for questions about riding, boarding, fares, facilities, or rules
  of a transport service, even if not a schedule lookup.
- in_domain=false for a place's crowd level or ongoing local events asked on their own
  (지금 붐비나요, what events are happening) — not a transit-service question.
- intent guide:
  search_rail = train schedules or availability (시간표, 몇 시, schedule, jadwal)
  search_bus / search_flight = same for buses / flights
  get_realtime_status = ONLY for current delays or disruptions happening now
    (지금 지연, 운행 중단, delayed right now). Not for timetables.
  search_lodging = hotels or places to stay (호텔, 숙소, hotel, penginapan).
    The place name is the AREA, not a destination to travel to.
  share_mobility = shared bikes, shared cars, rentals (따릉이, 자전거, 킥보드,
    카셰어링, bike share, sepeda sewa). Never classify these as bus.
  plan_journey = how to get from A to B across multiple modes
  fare_policy = price or discount rules
  search_parking = parking for the user's OWN car (주차, 주차장, parking, parkir).
    NOT share_mobility — this is about leaving your own car somewhere, not renting one.
  search_ev_charger = electric vehicle charging stations (충전, 충전소, EV charger,
    pengisian). NOT share_mobility — this is about charging a car, not renting one.
- For '<place> 근처 X' (near / dekat / 附近), put <place> in origin and leave
  destination empty. The place is a reference point, not a destination.
- toxicity: 0.0 to 1.0. Complaints about delays are NOT toxic. Detect obfuscation
  (separated jamo, inserted spaces, asterisk masking).
- origin/destination/datetime: copy the phrase from the message, or "" if absent.
- pax: number of travellers, 0 if not stated."""

GATE_SCHEMA = {
    "type": "object",
    "required": ["lang", "in_domain", "toxicity", "intent",
                 "origin", "destination", "datetime", "pax"],
    "properties": {
        "lang": {"type": "string", "enum": [*LANGS, "other"]},
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
    },
}


def call_gate(text: str) -> dict:
    payload = {
        "model": GATE_MODEL,
        "messages": [{"role": "system", "content": GATE_SYSTEM},
                     {"role": "user", "content": text}],
        "temperature": 0.0, "max_tokens": 200,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "gate", "schema": GATE_SCHEMA, "strict": True}},
        # 공백 토큰 생성을 막아 마지막 필드에서 루프에 빠지는 것을 방지
        "extra_body": {"guided_decoding_backend": "xgrammar:disable-any-whitespace"},
    }
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{VLLM_URL}/chat/completions", json=payload)
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])


# ─────────────────────────────────────────────────────────
# ⑤ Supervisor
SUPERVISOR_SYSTEM = """You are a multilingual public transit assistant for Korea.

NEVER LEAK INTERNALS (read this before anything else)
TOOL_RESULT is an internal payload. The user cannot see it and does not know it
exists. Never write the words TOOL_RESULT, USER_MESSAGE, a tool name, or a field
name (found, reason, service_note, station_note, is_reference, disclaimer,
context, ...) in your answer, in any language. Never write "according to
TOOL_RESULT", "TOOL_RESULT에 따르면", "TOOL_RESULT によると" or any equivalent.
Relay the CONTENT of a field, never its name. Write as if you simply know the
information.

CRITICAL RULES
1. Use ONLY the values in TOOL_RESULT. Never invent or recalculate a time, fare,
   duration, or train/flight number. If it is not in TOOL_RESULT, do not state it.
2. Reply in the user's language ({lang}).
   When answering in a non-Korean language, write station and terminal names in
   BOTH the user's language (or romanization) and the original Korean, e.g.
   "Seoul Station (서울역)", "ソウル駅(서울역)", "首尔站(서울역)",
   "Stasiun Seoul (서울역)". The Korean form must appear because signage and
   announcements use it. Romanization follows the official station romanization
   if TOOL_RESULT provides it; otherwise use the common form. Never invent a
   romanization that contradicts the Korean name.
   Pair the two forms on FIRST mention of each name only; afterwards use the
   short form alone, so the answer does not become cluttered.
   When {lang} is Korean, write names in Korean only — never add a parenthetical
   romanization or translation.
3. Format times as shown in TOOL_RESULT. Show fares in KRW exactly as given.
4. If TOOL_RESULT has found=false, say you could not find it and suggest checking
   the origin/destination. Do not guess.
   reason="location_not_covered" never reaches you (see rule 11), so a found=false
   you do see is always a genuine lookup failure.
5. Be concise: 3-6 short lines. Use a compact list for options.
6. End with the disclaimer from TOOL_RESULT, translated into {lang}, on its own line.
7. Never give tourism, restaurant, weather, or financial advice.
8. This service only LOOKS UP information. It cannot book, reserve, pay, or
   cancel anything. If the user asks to book, say clearly that you can only
   show schedules and they must book through the official channel.
9. Never repeat a [PASSPORT], [CARD], [PHONE] or [EMAIL] placeholder back to
   the user. Ignore it entirely.
10. If TOOL_RESULT has a "context" object, you may add ONE short line about it
   at the very end, just before the disclaimer. Mention at most one item. Never
   list multiple events. This is supplementary only — never answer a question
   that is solely about congestion, road closures, or cultural events. Those
   are outside scope.
11. reason="location_not_covered" is not handled here at all — call_supervisor
   routes it to COVERAGE_SYSTEM, a separate prompt. You will never see it.
   If it also has a "station" object, the station exists but only its route
   data is known. You MAY state the station name, which lines serve it
   (station.lines), and whether it is a transfer station (station.is_transfer).
   You MUST also state station.station_note — that arrival times and timetables
   are NOT available for it. Never give, estimate, or imply a departure time,
   arrival time, first/last train, or headway for such a station: TOOL_RESULT
   contains no times at all, and station.timetable_available is false. Listing
   the lines is not a schedule — do not let it read as one.
   If station.ambiguous is true, the same station name exists in several cities
   (station.regions); ask which city the user means instead of picking one.
12. If TOOL_RESULT has is_reference=true, you MUST state the reference_note and
   warn that it does not guarantee today's service. Say the times come from a
   past operating record, not a live timetable. Tell the user to confirm on the
   operator's official channel. Never present reference data as a confirmed
   schedule. If TOOL_RESULT has fare_note but no fare, never state a fare —
   relay fare_note instead.
13. When {lang} is NOT Korean and the answer actually contains station or terminal
   names or a route, add ONE short sentence suggesting the user save or screenshot
   the answer, since the Korean names can be shown to station staff. Exactly one
   sentence, placed immediately before the disclaimer line. Never add it to a
   Korean answer, to a refusal or out-of-scope answer, or to a found=false /
   location_not_covered / error answer — those have no names worth keeping."""


# 커버리지 밖 응답 전용 프롬프트.
#
# 왜 분리했는가: 이 답변은 내용이 이미 정해져 있다(service_note + 선택적으로
# station). 그런데 13개 규칙짜리 SUPERVISOR_SYSTEM 안에 "대체 서비스를
# 권하지 말 것"을 넣어 두면 Haiku 가 지키지 못한다 — 규칙을 강화하고 위치를
# 올려 봐도 세 문항 중 어느 하나는 매번 "대전시 홈페이지나 앱에서 확인해
# 보세요" 를 덧붙였다(실측). 도우려는 성향이 부정 제약을 이긴다.
# 할 일이 하나뿐인 짧은 프롬프트로 바꾸면 덧붙일 여지 자체가 없어진다.
COVERAGE_SYSTEM = """You do ONE job: state, in {lang}, that a service does not
operate in the area the user asked about. Nothing else.

Write one or two sentences in {lang}, based on service_note and the place name in
"requested". That is the entire answer.

If TOOL_RESULT contains a "station" object, you may add: the station name, which
lines serve it (station.lines), whether it is a transfer station
(station.is_transfer), and then station.station_note. Never give, estimate or
imply any departure time, arrival time, first/last train or headway — there are
no times in TOOL_RESULT at all. Listing lines is not a schedule. If
station.ambiguous is true, the same name exists in several cities
(station.regions) — ask which city the user means instead of picking one.

Forbidden, without exception:
  - any other operator, brand, service or app
  - any city/government website, office, hotline or "local app"
  - any alternative location, nearby area, or other way to look it up
  - any offer of further help, and any closing disclaimer line
  - the words TOOL_RESULT, service_note, station_note, or any other field name
Adding a helpful-sounding suggestion is a worse failure than a curt answer: you
cannot verify that it serves that area.

Reply in {lang} only."""


def call_supervisor(user_text: str, tool_result: dict, lang: str) -> str:
    # 커버리지 밖은 전용 프롬프트로 보낸다 (위 주석 참고).
    system = (COVERAGE_SYSTEM if tool_result.get("reason") == "location_not_covered"
              else SUPERVISOR_SYSTEM)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 700,
        "temperature": 0.2,
        "system": system.format(lang=lang),
        "messages": [{"role": "user", "content":
                      f"USER_MESSAGE:\n{user_text}\n\n"
                      f"TOOL_RESULT:\n{json.dumps(tool_result, ensure_ascii=False, indent=1)}"}],
    }
    resp = bedrock.invoke_model(modelId=CLAUDE_MODEL, body=json.dumps(body))
    data = json.loads(resp["body"].read())
    return "".join(b.get("text", "") for b in data.get("content", []))


# ─────────────────────────────────────────────────────────
# ⑥ 검증 — contextual grounding 이 ko/zh/ja/id 미지원이라 결정론적 대조로 대체
def verify(answer: str, tool_result: dict) -> tuple[bool, list[str]]:
    src = json.dumps(tool_result, ensure_ascii=False)
    src_digits = re.sub(r"[,\s]", "", src)
    unsupported = []
    for tok in set(re.findall(r"\d{1,2}:\d{2}|\d[\d,]{2,}", answer)):
        if re.sub(r"[,\s]", "", tok) not in src_digits:
            unsupported.append(tok)
    return (not unsupported), unsupported


# ─────────────────────────────────────────────────────────
# ⑦ Guardrail
def apply_guardrail(text: str, source: str) -> tuple[bool, str]:
    if not GUARDRAIL_ID:
        return True, text
    r = bedrock.apply_guardrail(
        guardrailIdentifier=GUARDRAIL_ID, guardrailVersion=GUARDRAIL_VERSION,
        source=source, content=[{"text": {"text": text}}])
    if r["action"] == "GUARDRAIL_INTERVENED":
        outs = r.get("outputs", [])
        return False, outs[0]["text"] if outs else ""
    return True, text


# ─────────────────────────────────────────────────────────
def handle(text: str, verbose: bool = False) -> dict:
    t0 = time.perf_counter()
    trace = {}

    # ① 게이트
    g = call_gate(text)
    lang = L(g.get("lang", "en"))
    trace["gate"] = g
    trace["gate_ms"] = round((time.perf_counter() - t0) * 1000)

    # ② 금지어
    hit = blocklist_hit(text)
    if hit:
        return {"route": "blocked", "reason": f"blocklist:{hit}", "lang": lang,
                "answer": WARNING[lang], "trace": trace,
                "total_ms": round((time.perf_counter() - t0) * 1000)}

    if g.get("toxicity", 0) >= 0.6:
        return {"route": "blocked", "reason": "toxicity", "lang": lang,
                "answer": WARNING[lang], "trace": trace,
                "total_ms": round((time.perf_counter() - t0) * 1000)}

    # ③ PII 마스킹 — 클라우드 전송 전
    masked, pii = mask_pii(text)
    trace["pii_masked"] = pii

    # ⑦-in Guardrail 입력 검사
    ok, _ = apply_guardrail(masked, "INPUT")
    if not ok:
        return {"route": "blocked", "reason": "guardrail_input", "lang": lang,
                "answer": REFUSAL[lang], "trace": trace,
                "total_ms": round((time.perf_counter() - t0) * 1000)}

    if not g.get("in_domain"):
        return {"route": "blocked", "reason": "out_of_domain", "lang": lang,
                "answer": REFUSAL[lang], "trace": trace,
                "total_ms": round((time.perf_counter() - t0) * 1000)}

    # 예약·결제 요청은 조회 서비스 범위 밖 — 도구 호출 전에 분기한다
    low = text.casefold()
    if any(w.casefold() in low for w in BOOKING_WORDS):
        return {"route": "booking_unsupported", "reason": "booking_request", "lang": lang,
                "answer": BOOKING_ONLY[lang], "trace": trace,
                "total_ms": round((time.perf_counter() - t0) * 1000)}

    # ④ 도구 호출
    slots = {"origin": g.get("origin") or None, "destination": g.get("destination") or None,
             "datetime": g.get("datetime") or None, "pax": g.get("pax") or None}
    intent = g["intent"]
    if intent not in tools.TOOL_MAP:
        if slots["origin"] and slots["destination"]:
            intent = "plan_journey"
        else:
            return {"route": "blocked", "reason": f"no_tool:{intent}", "lang": lang,
                    "answer": REFUSAL[lang], "trace": trace,
                    "total_ms": round((time.perf_counter() - t0) * 1000)}
    tr = tools.call_tool(intent, slots)
    trace["tool"] = intent
    trace["tool_found"] = tr.get("found")

    if not tr.get("found") and tr.get("reason") != "location_not_covered":
        return {"route": "tool_empty", "reason": tr.get("reason"), "lang": lang,
                "answer": NOT_FOUND[lang], "trace": trace,
                "total_ms": round((time.perf_counter() - t0) * 1000)}

    # location_not_covered 는 found=false 지만 Supervisor 로 넘겨 service_note 를
    # 사용자 언어로 안내하게 한다 (SUPERVISOR_SYSTEM 규칙 11) — 여기서 일반
    # NOT_FOUND 문구로 조기 반환하면 서비스 커버리지 안내가 나가지 않는다.

    # ⑤ Supervisor
    answer = call_supervisor(masked, tr, lang)

    # ⑥ 검증
    grounded, bad = verify(answer, tr)
    trace["grounded"] = grounded
    if not grounded:
        trace["unsupported_numbers"] = bad
        answer = call_supervisor(
            masked + "\n\n[SYSTEM] Your previous answer contained numbers not present "
                     "in TOOL_RESULT. Rewrite using only values from TOOL_RESULT.", tr, lang)
        grounded, bad = verify(answer, tr)
        trace["grounded_retry"] = grounded

    if not grounded:
        # 재생성 후에도 근거 없는 숫자가 남으면 답변을 폐기한다.
        # 잘못된 시각·요금 안내는 민원으로 직결되므로 침묵이 안전하다.
        return {"route": "ungrounded", "reason": "hallucinated_numbers", "lang": lang,
                "answer": NOT_FOUND[lang], "trace": trace,
                "total_ms": round((time.perf_counter() - t0) * 1000)}

    # ⑦ Guardrail 출력 검사
    ok, masked_out = apply_guardrail(answer, "OUTPUT")
    if not ok:
        return {"route": "blocked", "reason": "guardrail_output", "lang": lang,
                "answer": WARNING[lang], "trace": trace,
                "total_ms": round((time.perf_counter() - t0) * 1000)}

    return {"route": "answered", "lang": lang, "answer": answer, "trace": trace,
            "total_ms": round((time.perf_counter() - t0) * 1000)}


# ─────────────────────────────────────────────────────────
DEMO = [
    ("ko", "서울역에서 부산역 가는 KTX 오늘 오후 시간표 알려줘"),
    ("en", "What trains go from Seoul Station to Busan this afternoon?"),
    ("id", "Jadwal kereta dari Stasiun Seoul ke Busan sore ini apa saja?"),
    ("ja", "ソウル駅から釜山駅までのKTXの時刻表を教えてください"),
    ("zh", "首尔站到釜山站的KTX今天下午有哪些班次？"),
    ("ko", "인천공항에서 제주 가는 항공편 내일 아침"),
    ("ko", "동서울터미널에서 속초 가는 버스"),
    ("ko", "강남역 근처 따릉이 대여소 어디 있어?"),
    ("ko", "제주도 맛집 좀 추천해줘"),                    # 차단 기대
    ("en", "Plan me a 3 day itinerary for Busan"),        # 차단 기대
    ("ko", "제 여권번호 M12345678로 예약해줘. 서울역에서 부산역"),  # PII 마스킹
]


def run_demo():
    ok = blocked = fail = 0
    for lang, text in DEMO:
        print(f"\n{'─' * 66}")
        print(f"[{lang}] {text}")
        try:
            r = handle(text)
        except Exception as e:
            print(f"  ERROR: {e}")
            fail += 1
            continue
        tr = r["trace"]
        tag = {"answered": "응답", "blocked": "차단", "tool_empty": "미조회",
               "ungrounded": "환각차단", "booking_unsupported": "예약불가"}[r["route"]]
        print(f"  → [{tag}] {r.get('reason','')}  "
              f"lang={r['lang']} gate={tr.get('gate_ms')}ms total={r['total_ms']}ms "
              f"grounded={tr.get('grounded_retry', tr.get('grounded'))}"
              + (" (재생성)" if 'grounded_retry' in tr else ""))
        if tr.get("pii_masked"):
            print(f"  PII 마스킹: {tr['pii_masked']}")
        print("  " + r["answer"].replace("\n", "\n  ")[:700])
        if r["route"] == "answered":
            ok += 1
        elif r["route"] == "blocked":
            blocked += 1
    print(f"\n{'═' * 66}")
    print(f" 응답 {ok} / 차단 {blocked} / 실패 {fail}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if not GUARDRAIL_ID:
        print("경고: GUARDRAIL_ID 미설정 — 가드레일 검사가 생략됩니다", file=sys.stderr)

    if a.demo:
        run_demo()
    elif a.serve:
        print("교통 안내 챗봇 (종료: quit)")
        while True:
            try:
                q = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in ("quit", "exit", "종료"):
                break
            if not q:
                continue
            r = handle(q)
            print(f"\n{r['answer']}")
            print(f"\n[{r['route']} · {r['lang']} · {r['total_ms']}ms]")
    elif a.text:
        r = handle(a.text, a.verbose)
        print(r["answer"])
        print(f"\n[{r['route']} · {r['lang']} · {r['total_ms']}ms]")
        if a.verbose:
            print(json.dumps(r["trace"], ensure_ascii=False, indent=2))
    else:
        ap.print_help()
