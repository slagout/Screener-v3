"""
FRANCINE SCREENER V3 — Tiered Desktop App
=========================================
V2's EXACT screening logic + tiered UI — NO industry exclusions.
All industry/sector/name blocks from V2 removed per Tom's directive.
Identical to V2 (francine_current_discovery.py) in deep_screen, IV calc,
margin, volume, CAGR, and options-liquidity checks.
"""
import streamlit as st
import yfinance as yf
import pandas as pd
import requests
import io
import time
import re
import html
import os
import sys
import urllib.parse
from datetime import datetime, timedelta
from collections import Counter
    # 3. Deep screen sequentially (20-100% of the bar)

st.set_page_config(page_title="Stock Screener", page_icon=os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "assets", "favicon.png"), layout="wide", initial_sidebar_state="expanded")


def _favicon_data_uri():
    import base64
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "assets", "favicon.png")
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()

# ── ALL INDUSTRY/SECTOR/NAME EXCLUSIONS REMOVED per Tom's directive ──
# V3 scans ALL industries: pharma, banks, insurance, tobacco, cannabis,
# REITs, financial services no longer blocked.
EXCLUDED_COUNTRIES = [
    "china", "russia", "north korea", "brazil", "argentina",
    "mexico", "chile", "colombia", "peru", "venezuela",
]
DEFAULT_PRICE_MIN = 10.0
DEFAULT_PRICE_MAX = 50.0
DEFAULT_IV_MIN = 28.0
DEFAULT_IV_MAX = 48.0
DEFAULT_MIN_VOLUME = 2_000_000
DEEP_SCREEN_WORKERS = 2
SCREEN_CACHE_TTL = 3600
FAILED_TICKERS = []
MISSING_PRICES = []

_YF_SESSION = None


def get_yfinance_session():
    """Initialize or return shared requests.Session with Yahoo Finance authentication cookies and crumb."""
    global _YF_SESSION
    if _YF_SESSION is not None:
        return _YF_SESSION

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    })
    try:
        # 1. Fetch initial cookies from fc.yahoo.com
        sess.get("https://fc.yahoo.com/", timeout=10)
        # 2. Request authentication crumb
        r = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10)
        if r.status_code == 200 and r.text.strip():
            crumb = r.text.strip()
            # If internal scraper attributes exist, update them
            if hasattr(yf, "scrapers") and hasattr(yf.scrapers, "quote"):
                setattr(yf.scrapers.quote, "DEFAULT_CRUMB", crumb)
                setattr(yf.scrapers.quote, "DEFAULT_COOKIE", "; ".join(f"{k}={v}" for k, v in sess.cookies.items()))
    except Exception:
        pass  # Fall back to standard session

    _YF_SESSION = sess
    return _YF_SESSION


def fetch_with_retry(fn, max_retries=3, base_delay=1.0):
    global _YF_SESSION
    for attempt in range(max_retries):
        try:
            result = fn()
            if result is None:
                raise ValueError("None response")
            if hasattr(result, "status_code") and result.status_code == 401:
                _YF_SESSION = None  # Reset session to refresh crumb on 401
                raise ValueError("401 Unauthorized — crumb expired")
            return result
        except Exception:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
    return None


def test_yahoo_connectivity():
    """Verify Yahoo Finance API works before heavy lifting."""
    sess = get_yfinance_session()
    try:
        test = yf.Ticker("AAPL", session=sess)
        info = fetch_with_retry(lambda: test.info, max_retries=2, base_delay=1.0)
        if not info or not info.get("symbol"):
            st.error("❌ Yahoo Finance connection failed or is rate-limiting requests.")
            st.info("💡 Please wait a few minutes before retrying.")
            return False
        return True
    except Exception as e:
        st.error(f"❌ Yahoo Finance connectivity error: {e}")
        return False


def pre_filter_tickers(tickers):
    """Remove obvious delisted, warrant, unit, right, and problematic ticker symbols."""
    valid = []
    for t in tickers:
        s = str(t).strip().upper()
        # Skip warrants, units, rights (e.g. -WS, -WT, -W, -U, -R, or 5-char ending in W/U/R)
        if re.search(r"[-.](?:WS|WT|W|U|R|RT)$", s):
            continue
        if len(s) == 5 and s[-1] in ("W", "U", "R"):
            continue
        # Ensure standard ticker pattern (letters, optional single hyphen for class shares like BRK-B)
        if not re.match(r"^[A-Z]{1,5}(?:-[A-Z]{1,2})?$", s):
            continue
        valid.append(s)
    return valid


@st.cache_resource
def _screen_cache():
    return {}

# ── Tier definitions ──────────────────────────────────────────────────
TIERS = {
    "S&P 500":  {"label":"Tier 1 — S&P 500","desc":"~503 stocks","color":"#42a5f5"},
    "S&P 400":  {"label":"Tier 2 — S&P 400","desc":"~400 stocks","color":"#66bb6a"},
    "S&P 600":  {"label":"Tier 3 — S&P 600","desc":"~600 stocks","color":"#f9a825"},
    "Rest":     {"label":"Tier 4 — Rest of Market","desc":"~5,500 stocks","color":"#ab47bc"},
}

# ── Tier loaders (V2 exact) ────────────────────────────────────────────
def load_index(name):
    pages = {"S&P 500":"https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
             "S&P 400":"https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
             "S&P 600":"https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"}
    url = pages.get(name)
    if not url: return [], {}
    h = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"}
    for attempt in range(2):
        try:
            r = requests.get(url, headers=h, timeout=30)
            r.raise_for_status()
            df = pd.read_html(io.StringIO(r.text))[0]
            tk = df["Symbol"].str.replace(".","-",regex=False).tolist()
            sc = "GICS Sector" if "GICS Sector" in df.columns else df.columns[2]
            sectors = dict(zip(tk, df[sc].fillna("").tolist()))
            return tk, sectors
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
            st.warning(f"Could not load {name} (attempt {attempt+1}): {e}")
            return [], {}

@st.cache_data(ttl=3600)
def get_tickers(name): return load_index(name)[0]

@st.cache_data(ttl=900, show_spinner=False)
def nasdaq_universe():
    """Fetch all listed US stocks with current price, volume, sector and industry in a single request."""
    url = ("https://api.nasdaq.com/api/screener/stocks"
           "?tableonly=true&limit=25&offset=0&download=true")
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for attempt in range(3):
        try:
            r = requests.get(url, headers=h, timeout=30)
            r.raise_for_status()
            rows = r.json().get("data", {}).get("rows", [])
            out = {}
            for row in rows:
                raw_sym = row.get("symbol") or ""
                # Normalize share classes: Nasdaq uses 'BRK/B' or 'BRK.B', app uses 'BRK-B'
                sym = raw_sym.replace("/", "-").replace(".", "-").upper().strip()
                price_str = (row.get("lastsale") or "").replace("$", "").replace(",", "").strip()
                try:
                    px = float(price_str)
                except ValueError:
                    continue
                try:
                    vol = int((row.get("volume") or "0").replace(",", "").strip())
                except ValueError:
                    vol = 0
                if not sym:
                    continue
                out[sym] = {
                    "price": px,
                    "volume": vol,
                    "name": row.get("name") or "",
                    "market_cap": row.get("marketCap") or "",
                    "sector": row.get("sector") or "",
                    "industry": row.get("industry") or "",
                    "country": row.get("country") or "",
                }
            return out
        except Exception:
            if attempt < 2:
                time.sleep(2)
    return {}


def get_rest_tickers(exclude):
    return _rest_tickers(frozenset(exclude))

@st.cache_data(ttl=3600, show_spinner=False)
def _rest_tickers(exclude):
    tk = set(); h = {"User-Agent":"Mozilla/5.0"}
    for url,label in [("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt","NASDAQ"),
                      ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt","NYSE/AMEX")]:
        try:
            r = requests.get(url,headers=h,timeout=30); r.raise_for_status()
            lines = r.text.strip().split("\n")
            dl = [l for l in lines if l and not l.startswith("File Creation")]
            if len(dl)<2: continue
            cols = dl[0].split("|" if "|" in dl[0] else "\t")
            cm = {c.strip().lower():i for i,c in enumerate(cols)}
            si = cm.get("symbol", cm.get("act symbol",0))
            sni = cm.get("security name", None)
            ti = cm.get("test issue",None); ei = cm.get("etf",None)
            for line in dl[1:]:
                p = line.split("|" if "|" in line else "\t")
                if len(p)<=si: continue
                s = p[si].strip().replace(".","-")
                if not re.match(r"^[A-Z]{1,5}$",s): continue
                if ti and len(p)>ti and p[ti].strip().upper()=="Y": continue
                if ei and len(p)>ei and p[ei].strip().upper()=="Y": continue
                sec_name = p[sni].strip().lower() if sni and len(p)>sni else ""
                if any(w in sec_name for w in ["warrant", "unit", "right", "preferred", "pfd", "debt", "notes", "due 20", "depositary share", "etn"]):
                    continue
                if s in exclude: continue
                tk.add(s)
        except: pass
    return sorted(tk)

# ── V2 EXACT filter functions ─────────────────────────────────────────
def industry_pass(info):
    """All industries allowed — exclusions removed per V3 spec."""
    return True

def geo_pass(info):
    c = (info.get("country") or "").lower()
    if not c: return True
    for e in EXCLUDED_COUNTRIES:
        if e in c: return False
    return True

# ── V2 EXACT tier_name_filter ─────────────────────────────────────────
def tier_name_filter(tickers):
    """First-pass filter — no industry/sector exclusions in V3."""
    return list(tickers)

# ── V2 EXACT deep_screen ──────────────────────────────────────────────
def deep_screen(ticker, price, iv_min, iv_max, min_vol):
    result = {
        "ticker": ticker, "qualified": False, "status": "rejected", "fail_reason": "",
        "company_name": "", "sector": "", "industry": "", "exchange": "",
        "price": price, "iv": None, "volume": 0,
        "avg_volume": 0, "margin": None, "cagr": None,
        "driver": "", "options_liquid": False,
    }
    try:
        sess = get_yfinance_session()
        stock = yf.Ticker(ticker, session=sess)
        info = fetch_with_retry(lambda: stock.info)
        if info is None:
            if ticker not in FAILED_TICKERS:
                FAILED_TICKERS.append(ticker)
            result["status"] = "fetch_error"
            result["fail_reason"] = "FETCH_ERROR: max retries exceeded"
            return result
        if not info:
            if ticker not in FAILED_TICKERS:
                FAILED_TICKERS.append(ticker)
            result["status"] = "fetch_error"
            result["fail_reason"] = "FETCH_ERROR: empty response"
            return result

        result["company_name"] = info.get("longName") or info.get("shortName") or ticker
        result["sector"] = info.get("sector") or "N/A"
        result["industry"] = info.get("industry") or "N/A"
        result["exchange"] = info.get("fullExchangeName") or info.get("exchange") or ""

        # Industry/sector/name filter
        if not industry_pass(info):
            result["fail_reason"] = "Industry excluded"
            return result
        if not geo_pass(info):
            result["fail_reason"] = "Geography excluded"
            return result

        # Cheap volume check first to skip option-chain calls
        avg_vol = int(info.get("averageVolume") or info.get("averageDailyVolume10Day") or 0)
        yahoo_err = ""
        if not avg_vol:
            # Yahoo sometimes returns a partial info dict; fall back to price history
            try:
                hist = stock.history(period="3mo")
                if not hist.empty:
                    avg_vol = int(hist["Volume"].mean())
                else:
                    yahoo_err = " (Yahoo Finance returned no history)"
            except Exception as e:
                yahoo_err = f" (Yahoo Finance error: {type(e).__name__}: {str(e)[:120]})"
        result["avg_volume"] = avg_vol
        result["volume"] = avg_vol
        if avg_vol < min_vol:
            result["fail_reason"] = f"Avg volume {avg_vol:,} < {min_vol:,}{yahoo_err}"
            return result

        # Options liquidity check (need 2+ weeklies with volume)
        exp_dates = ()
        opt_err = ""
        for attempt in range(2):
            try:
                exp_dates = stock.options
                break
            except Exception as e:
                opt_err = f" (Yahoo Finance error: {type(e).__name__}: {str(e)[:120]})"
                time.sleep(0.5)
        if not exp_dates or len(exp_dates) < 2:
            result["fail_reason"] = f"No weekly options chain{opt_err}"
            return result

        weeks_with_volume = 0
        for exp in exp_dates[:10]:
            try:
                chain = stock.option_chain(exp)
                if (chain.calls["volume"].sum() + chain.puts["volume"].sum()) > 0:
                    weeks_with_volume += 1
                    if weeks_with_volume >= 2:
                        break
            except Exception:
                pass
        if weeks_with_volume < 2:
            result["fail_reason"] = "Low options activity"
            return result
        result["options_liquid"] = True

        # IV calculation (V2 exact: custom formula → yfinance fallback)
        iv_found = False
        now = datetime.now()
        target_30d = now + timedelta(days=30)
        last_calc_iv = None

        try:
            best_exp = min(exp_dates, key=lambda e: abs(
                (datetime.strptime(e[:10], "%Y-%m-%d") - target_30d).days))
            chain_30d = stock.option_chain(best_exp)
            tte = (datetime.strptime(best_exp[:10], "%Y-%m-%d") - now).days / 365.0

            if not chain_30d.calls.empty:
                chain_30d.calls["dist"] = abs(chain_30d.calls["strike"] - price)
                nearest_call = chain_30d.calls.loc[chain_30d.calls["dist"].idxmin()]
                call_mid = (float(nearest_call.get("bid", 0)) + float(nearest_call.get("ask", 0))) / 2
                call_price = call_mid if call_mid > 0.01 else float(nearest_call.get("lastPrice", 0))
                if call_price > 0.01 and tte > 0.001:
                    iv_approx = call_price / (price * 0.4 * (tte ** 0.5))
                    if 0.01 < iv_approx < 5.0:
                        iv_pct = iv_approx * 100
                        if iv_pct <= 100:
                            last_calc_iv = iv_pct
                            if iv_min <= iv_pct <= iv_max:
                                result["iv"] = round(iv_pct, 1)
                                iv_found = True

            if not iv_found and not chain_30d.puts.empty:
                chain_30d.puts["dist"] = abs(chain_30d.puts["strike"] - price)
                nearest_put = chain_30d.puts.loc[chain_30d.puts["dist"].idxmin()]
                put_mid = (float(nearest_put.get("bid", 0)) + float(nearest_put.get("ask", 0))) / 2
                put_price = put_mid if put_mid > 0.01 else float(nearest_put.get("lastPrice", 0))
                if put_price > 0.01 and tte > 0.001:
                    iv_approx = put_price / (price * 0.4 * (tte ** 0.5))
                    if 0.01 < iv_approx < 5.0:
                        iv_pct = iv_approx * 100
                        if iv_pct <= 100:
                            if last_calc_iv is None:
                                last_calc_iv = iv_pct
                            if iv_min <= iv_pct <= iv_max:
                                result["iv"] = round(iv_pct, 1)
                                iv_found = True
        except Exception:
            pass

        # Fallback to yfinance IV
        if not iv_found:
            for exp in exp_dates[:5]:
                try:
                    chain = stock.option_chain(exp)
                    if not chain.calls.empty:
                        chain.calls["dist"] = abs(chain.calls["strike"] - price)
                        nc = chain.calls.loc[chain.calls["dist"].idxmin()]
                        chain_iv = nc.get("impliedVolatility")
                        if chain_iv and float(chain_iv) > 0.001:
                            iv_pct = float(chain_iv) * 100
                            if iv_pct <= 100:
                                if last_calc_iv is None:
                                    last_calc_iv = iv_pct
                                if iv_min <= iv_pct <= iv_max:
                                    result["iv"] = round(iv_pct, 1)
                                    iv_found = True
                                    break
                except Exception:
                    pass

        if not iv_found:
            if last_calc_iv is not None:
                result["fail_reason"] = f"IV {last_calc_iv:.1f}% outside {iv_min}-{iv_max}% range"
            else:
                result["fail_reason"] = "No IV data available"
            return result

        # Margin check
        fin = stock.financials
        if fin is not None and not fin.empty:
            year = fin.iloc[:, 0]
            op_income = None
            revenue = None
            for k in ["Operating Income", "EBIT"]:
                if k in year.index:
                    op_income = float(year.loc[k])
                    break
            for k in ["Total Revenue", "Revenue"]:
                if k in year.index:
                    revenue = float(year.loc[k])
                    break
            if op_income is not None and revenue and revenue != 0:
                margin_pct = (op_income / revenue) * 100
                if margin_pct <= 0:
                    result["fail_reason"] = f"Margin {margin_pct:.1f}% (non-positive)"
                    return result
                result["margin"] = round(margin_pct, 2)
        else:
            result["fail_reason"] = "No financial data available"
            return result

        # CAGR from revenue history
        if fin.shape[1] >= 3:
            revenues = []
            for i in range(min(4, fin.shape[1])):
                col = fin.iloc[:, i]
                found = False
                for k in ["Total Revenue", "Revenue"]:
                    if k in col.index:
                        revenues.append(float(col.loc[k]))
                        found = True
                        break
                if not found:
                    revenues.append(None)
            revenues = [r for r in revenues if r]
            if len(revenues) >= 3 and revenues[-1] > 0 and revenues[0] > 0:
                n_periods = len(revenues) - 1
                cagr = ((revenues[0] / revenues[-1]) ** (1.0 / n_periods) - 1) * 100
                result["cagr"] = round(cagr, 2)

        # Business description / driver
        summary = info.get("longBusinessSummary") or ""
        if summary:
            sentences = [x.strip() for x in summary.replace("\n", " ").split(".")[:3] if x.strip()]
            result["driver"] = ". ".join(sentences)[:250]
        else:
            result["driver"] = ticker

        result["qualified"] = True
        result["status"] = "ok"
        return result

    except Exception as e:
        if ticker not in FAILED_TICKERS:
            FAILED_TICKERS.append(ticker)
        result["status"] = "fetch_error"
        result["fail_reason"] = f"Error: {type(e).__name__}: {str(e)[:150]}"
        return result

LOADER_TIPS = [
    "The price filter runs first, so the slower options checks only run on stocks in your range.",
    "Implied volatility (IV) is the market's estimate of how much a stock may move.",
    "Weekly options give you more frequent expirations to work with.",
    "Many stocks are checked in parallel to keep the scan fast.",
]
_SPARKLE = ("<svg width='22' height='22' viewBox='0 0 24 24' fill='none' stroke='#ff6b3d' stroke-width='2' "
            "stroke-linecap='round' stroke-linejoin='round'><path d='M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z'/>"
            "<path d='M19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z'/></svg>")


def _build_pixel_candles(n=22, rows=16, seed=7):
    # Deterministic random-walk candles drawn on a pixel grid: (col, row, color)
    import random
    rnd = random.Random(seed)
    px, level = [], rows * 0.7
    for c in range(n):
        o = level
        cl = max(2.0, min(rows - 3.0, o + rnd.uniform(-3.2, 3.2) - 0.25))
        level = cl
        hi = max(o, cl) + rnd.uniform(0.5, 2.5)
        lo = min(o, cl) - rnd.uniform(0.5, 2.5)
        up = cl >= o
        base = ["#1f9d63", "#4fbf8a", "#9ddcbc"] if up else ["#d64545", "#ff6b3d", "#ffb59c"]
        x0 = c * 4
        for r in range(rows):
            y = rows - 1 - r
            if lo <= r <= hi:
                in_body = min(o, cl) <= r <= max(o, cl)
                shade = base[min(2, abs(r - int((o + cl) / 2)) // 2)] if in_body else base[2]
                if in_body:
                    for dx in range(3):
                        px.append((x0 + dx, y, shade))
                else:
                    px.append((x0 + 1, y, shade))
    return px, n * 4


_PIXELS, _PIXEL_COLS = _build_pixel_candles()
_PIXEL_ROWS = 16


def _pixel_chart(pct):
    s = 10
    edge = _PIXEL_COLS * pct / 100
    rects = "".join(
        f"<rect x='{x*s}' y='{y*s}' width='{s-1}' height='{s-1}' "
        + (f"fill='{col}'/>" if x < edge else "style='fill:var(--px-off)'/>")
        for x, y, col in _PIXELS
    )
    return (f"<svg class='pixel-chart' viewBox='0 0 {_PIXEL_COLS*s} {_PIXEL_ROWS*s}' "
            f"preserveAspectRatio='xMidYMid meet' shape-rendering='crispEdges'>{rects}</svg>")


WIKI_EXTRA_TIPS = [
    "Implied volatility (IV) is measured on the expiration closest to 30 days out, near the current price.",
    "Results are cached for an hour per stock, so re-running the same screen is much faster.",
    "Downloading a CSV or PDF does not clear your results; run another screen whenever you like.",
    "Only a new screen replaces the current results.",
    "A positive net margin and revenue CAGR help filter out unprofitable or shrinking companies.",
]


def render_wiki():
    st.markdown(
        "**Stock Screener** finds options-liquid stocks across four market tiers, "
        "then filters them by price, implied volatility, volume and profitability."
    )
    t1, t2, t3, t4 = st.tabs(["Getting started", "Filters", "Results", "Did you know"])
    with t1:
        st.markdown(
            "1. Pick one or more **tiers** in the sidebar (or use the Tier 1 / Tiers 1-3 / All 4 / Clear presets).\n"
            "2. Set the **price range**, **IV target** and **minimum volume**.\n"
            "3. Click **Run screen** and watch the pixel candlestick chart fill as the scan progresses.\n"
            "4. Review the cards, then use **Download Results** to export a CSV or PDF.\n"
            "5. Change the parameters and run again. Your current results stay until a new screen finishes."
        )
    with t2:
        st.markdown(
            "- **Price range**: current price must be between From and To.\n"
            "- **IV target %**: stocks must have IV within the target plus or minus 10 points.\n"
            "- **Min volume**: minimum average daily share volume.\n"
            "- **Weekly options**: at least two expirations that have traded volume.\n"
            "- **Margin**: net profit margin must be above 0%.\n"
            "- **Blocked regions**: companies based in China, Russia and Latin America are excluded."
        )
    with t3:
        st.markdown(
            "Each card shows price, IV, average volume, margin, revenue CAGR, sector and a short business summary.\n\n"
            "The **Failure Summary** lists why stocks were dropped. If it shows Yahoo Finance errors or empty "
            "option expirations, Yahoo is throttling requests; wait a few minutes and run again."
        )
    with t4:
        for tip in LOADER_TIPS + WIKI_EXTRA_TIPS:
            st.markdown(f"- {tip}")


def render_progress(ph, tiers, idx, tier_pct, detail, found=0):
    pct = max(0, min(100, int(100 * (idx + tier_pct / 100) / len(tiers))))
    tip = LOADER_TIPS[(pct // 10) % len(LOADER_TIPS)]
    cards = ""
    for i, k in enumerate(tiers):
        state = "Done" if i < idx else ("Scanning" if i == idx else "Queued")
        cards += (f"<div class='loader-card'><b>{state}</b>"
                  f"<span>{html.escape(TIERS[k]['label'])}</span></div>")
    cards += f"<div class='loader-card'><b class='pos'>{found}</b><span>Qualified so far</span></div>"
    ph.markdown(
        f"<div class='loader'><div class='loader-badge'>{_SPARKLE}</div>"
        f"<div class='loader-title'>Screening {html.escape(tiers[idx])}…</div>"
        f"<div class='pixel-wrap'>{_pixel_chart(pct)}<span class='pixel-pct'>{pct}%</span></div>"
        f"<div class='loader-detail'>{html.escape(detail)}</div>"
        f"<div class='loader-tip'><b>Did you know?</b>{html.escape(tip)}</div>"
        f"<div class='loader-cards'>{cards}</div></div>",
        unsafe_allow_html=True,
    )

def _legacy_yahoo_prefilter(passed, price_min, price_max, min_vol, report):
    """Fallback batch price & volume filter via yfinance if Nasdaq screener is unreachable."""
    sess = get_yfinance_session()
    priced = []
    batch_size = 150
    for i in range(0, len(passed), batch_size):
        batch = passed[i:i + batch_size]
        report(20 * i / len(passed), f"Filtering {len(passed)} tickers by price & volume (Yahoo fallback)", 0)
        try:
            data = yf.download(
                batch,
                period="5d",
                interval="1d",
                progress=False,
                auto_adjust=False,
                group_by="ticker",
                session=sess,
            )
            time.sleep(1.0)
            if data.empty:
                MISSING_PRICES.extend(t for t in batch if t not in MISSING_PRICES)
                continue
            if isinstance(data.columns, pd.MultiIndex):
                close_level = next(
                    (level for level in range(data.columns.nlevels)
                     if "Close" in data.columns.get_level_values(level)),
                    None,
                )
                if close_level is None:
                    closes = pd.Series(dtype="float64")
                    vols = pd.Series(dtype="float64")
                else:
                    closes = data.xs("Close", axis=1, level=close_level).iloc[-1]
                    vols = data.xs("Volume", axis=1, level=close_level).max()
            else:
                if "Close" in data.columns:
                    close_data = data["Close"]
                    closes = close_data.iloc[-1] if isinstance(close_data, pd.DataFrame) else pd.Series({batch[0]: close_data.iloc[-1]})
                    vol_data = data.get("Volume")
                    vols = vol_data.max() if isinstance(vol_data, pd.DataFrame) else pd.Series({batch[0]: vol_data.max() if vol_data is not None else 0})
                else:
                    closes = pd.Series(dtype="float64")
                    vols = pd.Series(dtype="float64")

            for t in batch:
                if t in closes.index:
                    px = float(closes[t])
                    if pd.notna(px) and price_min <= px <= price_max:
                        v_max = float(vols.get(t, 0)) if (t in vols.index and pd.notna(vols.get(t, 0))) else 0
                        if v_max == 0 or v_max >= min_vol * 0.3:
                            priced.append((t, px))
                    elif pd.isna(px):
                        if t not in MISSING_PRICES:
                            MISSING_PRICES.append(t)
                elif t not in MISSING_PRICES:
                    MISSING_PRICES.append(t)
        except Exception:
            MISSING_PRICES.extend(t for t in batch if t not in MISSING_PRICES)
            continue
    return priced


# ── TIER SCAN (V2 exact pipeline: industry filter → batch price → deep) ──
def scan_tier(tier_name, tickers, price_min, price_max,
              iv_min, iv_max, min_vol, report):
    qualified = []
    failed = []
    report(0, f"Loaded {len(tickers)} tickers", 0)

    if not tickers:
        return [], []

    # 1. Industry/name pre-filter & delisted/junk symbol filter
    passed = pre_filter_tickers(tier_name_filter(tickers))
    if not passed:
        return [], []

    # 2. Fast price & volume pre-filter using Nasdaq universe (1 single request, no API key)
    universe = nasdaq_universe()
    if universe:
        report(10, f"Screening {len(passed)} tickers via Nasdaq universe", 0)
        priced = []
        for t in passed:
            u = universe.get(t)
            if not u:
                continue  # delisted/unlisted or non-common equity — skip silently
            if price_min <= u["price"] <= price_max:
                # Volume heuristic: check recent day volume before deep screening
                if u["volume"] == 0 or u["volume"] >= min_vol * 0.3:
                    priced.append((t, u["price"]))
    else:
        # Fallback to batched Yahoo download if Nasdaq screener API is temporarily unavailable
        priced = _legacy_yahoo_prefilter(passed, price_min, price_max, min_vol, report)

    report(20, f"{len(priced)} of {len(passed)} pass price filter", 0)

    if not priced:
        return [], []

    # 3. Deep screen in parallel (20-100% of the bar); results re-sorted to input order
    total_priced = len(priced)
    done = 0
    results = {}
    cache = _screen_cache()

    def screen_cached(t, px):
        key = (t, round(px, 2), iv_min, iv_max, min_vol)
        hit = cache.get(key)
        if hit and time.time() - hit[0] < SCREEN_CACHE_TTL:
            cached = dict(hit[1])
            cached.setdefault("status", "ok" if cached["qualified"] else "rejected")
            return cached
        r = deep_screen(t, px, iv_min, iv_max, min_vol)
        # Don't cache Yahoo throttling/errors, so they are retried next run
        if r["status"] != "fetch_error" and not any(
            s in r["fail_reason"] for s in ("Yahoo", "Error:", "No data")
        ):
            cache[key] = (time.time(), dict(r))
        return r

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=DEEP_SCREEN_WORKERS) as pool:
        futures = {pool.submit(screen_cached, t, px): (idx, t)
                   for idx, (t, px) in enumerate(priced)}
        for fut in as_completed(futures):
            idx, t = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = {"ticker": t, "qualified": False, "status": "fetch_error", "fail_reason": f"Error: {e}"}
            result["tier"] = tier_name
            results[idx] = result
            done += 1
            n_q = sum(1 for r in results.values() if r.get("qualified"))
            report(20 + 80 * done / total_priced,
                   f"Deep screening {done} of {total_priced} candidates", n_q)

    for i in sorted(results):
        (qualified if results[i]["qualified"] else failed).append(results[i])

    return qualified, failed

# ═══════════════════════════════════════════════════════════════════════
# STREAMLIT UI
# ═══════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html,body,[class*="css"],.stApp{font-family:'Inter',sans-serif}
.stApp{background:#f6f5f3;color:#1f2328}
.block-container{max-width:860px;padding-top:2rem}
header[data-testid="stHeader"]{background:transparent}
.main-title{font-size:1.9rem;font-weight:700;letter-spacing:-0.02em;color:#1f2328}
.subtitle{color:#6b7280;margin:0.2rem 0 1.5rem;font-size:0.95rem}
.section-label{color:#6b7280;font-weight:600;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.1em;margin:1.4rem 0 0.6rem}
.chip-row{display:flex;flex-wrap:wrap;gap:0.5rem}
.chip{background:#fff;border:1px solid #e6e3de;border-radius:999px;padding:0.35rem 0.85rem;font-size:0.82rem;color:#4b5563}
.chip b{color:#1f2328;font-weight:600}
.chip.ok{border-color:#bfe3cf;background:#e9f6ee}
.chip.no{border-color:#f1c9c9;background:#fbeeee}
.card{background:#fff;border:1px solid #e6e3de;border-radius:12px;padding:1.1rem 1.25rem;margin-bottom:0.9rem;box-shadow:0 1px 2px rgba(0,0,0,.04);transition:box-shadow .15s}
.card:hover{box-shadow:0 4px 14px rgba(0,0,0,.08)}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem}
.card-name{font-size:1.05rem;font-weight:600;color:#1f2328}
.card-sub{font-size:0.8rem;color:#6b7280;margin-top:0.15rem}
.card-price{font-size:1.35rem;font-weight:700;color:#1f2328;text-align:right}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:0.6rem;margin-top:0.9rem}
.metric{background:#f6f5f3;border-radius:8px;padding:0.55rem 0.7rem}
.metric span{display:block;font-size:0.68rem;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em}
.metric b{font-size:0.98rem;color:#1f2328;font-weight:600}
.pos{color:#1f9d63!important}.neg{color:#d64545!important}
.tier-tag{display:inline-block;padding:0.1rem 0.55rem;border-radius:999px;font-size:0.68rem;font-weight:600;color:#fff;margin-left:0.5rem;vertical-align:middle}
.card-links{display:flex;align-items:center;gap:0.45rem;margin-top:0.4rem;flex-wrap:wrap}
.ext-link{display:inline-flex;align-items:center;gap:0.28rem;padding:0.22rem 0.55rem;font-size:0.72rem;font-weight:500;border-radius:6px;border:1px solid #e6e3de;background:#f6f5f3;color:#374151;text-decoration:none;transition:all .15s ease}
.ext-link:hover{border-color:#ff6b3d;color:#ff6b3d;text-decoration:none;background:#fff5f0}
.ext-link svg{width:11px;height:11px;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;fill:none}
.driver{margin-top:0.8rem;font-size:0.82rem;color:#6b7280;line-height:1.5}
.stButton>button{border-radius:8px;border:1px solid #e6e3de;background:#fff;color:#1f2328;font-weight:500}
.stButton>button:hover{border-color:#ff6b3d;color:#ff6b3d}
.stButton>button[kind="primary"]{background:#ff6b3d;border-color:#ff6b3d;color:#fff;font-weight:600}
.stButton>button[kind="primary"]:hover{background:#e85a2d;color:#fff}
div[data-testid="stStatusWidget"]{display:none!important}
@media(max-width:640px){.metrics{grid-template-columns:repeat(2,1fr)}}
section[data-testid="stSidebar"]{background:#fff;border-right:1px solid #e6e3de}
section[data-testid="stSidebar"] .section-label{margin-top:0.4rem}
.side-brand{font-weight:700;font-size:1.1rem;color:#1f2328;margin-bottom:0.5rem;display:flex;align-items:center;gap:8px}
.side-brand img{width:22px;height:22px;border-radius:5px;image-rendering:pixelated}
.loader{padding:3rem 0.5rem 1.5rem;margin:0.8rem 0;text-align:center}
.loader-badge{width:46px;height:46px;margin:0 auto 1rem;border-radius:50%;background:#fff0ea;display:flex;align-items:center;justify-content:center;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{transform:scale(1.1)}}
.loader-title{font-size:1.05rem;font-weight:600;color:#1f2328;margin-bottom:1rem}
.loader-track{position:relative;height:18px;max-width:520px;margin:0 auto;background:#ffe9e1;border-radius:999px;overflow:hidden}
.loader-fill{height:100%;background:#ff6b3d;border-radius:999px;transition:width .3s ease}
.loader-pct{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:0.68rem;font-weight:600;color:#1f2328}
.pixel-wrap{max-width:560px;margin:0 auto;text-align:center}
.pixel-chart{width:100%;height:auto;display:block}
.pixel-pct{display:block;margin-top:0.5rem;font-size:0.78rem;font-weight:600;letter-spacing:0.08em;color:#1f2328;font-variant-numeric:tabular-nums}
.loader-detail{margin-top:0.9rem;font-size:0.82rem;color:#6b7280}
.loader-tip{margin:1.6rem auto 0;max-width:420px;font-size:0.76rem;color:#6b7280;line-height:1.5}
.loader-tip b{display:block;color:#1f2328;font-weight:600;margin-bottom:0.1rem}
.loader-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:0.7rem;margin-top:2.2rem;text-align:left}
.loader-card{background:#fff;border:1px solid #e6e3de;border-radius:10px;padding:0.8rem 0.9rem}
.loader-card b{display:block;font-size:1.2rem;font-weight:700;color:#1f2328}
.loader-card span{font-size:0.76rem;color:#6b7280}
</style>
""", unsafe_allow_html=True)


def theme_css(dark):
    t = dict(
        bg="#0d1117", panel="#161b22", soft="#010409", border="#30363d", text="#e6edf3",
        muted="#8b949e", btn="#1f6feb", btn_text="#ffffff", btn_hover="#388bfd", off="#21262d",
        ok="#0f2d1c", ok_b="#238636", no="#2d1315", no_b="#da3633",
    ) if dark else dict(
        bg="#fdf6e3", panel="#fffdf5", soft="#eee8d5", border="#d9d2bb", text="#073642",
        muted="#657b83", btn="#268bd2", btn_text="#fdf6e3", btn_hover="#1e6fa8", off="#e4dcc3",
        ok="#e6efd0", ok_b="#b5c77a", no="#f6dcd3", no_b="#e0a28f",
    )
    return f"""<style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&display=swap');
:root{{--px-off:{t['off']}}}
html,body,[class*="css"],.stApp{{font-family:'Geist','Inter',sans-serif}}
.stApp,header[data-testid="stHeader"]{{background:{t['bg']}!important;color:{t['text']}}}
.stApp p,.stApp label,.stApp span,.stApp h1,.stApp h2,.stApp h3,.stApp li,.stApp div[data-testid="stMarkdownContainer"]{{color:{t['text']}}}
.stApp svg{{color:{t['text']}}}
section[data-testid="stSidebar"]{{background:{t['soft']}!important;border-right:1px solid {t['border']}}}
.main-title,.card-name,.card-price,.metric b,.side-brand,.loader-title,.loader-card b,.chip b,.loader-tip b,.loader-pct,.pixel-pct{{color:{t['text']}!important}}
.subtitle,.section-label,.card-sub,.metric span,.driver,.loader-detail,.loader-tip,.loader-card span{{color:{t['muted']}!important}}
.card,.chip,.loader-card{{background:{t['panel']}!important;border-color:{t['border']}!important}}
.ext-link{{background:{t['soft']}!important;border-color:{t['border']}!important;color:{t['text']}!important}}
.ext-link:hover{{border-color:{t['btn']}!important;color:{t['btn']}!important}}
.metric{{background:{t['soft']}!important}}
.chip.ok{{background:{t['ok']}!important;border-color:{t['ok_b']}!important}}
.chip.no{{background:{t['no']}!important;border-color:{t['no_b']}!important}}
.chip{{color:{t['muted']}!important}}
.loader-badge{{background:{t['soft']}!important}}
.loader-badge svg{{stroke:{t['text']}!important}}
div[data-baseweb="input"],div[data-baseweb="input"] input,div[data-baseweb="base-input"]{{background:{t['soft']}!important;color:{t['text']}!important}}
div[data-baseweb="input"] button{{background:transparent!important;color:{t['text']}!important}}
.stButton>button,div[data-testid="stPopover"] button,.stDownloadButton>button{{background:{t['panel']}!important;color:{t['text']}!important;border:1px solid {t['border']}!important;border-radius:6px}}
.stButton>button:hover,div[data-testid="stPopover"] button:hover,.stDownloadButton>button:hover{{border-color:{t['text']}!important;color:{t['text']}!important}}
.stButton>button[kind="primary"]{{background:{t['btn']}!important;border-color:{t['btn']}!important;color:{t['btn_text']}!important}}
.stButton>button[kind="primary"] p{{color:{t['btn_text']}!important}}
.stButton>button[kind="primary"]:hover{{background:{t['btn_hover']}!important;border-color:{t['btn_hover']}!important;color:{t['btn_text']}!important}}
div[data-testid="stExpander"] details{{border-color:{t['border']}!important;background:{t['panel']}!important}}
hr{{border-color:{t['border']}!important}}
</style>"""


st.markdown(theme_css(st.session_state.get("dark_mode", False)), unsafe_allow_html=True)

st.markdown('<div class="main-title">Stock Screener</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Options-liquid stocks across four market tiers, no industry exclusions.</div>', unsafe_allow_html=True)

# Criteria display
st.markdown('<div class="section-label">Screening criteria</div>', unsafe_allow_html=True)
st.markdown("""
<div class="chip-row">
<span class="chip ok"><b>All industries</b></span>
<span class="chip no"><b>Blocked:</b> China, Russia, Latin America</span>
<span class="chip"><b>Weekly options</b> with volume</span>
<span class="chip"><b>IV</b> target ±10</span>
<span class="chip"><b>Volume</b> ≥ 2M</span>
<span class="chip"><b>Margin</b> &gt; 0%</span>
</div>
""", unsafe_allow_html=True)

# Tier selection
tier_keys = list(TIERS.keys())
if "_tier_init" not in st.session_state:
    for k in tier_keys:
        st.session_state[f"tc_{k}"] = (k == "S&P 500")
    st.session_state._tier_init = True

# Preset handler
preset = st.session_state.get("_preset", None)
if preset:
    for k in tier_keys:
        if preset == "t1":
            v = (k == "S&P 500")
        elif preset == "t13":
            v = (k in ("S&P 500", "S&P 400", "S&P 600"))
        elif preset == "all":
            v = True
        elif preset == "none":
            v = False
        st.session_state[f"tc_{k}"] = v
        st.session_state[f"cb_{k}"] = v  # sync widget key so checkbox renders correctly
    del st.session_state._preset
    st.rerun()

with st.sidebar:
    st.markdown(f'<div class="side-brand"><img src="{_favicon_data_uri()}" alt="">Stock Screener</div>', unsafe_allow_html=True)
    st.toggle("Dark mode", key="dark_mode")
    if hasattr(st, "dialog"):
        if st.button("How to use", use_container_width=True):
            st.dialog("How to use Stock Screener", width="large")(render_wiki)()
    else:
        with st.popover("How to use", use_container_width=True):
            render_wiki()
    st.markdown('<div class="section-label">Tier selection</div>', unsafe_allow_html=True)
    pc = st.columns(2)
    if pc[0].button("Tier 1", use_container_width=True):
        st.session_state._preset = "t1"; st.rerun()
    if pc[1].button("Tiers 1-3", use_container_width=True):
        st.session_state._preset = "t13"; st.rerun()
    if pc[0].button("All 4", use_container_width=True):
        st.session_state._preset = "all"; st.rerun()
    if pc[1].button("Clear", use_container_width=True):
        st.session_state._preset = "none"; st.rerun()

    for k in tier_keys:
        ck = f"tc_{k}"
        bk = f"cb_{k}"
        if bk not in st.session_state:
            st.session_state[bk] = st.session_state.get(ck, (k == "S&P 500"))
        val = st.checkbox(f"{TIERS[k]['label']} — {TIERS[k]['desc']}", key=bk)
        st.session_state[ck] = val

    st.markdown('<div class="section-label">Parameters</div>', unsafe_allow_html=True)
    pr = st.columns(2)
    price_min = pr[0].number_input("From $", min_value=0.0, value=10.0, step=5.0)
    price_max = pr[1].number_input("To $", min_value=0.0, value=50.0, step=5.0)
    iv_target = st.number_input("IV target %", min_value=0.0, value=35.0, step=5.0)
    min_vol = st.number_input("Min volume", min_value=0, value=2000000, step=500000)

    iv_min = max(0, iv_target - 10)
    iv_max = iv_target + 10
    st.caption(f"IV range: {iv_min:.0f}–{iv_max:.0f}%")

    run = st.button("Run screen", type="primary", use_container_width=True)

selected = [k for k in tier_keys if st.session_state.get(f"tc_{k}", False)]

# ── SCREENING ──────────────────────────────────────────────────────────
if run:
    if price_min <= 0 or price_max <= 0:
        st.error("Enter valid price range.")
    elif price_min >= price_max:
        st.error("'To:' must be greater than 'From:'.")
    elif not selected:
        st.error("Select at least one tier.")
    elif not test_yahoo_connectivity():
        pass
    else:
        FAILED_TICKERS.clear()
        MISSING_PRICES.clear()
        all_qualified = []
        all_failed = []
        loader_ph = st.empty()

        for tier_idx, tier_name in enumerate(selected):
            render_progress(loader_ph, selected, tier_idx, 0, "Loading tickers", len(all_qualified))

            if tier_name == "Rest":
                index_tickers = set()
                for k in ["S&P 500", "S&P 400", "S&P 600"]:
                    try:
                        tk = get_tickers(k)
                        index_tickers.update(tk)
                    except Exception:
                        pass
                tickers = get_rest_tickers(index_tickers)
                if not tickers:
                    st.info("No stocks loaded for Rest of Market.")
                    continue
            else:
                tickers, _ = load_index(tier_name)
                if not tickers:
                    st.warning(f"Could not load {tier_name}.")
                    continue

            def report(pct, detail, n_q, _i=tier_idx, _base=len(all_qualified)):
                render_progress(loader_ph, selected, _i, pct, detail, _base + n_q)

            q, f = scan_tier(tier_name, tickers, price_min, price_max,
                             iv_min, iv_max, min_vol, report)
            all_qualified.extend(q)
            all_failed.extend(f)

        loader_ph.empty()
        st.session_state.results = (all_qualified, all_failed)
        st.session_state.failed_tickers = list(FAILED_TICKERS)
        st.session_state.missing_prices = list(MISSING_PRICES)


def build_pdf(rows):
    from fpdf import FPDF

    def clean(s):
        return str(s).encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, f"Stock Screener Results ({len(rows)} qualified)",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    for q in rows:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, clean(f"{q.get('ticker','')} - {q.get('company_name','')}  ${q.get('price',0):.2f}"),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=9)
        cagr = q.get("cagr")
        pdf.multi_cell(0, 5, clean(
            f"Tier: {q.get('tier','')} | {q.get('sector','N/A')} / {q.get('industry','N/A')}\n"
            f"IV: {q.get('iv',0):.1f}% | Avg volume: {(q.get('avg_volume') or 0):,} | "
            f"Margin: {(q.get('margin') or 0):.1f}% | Rev CAGR: {'n/a' if cagr is None else f'{cagr:+.1f}%'}\n"
            f"{q.get('driver','')}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    return bytes(pdf.output())


def simply_wall_st_url(ticker, company_name, sector, industry, exchange):
    search_url = f"https://simplywall.st/search?{urllib.parse.urlencode({'q': ticker})}"
    exchange_aliases = {
        "nasdaq": "nasdaq", "nasdaqgs": "nasdaq", "nasdaqgm": "nasdaq", "nasdaqcm": "nasdaq",
        "nms": "nasdaq", "ngm": "nasdaq", "ncm": "nasdaq",
        "nyse": "nyse", "nyq": "nyse",
        "nysearca": "nysearca", "arca": "nysearca", "pcx": "nysearca",
        "nyseamerican": "nyseamerican", "amex": "nyseamerican", "ase": "nyseamerican",
        "bats": "bats", "bts": "bats",
    }
    exchange_key = re.sub(r"[^a-z]", "", (exchange or "").lower())
    exchange_slug = exchange_aliases.get(exchange_key)
    if not exchange_slug:
        return search_url

    industry_key = (industry or "").lower()
    sector_key = (sector or "").lower()
    category = None
    if sector_key == "energy" or re.search(r"oil|gas|coal|uranium", industry_key):
        category = "energy"
    elif re.search(r"software", industry_key):
        category = "software"
    elif re.search(r"semiconductor|consumer electronics|computer hardware|communication equipment", industry_key):
        category = "tech"
    elif re.search(r"biotechnology", industry_key):
        category = "biotechnology"
    elif re.search(r"pharmaceutical|drug manufacturer", industry_key):
        category = "pharmaceuticals"
    elif re.search(r"medical device|medical equipment|medical instrument", industry_key):
        category = "medical-equipment"
    elif re.search(r"healthcare provider|healthcare plan|medical care facilit", industry_key):
        category = "healthcare-services"
    elif "bank" in industry_key:
        category = "banks"
    elif "insurance" in industry_key:
        category = "insurance"
    elif "reit" in industry_key or sector_key == "real estate":
        category = "real-estate"
    elif re.search(r"retail|department store", industry_key):
        category = "retail"
    elif re.search(r"restaurant|hotel|travel|leisure", industry_key):
        category = "consumer-services"
    elif sector_key == "utilities":
        category = "utilities"
    elif sector_key == "basic materials":
        category = "materials"
    elif sector_key == "financial services":
        category = "diversified-financials"
    elif sector_key == "technology":
        category = "tech"
    if not category:
        return search_url

    company_slug = re.sub(
        r"\b(?:incorporated|corporation|corp|inc|company|co|limited|ltd|plc|llc)\b",
        "",
        (company_name or "").lower(),
    )
    company_slug = re.sub(r"[^a-z0-9]+", "-", company_slug).strip("-")
    if not company_slug:
        return search_url

    ticker_slug = urllib.parse.quote(ticker.lower(), safe="-")
    return f"https://simplywall.st/stocks/us/{category}/{exchange_slug}-{ticker_slug}/{company_slug}"


def stockanalysis_url(ticker):
    slug = urllib.parse.quote((ticker or "").lower().replace(".", "-"), safe="-")
    return f"https://stockanalysis.com/stocks/{slug}/"


def render_results(all_qualified, all_failed):
        st.markdown("---")
        st.markdown("## Results: " + str(len(all_qualified)) + " qualified")
        failed_tickers = st.session_state.get("failed_tickers", [])
        missing_prices = st.session_state.get("missing_prices", [])
        if failed_tickers:
            st.error(f"⚠️ {len(failed_tickers)} tickers failed to fetch: {', '.join(failed_tickers)}")
        if missing_prices:
            st.error(f"⚠️ {len(missing_prices)} tickers missing from batch price data")
            st.caption(f"Missing price tickers: {', '.join(missing_prices)}")

        if all_qualified:
            for idx, q in enumerate(all_qualified, 1):
                tn = q.get("tier", "")
                tc = TIERS.get(tn, {}).get("color", "#555")
                cagr = q.get("cagr")
                cagr_html = (f"<b class='{'pos' if cagr >= 0 else 'neg'}'>{cagr:+.1f}%</b>"
                             if cagr is not None else "<b>—</b>")
                name = html.escape(q.get("company_name", ""))
                tkr = q.get("ticker", "")
                tkr_esc = html.escape(tkr)
                tkr_param = urllib.parse.quote(tkr)
                ss_url = f"https://app.stocksimplifier.com/simplify?ticker={tkr_param}"
                sw_url = simply_wall_st_url(
                    tkr, q.get("company_name", ""), q.get("sector", ""),
                    q.get("industry", ""), q.get("exchange", ""),
                )
                sa_url = stockanalysis_url(tkr)
                ext_icon = ("<svg viewBox='0 0 24 24'><path d='M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6'/>"
                            "<polyline points='15 3 21 3 21 9'/><line x1='10' y1='14' x2='21' y2='3'/></svg>")

                st.markdown(
                    f"<div class='card'><div class='card-head'><div>"
                    f"<div class='card-name'>{name} · {tkr_esc}"
                    f"<span class='tier-tag' style='background:{tc}'>{html.escape(tn)}</span></div>"
                    f"<div class='card-sub'>{html.escape(q.get('sector','N/A'))} / {html.escape(q.get('industry','N/A'))}</div>"
                    f"<div class='card-links'>"
                    f"<a class='ext-link' href='{ss_url}' target='_blank' rel='noopener noreferrer'>{ext_icon} Stock Simplifier</a>"
                    f"<a class='ext-link' href='{sw_url}' target='_blank' rel='noopener noreferrer'>{ext_icon} Simply Wall St</a>"
                                        f"<a class='ext-link' href='{sa_url}' target='_blank' rel='noopener noreferrer'>{ext_icon} StockAnalysis</a>"
                                        f"</div></div>"
                    f"<div class='card-price'>${q['price']:.2f}</div></div>"
                    f"<div class='metrics'>"
                    f"<div class='metric'><span>IV</span><b>{q['iv']:.1f}%</b></div>"
                    f"<div class='metric'><span>Avg volume</span><b>{(q.get('avg_volume') or 0):,}</b></div>"
                    f"<div class='metric'><span>Margin</span><b>{(q.get('margin') or 0):.1f}%</b></div>"
                    f"<div class='metric'><span>Rev CAGR</span>{cagr_html}</div></div>"
                    f"<div class='driver'>{html.escape(q.get('driver',''))}</div></div>",
                    unsafe_allow_html=True,
                )

            # ── CSV download ──
            csv_lines = []
            csv_lines.append("ticker,company_name,price,iv,avg_volume,margin,cagr,sector,industry,tier,options_liquid,driver,stock_simplifier_url,simply_wall_st_url,stockanalysis_url")
            for q in all_qualified:
                t_val = q.get("ticker", "")
                ss_lnk = f"https://app.stocksimplifier.com/simplify?ticker={urllib.parse.quote(t_val)}"
                sw_lnk = simply_wall_st_url(
                    t_val, q.get("company_name", ""), q.get("sector", ""),
                    q.get("industry", ""), q.get("exchange", ""),
                )
                row = (
                    t_val,
                    q.get("company_name","").replace(","," "),
                    q.get("price",0),
                    q.get("iv",0),
                    q.get("avg_volume") or 0,
                    q.get("margin") or 0,
                    q.get("cagr") or 0,
                    q.get("sector","").replace(","," "),
                    q.get("industry","").replace(","," "),
                    q.get("tier",""),
                    "Yes" if q.get("options_liquid") else "No",
                    q.get("driver","").replace(","," "),
                    ss_lnk,
                    sw_lnk,
                    stockanalysis_url(t_val),
                )
                csv_lines.append(",".join(str(v) for v in row))
            csv_data = "\n".join(csv_lines)

            with st.popover("📥 Download Results"):
                st.download_button(
                    "CSV",
                    data=csv_data,
                    file_name="francine_screener_v3_results.csv",
                    mime="text/csv",
                    key="dl_csv",
                    use_container_width=True,
                )
                st.download_button(
                    "PDF",
                    data=build_pdf(all_qualified),
                    file_name="francine_screener_v3_results.pdf",
                    mime="application/pdf",
                    key="dl_pdf",
                    use_container_width=True,
                )
        else:
            st.warning("No qualified companies found.")

        with st.expander(f"📊 Failure Summary ({len(all_failed)} total)"):
            st.markdown(f"**Fetch errors: {sum(r.get('status') == 'fetch_error' for r in all_failed)}**")
            st.markdown(f"**Filter rejections: {sum(r.get('status', 'rejected') == 'rejected' for r in all_failed)}**")
            st.markdown(f"**Missing batch prices: {len(missing_prices)}**")
            for status, label in (("fetch_error", "Fetch errors"), ("rejected", "Filter rejections")):
                status_rows = [r for r in all_failed if r.get("status", "rejected") == status]
                if status_rows:
                    st.markdown(f"**{label} by reason**")
                    reasons = Counter(r.get("fail_reason", "Unknown")[:200] for r in status_rows)
                    for reason, count in reasons.most_common(10):
                        st.markdown(f"- **{reason}**: {count}")


if "results" in st.session_state:
    render_results(*st.session_state.results)