"""A data-only notebook bridge. Cells, outputs and Python code are never evaluated.

Only explicit metadata.alpha_harness.template_lab declarations are importable. The
returned documents use the existing Template Lab format; they are not executable tasks.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from ..schemas import Out
from . import template
from .fastexpr import ParseError, parse, walk
from .template_document import TemplateBody, ValuesVariable

MAX_BYTES = 2_000_000
MAX_CELLS = 1000
MAX_TEMPLATES = 100


class ImportRefused(ValueError):
    """Invalid or unsupported notebook data, without echoing private source values."""


class NotebookPreview(Out):
    source_sha256: str
    cells: int
    code_cells_ignored: int
    templates: list[TemplateBody]
    requires_market_validation: Literal[True] = True
    warnings: list[str] = Field(default_factory=list)


def preview_notebook(raw: str | bytes) -> NotebookPreview:
    """Read only declared templates; never infer them from code or output cells."""
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(encoded) > MAX_BYTES:
        raise ImportRefused(
            "Notebook exceeds the 2 MB import limit. Export a small template bundle."
        )
    try:
        notebook = json.loads(encoded)
    except ValueError, RecursionError:
        raise ImportRefused("Notebook must be valid bounded JSON.") from None
    if not isinstance(notebook, dict) or notebook.get("nbformat") != 4:
        raise ImportRefused("Only notebook format 4 is supported.")
    cells = notebook.get("cells")
    if not isinstance(cells, list) or len(cells) > MAX_CELLS:
        raise ImportRefused("Notebook cells must be an array of at most 1000 items.")
    if any(not isinstance(cell, dict) for cell in cells):
        raise ImportRefused("Each notebook cell must be an object.")
    metadata = notebook.get("metadata") or {}
    extension = metadata.get("alpha_harness", {}) if isinstance(metadata, dict) else None
    if not isinstance(extension, dict):
        raise ImportRefused("alpha_harness metadata must be an object.")
    bundle = extension.get("template_lab", [])
    if not isinstance(bundle, list) or len(bundle) > MAX_TEMPLATES:
        raise ImportRefused("template_lab must be an array of at most 100 templates.")
    documents: list[TemplateBody] = []
    names: set[str] = set()
    for index, item in enumerate(bundle):
        try:
            document = TemplateBody.model_validate(item)
        except ValidationError:
            raise ImportRefused(f"Template {index + 1} has invalid document fields.") from None
        name = document.name.strip()
        if not name or name.casefold() in names:
            raise ImportRefused(f"Template {index + 1} has an empty or duplicate name.")
        names.add(name.casefold())
        document.name = name
        try:
            tree = parse(document.text)
            if any(node.value == template.HOLE for _, node in walk(tree)):
                raise ValueError("Unfilled template")
            used = {node.value[1:] for _, node in walk(tree) if node.value.startswith("$")}
            if used != set(document.variables):
                raise ValueError("Variables do not match")
            if len(used) > template.MAX_VARIABLES:
                raise ValueError("Too many variables")
            for variable in document.variables.values():
                if isinstance(variable, ValuesVariable):
                    template.values(variable.values)
        except ValueError, ParseError, RecursionError:
            raise ImportRefused(
                f"Template {index + 1} has invalid expression or parameter syntax."
            ) from None
        documents.append(document)
    warnings = [
        "Code cells, outputs, notebook execution hooks and undeclared metadata were ignored.",
        "Validate account operators, field types and market coverage in Template Lab "
        "before any run.",
    ]
    if not documents:
        warnings.append("No declarative template_lab metadata was supplied; no templates imported.")
    return NotebookPreview(
        source_sha256=hashlib.sha256(encoded).hexdigest(),
        cells=len(cells),
        code_cells_ignored=sum(cell.get("cell_type") == "code" for cell in cells),
        templates=documents,
        warnings=warnings,
    )


def main() -> None:
    """Inspect locally, or explicitly export declared templates into a new private file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notebook", type=Path)
    parser.add_argument("--output", type=Path, help="New private JSON file; never overwritten")
    args = parser.parse_args()
    if args.notebook.stat().st_size > MAX_BYTES:
        parser.error("Notebook exceeds 2 MB; provide a small declarative export.")
    try:
        preview = preview_notebook(args.notebook.read_bytes())
    except ImportRefused as exc:
        parser.error(str(exc))
    if args.output:
        # Exclusive, owner-only: research expressions must not become a public export by default.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(preview.model_dump_json(indent=2))
    summary: dict[str, Any] = {
        "cells": preview.cells,
        "codeCellsIgnored": preview.code_cells_ignored,
        "declaredTemplates": len(preview.templates),
        "requiresMarketValidation": True,
        "warnings": preview.warnings,
    }
    # CLI output is a count-only inspection, never private expressions or cell contents.
    print(json.dumps(summary))  # noqa: T201


if __name__ == "__main__":
    main()
