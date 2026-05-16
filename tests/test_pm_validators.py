"""Phase 6 — regression tests for the extracted numerical-integrity validators.

Written BEFORE the implementation (TDD) so that:
  - compute_peg_safely uses the correct formula (P/E divided by growth as
    decimal, not by growth as percentage integer)
  - validate_no_invented_digits blocks any digit-bearing token not in the
    allowed-displays set (the harsh-mode gate)
  - check_forbidden_soundbites rejects Cramer / "the street" / etc.
  - percentile_rank_in_peers ranks correctly (0.0 = cheapest, 1.0 = priciest)

These tests are the ground truth — if the validator ever drifts (the way the
bot's in-prose PEG drifted), CI catches it.
"""
from __future__ import annotations

import pytest

from agents.pm_validators import (
    check_forbidden_soundbites,
    compute_peg_safely,
    percentile_rank_in_peers,
    validate_no_invented_digits,
)


# ── compute_peg_safely ────────────────────────────────────────────────────────

class TestComputePeg:
    def test_correct_formula_nvda_example(self):
        # NVDA real numbers per Claude Chat audit: forward P/E 27.4, growth 24.9%
        # → PEG = 27.4 / 24.9 = 1.10 (NOT 27.4 / 0.249 = 110, NOT 27.4 / 24.9% = 0.011)
        peg = compute_peg_safely(forward_pe=27.4, growth_decimal=0.249)
        assert peg == pytest.approx(1.10, rel=0.02)

    def test_correct_formula_tsm_example(self):
        # TSM: forward P/E 25.3, growth 25%
        peg = compute_peg_safely(forward_pe=25.3, growth_decimal=0.25)
        assert peg == pytest.approx(1.01, rel=0.02)

    def test_none_growth_returns_none(self):
        assert compute_peg_safely(forward_pe=27.4, growth_decimal=None) is None

    def test_zero_growth_returns_none(self):
        assert compute_peg_safely(forward_pe=27.4, growth_decimal=0.0) is None

    def test_negative_growth_returns_none(self):
        # No PEG when company is shrinking
        assert compute_peg_safely(forward_pe=27.4, growth_decimal=-0.05) is None

    def test_none_pe_returns_none(self):
        assert compute_peg_safely(forward_pe=None, growth_decimal=0.25) is None


# ── validate_no_invented_digits ───────────────────────────────────────────────

class TestValidateNoInventedDigits:
    def test_blocks_invented_forward_pe(self):
        # The exact NVDA failure: bot writes "19.8" when snapshot has "27.4x"
        allowed = {"$235.74", "27.4x", "1.10", "25%"}
        err = validate_no_invented_digits(
            "Forward P/E de 19.8 amb PEG 0.27", allowed,
        )
        assert err is not None
        assert "19.8" in err or "0.27" in err

    def test_blocks_invented_peg(self):
        allowed = {"27.4x", "1.10"}
        err = validate_no_invented_digits("PEG és 0.27 — molt baix", allowed)
        assert err is not None
        assert "0.27" in err

    def test_allows_quoted_numbers(self):
        allowed = {"$235.74", "27.4x", "1.10", "25%"}
        err = validate_no_invented_digits(
            "Forward P/E de 27.4x és just per 25% growth, PEG 1.10",
            allowed,
        )
        assert err is None, f"unexpected error: {err}"

    def test_exempts_year_tokens(self):
        # Years like 2026, 2025 are common and not "ratios"
        assert validate_no_invented_digits(
            "Al 2026 esperem que el Q1 mostri creixement",
            set(),
        ) is None

    def test_exempts_quarter_tokens(self):
        assert validate_no_invented_digits(
            "Al Q1 2026, els resultats van superar el Q4 anterior",
            set(),
        ) is None

    def test_exempts_structural_tokens(self):
        # SEC filing types and process nodes are structural, not ratios
        assert validate_no_invented_digits(
            "El 8-K (i el 10-Q) mostren el procés a 3nm",
            set(),
        ) is None

    def test_empty_text_no_error(self):
        assert validate_no_invented_digits("", set()) is None
        assert validate_no_invented_digits(None, set()) is None

    def test_dollar_amount_blocked_when_not_in_allowed(self):
        # $31.28B board approval — needs to be in allowed (e.g. from snapshot)
        # or it's fabrication
        err = validate_no_invented_digits(
            "El board va aprovar $31.28B en buybacks",
            allowed_displays={"$5700B", "55.6%"},
        )
        assert err is not None
        assert "31.28" in err or "$31.28B" in err

    def test_percentage_blocked_when_not_in_allowed(self):
        allowed = {"55.6%"}
        err = validate_no_invented_digits(
            "Customer concentration is 73%", allowed,
        )
        assert err is not None
        assert "73" in err


# ── check_forbidden_soundbites ────────────────────────────────────────────────

class TestForbiddenSoundbites:
    @pytest.mark.parametrize("text", [
        "Cramer diu que NVDA puja",
        "Jim Cramer és bullish sobre l'AI",
        "Per The Street, NVDA és comprar",
        "the street loves this stock",
        "smart money is accumulating",
        "Analistes unànimes en la recomanació de compra",
        "El consens analista és unànime",
        "Everyone agrees this is a winner",
        "Tothom creu que pujarà",
        "Buy the dip on the next 5% drop",
        "NVDA to the moon",
    ])
    def test_blocks_known_soundbites(self, text):
        err = check_forbidden_soundbites(text)
        assert err is not None, f"failed to block: {text!r}"

    @pytest.mark.parametrize("text", [
        "El balanç és sòlid",
        "Forward P/E de 27.4x és just",
        "Marge brut 75%; ROIC 22%",
        "Beat Q1 by 6%, guidance raised",
        "Concentració de clients hyperscaler 50-60% revenue",
    ])
    def test_allows_clean_prose(self, text):
        assert check_forbidden_soundbites(text) is None


# ── percentile_rank_in_peers ──────────────────────────────────────────────────

class TestPercentileRank:
    @pytest.fixture
    def peers(self):
        # Lower forward_pe = cheaper.
        return [
            {"ticker": "A", "forward_pe": 10.0},
            {"ticker": "B", "forward_pe": 20.0},
            {"ticker": "C", "forward_pe": 30.0},
            {"ticker": "D", "forward_pe": 40.0},
        ]

    def test_cheapest_ranks_zero(self, peers):
        # A has the lowest P/E → 0th percentile (cheapest)
        assert percentile_rank_in_peers(peers, "forward_pe", "A") == pytest.approx(0.0)

    def test_priciest_ranks_high(self, peers):
        # D has the highest P/E → 75th percentile (3 of 4 ranks below it)
        assert percentile_rank_in_peers(peers, "forward_pe", "D") == pytest.approx(0.75)

    def test_middle_ranks_middle(self, peers):
        # B is at the 25th percentile, C at 50th
        assert percentile_rank_in_peers(peers, "forward_pe", "B") == pytest.approx(0.25)
        assert percentile_rank_in_peers(peers, "forward_pe", "C") == pytest.approx(0.50)

    def test_unknown_ticker_returns_neg_one(self, peers):
        assert percentile_rank_in_peers(peers, "forward_pe", "ZZZ") == -1.0

    def test_metric_with_none_skipped(self):
        peers = [
            {"ticker": "A", "forward_pe": 10.0},
            {"ticker": "B", "forward_pe": None},
            {"ticker": "C", "forward_pe": 30.0},
        ]
        # B is skipped; A and C considered. A is the cheaper → 0.0
        assert percentile_rank_in_peers(peers, "forward_pe", "A") == pytest.approx(0.0)
        assert percentile_rank_in_peers(peers, "forward_pe", "C") == pytest.approx(0.5)

    def test_works_with_nested_display_dict(self):
        # Real peer_snapshot shape: {"forward_pe": {"value": 27.4, "display": "27.4x"}}
        peers = [
            {"ticker": "A", "forward_pe": {"value": 10.0, "display": "10.0x"}},
            {"ticker": "B", "forward_pe": {"value": 30.0, "display": "30.0x"}},
        ]
        assert percentile_rank_in_peers(peers, "forward_pe", "A") == pytest.approx(0.0)
        assert percentile_rank_in_peers(peers, "forward_pe", "B") == pytest.approx(0.5)
