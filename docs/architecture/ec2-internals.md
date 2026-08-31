# EC2 내부 구성

인스턴스 `i-008500ef9e7c53aec` (g6e.xlarge, NVIDIA L40S 46GB, Ubuntu 22.04
Deep Learning AMI) 안에서 도는 프로세스와 포트.

```mermaid
flowchart TB
    subgraph ec2 ["EC2 i-008500ef9e7c53aec"]
        direction TB

        subgraph pub ["공개 포트 (보안그룹에서 0.0.0.0/0 허용)"]
            P80[":80/tcp"]
            P443[":443/tcp"]
        end

        Caddy["caddy.service<br/>리버스 프록시 + Let's Encrypt 자동 인증서"]
        P80 --> Caddy
        P443 --> Caddy

        subgraph priv ["내부 전용 포트 (보안그룹 비공개, localhost 또는 EC2 내부에서만)"]
            P8080[":8080"]
            P7860[":7860"]
            P8000["127.0.0.1:8000"]
        end

        Caddy -->|"maas-transit.duckdns.org"| P8080
        Caddy -->|"maas-ui.duckdns.org"| P7860

        MaasApi["maas-api.service<br/>python3 -m uvicorn ui.api:app<br/>WorkingDirectory=/opt/maas"]
        MaasUi["maas-ui.service<br/>python3 -m uvicorn ui.server:app"]
        P8080 --> MaasApi
        P7860 --> MaasUi

        VLLM["Docker 컨테이너 vllm<br/>vllm/vllm-openai, Qwen3-4B<br/>포트 매핑 127.0.0.1:8000->8000"]
        P8000 --> VLLM
        MaasApi -->|"OpenAI 호환 API"| P8000
        MaasUi -->|"OpenAI 호환 API"| P8000

        CodeDeployAgent["codedeploy-agent<br/>CodeDeploy 배포 에이전트"]
        SSMAgent["amazon-ssm-agent<br/>SSM 원격 접속·명령 실행"]
    end
```

## `/opt/maas` 디렉터리 구조

CodeDeploy가 `appspec.yml`의 `files:` 매핑으로 배포하는 대상. systemd 유닛
파일은 이 밖(`/etc/systemd/system/`)에 있어 배포 대상이 아니다.

```
/opt/maas/
├── requirements.txt        # AfterInstall 훅에서 pip install 대상
├── gate/
│   ├── *.py                 # pipeline.py, tools.py, api.py, geocode.py 등
│   ├── *.json                # transit_nodes.json, admin_areas.json 등 (좌표/코드 캐시)
│   └── *.xlsx                 # seoul_121_areas.xlsx (원본 데이터)
└── ui/
    ├── *.py                 # api.py(:8080), server.py(:7860), eval_endpoint.py
    └── *.html                 # index.html
```

## 요약

- `MaasApi`/`MaasUi` → vLLM 화살표는 in-process 함수 호출이 아니라 실제
  HTTP(OpenAI 호환 API) 호출이다 — `VLLM_URL` 환경변수로 가리킨다.
- vLLM은 Docker 포트 매핑 자체가 `127.0.0.1:8000`이라 EC2 밖에서는 물론
  같은 서버의 다른 컨테이너에서도(host 네트워크가 아니라면) 접근할 수 없다.
- `codedeploy-agent`, `amazon-ssm-agent`는 애플리케이션 프로세스가 아니라
  배포/운영을 위한 인프라 에이전트다 — 별도 포트를 열지 않는다.

관리 명령은 [operations.md](operations.md)를 참고.
