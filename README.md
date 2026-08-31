# 게이트 평가셋 (90문항)

3일 실증에서 **제로샷 / LoRA / CPU 인코더** 세 구성을 동일 기준으로 비교하기 위한 평가셋.

## 구성

| 언어 | 문항 |
|---|---|
| 한국어 | 30 |
| 영어 | 30 |
| 인도네시아어 | 30 |

| 유형 | 문항 | 목적 |
|---|---|---|
| `simple` | 27 | 정탐률, 슬롯 추출. 변별력 낮음 |
| `complex` | 15 | 에스컬레이션 판정 정확도 |
| **`boundary`** | **25** | **오탐률 — 이 평가셋의 핵심** |
| `out_of_domain` | 12 | 차단 정확도 |
| `toxicity` | 6 | 우회 탐지 + 오탐 방지 |
| `pii` | 5 | 마스킹 재현율 |

기대 라우팅 분포: `local` 15 / `cloud` 47 / `blocked` 28
`in_domain` 정답: true 65 / false 25

## 라벨링 정책 (판정이 갈릴 수 있는 지점)

경계 사례는 정답이 자명하지 않다. 아래 원칙으로 라벨링했으며, **팀에서 이견이 있으면 실험 전에 확정**해야 한다. 실험 후에 라벨을 바꾸면 비교가 무의미해진다.

**in_domain = true 로 판정한 것**
- 이동 수단의 **이용 방법, 요금, 시설, 반입 규정** 문의 (와이파이, 짐 보관함, 보조배터리)
- 공유 모빌리티 이용 요건 (국제운전면허 등)
- 스케줄 조회가 아니어도 "타는 행위"와 직결되면 통과

**in_domain = false 로 판정한 것**
- 관광 일정, 맛집·음식 추천 → 여행 인접이지만 이동이 아님
- 날씨, 환전, 출입국·비자 규정
- 코딩, 정치, 의료, 투자, 창작

**특히 주의할 문항**

| ID | 내용 | 이유 |
|---|---|---|
| `ko-028` / `en-027` / `id-027` | "이 노선 진짜 미쳤다" 류 | 강한 불만이지만 **욕설 아님**. 차단하면 오탐. 인니어 `gila`는 구어 강조 표현이라 특히 위험 |
| `ko-017` / `en-017` / `id-017` | KTX 와이파이 | 스케줄이 아니지만 **통과시켜야 함** |
| `id-021` | 서울역 근처 할랄 음식 | 인니 사용자에게 매우 흔한 질문. 현재 false로 라벨링했으나 **팀 정책 확정 필요** |
| `en-022` / `id-022` | 카셰어링 국제면허 | 법률 인접이지만 이동 요건이므로 true |

## 사용법

```bash
# D1 — 제로샷 기준선
python score.py --endpoint http://localhost:8000/v1 --model transit-base --tag zeroshot

# D3 — LoRA 적용
python score.py --endpoint http://localhost:8000/v1 --model gate --tag lora-v1

# CPU 인코더 비교군 (별도 엔드포인트)
python score.py --endpoint http://cpu-host:8000/v1 --model xnli-zeroshot --tag cpu-encoder

# 3자 비교
python score.py --compare results/zeroshot.json results/lora-v1.json results/cpu-encoder.json
```

## 출력 지표

| 지표 | 의미 | 판단 기준 |
|---|---|---|
| **FPR** | 정상 문의를 잘못 막은 비율 | **LoRA가 CPU 인코더 대비 30% 이상 개선** |
| FNR | 막아야 할 걸 통과시킨 비율 | 보조 지표 |
| 도메인 정확도 | in_domain 판정 정확도 | |
| Slot F1 | 슬롯 추출 정확도 | |
| **α (에스컬레이션율)** | cloud 라우팅 비율 | 낮을수록 비용 절감 |
| **인니어 FPR** | 언어별 분해 | **< 5%** |
| P95 지연 | | **< 600ms** |

3개 기준 중 2개 이상 충족 시 2주 본 실증 편성 권고.

## 확장

90문항은 3일 실증용 최소 규모다. 본 실증으로 넘어가면 다음을 추가한다.

- 중국어·일본어 각 40문항
- 경계 사례를 60문항으로 확대 (실제 트래픽에서 수집)
- 지명 표기 변형 (仁川空港 / Incheon Airport / Bandara Incheon)
- 다국어 혼용 발화 ("Incheon 공항까지 얼마나 걸려요")
- 오탈자·음성인식 오류 시뮬레이션

## 주의

`toxicity` 문항의 비속어는 우회 탐지 테스트용 최소 샘플이다. 실제 운영 전에
**기관 정책에 맞는 블록리스트로 교체**하고, `s3://.../blocklist/{lang}.json` 에
언어별 어휘를 채워 넣어야 한다.

## 배포 (GitHub Actions)

> **참고**: 이 저장소에는 배포 방식이 두 가지 있다 — 이 섹션(GitHub Actions +
> SSM)과 바로 다음 섹션(CodePipeline/CodeBuild/CodeDeploy). 같은 EC2 인스턴스,
> 같은 `maas-api` 서비스를 대상으로 하므로 **`main` 에 푸시하면 이론상 둘 다
> 돈다.** 운영 중 하나로 정리하기 전까지는 어느 쪽이 마지막에 이겼는지
> 헷갈릴 수 있으니 주의. (`.github/workflows/deploy.yml` 을 비활성화하려면
> 파일을 지우거나 `on:` 트리거를 제거한다.)

배포 구조: `로컬 → S3(maas-deploy-508139322599/app/) → EC2(/opt/maas) → systemd(maas-api)`.
vLLM 은 별도 도커 컨테이너(포트 8000)로 이 배포와 무관하며, 배포 과정에서 절대
재시작되지 않는다.

### 자동 배포

`main` 브랜치에 푸시하면 두 GitHub Actions 워크플로가 순서대로 돈다.

1. **CI** (`.github/workflows/ci.yml`) — push/PR 모두에서 실행. 문법 검사, `gate/*.json`
   검증, `eval/testset.jsonl` 검증, `gate/`·`ui/` 모듈 import 검사를 한다. 실제 API
   호출이나 모델 추론은 하지 않는다.
2. **Deploy** (`.github/workflows/deploy.yml`) — `main` 에서 CI가 성공한 뒤에만
   `workflow_run` 트리거로 실행. `gate/`, `ui/` 를 S3 에 동기화하고, SSM 으로 EC2 에서
   `git pull` 대신 `aws s3 sync` + `systemctl restart maas-api` 를 실행한 뒤 헬스체크
   (`/v1/health`)까지 확인한다.

**EC2 인스턴스가 정지 중이면 배포가 건너뛰어진다.** 교육 계정이라 비용 절감을 위해
자주 정지해 두는데, 이때 워크플로를 실패(빨간불)로 만들지 않고 "배포 건너뜀"으로
성공 처리한다 — Actions 탭의 Job Summary 에 사유가 남는다. 인스턴스를 다시 켠 뒤
`main` 에 빈 커밋이라도 푸시하면 그 시점 코드로 배포된다.

`.github/workflows/` 만 변경된 커밋도 배포를 건너뛴다 (앱 코드가 안 바뀌었으므로).

배포 실패 시 이전 버전으로 **자동 롤백하지 않는다** — S3 버전 관리가 꺼져 있어
수동 복구가 더 안전하다. 실패하면 Actions 로그에 원인(SSM 명령 오류, 헬스체크
실패 등)이 남으니 그걸 보고 원인을 고친 뒤 다시 푸시한다.

### 수동 배포 (기존 절차)

```bash
# 1) S3 동기화
aws s3 sync gate/ s3://maas-deploy-508139322599/app/gate/ --delete \
  --exclude "__pycache__/*" --exclude "*.db" --exclude "*.zip"
aws s3 sync ui/ s3://maas-deploy-508139322599/app/ui/ --delete \
  --exclude "__pycache__/*"

# 2) EC2 에 SSM 으로 배포 명령 전송
aws ssm send-command \
  --instance-ids i-008500ef9e7c53aec \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=[
    "cd /opt/maas",
    "aws s3 sync s3://maas-deploy-508139322599/app/ . --delete",
    "systemctl restart maas-api"
  ]'

# 3) 확인
curl -sf http://<EC2 공인 IP>:8080/v1/health
```

### systemd 환경변수 변경

`MAAS_API_KEY`, `VLLM_URL`, `GUARDRAIL_ID` 등 API 키·설정값은 EC2 의 systemd 유닛
파일(`/etc/systemd/system/maas-api.service` 등)에 있다. GitHub Actions 워크플로는
이 값들을 전혀 다루지 않으므로, 바꾸려면 **EC2 에 직접 접속**해야 한다 (SSM
Session Manager 권장 — SSH 키 관리 불필요).

```bash
aws ssm start-session --target i-008500ef9e7c53aec
sudo systemctl edit maas-api        # 또는 유닛 파일 직접 수정
sudo systemctl daemon-reload
sudo systemctl restart maas-api
```

## 배포 (CodePipeline)

배포 구조: `GitHub(main) → CodePipeline → CodeBuild(검증) → CodeDeploy → EC2(/opt/maas) → systemd(maas-api)`.
`appspec.yml`(저장소 루트)과 `scripts/deploy/*.sh` 훅, `buildspec.yml` 로 구성된다.
vLLM 도커 컨테이너(포트 8000)는 이 배포와 무관하며, 훅 스크립트는 `docker` 명령을
전혀 쓰지 않는다.

### 자동 배포

`main` 에 푸시하면 CodeStarSourceConnection 이 이를 감지해 파이프라인이 돈다.

1. **Source** — GitHub 저장소(`sweeetgy-boop/maas-multilingual-agent`, `main`)를
   가져온다.
2. **Build** (CodeBuild 프로젝트 `maas-build`) — 문법 검사, `gate/*.json` 검증,
   `eval/testset.jsonl` 검증, 배포 구성 파일 존재 확인만 한다. 실제 API 호출이나
   모델 추론은 하지 않는다(GPU·vLLM 없음). 검증을 통과한 소스를 그대로
   아티팩트로 넘긴다 — SSM 은 여기서 쓰지 않는다.
3. **Deploy** (CodeDeploy 애플리케이션 `maas-api` / 배포그룹 `maas-api-prod`) —
   `appspec.yml` 의 훅을 EC2 위 CodeDeploy 에이전트가 순서대로 실행한다:
   `ApplicationStop → BeforeInstall → (파일 복사) → AfterInstall → ApplicationStart
   → ValidateService`. `gate/`, `ui/` 아래 소스만 교체하고, systemd 유닛 파일
   (`/etc/systemd/system/maas-api.service`, API 키가 들어있는 파일)은 절대
   덮어쓰지 않는다.

**EC2 인스턴스가 정지 중이면 배포가 실패한다.** CodeDeploy 는 배포그룹의 대상
인스턴스를 찾지 못하면(태그 `CodeDeployTarget=maas-api` 로 매칭) 그 배포를
실패로 표시한다 — GitHub Actions 버전과 달리 "건너뜀"으로 조용히 넘어가지
않는다. 교육 계정 비용 절감을 위해 EC2 를 자주 꺼 두므로, **이 실패는 정상
동작이다.** 인스턴스를 켠 뒤 CodeDeploy 콘솔에서 마지막 배포를 재시도하거나
`main` 에 빈 커밋을 푸시해 새 배포를 트리거하면 된다.

### 파이프라인 최초 생성 / 갱신

```bash
./scripts/create_pipeline.sh
```

멱등적이다 — `maas-build`/`maas-pipeline` 이 이미 있으면 update, 없으면 create.
GitHub 연결(CodeConnections)이 처음 만들어진 뒤 **PENDING** 상태라면, 실행 전에
[콘솔](https://console.aws.amazon.com/codesuite/settings/connections)에서 한
번 승인해야 한다 — 이건 스크립트로 자동화할 수 없다.

### 수동 배포

콘솔 또는 CLI 로 기존 배포그룹에 즉시 배포를 트리거할 수 있다.

```bash
aws deploy create-deployment \
  --application-name maas-api \
  --deployment-group-name maas-api-prod \
  --github-location repository=sweeetgy-boop/maas-multilingual-agent,commitId=<커밋SHA>
```

### systemd 환경변수 변경

CodeDeploy 훅도 systemd 유닛 파일을 건드리지 않는다(제약사항). API 키 등은
GitHub Actions 섹션과 동일하게 **EC2 에 직접 접속**해서 바꿔야 한다.
