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

import argparse
import ast
import copy
import dataclasses
import importlib.metadata
import json
import pathlib
import re
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk
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
        rest = line[len(_CODE_PREFIX) :].strip()
    elif cell_type == "markdown":
        if not line.startswith(_MD_PREFIX):
            return None
        rest = line[len(_MD_PREFIX) :]
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


def _strip_directive(cell: dict[str, Any]) -> dict[str, Any]:
    """Remove the directive comment from *cell*.

    Assumes *cell* is already a writable copy. Returns *cell* for chaining.

    Markdown cells only have the directive line removed.
    """
    if parse_cell_directive(cell) is None:
        return cell

    source = cell.get("source", [])
    is_list = isinstance(source, list)
    text = "".join(source) if is_list else source
    lines = text.splitlines(keepends=True)
    if not lines:
        return cell

    # Remove directive comment (first line)
    lines = lines[1:]
    new_text = "".join(lines)
    cell["source"] = new_text.splitlines(keepends=True) if is_list else new_text
    return cell


# ---------------------------------------------------------------------------
# First-cell variable parsing
# ---------------------------------------------------------------------------

_OptionValue = str | bool | int | float


@dataclasses.dataclass
class CellVariable:
    """A variable declared with a ``Literal`` type annotation in a notebook cell."""

    name: str
    options: tuple[_OptionValue, ...]
    default: _OptionValue


def parse_first_cell_variables(cell: dict[str, Any]) -> list[CellVariable]:
    """Extract ``Literal``-annotated variables from *cell*'s source code.

    Each ``name: Literal[opt1, opt2, ...] = default`` line becomes a
    :class:`CellVariable`.  Only variables whose name starts with ``_`` are
    included (matching the notebook convention for launcher-injectable vars).
    """
    source = cell.get("source", [])
    text = "".join(source) if isinstance(source, list) else source
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    results: list[CellVariable] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not isinstance(target, ast.Name) or not target.id.startswith("_"):
            continue
        ann = node.annotation
        if not (
            isinstance(ann, ast.Subscript)
            and isinstance(ann.value, ast.Name)
            and ann.value.id == "Literal"
        ):
            continue
        # Extract options from the Literal slice
        slice_node = ann.slice
        if isinstance(slice_node, ast.Tuple):
            elts = slice_node.elts
        else:
            elts = [slice_node]
        options = tuple(e.value for e in elts if isinstance(e, ast.Constant))
        if not options:
            continue
        # Extract default
        if node.value is None or not isinstance(node.value, ast.Constant):
            continue
        results.append(
            CellVariable(
                name=target.id,
                options=tuple(
                    v for v in options if isinstance(v, (str, bool, int, float))
                ),
                default=(
                    node.value.value
                    if isinstance(node.value.value, (str, bool, int, float))
                    else str(node.value.value)
                ),
            )
        )
    return results


_LITERAL_LINE_RE = re.compile(r"^(\s*_\w+)\s*:.*Literal\[.*\]\s*=.*$")


def _modify_first_cell(
    nb: dict[str, Any],
    selections: dict[str, _OptionValue],
) -> None:
    """Replace cell 0 of *nb* in-place with a markdown table showing injected values."""
    if not nb["cells"]:
        return
    rows = "\n".join(f"| `{var}` | `{repr(val)}` |" for var, val in selections.items())
    source = (
        "*This notebook was modified by the launcher according to the following config:*\n\n"
        "| Variable | Value |\n"
        "|---|---|\n"
        f"{rows}"
    )
    cell = nb["cells"][0]
    cell["cell_type"] = "markdown"
    cell.pop("execution_count", None)
    cell.pop("outputs", None)
    cell["source"] = source


def build_context_from_selections(
    variables: list[CellVariable],
    selections: dict[str, _OptionValue],
) -> ExperimentContext:
    """Build an :class:`ExperimentContext` from user-selected variable values.

    For each string-valued :class:`CellVariable`, every option becomes a key
    in the context; the key is ``True`` only for the selected option.

    Example: ``_experiment`` with options ``("pretest", "ephys", "hab")`` and
    selected value ``"hab"`` produces
    ``{"pretest": False, "ephys": False, "hab": True}``.
    """
    ctx: ExperimentContext = {}
    for var in variables:
        selected = selections.get(var.name)
        for opt in var.options:
            if isinstance(opt, str):
                ctx[opt] = opt == selected
    return ctx


def filter_notebook(
    nb: dict[str, Any],
    ctx: ExperimentContext,
    variable_selections: dict[str, _OptionValue] | None = None,
) -> dict[str, Any]:
    """Return a deep-copy of *nb* with cells filtered and cleaned according to *ctx*."""
    nb = copy.deepcopy(nb)
    nb["cells"] = [_strip_directive(c) for c in nb["cells"] if cell_is_visible(c, ctx)]
    if variable_selections is not None:
        _modify_first_cell(nb, variable_selections)
    return nb


def load_notebook(path: str | pathlib.Path) -> dict[str, Any]:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def save_notebook(nb: dict[str, Any], path: str | pathlib.Path) -> None:
    pathlib.Path(path).write_text(json.dumps(nb, indent=1), encoding="utf-8")


def generate_filtered_notebook(
    source: str | pathlib.Path,
    ctx: ExperimentContext,
    output: str | pathlib.Path | None = None,
    variable_selections: dict[str, _OptionValue] | None = None,
    overwrite: bool = False,
) -> pathlib.Path:
    """Write a filtered copy of *source* and return its path.

    If *overwrite* is ``True`` the result is written back to *source* and
    *output* is ignored.
    """
    source = pathlib.Path(source)
    nb = load_notebook(source)
    filtered = filter_notebook(nb, ctx, variable_selections=variable_selections)
    if overwrite:
        output = source
    elif output is None:
        tag = "_".join(k for k, v in ctx.items() if v) or "modified"
        output = source.with_stem(f"{source.stem}_{tag}")
    output = pathlib.Path(output)
    save_notebook(filtered, output)
    return output


# ---------------------------------------------------------------------------
# Launcher GUI
# ---------------------------------------------------------------------------


def launch_notebook(path: str | pathlib.Path) -> None:
    """Open *path* within JupyterLab (non-blocking)."""
    subprocess.Popen(
        [r"C:\JupyterLab\JupyterLab.exe", str(path)],
        creationflags=subprocess.DETACHED_PROCESS if sys.platform.startswith("win") else 0,
    )


def run_launcher(notebook_path: str | pathlib.Path) -> None:
    """Open a GUI to select variable values, then generate and launch a filtered notebook."""
    notebook_path = pathlib.Path(notebook_path)
    nb = load_notebook(notebook_path)
    variables = parse_first_cell_variables(nb["cells"][0]) if nb["cells"] else []

    root = tk.Tk()
    root.title("Notebook Launcher")
    root.resizable(False, False)

    # Build a dropdown (Combobox) for each Literal-typed variable in cell 0.
    # Map display strings back to typed values for each variable.
    tk_vars: dict[str, tk.StringVar] = {}
    option_maps: dict[str, dict[str, _OptionValue]] = {}

    if variables:
        tk.Label(
            root,
            text="Select options:",
            font=("TkDefaultFont", 11),
        ).pack(padx=20, pady=(16, 4))

        for var in variables:
            frame = tk.Frame(root)
            frame.pack(fill="x", padx=20, pady=2)
            tk.Label(frame, text=f"{var.name}:").pack(side="left", padx=(12, 4))

            display_to_value = {str(opt): opt for opt in var.options}
            option_maps[var.name] = display_to_value
            display_options = list(display_to_value.keys())

            sv = tk.StringVar(value=str(var.default))
            tk_vars[var.name] = sv

            combo = ttk.Combobox(
                frame,
                textvariable=sv,
                values=display_options,
                state="readonly",
            )
            combo.pack(side="left", fill="x", expand=True, padx=4)
    else:
        tk.Label(
            root,
            text="No configurable variables found.",
            font=("TkDefaultFont", 11),
        ).pack(padx=20, pady=(16, 4))

    overwrite_var = tk.BooleanVar(value=True)

    def _on_overwrite_toggle(*_: object) -> None:
        suffix_entry.config(state="disabled" if overwrite_var.get() else "normal")

    overwrite_check = tk.Checkbutton(
        root,
        text="Overwrite original file",
        variable=overwrite_var,
        command=_on_overwrite_toggle,
    )
    overwrite_check.pack(pady=(0, 4))

    suffix_frame = tk.Frame(root)
    suffix_frame.pack(fill="x", padx=20, pady=(0, 4))
    tk.Label(suffix_frame, text="Custom path:").pack(side="left", padx=(0, 4))
    suffix_var = tk.StringVar()
    suffix_entry = tk.Entry(suffix_frame, textvariable=suffix_var, state="disabled")
    suffix_entry.pack(side="left", fill="x", expand=True)

    def _update_suffix(*_: object) -> None:
        suffix_var.set("_".join(sv.get() for sv in tk_vars.values()))

    for sv in tk_vars.values():
        sv.trace_add("write", _update_suffix)
    _update_suffix()

    def _launch() -> None:
        selections: dict[str, _OptionValue] = {}
        for name, sv in tk_vars.items():
            display_val = sv.get()
            selections[name] = option_maps[name][display_val]
        overwrite = overwrite_var.get()
        suffix = suffix_var.get().strip()
        output: pathlib.Path | None = None
        if not overwrite and suffix:
            output = notebook_path.with_stem(
                f"{notebook_path.stem}_{suffix.removeprefix('_')}"
            )
        ctx = build_context_from_selections(variables, selections)
        out = generate_filtered_notebook(
            notebook_path,
            ctx,
            output=output,
            variable_selections=selections or None,
            overwrite=overwrite,
        )
        launch_notebook(out)
        root.destroy()

    def _reset_update() -> None:
        bat = notebook_path.parent.parent / "reset_update_launch.bat"
        if not bat.exists():
            messagebox.showerror("Not found", f"Reset script not found:\n{bat}")
            return
        if not messagebox.askyesno(
            "Reset & Update",
            "This will reset the repository to origin/main and update the Python "
            "environment.\n\nContinue?",
        ):
            return
        repo_path = "c:/users/svc_neuropix/documents/github/np_notebooks"
        cmds = (
            "git fetch origin"
            " && git reset --hard origin/main"
            " && uv sync --python 3.11"
            " && pause"
        )
        p = subprocess.Popen(
            ["cmd", "/c", cmds],
            cwd=repo_path,
            creationflags=(
                subprocess.CREATE_NEW_CONSOLE if sys.platform.startswith("win") else 0
            ),
        )
        p.wait()
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=16)
    tk.Button(btn_frame, text="Launch", command=_launch, width=16).pack(
        side="left", padx=4
    )
    tk.Button(btn_frame, text="Reset & Update", command=_reset_update, width=16).pack(
        side="left", padx=4
    )
    root.bind("<Return>", lambda _: _launch())
    root.mainloop()


def main() -> None:
    """CLI entry point: ``np-notebooks-launcher [notebook_path]``."""
    print(
        f"np-notebooks-launcher {importlib.metadata.version('np-notebooks-launcher')}"
    )

    parser = argparse.ArgumentParser(
        description="Select an experiment type and launch a filtered Jupyter notebook."
    )
    parser.add_argument(
        "path",
        help="Path to the source .ipynb file (default: notebooks/dynamic_routing.ipynb "
        "relative to the package install location).",
    )
    args = parser.parse_args()

    notebook_path = pathlib.Path(args.path)

    if not notebook_path.exists():
        parser.error(f"Notebook not found: {notebook_path}")

    run_launcher(notebook_path)
