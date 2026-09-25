from catalystedge.connector_status import MANUAL_ONLY, connector_state


def test_states():
    assert connector_state("finnhub_news", "ok", "ok", True, None) == "ACTIVE"
    assert connector_state("sam_gov", "disabled", "disabled", True, "auth required: set SAM_GOV_API_KEY") == \
        "AUTH REQUIRED"
    assert connector_state("tiingo_eod", "failed", "failed", True, "hourly budget of 45 calls used") == "RATE LIMITED"
    assert connector_state("usaspending", "failed", "failed", True, "network error: ConnectError") == \
        "TEMPORARILY UNAVAILABLE"
    assert connector_state("tiingo_news", "disabled", "disabled", False, None) == "OFF"
    assert connector_state("investing_rss", "disabled", "disabled", False, None) == "MANUAL ONLY"
    assert connector_state("benzinga_news", "disabled", "disabled", False, None) == "AUTH REQUIRED"
    assert any(m["name"] == "Reuters" for m in MANUAL_ONLY)
