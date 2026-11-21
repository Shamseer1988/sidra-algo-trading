"""Settings > Swing trading: read and save the paper desk's capital and limits.

The response describes each control in the same shape as the intraday
catalogue, so the screen draws both with one component. A save is one recorded
change (settings revision plus audit entry, with the reason). Paper only:
nothing here reaches a broker or a live risk gate; see
``app.services.swing_settings``.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, require_roles
from app.api.routes.charges import save_card
from app.db.models import ApplicationSetting, User, UserRole
from app.services.swing_paper import RESULTS_AVOID_DAYS, RESULTS_EXIT_DAYS
from app.services.swing_settings import SWING_RULES_KEY, SwingRuleSettings, swing_rule_settings

router = APIRouter(prefix="/settings/swing", tags=["Settings"])

EFFECT = "NEXT_SESSION"
EFFECT_LABEL = "Applies from the next evening run; positions already open keep their size and stop."

GROUPS = {
    "SWING_CAPITAL": "Capital and risk",
    "SWING_POSITIONS": "Positions",
    "SWING_ENTRY": "Entry filters",
}

# key: (group, label, help, unit, kind, is_ceiling)
SPECS: dict[str, tuple[str, str, str, str, str, bool]] = {
    "capital": (
        "SWING_CAPITAL",
        "Swing capital",
        "The paper money the swing desk sizes against. Separate from the intraday account capital; "
        "it never moves real money.",
        "INR",
        "number",
        False,
    ),
    "risk_per_trade_percent": (
        "SWING_CAPITAL",
        "Risk a trade",
        "What one trade loses if its stop is hit, as a share of swing capital. Shares = this amount "
        "divided by the distance from entry to stop.",
        "PERCENT",
        "number",
        False,
    ),
    "heat_cap_percent": (
        "SWING_CAPITAL",
        "Open-risk cap",
        "All open trades' risk added together may not exceed this share of capital. A new plan that "
        "would cross it is skipped.",
        "PERCENT",
        "number",
        True,
    ),
    "max_position_percent": (
        "SWING_CAPITAL",
        "Largest single position",
        "No one share may take more than this share of capital, however tight its stop.",
        "PERCENT",
        "number",
        True,
    ),
    "max_open_positions": (
        "SWING_POSITIONS",
        "Open positions",
        "The most swing positions held at once, counting plans waiting for tomorrow's open.",
        "COUNT",
        "integer",
        True,
    ),
    "max_per_sector": (
        "SWING_POSITIONS",
        "Positions per sector",
        "The most positions in one sector at once, so one industry's bad day is not five losses.",
        "COUNT",
        "integer",
        True,
    ),
    "gap_skip_percent": (
        "SWING_ENTRY",
        "Skip a gap above",
        "If the next session opens more than this far above the entry trigger, the plan is cancelled "
        "rather than chased.",
        "PERCENT",
        "number",
        False,
    ),
    "regime_sma": (
        "SWING_ENTRY",
        "Market filter average",
        "No new entries while the benchmark index closes below its simple moving average of this many "
        "sessions. Open positions are still managed.",
        "COUNT",
        "integer",
        False,
    ),
}


class SwingSettingSpec(BaseModel):
    key: str
    group: str
    group_label: str
    label: str
    help: str
    unit: str
    kind: str
    choices: list[str] = []
    is_ceiling: bool
    effect: str = EFFECT
    effect_label: str = EFFECT_LABEL
    value: float | int
    minimum: float | None
    maximum: float | None
    exclusive_minimum: float | None
    exclusive_maximum: float | None
    last_changed_at: str | None


class SwingEffective(BaseModel):
    risk_per_trade: float
    heat_cap: float
    largest_position: float
    trades_the_cap_allows: int


class SwingSchedule(BaseModel):
    quiet_hours: str
    plan_after: str
    entry: str
    results_avoid_days: int
    results_exit_days: int


class SwingSettingsView(BaseModel):
    settings: list[SwingSettingSpec]
    group_order: list[str]
    group_labels: dict[str, str]
    effective: SwingEffective
    schedule: SwingSchedule
    saved: bool


class SwingSettingsRequest(BaseModel):
    values: SwingRuleSettings
    reason: str = Field(min_length=1, max_length=300)


def _bounds(field: str) -> dict[str, float | None]:
    found: dict[str, float | None] = {
        "minimum": None,
        "maximum": None,
        "exclusive_minimum": None,
        "exclusive_maximum": None,
    }
    for item in SwingRuleSettings.model_fields[field].metadata:
        for attr, key in (
            ("ge", "minimum"),
            ("le", "maximum"),
            ("gt", "exclusive_minimum"),
            ("lt", "exclusive_maximum"),
        ):
            value = getattr(item, attr, None)
            if value is not None:
                found[key] = float(value)
    return found


async def _view(session) -> SwingSettingsView:  # noqa: ANN001
    current = await swing_rule_settings(session)
    row = await session.get(ApplicationSetting, SWING_RULES_KEY)
    changed = row.updated_at.isoformat() if row is not None and row.updated_at else None
    values = current.model_dump()
    specs = [
        SwingSettingSpec(
            key=key,
            group=group,
            group_label=GROUPS[group],
            label=label,
            help=text,
            unit=unit,
            kind=kind,
            is_ceiling=ceiling,
            value=values[key],
            last_changed_at=changed,
            **_bounds(key),
        )
        for key, (group, label, text, unit, kind, ceiling) in SPECS.items()
    ]
    rules = current.rules()
    return SwingSettingsView(
        settings=specs,
        group_order=list(GROUPS),
        group_labels=GROUPS,
        effective=SwingEffective(
            risk_per_trade=float(rules.risk_per_trade),
            heat_cap=float(rules.heat_cap),
            largest_position=float(rules.capital * rules.max_position_percent / 100),
            trades_the_cap_allows=min(int(rules.heat_cap // rules.risk_per_trade), rules.max_open_positions),
        ),
        schedule=SwingSchedule(
            quiet_hours="9:00 AM - 3:40 PM IST on weekdays (the swing desk stays out of the intraday desk's way)",
            plan_after="6:00 PM IST, once the day's candles are final",
            entry="At the next session's open; a plan that gaps beyond the limit is cancelled",
            results_avoid_days=RESULTS_AVOID_DAYS,
            results_exit_days=RESULTS_EXIT_DAYS,
        ),
        saved=row is not None,
    )


@router.get("", response_model=SwingSettingsView)
async def swing_settings(_: CurrentUser, session: DbSession) -> SwingSettingsView:
    return await _view(session)


@router.put("", response_model=SwingSettingsView)
async def update_swing_settings(
    request: SwingSettingsRequest,
    session: DbSession,
    user: User = Depends(require_roles(UserRole.ADMIN)),
) -> SwingSettingsView:
    if not request.reason.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give a reason for the change.")
    before = (await swing_rule_settings(session)).model_dump()
    after = request.values.model_dump()
    if before == after:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing changed.")
    await save_card(
        session, SWING_RULES_KEY, before, after, user, "settings.swing_rules_updated", request.reason.strip()
    )
    await session.commit()
    return await _view(session)
