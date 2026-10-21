"""Frozen entry dispatch must precede Qt and collector initialization."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


class DiagnosticEntryTests(unittest.TestCase):
    """Exercise the real application entry without UI dependencies or Office."""

    def test_diagnostic_arguments_reach_cli_without_importing_qt(self) -> None:
        entry = Path(__file__).resolve().parents[1] / "app" / "main.py"
        diagnostic = ModuleType("knowmate.secure.ppt_quit_compare")
        diagnostic.main = Mock(return_value=7)
        arguments = [str(entry), "--ppt-quit-compare", "--live", "--confirm-isolated-session"]
        with patch.object(sys, "argv", arguments), patch.dict(sys.modules, {
                "knowmate.secure.ppt_quit_compare": diagnostic, "PyQt6": None}), self.assertRaises(
                SystemExit) as stopped:
            runpy.run_path(str(entry), run_name="__main__")
        self.assertEqual(stopped.exception.code, 7)
        diagnostic.main.assert_called_once_with(arguments[2:])


if __name__ == "__main__":
    unittest.main()
