import ccxt
import time
import math
import json
import os
import threading
from datetime import datetime
import streamlit as st
from google import genai

# =====================================================================
# CONFIGURACIÓN GENERAL Y MEMORIA COMPARTIDA 24/7
# =====================================================================
SYMBOL = 'BTC/USDT'
TIMEFRAME = '1m'
COMISION = 0.001
RIESGO_POR_TRADE = 0.98
UMBRAL_PRE_COMPRA = 55
UMBRAL_COMPRA_FINAL = 65
ARCHIVO_ESTADO = 'estado_bot.json'

def obtener_api_key():
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return os.environ.get("GEMINI_API_KEY", "")

def conectar_exchange():
    # Prueba exchanges compatibles con servidores en la nube de EE. UU. y Europa
    for nombre in ['kraken', 'bybit', 'coinbase', 'binance']:
        try:
            ex = getattr(ccxt, nombre)({'enableRateLimit': True})
            ex.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=5)
            return ex, nombre.upper()
        except Exception:
            continue
    return ccxt.kraken({'enableRateLimit': True}), "KRAKEN"

# =====================================================================
# INDICADORES MATEMÁTICOS INSTITUCIONALES
# =====================================================================
def calcular_ema(precios, periodo):
    k = 2.0 / (periodo + 1)
    ema = [sum(precios[:periodo]) / periodo]
    for p in precios[periodo:]:
        ema.append(p * k + ema[-1] * (1.0 - k))
    return ema

def calcular_rsi_wilder(precios, periodo=14):
    deltas = [precios[i] - precios[i - 1] for i in range(1, len(precios))]
    ganancias = [max(d, 0.0) for d in deltas]
    perdidas = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(ganancias[:periodo]) / periodo
    avg_loss = sum(perdidas[:periodo]) / periodo
    for i in range(periodo, len(deltas)):
        avg_gain = (avg_gain * (periodo - 1) + ganancias[i]) / periodo
        avg_loss = (avg_loss * (periodo - 1) + perdidas[i]) / periodo
    return 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

def calcular_bollinger(precios, periodo=20, mult=2.0):
    ventana = precios[-periodo:]
    sma = sum(ventana) / periodo
    std = math.sqrt(sum((x - sma) ** 2 for x in ventana) / periodo)
    return sma - mult * std, sma, sma + mult * std

def calcular_macd(precios):
    ema12 = calcular_ema(precios, 12)
    ema26 = calcular_ema(precios, 26)
    n = len(ema26)
    linea = [ema12[-n + i] - ema26[i] for i in range(n)]
    senal = calcular_ema(linea, 9)
    return linea[-1] - senal[-1], linea[-2] - senal[-2]

def calcular_atr(velas, periodo=14):
    trs = [
        max(velas[i][2] - velas[i][3], abs(velas[i][2] - velas[i - 1][4]), abs(velas[i][3] - velas[i - 1][4]))
        for i in range(1, len(velas))
    ]
    return sum(trs[-periodo:]) / periodo

def consultar_ia_gemini(ia_client, info, ultimas_velas):
    if not ia_client:
        return 0, "Falta configurar GEMINI_API_KEY en Secrets"
    try:
        resumen = [f"C:{v[4]:.0f} V:{v[5]:.1f}" for v in ultimas_velas[-5:]]
        prompt = (
            f"Analiza {SYMBOL} (1m): Precio ${info['precio']:.2f}, Régimen {info['regimen']}, "
            f"RSI {info['rsi']:.1f}, Score {info['score']}/100 ({info['razones']}). Velas: {'|'.join(resumen)}. "
            f'Responde SOLO JSON: {{"ajuste_puntos": <entre -20 y 20>, "analisis": "<max 12 palabras>"}}'
        )
        resp = ia_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        datos = json.loads(resp.text.strip().replace('```json', '').replace('```', '').strip())
        return max(-20, min(20, int(datos.get("ajuste_puntos", 0)))), datos.get("analisis", "OK")
    except Exception as e:
        return 0, f"IA en espera ({str(e)[:20]})"

def evaluar_mercado(velas):
    cierres = [v[4] for v in velas]
    volumenes = [v[5] for v in velas]
    precio = cierres[-1]
    ema50 = calcular_ema(cierres, 50)[-1]
    ema200 = calcular_ema(cierres, 200)[-1]
    rsi = calcular_rsi_wilder(cierres, 14)
    bb_inf, _, bb_sup = calcular_bollinger(cierres, 20, 2.0)
    hist_act, hist_prev = calcular_macd(cierres)
    atr = calcular_atr(velas, 14)
    vol_medio = sum(volumenes[-21:-1]) / 20
    vol_ratio = volumenes[-1] / vol_medio if vol_medio > 0 else 1.0

    if precio > ema50 > ema200:
        regimen = "ALCISTA"
    elif precio < ema200 and (ema200 - precio) / ema200 > 0.008:
        regimen = "DESPLOME"
    else:
        regimen = "LATERAL"

    score, razones = 0, []
    if rsi < 30: score += 30; razones.append("RSI<30")
    elif rsi < 40: score += 20; razones.append("RSI<40")
    elif rsi < 48 and regimen == "ALCISTA": score += 15; razones.append("Pullback RSI")

    dist_bb = (precio - bb_inf) / (bb_sup - bb_inf) if (bb_sup - bb_inf) > 0 else 0.5
    if precio <= bb_inf: score += 25; razones.append("Banda Inf BB")
    elif dist_bb < 0.25: score += 15; razones.append("Zona baja BB")

    if hist_act > hist_prev and hist_act < 0: score += 20; razones.append("Giro MACD")
    elif hist_act > 0 and hist_act > hist_prev: score += 15; razones.append("Impulso MACD")

    if regimen == "ALCISTA": score += 15; razones.append("Tendencia EMA")
    elif regimen == "LATERAL": score += 5
    elif regimen == "DESPLOME": score -= 30; razones.append("Desplome")

    if vol_ratio > 1.3: score += 10; razones.append(f"Vol {vol_ratio:.1f}x")

    return {
        "precio": precio, "rsi": rsi, "atr": atr, "regimen": regimen,
        "score": max(0, min(100, score)), "razones": ", ".join(razones) or "Neutro"
    }

# =====================================================================
# MOTOR PERSISTENTE EN SEGUNDO PLANO (NO SE REINICIA AL RECARGAR LA WEB)
# =====================================================================
@st.cache_resource
def iniciar_motor_24_7():
    exchange, nombre_ex = conectar_exchange()
    api_key = obtener_api_key()
    ia_client = genai.Client(api_key=api_key) if api_key else None

    estado = {
        "usdt": 1000.0, "btc": 0.0, "precio_actual": 0.0, "precio_entrada": 0.0,
        "stop_loss": 0.0, "take_profit": 0.0, "max_precio": 0.0,
        "ganadas": 0, "perdidas": 0, "regimen": "INICIANDO", "rsi": 50.0,
        "score": 0, "ultimo_comentario_ia": "IA lista (esperando Score >= 55)" if ia_client else "Falta GEMINI_API_KEY",
        "ultima_actualizacion": "-", "exchange": nombre_ex, "historial": []
    }

    if os.path.exists(ARCHIVO_ESTADO):
        try:
            with open(ARCHIVO_ESTADO, 'r', encoding='utf-8') as f:
                estado.update(json.load(f))
        except Exception:
            pass

    def bucle_bot():
        while True:
            try:
                velas = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=220)
                info = evaluar_mercado(velas)
                precio = info["precio"]
                atr = max(info["atr"], precio * 0.0015)
                ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                score_final = info["score"]
                if estado["btc"] == 0 and info["score"] >= UMBRAL_PRE_COMPRA:
                    ajuste, analisis = consultar_ia_gemini(ia_client, info, velas)
                    score_final = max(0, min(100, info["score"] + ajuste))
                    estado["ultimo_comentario_ia"] = f"({ajuste:+d} pts) {analisis}"

                if estado["btc"] == 0 and score_final >= UMBRAL_COMPRA_FINAL:
                    inv = estado["usdt"] * RIESGO_POR_TRADE
                    estado["btc"] = (inv / precio) * (1.0 - COMISION)
                    estado["usdt"] -= inv
                    estado["precio_entrada"] = precio
                    estado["max_precio"] = precio
                    estado["stop_loss"] = precio - (1.8 * atr)
                    estado["take_profit"] = precio + (3.0 * atr)
                    estado["historial"].insert(0, f"[{ahora}] COMPRA a ${precio:.2f} | Score: {score_final} | {info['razones']}")

                elif estado["btc"] > 0:
                    if precio > estado["max_precio"]:
                        estado["max_precio"] = precio
                        estado["stop_loss"] = max(estado["stop_loss"], precio - (1.5 * atr))

                    cambio = ((precio - estado["precio_entrada"]) / estado["precio_entrada"]) * 100
                    motivo = None
                    if precio <= estado["stop_loss"]: motivo = "Stop Loss / Trailing"
                    elif precio >= estado["take_profit"]: motivo = "Take Profit"
                    elif info["rsi"] > 74 and cambio > 0.35: motivo = "RSI Sobrecompra"

                    if motivo:
                        rec = (estado["btc"] * precio) * (1.0 - COMISION)
                        if rec >= (estado["btc"] * estado["precio_entrada"]): estado["ganadas"] += 1
                        else: estado["perdidas"] += 1
                        estado["usdt"] += rec
                        estado["btc"] = 0.0
                        estado["historial"].insert(0, f"[{ahora}] VENTA a ${precio:.2f} ({cambio:+.2f}%) | {motivo}")

                estado.update({
                    "precio_actual": precio, "regimen": info["regimen"],
                    "rsi": round(info["rsi"], 1), "score": score_final,
                    "ultima_actualizacion": ahora, "historial": estado["historial"][:25]
                })
                with open(ARCHIVO_ESTADO, 'w', encoding='utf-8') as f:
                    json.dump(estado, f)
            except Exception as e:
                estado["ultimo_comentario_ia"] = f"Reconectando: {str(e)[:30]}"
            time.sleep(30)

    threading.Thread(target=bucle_bot, daemon=True).start()
    return estado

# =====================================================================
# INTERFAZ WEB PARA MÓVIL Y ORDENADOR
# =====================================================================
st.set_page_config(page_title="Bot Trading IA 24/7", layout="centered")
estado = iniciar_motor_24_7()
cartera = estado["usdt"] + (estado["btc"] * estado["precio_actual"])

st.title(f"Bot Cuantitativo + Gemini IA ({estado['exchange']})")
col1, col2, col3 = st.columns(3)
col1.metric("Valor Cartera", f"${cartera:.2f}", f"{cartera - 1000.0:+.2f} USDT")
col2.metric("Precio BTC", f"${estado['precio_actual']:.2f}")
col3.metric("Score Confluencia", f"{estado['score']} / 100")

col4, col5, col6 = st.columns(3)
col4.metric("Régimen / RSI", f"{estado['regimen']} ({estado['rsi']})")
col5.metric("Posición", "COMPRADO" if estado["btc"] > 0 else "EN ESPERA")
col6.metric("Ganadas / Perdidas", f"{estado['ganadas']}W / {estado['perdidas']}L")

st.info(f"**Último dictamen IA ({estado['ultima_actualizacion']} UTC):** {estado['ultimo_comentario_ia']}")

st.subheader("Historial de Operaciones")
if estado["historial"]:
    for item in estado["historial"]:
        st.code(item)
else:
    st.caption("Esperando primera oportunidad de compra (Score >= 65)...")

time.sleep(15)
st.rerun()
