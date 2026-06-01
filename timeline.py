# -*- coding: utf-8 -*-
"""
사실관계 타임라인 분석 (2단계)
==================================================
담당자가 모으는 자료를 한 타임라인에 올려 사실관계를 대조한다.
- 진술 직력(신청서 자유서술 기반)  vs  전산 직력(4대보험 경력산정내역)
- 작업내용(부담요인)
- 의료기록(진단·치료 시점)
→ 불일치·공백·쟁점을 자동 추출. (외부 전송 없음, 로컬 계산)

개인정보 제약으로 실제 연동 대신, 입력/업로드된 자료를 그대로 분석한다.
"""
from datetime import date
import re


def _parse_ym(s):
    """'2001-02-03' / '2001-02' / '2001.2' → (year, month) or None"""
    if not s:
        return None
    m = re.search(r"(\d{4})[-.\s]*(\d{1,2})?", str(s))
    if not m:
        return None
    y = int(m.group(1))
    mo = int(m.group(2)) if m.group(2) else 1
    return (y, mo)


def _months(a, b):
    if not a or not b:
        return 0
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


def _fmt_dur(months):
    if months <= 0:
        return "0개월"
    y, mo = months // 12, months % 12
    return (f"{y}년 " if y else "") + (f"{mo}개월" if mo else ("0개월" if not y else "")).strip()


def analyze(payload):
    """
    payload = {
      "birth": "1965-03",  "onset": "2024-06" (발병/진단일),
      "disease": "호흡기질환(천식 포함)",
      "hazard_jobs": ["용접원"],   # 신체부담/유해 직종 키워드
      "stated_years": 26,          # 진술 직력(본인 주장)
      "careers": [ {start,end,site,job,insure,hazard(bool)} ... ],  # 전산 직력
      "medical": [ {date, hospital, dx, note} ... ],                 # 의료기록
    }
    """
    careers = payload.get("careers", [])
    medical = payload.get("medical", [])
    onset = _parse_ym(payload.get("onset"))
    stated = payload.get("stated_years")

    # ── 전산 직력 집계 ──
    total_m = 0
    hazard_m = 0
    spans = []
    for c in careers:
        a, b = _parse_ym(c.get("start")), _parse_ym(c.get("end"))
        m = _months(a, b)
        total_m += m
        if c.get("hazard"):
            hazard_m += m
        spans.append({**c, "_a": a, "_b": b, "_m": m})

    official_years = round(total_m / 12, 1)
    hazard_years = round(hazard_m / 12, 1)

    # ── 쟁점 자동 추출 ──
    issues = []

    # (1) 진술 직력 vs 전산 직력 불일치
    if stated and official_years:
        gap = stated - official_years
        if abs(gap) >= 2:
            issues.append({
                "type": "직력 불일치", "level": "high",
                "text": f"본인 진술 직력 {stated}년 vs 4대보험 확인 직력 {official_years}년 "
                        f"(차이 {round(abs(gap),1)}년). 4대보험 누락 기간일 수 있어 추가 입증 필요.",
                "action": "누락 의심 기간의 근로계약서·급여명세·동료 진술·국민연금 이력 등으로 보완"
            })

    # (2) 유해업무 노출기간 (인정 기준 충족 여부 단서)
    if hazard_years:
        issues.append({
            "type": "유해업무 노출기간", "level": "info",
            "text": f"신체부담·유해 직종 합산 {hazard_years}년 (전체 {official_years}년 중). "
                    f"유해인자 노출기간은 업무관련성 판단의 핵심 정량지표.",
            "action": "노출기간이 인정기준(질병별)에 근접/충족하는지 확인. 미달 시 누락기간 보완 입증"
        })

    # (3) 발병시점에 어느 사업장 재직 중이었나
    if onset:
        at_onset = [s for s in spans if s["_a"] and s["_b"] and
                    (s["_a"][0]*12+s["_a"][1]) <= (onset[0]*12+onset[1]) <= (s["_b"][0]*12+s["_b"][1])]
        if at_onset:
            sites = ", ".join(s.get("site", "?") for s in at_onset)
            issues.append({
                "type": "발병시점 재직지", "level": "mid",
                "text": f"발병(진단) 시점({payload.get('onset')})에 재직 중이던 사업장: {sites}. "
                        f"해당 사업장의 작업환경·유해인자 우선 조사 대상.",
                "action": f"{sites}의 작업환경측정·공정자료를 우선 확보"
            })
        else:
            issues.append({
                "type": "발병시점 재직지", "level": "high",
                "text": f"발병 시점({payload.get('onset')})에 전산상 재직 사업장이 확인되지 않음. "
                        f"퇴직 후 발병이거나 직력 공백 구간일 수 있음.",
                "action": "발병 직전 사업장과 발병 사이의 공백·잠복기 검토 (특히 진폐·직업성암은 잠복기 고려)"
            })

    # (4) 의료기록 공백 / 첫 진료시점
    if medical:
        dates = sorted([_parse_ym(m.get("date")) for m in medical if _parse_ym(m.get("date"))])
        if dates:
            first = dates[0]
            # 첫 진료가 어느 재직기간에 속하는지
            during = [s for s in spans if s["_a"] and s["_b"] and
                      (s["_a"][0]*12+s["_a"][1]) <= (first[0]*12+first[1]) <= (s["_b"][0]*12+s["_b"][1])]
            if during:
                issues.append({
                    "type": "최초 진료시점", "level": "info",
                    "text": f"최초 관련 진료({first[0]}.{first[1]:02d})가 {during[0].get('site')} 재직 중 발생. "
                            f"재직 중 증상 발현은 업무관련성에 유리한 정황.",
                    "action": "해당 시점 의무기록·산업의학적 소견으로 발병시기 입증"
                })

    # ── 타임라인 이벤트 (그리기용) ──
    events = []
    for s in spans:
        if s["_a"]:
            events.append({"kind": "career", "start": s.get("start"), "end": s.get("end"),
                           "label": f'{s.get("site","")} · {s.get("job","")}',
                           "insure": s.get("insure", ""), "hazard": bool(s.get("hazard")),
                           "dur": _fmt_dur(s["_m"])})
    for m in medical:
        events.append({"kind": "medical", "date": m.get("date"),
                       "label": f'{m.get("hospital","")} · {m.get("dx","")}', "note": m.get("note", "")})
    if onset:
        events.append({"kind": "onset", "date": payload.get("onset"), "label": "발병(진단) 추정"})

    return {
        "summary": {
            "official_years": official_years, "hazard_years": hazard_years,
            "stated_years": stated,
            "career_count": len(careers), "medical_count": len(medical),
        },
        "issues": issues, "events": events,
    }


# ── 26년 용접공(울산) 폐질환 시나리오 예시 ──
def demo_payload():
    return {
        "birth": "1962-05", "onset": "2024-08",
        "disease": "호흡기질환(천식 포함)",
        "hazard_jobs": ["용접원"],
        "stated_years": 26,
        "careers": [
            {"start": "2019-06", "end": "2025-02", "site": "OO조선", "job": "용접원", "insure": "고용보험", "hazard": True},
            {"start": "2018-01", "end": "2018-02", "site": "A중공업", "job": "용접원", "insure": "고용보험", "hazard": True},
            {"start": "2017-06", "end": "2017-12", "site": "A중공업", "job": "용접원", "insure": "고용보험", "hazard": True},
            {"start": "2001-02", "end": "2013-10", "site": "현대중공업", "job": "용접원", "insure": "산재보험", "hazard": True},
            {"start": "1998-04", "end": "2001-07", "site": "B기업", "job": "용접보조", "insure": "고용보험", "hazard": True},
            {"start": "1996-12", "end": "1998-04", "site": "C조선", "job": "용접원", "insure": "고용보험", "hazard": True},
        ],
        "medical": [
            {"date": "2013-05", "hospital": "울산OO병원", "dx": "기침·호흡곤란 호소", "note": "현대중공업 재직 중 최초 호흡기 증상"},
            {"date": "2020-11", "hospital": "OO대학병원", "dx": "만성폐쇄성폐질환 의증", "note": "흉부CT 시행"},
            {"date": "2024-08", "hospital": "OO대학병원", "dx": "직업성 폐질환 진단", "note": "폐기능검사·작업력 확인"},
        ],
    }


if __name__ == "__main__":
    import json
    r = analyze(demo_payload())
    print(json.dumps(r["summary"], ensure_ascii=False))
    print("\n[쟁점]")
    for i in r["issues"]:
        print(f"  [{i['level']}] {i['type']}: {i['text']}")
        print(f"       ▶ {i['action']}")
