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
