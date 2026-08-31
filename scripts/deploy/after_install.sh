#!/bin/bash
# CodeDeploy AfterInstall 훅.
#
# 이 시점에는 새 소스가 이미 /opt/maas/gate, /opt/maas/ui 에 복사돼 있다
# (appspec.yml 의 files: 매핑). 의존성을 설치하고 소유권/권한을 정리한다.
#
# requirements.txt 는 files: 매핑 대상이 아니라 /opt/maas 에는 없다 -
# CodeDeploy 훅은 배포 아카이브(저장소 루트 그대로)를 현재 디렉터리로 실행
# 되므로 상대경로로 참조한다.
#
# systemd 유닛 파일(/etc/systemd/system/maas-api.service)은 여기서
# 건드리지 않는다 - API 키들이 Environment= 로 들어 있어 배포 대상이 아니다.

set -euo pipefail

pip3 install --break-system-packages -q -r requirements.txt

# root 소유, 파일은 읽기전용(644), 디렉터리는 탐색 가능(755) - maas-api
# 유닛이 어떤 사용자로 돌든 최소한 읽을 수 있게 한다.
chown -R root:root /opt/maas/gate /opt/maas/ui
find /opt/maas/gate /opt/maas/ui -type d -exec chmod 755 {} +
find /opt/maas/gate /opt/maas/ui -type f -exec chmod 644 {} +

exit 0
