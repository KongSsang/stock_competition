# -*- coding: utf-8 -*-
"""
🏆 주식 수익률 데스매치
- 국내주식: 네이버 모바일 API로 NXT 통합 현재가(프리/메인/애프터마켓)까지 반영
- 미국주식: yfinance prepost=True 로 프리장/애프터장 최신가 반영
- 갱신할 때마다 순위 스냅샷을 CSV에 저장 → 시간에 따른 '순위 추이(레이스)' 그래프 표시
"""

import os
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import pytz
import requests
import streamlit as st
import yfinance as yf

# 자동 새로고침은 선택적 의존성 (없어도 수동 새로고침으로 동작)
try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except Exception:
    HAS_AUTOREFRESH = False

# ============================================================
# 상수 / 설정
# ============================================================
KST = pytz.timezone("Asia/Seoul")
ET = pytz.timezone("America/New_York")
HISTORY_FILE = "ranking_history.csv"

# 한국 색 관례: 상승=빨강, 하락=파랑
COLOR_UP = "#E11D48"
COLOR_DOWN = "#2563EB"
GOLD, SILVER, BRONZE = "#F59E0B", "#94A3B8", "#B45309"

# 참가자별 고정 색 (순위 추이 그래프 / 리스트에서 신원 식별용)
IDENTITY_PALETTE = [
    "#E11D48", "#2563EB", "#059669", "#D97706",
    "#7C3AED", "#DB2777", "#0891B2", "#65A30D",
]

# ── 참가자 정보 ──────────────────────────────────────────────
PARTICIPANTS = [
    {"name": "송재준", "stock_name": "SPCF",        "ticker": "SPCF",   "buy_price": 30.15},
    {"name": "공상민", "stock_name": "LG씨엔에스",   "ticker": "064400", "buy_price": 89900},
    {"name": "변우진", "stock_name": "한국금융지주", "ticker": "071050", "buy_price": 221500},
    {"name": "오호근", "stock_name": "엔비디아",     "ticker": "NVDA",   "buy_price": 209.63},
    {"name": "박범휘", "stock_name": "한미반도체",   "ticker": "042700", "buy_price": 300500},
]


# ============================================================
# 유틸: 종목 구분 / 색
# ============================================================
def is_us_stock(ticker: str) -> bool:
    base = str(ticker).split(".")[0]
    return any(c.isalpha() for c in base)


def color_map_for(names):
    return {name: IDENTITY_PALETTE[i % len(IDENTITY_PALETTE)] for i, name in enumerate(names)}


# ============================================================
# 장 세션 라벨 (시각 기준)
# ============================================================
def kr_session_label(now_kst: datetime) -> str:
    """국내 장 세션 라벨. 가격 자체는 네이버가 통합 최신가를 주므로 라벨만 시간으로 판단."""
    h, m = now_kst.hour, now_kst.minute
    minutes = h * 60 + m
    if 9 * 60 <= minutes < 15 * 60 + 30:
        return "정규장"
    if 8 * 60 <= minutes < 9 * 60:
        return "NXT 프리"
    if 15 * 60 + 30 <= minutes < 20 * 60:
        return "NXT 애프터"
    return "장마감"


def us_session_label(now_et: datetime) -> str:
    minutes = now_et.hour * 60 + now_et.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "정규장"
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "프리마켓"
    if 16 * 60 <= minutes < 20 * 60:
        return "애프터마켓"
    return "장마감"


# ============================================================
# 가격 수집
# ============================================================
@st.cache_data(ttl=60, show_spinner=False)
def fetch_kr_price(code: str):
    """
    네이버 모바일 API의 통합 현재가(closePrice)를 사용.
    NXT 통합 종목은 정규장/프리/애프터마켓 최신 체결가가 여기에 반영됨.
    반환: (price, session_label, currency, raw_dict)
    """
    code = str(code).split(".")[0]
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Referer": "https://m.stock.naver.com/",
    }
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    res = requests.get(url, headers=headers, timeout=6)
    res.raise_for_status()
    data = res.json()

    price = float(str(data.get("closePrice", "")).replace(",", ""))
    session = kr_session_label(datetime.now(KST))
    return price, session, "KRW", data


@st.cache_data(ttl=60, show_spinner=False)
def fetch_us_price(ticker: str):
    """yfinance prepost 포함 1분봉의 마지막 유효 체결가. 실패 시 일봉 종가로 폴백."""
    t = yf.Ticker(ticker)
    try:
        hist = t.history(period="2d", interval="1m", prepost=True)
        closes = hist["Close"].dropna()
        if len(closes) > 0:
            price = float(closes.iloc[-1])
            session = us_session_label(datetime.now(ET))
            return price, session, "USD", None
    except Exception:
        pass
    # 폴백: 최근 일봉 종가
    daily = t.history(period="5d")["Close"].dropna()
    if len(daily) > 0:
        return float(daily.iloc[-1]), "장마감", "USD", None
    raise RuntimeError("price unavailable")


def get_price(participant):
    """참가자 1명의 (price, session, currency)를 반환. 실패 시 None."""
    ticker = participant["ticker"]
    try:
        if is_us_stock(ticker):
            price, session, cur, _ = fetch_us_price(str(ticker).strip())
        else:
            price, session, cur, _ = fetch_kr_price(str(ticker).strip())
        return price, session, cur
    except Exception:
        return None, None, None


# ============================================================
# 결과 계산 / 순위
# ============================================================
def build_results(participants, price_lookup):
    """
    price_lookup(participant) -> (price, session, currency) | (None, ...)
    수익률 계산 후 내림차순 정렬 + 순위(rank) 부여한 DataFrame 반환.
    """
    rows = []
    for p in participants:
        price, session, cur = price_lookup(p)
        if price is not None:
            roi = (price - p["buy_price"]) / p["buy_price"] * 100
            rows.append({
                "참가자": p["name"], "종목명": p["stock_name"],
                "매수단가": p["buy_price"], "현재가": price,
                "통화": cur, "세션": session, "수익률": roi, "유효": True,
            })
        else:
            rows.append({
                "참가자": p["name"], "종목명": p["stock_name"],
                "매수단가": p["buy_price"], "현재가": None,
                "통화": None, "세션": "조회실패", "수익률": None, "유효": False,
            })

    df = pd.DataFrame(rows)
    # 유효 행만 먼저, 수익률 내림차순. 실패 행은 맨 뒤로.
    df = df.sort_values(by=["유효", "수익률"], ascending=[False, False]).reset_index(drop=True)
    df["순위"] = [i + 1 if df.loc[i, "유효"] else None for i in range(len(df))]
    return df


# ============================================================
# 순위 기록 (CSV 영속화)
# ============================================================
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            return pd.read_csv(HISTORY_FILE, parse_dates=["timestamp"])
        except Exception:
            return pd.DataFrame(columns=["timestamp", "name", "roi", "rank"])
    return pd.DataFrame(columns=["timestamp", "name", "roi", "rank"])


def append_snapshot(df_results, min_gap_minutes=5, now=None):
    """유효한 참가자들의 (시각, 이름, 수익률, 순위) 스냅샷을 기록.
    직전 기록과 min_gap_minutes 이내면 건너뜀(과도한 적재 방지)."""
    now = now or datetime.now(KST).replace(tzinfo=None)
    hist = load_history()

    if not hist.empty:
        last = pd.to_datetime(hist["timestamp"]).max()
        if (now - last).total_seconds() < min_gap_minutes * 60:
            return hist  # 너무 이르면 적재 안 함

    valid = df_results[df_results["유효"]]
    if valid.empty:
        return hist

    new = pd.DataFrame({
        "timestamp": now,
        "name": valid["참가자"].values,
        "roi": valid["수익률"].values.round(4),
        "rank": valid["순위"].values,
    })
    out = pd.concat([hist, new], ignore_index=True)
    out.to_csv(HISTORY_FILE, index=False)
    return out


# ============================================================
# 그래프
# ============================================================
PLOT_FONT = "Noto Sans KR, sans-serif"


def build_rank_race_figure(history_df, color_map):
    """순위 추이(레이스): x=시간, y=순위(1위가 위). 사람별 라인."""
    fig = go.Figure()
    names = list(color_map.keys())
    hist = history_df.copy()
    hist["timestamp"] = pd.to_datetime(hist["timestamp"])

    max_rank = int(hist["rank"].max()) if not hist.empty else len(names)

    for name in names:
        sub = hist[hist["name"] == name].sort_values("timestamp")
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["timestamp"], y=sub["rank"],
            mode="lines+markers", name=name,
            line=dict(color=color_map[name], width=3, shape="spline", smoothing=0.6),
            marker=dict(size=8, color=color_map[name], line=dict(color="white", width=1.5)),
            hovertemplate=f"<b>{name}</b><br>%{{x|%m/%d %H:%M}}<br>%{{y}}위<extra></extra>",
        ))
        # 마지막 지점에 이름 라벨
        last = sub.iloc[-1]
        fig.add_annotation(
            x=last["timestamp"], y=last["rank"], text=f"  {name}",
            showarrow=False, xanchor="left", yanchor="middle",
            font=dict(color=color_map[name], size=12, family=PLOT_FONT),
        )

    fig.update_layout(
        height=420, font=dict(family=PLOT_FONT, color="#475569"),
        margin=dict(t=10, b=20, l=20, r=70),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False, hovermode="closest",
        xaxis=dict(gridcolor="#EEF2F6", showline=False, title=None),
        yaxis=dict(
            autorange="reversed", dtick=1, gridcolor="#EEF2F6",
            title="순위", range=[max_rank + 0.5, 0.5],
            tickprefix="", ticksuffix="위",
        ),
    )
    return fig


def build_roi_bar_figure(df_results):
    """현재 수익률 막대(상승=빨강, 하락=파랑)."""
    valid = df_results[df_results["유효"]].copy()
    colors = [COLOR_UP if v >= 0 else COLOR_DOWN for v in valid["수익률"]]
    texts = [f"{v:+.2f}%" for v in valid["수익률"]]

    fig = go.Figure(go.Bar(
        x=valid["참가자"], y=valid["수익률"],
        marker=dict(color=colors, line=dict(width=0)),
        text=texts, textposition="outside",
        textfont=dict(family=PLOT_FONT, size=13),
        cliponaxis=False,
        hovertemplate="<b>%{x}</b><br>%{y:.2f}%<extra></extra>",
    ))
    fig.update_layout(
        height=320, font=dict(family=PLOT_FONT, color="#475569"),
        margin=dict(t=30, b=10, l=20, r=20),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(showline=False, title=None),
        yaxis=dict(gridcolor="#EEF2F6", zeroline=True, zerolinecolor="#CBD5E1",
                   title="수익률 (%)", ticksuffix="%"),
    )
    return fig


def build_roi_trend_figure(history_df, color_map):
    """수익률 추이(보조 그래프)."""
    fig = go.Figure()
    hist = history_df.copy()
    hist["timestamp"] = pd.to_datetime(hist["timestamp"])
    for name, color in color_map.items():
        sub = hist[hist["name"] == name].sort_values("timestamp")
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["timestamp"], y=sub["roi"], mode="lines", name=name,
            line=dict(color=color, width=2.5, shape="spline", smoothing=0.5),
            hovertemplate=f"<b>{name}</b><br>%{{x|%m/%d %H:%M}}<br>%{{y:.2f}}%<extra></extra>",
        ))
    fig.update_layout(
        height=340, font=dict(family=PLOT_FONT, color="#475569"),
        margin=dict(t=10, b=20, l=20, r=20),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.2),
        xaxis=dict(gridcolor="#EEF2F6", title=None),
        yaxis=dict(gridcolor="#EEF2F6", zeroline=True, zerolinecolor="#CBD5E1",
                   ticksuffix="%", title=None),
    )
    return fig


# ============================================================
# 포맷 헬퍼
# ============================================================
def fmt_price(value, currency):
    if value is None:
        return "—"
    if currency == "USD":
        return f"${value:,.2f}"
    return f"₩{value:,.0f}"


# ============================================================
# CSS
# ============================================================
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=Noto+Sans+KR:wght@400;500;700;900&family=JetBrains+Mono:wght@600;800&display=swap');

html, body, .stApp, p, label, span, div { font-family: 'Noto Sans KR', sans-serif; }

.stApp {
    background:
      radial-gradient(1200px 500px at 0% -10%, #EEF2FF 0%, transparent 55%),
      radial-gradient(1000px 500px at 100% -10%, #FFF1F2 0%, transparent 55%),
      #F8FAFC !important;
}
.block-container { padding-top: 2rem; max-width: 880px; }

/* ── 히어로 ── */
.hero {
    background: linear-gradient(135deg, #0B1220 0%, #1E293B 55%, #312E81 100%);
    border-radius: 22px; padding: 1.8rem 2rem; margin-bottom: 1.4rem;
    box-shadow: 0 18px 40px -12px rgba(30,41,59,.45);
    position: relative; overflow: hidden;
}
.hero::after{
    content:""; position:absolute; right:-60px; top:-60px;
    width:200px; height:200px; border-radius:50%;
    background: radial-gradient(circle, rgba(225,29,72,.35), transparent 70%);
}
.hero .eyebrow{
    color:#A5B4FC; font-size:.8rem; letter-spacing:.18em; font-weight:700;
    text-transform:uppercase; margin:0 0 .35rem 0;
}
.hero h1{
    font-family:'Black Han Sans', sans-serif !important;
    color:#fff !important; font-size:2.5rem !important; line-height:1.05;
    margin:0 !important; letter-spacing:-.01em;
}
.hero .sub{ color:#CBD5E1; margin:.55rem 0 0 0; font-size:.92rem; }

.live{
    display:inline-flex; align-items:center; gap:.4rem;
    background:rgba(16,185,129,.14); color:#34D399;
    border:1px solid rgba(52,211,153,.35);
    padding:.2rem .6rem; border-radius:999px; font-size:.78rem; font-weight:700;
}
.live .dot{
    width:7px; height:7px; border-radius:50%; background:#34D399;
    box-shadow:0 0 0 0 rgba(52,211,153,.7); animation:pulse 1.6s infinite;
}
@keyframes pulse{
    0%{box-shadow:0 0 0 0 rgba(52,211,153,.6);}
    70%{box-shadow:0 0 0 8px rgba(52,211,153,0);}
    100%{box-shadow:0 0 0 0 rgba(52,211,153,0);}
}

/* ── 섹션 타이틀 ── */
.sec-title{
    font-weight:800; font-size:1.05rem; color:#0F172A;
    margin:1.6rem 0 .7rem 0; display:flex; align-items:center; gap:.5rem;
}
.sec-title .bar{ width:4px; height:18px; border-radius:2px; background:#4F46E5; display:inline-block; }

/* ── 시상대 ── */
.podium{ display:flex; gap:.8rem; }
.pod{
    flex:1; background:#fff; border:1px solid #E5E9F0; border-radius:18px;
    padding:1.1rem .8rem; text-align:center; box-shadow:0 6px 16px -10px rgba(15,23,42,.25);
    position:relative;
}
.pod .medal{ font-size:1.7rem; line-height:1; }
.pod .nm{ font-weight:800; color:#0F172A; margin:.45rem 0 .1rem; font-size:1.02rem; }
.pod .stk{ color:#94A3B8; font-size:.78rem; }
.pod .roi{ font-family:'JetBrains Mono', monospace; font-weight:800; font-size:1.35rem; margin-top:.5rem; }
.pod.first{ border-color:#FCD34D; box-shadow:0 10px 24px -10px rgba(245,158,11,.45); transform:translateY(-6px); }
.pod.first .medal{ font-size:2rem; }

/* ── 랭킹 리스트 ── */
.row{
    display:flex; align-items:center; gap:.9rem; background:#fff;
    border:1px solid #E5E9F0; border-radius:14px; padding:.75rem 1rem; margin-bottom:.55rem;
    box-shadow:0 3px 10px -8px rgba(15,23,42,.2);
}
.row .rk{ font-family:'JetBrains Mono', monospace; font-weight:800; font-size:1.05rem; color:#94A3B8; width:26px; text-align:center; }
.row .dot{ width:10px; height:10px; border-radius:50%; flex:0 0 auto; }
.row .who{ flex:1; min-width:0; }
.row .who .nm{ font-weight:700; color:#0F172A; }
.row .who .meta{ color:#94A3B8; font-size:.78rem; margin-top:1px; }
.row .badge{ font-size:.68rem; font-weight:700; padding:.12rem .45rem; border-radius:6px;
    background:#EEF2F6; color:#64748B; margin-left:.4rem; vertical-align:middle; }
.row .pr{ font-family:'JetBrains Mono', monospace; font-weight:800; font-size:1.05rem; white-space:nowrap; }
.up{ color:#E11D48 !important; }
.down{ color:#2563EB !important; }
.flat{ color:#64748B !important; }

/* Streamlit 버튼 톤 정리 */
.stButton>button{
    border-radius:12px; border:1px solid #E5E9F0; font-weight:700;
    background:#fff; color:#0F172A;
}
.stButton>button:hover{ border-color:#4F46E5; color:#4F46E5; }
</style>
"""


# ============================================================
# 메인 (Streamlit UI)
# ============================================================
def main():
    st.set_page_config(page_title="🏆 주식 수익률 데스매치", page_icon="🏆", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)

    color_map = color_map_for([p["name"] for p in PARTICIPANTS])

    # ── 사이드바: 컨트롤 ──
    with st.sidebar:
        st.markdown("### ⚙️ 설정")
        auto = st.toggle("자동 새로고침", value=False,
                         help="켜면 일정 간격으로 가격을 다시 불러옵니다.")
        interval = st.select_slider("새로고침 간격", options=[30, 60, 120, 300],
                                    value=60, format_func=lambda s: f"{s}초")
        gap = st.select_slider("기록 간격(분)", options=[1, 3, 5, 10, 15, 30], value=5,
                               help="이 간격보다 자주는 순위 기록을 남기지 않습니다.")
        if not HAS_AUTOREFRESH and auto:
            st.caption("⚠️ 자동 새로고침에는 `streamlit-autorefresh` 패키지가 필요해요.")
        st.divider()
        if st.button("🗑️ 순위 기록 초기화", use_container_width=True):
            if os.path.exists(HISTORY_FILE):
                os.remove(HISTORY_FILE)
            st.success("기록을 초기화했어요.")

    if auto and HAS_AUTOREFRESH:
        st_autorefresh(interval=interval * 1000, key="auto_refresh")

    # ── 데이터 수집 ──
    with st.spinner("실시간 가격을 불러오는 중… (NXT / 프리·애프터마켓 포함) 🚀"):
        df = build_results(PARTICIPANTS, get_price)

    history = append_snapshot(df, min_gap_minutes=gap)
    now_kst = datetime.now(KST)

    leader = df[df["유효"]].iloc[0] if df["유효"].any() else None

    # ── 히어로 ──
    st.markdown(
        f"""
        <div class="hero">
            <p class="eyebrow">WEEKLY RETURN · 친구들 주식 대결</p>
            <h1>주식 수익률 데스매치</h1>
            <p class="sub">
                <span class="live"><span class="dot"></span>LIVE</span>
                &nbsp; 업데이트 {now_kst.strftime('%m월 %d일 %H:%M:%S')} (KST)
                {'· 🥇 ' + leader['참가자'] + ' 선두' if leader is not None else ''}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("🔄 지금 새로고침", use_container_width=True):
            fetch_kr_price.clear()
            fetch_us_price.clear()
            st.rerun()
    with c2:
        st.caption(f"📍 {len(df[df['유효']])}/{len(df)}명 가격 조회 성공 · 기록 {len(history)//max(len(df[df['유효']]),1)}회차")

    # ── 시상대 (TOP 3) ──
    valid = df[df["유효"]].reset_index(drop=True)
    if len(valid) >= 1:
        st.markdown('<div class="sec-title"><span class="bar"></span>🏅 시상대</div>', unsafe_allow_html=True)
        medals = ["🥇", "🥈", "🥉"]
        classes = ["first", "", ""]
        cards = []
        for i in range(min(3, len(valid))):
            r = valid.loc[i]
            sign_cls = "up" if r["수익률"] >= 0 else "down"
            cards.append(f"""
                <div class="pod {classes[i]}">
                    <div class="medal">{medals[i]}</div>
                    <div class="nm">{r['참가자']}</div>
                    <div class="stk">{r['종목명']}</div>
                    <div class="roi {sign_cls}">{r['수익률']:+.2f}%</div>
                </div>""")
        st.markdown(f'<div class="podium">{"".join(cards)}</div>', unsafe_allow_html=True)

    # ── 순위 추이 (레이스) ── 핵심 그래프
    st.markdown('<div class="sec-title"><span class="bar"></span>📈 순위 추이</div>', unsafe_allow_html=True)
    distinct_times = pd.to_datetime(history["timestamp"]).nunique() if not history.empty else 0
    if distinct_times >= 2:
        st.plotly_chart(build_rank_race_figure(history, color_map),
                        use_container_width=True, config={"displayModeBar": False})
    else:
        st.info("기록이 2회 이상 쌓이면 시간에 따른 순위 변화가 그래프로 나타나요. "
                "새로고침을 반복하거나 자동 새로고침을 켜두세요. ⏱️")

    # ── 현재 수익률 막대 ──
    st.markdown('<div class="sec-title"><span class="bar"></span>📊 현재 수익률</div>', unsafe_allow_html=True)
    if len(valid) >= 1:
        st.plotly_chart(build_roi_bar_figure(df),
                        use_container_width=True, config={"displayModeBar": False})

    # ── 전체 랭킹 리스트 ──
    st.markdown('<div class="sec-title"><span class="bar"></span>🔥 전체 랭킹</div>', unsafe_allow_html=True)
    for _, r in df.iterrows():
        if r["유효"]:
            sign_cls = "up" if r["수익률"] > 0 else ("down" if r["수익률"] < 0 else "flat")
            sign = "+" if r["수익률"] >= 0 else ""
            rk = f"{int(r['순위'])}"
            roi_txt = f"{sign}{r['수익률']:.2f}%"
            price_txt = (f"매수 {fmt_price(r['매수단가'], r['통화'])} → "
                         f"현재 {fmt_price(r['현재가'], r['통화'])}")
            badge = f'<span class="badge">{r["세션"]}</span>'
        else:
            sign_cls, roi_txt, rk = "flat", "조회실패", "–"
            price_txt = "가격을 불러오지 못했어요"
            badge = '<span class="badge">N/A</span>'

        dot = color_map.get(r["참가자"], "#CBD5E1")
        st.markdown(
            f"""
            <div class="row">
                <div class="rk">{rk}</div>
                <div class="dot" style="background:{dot}"></div>
                <div class="who">
                    <div class="nm">{r['참가자']} {badge}</div>
                    <div class="meta">{r['종목명']} · {price_txt}</div>
                </div>
                <div class="pr {sign_cls}">{roi_txt}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── 보조: 수익률 추이 + 원본 데이터 ──
    if distinct_times >= 2:
        with st.expander("📉 수익률 추이 그래프 보기"):
            st.plotly_chart(build_roi_trend_figure(history, color_map),
                            use_container_width=True, config={"displayModeBar": False})

    with st.expander("🧾 기록 원본 / 진단"):
        st.caption(f"기록 파일: `{HISTORY_FILE}` · 총 {len(history)}행")
        if not history.empty:
            st.dataframe(history.sort_values("timestamp", ascending=False),
                         use_container_width=True, height=240)
        st.caption("국내가는 네이버 통합가(NXT 포함), 미국가는 yfinance prepost 기준입니다.")


if __name__ == "__main__":
    main()
