"""Change a saved setting from the terminal, the same way the Settings screen does.

The screen is the supported path and stays so. This exists for the case the
screen cannot serve: an operator who wants the change written down, reviewed and
applied in one reproducible step, with the diff on screen before anything moves.

**It is not a shortcut past the screen's safeguards; it is the same code.** The
new value is validated by the same ``TradingControls`` and
``StrategyConfiguration`` models, checked for the same contradictions, recorded
as the same settings revision, and audited. Writing JSON into Postgres by hand
does none of that, and a controls row that no longer validates is not a cosmetic
fault: ``live_controls`` returns None for it and every live order is refused.

**Reports by default. Nothing is written without --apply.** Every key it would
change is printed before and after.

Usage:

    # see what would change
    python scripts/apply_settings.py --control risk_per_trade_percent=1.0

    # several at once, applied together as one revision
    python scripts/apply_settings.py --apply \
        --control risk_per_trade_percent=1.0 \
        --control maximum_daily_risk_percent=3.0

    # a strategy, including its nested exit rules
    python scripts/apply_settings.py --apply \
        --strategy orb-retest minimum_score=75 \
        --strategy orb-retest exit_rules.trailing_rule=BREAKEVEN_AT_R

    # switch a strategy on or off
    python scripts/apply_settings.py --apply --strategy rs-pullback enabled=true
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError  # noqa: E402

from app.db.models import ApplicationSetting, AuditLog  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.settings_history import record_revision, summarise, summarise_strategies  # noqa: E402

RULE = "=" * 88
TRUE = {"true", "yes", "on", "1"}
FALSE = {"false", "no", "off", "0"}


def refuse(what: str, error: Exception) -> None:
    """Report a rejected value in words, and write nothing.

    A pydantic traceback is the right information in the wrong form for
    somebody reading it on a terminal before a session. The bound that was
    broken is the whole message.
    """
    print(f"\n  REFUSED — {what} was not changed:")
    for line in str(error).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("For further information"):
            print(f"    {stripped}")
    raise SystemExit(1)


def parse_value(raw: str) -> object:
    """A command-line string as the value it is meant to be.

    Order matters. ``true`` is a boolean and not the string "true", and ``2`` is
    an integer and not the float 2.0, because a leverage switch is not the
    number one and a trade ceiling is not a fraction. Anything that is none of
    those stays text, which is what a time like ``14:45`` and a rule name like
    ``BREAKEVEN_AT_R`` need. The models validate it afterwards either way.
    """
    text = raw.strip()
    lowered = text.lower()
    if lowered in TRUE:
        return True
    if lowered in FALSE:
        return False
    if lowered in {"none", "null", ""}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def split_pair(pair: str) -> tuple[str, object]:
    if "=" not in pair:
        raise SystemExit(f"Expected key=value, got: {pair}")
    key, _, raw = pair.partition("=")
    return key.strip(), parse_value(raw)


def apply_path(payload: dict, key: str, value: object) -> None:
    """Set ``key`` on a payload, following one dot into a nested block.

    ``exit_rules.trailing_rule`` has to reach inside the strategy's exit rules.
    A flat update would add a key called "exit_rules.trailing_rule" that
    nothing reads, and the save would look successful while changing nothing --
    which is exactly how a trailing rule was once swept without being tested.
    """
    if "." not in key:
        payload[key] = value
        return
    outer, _, inner = key.partition(".")
    nested = payload.get(outer)
    if not isinstance(nested, dict):
        raise SystemExit(f"{outer} is not a nested block on this object, so {key} cannot be set")
    payload[outer] = {**nested, inner: value}


def _squash(text: str) -> str:
    """Letters and digits only, lowercased.

    So that one typed name reaches a strategy however it is punctuated: the
    type is ``orb-retest-v1``, the display name is "ORB Retest — Default", and
    an operator types ``orb-retest``. Comparing the raw strings matches
    neither.
    """
    return "".join(character for character in text.lower() if character.isalnum())


def read_path(payload: dict, key: str) -> object:
    """The value one dotted key points at, so a diff shows the leaf.

    Printing the whole ``exit_rules`` block either side of a one-field change
    buries it: the reader has to compare two forty-character dictionaries to
    find the word that moved.
    """
    if "." not in key:
        return payload.get(key)
    outer, _, inner = key.partition(".")
    nested = payload.get(outer)
    return nested.get(inner) if isinstance(nested, dict) else None


def matches(configuration, selector: str) -> bool:  # noqa: ANN001 - StrategyConfiguration
    """Does this strategy answer to the name the operator typed?

    Its id, its type or any part of its display name, ignoring case and
    punctuation, so that ``orb-retest`` finds "ORB Retest — Default" without
    the operator having to paste a uuid.
    """
    needle = _squash(selector)
    if not needle:
        return False
    return any(
        needle in _squash(field) for field in (configuration.id, configuration.strategy_type, configuration.name)
    )


def show(label: str, before: object, after: object) -> bool:
    changed = json.dumps(before, default=str) != json.dumps(after, default=str)
    flag = "*" if changed else " "
    print(f"    {flag} {label:<34}{str(before):<22} -> {after}")
    return changed


async def change_controls(session, pairs: list[str], *, apply: bool) -> int:  # noqa: ANN001
    from app.api.routes.settings import DEFAULT_TRADING_CONTROLS, TRADING_KEY, TradingControls, ranking_built_too_late

    setting = await session.get(ApplicationSetting, TRADING_KEY)
    previous = dict(setting.value) if setting else dict(DEFAULT_TRADING_CONTROLS)
    proposed = dict(previous)
    for pair in pairs:
        key, value = split_pair(pair)
        if key not in previous and key not in DEFAULT_TRADING_CONTROLS:
            raise SystemExit(f"{key} is not a trading control. Check the Settings screen for the exact name.")
        apply_path(proposed, key, value)

    # The same model the screen validates against, so a value out of bounds is
    # refused here rather than written and refused at every live order.
    try:
        controls = TradingControls(**proposed)
    except ValidationError as error:
        refuse("the trading controls", error)
    payload = controls.model_dump()
    contradiction = ranking_built_too_late(controls)
    if contradiction:
        raise SystemExit(f"Refused: {contradiction}")

    print("\n  TRADING CONTROLS")
    touched = 0
    for key in sorted({split_pair(pair)[0] for pair in pairs}):
        if show(key, read_path(previous, key), read_path(payload, key)):
            touched += 1
    if not touched:
        print("    Nothing differs from what is already saved.")
        return 0
    if not apply:
        return touched

    summary = summarise(previous, payload)
    if setting is None:
        session.add(ApplicationSetting(key=TRADING_KEY, value=payload))
    else:
        setting.value = payload
    await record_revision(session, TRADING_KEY, payload, summary)
    session.add(
        AuditLog(
            event_type="settings.trading_updated",
            metadata_json={
                "changed_keys": summary.changed_keys,
                "risk_increased": summary.risk_increased,
                "source": "scripts/apply_settings.py",
            },
        )
    )
    if summary.risk_increased:
        print("    [RISK UP] " + "; ".join(summary.risk_increased))
    return touched


async def change_strategies(session, requests: list[list[str]], *, apply: bool) -> int:  # noqa: ANN001
    from app.services.strategy_registry import DEFAULT_STRATEGIES, STRATEGIES_KEY, StrategyConfiguration

    setting = await session.get(ApplicationSetting, STRATEGIES_KEY)
    stored = list(setting.value) if setting else [dict(item) for item in DEFAULT_STRATEGIES]
    previous = [StrategyConfiguration.model_validate(item) for item in stored]
    payloads = [item.model_dump() for item in previous]

    wanted: dict[int, list[str]] = {}
    for selector, *pairs in requests:
        hits = [index for index, item in enumerate(previous) if matches(item, selector)]
        if not hits:
            raise SystemExit(f"No strategy matches {selector!r}. Names: " + ", ".join(i.name for i in previous))
        if len(hits) > 1:
            raise SystemExit(f"{selector!r} matches more than one strategy; use its id or type instead.")
        wanted.setdefault(hits[0], []).extend(pairs)

    print("\n  STRATEGIES")
    touched = 0
    for index, pairs in sorted(wanted.items()):
        print(f"    {previous[index].name}")
        proposed = dict(payloads[index])
        for pair in pairs:
            key, value = split_pair(pair)
            apply_path(proposed, key, value)
        # Validated one strategy at a time, so a refusal names the strategy the
        # bad value belongs to.
        try:
            configuration = StrategyConfiguration.model_validate(proposed)
        except ValidationError as error:
            refuse(previous[index].name, error)
        after = configuration.model_dump()
        for pair in pairs:
            key = split_pair(pair)[0]
            if show(f"  {key}", read_path(payloads[index], key), read_path(after, key)):
                touched += 1
        payloads[index] = after

    if not touched:
        print("    Nothing differs from what is already saved.")
        return 0
    if not apply:
        return touched

    # Version bumped exactly as the screen bumps it: only where the strategy
    # really changed, ignoring the version field itself.
    final = []
    for before, after in zip(previous, payloads, strict=True):
        item = StrategyConfiguration.model_validate(after)
        changed = item.model_dump(exclude={"version"}) != before.model_dump(exclude={"version"})
        final.append(
            item.model_copy(update={"version": before.version + 1 if changed else before.version}).model_dump()
        )

    summary = summarise_strategies([item.model_dump() for item in previous], final)
    if setting is None:
        session.add(ApplicationSetting(key=STRATEGIES_KEY, value=final))
    else:
        setting.value = final
    await record_revision(session, STRATEGIES_KEY, final, summary)
    session.add(
        AuditLog(
            event_type="settings.strategies_updated",
            metadata_json={
                "changed_keys": summary.changed_keys,
                "risk_increased": summary.risk_increased,
                "source": "scripts/apply_settings.py",
            },
        )
    )
    if summary.risk_increased:
        print("    [RISK UP] " + "; ".join(summary.risk_increased))
    return touched


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--control", action="append", default=[], metavar="KEY=VALUE", help="An account control.")
    parser.add_argument(
        "--strategy",
        action="append",
        nargs="+",
        default=[],
        metavar=("SELECTOR", "KEY=VALUE"),
        help="A strategy's own setting: --strategy orb-retest minimum_score=75",
    )
    parser.add_argument("--apply", action="store_true", help="Write it. Without this nothing is changed.")
    args = parser.parse_args()

    if not args.control and not args.strategy:
        parser.error("Nothing to change. Pass --control or --strategy.")
    for request in args.strategy:
        if len(request) < 2:
            parser.error(f"--strategy {request[0]} needs at least one KEY=VALUE after the name")

    print(RULE)
    print("APPLY SETTINGS" + ("" if args.apply else "   (report only)"))
    print(RULE)
    print("  The same validation, versioning and settings history the Settings screen uses.")

    async with SessionLocal() as session:
        touched = 0
        if args.control:
            touched += await change_controls(session, args.control, apply=args.apply)
        if args.strategy:
            touched += await change_strategies(session, args.strategy, apply=args.apply)
        if args.apply and touched:
            await session.commit()

    print("\n" + RULE)
    if not touched:
        print("  Nothing to do; the saved settings already hold these values.")
    elif args.apply:
        print(f"  {touched} setting(s) written, as one revision. Run check_settings.py to read it back.")
        print("  The scanner reads the saved controls per candle, so this takes effect without a restart.")
    else:
        print(f"  {touched} setting(s) would change. Read the lines marked * above, then re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
