"""Tests for np_notebooks_launcher."""
from __future__ import annotations

import ast
import pathlib

import pytest

from np_notebooks_launcher import (
    CellVariable,
    ExperimentContext,
    _modify_first_cell,
    _strip_directive,
    _strip_namespace,
    build_context_from_selections,
    cell_is_visible,
    evaluate_condition,
    filter_notebook,
    generate_filtered_notebook,
    load_notebook,
    parse_cell_directive,
    parse_first_cell_variables,
    save_notebook,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def code_cell(source: str | list[str]) -> dict:
    return {"cell_type": "code", "source": source, "metadata": {}, "outputs": []}


def md_cell(source: str | list[str]) -> dict:
    return {"cell_type": "markdown", "source": source, "metadata": {}}


def notebook(*cells) -> dict:
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": list(cells)}


# ---------------------------------------------------------------------------
# parse_cell_directive
# ---------------------------------------------------------------------------

class TestParseCellDirective:
    def test_code_show_if(self):
        cell = code_cell("# /// show-if: experiment=ephys\nif ephys:\n    pass\n")
        assert parse_cell_directive(cell) == ("show-if", "experiment=ephys")

    def test_code_hide_if(self):
        cell = code_cell(["# /// hide-if: experiment=pretest\n", "do_thing()\n"])
        assert parse_cell_directive(cell) == ("hide-if", "experiment=pretest")

    def test_code_complex_expr(self):
        cell = code_cell("# /// show-if: experiment=(ephys or opto) and not pretest\n")
        assert parse_cell_directive(cell) == (
            "show-if",
            "experiment=(ephys or opto) and not pretest",
        )

    def test_markdown_show_if(self):
        cell = md_cell("<!-- /// show-if: experiment=ephys -->\n## Section\n")
        assert parse_cell_directive(cell) == ("show-if", "experiment=ephys")

    def test_markdown_list_source(self):
        cell = md_cell(["<!-- /// show-if: experiment=hab -->\n", "## Hab\n"])
        assert parse_cell_directive(cell) == ("show-if", "experiment=hab")

    def test_no_directive_code(self):
        cell = code_cell("x = 1\n")
        assert parse_cell_directive(cell) is None

    def test_no_directive_markdown(self):
        cell = md_cell("## Just a heading\n")
        assert parse_cell_directive(cell) is None

    def test_empty_source(self):
        assert parse_cell_directive(code_cell("")) is None
        assert parse_cell_directive(code_cell([])) is None

    def test_raw_cell_ignored(self):
        cell = {"cell_type": "raw", "source": "# /// show-if: experiment=ephys\n"}
        assert parse_cell_directive(cell) is None


# ---------------------------------------------------------------------------
# _strip_namespace
# ---------------------------------------------------------------------------

class TestStripNamespace:
    def test_experiment_simple(self):
        assert _strip_namespace("experiment=ephys") == ("experiment", "ephys")

    def test_experiment_complex(self):
        ns, bare = _strip_namespace("experiment=(ephys or opto) and not pretest")
        assert ns == "experiment"
        assert bare == "(ephys or opto) and not pretest"

    def test_user_namespace(self):
        assert _strip_namespace("user=ben") == ("user", "ben")

    def test_no_namespace(self):
        assert _strip_namespace("ephys") == ("", "ephys")

    def test_strips_whitespace(self):
        assert _strip_namespace("  experiment=ephys  ") == ("experiment", "ephys")

    def test_equals_only_in_value(self):
        # namespace= prefix ends at first =; value may not contain = but check robustness
        ns, bare = _strip_namespace("experiment=hab or (ephys and not pretest)")
        assert ns == "experiment"
        assert bare == "hab or (ephys and not pretest)"


# ---------------------------------------------------------------------------
# _strip_directive
# ---------------------------------------------------------------------------

class TestStripDirective:
    def test_removes_directive_from_code(self):
        cell = code_cell("# /// show-if: experiment=ephys\nif ephys:\n    do_thing()\n")
        result = _strip_directive(cell)
        assert result["source"] == "if ephys:\n    do_thing()\n"

    def test_removes_directive_from_markdown(self):
        cell = md_cell("<!-- /// show-if: experiment=ephys -->\n## Ephys section\n")
        result = _strip_directive(cell)
        assert result["source"] == "## Ephys section\n"

    def test_no_directive_unchanged(self):
        cell = code_cell("x = 1\n")
        result = _strip_directive(cell)
        assert result["source"] == "x = 1\n"

    def test_list_source_remains_list(self):
        cell = code_cell(["# /// show-if: experiment=ephys\n", "if ephys:\n", "    do_thing()\n"])
        result = _strip_directive(cell)
        assert isinstance(result["source"], list)
        assert "".join(result["source"]) == "if ephys:\n    do_thing()\n"


# ---------------------------------------------------------------------------
# evaluate_condition
# ---------------------------------------------------------------------------

class TestEvaluateCondition:
    CTX: ExperimentContext = {
        "ephys": True,
        "hab": False,
        "opto": False,
        "optotagging": False,
        "pretest": False,
        "hab_day_1": False,
    }

    def test_simple_true(self):
        assert evaluate_condition("ephys", self.CTX) is True

    def test_simple_false(self):
        assert evaluate_condition("hab", self.CTX) is False

    def test_unknown_name_is_false(self):
        assert evaluate_condition("unknown_type", self.CTX) is False

    def test_and_true(self):
        assert evaluate_condition("ephys and not pretest", self.CTX) is True

    def test_and_false(self):
        assert evaluate_condition("ephys and hab", self.CTX) is False

    def test_or_true(self):
        assert evaluate_condition("ephys or opto", self.CTX) is True

    def test_or_false(self):
        assert evaluate_condition("hab or opto", self.CTX) is False

    def test_not(self):
        assert evaluate_condition("not hab", self.CTX) is True
        assert evaluate_condition("not ephys", self.CTX) is False

    def test_complex(self):
        assert evaluate_condition("(ephys or opto) and not pretest", self.CTX) is True

    def test_complex_false(self):
        ctx = {**self.CTX, "pretest": True}
        assert evaluate_condition("(ephys or opto) and not pretest", ctx) is False

    def test_hab_or_ephys_not_pretest(self):
        assert evaluate_condition("hab or (ephys and not pretest)", self.CTX) is True

    def test_invalid_expression_raises(self):
        with pytest.raises(Exception):
            evaluate_condition("ephys + opto", self.CTX)  # + not supported


# ---------------------------------------------------------------------------
# cell_is_visible
# ---------------------------------------------------------------------------

class TestCellIsVisible:
    CTX: ExperimentContext = {
        "ephys": True,
        "hab": False,
        "opto": False,
        "optotagging": False,
        "pretest": False,
        "hab_day_1": False,
    }

    def test_no_directive_always_visible(self):
        assert cell_is_visible(code_cell("x = 1\n"), self.CTX) is True
        assert cell_is_visible(md_cell("## Heading\n"), self.CTX) is True

    def test_show_if_true(self):
        cell = code_cell("# /// show-if: experiment=ephys\npass\n")
        assert cell_is_visible(cell, self.CTX) is True

    def test_show_if_false(self):
        cell = code_cell("# /// show-if: experiment=hab\npass\n")
        assert cell_is_visible(cell, self.CTX) is False

    def test_hide_if_true_hides_cell(self):
        cell = code_cell("# /// hide-if: experiment=pretest\npass\n")
        assert cell_is_visible(cell, self.CTX) is True  # pretest=False → not hidden

    def test_hide_if_false_hides_cell(self):
        ctx = {**self.CTX, "pretest": True}
        cell = code_cell("# /// hide-if: experiment=pretest\npass\n")
        assert cell_is_visible(cell, ctx) is False  # pretest=True → hidden

    def test_show_if_markdown(self):
        cell = md_cell("<!-- /// show-if: experiment=ephys -->\n## Ephys only\n")
        assert cell_is_visible(cell, self.CTX) is True

    def test_show_if_markdown_false(self):
        cell = md_cell("<!-- /// show-if: experiment=hab -->\n## Hab only\n")
        assert cell_is_visible(cell, self.CTX) is False

    def test_unknown_directive_visible(self):
        cell = code_cell("# /// future-directive: experiment=ephys\npass\n")
        assert cell_is_visible(cell, self.CTX) is True

    def test_complex_condition(self):
        cell = code_cell("# /// show-if: experiment=(ephys or opto) and not pretest\n")
        assert cell_is_visible(cell, self.CTX) is True
        ctx_pretest = {**self.CTX, "pretest": True}
        assert cell_is_visible(cell, ctx_pretest) is False


# ---------------------------------------------------------------------------
# parse_first_cell_variables
# ---------------------------------------------------------------------------

class TestParseFirstCellVariables:
    def test_single_string_literal(self):
        cell = code_cell(
            '# comment\n_experiment: Literal["pretest", "ephys", "opto"] = "pretest"\n'
        )
        result = parse_first_cell_variables(cell)
        assert len(result) == 1
        assert result[0] == CellVariable(
            name="_experiment",
            options=("pretest", "ephys", "opto"),
            default="pretest",
        )

    def test_bool_literal(self):
        cell = code_cell("_flag: Literal[True, False] = True\n")
        result = parse_first_cell_variables(cell)
        assert len(result) == 1
        assert result[0].options == (True, False)
        assert result[0].default is True

    def test_int_literal(self):
        cell = code_cell("_count: Literal[1, 2, 3] = 2\n")
        result = parse_first_cell_variables(cell)
        assert len(result) == 1
        assert result[0].options == (1, 2, 3)
        assert result[0].default == 2

    def test_multiple_variables(self):
        cell = code_cell(
            '# comment\n'
            '_experiment: Literal["a", "b"] = "a"\n'
            '_mode: Literal[1, 2] = 1\n'
        )
        result = parse_first_cell_variables(cell)
        assert len(result) == 2
        assert result[0].name == "_experiment"
        assert result[1].name == "_mode"

    def test_no_underscore_prefix_skipped(self):
        cell = code_cell('experiment: Literal["a", "b"] = "a"\n')
        result = parse_first_cell_variables(cell)
        assert result == []

    def test_no_literal_annotation_skipped(self):
        cell = code_cell("_x: str = 'hello'\n")
        result = parse_first_cell_variables(cell)
        assert result == []

    def test_no_default_skipped(self):
        cell = code_cell('_x: Literal["a", "b"]\n')
        result = parse_first_cell_variables(cell)
        assert result == []

    def test_empty_cell(self):
        assert parse_first_cell_variables(code_cell("")) == []
        assert parse_first_cell_variables(code_cell([])) == []

    def test_syntax_error_returns_empty(self):
        cell = code_cell("this is not valid python {{{\n")
        assert parse_first_cell_variables(cell) == []

    def test_list_source(self):
        cell = code_cell(['# comment\n', '_x: Literal["a", "b"] = "a"\n'])
        result = parse_first_cell_variables(cell)
        assert len(result) == 1
        assert result[0].name == "_x"


# ---------------------------------------------------------------------------
# _modify_first_cell
# ---------------------------------------------------------------------------

class TestModifyFirstCell:
    def test_replaces_cell_with_markdown_table(self):
        nb = notebook(code_cell(
            '# variables in this cell can be injected by launcher:\n'
            '_experiment: Literal["pretest", "ephys"] = "pretest"\n'
        ))
        _modify_first_cell(nb, {"_experiment": "ephys"})
        cell = nb["cells"][0]
        assert cell["cell_type"] == "markdown"
        src = cell["source"]
        assert "modified by the launcher" in src
        assert "_experiment" in src
        assert "'ephys'" in src

    def test_preserves_bool_type(self):
        nb = notebook(code_cell(
            '# comment\n'
            '_flag: Literal[True, False] = True\n'
        ))
        _modify_first_cell(nb, {"_flag": False})
        src = nb["cells"][0]["source"]
        assert "_flag" in src
        assert "False" in src

    def test_preserves_int_type(self):
        nb = notebook(code_cell(
            '# comment\n'
            '_count: Literal[1, 2, 3] = 1\n'
        ))
        _modify_first_cell(nb, {"_count": 42})
        src = nb["cells"][0]["source"]
        assert "_count" in src
        assert "42" in src

    def test_no_selections_produces_empty_table(self):
        nb = notebook(code_cell(
            '# comment\n'
            '_experiment: Literal["a", "b"] = "a"\n'
        ))
        _modify_first_cell(nb, {})
        cell = nb["cells"][0]
        assert cell["cell_type"] == "markdown"
        assert "modified by the launcher" in cell["source"]

    def test_multiple_variables_in_table(self):
        nb = notebook(code_cell(
            '_x: Literal["a"] = "a"\n'
            '_y: Literal[1, 2] = 1\n'
        ))
        _modify_first_cell(nb, {"_x": "a", "_y": 2})
        src = nb["cells"][0]["source"]
        assert "_x" in src
        assert "_y" in src

    def test_empty_notebook(self):
        nb = notebook()
        _modify_first_cell(nb, {"_x": "a"})  # should not raise


# ---------------------------------------------------------------------------
# filter_notebook with variable_selections
# ---------------------------------------------------------------------------

class TestFilterNotebookWithSelections:
    CTX_EPHYS: ExperimentContext = {
        "ephys": True, "hab": False, "opto": False,
        "optotagging": False, "pretest": False, "hab_day_1": False,
    }

    def test_modifies_first_cell(self):
        nb = notebook(
            code_cell('# comment\n_experiment: Literal["a", "b"] = "a"\n'),
            code_cell("x = 1\n"),
        )
        filtered = filter_notebook(nb, {}, variable_selections={"_experiment": "b"})
        cell = filtered["cells"][0]
        assert cell["cell_type"] == "markdown"
        assert "_experiment" in cell["source"]
        assert "'b'" in cell["source"]
        assert "modified by the launcher" in cell["source"]

    def test_no_selections_leaves_unchanged(self):
        nb = notebook(md_cell("# Title\n"), code_cell("x = 1\n"))
        filtered = filter_notebook(nb, self.CTX_EPHYS)
        assert len(filtered["cells"]) == 2
        assert filtered["cells"][0]["source"] == "# Title\n"


# ---------------------------------------------------------------------------
# build_context_from_selections
# ---------------------------------------------------------------------------

class TestBuildContextFromSelections:
    EXPERIMENT_VAR = CellVariable(
        name="_experiment",
        options=("pretest", "ephys", "opto", "hab", "training"),
        default="pretest",
    )

    def test_selected_option_is_true(self):
        ctx = build_context_from_selections([self.EXPERIMENT_VAR], {"_experiment": "hab"})
        assert ctx["hab"] is True

    def test_other_options_are_false(self):
        ctx = build_context_from_selections([self.EXPERIMENT_VAR], {"_experiment": "hab"})
        for opt in ("pretest", "ephys", "opto", "training"):
            assert ctx[opt] is False

    def test_ephys_selected(self):
        ctx = build_context_from_selections([self.EXPERIMENT_VAR], {"_experiment": "ephys"})
        assert ctx["ephys"] is True
        assert ctx["hab"] is False

    def test_missing_selection_all_false(self):
        ctx = build_context_from_selections([self.EXPERIMENT_VAR], {})
        assert all(not v for v in ctx.values())

    def test_non_string_options_excluded(self):
        var = CellVariable(name="_flag", options=(True, False), default=True)
        ctx = build_context_from_selections([var], {"_flag": True})
        assert ctx == {}  # booleans are not added as context keys

    def test_hab_cell_visible_when_hab_selected(self):
        """Regression: show-if: _experiment=hab or (ephys and not pretest) with hab selected."""
        ctx = build_context_from_selections([self.EXPERIMENT_VAR], {"_experiment": "hab"})
        cell = code_cell("# /// show-if: _experiment=hab or (ephys and not pretest)\npass\n")
        assert cell_is_visible(cell, ctx) is True

    def test_hab_cell_hidden_when_ephys_pretest_selected(self):
        ctx = build_context_from_selections([self.EXPERIMENT_VAR], {"_experiment": "pretest"})
        cell = code_cell("# /// show-if: _experiment=hab or (ephys and not pretest)\npass\n")
        assert cell_is_visible(cell, ctx) is False

    def test_hab_cell_visible_when_ephys_selected(self):
        ctx = build_context_from_selections([self.EXPERIMENT_VAR], {"_experiment": "ephys"})
        cell = code_cell("# /// show-if: _experiment=hab or (ephys and not pretest)\npass\n")
        assert cell_is_visible(cell, ctx) is True


# ---------------------------------------------------------------------------
# filter_notebook
# ---------------------------------------------------------------------------

class TestFilterNotebook:
    CTX_EPHYS: ExperimentContext = {
        "ephys": True, "hab": False, "opto": False,
        "optotagging": False, "pretest": False, "hab_day_1": False,
    }
    CTX_HAB: ExperimentContext = {
        "ephys": False, "hab": True, "opto": False,
        "optotagging": False, "pretest": False, "hab_day_1": False,
    }

    def _nb(self):
        return notebook(
            md_cell("## Always visible\n"),
            code_cell("# /// show-if: experiment=ephys\ncheck_ephys()\n"),
            code_cell("# /// show-if: experiment=hab\ncheck_hab()\n"),
            code_cell("x = 1\n"),  # no directive
        )

    def test_ephys_context(self):
        filtered = filter_notebook(self._nb(), self.CTX_EPHYS)
        assert len(filtered["cells"]) == 3  # always + ephys + no-directive

    def test_hab_context(self):
        filtered = filter_notebook(self._nb(), self.CTX_HAB)
        assert len(filtered["cells"]) == 3  # always + hab + no-directive

    def test_does_not_mutate_original(self):
        nb = self._nb()
        original_count = len(nb["cells"])
        filter_notebook(nb, self.CTX_EPHYS)
        assert len(nb["cells"]) == original_count

    def test_all_cells_hidden(self):
        nb = notebook(
            code_cell("# /// show-if: experiment=ephys\npass\n"),
            code_cell("# /// show-if: experiment=ephys\npass\n"),
        )
        ctx = {**self.CTX_EPHYS, "ephys": False}
        filtered = filter_notebook(nb, ctx)
        assert filtered["cells"] == []

    def test_markdown_pairs_filtered_together(self):
        """Markdown + code cells with matching directives both hide."""
        nb = notebook(
            md_cell("<!-- /// show-if: experiment=hab -->\n## Hab section\n"),
            code_cell("# /// show-if: experiment=hab\nhab_thing()\n"),
            code_cell("always()\n"),
        )
        filtered = filter_notebook(nb, self.CTX_EPHYS)
        assert len(filtered["cells"]) == 1
        assert filtered["cells"][0]["source"] == "always()\n"


# ---------------------------------------------------------------------------
# load_notebook / save_notebook / generate_filtered_notebook
# ---------------------------------------------------------------------------

class TestIO:
    def test_round_trip(self, tmp_path):
        nb = notebook(code_cell("x = 1\n"), md_cell("## Hi\n"))
        path = tmp_path / "test.ipynb"
        save_notebook(nb, path)
        loaded = load_notebook(path)
        assert loaded["cells"][0]["source"] == "x = 1\n"

    def test_generate_filtered_notebook(self, tmp_path):
        nb = notebook(
            code_cell("always()\n"),
            code_cell("# /// show-if: experiment=ephys\nephys_only()\n"),
        )
        src = tmp_path / "source.ipynb"
        save_notebook(nb, src)

        ctx: ExperimentContext = {
            "ephys": False, "hab": True, "opto": False,
            "optotagging": False, "pretest": False, "hab_day_1": True,
        }
        out = generate_filtered_notebook(src, ctx)
        assert out.exists()
        result = load_notebook(out)
        assert len(result["cells"]) == 1
        assert result["cells"][0]["source"] == "always()\n"

    def test_generate_custom_output_path(self, tmp_path):
        nb = notebook(code_cell("x = 1\n"))
        src = tmp_path / "nb.ipynb"
        save_notebook(nb, src)
        out_path = tmp_path / "custom_output.ipynb"
        out = generate_filtered_notebook(src, {"ephys": True}, output=out_path)
        assert out == out_path
        assert out_path.exists()

    def test_generate_output_name_from_context(self, tmp_path):
        nb = notebook(code_cell("x = 1\n"))
        src = tmp_path / "nb.ipynb"
        save_notebook(nb, src)
        ctx: ExperimentContext = {"ephys": True, "hab": False, "opto": True}
        out = generate_filtered_notebook(src, ctx)
        assert "ephys" in out.name
        assert "opto" in out.name

    def test_generate_with_variable_selections(self, tmp_path):
        nb = notebook(code_cell(
            '# comment\n_experiment: Literal["a", "b"] = "a"\n'
        ))
        src = tmp_path / "nb.ipynb"
        save_notebook(nb, src)
        out = generate_filtered_notebook(
            src, {}, output=tmp_path / "out.ipynb",
            variable_selections={"_experiment": "b"},
        )
        result = load_notebook(out)
        cell = result["cells"][0]
        assert cell["cell_type"] == "markdown"
        assert "_experiment" in cell["source"]
        assert "'b'" in cell["source"]
        assert "modified by the launcher" in cell["source"]


# ---------------------------------------------------------------------------
# Integration: dynamic_routing.ipynb
# ---------------------------------------------------------------------------

NOTEBOOK_PATH = pathlib.Path(__file__).parent.parent / "notebooks" / "dynamic_routing.ipynb"


@pytest.mark.skipif(not NOTEBOOK_PATH.exists(), reason="notebook not found")
class TestDynamicRoutingNotebook:
    def test_loads(self):
        nb = load_notebook(NOTEBOOK_PATH)
        assert nb["cells"]

    def test_first_cell_has_variables(self):
        nb = load_notebook(NOTEBOOK_PATH)
        variables = parse_first_cell_variables(nb["cells"][0])
        assert len(variables) >= 1
        assert variables[0].name == "_experiment"
        assert len(variables[0].options) >= 2
        assert variables[0].default in variables[0].options

    def test_filter_with_each_option(self):
        nb = load_notebook(NOTEBOOK_PATH)
        variables = parse_first_cell_variables(nb["cells"][0])
        # Different options must produce different cell counts (filtering is actually happening).
        counts: dict[str, int] = {}
        for var in variables:
            for opt in var.options:
                ctx = build_context_from_selections(variables, {var.name: opt})
                filtered = filter_notebook(nb, ctx, variable_selections={var.name: opt})
                assert len(filtered["cells"]) > 0, f"No cells for {var.name}={opt}"
                counts[str(opt)] = len(filtered["cells"])
        assert len(set(counts.values())) > 1, "All options produced the same cell count — filtering is not working"

    def test_generate_for_each_option(self, tmp_path):
        nb = load_notebook(NOTEBOOK_PATH)
        variables = parse_first_cell_variables(nb["cells"][0])
        for var in variables:
            for opt in var.options:
                ctx = build_context_from_selections(variables, {var.name: opt})
                out = generate_filtered_notebook(
                    NOTEBOOK_PATH, ctx,
                    output=tmp_path / f"{var.name}_{opt}.ipynb",
                    variable_selections={var.name: opt},
                )
                result = load_notebook(out)
                assert result["cells"], f"No cells for {var.name}={opt}"
                cell = result["cells"][0]
                assert cell["cell_type"] == "markdown"
                assert "modified by the launcher" in cell["source"]

    def test_hab_injection_widget_visible(self):
        """Regression: show-if: _experiment=hab or (ephys and not pretest)."""
        nb = load_notebook(NOTEBOOK_PATH)
        variables = parse_first_cell_variables(nb["cells"][0])

        def has_injection_widget(opt: str) -> bool:
            ctx = build_context_from_selections(variables, {"_experiment": opt})
            filtered = filter_notebook(nb, ctx)
            return any(
                "InjectionWidget" in (
                    "".join(c.get("source", [])) if isinstance(c.get("source"), list)
                    else c.get("source", "")
                )
                for c in filtered["cells"]
            )

        assert has_injection_widget("hab"), "InjectionWidget cell missing for hab"
        assert has_injection_widget("ephys"), "InjectionWidget cell missing for ephys"
        assert not has_injection_widget("pretest"), "InjectionWidget cell should be absent for pretest"
        assert not has_injection_widget("opto"), "InjectionWidget cell should be absent for opto"
        assert not has_injection_widget("training"), "InjectionWidget cell should be absent for training"
