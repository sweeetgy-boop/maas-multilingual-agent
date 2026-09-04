# 전체 인프라 구성도

2026-08-31 기준, 실제 운영 중인 구성이다. AWS 계정 508139322599(kosa-edu-3,
2026-09-08 만료), 리전 ap-northeast-2(서울).

```mermaid
flowchart TD
    User(["사용자<br/>브라우저 / API 클라이언트"])

    DuckDNS["DuckDNS<br/>maas-transit.duckdns.org<br/>maas-ui.duckdns.org"]
    EIP["Elastic IP<br/>43.201.216.37"]
    SG["보안그룹 sg-0ce81d06ebaf0db08<br/>80/tcp, 443/tcp 만 0.0.0.0/0 공개"]

    User --> DuckDNS --> EIP --> SG

    subgraph ec2["EC2 i-008500ef9e7c53aec (g6e.xlarge, NVIDIA L40S)"]
        Caddy["Caddy :80 / :443<br/>리버스 프록시 + Let's Encrypt 자동 인증서"]
        MaasApi["maas-api :8080<br/>FastAPI (ui/api.py)"]
        MaasUi["maas-ui :7860<br/>FastAPI + HTML (ui/server.py)"]
        Gate["gate/ 로직<br/>pipeline.py, tools.py"]
        VLLM["vLLM :8000 (Docker, localhost 전용)<br/>Qwen3-4B"]
    end

    SG --> Caddy
    Caddy -->|"maas-transit.duckdns.org"| MaasApi
    Caddy -->|"maas-ui.duckdns.org"| MaasUi
    MaasApi --> Gate
    MaasUi --> Gate
    Gate --> VLLM

    Bedrock["AWS Bedrock Runtime<br/>Claude Haiku (Supervisor)<br/>Guardrails 671i24gxu4oo (Standard, apac)"]
    Gate --> Bedrock

    subgraph external["외부 API"]
        Kakao["카카오 로컬<br/>지오코딩"]
        Seoul["서울 실시간 도시데이터<br/>121개 장소"]
        DataGoKr["공공데이터포털<br/>코레일 / TAGO 고속버스"]
        ODsay["ODsay 경로탐색<br/>일 30건 제한, 현재 미사용(폴백 유지)"]
    end

    Gate --> Kakao
    Gate --> Seoul
    Gate --> DataGoKr
    Gate -.->|"미사용"| ODsay
```

## 요약

- 외부에 노출되는 건 Caddy(80/443)뿐이다. `maas-api`(8080)와 `maas-ui`(7860)는
  보안그룹에서 막혀 있고 Caddy를 통해서만 도달한다.
- `maas-api`와 `maas-ui`는 별개 프로세스지만 같은 `gate/` 파이썬 모듈
  (`pipeline.py`, `tools.py`)을 각자 임포트해서 쓴다 — 서로 네트워크로
  호출하지 않는다.
- vLLM은 Docker 컨테이너로 `127.0.0.1:8000`에만 바인딩돼 있어 EC2 내부에서만
  접근 가능하다.
- ODsay는 일일 호출 한도(30건)가 낮아 실사용에서는 꺼져 있고, 실패 시
  폴백(안내 문구)으로 항상 동작하는 상태를 유지한다.

세부 구성요소·리소스 ID는 [README.md](README.md) 표를 참고.
