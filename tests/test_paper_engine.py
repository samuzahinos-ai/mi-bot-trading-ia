import unittest

from paper_engine import (
    close_position,
    default_state,
    ema_series,
    evaluate_pair,
    rsi_series,
)


class FakeExchange:
    def fetch_order_book(self, symbol, limit=15):
        return {
            "bids": [[99.9, 10.0], [99.8, 8.0]],
            "asks": [[100.1, 5.0], [100.2, 4.0]],
        }


def make_candles(count=320):
    candles = []
    price = 100.0
    for index in range(count):
        # Serie determinista ascendente con oscilación suave.
        close = price + 0.08 + (0.15 if index % 7 == 0 else -0.03)
        open_price = price
        high = max(open_price, close) + 0.12
        low = min(open_price, close) - 0.10
        volume = 1000.0 + (index % 11) * 15.0
        candles.append([index * 60_000, open_price, high, low, close, volume])
        price = close
    return candles


class PaperEngineTests(unittest.TestCase):
    def test_ema_returns_same_number_of_values(self):
        values = [float(i) for i in range(1, 101)]
        result = ema_series(values, 20)
        self.assertEqual(len(result), len(values))
        self.assertAlmostEqual(result[-1], 90.5, delta=10.0)

    def test_rsi_stays_in_valid_range(self):
        values = [100.0 + i * 0.1 for i in range(40)]
        result = rsi_series(values, 14)
        self.assertEqual(len(result), len(values))
        self.assertIsNone(result[0])
        self.assertGreaterEqual(result[-1], 0.0)
        self.assertLessEqual(result[-1], 100.0)

    def test_evaluate_pair_returns_bounded_score(self):
        info = evaluate_pair(FakeExchange(), "BTC/USDT", make_candles())
        self.assertGreaterEqual(info["score"], 0)
        self.assertLessEqual(info["score"], 100)
        self.assertEqual(info["symbol"], "BTC/USDT")
        self.assertIn(info["regime"], {"ALCISTA", "LATERAL", "BAJISTA FUERTE"})

    def test_close_position_counts_entry_fee_in_net_pnl(self):
        state = default_state()
        state["cash"] = 0.0
        state["position"] = {
            "symbol": "BTC/USDT",
            "quantity": 10.0,
            "entry_price": 100.0,
            "entry_total_cost": 1001.0,
            "last_price": 100.0,
        }
        close_position(state, 110.0, "test", 123456)
        self.assertAlmostEqual(state["cash"], 1098.9, places=6)
        self.assertAlmostEqual(state["realized_pnl"], 97.9, places=6)
        self.assertEqual(state["wins"], 1)
        self.assertIsNone(state["position"])


if __name__ == "__main__":
    unittest.main()
