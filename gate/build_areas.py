"""
서울 주요 121개 장소 목록 빌드 스크립트 — 1회성 데이터 준비.

  seoul_121_areas.xlsx  장소명(한/영) · 코드 · 분류
  seoul_121_areas.zip   장소 영역 shapefile (경위도, 좌표계 WGS84 그대로)

두 파일을 합쳐 gate/seoul_areas.json 을 만든다. citydata_api.py 는 런타임에
이 JSON 만 읽는다 (openpyxl/zipfile 을 파이프라인 의존성에 넣지 않기 위함).
좌표는 shapefile 폴리곤의 바운딩박스 중심을 장소 중심좌표로 근사한다.

사용법: python build_areas.py
"""

from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
XLSX_PATH = HERE / "seoul_121_areas.xlsx"
ZIP_PATH = HERE / "seoul_121_areas.zip"
OUT_PATH = HERE / "seoul_areas.json"


def load_xlsx() -> dict[str, dict]:
    import openpyxl
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    ws = wb["장소목록"]
    out = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        category, no, code, name, eng = row[0], row[1], row[2], row[3], row[4]
        if not code:
            continue
        out[code] = {"code": code, "name": name, "eng": eng, "category": category}
    return out


def load_shapefile_centroids() -> dict[str, tuple[float, float]]:
    with zipfile.ZipFile(ZIP_PATH) as zf:
        shp_name = next(n for n in zf.namelist() if n.endswith(".shp"))
        dbf_name = next(n for n in zf.namelist() if n.endswith(".dbf"))
        shp_data = zf.read(shp_name)
        dbf_data = zf.read(dbf_name)

    # .dbf: 헤더 뒤 필드 디스크립터(32바이트씩), 0x0D 로 종료
    header_len, record_len = struct.unpack("<hh", dbf_data[8:12])
    fields, pos = [], 32
    while dbf_data[pos] != 0x0D:
        name = dbf_data[pos:pos + 11].split(b"\x00")[0].decode("utf-8", "replace")
        flen = dbf_data[pos + 16]
        fields.append((name, flen))
        pos += 32

    codes = []
    off = header_len
    while off < len(dbf_data) and dbf_data[off:off + 1] != b"\x1a":
        rec = dbf_data[off:off + record_len]
        off += record_len
        p = 1
        vals = {}
        for name, flen in fields:
            vals[name] = rec[p:p + flen].decode("utf-8", "replace").strip()
            p += flen
        codes.append(vals["AREA_CD"])

    # .shp: 100바이트 헤더 뒤 레코드마다 (헤더 8바이트 + shape타입 4 + bbox 32...)
    boxes = []
    off = 100
    while off < len(shp_data):
        _rec_num, content_len = struct.unpack(">ii", shp_data[off:off + 8])
        off += 8
        content = shp_data[off:off + content_len * 2]
        off += content_len * 2
        box = struct.unpack("<4d", content[4:36])  # xmin, ymin, xmax, ymax
        boxes.append(box)

    return {code: ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2) for code, b in zip(codes, boxes)}


def main():
    areas = load_xlsx()
    centroids = load_shapefile_centroids()
    merged = []
    for code, a in areas.items():
        lat, lon = centroids.get(code, (None, None))
        merged.append({**a, "lat": lat, "lon": lon})
    merged.sort(key=lambda a: a["code"])
    OUT_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(merged)}개 장소 → {OUT_PATH}")


if __name__ == "__main__":
    main()
