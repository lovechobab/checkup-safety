"""규칙 엔진 회귀 테스트: pytest -q"""
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import EXAMS, assess, derive, rulebook  # noqa: E402

CASES = yaml.safe_load(open(ROOT / "demo_cases.yaml", encoding="utf-8"))["cases"]


def ids(fields, exams):
    return {a.id for a in assess(fields, exams)}


# ---------- 1. 규칙 파일 자체 점검 ----------
def test_rulebook_integrity():
    book = rulebook()
    rule_ids = [r["id"] for r in book["rules"]]
    assert len(rule_ids) == len(set(rule_ids)), "규칙 ID 중복"
    for r in book["rules"]:
        assert r["level"] in ("red", "yellow")
        assert r["actions"], f"{r['id']} 조치 없음"
        assert r.get("evidence"), f"{r['id']} 근거 없음"
        for e in r["evidence"]:
            assert e in book["references"], f"{r['id']}: 참고문헌 {e} 없음"
        for s in r.get("suppressed_by", []):
            assert s in rule_ids


# ---------- 2. 시나리오 테스트 ----------
@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_demo_cases(case):
    fired = ids(case["fields"], case["exams"])
    for e in case.get("expect", []):
        assert e in fired, f"{e} 가 떠야 함. 실제: {sorted(fired)}"
    for a in case.get("absent", []):
        assert a not in fired, f"{a} 가 뜨면 안 됨. 실제: {sorted(fired)}"


# ---------- 3. 경계값 테스트 ----------
@pytest.mark.parametrize("glucose,expected", [(69, True), (70, False), (54, True)])
def test_hypoglycemia_threshold(glucose, expected):
    assert ("B2" in ids({"glucose_poc": glucose}, [])) == expected


@pytest.mark.parametrize("egfr,expected", [(29.9, True), (30, False), (45, False)])
def test_egfr_threshold(egfr, expected):
    assert ("C1" in ids({"egfr": egfr, "age": 50}, ["ct_contrast"])) == expected


def test_bmi_calculation():
    p = derive({"height_cm": 160, "weight_kg": 46}, [])
    assert p["bmi"] == 18.0


def test_bmi_boundary_underweight():
    # BMI 18.5 정확히 = 저체중 아님
    f = {"height_cm": 200, "weight_kg": 74, "sbp": 95}
    assert derive(f, [])["bmi"] == 18.5
    assert "A3" not in ids(f, ["blood_draw"])


def test_stopbang_partial():
    p = derive({"snoring": True, "age": 60, "sex": "M"}, [])
    assert p["stopbang"] == 3 and p["stopbang_items"] == 3


# ---------- 4. 검사 의존성: 검사가 없으면 경고도 없어야 한다 ----------
def test_exam_required():
    f = {"implant": True, "egfr": 20, "prior_syncope_blood": True, "sbp": 85}
    fired = ids(f, [])
    assert not fired & {"D1", "C1", "A1"}


def test_suppression():
    f = {"prior_syncope_blood": True, "sbp": 85}
    fired = ids(f, ["blood_draw"])
    assert "A1" in fired and "A2" not in fired and "A4" not in fired


# ---------- 5. 결측 처리: 정보가 없을 때 '위험 없음'으로 단정하지 않는다 ----------
def test_missing_egfr_prompts_check():
    assert "C2" in ids({"age": 70}, ["ct_contrast"])


def test_missing_pregnancy_prompts_check():
    assert "E2" in ids({"age": 30, "sex": "F"}, ["xray"])


def test_all_exam_codes_known():
    for r in rulebook()["rules"]:
        def walk(c):
            if "exam" in c:
                for e in (c["exam"] if isinstance(c["exam"], list) else [c["exam"]]):
                    assert e in EXAMS, e
            for k in ("all", "any"):
                for s in c.get(k, []):
                    walk(s)
        walk(r["when"])
