from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from app.core.config import Settings
from app.db.models import MarketCandle, ScanUniverseEntry
from app.db.session import SessionLocal, engine
from app.services.market_calculations import CompletedCandle
from app.services.universe import UniverseControls, rank_universe, refresh_universe

CONTROLS = UniverseControls(
    size=2,
    min_avg_turnover=Decimal("250000000"),
    min_price=Decimal("40"),
    max_price=Decimal("15000"),
    min_atr_percent=Decimal("0.8"),
    max_atr_percent=Decimal("8.0"),
)


def _daily(token: str, day: int, close: str, *, volume: int, spread: str = "2") -> CompletedCandle:
    opened = datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=day)
    price = Decimal(close)
    half = Decimal(spread) / 2
    return CompletedCandle(
        instrument_token=token,
        timeframe_seconds=86_400,
        opened_at=opened,
        closed_at=opened + timedelta(hours=6),
        open=price,
        high=price + half,
        low=price - half,
        close=price,
        volume=volume,
        tick_count=1,
    )


def _series(token: str, close: str, *, volume: int, spread: str, sessions: int = 10) -> list[CompletedCandle]:
    return [_daily(token, day, close, volume=volume, spread=spread) for day in range(sessions)]


def test_rank_universe_rejects_thin_short_and_out_of_band_instruments() -> None:
    daily = {
        "LIQUID": _series("LIQUID", "100", volume=4_000_000, spread="3"),  # turnover 400M, atr% ~3
        "THIN": _series("THIN", "100", volume=100_000, spread="3"),  # turnover 10M
        "SHORT": _series("SHORT", "100", volume=4_000_000, spread="3", sessions=3),
        "PENNY": _series("PENNY", "12", volume=90_000_000, spread="0.4"),  # below price band
        "SLEEPY": _series("SLEEPY", "100", volume=4_000_000, spread="0.1"),  # atr% ~0.1, below vol band
    }
    ranked = {candidate.instrument_token: candidate for candidate in rank_universe(daily, {}, CONTROLS)}

    assert ranked["LIQUID"].eligible and ranked["LIQUID"].selected
    assert ranked["THIN"].rejection_reason == "Below liquidity floor"
    assert ranked["SHORT"].rejection_reason == "Insufficient daily history"
    assert ranked["PENNY"].rejection_reason == "Outside price band"
    assert ranked["SLEEPY"].rejection_reason == "Outside volatility band"
    assert all(not ranked[token].selected for token in ("THIN", "SHORT", "PENNY", "SLEEPY"))


def test_rank_universe_selects_the_top_n_and_is_deterministic() -> None:
    daily = {
        "AAA": _series("AAA", "100", volume=6_000_000, spread="3"),
        "BBB": _series("BBB", "100", volume=5_000_000, spread="3"),
        "CCC": _series("CCC", "100", volume=4_000_000, spread="3"),
        "DDD": _series("DDD", "100", volume=3_500_000, spread="3"),
    }
    first = rank_universe(daily, {}, CONTROLS)
    second = rank_universe(daily, {}, CONTROLS)
    assert first == second

    selected = [candidate.instrument_token for candidate in first if candidate.selected]
    assert len(selected) == CONTROLS.size
    ranks = [candidate.rank for candidate in first]
    assert ranks == sorted(ranks) and ranks[0] == 1
    assert all(0.0 <= candidate.score <= 100.0 for candidate in first)


def test_rank_universe_gap_component_rewards_a_moderate_gap() -> None:
    daily = {
        "FLAT": _series("FLAT", "100", volume=4_000_000, spread="3"),
        "GAPPED": _series("GAPPED", "100", volume=4_000_000, spread="3"),
    }
    ranked = {
        candidate.instrument_token: candidate
        for candidate in rank_universe(daily, {"GAPPED": Decimal("101.5"), "FLAT": Decimal("100.0")}, CONTROLS)
    }
    assert ranked["GAPPED"].components["gap"] > ranked["FLAT"].components["gap"]


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://sidra:secret@postgres/sidra",
        "redis_url": "redis://redis:6379/0",
        "jwt_secret": "a-secure-test-secret-that-is-longer-than-32-characters",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


async def test_refresh_universe_persists_a_ranked_selection() -> None:
    await engine.dispose()
    session_date = date(2032, 4, 5)
    settings = _settings(
        upstox_subscriptions="NSE_INDEX|Nifty 50,NSE_EQ|AAA,NSE_EQ|BBB",
        universe_size=1,
        universe_min_avg_turnover=250_000_000,
        universe_min_atr_percent=0.8,
        universe_max_atr_percent=8.0,
    )
    rows = [
        MarketCandle(
            instrument_token=token,
            timeframe_seconds=86_400,
            session_date=session_date - timedelta(days=30 - day),
            opened_at=datetime(2032, 3, 6, tzinfo=UTC) + timedelta(days=day),
            closed_at=datetime(2032, 3, 6, 6, tzinfo=UTC) + timedelta(days=day),
            open=Decimal("100"),
            high=Decimal("101.5"),
            low=Decimal("98.5"),
            close=Decimal("100"),
            volume=volume,
            tick_count=1,
        )
        for token, volume in (("NSE_EQ|AAA", 4_000_000), ("NSE_EQ|BBB", 50_000))
        for day in range(10)
    ]
    try:
        async with SessionLocal() as session:
            session.add_all(rows)
            await session.commit()

        summary = await refresh_universe(settings, session_date)
        assert summary["candidates"] == 2
        assert summary["selected"] == 1

        async with SessionLocal() as session:
            entries = {
                entry.instrument_token: entry
                for entry in (
                    await session.scalars(
                        select(ScanUniverseEntry).where(ScanUniverseEntry.session_date == session_date)
                    )
                ).all()
            }
        assert entries["NSE_EQ|AAA"].selected is True
        assert entries["NSE_EQ|BBB"].eligible is False
        assert entries["NSE_EQ|BBB"].rejection_reason == "Below liquidity floor"
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(ScanUniverseEntry).where(ScanUniverseEntry.session_date == session_date))
            await session.execute(
                delete(MarketCandle).where(
                    MarketCandle.instrument_token.in_(["NSE_EQ|AAA", "NSE_EQ|BBB"]),
                    MarketCandle.timeframe_seconds == 86_400,
                )
            )
            await session.commit()
        await engine.dispose()


# --- the price band is the operator's, not the deployment's -------------------
#
# ``universe_max_price`` existed from the start and could only be changed by
# editing ``.env`` and restarting. It is a position-sizing control: a risk
# budget is spent in whole shares, so on a ₹10,000 account risking ₹100 a
# trade, a ₹4,777 share buys one -- which spends half the budget and earns half
# the target -- while a ₹500 share buys nineteen and spends all of it. That is
# a judgement an operator changes between sessions.


def _stored(**values: object):
    from types import SimpleNamespace

    base = {"universe_max_share_price": 0.0, "universe_min_share_price": 0.0}
    base.update(values)
    return SimpleNamespace(**base)


def test_a_saved_cap_beats_the_environment() -> None:
    controls = UniverseControls.from_settings(_settings(), _stored(universe_max_share_price=1500.0))
    assert controls.max_price == Decimal("1500")


def test_an_unset_cap_keeps_the_behaviour_the_deployment_already_had() -> None:
    """Zero means unbounded, so opening the screen and saving nothing changes
    nothing. A new control that silently narrowed the universe would be a
    scanner that stopped seeing shares nobody asked it to stop seeing."""
    assert UniverseControls.from_settings(_settings(), _stored()).max_price == Decimal("15000")
    assert UniverseControls.from_settings(_settings(), None).max_price == Decimal("15000")


def test_a_saved_floor_beats_the_environment() -> None:
    controls = UniverseControls.from_settings(_settings(), _stored(universe_min_share_price=100.0))
    assert controls.min_price == Decimal("100")


def test_an_unreadable_control_falls_back_rather_than_failing() -> None:
    """A universe that refuses to rebuild because a settings row will not parse
    is a scanner that watches nothing all day -- worse than the bounds it was
    trying to honour."""
    controls = UniverseControls.from_settings(_settings(), _stored(universe_max_share_price="not a number"))
    assert controls.max_price == Decimal("15000")


def test_the_cap_actually_removes_an_expensive_share() -> None:
    """End to end through the ranking, with the share that prompted this.

    HAL at ₹4,777 is a well-behaved instrument by every other measure; it is
    excluded because a ₹100 budget cannot be spent on it in whole shares.
    """
    capped = UniverseControls(
        size=5,
        min_avg_turnover=Decimal("0"),
        min_price=Decimal("40"),
        max_price=Decimal("1500"),
        min_atr_percent=Decimal("0"),
        max_atr_percent=Decimal("100"),
    )
    daily = {
        "HAL": _series("HAL", "4777", volume=1_000_000, spread="40"),
        "SBIN": _series("SBIN", "500", volume=1_000_000, spread="5"),
    }
    ranked = rank_universe(daily, {token: None for token in daily}, capped)
    by_token = {item.instrument_token: item for item in ranked}

    assert by_token["HAL"].eligible is False
    assert by_token["HAL"].selected is False
    assert by_token["SBIN"].eligible is True
    # The reason is on the row, because a share that silently stopped being
    # watched is a strategy that silently stopped being tested.
    assert by_token["HAL"].rejection_reason


# --- the rest of the universe follows the price band onto the screen ---------
#
# min_atr_percent, max_atr_percent, min_avg_turnover, size and the on/off
# switch all lived in .env and needed a container restart. That is the wrong
# shape for a decision made between sessions: on 9 October an AXISBANK trade
# was entered needing a 1.6% move from a share that travelled 1.13% all day,
# and squared off flat at 15:00. The ATR floor that would have excluded it was
# 0.8 in an environment file.


def test_a_saved_atr_floor_beats_the_environment() -> None:
    controls = UniverseControls.from_settings(_settings(), _stored(universe_min_atr_percent=2.0))
    assert controls.min_atr_percent == Decimal("2.0")


def test_a_saved_atr_ceiling_turnover_and_size_all_beat_the_environment() -> None:
    controls = UniverseControls.from_settings(
        _settings(),
        _stored(universe_max_atr_percent=6.0, universe_min_avg_turnover=500_000_000.0, universe_size=12),
    )
    assert controls.max_atr_percent == Decimal("6.0")
    assert controls.min_avg_turnover == Decimal("500000000.0")
    assert controls.size == 12


def test_saving_nothing_keeps_every_environment_value() -> None:
    """A deployment that never opens the screen must scan what it scanned
    yesterday. A new control that quietly narrowed the universe would be a
    scanner that stopped seeing shares nobody asked it to stop seeing."""
    env = _settings()
    controls = UniverseControls.from_settings(env, _stored())
    assert controls.min_atr_percent == Decimal(str(env.universe_min_atr_percent))
    assert controls.max_atr_percent == Decimal(str(env.universe_max_atr_percent))
    assert controls.min_avg_turnover == Decimal(str(env.universe_min_avg_turnover))
    assert controls.size == env.universe_size


def test_a_zero_size_is_not_a_size_anybody_means() -> None:
    """Zero reads as unbounded for the bands. A universe of zero shares is not
    an unbounded universe, it is a scanner watching nothing."""
    assert UniverseControls.from_settings(_settings(), _stored(universe_size=0)).size == _settings().universe_size


def test_a_zero_band_is_still_unbounded() -> None:
    env = _settings()
    controls = UniverseControls.from_settings(env, _stored(universe_min_atr_percent=0.0))
    assert controls.min_atr_percent == Decimal(str(env.universe_min_atr_percent))
