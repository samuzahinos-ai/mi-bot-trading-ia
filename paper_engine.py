"""Motor independiente de paper trading. No contiene interfaz web ni claves privadas."""
import sys
from datetime import datetime, timezone

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




def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_state():
    return {
        "cash": INITIAL_BALANCE,
        "bot_enabled": True,
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
    """Porcentaje del valor del libro (15 niveles) que está en compras.

    Algunos exchanges (Kraken) devuelven niveles de 3 valores [precio, cantidad, marca de tiempo],
    por eso se leen solo las dos primeras posiciones en lugar de desempaquetar.
    """
    try:
        book = exchange.fetch_order_book(symbol, limit=15)
        bid_value = sum(float(level[0]) * float(level[1]) for level in book.get("bids", [])[:15])
        ask_value = sum(float(level[0]) * float(level[1]) for level in book.get("asks", [])[:15])
        total = bid_value + ask_value
        return 100.0 * bid_value / total if total > 0 else 50.0
    except Exception as exc:
        print(f"Aviso: libro de órdenes no disponible para {symbol}: {type(exc).__name__}: {str(exc)[:120]}",
              file=sys.stderr)
        return 50.0


def volume_ratio_value(volumes, recent=5, baseline=60):
    """Volumen medio de las últimas velas frente al de las 60 anteriores.

    Una sola vela de 1 minuto suele tener volumen cero en pares poco líquidos, lo que hacía
    que este indicador valiera 0 casi siempre; promediar unas pocas velas lo estabiliza.
    """
    recent_values = volumes[-recent:]
    base_values = volumes[-(recent + baseline):-recent]
    if not recent_values or not base_values:
        return 1.0
    base_average = sum(base_values) / len(base_values)
    if base_average <= 0:
        return 1.0
    return (sum(recent_values) / len(recent_values)) / base_average


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
    volume_ratio = volume_ratio_value(volumes)

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
        "max_price": entry_price,
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


def manage_position_with_history(state, symbol, closed_candles):
    """Procesa cronológicamente todas las velas cerradas desde el ciclo anterior."""
    position = state.get("position")
    if not position or position.get("symbol") != symbol:
        return

    last_timestamp = int(position.get("last_candle_ts", 0))
    for index, candle in enumerate(closed_candles):
        if int(candle[0]) <= last_timestamp:
            continue
        history = closed_candles[:index + 1]
        closes = [row[4] for row in history]
        info = {
            "symbol": symbol,
            "timestamp": int(candle[0]),
            "open": candle[1],
            "high": candle[2],
            "low": candle[3],
            "price": candle[4],
            "atr": atr_value(history, 14),
            "rsi": rsi_series(closes, 14)[-1],
        }
        manage_open_position(state, info)
        if state.get("position") is None:
            return


def run_cycle(exchange, symbols, state, bot_enabled):
    now_day = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != now_day:
        state["day"] = now_day
        state["day_start_equity"] = current_equity(state)
        state["daily_realized_pnl"] = 0.0

    radar = {}
    candle_history = {}
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
            candle_history[symbol] = closed
        except Exception as exc:
            errors.append(f"{symbol}: {str(exc)[:100]}")

    state["radar"] = radar
    if state.get("position"):
        active_symbol = state["position"]["symbol"]
        active_history = candle_history.get(active_symbol)
        if active_history:
            manage_position_with_history(state, active_symbol, active_history)

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
    state["bot_enabled"] = bool(bot_enabled)
    return state


