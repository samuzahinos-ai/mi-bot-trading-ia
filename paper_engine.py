"""Motor de paper trading v2: seguimiento de tendencia (rupturas de Donchian) en velas de 4 horas.

Por qué existe esta versión
---------------------------
La v1 operaba con velas de 1 minuto. Cada ida y vuelta costaba ~0,30 % (comisiones y deslizamiento) y
el movimiento medio por operación era ~0,12 %, así que perdía casi siempre por construcción.
La v2 opera solo en velas de 4 horas, donde los movimientos grandes superan con holgura los costes.

Reglas (validadas con 9 años de datos, ver INFORME_BACKTEST.md):
  * 5 pares x 5 ventanas (60, 90, 120, 180, 240 velas) = 25 "huchas" independientes de igual tamaño.
  * Entrada: el cierre de una vela de 4h supera el máximo de las N velas anteriores.
  * Salida: el cierre cae bajo el mínimo de las N/2 velas anteriores, o se toca el stop de seguridad.
  * Stop de seguridad: 4 x ATR(14) bajo el precio de entrada (no se mueve).
  * Tamaño: se arriesga como máximo el 5 % de la hucha hasta el stop (límite: la hucha entera).
  * Solo posiciones largas (spot). Sin apalancamiento. Sin órdenes reales.

El motor es determinista: con las mismas velas y el mismo estado siempre decide lo mismo, y se
recupera solo si GitHub se salta ejecuciones (revisa todas las velas cerradas desde la entrada).
"""
import os
import sys
from datetime import datetime, timezone

# ----------------------------- CONFIGURACIÓN -----------------------------
STRATEGY_VERSION = 2
PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"]
TIMEFRAME = "4h"
BAR_MS = 4 * 60 * 60 * 1000
CANDLE_LIMIT = 720             # máximo que devuelve Kraken por petición
LOOKBACKS = (60, 90, 120, 180, 240)
ATR_PERIOD = 14
STOP_ATR = 4.0                 # stop de seguridad a 4 ATR de la entrada
SLEEVE_RISK = 0.05             # riesgo máximo por operación: 5 % de la hucha
INITIAL_BALANCE = 1000.0
EQUITY_POINT_EVERY_SECONDS = 1800
EQUITY_HISTORY_MAX = 3000
TRADES_MAX = 300


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# Kraken (nivel 1, 2026): 0,40 % maker / 0,80 % taker. Se usa el maker por lado como base
# y el backtest muestra que la estrategia sigue ganando con 0,80 %. Se puede cambiar por entorno.
FEE_RATE = _env_float("FEE_RATE", 0.0040)
SLIPPAGE_RATE = _env_float("SLIPPAGE_RATE", 0.0005)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------- ESTADO ---------------------------------
def exit_window(lookback):
    return max(3, lookback // 2)


def sleeve_key(symbol, lookback):
    return f"{symbol}|{lookback}"


def new_sleeves(symbols=None):
    symbols = symbols or PAIRS
    per_sleeve = INITIAL_BALANCE / (len(PAIRS) * len(LOOKBACKS))
    return {
        sleeve_key(symbol, n): {
            "symbol": symbol, "lookback": n, "cash": per_sleeve, "position": None, "cooldown_bar": 0,
        }
        for symbol in symbols for n in LOOKBACKS
    }


def default_state():
    return {
        "version": STRATEGY_VERSION,
        "initial_balance": INITIAL_BALANCE,
        "sleeves": new_sleeves(),
        "trades": [],
        "realized_pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "peak_equity": INITIAL_BALANCE,
        "max_drawdown_pct": 0.0,
        "last_scan": None,
        "last_error": None,
        "last_comment": "Esperando el primer escaneo.",
        "radar": {},
        "equity_history": [],
        "bot_enabled": True,
        "legacy_v1": None,
    }


def ensure_sleeves(state):
    """Añade huchas que falten (por ejemplo si se amplía la lista de pares)."""
    for key, sleeve in new_sleeves().items():
        state["sleeves"].setdefault(key, sleeve)
    return state


def open_positions(state):
    return [s["position"] for s in state["sleeves"].values() if s.get("position")]


def total_cash(state):
    return sum(float(s["cash"]) for s in state["sleeves"].values())


def portfolio_equity(state, prices=None):
    """Valor de la cuenta: efectivo + posiciones a precio de mercado descontando la comisión de salida."""
    prices = prices or {}
    total = 0.0
    for sleeve in state["sleeves"].values():
        total += float(sleeve["cash"])
        pos = sleeve.get("position")
        if pos:
            mark = prices.get(pos["symbol"], pos.get("last_price", pos["entry_price"]))
            total += pos["units"] * mark * (1.0 - FEE_RATE)
    return total


# ------------------------------ INDICADORES ------------------------------
# Las velas son listas [marca_de_tiempo_ms, apertura, máximo, mínimo, cierre, volumen].
def atr_at(candles, i, period=ATR_PERIOD):
    if i < period:
        return None
    total = 0.0
    for j in range(i - period + 1, i + 1):
        high, low, previous_close = candles[j][2], candles[j][3], candles[j - 1][4]
        total += max(high - low, abs(high - previous_close), abs(low - previous_close))
    return total / period


def breakout_level(candles, i, lookback):
    """Máximo de las `lookback` velas anteriores a la vela i (sin incluirla)."""
    if i < lookback:
        return None
    return max(c[2] for c in candles[i - lookback:i])


def exit_level(candles, i, window):
    if i < window:
        return None
    return min(c[3] for c in candles[i - window:i])


def entry_flag(candles, i, lookback):
    level = breakout_level(candles, i, lookback)
    return level is not None and candles[i][4] > level


def exit_flag(candles, i, window):
    level = exit_level(candles, i, window)
    return level is not None and candles[i][4] < level


# ---------------------------- PREPARAR VELAS -----------------------------
def prepare_candles(raw, now_ms=None):
    """Separa velas cerradas de la vela en curso usando el reloj, no suponiendo que la última esté abierta.

    Si Kraken aún no devuelve la vela actual (no ha habido operaciones), se crea una sintética con el
    último cierre para que el motor nunca trate una vela cerrada como si siguiera abierta.
    """
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = sorted({int(r[0]): [int(r[0]), *[float(x) for x in r[1:6]]] for r in raw if r[4] is not None}.values())
    if not rows:
        raise ValueError("sin velas")
    expected_current = (now_ms // BAR_MS) * BAR_MS
    if rows[-1][0] >= expected_current:
        closed, current = rows[:-1], rows[-1]
    else:
        last_close = rows[-1][4]
        closed, current = rows, [expected_current, last_close, last_close, last_close, last_close, 0.0]
    needed = max(LOOKBACKS) + ATR_PERIOD + 2
    if len(closed) < needed:
        raise ValueError(f"histórico insuficiente ({len(closed)} velas cerradas, hacen falta {needed})")
    fresh = closed[-1][0] + BAR_MS == current[0]
    return {"closed": closed, "current": current, "price": current[4], "fresh": fresh}


# --------------------------- SIMULADOR DE ÓRDENES ------------------------
def _record_trade(state, trade):
    state["trades"].insert(0, trade)
    state["trades"] = state["trades"][:TRADES_MAX]


def close_sleeve(state, sleeve, raw_price, reason, bar_ts):
    pos = sleeve["position"]
    exit_price = raw_price * (1.0 - SLIPPAGE_RATE)
    sleeve["cash"] += pos["units"] * exit_price * (1.0 - FEE_RATE)
    pnl = sleeve["cash"] - pos["entry_equity"]
    state["realized_pnl"] += pnl
    if pnl > 0:
        state["wins"] += 1
        state["gross_profit"] += pnl
    else:
        state["losses"] += 1
        state["gross_loss"] += -pnl
    _record_trade(state, {
        "time": utc_now(),
        "symbol": pos["symbol"],
        "side": "VENTA",
        "lookback": sleeve["lookback"],
        "price": round(exit_price, 8),
        "quantity": round(pos["units"], 8),
        "pnl": round(pnl, 4),
        "return_pct": round(100.0 * pnl / pos["entry_equity"], 3),
        "reason": reason,
        "candle_time": bar_ts,
    })
    state["last_comment"] = f"Cierre simulado {pos['symbol']} (ventana {sleeve['lookback']}): {reason}. Resultado {pnl:+.2f} USDT."
    sleeve["position"] = None
    sleeve["cooldown_bar"] = bar_ts  # no se reabre en la misma vela en la que se cerró


def open_sleeve(state, sleeve, data, atr):
    closed, current = data["closed"], data["current"]
    price = data["price"] * (1.0 + SLIPPAGE_RATE)
    stop_distance = STOP_ATR * atr
    fraction = min(1.0, SLEEVE_RISK / max(stop_distance / price, 1e-9))
    equity = sleeve["cash"]
    units = equity * fraction * (1.0 - FEE_RATE) / price
    if units <= 0 or equity <= 0:
        return
    lookback = sleeve["lookback"]
    sleeve["cash"] = equity * (1.0 - fraction)
    sleeve["position"] = {
        "symbol": sleeve["symbol"],
        "units": units,
        "entry_price": price,
        "entry_equity": equity,
        "entry_bar_ts": current[0],
        "entry_time": utc_now(),
        "stop": price - stop_distance,
        "fraction": fraction,
        "last_price": data["price"],
    }
    _record_trade(state, {
        "time": utc_now(),
        "symbol": sleeve["symbol"],
        "side": "COMPRA",
        "lookback": lookback,
        "price": round(price, 8),
        "quantity": round(units, 8),
        "pnl": None,
        "return_pct": None,
        "reason": f"cierre de 4h sobre el máximo de {lookback} velas; stop {price - stop_distance:.6g}",
        "candle_time": current[0],
    })
    state["last_comment"] = f"Compra simulada {sleeve['symbol']} (ventana {lookback})."


def _stop_fill(pos, data):
    """Precio de ejecución del stop si se ha tocado desde la entrada; None si no."""
    closed, current = data["closed"], data["current"]
    stop, entry_bar = pos["stop"], pos["entry_bar_ts"]
    bars = [bar for bar in closed if bar[0] >= entry_bar]
    if current[0] > entry_bar:
        bars.append(current)
    for bar in bars:
        if bar[3] <= stop:
            return min(bar[1], stop)  # si la vela abre por debajo del stop, hay hueco y se ejecuta a la apertura
    if data["price"] <= stop:
        return data["price"]
    return None


def _signal_exit_hit(sleeve, data):
    """¿Hubo alguna vela cerrada desde la entrada que cerrase bajo el canal de salida?"""
    closed = data["closed"]
    entry_bar = sleeve["position"]["entry_bar_ts"]
    window = exit_window(sleeve["lookback"])
    start = len(closed)
    while start > 0 and closed[start - 1][0] >= entry_bar:
        start -= 1
    return any(exit_flag(closed, j, window) for j in range(start, len(closed)))


def manage_sleeve(state, sleeve, data, entries_enabled):
    current = data["current"]
    pos = sleeve["position"]
    if pos:
        stop_price = _stop_fill(pos, data)
        if stop_price is not None:
            close_sleeve(state, sleeve, stop_price, "stop de seguridad (4 ATR)", current[0])
            return
        if _signal_exit_hit(sleeve, data):
            close_sleeve(state, sleeve, data["price"], f"cierre bajo el mínimo de {exit_window(sleeve['lookback'])} velas", current[0])
            return
        pos["last_price"] = data["price"]
        return
    if not entries_enabled or not data["fresh"] or current[0] <= sleeve.get("cooldown_bar", 0):
        return
    closed = data["closed"]
    last = len(closed) - 1
    if entry_flag(closed, last, sleeve["lookback"]):
        atr = atr_at(closed, last)
        if atr and atr > 0:
            open_sleeve(state, sleeve, data, atr)


def radar_item(symbol, data, state):
    closed, price = data["closed"], data["price"]
    last = len(closed) - 1
    atr = atr_at(closed, last) or 0.0
    window = closed[-max(LOOKBACKS):]
    high, low = max(c[2] for c in window), min(c[3] for c in window)
    channel = 100.0 * (price - low) / (high - low) if high > low else 50.0
    level = max(c[2] for c in closed[-LOOKBACKS[0]:])
    signals = sum(1 for n in LOOKBACKS if entry_flag(closed, last, n))
    active = sum(1 for n in LOOKBACKS if state["sleeves"][sleeve_key(symbol, n)].get("position"))
    if active:
        status = f"EN POSICIÓN ({active}/{len(LOOKBACKS)})"
    elif signals:
        status = f"SEÑAL ({signals}/{len(LOOKBACKS)})" + ("" if state.get("bot_enabled", True) else " · entradas en pausa")
    else:
        status = "FUERA (sin ruptura)"
    return {
        "symbol": symbol,
        "timestamp": data["current"][0],
        "price": price,
        "atr_pct": round(100.0 * atr / price, 2) if price else 0.0,
        "channel_pos": round(channel, 1),
        "breakout_level": level,
        "dist_breakout": round(100.0 * (level / price - 1.0), 2) if price else 0.0,
        "signals": signals,
        "active": active,
        "status": status,
    }


def _append_equity_point(state, equity):
    history = state.setdefault("equity_history", [])
    now = datetime.now(timezone.utc)
    if history:
        try:
            last = datetime.fromisoformat(history[-1][0])
            if (now - last).total_seconds() < EQUITY_POINT_EVERY_SECONDS:
                return
        except (ValueError, TypeError):
            pass
    history.append([now.isoformat(timespec="seconds"), round(equity, 2)])
    del history[:-EQUITY_HISTORY_MAX]


def run_cycle(exchange, symbols, state, bot_enabled, now_ms=None):
    ensure_sleeves(state)
    state["bot_enabled"] = bool(bot_enabled)  # antes del radar, para que muestre la pausa en este mismo ciclo
    radar, errors, prices = {}, [], {}
    state["last_comment"] = ""
    for symbol in symbols:
        try:
            raw = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
            data = prepare_candles(raw, now_ms)
            prices[symbol] = data["price"]
            for lookback in LOOKBACKS:
                manage_sleeve(state, state["sleeves"][sleeve_key(symbol, lookback)], data, bot_enabled)
            radar[symbol] = radar_item(symbol, data, state)
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {str(exc)[:100]}")
            print(f"Aviso: {symbol} no se pudo procesar: {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)

    if radar:
        state["radar"] = radar
    equity = portfolio_equity(state, prices)
    state["peak_equity"] = max(state.get("peak_equity", INITIAL_BALANCE), equity)
    if state["peak_equity"] > 0:
        drawdown = 100.0 * (state["peak_equity"] - equity) / state["peak_equity"]
        state["max_drawdown_pct"] = max(state.get("max_drawdown_pct", 0.0), drawdown)
    _append_equity_point(state, equity)

    open_count = len(open_positions(state))
    if not state["last_comment"]:
        signals = sum(item["signals"] for item in radar.values())
        if not radar:
            state["last_comment"] = "No se recibieron datos válidos. Revisa el exchange y sus mercados."
        elif open_count:
            state["last_comment"] = f"{open_count} huchas con posición abierta; el motor vigila stops y salidas."
        elif signals and not bot_enabled:
            state["last_comment"] = "Hay señales de compra, pero las nuevas entradas están en pausa."
        else:
            state["last_comment"] = "Sin posiciones: se espera un cierre de 4h por encima de un máximo de 60 a 240 velas."
    state["last_scan"] = utc_now()
    state["last_error"] = " | ".join(errors) if errors else None
    return state
