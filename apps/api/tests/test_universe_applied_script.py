"""Whether the ranking was obeyed, which is not what any screen reported.

Every view of the universe showed the ranking: what was ranked, what was
selected, with what scores. All of it was correct on 12 October, and all of it
was beside the point -- the scanner had cached an empty universe at 09:15 and
evaluated 27 instruments against a selection of 21. The question nothing could
answer was whether the selection was obeyed.

This script answers it by comparing the selection against the instruments that
actually produced evaluation rows, so the tests here drive it end to end
against seeded rows: a comparison that reads the wrong tables would still pass
a test of its arithmetic alone.
"""

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY
from app.db.models import ApplicationSetting, ScannerEvaluation, ScanUniverseEntry
from app.db.session import SessionLocal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import universe_applied  # noqa: E402

pytestmark = pytest.mark.anyio

DAY = date(2099, 7, 1)
SELECTED = ["NSE_EQ|UA-PNB", "NSE_EQ|UA-RVNL"]
REJECTED = ["NSE_EQ|UA-SBIN"]


async def seed(*, enabled: bool, scanned: list[str], ranked: bool = True) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ScanUniverseEntry).where(ScanUniverseEntry.session_date == DAY))
        await session.execute(delete(ScannerEvaluation).where(ScannerEvaluation.session_date == DAY))
        for rank, token in enumerate(SELECTED if ranked else [], start=1):
            session.add(
                ScanUniverseEntry(
                    session_date=DAY,
                    instrument_token=token,
                    rank=rank,
                    score=Decimal("90"),
                    selected=True,
                    eligible=True,
                )
            )
        for token in REJECTED if ranked else []:
            session.add(
                ScanUniverseEntry(
                    session_date=DAY,
                    instrument_token=token,
                    rank=0,
                    score=Decimal("10"),
                    selected=False,
                    eligible=False,
                    rejection_reason="Daily ATR 0.42% below the 0.8% floor",
                )
            )
        for token in scanned:
            session.add(
                ScannerEvaluation(
                    evaluation_key=f"ua-{token}",
                    instrument_token=token,
                    session_date=DAY,
                    candle_opened_at=datetime(2099, 7, 1, 4, 0, tzinfo=UTC),
                    strategy_id="orb-retest-default",
                    strategy_name="ORB Retest — Default",
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
            )
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


async def test_a_rejected_instrument_that_was_scanned_is_reported_and_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The 12 October shape. A non-zero exit so this can gate a morning check."""
    await seed(enabled=True, scanned=SELECTED + REJECTED)
    assert await run(monkeypatch) == 1
    printed = capsys.readouterr().out
    assert "SCANNED BUT NOT SELECTED" in printed
    assert "UA-SBIN" in printed
    # The reason the ranking gave, so the operator does not have to go and ask
    # the database why that name was refused.
    assert "Daily ATR 0.42%" in printed


async def test_a_scan_that_obeyed_the_ranking_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await seed(enabled=True, scanned=SELECTED)
    assert await run(monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "Nothing was scanned that the ranking had not selected" in printed
    assert "SCANNED BUT NOT SELECTED" not in printed


async def test_a_selected_instrument_with_no_candles_is_a_note_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A quiet instrument produces no evaluation rows on its own. Treating that
    as a fault would cry wolf on most days."""
    await seed(enabled=True, scanned=SELECTED[:1])
    assert await run(monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "Selected but never scanned" in printed
    assert "UA-RVNL" in printed


async def test_nothing_ranked_is_reported_as_no_filtering_at_all(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Failing open is correct and is still not filtering. It reported itself as
    healthy for a month because the ranking looked fine in isolation."""
    await seed(enabled=True, scanned=SELECTED, ranked=False)
    assert await run(monkeypatch) == 1
    assert "Nothing was ranked" in capsys.readouterr().out


async def test_the_comparison_is_not_a_fault_while_the_ranking_is_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """With it off, scanning everything is the documented behaviour, and a BAD
    line there would teach an operator to ignore the script."""
    await seed(enabled=False, scanned=SELECTED + REJECTED)
    assert await run(monkeypatch) == 0
    printed = capsys.readouterr().out
    assert "every subscribed instrument is scanned by design" in printed
    assert "[BAD]" not in printed
