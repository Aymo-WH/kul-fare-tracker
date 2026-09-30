"""Config loading for routes.py: the example ships, a local file or env var overrides it."""
import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

TRACKER = Path(__file__).resolve().parents[1] / "tracker"
sys.path.insert(0, str(TRACKER))

import routes  # noqa: E402


def test_example_config_loads():
    rs, th = routes.load_config(routes.EXAMPLE_CONFIG)
    assert rs and len({r.key for r in rs}) == len(rs)
    assert th["max_pct_of_typical"] == 0.95
    for r in rs:
        assert r.trip_type in ("oneway", "return")
        assert r.window is not None or r.window_dates is not None


def test_fixed_dates_and_cutover_are_parsed_as_dates():
    rs, _ = routes.load_config(routes.EXAMPLE_CONFIG)
    fixed = [r for r in rs if r.window_dates]
    assert fixed
    for r in fixed:
        assert all(isinstance(d, date) for d in r.window_dates)
        assert isinstance(r.history_since, date)
        assert r.real_trip


def test_env_var_overrides_path_and_thresholds(tmp_path, monkeypatch):
    cfg = {"thresholds": {"percentile_trigger": 10, "max_pct_of_typical": 0.9},
           "routes": [{"key": "KUL-PEN-OW", "origin": "KUL", "dest": "PEN",
                       "trip_type": "oneway", "label": "KUL → PEN", "window": [20, 90]}]}
    p = tmp_path / "mine.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FARE_ROUTES_CONFIG", str(p))
    try:
        m = importlib.reload(routes)
        assert list(m.BY_KEY) == ["KUL-PEN-OW"]
        assert m.ROUTES[0].window == (20, 90)
        assert m.PERCENTILE_TRIGGER == 10.0 and m.MAX_PCT_OF_TYPICAL == 0.9
    finally:
        monkeypatch.delenv("FARE_ROUTES_CONFIG")
        importlib.reload(routes)


def test_thresholds_default_when_absent(tmp_path):
    p = tmp_path / "bare.json"
    p.write_text(json.dumps({"routes": []}), encoding="utf-8")
    assert routes.load_config(p) == ([], {})


def test_duplicate_keys_rejected(tmp_path):
    r = {"key": "A", "origin": "KUL", "dest": "SIN", "trip_type": "oneway", "label": "x"}
    p = tmp_path / "dup.json"
    p.write_text(json.dumps({"routes": [r, r]}), encoding="utf-8")
    with pytest.raises(ValueError):
        routes.load_config(p)
