# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the run_dashboard tool (no AWS calls)."""

import json
import unittest
from unittest import mock

from omics.cli.run_dashboard.__main__ import (
    TASK_LABEL,
    WORKFLOW_LABEL,
    _is_benign_validation,
    _utilization_expr,
    build_dashboard_body,
    delete_dashboard,
    main,
)


class UtilizationExprTest(unittest.TestCase):
    def test_divides_per_task_then_groups(self):
        expr = _utilization_expr("aws.omics.task.cpu.usage", "aws.omics.task.cpu.limit", "123")
        # Both sides grouped by task id so usage/limit match per task.
        self.assertEqual(expr.count(f'avg by ("{TASK_LABEL}")'), 2)
        self.assertIn(" / ", expr)
        self.assertIn(f'"{WORKFLOW_LABEL}"="123"', expr)


class BuildDashboardBodyTest(unittest.TestCase):
    def test_one_row_per_workflow_five_plots_each(self):
        body = build_dashboard_body("us-west-2", [("111", "wf-a"), ("222", "wf-b")])
        charts = [w for w in body["widgets"] if w["type"] == "chart"]
        self.assertEqual(len(charts), 10)  # 2 workflows x 5 plots
        json.dumps(body)  # serializable

    def test_title_is_name_only_and_region_applied(self):
        body = build_dashboard_body("eu-west-1", [("999", "my-wf")])
        charts = [w for w in body["widgets"] if w["type"] == "chart"]
        titles = [c["properties"]["title"] for c in charts]
        # Title uses the name only, no "(id)" parenthetical.
        self.assertTrue(any(t.startswith("my-wf \u2014") for t in titles))
        self.assertFalse(any("(999)" in t for t in titles))
        # The id still appears in the queries.
        self.assertIn("999", json.dumps(body))
        self.assertTrue(all(c["properties"]["region"] == "eu-west-1" for c in charts))

    def test_rows_are_vertically_offset(self):
        body = build_dashboard_body("us-west-2", [("111", "a"), ("222", "b")])
        charts = [w for w in body["widgets"] if w["type"] == "chart"]
        ys = sorted({c["y"] for c in charts})
        self.assertEqual(ys, [2, 8])  # header height 2, row height 6

    def test_cpu_plot_has_avg_and_p99(self):
        body = build_dashboard_body("us-west-2", [("111", "wf-a")])
        charts = [w for w in body["widgets"] if w["type"] == "chart"]
        cpu = next(c for c in charts if c["properties"]["title"].endswith("CPU utilization %"))
        labels = [q["label"] for q in cpu["properties"]["data"]["queries"]]
        self.assertEqual(labels, ["avg", "p99"])
        for c in charts:
            for q in c["properties"]["data"]["queries"]:
                self.assertEqual(q["type"], "cloudwatch-metrics")
                self.assertEqual(q["language"], "PromQL")

    def test_workflow_name_with_special_chars_serializes(self):
        # A name with a double quote / backslash must not break JSON serialization
        # (the body is built as a dict and dumped once, so json handles escaping).
        name = 'my "quoted" wf \\ path'
        body = build_dashboard_body("us-west-2", [("123", name)])
        json.dumps(body)  # must not raise
        charts = [w for w in body["widgets"] if w["type"] == "chart"]
        self.assertTrue(any(name in c["properties"]["title"] for c in charts))


class BenignValidationTest(unittest.TestCase):
    def test_hides_x_property_warning_but_keeps_others(self):
        benign = (
            'The "x" property is not expected to be part of a widget definition, will be ignored'
        )
        self.assertTrue(_is_benign_validation(benign))
        self.assertFalse(_is_benign_validation("Some other validation problem"))


class DeleteTest(unittest.TestCase):
    def test_delete_dashboard_calls_delete_dashboards(self):
        session = mock.Mock()
        cw = session.client.return_value
        delete_dashboard("omics-workflow-usage", "us-west-2", session)
        session.client.assert_called_once_with("cloudwatch", region_name="us-west-2")
        cw.delete_dashboards.assert_called_once_with(DashboardNames=["omics-workflow-usage"])

    def test_main_delete_flag_deletes_without_workflow_ids(self):
        # --delete must not require -i, and must call delete (not put).
        with mock.patch("omics.cli.run_dashboard.__main__.boto3.Session") as sess_cls:
            cw = sess_cls.return_value.client.return_value
            rc = main(["--delete", "-d", "my-dash", "-r", "eu-west-1"])
        self.assertEqual(rc, 0)
        cw.delete_dashboards.assert_called_once_with(DashboardNames=["my-dash"])
        cw.put_dashboard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
