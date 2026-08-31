#!/usr/bin/env python3
"""
CI: eval/testset.jsonl 검증.

각 줄이 유효한 JSON 인지, 필수 필드(id, lang, category, text, expected,
route)를 갖추고 있는지, id 가 파일 전체에서 중복되지 않는지 확인한다.

사용법: python scripts/ci_check_testset.py [경로]  (기본값: eval/testset.jsonl)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_FIELDS = ("id", "lang", "category", "text", "expected", "route")


def check(path: Path) -> list[str]:
    errors: list[str] = []
    seen_ids: dict[str, int] = {}

    with path.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue

            try:
                row = json.loads(raw)
            except json.JSONDecodeError as e:
                errors.append(f"L{lineno}: JSON 파싱 실패 - {e}")
                continue

            if not isinstance(row, dict):
                errors.append(f"L{lineno}: 최상위 타입이 object 가 아님")
                continue

            missing = [k for k in REQUIRED_FIELDS if k not in row]
            if missing:
                errors.append(f"L{lineno} (id={row.get('id', '?')}): 필수 필드 누락 - {missing}")

            rid = row.get("id")
            if rid is not None:
                if rid in seen_ids:
                    errors.append(f"L{lineno}: id 중복 '{rid}' (첫 등장: L{seen_ids[rid]})")
                else:
                    seen_ids[rid] = lineno

    return errors


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("eval/testset.jsonl")
    if not path.exists():
        print(f"파일 없음: {path}", file=sys.stderr)
        return 1

    errors = check(path)
    if errors:
        print(f"{path} 검증 실패 ({len(errors)}건):")
        for e in errors:
            print(f"  - {e}")
        return 1

    n = sum(1 for line in path.open(encoding="utf-8") if line.strip())
    print(f"{path}: {n}줄 모두 유효 (필수 필드 완비, id 중복 없음)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
