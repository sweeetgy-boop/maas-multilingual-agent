#!/bin/bash
# CodeDeploy ApplicationStop 훅.
#
# 서비스가 아직 없을 수 있다 - 첫 배포에는 CodeDeploy 가 이 훅 자체를
# 실행하지 않지만(직전 성공 배포가 없으면 ApplicationStop 을 건너뛰는 것이
# CodeDeploy 의 표준 동작), 수동 재실행이나 유닛 파일이 아직 없는 특이
# 케이스에 대비해 systemctl 실패를 방어적으로 삼킨다.
#
# vLLM 컨테이너(포트 8000)는 이 배포와 무관하다 - 여기서 절대 건드리지 않는다.

systemctl stop maas-api || true
exit 0
