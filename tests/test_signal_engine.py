import unittest

import main


def make_candles(count=80):
    candles = []
    start = 1_790_000_000
    for i in range(count):
        close = 100.0 + i * 0.05
        candles.append({
            "time": start + i * 900,
            "open": close - 0.02,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": 100.0,
        })
    return candles


class IndependentSignalTests(unittest.TestCase):
    def test_candidate_has_correct_two_to_one_target(self):
        candles = make_candles(2)
        candidate = main._build_independent_candidate(
            "Test Strategy", "BUY", candles, 2.0, "unit test", stop_price=candles[-1]["close"] - 2.0
        )
        self.assertIsNotNone(candidate)
        self.assertAlmostEqual(candidate["entry"] - candidate["sl"], 2.0)
        self.assertAlmostEqual(candidate["tp"] - candidate["entry"], 4.0)
        self.assertEqual(candidate["risk_reward"], "1:2")
        self.assertIn("Test Strategy:BUY:", candidate["key"])

    def test_candidate_rejects_stop_on_wrong_side(self):
        candles = make_candles(2)
        candidate = main._build_independent_candidate(
            "Test Strategy", "BUY", candles, 2.0, "unit test", stop_price=200.0
        )
        self.assertIsNone(candidate)

    def test_bos_detector_emits_own_candidate(self):
        candles = make_candles(80)
        last = candles[-1]
        last.update({
            "open": 104.20,
            "high": 105.00,
            "low": 104.00,
            "close": 104.80,
            "volume": 150.0,
        })
        candidates = main.independent_signal_candidates(candles, make_candles(80), make_candles(80))
        bos = [c for c in candidates if c["strategy"] == "BOS Breakout"]
        self.assertTrue(bos)
        self.assertEqual(bos[0]["direction"], "BUY")
        self.assertLess(bos[0]["sl"], bos[0]["entry"])
        self.assertGreater(bos[0]["tp"], bos[0]["entry"])

    def test_alert_formatter_names_strategy_and_risk_levels(self):
        candles = make_candles(2)
        candidate = main._build_independent_candidate(
            "Liquidity Sweep", "SELL", candles, 2.0, "unit test", stop_price=105.0
        )
        message = main.format_independent_signal(candidate, "CLEAR")
        self.assertIn("Liquidity Sweep", message)
        self.assertIn("Stop Loss", message)
        self.assertIn("Take Profit", message)
        self.assertIn("SELL", message)


if __name__ == "__main__":
    unittest.main()
