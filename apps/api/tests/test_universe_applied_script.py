"""Whether the ranking was obeyed -- which is not what any screen reported, and
not what a naive comparison measures either.

The first version of this script compared the selection against every
instrument that had an evaluation row, and reported a leak on a morning when
the gate was working: five names refused by the ATR band, all five with rows.
Two writers put rows there for an instrument the universe correctly refused.

  * The data-quality gate runs before the universe gate and returns there, so
    its rows say nothing about the universe.
  * The first candle of the day is evaluated before the ranking is written --
    the gate fails open until it exists, by design -- so every subscribed
    instrument legitimately gets one minute of scanning.

Both are pinned here, because the comparison being narrow is the whole point:
a script that cries wolf every morning is one an operator learns to ignore,
which is worse than not having it.
"""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, QUALITY_GATE_ID, TRADING_KEY
from app.db.models import ApplicationSetting, ScannerEvaluation, ScanUniverseEntry
from app.db.session import SessionLocal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import universe_applied  # noqa: E402

pytestmark = pytest.mark.anyio

DAY = date(2099, 7, 1)
# 09:15 and 09:16 IST, the two minutes the whole distinction lives in.
OPENING = datetime(2099, 7, 1, 3, 45, tzinfo=UTC)
AFTER = datetime(2099, 7, 1, 3, 46, tzinfo=UTC)
WRITTEN = datetime(2099, 7, 1, 3, 45, 40, tzinfo=UTC)

SELECTED = ["NSE_EQ|UA-ADANIENT", "NSE_EQ|UA-PNB"]
REFUSED = ["NSE_EQ|UA-RELIANCE", "NSE_EQ|UA-SBIN"]


def evaluation(token: str, *, at: datetime, strategy_id: str = "orb-retest-default") -> ScannerEvaluation:
    return ScannerEvaluation(
        evaluation_key=f"ua-{token}-{strategy_id}-{at.isoformat()}",
        instrument_token=token,
        session_date=DAY,
        candle_opened_at=at,
        strategy_id=strategy_id,
        strategy_name=strategy_id,
        strategy_version=1,
        status="REJECTED",
        decision_state="AWAITING_BREAKOUT",
        reason="seeded",
        failed_conditions=[],
        data_quality_state="GOOD",
        candle_close=Decimal("100"),
        candle_volume=100,
        score=10,
    )


async def seed(
    *, enabled: bool = True, ranked: bool = True, evaluations: list[ScannerEvaluation] | None = None
) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ScanUniverseEntry).where(ScanUniverseEntry.session_date == DAY))
        await session.execute(delete(ScannerEvaluation).where(ScannerEvaluation.session_date == DAY))
        if ranked:
            for rank, token in enumerate(SELECTED, start=1):
                session.add(
                    ScanUniverseEntry(
                        session_date=DAY,
                        instrument_token=token,
                        rank=rank,
                        score=Decimal("90"),
                        selected=True,
                        eligible=True,
                        # The write time is what every comparison is against,
                        # so it is set rather than left to the server default.
                        created_at=WRITTEN,
                    )
                )
            for token in REFUSED:
                session.add(
                    ScanUniverseEntry(
                        session_date=DAY,
                        instrument_token=token,
                        rank=0,
                        score=Decimal("10"),
                        selected=False,
                        eligible=False,
                        rejection_reason="Outside volatility band",
                        created_at=WRITTEN,
                    )
                )
        for row in evaluations or []:
            session.add(row)
        row = await session.get(ApplicationSetting, TRADING_KEY)
        value = {**DEFAULT_TRADING_CONTROLS, "universe_enabled": enabled}
        if row:
            row.value = value
        else:
            session.add(ApplicationSetting(key=TRADING_KEY, value=value))
        await session.commit()


async def clean() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ScanUniverseEntry).where(ScanUniverseEntry.session_date == DAY))
        await session.execute(delete(ScannerEvaluation).where(ScannerEvaluation.session_date == DAY))
        await session.execute(delete(ApplicationSetting).where(ApplicationSetting.key == TRADING_KEY))
        await session.commit()


@pytest.fixture(autouse=True)
async def reset():
    await clean()
    yield
    await clean()


async def run(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["universe_applied.py", "--date", DAY.isoformat()])
    return await universe_applied.main()


async def test_this_mornings_shape_is_not_a_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """13 October, which the first version of this script called a failure.

    Two refused instruments, each with rows: one from the opening candle before
    the ranking existed, one a data-quality block. Neither reached the universe
    gate's decision, and neither is evidence about it.
    """
    await seed(
        evaluations=[
            evaluation(SELECTED[0], at=AFTER),
            evaluation(REFUSED[0], at=OPENING),
            evaluation(REFUSED[1], at=AFTER, strategy_id=QUALITY_GATE_ID),
        ]
    )
    assert await run(monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "Nothing a strategy scored under this ranking was outside its selection" in printed
    assert "SCANNED UNDER THIS RANKING BUT NOT SELECTED" not in printed
    # Shown, not silently dropped: an operator who cannot see what was excluded
    # cannot tell this script from the one that cried wolf.
    assert "scored only before the ranking was written" in printed
    assert "UA-RELIANCE" in printed
    assert "data-quality blocks only" in printed
    assert "UA-SBIN" in printed


async def test_a_refused_instrument_scored_after_the_ranking_is_a_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The real failure: a strategy scoring a refused name on a candle that
    opened after the gate had the ranking in front of it."""
    await seed(evaluations=[evaluation(SELECTED[0], at=AFTER), evaluation(REFUSED[0], at=AFTER)])
    assert await run(monkeypatch) == 1
    printed = capsys.readouterr().out
    assert "SCANNED UNDER THIS RANKING BUT NOT SELECTED" in printed
    assert "UA-RELIANCE" in printed
    assert "Outside volatility band" in printed


async def test_the_boundary_candle_is_not_called_a_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A row cannot be proven to have seen a ranking written during its own
    candle, and a real leak lasts the session rather than one minute."""
    await seed(evaluations=[evaluation(REFUSED[0], at=OPENING)])
    assert await run(monkeypatch) == 0
    assert "SCANNED UNDER THIS RANKING" not in capsys.readouterr().out


async def test_a_clean_session_counts_what_was_governed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await seed(evaluations=[evaluation(token, at=AFTER) for token in SELECTED])
    assert await run(monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "scanned under that ranking : 2" in printed
    assert "selected by the ranking    : 2" in printed


async def test_a_selected_instrument_with_no_candles_is_a_note_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Most of the selection has not been reached at 09:20, and a quiet
    instrument never is. Reporting that as a fault would cry wolf daily."""
    await seed(evaluations=[evaluation(SELECTED[0], at=AFTER)])
    assert await run(monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "selected but not yet scored under the ranking" in printed
    assert "UA-PNB" in printed


async def test_the_latest_build_is_the_one_rows_are_judged_against(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A worker restart rebuilds the ranking mid-session. The selection stored
    is that build's, so rows before it were judged by something else."""
    rebuilt = AFTER + timedelta(minutes=30)
    await seed(evaluations=[evaluation(REFUSED[0], at=AFTER)])
    async with SessionLocal() as session:
        entries = (await session.scalars(select(ScanUniverseEntry).where(ScanUniverseEntry.session_date == DAY))).all()
        for entry in entries:
            entry.created_at = rebuilt
        await session.commit()
    assert await run(monkeypatch) == 0
    assert "SCANNED UNDER THIS RANKING" not in capsys.readouterr().out


async def test_nothing_ranked_is_reported_as_no_filtering_at_all(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Failing open is correct and is still not filtering."""
    await seed(ranked=False, evaluations=[evaluation(SELECTED[0], at=AFTER)])
    assert await run(monkeypatch) == 1
    assert "Nothing was ranked" in capsys.readouterr().out


async def test_the_comparison_is_not_a_fault_while_the_ranking_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await seed(enabled=False, evaluations=[evaluation(token, at=AFTER) for token in SELECTED + REFUSED])
    assert await run(monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "every subscribed instrument is scanned by design" in printed
    assert "[BAD]" not in printed
