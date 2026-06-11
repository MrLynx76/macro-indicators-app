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
# Preservar el orden de inserción de los indicadores en las respuestas JSON
# (Flask ordena claves alfabéticamente por defecto). Esto hace que el orden
# del dict INDICATORS sea el orden visible en el dashboard.
app.json.sort_keys = False

# Configuración
PASSWORD = "Temporal1$"
FRED_API_KEY = os.environ.get('FRED_API_KEY', 'your_fred_api_key_here')
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY', 'your_polygon_api_key_here')

# Umbrales definidos por Boss (2026-05-21)
THRESHOLDS = {
    "DGS10": {"normal": (0, 4.5), "tension": (4.51, 4.65), "crisis": (4.66, 100)},
    "BAMLC0A4CBBB": {"normal": (0, 1.05), "tension": (1.05, 1.15), "crisis": (1.15, 100)},
    "BAMLH0A0HYM2": {"normal": (0, 4.5), "tension": (4.5, 6.5), "crisis": (6.5, 100)},
    "HYG": {"normal": (80, 10000), "tension": (78.5, 80), "crisis": (0, 78.5)},
    "MMNRNJ": {"normal": (0, 200), "tension": (80, 120), "crisis": (120, 500)},
    "WRESBAL": {"normal": (3100000, 100000000), "tension": (2900000, 3100000), "crisis": (0, 2900000)},
    "WTREGEN": {"normal": (0, 800000), "tension": (800000, 900000), "crisis": (900000, 10000000)},
    # F&G: la banda alta (codicia extrema) suele anticipar techos.
    "FG_STOCKS": {"normal": (0, 75), "tension": (75, 85), "crisis": (85, 100)},
    "FG_CRYPTO": {"normal": (0, 75), "tension": (75, 85), "crisis": (85, 100)},
}

INDICATORS = {
    # Fila 1 (izquierda a derecha)
    "BAMLC0A4CBBB": "Prima de riesgo US Bond BBB OAS",
    "BAMLH0A0HYM2": "Prima de riesgo US Bond HY OAS",
    "HYG": "iShares iBoxx $ High Yield Corporate Bond ETF",
    # Fila 2 (izquierda a derecha)
    "DGS10": "Rendimiento US Bond 10 años",
    "WRESBAL": "U.S. Reserve Balances (Millions of $)",
    "WTREGEN": "U.S. Treasury General Account (Millions of $)",
    # Fila 3 (izquierda a derecha)
    "MMNRNJ": "MOVE Index (Volatilidad US Bond)",
    "FG_STOCKS": "Índice Codicia y Miedo Stocks (CNN)",
    "FG_CRYPTO": "Índice Codicia y Miedo Crypto",
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

def _fetch_fred_data_uncached(series_id):
    """Llama a la API de FRED con un reintento corto y timeout amplio.
    Devuelve la lista de observaciones o [] si falla."""
    url = (f"https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json")
    for attempt in (1, 2):
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                obs = response.json().get('observations', [])
                print(f"[FRED] OK {series_id}: {len(obs)} obs")
                return obs
            print(f"[FRED] {series_id}: HTTP {response.status_code} (intento {attempt})")
        except Exception as e:
            print(f"[FRED] Error {series_id} (intento {attempt}): {str(e)[:60]}")
    return []

def fetch_fred_data(series_id):
    """FRED con cache de 10 min. Evita que un fallo puntual de la API
    haga desaparecer indicadores entre refrescos."""
    return _cached(f"fred:{series_id}",
                   lambda: _fetch_fred_data_uncached(series_id))

# --- Fuentes para indicadores que NO son series FRED -----------------------
# HYG (ETF) y MOVE Index no existen en FRED, y Stooq bloquea las IPs de
# datacenter (Render) -> "Max retries exceeded". Se obtienen de TradingView,
# que sí responde desde Render: el histórico diario por su feed websocket y,
# como respaldo, el valor actual por su endpoint REST.
NON_FRED = {
    "HYG":       {"source": "tv", "symbol": "AMEX:HYG"},
    "MMNRNJ":    {"source": "tv", "symbol": "TVC:MOVE"},
    "FG_STOCKS": {"source": "cnn_fg"},
    "FG_CRYPTO": {"source": "altme_fg"},
}

# Indicadores Fear & Greed: muestran "semana anterior / día anterior / hoy"
# en vez de los 3 últimos valores, y cada valor lleva su clasificación.
FG_INDICATORS = {"FG_STOCKS", "FG_CRYPTO"}

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

# --- Fear & Greed indices --------------------------------------------------
# CNN expone un endpoint JSON no oficial pero estable; alternative.me tiene
# API oficial gratuita. Los dos responden desde Render. Cada observación
# lleva su clasificación textual (Miedo extremo / Miedo / Neutral / Codicia
# / Codicia extrema).

_FG_BANDS = [
    (24, "Miedo extremo"),
    (44, "Miedo"),
    (55, "Neutral"),
    (74, "Codicia"),
    (100, "Codicia extrema"),
]

def _fg_rating_es(value):
    """Clasificación en español del valor (0-100) de un F&G index."""
    v = int(round(value))
    for top, label in _FG_BANDS:
        if v <= top:
            return label
    return "Codicia extrema"

def fetch_cnn_fear_greed():
    """Fear & Greed Index de CNN (stocks). Endpoint no oficial pero estable.
    CNN exige cabeceras de navegador y suele responder mejor a la ruta con
    fecha; si esa falla, intenta la ruta sin fecha como respaldo.
    Devuelve observaciones diarias [{'date','value','rating'}] ascendente."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.cnn.com",
        "Referer": "https://www.cnn.com/",
    }
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # La ruta SIN fecha trae el histórico completo (~250 días).
    # La ruta CON fecha solo devuelve el snapshot de ese día, así que la
    # dejamos como último recurso por si la otra falla.
    urls = [
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{today}",
    ]
    best = []
    for url in urls:
        tag = url.rsplit("/", 1)[-1] or "graphdata"
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"[CNN-FG] GET .../{tag} -> HTTP {r.status_code}")
                continue
            payload = r.json()
            hist = (payload.get("fear_and_greed_historical") or {}).get("data", [])
            observations = []
            for point in hist:
                try:
                    ts_ms = point["x"]            # ms desde epoch
                    value = float(point["y"])     # 0-100
                    date_str = datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
                    observations.append({"date": date_str,
                                         "value": value,
                                         "rating": _fg_rating_es(value)})
                except Exception:
                    continue
            # CNN puede emitir varios puntos para el mismo día (actualiza
            # intradía). Nos quedamos con el último valor de cada fecha.
            observations.sort(key=lambda o: o["date"])
            deduped = {o["date"]: o for o in observations}
            observations = list(deduped.values())
            print(f"[CNN-FG] GET .../{tag} -> HTTP 200, {len(observations)} puntos")
            # Necesitamos histórico real (>5 días) para que la tarjeta
            # tenga sentido. Si esta URL solo trae 1-2 puntos, probamos otra.
            if len(observations) > 5:
                print(f"[CNN-FG] OK ({tag}): {len(observations)} obs")
                return observations
            if len(observations) > len(best):
                best = observations
        except Exception as e:
            print(f"[CNN-FG] Error en {tag}: {str(e)[:80]}")
    if best:
        print(f"[CNN-FG] Aviso: solo {len(best)} punto(s) disponibles")
    return best

def fetch_crypto_fear_greed():
    """Crypto Fear & Greed Index de alternative.me (API oficial gratuita)."""
    try:
        url = "https://api.alternative.me/fng/?limit=120&format=json"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            print(f"[Crypto-FG] HTTP {r.status_code}")
            return []
        observations = []
        for point in r.json().get("data", []):
            try:
                ts = int(point["timestamp"])
                value = float(point["value"])
                date_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                observations.append({"date": date_str,
                                     "value": value,
                                     "rating": _fg_rating_es(value)})
            except Exception:
                continue
        observations.sort(key=lambda o: o["date"])
        print(f"[Crypto-FG] OK: {len(observations)} obs")
        return observations
    except Exception as e:
        print(f"[Crypto-FG] Error: {str(e)[:80]}")
        return []

def fetch_indicator(indicator_code):
    """Enruta cada indicador a su fuente: FRED, TradingView o F&G."""
    cfg = NON_FRED.get(indicator_code)
    if not cfg:
        return fetch_fred_data(indicator_code)

    source = cfg["source"]
    if source == "tv":
        sym = cfg["symbol"]
        obs = _cached(f"tvh:{sym}", lambda: fetch_tradingview_history(sym))
        if obs:
            return obs
        return _cached(f"tvq:{sym}", lambda: fetch_tradingview_quote(sym))
    if source == "cnn_fg":
        return _cached("cnn_fg", fetch_cnn_fear_greed)
    if source == "altme_fg":
        return _cached("altme_fg", fetch_crypto_fear_greed)
    return []

@app.route('/')
def index():
    return render_template('index.html')

def _select_fg_last_three(observations):
    """Para F&G: devuelve [semana anterior, día anterior, hoy] con etiqueta.
    Cada uno debe tener una fecha distinta — si la fuente repite puntos
    para el mismo día, los saltamos."""
    valid = [o for o in observations
             if o.get('value') is not None and o.get('value') != '.']
    if not valid:
        return []
    today = valid[-1]
    today_date = today['date']

    # Día anterior = observación más reciente con fecha distinta a hoy.
    day_before = today
    for o in reversed(valid[:-1]):
        if o['date'] != today_date:
            day_before = o
            break

    # Semana anterior = la más cercana a hoy - 7 días, evitando
    # las fechas de "hoy" y "día anterior".
    target = datetime.strptime(today_date, '%Y-%m-%d') - timedelta(days=7)
    exclude = {today_date, day_before['date']}
    week_candidates = [o for o in valid if o['date'] not in exclude] or [today]
    week_ago = min(week_candidates,
                   key=lambda o: abs(
                       (datetime.strptime(o['date'], '%Y-%m-%d') - target).days))

    def _item(obs, label):
        out = {'label': label, 'date': obs['date'],
               'value': float(obs['value'])}
        if 'rating' in obs:
            out['rating'] = obs['rating']
        return out

    return [_item(week_ago, 'Semana anterior'),
            _item(day_before, 'Día anterior'),
            _item(today, 'Hoy')]

def process_observations(indicator_code, indicator_name, observations):
    """Convierte las observaciones de un indicador al formato de salida.
    Devuelve None si no hay datos utilizables.
    Para indicadores F&G, la tabla cambia a semana/día/hoy y cada valor
    lleva su rating; también añade 'current_rating' al resultado."""
    if not observations:
        return None

    last_obs = observations[-1]
    current_value = float(last_obs['value']) if last_obs['value'] != '.' else None
    last_date = last_obs['date']
    current_rating = last_obs.get('rating')

    # Tabla de 3 valores
    if indicator_code in FG_INDICATORS:
        last_three = _select_fg_last_three(observations)
    else:
        last_three = []
        for obs in observations[-3:]:
            if obs['value'] != '.':
                item = {'date': obs['date'], 'value': float(obs['value'])}
                if 'rating' in obs:
                    item['rating'] = obs['rating']
                last_three.append(item)

    # Gráfico de los últimos 3 meses
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

    result = {
        'name': indicator_name,
        'current': current_value,
        'date': last_date,
        'last_three': last_three,
        'year_data': year_data,
        'status': status,
    }
    if current_rating:
        result['current_rating'] = current_rating
    return result

@app.route('/api/data', methods=['GET'])
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
def get_thresholds():
    """Retorna umbrales actuales"""
    return jsonify(THRESHOLDS)

@app.route('/api/thresholds', methods=['POST'])
def update_thresholds():
    """Actualiza umbrales"""
    global THRESHOLDS
    data = request.json
    THRESHOLDS = data
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
