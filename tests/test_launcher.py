"""Tests for np_notebooks_launcher."""
from __future__ import annotations

import json
import pathlib

import pytest

from np_notebooks_launcher import (
    ExperimentContext,
    EXPERIMENT_PRESETS,
    _strip_namespace,
    cell_is_visible,
    evaluate_condition,
    filter_notebook,
    generate_filtered_notebook,
    load_notebook,
    parse_cell_directive,
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


# ---------------------------------------------------------------------------
# EXPERIMENT_PRESETS
# ---------------------------------------------------------------------------

class TestExperimentPresets:
    def test_all_presets_present(self):
        expected = {"Ephys", "Ephys + Opto", "Hab", "Hab - day 1", "Pretest", "Behavior only"}
        assert set(EXPERIMENT_PRESETS.keys()) == expected

    def test_ephys_preset(self):
        ctx = EXPERIMENT_PRESETS["Ephys"]
        assert ctx["ephys"] is True
        assert ctx["hab"] is False
        assert ctx["pretest"] is False

    def test_pretest_has_ephys_true(self):
        # pretest implies ephys probes are inserted
        ctx = EXPERIMENT_PRESETS["Pretest"]
        assert ctx["ephys"] is True
        assert ctx["pretest"] is True

    def test_hab_day_1_preset(self):
        ctx = EXPERIMENT_PRESETS["Hab - day 1"]
        assert ctx["hab"] is True
        assert ctx["hab_day_1"] is True
        assert ctx["ephys"] is False

    def test_ephys_opto_preset(self):
        ctx = EXPERIMENT_PRESETS["Ephys + Opto"]
        assert ctx["ephys"] is True
        assert ctx["opto"] is True
        assert ctx["optotagging"] is True


# ---------------------------------------------------------------------------
# Integration: dynamic_routing.ipynb
# ---------------------------------------------------------------------------

NOTEBOOK_PATH = pathlib.Path(__file__).parent.parent / "notebooks" / "dynamic_routing.ipynb"


@pytest.mark.skipif(not NOTEBOOK_PATH.exists(), reason="notebook not found")
class TestDynamicRoutingNotebook:
    def test_loads(self):
        nb = load_notebook(NOTEBOOK_PATH)
        assert nb["cells"]

    @pytest.mark.parametrize("preset_name", list(EXPERIMENT_PRESETS))
    def test_filter_all_presets(self, preset_name):
        nb = load_notebook(NOTEBOOK_PATH)
        ctx = EXPERIMENT_PRESETS[preset_name]
        filtered = filter_notebook(nb, ctx)
        assert 0 < len(filtered["cells"]) <= len(nb["cells"])

    def test_ephys_has_more_cells_than_hab(self):
        nb = load_notebook(NOTEBOOK_PATH)
        ephys = filter_notebook(nb, EXPERIMENT_PRESETS["Ephys"])
        hab = filter_notebook(nb, EXPERIMENT_PRESETS["Hab"])
        assert len(ephys["cells"]) > len(hab["cells"])

    def test_ephys_opto_is_superset_of_ephys(self):
        nb = load_notebook(NOTEBOOK_PATH)
        ephys = filter_notebook(nb, EXPERIMENT_PRESETS["Ephys"])
        ephys_opto = filter_notebook(nb, EXPERIMENT_PRESETS["Ephys + Opto"])
        assert len(ephys_opto["cells"]) >= len(ephys["cells"])

    def test_generate_for_each_preset(self, tmp_path):
        nb = load_notebook(NOTEBOOK_PATH)
        for name, ctx in EXPERIMENT_PRESETS.items():
            out = generate_filtered_notebook(NOTEBOOK_PATH, ctx, output=tmp_path / f"{name}.ipynb")
            result = load_notebook(out)
            assert result["cells"], f"No cells for preset '{name}'"
