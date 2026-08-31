# 인프라 아키텍처

다국어 교통 연계 AI 에이전트의 실제 운영 인프라 문서. 프로젝트 인수인계와
시연 자료 용도로 2026-08-31 기준 구성을 기록한다.

값은 실 운영 중인 것이며, API 키·액세스 키 같은 비밀값은 이름과 용도만
적었다 (실제 값은 어디에도 없다).

## 다이어그램

| 다이어그램 | 내용 |
|---|---|
| [infrastructure.md](infrastructure.md) | 전체 인프라 — 사용자 → DNS → EC2 → 백엔드/외부 API |
| [cicd.md](cicd.md) | CI/CD 파이프라인 — GitHub → CodePipeline → CodeBuild → CodeDeploy |
| [request-flow.md](request-flow.md) | 요청 처리 흐름 — 방어 계층 6종과 차단 분기 |
| [ec2-internals.md](ec2-internals.md) | EC2 내부 — 포트·프로세스·systemd·Docker·디렉터리 구조 |

운영 절차(시작/정지, 배포, 인증서, 장애 진단, 종료 체크리스트)는
[operations.md](operations.md) 참고.

## 리소스 목록

### 네트워크·DNS

| 리소스 | 값 | 용도 |
|---|---|---|
| Elastic IP | `43.201.216.37` | EC2 고정 공인 IP |
| DuckDNS | `maas-transit.duckdns.org` | API(`maas-api`) 도메인 |
| DuckDNS | `maas-ui.duckdns.org` | 채팅 UI(`maas-ui`) 도메인 |
| 보안그룹 | `sg-0ce81d06ebaf0db08` (`maas-llm-sg`) | 인바운드 방화벽 |

**보안그룹 인바운드**: `80/tcp`, `443/tcp`는 `0.0.0.0/0`로 공개. 이 외에
`3000/tcp`, `7000/tcp`가 각각 특정 단일 IP 1개로 제한돼 열려 있는데(개인
접근용으로 추정, 실제로 리스닝 중인 서비스는 3000뿐이고 7000은 현재
아무것도 안 듣고 있다), 이 문서에는 출발지 IP를 적지 않았다 — 더 이상
필요 없으면 정리를 권장한다. `8080`(`maas-api`), `7860`(`maas-ui`)은 EC2
안에서만 접근 가능하고 보안그룹에 규칙이 없다(Caddy만 경유).

### EC2

| 항목 | 값 |
|---|---|
| 인스턴스 ID | `i-008500ef9e7c53aec` |
| 타입 | `g6e.xlarge` (NVIDIA L40S 46GB) |
| AMI | Ubuntu 22.04 Deep Learning AMI |
| IAM 역할 | `maas-llm-ec2` (SSM, Bedrock, S3, CodeDeploy 권한) |
| 배포 경로 | `/opt/maas/{gate,ui}/` + `requirements.txt` |

### CI/CD

| 리소스 | 이름/ID | 용도 |
|---|---|---|
| GitHub 저장소 | `sweeetgy-boop/maas-multilingual-agent` (`main`) | 소스 |
| CodeConnections | AWS Connector for GitHub 앱 | GitHub ↔ CodePipeline 연결 |
| CodePipeline | `maas-pipeline` | Source → Build → Deploy |
| CodeBuild | `maas-build` | 검증(문법/JSON/평가셋/구성파일) |
| CodeDeploy 애플리케이션 | `maas-api` | 배포 정의 |
| CodeDeploy 배포그룹 | `maas-api-prod` | 배포 대상(태그 `CodeDeployTarget=maas-api`) |
| 아티팩트 버킷 | `maas-pipeline-508139322599` (버전 관리 활성) | 파이프라인 아티팩트 |
| 배포 버킷 | `maas-deploy-508139322599` | (레거시 GitHub Actions+SSM 경로용, 현재 비활성) |
| IAM 역할 | `maas-codepipeline-role` | CodePipeline 실행 |
| IAM 역할 | `maas-codebuild-role` | CodeBuild 실행 |
| IAM 역할 | `maas-codedeploy-role` | CodeDeploy 실행 |

> GitHub Actions 워크플로(`.github/workflows/ci.yml.disabled`,
> `deploy.yml.disabled`)는 CodePipeline으로 일원화하며 비활성화했다.

### AWS 관리형 서비스

| 서비스 | 값 | 용도 |
|---|---|---|
| Bedrock Runtime | `anthropic.claude-3-haiku-20240307-v1:0` | Supervisor(답변 생성) |
| Bedrock Guardrails | `b53a6caaqa31` v1 (Standard tier, apac cross-region) | 입력·출력 콘텐츠 검사 |
| S3 | 위 두 버킷 | 배포 아티팩트 |
| SSM | Session Manager, Run Command | 원격 접속·운영 명령 |
| CloudWatch Logs | CodeBuild 빌드 로그 | 빌드 실패 진단 |

## 포트 구성표

| 포트 | 프로세스 | 공개 여부 |
|---|---|---|
| 22 | SSH | 보안그룹 규칙 없음(콘솔/SSM 권장) |
| 80 | Caddy (HTTP, ACME 챌린지) | 공개 |
| 443 | Caddy (HTTPS) | 공개 |
| 7860 | `maas-ui` (uvicorn) | 비공개, Caddy 경유만 |
| 8080 | `maas-api` (uvicorn) | 비공개, Caddy 경유만 |
| 8000 | vLLM (Docker, `127.0.0.1`만 바인딩) | EC2 내부에서만, 외부·컨테이너 밖 접근 불가 |

## systemd 서비스

| 서비스 | 실행 명령 | 관리 명령 |
|---|---|---|
| `maas-api` | `python3 -m uvicorn ui.api:app --host 0.0.0.0 --port 8080` | `systemctl status\|restart\|stop maas-api` |
| `maas-ui` | `python3 -m uvicorn ui.server:app` (포트 7860) | `systemctl status\|restart\|stop maas-ui` |
| `caddy` | Caddyfile 기반 리버스 프록시 | `systemctl status\|restart\|reload caddy` |

로그는 `journalctl -u <서비스명> -n 100 --no-pager`로 확인한다. 상세 절차는
[operations.md](operations.md).

## 환경변수 목록

값은 EC2의 systemd 유닛 파일(`/etc/systemd/system/maas-api.service` 등)에
`Environment=`로만 존재한다. 이 저장소 어디에도 실제 값을 넣지 않는다.

| 변수 | 용도 |
|---|---|
| `MAAS_API_KEY` | `ui/api.py` 인증(`X-API-Key` 헤더) |
| `VLLM_URL` | 게이트 모델(vLLM) 엔드포인트 |
| `GATE_MODEL` | 게이트 모델명 |
| `GUARDRAIL_ID` | Bedrock Guardrails 식별자 |
| `GUARDRAIL_VERSION` | Guardrails 버전 |
| `CLAUDE_MODEL` | Supervisor용 Bedrock 모델 ID |
| `AWS_REGION` | 리전 |
| `KAKAO_REST_API_KEY` | 카카오 로컬 API(지오코딩) |
| `SEOUL_OPEN_API_KEY` | 서울 열린데이터광장(실시간 도시데이터) |
| `DATA_GO_KR_KEY_ENC` | 공공데이터포털(코레일, TAGO 고속버스) |
| `ODSAY_API_KEY` | ODsay 경로탐색(현재 미사용) |
| `MAAS_RATE_PER_MIN` | API 레이트리밋(선택, 기본값 있음) |

## 접속 주소

| 용도 | 주소 |
|---|---|
| API | `https://maas-transit.duckdns.org` |
| 채팅 UI | `https://maas-ui.duckdns.org` |
| API 헬스체크 | `GET /v1/health` (인증 불필요) |
| API 문서(OpenAPI) | `GET /docs` |

## 알려진 제약

| 제약 | 내용 |
|---|---|
| 계정 만료 | 교육 계정(kosa-edu-3), **2026-09-08 만료** — 이후 전체 인프라 소멸 |
| 교통 데이터 | 열차/버스/항공 시간표·요금 다수가 **목(mock) 데이터** — 운영 전 실데이터 전환 필요 |
| ODsay 미사용 | 일 30건 제한이 너무 낮아 실사용에서 꺼둠. 호출 실패 시 폴백 문구로 항상 동작 |
| Bedrock 모델 제약 | 권한 경계(permission boundary)로 Sonnet 계열이 차단돼 있고 **Haiku만 사용 가능** |
| 비용 | EC2(g6e.xlarge)를 24시간 켜 두면 **하루 약 $29** — 교육 계정 예산 소진에 유의 |
| datetime 미승계 | 멀티턴에서 origin/destination/pax 는 슬롯을 승계하지만 datetime 은 승계하지 않는다(시점이 바뀌었을 위험이 더 큼). 그래서 "그럼 다음 열차는?" 같은 후속 질문이 직전 시각이 아니라 **질문 시점("지금") 기준**으로 계산돼, "다음"이라는 의미와 어긋나는 이른 시각이 나올 수 있다. 실제 교통 API 연동 후 "현재 시각 이후" 기준이 명확해지면 자연스럽게 정리될 사안 |

세부 배경은 [../FINDINGS.md](../FINDINGS.md)도 참고.
