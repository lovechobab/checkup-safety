"""Synthea 원본 CSV를 앱에서 쓰는 작은 파일로 줄인다 (생존 성인 환자, 필요한 열·코드만).

사용법:
  java -jar synthea-with-dependencies.jar -p 120 -s 42 --exporter.csv.export true \
       --exporter.fhir.export false --exporter.baseDirectory ./out Massachusetts
  python prepare_synthea.py ./out/csv
"""
import sys
from pathlib import Path

import pandas as pd

from synthea_loader import OBS_MAP, HIST_OBS

src = Path(sys.argv[1])
dst = Path(__file__).parent / "data" / "synthea"
dst.mkdir(parents=True, exist_ok=True)

p = pd.read_csv(src / "patients.csv")
p = p[p.DEATHDATE.isna()]
p = p[pd.to_datetime(p.BIRTHDATE) <= pd.Timestamp("2008-01-01")]  # 검진 대상: 성인
# 주소·SSN 등 식별자 형태의 열은 합성 데이터라도 제외
p[["Id", "BIRTHDATE", "FIRST", "LAST", "GENDER"]].to_csv(dst / "patients.csv", index=False)
ids = set(p.Id)

for name, cols in {
    "conditions": ["START", "STOP", "PATIENT", "CODE", "DESCRIPTION"],
    "medications": ["START", "STOP", "PATIENT", "CODE", "DESCRIPTION"],
    "devices": ["START", "STOP", "PATIENT", "CODE", "DESCRIPTION"],
}.items():
    d = pd.read_csv(src / f"{name}.csv")
    d[d.PATIENT.isin(ids)][cols].to_csv(dst / f"{name}.csv", index=False)

o = pd.read_csv(src / "observations.csv")
o = o[o.PATIENT.isin(ids) & o.CODE.isin(list(OBS_MAP) + list(HIST_OBS))]
# 환자·코드별 최근 3건만 보관
o = o.sort_values("DATE").groupby(["PATIENT", "CODE"]).tail(3)
o[["DATE", "PATIENT", "CODE", "DESCRIPTION", "VALUE", "UNITS"]].to_csv(dst / "observations.csv", index=False)

print(f"환자 {len(ids)}명 → {dst}")
