from flask import Flask, render_template, request, jsonify
import requests
import os
from datetime import datetime, timedelta
import json
from functools import wraps

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

def fetch_polygon_data(ticker):
    """Fetch daily stock data from Polygon.io"""
    try:
        url = f"https://api.polygon.io/v1/open-close/{ticker}/2026-05-20?adjusted=true&apiKey={POLYGON_API_KEY}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'OK':
                # Polygon retorna un día a la vez, necesitamos hacer múltiples requests
                # Por ahora retornamos datos básicos
                return [{
                    'date': data.get('from'),
                    'value': float(data.get('c', 0))
                }]
        return []
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return []

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data', methods=['GET'])
@check_auth
def get_data():
    """Retorna datos de todos los indicadores"""
    result = {}

    for indicator_code, indicator_name in INDICATORS.items():
        if indicator_code == "HYG":
            observations = fetch_polygon_data("HYG")
        elif indicator_code == "MMNRNJ":
            observations = fetch_polygon_data("MOVE")
        else:
            observations = fetch_fred_data(indicator_code)

        if observations:
            # Últimos datos
            last_obs = observations[-1]
            current_value = float(last_obs['value']) if last_obs['value'] != '.' else None
            last_date = last_obs['date']

            # Últimos 3 valores
            last_three = []
            for obs in observations[-3:]:
                if obs['value'] != '.':
                    last_three.append({
                        'date': obs['date'],
                        'value': float(obs['value'])
                    })

            # Últimos 3 meses
            cutoff_date = datetime.now() - timedelta(days=90)
            year_data = []
            for obs in observations:
                if obs['value'] != '.':
                    try:
                        obs_date = datetime.strptime(obs['date'], '%Y-%m-%d')
                        if obs_date >= cutoff_date:
                            year_data.append({
                                'date': obs['date'],
                                'value': float(obs['value'])
                            })
                    except:
                        pass

            status = get_status(current_value, indicator_code) if current_value else "gray"

            result[indicator_code] = {
                'name': indicator_name,
                'current': current_value,
                'date': last_date,
                'last_three': last_three,
                'year_data': year_data,
                'status': status
            }

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
