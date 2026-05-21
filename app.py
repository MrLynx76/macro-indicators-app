from flask import Flask, render_template, request, jsonify
import requests
import os
import csv
import io
from datetime import datetime, timedelta
import json
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# Configuración
PASSWORD = "Temporal1$"
FRED_API_KEY = os.environ.get('FRED_API_KEY', 'your_fred_api_key_here')
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY', 'your_polygon_api_key_here')

# Umbrales definidos por Boss (2026-05-21)
THRESHOLDS = {
    "DGS10": {"normal": (0, 4.5), "tension": (4.51, 4.65), "crisis": (4.66, 100)},
    "BAMLC0A4CBBB": {"normal": (0, 1.05), "tension": (1.05, 1.15), "crisis": (1.15, 100)},
    "HYG": {"normal": (80, 10000), "tension": (78.5, 80), "crisis": (0, 78.5)},
    "MMNRNJ": {"normal": (0, 200), "tension": (80, 120), "crisis": (120, 500)},
    "WRESBAL": {"normal": (3000, 100000), "tension": (2800, 3000), "crisis": (0, 2800)},
    "WTREGEN": {"normal": (0, 700), "tension": (700, 900), "crisis": (900, 10000)},
}

INDICATORS = {
    "DGS10": "Rendimiento US Bond 10 años",
    "BAMLC0A4CBBB": "Prima de riesgo US Bond BBB OAS",
    "HYG": "iShares iBoxx $ High Yield Corporate Bond ETF",
    "MMNRNJ": "MOVE Index (Volatilidad US Bond)",
    "WRESBAL": "FRED Reserve Balances",
    "WTREGEN": "FRED Treasury General Account",
}

def check_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        password = request.headers.get('X-Password')
        if password != PASSWORD:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated_function

def get_status(value, indicator):
    """Determina status (green/yellow/red) basado en umbrales"""
    thresholds = THRESHOLDS.get(indicator, {})

    if "crisis" in thresholds and thresholds["crisis"][0] <= value <= thresholds["crisis"][1]:
        return "red"
    elif "tension" in thresholds and thresholds["tension"][0] <= value <= thresholds["tension"][1]:
        return "yellow"
    elif "normal" in thresholds and thresholds["normal"][0] <= value <= thresholds["normal"][1]:
        return "green"
    return "gray"

def fetch_fred_data(series_id):
    """Fetch data from FRED API"""
    try:
        url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            observations = data.get('observations', [])
            return observations
        return []
    except Exception as e:
        print(f"Error fetching {series_id}: {e}")
        return []

# --- Fuentes para indicadores que NO son series FRED -----------------------
# HYG (ETF) y MOVE Index no existen en FRED. Se obtienen de Stooq (CSV diario
# con histórico) y, como respaldo, del endpoint no oficial de TradingView
# (sin JavaScript ni Selenium: funciona en el plan gratuito de Render).
NON_FRED = {
    "HYG":    {"source": "stooq", "symbol": "hyg.us"},
    "MMNRNJ": {"source": "stooq", "symbol": "^move",
               "fallback": ("tradingview", "TVC:MOVE")},
}

TV_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
}

# Cache en memoria para no saturar Stooq/TradingView (rate limits)
_CACHE = {}
CACHE_TTL = 600  # segundos (10 min)

def _cached(key, fn):
    """Devuelve el resultado de fn() cacheado durante CACHE_TTL segundos."""
    now = datetime.now().timestamp()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = fn()
    if value:  # solo se cachean resultados no vacíos
        _CACHE[key] = (now, value)
    return value

def fetch_stooq(symbol):
    """Descarga el CSV diario de Stooq. Devuelve observaciones
    [{'date','value'}] en orden ascendente, mismo formato que fetch_fred_data()."""
    try:
        d2 = datetime.now()
        d1 = d2 - timedelta(days=160)
        url = (f"https://stooq.com/q/d/l/?s={symbol}&i=d"
               f"&d1={d1:%Y%m%d}&d2={d2:%Y%m%d}")
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                timeout=8)
        if response.status_code != 200 or "Date,Open" not in response.text:
            print(f"[Stooq] Sin datos para {symbol}")
            return []
        observations = []
        for row in csv.DictReader(io.StringIO(response.text)):
            close = (row.get("Close") or "").strip()
            if close and close not in (".", "N/D"):
                observations.append({"date": row["Date"],
                                     "value": float(close)})
        print(f"[Stooq] OK {symbol}: {len(observations)} registros")
        return observations
    except Exception as e:
        print(f"[Stooq] Error {symbol}: {str(e)[:60]}")
        return []

def fetch_tradingview_quote(symbol):
    """Endpoint no oficial de TradingView (sin JS ni Selenium).
    symbol p.ej. 'TVC:MOVE'. Devuelve [{'date','value'}] con un único punto
    (valor actual), suficiente para mostrar el valor y calcular el status."""
    try:
        url = "https://scanner.tradingview.com/symbol"
        params = {"symbol": symbol, "fields": "close", "no_404": "true"}
        response = requests.get(url, params=params, headers=TV_HEADERS,
                                timeout=8)
        if response.status_code != 200:
            print(f"[TradingView] {symbol}: HTTP {response.status_code}")
            return []
        close = response.json().get("close")
        if close is None:
            return []
        print(f"[TradingView] OK {symbol}: {close}")
        return [{"date": datetime.now().strftime("%Y-%m-%d"),
                 "value": float(close)}]
    except Exception as e:
        print(f"[TradingView] Error {symbol}: {str(e)[:60]}")
        return []

def fetch_indicator(indicator_code):
    """Enruta cada indicador a su fuente: FRED, Stooq o TradingView."""
    cfg = NON_FRED.get(indicator_code)
    if not cfg:
        return fetch_fred_data(indicator_code)

    if cfg["source"] == "stooq":
        obs = _cached(f"stooq:{cfg['symbol']}",
                      lambda: fetch_stooq(cfg["symbol"]))
    else:
        obs = _cached(f"tv:{cfg['symbol']}",
                      lambda: fetch_tradingview_quote(cfg["symbol"]))

    # Fallback si la fuente principal no devolvió datos
    if not obs and "fallback" in cfg:
        fb_source, fb_symbol = cfg["fallback"]
        if fb_source == "tradingview":
            obs = _cached(f"tv:{fb_symbol}",
                          lambda: fetch_tradingview_quote(fb_symbol))
        elif fb_source == "stooq":
            obs = _cached(f"stooq:{fb_symbol}",
                          lambda: fetch_stooq(fb_symbol))
    return obs

@app.route('/')
def index():
    return render_template('index.html')

def process_observations(indicator_code, indicator_name, observations):
    """Convierte las observaciones de un indicador al formato de salida.
    Devuelve None si no hay datos utilizables."""
    if not observations:
        return None

    last_obs = observations[-1]
    current_value = float(last_obs['value']) if last_obs['value'] != '.' else None
    last_date = last_obs['date']

    # Últimos 3 valores
    last_three = []
    for obs in observations[-3:]:
        if obs['value'] != '.':
            last_three.append({'date': obs['date'],
                               'value': float(obs['value'])})

    # Últimos 3 meses
    cutoff_date = datetime.now() - timedelta(days=90)
    year_data = []
    for obs in observations:
        if obs['value'] != '.':
            try:
                obs_date = datetime.strptime(obs['date'], '%Y-%m-%d')
                if obs_date >= cutoff_date:
                    year_data.append({'date': obs['date'],
                                      'value': float(obs['value'])})
            except Exception:
                pass

    status = get_status(current_value, indicator_code) if current_value else "gray"

    return {
        'name': indicator_name,
        'current': current_value,
        'date': last_date,
        'last_three': last_three,
        'year_data': year_data,
        'status': status,
    }

@app.route('/api/data', methods=['GET'])
@check_auth
def get_data():
    """Retorna datos de todos los indicadores.
    SIEMPRE devuelve JSON válido (status 200), aunque alguna fuente falle:
    así el frontend nunca recibe una página de error HTML."""
    result = {}
    try:
        codes = list(INDICATORS.keys())
        # Descarga en paralelo: el tiempo total ≈ la llamada más lenta,
        # no la suma. Evita que gunicorn mate el worker por timeout.
        with ThreadPoolExecutor(max_workers=len(codes)) as executor:
            fetched = dict(zip(codes, executor.map(fetch_indicator, codes)))

        for indicator_code, indicator_name in INDICATORS.items():
            try:
                processed = process_observations(
                    indicator_code, indicator_name,
                    fetched.get(indicator_code))
                if processed:
                    result[indicator_code] = processed
            except Exception as e:
                print(f"[get_data] Error procesando {indicator_code}: {e}")
    except Exception as e:
        print(f"[get_data] Error general: {e}")

    return jsonify(result)

@app.route('/api/thresholds', methods=['GET'])
@check_auth
def get_thresholds():
    """Retorna umbrales actuales"""
    return jsonify(THRESHOLDS)

@app.route('/api/thresholds', methods=['POST'])
@check_auth
def update_thresholds():
    """Actualiza umbrales"""
    global THRESHOLDS
    data = request.json
    THRESHOLDS = data
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
