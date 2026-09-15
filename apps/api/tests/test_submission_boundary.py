"""Where the power to place an order is allowed to exist.

Phases 1 to 3 could assert that nothing in the repository could submit. Phase 4
adds code that can, so the invariant changes shape rather than disappearing: the
capability now exists in exactly one place, is exposed by exactly one class, and
is constructed in exactly one module.

That is worth asserting mechanically because it is what makes the rest of the
system safe by construction instead of by inspection. Reconciliation, shadow
evaluation and unknown-order recovery are all handed a read-only client, so no
edit to those files — however careless — can place an order. This file is what
stops that property from quietly decaying.

Each assertion below fails loudly when the boundary moves, which is the point:
moving it should be a deliberate act somebody reviews, not a side effect.
"""

import ast
from pathlib import Path

import pytest

from app.services.firstock.orders import FirstockOrderClient, FirstockReportClient
from app.services.live_readiness import SUBMISSION_ADAPTER_IMPLEMENTED

# Firstock's state-changing order endpoints, exactly as the API names them.
MUTATING_ENDPOINTS = {"placeOrder", "modifyOrder", "cancelOrder", "exitOrder"}

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# The only file permitted to name a state-changing endpoint.
BROKER_ADAPTER = "services/firstock/orders.py"

# The only file permitted to construct a submission-capable client.
CLIENT_FACTORY = "services/live_execution_gateway.py"

# Files that read broker state and must never be able to change it.
READ_ONLY_CALLERS = (
    "services/live_reconciliation.py",
    "services/live_shadow.py",
    "services/live_order_recovery.py",
)


def parse(relative: str) -> ast.AST:
    return ast.parse((APP_ROOT / relative).read_text(encoding="utf-8"))


def string_literals(tree: ast.AST) -> set[str]:
    """Every string constant except docstrings.

    Docstrings are excluded so that prose describing the boundary — including
    this module's own — is not mistaken for a breach of it.
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


def called_names(tree: ast.AST) -> set[str]:
    return {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


def test_only_the_broker_adapter_names_a_state_changing_endpoint() -> None:
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT).as_posix()
        if relative == BROKER_ADAPTER:
            continue
        for endpoint in sorted(MUTATING_ENDPOINTS & string_literals(ast.parse(path.read_text(encoding="utf-8")))):
            offenders.append(f"{relative} references {endpoint}")
    assert offenders == [], "A submission endpoint escaped the adapter: " + "; ".join(offenders)


def test_the_broker_adapter_does_name_them() -> None:
    """Guards the guard: a typo in the path above would make the test vacuous."""
    assert MUTATING_ENDPOINTS & string_literals(parse(BROKER_ADAPTER)) == {
        "placeOrder",
        "modifyOrder",
        "cancelOrder",
    }


@pytest.mark.parametrize("name", ["place_order", "modify_order", "cancel_order", "exit_order", "submit"])
def test_the_read_only_client_exposes_no_way_to_change_an_order(name: str) -> None:
    """This is what makes reconciliation and shadow evaluation provably safe."""
    assert not hasattr(FirstockReportClient, name)


@pytest.mark.parametrize("name", ["place_order", "modify_order", "cancel_order"])
def test_the_order_client_is_the_one_that_can(name: str) -> None:
    assert hasattr(FirstockOrderClient, name)


def test_only_the_gateway_constructs_a_submission_capable_client() -> None:
    """Asking "what can reach a broker with intent" should be one grep."""
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT).as_posix()
        if relative == CLIENT_FACTORY:
            continue
        if "FirstockOrderClient" in called_names(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(relative)
    assert offenders == [], "A submission client was built outside the gateway: " + "; ".join(offenders)


@pytest.mark.parametrize("relative", READ_ONLY_CALLERS)
def test_state_reading_modules_never_mention_the_submission_client(relative: str) -> None:
    """Not even as a type annotation: the name should not be reachable there."""
    source = (APP_ROOT / relative).read_text(encoding="utf-8")
    assert "FirstockOrderClient" not in source


def test_the_readiness_gate_agrees_that_an_adapter_exists() -> None:
    """The operator-facing claim and the code must not drift apart."""
    import app.services.live_orders  # noqa: F401 - existence is the assertion

    assert SUBMISSION_ADAPTER_IMPLEMENTED is True


def test_the_live_risk_engine_does_not_import_the_paper_risk_engine() -> None:
    """Shared code means a paper sizing bug can authorise a live order."""
    imported = {
        node.module
        for node in ast.walk(parse("services/live_risk.py"))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "app.services.risk_engine" not in imported


def test_the_live_approval_path_does_not_import_the_paper_approval_path() -> None:
    imported = {
        node.module
        for node in ast.walk(parse("services/live_approval.py"))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "app.services.assisted_trading" not in imported
