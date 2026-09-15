"""The invariant the whole live-execution layer rests on, checked mechanically.

Phases 1 to 3 build everything around placing an order — reading broker state,
reconciling it, translating symbols, authorising, recording what would have
happened — and place nothing. That claim is easy to make in a docstring and easy
to break with one line in an unrelated file, so it is asserted here against the
source itself rather than trusted.

When the phase that adds submission arrives, this test is expected to change in
the same commit, deliberately and visibly.
"""

import ast
from pathlib import Path

import pytest

from app.services.firstock.orders import FirstockReportClient
from app.services.live_readiness import SUBMISSION_ADAPTER_IMPLEMENTED

# Firstock's state-changing order endpoints, exactly as the API names them.
MUTATING_ENDPOINTS = {"placeOrder", "modifyOrder", "cancelOrder", "exitOrder"}

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def string_literals(tree: ast.AST) -> set[str]:
    """Every string constant except docstrings.

    Docstrings are excluded because this module's own prose, and the deliberate
    "this module contains no placeOrder" notes elsewhere, are documentation of
    the boundary rather than a breach of it.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docstrings
    }


def test_no_source_file_names_a_state_changing_order_endpoint() -> None:
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        literals = string_literals(ast.parse(path.read_text(encoding="utf-8")))
        for endpoint in sorted(MUTATING_ENDPOINTS & literals):
            offenders.append(f"{path.relative_to(APP_ROOT.parent)} references {endpoint}")
    assert offenders == [], "A submission endpoint appeared in the source: " + "; ".join(offenders)


@pytest.mark.parametrize("name", ["place_order", "modify_order", "cancel_order", "exit_order", "submit"])
def test_the_broker_client_exposes_no_way_to_change_an_order(name: str) -> None:
    """The read-only client is the only broker client the live path is given."""
    assert not hasattr(FirstockReportClient, name)


def test_the_readiness_gate_agrees_that_no_adapter_exists() -> None:
    """The operator-facing claim and the code must not drift apart."""
    assert SUBMISSION_ADAPTER_IMPLEMENTED is False


def test_the_live_risk_engine_does_not_import_the_paper_risk_engine() -> None:
    """Shared code means a paper sizing bug can authorise a live order."""
    source = (APP_ROOT / "services" / "live_risk.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}
    assert "app.services.risk_engine" not in imported
