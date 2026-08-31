#!/usr/bin/env python3
"""
CI: gate/, ui/ 의 모든 .py 모듈이 import 되는지 검사한다.

py_compile 은 문법만 본다. 이 스크립트는 실제로 모듈을 실행해(import) 존재
하지 않는 의존성, 깨진 형제 모듈 임포트(gate/ 파일들은 패키지가 아니라
평범한 스크립트라 서로를 `from transit_nodes import ...` 식으로 부른다),
모듈 최상단 코드의 런타임 오류까지 잡는다.

vLLM·AWS 로 실제 요청을 보내는 코드는 실행하지 않는다 — 각 파일의
`if __name__ == "__main__":` 블록은 이 스크립트가 부르지 않는다.

다만 gate/pipeline.py 는 최상단에서 `boto3.client("bedrock-runtime", ...)`
을 만든다(gate/api.py, gate/server.py, ui/api.py, ui/server.py,
gate/eval_endpoint.py, ui/eval_endpoint.py 가 모두 이를 통해 이걸 물고
들어온다). boto3.client() 생성 자체는 네트워크를 타지 않지만, 정적
자격 증명이 하나도 없으면 최신 botocore 가 SSO 류 폴백 자격 증명
공급자를 시도하다 선택적 의존성(botocore[crt]) 미설치로 예외를 던지는
경우가 있다. 그래서 이 스크립트는 더미 정적 자격 증명(진짜 키 아님,
실제 호출도 없음)을 미리 넣어 그 경로를 피한다.

gate/api.py 와 ui/api.py 처럼 서로 다른 디렉터리에 같은 파일명이 있는
경우, 평범한 `importlib.import_module("api")` 를 쓰면 두 번째 호출이
sys.modules 캐시에 걸려 있는 첫 번째 걸 돌려주고 실제로는 import 를
건너뛴다. 그래서 spec_from_file_location 으로 각 파일마다 고유한
합성 모듈명을 줘서 항상 새로 실행되게 한다.

사용법: python scripts/ci_check_imports.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# boto3.client() 구성이 자격 증명 탐색 체인에서 죽지 않게 더미 정적
# 자격 증명을 준다. 실제 API 호출은 절대 하지 않으므로 네트워크로 나가지
# 않는다 — 이미 설정된 값(예: 실제 CI 시크릿)이 있으면 덮어쓰지 않는다.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "dummy-ci-key")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "dummy-ci-secret")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")

PACKAGES = ("gate", "ui")


def _import_file(pkg: str, py_path: Path) -> None:
    unique_name = f"_ci_{pkg}_{py_path.stem}"
    spec = importlib.util.spec_from_file_location(unique_name, py_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"spec_from_file_location 실패: {py_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)


def main() -> int:
    failures: list[tuple[str, str]] = []
    total = 0

    for pkg in PACKAGES:
        pkg_dir = ROOT / pkg
        if not pkg_dir.is_dir():
            continue
        # 이 디렉터리의 형제 임포트(from transit_nodes import ...)가 풀리려면
        # 디렉터리 자체가 sys.path 에 있어야 한다.
        sys.path.insert(0, str(pkg_dir))
        try:
            for py_path in sorted(pkg_dir.glob("*.py")):
                total += 1
                rel = f"{pkg}/{py_path.name}"
                try:
                    _import_file(pkg, py_path)
                    print(f"OK   {rel}")
                except Exception as e:  # noqa: BLE001 - CI 리포팅 목적, 전부 잡아서 모아 보여준다
                    failures.append((rel, f"{type(e).__name__}: {e}"))
                    print(f"FAIL {rel}: {type(e).__name__}: {e}")
        finally:
            sys.path.remove(str(pkg_dir))

    print()
    if failures:
        print(f"{len(failures)}/{total}개 모듈 import 실패:")
        for path, err in failures:
            print(f"  - {path}: {err}")
        return 1

    print(f"{total}개 모듈 모두 import 성공")
    return 0


if __name__ == "__main__":
    sys.exit(main())
