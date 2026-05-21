{\rtf1\ansi\ansicpg1252\cocoartf2868
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\paperw11900\paperh16840\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx566\tx1133\tx1700\tx2267\tx2834\tx3401\tx3968\tx4535\tx5102\tx5669\tx6236\tx6803\pardirnatural\partightenfactor0

\f0\fs24 \cf0 from flask import Flask, render_template, request, jsonify\
import requests\
import pandas as pd\
from datetime import datetime, timedelta\
import json\
from functools import wraps\
\
app = Flask(__name__)\
\
# Configuraci\'f3n\
PASSWORD = "Temporal1$"\
FRED_API_KEY = "your_fred_api_key_here"\
\
# Umbrales definidos por Boss (2026-05-21)\
THRESHOLDS = \{\
    "DGS10": \{"normal": (0, 4.5), "tension": (4.51, 4.65), "crisis": (4.66, 100)\},\
    "MMNRNJ": \{"normal": (0, 85), "tension": (85, 95), "crisis": (95, 1000)\},\
    "DXY": \{"normal": (0, 98.5), "tension": (98.5, 99.5), "crisis": (99.5, 200)\},\
    "WRESBAL": \{"normal": (3000, 100000), "tension": (2800, 3000), "crisis": (0, 2800)\},\
    "WTREGEN": \{"normal": (0, 700), "tension": (700, 900), "crisis": (900, 10000)\},\
    "BAMLC0A4CBBB": \{"normal": (0, 1.05), "tension": (1.05, 1.15), "crisis": (1.15, 100)\},\
    "BAMLH0A0HYM2": \{"normal": (0, 3.5), "tension": (3.5, 4.5), "crisis": (4.5, 100)\},\
    "HYG": \{"normal": (80, 10000), "tension": (78.5, 80), "crisis": (0, 78.5)\},\
\}\
\
INDICATORS = \{\
    "DGS10": "Rendimiento 10Y US",\
    "MMNRNJ": "MOVE Index (volatilidad bonos)",\
    "DXY": "\'cdndice D\'f3lar US",\
    "WRESBAL": "Reserve Balances (Fed)",\
    "WTREGEN": "Treasury General Account",\
    "BAMLC0A4CBBB": "BBB OAS (Corporate)",\
    "BAMLH0A0HYM2": "High Yield OAS",\
    "HYG": "iShares HY ETF (Precio)",\
\}\
\
def check_auth(f):\
    @wraps(f)\
    def decorated_function(*args, **kwargs):\
        password = request.headers.get('X-Password')\
        if password != PASSWORD:\
            return jsonify(\{"error": "Unauthorized"\}), 401\
        return f(*args, **kwargs)\
    return decorated_function\
\
def get_status(value, indicator):\
    thresholds = THRESHOLDS.get(indicator, \{\})\
    if "crisis" in thresholds and thresholds["crisis"][0] <= value <= thresholds["crisis"][1]:\
        return "red"\
    elif "tension" in thresholds and thresholds["tension"][0] <= value <= thresholds["tension"][1]:\
        return "yellow"\
    elif "normal" in thresholds and thresholds["normal"][0] <= value <= thresholds["normal"][1]:\
        return "green"\
    return "gray"\
\
def fetch_fred_data(series_id):\
    try:\
        url = f"https://api.stlouisfed.org/fred/series/observations?series_id=\{series_id\}&api_key=\{FRED_API_KEY\}&file_type=json"\
        response = requests.get(url, timeout=5)\
        if response.status_code == 200:\
            data = response.json()\
            observations = data.get('observations', [])\
            return observations\
        return []\
    except Exception as e:\
        print(f"Error fetching \{series_id\}: \{e\}")\
        return []\
\
@app.route('/')\
def index():\
    return render_template('index.html')\
\
@app.route('/api/data', methods=['GET'])\
@check_auth\
def get_data():\
    result = \{\}\
    for indicator_code, indicator_name in INDICATORS.items():\
        observations = fetch_fred_data(indicator_code)\
        if observations:\
            last_obs = observations[-1]\
            current_value = float(last_obs['value']) if last_obs['value'] != '.' else None\
            last_date = last_obs['date']\
            last_three = []\
            for obs in observations[-3:]:\
                if obs['value'] != '.':\
                    last_three.append(\{\
                        'date': obs['date'],\
                        'value': float(obs['value'])\
                    \})\
            cutoff_date = datetime.now() - timedelta(days=365)\
            year_data = []\
            for obs in observations:\
                if obs['value'] != '.':\
                    try:\
                        obs_date = datetime.strptime(obs['date'], '%Y-%m-%d')\
                        if obs_date >= cutoff_date:\
                            year_data.append(\{\
                                'date': obs['date'],\
                                'value': float(obs['value'])\
                            \})\
                    except:\
                        pass\
            status = get_status(current_value, indicator_code) if current_value else "gray"\
            result[indicator_code] = \{\
                'name': indicator_name,\
                'current': current_value,\
                'date': last_date,\
                'last_three': last_three,\
                'year_data': year_data,\
                'status': status\
            \}\
    return jsonify(result)\
\
@app.route('/api/thresholds', methods=['GET'])\
@check_auth\
def get_thresholds():\
    return jsonify(THRESHOLDS)\
\
@app.route('/api/thresholds', methods=['POST'])\
@check_auth\
def update_thresholds():\
    global THRESHOLDS\
    data = request.json\
    THRESHOLDS = data\
    return jsonify(\{"status": "ok"\})\
\
if __name__ == '__main__':\
    app.run(debug=False, host='0.0.0.0', port=5000)}