"""Which indicator inputs a signal actually had, and which it only pretended to.

This exists because of a defect worth stating plainly. Scoring used to award a
component **full marks when its input was missing** and zero when the input was
present and unfavourable. An instrument with no VWAP, no relative volume, no
NIFTY regime and no relative strength collected sixty of the hundred points for
free, against a threshold of eighty. Absent evidence outscored unfavourable
evidence, and tied with perfect evidence.

The justification written into that code was that "the data-quality gate already
blocks bad data". It does not. That gate watches the *feed* — tick freshness,
missing candle buckets, latency, duplicates — and says nothing about whether ATR
or a volume baseline has been computed. A perfectly healthy feed on a newly
tracked instrument passes it with none of these indicators available.

That mattered here more than it might elsewhere: the relative-volume baseline
needs ``RVOL_BASELINE_SESSIONS`` sessions of history, and this deployment holds
roughly eight. Relative volume was plausibly absent, and silently scoring 20/20,
across much of the paper record the strategy was being judged on.

**The rule now.** Each strategy names the inputs it genuinely depends on. A
required input that is missing blocks the signal — it is not scored at all,
because a breakout-retest strategy without volume confirmation is not that
strategy with a lower score, it is a different and untested one. An input that
is *not* required and is missing scores zero. Nothing scores full marks for
absence, ever.

An indicator that cannot be computed from enough history returns ``None`` (or
``INSUFFICIENT_DATA`` for the regime), so "missing" and "stale" arrive here as
the same thing, which is what the operator wanted: either way the input is not
something a live order should rest on.
"""

from typing import Any

ATR = "atr"
VWAP = "vwap"
EMA = "ema"
RVOL = "rvol"
REGIME = "regime"
RELATIVE_STRENGTH = "relative_strength"

# Everything a strategy may declare. A name outside this set is a configuration
# error rather than an input that is always missing: silently treating an
# unknown requirement as unmet would block every signal and look like a data
# problem, which is a long afternoon.
SUPPORTED_INPUTS = frozenset({ATR, VWAP, EMA, RVOL, REGIME, RELATIVE_STRENGTH})

# What each strategy rests on, used as the default when an operator has not
# chosen. Deliberately generous about what counts as load-bearing: the cost of
# requiring an input that turns out to be optional is some refused signals, and
# the cost of the reverse is a live order placed on evidence nobody had.
DEFAULT_REQUIRED_INPUTS = {
    # Breakout distance and stop width are both measured in ATR; volume is what
    # separates a retest from a drift back through the level.
    "orb-retest-v1": [ATR, EMA, RVOL],
    # The entry is defined relative to VWAP, so its absence is not a weak
    # signal, it is no signal.
    "vwap-pullback-v1": [ATR, VWAP, RVOL],
    "ema-momentum-v1": [ATR, EMA, RVOL],
    # The whole premise is outperformance against the benchmark.
    "rs-pullback-v1": [ATR, EMA, RELATIVE_STRENGTH],
}


def _present(value: Any) -> bool:
    return value is not None


def available_inputs(indicators: dict, nifty: dict) -> set[str]:
    """The inputs that were actually computed for this candle.

    Reads the same payload shapes the scorer reads, so the two cannot disagree
    about whether something was there.
    """
    found: set[str] = set()
    if not isinstance(indicators, dict):
        indicators = {}
    if not isinstance(nifty, dict):
        nifty = {}

    if _present(indicators.get(ATR)):
        found.add(ATR)
    if _present(indicators.get(VWAP)):
        found.add(VWAP)
    if _present(indicators.get("ema_fast")) and _present(indicators.get("ema_slow")):
        found.add(EMA)

    volume = indicators.get("volume")
    if isinstance(volume, dict) and _present(volume.get("relative_volume")):
        found.add(RVOL)

    relative = indicators.get(RELATIVE_STRENGTH)
    if isinstance(relative, dict) and _present(relative.get("relative_strength_percent")):
        found.add(RELATIVE_STRENGTH)

    regime = nifty.get("nifty_regime")
    if isinstance(regime, dict):
        name = regime.get("regime")
        # INSUFFICIENT_DATA is the benchmark saying it could not decide. Treating
        # it as a regime would be treating "I don't know" as an answer.
        if name not in (None, "INSUFFICIENT_DATA"):
            found.add(REGIME)

    return found


def missing_required(indicators: dict, nifty: dict, required: list[str] | None) -> list[str]:
    """Which of the required inputs this candle does not have, in a stable order.

    Sorted rather than set-ordered so the refusal reason recorded against a
    signal reads the same way twice, which matters when somebody is comparing
    two sessions' evaluations by eye.
    """
    if not required:
        return []
    return sorted(set(required) - available_inputs(indicators, nifty))


def normalise_required(values: Any) -> list[str]:
    """Accept what an operator or a stored configuration offers, reject nonsense."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    cleaned = []
    for value in values:
        name = str(value).strip().lower()
        if not name:
            continue
        if name not in SUPPORTED_INPUTS:
            raise ValueError(f"Unsupported required input {name!r}; expected one of {sorted(SUPPORTED_INPUTS)}")
        if name not in cleaned:
            cleaned.append(name)
    return cleaned
