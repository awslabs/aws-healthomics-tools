"""Tests for CloudWatch-aware metrics, CSV output, batch aggregation and plotting."""

import copy
import io
import unittest
import unittest.mock
from unittest.mock import MagicMock

import omics.cli.run_analyzer.batch as batch
from omics.cli.run_analyzer import timeline
from omics.cli.run_analyzer.__main__ import CLOUDWATCH_HEADERS, _handle_stats
from omics.cli.run_analyzer.metrics import CW_KEY, add_metrics


def _pricing():
    cache = MagicMock()
    cache.get_price.return_value = 1.0
    return cache


def _task(cw=None, **overrides):
    res = {
        "arn": "arn:aws:omics:us-east-1:123456789012:task/111",
        "name": "align (1)",
        "status": "COMPLETED",
        "creationTime": "2026-10-13T17:00:00.000Z",
        "startTime": "2026-10-13T17:00:00.000Z",
        "stopTime": "2026-10-13T17:10:00.000Z",
        "instanceType": "omics.m.4xlarge",
        "cpus": 16,
        "memory": 64,
        "metrics": {
            "cpusReserved": 16,
            "cpusMaximum": 15.0,
            "cpusAverage": 8.0,
            "gpusReserved": 0,
            "memoryReservedGiB": 64,
            "memoryMaximumGiB": 60.0,
            "memoryAverageGiB": 30.0,
        },
    }
    res.update(overrides)
    if cw is not None:
        res[CW_KEY] = cw
    return res


def _run():
    return {
        "arn": "arn:aws:omics:us-east-1:123456789012:run/1234567",
        "name": "run",
        "creationTime": "2026-10-13T17:00:00.000Z",
        "startTime": "2026-10-13T17:00:00.000Z",
        "stopTime": "2026-10-13T17:10:00.000Z",
        "storageType": "DYNAMIC",
        "workflow": "arn:aws:omics:us-east-1:123456789012:workflow/1",
        "metrics": {},
    }


class TestRecommendations(unittest.TestCase):
    def _metrics(self, res, headroom=0.0):
        add_metrics(res, [_run(), res], _pricing(), headroom)
        return res["metrics"]

    def test_manifest_only_task_is_unchanged(self):
        m = self._metrics(_task())
        self.assertEqual(m["omicsInstanceTypeMinimum"], "omics.m.4xlarge")
        self.assertNotIn("metricsSource", m)
        self.assertFalse(any(k in m for k in CLOUDWATCH_HEADERS))

    def _low_mem_task(self, cw):
        res = _task(cw)
        res["metrics"]["memoryMaximumGiB"] = 5.0
        return res

    def test_cloudwatch_p95_cpu_and_peak_memory_drive_sizing(self):
        cw = {"cpusMaximum": 15.5, "cpusP95": 3.2, "memoryMaximumGiB": 7.2, "cpusAverage": 3.0}
        m = self._metrics(self._low_mem_task(cw))
        self.assertEqual(m["metricsSource"], "cloudwatch")
        self.assertEqual(m["omicsInstanceTypeMinimum"], "omics.c.xlarge")  # 4 vCPU, 8 GiB
        self.assertEqual(m["recommendedCpus"], 4)
        self.assertEqual(m["recommendedMemoryGiB"], 8)
        self.assertAlmostEqual(m["cwCpuUtilizationRatio"], 3.2 / 16)
        self.assertAlmostEqual(m["cwMemoryUtilizationRatio"], 7.2 / 64)

    def test_headroom_applies_to_cloudwatch_basis(self):
        cw = {"cpusP95": 3.2, "memoryMaximumGiB": 7.2}
        m = self._metrics(self._low_mem_task(cw), headroom=0.5)
        self.assertEqual(m["recommendedCpus"], 8)
        self.assertEqual(m["recommendedMemoryGiB"], 16)

    def test_missing_cloudwatch_field_falls_back_to_manifest(self):
        m = self._metrics(_task({"cpusP95": 3.2}))
        self.assertEqual(m["metricsSource"], "mixed")
        self.assertEqual(m["recommendedMemoryGiB"], 64)  # manifest peak 60 GiB

    def test_empty_cloudwatch_metrics_use_manifest(self):
        m = self._metrics(_task({}))
        self.assertEqual(m["metricsSource"], "manifest")
        self.assertEqual(m["omicsInstanceTypeMinimum"], "omics.m.4xlarge")

    def test_memory_never_sized_below_manifest_peak(self):
        # CloudWatch peak (20 GiB) below the manifest peak (60 GiB): keep 60 GiB
        m = self._metrics(_task({"cpusP95": 3.2, "memoryMaximumGiB": 20.0}))
        self.assertEqual(m["recommendedMemoryGiB"], 64)
        self.assertGreaterEqual(m["recommendedMemoryGiB"], 60)

    def test_memory_uses_cloudwatch_peak_when_higher(self):
        res = self._low_mem_task({"cpusP95": 1.0, "memoryMaximumGiB": 14.0})
        self.assertEqual(self._metrics(res)["recommendedMemoryGiB"], 16)

    def test_zero_cloudwatch_values_fall_back_to_manifest(self):
        m = self._metrics(_task({"cpusP95": 0.0, "memoryMaximumGiB": 0.0}))
        self.assertEqual(m["omicsInstanceTypeMinimum"], "omics.m.4xlarge")
        self.assertEqual(m["recommendedCpus"], 16)

    def test_memory_pressure_uses_enforced_limit(self):
        cw = {"cpusP95": 4, "memoryMaximumGiB": 58.0, "memoryLimitGiB": 62.0}
        m = self._metrics(_task(cw))
        self.assertTrue(m["memoryPressure"])
        cw["memoryMaximumGiB"] = 30.0
        self.assertFalse(self._metrics(_task(cw))["memoryPressure"])

    def test_scratch_pressure(self):
        cw = {"scratchMaximumGiB": 95.0, "scratchLimitGiB": 100.0, "scratchStorageMode": "LOCAL"}
        m = self._metrics(_task(cw))
        self.assertTrue(m["scratchPressure"])
        self.assertAlmostEqual(m["scratchUtilizationRatio"], 0.95)
        self.assertEqual(m["scratchStorageMode"], "LOCAL")

    def test_scratch_without_limit_reports_usage_only(self):
        m = self._metrics(_task({"scratchMaximumGiB": 5.0}))
        self.assertEqual(m["scratchMaximumGiB"], 5.0)
        self.assertNotIn("scratchPressure", m)

    def _gpu_task(self, cw):
        res = _task(cw, instanceType="omics.g5.12xlarge", gpus=4)
        res["metrics"]["gpusReserved"] = 4
        res["metrics"]["memoryMaximumGiB"] = 3.0
        res["metrics"]["cpusMaximum"] = 2.0
        return res

    def test_idle_gpus_move_task_to_cpu_instance(self):
        cw = {"gpusObserved": 4, "gpusIdle": 4, "cpusP95": 2.0, "memoryMaximumGiB": 3.0}
        m = self._metrics(self._gpu_task(cw))
        self.assertEqual(m["recommendedGpus"], 0)
        self.assertEqual(m["omicsInstanceTypeMinimum"], "omics.c.large")

    def test_partially_idle_gpus_keep_reserved_instance(self):
        cw = {"gpusObserved": 4, "gpusIdle": 3, "cpusP95": 2.0, "memoryMaximumGiB": 3.0}
        m = self._metrics(self._gpu_task(cw))
        self.assertEqual(m["recommendedGpus"], 1)
        self.assertEqual(m["omicsInstanceTypeMinimum"], "omics.g5.12xlarge")

    def test_unobserved_gpus_make_no_gpu_recommendation(self):
        cw = {"gpusObserved": 2, "gpusIdle": 2, "cpusP95": 2.0, "memoryMaximumGiB": 3.0}
        m = self._metrics(self._gpu_task(cw))
        self.assertNotIn("recommendedGpus", m)
        self.assertEqual(m["omicsInstanceTypeMinimum"], "omics.g5.12xlarge")

    def test_io_bound_detection(self):
        cw = {"cpusAverage": 1.0, "fsReadGiB": 100.0, "fsWriteGiB": 20.0}  # 600 s run
        m = self._metrics(_task(cw))
        self.assertAlmostEqual(m["fsThroughputMiBps"], 120 * 1024 / 600)
        self.assertTrue(m["ioBound"])
        cw["cpusAverage"] = 12.0
        self.assertFalse(self._metrics(_task(cw))["ioBound"])

    def test_no_io_metrics_means_no_io_verdict(self):
        m = self._metrics(_task({"cpusAverage": 1.0}))
        self.assertNotIn("ioBound", m)

    def test_run_row_reports_cloudwatch_storage(self):
        run = _run()
        run[CW_KEY] = {"storageMaximumGiB": 42.0}
        add_metrics(run, [run], _pricing())
        self.assertEqual(run["metrics"]["cwStorageMaximumGiB"], 42.0)


class TestStatsColumns(unittest.TestCase):
    def _stats(self, resources):
        out = io.StringIO()
        opts = {"--headroom": None, "--write-config": None, "--workflow-owner-id": None}
        _handle_stats(resources, MagicMock(), _pricing(), opts, out)
        return out.getvalue().splitlines()

    def test_columns_unchanged_without_cloudwatch_data(self):
        header = self._stats([_run(), _task()])[0].split(",")
        self.assertNotIn("metricsSource", header)

    def test_columns_unchanged_when_no_run_had_metrics(self):
        run = _run()
        run[CW_KEY] = {}
        header = self._stats([run, _task({})])[0].split(",")
        self.assertNotIn("metricsSource", header)

    def test_cloudwatch_columns_appended_and_blank_when_missing(self):
        cw_task = _task({"cpusP95": 3.2, "memoryMaximumGiB": 7.2})
        other = _task({}, arn="arn:aws:omics:us-east-1:123456789012:task/222")
        lines = self._stats([_run(), cw_task, other])
        header = lines[0].split(",")
        self.assertEqual(header[-len(CLOUDWATCH_HEADERS) :], CLOUDWATCH_HEADERS)
        rows = [dict(zip(header, line.split(","))) for line in lines[1:]]
        # CloudWatch CPU used; manifest memory peak (60 GiB) beats CloudWatch (7.2 GiB)
        self.assertEqual(rows[1]["metricsSource"], "mixed")
        self.assertEqual(rows[2]["metricsSource"], "manifest")
        self.assertEqual(rows[2]["cwCpusP95"], "")


class TestFlagGating(unittest.TestCase):
    def test_embedded_cloudwatch_data_ignored_without_flag(self):
        import json
        import os
        import tempfile

        from omics.cli.run_analyzer import __main__ as main_mod

        run = _run()
        run[CW_KEY] = {}
        task = _task({"cpusP95": 3.2, "memoryMaximumGiB": 7.2})
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.json")
            out = os.path.join(tmp, "out.csv")
            with open(src, "w") as f:
                json.dump([run, task], f)
            session = MagicMock()
            with unittest.mock.patch.object(main_mod.boto3, "Session", return_value=session):
                with unittest.mock.patch.object(main_mod, "PricingCache", return_value=_pricing()):
                    main_mod.main(["-f", src, "-o", out])
            with open(out) as f:
                header = f.readline().strip().split(",")
        self.assertNotIn("metricsSource", header)


class TestBatch(unittest.TestCase):
    def _aggregate(self, runs):
        out = io.StringIO()
        batch.aggregate_and_print(runs, _pricing(), "NEXTFLOW", out=out)
        lines = out.getvalue().splitlines()
        header = lines[0].split(",")
        return header, [dict(zip(header, line.split(","))) for line in lines[1:]]

    def test_no_cloudwatch_columns_without_data(self):
        header, _ = self._aggregate([[_run(), _task()]])
        self.assertEqual(header, batch.hdrs)

    def test_missing_values_are_not_averaged_as_zero(self):
        with_cw = _task({"cpusP95": 4.0, "memoryMaximumGiB": 8.0, "memoryLimitGiB": 8.5})
        without = _task({}, name="align (2)")
        header, rows = self._aggregate([[_run(), with_cw], [_run(), copy.deepcopy(without)]])
        self.assertEqual(header, batch.hdrs + batch.cloudwatch_hdrs)
        row = rows[0]
        self.assertEqual(row["count"], "2")
        self.assertEqual(row["cloudwatchCount"], "1")
        self.assertEqual(row["meanCwCpuUtilizationRatio"], str(round(4.0 / 16, 4)))
        self.assertEqual(row["anyMemoryPressure"], "True")
        self.assertEqual(row["maximumGpuUtilizationMaximum"], "")


class TestTimelineOverlay(unittest.TestCase):
    def _tasks(self):
        return [_task()]

    def test_plot_without_utilization_is_unchanged(self):
        layout = timeline.plot_timeline(self._tasks(), show_plot=False)
        self.assertEqual(len(layout.children), 2)

    def test_plot_with_utilization_adds_charts(self):
        start = 1791910800.0  # 2026-10-13T17:00:00Z
        util = {
            "time": [start, start + 30],
            "cpuUsage": [1.0, None],
            "cpuLimit": [16.0, 16.0],
            "memoryUsageGiB": [2.0, 3.0],
            "memoryLimitGiB": [64.0, 64.0],
        }
        layout = timeline.plot_timeline(self._tasks(), show_plot=False, utilization=util)
        self.assertEqual(len(layout.children), 4)
        cpu_fig = layout.children[2]
        xs = cpu_fig.renderers[0].data_source.data["x"]
        self.assertAlmostEqual(xs[0], 0.0)
        self.assertAlmostEqual(xs[1], 0.5)  # minutes


if __name__ == "__main__":
    unittest.main()
