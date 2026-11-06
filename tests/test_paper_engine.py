import json
import random
import tempfile
import unittest
from pathlib import Path

import paper_engine as pe

BAR = pe.BAR_MS
START = 1_700_000_000_000 // BAR * BAR


def synthetic_bars(count, seed=11, start_ts=START):
    """Serie determinista con tramos alcistas, laterales y bajistas."""
    rng = random.Random(seed)
    price, drift, bars = 100.0, 0.0, []
    for i in range(count):
        if i % 150 == 0:
            drift = rng.choice([-0.0015, 0.0, 0.0025, 0.004])
        close = price * (1 + drift + rng.gauss(0, 0.012))
        high = max(price, close) * (1 + abs(rng.gauss(0, 0.004)))
        low = min(price, close) * (1 - abs(rng.gauss(0, 0.004)))
        bars.append([start_ts + i * BAR, price, high, low, close, 1000.0])
        price = close
    return bars


def flat_bars(count, price=100.0, start_ts=START):
    return [[start_ts + i * BAR, price, price * 1.001, price * 0.999, price, 1000.0] for i in range(count)]


class FakeExchange:
    def __init__(self, data=None):
        self.data = data or {}

    def fetch_ohlcv(self, symbol, timeframe, limit):
        if symbol not in self.data:
            raise RuntimeError("par no disponible")
        return self.data[symbol]


def cycle(state, bars, now_ms, symbol="BTC/USDT", enabled=True):
    return pe.run_cycle(FakeExchange({symbol: bars}), [symbol], state, enabled, now_ms=now_ms)


def breakout_scenario(extra=()):
    """300 velas planas y una vela que cierra con fuerza sobre todos los máximos (ruptura)."""
    bars = flat_bars(300)
    ts = bars[-1][0] + BAR
    bars.append([ts, 100.0, 108.0, 99.9, 107.0, 5000.0])          # vela de ruptura (cerrada)
    bars.extend(extra)
    return bars


class IndicatorTests(unittest.TestCase):
    def test_breakout_level_excludes_the_signal_bar(self):
        bars = flat_bars(80)
        bars.append([bars[-1][0] + BAR, 100.0, 200.0, 99.0, 100.05, 1.0])  # máximo enorme en la propia vela, cierre bajo el nivel
        level = pe.breakout_level(bars, len(bars) - 1, 60)
        self.assertAlmostEqual(level, 100.1, places=6)  # el máximo de 200 de la vela actual no cuenta
        self.assertFalse(pe.entry_flag(bars, len(bars) - 1, 60))

    def test_entry_and_exit_flags(self):
        bars = flat_bars(80)
        bars.append([bars[-1][0] + BAR, 100.0, 106.0, 99.9, 105.0, 1.0])
        self.assertTrue(pe.entry_flag(bars, len(bars) - 1, 60))
        bars.append([bars[-1][0] + BAR, 105.0, 105.0, 90.0, 92.0, 1.0])
        self.assertTrue(pe.exit_flag(bars, len(bars) - 1, 30))

    def test_atr_on_constant_range(self):
        bars = [[i * BAR, 100.0, 101.0, 99.0, 100.0, 1.0] for i in range(40)]
        self.assertAlmostEqual(pe.atr_at(bars, 30), 2.0, places=9)
        self.assertIsNone(pe.atr_at(bars, 5))

    def test_prepare_candles_uses_the_clock_to_find_the_open_candle(self):
        bars = flat_bars(300)
        open_candle_ts = bars[-1][0] + BAR
        with_current = bars + [[open_candle_ts, 100.0, 100.5, 99.8, 100.2, 10.0]]
        data = pe.prepare_candles(with_current, now_ms=open_candle_ts + 60_000)
        self.assertEqual(len(data["closed"]), 300)
        self.assertEqual(data["current"][0], open_candle_ts)
        self.assertTrue(data["fresh"])
        # Kraken todavía no devuelve la vela en curso: la última fila es una vela CERRADA y no debe descartarse.
        data = pe.prepare_candles(bars, now_ms=open_candle_ts + 60_000)
        self.assertEqual(len(data["closed"]), 300)
        self.assertEqual(data["current"][0], open_candle_ts)
        self.assertEqual(data["price"], bars[-1][4])

    def test_prepare_candles_rejects_short_history(self):
        with self.assertRaises(ValueError):
            pe.prepare_candles(flat_bars(100), now_ms=START + 101 * BAR)


class EngineTests(unittest.TestCase):
    def open_after_breakout(self):
        state = pe.default_state()
        bars = breakout_scenario()
        now = bars[-1][0] + BAR + 60_000
        current = [bars[-1][0] + BAR, 107.0, 107.0, 107.0, 107.0, 0.0]
        cycle(state, bars + [current], now)
        return state, bars, now

    def test_initial_equity_is_the_starting_balance(self):
        self.assertAlmostEqual(pe.portfolio_equity(pe.default_state()), pe.INITIAL_BALANCE, places=6)
        self.assertEqual(len(pe.default_state()["sleeves"]), len(pe.PAIRS) * len(pe.LOOKBACKS))

    def test_breakout_opens_positions_in_every_window(self):
        state, _, _ = self.open_after_breakout()
        opened = [s for s in state["sleeves"].values() if s["position"]]
        self.assertEqual(len(opened), len(pe.LOOKBACKS))
        buys = [t for t in state["trades"] if t["side"] == "COMPRA"]
        self.assertEqual(len(buys), len(pe.LOOKBACKS))
        # Solo se invierte el BTC; las demás huchas siguen intactas.
        self.assertEqual(sum(1 for s in state["sleeves"].values() if s["position"] and s["symbol"] != "BTC/USDT"), 0)

    def test_position_size_never_risks_more_than_the_cap(self):
        state, _, _ = self.open_after_breakout()
        sleeve = state["sleeves"][pe.sleeve_key("BTC/USDT", 60)]
        pos = sleeve["position"]
        loss_at_stop = pos["units"] * (pos["entry_price"] - pos["stop"])
        self.assertLessEqual(loss_at_stop, pe.SLEEVE_RISK * pos["entry_equity"] * 1.0001)
        self.assertLessEqual(pos["fraction"], 1.0)
        self.assertGreater(pos["entry_price"], 107.0)  # incluye deslizamiento

    def test_equity_after_entry_only_loses_the_costs(self):
        state, _, _ = self.open_after_breakout()
        equity = pe.portfolio_equity(state, {"BTC/USDT": 107.0})
        self.assertLess(equity, pe.INITIAL_BALANCE)
        self.assertGreater(equity, pe.INITIAL_BALANCE * 0.99)

    def test_stop_exit_uses_the_stop_level_and_counts_a_loss(self):
        state, bars, now = self.open_after_breakout()
        stop = state["sleeves"][pe.sleeve_key("BTC/USDT", 60)]["position"]["stop"]
        entry_bar = bars[-1][0] + BAR
        crash = [entry_bar, 107.0, 107.5, stop - 5.0, stop - 3.0, 9000.0]     # la vela de entrada ya cerrada tocó el stop
        nxt = [entry_bar + BAR, stop - 3.0, stop - 3.0, stop - 3.0, stop - 3.0, 0.0]
        bars2 = bars + [crash, nxt]
        cycle(state, bars2, nxt[0] + 60_000)
        self.assertEqual(sum(1 for s in state["sleeves"].values() if s["position"]), 0)
        self.assertGreaterEqual(state["losses"], len(pe.LOOKBACKS))
        sells = [t for t in state["trades"] if t["side"] == "VENTA"]
        self.assertTrue(all(t["price"] <= stop for t in sells))
        self.assertTrue(all(t["pnl"] < 0 for t in sells))

    def test_cooldown_blocks_reentry_in_the_same_bar(self):
        state, bars, now = self.open_after_breakout()
        stop = state["sleeves"][pe.sleeve_key("BTC/USDT", 60)]["position"]["stop"]
        entry_bar = bars[-1][0] + BAR
        crash = [entry_bar, 107.0, 107.5, stop - 5.0, 112.0, 9000.0]          # toca el stop pero cierra sobre el máximo
        nxt = [entry_bar + BAR, 112.0, 112.0, 112.0, 112.0, 0.0]
        cycle(state, bars + [crash, nxt], nxt[0] + 60_000)
        cycle(state, bars + [crash, nxt], nxt[0] + 120_000)
        # Las salidas ocurrieron en la vela `nxt`; ninguna hucha puede volver a comprar hasta la siguiente vela.
        self.assertEqual(sum(1 for t in state["trades"] if t["side"] == "COMPRA"), len(pe.LOOKBACKS))

    def test_no_double_entry_when_the_cycle_runs_many_times_in_the_same_bar(self):
        state, bars, now = self.open_after_breakout()
        current = [bars[-1][0] + BAR, 107.0, 107.4, 106.8, 107.2, 10.0]
        for minutes in (5, 10, 15, 60, 120):
            cycle(state, bars + [current], bars[-1][0] + BAR + minutes * 60_000)
        self.assertEqual(sum(1 for t in state["trades"] if t["side"] == "COMPRA"), len(pe.LOOKBACKS))

    def test_signal_exit_is_caught_up_after_missed_runs(self):
        state, bars, _ = self.open_after_breakout()
        entry_bar = bars[-1][0] + BAR
        # Tras la entrada, GitHub no ejecuta nada durante varias velas: una cierra bajo el canal de salida y luego se recupera.
        later = [
            [entry_bar, 107.0, 107.5, 106.0, 107.0, 10.0],
            [entry_bar + BAR, 107.0, 107.0, 98.5, 98.8, 10.0],   # cierre bajo el mínimo de las velas previas
            [entry_bar + 2 * BAR, 98.8, 109.0, 98.7, 108.0, 10.0],
            [entry_bar + 3 * BAR, 108.0, 108.0, 108.0, 108.0, 0.0],
        ]
        cycle(state, bars + later, later[-1][0] + 60_000)
        remaining = [s["lookback"] for s in state["sleeves"].values() if s["position"]]
        self.assertNotIn(60, remaining)

    def test_entries_paused_blocks_buys_but_not_exits(self):
        state = pe.default_state()
        bars = breakout_scenario()
        current = [bars[-1][0] + BAR, 107.0, 107.0, 107.0, 107.0, 0.0]
        cycle(state, bars + [current], current[0] + 60_000, enabled=False)
        self.assertEqual(len(state["trades"]), 0)
        self.assertIn("pausa", state["radar"]["BTC/USDT"]["status"])

    def test_one_failing_pair_does_not_stop_the_others(self):
        state = pe.default_state()
        bars = flat_bars(300)
        now = bars[-1][0] + BAR + 60_000
        exchange = FakeExchange({"ETH/USDT": bars})
        pe.run_cycle(exchange, ["BTC/USDT", "ETH/USDT"], state, True, now_ms=now)
        self.assertIn("ETH/USDT", state["radar"])
        self.assertNotIn("BTC/USDT", state["radar"])
        self.assertIn("BTC/USDT", state["last_error"])

    def test_radar_reports_distance_to_breakout(self):
        state = pe.default_state()
        bars = flat_bars(300)
        cycle(state, bars, bars[-1][0] + BAR + 60_000)
        item = state["radar"]["BTC/USDT"]
        self.assertEqual(item["signals"], 0)
        self.assertGreater(item["dist_breakout"], 0)
        self.assertIn("FUERA", item["status"])


class ConsistencyTests(unittest.TestCase):
    """El motor, llamado dos veces por vela como en producción, debe replicar un simulador de referencia."""

    @staticmethod
    def reference(bars, start, lookback):
        """Simulador independiente de una hucha: mismas reglas, escrito de forma directa."""
        fee, slip, window = pe.FEE_RATE, pe.SLIPPAGE_RATE, pe.exit_window(lookback)
        trades, pos, cash, cash_left, cooldown = [], None, 1.0, 0.0, 0
        for t in range(start, len(bars)):
            ts, o, h, l = bars[t][0], bars[t][1], bars[t][2], bars[t][3]
            prev = t - 1
            if pos:
                signal = prev >= window and bars[prev][4] < min(b[3] for b in bars[prev - window:prev])
                signal = signal and bars[prev][0] >= pos["bar"]
                stopped = pos["bar"] < ts and l <= pos["stop"]
                if signal or stopped:
                    raw = o if signal else min(o, pos["stop"])
                    cash = cash_left + pos["units"] * raw * (1 - slip) * (1 - fee)
                    trades.append((pos["bar"], ts, cash / pos["equity"] - 1))
                    pos, cooldown = None, ts
                continue
            if prev >= lookback and ts > cooldown and bars[prev][4] > max(b[2] for b in bars[prev - lookback:prev]):
                atr = sum(max(bars[k][2] - bars[k][3], abs(bars[k][2] - bars[k - 1][4]), abs(bars[k][3] - bars[k - 1][4]))
                          for k in range(prev - 13, prev + 1)) / 14
                px = o * (1 + slip)
                fraction = min(1.0, pe.SLEEVE_RISK / max(pe.STOP_ATR * atr / px, 1e-9))
                pos = {"equity": cash, "units": cash * fraction * (1 - fee) / px, "stop": px - pe.STOP_ATR * atr, "bar": ts}
                cash_left = cash * (1 - fraction)
        return trades

    def test_engine_matches_reference_simulator(self):
        bars = synthetic_bars(1400, seed=5)
        start = 300
        state, exchange = pe.default_state(), FakeExchange()
        for t in range(start, len(bars)):
            ts, o, h, l, c, v = bars[t]
            history = bars[max(0, t - 719):t]
            exchange.data = {"BTC/USDT": history + [[ts, o, o, o, o, 0.0]]}
            pe.run_cycle(exchange, ["BTC/USDT"], state, True, now_ms=ts + 60_000)
            exchange.data = {"BTC/USDT": history + [[ts, o, h, l, c, v]]}
            pe.run_cycle(exchange, ["BTC/USDT"], state, True, now_ms=ts + BAR - 60_000)

        live, buys = {n: [] for n in pe.LOOKBACKS}, {}
        for trade in reversed(state["trades"]):
            n = trade["lookback"]
            if trade["side"] == "COMPRA":
                buys[n] = trade
            else:
                live[n].append((buys.pop(n)["candle_time"], trade["candle_time"]))
        total = 0
        for n in pe.LOOKBACKS:
            expected = [(a, b) for a, b, _ in self.reference(bars, start, n)]
            self.assertEqual(live[n], expected, f"ventana {n}")
            total += len(expected)
        self.assertGreater(total, 5, "la serie sintética debería generar operaciones")


class WorkerTests(unittest.TestCase):
    def test_old_state_is_archived_and_a_clean_account_starts(self):
        import worker
        old = {"cash": 961.12, "initial_balance": 1000.0, "position": None, "wins": 0, "losses": 49,
               "max_drawdown_pct": 3.89, "bot_enabled": False, "trades": [{"x": 1}]}
        with tempfile.TemporaryDirectory() as folder:
            previous = worker.STATE_PATH
            worker.STATE_PATH = Path(folder) / "paper_state.json"
            try:
                worker.STATE_PATH.write_text(json.dumps(old), encoding="utf-8")
                state = worker.load_state()
            finally:
                worker.STATE_PATH = previous
        self.assertEqual(state["version"], pe.STRATEGY_VERSION)
        self.assertEqual(state["trades"], [])
        self.assertAlmostEqual(pe.portfolio_equity(state), 1000.0, places=6)
        self.assertEqual(state["legacy_v1"]["losses"], 49)
        self.assertAlmostEqual(state["legacy_v1"]["return_pct"], -3.89, places=2)
        self.assertFalse(state["bot_enabled"])  # se respeta la pausa del usuario

    def test_state_roundtrip_through_json(self):
        import worker
        state = pe.default_state()
        bars = breakout_scenario()
        cycle(state, bars + [[bars[-1][0] + BAR, 107.0, 107.0, 107.0, 107.0, 0.0]], bars[-1][0] + BAR + 60_000)
        with tempfile.TemporaryDirectory() as folder:
            previous = worker.STATE_PATH
            worker.STATE_PATH = Path(folder) / "paper_state.json"
            try:
                worker.save_state(state)
                loaded = worker.load_state()
            finally:
                worker.STATE_PATH = previous
        self.assertAlmostEqual(pe.portfolio_equity(loaded, {"BTC/USDT": 107.0}), pe.portfolio_equity(state, {"BTC/USDT": 107.0}), places=6)
        self.assertEqual(len(pe.open_positions(loaded)), len(pe.LOOKBACKS))


if __name__ == "__main__":
    unittest.main()
