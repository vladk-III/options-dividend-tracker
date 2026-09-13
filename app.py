# ============================================================================
# 🌾 OPTIONS & DIVIDEND FARM DASHBOARD — Render.com version
# ============================================================================
# This is the hosted version of the dashboard: it runs as a small web service
# on Render.com instead of a Colab cell. Behavior is identical; only how it
# reads config/secrets and starts the server has changed.
#
# WHAT THIS TRACKS
#   1. Options trades you sell (Cash-Secured Puts & Covered Calls = "the wheel")
#   2. Dividend payments on stocks you hold
#   3. How options premium reduces the cost basis of the underlying stock
#      (full wheel accounting: assignment creates a lot, CC premium lowers
#      the lot's cost basis, assignment-away realizes the gain)
#
# DATA PERSISTENCE
#   Four CSVs live in a GitHub repo (this is a SEPARATE repo from the one
#   hosting this app's code) and are pulled/pushed via the GitHub Contents API:
#     - options_trades.csv
#     - seed_lots.csv        (shares you already owned before you started
#                              tracking the wheel here -- your starting cost basis)
#     - holdings.csv         (ticker + current shares you hold, for dividends)
#     - dividends_cache.csv  (dividend history pulled from Yahoo Finance via
#                              yfinance, cached here so you don't need to
#                              re-fetch every time the app starts)
#
#   Dividends are no longer entered by hand: hit "Sync dividends" and the
#   app pulls each holding's full dividend history from Yahoo Finance
#   (free, no API key) and applies your CURRENT share count to every past
#   payment. That's a simplification worth knowing -- if your share count
#   has changed over time, historical income here reflects what you'd have
#   earned at TODAY's share count, not what you actually received back then.
#
#   Everything else (the full lot ledger, realized gains, farm stats) is
#   RECOMPUTED each run by replaying seed_lots + options_trades in date
#   order. That keeps one source of truth and means you can never get the
#   derived numbers out of sync with what you actually logged.
#
# SETUP — see README.md in this same folder for the full walkthrough.
# ============================================================================

import base64
import json
import math
import os
import random
import time
import uuid
from datetime import datetime, date

import pandas as pd
import plotly.graph_objects as go
import requests
import yfinance as yf
from dash import Dash, dcc, html, Input, Output, State, ctx, dash_table, no_update

# ============================================================================
# CONFIG — edit these two, then set GITHUB_TOKEN as a Render environment
# variable (never hard-code the token itself here — see README.md)
# ============================================================================
GITHUB_OWNER = "your-github-username"
GITHUB_REPO = "options-dividend-tracker"      # repo must already exist
GITHUB_BRANCH = "main"
DATA_DIR = "data"                              # folder inside the repo

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise RuntimeError(
        "GITHUB_TOKEN is not set. In your Render service, go to "
        "Environment -> Add Environment Variable, name it GITHUB_TOKEN, "
        "and paste a GitHub personal access token with 'repo' scope."
    )

GOLD = "#D4AF37"
GREEN = "#4CAF50"
DARK_GREEN = "#2E7D32"
SOIL = "#6D4C41"
SKY = "#E8F5E9"

FILES = {
    "trades": f"{DATA_DIR}/options_trades.csv",
    "seed_lots": f"{DATA_DIR}/seed_lots.csv",
    "holdings": f"{DATA_DIR}/holdings.csv",
    "dividends": f"{DATA_DIR}/dividends_cache.csv",
}

TRADE_COLS = ["trade_id", "ticker", "opt_type", "strike", "premium_per_share",
              "contracts", "open_date", "expiration_date", "status",
              "close_date", "closing_debit_per_share", "notes", "rolled_from_trade_id"]
SEED_LOT_COLS = ["lot_id", "ticker", "shares", "cost_basis_per_share",
                  "open_date", "notes"]
HOLDINGS_COLS = ["ticker", "shares", "notes"]
DIV_COLS = ["div_id", "ticker", "shares_owned", "div_per_share",
            "payment_date", "notes"]

# ============================================================================
# GITHUB CSV I/O
# ============================================================================
API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents"
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}",
           "Accept": "application/vnd.github+json"}


def load_csv_from_github(path, columns):
    r = requests.get(f"{API_BASE}/{path}", headers=HEADERS,
                      params={"ref": GITHUB_BRANCH})
    if r.status_code == 200:
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        if content.strip():
            df = pd.read_csv(pd.io.common.StringIO(content))
            for c in columns:
                if c not in df.columns:
                    df[c] = None
            return df[columns]
    return pd.DataFrame(columns=columns)


def save_csv_to_github(path, df, message):
    csv_str = df.to_csv(index=False)
    content_b64 = base64.b64encode(csv_str.encode("utf-8")).decode("utf-8")
    get_r = requests.get(f"{API_BASE}/{path}", headers=HEADERS,
                          params={"ref": GITHUB_BRANCH})
    sha = get_r.json().get("sha") if get_r.status_code == 200 else None
    payload = {"message": message, "content": content_b64, "branch": GITHUB_BRANCH}
    if sha:
        payload["sha"] = sha
    put_r = requests.put(f"{API_BASE}/{path}", headers=HEADERS, data=json.dumps(payload))
    return put_r.status_code in (200, 201)


def load_all():
    return (
        load_csv_from_github(FILES["trades"], TRADE_COLS),
        load_csv_from_github(FILES["seed_lots"], SEED_LOT_COLS),
        load_csv_from_github(FILES["holdings"], HOLDINGS_COLS),
        load_csv_from_github(FILES["dividends"], DIV_COLS),
    )


# ============================================================================
# WHEEL-STRATEGY COST BASIS ENGINE
# ============================================================================
# NOTE: this assumes one options trade's contracts*100 shares maps cleanly
# onto one lot (the normal case when you wheel 100-share blocks one at a
# time). If you ever cover a CC with shares split across two differently-
# priced lots, log two separate trade rows so each maps to one lot.

def rebuild_ledger(trades_df, seed_lots_df):
    lots = []
    for _, r in seed_lots_df.iterrows():
        lots.append({
            "lot_id": r["lot_id"], "ticker": r["ticker"],
            "shares": float(r["shares"]),
            "cost_basis_per_share": float(r["cost_basis_per_share"]),
            "open_date": r["open_date"], "status": "open",
            "close_date": None, "realized_gain": 0.0,
            "premium_applied": 0.0,
        })

    income_events = []  # pure premium income (not applied to a lot's basis)
    realized_events = []  # closed-lot realized gains

    events = trades_df[trades_df["status"].isin(
        ["expired_worthless", "assigned", "rolled", "closed_early"])].copy()
    if events.empty:
        return lots, income_events, realized_events

    events["event_date"] = events["close_date"].fillna(events["expiration_date"])
    events["event_date"] = pd.to_datetime(events["event_date"])
    events = events.sort_values("event_date")

    for _, t in events.iterrows():
        shares_covered = float(t["contracts"]) * 100
        closing_debit = float(t["closing_debit_per_share"]) if pd.notna(t["closing_debit_per_share"]) else 0.0
        net_premium = float(t["premium_per_share"]) - closing_debit

        if t["opt_type"] == "CSP":
            if t["status"] == "assigned":
                lots.append({
                    "lot_id": f"wheel-{t['trade_id']}", "ticker": t["ticker"],
                    "shares": shares_covered,
                    "cost_basis_per_share": float(t["strike"]) - net_premium,
                    "open_date": t["event_date"].date().isoformat(),
                    "status": "open", "close_date": None,
                    "realized_gain": 0.0, "premium_applied": net_premium * shares_covered,
                })
            else:
                income_events.append({
                    "date": t["event_date"], "ticker": t["ticker"],
                    "trade_id": t["trade_id"], "type": "CSP premium (no assignment)",
                    "amount": net_premium * shares_covered,
                })

        elif t["opt_type"] == "CC":
            open_lots = [l for l in lots if l["ticker"] == t["ticker"]
                         and l["status"] == "open" and l["shares"] >= shares_covered]
            if not open_lots:
                income_events.append({
                    "date": t["event_date"], "ticker": t["ticker"],
                    "trade_id": t["trade_id"],
                    "type": "CC premium (no matching open lot found)",
                    "amount": net_premium * shares_covered,
                })
                continue
            lot = open_lots[0]  # FIFO: first open lot with enough shares
            lot["cost_basis_per_share"] -= net_premium / (lot["shares"] / shares_covered)
            lot["premium_applied"] += net_premium * shares_covered

            if t["status"] == "assigned":
                gain = (float(t["strike"]) - lot["cost_basis_per_share"]) * shares_covered
                if lot["shares"] == shares_covered:
                    lot["status"] = "closed"
                    lot["close_date"] = t["event_date"].date().isoformat()
                    lot["realized_gain"] = gain
                else:
                    # split off the called-away portion, keep remainder open
                    lot["shares"] -= shares_covered
                    lots.append({
                        "lot_id": f"{lot['lot_id']}-called-{t['trade_id']}",
                        "ticker": t["ticker"], "shares": shares_covered,
                        "cost_basis_per_share": lot["cost_basis_per_share"],
                        "open_date": lot["open_date"], "status": "closed",
                        "close_date": t["event_date"].date().isoformat(),
                        "realized_gain": gain, "premium_applied": 0.0,
                    })
                realized_events.append({
                    "date": t["event_date"], "ticker": t["ticker"],
                    "trade_id": t["trade_id"], "amount": gain,
                })

    return lots, income_events, realized_events


def summarize(trades_df, seed_lots_df, dividends_df):
    lots, income_events, realized_events = rebuild_ledger(trades_df, seed_lots_df)
    lots_df = pd.DataFrame(lots)
    income_df = pd.DataFrame(income_events)
    realized_df = pd.DataFrame(realized_events)

    total_premium_collected = 0.0
    if not trades_df.empty:
        closed = trades_df[trades_df["status"].isin(
            ["expired_worthless", "assigned", "rolled", "closed_early"])].copy()
        if not closed.empty:
            closing_debit = closed["closing_debit_per_share"].fillna(0)
            total_premium_collected = (
                (closed["premium_per_share"] - closing_debit) * closed["contracts"] * 100
            ).sum()

    total_pure_income = income_df["amount"].sum() if not income_df.empty else 0.0
    total_basis_reduction = (lots_df["premium_applied"].sum()
                              if not lots_df.empty and "premium_applied" in lots_df else 0.0)
    total_realized_gain = realized_df["amount"].sum() if not realized_df.empty else 0.0
    total_dividends = ((dividends_df["shares_owned"] * dividends_df["div_per_share"]).sum()
                        if not dividends_df.empty else 0.0)

    return {
        "lots_df": lots_df, "income_df": income_df, "realized_df": realized_df,
        "total_premium_collected": total_premium_collected,
        "total_pure_income": total_pure_income,
        "total_basis_reduction": total_basis_reduction,
        "total_realized_gain": total_realized_gain,
        "total_dividends": total_dividends,
        "total_farm_income": total_premium_collected + total_dividends,
    }


def compute_dividend_rate(dividends_df):
    """Average $/second dividend accrual rate, based on total dividends logged
    divided by the time elapsed since your first logged payment. This is an
    AVERAGE historical rate for the ticking display — dividends don't
    literally arrive every second, this just animates your run-rate."""
    if dividends_df.empty:
        return {"base": 0.0, "rate_per_sec": 0.0, "start_epoch_ms": int(time.time() * 1000)}
    d = dividends_df.copy()
    d["payment_date"] = pd.to_datetime(d["payment_date"])
    total = float((d["shares_owned"] * d["div_per_share"]).sum())
    first_date = d["payment_date"].min()
    seconds_elapsed = max((datetime.now() - first_date.to_pydatetime()).total_seconds(), 86400)
    rate = total / seconds_elapsed
    return {"base": total, "rate_per_sec": rate, "start_epoch_ms": int(time.time() * 1000)}


# ============================================================================
# YAHOO FINANCE DIVIDEND SYNC
# ============================================================================
def sync_dividends_from_yfinance(holdings_df):
    """Pulls each holding's full dividend history from Yahoo Finance and
    applies the CURRENT share count from holdings_df to every past payment.
    (Yahoo doesn't know how many shares you held in the past -- only you do
    -- so this is an approximation of historical income, not your exact
    received amount, unless your share count has stayed constant.)"""
    rows = []
    for _, h in holdings_df.iterrows():
        ticker = str(h["ticker"]).upper()
        shares = float(h["shares"])
        try:
            series = yf.Ticker(ticker).dividends
            for dt, amt in series.items():
                ts = pd.Timestamp(dt)
                if ts.tzinfo is not None:
                    ts = ts.tz_localize(None)
                pay_date = ts.date().isoformat()
                rows.append({
                    "div_id": f"{ticker}-{pay_date}", "ticker": ticker,
                    "shares_owned": shares, "div_per_share": float(amt),
                    "payment_date": pay_date, "notes": "synced from yfinance",
                })
        except Exception as e:
            rows.append({"div_id": f"{ticker}-sync-error", "ticker": ticker,
                          "shares_owned": shares, "div_per_share": 0.0,
                          "payment_date": None, "notes": f"sync error: {e}"})
    return pd.DataFrame(rows, columns=DIV_COLS)


# ============================================================================
# DIVIDEND CALENDAR + GROWTH PROJECTION HELPERS
# ============================================================================
def dividend_ttm_and_cagr(div_df):
    """Per-ticker trailing-12-month income and historical dividend-per-share
    growth rate (CAGR between first and last logged payment)."""
    if div_df.empty:
        return {}
    d = div_df.copy()
    d["payment_date"] = pd.to_datetime(d["payment_date"])
    now = pd.Timestamp.now()
    out = {}
    for tkr, g in d.groupby("ticker"):
        g = g.sort_values("payment_date")
        ttm = g[g["payment_date"] >= now - pd.Timedelta(days=365)]
        ttm_income = float((ttm["shares_owned"] * ttm["div_per_share"]).sum())
        first, last = g.iloc[0], g.iloc[-1]
        years = max((last["payment_date"] - first["payment_date"]).days / 365.25, 0.01)
        if len(g) >= 2 and first["div_per_share"] > 0:
            cagr = (last["div_per_share"] / first["div_per_share"]) ** (1 / years) - 1
        else:
            cagr = 0.0
        out[tkr] = {
            "ttm": ttm_income, "cagr": cagr, "last_date": last["payment_date"],
            "last_div_per_share": float(last["div_per_share"]),
            "shares": float(last["shares_owned"]), "n_payments": len(g),
        }
    return out


def estimate_interval_days(div_df, ticker):
    d = div_df[div_df["ticker"] == ticker].copy()
    d["payment_date"] = pd.to_datetime(d["payment_date"])
    d = d.sort_values("payment_date")
    if len(d) < 2:
        return 91  # assume quarterly until we have 2+ data points
    diffs = d["payment_date"].diff().dropna().dt.days
    return max(int(diffs.median()), 1)


def crop_emoji(progress):
    if progress >= 0.95:
        return "🌾"
    if progress >= 0.6:
        return "🪴"
    if progress >= 0.25:
        return "🌿"
    return "🌱"


def build_calendar_fields(div_df):
    stats = dividend_ttm_and_cagr(div_df)
    now = pd.Timestamp.now()
    fields = []
    for tkr, s in stats.items():
        interval = estimate_interval_days(div_df, tkr)
        next_date = s["last_date"] + pd.Timedelta(days=interval)
        elapsed = (now - s["last_date"]).days
        progress = max(0.0, min(1.0, elapsed / interval))
        fields.append({
            "ticker": tkr, "progress": progress, "next_date": next_date,
            "est_amount": s["last_div_per_share"] * s["shares"],
            "interval_days": interval, "estimated_cadence": s["n_payments"] < 2,
        })
    fields.sort(key=lambda f: f["next_date"])
    return fields


def project_income(div_df, years=10, override_rate=None):
    """Projects total portfolio dividend income out `years` years, compounding
    each ticker's TTM income at either its own historical CAGR or a supplied
    override rate. Assumes current share counts stay constant (no DRIP)."""
    stats = dividend_ttm_and_cagr(div_df)
    yearly_totals = {y: 0.0 for y in range(0, years + 1)}
    if not stats:
        return pd.DataFrame({"year": list(yearly_totals.keys()), "income": 0.0}), {}
    per_ticker_proj = {}
    for tkr, s in stats.items():
        rate = override_rate if override_rate is not None else s["cagr"]
        base = s["ttm"] if s["ttm"] > 0 else s["last_div_per_share"] * s["shares"] * 4
        proj = []
        for y in range(0, years + 1):
            val = base * ((1 + rate) ** y)
            yearly_totals[y] += val
            proj.append(val)
        per_ticker_proj[tkr] = proj
    df = pd.DataFrame({"year": list(yearly_totals.keys()), "income": list(yearly_totals.values())})
    return df, per_ticker_proj


def build_growth_chart(df):
    fig = go.Figure()
    max_income = df["income"].max() if not df.empty else 1
    labels = ["Now" if y == 0 else f"Yr {y}" for y in df["year"]]
    colors = [GOLD if (r / max_income if max_income else 0) > 0.6 else GREEN for r in df["income"]]
    fig.add_trace(go.Bar(x=labels, y=df["income"], marker_color=colors,
                          text=[f"${v:,.0f}" for v in df["income"]], textposition="outside"))
    for label, val in zip(labels, df["income"]):
        frac = (val / max_income) if max_income else 0
        fig.add_annotation(x=label, y=val, text="🌳", showarrow=False,
                            yshift=14 + int(frac * 22), font=dict(size=14 + int(frac * 18)))
    fig.update_layout(title="Projected annual dividend income — future forest",
                       title_font_color=GOLD, plot_bgcolor="white", paper_bgcolor="white",
                       height=320, margin=dict(l=30, r=10, t=40, b=30), showlegend=False)
    return fig


# ============================================================================
# FARM VISUAL HELPERS
# ============================================================================
TIERS = [0, 100, 500, 1000, 2500, 5000, 10000, 25000]
STAGE_LABELS = ["Bare soil", "Seedling", "Sprouting", "Growing",
                "Young tree", "Small grove", "Orchard", "Full farm!"]
TREE_COUNTS = [0, 1, 3, 6, 10, 16, 24, 36]


def farm_stage(total):
    idx = 0
    for i, t in enumerate(TIERS):
        if total >= t:
            idx = i
    next_tier = TIERS[idx + 1] if idx + 1 < len(TIERS) else None
    progress = 1.0 if next_tier is None else (total - TIERS[idx]) / (next_tier - TIERS[idx])
    return idx, STAGE_LABELS[idx], progress, next_tier


def tree_positions(n):
    """Organic scatter using a golden-angle spiral so trees don't look gridded."""
    pts = []
    golden_angle = math.pi * (3 - math.sqrt(5))
    for i in range(n):
        r = 0.55 * math.sqrt(i + 1)
        theta = i * golden_angle
        pts.append((r * math.cos(theta), r * math.sin(theta)))
    return pts


def coin_pile_positions(total_div):
    """A little heap of coins in the corner of the plot, growing with total dividends."""
    random.seed(42)
    n = min(int(total_div // 10), 140)
    if n <= 0:
        return []
    pts = []
    for i in range(n):
        layer = i // 22
        angle = random.uniform(0, 2 * math.pi)
        radius = random.uniform(0, 0.55 + 0.04 * layer)
        x = 3.2 + radius * math.cos(angle)
        y = -3.2 + radius * math.sin(angle)
        z = 0.035 * layer + random.uniform(0, 0.015)
        pts.append((x, y, z))
    return pts


def build_farm_3d(stage_idx, total_dividends):
    """A rotatable 3D farm scene: ground plane + grove of trees + a gold coin pile."""
    fig = go.Figure()

    # Ground
    fig.add_trace(go.Surface(
        x=[[-5, 5], [-5, 5]], y=[[-5, -5], [5, 5]], z=[[0, 0], [0, 0]],
        showscale=False, colorscale=[[0, SOIL], [1, GREEN]], opacity=0.95,
        lighting=dict(diffuse=0.9, ambient=0.55, specular=0.1), hoverinfo="skip",
    ))

    # Trees: brown trunk lines + green canopy markers
    positions = tree_positions(TREE_COUNTS[stage_idx])
    if positions:
        tx, ty, tz = [], [], []
        for (x, y) in positions:
            tx += [x, x, None]
            ty += [y, y, None]
            tz += [0, 0.5, None]
        fig.add_trace(go.Scatter3d(x=tx, y=ty, z=tz, mode="lines",
                                    line=dict(color=SOIL, width=8),
                                    hoverinfo="skip", showlegend=False))
        fig.add_trace(go.Scatter3d(
            x=[p[0] for p in positions], y=[p[1] for p in positions],
            z=[0.55] * len(positions), mode="markers",
            marker=dict(size=16, color=GREEN, opacity=0.95), hoverinfo="skip", showlegend=False))

    # Gold coin pile
    coins = coin_pile_positions(total_dividends)
    if coins:
        fig.add_trace(go.Scatter3d(
            x=[c[0] for c in coins], y=[c[1] for c in coins], z=[c[2] for c in coins],
            mode="markers",
            marker=dict(size=5, color=GOLD, opacity=1, line=dict(width=1, color="#8a6d00")),
            hoverinfo="skip", showlegend=False))

    fig.update_layout(
        scene=dict(
            xaxis=dict(visible=False, range=[-5, 5]),
            yaxis=dict(visible=False, range=[-5, 5]),
            zaxis=dict(visible=False, range=[0, 3]),
            aspectmode="manual", aspectratio=dict(x=1, y=1, z=0.35),
            camera=dict(eye=dict(x=1.35, y=1.35, z=0.85)),
            bgcolor=SKY,
        ),
        margin=dict(l=0, r=0, t=0, b=0), height=320,
        paper_bgcolor=SKY, showlegend=False,
    )
    return fig


# ============================================================================
# DASH APP
# ============================================================================
app = Dash(__name__, suppress_callback_exceptions=True)
server = app.server  # exposes the underlying Flask app for gunicorn ("app:server")
app.title = "🌾 Options & Dividend Farm"

VIEWPORT = {
    "name": "viewport",
    "content": "width=device-width, initial-scale=1, maximum-scale=1",
}
app.index_string = """<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
{%favicon%}{%css%}</head><body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}
</footer></body></html>"""

CARD_STYLE = {
    "background": "white", "borderRadius": "16px", "padding": "16px",
    "margin": "10px 0", "boxShadow": "0 2px 8px rgba(0,0,0,0.08)",
}
INPUT_STYLE = {"width": "100%", "padding": "10px", "marginBottom": "10px",
               "borderRadius": "8px", "border": "1px solid #ccc", "fontSize": "16px"}
BTN_STYLE = {"width": "100%", "padding": "12px", "borderRadius": "10px",
             "border": "none", "background": DARK_GREEN, "color": "white",
             "fontSize": "16px", "fontWeight": "bold", "marginTop": "6px"}
GOLD_BTN_STYLE = {**BTN_STYLE, "background": GOLD, "color": "#3a2f00"}


def field(label, comp):
    return html.Div([html.Label(label, style={"fontWeight": "600", "fontSize": "14px"}), comp])


app.layout = html.Div(style={"background": SKY, "minHeight": "100vh",
                              "fontFamily": "Helvetica, Arial, sans-serif",
                              "maxWidth": "480px", "margin": "0 auto", "padding": "10px"}, children=[
    dcc.Store(id="store-trades"),
    dcc.Store(id="store-seedlots"),
    dcc.Store(id="store-holdings"),
    dcc.Store(id="store-dividends"),
    dcc.Store(id="store-dividend-rate"),
    dcc.Interval(id="ticker-interval", interval=1000, n_intervals=0),
    html.H2("🌾 Options & Dividend Farm", style={"textAlign": "center", "color": DARK_GREEN}),
    html.Div(id="save-status", style={"textAlign": "center", "fontSize": "13px", "color": "#888"}),
    html.Div(id="dividend-ticker-display", style={
        "textAlign": "center", "fontSize": "15px", "fontWeight": "bold", "color": GOLD,
        "background": "#fffbe8", "border": f"1px solid {GOLD}", "borderRadius": "10px",
        "padding": "8px", "margin": "6px 0"}),

    dcc.Tabs(id="tabs", value="farm", children=[
        dcc.Tab(label="🌻 Farm", value="farm"),
        dcc.Tab(label="📝 Options", value="options"),
        dcc.Tab(label="💵 Dividends", value="dividends"),
        dcc.Tab(label="📅 Calendar", value="calendar"),
        dcc.Tab(label="📈 Growth", value="growth"),
        dcc.Tab(label="🌱 Lots", value="lots"),
    ]),
    html.Div(id="tab-content"),
])


# ---- TAB: FARM OVERVIEW ----------------------------------------------------
def render_farm(s):
    stage_idx, stage_label, progress, next_tier = farm_stage(s["total_farm_income"])
    fig = build_farm_3d(stage_idx, s["total_dividends"])
    return html.Div([
        html.Div([
            html.Div(stage_label, style={"textAlign": "center", "fontWeight": "bold",
                                          "color": DARK_GREEN, "marginBottom": "4px"}),
            dcc.Graph(figure=fig, config={"displayModeBar": False, "scrollZoom": False},
                      style={"touchAction": "none"}),
            html.Div("🔄 drag to rotate the farm", style={"textAlign": "center", "fontSize": "11px", "color": "#999"}),
            html.Div(f"Total farm income: ${s['total_farm_income']:,.2f}",
                      style={"textAlign": "center", "marginTop": "6px"}),
            html.Div(
                style={"background": "#ddd", "borderRadius": "8px", "height": "14px",
                       "marginTop": "8px", "overflow": "hidden"},
                children=html.Div(style={"width": f"{progress*100:.0f}%", "background": GREEN,
                                          "height": "100%"})),
            html.Div(
                f"Next stage at ${next_tier:,.0f}" if next_tier else "Max stage reached! 🎉",
                style={"textAlign": "center", "fontSize": "12px", "color": "#666", "marginTop": "4px"}),
        ], style=CARD_STYLE),

        html.Div([
            html.Div("💰 Options premium", style={"fontWeight": "bold", "color": DARK_GREEN}),
            html.Div(f"Total collected: ${s['total_premium_collected']:,.2f}"),
            html.Div(f"→ Reduced cost basis: ${s['total_basis_reduction']:,.2f}", style={"fontSize": "13px"}),
            html.Div(f"→ Pure premium income: ${s['total_pure_income']:,.2f}", style={"fontSize": "13px"}),
            html.Div(f"→ Realized gains from assignment: ${s['total_realized_gain']:,.2f}", style={"fontSize": "13px"}),
        ], style=CARD_STYLE),

        html.Div([
            html.Div("🪙 Dividend gold pile", style={"fontWeight": "bold", "color": GOLD}),
            html.Div(f"Total dividends: ${s['total_dividends']:,.2f}", style={"marginTop": "6px"}),
        ], style=CARD_STYLE),
    ])


# ---- TAB: OPTIONS TRADES ---------------------------------------------------
def render_options(trades_df):
    table_df = trades_df.copy()
    return html.Div([
        html.Div([
            html.H4("Log an options trade", style={"color": DARK_GREEN}),
            field("Ticker", dcc.Input(id="in-ticker", type="text", style=INPUT_STYLE, placeholder="e.g. AAPL")),
            field("Type", dcc.Dropdown(id="in-opttype", options=["CSP", "CC"], value="CSP", style=INPUT_STYLE)),
            field("Strike", dcc.Input(id="in-strike", type="number", style=INPUT_STYLE)),
            field("Premium per share", dcc.Input(id="in-premium", type="number", style=INPUT_STYLE)),
            field("Contracts", dcc.Input(id="in-contracts", type="number", value=1, style=INPUT_STYLE)),
            field("Open date", dcc.DatePickerSingle(id="in-opendate", date=date.today(), style=INPUT_STYLE)),
            field("Expiration date", dcc.DatePickerSingle(id="in-expdate", date=date.today(), style=INPUT_STYLE)),
            field("Status", dcc.Dropdown(id="in-status",
                  options=["open", "expired_worthless", "assigned", "rolled", "closed_early"],
                  value="open", style=INPUT_STYLE)),
            field("Close date (if closed/rolled/assigned early)",
                  dcc.DatePickerSingle(id="in-closedate", style=INPUT_STYLE)),
            field("Closing debit per share (if bought back/rolled)",
                  dcc.Input(id="in-closedebit", type="number", style=INPUT_STYLE)),
            field("Notes", dcc.Input(id="in-notes", type="text", style=INPUT_STYLE)),
            html.Button("➕ Add trade", id="btn-add-trade", style=BTN_STYLE),
        ], style=CARD_STYLE),

        html.Div([
            html.H4("🔁 Roll an existing trade", style={"color": DARK_GREEN}),
            html.Div("Closes the selected trade and automatically opens the new one, linked together.",
                      style={"fontSize": "12px", "color": "#666", "marginBottom": "6px"}),
            field("Trade to roll", dcc.Dropdown(id="in-roll-select", options=open_trade_options(trades_df),
                                                  style=INPUT_STYLE, placeholder="Select an open trade")),
            field("Close date", dcc.DatePickerSingle(id="in-roll-closedate", date=date.today(), style=INPUT_STYLE)),
            field("Closing debit per share (cost to buy it back)",
                  dcc.Input(id="in-roll-closedebit", type="number", value=0, style=INPUT_STYLE)),
            html.Div("New trade:", style={"fontWeight": "600", "marginTop": "6px"}),
            field("New strike", dcc.Input(id="in-roll-newstrike", type="number", style=INPUT_STYLE)),
            field("New premium per share", dcc.Input(id="in-roll-newpremium", type="number", style=INPUT_STYLE)),
            field("New expiration date", dcc.DatePickerSingle(id="in-roll-newexp", date=date.today(), style=INPUT_STYLE)),
            html.Button("🔁 Execute roll", id="btn-roll-trade", style=GOLD_BTN_STYLE),
        ], style=CARD_STYLE),

        html.Div([
            html.H4("Trades", style={"color": DARK_GREEN}),
            dash_table.DataTable(
                data=table_df.to_dict("records"), columns=[{"name": c, "id": c} for c in TRADE_COLS],
                style_table={"overflowX": "auto"}, style_cell={"fontSize": "11px", "padding": "4px"},
                page_size=8),
        ], style=CARD_STYLE),
    ])


def open_trade_options(trades_df):
    if trades_df.empty:
        return []
    open_df = trades_df[trades_df["status"] == "open"]
    return [{"label": f"{r.ticker} {r.opt_type} ${r.strike} exp {r.expiration_date}", "value": r.trade_id}
            for r in open_df.itertuples()]


# ---- TAB: DIVIDEND CALENDAR ------------------------------------------------
def render_calendar(div_df):
    fields = build_calendar_fields(div_df)
    if not fields:
        return html.Div([html.Div("Log some dividends to see your calendar 🌾",
                                    style={"textAlign": "center", "color": "#666"})], style=CARD_STYLE)
    cards = []
    for f in fields:
        harvest_note = " — harvest ready! 🎉" if f["progress"] >= 1.0 else ""
        cadence_note = ("estimated cadence (only 1 payment logged so far)" if f["estimated_cadence"]
                         else f"~every {f['interval_days']} days")
        cards.append(html.Div([
            html.Div([
                html.Span(crop_emoji(f["progress"]), style={"fontSize": "28px", "marginRight": "8px"}),
                html.Span(f["ticker"], style={"fontWeight": "bold", "fontSize": "18px"}),
            ]),
            html.Div(style={"background": "#ddd", "borderRadius": "8px", "height": "12px",
                             "margin": "8px 0", "overflow": "hidden"},
                      children=html.Div(style={"width": f"{f['progress']*100:.0f}%",
                                                "background": GREEN, "height": "100%"})),
            html.Div(f"{f['progress']*100:.0f}% grown{harvest_note}", style={"fontSize": "12px", "color": "#666"}),
            html.Div(f"🗓️ Est. next payment: {f['next_date'].date()}  (~${f['est_amount']:,.2f})",
                      style={"fontSize": "13px", "marginTop": "4px"}),
            html.Div(cadence_note, style={"fontSize": "11px", "color": "#999"}),
        ], style=CARD_STYLE))
    return html.Div(cards)


# ---- TAB: FUTURE DIVIDEND GROWTH -------------------------------------------
def render_growth(div_df):
    default_df, _ = project_income(div_df, years=10, override_rate=None)
    fig = build_growth_chart(default_df)
    stats = dividend_ttm_and_cagr(div_df)
    table_rows = [{"ticker": tkr, "ttm_income": round(s["ttm"], 2),
                    "historical_annual_growth_pct": round(s["cagr"] * 100, 2)}
                   for tkr, s in stats.items()]
    return html.Div([
        html.Div([
            html.H4("📈 Future Dividend Forest", style={"color": GOLD}),
            html.Div("Projects your total dividend income forward using each stock's own "
                      "historical dividend-growth rate, assuming your current share counts "
                      "(no reinvestment). Override below to test a flat rate for everything.",
                      style={"fontSize": "12px", "color": "#666"}),
            field("Override growth rate % (optional, applies to all tickers)",
                  dcc.Input(id="in-growth-override", type="number", placeholder="e.g. 5", style=INPUT_STYLE)),
            html.Button("🔄 Recalculate forest", id="btn-recalc-growth", style=GOLD_BTN_STYLE),
        ], style=CARD_STYLE),
        html.Div(id="growth-chart-area", style=CARD_STYLE,
                  children=[dcc.Graph(figure=fig, config={"displayModeBar": False})]),
        html.Div([
            html.H4("Per-ticker growth assumptions", style={"color": DARK_GREEN}),
            dash_table.DataTable(
                data=table_rows,
                columns=[{"name": c, "id": c} for c in
                         ["ticker", "ttm_income", "historical_annual_growth_pct"]],
                style_cell={"fontSize": "12px", "padding": "4px"}, page_size=10),
        ], style=CARD_STYLE),
    ])


# ---- TAB: DIVIDENDS --------------------------------------------------------
def render_dividends(div_df, holdings_df):
    fig = go.Figure()
    if not div_df.empty:
        d = div_df.copy()
        d["payment_date"] = pd.to_datetime(d["payment_date"])
        d["total_amount"] = d["shares_owned"] * d["div_per_share"]
        d = d.sort_values("payment_date")
        for tkr, g in d.groupby("ticker"):
            fig.add_trace(go.Scatter(x=g["payment_date"], y=g["div_per_share"],
                                      mode="lines+markers", name=tkr,
                                      line=dict(color=GOLD, width=2),
                                      marker=dict(size=7, color=GOLD)))
    fig.update_layout(title="Dividend per share growth over time", title_font_color=GOLD,
                       plot_bgcolor="white", paper_bgcolor="white",
                       margin=dict(l=30, r=10, t=40, b=30), height=280,
                       legend=dict(orientation="h"))

    return html.Div([
        html.Div([
            html.H4("Your holdings", style={"color": GOLD}),
            html.Div("Dividend payments are pulled automatically from Yahoo Finance for "
                      "these tickers — you don't log payments by hand. Your current share "
                      "count is applied to the whole pulled history (see note in the script "
                      "if your share count has changed over time).",
                      style={"fontSize": "12px", "color": "#666", "marginBottom": "6px"}),
            field("Ticker", dcc.Input(id="in-hold-ticker", type="text", style=INPUT_STYLE, placeholder="e.g. KO")),
            field("Current shares", dcc.Input(id="in-hold-shares", type="number", style=INPUT_STYLE)),
            html.Button("➕ Add / update holding", id="btn-add-holding", style=GOLD_BTN_STYLE),
            dash_table.DataTable(
                data=holdings_df.to_dict("records"), columns=[{"name": c, "id": c} for c in HOLDINGS_COLS],
                style_table={"overflowX": "auto", "marginTop": "10px"},
                style_cell={"fontSize": "12px", "padding": "4px"}, page_size=8),
            html.Button("🔄 Sync dividends from Yahoo Finance", id="btn-sync-dividends",
                        style={**BTN_STYLE, "marginTop": "10px"}),
        ], style=CARD_STYLE),
        html.Div([dcc.Graph(figure=fig, config={"displayModeBar": False})], style=CARD_STYLE),
        html.Div([
            html.H4("Dividend log (from Yahoo Finance)", style={"color": GOLD}),
            dash_table.DataTable(
                data=div_df.to_dict("records"), columns=[{"name": c, "id": c} for c in DIV_COLS],
                style_table={"overflowX": "auto"}, style_cell={"fontSize": "11px", "padding": "4px"},
                page_size=8, sort_by=[{"column_id": "payment_date", "direction": "desc"}]),
        ], style=CARD_STYLE),
    ])


# ---- TAB: LOTS / COST BASIS ------------------------------------------------
def render_lots(s, seed_lots_df):
    lots_df = s["lots_df"]
    return html.Div([
        html.Div([
            html.H4("Seed a starting lot (shares you already owned)", style={"color": DARK_GREEN}),
            field("Ticker", dcc.Input(id="in-lot-ticker", type="text", style=INPUT_STYLE)),
            field("Shares", dcc.Input(id="in-lot-shares", type="number", style=INPUT_STYLE)),
            field("Original cost basis per share", dcc.Input(id="in-lot-basis", type="number", style=INPUT_STYLE)),
            field("Open date", dcc.DatePickerSingle(id="in-lot-date", date=date.today(), style=INPUT_STYLE)),
            html.Button("➕ Add seed lot", id="btn-add-lot", style=BTN_STYLE),
        ], style=CARD_STYLE),
        html.Div([
            html.H4("Lot ledger (auto-computed from trades)", style={"color": DARK_GREEN}),
            dash_table.DataTable(
                data=(lots_df.to_dict("records") if not lots_df.empty else []),
                columns=[{"name": c, "id": c} for c in
                         ["lot_id", "ticker", "shares", "cost_basis_per_share", "status",
                          "open_date", "close_date", "realized_gain", "premium_applied"]],
                style_table={"overflowX": "auto"}, style_cell={"fontSize": "10px", "padding": "4px"},
                page_size=10),
        ], style=CARD_STYLE),
    ])


# ============================================================================
# CALLBACKS
# ============================================================================
@app.callback(
    Output("store-trades", "data"), Output("store-seedlots", "data"),
    Output("store-holdings", "data"), Output("store-dividends", "data"),
    Input("tabs", "value"), prevent_initial_call=False,
)
def initial_load(_):
    trades_df, seed_df, holdings_df, div_df = load_all()
    return (trades_df.to_json(orient="split"), seed_df.to_json(orient="split"),
            holdings_df.to_json(orient="split"), div_df.to_json(orient="split"))


@app.callback(Output("tab-content", "children"),
              Input("tabs", "value"), Input("store-trades", "data"),
              Input("store-seedlots", "data"), Input("store-holdings", "data"),
              Input("store-dividends", "data"))
def render_tab(tab, trades_json, seed_json, holdings_json, div_json):
    if not trades_json:
        return html.Div("Loading...", style={"textAlign": "center", "padding": "40px"})
    trades_df = pd.read_json(trades_json, orient="split") if trades_json else pd.DataFrame(columns=TRADE_COLS)
    seed_df = pd.read_json(seed_json, orient="split") if seed_json else pd.DataFrame(columns=SEED_LOT_COLS)
    holdings_df = pd.read_json(holdings_json, orient="split") if holdings_json else pd.DataFrame(columns=HOLDINGS_COLS)
    div_df = pd.read_json(div_json, orient="split") if div_json else pd.DataFrame(columns=DIV_COLS)

    if tab == "farm":
        s = summarize(trades_df, seed_df, div_df)
        return render_farm(s)
    elif tab == "options":
        return render_options(trades_df)
    elif tab == "dividends":
        return render_dividends(div_df, holdings_df)
    elif tab == "calendar":
        return render_calendar(div_df)
    elif tab == "growth":
        return render_growth(div_df)
    elif tab == "lots":
        s = summarize(trades_df, seed_df, div_df)
        return render_lots(s, seed_df)
    return html.Div()


@app.callback(Output("store-dividend-rate", "data"), Input("store-dividends", "data"))
def update_dividend_rate(div_json):
    div_df = pd.read_json(div_json, orient="split") if div_json else pd.DataFrame(columns=DIV_COLS)
    return compute_dividend_rate(div_df)


# Runs entirely in the browser every second — no server round trip, so it
# doesn't hit the GitHub API or slow down while you're just watching it tick.
app.clientside_callback(
    """
    function(n, rateData) {
        if (!rateData) { return ""; }
        const elapsedSec = (Date.now() - rateData.start_epoch_ms) / 1000;
        const current = rateData.base + rateData.rate_per_sec * elapsedSec;
        const perSec = rateData.rate_per_sec;
        return "💧 Dividend income ticking: $" + current.toFixed(4) +
               "  (~$" + perSec.toFixed(6) + "/sec avg run-rate)";
    }
    """,
    Output("dividend-ticker-display", "children"),
    Input("ticker-interval", "n_intervals"),
    State("store-dividend-rate", "data"),
)


@app.callback(
    Output("store-trades", "data", allow_duplicate=True),
    Output("save-status", "children"),
    Input("btn-add-trade", "n_clicks"),
    State("in-ticker", "value"), State("in-opttype", "value"), State("in-strike", "value"),
    State("in-premium", "value"), State("in-contracts", "value"), State("in-opendate", "date"),
    State("in-expdate", "date"), State("in-status", "value"), State("in-closedate", "date"),
    State("in-closedebit", "value"), State("in-notes", "value"), State("store-trades", "data"),
    prevent_initial_call=True,
)
def add_trade(n, ticker, opttype, strike, premium, contracts, opendate, expdate,
              status, closedate, closedebit, notes, trades_json):
    if not n or not ticker or strike is None or premium is None:
        return no_update, "⚠️ Fill in ticker, strike, and premium."
    trades_df = pd.read_json(trades_json, orient="split") if trades_json else pd.DataFrame(columns=TRADE_COLS)
    new_row = {"trade_id": str(uuid.uuid4())[:8], "ticker": ticker.upper(), "opt_type": opttype,
               "strike": strike, "premium_per_share": premium, "contracts": contracts or 1,
               "open_date": opendate, "expiration_date": expdate, "status": status,
               "close_date": closedate, "closing_debit_per_share": closedebit, "notes": notes}
    trades_df = pd.concat([trades_df, pd.DataFrame([new_row])], ignore_index=True)
    ok = save_csv_to_github(FILES["trades"], trades_df, f"Add trade {new_row['trade_id']}")
    msg = "✅ Saved to GitHub" if ok else "❌ GitHub save failed — check token/repo config"
    return trades_df.to_json(orient="split"), msg


@app.callback(
    Output("store-holdings", "data", allow_duplicate=True),
    Output("save-status", "children", allow_duplicate=True),
    Input("btn-add-holding", "n_clicks"),
    State("in-hold-ticker", "value"), State("in-hold-shares", "value"), State("store-holdings", "data"),
    prevent_initial_call=True,
)
def add_or_update_holding(n, ticker, shares, holdings_json):
    if not n or not ticker or shares is None:
        return no_update, "⚠️ Fill in ticker and shares."
    holdings_df = pd.read_json(holdings_json, orient="split") if holdings_json else pd.DataFrame(columns=HOLDINGS_COLS)
    ticker = ticker.upper()
    if not holdings_df.empty and (holdings_df["ticker"] == ticker).any():
        holdings_df.loc[holdings_df["ticker"] == ticker, "shares"] = shares
    else:
        new_row = {"ticker": ticker, "shares": shares, "notes": ""}
        holdings_df = pd.concat([holdings_df, pd.DataFrame([new_row])], ignore_index=True)
    ok = save_csv_to_github(FILES["holdings"], holdings_df, f"Add/update holding {ticker}")
    msg = "✅ Saved to GitHub — hit Sync to pull its dividend history" if ok else "❌ GitHub save failed — check token/repo config"
    return holdings_df.to_json(orient="split"), msg


@app.callback(
    Output("store-dividends", "data", allow_duplicate=True),
    Output("save-status", "children", allow_duplicate=True),
    Input("btn-sync-dividends", "n_clicks"),
    State("store-holdings", "data"),
    prevent_initial_call=True,
)
def sync_dividends(n, holdings_json):
    if not n:
        return no_update, no_update
    holdings_df = pd.read_json(holdings_json, orient="split") if holdings_json else pd.DataFrame(columns=HOLDINGS_COLS)
    if holdings_df.empty:
        return no_update, "⚠️ Add a holding first."
    div_df = sync_dividends_from_yfinance(holdings_df)
    ok = save_csv_to_github(FILES["dividends"], div_df, "Sync dividends from yfinance")
    n_payments = len(div_df[div_df["payment_date"].notna()]) if not div_df.empty else 0
    msg = (f"✅ Synced {n_payments} dividend payments from Yahoo Finance"
           if ok else "❌ GitHub save failed — check token/repo config")
    return div_df.to_json(orient="split"), msg


@app.callback(
    Output("store-seedlots", "data", allow_duplicate=True),
    Output("save-status", "children", allow_duplicate=True),
    Input("btn-add-lot", "n_clicks"),
    State("in-lot-ticker", "value"), State("in-lot-shares", "value"),
    State("in-lot-basis", "value"), State("in-lot-date", "date"), State("store-seedlots", "data"),
    prevent_initial_call=True,
)
def add_seed_lot(n, ticker, shares, basis, opendate, seed_json):
    if not n or not ticker or shares is None or basis is None:
        return no_update, "⚠️ Fill in ticker, shares, and cost basis."
    seed_df = pd.read_json(seed_json, orient="split") if seed_json else pd.DataFrame(columns=SEED_LOT_COLS)
    new_row = {"lot_id": str(uuid.uuid4())[:8], "ticker": ticker.upper(), "shares": shares,
               "cost_basis_per_share": basis, "open_date": opendate, "notes": ""}
    seed_df = pd.concat([seed_df, pd.DataFrame([new_row])], ignore_index=True)
    ok = save_csv_to_github(FILES["seed_lots"], seed_df, f"Add seed lot {new_row['lot_id']}")
    msg = "✅ Saved to GitHub" if ok else "❌ GitHub save failed — check token/repo config"
    return seed_df.to_json(orient="split"), msg


@app.callback(
    Output("store-trades", "data", allow_duplicate=True),
    Output("save-status", "children", allow_duplicate=True),
    Input("btn-roll-trade", "n_clicks"),
    State("in-roll-select", "value"), State("in-roll-closedate", "date"),
    State("in-roll-closedebit", "value"), State("in-roll-newstrike", "value"),
    State("in-roll-newpremium", "value"), State("in-roll-newexp", "date"),
    State("store-trades", "data"),
    prevent_initial_call=True,
)
def roll_trade(n, old_trade_id, close_date, closing_debit, new_strike, new_premium,
                new_exp, trades_json):
    if not n or not old_trade_id or new_strike is None or new_premium is None:
        return no_update, "⚠️ Select a trade and fill in the new strike/premium."
    trades_df = pd.read_json(trades_json, orient="split") if trades_json else pd.DataFrame(columns=TRADE_COLS)
    match = trades_df["trade_id"] == old_trade_id
    if not match.any():
        return no_update, "⚠️ Couldn't find that trade."
    old_row = trades_df.loc[match].iloc[0]
    trades_df.loc[match, "status"] = "rolled"
    trades_df.loc[match, "close_date"] = close_date
    trades_df.loc[match, "closing_debit_per_share"] = closing_debit or 0

    new_trade_id = str(uuid.uuid4())[:8]
    new_row = {"trade_id": new_trade_id, "ticker": old_row["ticker"], "opt_type": old_row["opt_type"],
               "strike": new_strike, "premium_per_share": new_premium, "contracts": old_row["contracts"],
               "open_date": close_date, "expiration_date": new_exp, "status": "open",
               "close_date": None, "closing_debit_per_share": None,
               "notes": f"Rolled from {old_trade_id}", "rolled_from_trade_id": old_trade_id}
    trades_df = pd.concat([trades_df, pd.DataFrame([new_row])], ignore_index=True)
    ok = save_csv_to_github(FILES["trades"], trades_df, f"Roll {old_trade_id} -> {new_trade_id}")
    msg = "✅ Rolled and saved to GitHub" if ok else "❌ GitHub save failed — check token/repo config"
    return trades_df.to_json(orient="split"), msg


@app.callback(
    Output("growth-chart-area", "children"),
    Input("btn-recalc-growth", "n_clicks"),
    State("in-growth-override", "value"), State("store-dividends", "data"),
    prevent_initial_call=True,
)
def recalc_growth(n, override_pct, div_json):
    div_df = pd.read_json(div_json, orient="split") if div_json else pd.DataFrame(columns=DIV_COLS)
    rate = (override_pct / 100.0) if override_pct not in (None, "") else None
    df, _ = project_income(div_df, years=10, override_rate=rate)
    fig = build_growth_chart(df)
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


# ============================================================================
# RUN
# ============================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)
