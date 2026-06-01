# -*- coding: utf-8 -*-
"""
별표3 기반 분류 매핑 엔진
=================================================
민원인 일상어 → 산재보험법 시행령 별표3 인정요건 대조 → 표준 질병분류 후보.

설계 원칙:
  - AI는 '판정'하지 않는다. 별표3 요건과 입력의 '겹침'을 근거와 함께 후보로 제시.
  - 자문위 결정 전에 예측을 '봉인'(타임스탬프 기록)하여 독립성을 보장.
  - 봉인된 예측 ↔ 자문위 결정을 사후 '대조'하여 일치=근거기록 / 불일치=재조사 쟁점.

출처: 산업재해보상보험법 시행령 별표3(제34조제3항 관련).
      ※ 현재는 핵심 요건을 내장. 법령 API(국가법령정보 OC) 연동 시 동적 로딩으로 교체 가능.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

LAW_SOURCE = "산업재해보상보험법 시행령 별표3 (제34조제3항 관련)"
LAW_URL = "https://www.law.go.kr/법령/산업재해보상보험법시행령/별표3"

# ── 별표3 핵심 인정요건 지식베이스 ──
# signals: 일상어/사실관계에서 잡아낼 신호 키워드(증상·작업·노출)
# criteria: 그 분류로 인정되기 위해 확인해야 할 법정 요건(check=자동확인 가능 여부)
DISEASE_KB = {
    "소음성난청": {
        "ref": "별표3 제7호 차목",
        "signals": ["귀", "먹먹", "난청", "잘 안 들", "이명", "소음", "시끄", "청력", "프레스", "단조", "방직"],
        "criteria": [
            {"id": "noise_85db", "text": "연속음 85dB(A) 이상 소음 작업장 노출", "need": "작업환경측정자료"},
            {"id": "dur_3y", "text": "해당 작업 3년 이상 종사 경력", "need": "직력자료"},
            {"id": "loss_40db", "text": "한 귀 청력손실 40dB 이상(6분법) 감각신경성 난청", "need": "순음청력검사"},
            {"id": "no_conductive", "text": "기도-골도 청력역치 차이 각 주파수 10dB 이내(전음성 배제)", "need": "순음청력검사"},
        ],
    },
    "근골격계질환": {
        "ref": "별표3 제2호",
        "signals": ["허리", "무릎", "어깨", "목", "손목", "디스크", "추간판", "삐끗", "무거운", "중량물",
                    "반복", "들어올", "쪼그", "통증", "결림"],
        "criteria": [
            {"id": "body_burden", "text": "특정 신체부위에 부담을 주는 업무 수행", "need": "작업내용·작업동작 분석"},
            {"id": "repetition", "text": "반복동작·중량물 취급 등 부담요인 노출", "need": "작업환경·업무량 자료"},
            {"id": "med_finding", "text": "해당 부위 손상의 의학적 소견(영상검사 등)", "need": "MRI/X-ray 등 의무기록"},
            {"id": "causal", "text": "퇴행성 변화가 아닌 업무 관련 악화로 인정", "need": "의학적 소견서"},
        ],
    },
    "뇌심혈관질환": {
        "ref": "별표3 제1호",
        "signals": ["쓰러", "뇌출혈", "뇌경색", "심근경색", "심장", "뇌졸중", "과로", "야근",
                    "스트레스", "교대", "장시간", "혈압", "가슴"],
        "criteria": [
            {"id": "acute_event", "text": "돌발·급격한 업무환경 변화 또는 단기·만성 과로", "need": "근무기록·사건경위"},
            {"id": "worktime", "text": "발병 전 12주 업무시간 등 과로 기준 충족", "need": "근로시간 자료"},
            {"id": "diagnosis", "text": "뇌실질내출혈·뇌경색·심근경색 등 진단 확정", "need": "의무기록"},
            {"id": "not_natural", "text": "자연발생적 악화가 아닐 것", "need": "기왕력·건강검진 자료"},
        ],
    },
    "직업성폐질환": {
        "ref": "별표3 제3·10호",
        "signals": ["숨", "기침", "호흡", "폐", "진폐", "천식", "분진", "용접", "용접흄", "먼지",
                    "가래", "흉부", "COPD"],
        "criteria": [
            {"id": "dust_exposure", "text": "분진·용접흄 등 호흡기 유해인자 노출", "need": "작업환경측정·작업공정"},
            {"id": "exposure_dur", "text": "유해인자 노출 기간·농도가 발병 가능 수준", "need": "직력·측정자료"},
            {"id": "img_finding", "text": "흉부 영상·폐기능 검사상 해당 소견", "need": "흉부X-ray/CT·폐기능검사"},
            {"id": "exclude_other", "text": "흡연 등 다른 원인과의 관계 검토", "need": "기왕력·문진"},
        ],
    },
    "직업성암": {
        "ref": "별표3 제5호",
        "signals": ["암", "백혈병", "악성", "종양", "벤젠", "석면", "발암", "방사선", "포름알데히드"],
        "criteria": [
            {"id": "carcinogen", "text": "벤젠·석면 등 발암물질 노출 경력", "need": "작업환경측정·직력"},
            {"id": "latency", "text": "노출 후 해당 암의 잠복기 정합", "need": "직력·진단일"},
            {"id": "dose", "text": "누적 노출량이 발병 가능 수준", "need": "역학조사·측정자료"},
            {"id": "histology", "text": "조직학적으로 해당 암종 확정", "need": "병리검사 의무기록"},
        ],
    },
}


def _signal_text(case_text, exposure="", job=""):
    return f"{case_text} {exposure} {job}"


# DISEASE_KB 분류명 → 질병판정서 kindc 값 매핑
KB_TO_KINDC = {
    "소음성난청": "난청",
    "근골격계질환": "근골격계질환(척추질환 제외)",
    "뇌심혈관질환": "뇌혈관질환",
    "직업성폐질환": "호흡기질환(천식 포함)",
    "직업성암": "악성신생물(직업성 암 포함)",
}


def predict(case_text, exposure="", job="", years=None):
    """일상어/사실관계 → 별표3 분류 후보 리스트(신뢰도 내림차순)."""
    text = _signal_text(case_text, exposure, job)
    results = []
    for dz, kb in DISEASE_KB.items():
        hits = [s for s in kb["signals"] if s in text]
        if not hits:
            continue
        conf = min(1.0, round(len(hits) / 4, 2))
        crits = []
        for c in kb["criteria"]:
            status = "확인필요"
            if c["id"] == "dur_3y" and years and years >= 3:
                status = "충족추정"
            crits.append({**c, "status": status})
        results.append({
            "disease": dz, "kindc": KB_TO_KINDC.get(dz, dz), "ref": kb["ref"],
            "confidence": conf, "matched_signals": hits, "criteria": crits,
        })
    results.sort(key=lambda x: x["confidence"], reverse=True)

    # ── 근골격계: 척추/비척추 부위 분기 (둘 다 후보로 제시) ──
    SPINE_KW = ["허리", "목", "요추", "경추", "척추", "디스크", "추간판", "협착"]
    LIMB_KW = ["어깨", "무릎", "손목", "팔꿈치", "발목", "손가락", "회전근개"]
    msk = next((r for r in results if r["disease"] == "근골격계질환"), None)
    if msk:
        has_spine = any(k in text for k in SPINE_KW)
        has_limb = any(k in text for k in LIMB_KW)
        spine_pred = {
            "disease": "근골격계질환(척추)", "kindc": "근골격계질환 (척추질환)",
            "ref": "별표3 제2호", "confidence": msk["confidence"],
            "matched_signals": [k for k in SPINE_KW if k in text],
            "criteria": msk["criteria"],
        }
        nonspine_pred = {
            **msk, "disease": "근골격계질환(척추 제외)",
            "kindc": "근골격계질환(척추질환 제외)",
        }
        # 기존 단일 근골격계 항목을 제거하고, 해당하는 후보들로 교체
        results = [r for r in results if r["disease"] != "근골격계질환"]
        if has_spine and has_limb:
            # 부위가 섞임 → 둘 다 후보(척추 우선)
            results = [spine_pred, nonspine_pred] + results
        elif has_spine:
            # 허리·목 등 척추 신호 → 척추질환 1순위 + 척추제외도 후보로 함께
            results = [spine_pred, nonspine_pred] + results
        else:
            # 어깨·무릎 등만 → 척추제외
            results = [nonspine_pred] + results
        # 신뢰도 재정렬(척추 신호가 분명하면 척추 우선 유지)
    return results


def seal_prediction(case_id, case_text, predictions):
    """예측 봉인: 타임스탬프 + 해시로 '자문위보다 먼저, 독립적으로' 생성됐음을 기록."""
    ts = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({"case_id": case_id, "case_text": case_text,
                          "predictions": predictions, "sealed_at": ts},
                         ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return {"sealed_at": ts, "seal_hash": digest, "predictions": predictions,
            "source": LAW_SOURCE, "source_url": LAW_URL}


def compare(sealed, advisory_disease):
    """봉인된 AI 예측 ↔ 자문위 결정 대조. 둘 다 질병구분(kindc) 기준으로 비교."""
    preds = sealed["predictions"]
    # 예측의 kindc(판정서 분류값) 기준. 없으면 disease로 폴백.
    def pk(p):
        return p.get("kindc") or p.get("disease")
    top = pk(preds[0]) if preds else None
    pred_set = {pk(p) for p in preds}
    if advisory_disease == top:
        verdict = "일치"
    elif advisory_disease in pred_set:
        verdict = "후보내_일치"
    else:
        verdict = "불일치"
    focus = []
    if verdict != "일치":
        # 자문위가 고른 분류의 요건을 KB에서 역으로 찾아 제시
        adv_kb = None
        for name, kb in DISEASE_KB.items():
            if KB_TO_KINDC.get(name) == advisory_disease or name == advisory_disease:
                adv_kb = kb
                break
        if adv_kb:
            focus = [f"{c['text']} → {c['need']}" for c in adv_kb["criteria"]]
    return {
        "verdict": verdict,
        "ai_top": top,
        "advisory": advisory_disease,
        "in_candidates": advisory_disease in pred_set,
        "recheck_focus": focus,
    }


if __name__ == "__main__":
    # 데모: 일상어 입력 → 예측 → 봉인 → 자문위 입력 → 대조
    txt = "귀가 먹먹하고 시끄러운 공장에서 오래 일했다"
    preds = predict(txt, exposure="소음", job="프레스공", years=15)
    print("[예측]")
    for p in preds:
        print(f"  {p['disease']} (신뢰도 {p['confidence']}) · {p['ref']} · 신호 {p['matched_signals']}")
    sealed = seal_prediction("CASE-001", txt, preds)
    print(f"\n[봉인] {sealed['sealed_at']} · hash={sealed['seal_hash']}")
    print("\n[자문위 입력] 소음성난청")
    print("[대조]", json.dumps(compare(sealed, "소음성난청"), ensure_ascii=False))
    print("\n[자문위가 다르게 본 경우] 이명(→ 후보 밖)")
    print("[대조]", json.dumps(compare(sealed, "메니에르병"), ensure_ascii=False))
