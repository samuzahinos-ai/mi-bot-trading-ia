"""Ejecuta exactamente un ciclo de paper trading en GitHub Actions."""
import json
import os
import tempfile
from pathlib import Path

import ccxt

from paper_engine import PAIRS, default_state, run_cycle, utc_now

STATE_PATH = Path("paper_state.json")
ALLOWED_EXCHANGES = {"kraken", "binance", "bybit", "coinbase"}


def load_state():
    if not STATE_PATH.exists():
        return default_state()
    try:
        state = default_state()
        state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        return state
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"No se pudo leer paper_state.json: {exc}") from exc


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
    exchange.load_markets()
    available = [symbol for symbol in PAIRS if symbol in exchange.markets]
    if not available:
        raise RuntimeError(f"{exchange_id} no ofrece ninguno de los pares configurados.")

    enabled = bool(state.get("bot_enabled", True))
    print(f"Exchange: {exchange_id}; pares: {', '.join(available)}; nuevas entradas: {enabled}")
    state = run_cycle(exchange, available, state, enabled)
    state["worker_last_run"] = utc_now()
    state["worker_exchange"] = exchange_id.upper()
    save_state(state)
    print(f"Ciclo completado. Último escaneo: {state.get('last_scan')}; efectivo: {state['cash']:.2f} USDT")
    if state.get("last_error"):
        print(f"Advertencias: {state['last_error']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR DEL WORKER: {type(exc).__name__}: {exc}")
        raise
