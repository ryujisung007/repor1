"""
식품안전나라 품목제조보고 조회 시스템 v4
- 카테고리 전체 조회
- 주요원재료 컬럼
- 제조사 필터 (session_state 유지)
- 보고일자 기간 필터
- GPT-4o-mini AI 현황분석 (플레이버 / 컨셉)
- 전체 DB 스캔 → 보고일자 정렬 (최신 정확도 확보)
"""

import streamlit as st
import requests
import pandas as pd
import plotly.express as px
from datetime import datetime, date, timedelta
import time
import json

# ───────────────────────────────
# 스타일
# ───────────────────────────────
st.set_page_config(page_title="식품안전나라 품목조회", layout="wide")
st.markdown("""
<style>
[data-testid="stSidebar"] { background: #f8f9fb; }
div[data-testid="stMetric"] { background: #f0f2f5; border-radius: 10px; padding: 12px; }
</style>
""", unsafe_allow_html=True)

# ───────────────────────────────
# session_state 초기화
# ───────────────────────────────
for _k, _v in {
    "result_df":    None,
    "result_label": "",
    "result_total": 0,
    "result_mode":  "",
    "result_msg":   "",
    "status_msgs":  {},
    "stop_scan":    False,
    "is_scanning":  False,
    "_raw_fields":  [],
}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ───────────────────────────────
# 상수
# ───────────────────────────────
SERVICE_ID = "I1250"
_API_KEYS  = [
    "9171f7ffd72f4ffcb62f",   # 키 1
    "7270692908c74bccaebc",   # 키 2
]
DAILY_LIMIT = 2000            # 키당 일일 한도

class _KeyPool:
    """라운드로빈으로 두 키를 교대 사용 → 실질 일일 4,000회"""
    def __init__(self, keys):
        self.keys  = keys
        self._idx  = 0
    def next(self) -> str:
        k = self.keys[self._idx % len(self.keys)]
        self._idx += 1
        return k
    def base_url(self) -> str:
        return f"http://openapi.foodsafetykorea.go.kr/api/{self.next()}/{SERVICE_ID}/json"
    @property
    def total_limit(self) -> int:
        return DAILY_LIMIT * len(self.keys)

if "_key_pool" not in st.session_state:
    st.session_state["_key_pool"] = _KeyPool(_API_KEYS)

_pool = st.session_state["_key_pool"]

# 하위 호환 (caption 등에서 사용)
API_KEY  = _API_KEYS[0]

# ── Gemini API 키 (st.secrets에서 자동 로드) ──
def _get_gemini_key() -> str:
    try:
        return st.secrets.get("GOOGLE_API_KEY", "") or st.secrets.get("google_api_key", "")
    except Exception:
        return ""

FOOD_TYPES = {
    "음료류": [
        "혼합음료", "탄산음료", "탄산수",
        "과.채주스", "과.채음료", "음료베이스",
        "침출차", "추출차", "액상차",
        "두유류", "유산균음료", "커피", "인삼.홍삼음료",
    ],
    "과자류":      ["과자", "캔디류", "추잉껌", "빙과", "아이스크림류"],
    "빵·면류":     ["빵류", "떡류", "면류", "즉석섭취식품"],
    "조미·소스류": ["소스", "복합조미식품", "향신료가공품", "식초", "드레싱"],
    "유가공품":    ["치즈", "버터", "발효유", "우유류", "가공유"],
    "건강기능식품": ["건강기능식품"],
    "기타":        ["잼류", "식용유지", "김치류", "두부류", "즉석조리식품", "레토르트식품"],
}


# ───────────────────────────────
# API 유틸
# ───────────────────────────────
def _safe_get(url: str):
    """GET → (data_dict | None, error_msg | None)"""
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        return None, "응답 시간 초과 (30초)"
    except requests.exceptions.ConnectionError:
        return None, "서버 연결 실패"
    except Exception as e:
        return None, f"HTTP 오류: {e}"

    raw = r.text.strip()
    if not raw:
        return None, f"빈 응답 (HTTP {r.status_code}) — API 키 또는 서버 점검 확인"
    try:
        return r.json(), None
    except Exception:
        return None, f"JSON 파싱 실패 — 응답: {raw[:200]}"


# stop_flag가 True이면 스캔 즉시 중단 (캐시 비대상)
def fetch_food_data(food_type: str, top_n: int = 100, max_pages: int = 100, stop_flag: list = None):
    """
    전체 DB를 max_pages 페이지 스캔 → PRMS_DT 정렬 → 최신 top_n 반환.
    DB 인덱스 ≠ 보고일자 순서이므로 전체 스캔 후 날짜 정렬이 필수.
    """
    # probe: total_count 확인 (키1 고정 — probe는 1회만)
    _probe_url = f"http://openapi.foodsafetykorea.go.kr/api/{_API_KEYS[0]}/{SERVICE_ID}/json/1/1"
    data, err = _safe_get(_probe_url)
    if err:
        return None, err, 0, 0
    if SERVICE_ID not in data:
        return None, f"API 오류: {data}", 0, 0

    total = int(data[SERVICE_ID].get("total_count", 0))
    if total == 0:
        return [], "DB 레코드 0건", 0, 0

    collected  = []
    cursor     = total
    pages_done = 0
    page_size  = 1000

    while cursor > 0 and pages_done < max_pages:
        # 중단 플래그 체크
        if stop_flag and stop_flag[0]:
            scanned  = pages_done * page_size
            coverage = round(scanned / total * 100, 1) if total else 0
            if collected:
                collected.sort(key=lambda r: r.get("PRMS_DT", "0") or "0", reverse=True)
                collected = collected[:top_n]
            return collected, f"⛔ 사용자 중단 — {pages_done}페이지({scanned:,}건) 스캔 / {coverage}%", total, pages_done

        p_s = max(1, cursor - page_size + 1)
        p_e = cursor
        d, err = _safe_get(f"{_pool.base_url()}/{p_s}/{p_e}")
        if err:
            return None, err, total, pages_done

        if SERVICE_ID not in d:
            return None, f"API 오류: {d}", total, pages_done

        res  = d[SERVICE_ID]
        code = res.get("RESULT", {}).get("CODE", "")
        msg  = res.get("RESULT", {}).get("MSG", "")
        if code == "INFO-200":
            break
        if code != "INFO-000":
            return None, f"[{code}] {msg}", total, pages_done

        for r in res.get("row", []):
            if r.get("PRDLST_DCNM", "").strip() == food_type.strip():
                collected.append(r)

        cursor     = p_s - 1
        pages_done += 1
        time.sleep(0.2)

    if collected:
        collected.sort(key=lambda r: r.get("PRMS_DT", "0") or "0", reverse=True)
        collected = collected[:top_n]

    scanned  = min(pages_done * page_size, total)
    coverage = round(scanned / total * 100, 1) if total else 0
    return (
        collected,
        f"정상 — {pages_done}페이지({scanned:,}건 스캔) / DB 커버리지 {coverage}%",
        total,
        pages_done,
    )


def fetch_multiple(types_list: list, per_type: int, max_pages: int):
    all_rows    = []
    status_msgs = {}
    prog        = st.progress(0, text="조회 중...")

    for i, ft in enumerate(types_list):
        prog.progress((i + 1) / len(types_list), text=f"📡 {ft} 조회 중…")
        rows, msg, total, _ = fetch_food_data(ft, top_n=per_type, max_pages=max_pages)
        status_msgs[ft] = {
            "msg":     msg or "",
            "total":   total,
            "fetched": len(rows) if rows else 0,
        }
        if rows:
            all_rows.extend(rows)
        time.sleep(0.2)

    prog.empty()
    return all_rows, status_msgs


# ───────────────────────────────
# 데이터 변환
# ───────────────────────────────
COL_MAP = {
    "PRDLST_NM":                "제품명",
    "PRDLST_DCNM":              "품목유형",
    "BSSH_NM":                  "제조사",
    "PRMS_DT":                  "보고일자",
    # 원재료명: API 버전에 따라 필드명이 다를 수 있어 모두 매핑
    "RAWMTRL_NM":               "주요원재료",
    "RAW_MTRL_NM":              "주요원재료",
    "INGR_NM":                  "주요원재료",
    "PRPOS":                    "용도",
    "POG_DAYCNT":               "유통기한",
    "PRODUCTION":               "생산종료",
    "INDUTY_CD_NM":             "업종",
    "LCNS_NO":                  "인허가번호",
    "PRDLST_REPORT_NO":         "품목제조번호",
    "HIENG_LNTRT_DVS_NM":       "고열량저영양",
    "CHILD_CRTFC_YN":           "어린이기호식품인증",
    "LAST_UPDT_DTM":            "최종수정일",
    "DISPOS":                   "제품형태",
    "FRMLC_MTRQLT":             "포장재질",
    "QLITY_MNTNC_TMLMT_DAYCNT": "품질유지기한일수",
    "ETQTY_XPORT_PRDLST_YN":    "내수겸용",
}

def to_df(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    # 원시 필드명을 session_state에 저장 (디버그용)
    st.session_state["_raw_fields"] = sorted(df.columns.tolist())

    # COL_MAP 적용 (중복 매핑 시 먼저 발견된 것 우선)
    rename = {}
    mapped_targets = set()
    for k, v in COL_MAP.items():
        if k in df.columns and v not in mapped_targets:
            rename[k] = v
            mapped_targets.add(v)
    df = df.rename(columns=rename)

    # 원재료 컬럼이 없으면 빈 컬럼 추가 (테이블 표시 일관성)
    if "주요원재료" not in df.columns:
        df["주요원재료"] = ""

    if "보고일자" in df.columns:
        df["보고일자"]    = df["보고일자"].astype(str)
        df["보고일자_dt"] = pd.to_datetime(df["보고일자"], format="%Y%m%d", errors="coerce")
        df = df.sort_values("보고일자_dt", ascending=False).reset_index(drop=True)
    return df

def date_filter(df: pd.DataFrame, d_from, d_to) -> pd.DataFrame:
    if df.empty or "보고일자_dt" not in df.columns:
        return df
    m = pd.Series(True, index=df.index)
    if d_from:
        m &= df["보고일자_dt"] >= pd.Timestamp(d_from)
    if d_to:
        m &= df["보고일자_dt"] <= pd.Timestamp(d_to)
    return df[m].reset_index(drop=True)


# ───────────────────────────────
# 제품 목록 테이블 (session_state 유지)
# ───────────────────────────────
def product_table(df: pd.DataFrame, show_type: bool, pfx: str):
    ncols = 3 if show_type else 2
    cols  = st.columns(ncols)

    with cols[0]:
        search = st.text_input("🔎 제품명/원재료 검색", key=f"{pfx}_srch")
    with cols[1]:
        maker_opts = ["전체"] + (sorted(df["제조사"].dropna().unique()) if "제조사" in df.columns else [])
        sel_maker  = st.selectbox("제조사 필터", maker_opts, key=f"{pfx}_maker")

    sel_type = "전체"
    if show_type and "품목유형" in df.columns:
        with cols[2]:
            type_opts = ["전체"] + sorted(df["품목유형"].dropna().unique())
            sel_type  = st.selectbox("품목유형 필터", type_opts, key=f"{pfx}_type")

    filt = df.copy()
    if search:
        filt = filt[filt.apply(lambda r: search.lower() in str(r).lower(), axis=1)]
    if sel_maker != "전체" and "제조사" in filt.columns:
        filt = filt[filt["제조사"] == sel_maker]
    if sel_type != "전체" and "품목유형" in filt.columns:
        filt = filt[filt["품목유형"] == sel_type]

    show = ["제품명", "품목유형", "제조사", "보고일자", "주요원재료", "유통기한", "생산종료"]
    show = [c for c in show if c in filt.columns]
    st.dataframe(filt[show].reset_index(drop=True), use_container_width=True, height=500)
    st.caption(f"총 {len(filt)}건 표시")


# ───────────────────────────────
# AI 현황분석 탭
# ───────────────────────────────
def ai_tab(df: pd.DataFrame, label: str, oai_key: str = ""):
    st.markdown(f"### 🤖 AI 현황분석 — {label}")

    gemini_key = _get_gemini_key()
    if not gemini_key:
        st.warning("⚠️ st.secrets에 GOOGLE_API_KEY가 설정되지 않았습니다.")
        return

    st.caption(f"분석 대상: 최대 150건 / Gemini 2.0 Flash (secrets 키 사용)")
    if not st.button("🔍 AI 분석 실행", key=f"ai_{label[:8]}", type="primary"):
        return

    sample   = df.head(150).copy()
    has_ingr = "주요원재료" in sample.columns
    lines = [
        f"{row['제품명']} / {row['주요원재료'] if has_ingr else ''}"
        for _, row in sample.fillna("").iterrows()
    ]

    prompt = f"""식품 R&D 전문가입니다. 아래 제품 목록을 분석해 JSON만 반환하세요.
JSON 외 텍스트·마크다운 코드블록 절대 금지.
형식: {{"flavors":{{"딸기":12,"복숭아":8}},"concepts":{{"제로슈거":15,"탄산":6}}}}
flavors: 주요 플레이버/과일/향 (딸기,복숭아,사과,레몬,오렌지,포도,망고,파인애플,메론,자몽,블루베리,라임,녹차,홍차,커피,콜라,오리지널,기타)
concepts: 마케팅·기능 컨셉 (제로슈거,저칼로리,탄산,프리미엄,유기농,기능성,비타민,단백질,발효,식이섬유,무첨가,어린이,기타)
각 값은 제품 수(정수), 상위 10개.

{len(lines)}개 제품:
""" + "\n".join(lines)

    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}",
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        r.raise_for_status()
        content = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        content = content.replace("```json", "").replace("```", "").strip()
        result  = json.loads(content)
    except Exception as e:
        st.error(f"Gemini 오류: {e}")
        return

    flavors  = result.get("flavors", {})
    concepts = result.get("concepts", {})

    c1, c2 = st.columns(2)
    with c1:
        if flavors:
            fdf = pd.DataFrame(flavors.items(), columns=["플레이버", "건수"]).sort_values("건수", ascending=False)
            fig = px.bar(fdf, x="건수", y="플레이버", orientation="h", title="🍓 플레이버별",
                         color="건수", color_continuous_scale="Oranges")
            fig.update_layout(height=420, showlegend=False, yaxis=dict(autorange="reversed"))
            fig.update_coloraxes(showscale=False)
            st.plotly_chart(fig, use_container_width=True)

    with c2:
        if concepts:
            cdf = pd.DataFrame(concepts.items(), columns=["컨셉", "건수"]).sort_values("건수", ascending=False)
            fig2 = px.bar(cdf, x="건수", y="컨셉", orientation="h", title="💡 컨셉별",
                          color="건수", color_continuous_scale="Blues")
            fig2.update_layout(height=420, showlegend=False, yaxis=dict(autorange="reversed"))
            fig2.update_coloraxes(showscale=False)
            st.plotly_chart(fig2, use_container_width=True)

    if flavors:
        fig3 = px.pie(values=list(flavors.values()), names=list(flavors.keys()),
                      title="플레이버 비중", color_discrete_sequence=px.colors.qualitative.Pastel)
        fig3.update_layout(height=360)
        st.plotly_chart(fig3, use_container_width=True)

    st.caption(f"※ {len(lines)}건 기반 GPT 추정 결과")


# ───────────────────────────────
# 분석 차트
# ───────────────────────────────
def analysis_charts(df: pd.DataFrame, label: str, show_type_chart: bool):
    st.markdown(f"### 📊 {label} 데이터 분석")

    if show_type_chart and "품목유형" in df.columns:
        c1, c2 = st.columns(2)
        with c1:
            tc  = df["품목유형"].value_counts()
            fig = px.bar(x=tc.index, y=tc.values, title="품목유형별 건수",
                         labels={"x": "품목유형", "y": "건수"}, color=tc.index)
            fig.update_layout(height=380, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            if "제조사" in df.columns:
                mt = df.groupby("품목유형")["제조사"].nunique().reset_index()
                mt.columns = ["품목유형", "제조사수"]
                fig2 = px.bar(mt, x="품목유형", y="제조사수", title="품목유형별 제조사 다양성", color="품목유형")
                fig2.update_layout(height=380, showlegend=False)
                st.plotly_chart(fig2, use_container_width=True)

    c1, c2 = st.columns(2)
    with c1:
        if "제조사" in df.columns:
            mc  = df["제조사"].value_counts().head(15)
            fig = px.bar(x=mc.values, y=mc.index, orientation="h", title="제조사별 제품 수 (상위 15)",
                         color=mc.values, color_continuous_scale="Blues")
            fig.update_layout(height=450, showlegend=False, yaxis=dict(autorange="reversed"))
            fig.update_coloraxes(showscale=False)
            st.plotly_chart(fig, use_container_width=True)

    with c2:
        if "보고일자_dt" in df.columns:
            ddt = df.dropna(subset=["보고일자_dt"]).copy()
            if not ddt.empty:
                ddt["연월"] = ddt["보고일자_dt"].dt.to_period("M").astype(str)
                mon = ddt["연월"].value_counts().sort_index().tail(24)
                fig2 = px.line(x=mon.index, y=mon.values, title="월별 보고 건수 (최근 24개월)",
                               markers=True)
                fig2.update_layout(height=450)
                st.plotly_chart(fig2, use_container_width=True)

    if "생산종료" in df.columns:
        pc  = df["생산종료"].value_counts()
        fig = px.pie(values=pc.values, names=pc.index, title="생산종료 현황",
                     color_discrete_sequence=px.colors.qualitative.Set2)
        fig.update_layout(height=350)
        st.plotly_chart(fig, use_container_width=True)


# ───────────────────────────────
# 사이드바
# ───────────────────────────────
with st.sidebar:
    st.markdown("## 🔍 조회 설정")
    st.markdown("---")

    mode = st.radio("조회 방식", ["📋 단일 유형 조회", "📊 복수 유형 비교"])
    st.markdown("---")

    # ── 조회 대상 ──
    if mode == "📋 단일 유형 조회":
        category     = st.selectbox("카테고리", list(FOOD_TYPES.keys()))
        category_all = st.checkbox(
            "카테고리 전체 조회",
            help="선택한 카테고리의 모든 품목유형을 합산 조회합니다",
        )
        if category_all:
            food_type = None
            st.info(f"**{category}** — {len(FOOD_TYPES[category])}개 품목유형 전체 조회")
            count = st.slider("품목유형별 출력 건수", 10, 100, 20, step=5)
        else:
            food_type   = st.selectbox("품목유형", FOOD_TYPES[category])
            custom_type = st.text_input("직접 입력 (선택사항)", placeholder="예: 혼합음료")
            if custom_type.strip():
                food_type = custom_type.strip()
            count = st.slider("출력 건수 (최신순)", 10, 500, 100, step=10)
    else:
        selected_types = []
        for cat, types in FOOD_TYPES.items():
            with st.expander(cat, expanded=(cat == "음료류")):
                for t in types:
                    if st.checkbox(t, value=(t in ["혼합음료", "과.채음료"]), key=f"cb_{t}"):
                        selected_types.append(t)
        per_type = st.slider("유형별 출력 건수", 10, 100, 20, step=5)
        category_all = False

    st.markdown("---")

    # ── 스캔 범위 ──
    st.markdown("#### 🔭 스캔 범위")
    max_pages = st.slider(
        "스캔 페이지 수", 10, 300, 100, step=10,
        help="1페이지 = DB 1,000건\n대형 유형(혼합음료 등) → 200 이상 권장\n소형 유형 → 50~100",
    )

    # 유형 수 계산 (복수 비교 대응)
    if mode == "📋 단일 유형 조회":
        _n_types = len(FOOD_TYPES[category]) if category_all else 1
    else:
        _n_types = max(len(selected_types), 1) if selected_types else 1

    _api_calls   = (_n_types * (max_pages + 1))   # 유형별 (probe1 + scan)
    _pct         = round(_api_calls / (_pool.total_limit) * 100, 1)
    _sec_per_page = 0.4                            # 실측 기준 (요청 0.2s + 처리)
    _est_sec     = int(_n_types * max_pages * _sec_per_page)
    _est_min     = _est_sec // 60
    _est_rem     = _est_sec % 60

    if _est_min > 0:
        _time_str = f"약 {_est_min}분 {_est_rem}초"
    else:
        _time_str = f"약 {_est_sec}초"

    # 소진율에 따른 색상 경고
    if _pct >= 80:
        st.error(f"⚠️ API {_api_calls}회 사용 · 일일 한도 **{_pct}% 소진**")
    elif _pct >= 50:
        st.warning(f"⚡ API {_api_calls}회 사용 · 일일 한도 {_pct}% 소진")
    else:
        st.info(f"📊 API {_api_calls}회 사용 · 일일 한도 {_pct}% 소진")

    st.caption(f"⏱ 예상 소요시간: {_time_str}  |  DB {max_pages*1000:,}건 스캔")

    st.markdown("---")

    # ── 보고일자 기간 필터 ──
    st.markdown("#### 📅 보고일자 기간 필터")
    use_date = st.checkbox("기간 필터 사용")
    if use_date:
        d_from = st.date_input("시작일", value=date.today() - timedelta(days=365), key="df_from")
        d_to   = st.date_input("종료일", value=date.today(), key="df_to")
    else:
        d_from = None
        d_to   = None

    st.markdown("---")
    run = st.button("🚀 조회 실행", use_container_width=True, type="primary")
    st.markdown("---")
    st.caption(f"📡 식품안전나라 I1250 API")
    st.caption(f"🔑 키 {len(_API_KEYS)}개 · 일 {_pool.total_limit:,}회 한도")
    st.caption("🤖 AI분석: Gemini (secrets)")


# ───────────────────────────────
# 조회 실행 → session_state 저장
# ───────────────────────────────
if run:
    if mode == "📋 단일 유형 조회":
        if category_all:
            _prog_box2 = st.empty()
            with _prog_box2.container():
                st.info(f"📡 **{category} 전체** 조회 중…  |  예상 소요: {_time_str}  |  API {_api_calls}회 ({_pct}% 소진 예정)")
            rows, smsgs = fetch_multiple(FOOD_TYPES[category], count, max_pages)
            _prog_box2.empty()
            st.session_state["status_msgs"] = smsgs
            if rows:
                df = to_df(rows)
                if use_date:
                    df = date_filter(df, d_from, d_to)
                st.session_state.update({
                    "result_df":    df,
                    "result_label": category,
                    "result_total": 0,
                    "result_mode":  "cat_all",
                    "result_msg":   f"✅ {category} 전체 조회 완료 → {len(df)}건",
                })
            else:
                st.session_state["result_df"]  = pd.DataFrame()
                st.session_state["result_msg"] = "조회 결과 없음"
        else:
            import threading, time as _time

            st.session_state["stop_scan"]  = False
            st.session_state["is_scanning"] = True

            _stop_flag = [False]

            _prog_box  = st.empty()
            _stop_area = st.empty()

            with _prog_box.container():
                st.info(f"📡 **'{food_type}'** 조회 중…  |  예상 소요: {_time_str}  |  API {_api_calls}회 ({_pct}% 소진 예정)")
                _pbar = st.progress(0, text="DB 스캔 시작…")

            with _stop_area:
                if st.button("⛔ 조회 중단", key="stop_btn", type="secondary"):
                    _stop_flag[0] = True
                    st.session_state["stop_scan"] = True

            _state = {"done": False, "result": None}

            def _run():
                _state["result"] = fetch_food_data(
                    food_type, top_n=count, max_pages=max_pages, stop_flag=_stop_flag
                )
                _state["done"] = True

            _t = threading.Thread(target=_run, daemon=True)
            _t.start()

            _elapsed    = 0
            _total_est  = max(_api_calls * 0.4, 1)
            while not _state["done"]:
                _elapsed   += 0.5
                _prog_val   = min(int(_elapsed / _total_est * 95), 95)
                _remaining  = max(int(_total_est - _elapsed), 0)
                _pbar.progress(_prog_val, text=f"스캔 중… 경과 {int(_elapsed)}초 / 잔여 약 {_remaining}초")
                _time.sleep(0.5)

            _prog_box.empty()
            _stop_area.empty()
            st.session_state["is_scanning"] = False

            rows, msg, total, _ = _state["result"]
            if rows is None:
                st.error(f"❌ {msg}")
            elif not rows:
                st.session_state["result_df"]  = pd.DataFrame()
                st.session_state["result_msg"] = f"'{food_type}' 데이터 없음"
            else:
                df = to_df(rows)
                if use_date:
                    df = date_filter(df, d_from, d_to)
                st.session_state.update({
                    "result_df":    df,
                    "result_label": food_type,
                    "result_total": total,
                    "result_mode":  "single",
                    "result_msg":   f"✅ {msg} | 출력 {len(df)}건",
                })
    else:
        if not selected_types:
            st.warning("품목유형을 1개 이상 선택하세요.")
        else:
            _prog_box3 = st.empty()
            with _prog_box3.container():
                st.info(f"📡 복수 유형 조회 중…  |  예상 소요: {_time_str}  |  API {_api_calls}회 ({_pct}% 소진 예정)")
            rows, smsgs = fetch_multiple(selected_types, per_type, max_pages)
            _prog_box3.empty()
            st.session_state["status_msgs"] = smsgs
            if rows:
                df = to_df(rows)
                if use_date:
                    df = date_filter(df, d_from, d_to)
                label = ", ".join(selected_types[:3]) + ("…" if len(selected_types) > 3 else "")
                st.session_state.update({
                    "result_df":    df,
                    "result_label": label,
                    "result_total": 0,
                    "result_mode":  "multi",
                    "result_msg":   f"✅ 복수 유형 조회 완료 → {len(df)}건",
                })
            else:
                st.session_state["result_df"]  = pd.DataFrame()
                st.session_state["result_msg"] = "조회 결과 없음"


# ───────────────────────────────
# 결과 렌더링 (session_state 기반)
# ───────────────────────────────
st.markdown("# 🏭 식품안전나라 품목제조보고 조회")
st.markdown("---")

df        = st.session_state["result_df"]
r_mode    = st.session_state["result_mode"]
r_label   = st.session_state["result_label"]
r_total   = st.session_state["result_total"]
r_msg     = st.session_state["result_msg"]
smsgs     = st.session_state["status_msgs"]

if df is None:
    # 초기 화면
    st.info("👈 왼쪽 사이드바에서 설정 후 **[🚀 조회 실행]** 버튼을 누르세요.")
    st.markdown("""
#### 주요 기능

| 기능 | 위치 |
|---|---|
| 카테고리 전체 조회 | 사이드바 → **카테고리 전체 조회** 체크박스 |
| 주요원재료 컬럼 | 제품 목록 테이블에 자동 포함 |
| 제조사 필터 | 제품 목록 탭 → **제조사 필터** 드롭다운 |
| 보고일자 기간 필터 | 사이드바 → **📅 보고일자 기간 필터** |
| AI 플레이버/컨셉 분석 | 조회 후 **🤖 AI 현황분석** 탭 |
| 전체 DB 스캔 | 사이드바 → **스캔 페이지 수** 슬라이더 |
""")

elif df.empty:
    st.warning(f"⚠️ {r_msg}")

else:
    st.success(r_msg)

    # 카테고리/복수 유형 요약 메트릭
    if smsgs and r_mode in ("cat_all", "multi"):
        keys  = list(smsgs.keys())
        scols = st.columns(min(len(keys), 6))
        for i, ft in enumerate(keys):
            info = smsgs[ft]
            with scols[i % len(scols)]:
                if "정상" in info.get("msg", ""):
                    st.metric(ft, f"{info['fetched']}건", f"전체 {info['total']:,}건")
                else:
                    st.metric(ft, "❌", info.get("msg", "")[:15])
        st.markdown("---")

    # 상단 메트릭
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("조회 결과", f"{len(df)}건")
    if r_mode == "single":
        c2.metric("전체 DB", f"{r_total:,}건")
        c3.metric("품목유형", r_label)
    else:
        c2.metric("카테고리/유형", r_label)
        c3.metric("품목유형 수", f"{df['품목유형'].nunique()}개" if "품목유형" in df.columns else "-")
    if "제조사" in df.columns:
        c4.metric("제조사 수", f"{df['제조사'].nunique()}개")
    st.markdown("---")

    # 탭
    t1, t2, t3, t4 = st.tabs(["📋 제품 목록", "📊 분석 차트", "🤖 AI 현황분석", "📥 원시 데이터"])

    with t1:
        st.markdown(f"### 📋 {r_label} 품목 목록 ({len(df)}건)")
        product_table(df, show_type=(r_mode != "single"), pfx="res")

    with t2:
        analysis_charts(df, r_label, show_type_chart=(r_mode != "single"))

    with t3:
        ai_tab(df, r_label)

    with t4:
        st.dataframe(df, use_container_width=True, height=500)
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 CSV 다운로드", csv,
            f"{r_label}_품목제조보고_{datetime.now().strftime('%Y%m%d')}.csv",
            "text/csv", use_container_width=True,
        )
        raw_fields = st.session_state.get("_raw_fields", [])
        if raw_fields:
            with st.expander("🔬 API 원시 필드 목록 (디버그)"):
                st.caption("아래 필드명 중 원재료명에 해당하는 항목을 확인하세요.")
                st.code(", ".join(raw_fields))
