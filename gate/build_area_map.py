"""
서울 121장소 별칭 테이블 빌드 스크립트 — 1회성 데이터 준비.

  seoul_121_areas.xlsx (CATEGORY | NO | AREA_CD | AREA_NM | ENG_NM, 121행)

을 읽어 gate/area_aliases.json 을 만든다. tools.py 의 resolve_place() 는
이 파일을 읽어 서울 121장소도 "장소"로 인식하도록 확장한다 (기존
PLACE_ALIASES 철도역·공항 테이블과는 별개 계층).

별칭 생성 규칙 (AREA_NM 기준, 상호 배타적으로 하나만 적용):
  - 괄호 포함: "X(Y)역" 처럼 괄호 뒤에 글자가 더 있으면 내용물+접미사와
    내용물 단독을 별칭으로 ("총신대입구(이수)역" → "이수역", "이수").
    괄호가 맨 끝이면 괄호를 뗀 나머지만 별칭으로 ("홍대입구역(2호선)" → "홍대입구역").
  - "·" 포함: 각 조각을 그대로 별칭으로 ("신촌·이대역" → "신촌", "이대역").
  - "역" 로 끝남: 접미사를 뗀 형태도 별칭으로 ("강남역" → "강남").
  원본 AREA_NM 과 ENG_NM 소문자는 항상 별칭에 포함한다.

여러 장소가 같은 별칭 문자열로 겹치면(드묾) 카테고리 우선순위로 정한다.
자주 겹치는 진짜 케이스는 정확히 같은 문자열이 아니라 부분 문자열
포함관계이므로(예: "홍대"는 "홍대 관광특구"와 "홍대입구역" 둘 다에 포함),
그 부분은 tools.py 의 resolve_place() 쪽에서 후보를 모아 우선순위로 고른다
— 이 스크립트는 정확히 같은 문자열이 겹치는 드문 경우만 처리한다.

사용법: python build_area_map.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
XLSX_PATH = HERE / "seoul_121_areas.xlsx"
OUT_PATH = HERE / "area_aliases.json"

# 교통 안내이므로 역을 우선한다: 인구밀집지역(역 이름 다수) > 발달상권 >
# 관광특구 > 공원 > 고궁·문화유산. 숫자가 작을수록 우선.
CATEGORY_PRIORITY = {
    "인구밀집지역": 0,
    "발달상권": 1,
    "관광특구": 2,
    "공원": 3,
    "고궁·문화유산": 4,
}

PAREN_RE = re.compile(r"^(?P<prefix>.*)\((?P<content>[^)]+)\)(?P<suffix>.*)$")


def gen_aliases(name: str) -> list[str]:
    aliases = [name]
    m = PAREN_RE.match(name)
    if m:
        prefix, content, suffix = m.group("prefix").strip(), m.group("content"), m.group("suffix").strip()
        if suffix:
            aliases.append(content + suffix)
            aliases.append(content)
        else:
            aliases.append(prefix)
    elif "·" in name:
        aliases.extend(part for part in name.split("·") if part)
    elif name.endswith("역"):
        aliases.append(name[:-1])
    return aliases


def load_rows() -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    ws = wb["장소목록"]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        category, _no, code, name, eng = row[0], row[1], row[2], row[3], row[4]
        if not code:
            continue
        rows.append({"category": category, "code": code, "name": name, "eng": eng})
    return rows


def main():
    rows = load_rows()
    result: dict[str, dict] = {}
    collisions = []

    for r in rows:
        record = {"code": r["code"], "name": r["name"], "category": r["category"]}
        aliases = gen_aliases(r["name"])
        if r["eng"]:
            aliases.append(r["eng"].casefold())

        for alias in aliases:
            alias = alias.strip()
            if not alias:
                continue
            existing = result.get(alias)
            if existing is not None and existing["code"] != record["code"]:
                collisions.append((alias, existing["code"], record["code"]))
                if (CATEGORY_PRIORITY.get(record["category"], 99)
                        >= CATEGORY_PRIORITY.get(existing["category"], 99)):
                    continue  # 기존 항목이 우선순위가 같거나 높으면 유지
            result[alias] = record

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(rows)}개 장소 → 별칭 {len(result)}개 → {OUT_PATH}")
    if collisions:
        print(f"동일 문자열 충돌 {len(collisions)}건 (우선순위로 해소):")
        for alias, old, new in collisions:
            print(f"  {alias!r}: {old} vs {new}")


if __name__ == "__main__":
    main()
