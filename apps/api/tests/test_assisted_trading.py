from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import TradeApprovalIntent
from app.services.assisted_trading import decide_approval
from app.services.risk_engine import PaperRiskEngine, RiskDecision


class SignalSession:
    def __init__(self, signal: object | None) -> None:
        self.signal = signal

    async def get(self, _model: object, _reference_id: str) -> object | None:
        return self.signal


@pytest.mark.asyncio
async def test_expired_or_rejected_approvals_are_terminal_without_risk_reservation() -> None:
    expired = TradeApprovalIntent(
        reference_id="expired-signal",
        decision="PENDING",
        source="WEB",
        status="PENDING",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await decide_approval(SignalSession(object()), expired, "APPROVE")  # type: ignore[arg-type]
    assert expired.status == "EXPIRED"
    assert expired.submission_block_reason == "Approval expired before risk revalidation"

    rejected = TradeApprovalIntent(
        reference_id="rejected-signal",
        decision="PENDING",
        source="WEB",
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await decide_approval(SignalSession(object()), rejected, "REJECT")  # type: ignore[arg-type]
    assert (rejected.status, rejected.decision) == ("REJECTED", "REJECT")


@pytest.mark.asyncio
async def test_approved_request_revalidates_risk_and_remains_paper_only(monkeypatch: pytest.MonkeyPatch) -> None:
    signal = object()

    async def reserve_signal(_self: PaperRiskEngine, candidate: object) -> RiskDecision:
        assert candidate is signal
        return RiskDecision(allowed=True, reason="Paper risk reserved")

    monkeypatch.setattr(PaperRiskEngine, "reserve_signal", reserve_signal)
    approval = TradeApprovalIntent(
        reference_id="approved-signal",
        decision="PENDING",
        source="WEB",
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await decide_approval(SignalSession(signal), approval, "APPROVE")  # type: ignore[arg-type]

    assert approval.status == "APPROVED_PAPER_ONLY"
    assert approval.risk_revalidated_at is not None
    assert approval.submission_block_reason == "Live broker submission is unavailable in this release"
