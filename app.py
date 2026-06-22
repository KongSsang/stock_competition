import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import plotly.express as px
from bs4 import BeautifulSoup
from datetime import datetime
import pytz

# ============================================================
# 페이지 기본 설정 & 글로벌 스타일
# ============================================================
st.set_page_config(
    page_title="🔥 일주일 주식 수익률 데스매치",
    page_icon="🏆",
    layout="centered"
)

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Gowun+Dodum&family=Noto+Sans+KR:wght@400;500;700&display=swap');

    html, body, .stApp, .stMarkdown, p, h1, h2, h3, h4, h5, h6,
    label, .stMetric, button {
        font-family: 'Gowun Dodum', 'Noto Sans KR', sans-serif !important;
    }

    .stApp {
        background: radial-gradient(circle at 0% 0%, #F8FAFC 0%, transparent 40%),
                    radial-gradient(circle at 100% 0%, #EFF6FF 0%, transparent 40%),
                    #F1F5F9 !important;
    }

    .hero-card {
        background: linear-gradient(135deg, #1E293B 0%, #334155 50%, #475569 100%);
        padding: 2rem 2.5rem;
        border-radius: 20px;
        color: white;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.15);
        margin-bottom: 2rem;
        text-align: center;
    }
    .hero-card h1 {
        color: white !important;
        font-size: 2.2rem !important;
        margin: 0 !important;
        font-weight: 700 !important;
    }
    .hero-card p {
        color: #CBD5E1 !important;
        margin: 0.5rem 0 0 0 !important;
        font-size: 1rem;
    }

    /* 카드 스타일 */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: white;
        border-radius: 16px !important;
        border: 1px solid #E2E8F0 !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
        padding: 0.5rem;
    }
    
    .rank-1 { color: #F59E0B; font-weight: bold; font-size: 1.2rem; } /* Gold */
    .rank-2 { color: #94A3B8; font-weight: bold; font-size: 1.1rem; } /* Silver */
    .rank-3 { color: #B45309; font-weight: bold; font-size: 1.1rem; } /* Bronze */
    
    .profit-up { color: #EF4444 !important; font-weight: 700; }
    .profit-down { color: #3B82F6 !important; font-weight: 700; }
</style>
""", unsafe_allow_html=True)

# ============================================================
# 유틸 함수 (주식 가격 수집 - 프리장/애프터장 통합)
# ============================================================
def is_us_stock(ticker):
    base_ticker = str(ticker).split('.')[0]
    return any(c.isalpha() for c in base_ticker)

@st.cache_data(ttl=60)
def fetch_realtime_price(ticker_symbol):
    ticker_str = str(ticker_symbol).strip()

    if is_us_stock(ticker_str):
        try:
            stock = yf.Ticker(ticker_str)
            # prepost=True 를 통해 프리장/애프터장 데이터를 모두 포함시킵니다.
            # 최근 5일(period)의 데이터를 1분 간격(interval)으로 가져와 가장 최신 값을 씁니다.
            hist = stock.history(period="5d", interval="1m", prepost=True)
            if not hist.empty:
                curr_price = float(hist['Close'].iloc[-1])
                return curr_price, "USD (Extended)"
            return None, None
        except Exception:
            return None, None
    else:
        try:
            code = ticker_str.split('.')[0]
            headers = {'User-Agent': 'Mozilla/5.0'}
            
            # 1. 정규장 종가 스크래핑
            url_main = f"https://finance.naver.com/item/main.naver?code={code}"
            res_main = requests.get(url_main, headers=headers, timeout=5)
            soup_main = BeautifulSoup(res_main.text, 'html.parser')
            price_tag = soup_main.select_one(".no_today .blind")
            regular_price = int(price_tag.text.replace(',', '')) if price_tag else None

            # 2. 시간외 단일가 가격 스크래핑
            url_after = f"https://finance.naver.com/item/sise_single.naver?code={code}"
            res_after = requests.get(url_after, headers=headers, timeout=5)
            soup_after = BeautifulSoup(res_after.text, 'html.parser')
            
            after_price = None
            rows = soup_after.select('table.type2 tr')
            for row in rows:
                time_td = row.select_one('td.time')
                if time_td and time_td.text.strip() != '':
                    price_td = row.select_one('td.num span')
                    if price_td:
                        after_str = price_td.text.replace(',', '').strip()
                        if after_str.isdigit():
                            after_price = int(after_str)
                            break # 가장 최근 시간외 체결가 1개만 파싱
            
            # 3. KST 시간 기준 로직 분기 (장중에는 정규장, 장마감 이후엔 시간외)
            kst_now = datetime.now(pytz.timezone('Asia/Seoul'))
            # 오후 16시(4시) 부터 다음날 아침 08시 30분 사이인지 확인
            is_after_hours = (kst_now.hour >= 16) or (kst_now.hour < 8) or (kst_now.hour == 8 and kst_now.minute <= 30)

            if is_after_hours and after_price is not None:
                return float(after_price), "KRW (시간외)"
            else:
                return float(regular_price) if regular_price else None, "KRW (정규장)"
                
        except Exception:
            return None, None

# ============================================================
# 데이터 설정 (참가자 정보)
# ============================================================
participants = [
    {"name": "참가자1", "stock_name": "SPCF", "ticker": "SPCF", "buy_price": 30.15},
    {"name": "참가자2", "stock_name": "LG씨엔에스", "ticker": "480560", "buy_price": 89900},
    {"name": "참가자3", "stock_name": "삼성전자", "ticker": "005930", "buy_price": 357500},
    {"name": "참가자4", "stock_name": "엔비디아", "ticker": "NVDA", "buy_price": 209.63},
    {"name": "참가자5", "stock_name": "한미반도체", "ticker": "042700", "buy_price": 300500},
]

# ============================================================
# 메인 로직 & 렌더링
# ============================================================
st.markdown(
    """
    <div class="hero-card">
        <h1>🏆 일주일 주식 수익률 데스매치</h1>
        <p>프리장/시간외 가격 실시간 반영 중 🕒</p>
    </div>
    """,
    unsafe_allow_html=True,
)

results = []

with st.spinner('애프터마켓 데이터를 포함하여 실시간 가격을 불러오는 중입니다... 🚀'):
    for p in participants:
        curr_price, currency = fetch_realtime_price(p["ticker"])
        
        if curr_price is not None:
            roi = ((curr_price - p["buy_price"]) / p["buy_price"]) * 100
            results.append({
                "참가자": p["name"],
                "종목명": p["stock_name"],
                "매수단가": p["buy_price"],
                "현재가": curr_price,
                "상태": currency, # 상태(정규장/시간외) 표시용
                "수익률(%)": roi
            })
        else:
            results.append({
                "참가자": p["name"],
                "종목명": p["stock_name"],
                "매수단가": p["buy_price"],
                "현재가": 0,
                "상태": "N/A",
                "수익률(%)": 0.0
            })

# 수익률 기준으로 내림차순 정렬
df_results = pd.DataFrame(results)
df_results = df_results.sort_values(by="수익률(%)", ascending=False).reset_index(drop=True)

# 1. 시각화 (수익률 바 차트)
st.subheader("📊 현재 수익률 랭킹")
fig = px.bar(
    df_results, 
    x="참가자", 
    y="수익률(%)", 
    color="수익률(%)",
    color_continuous_scale=px.colors.diverging.RdBu_r,
    text=df_results["수익률(%)"].apply(lambda x: f"{x:+.2f}%")
)
fig.update_traces(textposition='outside')
fig.update_layout(
    margin=dict(t=20, b=20, l=20, r=20),
    plot_bgcolor='rgba(0,0,0,0)',
    paper_bgcolor='rgba(0,0,0,0)',
    coloraxis_showscale=False,
    yaxis=dict(title="수익률 (%)", gridcolor='#E2E8F0')
)
st.plotly_chart(fig, use_container_width=True)

# 2. 개별 참가자 상세 카드
st.subheader("🔥 참가자별 상세 현황")

for idx, row in df_results.iterrows():
    rank = idx + 1
    
    if rank == 1:
        rank_html = f"<span class='rank-1'>🥇 1위</span>"
    elif rank == 2:
        rank_html = f"<span class='rank-2'>🥈 2위</span>"
    elif rank == 3:
        rank_html = f"<span class='rank-3'>🥉 3위</span>"
    else:
        rank_html = f"<span style='color:#64748B; font-weight:bold;'>{rank}위</span>"

    roi_class = "profit-up" if row["수익률(%)"] >= 0 else "profit-down"
    roi_sign = "+" if row["수익률(%)"] >= 0 else ""
    
    # 통화 단위 표시 분기
    unit = "$" if "USD" in row["상태"] else "₩"
    
    format_buy = f"{unit}{row['매수단가']:,.2f}" if unit == "$" else f"{unit}{row['매수단가']:,.0f}"
    format_curr = f"{unit}{row['현재가']:,.2f}" if unit == "$" else f"{unit}{row['현재가']:,.0f}"

    with st.container(border=True):
        col1, col2, col3 = st.columns([1, 2, 1])
        
        with col1:
            st.markdown(f"{rank_html}<br><span style='font-size:1.1rem;'>**{row['참가자']}**</span>", unsafe_allow_html=True)
            
        with col2:
            # 종목명 옆에 현재 가격이 어느 장(정규/시간외)인지 라벨링 추가
            status_badge = f"<span style='font-size:0.75rem; background:#E2E8F0; padding:2px 6px; border-radius:4px; margin-left:6px;'>{row['상태']}</span>"
            st.markdown(f"<span style='color:#64748B; font-size:0.9rem;'>선택 종목</span><br>**{row['종목명']}** {status_badge}", unsafe_allow_html=True)
            st.markdown(f"<span style='color:#94A3B8; font-size:0.8rem;'>매수: {format_buy} → 현재: {format_curr}</span>", unsafe_allow_html=True)
            
        with col3:
            st.markdown(f"<div style='text-align:right;'><span style='color:#64748B; font-size:0.9rem;'>현재 수익률</span><br><span class='{roi_class}' style='font-size:1.4rem;'>{roi_sign}{row['수익률(%)']:.2f}%</span></div>", unsafe_allow_html=True)
