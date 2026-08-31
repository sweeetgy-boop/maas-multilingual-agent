#!/bin/bash
# CodeDeploy ApplicationStart 훅.
#
# systemd 유닛 파일 자체는 건드리지 않는다 - 새 소스만 반영된 상태에서
# 서비스를 다시 읽어들이고 재시작한다. vLLM 컨테이너(포트 8000)는 절대
# 건드리지 않는다.

set -euo pipefail

systemctl daemon-reload
systemctl restart maas-api
sleep 5

exit 0
