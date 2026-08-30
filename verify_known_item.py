# -*- coding: utf-8 -*-
"""
골든타임 — Known-item 정량 검증 스크립트
========================================
실제 판정서 N건을 '정답'으로 숨겨두고, 그 판정서의 핵심 요소(부위·부담요인·직종)만으로
시스템 유사 검색을 돌렸을 때 원본 판정서가 Top-1 / Top-3 / Top-10 안에 드는지 측정한다.

사용법:
    python3 verify_known_item.py <SERVICE_KEY>

출력:
    - 콘솔: 질병구분별 / 전체 Top-1·3·10 적중률
    - 검증결과_요약.txt  : 발표 슬라이드에 넣을 요약
    - 담당자평가_사례목록.csv : 담당자에게 보낼 사례별 평가지 (엑셀로 열림)
"""
import sys, csv, random, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import jbpjeong_source as src
import app  # rank_similar 재사용

# ── 검증 표본 설계 (질병구분별 건수 — 데이터 양에 비례) ──
SAMPLE_PLAN = [
    ("근골격계질환(척추질환 제외)", 15),
    ("근골격계질환 (척추질환)", 10),
    ("호흡기질환(천식 포함)", 10),
    ("난청", 5),
    ("뇌혈관질환", 5),
    ("악성신생물(직업성 암 포함)", 5),
]
POOL_PER_KINDC = 120   # 각 질병구분에서 가져올 후보 풀(검색 대상이자 표본 추출원)
TOPNS = (1, 3, 10)

# ── 울산 주력산업(조선·중공업·자동차·석유화학) 직종·업종 키워드 ──
# 지역 필드가 API에 없어 '울산 판정서'를 직접 고를 수는 없으므로,
# 울산에서 흔한 직종·업종을 우선 표본으로 뽑아 담당자에게 익숙한 사례로 구성한다.
ULSAN_KW = [
    "용접", "도장", "배관", "선박", "조선", "취부", "사상", "그라인", "절단",  # 조선·중공업
    "조립", "프레스", "자동차", "도금",                                      # 자동차
    "플랜트", "보온", "화학", "정유", "탱크",                                 # 석유화학
    "크레인", "중장비", "건조",
]

def is_ulsan_like(rec):
    blob = (rec.get("job_type") or "") + (rec.get("industry") or "")
    return any(k in blob for k in ULSAN_KW)

def build_query_case(rec, kindc):
    """판정서에서 '실사용 입력' 조건을 재구성 — 본문 자체가 아니라 담당자가 입력할 법한 요소만."""
    return {
        "kindc": kindc,
        "disease_group": kindc,
        "body_part": ",".join(rec.get("body_parts") or ([] if not rec.get("body_part") else [rec["body_part"]])),
        "burden": rec.get("burden") or [],
        "job_type": rec.get("job_type") or "",
        "exposure_years": rec.get("exposure_years"),
        "sangbyeong": "",
    }

def main():
    if len(sys.argv) < 2:
        print("사용법: python3 verify_known_item.py <SERVICE_KEY>")
        sys.exit(1)
    key = sys.argv[1]
    random.seed(42)  # 재현 가능

    rows_csv = []
    per_kindc = {}
    overall = {n: 0 for n in TOPNS}
    overall_total = 0
    overall_rr = 0.0

    for kindc, want_n in SAMPLE_PLAN:
        print(f"\n[{kindc}] 판정서 수집 중...")
        try:
            pool, total = src.fetch_body_recent(key, kindc=kindc, want=POOL_PER_KINDC)
        except Exception as e:
            print(f"  ! 수집 실패: {e} — 건너뜀")
            continue
        pool = [p for p in pool if p.get("case_no")]
        if len(pool) < want_n + 5:
            print(f"  ! 풀이 작음({len(pool)}건) — 표본 축소")
            want_n = max(min(want_n, len(pool) - 3), 0)
        if want_n == 0:
            continue
        # 표본: 부위/부담요인/직종 중 1개 이상 추출된 판정서만 (입력을 만들 수 있어야 함)
        eligible = [p for p in pool if (p.get("body_parts") or p.get("burden") or p.get("job_type"))]
        # 울산 주력산업 직종·업종 우선 표본, 부족하면 일반에서 보충
        ulsan_first = [p for p in eligible if is_ulsan_like(p)]
        others = [p for p in eligible if not is_ulsan_like(p)]
        random.shuffle(ulsan_first); random.shuffle(others)
        sample = (ulsan_first + others)[:want_n]
        n_ulsan = sum(1 for p in sample if is_ulsan_like(p))

        hit = {n: 0 for n in TOPNS}
        rr_sum = 0.0   # MRR용 — 원본 순위의 역수 합
        for target in sample:
            case = build_query_case(target, kindc)
            ranked = app.rank_similar(case, pool, topn=max(TOPNS))
            ranked_ids = [r["case_no"] for r in ranked]
            rank = ranked_ids.index(target["case_no"]) + 1 if target["case_no"] in ranked_ids else None
            if rank is not None:
                rr_sum += 1.0 / rank
            for n in TOPNS:
                if rank is not None and rank <= n:
                    hit[n] += 1
            rows_csv.append({
                "질병구분": kindc,
                "판정서번호": target["case_no"],
                "심의결과": target.get("verdict", ""),
                "직종": target.get("job_type", ""),
                "업종": target.get("industry", ""),
                "울산 주력산업형": "O" if is_ulsan_like(target) else "",
                "입력 요소(부위)": ",".join(target.get("body_parts") or []),
                "입력 요소(부담요인)": ",".join(target.get("burden") or []),
                "원본 순위": rank if rank is not None else f">{max(TOPNS)}",
                "Hit@3": "O" if (rank is not None and rank <= 3) else "X",
            })
        n_s = len(sample)
        per_kindc[kindc] = (n_s, dict(hit), rr_sum)
        overall_total += n_s
        overall_rr += rr_sum
        for n in TOPNS:
            overall[n] += hit[n]
        mrr_k = rr_sum / n_s if n_s else 0
        print(f"  표본 {n_s}건(울산형 {n_ulsan}) — Top1 {hit[1]} · Top3 {hit[3]} · Top10 {hit[10]} · MRR {mrr_k:.2f}")

    # ── 요약 출력 ──
    lines = []
    lines.append("=" * 56)
    lines.append("골든타임 Known-item 정량 검증 결과")
    lines.append(f"(실제 판정서 {overall_total}건 — 원본을 숨기고 핵심 요소만으로 검색)")
    n_ul_all = sum(1 for r in rows_csv if r.get("울산 주력산업형") == "O")
    lines.append(f"(표본 중 울산 주력산업형 직종·업종 {n_ul_all}건 — 조선·자동차·화학 등)")
    lines.append("=" * 56)
    lines.append(f"{'질병구분':<24}{'표본':>4}{'Hit@1':>7}{'Hit@3':>7}{'Hit@10':>8}{'MRR':>7}")
    for kindc, (n_s, hit, rr) in per_kindc.items():
        lines.append(f"{kindc:<24}{n_s:>4}{hit[1]:>7}{hit[3]:>7}{hit[10]:>8}{(rr/n_s if n_s else 0):>7.2f}")
    lines.append("-" * 64)
    if overall_total:
        mrr = overall_rr / overall_total
        lines.append(f"{'전체':<24}{overall_total:>4}"
                     f"{overall[1]:>7}{overall[3]:>7}{overall[10]:>8}{mrr:>7.2f}")
        lines.append("")
        lines.append(f"Hit@1: {overall[1]/overall_total*100:.0f}%   "
                     f"Hit@3: {overall[3]/overall_total*100:.0f}%   "
                     f"Hit@10: {overall[10]/overall_total*100:.0f}%   "
                     f"MRR: {mrr:.2f}")
        lines.append("(Hit@K = 원본이 상위 K건 안에 든 비율 · MRR = 원본 순위 역수의 평균, 1에 가까울수록 상위)")
    summary = "\n".join(lines)
    print("\n" + summary)
    with open("검증결과_요약.txt", "w", encoding="utf-8") as f:
        f.write(summary + "\n")

    # ── 검증 기록 CSV (재현성 증빙용, 엑셀에서 열림) ──
    if rows_csv:
        with open("검증기록_사례별.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows_csv[0].keys()))
            w.writeheader()
            w.writerows(rows_csv)
        print(f"\n생성: 검증결과_요약.txt / 검증기록_사례별.csv ({len(rows_csv)}건)")
        print("→ 요약은 발표 슬라이드에, 사례별 기록은 재현성 증빙(부록)으로 활용하세요.")

if __name__ == "__main__":
    main()
