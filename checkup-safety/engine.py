"""건강검진 안전 경고 규칙 엔진.

환자 상태(dict) + 오늘 예정 검사(list) → rules.yaml 의 규칙을 평가해 경고 목록을 돌려준다.
AI 예측 모델이 아니라, 근거 문헌이 붙은 if-then 규칙이므로 왜 경고가 떴는지 항상 설명할 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RULES_PATH = Path(__file__).parent / "rules.yaml"

# 검사 코드 → (한글 이름, 담당 검사실)
EXAMS: dict[str, tuple[str, str]] = {
    "blood_draw": ("공복 채혈", "채혈실"),
    "xray": ("흉부 X-ray", "영상의학"),
    "ct_contrast": ("조영 CT", "영상의학"),
    "mri": ("MRI", "영상의학"),
    "mri_contrast": ("조영 MRI(가돌리늄)", "영상의학"),
    "sedation_egd": ("수면 위내시경", "내시경실"),
    "sedation_colon": ("수면 대장내시경", "내시경실"),
    "exercise_test": ("운동부하검사", "기능검사실"),
    "pft": ("폐기능검사", "기능검사실"),
}
STATIONS = ["접수·문진", "신체계측", "채혈실", "영상의학", "내시경실", "기능검사실"]
FASTING_EXAMS = {"blood_draw", "sedation_egd", "sedation_colon"}
SEDATION_EXAMS = {"sedation_egd", "sedation_colon"}
LEVEL_ORDER = {"red": 2, "yellow": 1, "green": 0}


# ---------------------------------------------------------------- 파생 변수
def stop_bang(p: dict) -> tuple[int | None, int]:
    """STOP-Bang 점수(0~8)와 응답된 문항 수를 반환. 문항이 하나도 없으면 None."""
    items = {
        "S": p.get("snoring"),
        "T": p.get("tired"),
        "O": p.get("observed_apnea"),
        "P": p.get("hypertension"),
        "B": None if p.get("bmi") is None else p["bmi"] > 35,
        "A": None if p.get("age") is None else p["age"] > 50,
        "N": p.get("neck_large"),
        "G": None if p.get("sex") is None else p["sex"] == "M",
    }
    answered = [v for v in items.values() if v is not None]
    if not answered:
        return None, 0
    return sum(bool(v) for v in answered), len(answered)


def derive(patient: dict, exams: list[str]) -> dict:
    """측정값에서 BMI, STOP-Bang 등을 계산해 평가용 상태를 만든다."""
    p = {k: v for k, v in patient.items() if v is not None and v != ""}
    h, w = p.get("height_cm"), p.get("weight_kg")
    if h and w:
        p["bmi"] = round(w / (h / 100) ** 2, 1)
    score, n = stop_bang(p)
    if score is not None:
        p["stopbang"] = score
        p["stopbang_items"] = n
    ex = set(exams)
    p["_exams"] = ex
    p["fasting_exam"] = bool(ex & FASTING_EXAMS)
    p["sedation"] = bool(ex & SEDATION_EXAMS)
    return p


# ---------------------------------------------------------------- 조건 평가
def _cmp(val: Any, op: str, target: Any) -> bool:
    if op == "eq":
        return val == target
    if op == "ne":
        return val != target
    if op == "in":
        return val in target
    if val is None:
        return False
    try:
        return {"lt": val < target, "le": val <= target,
                "gt": val > target, "ge": val >= target}[op]
    except TypeError:
        return False


def evaluate(cond: dict, p: dict, trace: list[str]) -> bool:
    """조건 트리를 평가한다. trace 에는 참이 된 말단 조건을 사람이 읽을 수 있게 남긴다."""
    if "all" in cond:
        sub: list[str] = []
        ok = all(evaluate(c, p, sub) for c in cond["all"])
        if ok:
            trace.extend(sub)
        return ok
    if "any" in cond:
        hit = False
        for c in cond["any"]:
            sub = []
            if evaluate(c, p, sub):
                trace.extend(sub)
                hit = True  # 설명을 위해 참인 항목을 모두 모은다
        return hit
    if "not" in cond:
        return not evaluate(cond["not"], p, [])
    if "exam" in cond:
        names = cond["exam"] if isinstance(cond["exam"], list) else [cond["exam"]]
        hit = [n for n in names if n in p["_exams"]]
        if hit:
            trace.append("예정 검사: " + ", ".join(EXAMS[n][0] for n in hit))
        return bool(hit)
    f = cond["field"]
    val = p.get(f)
    if "missing" in cond:
        ok = (val is None) == cond["missing"]
        if ok and cond["missing"]:
            trace.append(f"{label(f)} 정보 없음")
        return ok
    for op in ("eq", "ne", "in", "lt", "le", "gt", "ge"):
        if op in cond:
            ok = _cmp(val, op, cond[op])
            if ok:
                trace.append(_describe(f, val))
            return ok
    raise ValueError(f"알 수 없는 조건: {cond}")


# ---------------------------------------------------------------- 결과
@dataclass
class Alert:
    id: str
    level: str
    module: str
    title: str
    why: str
    actions: list[str]
    stations: list[str]
    evidence: list[str]
    triggered_by: list[str] = field(default_factory=list)


def load_rules(path: Path = RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


_RULEBOOK: dict | None = None


def rulebook() -> dict:
    global _RULEBOOK
    if _RULEBOOK is None:
        _RULEBOOK = load_rules()
    return _RULEBOOK


def assess(patient: dict, exams: list[str], book: dict | None = None) -> list[Alert]:
    book = book or rulebook()
    p = derive(patient, exams)
    fired: dict[str, Alert] = {}
    for r in book["rules"]:
        trace: list[str] = []
        if evaluate(r["when"], p, trace):
            fired[r["id"]] = Alert(
                id=r["id"], level=r["level"], module=r["module"], title=r["title"],
                why=r["why"], actions=r["actions"], stations=r["stations"],
                evidence=r.get("evidence", []),
                triggered_by=list(dict.fromkeys(trace)),  # 중복 제거, 순서 유지
            )
    # 더 구체적인(상위) 규칙이 발동하면 일반 규칙은 숨긴다
    for r in book["rules"]:
        if r["id"] in fired and any(s in fired for s in r.get("suppressed_by", [])):
            del fired[r["id"]]
    return sorted(fired.values(), key=lambda a: (-LEVEL_ORDER[a.level], a.id))


def max_level(alerts: list[Alert]) -> str:
    return max((a.level for a in alerts), key=LEVEL_ORDER.get, default="green")


# ---------------------------------------------------------------- 표시용
LABELS = {
    "age": "나이", "sex": "성별", "bmi": "BMI", "sbp": "수축기혈압", "dbp": "이완기혈압",
    "hr": "맥박", "temp_c": "체온", "spo2": "산소포화도", "glucose_poc": "현장 혈당",
    "height_cm": "키", "weight_kg": "몸무게", "egfr": "eGFR", "hb": "혈색소(Hb)",
    "platelet": "혈소판", "pregnancy": "임신 여부", "prior_syncope_blood": "과거 채혈 중 실신",
    "needle_fear": "주사 공포", "dehydration": "탈수 의심(구갈·설사·수분섭취 부족)",
    "diabetes": "당뇨", "insulin": "인슐린 사용", "sulfonylurea": "설포닐우레아 복용",
    "hypoglycemia_history": "저혈당 병력", "anticoagulant": "항응고제 복용",
    "antiplatelet": "항혈소판제 복용", "bleeding_disorder": "출혈질환",
    "ckd": "만성콩팥병", "recent_aki": "최근 급성신손상", "contrast_reaction": "과거 조영제 반응",
    "implant": "체내 삽입 기기", "metal_fragment": "금속 이물 가능성",
    "claustrophobia": "폐쇄공포증", "fall_history": "최근 1년 낙상", "mobility_aid": "보행보조기구",
    "cardiac_disease": "심혈관질환", "exertional_symptoms": "운동 시 흉통·실신",
    "severe_lung_disease": "중증 호흡기질환", "fever_today": "오늘 발열·감염 증상",
    "cognitive_issue": "인지장애·의사소통 곤란", "osa": "수면무호흡 진단",
    "snoring": "코골이", "tired": "주간 피로", "observed_apnea": "수면 중 무호흡 목격",
    "hypertension": "고혈압", "neck_large": "목둘레 >40cm", "stopbang": "STOP-Bang 점수",
    "fasting_hours": "금식 시간", "diuretic": "이뇨제 복용", "fasting_exam": "금식 검사",
    "sedation": "진정(수면) 검사",
}
UNITS = {"bmi": "kg/m²", "sbp": "mmHg", "dbp": "mmHg", "hr": "/분", "temp_c": "°C",
         "spo2": "%", "glucose_poc": "mg/dL", "egfr": "mL/min/1.73m²", "hb": "g/dL",
         "platelet": "×10³/µL", "age": "세", "fasting_hours": "시간", "stopbang": "점"}
VALUE_TEXT = {"unknown": "모름", "yes": "예", "no": "아니오", "mild": "경증",
              "moderate_severe": "중등도 이상", "M": "남", "F": "여"}


def label(f: str) -> str:
    return LABELS.get(f, f)


def _describe(f: str, val: Any) -> str:
    if val is True:
        return label(f)
    if isinstance(val, str):
        return f"{label(f)}: {VALUE_TEXT.get(val, val)}"
    return f"{label(f)} {val}{UNITS.get(f, '')}"
