"""Tests for scripts/kalshi_edge_scanner.py and scripts/kalshi_paper_bot.py.

Everything runs offline: network calls are monkeypatched with realistic
response shapes from the Kalshi / NWS / Open-Meteo public APIs.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

import kalshi_edge_scanner as scanner  # noqa: E402
import kalshi_paper_bot as bot  # noqa: E402


# ----------------------------------------------------------------------------
# scanner: pure logic
# ----------------------------------------------------------------------------

def test_event_date_from_ticker():
    assert scanner.event_date_from_ticker("KXHIGHNY-26JUL17") == "2026-07-17"
    assert scanner.event_date_from_ticker("KXHIGHNY-26JUL17-B85") == "2026-07-17"
    assert scanner.event_date_from_ticker("garbage") is None


def test_taker_fee_rounds_up_to_cent():
    assert scanner.taker_fee(0.5) == 0.02   # 7 * .25 = 1.75c -> 2c
    assert scanner.taker_fee(0.05) == 0.01  # 0.3325c -> 1c


def test_blended_forecast_multi_model():
    fc = {"nws": {"2026-07-20": 90.0},
          "om": {"2026-07-20": {"ecmwf_ifs025": 92.0, "gfs_seamless": 94.0}}}
    mu, extra_var, sources = scanner.blended_forecast(fc, "2026-07-20")
    assert mu == pytest.approx(92.0)
    assert extra_var == pytest.approx(8 / 3)  # population variance of 90,92,94
    assert len(sources) == 3
    assert scanner.blended_forecast(fc, "2026-07-21") is None


def test_fair_prob_plain_normal():
    m = {"strike_type": "greater", "floor_strike": 89}
    p = scanner.fair_prob_for_strike(m, 92.0, 2.0)
    assert 0.85 < p < 0.95
    m = {"strike_type": "between", "floor_strike": 90, "cap_strike": 94}
    assert 0.7 < scanner.fair_prob_for_strike(m, 92.0, 2.0) < 0.9


def test_fair_prob_observed_max_truncation():
    # Observed 91F already: "high > 90" is certain regardless of forecast.
    m = {"strike_type": "greater", "floor_strike": 90}
    assert scanner.fair_prob_for_strike(m, 88.0, 2.0, obs_max=91.0) == 1.0
    # ...and "high < 91" is impossible.
    m = {"strike_type": "less", "cap_strike": 91}
    assert scanner.fair_prob_for_strike(m, 88.0, 2.0, obs_max=91.0) == 0.0
    # Truncation shifts mass upward vs the plain normal.
    m = {"strike_type": "greater", "floor_strike": 93}
    plain = scanner.fair_prob_for_strike(m, 92.0, 2.0)
    trunc = scanner.fair_prob_for_strike(m, 92.0, 2.0, obs_max=91.0)
    assert trunc > plain
    # Forecast entirely below the observed max: day is done, high = obs.
    m = {"strike_type": "greater", "floor_strike": 95}
    assert scanner.fair_prob_for_strike(m, 70.0, 1.0, obs_max=90.0) == 0.0


def test_openmeteo_multi_model_parsing(monkeypatch):
    def fake_get_json(url, params=None):
        assert params["temperature_unit"] == "fahrenheit"
        assert params["models"] == ",".join(scanner.OPEN_METEO_MODELS)
        return {"daily": {
            "time": ["2026-07-20", "2026-07-21"],
            "temperature_2m_max_ecmwf_ifs025": [91.4, 89.0],
            "temperature_2m_max_gfs_seamless": [93.0, None],
            "temperature_2m_max_icon_seamless": [None, 90.1],
        }}
    monkeypatch.setattr(scanner, "get_json", fake_get_json)
    highs = scanner.openmeteo_daily_highs(40.78, -73.97)
    assert highs["2026-07-20"] == {"ecmwf_ifs025": 91.4, "gfs_seamless": 93.0}
    assert highs["2026-07-21"] == {"ecmwf_ifs025": 89.0, "icon_seamless": 90.1}


def test_evaluate_emits_raw_payload():
    m = {"ticker": "KXHIGHNY-26JUL20-T92", "event_ticker": "KXHIGHNY-26JUL20",
         "strike_type": "greater", "floor_strike": 92,
         "yes_ask_dollars": "0.40", "no_ask_dollars": "0.65",
         "yes_ask_size_fp": "100", "no_ask_size_fp": "100"}
    alerts = []
    scanner.evaluate(m, 0.60, alerts, benchmark="b", settlement="s", why="w")
    assert len(alerts) == 1
    raw = alerts[0]["raw"]
    assert raw["action"] == "buy_yes" and raw["ask"] == 0.40
    # net = 0.60 - 0.40 - fee(0.02) - haircut(0.05) = 0.13
    assert raw["net"] == pytest.approx(0.13)
    assert raw["fair"] == pytest.approx(0.60)


def test_min_price_filters_longshots():
    # A 5c longshot that the model loves is exactly the losing pattern -- and
    # is now below MIN_PRICE, so it must not produce an alert.
    m = {"ticker": "KXHIGHMIA-26JUL20-B88.5", "event_ticker": "KXHIGHMIA-26JUL20",
         "strike_type": "greater", "floor_strike": 88,
         "yes_ask_dollars": "0.05", "no_ask_dollars": "0.97",
         "yes_ask_size_fp": "800", "no_ask_size_fp": "800"}
    alerts = []
    scanner.evaluate(m, 0.30, alerts, benchmark="b", settlement="s", why="w")
    assert alerts == []


def test_max_edge_ratio_rejects_too_good_to_be_true():
    # Market says ~15c, model claims 45% -- a 3x relative gap. That is the
    # overconfidence fingerprint from the live run and must be skipped, even
    # though the ask clears MIN_PRICE and the raw net edge looks huge.
    m = {"ticker": "KXHIGHCHI-26JUL20-B85.5", "event_ticker": "KXHIGHCHI-26JUL20",
         "strike_type": "greater", "floor_strike": 85,
         "yes_ask_dollars": "0.15", "no_ask_dollars": "0.88",
         "yes_ask_size_fp": "200", "no_ask_size_fp": "200"}
    alerts = []
    scanner.evaluate(m, 0.45, alerts, benchmark="b", settlement="s", why="w")
    assert all(a["raw"]["action"] != "buy_yes" for a in alerts)
    # A believable central edge (market 40c, model 60%: net 0.13, ratio 1.5x)
    # still passes both the ratio guard and the raised net-edge threshold.
    m2 = dict(m, yes_ask_dollars="0.40", no_ask_dollars="0.62")
    alerts2 = []
    scanner.evaluate(m2, 0.60, alerts2, benchmark="b", settlement="s", why="w")
    assert any(a["raw"]["action"] == "buy_yes" for a in alerts2)


# ----------------------------------------------------------------------------
# paper bot: full open -> settle cycle, offline
# ----------------------------------------------------------------------------

@pytest.fixture
def paper_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    return tmp_path


def _weather_alert(net=0.15):
    return {"ticker": "KXHIGHNY-26JUL20-T92", "kind": "BUY YES",
            "raw": {"action": "buy_yes", "ticker": "KXHIGHNY-26JUL20-T92",
                    "event": "KXHIGHNY-26JUL20", "ask": 0.40, "fair": 0.60,
                    "net": net, "size": 100}}


def test_cycle_opens_then_settles(paper_dir, monkeypatch, capsys):
    monkeypatch.setattr(bot, "scan", lambda verbose=False: ([_weather_alert()], []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": []})

    state = bot.load_state()
    bot.run_cycle(state, verbose=False)

    assert len(state["positions"]) == 1
    pos = next(iter(state["positions"].values()))
    cost = 0.40 + scanner.taker_fee(0.40)  # 0.42
    # tenth-Kelly ~$31 -> capped at 2% of equity ($20) -> int($20 // $0.42)
    assert pos["contracts"] == min(int((bot.MAX_TRADE_FRACTION * 1000) // cost),
                                   100, bot.MAX_CONTRACTS_PER_TRADE)
    assert pos["contracts"] == 47
    assert pos["model_fair"] == pytest.approx(0.60)
    assert state["cash"] == pytest.approx(1000 - pos["cost_total"])

    # Second cycle: market settled YES and is no longer in the scan.
    monkeypatch.setattr(bot, "scan", lambda verbose=False: ([], []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": [
        {"ticker": "KXHIGHNY-26JUL20-T92", "status": "settled", "result": "yes"}]})
    bot.run_cycle(state, verbose=False)

    assert not state["positions"]
    expected_pnl = pos["contracts"] * 1.0 - pos["cost_total"]
    assert state["realized_pnl"] == pytest.approx(expected_pnl)
    assert state["cash"] == pytest.approx(1000 + expected_pnl)

    # State persisted and reloadable; logs written.
    with open(os.path.join(str(paper_dir), "state.json")) as fh:
        assert json.load(fh)["cycles"] == 2
    assert os.path.exists(os.path.join(str(paper_dir), "trades.csv"))
    assert os.path.exists(os.path.join(str(paper_dir), "equity.csv"))


def test_losing_settlement(paper_dir, monkeypatch):
    monkeypatch.setattr(bot, "scan", lambda verbose=False: ([_weather_alert()], []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": []})
    state = bot.load_state()
    bot.run_cycle(state, verbose=False)
    pos = next(iter(state["positions"].values()))

    monkeypatch.setattr(bot, "scan", lambda verbose=False: ([], []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": [
        {"ticker": "KXHIGHNY-26JUL20-T92", "status": "settled", "result": "no"}]})
    bot.run_cycle(state, verbose=False)
    assert state["realized_pnl"] == pytest.approx(-pos["cost_total"])
    assert state["cash"] == pytest.approx(1000 - pos["cost_total"])


def test_arb_basket_fills_all_legs(paper_dir, monkeypatch):
    legs = [{"ticker": f"KXFED-26JUL-T{i}", "action": "buy_yes",
             "ask": 0.25, "size": 60} for i in range(3)]
    arb = {"ticker": "KXFED-26JUL", "kind": "INTERNAL ARB (buy all YES)",
           "raw": {"action": "arb_yes", "event": "KXFED-26JUL",
                   "net": 0.16, "legs": legs}}
    monkeypatch.setattr(bot, "scan", lambda verbose=False: ([arb], []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": []})
    state = bot.load_state()
    bot.run_cycle(state, verbose=False)

    assert len(state["positions"]) == 3
    sets = {p["contracts"] for p in state["positions"].values()}
    assert len(sets) == 1  # equal size on every leg
    per_set = 3 * (0.25 + scanner.taker_fee(0.25))
    # capped by the thinnest leg (60) vs the 15% bankroll cap
    assert sets.pop() == min(60, int((bot.MAX_ARB_FRACTION * 1000) // per_set))

    # Re-running must not stack the same basket.
    bot.run_cycle(state, verbose=False)
    assert len(state["positions"]) == 3


def test_exposure_limits_respected(paper_dir, monkeypatch):
    # 30 distinct high-edge alerts -> capped by MAX_OPEN_POSITIONS.
    alerts = []
    for i in range(30):
        a = _weather_alert()
        a["raw"] = dict(a["raw"], ticker=f"T{i}", event=f"E{i % 5}")
        a["ticker"] = f"T{i}"
        alerts.append(a)
    monkeypatch.setattr(bot, "scan", lambda verbose=False: (alerts, []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": []})
    state = bot.load_state()
    bot.run_cycle(state, verbose=False)

    assert len(state["positions"]) <= bot.MAX_OPEN_POSITIONS
    assert bot.cost_basis(state) <= bot.MAX_TOTAL_EXPOSURE * bot.equity(state) + 1e-6
    assert state["cash"] >= 0


def test_tiny_edge_or_thin_book_skipped(paper_dir, monkeypatch):
    thin = _weather_alert()
    thin["raw"] = dict(thin["raw"], size=0)          # nothing showing
    bad = _weather_alert()
    bad["raw"] = dict(bad["raw"], ticker="X", fair=0.41)  # edge < fee -> Kelly 0
    monkeypatch.setattr(bot, "scan", lambda verbose=False: ([thin, bad], []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": []})
    state = bot.load_state()
    bot.run_cycle(state, verbose=False)
    assert not state["positions"]
    assert state["cash"] == pytest.approx(1000)


def test_contract_cap_limits_cheap_position(paper_dir, monkeypatch):
    # A deep, cheap book that Kelly would happily buy thousands of is clamped
    # to MAX_CONTRACTS_PER_TRADE -- the 781-contract lottery ticket can't recur.
    big = {"ticker": "KXHIGHMIA-26JUL22-B94.5", "kind": "BUY YES",
           "raw": {"action": "buy_yes", "ticker": "KXHIGHMIA-26JUL22-B94.5",
                   "event": "KXHIGHMIA-26JUL22", "ask": 0.10, "fair": 0.22,
                   "net": 0.12, "size": 5000}}
    monkeypatch.setattr(bot, "scan", lambda verbose=False: ([big], []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": []})
    state = bot.load_state()
    bot.run_cycle(state, verbose=False)
    pos = next(iter(state["positions"].values()))
    assert pos["contracts"] <= bot.MAX_CONTRACTS_PER_TRADE


def test_daily_drawdown_halt_blocks_new_trades(paper_dir, monkeypatch):
    monkeypatch.setattr(bot, "scan", lambda verbose=False: ([_weather_alert()], []))
    monkeypatch.setattr(bot, "get_json", lambda url, params=None: {"markets": []})
    state = bot.load_state()
    # Seed a 24h window whose peak is far above current equity -> halt condition.
    state["equity_window"] = [[bot.now_iso(), 1000.0]]
    state["cash"] = 800.0  # equity now 800 vs peak 1000 = -20% > 10% halt
    bot.run_cycle(state, verbose=False)
    assert state["positions"] == {}   # suppressed
    assert state["cash"] == pytest.approx(800.0)


def test_calibrate_reads_settlements(paper_dir, monkeypatch, capsys):
    # Two settled trades the model rated 25%, both lost -> overconfident report.
    monkeypatch.setattr(bot, "now_iso", lambda: "2026-07-20T00:00:00Z")
    bot.log_trade("settle", "A", "yes", 10, 0.0, 0.0, -3.0, "result=no", fair=0.25)
    bot.log_trade("settle", "B", "yes", 10, 1.0, 10.0, 7.0, "result=yes", fair=0.25)
    bot.calibrate()
    out = capsys.readouterr().out
    assert "2 settled" in out
    assert "OVERALL" in out
