# 운영 절차

[README.md](README.md)의 리소스 목록을 참고하며 읽는다. 모든 명령은
계정 508139322599 / 리전 `ap-northeast-2`에 대한 자격 증명이 있다고
가정한다.

## 인스턴스 시작·정지

교육 계정 비용 절감을 위해 EC2를 자주 꺼 둔다. g6e.xlarge를 24시간 켜 두면
하루 약 $29다.

```bash
# 시작
aws ec2 start-instances --instance-ids i-008500ef9e7c53aec --region ap-northeast-2
# 상태가 running 이 될 때까지 대기
aws ec2 wait instance-running --instance-ids i-008500ef9e7c53aec --region ap-northeast-2

# 정지
aws ec2 stop-instances --instance-ids i-008500ef9e7c53aec --region ap-northeast-2
```

Elastic IP를 쓰므로 재시작해도 공인 IP(`43.201.216.37`)는 바뀌지 않는다 —
DuckDNS를 다시 갱신할 필요가 없다(아래 "Elastic IP가 바뀌었을 때" 참고).

인스턴스를 켠 직후에는 vLLM 컨테이너와 각 systemd 서비스가 뜨는 데 시간이
걸린다(모델 로딩 약 2분). 바로 헬스체크가 실패해도 1~2분 뒤 재시도한다.

## vLLM 컨테이너 재기동

**주의**: 모델 로딩에 약 2분이 걸린다. 다른 이유로 죽어 있거나 응답이
이상할 때만 재기동한다 — 평소 배포 절차에서는 절대 건드리지 않는다.

```bash
# EC2 접속 후(아래 SSM Session Manager 참고)
docker ps --filter name=vllm
docker logs --tail 100 vllm
docker restart vllm
# 재기동 후 모델 로딩 완료 확인 (약 2분 소요)
docker logs -f vllm
```

## systemd 서비스 관리 (`maas-api`, `maas-ui`, `caddy`)

```bash
systemctl status maas-api
systemctl status maas-ui
systemctl status caddy

systemctl restart maas-api
systemctl restart maas-ui
systemctl reload caddy      # 설정만 다시 읽음 (연결 안 끊음)
systemctl restart caddy     # 완전 재시작 (필요할 때만)

journalctl -u maas-api -n 100 --no-pager
journalctl -u maas-api -f          # 실시간 팔로우
```

`maas-api`/`maas-ui`의 소스는 CodeDeploy가 관리한다(`/opt/maas/gate`,
`/opt/maas/ui`) — 직접 EC2에서 수정하면 다음 배포 때 덮어써진다. 급한 핫픽스가
아니면 저장소에서 고치고 재배포한다.

## 배포 방법

### 자동 배포

`main`에 푸시하면 CodePipeline이 자동으로 돈다. 세부 흐름은
[cicd.md](cicd.md) 참고.

```bash
git push origin main
```

파이프라인 상태 확인:

```bash
aws codepipeline get-pipeline-state --name maas-pipeline --region ap-northeast-2 \
  --query 'stageStates[].{Stage:stageName,Status:latestExecution.status}' --output table

aws codepipeline list-pipeline-executions --pipeline-name maas-pipeline \
  --region ap-northeast-2 --max-items 5 \
  --query 'pipelineExecutionSummaries[].{Id:pipelineExecutionId,Status:status,Start:startTime}' \
  --output table
```

### 수동 배포

```bash
aws deploy create-deployment \
  --application-name maas-api \
  --deployment-group-name maas-api-prod \
  --github-location repository=sweeetgy-boop/maas-multilingual-agent,commitId=<커밋SHA> \
  --region ap-northeast-2
```

또는 콘솔에서 CodeDeploy → 애플리케이션 `maas-api` → 배포 생성.

### CodePipeline/CodeBuild 재생성

리소스가 삭제됐거나 설정을 바꿔야 하면(멱등적이라 재실행해도 안전):

```bash
./scripts/create_pipeline.sh
```

## 인증서 갱신

Caddy가 Let's Encrypt 인증서를 자동으로 발급·갱신한다 — 별도 조치가
필요 없다. 확인만 한다.

```bash
# 만료일 확인
echo | openssl s_client -connect maas-transit.duckdns.org:443 -servername maas-transit.duckdns.org 2>/dev/null \
  | openssl x509 -noout -dates

# EC2 안에서 Caddy 인증서 저장소 직접 확인
ls -la /var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/

# 갱신 이슈 의심되면 caddy 로그 확인
journalctl -u caddy -n 200 --no-pager | grep -i cert
```

## Elastic IP가 바뀌었을 때 DuckDNS 갱신

Elastic IP는 인스턴스를 껐다 켜도 유지되므로 평소엔 필요 없다. EIP를
새로 할당하거나 다른 인스턴스에 재연결한 경우에만 해당한다.

1. 새 공인 IP 확인: `aws ec2 describe-addresses --region ap-northeast-2`
2. DuckDNS 갱신(토큰 필요, 브라우저 또는 curl):
   ```bash
   curl "https://www.duckdns.org/update?domains=maas-transit,maas-ui&token=<DUCKDNS_TOKEN>&ip=<새IP>"
   ```
3. DNS 전파 확인: `dig +short maas-transit.duckdns.org`
4. Caddy가 새 인증서를 자동 재발급하는지 확인 (위 "인증서 갱신" 절차)

## 장애 진단 순서

1. **헬스체크** — 가장 바깥부터.
   ```bash
   curl -sf https://maas-transit.duckdns.org/v1/health
   ```
   실패하면 DNS/Caddy/네트워크 문제일 수 있다. EC2 안에서
   `curl -sf localhost:8080/v1/health`로 좁혀본다.

2. **systemd 상태**
   ```bash
   systemctl status caddy maas-api maas-ui
   ```
   `failed`/`inactive`면 3번으로.

3. **journalctl**
   ```bash
   journalctl -u maas-api -n 50 --no-pager
   journalctl -u caddy -n 50 --no-pager
   ```
   최근 에러 스택트레이스를 찾는다. `Environment=` 설정 오류(키 누락 등)는
   보통 시작 직후 로그에 바로 나온다.

4. **docker logs** (vLLM 관련 증상 — 게이트/Supervisor가 느리거나 502)
   ```bash
   docker ps --filter name=vllm
   docker logs --tail 100 vllm
   ```
   컨테이너가 안 떠 있으면 "vLLM 컨테이너 재기동" 절차로. 떠 있는데
   느리면 모델 로딩 중이거나 GPU 메모리 문제일 수 있다.

5. **CodeDeploy 배포 로그** (배포 직후 장애일 때)
   ```bash
   aws deploy get-deployment --deployment-id <ID> --region ap-northeast-2
   ```
   콘솔의 배포 상세 페이지에서 훅별(BeforeInstall/AfterInstall/…) 로그를
   바로 볼 수 있다 — CLI보다 편하다.

## 프로젝트 종료 시 정리 체크리스트

계정이 2026-09-08 만료되지만, 그 전에 수동으로 정리하려면:

- [ ] Elastic IP 해제 (`aws ec2 release-address`) — 연결 안 된 EIP는 과금됨
- [ ] EC2 인스턴스 종료 (`aws ec2 terminate-instances`)
- [ ] CodePipeline 삭제 (`aws codepipeline delete-pipeline --name maas-pipeline`)
- [ ] CodeBuild 프로젝트 삭제 (`aws codebuild delete-project --name maas-build`)
- [ ] CodeDeploy 애플리케이션 삭제 (`aws deploy delete-application --application-name maas-api`)
- [ ] S3 버킷 비우고 삭제 (`maas-pipeline-508139322599`, `maas-deploy-508139322599`)
- [ ] CodeConnections 연결 삭제
- [ ] Bedrock Guardrail 삭제
- [ ] IAM 역할 삭제 (`maas-codepipeline-role`, `maas-codebuild-role`,
      `maas-codedeploy-role`, `maas-llm-ec2`)
- [ ] IAM 사용자 삭제 (배포/운영에 쓰던 CLI 자격 증명용 계정 — 액세스 키부터
      비활성화한 뒤 삭제)
- [ ] **외부 API 키 재발급/폐기**: 카카오, 서울 열린데이터광장, 공공데이터포털,
      ODsay — 개인/팀 계정으로 발급받은 키라면 교육 계정 종료와 별개로
      직접 폐기해야 한다
- [ ] DuckDNS 도메인 정리(더 이상 안 쓰면 삭제 또는 IP 연결 해제)
