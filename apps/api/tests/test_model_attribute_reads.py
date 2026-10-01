"""Every attribute read from an annotated model must exist on that model.

This exists because the same defect has now reached a live account twice.

    ExecutionReconciliation.findings_json   (no such column; the field is `findings`)
    LiveOrderSubmission.instrument_token    (no such column; it lives in a snapshot)

Both shipped with passing tests, and the tests passed for the same reason each
time: the code under test was handed a stand-in object, and the stand-in was
given whatever attribute the code reached for. A SimpleNamespace answers every
question you ask it. That makes such a test a check that one author spelled a
name the same way twice, not a check that the name exists.

The second one surfaced as "NOT PROTECTED: AttributeError" on a live short that
then had no stop behind it, which is as expensive as a typo gets.

So this reads the source instead. Where a parameter or variable is *annotated*
with a mapped class, every attribute taken from it must be one that class
actually provides. Annotation-driven rather than name-driven on purpose: a
heuristic that guessed the type from the variable name flagged seven reads in
this codebase and all seven were correct code -- `approval: TradeApprovalIntent`
is not a LiveOrderApproval, and an adapter's BrokerSubmission is not ours. A
check that cries wolf gets switched off.

What it cannot see, stated rather than implied: unannotated locals, anything
reached through a collection, getattr(), and attributes set dynamically. It is
a floor, not a proof.
"""

import ast
from pathlib import Path

import pytest

from app.db import models

APP = Path(__file__).resolve().parents[1] / "app"


def _mapped_classes() -> dict[str, set[str]]:
    found = {}
    for name in dir(models):
        obj = getattr(models, name)
        if isinstance(obj, type) and hasattr(obj, "__tablename__"):
            found[name] = {a for a in dir(obj) if not a.startswith("_")}
    return found


MODELS = _mapped_classes()


def _subscript_container(node: ast.Subscript) -> str:
    container = node.value
    return container.id if isinstance(container, ast.Name) else str(getattr(container, "attr", ""))


def _annotation_name(node: ast.expr | None) -> str | None:
    """The model named by an annotation, through Optional and unions only.

    Containers are deliberately NOT unwrapped here. Treating list[X] as X bound
    the *collection* to the element type, so the loop variable over it was never
    bound at all and the collection form went unchecked -- which the guard below
    caught, having been written to fail exactly when this walker stops seeing
    things.
    """
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id if node.id in MODELS else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):  # X | None
        return _annotation_name(node.left) or _annotation_name(node.right)
    if isinstance(node, ast.Subscript):
        if _subscript_container(node) not in {"Optional", "Union"}:
            return None
        if isinstance(node.slice, ast.Tuple):  # Union[X, None]
            for element in node.slice.elts:
                found = _annotation_name(element)
                if found:
                    return found
            return None
        return _annotation_name(node.slice)
    return None


def _element_name(node: ast.expr | None) -> str | None:
    """The model inside list[X] / Iterable[X] / Sequence[X], if any.

    Collections are worth following because the pattern that failed appears
    twice more that way: a function takes list[LiveOrderSubmission] and the
    loop variable over it carries no annotation of its own.
    """
    if isinstance(node, ast.Subscript) and _subscript_container(node) in {
        "list",
        "List",
        "Iterable",
        "Sequence",
        "Collection",
        "set",
        "Set",
    }:
        return _annotation_name(node.slice)
    return None


def _suspect_reads(source: str) -> list[tuple[int, str, str]]:
    tree = ast.parse(source)
    found: list[tuple[int, str, str]] = []

    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        bound: dict[str, str] = {}
        args = scope.args
        collections: dict[str, str] = {}
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            model = _annotation_name(arg.annotation)
            if model:
                bound[arg.arg] = model
                continue
            element = _element_name(arg.annotation)
            if element:
                collections[arg.arg] = element
        # A loop over an annotated collection binds its element type.
        for node in ast.walk(scope):
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                source_name = node.iter.id if isinstance(node.iter, ast.Name) else None
                if source_name and source_name in collections:
                    bound[node.target.id] = collections[source_name]
        for node in ast.walk(scope):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                model = _annotation_name(node.annotation)
                if model:
                    bound[node.target.id] = model
        if not bound:
            continue
        for node in ast.walk(scope):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                model = bound.get(node.value.id)
                if model and node.attr not in MODELS[model]:
                    found.append((node.lineno, f"{node.value.id}.{node.attr}", model))
    return found


@pytest.mark.parametrize("path", sorted(APP.rglob("*.py")), ids=lambda p: str(p.relative_to(APP)))
def test_annotated_model_reads_exist_on_the_model(path: Path) -> None:
    problems = _suspect_reads(path.read_text())
    assert not problems, "\n".join(
        f"{path.relative_to(APP)}:{line}  {read}  -- {model} has no such attribute" for line, read, model in problems
    )


def test_the_check_catches_the_bug_that_reached_production() -> None:
    """A guard on the guard.

    If the walker silently stopped finding anything -- an AST shape changed, an
    annotation form went unhandled -- every test above would pass by seeing
    nothing. This feeds it the exact code that failed live and requires a hit.
    """
    source = "async def _protect(submission: LiveOrderSubmission) -> None:\n    token = submission.instrument_token\n"
    assert _suspect_reads(source.replace("instrument_token", "no_such_field_at_all"))

    # And the real one must now pass, because the property exists.
    assert not _suspect_reads(source)

    # The collection form, which is how the other two call sites are written.
    looped = (
        "def _range(rows: list[LiveOrderSubmission]) -> None:\n"
        "    for submission in rows:\n"
        "        print(submission.no_such_field_at_all)\n"
    )
    assert _suspect_reads(looped), "a loop over an annotated collection must be followed"


def test_some_models_were_actually_discovered() -> None:
    """Zero models would make every assertion above vacuously true."""
    assert len(MODELS) > 20, f"only found {len(MODELS)} mapped classes"
    assert "instrument_token" in MODELS["LiveOrderSubmission"]
