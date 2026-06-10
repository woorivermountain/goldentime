# -*- coding: utf-8 -*-
"""
질병판정서 데이터 소스 + 분류값 + 본문 추출
==================================================
근로복지공단 질병판정서 조회 서비스(jilbyeongPstateInfoService).
판정위가 실제 내린 결정문. kinda(심의결과)/kindb(직종)/kindc(질병구분)이 구조화됨.
본문(noncontent)은 [주문/청구취지/신청내용] 정형 양식 → 휴리스틱 추출 용이.
"""
import re, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

SVC = "https://apis.data.go.kr/B490001/jilbyeongPstateInfoService"
OP_BODY = "getJilbyeongResultNaeyongPstate"

# ── 확정된 분류값 (API 조회로 확인됨) ──
KINDC_LIST = [
    "근골격계질환(척추질환 제외)", "근골격계질환 (척추질환)", "호흡기질환(천식 포함)",
    "난청", "뇌혈관질환", "심장질환", "정신질환", "악성신생물(직업성 암 포함)",
    "진폐", "석면폐증", "피부질환", "안질환", "진동으로 인한 증상",
    "독성감염", "기타 간질환", "안면신경 마비", "자해행위(자살 포함)",
    "이상기압으로 인한 질병(압착증, 감압병 등)", "일사병,열사병,화상,동상", "사인미상",
]
KINDA_LIST = ["인정", "일부인정", "변경인정", "불인정", "보류", "판정위이송"]
KINDB_TOP = [
    "건설 및 광업 단순 종사원", "제조관련 단순 종사원", "주방장 및 조리사",
    "청소원 및 환경 미화원", "건설관련 기능 종사자", "음식관련 단순 종사원",
    "하역 및 적재 단순 종사원", "음식서비스 종사자", "운송 서비스 종사자",
    "자동차 운전원", "자동차 정비원", "매장 판매 종사자", "용접원", "배달원", "경비원 및 검표원",
]

# ── 질병구분별 필터 스키마 ──
# axis1 = 1차 분류축(트리 가지), axis2 = 부담/유해요인. 질병 성격에 맞게 다르게 제공.
DISEASE_FILTERS = {
    "근골격계질환(척추질환 제외)": {
        "axis1_label": "신체부위", "axis1": ["허리", "목", "어깨", "무릎", "손/손목", "팔꿈치"],
        "axis2_label": "부담요인", "axis2": ["중량물", "반복동작", "진동", "부적절자세", "장시간"],
    },
    "근골격계질환 (척추질환)": {
        "axis1_label": "부위", "axis1": ["요추", "경추", "흉추"],
        "axis2_label": "부담요인", "axis2": ["중량물", "반복동작", "부적절자세", "장시간운전"],
    },
    "호흡기질환(천식 포함)": {
        "axis1_label": "유해인자", "axis1": ["용접흄", "분진", "유기용제", "석면", "결정형유리규산"],
        "axis2_label": "노출형태", "axis2": ["고농도노출", "장기노출", "밀폐공간"],
    },
    "진폐": {
        "axis1_label": "유해인자", "axis1": ["석탄분진", "광물분진", "결정형유리규산"],
        "axis2_label": "노출형태", "axis2": ["장기노출", "고농도노출"],
    },
    "석면폐증": {
        "axis1_label": "유해인자", "axis1": ["석면"],
        "axis2_label": "노출형태", "axis2": ["직접취급", "간접노출", "장기노출"],
    },
    "난청": {
        "axis1_label": "소음수준", "axis1": ["85dB이상", "90dB이상", "충격소음"],
        "axis2_label": "노출조건", "axis2": ["연속노출", "장기노출", "보호구미착용"],
    },
    "뇌혈관질환": {
        "axis1_label": "위험요인", "axis1": ["과로", "야간근무", "교대근무", "스트레스"],
        "axis2_label": "발병형태", "axis2": ["급성발병", "기왕증동반"],
    },
    "심장질환": {
        "axis1_label": "위험요인", "axis1": ["과로", "야간근무", "교대근무", "스트레스"],
        "axis2_label": "발병형태", "axis2": ["급성발병", "기왕증동반"],
    },
    "악성신생물(직업성 암 포함)": {
        "axis1_label": "유해인자", "axis1": ["석면", "벤젠", "결정형유리규산", "용접흄", "방사선"],
        "axis2_label": "노출형태", "axis2": ["장기노출", "고농도노출"],
    },
    "정신질환": {
        "axis1_label": "유발요인", "axis1": ["직장내괴롭힘", "과로", "사고목격", "폭언폭행"],
        "axis2_label": "양상", "axis2": ["급성", "지속성"],
    },
    "피부질환": {
        "axis1_label": "유해인자", "axis1": ["화학물질", "유기용제", "금속", "습윤작업"],
        "axis2_label": "노출형태", "axis2": ["접촉", "장기노출"],
    },
}
# 정의 안 된 질병은 직종 축 + 일반 부담요인 사용
DEFAULT_FILTER = {
    "axis1_label": "직종", "axis1": [], "axis2_label": "부담요인",
    "axis2": ["중량물", "반복동작", "장시간", "유해물질노출"],
}

def filters_for(kindc):
    return DISEASE_FILTERS.get(kindc, DEFAULT_FILTER)

# 심의결과 → 트리 색상용 정규화(인정계열/불인정/보류계열)
def norm_result(kinda):
    k = (kinda or "").strip()
    if k in ("인정", "일부인정", "변경인정"):
        return "인정"
    if k == "불인정":
        return "불인정"
    return "보류"   # 보류/판정위이송 = 재해조사 단계

# 신체부위·부담요인 추출 키워드
BODY = ["허리","요추","목","경추","어깨","견관절","무릎","슬관절","손목","수근관","수부",
        "팔꿈치","주관절","척추","골반","고관절","발목"]
BURDEN = ["중량물","반복","진동","부적절","쪼그","구부","비틀","들어올","밀기","당기기","장시간"]
DOCS = ["MRI","CT","X-ray","엑스레이","근전도","초음파","작업환경","특수건강진단",
        "사실관계","현장조사","동료","진술","의학적 소견","감정"]

def fetch_body(service_key, kindc=None, kinda=None, page=1, rows=50, cq=None):
    params = {"serviceKey": service_key, "pageNo": page, "numOfRows": rows}
    if kindc: params["kindc"] = kindc
    if kinda: params["kinda"] = kinda
    if cq: params["cq"] = cq
    url = f"{SVC}/{OP_BODY}?" + urllib.parse.urlencode(params)
    txt = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"sanjae/1.0"}), timeout=30).read().decode("utf-8","replace")
    root = ET.fromstring(txt)
    total = root.findtext(".//totalCount")
    result_code = root.findtext(".//resultCode") or ""
    out = [normalize({c.tag:(c.text or "") for c in it}) for it in root.findall(".//item")]
    global LAST_FETCH
    LAST_FETCH = {"result_code": result_code, "total": total, "rows": len(out),
                  "op": OP_BODY, "kindc": kindc or "", "at": _now_str()}
    return out, total


def _fetch_page(service_key, kindc, kinda, page, rows, cq):
    params = {"serviceKey": service_key, "pageNo": page, "numOfRows": rows}
    if kindc: params["kindc"] = kindc
    if kinda: params["kinda"] = kinda
    if cq: params["cq"] = cq
    url = f"{SVC}/{OP_BODY}?" + urllib.parse.urlencode(params)
    txt = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"sanjae/1.0"}), timeout=30).read().decode("utf-8","replace")
    root = ET.fromstring(txt)
    total = root.findtext(".//totalCount")
    rc = root.findtext(".//resultCode") or ""
    items = [normalize({c.tag:(c.text or "") for c in it}) for it in root.findall(".//item")]
    return items, int(total or 0), rc


def fetch_body_recent(service_key, kindc=None, kinda=None, want=60, cq=None):
    """최근 판정서 우선 수집.
    API에 정렬 옵션이 없어 앞 페이지엔 과거 접수분이 몰리므로,
    마지막 페이지부터 역순으로 모은 뒤 접수번호(연도) 기준 내림차순 정렬한다.
    """
    import re as _re
    rows = 100
    # 1) 첫 호출로 전체 건수 파악
    first, total, rc = _fetch_page(service_key, kindc, kinda, 1, rows, cq)
    global LAST_FETCH
    LAST_FETCH = {"result_code": rc, "total": str(total), "rows": 0,
                  "op": OP_BODY, "kindc": kindc or "", "at": _now_str()}
    if total <= 0:
        return [], 0
    last_page = (total + rows - 1) // rows
    collected = []
    seen = set()
    # 2) 마지막 페이지부터 역순으로 want건 채울 때까지
    p = last_page
    pages_tried = 0
    while p >= 1 and len(collected) < want and pages_tried < 8:
        items, _, _ = _fetch_page(service_key, kindc, kinda, p, rows, cq)
        for it in items:
            k = it.get("case_no") or id(it)
            if k in seen:
                continue
            seen.add(k)
            collected.append(it)
        pages_tried += 1
        p -= 1
    # 3) 접수번호의 연도+번호로 내림차순(최근 먼저)
    def _key(it):
        s = str(it.get("case_no") or "")
        m = _re.search(r"(20\d{2}|19\d{2})", s)
        y = int(m.group(1)) if m else 0
        n = _re.findall(r"\d+", s)
        seq = int(n[-1]) if n else 0
        return (y, seq)
    collected.sort(key=_key, reverse=True)
    LAST_FETCH["rows"] = len(collected)
    return collected[:max(want, 0)], total

LAST_FETCH = {}
def _now_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def extract(noncontent):
    t = noncontent or ""
    parts = sorted({b for b in BODY if b in t})
    burden = sorted({b for b in BURDEN if b in t})
    docs = sorted({d for d in DOCS if d in t})
    # 신청내용: 공백/탭/줄바꿈 섞여 있어 유연하게. 여러 표기 시도.
    sintcheong = ""
    for marker in ["신청내용", "신청 내용", "재해경위", "신청 경위", "처분내용"]:
        m = re.search(re.escape(marker) + r"\s*[:：]?\s*(.{20,500})", t, re.S)
        if m:
            sintcheong = re.sub(r"\s+", " ", m.group(1)).strip()[:300]
            break
    # 그래도 비면 본문 중반부(보통 사실관계 서술)를 발췌
    if not sintcheong and len(t) > 200:
        mid = t[len(t)//4: len(t)//4 + 300]
        sintcheong = re.sub(r"\s+", " ", mid).strip()
    yrs = [int(x.group(2)) for x in re.finditer(r"(근무|종사|재직|일하)[^.]{0,15}?(\d{1,2})\s*년", t)]
    return {"body_parts": parts, "burden": burden, "docs": docs,
            "sintcheong": sintcheong, "years": max(yrs) if yrs else None}

def normalize(it):
    nc = it.get("noncontent","") or ""
    ext = extract(nc)
    return {
        "case_no": it.get("accnum",""), "verdict": it.get("kinda",""),
        "result": norm_result(it.get("kinda","")),
        "job_type": it.get("kindb",""), "disease_kindc": it.get("kindc",""),
        "industry": it.get("title",""),
        "body_part": (ext["body_parts"][0] if ext["body_parts"] else ""),
        "body_parts": ext["body_parts"], "burden": ext["burden"],
        "decisive_docs": ext["docs"], "exposure_years": ext["years"],
        "sintcheong": ext["sintcheong"], "excerpt": nc[:220], "noncontent": nc,
        "source": "질병판정서", "source_url": "https://www.data.go.kr/data/15110836/openapi.do",
    }

if __name__ == "__main__":
    import sys
    key = sys.argv[1] if len(sys.argv)>1 else "KEY"
    recs, total = fetch_body(key, kindc="근골격계질환(척추질환 제외)", rows=3)
    print("total:", total)
    for r in recs:
        print("─"*40); print(r["case_no"], r["verdict"],"→",r["result"],"|",r["job_type"])
        print(" 부위:",r["body_parts"],"| 부담:",r["burden"],"| 서류:",r["decisive_docs"])
        print(" 신청내용:",r["sintcheong"][:120])
