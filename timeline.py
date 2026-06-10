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


def from_verdict(rec):
    """판정서 1건(noncontent 포함)에서 타임라인 케이스를 추출.
    본문 서술에서 사업장·근무연수·직종·발병/진단 정보를 파싱한다.
    개인정보(4대보험 원본)는 없으므로 본문에 서술된 범위만 복원한다."""
    t = (rec.get("noncontent") or rec.get("excerpt") or "")
    t = re.sub(r"\s+", " ", t)
    job = rec.get("job_type") or ""
    kindc = rec.get("disease") or rec.get("kindc") or ""

    # 1) 사업장 + 근무기간(연-연 범위)
    careers = []
    for m in re.finditer(r"([가-힣A-Za-z0-9()㈜·]{2,18}?)\s*(?:에서|에|,)?\s*(\d{4})\s*[.\-년]\s*(?:\d{1,2}\s*[.\-월]?\s*)?(?:부터|~|-|―)\s*(\d{4})", t):
        site = m.group(1).strip(" ,·")[-14:]
        a, b = m.group(2), m.group(3)
        if 1960 <= int(a) <= 2026 and 1960 <= int(b) <= 2026 and int(b) >= int(a):
            careers.append({"start": a, "end": b, "site": site or "사업장", "job": job, "insure": "", "hazard": True})

    # 2) 발병/진단 시점
    onset = None
    mo = re.search(r"(\d{4})\s*년?\s*(\d{1,2})?\s*월?[^.]{0,15}?(?:진단|발병|확진|판정)", t)
    if mo:
        onset = mo.group(1) + ("-" + mo.group(2).zfill(2) if mo.group(2) else "")

    # 3) 진술 총 연수
    stated = rec.get("exposure_years")
    ym = re.search(r"(?:총|약)?\s*(\d{1,2})\s*년\s*(?:간|동안|근무|종사|재직)", t)
    if ym and not stated:
        stated = int(ym.group(1))

    # 4) 의료기록(병원명+연도)
    medical = []
    for m in re.finditer(r"(\d{4})\s*[.\-년]\s*(\d{1,2})?\s*[.\-월]?[^.]{0,18}?([가-힣]{2,10}(?:병원|의원|대학병원|의료원))", t):
        medical.append({"date": m.group(1) + ("-" + (m.group(2) or "1").zfill(2)),
                        "hospital": m.group(3), "dx": "", "note": "본문 기재"})

    # 추출 실패 필드는 '미상'으로(빈칸/오추출보다 정직)
    BAD_SITE = ("근무", "종사", "재직", "하였", "당시", "기간", "이후", "에서", "부터")
    for cr in careers:
        st = cr.get("site") or ""
        if (not st) or any(b in st for b in BAD_SITE) or len(st) < 2:
            cr["site"] = "미상"
        if not cr.get("job"):
            cr["job"] = job or "미상"
        if not cr.get("insure"):
            cr["insure"] = "미상"
    for md in medical:
        if not md.get("hospital"):
            md["hospital"] = "미상"
        if not md.get("dx"):
            md["dx"] = "미상"

    return {
        "source_case": rec.get("case_no") or rec.get("accnum") or "",
        "source_result": rec.get("verdict") or rec.get("result") or "",
        "disease": kindc, "hazard_jobs": [job] if job else [],
        "stated_years": stated, "onset": onset,
        "careers": careers, "medical": medical[:5],
        "raw_excerpt": t[:200],
    }


def cases_from_records(records, limit=8):
    """여러 판정서에서 '사업장별 직력(careers)이 실제로 추출된' 케이스만 반환.
    careers가 있어야 타임라인을 그릴 수 있으므로 그것을 필수 조건으로 한다."""
    out = []
    for r in records:
        c = from_verdict(r)
        if not c["careers"]:
            continue  # 사업장별 기간이 없으면 타임라인을 못 그리므로 제외
        # 진술 연수가 없으면 careers 합산으로 채움
        if not c.get("stated_years"):
            tot = 0
            for cr in c["careers"]:
                a, b = _parse_ym(cr.get("start")), _parse_ym(cr.get("end"))
                tot += _months(a, b)
            if tot > 0:
                c["stated_years"] = round(tot / 12, 1)
        # 발병시점이 없으면 마지막 직력 종료 또는 마지막 의료기록으로 추정
        if not c.get("onset"):
            if c.get("medical"):
                c["onset"] = c["medical"][-1].get("date")
            elif c["careers"]:
                c["onset"] = c["careers"][0].get("end")
        out.append(c)
        if len(out) >= limit:
            break
    return out


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


# ── 질병별 대표 시나리오 (실제 추출이 안 될 때 보완용 예시) ──
SAMPLE_CASES = {
    "근골격계질환(척추질환 제외)": {
        "label": "근골격계 · 조선소 배관공 어깨",
        "disease": "근골격계질환(척추질환 제외)", "hazard_jobs": ["배관공"], "stated_years": 22, "onset": "2023-09",
        "careers": [
            {"start": "2002-03", "end": "2013-12", "site": "현대중공업", "job": "배관공", "insure": "산재보험", "hazard": True},
            {"start": "2014-01", "end": "2024-02", "site": "OO조선", "job": "배관공", "insure": "고용보험", "hazard": True},
        ],
        "medical": [
            {"date": "2021-04", "hospital": "울산정형외과", "dx": "회전근개 파열 의증", "note": "어깨 통증 최초 진료"},
            {"date": "2023-09", "hospital": "OO대학병원", "dx": "회전근개 완전파열", "note": "MRI·근전도 시행"},
        ],
    },
    "근골격계질환 (척추질환)": {
        "label": "근골격계(척추) · 중량물 운반 요추",
        "disease": "근골격계질환 (척추질환)", "hazard_jobs": ["하역종사원"], "stated_years": 18, "onset": "2022-06",
        "careers": [
            {"start": "2006-05", "end": "2024-01", "site": "OO물류", "job": "하역종사원", "insure": "고용보험", "hazard": True},
        ],
        "medical": [
            {"date": "2019-02", "hospital": "OO병원", "dx": "요통", "note": "허리 통증 보존치료"},
            {"date": "2022-06", "hospital": "OO대학병원", "dx": "추간판탈출증(L4-5)", "note": "MRI 확인"},
        ],
    },
    "난청": {
        "label": "난청 · 소음 작업장 30년",
        "disease": "난청", "hazard_jobs": ["성형공"], "stated_years": 30, "onset": "2024-03",
        "careers": [
            {"start": "1994-04", "end": "2024-03", "site": "OO금속", "job": "성형공(프레스)", "insure": "산재보험", "hazard": True},
        ],
        "medical": [
            {"date": "2024-03", "hospital": "OO이비인후과", "dx": "소음성 난청", "note": "순음청력검사 시행"},
        ],
    },
    "심장질환": {
        "label": "심장 · 교대근무 과로",
        "disease": "심장질환", "hazard_jobs": ["생산직"], "stated_years": 15, "onset": "2024-01",
        "careers": [
            {"start": "2009-08", "end": "2024-01", "site": "OO제조", "job": "생산직(주야 2교대)", "insure": "고용보험", "hazard": True},
        ],
        "medical": [
            {"date": "2024-01", "hospital": "OO대학병원 응급실", "dx": "급성심근경색", "note": "야간근무 중 발병"},
        ],
    },
    "뇌혈관질환": {
        "label": "뇌혈관 · 장시간 근로 후 발병",
        "disease": "뇌혈관질환", "hazard_jobs": ["운전원"], "stated_years": 12, "onset": "2023-11",
        "careers": [
            {"start": "2012-02", "end": "2023-11", "site": "OO운수", "job": "버스운전원", "insure": "고용보험", "hazard": True},
        ],
        "medical": [
            {"date": "2023-11", "hospital": "OO대학병원", "dx": "뇌출혈", "note": "연속 근무 후 발병"},
        ],
    },
    "정신질환": {
        "label": "정신 · 직장 내 괴롭힘",
        "disease": "정신질환", "hazard_jobs": ["사무직"], "stated_years": 8, "onset": "2023-05",
        "careers": [
            {"start": "2016-03", "end": "2023-08", "site": "OO기업", "job": "사무직", "insure": "고용보험", "hazard": False},
        ],
        "medical": [
            {"date": "2022-10", "hospital": "OO정신건강의학과", "dx": "적응장애", "note": "직장 스트레스 호소"},
            {"date": "2023-05", "hospital": "OO정신건강의학과", "dx": "우울증", "note": "증상 악화"},
        ],
    },
}


def sample_cases(diseases=None):
    """질병별 대표 시나리오를 분석 결과와 함께 반환."""
    keys = diseases or list(SAMPLE_CASES.keys())
    out = []
    for k in keys:
        c = SAMPLE_CASES.get(k)
        if not c:
            continue
        payload = {k2: v for k2, v in c.items() if k2 != "label"}
        out.append({
            "label": c["label"], "disease": k, "kind": "sample",
            "payload": payload, "analysis": analyze(payload),
        })
    return out


if __name__ == "__main__":
    import json
    r = analyze(demo_payload())
    print(json.dumps(r["summary"], ensure_ascii=False))
    print("\n[쟁점]")
    for i in r["issues"]:
        print(f"  [{i['level']}] {i['type']}: {i['text']}")
        print(f"       ▶ {i['action']}")
