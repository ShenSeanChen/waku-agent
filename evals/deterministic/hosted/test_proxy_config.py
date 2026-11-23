"""DETERMINISTIC EVAL -- the metering proxy's prices and settings (spec 004 C1).

The price table decides when a person's $1 is spent, so every free model has a
price and a model without one is refused at startup rather than charged as zero.
The settings are pinned in both directions to proxy.env.example and to the
heredoc install.sh writes, so a misspelt name fails here, not on the VM.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from hosted.proxy import prices
from hosted.proxy.config import REQUIRED_ENV_NAMES, config_from_env

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "hosted" / "deploy" / "proxy.env.example"


def _example_env() -> dict[str, str]:
    env = {}
    for line in EXAMPLE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            env[name] = value
    return env


def test_the_example_names_exactly_the_required_settings():
    assert set(_example_env()) == set(REQUIRED_ENV_NAMES)


def test_install_writes_exactly_the_required_settings():
    script = ("set -eu; . hosted/deploy/envfiles.sh; root=/srv/waku; "
              "platform_key=k; free_model=claude-sonnet-5-5; "
              "free_small_model=claude-haiku-4-5; waku_proxy_env")
    out = subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    names = [line.split("=", 1)[0] for line in out.splitlines() if "=" in line]
    assert names == list(REQUIRED_ENV_NAMES)
    assert "WAKU_FREE_MODELS=claude-sonnet-5-5,claude-haiku-4-5" in out


def test_the_example_parses_and_names_sonnet_and_haiku():
    config = config_from_env(_example_env())
    assert config.free_models == ("claude-sonnet-5-5", "claude-haiku-4-5")
    assert config.monthly_cap_usd == 1.0
    assert config.max_tokens_ceiling == 8192


def test_a_missing_setting_is_refused():
    env = _example_env()
    del env["WAKU_FREE_MONTHLY_CAP_USD"]
    with pytest.raises(ValueError, match="WAKU_FREE_MONTHLY_CAP_USD"):
        config_from_env(env)


def test_a_free_model_without_a_price_is_refused_at_startup():
    env = _example_env() | {"WAKU_FREE_MODELS": "claude-sonnet-5-5,claude-unpriced-9"}
    with pytest.raises(ValueError, match="claude-unpriced-9"):
        config_from_env(env)


def test_prices_are_the_published_rates():
    """claude-api skill table, cached 2026-09-25: $/M tokens."""
    assert prices.PRICES["claude-sonnet-5-5"] == prices.Price(2.00, 10.00, 2.50, 0.20)
    assert prices.PRICES["claude-haiku-4-5"] == prices.Price(1.00, 5.00, 1.25, 0.10)


def test_a_calls_cost_prices_all_four_usage_fields():
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000,
             "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert prices.cost("claude-sonnet-5-5", usage) == pytest.approx(2.0 + 1.0 + 2.5 + 0.2)


def test_a_missing_usage_field_counts_as_zero():
    assert prices.cost("claude-haiku-4-5", {"output_tokens": 1_000_000}) == pytest.approx(5.0)


def test_the_worst_case_is_ten_percent_more_input_and_every_output_token():
    assert prices.worst_case("claude-sonnet-5-5", input_tokens=1_000_000,
                             max_tokens=100_000) == pytest.approx(1.1 * 2.0 + 1.0)


def test_no_price_is_ever_written_as_a_literal_outside_the_table():
    """One table, so a price change is one edit."""
    source = (ROOT / "hosted" / "proxy" / "app.py").read_text() if (ROOT / "hosted" / "proxy" / "app.py").exists() else ""
    assert not re.search(r"\b10\.00\b|\b2\.00\b", source)


# --- spec 004 E: treg ------------------------------------------------------------


def test_the_example_and_a_fresh_install_leave_the_treg_relay_off():
    config = config_from_env(_example_env())
    assert config.treg_token == "" and config.treg_max_call_usd == 0.5


def test_a_proxy_env_written_before_treg_still_starts():
    """upgrade.sh never writes config/, so a deployment installed before spec
    004 E has neither line. A proxy that refused that file would take the free
    tier down on the upgrade that added treg."""
    env = _example_env()
    del env["WAKU_TREG_TOKEN"], env["WAKU_TREG_MAX_CALL_USD"]
    config = config_from_env(env)
    assert config.treg_token == "" and config.treg_max_call_usd == 0.5


def test_a_treg_token_and_ceiling_are_read():
    config = config_from_env(_example_env() | {"WAKU_TREG_TOKEN": "treg_org_x",
                                               "WAKU_TREG_MAX_CALL_USD": "0.25"})
    assert config.treg_token == "treg_org_x" and config.treg_max_call_usd == 0.25
    assert "treg_org_x" not in repr(config)


@pytest.mark.parametrize("ceiling", ["0", "-1", "nan", "inf", "11", "fifty cents"])
def test_a_ceiling_that_is_not_a_price_is_refused_at_startup(ceiling):
    with pytest.raises(ValueError, match="WAKU_TREG_MAX_CALL_USD"):
        config_from_env(_example_env() | {"WAKU_TREG_MAX_CALL_USD": ceiling})
