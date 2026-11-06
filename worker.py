"""Ejecuta exactamente un ciclo de paper trading en GitHub Actions."""
import json
import os
import tempfile
import time
from pathlib import Path

import ccxt

from paper_engine import (
    PAIRS,
    STRATEGY_VERSION,
    default_state,
    ensure_sleeves,
    open_positions,
    portfolio_equity,
    run_cycle,
    utc_now,
)

STATE_PATH = Path("paper_state.json")
ALLOWED_EXCHANGES = {"kraken", "binance", "bybit", "coinbase"}


def summarize_legacy(old):
    """Resume una cuenta de la estrategia anterior (v1) para mostrarla en el panel sin mezclarla con la nueva."""
    cash = float(old.get("cash", 0.0))
    position = old.get("position")
    if position:
        cash += float(position.get("quantity", 0.0)) * float(position.get("last_price", position.get("entry_price", 0.0)))
    initial = float(old.get("initial_balance", 1000.0))
    wins, losses = int(old.get("wins", 0)), int(old.get("losses", 0))
    return {
        "strategy": "v1: scalping en velas de 1 minuto",
        "equity": round(cash, 2),
        "initial_balance": initial,
        "return_pct": round(100.0 * (cash - initial) / initial, 2) if initial else 0.0,
        "wins": wins,
        "losses": losses,
        "max_drawdown_pct": round(float(old.get("max_drawdown_pct", 0.0)), 2),
        "retired_at": utc_now(),
    }


def load_state():
    if not STATE_PATH.exists():
        return default_state()
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"No se pudo leer paper_state.json: {exc}") from exc
    if loaded.get("version") != STRATEGY_VERSION:
        # Estado de otra estrategia: se archiva un resumen y se empieza una cuenta nueva y limpia.
        state = default_state()
        state["legacy_v1"] = summarize_legacy(loaded)
        state["bot_enabled"] = bool(loaded.get("bot_enabled", True))
        print(f"Estado v{loaded.get('version', 1)} archivado; nueva cuenta v{STRATEGY_VERSION} de 1000 USDT.")
        return state
    state = default_state()
    state.update(loaded)
    return ensure_sleeves(state)


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="paper_state_", suffix=".json", dir=str(STATE_PATH.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, STATE_PATH)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def load_markets_with_retries(exchange, attempts=3, pause_seconds=5):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            exchange.load_markets()
            return
        except Exception as exc:  # errores de red o límites temporales del exchange
            last_error = exc
            print(f"Intento {attempt}/{attempts} de cargar mercados falló: {type(exc).__name__}: {str(exc)[:120]}")
            if attempt < attempts:
                time.sleep(pause_seconds)
    raise last_error


def main():
    state = load_state()

    # Solo una ejecución manual puede cambiar el interruptor persistente.
    if os.environ.get("SET_BOT_ENABLED") == "1":
        state["bot_enabled"] = os.environ.get("BOT_ENABLED_INPUT", "true").lower() == "true"

    exchange_id = os.environ.get("EXCHANGE_ID", "kraken").lower()
    if exchange_id not in ALLOWED_EXCHANGES:
        raise ValueError(f"Exchange no permitido: {exchange_id}")

    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    })
    try:
        load_markets_with_retries(exchange)
    except Exception as exc:
        # Latido: se guarda igualmente que el motor se ejecutó, para que el panel distinga
        # "el exchange falla" de "GitHub no ha lanzado el motor". El estado de la cuenta no cambia.
        message = f"{exchange_id}: no se pudieron cargar los mercados ({type(exc).__name__}: {str(exc)[:120]})"
        print(f"::warning::{message}")
        state["worker_last_run"] = utc_now()
        state["worker_exchange"] = exchange_id.upper()
        state["last_error"] = message
        save_state(state)
        return
    available = [symbol for symbol in PAIRS if symbol in exchange.markets]
    if not available:
        raise RuntimeError(f"{exchange_id} no ofrece ninguno de los pares configurados.")

    enabled = bool(state.get("bot_enabled", True))
    print(f"Exchange: {exchange_id}; pares: {', '.join(available)}; nuevas entradas: {enabled}")
    state = run_cycle(exchange, available, state, enabled)
    state["worker_last_run"] = utc_now()
    state["worker_exchange"] = exchange_id.upper()
    save_state(state)
    print(
        f"Ciclo completado. Último escaneo: {state.get('last_scan')}; patrimonio: {portfolio_equity(state):.2f} USDT; "
        f"posiciones abiertas: {len(open_positions(state))}"
    )
    print(f"Comentario: {state.get('last_comment')}")
    if state.get("last_error"):
        print(f"Advertencias: {state['last_error']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR DEL WORKER: {type(exc).__name__}: {exc}")
        raise
