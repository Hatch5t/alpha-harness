"""Notebook fixtures are synthetic. No code, outputs or credentials are imported."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_harness.api.template_lab import TemplateBody, _doc
from alpha_harness.labs.notebook_import import ImportRefused, main, preview_notebook


def notebook(templates: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"alpha_harness": {"template_lab": templates}, "api_key": "private-marker"},
            "cells": [
                {
                    "cell_type": "code",
                    "source": ["raise RuntimeError('must never execute')"],
                    "outputs": [{"private_research": "private-marker"}],
                }
            ],
        }
    )


def document() -> dict[str, object]:
    return {
        "name": "Synthetic template",
        "text": "rank(ts_delta($field, $days))",
        "variables": {
            "field": {"kind": "fields", "dataset_ids": ["synthetic"]},
            "days": {"kind": "values", "values": "5, 20"},
        },
    }


class NotebookImportTests(unittest.TestCase):
    def test_declared_document_reuses_template_lab_without_executing_cells(self) -> None:
        result = preview_notebook(notebook([document()]))
        self.assertEqual((result.cells, result.code_cells_ignored), (1, 1))
        self.assertEqual(len(result.templates), 1)
        self.assertTrue(result.requires_market_validation)
        self.assertNotIn("private-marker", result.model_dump_json())
        self.assertEqual(len(result.source_sha256), 64)
        compatible = TemplateBody.model_validate(result.templates[0].model_dump())
        self.assertEqual(_doc(compatible)["text"], document()["text"])

    def test_dynamic_notebook_without_declarations_imports_nothing(self) -> None:
        result = preview_notebook(notebook([]))
        self.assertEqual(result.templates, [])
        self.assertTrue(any("no templates" in warning for warning in result.warnings))

    def test_invalid_parameters_names_and_syntax_are_refused_without_echoing_source(self) -> None:
        bad = document()
        bad["text"] = "private-marker + ("
        with self.assertRaises(ImportRefused) as caught:
            preview_notebook(notebook([bad]))
        self.assertNotIn("private-marker", str(caught.exception))
        bad["text"] = "rank($undeclared)"
        with self.assertRaises(ImportRefused):
            preview_notebook(notebook([bad]))
        with self.assertRaises(ImportRefused):
            preview_notebook(notebook([document(), document()]))

    def test_unsupported_or_oversized_json_fails_safely(self) -> None:
        for text in ["{bad", "[]", json.dumps({"nbformat": 3}), " " * 2_000_001]:
            with self.subTest(size=len(text)), self.assertRaises(ImportRefused):
                preview_notebook(text)

    def test_cli_exports_new_owner_only_file_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "fixture.ipynb", Path(folder) / "private.json"
            source.write_text(notebook([document()]))
            argv = ["notebook_import", str(source), "--output", str(target)]
            with patch("sys.argv", argv), patch("builtins.print") as output:
                main()
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("rank", output.call_args.args[0])
            self.assertNotIn("private-marker", target.read_text())
            with patch("sys.argv", argv), self.assertRaises(FileExistsError):
                main()


if __name__ == "__main__":
    unittest.main()
