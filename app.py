import json
import math
import os
import sqlite3
import threading
from datetime import datetime, timezone

import ccxt
import streamlit as st

# Gemini solo redacta un comentario opcional. Nunca decide ni autoriza operaciones.
try:
    from google import genai
except Exception:
    genai = None

st.set_page_config(page_title="Bot de trading | Simulación", page_icon="📊", layout="wide")

# ----------------------------- CONFIGURACIÓN -----------------------------
PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"]
TIMEFRAME = "1m"
CANDLE_LIMIT = 700
INITIAL_BALANCE = 1000.0
FEE_RATE = 0.001              # 0,10 % por lado; supuesto configurable
SLIPPAGE_RATE = 0.0005        # 0,05 % por lado; supuesto de simulación
RISK_PER_TRADE = 0.01         # Riesgo teórico del 1 % del patrimonio hasta el stop
MAX_ALLOCATION = 0.25         # Máximo 25 % del efectivo en una posición
MAX_DAILY_LOSS = 0.03         # Bloquea nuevas entradas al perder 3 % en el día
MIN_SCORE = 70
DB_PATH = os.environ.get("PAPER_DB_PATH", "paper_trading.sqlite3")
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_state():
    return {
        "cash": INITIAL_BALANCE,
        "initial_balance": INITIAL_BALANCE,
        "position": None,
        "trades": [],
        "realized_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "peak_equity": INITIAL_BALANCE,
        "max_drawdown_pct": 0.0,
        "day": datetime.now(timezone.utc).date().isoformat(),
        "day_start_equity": INITIAL_BALANCE,
        "daily_realized_pnl": 0.0,
        "last_scan": None,
        "last_error": None,
        "last_comment": "Esperando el primer escaneo.",
        "radar": {},
    }


def db_connection():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.execute(
        "CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)"
    )
    con.commit()
    return con


def load_state():
    con = db_connection()
    try:
        row = con.execute("SELECT payload FROM bot_state WHERE id = 1").fetchone()
        if row is None:
            state = default_state()
            con.execute(
                "INSERT INTO bot_state (id, payload) VALUES (1, ?)",
                (json.dumps(state),),
            )
            con.commit()
            return state
        state = default_state()
        state.update(json.loads(row[0]))
        return state
    finally:
        con.close()


def save_state(state):
    con = db_connection()
    try:
        con.execute(
            "INSERT INTO bot_state (id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (json.dumps(state),),
        )
        con.commit()
    finally:
        con.close()


def reset_state():
    con = db_connection()
    try:
        con.execute("DELETE FROM bot_state WHERE id = 1")
        con.commit()
    finally:
        con.close()


@st.cache_resource
def get_runtime():
    exchange_id = os.environ.get("EXCHANGE_ID", "kraken").lower()
    if exchange_id not in {"kraken", "binance", "bybit", "coinbase"}:
        raise ValueError("EXCHANGE_ID no válido. Usa kraken, binance, bybit o coinbase.")
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({
        "enableRateLimit": True,
        "timeout": 15000,
        "options": {"defaultType": "spot"},
    })
    exchange.load_markets()
    available = [symbol for symbol in PAIRS if symbol in exchange.markets]
    return exchange, exchange_id.upper(), available, threading.Lock()


# ----------------------------- INDICADORES -----------------------------
def ema_series(values, period):
    if len(values) < period:
        raise ValueError("No hay suficientes datos para calcular EMA.")
    k = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    result = [None] * (period - 1) + [current]
    for value in values[period:]:
        current = value * k + current * (1.0 - k)
        result.append(current)
    return result


def rsi_series(closes, period=14):
    if len(closes) <= period:
        raise ValueError("No hay suficientes datos para RSI.")
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result = [None] * period
    result.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return result


def macd_histogram(closes):
    fast = ema_series(closes, 12)
    slow = ema_series(closes, 26)
    start = 25
    macd = [fast[i] - slow[i] for i in range(start, len(closes))]
    signal = ema_series(macd, 9)
    return macd[-1] - signal[-1], macd[-2] - signal[-2]


def atr_value(candles, period=14):
    true_ranges = []
    for i in range(1, len(candles)):
        high, low, previous_close = candles[i][2], candles[i][3], candles[i - 1][4]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(true_ranges[-period:]) / period


def rolling_vwap(candles, period=50):
    recent = candles[-period:]
    volume = sum(row[5] for row in recent)
    if volume <= 0:
        return recent[-1][4]
    return sum(((row[2] + row[3] + row[4]) / 3.0) * row[5] for row in recent) / volume


def order_book_pressure(exchange, symbol):
    try:
        book = exchange.fetch_order_book(symbol, limit=15)
        bid_value = sum(price * amount for price, amount in book.get("bids", [])[:15])
        ask_value = sum(price * amount for price, amount in book.get("asks", [])[:15])
        total = bid_value + ask_value
        return 100.0 * bid_value / total if total > 0 else 50.0
    except Exception:
        return 50.0


def evaluate_pair(exchange, symbol, candles):
    if len(candles) < 230:
        raise ValueError("Se requieren al menos 230 velas cerradas.")
    closes = [row[4] for row in candles]
    volumes = [row[5] for row in candles]
    price = closes[-1]

    ema50 = ema_series(closes, 50)[-1]
    ema200 = ema_series(closes, 200)[-1]
    rsi_values = rsi_series(closes, 14)
    rsi = rsi_values[-1]
    valid_rsi = [value for value in rsi_values[-14:] if value is not None]
    stoch = 50.0
    if valid_rsi and max(valid_rsi) != min(valid_rsi):
        stoch = 100.0 * (valid_rsi[-1] - min(valid_rsi)) / (max(valid_rsi) - min(valid_rsi))

    macd_now, macd_previous = macd_histogram(closes)
    atr = atr_value(candles, 14)
    vwap = rolling_vwap(candles, 50)
    pressure = order_book_pressure(exchange, symbol)
    average_volume = sum(volumes[-21:-1]) / 20.0
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else 1.0

    if price > ema50 > ema200:
        regime = "ALCISTA"
    elif price < ema200 and (ema200 - price) / ema200 > 0.008:
        regime = "BAJISTA FUERTE"
    else:
        regime = "LATERAL"

    score = 0
    reasons = []
    if price > ema50 > ema200:
        score += 25
        reasons.append("tendencia EMA alcista")
    if 38 <= rsi <= 62:
        score += 15
        reasons.append("RSI en zona moderada")
    elif 30 <= rsi < 38 and regime == "ALCISTA":
        score += 10
        reasons.append("posible retroceso en tendencia")
    if macd_now > macd_previous and macd_now > 0:
        score += 20
        reasons.append("impulso MACD positivo")
    elif macd_now > macd_previous:
        score += 10
        reasons.append("MACD mejorando")
    if price > vwap:
        score += 10
        reasons.append("precio sobre VWAP móvil")
    if volume_ratio >= 1.2:
        score += 10
        reasons.append("volumen superior a la media")
    if pressure >= 55:
        score += 10
        reasons.append("libro inclinado a compras")
    if atr > 0 and atr / price < 0.02:
        score += 10
        reasons.append("volatilidad dentro del límite")
    if rsi > 72:
        score -= 15
        reasons.append("RSI elevado")
    if regime == "BAJISTA FUERTE":
        score -= 25
        reasons.append("tendencia bajista fuerte")

    return {
        "symbol": symbol,
        "timestamp": int(candles[-1][0]),
        "price": price,
        "open": candles[-1][1],
        "high": candles[-1][2],
        "low": candles[-1][3],
        "rsi": round(rsi, 1),
        "stoch_rsi": round(stoch, 1),
        "atr": atr,
        "vwap": vwap,
        "buy_pressure": round(pressure, 1),
        "volume_ratio": round(volume_ratio, 2),
        "regime": regime,
        "score": max(0, min(100, score)),
        "reasons": ", ".join(reasons) if reasons else "sin señal clara",
    }


# -------------------------- SIMULADOR DE ÓRDENES -------------------------
def current_equity(state, prices=None):
    prices = prices or {}
    position = state.get("position")
    if not position:
        return float(state["cash"])
    mark = prices.get(position["symbol"], position["last_price"])
    # Valoración neta estimada: descuenta comisión de una hipotética salida.
    return float(state["cash"]) + position["quantity"] * mark * (1.0 - FEE_RATE)


def close_position(state, exit_price, reason, candle_time):
    pos = state["position"]
    proceeds = pos["quantity"] * exit_price * (1.0 - FEE_RATE)
    pnl = proceeds - pos["entry_total_cost"]
    state["cash"] += proceeds
    state["realized_pnl"] += pnl
    state["daily_realized_pnl"] += pnl
    if pnl >= 0:
        state["wins"] += 1
    else:
        state["losses"] += 1
    state["trades"].insert(0, {
        "time": utc_now(),
        "symbol": pos["symbol"],
        "side": "VENTA",
        "price": round(exit_price, 8),
        "quantity": round(pos["quantity"], 8),
        "pnl": round(pnl, 4),
        "reason": reason,
        "candle_time": candle_time,
    })
    state["trades"] = state["trades"][:200]
    state["last_comment"] = f"Cierre simulado {pos['symbol']}: {reason}. PnL neto {pnl:+.2f} USDT."
    state["position"] = None


def manage_open_position(state, info):
    pos = state.get("position")
    if not pos or info["symbol"] != pos["symbol"]:
        return
    if info["timestamp"] <= pos.get("last_candle_ts", 0):
        return

    stop = pos["stop_loss"]
    target = pos["take_profit"]
    low, high, opening, close = info["low"], info["high"], info["open"], info["price"]

    # Si en una misma vela se tocaron stop y objetivo, se supone que saltó primero el stop.
    stop_hit = low <= stop
    target_hit = high >= target
    if stop_hit:
        raw_exit = min(opening, stop)
        exit_price = raw_exit * (1.0 - SLIPPAGE_RATE)
        reason = "stop loss (prioridad conservadora intravela)"
        close_position(state, exit_price, reason, info["timestamp"])
        return
    if target_hit:
        exit_price = target * (1.0 - SLIPPAGE_RATE)
        close_position(state, exit_price, "take profit", info["timestamp"])
        return

    if info["rsi"] >= 78 and close > pos["entry_price"]:
        exit_price = close * (1.0 - SLIPPAGE_RATE)
        close_position(state, exit_price, "salida por RSI extremo", info["timestamp"])
        return

    pos["last_price"] = close
    pos["last_candle_ts"] = info["timestamp"]
    pos["max_price"] = max(pos["max_price"], high)
    # Break-even solo cuando la subida cubre aproximadamente comisiones y deslizamiento.
    fee_buffer = 2.0 * FEE_RATE + 2.0 * SLIPPAGE_RATE
    if pos["max_price"] >= pos["entry_price"] * (1.0 + fee_buffer + 0.003):
        pos["stop_loss"] = max(pos["stop_loss"], pos["entry_price"] * (1.0 + fee_buffer))
    trailing = pos["max_price"] - 1.8 * max(info["atr"], close * 0.001)
    pos["stop_loss"] = max(pos["stop_loss"], trailing)


def maybe_open_position(state, candidates, bot_enabled):
    if not bot_enabled or state.get("position") is not None or not candidates:
        return
    today_equity = current_equity(state, {x["symbol"]: x["price"] for x in candidates})
    daily_floor = state["day_start_equity"] * (1.0 - MAX_DAILY_LOSS)
    if today_equity <= daily_floor or state["daily_realized_pnl"] <= -MAX_DAILY_LOSS * state["day_start_equity"]:
        state["last_comment"] = "Límite diario de pérdida alcanzado: nuevas entradas bloqueadas."
        return

    candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)
    chosen = candidates[0]
    if chosen["score"] < MIN_SCORE or chosen["regime"] != "ALCISTA" or chosen["atr"] <= 0:
        state["last_comment"] = (
            f"Sin entrada: mejor señal {chosen['symbol']} con {chosen['score']}/100; "
            "se exige tendencia alcista y puntuación mínima."
        )
        return

    if state.get("last_entry_candle") == chosen["timestamp"]:
        return

    equity = today_equity
    entry_price = chosen["price"] * (1.0 + SLIPPAGE_RATE)
    stop_distance = 1.5 * chosen["atr"]
    risk_budget = equity * RISK_PER_TRADE
    quantity_by_risk = risk_budget / stop_distance
    max_spend = state["cash"] * MAX_ALLOCATION
    quantity_by_allocation = max_spend / (entry_price * (1.0 + FEE_RATE))
    quantity = min(quantity_by_risk, quantity_by_allocation)
    total_cost = quantity * entry_price * (1.0 + FEE_RATE)

    if quantity <= 0 or total_cost <= 0 or total_cost > state["cash"]:
        state["last_comment"] = "Entrada omitida: tamaño de posición no válido."
        return

    state["cash"] -= total_cost
    state["position"] = {
        "symbol": chosen["symbol"],
        "quantity": quantity,
        "entry_price": entry_price,
        "entry_total_cost": total_cost,
        "stop_loss": entry_price - stop_distance,
        "take_profit": entry_price + 2.5 * stop_distance,
        "max_price": chosen["high"],
        "last_price": chosen["price"],
        "last_candle_ts": chosen["timestamp"],
        "opened_at": utc_now(),
    }
    state["last_entry_candle"] = chosen["timestamp"]
    state["trades"].insert(0, {
        "time": utc_now(),
        "symbol": chosen["symbol"],
        "side": "COMPRA",
        "price": round(entry_price, 8),
        "quantity": round(quantity, 8),
        "pnl": None,
        "reason": f"score {chosen['score']}/100: {chosen['reasons']}",
        "candle_time": chosen["timestamp"],
    })
    state["trades"] = state["trades"][:200]
    state["last_comment"] = f"Compra simulada {chosen['symbol']}; score {chosen['score']}/100."


def run_cycle(exchange, symbols, state, bot_enabled):
    now_day = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != now_day:
        state["day"] = now_day
        state["day_start_equity"] = current_equity(state)
        state["daily_realized_pnl"] = 0.0

    radar = {}
    candle_data = {}
    errors = []
    for symbol in symbols:
        try:
            raw = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
            # La última vela suele estar abierta. No se usa para indicadores ni decisiones.
            closed = raw[:-1]
            if len(closed) < 230:
                raise ValueError("histórico insuficiente")
            info = evaluate_pair(exchange, symbol, closed)
            radar[symbol] = info
            candle_data[symbol] = closed[-1]
        except Exception as exc:
            errors.append(f"{symbol}: {str(exc)[:100]}")

    state["radar"] = radar
    if state.get("position"):
        active_symbol = state["position"]["symbol"]
        active_info = radar.get(active_symbol)
        if active_info:
            manage_open_position(state, active_info)

    if state.get("position") is None:
        maybe_open_position(state, list(radar.values()), bot_enabled)

    prices = {symbol: item["price"] for symbol, item in radar.items()}
    equity = current_equity(state, prices)
    state["peak_equity"] = max(state.get("peak_equity", INITIAL_BALANCE), equity)
    if state["peak_equity"] > 0:
        drawdown = 100.0 * (state["peak_equity"] - equity) / state["peak_equity"]
        state["max_drawdown_pct"] = max(state.get("max_drawdown_pct", 0.0), drawdown)
    state["last_scan"] = utc_now()
    state["last_error"] = " | ".join(errors) if errors else None
    if not radar and errors:
        state["last_comment"] = "No se recibieron datos válidos. Revisa el exchange y sus mercados."
    save_state(state)
    return state


# ------------------------------- INTERFAZ --------------------------------
st.title("📊 Bot de trading con simulación")
st.caption("Solo datos públicos y operaciones ficticias. No se envían órdenes al exchange.")

with st.sidebar:
    st.header("Control")
    bot_enabled = st.toggle("Permitir nuevas entradas simuladas", value=True)
    st.caption("Al desactivarlo, se bloquean nuevas compras; la posición abierta sigue gestionándose.")
    st.divider()
    st.write("**Supuestos del simulador**")
    st.write(f"Comisión: {FEE_RATE * 100:.2f} % por lado")
    st.write(f"Deslizamiento: {SLIPPAGE_RATE * 100:.2f} % por lado")
    st.write(f"Riesgo teórico por operación: {RISK_PER_TRADE * 100:.1f} %")
    st.write(f"Máxima asignación: {MAX_ALLOCATION * 100:.0f} % del efectivo")
    st.write(f"Pérdida diaria máxima: {MAX_DAILY_LOSS * 100:.0f} %")

try:
    exchange, exchange_name, available_symbols, cycle_lock = get_runtime()
except Exception as exc:
    st.error(f"No se pudo inicializar el exchange: {exc}")
    st.stop()

if not available_symbols:
    st.error(
        f"{exchange_name} no anuncia ninguno de los pares configurados ({', '.join(PAIRS)}). "
        "Cambia EXCHANGE_ID en la configuración."
    )
    st.stop()

missing = sorted(set(PAIRS) - set(available_symbols))
if missing:
    st.warning(f"Pares no disponibles en {exchange_name}: {', '.join(missing)}. Se omiten.")

st.info(
    f"Fuente de precios: {exchange_name} | Marco temporal: {TIMEFRAME} | "
    f"Activos analizados: {', '.join(available_symbols)}"
)


@st.fragment(run_every="30s")
def live_panel():
    if cycle_lock.acquire(blocking=False):
        try:
            state = load_state()
            state = run_cycle(exchange, available_symbols, state, bot_enabled)
        except Exception as exc:
            st.error(f"Error durante el ciclo: {exc}")
            state = load_state()
            state["last_error"] = str(exc)[:250]
            save_state(state)
        finally:
            cycle_lock.release()
    else:
        state = load_state()
        st.caption("Hay otro ciclo de análisis en curso; se mostrará su resultado al terminar.")

    position = state.get("position")
    prices = {symbol: item["price"] for symbol, item in state.get("radar", {}).items()}
    equity = current_equity(state, prices)
    pnl = equity - state["initial_balance"]
    pnl_pct = 100.0 * pnl / state["initial_balance"] if state["initial_balance"] else 0.0
    total_closed = state["wins"] + state["losses"]
    win_rate = 100.0 * state["wins"] / total_closed if total_closed else None
    loss_amount = sum(abs(t["pnl"]) for t in state["trades"] if t.get("pnl") is not None and t["pnl"] < 0)
    gain_amount = sum(t["pnl"] for t in state["trades"] if t.get("pnl") is not None and t["pnl"] > 0)
    profit_factor = gain_amount / loss_amount if loss_amount > 0 else (math.inf if gain_amount > 0 else None)

    cols = st.columns(4)
    cols[0].metric("Patrimonio estimado", f"{equity:,.2f} USDT", f"{pnl_pct:+.2f} %")
    cols[1].metric("Efectivo libre", f"{state['cash']:,.2f} USDT")
    cols[2].metric("Operaciones cerradas", str(total_closed), f"{state['wins']} ganadas / {state['losses']} perdidas")
    cols[3].metric("Drawdown máximo", f"{state['max_drawdown_pct']:.2f} %")

    if position:
        st.subheader(f"Posición simulada abierta: {position['symbol']}")
        pcols = st.columns(4)
        pcols[0].metric("Precio de entrada", f"{position['entry_price']:.6f}")
        pcols[1].metric("Cantidad", f"{position['quantity']:.8f}")
        pcols[2].metric("Stop loss", f"{position['stop_loss']:.6f}")
        pcols[3].metric("Take profit", f"{position['take_profit']:.6f}")
    else:
        st.subheader("Sin posición abierta")

    st.subheader("Radar de mercado")
    rows = []
    for symbol, item in state.get("radar", {}).items():
        rows.append({
            "Activo": symbol,
            "Precio": item["price"],
            "Tendencia": item["regime"],
            "RSI": item["rsi"],
            "Stoch RSI": item["stoch_rsi"],
            "Libro compra %": item["buy_pressure"],
            "Volumen x": item["volume_ratio"],
            "Puntuación": item["score"],
            "Motivos": item["reasons"],
        })
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.warning("Todavía no hay datos completos de mercado.")

    st.caption(f"Último ciclo UTC: {state.get('last_scan') or 'pendiente'}")
    st.write(f"**Comentario del motor:** {state.get('last_comment', '-')}")
    if state.get("last_error"):
        st.warning(f"Incidencias de datos: {state['last_error']}")

    st.subheader("Registro reciente")
    trades = state.get("trades", [])
    if trades:
        st.dataframe(trades[:50], use_container_width=True, hide_index=True)
        csv_data = "time,symbol,side,price,quantity,pnl,reason,candle_time\n"
        for trade in trades:
            values = [
                trade.get("time", ""), trade.get("symbol", ""), trade.get("side", ""),
                trade.get("price", ""), trade.get("quantity", ""),
                trade.get("pnl", ""), str(trade.get("reason", "")).replace(",", ";"),
                trade.get("candle_time", ""),
            ]
            csv_data += ",".join(str(value) for value in values) + "\n"
        st.download_button(
            "Descargar operaciones CSV",
            data=csv_data,
            file_name="operaciones_paper_trading.csv",
            mime="text/csv",
        )
    else:
        st.caption("Todavía no se ha ejecutado ninguna operación simulada.")

    st.caption(
        f"Win rate: {win_rate:.1f} %" if win_rate is not None else "Win rate: sin operaciones cerradas"
    )
    if profit_factor is None:
        st.caption("Profit factor: no disponible todavía.")
    elif math.isinf(profit_factor):
        st.caption("Profit factor: infinito (hay ganancias, pero aún no hay pérdidas cerradas).")
    else:
        st.caption(f"Profit factor: {profit_factor:.2f}")


live_panel()

st.divider()
st.subheader("Análisis descriptivo opcional con Gemini")
st.caption("Gemini solo comenta el radar. No cambia puntuaciones, no predice beneficios ni autoriza operaciones.")
if st.button("Analizar el radar con Gemini"):
    api_key = None
    try:
        api_key = st.secrets.get("GEMINI_API_KEY")
    except Exception:
        pass
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    state_for_ai = load_state()
    if not api_key:
        st.warning("Gemini está desactivado. Añade GEMINI_API_KEY en Streamlit Secrets para habilitarlo.")
    elif genai is None:
        st.error("No está instalado google-genai. Revisa requirements.txt.")
    elif not state_for_ai.get("radar"):
        st.warning("Aún no hay datos de mercado para analizar.")
    else:
        try:
            client = genai.Client(api_key=api_key)
            compact = [
                {
                    "activo": key,
                    "precio": value["price"],
                    "tendencia": value["regime"],
                    "rsi": value["rsi"],
                    "score": value["score"],
                    "razones": value["reasons"],
                }
                for key, value in state_for_ai["radar"].items()
            ]
            prompt = (
                "Eres un comentarista educativo de datos de mercado. Resume el radar en español, "
                "señala incertidumbres y riesgos, no des órdenes de compra/venta ni promesas de rentabilidad. "
                "Aclara que una puntuación heurística no es una probabilidad de éxito. Datos: "
                + json.dumps(compact, ensure_ascii=False)
            )
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            st.write(response.text or "Gemini no devolvió texto.")
        except Exception as exc:
            st.error(f"No se pudo consultar Gemini: {str(exc)[:250]}")

st.divider()
st.warning(
    "Limitaciones importantes: la simulación usa velas de 1 minuto cerradas y supuestos fijos de comisión "
    "y deslizamiento. No representa la ejecución real, no garantiza que un stop pueda ejecutarse al precio "
    "indicado y no demuestra que la estrategia tenga ventaja estadística. Hace falta backtesting fuera de "
    "muestra y pruebas prolongadas en papel antes de plantearse cualquier uso con dinero real."
)

with st.expander("Reiniciar la cuenta ficticia"):
    st.write("Esto borra el saldo, la posición y el historial guardados en esta instancia.")
    confirm_reset = st.checkbox("Confirmo que quiero reiniciar la cuenta ficticia")
    if st.button("Reiniciar simulación", disabled=not confirm_reset):
        reset_state()
        st.success("Cuenta de simulación reiniciada. Recarga la página para comenzar de nuevo.")
