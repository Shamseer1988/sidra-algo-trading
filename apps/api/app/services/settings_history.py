"""Recording what a settings change changed, and when each control last moved.

Two questions the current settings row cannot answer, and one it answers
misleadingly.

``application_settings`` carries a row-level ``updated_at``. Shown against an
individual control it would say the daily loss stop changed this morning when
what actually changed was the minimum score — a timestamp that is true of the
row and false of the field the operator is looking at.

So each save writes a revision, and a control's "last changed" is the most
recent revision that listed it. A control never touched since the feature
existed reports None, which the UI shows as "not changed here" rather than
inventing a date.

The revision also holds the whole configuration as it stood, not a diff. A trade
has to be explicable by the settings that produced it, and reconstructing a
historical configuration by replaying diffs works until the one time it matters.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SettingRevision

# Limits that can be loosened. Tightening one needs no explanation later;
# raising one is the change somebody may have to account for, so it is named in
# the revision rather than left to be diffed out of two snapshots.
RISK_CEILINGS = (
    "risk_per_trade_percent",
    "maximum_daily_risk_percent",
    "maximum_daily_trades",
    "maximum_open_positions",
    "maximum_open_exposure_percent",
    "daily_loss_limit",
    "intraday_leverage_multiplier",
)


@dataclass(frozen=True)
class ChangeSummary:
    changed_keys: list[str]
    risk_increased: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.changed_keys


def _same(before: Any, after: Any) -> bool:
    """Whether two stored values are the same setting.

    Numbers are compared numerically, because 4 from a form and 4.0 from JSON
    are one value wearing two coats — comparing their text would fill the
    history with edits nobody made and put a spurious "last changed" timestamp
    on a control the operator never touched. Booleans are excluded from that:
    True == 1 in Python, and a leverage switch is not the number one.
    """
    if isinstance(before, bool) or isinstance(after, bool):
        return before is after
    if isinstance(before, int | float) and isinstance(after, int | float):
        return float(before) == float(after)
    return before == after


def summarise(previous: dict[str, Any], current: dict[str, Any]) -> ChangeSummary:
    """Which keys moved, and which of those were loosened."""
    changed = sorted(key for key in set(previous) | set(current) if not _same(previous.get(key), current.get(key)))
    increased = []
    for key in RISK_CEILINGS:
        before, after = previous.get(key), current.get(key)
        if isinstance(before, bool) or isinstance(after, bool):
            continue
        if isinstance(before, int | float) and isinstance(after, int | float) and after > before:
            increased.append(f"{key}: {before} -> {after}")
    return ChangeSummary(changed_keys=changed, risk_increased=increased)


async def record_revision(
    session: AsyncSession,
    key: str,
    value: dict[str, Any],
    summary: ChangeSummary,
    changed_by_user_id: Any = None,
) -> SettingRevision:
    """Append one revision. The caller commits.

    Written even when nothing changed: a save that an operator performed is a
    fact about the day, and a history with gaps where somebody pressed the
    button is harder to reason about than one with a no-op in it.
    """
    revision = SettingRevision(
        key=key,
        value=value,
        changed_keys=list(summary.changed_keys),
        risk_increased=list(summary.risk_increased),
        changed_by_user_id=changed_by_user_id,
    )
    session.add(revision)
    await session.flush()
    return revision


async def last_changed_per_key(session: AsyncSession, key: str) -> dict[str, str]:
    """When each individual control last moved, newest revision wins.

    Walks revisions oldest-first and lets later ones overwrite, which is the
    cheapest correct way to get a per-key answer out of per-save rows.
    """
    revisions = await session.scalars(
        select(SettingRevision).where(SettingRevision.key == key).order_by(SettingRevision.created_at.asc())
    )
    stamps: dict[str, str] = {}
    for revision in revisions.all():
        when = revision.created_at.isoformat() if revision.created_at else None
        if when is None:
            continue
        for changed in revision.changed_keys or []:
            stamps[str(changed)] = when
    return stamps


async def revision_history(session: AsyncSession, key: str, limit: int = 50) -> list[SettingRevision]:
    """Newest first, for an operator reading back what was done."""
    rows = await session.scalars(
        select(SettingRevision)
        .where(SettingRevision.key == key)
        .order_by(SettingRevision.created_at.desc())
        .limit(limit)
    )
    return list(rows.all())
