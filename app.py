# -*- coding: utf-8 -*-
"""
🏆 주식 수익률 데스매치
- 국내주식: 네이버 실시간 폴링 시세(polling.finance.naver.com)로 NXT(애프터/프리마켓)까지 반영
- 미국주식: yfinance prepost=True 로 프리장/애프터장 최신가 반영
- 갱신할 때마다 순위 스냅샷을 CSV에 저장 → 시간에 따른 '순위 추이(레이스)' 그래프 표시
- 값이 이상하면 하단 '🔧 가격 진단' 패널에서 소스별 원본값 확인 가능
"""

import os
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import pytz
import requests
import streamlit as st
import yfinance as yf

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

COLOR_UP = "#E11D48"     # 한국 관례: 상승=빨강
COLOR_DOWN = "#2563EB"   # 하락=파랑
GOLD, SILVER, BRONZE = "#F59E0B", "#94A3B8", "#B45309"

IDENTITY_PALETTE = [
    "#E11D48", "#2563EB", "#059669", "#D97706",
    "#7C3AED", "#DB2777", "#0891B2", "#65A30D",
]

PARTICIPANTS = [
    {"name": "송재준", "stock_name": "SPCF",        "ticker": "SPCF",   "buy_price": 30.15},
    {"name": "공상민", "stock_name": "LG씨엔에스",   "ticker": "064400", "buy_price": 89900},
    {"name": "변우진", "stock_name": "한국금융지주", "ticker": "071050", "buy_price": 221500},
    {"name": "오호근", "stock_name": "엔비디아",     "ticker": "NVDA",   "buy_price": 209.63},
    {"name": "박범휘", "stock_name": "한미반도체",   "ticker": "042700", "buy_price": 300500},
]

NAVER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://finance.naver.com/",
}


# ============================================================
# 유틸
# ============================================================
def is_us_stock(ticker: str) -> bool:
    base = str(ticker).split(".")[0]
    return any(c.isalpha() for c in base)


def color_map_for(names):
    return {name: IDENTITY_PALETTE[i % len(IDENTITY_PALETTE)] for i, name in enumerate(names)}


def _to_num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def kr_session_label(now_kst: datetime) -> str:
    minutes = now_kst.hour * 60 + now_kst.minute
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
# 가격 수집 — 국내 (네이버 /basic + overMarketPriceInfo = NXT 반영)
# ============================================================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_kr_price(code: str):
    """
    네이버 m.stock.naver.com /basic 사용.
    - 정규장(09:00~15:30): closePrice = 실시간 현재가
    - 장 외(프리 08:00~09:00 / 애프터 15:30~20:00): overMarketPriceInfo.overPrice = NXT 체결가
      (overMarketStatus == 'OPEN' 일 때만 NXT가로 전환)
    반환: (price, session, currency, diag)
    """
    code = str(code).split(".")[0]
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    b = requests.get(url, headers=NAVER_HEADERS, timeout=6).json()

    close_price = _to_num(b.get("closePrice"))          # KRX 현재가/종가
    over = b.get("overMarketPriceInfo") or {}
    over_price = _to_num(over.get("overPrice"))          # NXT 프리/애프터 체결가
    over_status = over.get("overMarketStatus")           # OPEN / CLOSE
    session_type = over.get("tradingSessionType")        # PRE_MARKET / AFTER_MARKET

    now = datetime.now(KST)
    in_regular = 9 * 60 <= (now.hour * 60 + now.minute) < 15 * 60 + 30

    if (not in_regular) and over_status == "OPEN" and over_price:
        price = over_price
        if session_type == "PRE_MARKET":
            session = "NXT 프리"
        elif session_type == "AFTER_MARKET":
            session = "NXT 애프터"
        else:
            session = "NXT"
        picked = "overMarketPriceInfo.overPrice"
    else:
        price = close_price
        session = kr_session_label(now)
        picked = "closePrice"

    if price is None:
        raise RuntimeError("KR price unavailable")

    diag = {
        "code": code, "picked": picked,
        "closePrice (KRX)": b.get("closePrice"),
        "overPrice (NXT)": over.get("overPrice"),
        "overMarketStatus": over_status,
        "tradingSessionType": session_type,
        "localTradedAt": over.get("localTradedAt") or b.get("localTradedAt"),
        "marketStatus": b.get("marketStatus"),
    }
    return price, session, "KRW", diag


# ============================================================
# 가격 수집 — 미국 (yfinance prepost)
# ============================================================
@st.cache_data(ttl=30, show_spinner=False)
def fetch_us_price(ticker: str):
    """prepost 포함 1분봉의 마지막 유효 체결가. 실패 시 일봉 종가로 폴백."""
    t = yf.Ticker(ticker)
    diag = {"ticker": ticker, "sources": {}}
    try:
        hist = t.history(period="2d", interval="1m", prepost=True)
        closes = hist["Close"].dropna()
        if len(closes) > 0:
            price = float(closes.iloc[-1])
            last_ts = closes.index[-1]
            diag["sources"]["prepost_1m"] = {
                "price": round(price, 4),
                "last_bar": str(last_ts),
                "bars": int(len(closes)),
            }
            diag["picked"] = "prepost_1m"
            return price, us_session_label(datetime.now(ET)), "USD", diag
    except Exception as e:
        diag["sources"]["prepost_err"] = str(e)

    daily = t.history(period="5d")["Close"].dropna()
    if len(daily) > 0:
        price = float(daily.iloc[-1])
        diag["sources"]["daily"] = {"price": round(price, 4), "last_bar": str(daily.index[-1])}
        diag["picked"] = "daily_fallback"
        return price, "장마감", "USD", diag
    raise RuntimeError("US price unavailable")


def get_price(participant):
    """반환: (price, session, currency, diag) — 실패 시 price=None."""
    ticker = str(participant["ticker"]).strip()
    try:
        if is_us_stock(ticker):
            return fetch_us_price(ticker)
        return fetch_kr_price(ticker)
    except Exception as e:
        return None, None, None, {"error": str(e)}


# ============================================================
# 결과 계산 / 순위
# ============================================================
def build_results(participants, price_lookup):
    """반환: (정렬·순위 부여된 DataFrame, diag_map[name]=diag)"""
    rows, diag_map = [], {}
    for p in participants:
        price, session, cur, diag = price_lookup(p)
        diag_map[p["name"]] = diag
        if price is not None:
            roi = (price - p["buy_price"]) / p["buy_price"] * 100
            rows.append({"참가자": p["name"], "종목명": p["stock_name"],
                         "매수단가": p["buy_price"], "현재가": price,
                         "통화": cur, "세션": session, "수익률": roi, "유효": True})
        else:
            rows.append({"참가자": p["name"], "종목명": p["stock_name"],
                         "매수단가": p["buy_price"], "현재가": None,
                         "통화": None, "세션": "조회실패", "수익률": None, "유효": False})

    df = pd.DataFrame(rows)
    df = df.sort_values(by=["유효", "수익률"], ascending=[False, False]).reset_index(drop=True)
    df["순위"] = [i + 1 if df.loc[i, "유효"] else None for i in range(len(df))]
    return df, diag_map


# ============================================================
# 순위 기록 저장소 — Supabase(무료 Postgres) 우선, 없으면 로컬 CSV 폴백
# ============================================================
TABLE = "ranking_history"


def _supabase():
    """secrets 에 SUPABASE_URL / SUPABASE_KEY 가 있으면 (base_url, headers) 반환, 없으면 None."""
    try:
        url = str(st.secrets["SUPABASE_URL"]).rstrip("/")
        key = str(st.secrets["SUPABASE_KEY"])
        if not url or not key:
            return None
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        return f"{url}/rest/v1/{TABLE}", headers
    except Exception:
        return None


def storage_mode():
    return "supabase" if _supabase() else "csv"


def _empty_history():
    return pd.DataFrame(columns=["timestamp", "name", "roi", "rank"])


def load_history():
    sb = _supabase()
    if sb:
        base, headers = sb
        try:
            r = requests.get(
                f"{base}?select=ts,name,roi,rank&order=ts.asc",
                headers=headers, timeout=8,
            )
            r.raise_for_status()
            rows = r.json()
            if not rows:
                return _empty_history()
            df = pd.DataFrame(rows).rename(columns={"ts": "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            return df[["timestamp", "name", "roi", "rank"]]
        except Exception:
            return _empty_history()
    # 폴백: 로컬 CSV
    if os.path.exists(HISTORY_FILE):
        try:
            return pd.read_csv(HISTORY_FILE, parse_dates=["timestamp"])
        except Exception:
            pass
    return _empty_history()


def append_snapshot(df_results, min_gap_minutes=5, now=None):
    now = now or datetime.now(KST).replace(tzinfo=None)
    hist = load_history()
    if not hist.empty:
        last = pd.to_datetime(hist["timestamp"]).max()
        if (now - last).total_seconds() < min_gap_minutes * 60:
            return hist  # 너무 이르면 적재 안 함

    valid = df_results[df_results["유효"]]
    if valid.empty:
        return hist

    ts_str = now.strftime("%Y-%m-%d %H:%M:%S")
    new_rows = [
        {"ts": ts_str, "name": r["참가자"],
         "roi": round(float(r["수익률"]), 4), "rank": int(r["순위"])}
        for _, r in valid.iterrows()
    ]

    sb = _supabase()
    if sb:
        base, headers = sb
        try:
            requests.post(base, headers={**headers, "Prefer": "return=minimal"},
                          json=new_rows, timeout=8).raise_for_status()
        except Exception:
            pass  # 저장 실패해도 화면은 계속 동작
        return load_history()

    # 폴백: 로컬 CSV
    new = pd.DataFrame([{"timestamp": now, "name": x["name"],
                         "roi": x["roi"], "rank": x["rank"]} for x in new_rows])
    out = pd.concat([hist, new], ignore_index=True)
    out.to_csv(HISTORY_FILE, index=False)
    return out


def clear_history():
    sb = _supabase()
    if sb:
        base, headers = sb
        try:
            # PostgREST 는 삭제 시 필터 필수 → 모든 행(id >= 0) 삭제
            requests.delete(f"{base}?id=gte.0", headers=headers, timeout=8).raise_for_status()
            return True
        except Exception:
            return False
    if os.path.exists(HISTORY_FILE):
        os.remove(HISTORY_FILE)
    return True


# ============================================================
# 그래프
# ============================================================
PLOT_FONT = "Noto Sans KR, sans-serif"


def build_rank_race_figure(history_df, color_map):
    fig = go.Figure()
    hist = history_df.copy()
    hist["timestamp"] = pd.to_datetime(hist["timestamp"])
    max_rank = int(hist["rank"].max()) if not hist.empty else len(color_map)
    for name in color_map:
        sub = hist[hist["name"] == name].sort_values("timestamp")
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["timestamp"], y=sub["rank"], mode="lines+markers", name=name,
            line=dict(color=color_map[name], width=3, shape="spline", smoothing=0.6),
            marker=dict(size=8, color=color_map[name], line=dict(color="white", width=1.5)),
            hovertemplate=f"<b>{name}</b><br>%{{x|%m/%d %H:%M}}<br>%{{y}}위<extra></extra>",
        ))
        last = sub.iloc[-1]
        fig.add_annotation(x=last["timestamp"], y=last["rank"], text=f"  {name}",
                           showarrow=False, xanchor="left", yanchor="middle",
                           font=dict(color=color_map[name], size=12, family=PLOT_FONT))
    fig.update_layout(
        height=420, font=dict(family=PLOT_FONT, color="#475569"),
        margin=dict(t=10, b=20, l=20, r=70),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False, hovermode="closest",
        xaxis=dict(gridcolor="#EEF2F6", showline=False, title=None),
        yaxis=dict(autorange="reversed", dtick=1, gridcolor="#EEF2F6",
                   title="순위", range=[max_rank + 0.5, 0.5], ticksuffix="위"),
    )
    return fig


def build_roi_bar_figure(df_results):
    valid = df_results[df_results["유효"]].copy()
    colors = [COLOR_UP if v >= 0 else COLOR_DOWN for v in valid["수익률"]]
    texts = [f"{v:+.2f}%" for v in valid["수익률"]]
    fig = go.Figure(go.Bar(
        x=valid["참가자"], y=valid["수익률"],
        marker=dict(color=colors), text=texts, textposition="outside",
        textfont=dict(family=PLOT_FONT, size=13), cliponaxis=False,
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
.stApp { background:
   radial-gradient(1200px 500px at 0% -10%, #EEF2FF 0%, transparent 55%),
   radial-gradient(1000px 500px at 100% -10%, #FFF1F2 0%, transparent 55%),
   #F8FAFC !important; }
.block-container { padding-top: 2rem; max-width: 880px; }
.hero { background: linear-gradient(135deg, #0B1220 0%, #1E293B 55%, #312E81 100%);
   border-radius: 22px; padding: 1.8rem 2rem; margin-bottom: 1.4rem;
   box-shadow: 0 18px 40px -12px rgba(30,41,59,.45); position: relative; overflow: hidden; }
.hero::after{ content:""; position:absolute; right:-60px; top:-60px; width:200px; height:200px;
   border-radius:50%; background: radial-gradient(circle, rgba(225,29,72,.35), transparent 70%); }
.hero .eyebrow{ color:#A5B4FC; font-size:.8rem; letter-spacing:.18em; font-weight:700;
   text-transform:uppercase; margin:0 0 .35rem 0; }
.hero h1{ font-family:'Black Han Sans', sans-serif !important; color:#fff !important;
   font-size:2.5rem !important; line-height:1.05; margin:0 !important; letter-spacing:-.01em; }
.hero .sub{ color:#CBD5E1; margin:.55rem 0 0 0; font-size:.92rem; }
.live{ display:inline-flex; align-items:center; gap:.4rem; background:rgba(16,185,129,.14);
   color:#34D399; border:1px solid rgba(52,211,153,.35); padding:.2rem .6rem;
   border-radius:999px; font-size:.78rem; font-weight:700; }
.live .dot{ width:7px; height:7px; border-radius:50%; background:#34D399; animation:pulse 1.6s infinite; }
@keyframes pulse{ 0%{box-shadow:0 0 0 0 rgba(52,211,153,.6);} 70%{box-shadow:0 0 0 8px rgba(52,211,153,0);}
   100%{box-shadow:0 0 0 0 rgba(52,211,153,0);} }
.sec-title{ font-weight:800; font-size:1.05rem; color:#0F172A; margin:1.6rem 0 .7rem 0;
   display:flex; align-items:center; gap:.5rem; }
.sec-title .bar{ width:4px; height:18px; border-radius:2px; background:#4F46E5; display:inline-block; }
.podium{ display:flex; gap:.8rem; }
.pod{ flex:1; background:#fff; border:1px solid #E5E9F0; border-radius:18px; padding:1.1rem .8rem;
   text-align:center; box-shadow:0 6px 16px -10px rgba(15,23,42,.25); }
.pod .medal{ font-size:1.7rem; line-height:1; }
.pod .nm{ font-weight:800; color:#0F172A; margin:.45rem 0 .1rem; font-size:1.02rem; }
.pod .stk{ color:#94A3B8; font-size:.78rem; }
.pod .roi{ font-family:'JetBrains Mono', monospace; font-weight:800; font-size:1.35rem; margin-top:.5rem; }
.pod.first{ border-color:#FCD34D; box-shadow:0 10px 24px -10px rgba(245,158,11,.45); transform:translateY(-6px); }
.pod.first .medal{ font-size:2rem; }
.row{ display:flex; align-items:center; gap:.9rem; background:#fff; border:1px solid #E5E9F0;
   border-radius:14px; padding:.75rem 1rem; margin-bottom:.55rem; box-shadow:0 3px 10px -8px rgba(15,23,42,.2); }
.row .rk{ font-family:'JetBrains Mono', monospace; font-weight:800; font-size:1.05rem; color:#94A3B8; width:26px; text-align:center; }
.row .dot{ width:10px; height:10px; border-radius:50%; flex:0 0 auto; }
.row .who{ flex:1; min-width:0; }
.row .who .nm{ font-weight:700; color:#0F172A; }
.row .who .meta{ color:#94A3B8; font-size:.78rem; margin-top:1px; }
.row .badge{ font-size:.68rem; font-weight:700; padding:.12rem .45rem; border-radius:6px;
   background:#EEF2F6; color:#64748B; margin-left:.4rem; vertical-align:middle; }
.row .pr{ font-family:'JetBrains Mono', monospace; font-weight:800; font-size:1.05rem; white-space:nowrap; }
.up{ color:#E11D48 !important; } .down{ color:#2563EB !important; } .flat{ color:#64748B !important; }
.stButton>button{ border-radius:12px; border:1px solid #E5E9F0; font-weight:700; background:#fff; color:#0F172A; }
.stButton>button:hover{ border-color:#4F46E5; color:#4F46E5; }
</style>
"""


# ============================================================
# 메인
# ============================================================
def main():
    st.set_page_config(page_title="🏆 주식 수익률 데스매치", page_icon="🏆", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    color_map = color_map_for([p["name"] for p in PARTICIPANTS])

    with st.sidebar:
        st.markdown("### ⚙️ 설정")
        auto = st.toggle("자동 새로고침", value=False)
        interval = st.select_slider("새로고침 간격", options=[30, 60, 120, 300],
                                    value=60, format_func=lambda s: f"{s}초")
        gap = st.select_slider("기록 간격(분)", options=[1, 3, 5, 10, 15, 30], value=5)
        if not HAS_AUTOREFRESH and auto:
            st.caption("⚠️ 자동 새로고침엔 `streamlit-autorefresh` 패키지가 필요해요.")
        st.divider()
        mode = storage_mode()
        st.caption("☁️ Supabase에 기록 저장 중" if mode == "supabase"
                   else "💾 로컬 CSV에 기록 저장 중 (Supabase 미설정)")
        if st.button("🗑️ 순위 기록 초기화", use_container_width=True):
            st.success("기록을 초기화했어요." if clear_history() else "초기화에 실패했어요.")

    if auto and HAS_AUTOREFRESH:
        st_autorefresh(interval=interval * 1000, key="auto_refresh")

    with st.spinner("실시간 가격을 불러오는 중… (NXT / 프리·애프터마켓 포함) 🚀"):
        df, diag_map = build_results(PARTICIPANTS, get_price)

    history = append_snapshot(df, min_gap_minutes=gap)
    now_kst = datetime.now(KST)
    leader = df[df["유효"]].iloc[0] if df["유효"].any() else None

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
        """, unsafe_allow_html=True)

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("🔄 지금 새로고침", use_container_width=True):
            fetch_kr_price.clear()
            fetch_us_price.clear()
            st.rerun()
    with c2:
        n_ok = len(df[df["유효"]])
        st.caption(f"📍 {n_ok}/{len(df)}명 조회 성공 · 기록 {len(history)//max(n_ok,1)}회차")

    valid = df[df["유효"]].reset_index(drop=True)
    if len(valid) >= 1:
        st.markdown('<div class="sec-title"><span class="bar"></span>🏅 시상대</div>', unsafe_allow_html=True)
        medals, classes, cards = ["🥇", "🥈", "🥉"], ["first", "", ""], []
        for i in range(min(3, len(valid))):
            r = valid.loc[i]
            sc = "up" if r["수익률"] >= 0 else "down"
            cards.append(f'<div class="pod {classes[i]}"><div class="medal">{medals[i]}</div>'
                         f'<div class="nm">{r["참가자"]}</div><div class="stk">{r["종목명"]}</div>'
                         f'<div class="roi {sc}">{r["수익률"]:+.2f}%</div></div>')
        st.markdown(f'<div class="podium">{"".join(cards)}</div>', unsafe_allow_html=True)

    st.markdown('<div class="sec-title"><span class="bar"></span>📈 순위 추이</div>', unsafe_allow_html=True)
    distinct_times = pd.to_datetime(history["timestamp"]).nunique() if not history.empty else 0
    if distinct_times >= 2:
        st.plotly_chart(build_rank_race_figure(history, color_map),
                        use_container_width=True, config={"displayModeBar": False})
    else:
        st.info("기록이 2회 이상 쌓이면 시간에 따른 순위 변화가 그래프로 나타나요. ⏱️")

    st.markdown('<div class="sec-title"><span class="bar"></span>📊 현재 수익률</div>', unsafe_allow_html=True)
    if len(valid) >= 1:
        st.plotly_chart(build_roi_bar_figure(df),
                        use_container_width=True, config={"displayModeBar": False})

    st.markdown('<div class="sec-title"><span class="bar"></span>🔥 전체 랭킹</div>', unsafe_allow_html=True)
    for _, r in df.iterrows():
        if r["유효"]:
            sc = "up" if r["수익률"] > 0 else ("down" if r["수익률"] < 0 else "flat")
            sign = "+" if r["수익률"] >= 0 else ""
            rk, roi_txt = f"{int(r['순위'])}", f"{sign}{r['수익률']:.2f}%"
            price_txt = (f"매수 {fmt_price(r['매수단가'], r['통화'])} → "
                         f"현재 {fmt_price(r['현재가'], r['통화'])}")
            badge = f'<span class="badge">{r["세션"]}</span>'
        else:
            sc, roi_txt, rk = "flat", "조회실패", "–"
            price_txt, badge = "가격을 불러오지 못했어요", '<span class="badge">N/A</span>'
        dot = color_map.get(r["참가자"], "#CBD5E1")
        st.markdown(
            f'<div class="row"><div class="rk">{rk}</div>'
            f'<div class="dot" style="background:{dot}"></div>'
            f'<div class="who"><div class="nm">{r["참가자"]} {badge}</div>'
            f'<div class="meta">{r["종목명"]} · {price_txt}</div></div>'
            f'<div class="pr {sc}">{roi_txt}</div></div>', unsafe_allow_html=True)

    if distinct_times >= 2:
        with st.expander("📉 수익률 추이 그래프 보기"):
            st.plotly_chart(build_roi_trend_figure(history, color_map),
                            use_container_width=True, config={"displayModeBar": False})

    # ── 가격 진단: 소스별 원본값 확인 ──
    with st.expander("🔧 가격 진단 (값이 이상하면 열어보세요)"):
        st.caption("각 종목을 어떤 소스의 어떤 값으로 채웠는지 보여줘요. "
                   "실제 시세와 다르면 이 내용을 캡처해 알려주시면 정확히 맞출 수 있어요.")
        for p in PARTICIPANTS:
            d = diag_map.get(p["name"], {})
            st.markdown(f"**{p['name']} · {p['stock_name']} ({p['ticker']})** "
                        f"— picked: `{d.get('picked', d.get('error', '—'))}`")
            st.json(d, expanded=False)
        st.caption(f"저장소: {'☁️ Supabase' if storage_mode()=='supabase' else '💾 로컬 CSV'} · 총 {len(history)}행")


if __name__ == "__main__":
    main()
