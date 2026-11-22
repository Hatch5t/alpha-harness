# Safe notebook template bridge

The reader consumes notebook JSON as data. It does not execute Python, load notebook
extensions, evaluate outputs, extract keys, contact a provider or create a research task.

From `backend/`, inspect an existing local notebook without printing its expressions:

```sh
PYTHONPATH=src uv run python -m alpha_harness.labs.notebook_import /path/to/private.ipynb
```

Only explicitly declared `metadata.alpha_harness.template_lab` entries are imported. Each
entry uses the existing Template Lab request format. This synthetic notebook illustrates it:

```json
{
  "nbformat": 4,
  "nbformat_minor": 5,
  "cells": [],
  "metadata": {
    "alpha_harness": {
      "template_lab": [{
        "name": "Synthetic template",
        "text": "rank(ts_delta($field, $days))",
        "variables": {
          "field": {"kind": "fields", "dataset_ids": []},
          "days": {"kind": "values", "values": "5, 20"}
        }
      }]
    }
  }
}
```

The reader checks bounded JSON size, notebook version, cell count, template count, unique
names, Fast Expression syntax, declared variable names and parameter-list syntax. Its
SHA-256 records the exact source bytes. No parameter space is expanded during import.

To export declared documents into a **new** local private file:

```sh
PYTHONPATH=src uv run python -m alpha_harness.labs.notebook_import \
  /path/to/private.ipynb --output /path/to/private/template-preview.json
```

The output is created with owner-only permissions and refuses to overwrite an existing
file. Keep it outside the public checkout. It includes expressions only from the explicitly
declared metadata, never code cells or output cells. Ordinary CLI output contains counts
and warnings only. Do not commit research exports or private notebooks to a public branch.

Each item in the output's `templates` array is compatible with the existing Template Lab
document API, `POST /api/template-lab/templates`; saving a template is not running a task.
GUI batch import is not implemented in this milestone. Any later campaign/UI importer must
retain the source hash and use the normal market preview before allowing a task to run.

Syntax acceptance is not market acceptance: actual account operators, field existence,
MATRIX/VECTOR types, universe coverage, competition constraints and the expanded candidate
must still pass the existing task preflight. The reader always reports
`requiresMarketValidation=true`.

The inspected local AIAC notebook generates expressions dynamically through Python and
LLM calls. It has no declarative bundle, so the reader imports zero templates. To reuse
those recipes, add explicit Template Lab documents and parameter spaces to metadata, or
develop a separately reviewed data-only converter. Do not execute the notebook merely to
discover what it contains.

Offline regression command:

```sh
PYTHONPATH=src uv run python -m unittest discover -s tests -p test_notebook_import.py
```
