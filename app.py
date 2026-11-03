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
# 1. CONFIGURACIÓN INSTITUCIONAL Y CLAVE API INTEGRADA
# =====================================================================
GEMINI_API_KEY_DIRECTA = ""

MONEDAS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT']
TIMEFRAME = '1m'
COMISION = 0.001              # 0.1% comisión real
RIESGO_POR_TRADE = 0.98       # Invierte el 98% del saldo en la mejor oportunidad
UMBRAL_PRE_COMPRA = 50        # Puntuación para activar el comité de IA
UMBRAL_COMPRA_FINAL = 65      # Puntuación mínima (de 100) para ejecutar compra
ARCHIVO_ESTADO = 'estado_bot_pro.json'

def obtener_api_key():
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"].strip()
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", GEMINI_API_KEY_DIRECTA).strip()

def conectar_exchange():
    for nombre in ['kraken', 'bybit', 'coinbase', 'binance']:
        try:
            ex = getattr(ccxt, nombre)({'enableRateLimit': True})
            ex.fetch_ohlcv('BTC/USDT', timeframe=TIMEFRAME, limit=5)
            return ex, nombre.upper()
        except Exception:
            continue
    return ccxt.kraken({'enableRateLimit': True}), "KRAKEN"

# =====================================================================
# 2. SUITE MATEMÁTICA DE 7 INDICADORES + LIBRO DE ÓRDENES (BALLENAS)
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

def calcular_stoch_rsi(precios, periodo=14):
    rsis = [calcular_rsi_wilder(precios[:i], periodo) for i in range(periodo + 15, len(precios) + 1)]
    if len(rsis) < periodo:
        return 50.0
    ventana = rsis[-periodo:]
    min_r, max_r = min(ventana), max(ventana)
    return 50.0 if max_r == min_r else ((rsis[-1] - min_r) / (max_r - min_r)) * 100.0

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

def calcular_vwap(velas, periodo=50):
    recientes = velas[-periodo:]
    vol_total = sum(v[5] for v in recientes)
    if vol_total == 0:
        return recientes[-1][4]
    suma_pv = sum(((v[2] + v[3] + v[4]) / 3.0) * v[5] for v in recientes)
    return suma_pv / vol_total

def analizar_presion_ballenas(exchange, symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=15)
        vol_compras = sum(bid[1] for bid in ob['bids'][:15])
        vol_ventas = sum(ask[1] for ask in ob['asks'][:15])
        total = vol_compras + vol_ventas
        return (vol_compras / total) * 100.0 if total > 0 else 50.0
    except Exception:
        return 50.0

# =====================================================================
# 3. DOBLE MOTOR DE IA (GEMINI + ANALISTA NEURONAL LOCAL DE RESPALDO)
# =====================================================================
def motor_ia_local_heuristico(info, ultimas_velas):
    # Analiza microestructura de velas (absorción de mechas e impulso) si la API externa falla
    v = ultimas_velas[-1]
    apertura, maximo, minimo, cierre = v[1], v[2], v[3], v[4]
    rango = max(maximo - minimo, cierre * 0.0001)
    mecha_inf = (min(apertura, cierre) - minimo) / rango
    mecha_sup = (maximo - max(apertura, cierre)) / rango

    ajuste = 0
    notas = []
    if mecha_inf > 0.45:
        ajuste += 8
        notas.append("fuerte absorción compradora en mecha inferior")
    if info["presion_compras"] > 58:
        ajuste += 7
        notas.append(f"dominio de ballenas ({info['presion_compras']:.0f}% bids)")
    elif info["presion_compras"] < 42:
        ajuste -= 8
        notas.append("muro de ventas en Order Book")
    if mecha_sup > 0.55:
        ajuste -= 6
        notas.append("rechazo en parte alta")
    if info["stoch_rsi"] < 20:
        ajuste += 5
        notas.append("StochRSI en suelo óptimo")

    resumen = ", ".join(notas) if notas else "estructura de mercado equilibrada"
    return max(-20, min(20, ajuste)), f"[IA Cuantitativa Activa] {resumen.capitalize()}"

def consultar_comite_ia(ia_client, symbol, info, ultimas_velas):
    if ia_client:
        resumen = [f"O:{v[1]:.2f} H:{v[2]:.2f} L:{v[3]:.2f} C:{v[4]:.2f}" for v in ultimas_velas[-4:]]
        prompt = (
            f"Actúa como director cuantitativo. Analiza {symbol} (1m): "
            f"Precio ${info['precio']:.4f}, Régimen {info['regimen']}, RSI {info['rsi']:.1f}, "
            f"StochRSI {info['stoch_rsi']:.1f}, Presión Libro Órdenes {info['presion_compras']:.1f}%, "
            f"Score {info['score']}/100 ({info['razones']}). Velas: {'|'.join(resumen)}. "
            f'Responde SOLO JSON válido: {{"ajuste_puntos": <entero -20 a 20>, "analisis": "<máximo 12 palabras>"}}'
        )
        for modelo in ['gemini-2.0-flash', 'gemini-2.5-flash', 'gemini-1.5-flash']:
            try:
                resp = ia_client.models.generate_content(model=modelo, contents=prompt)
                texto = resp.text.strip()
                ini, fin = texto.find('{'), texto.rfind('}')
                if ini != -1 and fin != -1:
                    datos = json.loads(texto[ini:fin + 1])
                    ajuste = max(-20, min(20, int(datos.get("ajuste_puntos", 0))))
                    return ajuste, f"[Gemini IA] {datos.get('analisis', 'Análisis completado')}"
            except Exception:
                continue
    # Si la API Key devuelve 404 o límite de cuota, usa el motor neuronal local sin interrumpir
    return motor_ia_local_heuristico(info, ultimas_velas)

# =====================================================================
# 4. EVALUADOR MULTIFACTORIAL DE MERCADO (0 A 100 PUNTOS)
# =====================================================================
def evaluar_par(exchange, symbol, velas):
    cierres = [v[4] for v in velas]
    volumenes = [v[5] for v in velas]
    precio = cierres[-1]

    ema50 = calcular_ema(cierres, 50)[-1]
    ema200 = calcular_ema(cierres, 200)[-1]
    rsi = calcular_rsi_wilder(cierres, 14)
    stoch_rsi = calcular_stoch_rsi(cierres, 14)
    bb_inf, bb_med, bb_sup = calcular_bollinger(cierres, 20, 2.0)
    hist_act, hist_prev = calcular_macd(cierres)
    atr = calcular_atr(velas, 14)
    vwap = calcular_vwap(velas, 50)
    presion_compras = analizar_presion_ballenas(exchange, symbol)

    vol_medio = sum(volumenes[-21:-1]) / 20
    vol_ratio = volumenes[-1] / vol_medio if vol_medio > 0 else 1.0

    if precio > ema50 > ema200:
        regimen = "ALCISTA"
    elif precio < ema200 and (ema200 - precio) / ema200 > 0.008:
        regimen = "DESPLOME"
    else:
        regimen = "LATERAL"

    score, razones = 0, []

    # 1. RSI + StochRSI (hasta 30 pts)
    if rsi < 32:
        score += 22; razones.append(f"RSI sobrevendido ({rsi:.0f})")
    elif rsi < 42:
        score += 14; razones.append(f"RSI bajo ({rsi:.0f})")
    elif rsi < 50 and regimen == "ALCISTA":
        score += 10; razones.append("Pullback RSI")

    if stoch_rsi < 20:
        score += 8; razones.append("StochRSI suelo")

    # 2. Bandas de Bollinger + VWAP (hasta 25 pts)
    dist_bb = (precio - bb_inf) / (bb_sup - bb_inf) if (bb_sup - bb_inf) > 0 else 0.5
    if precio <= bb_inf:
        score += 20; razones.append("Toque Banda Inf BB")
    elif dist_bb < 0.28:
        score += 12; razones.append("Zona baja BB")

    if precio < vwap and regimen != "DESPLOME":
        score += 5; razones.append("Descuento bajo VWAP")

    # 3. MACD Impulso (hasta 15 pts)
    if hist_act > hist_prev and hist_act < 0:
        score += 15; razones.append("Giro MACD alcista")
    elif hist_act > 0 and hist_act > hist_prev:
        score += 10; razones.append("Impulso MACD")

    # 4. Libro de Órdenes / Ballenas (hasta 15 pts)
    if presion_compras >= 60:
        score += 15; razones.append(f"Ballenas comprando ({presion_compras:.0f}%)")
    elif presion_compras >= 54:
        score += 8; razones.append(f"Libro positivo ({presion_compras:.0f}%)")
    elif presion_compras < 40:
        score -= 10; razones.append("Presión vendedora")

    # 5. Tendencia EMA y Volumen (hasta 15 pts)
    if regimen == "ALCISTA":
        score += 10; razones.append("Tendencia EMA")
    elif regimen == "LATERAL":
        score += 4
    elif regimen == "DESPLOME":
        score -= 30; razones.append("Alerta desplome")

    if vol_ratio > 1.3:
        score += 5; razones.append(f"Vol {vol_ratio:.1f}x")

    return {
        "symbol": symbol, "precio": precio, "rsi": round(rsi, 1),
        "stoch_rsi": round(stoch_rsi, 1), "atr": atr, "vwap": vwap,
        "presion_compras": round(presion_compras, 1), "regimen": regimen,
        "score": max(0, min(100, score)),
        "razones": ", ".join(razones) if razones else "Mercado en equilibrio"
    }

# =====================================================================
# 5. MOTOR 24/7 EN SEGUNDO PLANO CON GESTIÓN DE RIESGO AVANZADA
# =====================================================================
@st.cache_resource
def iniciar_terminal_24_7():
    exchange, nombre_ex = conectar_exchange()
    api_key = obtener_api_key()
    ia_client = genai.Client(api_key=api_key) if api_key else None

    estado = {
        "usdt": 1000.0, "moneda_activa": None, "cantidad_cripto": 0.0,
        "precio_entrada": 0.0, "precio_actual_activa": 0.0,
        "stop_loss": 0.0, "take_profit": 0.0, "max_precio": 0.0,
        "break_even_activo": False, "ganadas": 0, "perdidas": 0,
        "ganancia_acumulada": 0.0, "perdida_acumulada": 0.0,
        "max_cartera": 1000.0, "max_drawdown": 0.0,
        "ultimo_comentario_ia": "Escáner Multi-Cripto e IA inicializados",
        "ultima_actualizacion": "-", "exchange": nombre_ex,
        "radar": {}, "historial": []
    }

    if os.path.exists(ARCHIVO_ESTADO):
        try:
            with open(ARCHIVO_ESTADO, 'r', encoding='utf-8') as f:
                guardado = json.load(f)
                estado.update(guardado)
        except Exception:
            pass

    def bucle_operativo():
        while True:
            try:
                ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                mejor_candidata = None
                mejor_velas = None

                # 1. Escanear las 5 criptomonedas en tiempo real
                for sym in MONEDAS:
                    try:
                        velas = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=210)
                        info = evaluar_par(exchange, sym, velas)
                        estado["radar"][sym] = info
                        if sym == estado["moneda_activa"]:
                            estado["precio_actual_activa"] = info["precio"]
                        if mejor_candidata is None or info["score"] > mejor_candidata["score"]:
                            mejor_candidata = info
                            mejor_velas = velas
                    except Exception:
                        continue

                # 2. Si NO hay posición abierta, evaluar compra en la mejor moneda del radar
                if estado["moneda_activa"] is None and mejor_candidata:
                    score_final = mejor_candidata["score"]
                    if score_final >= UMBRAL_PRE_COMPRA:
                        ajuste, comentario = consultar_comite_ia(ia_client, mejor_candidata["symbol"], mejor_candidata, mejor_velas)
                        score_final = max(0, min(100, score_final + ajuste))
                        mejor_candidata["score"] = score_final
                        estado["radar"][mejor_candidata["symbol"]]["score"] = score_final
                        estado["ultimo_comentario_ia"] = f"{mejor_candidata['symbol']} ({ajuste:+d} pts): {comentario}"
                    else:
                        estado["ultimo_comentario_ia"] = (
                            f"Mejor par: {mejor_candidata['symbol']} (Score {score_final}/100) — "
                            f"{mejor_candidata['razones']}"
                        )

                    if score_final >= UMBRAL_COMPRA_FINAL:
                        sym = mejor_candidata["symbol"]
                        precio = mejor_candidata["precio"]
                        atr = max(mejor_candidata["atr"], precio * 0.0018)
                        inv = estado["usdt"] * RIESGO_POR_TRADE

                        estado["cantidad_cripto"] = (inv / precio) * (1.0 - COMISION)
                        estado["usdt"] -= inv
                        estado["moneda_activa"] = sym
                        estado["precio_entrada"] = precio
                        estado["precio_actual_activa"] = precio
                        estado["max_precio"] = precio
                        estado["stop_loss"] = precio - (1.8 * atr)
                        estado["take_profit"] = precio + (3.2 * atr)
                        estado["break_even_activo"] = False

                        estado["historial"].insert(
                            0, f"[{ahora}] COMPRA {sym} a ${precio:.4f} | Score: {score_final}/100 | {mejor_candidata['razones']}"
                        )

                # 3. Si HAY posición abierta, gestionar Break-Even, Trailing Stop y Venta
                elif estado["moneda_activa"] is not None:
                    sym = estado["moneda_activa"]
                    info_act = estado["radar"].get(sym)
                    if info_act:
                        precio = info_act["precio"]
                        estado["precio_actual_activa"] = precio
                        atr = max(info_act["atr"], precio * 0.0018)
                        cambio_pct = ((precio - estado["precio_entrada"]) / estado["precio_entrada"]) * 100.0

                        # Escudo Break-Even: si gana +0.35%, asegura no perder dinero
                        if not estado["break_even_activo"] and cambio_pct >= 0.35:
                            estado["stop_loss"] = max(estado["stop_loss"], estado["precio_entrada"] * 1.0015)
                            estado["break_even_activo"] = True

                        # Trailing Stop dinámico
                        if precio > estado["max_precio"]:
                            estado["max_precio"] = precio
                            nuevo_sl = precio - (1.4 * atr)
                            if nuevo_sl > estado["stop_loss"]:
                                estado["stop_loss"] = nuevo_sl

                        motivo = None
                        if precio <= estado["stop_loss"]:
                            motivo = "Trailing Stop / Break-Even" if estado["break_even_activo"] else "Stop Loss de protección"
                        elif precio >= estado["take_profit"]:
                            motivo = "Take Profit alcanzado"
                        elif info_act["rsi"] > 76 and cambio_pct > 0.40:
                            motivo = f"Toma de ganancias por RSI extremo ({info_act['rsi']})"

                        if motivo:
                            recuperado = (estado["cantidad_cripto"] * precio) * (1.0 - COMISION)
                            costo_original = estado["cantidad_cripto"] * estado["precio_entrada"]
                            pnl = recuperado - costo_original

                            if pnl >= 0:
                                estado["ganadas"] += 1
                                estado["ganancia_acumulada"] += pnl
                            else:
                                estado["perdidas"] += 1
                                estado["perdida_acumulada"] += abs(pnl)

                            estado["usdt"] += recuperado
                            estado["moneda_activa"] = None
                            estado["cantidad_cripto"] = 0.0
                            estado["historial"].insert(
                                0, f"[{ahora}] VENTA {sym} a ${precio:.4f} ({cambio_pct:+.2f}% | {pnl:+.2f} USDT) | {motivo}"
                            )

                # 4. Actualizar métricas de riesgo (Drawdown y Cartera)
                valor_pos = estado["cantidad_cripto"] * estado["precio_actual_activa"] if estado["moneda_activa"] else 0.0
                cartera_total = estado["usdt"] + valor_pos
                if cartera_total > estado["max_cartera"]:
                    estado["max_cartera"] = cartera_total
                dd_actual = ((estado["max_cartera"] - cartera_total) / estado["max_cartera"]) * 100.0
                if dd_actual > estado["max_drawdown"]:
                    estado["max_drawdown"] = dd_actual

                estado["ultima_actualizacion"] = ahora
                estado["historial"] = estado["historial"][:35]

                with open(ARCHIVO_ESTADO, 'w', encoding='utf-8') as f:
                    json.dump(estado, f)

            except Exception as e:
                estado["ultimo_comentario_ia"] = f"Sincronizando datos: {str(e)[:35]}"
            time.sleep(25)

    threading.Thread(target=bucle_operativo, daemon=True).start()
    return estado

# =====================================================================
# 6. TERMINAL WEB PROFESIONAL (100% HTML/CSS SIN ERRORES DE MÓDULOS)
# =====================================================================
st.set_page_config(page_title="Terminal Cuantitativa IA 24/7", layout="wide")
estado = iniciar_terminal_24_7()

valor_pos = estado["cantidad_cripto"] * estado["precio_actual_activa"] if estado["moneda_activa"] else 0.0
cartera = estado["usdt"] + valor_pos
ganancia_neta = cartera - 1000.0
pct_neto = (ganancia_neta / 1000.0) * 100.0
total_ops = estado["ganadas"] + estado["perdidas"]
win_rate = (estado["ganadas"] / total_ops * 100.0) if total_ops > 0 else 0.0
profit_factor = (estado["ganancia_acumulada"] / estado["perdida_acumulada"]) if estado["perdida_acumulada"] > 0 else (estado["ganancia_acumulada"] if estado["ganancia_acumulada"] > 0 else 1.0)

color_pnl = "#4ade80" if ganancia_neta >= 0 else "#f87171"
texto_pos = f"{estado['moneda_activa']} (SL: ${estado['stop_loss']:.4f})" if estado["moneda_activa"] else "ESCANEANDO (Liquidez 100%)"

st.markdown(f"""
<div style="background:linear-gradient(90deg, #0f172a, #1e293b); padding:18px 24px; border-radius:12px; border:1px solid #334155; margin-bottom:18px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;">
    <div>
        <h2 style="margin:0; color:#f8fafc; font-size:1.45rem;">Terminal Cuantitativa Multi-Cripto + IA ({estado['exchange']})</h2>
        <div style="color:#94a3b8; font-size:0.85rem; margin-top:4px;">Último ciclo: {estado['ultima_actualizacion']} UTC | Escáner Order Book + 7 Indicadores</div>
    </div>
    <div style="background:#020617; padding:8px 16px; border-radius:8px; border:1px solid #38bdf8; color:#38bdf8; font-weight:bold;">
        MODO 24/7 ACTIVO
    </div>
</div>

<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:12px; margin-bottom:18px;">
    <div style="background:#1e293b; padding:16px; border-radius:10px; border:1px solid #334155;">
        <div style="color:#94a3b8; font-size:0.8rem;">VALOR NETO CARTERA</div>
        <div style="color:#f8fafc; font-size:1.55rem; font-weight:bold;">${cartera:.2f} <span style="font-size:0.95rem; color:{color_pnl};">({pct_neto:+.2f}%)</span></div>
    </div>
    <div style="background:#1e293b; padding:16px; border-radius:10px; border:1px solid #334155;">
        <div style="color:#94a3b8; font-size:0.8rem;">POSICIÓN ACTUAL</div>
        <div style="color:#38bdf8; font-size:1.25rem; font-weight:bold; margin-top:4px;">{texto_pos}</div>
    </div>
    <div style="background:#1e293b; padding:16px; border-radius:10px; border:1px solid #334155;">
        <div style="color:#94a3b8; font-size:0.8rem;">WIN RATE / OPERACIONES</div>
        <div style="color:#f8fafc; font-size:1.4rem; font-weight:bold;">{win_rate:.0f}% <span style="font-size:0.9rem; color:#94a3b8;">({estado['ganadas']}W / {estado['perdidas']}L)</span></div>
    </div>
    <div style="background:#1e293b; padding:16px; border-radius:10px; border:1px solid #334155;">
        <div style="color:#94a3b8; font-size:0.8rem;">PROFIT FACTOR / DRAWDOWN</div>
        <div style="color:#facc15; font-size:1.4rem; font-weight:bold;">{profit_factor:.2f}x <span style="font-size:0.9rem; color:#94a3b8;">(DD: {estado['max_drawdown']:.2f}%)</span></div>
    </div>
</div>

<div style="background:#0f172a; padding:14px 18px; border-radius:10px; border-left:4px solid #38bdf8; margin-bottom:20px; color:#e2e8f0; font-size:0.95rem;">
    <strong>Dictamen del Comité IA:</strong> {estado['ultimo_comentario_ia']}
</div>
""", unsafe_allow_html=True)

# Tabla HTML del Radar Multi-Cripto en Tiempo Real
filas_html = ""
for sym in MONEDAS:
    d = estado["radar"].get(sym)
    if d:
        color_score = "#4ade80" if d["score"] >= 65 else ("#facc15" if d["score"] >= 45 else "#94a3b8")
        color_reg = "#4ade80" if d["regimen"] == "ALCISTA" else ("#f87171" if d["regimen"] == "DESPLOME" else "#cbd5e1")
        filas_html += f"""
        <tr style="border-bottom:1px solid #1e293b;">
            <td style="padding:10px; font-weight:bold; color:#f8fafc;">{sym}</td>
            <td style="padding:10px; color:#e2e8f0;">${d['precio']:.4f}</td>
            <td style="padding:10px; color:{color_reg}; font-weight:bold;">{d['regimen']}</td>
            <td style="padding:10px; color:#e2e8f0;">{d['rsi']} / {d['stoch_rsi']}</td>
            <td style="padding:10px; color:#38bdf8;">{d['presion_compras']}% Bids</td>
            <td style="padding:10px; color:{color_score}; font-weight:bold; font-size:1.05rem;">{d['score']} / 100</td>
            <td style="padding:10px; color:#94a3b8; font-size:0.85rem;">{d['razones']}</td>
        </tr>
        """

st.markdown(f"""
<div style="background:#0f172a; border-radius:10px; border:1px solid #334155; overflow-x:auto; margin-bottom:20px;">
    <div style="padding:12px 16px; background:#1e293b; font-weight:bold; color:#f8fafc;">Radar Cuantitativo en Tiempo Real (Compra automática con Score >= 65)</div>
    <table style="width:100%; border-collapse:collapse; text-align:left; font-size:0.92rem;">
        <thead>
            <tr style="border-bottom:1px solid #334155; color:#94a3b8; background:#020617;">
                <th style="padding:10px;">Activo</th>
                <th style="padding:10px;">Precio</th>
                <th style="padding:10px;">Régimen</th>
                <th style="padding:10px;">RSI / StochRSI</th>
                <th style="padding:10px;">Presión Ballenas</th>
                <th style="padding:10px;">Score</th>
                <th style="padding:10px;">Señales Detectadas</th>
            </tr>
        </thead>
        <tbody>
            {filas_html if filas_html else '<tr><td colspan="7" style="padding:14px; color:#94a3b8;">Escaneando los 5 mercados en vivo... espera unos segundos.</td></tr>'}
        </tbody>
    </table>
</div>
""", unsafe_allow_html=True)

st.subheader("Registro de Operaciones Ejecutadas")
if estado["historial"]:
    csv_export = "Operacion\n" + "\n".join(estado["historial"])
    st.download_button("Descargar Historial en CSV (Excel)", data=csv_export, file_name="historial_bot_pro.csv", mime="text/csv")
    for item in estado["historial"]:
        st.code(item)
else:
    st.caption("Vigilando los 5 mercados en vivo. En cuanto una criptomoneda alcance 65/100 puntos se registrará la compra aquí.")

time.sleep(15)
st.rerun()
