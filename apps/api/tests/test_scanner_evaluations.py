from types import SimpleNamespace

from app.services.paper_strategy import AWAITING, SIGNALLED
from app.services.scanner_orchestration import PaperScannerOrchestrator


def test_scanner_evaluation_classifies_accepted_watching_and_rejected_decisions() -> None:
    accepted = SimpleNamespace(next_state=SIGNALLED, side="LONG", reason="Paper signal confirmed", score_breakdown={})
    watching = SimpleNamespace(next_state=AWAITING, side=None, reason="Awaiting breakout retest", score_breakdown={})
    rejected = SimpleNamespace(
        next_state=AWAITING,
        side=None,
        reason="Retest failed score threshold",
        score_breakdown={"vwap_alignment": 0, "volume_confirmation": 20},
    )

    assert PaperScannerOrchestrator._evaluation_status(accepted) == "ACCEPTED"
    assert PaperScannerOrchestrator._evaluation_status(watching) == "WATCHING"
    assert PaperScannerOrchestrator._evaluation_status(rejected) == "REJECTED"
    assert PaperScannerOrchestrator._failed_conditions(accepted) == []
    assert PaperScannerOrchestrator._failed_conditions(rejected) == [
        "Retest failed score threshold",
        "Vwap Alignment",
    ]
