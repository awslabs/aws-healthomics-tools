"""Tests for the aho.py CLI entry point."""

import sys
import unittest
from unittest import mock

from omics.cli.aho import main


class TestAho(unittest.TestCase):
    """Test cases for the aho.py CLI entry point."""

    def setUp(self):
        """Set up test fixtures."""
        self.original_argv = sys.argv

    def tearDown(self):
        """Tear down test fixtures."""
        sys.argv = self.original_argv

    @mock.patch("docopt.docopt")
    @mock.patch("omics.cli.run_analyzer.__main__.main")
    def test_main_run_analyzer(self, mock_run_analyzer_main, mock_docopt):
        """Test that the run_analyzer command is correctly routed."""
        mock_docopt.return_value = {"run_analyzer": True}
        sys.argv = ["aho", "run_analyzer", "--arg1", "--arg2"]

        main()

        mock_run_analyzer_main.assert_called_once_with(["--arg1", "--arg2"])

    @mock.patch("docopt.docopt")
    @mock.patch("omics.cli.rerun.__main__.main")
    def test_main_rerun(self, mock_rerun_main, mock_docopt):
        """Test that the rerun command is correctly routed."""
        mock_docopt.return_value = {"rerun": True}
        sys.argv = ["aho", "rerun", "--arg1", "--arg2"]

        main()

        mock_rerun_main.assert_called_once_with(["--arg1", "--arg2"])

    @mock.patch("docopt.docopt")
    @mock.patch("builtins.print")
    @mock.patch("sys.exit")
    def test_main_unknown_command(self, mock_sys_exit, mock_print, mock_docopt):
        """Test that unknown commands show help and exit with error."""
        mock_docopt.return_value = {}
        sys.argv = ["aho", "unknown_command"]

        main()

        self.assertEqual(mock_print.call_count, 2)  # Error message and doc string
        mock_sys_exit.assert_called_once_with(1)

    @mock.patch("docopt.docopt")
    @mock.patch("builtins.print")
    @mock.patch("sys.exit")
    def test_main_no_command(self, mock_sys_exit, mock_print, mock_docopt):
        """Test that no command shows help and exits with error."""
        mock_docopt.return_value = {}
        sys.argv = ["aho"]

        main()

        self.assertEqual(mock_print.call_count, 2)  # Error message and doc string
        mock_sys_exit.assert_called_once_with(1)

    @mock.patch("docopt.docopt")
    @mock.patch("omics.cli.run_analyzer.__main__.main")
    def test_main_docopt_parsing(self, mock_run_analyzer_main, mock_docopt):
        """Test that docopt correctly parses the command line arguments."""
        mock_docopt.return_value = {"run_analyzer": True}
        sys.argv = ["aho", "run_analyzer", "--arg1", "--arg2"]

        main()

        mock_docopt.assert_called_once_with(mock.ANY, argv=["run_analyzer"])


if __name__ == "__main__":
    unittest.main()
