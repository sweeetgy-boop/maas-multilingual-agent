#!/usr/bin/env python3
"""
공항·항공사 코드 캐시 빌드 스크립트 → gate/airports.json

세 API 가 서로 다른 코드 체계를 쓴다. 이 파일이 그 사이를 잇는다.

  TAGO 국내항공운항정보   airportId  "NAARKSS"  (NAA + ICAO)
  인천국제공항공사        airportCode "GMP"      (IATA)
  한국공항공사           schAirCode  "GMP"      (IATA)

작업 0 실측 결과 (2026-09-04)
  - **서비스 URL 이 가이드 문서와 다르다.** 문서의
    `/1613000/DmstcFlightNvgInfoService` 는 NO_OPENAPI_SERVICE_ERROR(12) 다.
    실제 동작하는 경로는 `/1613000/DmstcFlightNvgInfo` — 고속버스가
    `ExpBusInfoService` 가 아니라 `ExpBusInfo` 였던 것과 같은 규칙이다.
    오퍼레이션명은 문서대로 대문자 시작(`GetArprtList`)이 맞다.
  - GetArprtList 는 airportId + airportNm(한글) **두 필드뿐**이다.
    IATA 도 영문명도 없다. 그런데 airportId 가 `NAA` + ICAO 구조라
    (NAARKSS=RKSS=김포) ICAO→IATA 표 15줄이면 매핑이 닫힌다.
    국내 공항은 15개가 전부이므로 이 표는 완결적이다.
  - GetAirmanList 는 airlineId(ICAO 3자) + airlineNm 뿐이다. IATA 가 없다.
    각 항공사로 운항정보를 한 번씩 조회해 **vihicleId 접두 2자에서 IATA 를
    뽑았다**(KAL→KE1007→"KE"). 13사 중 11사가 이 방법으로 채워진다.
    나머지 2사(에어제타·에어프레미아)는 조회일에 국내선이 없어 비어 있고,
    에어프레미아는 인천 응답에서 보충된다. 끝내 못 찾으면 null 로 둔다.
  - 인천 API 는 코드표를 따로 주지 않지만 응답 자체가 코드표다.
    4일치 출발·도착 9,402건에서 **공항 160개 · 항공사 109개**가 나왔다
    (가이드 문서 부록의 공항 97행·항공사 92행보다 넓다).
    한 airportCode 에 두 개 이상의 한글명이 붙는 충돌은 0건,
    flightId 접두 2자 → airline 매핑도 100개 전부 충돌 0건이었다.
  - 인천 응답의 한글 공항명 중 19개가 "도쿄/나리타" 처럼 `/` 로
    도시와 공항을 잇는다. 이걸 쪼개면 "도쿄"·"나리타" 두 별칭이 공짜로
    나온다 — 사용자는 둘 중 아무거나 부른다.

문서 부록 (작업 0 재실측, 2026-09-04)
  - **가이드 문서 3종을 gate/ 에 두고 직접 파싱한다.** python-docx 가 없어
    zipfile + xml.etree 로 word/document.xml 을 읽는다. docx 는 zip 이라
    표준 라이브러리만으로 표를 꺼낼 수 있다.
  - icn_airport_guide.docx 부록: **항공사 91행(IATA+ICAO+국문)**,
    **공항 97행 × 2열 = 186개**(A-K / L-Z 를 한 표에 두 벌로 배치).
  - 부록과 API 수확본은 겹치지 않는 쪽이 많아 **둘 다 넣는다**:
      공항   부록 186 · API 172 → 합집합 **236**
      항공사 부록  91 · API 111 → 합집합 **142**
    부록에만 있는 공항이 64개(ANC·BOM·CAI…), API 에만 있는 것이 50개
    (ADD·BOS·CJJ…)다. 한쪽만 쓰면 그만큼이 빈다.
  - **영문 공항명은 한국공항공사에서 온다.** 부록도 인천 API 도 한글명만
    주는데, KAC /depart·/arrival 은 depAirportEng/arrAirportEng 를 함께
    준다(TAOYUAN, JEJU …). 영어 질의를 받으려면 이 이름이 필요하다.

인천 API 호출 최소화 (제약 7)
  인천은 **일일 500회**로 셋 중 가장 적다. 코드표는 이제 문서 부록이
  대신하므로 여기서는 **하루치 출발·도착 2회만** 부른다(이전 4일치 8회에서
  줄였다). KAC 는 오퍼레이션당 5,000회라 하루치 페이징(약 16회)이 부담이
  아니다.

별칭 방침
  자동 생성(한글명·`/` 분해·접미사 제거·IATA·ICAO)에 더해 중국어·일본어·
  영문 표기는 CURATED_ALIASES 에 손으로 넣는다. 5개 언어를 지원하므로
  "타이페이"·"TPE"·"타오위안"이 한 공항으로 모여야 한다.
  자동 생성만으로는 한글 표기 하나뿐이라 부족하다.

access_note 방침
  **철도가 실제로 닿는 공항에만** 넣는다(기능 10). 버스만 있는 공항에
  "대중교통으로 연결됩니다" 같은 문장을 채우면 없는 노선을 만드는 것과
  같다. 확인되지 않으면 null 로 둔다.

사용법: python build_airports.py
"""

from __future__ import annotations

import itertools
import json
import os
import re
import sys
import zipfile
from xml.etree import ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote

import httpx

HERE = Path(__file__).parent
OUT_PATH = HERE / "airports.json"
ICN_DOC_PATH = HERE / "icn_airport_guide.docx"
TRANSIT_NODES_PATH = HERE / "transit_nodes.json"

TAGO_BASE = "https://apis.data.go.kr/1613000/DmstcFlightNvgInfo"
ICN_BASE = "https://apis.data.go.kr/B551177/statusOfAllFltDeOdp"
KEY = unquote(os.environ.get("DATA_GO_KR_KEY_ENC", ""))
TIMEOUT = 40.0

KAC_BASE = "https://apis.data.go.kr/B551178/flight-status"

# 인천은 일일 500회다(제약 7). 코드표는 문서 부록이 대신하므로 하루치
# 출발·도착 2회만 부른다. KAC 는 오퍼레이션당 5,000회라 여유가 있다.
HARVEST_DAYS = 1

# KAC 는 numOfRows 상한이 100 이다(실측: 200 이상 HTTP_ERROR 04).
KAC_ROWS = 100

# 어느 API 가 그 공항을 서비스하는가. 실시간 조회 분기에 쓴다.
#   IIAC 인천국제공항공사  statusOfAllFltDeOdp   (ICN 전용)
#   KAC  한국공항공사      flight-status          (그 외 국내 14개)
# 운영 주체와는 다르다 — 양양은 양양국제공항공사가 운영하지만 운항정보는
# KAC 피드에 실려 온다(실측에서 YNY 2편 확인). 여기서 말하는 operator 는
# "어느 API 를 부를 것인가" 다.
OPERATOR_IIAC = "IIAC"
OPERATOR_KAC = "KAC"


# ── 국내 공항 15개: ICAO → IATA + 도시·영문명 ────────────────────
# GetArprtList 가 주지 않는 정보다. 국내 공항은 15개로 고정이라
# 여기서 표를 닫는다. airportId 의 `NAA` 를 뗀 나머지가 ICAO 다.
DOMESTIC: dict[str, dict] = {
    "RKSS": {"iata": "GMP", "en": "Gimpo",     "city_ko": "서울"},
    "RKSI": {"iata": "ICN", "en": "Incheon",   "city_ko": "인천"},
    "RKPC": {"iata": "CJU", "en": "Jeju",      "city_ko": "제주"},
    "RKPK": {"iata": "PUS", "en": "Gimhae",    "city_ko": "부산"},
    "RKTN": {"iata": "TAE", "en": "Daegu",     "city_ko": "대구"},
    "RKTU": {"iata": "CJJ", "en": "Cheongju",  "city_ko": "청주"},
    "RKJJ": {"iata": "KWJ", "en": "Gwangju",   "city_ko": "광주"},
    "RKJB": {"iata": "MWX", "en": "Muan",      "city_ko": "무안"},
    "RKNY": {"iata": "YNY", "en": "Yangyang",  "city_ko": "양양"},
    "RKPU": {"iata": "USN", "en": "Ulsan",     "city_ko": "울산"},
    "RKJY": {"iata": "RSU", "en": "Yeosu",     "city_ko": "여수"},
    "RKPS": {"iata": "HIN", "en": "Sacheon",   "city_ko": "사천"},
    "RKNW": {"iata": "WJU", "en": "Wonju",     "city_ko": "원주"},
    "RKJK": {"iata": "KUV", "en": "Gunsan",    "city_ko": "군산"},
    "RKTH": {"iata": "KPO", "en": "Pohang",    "city_ko": "포항"},
}

# ── 같은 도시권의 공항 묶음 (기능 6 대안 공항) ──────────────────
# "인천에서 제주" 처럼 노선이 없는 조합에서 같은 도시권의 다른 공항을
# 제시하는 데 쓴다. 서울 수도권만 공항이 둘이라 실질적으로 이 한 묶음이다.
CITY_GROUP: dict[str, str] = {"GMP": "서울", "ICN": "서울"}

# ── 철도가 실제로 닿는 공항만 (기능 10) ──────────────────────────
# 버스만 있는 공항은 넣지 않는다. 지어내는 것보다 비워두는 게 낫다.
ACCESS_NOTES: dict[str, str] = {
    "GMP": "김포공항은 지하철 5호선·9호선, 공항철도, 김포골드라인이 연결됩니다",
    "ICN": "인천공항은 공항철도(제1여객터미널역·제2여객터미널역)가 연결됩니다",
    "PUS": "김해공항은 부산김해경전철 공항역이 연결됩니다",
}

# ── 다국어 별칭 (자동 생성으로는 안 나오는 표기) ──────────────────
# 한국어 외 4개 언어(영어·중국어·일본어·인도네시아어) 질의를 받으므로
# 사용자가 실제로 쓰는 표기를 넣는다. 여기 없는 공항은 자동 생성
# 별칭(한글명·IATA·ICAO)만 갖는다.
CURATED_ALIASES: dict[str, list[str]] = {
    # 국내
    "GMP": ["김포", "김포공항", "김포국제공항", "Gimpo", "Gimpo Airport", "金浦",
            "金浦机场", "金浦空港", "ソウル/金浦", "Bandara Gimpo"],
    "ICN": ["인천", "인천공항", "인천국제공항", "Incheon", "Incheon Airport", "仁川",
            "仁川机场", "仁川空港", "インチョン", "Bandara Incheon"],
    "CJU": ["제주", "제주공항", "제주국제공항", "제주도", "Jeju", "Jeju Airport",
            "济州", "济州岛", "濟州", "済州", "済州島", "チェジュ", "Jeju Island"],
    "PUS": ["부산", "김해", "김해공항", "김해국제공항", "부산공항", "Busan", "Gimhae",
            "釜山", "金海", "釜山金海", "プサン", "キメ", "Bandara Busan"],
    "TAE": ["대구", "대구공항", "대구국제공항", "Daegu", "大邱", "大邱机场", "テグ"],
    "CJJ": ["청주", "청주공항", "청주국제공항", "Cheongju", "清州", "淸州", "清州空港"],
    "KWJ": ["광주", "광주공항", "Gwangju", "光州", "光州机场", "クァンジュ"],
    "MWX": ["무안", "무안공항", "무안국제공항", "Muan", "务安", "務安"],
    "YNY": ["양양", "양양공항", "양양국제공항", "속초", "강릉", "Yangyang", "襄阳", "襄陽"],
    "USN": ["울산", "울산공항", "Ulsan", "蔚山", "ウルサン"],
    "RSU": ["여수", "여수공항", "순천", "Yeosu", "丽水", "麗水"],
    "HIN": ["사천", "사천공항", "진주", "진주공항", "Sacheon", "泗川", "晋州"],
    "WJU": ["원주", "원주공항", "횡성", "Wonju", "原州"],
    "KUV": ["군산", "군산공항", "Gunsan", "群山"],
    "KPO": ["포항", "포항공항", "포항경주공항", "경주", "Pohang", "浦项", "浦項"],
    # 국제 주요 (인천 응답에 한글명 하나만 오므로 나머지 표기를 보탠다)
    "NRT": ["나리타", "도쿄", "동경", "Narita", "Tokyo", "成田", "東京", "东京", "成田空港"],
    "HND": ["하네다", "도쿄", "동경", "Haneda", "Tokyo", "羽田", "東京", "东京", "羽田空港"],
    "KIX": ["간사이", "오사카", "Kansai", "Osaka", "関西", "关西", "大阪"],
    "CTS": ["삿포로", "신치토세", "Sapporo", "Chitose", "札幌", "新千歳"],
    "FUK": ["후쿠오카", "Fukuoka", "福冈", "福岡"],
    "NGO": ["나고야", "주부", "Nagoya", "Chubu", "名古屋", "中部"],
    "OKA": ["오키나와", "나하", "Okinawa", "Naha", "冲绳", "沖縄", "那覇"],
    "TPE": ["타이베이", "타이페이", "대만", "타오위안", "Taipei", "Taiwan", "Taoyuan",
            "台北", "臺北", "桃園", "桃园", "タイペイ", "台湾"],
    "PVG": ["상하이", "상해", "푸동", "포동", "Shanghai", "Pudong", "上海", "浦东", "浦東"],
    "PEK": ["베이징", "북경", "서우두", "Beijing", "Peking", "北京", "首都"],
    "PKX": ["베이징", "북경", "다싱", "Beijing", "Daxing", "北京", "大兴", "大興"],
    "HKG": ["홍콩", "Hong Kong", "香港", "ホンコン"],
    "BKK": ["방콕", "수완나품", "태국", "Bangkok", "Suvarnabhumi", "曼谷", "バンコク"],
    "SIN": ["싱가포르", "창이", "Singapore", "Changi", "新加坡", "シンガポール"],
    "CGK": ["자카르타", "수카르노하타", "Jakarta", "Soekarno-Hatta", "雅加达",
            "Bandara Soekarno-Hatta", "ジャカルタ"],
    "DPS": ["발리", "덴파사르", "Bali", "Denpasar", "巴厘岛", "バリ", "Bandara Ngurah Rai"],
    "KUL": ["쿠알라룸푸르", "Kuala Lumpur", "吉隆坡", "クアラルンプール"],
    "MNL": ["마닐라", "Manila", "马尼拉", "マニラ"],
    "HAN": ["하노이", "Hanoi", "河内", "ハノイ"],
    "SGN": ["호치민", "사이공", "Ho Chi Minh", "Saigon", "胡志明", "ホーチミン"],
    "DAD": ["다낭", "Da Nang", "岘港", "ダナン"],
    "LAX": ["로스앤젤레스", "엘에이", "Los Angeles", "洛杉矶", "ロサンゼルス"],
    "JFK": ["뉴욕", "존에프케네디", "New York", "紐約", "纽约", "ニューヨーク"],
    "SFO": ["샌프란시스코", "San Francisco", "旧金山", "サンフランシスコ"],
    "SEA": ["시애틀", "타코마", "Seattle", "Tacoma", "西雅图", "シアトル"],
    "ORD": ["시카고", "오헤어", "Chicago", "O'Hare", "芝加哥", "シカゴ"],
    "ATL": ["애틀랜타", "Atlanta", "亚特兰大", "アトランタ"],
    "DFW": ["댈러스", "포트워스", "Dallas", "Fort Worth", "达拉斯", "ダラス"],
    "YVR": ["밴쿠버", "Vancouver", "温哥华", "バンクーバー"],
    "LHR": ["런던", "히드로", "London", "Heathrow", "伦敦", "ロンドン"],
    "CDG": ["파리", "샤를드골", "Paris", "Charles de Gaulle", "巴黎", "パリ"],
    "FRA": ["프랑크푸르트", "Frankfurt", "法兰克福", "フランクフルト"],
    "AMS": ["암스테르담", "스키폴", "Amsterdam", "Schiphol", "阿姆斯特丹"],
    "IST": ["이스탄불", "터키", "Istanbul", "伊斯坦布尔", "イスタンブール"],
    "DXB": ["두바이", "Dubai", "迪拜", "ドバイ"],
    "DOH": ["도하", "카타르", "Doha", "多哈", "ドーハ"],
    "SYD": ["시드니", "Sydney", "悉尼", "シドニー"],
    "ULN": ["울란바토르", "몽골", "Ulaanbaatar", "乌兰巴托"],
    "UBN": ["울란바토르", "신 울란바타르", "Ulaanbaatar", "乌兰巴托"],
    "GUM": ["괌", "Guam", "关岛", "グアム"],
    "SPN": ["사이판", "Saipan", "塞班", "サイパン"],
}

# ── 항공사 별칭 ─────────────────────────────────────────────
# 항공사명은 두 API 가 띄어쓰기를 다르게 준다("아시아나 항공" / "아시아나항공").
# 정규화로 흡수하되, 사용자가 부르는 통칭·영문명은 여기서 보탠다.
CURATED_AIRLINE_ALIASES: dict[str, list[str]] = {
    "KE": ["대한항공", "대한", "Korean Air", "KAL", "大韓航空", "大韩航空", "コリアンエア"],
    "OZ": ["아시아나", "아시아나항공", "Asiana", "Asiana Airlines", "韩亚航空", "アシアナ"],
    "7C": ["제주항공", "제주에어", "Jeju Air", "济州航空", "チェジュ航空"],
    "LJ": ["진에어", "Jin Air", "真航空", "ジンエアー"],
    "TW": ["티웨이", "티웨이항공", "T'way", "Tway Air", "德威航空"],
    "BX": ["에어부산", "Air Busan", "釜山航空", "エアプサン"],
    "RS": ["에어서울", "Air Seoul", "首尔航空", "エアソウル"],
    "ZE": ["이스타", "이스타항공", "Eastar Jet", "易斯达航空"],
    "RF": ["에어로케이", "에어로 K", "Aero K"],
    "YP": ["에어프레미아", "Air Premia"],
    "XU": ["섬에어", "Sum Air"],
    "WE": ["파라타항공", "파라타", "Parata Air"],
}

_SUFFIX_RE = re.compile(r"(국제공항|공항|공항공사)$")


# ── docx 부록 파서 ──────────────────────────────────────
# python-docx 없이 읽는다. docx 는 zip 이고 본문은 word/document.xml 이라
# 표준 라이브러리만으로 표를 꺼낼 수 있다. 의존성을 늘리지 않는 편이
# 배포(빌드 환경에 pip install 이 없다)에 안전하다.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_tables(path: Path) -> list[list[list[str]]]:
    """docx 의 모든 표를 [표][행][셀] 문자열로 돌려준다.
    파일이 없거나 깨졌으면 빈 리스트 — 부록이 없어도 빌드는 계속된다."""
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        print(f"  ! {path.name} 을 읽지 못했습니다. 부록 없이 진행합니다.",
              file=sys.stderr)
        return []

    def text_of(el) -> str:
        return "".join(t.text or "" for t in el.iter(f"{_W}t")).strip()

    out = []
    for tbl in root.iter(f"{_W}tbl"):
        out.append([[text_of(tc) for tc in tr.findall(f"{_W}tc")]
                    for tr in tbl.findall(f"{_W}tr")])
    return out


def load_icn_appendix() -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """인천공항 가이드 부록에서 코드표를 꺼낸다.

    반환: ({공항IATA: 한글명}, {항공사IATA: (ICAO, 한글명)})

    표를 순서(13번째·14번째)로 집지 않고 **헤더 문구로 찾는다.** 문서가
    개정되면 표 순서는 바뀌어도 헤더는 남는다.
      항공사 표  ['IATA 코드', 'ICAO 코드', '항공사명']            91행
      공항 표    ['공항코드(A-K)', '공항명', '공항코드(L-Z)', '공항명']
                 97행 × 2열 = 186개 (A-K 와 L-Z 를 한 표에 두 벌로 둔다)
    """
    airports: dict[str, str] = {}
    airlines: dict[str, tuple[str, str]] = {}
    for tbl in _docx_tables(ICN_DOC_PATH):
        if not tbl or not tbl[0]:
            continue
        head = "|".join(tbl[0])
        if "IATA" in head and "ICAO" in head:
            for row in tbl[1:]:
                if len(row) >= 3 and row[0]:
                    airlines[row[0].strip()] = (row[1].strip(), row[2].strip())
        elif "공항코드" in head:
            for row in tbl[1:]:
                # 한 행에 (A-K 코드, 이름, L-Z 코드, 이름) 두 쌍이 들어 있다
                for i in (0, 2):
                    if len(row) > i + 1 and row[i] and row[i + 1]:
                        airports[row[i].strip()] = row[i + 1].strip()
    return airports, airlines


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _items(body: dict) -> list[dict]:
    """items 정규화. 두 API 의 모양이 다르고, 1건일 때 단일 객체로 오는
    공공데이터포털 특유의 문제도 있다(제약 4).

      TAGO  body.items.item[]     ← dict 로 한 겹 감싼다
      인천   body.items[]          ← 래퍼 없이 바로 리스트
    """
    it = body.get("items")
    if isinstance(it, dict):
        it = it.get("item")
    if it is None:
        return []
    if isinstance(it, dict):        # 1건이 단일 객체로 온 경우
        return [it]
    return it if isinstance(it, list) else []


def _get(url: str, params: dict) -> dict | None:
    try:
        r = httpx.get(url, params={"serviceKey": KEY, **params}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["response"]["body"]
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        print(f"  ! {url.rsplit('/', 1)[-1]} {params.get('searchDate') or ''} 실패: "
              f"{type(e).__name__}", file=sys.stderr)
        return None


def _norm(s: str) -> str:
    """항공사명 비교용. 두 API 의 띄어쓰기가 다르다."""
    return re.sub(r"\s+", "", s or "")


# ─────────────────────────────────────────────────────────
def fetch_tago_airports() -> list[dict]:
    body = _get(f"{TAGO_BASE}/GetArprtList",
                {"numOfRows": 200, "pageNo": 1, "_type": "json"})
    if body is None:
        return []
    return _items(body)


def fetch_tago_airlines() -> list[dict]:
    body = _get(f"{TAGO_BASE}/GetAirmanList",
                {"numOfRows": 200, "pageNo": 1, "_type": "json"})
    if body is None:
        return []
    return _items(body)


def derive_airline_iata(airline_ids: list[str], airport_ids: list[str]) -> dict[str, str]:
    """TAGO 는 IATA 를 주지 않는다. 항공사별로 운항정보를 조회해
    vihicleId 접두 2자에서 IATA 를 뽑는다 (KAL → KE1007 → "KE").

    간선 노선 8개를 먼저 훑고, 거기서 안 잡힌 항공사만 15개 공항의
    전체 조합으로 넓힌다. 섬에어처럼 지선만 운항하는 항공사는 간선에
    안 나오지만, 남는 항공사가 두셋뿐이라 넓혀도 호출이 얼마 안 된다."""
    trunk = [("NAARKSS", "NAARKPC"), ("NAARKPC", "NAARKSS"),
             ("NAARKSS", "NAARKPK"), ("NAARKPK", "NAARKPC"),
             ("NAARKTU", "NAARKPC"), ("NAARKSI", "NAARKPK"),
             ("NAARKPC", "NAARKTN"), ("NAARKPC", "NAARKNY")]
    day = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
    out: dict[str, str] = {}

    def probe(aid: str, dep: str, arr: str) -> str | None:
        body = _get(f"{TAGO_BASE}/GetFlightOpratInfoList",
                    {"numOfRows": 5, "pageNo": 1, "_type": "json",
                     "depAirportId": dep, "arrAirportId": arr,
                     "depPlandTime": day, "airlineId": aid})
        return next((m.group(1) for x in _items(body or {})
                     if (m := re.match(r"^([A-Z0-9]{2})\d", x.get("vihicleId") or ""))),
                    None)

    for aid in airline_ids:
        for dep, arr in trunk:
            if hit := probe(aid, dep, arr):
                out[aid] = hit
                break

    rest = [a for a in airline_ids if a not in out]
    if rest:
        print(f"  간선에서 못 찾은 {len(rest)}사는 전체 조합으로 재탐색…")
        for aid in rest:
            for dep, arr in itertools.permutations(airport_ids, 2):
                if hit := probe(aid, dep, arr):
                    out[aid] = hit
                    break
    return out


def harvest_incheon() -> tuple[dict[str, str], dict[str, str]]:
    """인천 응답에서 공항 코드표와 항공사 코드표를 긁는다.

    반환: ({IATA: 한글공항명}, {IATA항공사코드: 한글항공사명})

    항공사 IATA 는 flightId 접두 2자에서 나온다. 실측상 100개 코드
    전부 충돌이 없었다. 코드셰어 Slave 편은 마케팅 항공사의 편명을
    쓰므로 이 매핑에 그대로 유효하다.

    **호출을 하루치 2회로 줄였다**(제약 7 — 인천은 일일 500회). 예전에는
    4일치 8회를 불렀지만, 이제 문서 부록이 코드표의 대부분을 대신한다."""
    airports: dict[str, str] = {}
    airlines: dict[str, str] = {}
    for off in range(HARVEST_DAYS):
        day = (date.today() + timedelta(days=off)).strftime("%Y%m%d")
        for op in ("getFltDeparturesDeOdp", "getFltArrivalsDeOdp"):
            body = _get(f"{ICN_BASE}/{op}",
                        {"type": "json", "numOfRows": 2000, "pageNo": 1,
                         "searchDate": day})
            rows = _items(body or {})
            print(f"  인천 {day} {op[6:-6]:<10} {len(rows):>5}건")
            for x in rows:
                code, name = x.get("airportCode"), x.get("airport")
                if code and name:
                    airports.setdefault(code.strip(), name.strip())
                m = re.match(r"^([A-Z0-9]{2})\d", x.get("flightId") or "")
                if m and x.get("airline"):
                    airlines.setdefault(m.group(1), x["airline"].strip())
    return airports, airlines


def harvest_kac() -> dict[str, tuple[str, str]]:
    """한국공항공사에서 공항 코드 → (한글명, 영문명) 을 긁는다.

    부록도 인천 API 도 한글명만 준다. KAC 의 /depart·/arrival 만
    depAirportEng/arrAirportEng 를 함께 줘서(TAOYUAN, JEJU …) 영어 질의를
    받으려면 이 이름이 필요하다.

    주의: 두 오퍼레이션의 필드명이 미묘하게 다르다 — /depart 는
    **arrvAirportCode**(v 가 있다), /arrival 은 arrAirportCode 다.
    같은 서비스인데 이름이 갈리므로 양쪽을 다 본다.

    numOfRows 상한은 100 이다(200 이상 HTTP_ERROR 04). 하루치가 약
    720편이라 오퍼레이션당 8페이지면 끝난다."""
    out: dict[str, tuple[str, str]] = {}
    day = date.today().strftime("%Y%m%d")
    for op in ("depart", "arrival"):
        got = 0
        for page in range(1, 12):
            body = _get(f"{KAC_BASE}/{op}",
                        {"numOfRows": KAC_ROWS, "pageNo": page, "type": "json",
                         "searchday": day})
            if body is None:
                break
            rows = _items(body)
            got += len(rows)
            for x in rows:
                # 출발·도착 양쪽 공항을 모두 담는다. 코드 필드명이
                # 오퍼레이션마다 달라 두 이름을 모두 본다.
                for code_key, ko_key, en_key in (
                        ("depAirportCode", "depAirport", "depAirportEng"),
                        ("arrAirportCode", "arrAirport", "arrAirportEng"),
                        ("arrvAirportCode", "arrAirport", "arrAirportEng")):
                    code = (x.get(code_key) or "").strip()
                    ko = (x.get(ko_key) or "").strip()
                    en = (x.get(en_key) or "").strip()
                    if code and ko:
                        out.setdefault(code, (ko, en))
            if got >= int(body.get("totalCount") or 0):
                break
        print(f"  KAC {op:<8} {got:>5}건")
    return out


def load_coords() -> dict[str, tuple[float, float]]:
    """transit_nodes.json 의 airport 노드에서 좌표를 가져온다.
    기능 10(공항까지 가는 연계 경로)이 좌표를 쓴다."""
    try:
        nodes = json.loads(TRANSIT_NODES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for n in nodes:
        if n.get("type") != "airport":
            continue
        out[_SUFFIX_RE.sub("", n["name"])] = (n.get("lat"), n.get("lon"))
    return out


def build_aliases(iata: str, icao: str | None, name_ko: str,
                  name_en: str | None, city_ko: str | None) -> list[str]:
    """자동 생성 + 손으로 넣은 별칭을 합친다.

    자동 생성은 네 갈래다:
      한글명 그대로 / 접미사("국제공항")를 뗀 형태 /
      "도쿄/나리타" 의 `/` 분해 / IATA·ICAO 코드
    """
    out: list[str] = [iata, name_ko]
    if icao:
        out.append(icao)
    if name_en:
        out.append(name_en)
    if city_ko:
        out.append(city_ko)
    stripped = _SUFFIX_RE.sub("", name_ko).strip()
    if stripped:
        out.append(stripped)
    for part in re.split(r"[/·]", name_ko):       # "도쿄/나리타" → 도쿄, 나리타
        part = part.strip()
        if part:
            out.append(part)
            s = _SUFFIX_RE.sub("", part).strip()
            if s:
                out.append(s)
    out.extend(CURATED_ALIASES.get(iata, []))

    seen: set[str] = set()
    uniq = []
    for a in out:
        a = (a or "").strip()
        if a and a.casefold() not in seen:
            seen.add(a.casefold())
            uniq.append(a)
    return uniq


# ─────────────────────────────────────────────────────────
def main() -> int:
    if not KEY:
        _die("DATA_GO_KR_KEY_ENC 가 없습니다. .env 를 확인하세요.")

    print("TAGO 공항·항공사 목록…")
    tago_ports = fetch_tago_airports()
    tago_airlines = fetch_tago_airlines()
    print(f"  공항 {len(tago_ports)} · 항공사 {len(tago_airlines)}")
    if not tago_ports:
        _die("TAGO 공항 목록을 받지 못했습니다. 중단합니다.")

    print("인천공항 가이드 부록…")
    doc_ports, doc_airlines = load_icn_appendix()
    print(f"  공항 {len(doc_ports)} · 항공사 {len(doc_airlines)}")

    print(f"인천 API 수확 ({HARVEST_DAYS}일치, 제약 7 로 호출 최소화)…")
    icn_ports, icn_airlines = harvest_incheon()
    print(f"  공항 {len(icn_ports)} · 항공사 {len(icn_airlines)}")

    print("한국공항공사 수확 (영문 공항명)…")
    kac_ports = harvest_kac()
    print(f"  공항 {len(kac_ports)}")

    print("TAGO 항공사 IATA 유도 (편명 접두)…")
    iata_by_tago = derive_airline_iata(
        [a["airlineId"] for a in tago_airlines],
        [(x.get("airportId") or "").strip() for x in tago_ports])
    print(f"  {len(iata_by_tago)}/{len(tago_airlines)}개 유도")

    coords = load_coords()

    # ── 공항 병합 ────────────────────────────────────────
    # 1) TAGO 국내 15개가 기준. tago_id·좌표·operator·access_note 를 갖는다.
    # 2) 부록 186개 + 인천 수확 + KAC 수확(영문명)을 얹는다.
    #    셋은 겹치지 않는 쪽이 많아 합집합이 각각보다 크다.
    airports: dict[str, dict] = {}
    for x in tago_ports:
        aid = (x.get("airportId") or "").strip()
        icao = aid[3:] if aid.startswith("NAA") else None
        meta = DOMESTIC.get(icao or "")
        if not meta:
            print(f"  ! 미등록 국내 공항 {aid} {x.get('airportNm')} — "
                  f"DOMESTIC 표에 추가가 필요합니다", file=sys.stderr)
            continue
        iata = meta["iata"]
        name_ko = (x.get("airportNm") or "").strip()
        lat, lon = coords.get(_SUFFIX_RE.sub("", name_ko), (None, None))
        airports[iata] = {
            "iata": iata, "icao": icao, "tago_id": aid,
            "name_ko": name_ko, "name_en": meta["en"], "city_ko": meta["city_ko"],
            "domestic": True,
            "operator": OPERATOR_IIAC if iata == "ICN" else OPERATOR_KAC,
            "lat": lat, "lon": lon,
            "city_group": CITY_GROUP.get(iata),
            "access_note": ACCESS_NOTES.get(iata),
            "aliases": [],
        }

    def add_foreign(code: str, name_ko: str, name_en: str | None = None) -> None:
        # 출처가 "도쿄/ 나리타"·"오사카/ 간사이" 처럼 슬래시 뒤에 공백을
        # 흘린다. 별칭 분해와 표시 양쪽에 걸리므로 여기서 한 번 정리한다.
        code = code.strip()
        name_ko = re.sub(r"\s*/\s*", "/", (name_ko or "").strip())
        if not code or not name_ko:
            return
        cur = airports.get(code)
        if cur is None:
            airports[code] = {
                "iata": code, "icao": None, "tago_id": None,
                "name_ko": name_ko, "name_en": name_en or None, "city_ko": None,
                "domestic": False, "operator": None,
                "lat": None, "lon": None, "city_group": None,
                "access_note": None, "aliases": [],
            }
        elif not cur.get("name_en") and name_en:
            cur["name_en"] = name_en          # 영문명만 뒤늦게 채운다

    for code, name_ko in sorted(doc_ports.items()):
        add_foreign(code, name_ko)
    for code, name_ko in sorted(icn_ports.items()):
        add_foreign(code, name_ko)
    for code, (name_ko, name_en) in sorted(kac_ports.items()):
        add_foreign(code, name_ko, name_en)

    for a in airports.values():
        a["aliases"] = build_aliases(a["iata"], a["icao"], a["name_ko"],
                                     a["name_en"], a["city_ko"])

    # ── 항공사 병합 ──────────────────────────────────────
    # TAGO(ICAO 3자) · 부록(IATA+ICAO) · 인천(편명 접두 IATA) 셋을 합친다.
    # 이름 비교는 띄어쓰기를 지우고 한다 — 두 API 가 "아시아나 항공"과
    # "아시아나항공"으로 다르게 준다.
    airlines: dict[str, dict] = {}

    for code, (icao, name_ko) in sorted(doc_airlines.items()):
        airlines[code] = {"iata": code, "icao": icao or None, "tago_id": None,
                          "name_ko": name_ko, "domestic": False, "aliases": []}

    by_name = {_norm(v[1]): k for k, v in doc_airlines.items()}
    by_name.update({_norm(v): k for k, v in icn_airlines.items()})

    for a in tago_airlines:
        tid, name_ko = a["airlineId"], (a.get("airlineNm") or "").strip()
        iata = iata_by_tago.get(tid) or by_name.get(_norm(name_ko))
        key = iata or tid
        cur = airlines.get(key, {})
        airlines[key] = {"iata": iata, "icao": cur.get("icao") or tid,
                         "tago_id": tid,
                         "name_ko": cur.get("name_ko") or name_ko,
                         "domestic": True, "aliases": []}

    for code, name_ko in sorted(icn_airlines.items()):
        if code not in airlines:
            airlines[code] = {"iata": code, "icao": None, "tago_id": None,
                              "name_ko": name_ko, "domestic": False, "aliases": []}

    for a in airlines.values():
        base = [a["name_ko"], _norm(a["name_ko"])]
        if a.get("iata"):
            base.append(a["iata"])
        if a.get("icao"):
            base.append(a["icao"])
        base.extend(CURATED_AIRLINE_ALIASES.get(a.get("iata") or "", []))
        seen, uniq = set(), []
        for x in base:
            x = (x or "").strip()
            if x and x.casefold() not in seen:
                seen.add(x.casefold())
                uniq.append(x)
        a["aliases"] = uniq

    missing = [a["name_ko"] for a in airlines.values()
               if a["domestic"] and not a["iata"]]
    if missing:
        print(f"  ! IATA 를 못 찾은 국내 항공사: {', '.join(missing)} "
              f"(조회일에 국내선이 없는 항공사입니다. null 로 둡니다)",
              file=sys.stderr)

    out = {
        "generated_at": date.today().isoformat(),
        "source": {
            "tago": f"{TAGO_BASE}/GetArprtList, GetAirmanList",
            "incheon_doc": f"{ICN_DOC_PATH.name} 부록 (공항 {len(doc_ports)}, "
                           f"항공사 {len(doc_airlines)})",
            "incheon_api": f"{ICN_BASE} ({HARVEST_DAYS}일치 출발·도착)",
            "kac": f"{KAC_BASE} (하루치 depart·arrival, 영문 공항명)",
        },
        "airports": sorted(airports.values(),
                           key=lambda x: (not x["domestic"], x["iata"])),
        "airlines": sorted(airlines.values(),
                           key=lambda x: (not x["domestic"], x["iata"] or "ZZ")),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    dom = sum(1 for a in out["airports"] if a["domestic"])
    with_en = sum(1 for a in out["airports"] if a.get("name_en"))
    print(f"\n{OUT_PATH.name}: 공항 {len(out['airports'])}개(국내 {dom}, "
          f"영문명 {with_en}) · 항공사 {len(out['airlines'])}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
