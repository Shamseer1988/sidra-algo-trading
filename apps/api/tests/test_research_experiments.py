"""Research Lab Phase 5: pre-registered experiments, honest statistics, one look at the holdout.

The claims:

* an experiment that could not be a fair test is refused before it is frozen;
* registering freezes it and queues selection; it cannot be registered twice;
* selection never reads a bar from the holdout period;
* the holdout is scored once, for the chosen parameters, against the
  threshold written down before -- and a second look is refused;
* a crashed holdout puts the experiment back without spending the holdout;
* the statistics penalise choosing the best of many;
* a stop hit inside the entry candle now counts.
"""

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.models import (
    ResearchDataset,
    ResearchExperiment,
    ResearchExperimentResult,
    ResearchHypothesis,
    ResearchJob,
    ResearchJobEvent,
)
from app.db.session import SessionLocal
from app.services import research_experiments as rx
from app.services import research_statistics as stats
from app.services.strategy_registry import StrategyConfiguration
from tests.test_backtest_sweep import DAYS, _controls, _history, _strategy

# --- statistics ---------------------------------------------------------------------------------


def test_summary_and_interval():
    values = [1.0, -1.0, 2.0, -1.0, 1.5, -1.0, 2.0, -1.0, 1.0, -0.5, 2.0, -1.0]
    out = stats.summary(values)
    assert out["trades"] == 12 and out["win_rate"] == 0.5
    assert out["ci95_mean_r"][0] < out["mean_r"] < out["ci95_mean_r"][1]
    assert stats.summary([])["mean_r"] is None


def test_choosing_the_best_of_more_variants_needs_a_higher_sharpe():
    assert stats.expected_max_sharpe(1, 0.04) == 0
    thresholds = [stats.expected_max_sharpe(n, 0.04) for n in (2, 10, 40, 200)]
    assert thresholds == sorted(thresholds) and thresholds[0] > 0


def test_the_deflated_sharpe_falls_as_trials_rise():
    edge = [0.6, -1, 2, -1, 2, 0.4, -1, 2, 2, -1, 1, 0.5, -1, 2, 1.5, -1, 2, 0.3, -1, 2] * 3
    one = stats.deflated_sharpe(edge, trials=1)["dsr"]
    forty = stats.deflated_sharpe(edge, trials=40)["dsr"]
    assert one > forty
    noise = [1, -1] * 30
    assert stats.deflated_sharpe(noise, trials=40)["dsr"] < 0.5


def test_cost_sensitivity_and_regimes():
    trades = [
        SimpleNamespace(
            gross_pnl=Decimal("30"),
            fees_total=Decimal("10"),
            risk_amount=Decimal("100"),
            realized_r=Decimal("0.2"),
            session_date="2026-01-05",
        ),
        SimpleNamespace(
            gross_pnl=Decimal("-90"),
            fees_total=Decimal("10"),
            risk_amount=Decimal("100"),
            realized_r=Decimal("-1"),
            session_date="2026-01-06",
        ),
    ]
    assert stats.cost_sensitivity(trades) == {"x1": -0.4, "x1_5": -0.45, "x2": -0.5}
    regimes = stats.regime_split(trades, {date(2026, 1, 5): "UP", date(2026, 1, 6): "DOWN"})
    assert regimes["UP"]["mean_r"] == 0.2 and regimes["DOWN"]["mean_r"] == -1.0


# --- the entry candle ---------------------------------------------------------------------------


def test_a_stop_inside_the_entry_candle_counts():
    from app.services.backtesting import _exit_trade
    from app.services.exit_rules import ExitRules
    from tests.test_backtest_correctness import NO_COSTS, _bar

    decision = SimpleNamespace(
        side="LONG", entry_price=Decimal("100"), stop_price=Decimal("99"), target_price=Decimal("110")
    )
    candles = [_bar("10:00", "100", "100.5", "98.8", "99.5"), _bar("10:01", "99.5", "112", "99.4", "111")]
    price, exited_at, reason = _exit_trade(
        decision, candles, 0, NO_COSTS, entry_price=Decimal("100"), rules=ExitRules()
    )
    assert (reason, price, exited_at) == ("STOP", Decimal("99.0000"), candles[0].closed_at)  # not the later target


# --- experiments --------------------------------------------------------------------------------


@pytest.fixture
async def world():
    """A hypothesis and a COMPLETE dataset row covering the sweep fixture's four days."""

    async def wipe():
        async with SessionLocal() as session:
            await session.execute(delete(ResearchExperimentResult))
            await session.execute(delete(ResearchExperiment))
            await session.execute(delete(ResearchHypothesis))
            await session.execute(delete(ResearchJobEvent))
            await session.execute(delete(ResearchJob))
            await session.execute(delete(ResearchDataset).where(ResearchDataset.name.like("exp-test%")))
            await session.commit()

    await wipe()
    async with SessionLocal() as session:
        hypothesis = await rx.create_hypothesis(
            session, title="RS holds up", statement="RS Pullback has an edge.", rationale=None, user_id=None
        )
        dataset = ResearchDataset(
            name="exp-test",
            source="test",
            timeframe="1minute",
            price_adjustment="RAW",
            benchmark_key="NSE:26000",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            status="COMPLETE",
            storage_path="exp-test",
            content_sha256="f" * 64,
            provenance={},
        )
        session.add(dataset)
        await session.commit()
        ids = SimpleNamespace(hypothesis=hypothesis.id, dataset=dataset.id)
    yield ids
    await wipe()


def _fields(ids, **over):
    base = {
        "hypothesis_id": ids.hypothesis,
        "name": f"exp-{datetime.now().timestamp()}",
        "dataset_id": ids.dataset,
        "instruments": ["NSE:TEST"],
        "strategy": _strategy().model_dump(mode="json"),
        "controls": {**_controls(), "account_capital": 100000},
        "execution": {
            "slippage_bps": 0,
            "brokerage_percent": 0,
            "stt_sell_percent": 0,
            "exchange_transaction_percent": 0,
            "gst_percent": 0,
            "sebi_percent": 0,
            "stamp_duty_buy_percent": 0,
            "ipft_percent": 0,
        },
        "parameter_grid": {"minimum_rr": [1.0, 2.0]},
        "selection_start": DAYS[0],
        "selection_end": DAYS[2],
        "holdout_start": DAYS[3],
        "holdout_end": DAYS[3],
        "train_sessions": 5,
        "test_sessions": 1,
        "success_mean_r": Decimal("0.1"),
    }
    return {**base, **over}


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"holdout_start": DAYS[1]}, "holdout must start after"),
        ({"selection_start": date(2025, 12, 1)}, "inside the dataset"),
        ({"instruments": []}, "at least one instrument"),
        ({"parameter_grid": {"not_a_param": [1]}}, "Not sweepable"),
        ({"train_sessions": 2}, "at least 5 training"),
    ],
)
async def test_an_unfair_experiment_is_refused_before_it_is_frozen(world, override, message):
    async with SessionLocal() as session:
        with pytest.raises(rx.ExperimentError, match=message):
            await rx.create_experiment(session, **_fields(world, **override))


async def test_registering_freezes_and_queues_once(world):
    async with SessionLocal() as session:
        experiment = await rx.create_experiment(session, **_fields(world))
        assert experiment.status == rx.DRAFT and experiment.variants == 2
        await rx.register(session, experiment.id, user_id=None)
        await session.commit()
        with pytest.raises(rx.ExperimentError, match="Only a DRAFT"):
            await rx.register(session, experiment.id, user_id=None)
        jobs = (await session.scalars(select(ResearchJob))).all()
    assert [job.job_type for job in jobs] == [rx.SELECTION_JOB]
    assert jobs[0].payload == {"experiment_id": str(experiment.id)}


def _fake_reader(calls):
    by_instrument, benchmark = _history()

    async def read(session, dataset_id, instruments, start, end, root=None):
        calls.append((start, end))
        keep = lambda rows: [c for c in rows if start <= c.session_date <= end]  # noqa: E731
        dataset = await session.get(ResearchDataset, dataset_id)
        return dataset, {k: keep(v) for k, v in by_instrument.items()}, keep(benchmark)

    return read


async def _effective(_session, settings):
    return SimpleNamespace(
        opening_range_minutes=15,
        ema_fast_period=9,
        ema_slow_period=21,
        volume_lookback_candles=20,
        atr_period=14,
        daily_history_sessions=40,
        rvol_baseline_sessions=10,
    )


async def test_selection_never_reads_the_holdout_then_the_holdout_is_scored_once(world, monkeypatch):
    calls = []
    monkeypatch.setattr(rx, "read_dataset", _fake_reader(calls))
    monkeypatch.setattr("app.services.indicator_settings.effective", _effective)
    async with SessionLocal() as session:
        experiment = await rx.create_experiment(session, **_fields(world))
        await rx.register(session, experiment.id, user_id=None)
        await session.commit()
        experiment_id = experiment.id

    async with SessionLocal() as session:
        result = await rx.selection_job(session, {"experiment_id": str(experiment_id)}, SimpleNamespace(), None)
        await session.commit()
    assert result["status"] == rx.SELECTED
    assert all(end <= DAYS[2] for _, end in calls), "selection asked for bars past the selection period"

    async with SessionLocal() as session:
        experiment = await session.get(ResearchExperiment, experiment_id)
        assert experiment.status == rx.SELECTED and experiment.chosen_parameters is not None
        selection = (await rx.results_of(session, experiment_id))[0]
        walk = selection.summary["walk_forward"]
        assert selection.summary["variants_tested"] == 2 and walk["deflated_sharpe"]["trials"] == 2
        assert len(selection.code_fingerprint) == 64 and selection.dataset_sha256 == "f" * 64

        await rx.request_holdout(session, experiment_id, user_id=None)
        await session.commit()
    async with SessionLocal() as session:
        outcome = await rx.holdout_job(session, {"experiment_id": str(experiment_id)}, SimpleNamespace(), None)
        await session.commit()
    assert outcome["verdict"] in {rx.PASSED, rx.FAILED}
    assert calls[-1][1] == DAYS[3]  # only now is the holdout read

    async with SessionLocal() as session:
        experiment = await session.get(ResearchExperiment, experiment_id)
        assert experiment.holdout_used and experiment.status == rx.HOLDOUT_SCORED
        holdout = [r for r in await rx.results_of(session, experiment_id) if r.kind == "HOLDOUT"][0]
        assert holdout.summary["verdict"] == experiment.verdict and "verdict_rule" in holdout.summary
        with pytest.raises(rx.ExperimentError, match="already been used"):
            await rx.request_holdout(session, experiment_id, user_id=None)


async def test_a_crashed_holdout_does_not_spend_the_holdout(world, monkeypatch):
    calls = []
    monkeypatch.setattr(rx, "read_dataset", _fake_reader(calls))
    monkeypatch.setattr("app.services.indicator_settings.effective", _effective)
    async with SessionLocal() as session:
        experiment = await rx.create_experiment(session, **_fields(world))
        await rx.register(session, experiment.id, user_id=None)
        await session.commit()
        experiment_id = experiment.id
    async with SessionLocal() as session:
        await rx.selection_job(session, {"experiment_id": str(experiment_id)}, SimpleNamespace(), None)
        await rx.request_holdout(session, experiment_id, user_id=None)
        await session.commit()

    def explode(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rx, "run_portfolio_backtest", explode)
    async with SessionLocal() as session:
        with pytest.raises(Exception, match="Holdout scoring failed"):
            await rx.holdout_job(session, {"experiment_id": str(experiment_id)}, SimpleNamespace(), None)
    async with SessionLocal() as session:
        experiment = await session.get(ResearchExperiment, experiment_id)
        assert experiment.status == rx.SELECTED and not experiment.holdout_used


async def test_a_crashed_selection_is_marked_not_left_queued(world, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise OSError("dataset file missing")

    monkeypatch.setattr(rx, "read_dataset", broken)
    monkeypatch.setattr("app.services.indicator_settings.effective", _effective)
    async with SessionLocal() as session:
        experiment = await rx.create_experiment(session, **_fields(world))
        await rx.register(session, experiment.id, user_id=None)
        await session.commit()
        experiment_id = experiment.id
    async with SessionLocal() as session:
        with pytest.raises(Exception, match="Selection failed"):
            await rx.selection_job(session, {"experiment_id": str(experiment_id)}, SimpleNamespace(), None)
    async with SessionLocal() as session:
        assert (await session.get(ResearchExperiment, experiment_id)).status == rx.FAILED_TO_RUN


def test_apply_combination_routes_each_parameter_where_it_belongs():
    strategy = StrategyConfiguration(id="s", name="Strategy s", strategy_type="rs-pullback-v1")
    variant, controls, settings = rx.apply_combination(
        strategy,
        {"stop_atr_multiple": 2.1},
        SimpleNamespace(
            opening_range_minutes=15,
            ema_fast_period=9,
            ema_slow_period=21,
            volume_lookback_candles=20,
            atr_period=14,
            daily_history_sessions=40,
        ),
        {"minimum_score": 80, "trailing_rule": "BREAKEVEN_AT_R", "stop_atr_multiple": 1.5, "ema_fast_period": 12},
    )
    assert variant.minimum_score == 80 and variant.exit_rules.trailing_rule == "BREAKEVEN_AT_R"
    assert controls["stop_atr_multiple"] == 1.5 and settings.ema_fast_period == 12


async def test_the_holdout_route_demands_explicit_confirmation():
    from fastapi import HTTPException

    from app.api.routes import research as routes

    with pytest.raises(HTTPException) as refused:
        await routes.score_holdout(uuid4(), routes.HoldoutRequest(), None, None)
    assert refused.value.status_code == 422 and "once" in refused.value.detail
