# -*- coding: utf-8 -*-
"""
근로복지공단 판례 수집 + 추출 모듈 (실제 스펙 기준)
=====================================================
엔드포인트: getSjbPrecedentNaeyongPstate (산재 판결문 내용정보 조회)
  요청: ServiceKey, pageNo, numOfRows (필수) + kindA(사건결과)/kindB(사건유형)/kindC(질병구분)
  응답: accnum, courtname, kinda, kindb, kindc, title, noncontent(판결문 원문)

핵심: 노출요인·노출기간·결정적 서류는 API에 없고 noncontent에서 '추출'한다.
      결과(kinda)는 구조화값 그대로 사용.

stdlib만 사용 (colab에서 pip install 불필요).
  실호출:  fetch_judgments("디코딩키", rows=5)
  오프라인: python3 comwel_fetch.py   # mock 응답으로 파싱+추출 검증
"""

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = "http://apis.data.go.kr/B490001/sjbPrecedentInfoService"
OP_CONTENT = "getSjbPrecedentNaeyongPstate"          # 판결문 내용
OP_KIND_DISEASE = "getSjbSagoJilbyeongGubunPstate"   # kindC 후보값
OP_KIND_RESULT = "getSjbPrecedentResultYuhyeongPstate"  # kindA 후보값
OP_KIND_CASE = "getSjbSageonYuhyeongPstate"          # kindB 후보값


# ── HTTP (stdlib) ───────────────────────────────────────────
def _get(op, params, timeout=15):
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{BASE}/{op}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "sanjae/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _parse(text):
    """실제 응답은 XML. <header><resultCode><resultMsg>, <body><items><item>...</item>."""
    if "{" in text[:2]:                      # 혹시 JSON이면 폴백
        d = json.loads(text)
        resp = d.get("response", d)
        h = resp.get("header", {})
        b = resp.get("body", {})
        items = b.get("items", {})
        items = items.get("item", []) if isinstance(items, dict) else items
        if isinstance(items, dict):
            items = [items]
        return h.get("resultCode"), h.get("resultMsg"), b.get("totalCount"), (items or [])
    root = ET.fromstring(text)
    code = root.findtext("./header/resultCode")
    msg = root.findtext("./header/resultMsg")
    total = root.findtext("./body/totalCount")
    items = []
    for it in root.findall("./body/items/item"):
        items.append({child.tag: (child.text or "") for child in it})
    return code, msg, total, items


# ── 수집 ────────────────────────────────────────────────────
def fetch_judgments(service_key, page=1, rows=10, kindA=None, kindB=None, kindC=None):
    """판결문 내용 조회. 반환: 원시 판례 dict 리스트(+추출 필드 포함)."""
    code, msg, total, items = _parse(_get(OP_CONTENT, {
        "ServiceKey": service_key, "pageNo": page, "numOfRows": rows,
        "kindA": kindA, "kindB": kindB, "kindC": kindC,
    }))
    if str(code).zfill(2) != "00" and "NORMAL" not in str(msg).upper():
        raise RuntimeError(f"API 오류 resultCode={code} {msg}")
    return [normalize(it) for it in items], total


def fetch_kind_values(service_key, which="disease"):
    """kindA/B/C에 넣을 수 있는 후보값 목록 조회(필터 UI 구성용)."""
    op = {"disease": OP_KIND_DISEASE, "result": OP_KIND_RESULT, "case": OP_KIND_CASE}[which]
    _, _, _, items = _parse(_get(op, {"ServiceKey": service_key, "pageNo": 1, "numOfRows": 100}))
    return items   # 필드명은 실제 응답 확인 후 매핑


def normalize(it):
    """API 원시 필드 → 내부 스키마 + 원문 추출."""
    noncontent = it.get("noncontent", "") or ""
    ext = extract_indicators(noncontent, it.get("title", ""))
    return {
        "case_no": it.get("accnum", ""),
        "court": it.get("courtname", ""),
        "result": map_result(it.get("kinda", "")),   # 판결주문 → 인정/불인정/중립
        "verdict": it.get("kinda", ""),               # 원본 주문(취소/기각 등) 보존
        "case_type": it.get("kindb", ""),
        "kindc": it.get("kindc", ""),                 # 원본(업무상질병 등)
        "disease_group": ext["disease_group"],        # 추출한 세부질병(폐질환/난청/근골격계…)
        "title": it.get("title", ""),
        "exposure_factor": ext["exposure_factor"],
        "exposure_years": ext["exposure_years"],
        "decisive_docs": ext["decisive_docs"],
        "raw_len": len(noncontent),
        "excerpt": noncontent[:170],
        "source_url": "https://www.data.go.kr/data/15041878/openapi.do",
    }


# 판결주문 → 산재소송 맥락 승소/패소 번역
RESULT_MAP = {"취소": "인정", "일부취소": "인정",
              "기각": "불인정", "일부기각": "불인정", "각하": "불인정",
              "취하": "중립", "파기환송": "중립"}


def map_result(kinda):
    return RESULT_MAP.get((kinda or "").strip(), "중립")


# 세부 질병 분류: noncontent 키워드 기반(운영에선 LLM 권장)
DISEASE_PATTERNS = {
    "직업성폐질환": ["폐", "진폐", "COPD", "천식", "폐암", "용접흄", "분진"],
    "소음성난청": ["난청", "청력", "소음", "데시벨", "dB"],
    "근골격계질환": ["추간판", "디스크", "근골격", "회전근개", "수근관", "요추", "경추"],
    "뇌심혈관질환": ["뇌출혈", "뇌경색", "심근경색", "뇌졸중", "심혈관"],
    "직업성암": ["백혈병", "암", "악성", "벤젠", "석면"],
}


def classify_disease(text):
    best, score = None, 0
    for dz, kws in DISEASE_PATTERNS.items():
        s = sum(1 for k in kws if k in text)
        if s > score:
            best, score = dz, s
    return best


# ── 추출(데이터 코딩) ───────────────────────────────────────
# 운영에선 LLM(build_extract_prompt) 사용 권장. 아래는 무료/오프라인 휴리스틱.
HAZARDS = ["용접흄", "용접", "망간", "소음", "분진", "석면", "벤젠", "포름알데히드",
           "중량물", "반복동작", "진동", "야간근로", "교대근무", "유기용제", "크롬", "니켈"]
DOC_HINTS = ["작업환경측정", "특수건강진단", "역학조사", "현장조사", "동료", "진술",
             "청력검사", "MRI", "CT", "흉부", "보호구", "고용보험", "작업공정"]


def extract_indicators(noncontent, title=""):
    text = f"{title}\n{noncontent}"
    factors = sorted({h for h in HAZARDS if h in text})
    docs = sorted({d for d in DOC_HINTS if d in text})
    # 노출기간: 전문에는 날짜/금액 등 무관한 숫자가 많으므로
    # '노출/근무/종사/재직' 맥락 ±20자 안의 'N년'만 후보로 본다.
    yrs = []
    for m in re.finditer(r"(노출|근무|종사|재직|작업)[^.]{0,20}?(\d{1,2})\s*년", text):
        yrs.append(int(m.group(2)))
    exposure_years = max(yrs) if yrs else None
    return {"exposure_factor": "/".join(factors) or None,
            "exposure_years": exposure_years,
            "decisive_docs": docs,
            "disease_group": classify_disease(text)}


def build_extract_prompt(noncontent):
    """LLM 추출용 프롬프트. JSON만 출력하도록 강제."""
    return f"""다음 산재 판결문에서 아래 항목을 추출해 JSON으로만 출력하라.
설명·코드펜스 금지. 값이 불명확하면 null.
{{"exposure_factor": "핵심 유해인자(쉼표구분)",
 "exposure_years": 정수 또는 null,
 "decisive_docs": ["판단에 결정적이었던 증거/서류", ...],
 "reasoning_brief": "한 문장 근거(인용 아님, 요약)"}}

[판결문]
{noncontent[:4000]}
"""


# ── 데모용 샘플(실제 API 스키마와 동일: noncontent 원문 포함) ──
#    라이브 키가 없을 때 이 원문에 추출이 동일하게 적용된다.
MOCK_RECORDS = [
    {"accnum": "2019구단12345", "courtname": "서울행정법원", "kinda": "인정",
     "kindb": "직업성 질병", "kindc": "직업성폐질환", "title": "요양불승인처분취소",
     "noncontent": "원고는 약 28년간 조선소 등에서 용접 업무에 종사하면서 용접흄과 망간에 "
        "지속적으로 노출되었다. 작업환경측정 결과 용접흄 농도가 노출기준을 상회하였고, "
        "특수건강진단 및 동료 진술에 비추어 업무와 상병 사이의 상당인과관계가 인정된다."},
    {"accnum": "2020구단67890", "courtname": "부산지방법원", "kinda": "불인정",
     "kindb": "직업성 질병", "kindc": "직업성폐질환", "title": "요양불승인처분취소",
     "noncontent": "원고의 용접 종사기간은 합계 약 6년에 불과하고 판매업 등 비노출 직무와 "
        "반복적으로 교대되었다. 작업환경측정 자료가 없고 흡연력이 확인되어 누적 노출량이 "
        "부족하므로 용접흄으로 인한 업무관련성을 인정하기 어렵다."},
    {"accnum": "2021구단11223", "courtname": "대구지방법원", "kinda": "인정",
     "kindb": "직업성 질병", "kindc": "직업성폐질환", "title": "요양불승인처분취소",
     "noncontent": "원고는 약 19년간 철구조물 제작 현장에서 용접 및 분진 작업을 수행하였다. "
        "영세사업장으로 작업환경측정은 없었으나 현장조사와 작업공정 확인서, 보호구 미지급 "
        "정황으로 노출이 보강되어 업무상 질병으로 인정한다."},
    {"accnum": "2018구단33445", "courtname": "서울행정법원", "kinda": "인정",
     "kindb": "직업성 질병", "kindc": "소음성난청", "title": "장해급여부지급처분취소",
     "noncontent": "원고는 약 12년간 85데시벨 이상의 소음 사업장에서 근무하였고, 순음청력검사 "
        "및 작업환경측정(소음) 결과가 일관되어 소음성 난청의 업무관련성이 인정된다."},
    {"accnum": "2022구단55667", "courtname": "수원지방법원", "kinda": "인정",
     "kindb": "직업성 질병", "kindc": "근골격계질환", "title": "요양불승인처분취소",
     "noncontent": "원고는 약 9년간 중량물 취급 및 반복동작 작업을 수행하였고, MRI 소견과 "
        "작업동작 분석, 업무량 기록상 신체부담업무와 상병 부위가 일치하여 인정한다."},
]


def load_extracted_db(path="precedents_db.json"):
    """LLM 추출 결과 DB 로드(있으면 우선 사용). 내부 표준 스키마로 정규화."""
    import os
    if not os.path.exists(path):
        return []
    rows = json.load(open(path, encoding="utf-8"))
    out = []
    for r in rows:
        bp = (r.get("body_part") or "").split(",")[0].strip()
        out.append({
            "case_no": r.get("case_no", ""), "court": r.get("court", ""),
            "result": r.get("result", "중립"), "verdict": r.get("verdict", ""),
            "disease_group": "근골격계질환",          # 이 DB는 근골격계 추출분
            "disease_detail": r.get("disease_detail", ""),
            "body_part": bp,
            "exposure_factor": r.get("exposure_factor", ""),
            "exposure_years": r.get("exposure_years"),
            "decisive_docs": r.get("decisive_docs", []),
            "key_reason": r.get("key_reason", ""),
            "degenerative_issue": r.get("degenerative_issue", False),
            "excerpt": r.get("excerpt", ""),
            "source_url": r.get("source_url", ""),
        })
    return out


def get_records(service_key=None, kindC=None, rows=10, disease_group=None):
    """진입점. 우선순위: ①LLM추출 DB(해당 질병) → ②라이브 API → ③데모 샘플."""
    # ① LLM 추출 DB가 있고, 요청 질병이 그 DB 범위면 우선 사용
    ext = load_extracted_db()
    if ext and (disease_group is None or disease_group == "근골격계질환"):
        pool = [r for r in ext if (not disease_group or r["disease_group"] == disease_group)]
        if pool:
            return pool, len(pool), "extracted"
    # ② 라이브
    if service_key:
        recs, total = fetch_judgments(service_key, rows=rows, kindC=kindC)
        return recs, total, "live"
    # ③ 데모
    pool = [r for r in MOCK_RECORDS if (not kindC or r["kindc"] == kindC)]
    return [normalize(r) for r in pool], len(pool), "demo"


# ── 오프라인 검증 ───────────────────────────────────────────
_MOCK = json.dumps({"response": {
    "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
    "body": {"numOfRows": "1", "pageNo": "1", "totalCount": "1", "items": {"item": {
        "accnum": "2019구단12345", "courtname": "서울행정법원",
        "kinda": "인정", "kindb": "직업성 질병", "kindc": "직업성폐질환",
        "title": "요양불승인처분취소",
        "noncontent": ("원고는 약 28년간 조선소 등에서 용접 업무에 종사하면서 용접흄과 "
                       "망간에 지속적으로 노출되었고, 작업환경측정 결과 및 특수건강진단, "
                       "동료 진술에 비추어 업무관련성이 인정된다.")
    }}}}}, ensure_ascii=False)


if __name__ == "__main__":
    code, msg, total, items = _parse(_MOCK)
    print(f"[파싱] resultCode={code} total={total} items={len(items)}")
    rec = normalize(items[0])
    print("[정규화 + 추출 결과]")
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    print("\n→ 결과(인정)는 kinda 구조화값, 노출요인/기간/서류는 noncontent에서 추출됨.")
    print("→ 운영 전환: extract_indicators 대신 build_extract_prompt로 LLM 추출 권장.")
