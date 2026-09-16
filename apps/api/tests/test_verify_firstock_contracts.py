"""The verdict the contract probe prints.

The probe exists to answer one question — does the order book echo the remarks
we send — and the operator will act on its answer. A wrong verdict here is worse
than no verdict, so the three outcomes are pinned: supported, not supported, and
"could not tell from an empty book", which must never be reported as either of
the other two.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_firstock_contracts.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("verify_firstock_contracts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


probe = load_probe()


def test_an_order_book_carrying_remarks_reports_support() -> None:
    book = [{"orderNumber": "1", "status": "OPEN", "remarks": "sidra-abc"}]
    assert probe.report_remarks_support(book) is True


@pytest.mark.parametrize("key", ["remarks", "remark", "Remarks"])
def test_any_documented_spelling_counts(key: str) -> None:
    assert probe.report_remarks_support([{"orderNumber": "1", key: "x"}]) is True


def test_an_order_book_without_remarks_reports_the_problem() -> None:
    book = [{"orderNumber": "1", "status": "OPEN", "tradingSymbol": "IDEA-EQ"}]
    assert probe.report_remarks_support(book) is False


def test_an_empty_book_is_undetermined_not_a_failure() -> None:
    """Reporting "not supported" here would send someone chasing a non-problem."""
    assert probe.report_remarks_support([]) is None


def test_field_names_are_collected_across_records() -> None:
    """Partial records are normal; the operator needs the union, not the first row."""
    names = probe.field_names([{"a": 1}, {"b": 2}, {"a": 3, "c": 4}])
    assert names == ["a", "b", "c"]


def test_field_names_tolerates_non_dict_records() -> None:
    assert probe.field_names(["nonsense", None, {"a": 1}]) == ["a"]


def test_stage_two_requires_a_symbol_and_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither has a safe default, because a default here would place a real order."""
    monkeypatch.setattr("sys.argv", ["verify_firstock_contracts.py", "--place-test-order"])
    with pytest.raises(SystemExit) as exit_info:
        probe.main()
    # argparse exits 2 on a usage error; 0 would mean it went on to trade.
    assert exit_info.value.code == 2


def test_the_confirmation_phrase_is_not_something_typed_by_accident() -> None:
    assert probe.CONFIRMATION == "PLACE A REAL ORDER"
    assert len(probe.CONFIRMATION) > 10
