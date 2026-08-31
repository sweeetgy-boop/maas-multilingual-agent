#!/bin/bash
# CodeDeploy BeforeInstall 훅.
#
# appspec.yml 의 file_exists_behavior: OVERWRITE 는 새 배포 패키지에 있는
# 파일만 덮어쓴다 - 저장소에서 삭제된 파일은 EC2 에 그대로 남는다. 그래서
# 새 소스를 복사하기 전에 /opt/maas/gate, /opt/maas/ui 의 기존 .py/.json 을
# 미리 지워 "삭제됐는데 남아있는 파일" 문제를 막는다.
#
# /opt/maas 자체나 gate/ui 이외의 다른 경로는 절대 건드리지 않는다
# (systemd 유닛 파일도 이 범위 밖이라 안전하다).

set -euo pipefail

mkdir -p /opt/maas/gate /opt/maas/ui

for dir in /opt/maas/gate /opt/maas/ui; do
  find "$dir" -maxdepth 1 -type f \( -name "*.py" -o -name "*.json" \) -delete
done

# __pycache__ 정리
find /opt/maas/gate /opt/maas/ui -maxdepth 1 -type d -name "__pycache__" -exec rm -rf {} +

exit 0
