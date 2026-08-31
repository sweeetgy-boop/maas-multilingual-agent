#!/bin/bash
# CodeDeploy ValidateService 훅.
#
# status 가 ok 또는 degraded 면 성공으로 본다 - degraded 는 vLLM 컨테이너가
# 꺼져 있을 때 나오는데(별도 배포, 이 훅과 무관), maas-api 프로세스 자체는
# 정상이라는 뜻이므로 실패로 취급하지 않는다.
#
# set -e 를 쓰지 않는다: 재시도 루프 안에서 curl 이 실패해도 스크립트가
# 즉시 죽지 않고 다음 시도로 넘어가야 한다.

MAX_ATTEMPTS=6
INTERVAL=5
URL="http://localhost:8080/v1/health"

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  echo "헬스체크 시도 ${attempt}/${MAX_ATTEMPTS} ..."
  RESP=$(curl -sf --max-time 5 "$URL" 2>/dev/null)

  if [ -n "$RESP" ]; then
    echo "  응답: $RESP"
    STATUS=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('status',''))" "$RESP" 2>/dev/null)
    if [ "$STATUS" = "ok" ] || [ "$STATUS" = "degraded" ]; then
      echo "헬스체크 성공 (status=${STATUS})"
      exit 0
    fi
    echo "  status='${STATUS}' - 재시도"
  fi

  if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
    sleep "$INTERVAL"
  fi
done

echo "헬스체크 실패: ${MAX_ATTEMPTS}회 재시도 후에도 status=ok/degraded 를 받지 못했습니다" >&2
journalctl -u maas-api -n 30 --no-pager
exit 1
