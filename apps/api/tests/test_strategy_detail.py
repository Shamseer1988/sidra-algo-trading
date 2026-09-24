"""One strategy, described — and in particular, judged honestly.

The assessment is the part of this feature that can do harm. A screen that says
a strategy works, on evidence that does not support it, is how somebody talks
themselves into going live. So the rules are asserted here rather than left to
the prose:

  nothing is called good on backtest evidence alone
  an in-sample backtest is never treated as out-of-sample
  a losing forward result is reported on far less evidence than a winning one
  the strongest available verdict is "promising, not proven"

The last one is enforced by a test that walks every reachable verdict and
asserts none of them contains the word "profitable".
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.models import BacktestRun, BacktestSweep, BacktestTrade, PaperPosition, PaperSignal, User
from app.db.session import SessionLocal
from app.services import strategy_detail as detail
from app.services.exit_rules import ATR_MULTIPLE, ATR_TRAIL, BREAKEVEN_AT_R, ExitRules
from app.services.strategy_detail import (
    INCONCLUSIVE,
    MIN_TRADES,
    NEGATIVE,
    NOT_ENOUGH_EVIDENCE,
    PROMISING,
    Evidence,
    assess,
    describe_exit,
)


def evidence(source, trades, net, *, out_of_sample=True, wins=None):
    item = Evidence(source=source, out_of_sample=out_of_sample)
    item.trades = trades
    item.wins = trades if wins is None else wins
    item.losses = 0 if wins is None else trades - wins
    item.net_pnl = Decimal(str(net))
    return item


# --- the verdict ---------------------------------------------------------


def test_nothing_is_promising_on_backtest_alone():
    result = assess(evidence("BACKTEST", 200, 50_000), evidence("PAPER_FORWARD", 0, 0))
    assert result.verdict == NOT_ENOUGH_EVIDENCE
    assert f"{MIN_TRADES}" in result.headline


def test_nothing_is_promising_on_forward_alone():
    result = assess(evidence("BACKTEST", 0, 0), evidence("PAPER_FORWARD", 200, 9_000))
    assert result.verdict == NOT_ENOUGH_EVIDENCE


def test_an_in_sample_backtest_is_not_out_of_sample():
    result = assess(
        evidence("BACKTEST", 200, 50_000, out_of_sample=False),
        evidence("PAPER_FORWARD", 60, 4_000),
    )
    assert result.verdict == INCONCLUSIVE
    assert any("in-sample" in caveat for caveat in result.caveats)


def test_a_losing_forward_result_is_reported_immediately():
    # Asymmetric on purpose: being slow to believe good news costs a delayed
    # start; being slow to believe bad news costs money.
    result = assess(evidence("BACKTEST", 500, 90_000), evidence("PAPER_FORWARD", 4, -800))
    assert result.verdict == NEGATIVE
    assert "-800" in result.headline


def test_both_bodies_of_evidence_and_both_positive_is_the_best_available():
    result = assess(evidence("BACKTEST", 60, 20_000), evidence("PAPER_FORWARD", 40, 3_000))
    assert result.verdict == PROMISING
    assert "not proven" in result.headline


def test_no_verdict_ever_claims_a_strategy_is_profitable():
    cases = [
        assess(evidence("BACKTEST", 0, 0), evidence("PAPER_FORWARD", 0, 0)),
        assess(evidence("BACKTEST", 200, 50_000, out_of_sample=False), evidence("PAPER_FORWARD", 60, 4_000)),
        assess(evidence("BACKTEST", 500, 90_000), evidence("PAPER_FORWARD", 4, -800)),
        assess(evidence("BACKTEST", 60, 20_000), evidence("PAPER_FORWARD", 40, 3_000)),
    ]
    for case in cases:
        text = " ".join([case.headline, *case.caveats, detail.VERDICT_LABELS[case.verdict]]).lower()
        assert "profitable" not in text
        assert "proven" not in text or "not proven" in text


def test_a_sufficient_sample_still_carries_a_warning_about_its_width():
    result = assess(evidence("BACKTEST", 60, 20_000), evidence("PAPER_FORWARD", 40, 3_000))
    assert any("noise" in caveat for caveat in result.caveats)


def test_the_shortfall_says_how_many_more_trades_are_needed():
    result = assess(evidence("BACKTEST", 10, 500), evidence("PAPER_FORWARD", 0, 0))
    assert any(f"{MIN_TRADES - 10} more" in caveat for caveat in result.caveats)
    assert any(f"{MIN_TRADES} more" in caveat for caveat in result.caveats)


# --- the exit plan, in words ---------------------------------------------


def test_the_exit_plan_resolves_inherited_numbers_and_says_where_they_came_from():
    lines = describe_exit(ExitRules(), minimum_rr=1.5, account_atr=1.5, account_percent=0.4)
    stop = lines[0]
    assert "1.5× ATR (the account's multiple)" in stop
    assert "0.4% of the entry price (the account's floor)" in stop


def test_the_exit_plan_marks_a_strategys_own_override():
    lines = describe_exit(ExitRules(stop_atr_multiple=3.0), minimum_rr=1.5, account_atr=1.5, account_percent=0.4)
    assert "3.0× ATR (its own multiple)" in lines[0]


def test_the_exit_plan_says_plainly_when_nothing_closes_on_the_clock():
    lines = describe_exit(ExitRules(), minimum_rr=1.5, account_atr=1.5, account_percent=0.4)
    assert any("Nothing closes it because the session is ending" in line for line in lines)


def test_the_exit_plan_describes_a_square_off():
    lines = describe_exit(
        ExitRules(square_off_time="15:15", time_exit_minutes=45),
        minimum_rr=1.5,
        account_atr=1.5,
        account_percent=0.4,
    )
    assert any("after 45 minutes or at 15:15 IST" in line for line in lines)


def test_breakeven_is_described_as_price_not_money():
    lines = describe_exit(ExitRules(trailing_rule=BREAKEVEN_AT_R), minimum_rr=1.5, account_atr=1.5, account_percent=0.4)
    assert any("not on money" in line for line in lines)


def test_an_atr_trail_says_it_never_moves_backwards():
    lines = describe_exit(ExitRules(trailing_rule=ATR_TRAIL), minimum_rr=1.5, account_atr=1.5, account_percent=0.4)
    assert any("never moves" in line for line in lines)


def test_an_atr_target_names_its_fallback():
    lines = describe_exit(ExitRules(target_rule=ATR_MULTIPLE), minimum_rr=2.0, account_atr=1.5, account_percent=0.4)
    assert any("falling back" in line and "2.0:1" in line for line in lines)


def test_every_strategy_type_has_a_written_profile():
    from app.services.strategy_registry import StrategyRegistry

    for metadata in StrategyRegistry.metadata():
        assert metadata.identifier in detail.PROFILES, metadata.identifier


# --- evidence from the database ------------------------------------------

VERSION = "orb-retest-v1@7"
KEY = "test-strategy-detail"


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(BacktestTrade).where(BacktestTrade.trade_key.like(f"{KEY}%")))
        await session.execute(delete(BacktestSweep).where(BacktestSweep.strategy_id == "orb-retest-v1"))
        await session.execute(delete(BacktestRun).where(BacktestRun.data_fingerprint == KEY))
        await session.execute(delete(PaperSignal).where(PaperSignal.signal_key.like(f"{KEY}%")))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def a_run() -> BacktestRun:
    async with SessionLocal() as session:
        # The FK is RESTRICT, so the run needs a real owner. Any user will do;
        # the instance may have none, so one is created for the test.
        owner = (await session.scalars(select(User).limit(1))).first()
        if owner is None:
            owner = User(email=f"backtest-owner-{uuid4().hex}@test.invalid", password_hash="x")
            session.add(owner)
            await session.flush()
        run = BacktestRun(
            created_by_user_id=owner.id,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            timeframe_seconds=60,
            data_fingerprint=KEY,
            initial_capital=Decimal("10000"),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def a_forward_trade(net: str, suffix: str) -> None:
    async with SessionLocal() as session:
        signal = PaperSignal(
            signal_key=f"{KEY}-{suffix}",
            instrument_token="NSE:2885",
            session_date=date(2026, 9, 1),
            candle_opened_at=datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
            strategy_version=VERSION,
            side="LONG",
            entry_price=Decimal("100"),
            stop_price=Decimal("98"),
            target_price=Decimal("104"),
            quantity=10,
            risk_amount=Decimal("20"),
            score=90,
        )
        session.add(signal)
        await session.flush()
        session.add(
            PaperPosition(
                paper_signal_id=signal.id,
                instrument_token="NSE:2885",
                session_date=date(2026, 9, 1),
                strategy_version=VERSION,
                side="LONG",
                status="CLOSED",
                initial_quantity=10,
                open_quantity=0,
                stop_price=Decimal("98"),
                target_price=Decimal("104"),
                realized_pnl=Decimal(net),
                fees_total=Decimal("0"),
                total_pnl=Decimal(net),
                opened_at=datetime(2026, 9, 1, 4, 5, tzinfo=UTC),
                closed_at=datetime(2026, 9, 1, 6, 0, tzinfo=UTC),
            )
        )
        await session.commit()


async def test_forward_evidence_counts_only_closed_positions():
    await a_forward_trade("500", "a")
    await a_forward_trade("-200", "b")
    async with SessionLocal() as session:
        result = await detail.forward_evidence(session, VERSION)
    assert result.trades == 2
    assert result.wins == 1 and result.losses == 1
    assert result.net_pnl == Decimal("300")


async def test_forward_evidence_is_keyed_on_the_version_not_the_type():
    # A strategy whose parameters changed is, for this question, a different
    # strategy. Pooling the results would average two things nobody is running.
    await a_forward_trade("500", "a")
    async with SessionLocal() as session:
        assert (await detail.forward_evidence(session, "orb-retest-v1@99")).trades == 0


async def test_a_backtest_without_a_holdout_sweep_is_not_out_of_sample():
    run = await a_run()
    async with SessionLocal() as session:
        session.add(
            BacktestTrade(
                run_id=run.id,
                trade_key=f"{KEY}-1",
                strategy_id="orb-retest-v1",
                strategy_name="ORB",
                strategy_version=7,
                instrument_token="NSE:2885",
                session_date=date(2026, 8, 3),
                side="LONG",
                quantity=10,
                signal_at=datetime(2026, 8, 3, 4, 0, tzinfo=UTC),
                entered_at=datetime(2026, 8, 3, 4, 1, tzinfo=UTC),
                exited_at=datetime(2026, 8, 3, 5, 0, tzinfo=UTC),
                entry_price=Decimal("100"),
                exit_price=Decimal("104"),
                gross_pnl=Decimal("40"),
                fees_total=Decimal("5"),
                net_pnl=Decimal("35"),
                realized_r=Decimal("1.75"),
                exit_reason="TARGET",
            )
        )
        await session.commit()
    async with SessionLocal() as session:
        result = await detail.backtest_evidence(session, "orb-retest-v1")
    assert result.trades == 1
    assert result.out_of_sample is False
    assert result.net_pnl == Decimal("35")


async def test_a_completed_holdout_sweep_makes_the_backtest_out_of_sample():
    async with SessionLocal() as session:
        session.add(
            BacktestSweep(
                strategy_id="orb-retest-v1",
                status="COMPLETED",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
                validation_fraction=Decimal("0.35"),
            )
        )
        await session.commit()
    async with SessionLocal() as session:
        assert (await detail.backtest_evidence(session, "orb-retest-v1")).out_of_sample is True


async def test_signal_activity_reports_silence_distinctly_from_being_off():
    async with SessionLocal() as session:
        count, last = await detail.signal_activity(session, "orb-retest-v1@never-signalled")
    assert count == 0 and last is None
