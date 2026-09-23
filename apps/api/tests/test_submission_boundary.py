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
from app.services.upstox_orders import UpstoxOrderClient, UpstoxReportClient

# State-changing order endpoints, exactly as each broker names them. Firstock
# names methods; Upstox names URL paths. Both spellings belong here, because a
# guard that knows one broker's vocabulary silently stops guarding when a second
# broker arrives — which is what happened when the Upstox adapter was added and
# this file did not fail.
MUTATING_ENDPOINTS = {
    # Firstock
    "placeOrder",
    "modifyOrder",
    "cancelOrder",
    "exitOrder",
    # Upstox
    "/v3/order/place",
    "/v3/order/cancel",
    "/v2/order/place",
    "/v2/order/cancel",
}

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# The only files permitted to name a state-changing endpoint, one per broker.
BROKER_ADAPTERS = ("services/firstock/orders.py", "services/upstox_orders.py")

# The only file permitted to construct a submission-capable client.
CLIENT_FACTORY = "services/live_execution_gateway.py"

# Every submission-capable client class. Each must be unreachable from the
# read-only callers below.
SUBMISSION_CLIENTS = ("FirstockOrderClient", "UpstoxOrderClient")

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


def referenced_names(tree: ast.AST) -> set[str]:
    """Every name the module mentions, not only the ones it calls directly.

    Deliberately stricter than looking for a call. ``factory = UpstoxOrderClient``
    followed by ``factory(...)`` constructs a submission-capable client without
    ever calling that name, and a guard that only watched calls would have
    nothing to say about it. Naming the class at all is the thing being fenced.
    """
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            names.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
    return names


def test_only_a_broker_adapter_names_a_state_changing_endpoint() -> None:
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT).as_posix()
        if relative in BROKER_ADAPTERS:
            continue
        literals = string_literals(ast.parse(path.read_text(encoding="utf-8")))
        for endpoint in sorted(MUTATING_ENDPOINTS):
            if any(endpoint in literal for literal in literals):
                offenders.append(f"{relative} references {endpoint}")
    assert offenders == [], "A submission endpoint escaped its adapter: " + "; ".join(offenders)


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        ("services/firstock/orders.py", {"placeOrder", "modifyOrder", "cancelOrder"}),
        ("services/upstox_orders.py", {"/v3/order/place", "/v3/order/cancel"}),
    ],
)
def test_each_adapter_does_name_its_own(adapter: str, expected: set[str]) -> None:
    """Guards the guard: a typo in a path above would make the test vacuous."""
    literals = string_literals(parse(adapter))
    found = {e for e in MUTATING_ENDPOINTS if any(e in literal for literal in literals)}
    assert expected <= found


@pytest.mark.parametrize("client", [FirstockReportClient, UpstoxReportClient])
@pytest.mark.parametrize("name", ["place_order", "modify_order", "cancel_order", "exit_order", "submit"])
def test_the_read_only_client_exposes_no_way_to_change_an_order(client: type, name: str) -> None:
    """This is what makes reconciliation and shadow evaluation provably safe.

    Both brokers, because an operator now chooses between them and a guarantee
    that holds at one of them is not a guarantee.
    """
    assert not hasattr(client, name)


@pytest.mark.parametrize("name", ["place_order", "modify_order", "cancel_order"])
def test_the_order_client_is_the_one_that_can(name: str) -> None:
    assert hasattr(FirstockOrderClient, name)


@pytest.mark.parametrize("name", ["place_order", "cancel_order"])
def test_the_upstox_order_client_is_the_one_that_can(name: str) -> None:
    """Guards the guard: without this, deleting place_order would look like a pass."""
    assert hasattr(UpstoxOrderClient, name)


@pytest.mark.parametrize("client", SUBMISSION_CLIENTS)
def test_only_the_gateway_names_a_submission_capable_client(client: str) -> None:
    """Asking "what can reach a broker with intent" should be one grep."""
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT).as_posix()
        if relative == CLIENT_FACTORY:
            continue
        if client in referenced_names(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(relative)
    assert offenders == [], f"{client} was named outside the gateway: " + "; ".join(offenders)


def test_the_gateway_does_name_them() -> None:
    """Guards the guard: a renamed class would make the fence above vacuous."""
    named = referenced_names(parse(CLIENT_FACTORY))
    assert set(SUBMISSION_CLIENTS) <= named


@pytest.mark.parametrize("caller", READ_ONLY_CALLERS)
def test_a_read_only_caller_imports_nothing_that_can_submit(caller: str) -> None:
    """Recovery is the one that matters most.

    The thing it is resolving is an order that may already exist, so code that
    could place one there turns a single uncertain order into two certain ones.
    """
    imported = imported_names(parse(caller))
    assert not (set(SUBMISSION_CLIENTS) & imported), f"{caller} imports a client that can submit"


@pytest.mark.parametrize("caller", READ_ONLY_CALLERS)
def test_a_read_only_caller_names_no_broker_at_all(caller: str) -> None:
    """Broker-neutral by construction, not by care.

    A module that knows which broker it is talking to is a module that will grow
    a second code path the day a second broker is selected, and the two will
    drift. These three work from the adapter's normalised records instead.
    """
    imported = imported_names(parse(caller))
    assert not ({"FirstockReportClient", "UpstoxReportClient"} & imported), f"{caller} names a broker's own client"


@pytest.mark.parametrize("relative", READ_ONLY_CALLERS)
@pytest.mark.parametrize("client", SUBMISSION_CLIENTS)
def test_state_reading_modules_never_mention_a_submission_client(relative: str, client: str) -> None:
    """Not even as a type annotation: the name should not be reachable there."""
    assert client not in (APP_ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["place_order", "cancel_order", "modify_order"])
def test_the_upstox_read_only_client_exposes_no_way_to_change_an_order(name: str) -> None:
    from app.services.upstox_orders import UpstoxReportClient

    assert not hasattr(UpstoxReportClient, name)


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
