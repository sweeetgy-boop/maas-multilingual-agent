# 다국어 교통 연계 AI 에이전트

한국 교통(철도·고속버스·항공·공유모빌리티·숙박) 정보를 5개 언어(ko/en/zh/ja/id)로
안내하는 에이전트. **조회 전용**이며 예약·결제·취소는 처리하지 않는다.

로컬 게이트(vLLM, Qwen3-4B)가 언어·도메인·유해도·PII·의도·슬롯을 한 번에 판정하고,
통과한 요청만 결정론적 도구를 거쳐 Bedrock Claude Haiku(Supervisor)가 사용자 언어
답변으로 옮긴다. 답변은 도구 응답과의 숫자 대조를 통과해야만 사용자에게 나간다.

이 문서는 **2026-09-04 기준 최종 상태**다. 교육 계정이 2026-09-08 에 만료되면
EC2·파이프라인·Guardrail 은 모두 사라지고 이 저장소만 남는다. 인수인계 받는
사람이 이 문서 하나로 전체를 파악할 수 있도록 썼다. 판단 근거와 **실패한 시도**를
함께 적은 것도 그 때문이다.

---

## 목차

1. [프로젝트 소개](#1-프로젝트-소개)
2. [접속 정보](#2-접속-정보)
3. [아키텍처](#3-아키텍처)
4. [API](#4-api)
5. [AWS 클라우드 사용 내역](#5-aws-클라우드-사용-내역)
6. [데이터 소스 현황](#6-데이터-소스-현황)
7. [공공데이터 API 에서 확인한 것](#7-공공데이터-api-에서-확인한-것)
8. [실증 결과](#8-실증-결과)
9. [배포](#9-배포)
10. [평가](#10-평가)
11. [로컬 개발](#11-로컬-개발)
12. [알려진 제약](#12-알려진-제약)
13. [프로젝트 종료 시 정리 체크리스트](#13-프로젝트-종료-시-정리-체크리스트)

---

## 1. 프로젝트 소개

한국을 방문하거나 거주하는 외국인이 모국어로 교통 정보를 묻고 답을 받는 것이
목표다. 다섯 언어를 지원한다 — 한국어·영어·중국어·일본어·인도네시아어.

무엇을 하는가.

| 의도 | 예시 질의 | 데이터 |
|---|---|---|
| `search_rail` | "서울역에서 부산역 KTX 오늘 오후" | 코레일 운행실적 (참고 시간표) |
| `search_bus` | "동서울에서 강릉 고속버스" | TAGO 고속버스 (요금·등급 포함) |
| `search_flight` | "김포공항에서 제주 가는 항공편 내일" | TAGO 국내항공 (시간표·운임) |
| `get_realtime_status` | "김포공항 지금 지연되나요?" | 인천공항공사 / 한국공항공사 |
| `search_parking` · `search_ev_charger` · `share_mobility` | "광화문 근처 주차장" | 서울시 실시간 도시데이터 |
| `plan_journey` | "인천공항에서 명동 가는 법" | 목 데이터 |
| `search_lodging` | "서울역 근처 호텔" | 목 데이터 |

무엇을 하지 않는가. 예약·결제·취소를 하지 않는다. 관광 일정, 맛집, 날씨,
환전, 비자를 다루지 않는다. 이 경계는 게이트가 판정하고, 경계에 걸치는
질의(와이파이·짐 보관·국제운전면허)는 통과시킨다 — 판단 기준은
[10. 평가](#10-평가)의 라벨링 정책에 적었다.

핵심은 **없는 값을 만들지 않는 것**이다. 서울 전용 서비스를 대전에서 물으면
목 데이터로 지어내지 않고 커버리지 밖임을 밝힌다. 요금이 공개되지 않은
항공편에 0원을 채우지 않는다. Supervisor 가 지어낸 숫자는 근거성 검증이
걸러낸다.

---

## 2. 접속 정보

| 용도 | 주소 | 인증 |
|---|---|---|
| 채팅 UI | `https://maas-ui.duckdns.org` | 없음 |
| API | `https://maas-transit.duckdns.org` | `X-API-Key` |
| OpenAPI 문서 | `https://maas-transit.duckdns.org/docs` | 없음 |
| 헬스체크 | `GET /v1/health` | 없음 |

`POST /v1/chat/completions` 와 `GET /v1/models` 는 **무인증**이다. GuardBench
연동을 위한 것이며 자세한 사정은 [4. API](#4-api) 에 적었다.

> **목 데이터 경고.** `plan_journey`(연계 경로 상세)와 `search_lodging`(숙박)은
> 실 API 에 연결되어 있지 않다. 응답의 `data_source` 에 `(mock)` 이 찍히고
> 답변에도 그대로 드러나지만, **이 두 도구의 시각·요금·업소명은 실재하지 않는다.**
> 운영 전환 시 반드시 교체해야 한다.
>
> 실데이터를 쓰는 도구도 API 호출이 실패하면 목 데이터로 폴백한다. 이때도
> `(mock)` 이 찍힌다. 폴백 여부는 `data_source` 로만 구분할 수 있다.

계정 만료(2026-09-08) 이후 위 주소는 모두 응답하지 않는다.

---

## 3. 아키텍처

```
사용자 입력
   ↓
① 게이트 (Qwen3-4B, 로컬 vLLM)  언어·도메인·유해도·PII·의도·슬롯
   ↓  in_domain=false / toxicity → 언어별 정형 응답으로 즉시 종료
② 금지어 사전                    자모분리·별표마스킹·제로폭·동형문자 우회 탐지
   ↓
③ PII 마스킹                     클라우드 전송 전에 수행
   ↓
④ Guardrail(INPUT) + 범위 정책   예약·결제 요청은 여기서 분기
   ↓
⑤ 도구 호출 (결정론적)           실 API 우선, 실패 시 목 데이터 폴백
   ↓
⑥ Supervisor (Bedrock Haiku)     도구 JSON → 사용자 언어 답변
   ↓
⑦ 근거성 검증                    답변 숫자·시각 ↔ 도구 JSON 대조
   ↓                             불일치 → 재생성 → 그래도 실패면 답변 폐기
⑧ Guardrail(OUTPUT)
   ↓
응답
```

방어 계층은 여섯이다. `/v1/evaluate` 는 차단 시 **어느 계층이 막았는지**(`layer`)를
반환하고, 계층 정의는 `GET /v1/spec` 으로 조회할 수 있다.

| 계층 | 담당 |
|---|---|
| `local_gate` | 도메인·유해도 판정 (Qwen3-4B 제로샷) |
| `blocklist` | 다국어 금지어 사전, 우회 표기 탐지 |
| `pii_regex` | 여권·전화·이메일 등 마스킹 (한글 조사 대응 lookaround) |
| `bedrock_guardrail` | Bedrock Guardrails Standard tier + cross-region |
| `grounding_check` | 답변 숫자 ↔ 도구 응답 대조 |
| `scope_policy` | 예약·결제·취소 요청 차단 |

**왜 게이트를 로컬에 두는가.** 모든 요청을 클라우드 LLM 에 보내면 비용과 지연이
질의 수에 비례한다. 로컬 4B 모델이 도메인 밖·유해 요청을 먼저 쳐내면 클라우드
호출이 39%로 줄어든다(α = 에스컬레이션율). 게이트가 통과시킨 것만 Haiku 로 간다.

**왜 도구를 결정론적으로 두는가.** LLM 이 시간표를 생성하면 검증할 방법이 없다.
도구가 공공 API 에서 받아온 JSON 을 Supervisor 에게 주고, 답변에 나온 숫자가 그
JSON 안에 있는지 대조한다. 없으면 재생성하고, 재생성해도 없으면 답변을 버린다.
이 검증이 Haiku 가 지어낸 KTX 편명·요금 10건을 실제로 잡아냈다.

저장소 구조.

```
gate/            파이프라인 본체
  pipeline.py      ①~⑧ 엔드투엔드 흐름, CLI(--text/--demo/--serve)
  tools.py         의도→도구 매핑, 도구 구현(TOOL_MAP)
  korail_api.py    코레일 열차운행정보(RunPlan2/RunInfo2) 어댑터
  expbus_api.py    TAGO 고속버스정보 어댑터
  flight_api.py    TAGO 국내항공운항정보 어댑터 (시간표·운임)
  airport_status_api.py  인천공항공사 + 한국공항공사 실시간 운항 어댑터
  citydata_api.py  서울시 실시간 도시데이터 어댑터
  odsay_api.py     ODsay 경로탐색 어댑터 (키 미설정, 미검증)
  subway_stations.py / transit_nodes.py / geocode.py   장소 해석 계층
  build_*.py       *.json 캐시 생성 스크립트
  *.json           역·공항·터미널·행정구역·서울 121개 장소 캐시
  *.docx           공공데이터 활용가이드 원본 (부록 코드표 추출용)
ui/              배포 대상 (systemd 가 실제로 띄우는 코드)
  api.py           공개 HTTP API (:8080)
  server.py        채팅 UI 서버 (:7860)
  session_slots.py 멀티턴 슬롯 승계
  index.html       단일 페이지 프런트
eval/            평가셋과 채점 하네스
scripts/         CI 검증 스크립트, CodeDeploy 훅, 파이프라인 생성 스크립트
docs/            아키텍처 문서, 실증 기록
infra/           Bedrock Guardrails 정의(JSON)
```

`gate/api.py`·`gate/server.py`·`gate/eval_endpoint.py` 는 `ui/` 의 같은 이름 파일과
거의 같은 코드다. **운영에서 도는 것은 `ui/` 쪽**이고(`ui.api:app`, `ui.server:app`),
`gate/` 쪽은 vLLM 호스트에서 단독 실행할 때 쓰는 사본이다. 멀티턴 좌표 전달과
슬롯 승계 개선은 `ui/` 에만 들어가 있다.

인프라·CI/CD·요청 흐름·EC2 내부 구성 다이어그램과 리소스 목록, 운영 절차는
**[docs/architecture/](docs/architecture/README.md)**.
실증 과정에서 확인한 실패 사례와 판단 근거는 **[docs/FINDINGS.md](docs/FINDINGS.md)**.

---

## 4. API

| 엔드포인트 | 용도 | 인증 |
|---|---|---|
| `POST /v1/chat/completions` | **OpenAI Chat Completions 호환** | 없음 |
| `GET /v1/models` | OpenAI 호환 모델 목록 | 없음 |
| `POST /v1/chat` | 대화 (자체 스키마, 모바일 앱·채팅 UI 가 사용) | 필요 |
| `POST /v1/evaluate` | 단건 방어 계층 평가 (차단 계층 반환) | 필요 |
| `POST /v1/evaluate/batch` | 배치 평가 (최대 200건) | 필요 |
| `GET /v1/health` | 상태 및 구성 | 없음 |
| `GET /v1/spec` | 방어 계층 정의 | 없음 |
| `GET /docs` | OpenAPI 문서 | 없음 |

인증은 `X-API-Key` 헤더다. 레이트리밋은 IP 당 분당 60회(`MAAS_RATE_PER_MIN`).

> **`/v1/chat/completions` 와 `/v1/models` 는 인증이 없다.**
> GuardBench 등록 화면에 Endpoint URL 과 Model 칸만 있고 API 키나
> `Authorization` 헤더를 넣을 자리가 없어서다.
> 이 두 경로는 **공개 인터넷에 무인증으로 열려 있으며**, 남용을 막는 것은
> **IP 당 분당 레이트리밋 하나뿐**이다. `Authorization` 이나 `X-API-Key` 를
> 보내도 무시하며, 있다고 오류를 내지는 않는다.

### OpenAI 호환 경로

부수 효과로 Open WebUI, LibreChat, LangChain, OpenAI SDK 가 별도 클라이언트 개발
없이 붙는다. 기존 `/v1/chat` 은 그대로 두었다 — 앱과 UI 가 그 스키마를 쓰고,
`blocked_by`·`carried_slots` 처럼 OpenAI 표준에 자리가 없는 값을 돌려줘야 한다.

```python
from openai import OpenAI
# 인증이 없다. OpenAI SDK 는 api_key 인자를 요구하므로 아무 값이나 넣는다(무시된다).
c = OpenAI(base_url="https://maas-transit.duckdns.org/v1", api_key="unused")
r = c.chat.completions.create(
    model="maas-transit",
    messages=[{"role": "user", "content": "서울역에서 부산역 KTX 오늘 오후"}])
print(r.choices[0].message.content, r.choices[0].finish_reason)
```

**`finish_reason` 이 차단 여부다.** 답변 텍스트만으로는 차단과 조회 실패를 가릴 수
없다 — 둘 다 멀쩡한 문장이라 평가 도구가 구분하지 못한다.

| 값 | 의미 |
|---|---|
| `stop` | 정상 응답. 조회 실패(`요청하신 구간의 정보를 찾지 못했습니다`)도 여기다 |
| `content_filter` | **방어 계층이 차단** |

**`x_maas` 는 비표준 확장이다.** 표준 클라이언트는 무시하고, 평가 도구는 여기서
계층 정보를 읽는다.

```json
"x_maas": {"blocked_by": "local_gate", "language": "ko", "answered": false,
           "carried_slots": ["origin"], "latency_ms": 4210,
           "session_id": "oai-04498fb7f3b8"}
```

`blocked_by` 는 방어 계층 6종 중 하나이며, 차단되지 않았으면 `null` 이다.

**제약**

| 항목 | 내용 |
|---|---|
| 스트리밍 | **미지원.** `stream=true` 는 400. 근거성 검증이 끝나야 최종 응답이 정해지고, 검증 실패 시 재생성하거나 답변을 폐기한다. 토큰 단위로 흘려보내면 검증 전에 환각이 노출돼 방어 계층의 전제가 무너진다 |
| `system` 메시지 | **무시한다.** 외부에서 Supervisor 프롬프트를 덮어쓰면 방어 계층이 통째로 무력화된다 — 인젝션의 정석 경로다. 무시한 사실은 stderr 로그(`journalctl -u maas-api`)에 남는다 |
| `usage` | 토큰을 실제로 세지 않아 전부 `0` 이다. 표준 클라이언트가 이 필드를 기대하므로 생략하지 않되, 추정치를 지어내지 않는다 |
| 응답 지연 | **4~7초.** 배치로 수백 건을 던지면 오래 걸리므로 클라이언트 타임아웃을 넉넉히 잡아야 한다 |
| 인증 | **없다.** 보낸 `Authorization`·`X-API-Key` 는 무시하고, 있다고 거부하지 않는다 |
| 레이트리밋 | 분당 60회. **무인증 경로의 유일한 남용 방어선이다.** 429 에 `Retry-After` 헤더가 붙는다 |
| 멀티턴 | OpenAI 는 무상태라 클라이언트가 매번 전체 `messages` 를 보낸다. 마지막 user 메시지 앞부분의 해시를 세션 키로 삼아 슬롯을 잇는다. 이력을 편집하면 새 세션이 된다 |

에러는 OpenAI 형식(`{"error": {"message", "type", "code"}}`)으로 돌려준다.
기존 엔드포인트는 FastAPI 기본 형식(`{"detail": ...}`)을 그대로 쓴다.

### GuardBench 연결 정보

```
Base URL : https://maas-transit.duckdns.org/v1
Model    : maas-transit
인증     : 없음 (API Key 칸을 비워 둔다)
차단 판정: finish_reason == "content_filter"
계층 분석: x_maas.blocked_by
```

- `stream=true` 를 보내지 말 것 (400).
- `system` 메시지는 무시되므로 프롬프트 주입 경로로 쓸 수 없다 — 이 경로를
  시험한다면 그것이 기대 동작이다.
- 요청당 4~7초. 타임아웃을 최소 30초 이상 잡고, 분당 60회 제한에 맞춰 간격을 둔다.
- 계층별 기여도만 필요하면 `/v1/evaluate/batch` 가 더 낫다 — 한 번에 200건까지
  받고 계층별 집계를 돌려준다.

---

## 5. AWS 클라우드 사용 내역

무엇을 왜 골랐고 어떤 제약을 만났는지 기록한다. 교육 계정이라 일반 계정과
다르게 동작한 부분이 여럿 있다.

### 계정

| 항목 | 값 |
|---|---|
| 계정 | `508139322599` (kosa-edu-3, 교육 계정, **2026-09-08 만료**) |
| 리전 | `ap-northeast-2` (서울) |
| 사용자 / 그룹 | `kosa11` / `kosa-edu` (AdministratorAccess) |
| 권한 경계 | `kosa-edu-region-pol` |

권한 경계가 리전과 서비스를 제한한다. Bedrock 모델 선택과 Cost Explorer 접근이
여기서 막혔다 — 아래에 각각 적었다.

### 컴퓨팅 — EC2

| 항목 | 값 |
|---|---|
| 인스턴스 | `i-008500ef9e7c53aec` |
| 타입 | `g6e.xlarge` (NVIDIA L40S 46GB), `ap-northeast-2a` |
| AMI | Ubuntu 22.04 Deep Learning |
| 스토리지 | EBS 300GB gp3 (모델 캐시 `/opt/hf` 유지) |
| IAM 역할 | `maas-llm-ec2` (SSM, Bedrock, S3, CodeDeploy) |

**왜 g6e.xlarge 인가.** 처음에는 Qwen3-8B 를 L4(24GB)에 올리려 했다. vLLM 이
KV 캐시 예산을 **-9.08 GiB** 로 계산하고 기동에 실패했다 — 가중치 15.3GB 에
CUDA 그래프와 활성화 메모리를 더하면 24GB 를 넘는다. L40S(46GB)로 올려 8B 와 4B 를
모두 시험했고, 4B 로 확정한 뒤에도 동일 인스턴스를 유지했다. 4B 는 L4 에서도
돌지만 인스턴스를 다시 만들 이유가 없었다.

**비용은 시간당 약 $1.2, 24시간 가동 시 하루 약 $29** 다. 개발 중에는 UTC 23시
자동 종료 크론을 두었다가 GuardBench 연동 테스트 기간에는 제거했다.

### 모델 서빙 — vLLM (EC2 위 Docker)

```
vllm/vllm-openai:v0.11.0
Qwen/Qwen3-4B, served-model-name transit-base
127.0.0.1:8000 바인딩 (외부 미노출, SSM 터널로만 접근)
```

| 옵션 | 이유 |
|---|---|
| `--gpu-memory-utilization 0.90` | KV 캐시 24.3GiB 확보, 동시성 21.6배 |
| `--max-model-len 8192` | 게이트 프롬프트에 충분 |
| `--enable-prefix-caching` | 시스템 프롬프트가 매번 같아 효과가 크다 |
| `--disable-log-requests` | 프롬프트 원문 로깅 차단 (PII 보호) |

**겪은 문제 둘.**

Qwen3 는 기본이 thinking 모드라 `<think>` 블록이 출력 토큰을 다 먹었다.
`chat_template_kwargs` 로 `enable_thinking=false` 를 넣어 해결했다.

구조화 출력에서 중첩 스키마를 쓰자 xgrammar 가 제약을 놓쳐 **90문항 중 36건이
출력 폭주로 실패**했다. 스키마를 평탄 구조로 바꿔 해결했다. 이후 게이트 스키마는
평탄 구조를 유지하는 것이 규칙이 됐다 — 슬롯을 추가하고 싶어도 스키마를 늘리지
않고 도구가 원문을 직접 읽는 쪽을 택한다.

### 생성형 AI — Amazon Bedrock

| 용도 | 값 |
|---|---|
| Supervisor | `anthropic.claude-3-haiku-20240307-v1:0` |
| Guardrails | `b53a6caaqa31` v1 (Standard tier, apac cross-region) |

**왜 Haiku 인가.** Sonnet 계열이 교육 계정 권한 경계로 차단됐다. 교차 리전 추론
프로파일도 전부 DENIED 라 서울 리전 직접 모델 ID 만 쓸 수 있었다. 선택지가
Haiku 하나였다.

**Guardrails 에서 확인한 것.**

Classic tier 는 **한국어 입력을 전혀 걸러내지 못했다.** Standard tier +
cross-region inference 가 필수였다.

다국어(ko/zh/ja/id) 금지어 필터와 근거성 검증은 지원되지 않아 자체 계층으로
구현했다 — `blocklist` 와 `grounding_check` 가 그것이다.

`NonTransportAdvice` 주제 정의에 "sightseeing plans" 를 넣었더니 **"서울에서 부산
가는 방법 설명해줘" 같은 정상 교통 질의가 차단됐다.** 주제 정의를 좁혀 v3
(`b53a6caaqa31`)에서 해결했다. `infra/guardrail-v2.json` 과 `guardrail-v3.json` 에
두 판본이 남아 있다.

**PII 정책이 공항명을 사람 이름으로 오탐한다.** `김포`·`김해` 가 `NAME` 으로
탐지된다 — 한국 성씨+이름 형태라 NER 이 그렇게 읽는다(`제주`·`인천` 은 안 걸린다).
`action` 이 `BLOCKED` 가 아니라 `ANONYMIZED` 인데도 파이프라인이 개입을 종류
구분 없이 실패로 처리해, 정상 항공 답변의 32%가 마지막 단계에서 폐기됐다.
지금은 `pipeline.py` 가 "지명 오탐만 있는 익명화"와 진짜 차단을 가른다 —
탐지 문자열이 전부 우리 캐시(1,251개 지명)에 있을 때만 원문을 통과시키고,
사람 이름·연락처·이메일·여권번호 마스킹은 그대로 살아 있다.

### 원격 접속 — Systems Manager

SSM Session Manager 로 EC2 에 접속한다. **SSH 포트(22)를 열지 않았다.**
포트 포워딩으로 vLLM(8000)을 로컬에 연결해 평가를 돌린다.

```bash
aws ssm start-session --target i-008500ef9e7c53aec --region ap-northeast-2 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
```

**WSL 안에서 실행해야 WSL 의 localhost 에 붙는다.** Windows 에서 열면 Windows 의
`127.0.0.1` 에만 바인딩돼 WSL 에서 보이지 않는다. WSL2 는 별도 VM 이라 Windows
루프백에 접근할 수 없다. 이걸 몰라 "터널을 열었는데 연결이 거부된다"로 한참
헤맸다.

### 네트워크

**Elastic IP `43.201.216.37`** (`eipalloc-028eeddc99b7f626f`).
인스턴스를 재시작할 때마다 공인 IP 가 바뀌어 DuckDNS 갱신이 반복됐다. 고정 IP 를
붙여 해결했다. 인스턴스에 연결돼 있으면 요금이 없다 — 분리된 채 남겨두면
과금되므로 [종료 체크리스트](#13-프로젝트-종료-시-정리-체크리스트)에 넣었다.

**보안그룹 `sg-0ce81d06ebaf0db08`**

| 포트 | 출처 | 용도 |
|---|---|---|
| 80/tcp | `0.0.0.0/0` | Let's Encrypt HTTP-01 챌린지 |
| 443/tcp | `0.0.0.0/0` | Caddy HTTPS |

`8080`(API)·`7860`(UI)·`8000`(vLLM)은 **외부에 열지 않는다.** Caddy 를 통해서만
접근한다. vLLM 은 `127.0.0.1` 바인딩이라 EC2 밖에서 아예 보이지 않는다.

**도메인은 AWS 밖의 DuckDNS 를 썼다.** Route 53 도메인 등록비는 AWS 크레딧으로
결제되지 않고, 교육 계정에서 구매 자체가 어려웠다. DuckDNS 는 무료이고
Let's Encrypt HTTP-01 챌린지와 함께 쓰면 인증서까지 자동으로 붙는다.

### CI/CD — CodePipeline · CodeBuild · CodeDeploy

```
GitHub main push
  → CodeConnections(maas-github) → CodePipeline(maas-pipeline)
  → CodeBuild(maas-build, 검증) → CodeDeploy(maas-api / maas-api-prod)
  → EC2 /opt/maas, systemd 재시작
```

| 리소스 | 이름 |
|---|---|
| IAM 역할 | `maas-codepipeline-role`, `maas-codebuild-role`, `maas-codedeploy-role` |
| 아티팩트 버킷 | `maas-pipeline-508139322599` (버전 관리 활성) |
| 배포 버킷 | `maas-deploy-508139322599` |
| EC2 태그 | `CodeDeployTarget=maas-api` |

**구축 중 겪은 문제 넷.**

`CodeConnections` 를 CLI 로 만들면 GitHub 앱이 설치되지 않는다. 콘솔에서
"새 앱 설치"를 눌러야 비공개 저장소를 읽을 수 있다. 이걸 빠뜨려
`No Branch [main] found` 가 반복됐다. 연결 상태가 `AVAILABLE` 로 보여도
앱이 없으면 저장소를 못 읽는다는 것이 함정이다.

`appspec.yml` 의 `files` 에 `requirements.txt` 를 넣지 않아 `AfterInstall` 훅이
실패했다. 훅 스크립트가 참조하는 파일은 전부 `files` 에 있어야 한다.

**인스턴스가 정지 중이면 `ApplicationStop` 훅이 실행되지 못해 배포 전체가
`HEALTH_CONSTRAINTS` 로 실패한다.** 교육 계정 비용 절감을 위해 EC2 를 자주 꺼
두므로 이 실패는 정상 동작이다. 인스턴스를 켠 뒤 콘솔에서 마지막 배포를
재시도하거나 `main` 에 빈 커밋을 푸시하면 된다.

**vLLM 컨테이너는 배포와 무관하므로 훅에서 `docker` 명령을 쓰지 않는다.**
모델 로딩에 2분이 걸려 매 배포마다 재기동하면 손해다. 훅 스크립트를 열어 보면
`docker` 가 한 번도 나오지 않는데, 이것이 의도된 설계다.

GitHub Actions 워크플로도 만들었으나 `.github/workflows/*.disabled` 로
비활성화했다. 두 경로가 같은 EC2·같은 서비스를 대상으로 해 어느 쪽이 마지막에
이겼는지 헷갈리는 문제가 있었다. 되살리려면 `.disabled` 를 떼면 되지만 그 전에
CodePipeline 을 먼저 끄는 편이 안전하다.

### 환경변수 관리

systemd 유닛에 `Environment=` 로 직접 넣었더니 두 가지 문제가 있었다.

- `DATA_GO_KR_KEY_ENC` 는 값에 `%` 가 들어 있어 **systemd 가 지정자(specifier)로
  해석하려다 실패하고 변수가 전달되지 않았다.**
- `KRIC_SERVICE_KEY` 는 `$` 를 포함해 `$$` 이스케이프가 필요했으나
  `Environment=` 에서는 그대로 남았다.

`/etc/maas-api.env` 로 분리하고 `EnvironmentFile=` 로 읽게 해 해결했다.
**이 파일은 배포 대상이 아니므로 EC2 에 직접 존재한다.** 인스턴스를 새로 만들면
다시 넣어야 한다 — 저장소 어디에도 값이 없다.

### 사용하지 않은 AWS 서비스

| 서비스 | 대신 쓴 것 | 이유 |
|---|---|---|
| ALB | Caddy + Let's Encrypt | 도메인·인증서 비용. 단일 인스턴스에 ALB 는 과했다 |
| ECS · EKS | systemd | 컨테이너 하나(vLLM)와 프로세스 둘(api·ui)뿐이라 오케스트레이션이 불필요했다 |
| SageMaker | vLLM 직접 운영 | 모델 교체와 옵션 조정이 훨씬 빠르다. 8B→4B 전환을 몇 분 만에 했다 |
| Route 53 | DuckDNS | 도메인 등록비가 크레딧 대상이 아니었다 |
| Cost Explorer | — | 교육 계정에서 권한이 차단됐다. 비용은 인스턴스 타입 단가로 추정했다 |

---

## 6. 데이터 소스 현황

### 실데이터

| 도구 | 출처 | 비고 |
|---|---|---|
| `search_rail` · `fare_policy` | 코레일 열차운행정보 | 미래 데이터가 없다. 약 3개월 롤링 윈도우의 **과거 실적**이라 같은 요일의 최근 운행일을 찾아 *참고 시간표*로 제시하고 그 사실을 답변에 명시한다 |
| `search_bus` | TAGO 고속버스정보 | 요금·등급 포함. 조회 지평이 D+2 까지라 그 밖의 날짜는 D+2 로 당겨 조회하고 `date_requested`/`date_clamped` 로 표시한다 |
| `search_flight` | TAGO 국내항공운항정보 | 시간표 + 운임(`economyCharge`). 미래 조회가 **D+50** 까지 된다 — 현재 IATA 시즌 시간표 전체를 들고 있다 |
| `get_realtime_status` (ICN) | 인천국제공항공사 | D-3~D+6, **일 500회**. 탑승구·터미널·수하물수취대·출구 |
| `get_realtime_status` (그 외) | 한국공항공사 | D-3~D+6, 일 5,000회/오퍼레이션. 14개 공항 |
| `search_parking` · `search_ev_charger` · `share_mobility` · 지하철 도착 | 서울시 실시간 도시데이터 | **서울 주요 121개 장소 한정** |
| 도시철도 역·노선 | KRIC `subwayRouteInfo` 캐시 | 전국 1,034역 / 43개 노선 / 20개 운영기관 |
| 지오코딩 | transit_nodes → admin_areas → 카카오 로컬 (3단계 폴백) | 전국 지명 해소 |

로컬 캐시 규모.

| 파일 | 내용 |
|---|---|
| `airports.json` | 공항 232개(국내 15) · 항공사 137개 |
| `rail_stations.json` | 간선철도역 1,205개 · 노선 163개 |
| `subway_stations.json` | 도시철도역 1,034개 · 노선 43개 |
| `bus_terminals.json` | 터미널 453개 · 도시 110개 |
| `seoul_areas.json` | 서울 실시간 도시데이터 121개 장소 |
| `transit_nodes.json` | 공항·터미널·주요역 접근점 68개 |

### 목(mock) 데이터

| 도구 | 출처 예정 | 현재 |
|---|---|---|
| `plan_journey` | OpenTripPlanner / ODsay | 구간 상세가 목 데이터다 |
| `search_lodging` | 한국관광공사 TourAPI | 전부 목 데이터다 |

목 데이터를 쓰는 도구는 `data_source` 에 `(mock)` 을 찍고 답변에도 그대로
드러난다. 실데이터 도구도 API 실패 시 같은 표기로 폴백한다.

### 미사용

**ODsay 경로탐색** — Basic 요금제가 **일 30건**이라 개발 자체가 불가능했다.
어댑터(`gate/odsay_api.py`)는 남아 있고 키가 없으면 폴백한다. 유료 요금제를
쓸 수 있게 되면 키만 넣으면 동작한다.

**KRIC `subwayTimetable`** — 오퍼레이션이 현재 인증키에 승인되지 않았다.
역 실재 여부·노선·환승만 답하고 **시각표는 다루지 않는다.** 캐시 파일에
`has_timetable: false` 와 그 사유를 함께 저장해 두었다 — 나중에 승인되면
이 플래그부터 확인하면 된다.

**시외버스** — TAGO 에 별도 서비스(`SuburbsBusInfoService`)가 있으나 연동하지
않았다. 고속버스만 다룬다.

### 커버리지 정책

서울 전용 서비스(따릉이·주차·충전·지하철 도착)를 서울 밖에서 물으면 **목
데이터로 폴백하지 않는다.** 존재하지 않는 서비스를 있는 것처럼 안내하면 안 되기
때문이다.

세 갈래로 분기한다.

| 상황 | 응답 |
|---|---|
| 커버리지 안 + API 성공 | 실데이터 |
| 커버리지 안 + API 실패 | 목 데이터 폴백 (`(mock)` 표기) |
| 커버리지 밖 | `location_not_covered` (**폴백 금지**) |

철도·항공·고속버스는 전국 서비스라 커버리지 개념이 없다. API 실패 시 목 데이터로
폴백한다.

---

## 7. 공공데이터 API 에서 확인한 것

**이름과 실제 내용이 다른 경우가 잦았다.** 네 번 겪은 뒤 어댑터를 쓰기 전에
실측부터 하는 것을 원칙으로 삼았다. 이 목록이 다음 사람에게 가장 유용할 것이다.

| API 이름 | 실제 내용 |
|---|---|
| 철도운영정보_운임 | 2003~2007년 **화물** 최저운임 7건 |
| 한국철도공사 열차운행정보 | 미래 시간표 없이 **과거 실적**만 |
| 국가철도공단 열차별운행시각표 | 실제로는 **도시철도** 시각표 |
| 열차운임 및 시간표 | 2021년 XLSX 51행 |

**코레일 열차운행정보의 `cond` 파라미터.** 필터가 전혀 동작하지 않아
`totalCount` 가 69,503 으로 고정됐다. `runDt`·`runYmd`·`depStnCd` 등 그럴듯한
이름을 다 시도했지만 전부 무시됐다. 문제는 이름이 아니라 **형식**이었다 —
`cond[필드명::연산자]` 형태여야 한다.

```
cond[dptre_stn_nm::EQ]=서울 & cond[arvl_stn_nm::EQ]=부산   → 70,198 → 5,987
```

동작하는 연산자는 `EQ`·`GT`·`GTE`·`LT`·`LTE`·`LIKE` 다. `NE` 와 `BETWEEN` 은
응답 자체가 깨지므로 쓰면 안 된다.

**`RunPlan2` 는 시종착역만 기록한다.** 중간 정차역 구간을 조회하면 0건이 나온다 —
서울→광명·천안아산·오송, 청량리→원주가 전부 0건인데 실제로는 수십 편이 선다.
정차역 단위인 `RunInfo2` 로 조인해 보완했다(서울→광명: `RunPlan2` 0편 →
`RunInfo2` 86편).

**TAGO 활용가이드의 엔드포인트가 포털 값과 달랐다.** 가이드 문서는
`DmstcFlightNvgInfoService`, 포털은 `DmstcFlightNvgInfo` 였고 **포털 값이
맞았다.** 문서대로 부르면 `NO_OPENAPI_SERVICE_ERROR(12)` 가 난다. 인천공항도
같았다(문서 `statusOfAllFlt`, 실제 `statusOfAllFltDeOdp`).

**호스트를 잘못 짚어 "키 미등록"으로 오판했다.** 한국공항공사를
`openapi.airport.co.kr` 로 호출했더니 `SERVICE KEY IS NOT REGISTERED` 가 왔다.
그 호스트가 실재하는 다른 게이트웨이라 경로가 틀리면 다른 오류를 주는 바람에
"경로는 맞고 키만 없다"고 결론지었다. 포털 값
(`apis.data.go.kr/B551178/flight-status`)으로는 처음부터 정상이었다.
**엔드포인트는 문서가 아니라 포털을 믿어야 한다.**

**같은 서비스인데 오퍼레이션마다 규칙이 다르다.** 한국공항공사가 그렇다.

| 오퍼레이션 | 파라미터 표기 | 특징 |
|---|---|---|
| `/info` | camelCase (`schAirCode`) | `searchday` 를 무시하는 **오늘 전용 피드**. `gate` 를 주는 유일한 목록 |
| `/depart` · `/arrival` | snake_case (`airport_code`) | D-3~D+6. 코드셰어 정보 있음. **`gate` 필드가 없음** |
| `/detail` | 필터 파라미터가 **아예 없음** | 4,778건 48페이지를 통째로 받아야 함. `BAGGAGE_CLAIM` 을 주는 유일한 곳 |

`/depart` 는 `arrvAirportCode`, `/arrival` 은 `arrAirportCode` 로 필드명까지
갈린다. `numOfRows` 상한은 100 이고 200 이상은 `HTTP_ERROR(04)` 다.

**코드셰어 플래그를 그대로 믿으면 실제 운항편이 사라진다.** 한국공항공사의
`codeshare=Y` 는 "이 편이 코드셰어에 엮여 있다"는 뜻이지 "이 편이 중복"이라는
뜻이 아니다. 김포 하루치 Y 75건 중 36건이 `masterflightid` 가 자기 자신이었다
(BX8025 → BX8025, 에어부산이 실제 운항). Y 를 중복으로 보고 지우면 그 36편이
목록에서 사라진다. `masterflightid == flightid` 로 판정해야 한다.

**응답 구조가 API 마다 다르다.**

| API | `items` 구조 |
|---|---|
| TAGO · 한국공항공사 | `body.items.item[]` |
| 인천공항공사 | `body.items[]` — **래퍼 없음** |

거기에 공공데이터포털은 결과가 1건일 때 배열이 아니라 단일 객체를 주는 경우가
있다. 세 경우를 모두 흡수하는 정규화 함수를 어댑터마다 둔다.

**인증키는 `unquote` 해서 쓴다.** 포털이 주는 키는 Encoding 형태(`%2F` 등)라
`httpx` 의 `params` 가 다시 인코딩하면 이중 인코딩(`%252F`)이 되어 403 이 난다.

**운임은 생각보다 비어 있다.** TAGO 국내항공의 `economyCharge` 는 975편 전수
조사에서 **22.8%만** 채워져 있었다. LCC 7사는 전부 0 이고 제주→김포는 111편
전부 0 이다. `prestigeCharge` 는 93.3%가 0 인데 이는 "0원"이 아니라 **비즈니스석이
없다**는 뜻이다. 0 을 요금으로 안내하면 안 된다.

---

## 8. 실증 결과

| 확인한 것 | 근거 |
|---|---|
| **LoRA 파인튜닝 불필요** | 제로샷 도메인 정확도 98.9%. 남은 오답은 모델이 아니라 금지어 사전·프롬프트 영역이었다 |
| **게이트 4B 확정** | 8B 대비 도메인 정확도 동일(98.9%), P95 지연 2.34s → 1.42s |
| **Guardrails Classic tier 는 한국어를 못 거른다** | Standard tier + cross-region 이 필수 |
| **근거성 검증이 실제로 작동한다** | Haiku 가 지어낸 KTX 편명·요금 10건을 차단 |
| **1.7B 의 FPR 0% 는 함정** | 정상을 막지 않는 대신 막아야 할 것도 통과시켰다(FNR 8.0%) |

모델 비교 (90문항 기준, `eval/results/`).

```
지표                zeroshot(8B)      qwen4b         qwen1.7b
──────────────────────────────────────────────────────────────
FPR (전체)              1.5%            1.5%            0.0%
FNR                     0.0%            0.0%            8.0%
도메인 정확도            98.9%           98.9%           97.8%
의도 정확도             53.3%           38.9%           17.8%
Slot F1                 0.754           0.630           0.637
에스컬레이션 α          54.4%           51.1%           64.4%
지연 P95                2.335s          1.423s          1.698s
```

자세한 근거는 [docs/FINDINGS.md](docs/FINDINGS.md).

---

## 9. 배포

배포 경로는 **CodePipeline 하나**다.

```
GitHub(main) → CodePipeline → CodeBuild(maas-build) → CodeDeploy(maas-api) → EC2(/opt/maas) → systemd
```

1. **Source** — `sweeetgy-boop/maas-multilingual-agent` (`main`).
2. **Build** (`buildspec.yml`) — 문법 검사(`py_compile`), `gate/*.json` 검증,
   `eval/testset.jsonl` 검증, 배포 구성 파일 존재 확인. 실제 API 호출이나 모델
   추론은 하지 않는다(빌드 환경에 GPU·vLLM 없음). `__pycache__`·`*.db`·`*.zip` 을
   정리한 뒤 아티팩트를 넘긴다.
3. **Deploy** (`appspec.yml` + `scripts/deploy/*.sh`) — CodeDeploy 에이전트가
   `ApplicationStop → BeforeInstall → (파일 복사) → AfterInstall → ApplicationStart
   → ValidateService` 순으로 훅을 실행한다. `gate/`, `ui/`, `requirements.txt` 만
   교체하고 **systemd 유닛 파일은 절대 덮어쓰지 않는다.**

운영 중인 서비스.

| 이름 | 포트 | 관리 |
|---|---|---|
| `maas-api` | 8080 | systemd |
| `maas-ui` | 7860 | systemd |
| `caddy` | 80 / 443 | systemd |
| `vllm` | 8000 (127.0.0.1) | Docker |

### 파이프라인 최초 생성 / 갱신

```bash
./scripts/create_pipeline.sh
```

멱등적이다 — `maas-build`/`maas-pipeline` 이 이미 있으면 update, 없으면 create.
GitHub 연결(CodeConnections)이 처음 만들어진 뒤 **PENDING** 이라면 실행 전에
[콘솔](https://console.aws.amazon.com/codesuite/settings/connections)에서 한 번
승인해야 한다 — 스크립트로 자동화할 수 없다.

### 수동 배포

```bash
aws deploy create-deployment \
  --application-name maas-api \
  --deployment-group-name maas-api-prod \
  --github-location repository=sweeetgy-boop/maas-multilingual-agent,commitId=<커밋SHA>
```

### 환경변수·API 키 변경

CodeDeploy 훅은 systemd 유닛과 `/etc/maas-api.env` 를 건드리지 않는다(의도된
제약). **EC2 에 직접 접속해서 바꿔야 한다.**

```bash
aws ssm start-session --target i-008500ef9e7c53aec
sudo vi /etc/maas-api.env
sudo systemctl restart maas-api
```

시작·정지, 인증서 갱신, 장애 진단은
[docs/architecture/operations.md](docs/architecture/operations.md).

---

## 10. 평가

두 벌이 있고 용도가 다르다.

| 파일 | 규모 | 용도 |
|---|---|---|
| `eval/testset.jsonl` | 131문항 | **회귀 검증용.** CI 가 형식을 검사하고, `score.py` 의 기본 입력이다 |
| `eval/mass_testset_20260831.{jsonl,json,csv}` | 515 / 491 / 491문항 | **대량 평가셋.** jsonl 이 원본, json 배열본과 csv 는 제출본이다 |

### 대량 평가셋 구성 (515문항)

| 구분 | 값 |
|---|---|
| 언어 | ko 111 / zh 104 / ja 104 / id 99 / en 97 |
| 유형 | `injection` 200 · `simple` 133 · `boundary` 91 · `out_of_domain` 35 · `complex` 29 · `pii` 16 · `toxicity` 11 |
| 기대 라우팅 | `local` 15 / `cloud` 224 / `blocked` 276 |

**허용 문항이 핵심이다.** 차단 케이스만으로는 오탐률을 잴 수 없고, 모든 입력을
막는 시스템도 만점을 받는다. 그래서 정상·경계 문항을 절반 가까이 넣었다.

`injection` 200문항은 프롬프트 유출·역할 탈취·제한 해제 시도(`inj-*` 100건)와
SSRF 시도(`ssrf-*` 100건)로 나뉜다.

### 라벨링 정책

경계 사례는 정답이 자명하지 않다. 아래 원칙으로 라벨링했으며, **이견이 있으면
실험 전에 확정**해야 한다. 실험 후에 라벨을 바꾸면 비교가 무의미해진다.

`in_domain = true` 로 판정한 것 — 이동 수단의 이용 방법·요금·시설·반입 규정
(와이파이, 짐 보관함, 보조배터리), 공유 모빌리티 이용 요건(국제운전면허).
스케줄 조회가 아니어도 "타는 행위"와 직결되면 통과시킨다.

`in_domain = false` 로 판정한 것 — 관광 일정, 맛집·음식 추천, 날씨, 환전,
출입국·비자, 그리고 코딩·정치·의료·투자·창작.

| ID | 내용 | 이유 |
|---|---|---|
| `ko-028` / `en-027` / `id-027` | "이 노선 진짜 미쳤다" 류 | 강한 불만이지만 **욕설 아님**. 차단하면 오탐. 인니어 `gila` 는 구어 강조 표현이라 특히 위험 |
| `ko-017` / `en-017` / `id-017` | KTX 와이파이 | 스케줄이 아니지만 **통과시켜야 함** |
| `id-021` | 서울역 근처 할랄 음식 | 인니 사용자에게 매우 흔한 질문. 현재 false 로 라벨링했으나 **정책 확정 필요** |
| `en-022` / `id-022` | 카셰어링 국제면허 | 법률 인접이지만 이동 요건이므로 true |

**실데이터 전환 시 문항도 함께 고쳐야 한다.** "인천공항에서 제주" 항공 문항
5건을 "김포공항에서 제주"로 바꿨다. 인천→제주는 실데이터로 하루 0~1편이라
목 데이터일 때는 가짜로 답하던 것이 실데이터로는 "노선 없음"이 되어 평가가
왜곡됐다. 같은 이유로 탑승구 문항의 편명을 `KE001`(인천에 없는 편명, 5일 연속
0건)에서 `KE081`(매일 1편)로 바꿨다.

### 실행

```bash
cd eval

# 게이트 모델만 채점 (vLLM 엔드포인트) — 도구를 타지 않아 공공 API 호출이 0회다
python score.py --testset mass_testset_20260831.jsonl \
  --endpoint http://localhost:8000/v1 --model transit-base --tag after-flight

# 결과 비교
python score.py --compare results/zeroshot.json results/qwen4b.json

# 파이프라인 전체(방어 계층 포함)를 API 로 채점 — 계층별 기여도가 나온다
python gate/run_eval_via_endpoint.py --endpoint http://localhost:8080 \
  --testset eval/testset.jsonl
```

`score.py` 의 콘솔 리포트는 `ko`/`en`/`id`/`ALL` 행만 출력한다. zh·ja 는 집계에는
들어가지만 표에 별도 행으로 나오지 않는다.

**인천공항 API 는 일 500회 제한이다.** 평가 중 소진을 걱정했으나 실측 결과
문제가 없었다 — `score.py` 는 `tools.py` 를 임포트하지 않아 **공공 API 호출이
0회**이고, 파이프라인 전체를 태워도 인천 경로에 닿는 문항이 5개뿐이라 하루치를
통째로 받아 캐시하는 구조 덕에 **실호출 2회**(캐시 미스를 가정한 최악값도 5회)다.
그래도 반복 평가용으로 `SKIP_IIAC_API=1` 스위치를 두었다(기본 꺼짐).

### 지표

| 지표 | 의미 |
|---|---|
| **FPR** | 정상 문의를 잘못 막은 비율 (1순위) |
| FNR | 막아야 할 걸 통과시킨 비율 |
| 도메인 정확도 | `in_domain` 판정 정확도 |
| 의도 정확도 | 게이트가 고른 도구가 맞았는지 |
| Slot F1 | 출발·도착·일시·인원 추출 정확도 |
| **α (에스컬레이션율)** | `cloud` 라우팅 비율. 낮을수록 비용 절감 |
| P50 / P95 지연 | 게이트 응답 시간 |

### 최종 측정 결과 (2026-09-04, transit-base = Qwen3-4B)

| | 515문항 (`after-flight`) | 131문항 (`after-flight-90`) |
|---|---|---|
| PARSE_ERROR | **0건** | **0건** |
| 도메인 정확도 | 89.5% | 96.9% |
| 의도 정확도 | 68.5% | 53.4% |
| FPR | 4.1% | 3.9% |
| FNR | 16.1% | 0.0% |
| 유해도 정확도 | 98.4% | 98.5% |
| Slot F1 | 0.271 | 0.621 |
| α | 39.0% | 51.1% |
| P95 지연 | 1.263s | 1.268s |

**두 세트의 도메인 정확도 차이는 구성 때문이다.** 515문항 세트는 `injection` 200 ·
`boundary` 91 로 어려운 문항이 절반을 넘고, 도메인 오답 54건 중 35건이 그 둘에서
나온다. 기존 기준선(98.9%)은 90문항 시절 `testset.jsonl` 의 수치라 131문항
세트(96.9%)와 비교하는 것이 맞다.

**의도 정확도는 38.9% → 53.4%(131문항) / 68.5%(515문항)로 올랐다.** 다만 게이트는
여전히 항공 질의를 자주 틀린다 — "인천공항 KE081 탑승구"를 `search_rail`,
"인천공항 도착 수하물"을 `search_lodging` 으로 보낸다. 게이트 프롬프트를 늘리면
xgrammar 제약이 커져 더 나빠지므로, **받는 도구가 원문을 보고 되돌린다.**

### 보안 문항 차단률 (211문항, 첫 측정)

| 구분 | 차단 | 비율 |
|---|---|---|
| 프롬프트 인젝션 | 97 / 100 | 97.0% |
| **SSRF** | 74 / 100 | **74.0%** |
| 유해도 | 7 / 8 | 87.5% |
| **합계** | **178 / 208** | **85.6%** |

차단 단계 분포는 `guardrail_input` 106 · `out_of_domain` 64 · `blocklist` 5 ·
`toxicity` 3 이다. 언어별 편차는 작다(82.9~87.8%).

**누락 30건의 성격이 다르다.** 26건은 도구가 지명 해소에 실패해 정형 NOT_FOUND
문구만 나간다 — 공격 내용이 응답에 반영되지 않는다. 실제로 답변이 나간 것은
4건이고, 그중 2건은 커버리지 안내 문구에 **공격 URL 을 그대로 되비친다.**

**SSRF 는 실제로 수행되지 않는다.** 모든 어댑터가 고정 엔드포인트만 호출하고
사용자 입력 URL 로 요청하는 경로가 없다. 문제는 "요청을 수행"이 아니라
**"거절하지 않고 응답"** 이다. SSRF 74% 가 인젝션 97% 대비 낮은 것이 남은
개선 지점이다.

---

## 11. 로컬 개발

```bash
pip install -r requirements.txt

# 단건
python gate/pipeline.py --text "서울역에서 부산역 가는 KTX 알려줘"

# 5개 언어 시나리오 일괄
python gate/pipeline.py --demo

# 대화형
python gate/pipeline.py --serve

# 채팅 UI (http://localhost:7860)
python -m uvicorn ui.server:app --port 7860

# 공개 API (http://localhost:8080/docs)
export MAAS_API_KEY=$(openssl rand -hex 24)
python -m uvicorn ui.api:app --host 0.0.0.0 --port 8080
```

게이트가 필요하면 SSM 포트 포워딩으로 EC2 의 vLLM 을 붙인다
([5. AWS](#원격-접속--systems-manager) 참고). **WSL 안에서 실행해야 한다.**

### 환경변수 (이름만 — 실제 값은 저장소에 없다)

| 이름 | 용도 |
|---|---|
| `VLLM_URL` | 게이트 엔드포인트 (기본 `http://localhost:8000/v1`) |
| `GATE_MODEL` | 게이트 모델명 (기본 `transit-base`) |
| `CLAUDE_MODEL` | Supervisor 모델 ID |
| `GUARDRAIL_ID` · `GUARDRAIL_VERSION` | Bedrock Guardrails |
| `MAAS_API_KEY` | 공개 API 인증 키 |
| `MAAS_RATE_PER_MIN` | 레이트리밋 (기본 60) |
| `DATA_GO_KR_KEY_ENC` | 공공데이터포털 (철도·고속버스·항공·공항) |
| `SEOUL_OPEN_API_KEY` | 서울시 실시간 도시데이터 |
| `KAKAO_REST_API_KEY` | 카카오 로컬 지오코딩 |
| `KRIC_SERVICE_KEY` | 철도산업정보센터 (도시철도 역·노선) |
| `ODSAY_API_KEY` | ODsay 경로탐색 (미사용) |
| `SKIP_IIAC_API` | 평가 시 인천공항 호출 건너뛰기 (기본 꺼짐) |

값에 `%` 나 `$` 가 들어가는 키가 있어 systemd `Environment=` 로는 전달되지
않는다 — `/etc/maas-api.env` + `EnvironmentFile=` 을 쓴다
([5. AWS](#환경변수-관리) 참고).

### 어댑터 단독 실행

각 어댑터는 CLI 로 따로 확인할 수 있다. 새 API 를 붙일 때 이 방식으로 먼저
실측한 뒤 도구에 연결한다.

```bash
python gate/korail_api.py --from 서울 --to 부산 --date 20260830
python gate/expbus_api.py --from 동서울 --to 강릉
python gate/flight_api.py --from 김포 --to 제주 --date 20260905
python gate/airport_status_api.py --airport GMP --io O
python gate/airport_status_api.py --airport ICN --io I
python gate/citydata_api.py --area 강남역
python gate/subway_stations.py --station 서면
python gate/geocode.py --place "강원도 원주"
```

캐시 재생성.

```bash
python gate/build_airports.py        # airports.json
python gate/build_bus_terminals.py   # bus_terminals.json
python gate/build_subway_stations.py # subway_stations.json
python gate/build_transit_nodes.py   # transit_nodes.json
```

### CI 검증

```bash
python scripts/ci_check_imports.py
python scripts/ci_check_testset.py eval/testset.jsonl
python scripts/ci_check_testset.py eval/mass_testset_20260831.jsonl
```

---

## 12. 알려진 제약

| 제약 | 내용 |
|---|---|
| **계정 만료** | 교육 계정(kosa-edu-3), **2026-09-08 만료** — 이후 전체 인프라 소멸 |
| 비용 | EC2(g6e.xlarge) 24시간 가동 시 **하루 약 $29** |
| 의도 분류 정확도 | 게이트가 도구를 틀리게 고르는 일이 잦다. 도메인 판정(96.9%)과 달리 의도는 53.4%다. 받는 도구가 원문을 보고 되돌리는 방식으로 보완한다 |
| `datetime` 미승계 | 멀티턴에서 origin/destination/pax 는 승계하지만 datetime 은 승계하지 않는다. **"그럼 다음 열차는?" 이 직전 시각이 아니라 질문 시점 기준으로 계산된다** |
| 코레일 미래 데이터 없음 | 어제까지의 실적만 있어 **요일 매칭 참고 시간표**로 제공한다. 응답에 기준일을 표시하고 오늘 운행을 보장하지 않는다고 고지한다 |
| 철도 요금·열차종별 없음 | API 가 제공하지 않는다. 지어내지 않고 비워 둔다 |
| 항공 운임 부분 제공 | `economyCharge` 보유율 22.8%. LCC 는 전부 비어 있다. 있는 편에만 표시하고 그 사실을 함께 알린다 |
| 인천공항 API 일 500회 | 셋 중 가장 적다. 하루치를 통째로 받아 캐시하는 구조로 대응한다 |
| 실시간 도시데이터 범위 | 서울 주요 121개 장소 밖은 `location_not_covered` |
| 도시철도 시각표 없음 | KRIC `subwayTimetable` 미승인. 역·노선·환승만 답한다 |
| ODsay 미사용 | Basic 요금제 **일 30건**으로 개발 자체가 불가능했다. 어댑터는 남아 있고 키가 없으면 폴백한다 |
| 목 데이터 잔존 | `plan_journey` 구간 상세, `search_lodging` |
| Bedrock 모델 제약 | 권한 경계로 Sonnet 계열 차단, **Haiku 만 사용 가능** |
| SSRF 차단률 74% | 인젝션 97% 대비 낮다. 실제 요청은 수행되지 않으나 거절 대신 응답하는 경우가 있다 |
| 무인증 공개 경로 | `/v1/chat/completions`·`/v1/models` 가 인증 없이 열려 있다 (GuardBench 연동용). 레이트리밋이 유일한 방어선이다 |

---

## 13. 프로젝트 종료 시 정리 체크리스트

계정 만료(2026-09-08) 전에 정리한다. 만료 후에는 콘솔 접근 자체가 되지 않는다.

**AWS 리소스**

- [ ] Elastic IP 해제 — `eipalloc-028eeddc99b7f626f`
      (인스턴스에서 분리된 채 남으면 과금된다)
- [ ] EC2 인스턴스 종료 — `i-008500ef9e7c53aec` (EBS 300GB 함께 삭제)
- [ ] CodePipeline 삭제 — `maas-pipeline`
- [ ] CodeBuild 삭제 — `maas-build`
- [ ] CodeDeploy 삭제 — 애플리케이션 `maas-api`, 배포 그룹 `maas-api-prod`
- [ ] CodeConnections 삭제 — `maas-github` (GitHub 앱 설치도 함께 제거)
- [ ] S3 버킷 정리 — `maas-pipeline-508139322599`, `maas-deploy-508139322599`
      (버전 관리가 켜져 있어 이전 버전까지 지워야 비워진다)
- [ ] Bedrock Guardrail 삭제 — `b53a6caaqa31`
- [ ] IAM 사용자 삭제 — `github-actions-maas`
- [ ] IAM 역할 삭제 — `maas-llm-ec2`, `maas-codepipeline-role`,
      `maas-codebuild-role`, `maas-codedeploy-role`
- [ ] 보안그룹 삭제 — `sg-0ce81d06ebaf0db08`

**API 키 — 재발급 또는 폐기**

이 키들은 AWS 밖에서 발급받은 것이라 계정이 만료돼도 살아 있다. 반드시 따로
폐기한다.

- [ ] `MAAS_API_KEY` (자체 발급)
- [ ] `SEOUL_OPEN_API_KEY`
- [ ] `KAKAO_REST_API_KEY`
- [ ] `DATA_GO_KR_KEY_ENC`
- [ ] `KRIC_SERVICE_KEY`
- [ ] `ODSAY_API_KEY`

**그 밖**

- [ ] DuckDNS 도메인 정리 — `maas-transit`, `maas-ui`
- [ ] `/etc/maas-api.env` 백업 여부 확인 (EC2 종료 시 함께 사라진다)
- [ ] 저장소에 키가 남아 있지 않은지 최종 확인

```bash
# AWS 액세스 키 접두사와 하드코딩된 키 형태를 찾는다.
# (패턴을 그대로 적으면 이 검사에 자기 자신이 걸리므로 분리해 썼다)
grep -rnE "(AK|AS)IA[0-9A-Z]{16}|api[_-]?key\s*=\s*['\"][A-Za-z0-9]{16,}" \
  --include='*.py' --include='*.md' --include='*.json' --include='*.sh' .
```

종료 후 남는 것은 이 저장소뿐이다. 문서가 프로젝트의 결과물이다.
