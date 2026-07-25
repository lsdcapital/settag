"""Structural guards.

These assert nothing about behaviour. They exist because the CLI and the app
described the same batch differently once — the CLI counted genre evidence
while the app counted every task — and that class of bug is invisible in a
test of either UI on its own.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI_PACKAGES = ("src/settag/cli", "src/settag/tui")
MARKER = "# ui-count:"


def _ui_modules() -> list[Path]:
    modules = [path for package in UI_PACKAGES for path in sorted((REPO / package).rglob("*.py"))]
    assert modules, "found no UI modules to check; did the packages move?"
    return modules


def _sum_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sum"
    ]


def test_derived_counts_live_in_the_domain_layer() -> None:
    """A UI module may not aggregate over domain objects.

    Counts describing what SetTag will do or did belong to ``workflow`` or
    ``journal``, so both presentations read the same number. Aggregating over
    the UI's own state is fine; mark those with ``# ui-count: <reason>``.
    """
    offenders: list[str] = []
    for module in _ui_modules():
        source = module.read_text(encoding="utf-8")
        lines = source.splitlines()
        for call in _sum_calls(ast.parse(source)):
            # The marker may sit on the line above the call or anywhere inside it.
            start = max(call.lineno - 2, 0)
            if any(MARKER in line for line in lines[start : call.end_lineno]):
                continue
            relative = module.relative_to(REPO)
            offenders.append(f"{relative}:{call.lineno}: {lines[call.lineno - 1].strip()}")

    assert not offenders, (
        "These sum() calls are in a UI module:\n  "
        + "\n  ".join(offenders)
        + "\n\nIf the count describes a batch of writes or a track's metadata, move it "
        "to workflow.WriteSummary or a property on the domain object, so the CLI and "
        f"the app cannot disagree. If it counts the UI's own state, add '{MARKER} <reason>'."
    )


def test_every_ui_count_marker_gives_a_reason() -> None:
    """An unexplained marker is a silent opt-out, so require the reason."""
    unexplained: list[str] = []
    for module in _ui_modules():
        for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), start=1):
            if MARKER not in line:
                continue
            reason = line.split(MARKER, 1)[1].strip()
            if len(reason) < 10:
                unexplained.append(f"{module.relative_to(REPO)}:{number}")

    assert not unexplained, (
        f"These '{MARKER}' markers need a reason explaining what UI state is counted:\n  "
        + "\n  ".join(unexplained)
    )


def test_the_guard_would_catch_an_unmarked_aggregate(tmp_path: Path) -> None:
    """Guard the guard: an unmarked sum() must actually be detected."""
    module = tmp_path / "offender.py"
    module.write_text("total = sum(item.score for item in planned)\n", encoding="utf-8")

    calls = _sum_calls(ast.parse(module.read_text(encoding="utf-8")))

    assert len(calls) == 1
