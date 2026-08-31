# 요청 처리 흐름 — 방어 계층 6종

사용자 입력이 응답으로 나가기까지 실제 코드 실행 순서(`gate/pipeline.py`
`handle()`)를 따른다. 방어 계층 6종(`local_gate`, `blocklist`, `pii_regex`,
`bedrock_guardrail`, `grounding_check`, `scope_policy`)은 이 흐름 곳곳에
끼어 있다 — `bedrock_guardrail`은 입력·출력 두 번 검사한다.

```mermaid
flowchart TD
    Start(["사용자 입력"]) --> Gate["local_gate<br/>Qwen3-4B: 언어 / 도메인 / 유해도 / 의도 / 슬롯 추출"]

    Gate --> Blocklist{"blocklist<br/>다국어 금지어 사전 매칭?"}
    Blocklist -->|"예"| R_Warning["차단 응답<br/>(언어별 경고 문구)"]
    Blocklist -->|"아니오"| Toxicity{"toxicity >= 0.6?<br/>(local_gate)"}

    Toxicity -->|"예"| R_Warning
    Toxicity -->|"아니오"| PII["pii_regex<br/>이메일 / 카드 / 여권 / 전화번호 마스킹"]

    PII --> GuardIn{"bedrock_guardrail<br/>입력 검사"}
    GuardIn -->|"개입"| R_Warning
    GuardIn -->|"통과"| Domain{"in_domain == false?<br/>(local_gate)"}

    Domain -->|"예"| R_Refusal["차단 응답<br/>(교통 도메인 안내 문구)"]
    Domain -->|"아니오"| Scope{"scope_policy<br/>예약 / 결제 요청?"}

    Scope -->|"예"| R_Booking["차단 응답<br/>(조회 전용 안내)"]
    Scope -->|"아니오"| Tool["도구 호출<br/>(결정론적, gate/tools.py — 방어 계층 아님)"]

    Tool --> Found{"found == true?"}
    Found -->|"아니오"| R_NotFound["미조회 응답<br/>(정보 없음 안내)"]
    Found -->|"예"| Supervisor["Supervisor<br/>Bedrock Claude Haiku가 답변 생성"]

    Supervisor --> Ground{"grounding_check<br/>답변 숫자 ↔ 도구 결과 대조"}
    Ground -->|"실패, 1회만 재생성"| Supervisor
    Ground -->|"재실패"| R_NotFound
    Ground -->|"성공"| GuardOut{"bedrock_guardrail<br/>출력 검사"}

    GuardOut -->|"개입"| R_Warning
    GuardOut -->|"통과"| R_Answer["정상 응답"]
```

## 계층별 요약

| 계층 | 위치 | 실패 시 |
|---|---|---|
| `local_gate` | 최초 판정 + 유해도/도메인 재확인 지점 2곳 | 경고 또는 도메인 안내 문구로 차단 |
| `blocklist` | 게이트 직후 | 경고 문구로 차단 |
| `pii_regex` | 클라우드 전송 직전 | (차단 아님) 마스킹만 하고 통과 |
| `bedrock_guardrail` | 입력 직후 + 출력 직전, 총 2회 | 경고 문구로 차단 |
| `grounding_check` | Supervisor 응답 직후 | 1회 재생성 → 그래도 실패면 "정보 없음" 응답으로 폐기 |
| `scope_policy` | 도구 호출 직전 | 조회 전용 안내로 차단 |

LLM(Supervisor)은 도구 응답(TOOL_RESULT)의 값만 렌더링하며 시각·요금·경로를
직접 계산하지 않는다 — `grounding_check`가 이를 사후 검증한다.

세부 구성요소는 [README.md](README.md)를 참고.
