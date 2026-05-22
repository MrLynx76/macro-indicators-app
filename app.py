from flask import Flask, render_template, request, jsonify
import requests
import os
import json
import random
import string
import time
from datetime import datetime, timedelta
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
# HYG (ETF) y MOVE Index no existen en FRED, y Stooq bloquea las IPs de
# datacenter (Render) -> "Max retries exceeded". Se obtienen de TradingView,
# que sí responde desde Render: el histórico diario por su feed websocket y,
# como respaldo, el valor actual por su endpoint REST.
NON_FRED = {
    "HYG":    {"tv_symbol": "AMEX:HYG"},
    "MMNRNJ": {"tv_symbol": "TVC:MOVE"},
}

TV_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
}

# Cache en memoria para no saturar TradingView (rate limits / reconexiones)
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

def fetch_tradingview_quote(symbol):
    """Endpoint REST no oficial de TradingView. Devuelve [{'date','value'}]
    con un único punto (valor actual). Respaldo si el histórico falla."""
    try:
        url = "https://scanner.tradingview.com/symbol"
        params = {"symbol": symbol, "fields": "close", "no_404": "true"}
        response = requests.get(url, params=params, headers=TV_HEADERS,
                                timeout=8)
        if response.status_code != 200:
            print(f"[TradingView-REST] {symbol}: HTTP {response.status_code}")
            return []
        close = response.json().get("close")
        if close is None:
            return []
        print(f"[TradingView-REST] OK {symbol}: {close}")
        return [{"date": datetime.now().strftime("%Y-%m-%d"),
                 "value": float(close)}]
    except Exception as e:
        print(f"[TradingView-REST] Error {symbol}: {str(e)[:60]}")
        return []

# --- Histórico diario de TradingView vía websocket -------------------------
# TradingView sirve los datos de gráfico por un websocket con un protocolo
# de tramas "~m~<longitud>~m~<contenido>". No requiere API key ni navegador.

def _tv_build_msg(func, params):
    """Construye una trama del protocolo TradingView: ~m~<len>~m~<json>."""
    body = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"

def _tv_iter_frames(buffer):
    """Itera las tramas ~m~<longitud>~m~<contenido> de un mensaje recibido."""
    i, n = 0, len(buffer)
    while i < n:
        if buffer[i:i + 3] != "~m~":
            break
        j = buffer.find("~m~", i + 3)
        if j == -1:
            break
        try:
            length = int(buffer[i + 3:j])
        except ValueError:
            break
        start = j + 3
        yield buffer[start:start + length]
        i = start + length

def _tv_extract_bars(data, into):
    """Extrae las barras OHLC de un mensaje 'timescale_update' al dict 'into'
    (clave = timestamp unix, valor = cierre)."""
    if not isinstance(data, dict) or data.get("m") != "timescale_update":
        return
    try:
        series = data["p"][1].get("sds_1", {})
    except (IndexError, KeyError, TypeError):
        return
    for item in series.get("s", []):
        v = item.get("v", [])
        # v = [timestamp, open, high, low, close, volume]
        if len(v) >= 5 and v[0] is not None and v[4] is not None:
            into[int(v[0])] = float(v[4])

def fetch_tradingview_history(symbol, bars=150):
    """Obtiene el histórico diario de TradingView por websocket.
    symbol: p.ej. 'AMEX:HYG' o 'TVC:MOVE'.
    Devuelve [{'date','value'}] en orden ascendente, o [] si algo falla.
    Nunca lanza excepción: ante cualquier fallo registra el error y
    devuelve [] para que se use el respaldo REST."""
    try:
        import websocket  # paquete 'websocket-client'
    except ImportError:
        print("[TradingView-WS] Falta el paquete 'websocket-client'")
        return []

    ws = None
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket",
            timeout=8,
            origin="https://www.tradingview.com",
            header=["User-Agent: Mozilla/5.0"],
        )
        session = "cs_" + "".join(
            random.choices(string.ascii_lowercase + string.digits, k=12))
        ws.send(_tv_build_msg("set_auth_token", ["unauthorized_user_token"]))
        ws.send(_tv_build_msg("chart_create_session", [session, ""]))
        ws.send(_tv_build_msg("resolve_symbol", [
            session, "sym_1",
            '={"symbol":"%s","adjustment":"splits"}' % symbol]))
        ws.send(_tv_build_msg("create_series", [
            session, "sds_1", "s1", "sym_1", "1D", bars, ""]))

        bars_by_ts = {}
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                chunk = ws.recv()
            except Exception:
                break
            if not chunk:
                continue
            for frame in _tv_iter_frames(chunk):
                if frame.startswith("~h~"):  # heartbeat: devolverlo igual
                    ws.send(f"~m~{len(frame)}~m~{frame}")
                    continue
                if not frame.startswith("{"):
                    continue
                try:
                    data = json.loads(frame)
                except Exception:
                    continue
                _tv_extract_bars(data, bars_by_ts)
            if "series_completed" in chunk:
                break

        observations = [
            {"date": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
             "value": val}
            for ts, val in sorted(bars_by_ts.items())
        ]
        if observations:
            print(f"[TradingView-WS] OK {symbol}: {len(observations)} barras")
        else:
            print(f"[TradingView-WS] Sin datos para {symbol}")
        return observations
    except Exception as e:
        print(f"[TradingView-WS] Error {symbol}: {str(e)[:90]}")
        return []
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

def fetch_indicator(indicator_code):
    """Enruta cada indicador a su fuente: FRED o TradingView."""
    cfg = NON_FRED.get(indicator_code)
    if not cfg:
        return fetch_fred_data(indicator_code)

    tv_symbol = cfg["tv_symbol"]
    # 1) Histórico diario completo vía websocket.
    obs = _cached(f"tvh:{tv_symbol}",
                  lambda: fetch_tradingview_history(tv_symbol))
    if obs:
        return obs
    # 2) Respaldo: valor actual vía REST (al menos muestra el dato de hoy).
    return _cached(f"tvq:{tv_symbol}",
                   lambda: fetch_tradingview_quote(tv_symbol))

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
