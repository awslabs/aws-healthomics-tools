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
        sys.argv = ["aho", "run_analyzer", "123456"]

        main()

        mock_run_analyzer_main.assert_called_once_with(["123456"])

    @mock.patch("docopt.docopt")
    @mock.patch("omics.cli.rerun.__main__.main")
    def test_main_rerun(self, mock_rerun_main, mock_docopt):
        """Test that the rerun command is correctly routed."""

        mock_docopt.return_value = {"rerun": True}
        sys.argv = ["aho", "rerun", "123456"]

        main()

        mock_rerun_main.assert_called_once_with(["123456"])


if __name__ == "__main__":
    unittest.main()
