"""Synthea 합성 EMR(CSV) → 검진 안전 엔진 입력값 변환.

Synthea(https://synthetichealth.github.io/synthea/)는 실제 환자가 아닌 가상 환자 기록을 만드는
오픈소스 시뮬레이터다. 실제 개인정보가 없으므로 수업 규정(공개·합성 데이터 우선)에 맞는다.

이 모듈은 '과거 기록'에서 확인할 수 있는 정보(병력·복용약·최근 검사값·보조기구)만 미리 채운다.
오늘의 측정값(혈압·체중·혈당)과 문진(실신 경험·임신 가능성 등)은 검진 현장에서 입력한다.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data" / "synthea"

# 진단명(SNOMED 설명) 키워드 → 엔진 필드
CONDITION_MAP = {
    "diabetes": ["diabetes mellitus type 2", "diabetes mellitus type 1", "due to type 2 diabetes"],
    "ckd": ["chronic kidney disease", "end-stage renal disease", "disorder of kidney due to diabetes"],
    "cardiac_disease": ["ischemic heart disease", "heart failure", "atrial fibrillation",
                        "myocardial infarction", "coronary artery bypass", "angina"],
    "severe_lung_disease": ["emphysema", "chronic obstructive", "chronic bronchitis"],
    "osa": ["sleep apnea"],
    "hypertension": ["hypertension"],
    "cognitive_issue": ["alzheimer", "dementia"],
    "pregnancy_active": ["normal pregnancy"],
}
# 약 성분 키워드 → 엔진 필드
MED_MAP = {
    "insulin": ["insulin"],
    "sulfonylurea": ["glipizide", "glimepiride", "glyburide", "gliclazide"],
    "anticoagulant": ["warfarin", "apixaban", "rivaroxaban", "dabigatran", "edoxaban", "enoxaparin"],
    "antiplatelet": ["clopidogrel", "aspirin", "ticagrelor", "prasugrel"],
    "diuretic": ["furosemide", "hydrochlorothiazide", "chlorthalidone", "spironolactone"],
}
DEVICE_MAP = {
    "mobility_aid": ["wheelchair", "walker", "crutch", "cane"],
    "implant": ["pacemaker", "defibrillator", "cochlear", "neurostimulator", "infusion pump, implantable"],
}
# LOINC 코드 → 엔진 필드 (가장 최근 값 사용)
OBS_MAP = {
    "33914-3": "egfr", "62238-1": "egfr",
    "718-7": "hb",
    "777-3": "platelet", "26515-7": "platelet",
}
# 참고용 과거 활력징후(오늘 측정값으로 쓰지 않음)
HIST_OBS = {"39156-5": "bmi", "8480-6": "sbp", "8462-4": "dbp", "29463-7": "weight_kg", "8302-2": "height_cm"}


class SyntheaDB:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.dir = Path(data_dir)
        self.patients = pd.read_csv(self.dir / "patients.csv")
        self.conditions = pd.read_csv(self.dir / "conditions.csv")
        self.medications = pd.read_csv(self.dir / "medications.csv")
        self.observations = pd.read_csv(self.dir / "observations.csv")
        self.devices = pd.read_csv(self.dir / "devices.csv")

    def roster(self) -> pd.DataFrame:
        """선택 목록용: ID, 표시명, 나이, 성별, 주요 병력."""
        rows = []
        for _, r in self.patients.iterrows():
            info = self.load(r.Id)
            flags = [k for k in ("diabetes", "insulin", "anticoagulant", "antiplatelet", "ckd",
                                 "cardiac_disease", "osa", "mobility_aid", "cognitive_issue")
                     if info["fields"].get(k)]
            rows.append({"id": r.Id, "label": f"{r.FIRST[:-3]} {r.LAST[:-3]}",
                         "age": info["fields"]["age"], "sex": r.GENDER, "risk_tags": flags})
        return pd.DataFrame(rows)

    def load(self, pid: str, today: date | None = None) -> dict:
        today = today or date.today()
        pr = self.patients[self.patients.Id == pid].iloc[0]
        b = date.fromisoformat(pr.BIRTHDATE)
        age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        f: dict = {"age": age, "sex": pr.GENDER}
        evidence: dict[str, str] = {"age": "EMR 생년월일", "sex": "EMR"}

        cond = self.conditions[(self.conditions.PATIENT == pid) & self.conditions.STOP.isna()]
        for key, kws in CONDITION_MAP.items():
            hit = cond[cond.DESCRIPTION.str.lower().str.contains("|".join(kws), regex=True)]
            if key == "diabetes":
                hit = hit[~hit.DESCRIPTION.str.lower().str.contains("prediabetes")]
            if len(hit):
                f[key] = True
                evidence[key] = "EMR 진단: " + hit.DESCRIPTION.iloc[0]

        meds = self.medications[(self.medications.PATIENT == pid) & self.medications.STOP.isna()]
        for key, kws in MED_MAP.items():
            hit = meds[meds.DESCRIPTION.str.lower().str.contains("|".join(kws), regex=True)]
            if len(hit):
                f[key] = True
                evidence[key] = "EMR 처방: " + hit.DESCRIPTION.iloc[0]

        dev = self.devices[(self.devices.PATIENT == pid)]
        dev = dev[dev.STOP.isna() | (dev.STOP == dev.START) | (pd.to_datetime(dev.STOP, utc=True) > pd.Timestamp.now(tz="UTC"))]
        for key, kws in DEVICE_MAP.items():
            hit = dev[dev.DESCRIPTION.str.lower().str.contains("|".join(kws), regex=True)]
            if len(hit):
                f[key] = True
                evidence[key] = "EMR 기기: " + hit.DESCRIPTION.iloc[0]

        obs = self.observations[self.observations.PATIENT == pid].sort_values("DATE")
        history: dict = {}
        for code, key in {**OBS_MAP, **HIST_OBS}.items():
            o = obs[obs.CODE == code]
            if len(o):
                last = o.iloc[-1]
                try:
                    val = round(float(last.VALUE), 1)
                except ValueError:
                    continue
                if key in OBS_MAP.values():
                    f[key] = val
                    evidence[key] = f"EMR 검사 {last.DATE[:10]}"
                else:
                    history[key] = (val, last.DATE[:10])

        if f.pop("pregnancy_active", False):
            f["pregnancy"] = "yes"
        return {"fields": f, "evidence": evidence, "history": history,
                "name": f"{pr.FIRST[:-3]} {pr.LAST[:-3]}"}
