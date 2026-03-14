"""
🔍 식품안전나라 품목제조보고 조회 시스템 v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
속도 개선 전략
  1순위: 포털 내부 Ajax API  → 서버사이드 필터링, 1~2회 호출로 완료
  2순위: I1250 병렬 페이지네이션 → ThreadPoolExecutor로 동시 다중 호출
"""

import streamlit as st
import requests
import pandas as pd
import plotly.express as px
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# ━━━ 스타일 ━━━
st.markdown("""
<style>
[data-testid="stSidebar"] { background: #f8f9fb; }
div[data-testid="stMetric"] { background: #f0f2f5; border-radius: 10px; padding: 12px; }
</style>
""", unsafe_allow_html=True)

# ━━━ 상수 ━━━
API_KEY    = "9171f7ffd72f4ffcb62f"
SERVICE_ID = "I1250"
BASE_URL   = f"http://openapi.foodsafetykorea.go.kr/api/{API_KEY}/{SERVICE_ID}/json"

# 포털 내부 Ajax URL 후보 (우선순위 순)
# 실제 URL은 브라우저 DevTools → Network → XHR 탭에서 확인 가능
PORTAL_URLS = [
    "https://www.foodsafetykorea.go.kr/portal/specialinfo/searchInfoProductList.do",
    "https://www.foodsafetykorea.go.kr/portal/specialinfo/getSearchInfoProductList.do",
    "https://www.foodsafetykorea.go.kr/portal/product/retrieveProductList.do",
]

PORTAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": (
        "https://www.foodsafetykorea.go.kr/portal/specialinfo/"
        "searchInfoProduct.do?menu_grp=MENU_NEW04&menu_no=2815"
    ),
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
}

# 대분류 → 포털 코드 (DevTools에서 확인한 값으로 교체 가능)
CATEGORY_CODES = {
    "음료류":      "D007",
    "과자류":      "D004",
    "빵·면류":    "D005",
    "조미·소스류": "D010",
    "유가공품":    "D002",
    "건강기능식품": "J001",
    "기타":        "",
}

FOOD_TYPES = {
    "음료류":      ["혼합음료", "과·채음료", "과·채주스", "탄산음료",
                    "두유류", "유산균음료", "커피", "인삼·홍삼음료"],
    "과자류":      ["과자", "캔디류", "추잉껌", "빙과", "아이스크림"],
    "빵·면류":    ["빵류", "떡류", "면류", "즉석섭취식품"],
    "조미·소스류": ["소스", "복합조미식품", "향신료가공품", "식초", "드레싱"],
    "유가공품":    ["치즈", "버터", "발효유", "우유류", "가공유"],
    "건강기능식품": ["건강기능식품"],
    "기타":        ["잼류", "식용유지", "김치류", "두부류",
                    "즉석조리식품", "레토르트식품"],
}

FOOD_TYPE_TO_CATEGORY = {
    t: cat
    for cat, types in FOOD_TYPES.items()
    for t in types
}


# ══════════════════════════════════════════════════════
#  1순위: 포털 내부 Ajax API (서버사이드 필터링)
# ══════════════════════════════════════════════════════
def _try_portal_api(food_type: str, category: str, count: int):
    """
    포털 내부 Ajax 엔드포인트 시도.
    성공 시 (rows, source_msg) 반환, 실패 시 (None, reason).

    왜 빠른가:
      - 서버가 PRDLST_DCNM 조건으로 DB를 직접 쿼리
      - 전체 수백만 건을 순회할 필요 없음
      - 보통 1~3초 내 응답
    """
    cat_code = CATEGORY_CODES.get(category, "")

    # 포털이 사용하는 파라미터명을 알 수 없으므로 여러 조합 시도
    param_variants = [
        {
            "prdlst_dcnm":    food_type,
            "prdlst_dcnm_cd": cat_code,
            "pageIndex":      "1",
            "rows":           str(count),
            "sort_column":    "PRMS_DT",
            "sort_order":     "desc",
        },
        {
            "PRDLST_DCNM":    food_type,
            "PRDLST_DCNM_CD": cat_code,
            "pageIndex":      "1",
            "pageSize":       str(count),
        },
        {
            "searchType":    "PRDLST_DCNM",
            "searchKeyword": food_type,
            "catCd":         cat_code,
            "pageIndex":     "1",
            "rows":          str(count),
        },
    ]

    for url in PORTAL_URLS:
        for params in param_variants:
            try:
                resp = requests.post(
                    url, data=params,
                    headers=PORTAL_HEADERS, timeout=15,
                )
                if resp.status_code != 200:
                    continue

                ct = resp.headers.get("Content-Type", "")
                if "json" not in ct and "javascript" not in ct:
                    continue

                data = resp.json()
                rows = None

                if isinstance(data, list):
                    rows = data
                elif isinstance(data, dict):
                    for key in ("list", "rows", "data", "items",
                                "result", "productList", "row"):
                        if key in data and isinstance(data[key], list):
                            rows = data[key]
                            break

                if rows and len(rows) > 0:
                    rows = _normalize_portal_rows(rows, food_type)
                    return rows, f"포털 내부 API ({url.split('/')[-1]})"

            except Exception:
                continue

    return None, "포털 내부 API 미응답"


def _normalize_portal_rows(rows: list, food_type: str) -> list:
    """포털 응답 컬럼명 → I1250 형식 정규화"""
    portal_to_i1250 = {
        "prdlst_nm":        "PRDLST_NM",
        "bssh_nm":          "BSSH_NM",
        "prdlst_dcnm":      "PRDLST_DCNM",
        "prms_dt":          "PRMS_DT",
        "pog_daycnt":       "POG_DAYCNT",
        "production":       "PRODUCTION",
        "induty_cd_nm":     "INDUTY_CD_NM",
        "lcns_no":          "LCNS_NO",
        "prdlst_report_no": "PRDLST_REPORT_NO",
        "last_updt_dtm":    "LAST_UPDT_DTM",
    }
    normalized = []
    for row in rows:
        new = {
            portal_to_i1250.get(k.lower(), k.upper()): v
            for k, v in row.items()
        }
        if "PRDLST_DCNM" not in new:
            new["PRDLST_DCNM"] = food_type
        normalized.append(new)
    return normalized


# ══════════════════════════════════════════════════════
#  2순위: I1250 병렬 페이지네이션
# ══════════════════════════════════════════════════════
def _fetch_page(page_start: int, page_end: int):
    """단일 페이지 호출 — ThreadPoolExecutor 워커에서 실행"""
    url = f"{BASE_URL}/{page_start}/{page_end}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data   = resp.json()
        result = data.get(SERVICE_ID, {})
        code   = result.get("RESULT", {}).get("CODE", "")
        if code == "INFO-000":
            return result.get("row", [])
        return []
    except Exception:
        return None


def fetch_by_parallel(food_type: str, count: int):
    """
    병렬 페이지네이션 (ThreadPoolExecutor).

    기존 순차 방식 대비 속도:
      순차: 페이지당 ~3초 × n페이지 = 수분
      병렬: 모든 페이지 동시 호출 → 최대 페이지 1개 기다리는 시간

    조기 종료:
      count건이 수집되면 남은 future를 cancel하여
      불필요한 API 호출 중단
    """
    PAGE_SIZE   = 1000
    MAX_WORKERS = 5     # 동시 호출 수 (API 서버 부하 고려)

    # total_count 파악
    try:
        resp  = requests.get(f"{BASE_URL}/1/1", timeout=15)
        data  = resp.json()
        total = int(data.get(SERVICE_ID, {}).get("total_count", 0))
    except Exception as e:
        return None, f"total_count 조회 실패: {e}", 0

    if total == 0:
        return [], "데이터 없음", 0

    # 페이지 범위 목록 생성
    pages = [
        (s, min(s + PAGE_SIZE - 1, total))
        for s in range(1, total + 1, PAGE_SIZE)
    ]

    collected  = []
    enough     = False

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(_fetch_page, ps, pe): (ps, pe)
            for ps, pe in pages
        }
        for fut in as_completed(futures):
            if enough:
                fut.cancel()
                continue

            rows = fut.result()
            if rows:
                matched = [
                    r for r in rows
                    if r.get("PRDLST_DCNM", "").strip() == food_type.strip()
                ]
                collected.extend(matched)

            if len(collected) >= count:
                enough = True

    collected = collected[:count]
    return collected, f"I1250 병렬 ({len(pages)}페이지 동시호출)", total


# ══════════════════════════════════════════════════════
#  통합 조회 함수
# ══════════════════════════════════════════════════════
@st.cache_data(ttl=600, show_spinner=False)
def fetch_food_data(food_type: str, count: int = 100,
                    category: str = "") -> tuple:
    """
    1순위: 포털 내부 API → 실패 시 2순위
    2순위: I1250 병렬 페이지네이션
    반환: (rows, source_msg, total_count)
    """
    if not category:
        category = FOOD_TYPE_TO_CATEGORY.get(food_type, "")

    rows, msg = _try_portal_api(food_type, category, count)
    if rows is not None:
        return rows, msg, len(rows)

    rows, msg, total = fetch_by_parallel(food_type, count)
    return rows, msg, total


def fetch_multiple_types(types_list: list, per_type: int = 20) -> tuple:
    """복수 유형 순차 조회 (유형별 내부는 병렬)"""
    all_rows    = []
    status_msgs = {}
    progress    = st.progress(0, text="조회 중...")

    for i, ft in enumerate(types_list):
        progress.progress(
            (i + 1) / len(types_list),
            text=f"📡 {ft} 조회 중… ({i+1}/{len(types_list)})",
        )
        rows, msg, total = fetch_food_data(ft, per_type)
        status_msgs[ft]  = {
            "msg":     msg,
            "total":   total,
            "fetched": len(rows) if rows else 0,
        }
        if rows:
            all_rows.extend(rows)

    progress.empty()
    return all_rows, status_msgs


# ══════════════════════════════════════════════════════
#  Gemini AI 분석
# ══════════════════════════════════════════════════════

def _get_gemini_model(api_key: str, model_name: str):
    """
    Gemini 모델 객체 반환.
    - @st.cache_data 안에 넣으면 직렬화 오류가 나므로 반드시 밖에서 생성.
    - 결과 텍스트만 캐시.
    """
    if not GENAI_AVAILABLE:
        raise ImportError("google-generativeai 패키지가 설치되어 있지 않습니다.\n"
                          "pip install google-generativeai")
    if not api_key or not api_key.strip():
        raise ValueError("Gemini API 키가 비어 있습니다.\n"
                         "사이드바에서 API 키를 입력하거나 secrets.toml에 설정하세요.")
    genai.configure(api_key=api_key.strip())
    return genai.GenerativeModel(model_name)


@st.cache_data(ttl=1800, show_spinner=False)
def _cached_gemini(prompt: str, api_key: str, model_name: str) -> str:
    """
    결과 텍스트만 캐시 (모델 객체는 캐시 불가).
    같은 prompt + key + model 조합이면 30분간 재사용.
    """
    model = _get_gemini_model(api_key, model_name)
    resp  = model.generate_content(prompt)
    return resp.text


def run_gemini_analysis(df: pd.DataFrame, food_type: str,
                        api_key: str, model_name: str) -> dict:
    """
    4가지 분석을 순차 실행하고 결과 dict 반환.
    각 분석은 독립 프롬프트 → 별도 캐시.
    """
    if df.empty:
        return {}

    # ── 공통 컨텍스트 데이터 준비 ──
    total        = len(df)
    maker_top10  = (
        df["제조사"].value_counts().head(10).to_dict()
        if "제조사" in df.columns else {}
    )
    recent_prods = []
    if "제품명" in df.columns and "보고일자" in df.columns:
        recent_prods = (
            df[["제품명", "제조사", "보고일자"]]
            .head(30)
            .to_dict(orient="records")
        ) if "제조사" in df.columns else (
            df[["제품명", "보고일자"]].head(30).to_dict(orient="records")
        )

    monthly_trend = {}
    if "보고일자_dt" in df.columns:
        tmp = df.dropna(subset=["보고일자_dt"]).copy()
        if not tmp.empty:
            tmp["연월"] = tmp["보고일자_dt"].dt.to_period("M").astype(str)
            monthly_trend = (
                tmp["연월"].value_counts().sort_index().tail(24).to_dict()
            )

    # ── 프롬프트 정의 ──
    system_prefix = (
        f"당신은 식품 R&D 전문가입니다. "
        f"아래는 식품안전나라 품목제조보고 DB에서 조회한 "
        f"**{food_type}** 카테고리의 최신 데이터 {total}건입니다.\n"
        f"분석 결과는 한국어로, 식품 R&D 담당자가 바로 활용할 수 있는 "
        f"실무적 인사이트로 작성하세요.\n\n"
    )

    prompts = {
        "트렌드 요약": (
            system_prefix +
            f"### 월별 보고 건수 추이 (최근 24개월)\n{monthly_trend}\n\n"
            f"### 최신 보고 제품 30건\n{recent_prods}\n\n"
            "위 데이터를 바탕으로 다음을 분석하세요:\n"
            "1. 최근 {food_type} 시장의 신제품 출시 트렌드 (증가/감소/계절성)\n"
            "2. 주목할 만한 제품명 패턴 또는 키워드\n"
            "3. R&D 관점에서 시사점\n"
            "각 항목은 2~3문장으로 간결하게."
        ),
        "제조사 경쟁구도": (
            system_prefix +
            f"### 제조사별 제품 수 (상위 10)\n{maker_top10}\n\n"
            f"전체 제조사 수: {df['제조사'].nunique() if '제조사' in df.columns else 'N/A'}개\n\n"
            "위 데이터를 바탕으로 다음을 분석하세요:\n"
            "1. 시장 집중도 (상위 3개사 점유율 추정)\n"
            "2. 경쟁 구도 특징 (과점/분산/신규 진입 여부)\n"
            "3. 중소 제조사 진입 여지\n"
            "각 항목은 2~3문장으로 간결하게."
        ),
        "신제품 출시 패턴": (
            system_prefix +
            f"### 최신 보고 제품 30건\n{recent_prods}\n\n"
            f"### 월별 보고 건수\n{monthly_trend}\n\n"
            "위 데이터를 바탕으로 다음을 분석하세요:\n"
            "1. 제품명에서 보이는 공통 키워드/트렌드 (기능성, 원료, 포맷 등)\n"
            "2. 출시 시기 패턴 (특정 월 집중 여부)\n"
            "3. 예상되는 다음 트렌드 방향\n"
            "각 항목은 2~3문장으로 간결하게."
        ),
        "원료·성분 특징 요약": (
            system_prefix +
            f"### 최신 보고 제품 30건 (제품명 중심)\n{recent_prods}\n\n"
            "제품명만으로 추정 가능한 내용을 분석하세요:\n"
            "1. 자주 등장하는 원료/기능성 소재 키워드\n"
            "2. 무가당·저칼로리·기능성 등 헬스 포지셔닝 비중\n"
            "3. R&D 포뮬레이션 관점에서 주목할 소재\n"
            "각 항목은 2~3문장으로 간결하게.\n"
            "※ 실제 성분 데이터가 없으므로 제품명 기반 추정임을 명시하세요."
        ),
    }

    results = {}
    for title, prompt in prompts.items():
        try:
            results[title] = _cached_gemini(prompt, api_key, model_name)
        except Exception as e:
            results[title] = f"❌ 분석 실패: {e}"

    return results


# ══════════════════════════════════════════════════════
#  DataFrame 변환
# ══════════════════════════════════════════════════════
def to_dataframe(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    col_map = {
        "PRDLST_NM":                "제품명",
        "PRDLST_DCNM":              "식품유형",
        "BSSH_NM":                  "제조사",
        "PRMS_DT":                  "보고일자",
        "POG_DAYCNT":               "유통기한",
        "PRODUCTION":               "생산종료",
        "INDUTY_CD_NM":             "업종",
        "USAGE":                    "용법",
        "PRPOS":                    "용도",
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
    rename = {k: v for k, v in col_map.items() if k in df.columns}
    df     = df.rename(columns=rename)

    if "보고일자" in df.columns:
        df["보고일자"]    = df["보고일자"].astype(str)
        df["보고일자_dt"] = pd.to_datetime(
            df["보고일자"], format="%Y%m%d", errors="coerce"
        )
        df = df.sort_values("보고일자_dt", ascending=False).reset_index(drop=True)

    return df


# ══════════════════════════════════════════════════════
#  사이드바
# ══════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 🔍 조회 설정")
    st.markdown("---")

    mode = st.radio(
        "조회 방식",
        ["📋 단일 유형 조회", "📊 복수 유형 비교"],
    )

    st.markdown("---")

    if mode == "📋 단일 유형 조회":
        category  = st.selectbox("카테고리 (대분류)", list(FOOD_TYPES.keys()))
        food_type = st.selectbox("식품유형 (소분류)", FOOD_TYPES[category])

        custom_type = st.text_input(
            "또는 직접 입력",
            placeholder="예: 혼합음료, 잼류...",
            help="PRDLST_DCNM 값과 완전일치 — 가운뎃점 · 주의",
        )
        if custom_type.strip():
            food_type = custom_type.strip()
            category  = FOOD_TYPE_TO_CATEGORY.get(food_type, category)

        count = st.slider("조회 건수", 10, 300, 100, step=10)

    else:
        st.markdown("**비교할 유형 선택:**")
        selected_types = []
        for cat, types in FOOD_TYPES.items():
            with st.expander(cat, expanded=(cat == "음료류")):
                for t in types:
                    if st.checkbox(
                        t,
                        value=(t in ["혼합음료", "과·채음료"]),
                        key=f"cb_{t}",
                    ):
                        selected_types.append(t)
        per_type = st.slider("유형별 조회 건수", 10, 50, 20, step=5)

    st.markdown("---")
    run = st.button("🚀 조회 실행", use_container_width=True, type="primary")

    st.markdown("---")
    st.markdown("### 🤖 Gemini AI 설정")

    # API 키: secrets.toml 전용 (GEMINI_API_KEY 또는 GOOGLE_API_KEY 둘 다 허용)
    gemini_key = ""
    try:
        gemini_key = (
            st.secrets.get("GEMINI_API_KEY", "")
            or st.secrets.get("GOOGLE_API_KEY", "")
        )
    except Exception:
        pass

    if gemini_key:
        st.success("✅ API 키 연결됨", icon="🔑")
    else:
        st.warning("⚠️ API 키 없음", icon="🔑")
        st.caption(
            "`.streamlit/secrets.toml`에 아래 중 하나 추가:\n"
            "```toml\nGOOGLE_API_KEY = \"AIza...\"\n"
            "# 또는\nGEMINI_API_KEY = \"AIza...\"\n```"
        )

    gemini_model = st.selectbox(
        "모델 선택",
        ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash-preview-04-17"],
        index=0,
    )

    ai_auto = st.toggle("조회 후 자동 분석", value=False,
                        help="끄면 결과 아래 [AI 분석 실행] 버튼으로 수동 실행")

    st.markdown("---")
    st.markdown("""
**조회 우선순위**
1. 🏃 포털 내부 API *(서버필터)*
2. ⚡ I1250 병렬 페이지네이션
""")
    st.caption("📡 식품안전나라 I1250 API")
    st.caption(f"🔑 키: {API_KEY[:8]}...")
    st.caption("⚠️ 일일 API 호출 2,000회 제한")


# ══════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════
st.markdown("# 🏭 식품안전나라 품목제조보고 조회")
st.markdown("식품유형별 최신 품목제조보고 데이터를 실시간으로 조회합니다.")
st.markdown("---")

if run:

    # ━━ 단일 유형 조회 ━━
    if mode == "📋 단일 유형 조회":
        t_start    = time.time()
        status_box = st.empty()
        status_box.info(f"📡 **'{food_type}'** 조회 중… (포털 API 시도 → 실패 시 병렬 I1250)")

        rows, source_msg, total = fetch_food_data(food_type, count, category)
        elapsed = time.time() - t_start

        if rows is None:
            st.error(f"❌ 조회 실패: {source_msg}")
        elif len(rows) == 0:
            st.warning(
                f"⚠️ '{food_type}' 데이터가 없습니다.\n\n"
                "식품안전나라 DB의 실제 PRDLST_DCNM 값과 일치하는지 확인하세요. "
                "(가운뎃점 `·` vs 마침표 `.` 구분)"
            )
        else:
            badge = "🏃 포털 내부 API" if "포털" in source_msg else "⚡ I1250 병렬"
            status_box.success(
                f"✅ **{badge}** ({source_msg}) — "
                f"{len(rows)}건 조회 완료 / 소요: **{elapsed:.1f}초**"
            )

            df = to_dataframe(rows)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("조회 결과",    f"{len(df)}건")
            c2.metric("전체 등록 수", f"{total:,}건" if total > len(df) else "-")
            c3.metric("식품유형",      food_type)
            if "제조사" in df.columns:
                c4.metric("제조사 수", f"{df['제조사'].nunique()}개")

            st.markdown("---")
            tab1, tab2, tab3 = st.tabs(["📋 제품 목록", "📊 분석 차트", "📥 원시 데이터"])

            with tab1:
                st.markdown(f"### 📋 {food_type} 품목제조보고 ({len(df)}건)")
                col_a, col_b = st.columns(2)
                with col_a:
                    search = st.text_input("🔎 제품명/제조사 검색")
                with col_b:
                    if "제조사" in df.columns:
                        makers    = ["전체"] + sorted(
                            df["제조사"].dropna().unique().tolist()
                        )
                        sel_maker = st.selectbox("제조사 필터", makers)

                filtered = df.copy()
                if search:
                    mask     = filtered.apply(
                        lambda r: search.lower() in str(r).lower(), axis=1
                    )
                    filtered = filtered[mask]
                if "제조사" in df.columns and sel_maker != "전체":
                    filtered = filtered[filtered["제조사"] == sel_maker]

                sc = ["제품명", "식품유형", "제조사", "보고일자", "유통기한", "생산종료"]
                sc = [c for c in sc if c in filtered.columns]
                st.dataframe(
                    filtered[sc].reset_index(drop=True),
                    use_container_width=True, height=500,
                )
                st.caption(f"총 {len(filtered)}건 표시 중")

            with tab2:
                st.markdown(f"### 📊 {food_type} 데이터 분석")
                ch1, ch2 = st.columns(2)

                if "제조사" in df.columns:
                    with ch1:
                        mc   = df["제조사"].value_counts().head(15)
                        fig1 = px.bar(
                            x=mc.values, y=mc.index, orientation="h",
                            title="제조사별 제품 수 (상위 15)",
                            labels={"x": "제품 수", "y": "제조사"},
                            color=mc.values,
                            color_continuous_scale="Blues",
                        )
                        fig1.update_layout(
                            height=450, showlegend=False,
                            yaxis=dict(autorange="reversed"),
                        )
                        fig1.update_coloraxes(showscale=False)
                        st.plotly_chart(fig1, use_container_width=True)

                if "보고일자_dt" in df.columns:
                    with ch2:
                        df_dt = df.dropna(subset=["보고일자_dt"]).copy()
                        if not df_dt.empty:
                            df_dt["연월"] = (
                                df_dt["보고일자_dt"].dt.to_period("M").astype(str)
                            )
                            monthly = (
                                df_dt["연월"].value_counts()
                                .sort_index().tail(24)
                            )
                            fig2 = px.line(
                                x=monthly.index, y=monthly.values,
                                title="월별 보고 건수 추이 (최근 24개월)",
                                labels={"x": "연월", "y": "건수"},
                                markers=True,
                            )
                            fig2.update_layout(height=450)
                            st.plotly_chart(fig2, use_container_width=True)

                if "생산종료" in df.columns:
                    pc   = df["생산종료"].value_counts()
                    fig3 = px.pie(
                        values=pc.values, names=pc.index,
                        title="생산종료 현황",
                        color_discrete_sequence=px.colors.qualitative.Set2,
                    )
                    fig3.update_layout(height=350)
                    st.plotly_chart(fig3, use_container_width=True)

            with tab3:
                st.markdown("### 📥 원시 데이터 (전체 필드)")
                st.dataframe(df, use_container_width=True, height=500)
                csv = df.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "📥 CSV 다운로드", csv,
                    f"{food_type}_품목제조보고_{datetime.now().strftime('%Y%m%d')}.csv",
                    "text/csv", use_container_width=True,
                )

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            #  🤖 Gemini AI 분석 — 탭 바깥, 조회 결과 바로 아래
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            st.markdown("---")
            st.markdown("## 🤖 Gemini AI 분석")

            if not GENAI_AVAILABLE:
                st.error(
                    "google-generativeai 패키지가 없습니다.\n\n"
                    "터미널에서 아래 명령 실행 후 재시작:\n"
                    "```bash\npip install google-generativeai\n```"
                )
            elif not gemini_key:
                st.warning(
                    "⚠️ **Gemini API 키가 없습니다.**\n\n"
                    "`.streamlit/secrets.toml` 파일에 아래 중 하나를 추가하세요:\n"
                    "```toml\nGOOGLE_API_KEY = \"AIza...\"\n"
                    "# 또는\nGEMINI_API_KEY = \"AIza...\"\n```\n\n"
                    "[🔑 Google AI Studio에서 무료 발급](https://aistudio.google.com/app/apikey)"
                )
            else:
                # 자동 분석 토글이 켜져 있으면 바로 실행, 아니면 버튼 표시
                do_analysis = ai_auto
                if not ai_auto:
                    do_analysis = st.button(
                        f"🔍 AI 분석 실행 ({food_type} {len(df)}건)",
                        key="btn_ai_single",
                        type="primary",
                        use_container_width=True,
                    )

                if do_analysis:
                    icons = {
                        "트렌드 요약":         "📈",
                        "제조사 경쟁구도":     "🏢",
                        "신제품 출시 패턴":    "🆕",
                        "원료·성분 특징 요약": "🧪",
                    }

                    # 4개 분석을 하나씩 스트리밍 표시
                    ai_results = {}
                    for title in icons:
                        col_icon, col_title = st.columns([0.05, 0.95])
                        col_title.markdown(f"#### {icons[title]} {title}")
                        result_box = st.empty()
                        result_box.info("분석 중…")

                        try:
                            # 캐시 함수 호출 (같은 입력이면 즉시 반환)
                            monthly_trend = {}
                            if "보고일자_dt" in df.columns:
                                tmp = df.dropna(subset=["보고일자_dt"]).copy()
                                if not tmp.empty:
                                    tmp["연월"] = (
                                        tmp["보고일자_dt"]
                                        .dt.to_period("M").astype(str)
                                    )
                                    monthly_trend = (
                                        tmp["연월"].value_counts()
                                        .sort_index().tail(24).to_dict()
                                    )

                            maker_top10 = (
                                df["제조사"].value_counts().head(10).to_dict()
                                if "제조사" in df.columns else {}
                            )
                            recent_prods = []
                            if "제품명" in df.columns:
                                cols = [c for c in ["제품명", "제조사", "보고일자"]
                                        if c in df.columns]
                                recent_prods = df[cols].head(30).to_dict(orient="records")

                            system_prefix = (
                                f"당신은 식품 R&D 전문가입니다. "
                                f"식품안전나라 품목제조보고 DB에서 조회한 "
                                f"**{food_type}** 카테고리 {len(df)}건 데이터를 분석합니다.\n"
                                f"결과는 한국어로, 식품 R&D 담당자가 즉시 활용 가능한 "
                                f"실무적 인사이트로 작성하세요.\n\n"
                            )

                            prompt_map = {
                                "트렌드 요약": (
                                    system_prefix
                                    + f"### 월별 보고 건수 (최근 24개월)\n{monthly_trend}\n\n"
                                    + f"### 최신 보고 제품 30건\n{recent_prods}\n\n"
                                    + "분석 항목:\n"
                                    + "1. 신제품 출시 트렌드 (증가/감소/계절성)\n"
                                    + "2. 주목할 제품명 패턴·키워드\n"
                                    + "3. R&D 관점 시사점\n"
                                    + "각 항목 2~3문장으로 간결하게."
                                ),
                                "제조사 경쟁구도": (
                                    system_prefix
                                    + f"### 제조사별 제품 수 (상위 10)\n{maker_top10}\n"
                                    + f"전체 제조사 수: {df['제조사'].nunique() if '제조사' in df.columns else 'N/A'}개\n\n"
                                    + "분석 항목:\n"
                                    + "1. 시장 집중도 (상위 3개사 점유율 추정)\n"
                                    + "2. 경쟁 구도 특징 (과점/분산/신규 진입)\n"
                                    + "3. 중소 제조사 진입 여지\n"
                                    + "각 항목 2~3문장으로 간결하게."
                                ),
                                "신제품 출시 패턴": (
                                    system_prefix
                                    + f"### 최신 보고 제품 30건\n{recent_prods}\n\n"
                                    + f"### 월별 보고 건수\n{monthly_trend}\n\n"
                                    + "분석 항목:\n"
                                    + "1. 제품명 공통 키워드·트렌드 (기능성, 원료, 포맷 등)\n"
                                    + "2. 출시 시기 패턴 (특정 월 집중 여부)\n"
                                    + "3. 예상 다음 트렌드 방향\n"
                                    + "각 항목 2~3문장으로 간결하게."
                                ),
                                "원료·성분 특징 요약": (
                                    system_prefix
                                    + f"### 최신 보고 제품 30건 (제품명 중심)\n{recent_prods}\n\n"
                                    + "분석 항목:\n"
                                    + "1. 자주 등장하는 원료·기능성 소재 키워드\n"
                                    + "2. 무가당·저칼로리·기능성 등 헬스 포지셔닝 비중\n"
                                    + "3. R&D 포뮬레이션 관점 주목 소재\n"
                                    + "각 항목 2~3문장으로 간결하게.\n"
                                    + "※ 제품명 기반 추정임을 명시하세요."
                                ),
                            }

                            text = _cached_gemini(
                                prompt_map[title], gemini_key, gemini_model
                            )
                            ai_results[title] = text
                            result_box.markdown(text)

                        except Exception as e:
                            err_msg = str(e)
                            ai_results[title] = f"❌ {err_msg}"
                            result_box.error(f"분석 실패: {err_msg}")

                        st.markdown("")  # 간격

                    # 전체 결과 다운로드
                    if ai_results:
                        full_text = "\n\n".join(
                            f"## {icons.get(t,'')} {t}\n{c}"
                            for t, c in ai_results.items()
                        )
                        st.download_button(
                            "📥 AI 분석 결과 TXT 다운로드",
                            full_text.encode("utf-8"),
                            f"{food_type}_AI분석_{datetime.now().strftime('%Y%m%d')}.txt",
                            "text/plain",
                            use_container_width=True,
                        )

    # ━━ 복수 유형 비교 ━━
    else:
        if not selected_types:
            st.warning("⚠️ 비교할 식품유형을 1개 이상 선택하세요.")
        else:
            t_start  = time.time()
            all_rows, status_msgs = fetch_multiple_types(selected_types, per_type)
            elapsed  = time.time() - t_start

            st.success(f"✅ {len(selected_types)}개 유형 조회 완료 ({elapsed:.1f}초)")
            st.markdown("### 📡 조회 결과 요약")

            summary_cols = st.columns(min(len(selected_types), 5))
            for i, ft in enumerate(selected_types):
                info = status_msgs[ft]
                with summary_cols[i % len(summary_cols)]:
                    if info["fetched"] > 0:
                        st.metric(ft, f"{info['fetched']}건",
                                  f"전체 {info['total']:,}건")
                    else:
                        st.metric(ft, "0건", info["msg"])

            if all_rows:
                df = to_dataframe(all_rows)
                st.markdown("---")

                tab1, tab2, tab3 = st.tabs(
                    ["📋 통합 목록", "📊 유형별 비교", "📥 데이터"]
                )

                with tab1:
                    st.markdown(f"### 📋 통합 품목 목록 ({len(df)}건)")
                    types_in = ["전체"] + sorted(
                        df["식품유형"].dropna().unique().tolist()
                    )
                    sel_type = st.selectbox("식품유형 필터", types_in)
                    show_df  = (
                        df if sel_type == "전체"
                        else df[df["식품유형"] == sel_type]
                    )
                    sc = ["제품명", "식품유형", "제조사", "보고일자", "유통기한"]
                    sc = [c for c in sc if c in show_df.columns]
                    st.dataframe(
                        show_df[sc].reset_index(drop=True),
                        use_container_width=True, height=500,
                    )

                with tab2:
                    st.markdown("### 📊 식품유형별 비교 분석")
                    ch1, ch2 = st.columns(2)

                    with ch1:
                        tc  = df["식품유형"].value_counts()
                        fig = px.bar(
                            x=tc.index, y=tc.values,
                            title="식품유형별 조회 건수",
                            labels={"x": "식품유형", "y": "건수"},
                            color=tc.index,
                        )
                        fig.update_layout(height=400, showlegend=False)
                        st.plotly_chart(fig, use_container_width=True)

                    with ch2:
                        if "제조사" in df.columns:
                            mt = (
                                df.groupby("식품유형")["제조사"]
                                .nunique().reset_index()
                            )
                            mt.columns = ["식품유형", "제조사수"]
                            fig2 = px.bar(
                                mt, x="식품유형", y="제조사수",
                                title="유형별 제조사 다양성",
                                color="식품유형",
                            )
                            fig2.update_layout(height=400, showlegend=False)
                            st.plotly_chart(fig2, use_container_width=True)

                    st.markdown("#### 🏢 유형별 상위 제조사")
                    for ft in selected_types:
                        ft_df = df[df["식품유형"] == ft]
                        if not ft_df.empty and "제조사" in ft_df.columns:
                            top = ft_df["제조사"].value_counts().head(5)
                            with st.expander(
                                f"**{ft}** — 상위 제조사 (총 {len(ft_df)}건)"
                            ):
                                for rank, (maker, cnt) in enumerate(top.items(), 1):
                                    st.markdown(f"{rank}. **{maker}** — {cnt}건")

                with tab3:
                    st.dataframe(df, use_container_width=True, height=500)
                    csv = df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 CSV 다운로드", csv,
                        f"품목제조보고_비교_{datetime.now().strftime('%Y%m%d')}.csv",
                        "text/csv", use_container_width=True,
                    )

# ━━━━ 초기 안내 ━━━━
else:
    st.info("👈 왼쪽 사이드바에서 식품유형을 선택하고 **[조회 실행]** 버튼을 누르세요.")

    st.markdown("""
### ⚡ 속도 개선 구조

| 단계 | 방식 | 예상 소요 |
|---|---|---|
| **1순위** | 포털 내부 Ajax API (서버사이드 필터) | **1~3초** |
| **2순위** | I1250 병렬 페이지네이션 (동시 5개) | 10~20초 |
| ~~기존~~ | ~~I1250 순차 페이지네이션~~ | ~~60~120초~~ |

### 포털 내부 API가 작동하지 않는다면

브라우저에서 실제 Ajax URL을 확인해서 `PORTAL_URLS` 리스트에 추가하세요:

```
1. 크롬에서 아래 URL 접속
   https://www.foodsafetykorea.go.kr/portal/specialinfo/searchInfoProduct.do
2. F12 → Network 탭 → XHR/Fetch 필터
3. 검색 실행 후 나타나는 .do 요청 클릭
4. Request URL 복사 → PORTAL_URLS에 추가
5. Headers / Payload 탭에서 파라미터명 확인 → param_variants에 추가
```

### 주의사항

> ⚠️ **가운뎃점** `·` (U+00B7) vs 마침표 `.` 구분
> 식품안전나라 DB는 `과·채주스` (가운뎃점) 표기를 사용합니다.
""")
