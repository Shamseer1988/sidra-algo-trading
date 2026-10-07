"""The deployment snapshot's feature list has to stay true.

Section 1 of the snapshot answers "is this container running the code I think it
is" by checking that named symbols exist. The failure mode is quiet and nasty:
rename one of those symbols and the snapshot reports the fix as missing forever,
sending an operator to rebuild a container that was already correct — or worse,
teaching them to ignore the section.

So the list is pinned here. A rename breaks this test, not tomorrow morning.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import snapshot  # noqa: E402


def test_the_feature_list_is_not_empty():
    assert snapshot.FEATURES


@pytest.mark.parametrize(("label", "module_name", "symbol"), snapshot.FEATURES)
def test_every_feature_the_snapshot_checks_for_actually_exists(label: str, module_name: str, symbol: str):
    module = importlib.import_module(module_name)
    assert hasattr(module, symbol), f"{label}: {module_name}.{symbol} is gone, so the snapshot now lies about it"


def test_no_credential_is_ever_printed():
    """The output of this script gets pasted into chat windows. Presence only —
    not masked, not truncated, not the first four characters."""
    source = (Path(__file__).resolve().parents[1] / "scripts" / "snapshot.py").read_text()
    # Every secret-bearing field on Settings, by name. The script may ask
    # whether they are configured; it may never read one.
    for forbidden in (
        "jwt_secret",
        "firstock_api_key",
        "firstock_password",
        "firstock_totp_secret",
        "upstox_access_token",
        "upstox_api_key",
        "upstox_api_secret",
        "upstox_token_encryption_key",
        "upstox_totp_secret",
        "telegram_bot_token",
    ):
        assert forbidden not in source, f"snapshot.py reads {forbidden}; it must only report configured yes/no"


# --- the scanner's master switch, reported honestly --------------------------
#
# Three states, and conflating any two has already misled once. An unset key is
# how a deployment that has never touched the switch looks, and the scanner
# reads it as on; reporting that as "unknown" raises a question about the most
# important switch in the system on every single run, which is how a reader
# learns to skip the line. Reporting an unreadable key as "on" is worse: it
# says everything is fine because nothing could be read.


def test_an_unset_switch_reads_as_on_because_that_is_what_the_scanner_does():
    line = snapshot.tracking_line(None)
    assert line.startswith("on")
    assert "unknown" not in line


def test_an_explicit_false_reads_as_off():
    assert snapshot.tracking_line("false") == "off"


def test_an_explicit_true_reads_as_on():
    assert snapshot.tracking_line("true") == "on"


def test_a_key_that_could_not_be_read_is_never_reported_as_on():
    line = snapshot.tracking_line(snapshot.UNREADABLE)
    assert "unknown" in line
    assert not line.startswith("on")


def test_the_three_states_are_all_distinguishable():
    readings = {
        snapshot.tracking_line(None),
        snapshot.tracking_line("false"),
        snapshot.tracking_line(snapshot.UNREADABLE),
    }
    assert len(readings) == 3
