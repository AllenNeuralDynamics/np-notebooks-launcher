# np_notebooks_launcher

## For notebook users

- Run `np-notebooks-launcher` from the command line
- A GUI pops up — pick your experiment type (Ephys, Hab, Pretest, etc.)
- Click Launch → a filtered copy of the notebook opens in JupyterLab with only the relevant cells for that experiment type

## For notebook developers

Annotate cells with directives to control when they appear:

- Code cells: `# /// show-if: experiment=ephys or opto`
- Markdown cells: `<!-- /// show-if: experiment=ephys -->`
- `hide-if` works as the inverse
- Cells with no directive are always shown

Conditions use `and`, `or`, `not` with names from the experiment context:
`ephys`, `hab`, `hab_day_1`, `opto`, `optotagging`, `pretest`

The source notebook stays unchanged — filtered copies (e.g. `dynamic_routing_ephys.ipynb`) are generated at launch time.

### Example

```python
# /// show-if: experiment=(ephys or opto) and not pretest
if (ephys or opto) and not pretest:
    ...
```
