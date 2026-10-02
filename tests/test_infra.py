from __future__ import annotations

import base64
import json
import math
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_infra.auth import Signer
from kalshi_infra.ledger import fills_by_order, reconcile
from kalshi_infra.lock import AlreadyRunning, single_instance
from kalshi_infra.prereg import Candidate, Verdict, score
from kalshi_infra.risk import RiskController, RiskLimits
from kalshi_infra.sizing import contracts_for, read_stake_fraction, stake_for
from kalshi_infra.stats import block_ci, market_null_pvalue, stake_state, write_atomic


# ---------------------------------------------------------------- auth
@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signature_verifies_and_strips_query(key):
    s = Signer.from_key("kid", key)
    h = s.headers("get", "/trade-api/v2/portfolio/balance?x=1", now_ms=1700000000000)
    assert h["KALSHI-ACCESS-KEY"] == "kid"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_self_test_passes(key):
    Signer.from_key("kid", key).self_test()


def test_missing_key_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Signer("kid", tmp_path / "nope.pem")


# ---------------------------------------------------------------- lock
def test_lock_blocks_second_live_instance(tmp_path):
    p = tmp_path / "bot.pid"
    p.write_text(str(os.getppid()))          # a process that is certainly alive
    with pytest.raises(AlreadyRunning):
        with single_instance(p):
            pass


def test_lock_reclaims_stale_pid_and_cleans_up(tmp_path):
    p = tmp_path / "bot.pid"
    p.write_text("999999999")                # no such process
    with single_instance(p):
        assert p.read_text() == str(os.getpid())
    assert not p.exists()


# ---------------------------------------------------------------- risk
def _rc(tmp_path, **kw) -> RiskController:
    return RiskController(RiskLimits(live_enabled=True, kill_file=tmp_path / "KILL", **kw))


def _order(**kw):
    base = dict(count=10, signal_price_cents=50, current_price_cents=50,
                feed_age_s=0.1, seconds_to_expiry=60)
    base.update(kw)
    return base


def test_live_disabled_by_default(tmp_path):
    rc = RiskController(RiskLimits(kill_file=tmp_path / "KILL"))
    assert rc.check_pre_order(**_order()).reason == "live_disabled"


def test_clean_order_allowed(tmp_path):
    assert _rc(tmp_path).check_pre_order(**_order()).allow


def test_kill_file_latches(tmp_path):
    rc = _rc(tmp_path)
    (tmp_path / "KILL").touch()
    assert rc.check_pre_order(**_order()).reason == "kill_file_present"
    (tmp_path / "KILL").unlink()
    assert not rc.check_pre_order(**_order()).allow     # still killed until restart


@pytest.mark.parametrize("kw,reason", [
    (dict(count=0), "non_positive_count"),
    (dict(signal_price_cents=0), "signal_price_out_of_band"),
    (dict(current_price_cents=100), "current_price_out_of_band"),
    (dict(count=100, current_price_cents=50, signal_price_cents=50), "stake_over_cap"),
    (dict(current_price_cents=56), "slippage"),
    (dict(feed_age_s=2.5), "feed_stale"),
])
def test_gates_refuse_with_reason(tmp_path, kw, reason):
    d = _rc(tmp_path).check_pre_order(**_order(**kw))
    assert not d.allow and d.reason.startswith(reason)


def test_daily_loss_kill_and_midnight_rotation(tmp_path):
    rc = RiskController(RiskLimits(live_enabled=True, kill_file=tmp_path / "KILL",
                                   daily_loss_kill_usd=50), today=date(2026, 1, 1))
    rc.record_realized_pnl(-30, today=date(2026, 1, 1))
    rc.record_realized_pnl(-30, today=date(2026, 1, 2))   # new day: counter reset
    assert rc.check_pre_order(**_order()).allow
    rc.record_realized_pnl(-25, today=date(2026, 1, 2))
    assert rc.check_pre_order(**_order()).reason == "daily_loss_limit"


def test_single_latency_blip_does_not_block(tmp_path):
    rc = _rc(tmp_path)
    for _ in range(20):
        rc.record_latency_ms(100)
    rc.record_latency_ms(9000)
    assert rc.check_pre_order(**_order()).allow
    rc.record_latency_ms(9000)
    assert rc.check_pre_order(**_order()).reason.startswith("latency")


def test_latency_gate_dormant_below_min_samples(tmp_path):
    rc = _rc(tmp_path)
    for _ in range(5):
        rc.record_latency_ms(9000)
    assert rc.check_pre_order(**_order()).allow


# ---------------------------------------------------------------- sizing
def test_stake_clamps_and_funding_guard():
    assert stake_for(1000, divisor=20, floor_usd=1, cap_usd=200) == 50.0
    assert stake_for(100_000, divisor=20, floor_usd=1, cap_usd=200) == 200.0
    assert stake_for(1.09, divisor=20, floor_usd=1, cap_usd=200) == 0.0
    assert stake_for(27.50, divisor=20, floor_usd=25, cap_usd=200) == 25.0   # float trap


def test_drawdown_tier_needs_both_params():
    assert stake_for(300, divisor=10, floor_usd=1, cap_usd=999,
                     low_balance_threshold=300, low_divisor=30) == 10.0
    assert stake_for(300, divisor=10, floor_usd=1, cap_usd=999,
                     low_balance_threshold=300) == 30.0


def test_bad_config_raises():
    with pytest.raises(ValueError):
        stake_for(100, divisor=0, floor_usd=1, cap_usd=2)
    with pytest.raises(ValueError):
        stake_for(100, divisor=10, floor_usd=5, cap_usd=2)


def test_contracts_for():
    assert contracts_for(10, 0.40, max_contracts=100) == 25
    assert contracts_for(0.10, 0.40, max_contracts=100) == 1
    assert contracts_for(1000, 0.40, max_contracts=100) == 100


def _state(tmp_path, **kw) -> Path:
    st = {"cohort": "A", "fraction": 0.02, "computed_at": datetime.now(UTC).isoformat(),
          "reason": "ok"}
    st.update(kw)
    p = tmp_path / "stake.json"
    p.write_text(json.dumps(st))
    return p


def test_fresh_state_is_read(tmp_path):
    assert read_stake_fraction(_state(tmp_path), max_stale_hours=6, cohort="A") == (0.02, "ok")


@pytest.mark.parametrize("kw,why", [
    (dict(cohort="B"), "cohort"),
    (dict(fraction=True), "not a number"),
    (dict(fraction=1.5), "outside"),
    (dict(computed_at=(datetime.now(UTC) - timedelta(hours=7)).isoformat()), "stale"),
    (dict(computed_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()), "future"),
])
def test_state_fails_closed(tmp_path, kw, why):
    frac, reason = read_stake_fraction(_state(tmp_path, **kw), max_stale_hours=6, cohort="A")
    assert frac == 0.0 and why in reason


def test_missing_and_corrupt_state(tmp_path):
    assert read_stake_fraction(tmp_path / "x.json", max_stale_hours=6)[0] == 0.0
    (tmp_path / "bad.json").write_text("{trunc")
    assert read_stake_fraction(tmp_path / "bad.json", max_stale_hours=6)[0] == 0.0


# ---------------------------------------------------------------- stats
def _rows(days: int, per_day: int, pnl: float, price: float = 0.5) -> list[dict]:
    d0 = date(2026, 1, 1)
    return [{"day": (d0 + timedelta(days=i)).isoformat(), "price": price,
             "pnl_per_contract": pnl + (0.01 if j % 2 else -0.01)}
            for i in range(days) for j in range(per_day)]


def test_block_ci_undefined_on_few_days():
    lo, hi = block_ci(_rows(2, 20, 0.05))
    assert math.isnan(lo) and math.isnan(hi)


def test_block_ci_brackets_mean():
    lo, hi = block_ci(_rows(10, 10, 0.05), reps=500)
    assert lo <= 5.0 <= hi


def test_market_null():
    assert market_null_pvalue([0.5, 0.5], 0) == pytest.approx(1.0)
    assert market_null_pvalue([0.5, 0.5], 2) == pytest.approx(0.25)


def test_stake_state_sits_out_until_evidence():
    assert stake_state(_rows(5, 20, 0.05), cohort="A", reps=200)["fraction"] == 0.0
    assert stake_state(_rows(10, 20, -0.05), cohort="A", reps=200)["reason"] == "ci_lo <= 0"


def test_stake_state_sizes_off_lower_bound_and_caps():
    st = stake_state(_rows(10, 20, 0.05), cohort="A", reps=500)
    assert st["fraction"] > 0
    assert st["kelly_full"] == pytest.approx((st["ci_lo"] / 100) / (1 - st["mean_price"]), rel=1e-6)
    big = stake_state(_rows(10, 20, 0.40), cohort="A", reps=500, kelly_fraction=1.0)
    assert big["capped"] and big["fraction"] == 0.10


def test_write_atomic_roundtrip(tmp_path):
    write_atomic(tmp_path / "s" / "state.json", {"a": 1})
    assert json.loads((tmp_path / "s" / "state.json").read_text()) == {"a": 1}


# ---------------------------------------------------------------- prereg
def _cand(**kw):
    base = dict(name="C", hypothesis="h", mechanism="m", falsifier="f",
                scored_from="2026-01-01", min_n=60, min_days=8)
    base.update(kw)
    return Candidate(**base)


def test_discovery_rows_never_count():
    s = score(_cand(scored_from="2026-02-01"), _rows(20, 10, 0.05))
    assert s.verdict is Verdict.UNDECIDED and s.n == 0


def test_pass_and_fail():
    assert score(_cand(), _rows(10, 10, 0.05), reps=500).verdict is Verdict.PASS
    assert score(_cand(), _rows(10, 10, -0.05), reps=500).verdict is Verdict.FAIL


# ---------------------------------------------------------------- ledger
def test_partial_fills_aggregate():
    fills = [
        {"order_id": "o1", "count": 3, "side": "yes", "yes_price_dollars": "0.40", "fee_cost": "0.02"},
        {"order_id": "o1", "count": 1, "side": "yes", "yes_price_dollars": "0.44", "fee_cost": "0.01"},
        {"order_id": "o2", "count": 0, "side": "no", "no_price_dollars": "0.60"},
    ]
    out = fills_by_order(fills)
    assert set(out) == {"o1"}
    assert out["o1"].contracts == 4 and out["o1"].avg_price == Decimal("0.41")
    assert out["o1"].fees == Decimal("0.03")


def test_reconcile_exact():
    r = reconcile(deposits=[Decimal("100")], withdrawals=[], realized_pnl=[Decimal("5.25")],
                  open_cost=Decimal("10"), actual_balance=Decimal("95.25"))
    assert r.ok
    r2 = reconcile(deposits=[Decimal("100")], withdrawals=[], realized_pnl=[],
                   open_cost=Decimal("0"), actual_balance=Decimal("99.99"))
    assert not r2.ok and r2.difference == Decimal("-0.01")
