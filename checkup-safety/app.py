"""검진 안전 알리미 — 건강검진 현장 실시간 위험 알림 (교육용 프로토타입)

실행: streamlit run app.py
여러 브라우저 탭(=검사실 단말)을 열면 같은 서버 메모리를 공유하므로,
신체계측실에서 혈압을 입력하면 채혈실·내시경실 화면에 경고가 몇 초 안에 나타난다.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
import yaml

from engine import (EXAMS, STATIONS, Alert, assess, derive, label, max_level,
                    rulebook, VALUE_TEXT, UNITS)
from synthea_loader import SyntheaDB

KST = timezone(timedelta(hours=9))
st.set_page_config(page_title="검진 안전 알리미", layout="wide")

LEVEL_KO = {"red": "고위험", "yellow": "주의", "green": "특이사항 없음"}
LEVEL_COLOR = {"red": "#c62828", "yellow": "#b26a00", "green": "#2e7d32"}
LEVEL_BG = {"red": "#fdecea", "yellow": "#fff4e0", "green": "#e8f5e9"}
NEW_SECONDS = 90  # 이 시간 안에 새로 생긴 경고는 'NEW' 표시

st.markdown("""
<style>
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;font-size:0.85rem;color:#fff}
.new{display:inline-block;padding:1px 7px;border-radius:8px;font-size:0.72rem;font-weight:700;background:#1565c0;color:#fff;margin-left:6px}
.alert-card{border-left:6px solid;padding:10px 14px;border-radius:6px;margin:6px 0}
.alert-card h4{margin:0 0 4px 0;font-size:1.0rem}
.small{font-size:0.82rem;color:#555}
</style>""", unsafe_allow_html=True)


# ═════════════════════════════════════════════ 공유 저장소 (서버 메모리)
@st.cache_resource
def store():
    """모든 브라우저 세션이 공유하는 '오늘의 검진 대기열'. DB 없이 메모리에만 존재하며 서버를 끄면 사라진다."""
    return {"patients": OrderedDict(), "lock": threading.Lock(), "seq": [0]}


@st.cache_resource
def synthea():
    try:
        db = SyntheaDB()
        return db, db.roster()
    except FileNotFoundError:
        return None, None


@st.cache_data
def cohort_fields():
    db, roster = synthea()
    return [db.load(pid)["fields"] for pid in roster.id]


@st.cache_data
def demo_cases():
    with open("demo_cases.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["cases"]


def now():
    return datetime.now(KST)


def new_patient(name: str, exams: list[str], fields: dict, sources: dict, history: dict | None = None,
                origin: str = "현장") -> str:
    S = store()
    with S["lock"]:
        S["seq"][0] += 1
        pid = f"A-{S['seq'][0]:03d}"
        rec = {"id": pid, "name": name, "exams": exams, "fields": {}, "sources": {},
               "history": history or {}, "log": [], "first_seen": {}, "acked": {},
               "created": now(), "origin": origin}
        S["patients"][pid] = rec
    update(pid, fields, "접수·문진", sources)
    return pid


def update(pid: str, changes: dict, station: str, sources: dict | None = None):
    """측정값·문진을 반영하고, 새로 생긴 경고를 기록한다."""
    S = store()
    with S["lock"]:
        rec = S["patients"][pid]
        before = {a.id for a in assess(rec["fields"], rec["exams"])}
        for k, v in changes.items():
            if v is None or v == "":
                if k in rec["fields"]:
                    rec["fields"].pop(k)
                continue
            if rec["fields"].get(k) != v:
                rec["fields"][k] = v
                rec["sources"][k] = (sources or {}).get(k) or f"현장 입력 · {station} {now():%H:%M}"
        alerts = assess(rec["fields"], rec["exams"])
        t = now()
        for a in alerts:
            if a.id not in before:
                rec["first_seen"][a.id] = t
                rec["log"].append((t, station, f"[{LEVEL_KO[a.level]}] {a.title}"))
        for gone in before - {a.id for a in alerts}:
            rec["log"].append((t, station, f"경고 해제: {gone}"))
            rec["first_seen"].pop(gone, None)


def acknowledge(pid: str, aid: str, station: str):
    S = store()
    with S["lock"]:
        rec = S["patients"][pid]
        rec["acked"][aid] = (station, now())
        rec["log"].append((now(), station, f"조치 확인: {aid}"))


# ═════════════════════════════════════════════ 표시 도우미
def badge(level: str) -> str:
    return f'<span class="badge" style="background:{LEVEL_COLOR[level]}">{LEVEL_KO[level]}</span>'


def is_new(rec, aid) -> bool:
    t = rec["first_seen"].get(aid)
    return t is not None and (now() - t).total_seconds() < NEW_SECONDS


def show_alert(rec: dict, a: Alert, station: str | None, key: str, compact: bool = False):
    book = rulebook()
    new = '<span class="new">NEW</span>' if is_new(rec, a.id) else ""
    acked = rec["acked"].get(a.id)
    ack_txt = f' · <span class="small">✔ 조치 확인({acked[0]} {acked[1]:%H:%M})</span>' if acked else ""
    trig = " / ".join(a.triggered_by)
    html = (f'<div class="alert-card" style="border-color:{LEVEL_COLOR[a.level]};background:{LEVEL_BG[a.level]}">'
            f'<h4>{badge(a.level)} &nbsp;{a.title}{new}{ack_txt}</h4>'
            f'<div class="small">근거 데이터: {trig}</div>')
    if not compact:
        html += f'<div style="margin-top:6px">{a.why}</div><ul style="margin:6px 0 0 0">'
        html += "".join(f"<li>{x}</li>" for x in a.actions) + "</ul>"
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)
    if not compact:
        c1, c2 = st.columns([1, 4])
        with c2.expander("참고문헌 · 규칙 정보"):
            rule = next(r for r in book["rules"] if r["id"] == a.id)
            st.caption(f"규칙 {a.id} · 근거 수준: {rule['basis']}")
            for e in a.evidence:
                ref = book["references"][e]
                st.markdown(f"- {ref['text']}" + (f" [링크]({ref['url']})" if ref.get("url") else ""))
        if station and not acked:
            if c1.button("조치 확인", key=f"ack-{key}"):
                acknowledge(rec["id"], a.id, station)
                st.rerun()


def fv(d, k):
    return None if d.get(k) is None else float(d[k])


def iv(d, k):
    return None if d.get(k) is None else int(round(d[k]))


def fmt_val(k, v):
    if v is True:
        return "예"
    if isinstance(v, str):
        return VALUE_TEXT.get(v, v)
    return f"{v} {UNITS.get(k, '')}".strip()


def patient_header(rec):
    f = rec["fields"]
    sex = {"M": "남", "F": "여"}.get(f.get("sex"), "?")
    exams = ", ".join(EXAMS[e][0] for e in rec["exams"])
    return f"**{rec['id']}** · {rec['name']} · {f.get('age', '?')}세 {sex} · 예정: {exams}"


def pick_patient(key: str, filter_fn=None):
    pats = [r for r in store()["patients"].values() if not filter_fn or filter_fn(r)]
    if not pats:
        st.info("해당하는 수검자가 없습니다. 먼저 [접수·문진]에서 등록하세요.")
        return None
    opts = {f"{r['id']} · {r['name']} ({r['fields'].get('age', '?')}세)": r["id"] for r in pats}
    choice = st.selectbox("수검자 선택", list(opts), key=key, index=len(opts) - 1)
    return store()["patients"][opts[choice]]


# ═════════════════════════════════════════════ 사이드바
st.sidebar.title("검진 안전 알리미")
st.sidebar.caption("건강검진 현장 실시간 위험 알림 · 교육용 프로토타입")
page = st.sidebar.radio("화면", ["실시간 대시보드", "① 접수·문진", "② 신체계측·활력징후",
                               "③ 검사실 화면", "규칙과 근거", "합성 코호트 검증"])
st.sidebar.divider()
if st.sidebar.button("시연 시나리오 8건 한꺼번에 등록"):
    for c in demo_cases():
        new_patient(f"[시나리오] {c['name']}", c["exams"], c["fields"],
                    {k: "시연 시나리오" for k in c["fields"]}, origin="시나리오")
    st.sidebar.success("등록했습니다.")
if st.sidebar.button("대기열 비우기"):
    with store()["lock"]:
        store()["patients"].clear()
        store()["seq"][0] = 0
st.sidebar.warning("교육용 프로토타입입니다. 실제 진단·치료 결정에 사용하지 마세요. "
                   "모든 데이터는 가상(합성) 데이터이며 서버 메모리에만 저장됩니다.")


# ═════════════════════════════════════════════ 1. 대시보드
def dashboard():
    st.header("실시간 대시보드")
    auto = st.toggle("3초마다 자동 새로고침", value=True)

    @st.fragment(run_every="3s" if auto else None)
    def body():
        pats = list(store()["patients"].values())
        if not pats:
            st.info("대기 중인 수검자가 없습니다. 왼쪽 [시연 시나리오 등록] 또는 [① 접수·문진]에서 시작하세요.")
            return
        rows, all_alerts = [], {}
        for r in pats:
            al = assess(r["fields"], r["exams"])
            all_alerts[r["id"]] = al
            unacked = [a for a in al if a.id not in r["acked"]]
            rows.append((r, al, unacked))
        n_red = sum(1 for _, al, _ in rows if max_level(al) == "red")
        n_new = sum(1 for r, al, _ in rows for a in al if is_new(r, a.id))
        c = st.columns(4)
        c[0].metric("검진 중 수검자", len(pats))
        c[1].metric("고위험 수검자", n_red)
        c[2].metric("미확인 경고", sum(len(u) for _, _, u in rows))
        c[3].metric(f"최근 {NEW_SECONDS}초 신규 경고", n_new)
        st.caption(f"마지막 갱신 {now():%H:%M:%S}")

        order = {"red": 0, "yellow": 1, "green": 2}
        rows.sort(key=lambda x: (order[max_level(x[1])], x[0]["id"]))
        for r, al, unacked in rows:
            lvl = max_level(al)
            with st.container(border=True):
                st.markdown(f"{badge(lvl)} &nbsp; {patient_header(r)}", unsafe_allow_html=True)
                if not al:
                    st.caption("현재까지 입력된 정보로는 특이 경고가 없습니다.")
                by_station: dict[str, list[Alert]] = {}
                for a in al:
                    for s in a.stations:
                        by_station.setdefault(s, []).append(a)
                cols = st.columns(len(STATIONS))
                for col, s in zip(cols, STATIONS):
                    items = by_station.get(s, [])
                    txt = "".join(
                        f'<div style="color:{LEVEL_COLOR[a.level]};font-size:0.8rem">'
                        f'{"✔ " if a.id in r["acked"] else "● "}{a.title.split(" (")[0]}'
                        f'{"<span class=new>NEW</span>" if is_new(r, a.id) else ""}</div>' for a in items)
                    col.markdown(f'<div class="small"><b>{s}</b></div>{txt or "<span class=small>-</span>"}',
                                 unsafe_allow_html=True)
    body()


# ═════════════════════════════════════════════ 2. 접수·문진
YESNO = [  # (필드, 질문)
    ("prior_syncope_blood", "과거 채혈·주사 중 실신(기절)한 적이 있다"),
    ("needle_fear", "주사·피를 보면 매우 불안하다"),
    ("dehydration", "최근 설사·구토, 또는 어제부터 물을 거의 못 마셨다"),
    ("diabetes", "당뇨가 있다"),
    ("insulin", "인슐린 주사를 맞는다"),
    ("sulfonylurea", "설포닐우레아계 당뇨약(글리메피리드·글리피지드 등)을 먹는다"),
    ("hypoglycemia_history", "저혈당(식은땀·떨림)을 겪은 적이 있다"),
    ("anticoagulant", "항응고제(와파린·아픽사반·리바록사반 등)를 먹는다"),
    ("antiplatelet", "항혈소판제(아스피린·클로피도그렐 등)를 먹는다"),
    ("bleeding_disorder", "피가 잘 멈추지 않는 질환이 있다"),
    ("ckd", "콩팥(신장) 질환이 있다"),
    ("recent_aki", "최근 3개월 내 급성 콩팥 손상 진단을 받았다"),
    ("hypertension", "고혈압 진단을 받았다"),
    ("cardiac_disease", "심장질환(협심증·심근경색·심부전·부정맥)이 있다"),
    ("exertional_symptoms", "운동할 때 가슴 통증이나 실신이 있었다"),
    ("severe_lung_disease", "중증 폐질환(COPD 등)이 있다"),
    ("osa", "수면무호흡증 진단을 받았다"),
    ("snoring", "코골이가 심하다(닫힌 문 너머로 들릴 정도)"),
    ("tired", "낮에 자주 피곤하거나 졸린다"),
    ("observed_apnea", "자는 동안 숨이 멈춘다는 말을 들었다"),
    ("neck_large", "목둘레가 40cm보다 크다"),
    ("implant", "몸 안에 심박조율기·제세동기·인공와우·약물펌프 등이 있다"),
    ("metal_fragment", "금속 파편(용접·연마 작업, 눈 외상 등)이 몸에 있을 수 있다"),
    ("claustrophobia", "좁은 공간에 있으면 공황이 온다"),
    ("fall_history", "최근 1년 안에 넘어진 적이 있다"),
    ("mobility_aid", "지팡이·보행기·휠체어를 사용한다"),
    ("fever_today", "오늘 열·기침·몸살 등 감염 증상이 있다"),
    ("cognitive_issue", "(직원 판단) 인지장애·의사소통 곤란으로 지시 이해가 어렵다"),
]


def questionnaire(prefill: dict, prefill_src: dict, key: str) -> dict:
    out: dict = {}
    c1, c2 = st.columns(2)
    out["age"] = c1.number_input("나이", 0, 120, value=prefill.get("age", 40), key=f"{key}-age")
    sex_opts = ["M", "F"]
    out["sex"] = c2.radio("성별", sex_opts, index=sex_opts.index(prefill.get("sex", "M")),
                          format_func=lambda s: {"M": "남", "F": "여"}[s], horizontal=True, key=f"{key}-sex")
    if out["sex"] == "F":
        po = ["unknown", "no", "yes"]
        out["pregnancy"] = st.radio("임신 가능성", po, index=po.index(prefill.get("pregnancy", "unknown")),
                                    format_func=VALUE_TEXT.get, horizontal=True, key=f"{key}-preg")
    co = ["none", "mild", "moderate_severe"]
    out["contrast_reaction"] = st.radio(
        "과거 조영제 이상반응", co, index=co.index(prefill.get("contrast_reaction", "none")),
        format_func={"none": "없음/모름", "mild": "경증(두드러기 등)", "moderate_severe": "중등도 이상(호흡곤란·혈압저하 등)"}.get,
        horizontal=True, key=f"{key}-cr")
    if out["contrast_reaction"] == "none":
        out["contrast_reaction"] = None
    out["fasting_hours"] = st.number_input("마지막 식사 후 경과 시간(시간)", 0.0, 48.0,
                                           value=fv(prefill,"fasting_hours"), step=0.5, key=f"{key}-fast")
    st.markdown("**문진 (해당 항목 체크)** — 회색 설명이 붙은 항목은 EMR(과거 기록)에서 자동 입력됨")
    cols = st.columns(2)
    for i, (k, q) in enumerate(YESNO):
        help_ = prefill_src.get(k) if prefill.get(k) else None
        out[k] = cols[i % 2].checkbox(q + (" ·EMR" if help_ else ""), value=bool(prefill.get(k)),
                                      help=help_, key=f"{key}-{k}") or None
    return out


def page_intake():
    st.header("① 접수·문진")
    tab_new, tab_edit = st.tabs(["새 수검자 등록", "등록된 수검자 문진 수정"])
    with tab_new:
        src = st.radio("과거 기록", ["EMR 없음 (현장 문진만)", "Synthea 합성 EMR 불러오기"], horizontal=True)
        prefill, prefill_src, history, name = {}, {}, {}, ""
        if src.startswith("Synthea"):
            db, roster = synthea()
            if db is None:
                st.error("data/synthea 폴더가 없습니다. README의 Synthea 준비 방법을 참고하세요.")
                return
            only_risk = st.checkbox("병력이 있는 환자만 보기", value=True)
            r = roster[roster.risk_tags.str.len() > 0] if only_risk else roster
            labels = {f"{x.label} · {x.age}세 {x.sex} · {', '.join(label(t) for t in x.risk_tags) or '특이병력 없음'}": x.id
                      for x in r.itertuples()}
            sel = st.selectbox("합성 환자 선택", list(labels))
            emr = db.load(labels[sel])
            prefill, prefill_src, history, name = emr["fields"], emr["evidence"], emr["history"], emr["name"]
            with st.expander("EMR에서 가져온 정보", expanded=True):
                st.dataframe(pd.DataFrame(
                    [{"항목": label(k), "값": fmt_val(k, v), "출처": prefill_src.get(k, "")} for k, v in prefill.items()]
                    + [{"항목": f"(참고) 과거 {label(k)}", "값": fmt_val(k, v[0]), "출처": f"EMR {v[1]} — 오늘 값은 현장에서 다시 측정"}
                       for k, v in history.items()]), hide_index=True, width="stretch")
        exams = st.multiselect("오늘 예정 검사", list(EXAMS), default=["blood_draw", "xray"],
                               format_func=lambda e: EXAMS[e][0])
        with st.form("intake-new"):
            name = st.text_input("표시 이름(실명 대신 별칭)", value=name or "수검자")
            ans = questionnaire(prefill, prefill_src, "new")
            if st.form_submit_button("등록", type="primary"):
                srcs = {k: prefill_src[k] for k in ans if k in prefill_src and ans.get(k) == prefill.get(k)}
                pid = new_patient(name, exams, ans, srcs, history,
                                  origin="Synthea" if src.startswith("Synthea") else "현장")
                st.success(f"{pid} 등록 완료")
                for a in assess(store()["patients"][pid]["fields"], exams):
                    show_alert(store()["patients"][pid], a, None, f"new-{a.id}", compact=True)
    with tab_edit:
        rec = pick_patient("edit-pick")
        if rec:
            exams = st.multiselect("예정 검사", list(EXAMS), default=rec["exams"],
                                   format_func=lambda e: EXAMS[e][0], key=f"ex-{rec['id']}")
            with st.form(f"edit-{rec['id']}"):
                ans = questionnaire(rec["fields"], rec["sources"], f"e{rec['id']}")
                if st.form_submit_button("저장", type="primary"):
                    rec["exams"] = exams
                    # 체크 해제된 문진 항목은 삭제
                    update(rec["id"], ans, "접수·문진")
                    st.success("저장했습니다.")


# ═════════════════════════════════════════════ 3. 신체계측
def page_vitals():
    st.header("② 신체계측·활력징후")
    st.caption("측정값을 저장하는 즉시 BMI·STOP-Bang이 계산되고, 이후 검사실에 경고가 전달됩니다.")
    rec = pick_patient("vit-pick")
    if not rec:
        return
    st.markdown(patient_header(rec))
    f = rec["fields"]
    if rec["history"]:
        st.caption("EMR 과거값: " + ", ".join(f"{label(k)} {v[0]} ({v[1]})" for k, v in rec["history"].items()))
    with st.form(f"vit-{rec['id']}"):
        c = st.columns(4)
        vals = {
            "height_cm": c[0].number_input("키 (cm)", 100.0, 220.0, value=fv(f,"height_cm"), step=0.1),
            "weight_kg": c[1].number_input("몸무게 (kg)", 25.0, 250.0, value=fv(f,"weight_kg"), step=0.1),
            "sbp": c[2].number_input("수축기혈압 (mmHg)", 50, 260, value=iv(f,"sbp")),
            "dbp": c[3].number_input("이완기혈압 (mmHg)", 30, 160, value=iv(f,"dbp")),
            "hr": c[0].number_input("맥박 (/분)", 30, 200, value=iv(f,"hr")),
            "temp_c": c[1].number_input("체온 (°C)", 34.0, 42.0, value=fv(f,"temp_c"), step=0.1),
            "spo2": c[2].number_input("SpO₂ (%)", 70, 100, value=iv(f,"spo2")),
            "glucose_poc": c[3].number_input("현장 혈당 (mg/dL)", 20, 600, value=iv(f,"glucose_poc")),
        }
        if st.form_submit_button("저장", type="primary"):
            update(rec["id"], vals, "신체계측")
            st.rerun()
    p = derive(rec["fields"], rec["exams"])
    m = st.columns(3)
    m[0].metric("BMI", p.get("bmi", "-"))
    m[1].metric("STOP-Bang", f"{p['stopbang']}점" if "stopbang" in p else "-",
                help=f"응답 {p.get('stopbang_items', 0)}/8 문항 기준")
    m[2].metric("현재 최고 경고", LEVEL_KO[max_level(assess(rec['fields'], rec['exams']))])
    st.subheader("현재 경고")
    for a in assess(rec["fields"], rec["exams"]):
        show_alert(rec, a, None, f"v-{rec['id']}-{a.id}", compact=True)


# ═════════════════════════════════════════════ 4. 검사실
def page_station():
    st.header("③ 검사실 화면")
    station = st.radio("검사실", STATIONS[2:], horizontal=True)
    auto = st.toggle("3초마다 자동 새로고침", value=False,
                     help="켜 두면 다른 단말에서 입력한 값이 바로 반영됩니다(조치 확인 버튼 누를 때는 꺼 두세요).")

    @st.fragment(run_every="3s" if auto else None)
    def body():
        pats = [r for r in store()["patients"].values()
                if any(EXAMS[e][1] == station for e in r["exams"])]
        if not pats:
            st.info(f"{station} 검사 예정 수검자가 없습니다.")
            return
        for r in pats:
            al = [a for a in assess(r["fields"], r["exams"]) if station in a.stations]
            lvl = max_level(al)
            with st.expander(f"{r['id']} · {r['name']} — {LEVEL_KO[lvl]} ({len(al)}건)",
                             expanded=lvl != "green"):
                st.markdown(patient_header(r))
                st.caption("입력된 정보: " + ", ".join(f"{label(k)}={fmt_val(k, v)}" for k, v in r["fields"].items()))
                if not al:
                    st.success("이 검사실 관련 경고 없음 — 표준 절차 진행")
                for a in al:
                    show_alert(r, a, station, f"{station}-{r['id']}-{a.id}")
                with st.popover("기록 보기"):
                    for t, s, msg in r["log"][::-1]:
                        st.caption(f"{t:%H:%M:%S} · {s} · {msg}")
    body()


# ═════════════════════════════════════════════ 5. 규칙
def page_rules():
    st.header("규칙과 근거")
    st.caption("모든 경고는 아래 규칙에서만 나옵니다. 기준값을 바꾸려면 rules.yaml을 수정하세요(코드 수정 불필요).")
    book = rulebook()
    basis_ko = {"guideline": "지침 기준", "adapted": "지침 응용", "team": "팀 설정(검토 필요)"}
    df = pd.DataFrame([{"ID": r["id"], "모듈": r["module"], "수준": LEVEL_KO[r["level"]], "규칙": r["title"],
                        "근거 수준": basis_ko[r["basis"]], "검사실": ", ".join(r["stations"]),
                        "참고문헌": ", ".join(r["evidence"])} for r in book["rules"]])
    st.dataframe(df, hide_index=True, width="stretch")
    st.subheader("참고문헌")
    for k, v in book["references"].items():
        st.markdown(f"- **{k}** — {v['text']}" + (f" [링크]({v['url']})" if v.get("url") else ""))


# ═════════════════════════════════════════════ 6. 코호트 검증
def page_validate():
    st.header("합성 코호트 검증 (Synthea)")
    st.caption("합성 성인 환자 전원에게 같은 검사 묶음을 배정했다고 가정하고, EMR 정보만으로 어떤 경고가 몇 명에게 뜨는지 봅니다. "
               "현장 측정값·문진이 없는 상태이므로 '사전 선별' 단계의 결과입니다.")
    db, roster = synthea()
    if db is None:
        st.error("Synthea 데이터가 없습니다.")
        return
    exams = st.multiselect("가정할 검사", list(EXAMS), default=["blood_draw", "ct_contrast", "sedation_colon"],
                           format_func=lambda e: EXAMS[e][0])
    rows = []
    for pid, fields in zip(roster.id, cohort_fields()):
        al = assess(fields, exams)
        rows.append({"id": pid, "level": max_level(al), "alerts": [a.id for a in al]})
    res = pd.DataFrame(rows)
    c = st.columns(3)
    for col, lv in zip(c, ["red", "yellow", "green"]):
        n = (res.level == lv).sum()
        col.metric(f"{LEVEL_KO[lv]} (전체의 {n / len(res):.0%})", f"{n}명")
    titles = {r["id"]: r["title"] for r in rulebook()["rules"]}
    cnt = res.explode("alerts").dropna().alerts.value_counts()
    st.bar_chart(pd.DataFrame({"규칙": [f"{i} {titles[i][:22]}" for i in cnt.index], "환자 수": cnt.values}),
                 x="규칙", y="환자 수", horizontal=True)
    st.caption(f"합성 환자 {len(res)}명 · 생성: Synthea (seed 42, Massachusetts 모듈)")


{"실시간 대시보드": dashboard, "① 접수·문진": page_intake, "② 신체계측·활력징후": page_vitals,
 "③ 검사실 화면": page_station, "규칙과 근거": page_rules, "합성 코호트 검증": page_validate}[page]()
