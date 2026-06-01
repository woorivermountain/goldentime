# -*- coding: utf-8 -*-
"""
산재 재조사 가이드라인 — 백엔드 서버 (stdlib만, Flask/pip 불필요)
==================================================================
역할: 대시보드(브라우저)는 data.go.kr를 직접 못 부르므로(CORS·키 보안),
      이 서버가 키를 들고 공공데이터 API를 호출 → 추출 → 가이드라인을 만들어 돌려준다.

실행:
  # 데모 모드 (키 없이 바로 시연)
  python3 app.py
  → 브라우저에서 http://localhost:8000 접속

  # 라이브 모드 (실제 공공데이터)
  SERVICE_KEY="발급키" LAW_OC="이메일ID" python3 app.py     (mac/linux)
  set SERVICE_KEY=발급키 && python3 app.py                  (windows)
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import comwel_fetch
import law_mapper
import jbpjeong_source as jbp
import timeline

SERVICE_KEY = os.environ.get("SERVICE_KEY")    # 있으면 라이브
LAW_OC = os.environ.get("LAW_OC")
HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD = os.path.join(HERE, "dashboard.html")

# 이 API의 kindC(사고/질병 구분)에서 우리 프로젝트 대상은 '업무상질병' 하나.
# (나머지: 교통사고/보험료/체당금 등은 질병 산재가 아니므로 제외)
DISEASE_KINDC = "업무상질병"

# kinda는 '인정/불인정'이 아니라 행정소송 판결 주문이다. 산재소송 맥락으로 번역:
#   취소/일부취소 = 공단 불승인처분 취소 = 근로자 승소(인정 방향)
#   기각/일부기각/각하 = 처분 유지 = 패소(불인정 방향)
#   취하/파기환송 = 중립
RESULT_MAP = {
    "취소": "인정", "일부취소": "인정",
    "기각": "불인정", "일부기각": "불인정", "각하": "불인정",
    "취하": "중립", "파기환송": "중립",
}


def map_result(kinda):
    return RESULT_MAP.get((kinda or "").strip(), "중립")


LAWS = [
    {"law": "산업재해보상보험법", "art": "제37조(업무상의 재해의 인정 기준)",
     "point": "업무수행성·업무기인성(상당인과관계)이 인정되어야 업무상 질병으로 인정.",
     "url": "https://www.law.go.kr/법령/산업재해보상보험법/제37조"},
    {"law": "산업재해보상보험법 시행령", "art": "별표3(업무상 질병 구체적 인정 기준)",
     "point": "유해인자별 노출 요건·기간·작업환경 기준을 구체적으로 열거.",
     "url": "https://www.law.go.kr/법령/산업재해보상보험법시행령"},
    {"law": "산업재해보상보험법", "art": "제38조(업무상질병판정위원회)",
     "point": "인정 여부는 판정위 심의로 결정 — 자료 충분성이 심의 속도를 좌우.",
     "url": "https://www.law.go.kr/법령/산업재해보상보험법/제38조"},
]


# ── 분석 로직 ────────────────────────────────────────────────
def months(s, e):
    a, b = map(int, s.split("-")); c, d = map(int, e.split("-"))
    return (c - a) * 12 + (d - b)


def compute_metrics(work_history):
    tot = sum(months(w["start"], w["end"]) for w in work_history)
    exp = sum(months(w["start"], w["end"]) for w in work_history if w.get("exposed"))
    eseg = sum(1 for w in work_history if w.get("exposed"))
    cont = round(exp / tot, 2) if tot else 0
    return {"tot": tot, "exp": exp, "cont": cont, "seg": len(work_history), "eseg": eseg,
            "nonExp": round((1 - cont) * 100, 1),
            "totStr": f"{tot // 12}년 {tot % 12}개월", "expStr": f"{exp // 12}년 {exp % 12}개월"}


def toks(s):
    return set(re.findall(r"[가-힣A-Za-z0-9]+", str(s)))


def rank(case, recs, m):
    cy = m["exp"] / 12
    ct = toks(case["sangbyeong"] + case["job"] + case["exposure_factor"])
    out = []
    for p in recs:
        st = 0.5 if p["disease_group"] == case["disease_group"] else 0
        if toks(case["exposure_factor"]) & toks(p.get("exposure_factor") or ""):
            st += 0.3
        pt = toks((p.get("summary") or p.get("title") or "") + " " + (p.get("exposure_factor") or ""))
        uni = ct | pt
        j = len(ct & pt) / len(uni) if uni else 0
        py = p.get("exposure_years")
        out.append({**p, "sim": round(st + 0.2 * j, 3),
                    "delta": round((py - cy), 1) if py is not None else None})
    return sorted(out, key=lambda x: x["sim"], reverse=True)


def build_guideline(case, ranked, m):
    acc = [p for p in ranked if p["result"] == "인정"]
    rej = [p for p in ranked if p["result"] == "불인정"]
    freq = {}
    for p in acc:
        for d in (p.get("decisive_docs") or []):
            freq[d] = freq.get(d, 0) + 1
    key_docs = sorted(freq, key=freq.get, reverse=True)

    is_msk = case.get("disease_group") == "근골격계질환"
    cp = []
    if is_msk:
        # 근골격계 핵심: 퇴행성 vs 업무성 감별이 결과를 가른다
        deg = [p for p in ranked if p.get("degenerative_issue")]
        deg_rej = [p for p in deg if p["result"] == "불인정"]
        if deg_rej:
            cp.append(f"퇴행성 변화 쟁점 판례 {len(deg)}건 중 {len(deg_rej)}건 불인정 — "
                      f"기왕증·연령퇴행과 업무 기여를 감별할 의학적 소견(영상검사) 확보 필요")
        cp.append("발병 전 동일부위 치료력·기왕증 여부 확인 (불인정 사유 1순위)")
        cp.append("작업의 신체부담 정도를 작업동작 분석·업무량 기록으로 구체적 입증")
        cp.append("재해 직후 촬영 영상(MRI/CT)과 상병 부위 일치 여부 — 인정 판례의 공통 근거")
    else:
        if m["cont"] < 0.7:
            cp.append(f"노출 연속성 지표 {m['cont']} (비노출 직무 비중 {m['nonExp']}%) — "
                      f"노출 직무 구간별 작업내용을 분리 입증 필요")
        accyrs = [p["exposure_years"] for p in acc if p.get("exposure_years") is not None]
        if accyrs and m["exp"] / 12 < min(accyrs):
            cp.append(f"본 케이스 노출기간이 유사 인정 판례 최소치({min(accyrs)}년)보다 짧음 — "
                      f"누락 직력(타 기관 기록 대조)·노출량 보강 검토")
        cp.append("작업환경측정자료 부재 시 → 현장조사·동료진술로 노출 보강 검토")

    # 인정/기각 판례의 실제 핵심근거(LLM 추출 key_reason) 요약 제공
    acc_reasons = [p.get("key_reason") for p in acc if p.get("key_reason")][:2]
    rej_reasons = [p.get("key_reason") for p in rej if p.get("key_reason")][:2]

    if is_msk:
        lead = ("근골격계 판례에서 결과를 가른 핵심은 '퇴행성·기왕증과 업무 기여의 감별'입니다. "
                "인정 사례는 재해와 상병의 시간적·부위적 일치를 영상·의학소견으로 입증했고, "
                "불인정 사례는 발병 전 기존 질환이나 퇴행성 변화로 설명되었습니다.")
    else:
        lead = ("동일 질병구분·유사 노출요인 판례에서는 노출의 '연속성'과 '누적 노출량'이 결과를 "
                "갈랐습니다.")

    return {
        "acc": len(acc), "rej": len(rej), "keyDocs": key_docs, "cp": cp, "lead": lead,
        "acc_reasons": acc_reasons, "rej_reasons": rej_reasons,
    }


def build_tree(case, records):
    """판례 트리. 근골격계=신체부위 축, 그 외=노출요인 축.
       잎 노드에 추출정보(상병명·핵심근거·퇴행성쟁점) 포함."""
    is_msk = case.get("disease_group") == "근골격계질환"
    BODY = ["허리", "요추", "목", "경추", "어깨", "무릎", "손목", "척추", "골반", "고관절"]
    CANON = ["용접흄", "분진", "소음", "망간", "석면", "벤젠", "유기용제",
             "중량물", "반복동작", "진동", "야간근로"]
    case_keys = set(re.findall(r"[가-힣A-Za-z0-9]+", case.get("exposure_factor", "")))

    def body_key(p):
        bp = p.get("body_part") or ""
        for b in BODY:
            if b in bp:
                return "허리" if b in ("허리", "요추") else ("목" if b in ("목", "경추") else b)
        det = p.get("disease_detail") or ""
        for b in BODY:
            if b in det:
                return b
        return "기타"

    def factor_key(p):
        f = p.get("exposure_factor") or ""
        for c in CANON:
            if c in f and c in (case.get("exposure_factor") or ""):
                return c
        for c in CANON:
            if c in f:
                return c
        return (f.split("/")[0].strip() or "기타")

    keyfn = body_key if is_msk else factor_key
    axis = "신체부위" if is_msk else "노출요인"

    groups = {}
    for p in records:
        groups.setdefault(keyfn(p), []).append(p)

    children = []
    for grp, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        acc = sum(1 for p in ps if p["result"] == "인정")
        if is_msk:
            syn = {"허리": ["허리", "요추", "요부"], "목": ["목", "경추"],
                   "어깨": ["어깨", "견관절"], "무릎": ["무릎", "슬관절"]}
            cand = (case.get("body_part") or "") + (case.get("sangbyeong") or "")
            terms = syn.get(grp, [grp])
            onpath = any(t in cand for t in terms)
        else:
            onpath = bool(case_keys & set(re.findall(r"[가-힣A-Za-z0-9]+", grp)))
        leaves = [{
            "type": "case", "id": p["case_no"], "label": p["case_no"],
            "result": p["result"], "years": p.get("exposure_years"),
            "factor": p.get("exposure_factor"), "docs": p.get("decisive_docs") or [],
            "court": p.get("court"), "sim": p.get("sim"),
            "disease_detail": p.get("disease_detail"), "key_reason": p.get("key_reason"),
            "degenerative": p.get("degenerative_issue"), "excerpt": p.get("excerpt"),
        } for p in sorted(ps, key=lambda x: -(x.get("sim") or 0))]
        children.append({
            "type": "factor", "id": f"g::{grp}", "label": grp, "axis": axis,
            "count": len(ps), "accept": acc,
            "accept_rate": round(acc / len(ps) * 100) if ps else 0,
            "onpath": onpath, "children": leaves,
        })

    total = len(records)
    total_acc = sum(1 for p in records if p["result"] == "인정")
    return {
        "type": "root", "id": "root", "label": case.get("disease_group", "질병구분"),
        "axis": axis, "count": total, "accept": total_acc,
        "accept_rate": round(total_acc / total * 100) if total else 0,
        "children": children,
    }


def build_tree_jbp(case, records):
    """판정서 트리. 질병구분에 따라 분류축이 다름:
       근골격계=신체부위 / 호흡기·직업성암·진폐=부담요인(유해인자) / 그 외=직종."""
    kindc = case.get("kindc") or case.get("disease_group") or ""
    is_msk = "근골격계" in kindc

    syn_part = {"허리": ["허리", "요추", "요부"], "목": ["목", "경추"],
                "어깨": ["어깨", "견관절"], "무릎": ["무릎", "슬관절"],
                "손/손목": ["수부", "수근관", "손목"], "팔꿈치": ["팔꿈치", "주관절"]}
    # 호흡기·암·진폐 등: 유해인자(부담요인)로 묶음
    syn_haz = {"분진": ["분진", "먼지"], "용접흄": ["용접", "흄", "망간"], "석면": ["석면"],
               "유기용제": ["유기용제", "벤젠", "톨루엔"], "결정형유리규산": ["규산", "규폐"],
               "기타 유해인자": []}

    use_part = is_msk
    use_haz = any(k in kindc for k in ["호흡기", "암", "악성신생물", "진폐", "석면폐", "독성"])

    def norm_part(p):
        bps = p.get("body_parts") or ([p["body_part"]] if p.get("body_part") else [])
        for label, kws in syn_part.items():
            if any(any(k in bp for k in kws) for bp in bps):
                return label
        return bps[0] if bps else "기타"

    def norm_haz(p):
        blob = " ".join(p.get("burden") or []) + " " + (p.get("excerpt") or "") + " " + (p.get("sintcheong") or "")
        for label, kws in syn_haz.items():
            if kws and any(k in blob for k in kws):
                return label
        return "기타 유해인자"

    def norm_job(p):
        return (p.get("job_type") or "기타 직종").strip()

    if use_part:
        keyfn, axis = norm_part, "신체부위"
    elif use_haz:
        keyfn, axis = norm_haz, "유해인자"
    else:
        keyfn, axis = norm_job, "직종"

    case_blob = (case.get("body_part") or "") + (case.get("sangbyeong") or "") + " ".join(case.get("burden") or [])
    groups = {}
    for p in records:
        groups.setdefault(keyfn(p), []).append(p)

    children = []
    for grp, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        acc = sum(1 for p in ps if p["result"] == "인정")
        rej = sum(1 for p in ps if p["result"] == "불인정")
        hold = sum(1 for p in ps if p["result"] == "보류")
        onpath = grp in case_blob if grp != "기타 유해인자" else False
        leaves = [{
            "type": "case", "id": p["case_no"], "label": p["case_no"],
            "result": p["result"], "verdict": p.get("verdict"),
            "years": p.get("exposure_years"), "job": p.get("job_type"),
            "burden": p.get("burden") or [], "docs": p.get("decisive_docs") or [],
            "body_parts": p.get("body_parts") or [], "sint": p.get("sintcheong"),
            "industry": p.get("industry"), "excerpt": p.get("excerpt"),
            "noncontent": p.get("noncontent") or p.get("excerpt") or "",
            "source_url": p.get("source_url") or "https://www.data.go.kr/data/15110836/openapi.do",
        } for p in ps]
        children.append({
            "type": "factor", "id": f"b::{grp}", "label": grp, "axis": axis,
            "count": len(ps), "accept": acc, "reject": rej, "hold": hold,
            "accept_rate": round(acc / len(ps) * 100) if ps else 0,
            "onpath": onpath, "children": leaves,
        })
    total = len(records)
    return {
        "type": "root", "id": "root", "label": case.get("disease_group", "질병구분"),
        "axis": axis, "count": total,
        "accept": sum(1 for p in records if p["result"] == "인정"),
        "reject": sum(1 for p in records if p["result"] == "불인정"),
        "hold": sum(1 for p in records if p["result"] == "보류"),
        "accept_rate": round(sum(1 for p in records if p["result"] == "인정") / total * 100) if total else 0,
        "children": children,
    }


def rank_similar(case, records, topn=3):
    """현재 사건과 각 판정서의 유사도를 '비교 요소 중 일치 비율'로 산출. 정직한 절대 점수."""
    sel_parts = [x for x in (case.get("body_part") or "").split(",") if x]
    sel_burden = case.get("burden") or []
    case_years = case.get("exposure_years")
    cj = (case.get("job_type") or "").strip()
    ISSUE_KW = ["퇴행", "기왕", "흡연", "고령"]

    # 이 사건에서 '비교 가능한' 요소만 분모로 (입력 안 한 요소는 제외)
    axes = []
    if sel_parts: axes.append("part")
    if sel_burden: axes.append("burden")
    if cj: axes.append("job")
    if case_years: axes.append("years")
    denom = max(len(axes), 1)

    scored = []
    for p in records:
        matched = 0
        weight = 0.0
        reasons = []
        if "part" in axes:
            hit = [sp for sp in sel_parts if _match_part(p, [sp])]
            if hit:
                matched += 1; weight += 1.0
                reasons.append("부위·인자 일치(" + ", ".join(hit) + ")")
        if "burden" in axes:
            hit = [b for b in sel_burden if _match_burden(p, [b])]
            if hit:
                matched += 1; weight += 0.9
                reasons.append("부담요인 겹침(" + ", ".join(hit) + ")")
        if "job" in axes:
            if cj in (p.get("job_type") or ""):
                matched += 1; weight += 0.8
                reasons.append("직종 일치(" + cj + ")")
        if "years" in axes:
            if p.get("exposure_years") and abs(case_years - p["exposure_years"]) <= 5:
                matched += 1; weight += 0.7
                reasons.append(f"근무기간 유사({p['exposure_years']}년)")
        # 쟁점어는 가점(분모엔 안 들어감)
        blob = (p.get("excerpt") or "") + (p.get("sintcheong") or "")
        if any(k in blob for k in ISSUE_KW):
            weight += 0.2
        if matched > 0:
            pct = round(matched / denom * 100)
            scored.append((weight, pct, matched, reasons, p))
    scored.sort(key=lambda x: (-x[0], -x[1]))

    out = []
    for weight, pct, matched, reasons, p in scored[:topn]:
        out.append({
            "case_no": p["case_no"], "result": p["result"], "verdict": p.get("verdict"),
            "job": p.get("job_type"), "years": p.get("exposure_years"),
            "body_parts": p.get("body_parts") or [], "burden": p.get("burden") or [],
            "docs": p.get("decisive_docs") or [], "sint": p.get("sintcheong"),
            "noncontent": p.get("noncontent") or p.get("excerpt") or "",
            "match_pct": pct, "matched": matched, "axes_total": denom, "reasons": reasons,
        })
    return out


def build_guideline_jbp(case, records):
    acc = [p for p in records if p["result"] == "인정"]
    rej = [p for p in records if p["result"] == "불인정"]
    hold = [p for p in records if p["result"] == "보류"]
    na, nr = len(acc), len(rej)

    def ratio(group, field, key):
        if not group:
            return 0
        return round(sum(1 for p in group if key in (p.get(field) or [])) / len(group) * 100)

    def text_ratio(group, kw):
        if not group:
            return 0
        return round(sum(1 for p in group if kw in (p.get("excerpt") or "") or kw in (p.get("sintcheong") or "")) / len(group) * 100)

    # 결정적 자료 빈출(인정 기준)
    freq = {}
    for p in acc:
        for d in (p.get("decisive_docs") or []):
            freq[d] = freq.get(d, 0) + 1
    key_docs = sorted(freq, key=freq.get, reverse=True)
    # 부담요인 빈출(전체)
    bfreq = {}
    for p in records:
        for b in (p.get("burden") or []):
            bfreq[b] = bfreq.get(b, 0) + 1
    top_burden = sorted(bfreq, key=bfreq.get, reverse=True)[:5]

    # 자료별 '어떻게 확보하는가' 실무 지침 사전
    DOC_HOWTO = {
        "작업환경측정": "사업장 관할 또는 산업안전보건공단에 작업환경측정 결과표를 요청. 측정 미실시 사업장은 동종 공정 측정자료·유해인자 노출평가로 갈음 가능",
        "MRI": "주治의 의무기록 사본과 영상 CD를 확보하고, 판정 시점 기준 최신 영상인지 확인. 발병 전후 비교 영상이 있으면 업무 기여 입증에 유리",
        "특수건강진단": "사업장 보건관리자 또는 검진기관에 특수건강진단 결과 통보서를 요청. 유해인자 노출 이력과 건강이상 추세 확인",
        "작업환경": "공정별 작업내용·작업시간·노출 유해인자를 정리한 작업공정도와 사업주 확인서 확보",
        "진술": "동료 근로자 2인 이상의 구체적 진술서(작업내용·기간·강도)를 확보. 막연한 진술보다 일자·횟수 등 정량 표현이 효과적",
        "근전도": "신경전도·근전도 검사 결과지를 확보해 해당 부위 신경손상의 객관적 근거로 활용",
        "초음파": "근골격 초음파 판독소견서를 확보. 영상 검사와 임상소견의 일치 여부 확인",
        "흉부": "흉부 X-ray·CT 판독소견서와 폐기능검사 결과를 확보. 진폐·석면 관련은 표준판독 등급 명시 필요",
        "폐기능검사": "폐활량·확산능 등 폐기능검사 결과지를 확보. 노출 중단 후에도 지속되는 기능저하인지 확인",
    }
    BURDEN_HOWTO = {
        "중량물": "취급 중량물의 무게·빈도·자세를 작업분석으로 정량화(예: 20kg 이상을 1일 OO회). 근골격계 부담작업 11개 호 해당 여부 확인",
        "반복동작": "단위시간당 반복 횟수와 누적 작업시간을 작업동작 분석으로 기록",
        "진동": "진동공구 종류·1일 사용시간·총 사용기간을 정리하고 진동가속도 자료가 있으면 첨부",
        "용접흄": "용접 방식(피복아크/CO2 등)·1일 작업시간·환기상태·밀폐여부를 정리하고 망간 등 금속 노출 가능성 검토",
        "분진": "분진 종류·농도·노출시간과 보호구 착용 실태를 정리. 작업환경측정 결과와 연계",
        "석면": "석면 취급 공정·기간과 함께 잠복기(통상 10~40년)를 고려한 과거 직력 전체를 추적",
        "과로": "발병 전 12주간 주당 평균 업무시간(특히 60시간 초과 여부)과 야간·휴일근무 내역을 근태기록으로 입증",
    }

    def howto(name):
        for k, v in {**DOC_HOWTO, **BURDEN_HOWTO}.items():
            if k in name:
                return v
        return None

    # ── 데이터 기반 체크포인트 (근거 + 의미 + 구체 행동지침) ──
    cp = []
    # (1) 결정 자료 격차 → 무슨 자료를 어떻게 확보할지
    if acc and rej:
        all_docs = set()
        for p in acc + rej:
            all_docs.update(p.get("decisive_docs") or [])
        gaps = []
        for d in all_docs:
            ra, rr = ratio(acc, "decisive_docs", d), ratio(rej, "decisive_docs", d)
            if ra - rr >= 15:
                gaps.append((d, ra, rr))
        gaps.sort(key=lambda x: -(x[1] - x[2]))
        for d, ra, rr in gaps[:2]:
            tip = howto(d)
            txt = (f"【결정적 자료】 '{d}' 확보 — 인정 판정서의 {ra}%가 이 자료를 갖췄으나 불인정은 {rr}%에 그침"
                   f"(격차 {ra-rr}%p). 이 자료의 유무가 결과를 가르는 핵심 변수.")
            if tip:
                txt += f" ▶ 확보방법: {tip}"
            cp.append(txt)
    # (2) 부담요인 격차 → 어떻게 입증할지
    if acc and rej:
        all_b = set()
        for p in acc + rej:
            all_b.update(p.get("burden") or [])
        bgaps = []
        for b in all_b:
            ra, rr = ratio(acc, "burden", b), ratio(rej, "burden", b)
            if ra - rr >= 20:
                bgaps.append((b, ra, rr))
        bgaps.sort(key=lambda x: -(x[1] - x[2]))
        for b, ra, rr in bgaps[:1]:
            tip = howto(b)
            txt = (f"【부담·유해요인】 '{b}' 입증 — 인정 사례의 {ra}%에서 확인되나 불인정은 {rr}%"
                   f"(격차 {ra-rr}%p). 이 요인의 업무 관련성을 구체적으로 입증할 필요.")
            if tip:
                txt += f" ▶ 입증방법: {tip}"
            cp.append(txt)
    # (3) 본문 쟁점어 → 감별 방향까지
    for kw, label, action in [
        ("퇴행", "퇴행성 변화", "연령대비 퇴행 정도, 업무로 인한 자연경과 초과 악화 여부에 대한 주治의·자문의 소견을 확보. 발병 전후 영상 비교가 결정적"),
        ("기왕", "기왕증", "기왕증의 발병 시기·치료력과 현 상병의 인과관계 단절 여부를 의학적으로 정리. 업무가 기왕증을 자연경과 이상으로 악화시켰는지가 쟁점"),
    ]:
        ta, tr = text_ratio(acc, kw), text_ratio(rej, kw)
        if tr - ta >= 15:
            cp.append(f"【불인정 쟁점】 '{label}' — 불인정 판정서의 {tr}%에서 이 쟁점이 등장(인정은 {ta}%)."
                      f" 이 사건의 핵심 불인정 사유가 될 수 있음. ▶ 대응: {action}")
    # (4) 보류 경고
    if hold:
        cp.append(f"【재조사 유형 경고】 유사 조건에서 보류·판정위이송이 {len(hold)}건 발생 — 1차 자료만으로 판단이 어려웠던 유형."
                  f" 위 자료들을 상정 전에 선제 확보하면 추가 지연을 줄일 수 있음.")
    if not cp:
        cp.append("이 조건의 표본이 적어 그룹 비교가 어렵습니다. 질병구분 또는 필터 범위를 넓혀 보세요.")

    n = len(records)
    rate = round(na / n * 100) if n else 0
    lead = (f"동일 조건 판정서 {n}건 분석 — 인정 {na} · 불인정 {nr} · 보류/이송 {len(hold)} "
            f"(인정률 {rate}%). 아래는 인정 사례와 불인정 사례를 비교해 도출한, 이 사건에서 보강하면 좋을 항목입니다.")
    return {
        "acc": na, "rej": nr, "hold": len(hold), "rate": rate,
        "keyDocs": key_docs, "top_burden": top_burden, "cp": cp, "lead": lead,
    }


def _match_part(rec, parts):
    syn = {"허리": ["허리", "요추", "요부"], "목": ["목", "경추"], "어깨": ["어깨", "견관절"],
           "무릎": ["무릎", "슬관절"], "손/손목": ["수부", "수근관", "손목"], "팔꿈치": ["팔꿈치", "주관절"],
           "요추": ["요추", "허리"], "경추": ["경추", "목"], "흉추": ["흉추"],
           # 유해인자(호흡기·암·진폐)
           "용접흄": ["용접", "흄", "망간"], "분진": ["분진", "먼지"], "석면": ["석면"],
           "유기용제": ["유기용제", "벤젠", "톨루엔", "솔벤트"], "결정형유리규산": ["규산", "규폐", "실리카"],
           "석탄분진": ["석탄", "탄"], "광물분진": ["광물", "광산"], "방사선": ["방사선", "라돈"],
           # 소음(난청)
           "85dB이상": ["85", "소음"], "90dB이상": ["90", "소음"], "충격소음": ["충격", "폭발음"],
           # 위험요인(뇌심혈관·정신)
           "과로": ["과로", "장시간", "초과근무"], "야간근무": ["야간"], "교대근무": ["교대"],
           "스트레스": ["스트레스", "정신적"], "직장내괴롭힘": ["괴롭힘", "갑질"], "사고목격": ["목격", "외상"],
           "폭언폭행": ["폭언", "폭행"], "화학물질": ["화학", "산", "알칼리"], "금속": ["금속", "크롬", "니켈"], "습윤작업": ["습윤", "물작업"]}
    bps = rec.get("body_parts") or []
    blob = " ".join(bps) + " " + (rec.get("excerpt") or "") + " " + (rec.get("sintcheong") or "") + " ".join(rec.get("burden") or [])
    for sel in parts:
        kws = syn.get(sel, [sel])
        if any(k in blob for k in kws):
            return True
    return False

def _match_burden(rec, burdens):
    syn = {"중량물": ["중량물", "중량"], "반복동작": ["반복"], "진동": ["진동"],
           "부적절자세": ["부적절", "쪼그", "구부", "비틀"], "장시간": ["장시간"], "장시간운전": ["운전", "장시간"],
           "고농도노출": ["고농도", "다량"], "장기노출": ["장기간", "년간", "오랜"], "밀폐공간": ["밀폐", "환기"],
           "직접취급": ["직접", "취급"], "간접노출": ["간접", "주변"], "연속노출": ["연속", "지속"],
           "보호구미착용": ["보호구", "미착용", "마스크"], "급성발병": ["급성", "갑자기"], "기왕증동반": ["기왕", "기존"],
           "흡입": ["흡입", "호흡"], "접촉": ["접촉", "피부"], "유해물질노출": ["유해", "화학", "분진"], "지속성": ["지속"], "급성": ["급성"]}
    blob = " ".join(rec.get("burden") or []) + " " + (rec.get("excerpt") or "") + " " + (rec.get("sintcheong") or "")
    for sel in burdens:
        kws = syn.get(sel, [sel])
        if any(k in blob for k in kws):
            return True
    return False


def make_guideline_jbp(case):
    """판정서 기반 가이드라인 (메인 경로). SERVICE_KEY 필요."""
    kindc = case.get("kindc") or case.get("disease_group")
    records, total = jbp.fetch_body(SERVICE_KEY, kindc=kindc, rows=120)

    # ── 선택한 부위·부담요인으로 필터 ──
    sel_parts = [x for x in (case.get("body_part") or "").split(",") if x]
    sel_burden = case.get("burden") or []
    base_n = len(records)
    if sel_parts:
        records = [r for r in records if _match_part(r, sel_parts)]
    if sel_burden:
        records = [r for r in records if _match_burden(r, sel_burden)]
    # 필터 결과가 너무 적으면(5건 미만) 필터 해제하고 안내
    filter_note = None
    if (sel_parts or sel_burden) and len(records) < 5:
        records, _ = jbp.fetch_body(SERVICE_KEY, kindc=kindc, rows=120)
        filter_note = "선택 조건에 맞는 판정서가 적어 전체 결과를 표시합니다. 조건을 줄여보세요."
    elif sel_parts or sel_burden:
        filter_note = f"필터 적용: {base_n}건 → {len(records)}건 (부위: {', '.join(sel_parts) or '—'} · 부담요인: {', '.join(sel_burden) or '—'})"

    tree = build_tree_jbp(case, records)
    guide = build_guideline_jbp(case, records)
    lf = getattr(jbp, "LAST_FETCH", {}) or {}
    return {
        "mode": "판정서(LIVE)",
        "filter_note": filter_note,
        "evidence": {
            "result_code": lf.get("result_code", ""),
            "fetched_at": lf.get("at", ""),
            "population": total,           # 이 질병구분 전체 건수(모집단)
            "fetched": base_n,             # 실제 조회한 건수
            "analyzed": len(records),      # 필터 후 분석 건수
            "op": lf.get("op", ""),
            "kindc": kindc,
        },
        "provenance": [
            {"dataset": "근로복지공단_질병판정서 조회 서비스", "provider": "근로복지공단",
             "op": "getJilbyeongResultNaeyongPstate", "filter": f"kindc={kindc}",
             "count": total, "url": "https://www.data.go.kr/data/15110836/openapi.do"},
            {"dataset": "산재보험법 시행령 별표3", "provider": "법제처",
             "op": "분류 매핑 근거", "filter": "업무상질병 인정기준",
             "count": "—", "url": "https://www.law.go.kr"},
        ],
        "records": records, "tree": tree, "guideline": guide, "laws": LAWS,
        "similar": rank_similar(case, records, 3),
        "metrics": compute_metrics(case.get("work_history", [])),
    }


def make_guideline(case):
    # 판정서 키가 있으면 판정서(메인), 없으면 기존 판례/추출DB 경로(폴백)
    if SERVICE_KEY:
        try:
            return make_guideline_jbp(case)
        except Exception as e:
            print(f"[판정서 조회 실패 → 폴백] {e}")
    want = case["disease_group"]
    recs, total, mode = comwel_fetch.get_records(
        SERVICE_KEY, kindC=DISEASE_KINDC, rows=50, disease_group=want)
    if mode != "extracted":      # 라이브/데모면 세부질병 재필터
        filtered = [r for r in recs if (not r.get("disease_group") or r["disease_group"] == want)]
        recs = filtered or recs
    m = compute_metrics(case["work_history"])
    ranked = rank(case, recs, m)
    guide = build_guideline(case, ranked, m)
    op_label = ("getSjbPrecedentNaeyongPstate + Gemini 추출"
                if mode == "extracted" else "getSjbPrecedentNaeyongPstate")
    return {
        "mode": mode,
        "provenance": [
            {"dataset": "근로복지공단_산재보험 판례 판결문 조회 서비스", "provider": "근로복지공단",
             "op": op_label, "filter": f"kindC={DISEASE_KINDC} → {want}",
             "count": total, "url": "https://www.data.go.kr/data/15041878/openapi.do"},
            {"dataset": "국가법령정보 공동활용", "provider": "법제처",
             "op": "DRF/lawSearch", "filter": f"query={case['disease_group']} 업무상질병",
             "count": len(LAWS), "url": "https://open.law.go.kr"},
        ],
        "records": ranked, "laws": LAWS, "guideline": guide, "metrics": m,
        "tree": build_tree(case, ranked),
    }


# ── HTTP 핸들러 ──────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):  # 콘솔 소음 줄이기
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(DASHBOARD, encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "dashboard.html 을 같은 폴더에 두세요.", "text/plain; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, json.dumps({"mode": "판정서(LIVE)" if SERVICE_KEY else "demo"}))
        elif self.path == "/api/options":
            self._send(200, json.dumps({
                "kindc": jbp.KINDC_LIST, "kinda": jbp.KINDA_LIST, "kindb": jbp.KINDB_TOP,
                "filters": jbp.DISEASE_FILTERS, "default_filter": jbp.DEFAULT_FILTER,
            }, ensure_ascii=False))
        elif self.path == "/api/timeline_demo":
            self._send(200, json.dumps({
                "payload": timeline.demo_payload(),
                "analysis": timeline.analyze(timeline.demo_payload()),
            }, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(ln).decode("utf-8") if ln else "{}"
        try:
            body = json.loads(raw)
            if self.path == "/api/guideline":
                result = make_guideline(body)
            elif self.path == "/api/predict":
                preds = law_mapper.predict(
                    body.get("case_text", ""), body.get("exposure", ""),
                    body.get("job", ""), body.get("years"))
                result = law_mapper.seal_prediction(
                    body.get("case_id", "CASE"), body.get("case_text", ""), preds)
            elif self.path == "/api/compare":
                result = law_mapper.compare(body.get("sealed", {}), body.get("advisory", ""))
            elif self.path == "/api/timeline":
                payload = body if body.get("careers") else timeline.demo_payload()
                result = timeline.analyze(payload)
            elif self.path == "/api/timeline_demo":
                result = {"payload": timeline.demo_payload(), "analysis": timeline.analyze(timeline.demo_payload())}
            elif self.path == "/api/timeline_cases":
                # 판정서에서 실제 타임라인 케이스 추출
                kindc = body.get("kindc") or "호흡기질환(천식 포함)"
                if SERVICE_KEY:
                    recs, total = jbp.fetch_body(SERVICE_KEY, kindc=kindc, rows=60)
                else:
                    recs = []
                cases = timeline.cases_from_records(recs, limit=8)
                result = {"kindc": kindc, "source_count": len(recs),
                          "cases": [{"case": c, "analysis": timeline.analyze(c)} for c in cases]}
            elif self.path == "/api/timeline_gallery":
                # 여러 질병 사례 모아보기: 실제 추출(A) + 대표 시나리오(B) 섞기
                diseases = body.get("diseases") or [
                    "근골격계질환(척추질환 제외)", "호흡기질환(천식 포함)", "난청",
                    "심장질환", "뇌혈관질환", "정신질환"]
                gallery = []
                for dz in diseases:
                    real = []
                    if SERVICE_KEY:
                        try:
                            recs, _ = jbp.fetch_body(SERVICE_KEY, kindc=dz, rows=40)
                            real = timeline.cases_from_records(recs, limit=1)
                        except Exception:
                            real = []
                    if real:
                        c = real[0]
                        gallery.append({"disease": dz, "kind": "real",
                                        "label": (c.get("source_case") or dz) + " · 실제 판정서",
                                        "payload": c, "analysis": timeline.analyze(c)})
                    else:
                        s = timeline.sample_cases([dz])
                        if s:
                            gallery.append(s[0])
                result = {"gallery": gallery}
            else:
                return self._send(404, json.dumps({"error": "not found"}))
            self._send(200, json.dumps(result, ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))


def main():
    port = int(os.environ.get("PORT", 8000))
    mode = "라이브(실제 공공데이터)" if SERVICE_KEY else "데모(샘플 원문에 동일 추출 적용)"
    print(f"\n  산재 재조사 가이드라인 서버")
    print(f"  모드: {mode}")
    print(f"  → 포트 {port} (로컬: http://localhost:{port}, 종료: Ctrl+C)\n")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()
