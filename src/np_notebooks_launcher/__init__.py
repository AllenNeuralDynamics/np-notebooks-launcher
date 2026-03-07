"""Notebook launcher with experiment-type cell filtering.

Cell directives
---------------
Code cells:     ``# /// show-if: <namespace>=<expr>``
Markdown cells: ``<!-- /// show-if: <namespace>=<expr> -->``

``hide-if`` is also supported as an alternative to ``show-if``.

The namespace says *what* is being tested (e.g. ``experiment``, ``user``).
The expression after ``=`` is a boolean expression using ``and``, ``or``,
``not``, and names drawn from that namespace's context
(e.g. ``ephys``, ``hab``, ``opto``, ``optotagging``, ``pretest``,
``hab_day_1``).

Cells with no directive are always included.

Example
-------
::

    # /// show-if: experiment=(ephys or opto) and not pretest
    if (ephys or opto) and not pretest:
        ...
"""

from __future__ import annotations

import ast
import copy
import json
import pathlib
import re
import subprocess
import sys
import tkinter as tk
from typing import Any

# ---------------------------------------------------------------------------
# Directive parsing
# ---------------------------------------------------------------------------

_CODE_PREFIX = "# ///"
_MD_PREFIX = "<!-- ///"
_MD_SUFFIX = "-->"


def _first_line(cell: dict[str, Any]) -> str:
    source = cell.get("source", [])
    if isinstance(source, list):
        return source[0].rstrip("\n") if source else ""
    return source.split("\n")[0]


def parse_cell_directive(cell: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(directive_name, raw_condition)`` from the first line, or ``None``.

    The raw condition string may include a ``namespace=`` prefix
    (e.g. ``"experiment=ephys"``); callers should pass it through
    :func:`_strip_namespace` before evaluation.
    """
    line = _first_line(cell).strip()
    cell_type = cell.get("cell_type", "code")

    if cell_type == "code":
        if not line.startswith(_CODE_PREFIX):
            return None
        rest = line[len(_CODE_PREFIX):].strip()
    elif cell_type == "markdown":
        if not line.startswith(_MD_PREFIX):
            return None
        rest = line[len(_MD_PREFIX):]
        if rest.endswith(_MD_SUFFIX):
            rest = rest[: -len(_MD_SUFFIX)]
        rest = rest.strip()
    else:
        return None

    name, _, condition = rest.partition(":")
    return name.strip(), condition.strip()


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

ExperimentContext = dict[str, bool]


def _eval_node(node: ast.expr, ctx: ExperimentContext) -> bool:
    if isinstance(node, ast.Name):
        return bool(ctx.get(node.id, False))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_node(v, ctx) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_eval_node(v, ctx) for v in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, ctx)
    raise ValueError(f"Unsupported expression: {ast.dump(node)}")


def evaluate_condition(condition: str, ctx: ExperimentContext) -> bool:
    """Safely evaluate a bare boolean expression string against *ctx*.

    The expression must already have any ``namespace=`` prefix stripped
    (see :func:`_strip_namespace`). Uses AST parsing — no ``eval``.
    """
    tree = ast.parse(condition.strip(), mode="eval")
    return _eval_node(tree.body, ctx)


# ---------------------------------------------------------------------------
# Cell visibility
# ---------------------------------------------------------------------------


_NS_RE = re.compile(r"^(\w+)=(.+)$", re.DOTALL)


def _strip_namespace(condition: str) -> tuple[str, str]:
    """Return ``(namespace, bare_condition)`` by splitting on the first ``=``.

    ``"experiment=(ephys or opto) and not pretest"``
    → ``("experiment", "(ephys or opto) and not pretest")``

    Returns ``("", condition)`` if no ``namespace=`` prefix is found.
    """
    m = _NS_RE.match(condition.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return "", condition.strip()


def cell_is_visible(cell: dict[str, Any], ctx: ExperimentContext) -> bool:
    """Return ``True`` if this cell should be kept given *ctx*.

    Strips the ``namespace=`` prefix from the condition before evaluating,
    so ``experiment=ephys`` is treated the same as ``ephys`` for evaluation
    purposes (the namespace is informational).
    """
    directive = parse_cell_directive(cell)
    if directive is None:
        return True
    name, condition = directive
    if not condition:
        return True
    _namespace, bare = _strip_namespace(condition)
    result = evaluate_condition(bare, ctx)
    if name == "show-if":
        return result
    if name == "hide-if":
        return not result
    return True  # unknown directive → keep


# ---------------------------------------------------------------------------
# Notebook filtering
# ---------------------------------------------------------------------------


def filter_notebook(nb: dict[str, Any], ctx: ExperimentContext) -> dict[str, Any]:
    """Return a deep-copy of *nb* with cells filtered according to *ctx*."""
    nb = copy.deepcopy(nb)
    nb["cells"] = [c for c in nb["cells"] if cell_is_visible(c, ctx)]
    return nb


def load_notebook(path: str | pathlib.Path) -> dict[str, Any]:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def save_notebook(nb: dict[str, Any], path: str | pathlib.Path) -> None:
    pathlib.Path(path).write_text(json.dumps(nb, indent=1), encoding="utf-8")


def generate_filtered_notebook(
    source: str | pathlib.Path,
    ctx: ExperimentContext,
    output: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """Write a filtered copy of *source* and return its path."""
    source = pathlib.Path(source)
    nb = load_notebook(source)
    filtered = filter_notebook(nb, ctx)
    if output is None:
        tag = "_".join(k for k, v in ctx.items() if v) or "default"
        output = source.with_stem(f"{source.stem}_{tag}")
    output = pathlib.Path(output)
    save_notebook(filtered, output)
    return output


# ---------------------------------------------------------------------------
# Launcher GUI
# ---------------------------------------------------------------------------

# Predefined experiment-type presets.
# Keys match the boolean alias names used in dynamic_routing.ipynb cell 24.
EXPERIMENT_PRESETS: dict[str, ExperimentContext] = {
    "Ephys": {
        "ephys": True,
        "hab": False,
        "hab_day_1": False,
        "opto": False,
        "optotagging": False,
        "pretest": False,
    },
    "Ephys + Opto": {
        "ephys": True,
        "hab": False,
        "hab_day_1": False,
        "opto": True,
        "optotagging": True,
        "pretest": False,
    },
    "Hab": {
        "ephys": False,
        "hab": True,
        "hab_day_1": False,
        "opto": False,
        "optotagging": False,
        "pretest": False,
    },
    "Hab - day 1": {
        "ephys": False,
        "hab": True,
        "hab_day_1": True,
        "opto": False,
        "optotagging": False,
        "pretest": False,
    },
    "Pretest": {
        "ephys": True,
        "hab": False,
        "hab_day_1": False,
        "opto": False,
        "optotagging": False,
        "pretest": True,
    },
    "Behavior only": {
        "ephys": False,
        "hab": False,
        "hab_day_1": False,
        "opto": False,
        "optotagging": False,
        "pretest": False,
    },
}


def launch_notebook(path: str | pathlib.Path) -> None:
    """Open *path* in JupyterLab (non-blocking)."""
    subprocess.Popen(
        [sys.executable, "-m", "jupyter", "lab", str(path)],
        creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
    )


def run_launcher(notebook_path: str | pathlib.Path) -> None:
    """Open a GUI to select experiment type, then generate and launch a filtered notebook."""
    notebook_path = pathlib.Path(notebook_path)

    root = tk.Tk()
    root.title("Notebook Launcher")
    root.resizable(False, False)

    tk.Label(root, text="Select experiment type:", font=("TkDefaultFont", 11)).pack(
        padx=20, pady=(16, 4)
    )

    selected = tk.StringVar(value=next(iter(EXPERIMENT_PRESETS)))
    for name in EXPERIMENT_PRESETS:
        tk.Radiobutton(
            root, text=name, variable=selected, value=name, anchor="w"
        ).pack(fill="x", padx=32)

    def _launch() -> None:
        ctx = EXPERIMENT_PRESETS[selected.get()].copy()
        out = generate_filtered_notebook(notebook_path, ctx)
        launch_notebook(out)
        root.destroy()

    tk.Button(root, text="Launch", command=_launch, width=16).pack(pady=16)
    root.mainloop()


def main() -> None:
    """CLI entry point: ``np-notebooks-launcher [notebook_path]``."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Select an experiment type and launch a filtered Jupyter notebook."
    )
    parser.add_argument(
        "notebook",
        nargs="?",
        help="Path to the source .ipynb file (default: notebooks/dynamic_routing.ipynb "
        "relative to the package install location).",
    )
    args = parser.parse_args()

    if args.notebook:
        notebook_path = pathlib.Path(args.notebook)
    else:
        notebook_path = pathlib.Path(__file__).parent.parent.parent / "notebooks" / "dynamic_routing.ipynb"

    if not notebook_path.exists():
        parser.error(f"Notebook not found: {notebook_path}")

    run_launcher(notebook_path)
