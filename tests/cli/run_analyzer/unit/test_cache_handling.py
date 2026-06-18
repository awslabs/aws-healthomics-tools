"""Tests for call-cache handling in the run analyzer."""

import io
import sys
import unittest

from omics.cli.run_analyzer.__main__ import _handle_timeline, is_task_cached
from omics.cli.run_analyzer.exceptions import RunAnalyzerError


class TestIsTaskCached(unittest.TestCase):
    """Tests for the is_task_cached helper."""

    def test_task_with_all_timing_fields_is_not_cached(self):
        res = {
            "creationTime": "2024-01-15T10:00:00.000Z",
            "startTime": "2024-01-15T10:01:00.000Z",
            "stopTime": "2024-01-15T10:02:00.000Z",
        }
        self.assertFalse(is_task_cached(res))

    def test_task_missing_all_timing_fields_is_cached(self):
        res = {"arn": "arn:aws:omics:us-east-1:123:task/1", "name": "foo", "metrics": {}}
        self.assertTrue(is_task_cached(res))

    def test_task_missing_start_time_is_cached(self):
        res = {
            "creationTime": "2024-01-15T10:00:00.000Z",
            "stopTime": "2024-01-15T10:02:00.000Z",
        }
        self.assertTrue(is_task_cached(res))

    def test_task_missing_stop_time_is_cached(self):
        res = {
            "creationTime": "2024-01-15T10:00:00.000Z",
            "startTime": "2024-01-15T10:01:00.000Z",
        }
        self.assertTrue(is_task_cached(res))


class TestHandleTimelinePartialCache(unittest.TestCase):
    """Tests for _handle_timeline with partially cached runs."""

    def setUp(self):
        self.resources = [
            {
                "arn": "arn:aws:omics:us-east-1:123:run/1234567",
                "name": "my-workflow",
                "creationTime": "2024-01-15T10:00:00.000Z",
                "startTime": "2024-01-15T10:00:30.000Z",
                "stopTime": "2024-01-15T10:30:00.000Z",
            },
            {
                "arn": "arn:aws:omics:us-east-1:123:task/1234567/aaa",
                "name": "align_reads",
                "cpus": 4,
                "memory": 8,
                "creationTime": "2024-01-15T10:01:00.000Z",
                "startTime": "2024-01-15T10:02:00.000Z",
                "stopTime": "2024-01-15T10:15:00.000Z",
            },
            {
                "arn": "arn:aws:omics:us-east-1:123:task/1234567/bbb",
                "name": "cached_task_1",
                "metrics": {},
            },
            {
                "arn": "arn:aws:omics:us-east-1:123:task/1234567/ccc",
                "name": "cached_task_2",
                "metrics": {},
            },
        ]

    def test_partial_cache_emits_executed_rows(self):
        out = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            _handle_timeline(self.resources, out)
        finally:
            sys.stderr = old_stderr

        csv_output = out.getvalue()
        lines = csv_output.strip().split("\n")
        # Header + 2 executed resources (run + align_reads)
        self.assertEqual(len(lines), 3)
        self.assertIn("resource,pending,starting,running", lines[0])
        self.assertIn("align_reads", lines[2])

    def test_partial_cache_reports_cached_tasks_to_stderr(self):
        out = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            _handle_timeline(self.resources, out)
        finally:
            stderr_output = sys.stderr.getvalue()
            sys.stderr = old_stderr

        self.assertIn("2/4", stderr_output)
        self.assertIn("cached_task_1", stderr_output)
        self.assertIn("cached_task_2", stderr_output)
        self.assertIn("call cache", stderr_output)


class TestHandleTimelineFullyCache(unittest.TestCase):
    """Tests for _handle_timeline when all tasks are cached."""

    def test_fully_cached_raises_run_analyzer_error(self):
        resources = [
            {
                "arn": "arn:aws:omics:us-east-1:123:task/1234567/aaa",
                "name": "cached_task_1",
                "metrics": {},
            },
            {
                "arn": "arn:aws:omics:us-east-1:123:task/1234567/bbb",
                "name": "cached_task_2",
                "metrics": {},
            },
        ]
        out = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            with self.assertRaises(RunAnalyzerError) as ctx:
                _handle_timeline(resources, out)
            self.assertIn("all tasks", str(ctx.exception))
            self.assertIn("call cache", str(ctx.exception))
        finally:
            sys.stderr = old_stderr
