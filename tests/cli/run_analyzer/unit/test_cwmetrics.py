"""Tests for CloudWatch run-metrics retrieval in the run analyzer."""

import datetime
import json
import re
import unittest
from typing import Callable, Optional
from unittest.mock import MagicMock

import requests
from botocore.credentials import Credentials

import omics.cli.run_analyzer.cwmetrics as cw
from omics.cli.run_analyzer.cwmetrics import (
    CW_KEY,
    GIB,
    CloudWatchMetricsError,
    CloudWatchMetricsFetcher,
    PromQLClient,
)

RUN_ARN = "arn:aws:omics:us-east-1:123456789012:run/1234567"
START = "2026-10-13T17:00:00.000Z"
STOP = "2026-10-13T18:00:00.000Z"
NOW = datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc).timestamp()


def _task(tid: str, start: Optional[str] = START, stop: Optional[str] = STOP) -> dict:
    res = {"arn": f"arn:aws:omics:us-east-1:123456789012:task/{tid}", "name": f"t{tid}"}
    if start:
        res["creationTime"] = start
        res["startTime"] = start
    if stop:
        res["stopTime"] = stop
    res["metrics"] = {}
    return res


def _resources(*task_ids: str, start: str = START, stop: str = STOP) -> list[dict]:
    run = {"arn": RUN_ARN, "startTime": start, "stopTime": stop, "metrics": {}}
    return [run] + [_task(t, start, stop) for t in task_ids]


def _sample(value: float, **labels) -> dict:
    return {"metric": labels, "value": [NOW, str(value)]}


T = cw.TASK_LABEL


class FakeClient:
    """Fake PromQL client routing queries to a handler and recording them."""

    def __init__(self, handler: Callable[[str], list]):
        self.handler = handler
        self.queries: list[tuple[str, float]] = []
        self.range_queries: list[tuple[str, float, float, int]] = []

    def query(self, expr: str, at: float) -> list[dict]:
        self.queries.append((expr, at))
        return self.handler(expr)

    def query_range(self, expr: str, start: float, end: float, step: int) -> list[dict]:
        self.range_queries.append((expr, start, end, step))
        return self.handler(expr)


def _make_fetcher(client) -> tuple[CloudWatchMetricsFetcher, list[str]]:
    warnings: list[str] = []
    fetcher = CloudWatchMetricsFetcher(
        MagicMock(), warn=warnings.append, client_factory=lambda region: client, now=lambda: NOW
    )
    return fetcher, warnings


def _rich_handler(expr: str) -> list:
    """Responses for a run with one CPU task (111) and one GPU task (222)."""
    if expr.startswith("count(count_over_time("):
        return [{"metric": {}, "value": [NOW, "2"]}]
    if cw.CPU_USAGE in expr and "bool" in expr:
        # idle subquery: task 111 idle in 1 of 4 samples
        if "sum_over_time" in expr:
            return [_sample(1, **{T: "111"})]
        return [_sample(4, **{T: "111"})]
    table = [
        ("quantile_over_time", cw.CPU_USAGE, [_sample(3.5, **{T: "111"})]),
        ("max_over_time", cw.CPU_USAGE, [_sample(3.9, **{T: "111"})]),
        ("sum_over_time", cw.CPU_USAGE, [_sample(10.0, **{T: "111"})]),
        ("count_over_time", cw.CPU_USAGE, [_sample(4, **{T: "111"})]),
        ("max_over_time", cw.CPU_LIMIT, [_sample(4, **{T: "111"})]),
        ("quantile_over_time", cw.MEM_USAGE, [_sample(2 * GIB, **{T: "111"})]),
        ("max_over_time", cw.MEM_USAGE, [_sample(7.5 * GIB, **{T: "111"})]),
        ("sum_over_time", cw.MEM_USAGE, [_sample(8 * GIB, **{T: "111"})]),
        ("count_over_time", cw.MEM_USAGE, [_sample(4, **{T: "111"})]),
        ("max_over_time", cw.MEM_LIMIT, [_sample(7.8 * GIB, **{T: "111"})]),
        (
            "max_over_time",
            cw.SCRATCH_USAGE,
            [_sample(95 * GIB, **{T: "111", cw.SCRATCH_MODE_LABEL: "LOCAL"})],
        ),
        ("max_over_time", cw.SCRATCH_LIMIT, [_sample(100 * GIB, **{T: "111"})]),
        (
            "max_over_time",
            cw.GPU_UTIL,
            [
                _sample(80, **{T: "222", cw.GPU_LABEL: "0"}),
                _sample(1, **{T: "222", cw.GPU_LABEL: "1"}),
            ],
        ),
        ("sum_over_time", cw.GPU_UTIL, [_sample(162, **{T: "222"})]),
        ("count_over_time", cw.GPU_UTIL, [_sample(4, **{T: "222"})]),
        ("max_over_time", cw.GPU_MEM_USAGE, [_sample(10 * GIB, **{T: "222"})]),
        ("max_over_time", cw.GPU_MEM_LIMIT, [_sample(24 * GIB, **{T: "222"})]),
        ("max_over_time", cw.RUN_FS_USAGE, [{"metric": {}, "value": [NOW, str(50 * GIB)]}]),
        (
            "max_over_time",
            cw.FS_IO,
            [
                _sample(
                    3 * GIB,
                    **{T: "111", cw.FS_DIRECTION_LABEL: "read", "__temporality__": "cumulative"},
                ),
                _sample(
                    1 * GIB,
                    **{T: "111", cw.FS_DIRECTION_LABEL: "write", "__temporality__": "cumulative"},
                ),
            ],
        ),
    ]
    for func, metric, result in table:
        if func in expr and f'"{metric}"' in expr:
            return result
    return []


class TestHelpers(unittest.TestCase):
    def test_split_windows_short_run_is_one_window(self):
        self.assertEqual(cw._split_windows(0, 3600), [(0, 3600)])

    def test_split_windows_respects_24h_range_limit(self):
        windows = cw._split_windows(0, 3 * 86400)
        self.assertGreater(len(windows), 3)
        self.assertEqual(windows[0][0], 0)
        self.assertEqual(windows[-1][1], 3 * 86400)
        for (s1, e1), (s2, _e2) in zip(windows, windows[1:]):
            self.assertEqual(e1, s2)
        self.assertTrue(all(e - s <= cw.MAX_WINDOW_SECS for s, e in windows))

    def test_shards_small_run_needs_no_task_filter(self):
        self.assertEqual(cw._shards([str(i) for i in range(10)], 1), [None])

    def test_shards_large_run_respects_series_limit(self):
        ids = [str(i) for i in range(1000)]
        shards = cw._shards(ids, 2)
        self.assertEqual(sum(len(s) for s in shards), 1000)
        self.assertTrue(all(len(s) * 2 <= cw.MAX_SERIES_PER_QUERY for s in shards))

    def test_value_ignores_non_finite_and_malformed(self):
        self.assertIsNone(cw._value({"value": [0, "NaN"]}))
        self.assertIsNone(cw._value({"value": [0, "+Inf"]}))
        self.assertIsNone(cw._value({}))
        self.assertEqual(cw._value({"value": [0, "1.5"]}), 1.5)


class _Resp:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestPromQLClient(unittest.TestCase):
    def _client(self, responses):
        session = MagicMock()
        session.get_credentials.return_value = Credentials("AKIDEXAMPLE", "secret", "token")
        http = MagicMock()
        http.post.side_effect = responses
        sleeps: list[float] = []
        client = PromQLClient(session, "us-east-1", http=http, sleep=sleeps.append)
        return client, http, sleeps

    def test_successful_query_is_signed_post(self):
        ok = _Resp(200, {"status": "success", "data": {"result": [_sample(1)]}})
        client, http, _ = self._client([ok])
        self.assertEqual(client.query("up", 10.0), [_sample(1)])
        url = http.post.call_args.args[0]
        headers = http.post.call_args.kwargs["headers"]
        self.assertEqual(url, "https://monitoring.us-east-1.amazonaws.com/api/v1/query")
        self.assertIn("Authorization", headers)
        self.assertIn("query=up", http.post.call_args.kwargs["data"])

    def test_access_denied_is_fatal(self):
        client, _, _ = self._client([_Resp(403, {"message": "denied"})])
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            client.query("up", 1.0)
        self.assertTrue(ctx.exception.fatal)
        self.assertIn("cloudwatch:GetMetricData", str(ctx.exception))

    def test_unknown_operation_is_fatal(self):
        client, _, _ = self._client([_Resp(404, text="<UnknownOperationException/>")])
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            client.query("up", 1.0)
        self.assertTrue(ctx.exception.fatal)

    def test_connection_error_is_fatal(self):
        client, _, _ = self._client(requests.ConnectionError("no route"))
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            client.query("up", 1.0)
        self.assertTrue(ctx.exception.fatal)

    def test_throttling_retries_then_succeeds(self):
        ok = _Resp(200, {"status": "success", "data": {"result": []}})
        client, http, sleeps = self._client([_Resp(429, {"error": "slow"}), ok])
        self.assertEqual(client.query("up", 1.0), [])
        self.assertEqual(http.post.call_count, 2)
        self.assertEqual(len(sleeps), 1)

    def test_throttling_gives_up_with_non_fatal_error(self):
        client, http, _ = self._client([_Resp(429, {"error": "slow"})] * 4)
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            client.query("up", 1.0)
        self.assertFalse(ctx.exception.fatal)
        self.assertEqual(http.post.call_count, 4)

    def test_timeout_is_retried_and_not_fatal(self):
        client, http, sleeps = self._client([requests.Timeout("slow")] * 4)
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            client.query("up", 1.0)
        self.assertFalse(ctx.exception.fatal)
        self.assertEqual(http.post.call_count, 4)

    def test_limit_422_is_not_retried(self):
        client, http, _ = self._client([_Resp(422, {"error": "max series scanned exceeded"})])
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            client.query("up", 1.0)
        self.assertFalse(ctx.exception.fatal)
        self.assertEqual(http.post.call_count, 1)

    def test_throttling_422_is_retried(self):
        ok = _Resp(200, {"status": "success", "data": {"result": []}})
        client, http, _ = self._client([_Resp(422, {"error": "TPS limit exceeded"}), ok])
        self.assertEqual(client.query("up", 1.0), [])
        self.assertEqual(http.post.call_count, 2)

    def test_malformed_payloads_do_not_raise_unexpected_errors(self):
        client, _, _ = self._client(
            [
                _Resp(200, ["not", "a", "dict"]),
                _Resp(200, {"status": "success", "data": {"result": ["x", _sample(1)]}}),
                _Resp(200, {"status": "success", "data": None}),
            ]
        )
        with self.assertRaises(CloudWatchMetricsError):
            client.query("up", 1.0)
        self.assertEqual(client.query("up", 1.0), [_sample(1)])
        self.assertEqual(client.query("up", 1.0), [])

    def test_credential_refresh_failure_is_fatal(self):
        from botocore.exceptions import CredentialRetrievalError

        session = MagicMock()
        session.get_credentials.return_value.get_frozen_credentials.side_effect = (
            CredentialRetrievalError(provider="sso", error_msg="expired")
        )
        client = PromQLClient(session, "us-east-1", http=MagicMock())
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            client.query("up", 1.0)
        self.assertTrue(ctx.exception.fatal)

    def test_bad_query_is_not_fatal(self):
        client, _, _ = self._client([_Resp(400, {"error": "bad_data"})])
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            client.query("up", 1.0)
        self.assertFalse(ctx.exception.fatal)

    def test_error_status_in_payload(self):
        client, _, _ = self._client([_Resp(200, {"status": "error", "error": "boom"})])
        with self.assertRaisesRegex(CloudWatchMetricsError, "boom"):
            client.query("up", 1.0)

    def test_no_credentials_is_fatal(self):
        session = MagicMock()
        session.get_credentials.return_value = None
        with self.assertRaises(CloudWatchMetricsError) as ctx:
            PromQLClient(session, "us-east-1")
        self.assertTrue(ctx.exception.fatal)


class TestAttach(unittest.TestCase):
    def test_missing_metrics_explanation_is_given_once(self):
        fetcher, warnings = _make_fetcher(FakeClient(lambda expr: []))
        fetcher.attach(_resources("111"))
        fetcher.attach(_resources("222"))
        self.assertEqual(len(warnings), 2)
        self.assertIn("PutMetricData", warnings[0])
        self.assertNotIn("PutMetricData", warnings[1])

    def test_recent_run_without_metrics_is_not_marked_queried(self):
        warnings: list[str] = []
        fetcher = CloudWatchMetricsFetcher(
            MagicMock(),
            warn=warnings.append,
            client_factory=lambda region: FakeClient(lambda expr: []),
            now=lambda: cw._epoch(STOP) + 600,
        )
        resources = _resources("111")
        self.assertFalse(fetcher.attach(resources))
        self.assertNotIn(CW_KEY, resources[0])
        self.assertIn("may still be arriving", warnings[0])

    def test_incomplete_results_are_requeried(self):
        calls = {"fail": True}

        def handler(expr):
            if calls["fail"] and cw.MEM_USAGE in expr:
                raise CloudWatchMetricsError("throttled", transient=True)
            return _rich_handler(expr)

        client = FakeClient(handler)
        fetcher, _ = _make_fetcher(client)
        resources = _resources("111")
        fetcher.attach(resources)
        self.assertTrue(resources[0][cw.CW_INCOMPLETE_KEY])
        self.assertNotIn("memoryMaximumGiB", resources[1][CW_KEY])
        calls["fail"] = False
        reloaded = json.loads(json.dumps(resources))
        self.assertTrue(fetcher.attach(reloaded))
        self.assertNotIn(cw.CW_INCOMPLETE_KEY, reloaded[0])
        self.assertAlmostEqual(reloaded[1][CW_KEY]["memoryMaximumGiB"], 7.5)

    def test_truncated_response_is_treated_as_failure(self):
        def handler(expr):
            if f'"{cw.CPU_USAGE}"' in expr and "max_over_time" in expr and "bool" not in expr:
                return [_sample(1, **{T: str(i)}) for i in range(500)]
            return _rich_handler(expr)

        fetcher, warnings = _make_fetcher(FakeClient(handler))
        resources = _resources("111")
        fetcher.attach(resources)
        self.assertNotIn("cpusMaximum", resources[1][CW_KEY])
        self.assertTrue(any("truncated" in w for w in warnings))
        # deterministic: re-querying would give the same result, so not incomplete
        self.assertNotIn(cw.CW_INCOMPLETE_KEY, resources[0])

    def test_non_transient_failure_does_not_mark_incomplete(self):
        def handler(expr):
            if cw.MEM_USAGE in expr:
                raise CloudWatchMetricsError("limit exceeded")
            return _rich_handler(expr)

        fetcher, _ = _make_fetcher(FakeClient(handler))
        resources = _resources("111")
        fetcher.attach(resources)
        self.assertNotIn(cw.CW_INCOMPLETE_KEY, resources[0])

    def test_failed_requery_keeps_previous_metrics(self):
        resources = _resources("111")
        resources[0][CW_KEY] = {}
        resources[0][cw.CW_INCOMPLETE_KEY] = True
        resources[1][CW_KEY] = {"cpusP95": 2.0}

        def factory(region):
            raise CloudWatchMetricsError("no AWS credentials available", fatal=True)

        fetcher = CloudWatchMetricsFetcher(
            MagicMock(), warn=lambda m: None, client_factory=factory, now=lambda: NOW
        )
        self.assertTrue(fetcher.attach(resources))
        self.assertEqual(resources[1][CW_KEY], {"cpusP95": 2.0})
        self.assertTrue(resources[0][cw.CW_INCOMPLETE_KEY])

    def test_unexpected_exception_never_escapes(self):
        def handler(expr):
            raise RuntimeError("boom")

        fetcher, warnings = _make_fetcher(FakeClient(handler))
        self.assertFalse(fetcher.attach(_resources("111")))
        self.assertIsNone(fetcher.utilization(_resources("111")))
        self.assertEqual(len(warnings), 2)

    def test_shard_sizing_counts_all_run_tasks(self):
        executed = [str(1000000 + i) for i in range(400)]
        resources = _resources(*executed)
        resources += [_task(str(2000000 + i), stop=None) for i in range(100)]
        client = FakeClient(_rich_handler)
        fetcher, _ = _make_fetcher(client)
        fetcher.attach(resources)
        cpu_queries = [
            e for e, _ in client.queries if f'"{cw.CPU_USAGE}"' in e and "max_over_time" in e
        ]
        self.assertTrue(all(f'"{T}"=~' in e for e in cpu_queries if "bool" not in e))

    def test_counters_sum_series_that_differ_in_other_labels(self):
        def handler(expr):
            if expr.startswith("count(count_over_time("):
                return [{"metric": {}, "value": [NOW, "1"]}]
            if expr.startswith("max_over_time") and f'"{cw.FS_IO}"' in expr:
                base = {T: "111", cw.FS_DIRECTION_LABEL: "read", "__temporality__": "cumulative"}
                return [
                    _sample(1 * GIB, **base, device="a"),
                    _sample(2 * GIB, **base, device="b"),
                ]
            return []

        fetcher, _ = _make_fetcher(FakeClient(handler))
        resources = _resources("111")
        fetcher.attach(resources)
        self.assertAlmostEqual(resources[1][CW_KEY]["fsReadGiB"], 3)

    def test_run_without_metrics_falls_back_with_one_notice(self):
        client = FakeClient(lambda expr: [])
        fetcher, warnings = _make_fetcher(client)
        resources = _resources("111", "222")
        self.assertFalse(fetcher.attach(resources))
        self.assertEqual(len(client.queries), 1)  # only the probe
        self.assertEqual(len(warnings), 1)
        self.assertIn("no CloudWatch run metrics", warnings[0])
        self.assertTrue(all(res[CW_KEY] == {} for res in resources))

    def test_metrics_are_attached_and_converted(self):
        fetcher, warnings = _make_fetcher(FakeClient(_rich_handler))
        resources = _resources("111", "222")
        self.assertTrue(fetcher.attach(resources))
        self.assertEqual(warnings, [])
        cpu_task = resources[1][CW_KEY]
        self.assertAlmostEqual(cpu_task["cpusMaximum"], 3.9)
        self.assertAlmostEqual(cpu_task["cpusP95"], 3.5)
        self.assertAlmostEqual(cpu_task["cpusAverage"], 2.5)
        self.assertAlmostEqual(cpu_task["cpuIdleFraction"], 0.25)
        self.assertAlmostEqual(cpu_task["memoryMaximumGiB"], 7.5)
        self.assertAlmostEqual(cpu_task["memoryAverageGiB"], 2.0)
        self.assertAlmostEqual(cpu_task["memoryLimitGiB"], 7.8)
        self.assertEqual(cpu_task["scratchStorageMode"], "LOCAL")
        self.assertAlmostEqual(cpu_task["scratchMaximumGiB"], 95)
        self.assertAlmostEqual(cpu_task["scratchLimitGiB"], 100)
        self.assertAlmostEqual(cpu_task["fsReadGiB"], 3)
        self.assertAlmostEqual(cpu_task["fsWriteGiB"], 1)
        self.assertEqual(cpu_task["sampleCount"], 4)
        gpu_task = resources[2][CW_KEY]
        self.assertEqual(gpu_task["gpusObserved"], 2)
        self.assertEqual(gpu_task["gpusIdle"], 1)
        self.assertEqual(gpu_task["gpuUtilizationMaximum"], 80)
        self.assertAlmostEqual(gpu_task["gpuUtilizationAverage"], 40.5)
        self.assertAlmostEqual(gpu_task["gpuMemoryMaximumGiB"], 10)
        self.assertNotIn("cpusMaximum", gpu_task)
        self.assertAlmostEqual(resources[0][CW_KEY]["storageMaximumGiB"], 50)

    def test_queries_target_the_run_and_window(self):
        client = FakeClient(_rich_handler)
        fetcher, _ = _make_fetcher(client)
        fetcher.attach(_resources("111"))
        stop = cw._epoch(STOP) + cw.WINDOW_PAD_SECS
        for expr, at in client.queries:
            self.assertIn(f'"{cw.RUN_LABEL}"="1234567"', expr)
            self.assertNotIn("{sel}", expr)
            self.assertNotIn("{rng}", expr)
            self.assertEqual(at, stop)
        self.assertTrue(any("[3720s]" in expr for expr, _ in client.queries))

    def test_partial_task_coverage_is_reported(self):
        fetcher, warnings = _make_fetcher(FakeClient(_rich_handler))
        resources = _resources("111", "222", "333")
        self.assertTrue(fetcher.attach(resources))
        self.assertEqual(resources[3][CW_KEY], {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("1 of 3 executed tasks", warnings[0])

    def test_call_cached_tasks_are_not_queried_or_reported(self):
        client = FakeClient(_rich_handler)
        fetcher, warnings = _make_fetcher(client)
        resources = _resources("111", "222")
        resources.append(_task("444", start=None, stop=None))
        fetcher.attach(resources)
        self.assertNotIn(CW_KEY, resources[3])
        self.assertEqual(warnings, [])

    def test_failed_metric_query_only_drops_that_metric(self):
        def handler(expr):
            if cw.MEM_USAGE in expr and "max_over_time" in expr:
                raise CloudWatchMetricsError("limit exceeded")
            return _rich_handler(expr)

        fetcher, warnings = _make_fetcher(FakeClient(handler))
        resources = _resources("111")
        self.assertTrue(fetcher.attach(resources))
        self.assertNotIn("memoryMaximumGiB", resources[1][CW_KEY])
        self.assertIn("cpusMaximum", resources[1][CW_KEY])
        self.assertTrue(any("memMax" in w for w in warnings))

    def test_fatal_error_disables_fetcher(self):
        def handler(expr):
            raise CloudWatchMetricsError("access denied", fatal=True)

        client = FakeClient(handler)
        fetcher, warnings = _make_fetcher(client)
        self.assertFalse(fetcher.attach(_resources("111")))
        self.assertTrue(fetcher.disabled)
        n = len(client.queries)
        self.assertFalse(fetcher.attach(_resources("111")))
        self.assertEqual(len(client.queries), n)
        self.assertEqual(len(warnings), 1)
        self.assertIn("access denied", warnings[0])

    def test_client_creation_failure_is_handled(self):
        def factory(region):
            raise CloudWatchMetricsError("no AWS credentials available", fatal=True)

        warnings: list[str] = []
        fetcher = CloudWatchMetricsFetcher(
            MagicMock(), warn=warnings.append, client_factory=factory, now=lambda: NOW
        )
        self.assertFalse(fetcher.attach(_resources("111")))
        self.assertTrue(fetcher.disabled)
        self.assertEqual(len(warnings), 1)

    def test_runs_beyond_retention_are_not_queried(self):
        client = FakeClient(_rich_handler)
        fetcher, warnings = _make_fetcher(client)
        old = _resources("111", start="2024-01-01T00:00:00.000Z", stop="2024-01-01T01:00:00.000Z")
        self.assertFalse(fetcher.attach(old))
        self.assertEqual(client.queries, [])
        self.assertIn("retention", warnings[0])

    def test_already_attached_resources_are_not_queried(self):
        client = FakeClient(_rich_handler)
        fetcher, _ = _make_fetcher(client)
        resources = _resources("111")
        resources[1][CW_KEY] = {"cpusMaximum": 1.0}
        self.assertTrue(fetcher.attach(resources))
        self.assertEqual(client.queries, [])

    def test_unrecognized_run_arn_is_skipped(self):
        client = FakeClient(_rich_handler)
        fetcher, warnings = _make_fetcher(client)
        resources = _resources("111")
        resources[0]["arn"] = "arn:aws:omics:us-east-1:123456789012:run/abc"
        self.assertFalse(fetcher.attach(resources))
        self.assertEqual(client.queries, [])
        self.assertEqual(len(warnings), 1)

    def test_no_run_resource_is_skipped(self):
        fetcher, warnings = _make_fetcher(FakeClient(_rich_handler))
        self.assertFalse(fetcher.attach([_task("111")]))
        self.assertEqual(len(warnings), 1)

    def test_long_runs_are_split_and_combined(self):
        windows_seen: set[float] = set()

        def handler(expr):
            if expr.startswith("count(count_over_time("):
                return [{"metric": {}, "value": [NOW, "1"]}]
            if f'"{cw.CPU_USAGE}"' in expr and "bool" not in expr:
                if "max_over_time" in expr:
                    return [_sample(1 + len(windows_seen), **{T: "111"})]
                if "count_over_time" in expr:
                    return [_sample(10, **{T: "111"})]
                if "sum_over_time" in expr:
                    return [_sample(20, **{T: "111"})]
            return []

        client = FakeClient(handler)
        orig = client.query

        def query(expr, at):
            windows_seen.add(at)
            return orig(expr, at)

        client.query = query  # type: ignore[method-assign]
        fetcher, _ = _make_fetcher(client)
        resources = _resources(
            "111", start="2026-10-10T00:00:00.000Z", stop="2026-10-12T12:00:00.000Z"
        )
        self.assertTrue(fetcher.attach(resources))
        self.assertGreaterEqual(len(windows_seen), 3)
        for expr, _ in client.queries:
            rng = re.search(r"\[(\d+)s", expr)
            if rng:
                self.assertLessEqual(int(rng.group(1)), 86400)
        metrics = resources[1][CW_KEY]
        self.assertAlmostEqual(metrics["cpusAverage"], 2.0)  # sum/count over all windows
        self.assertGreater(metrics["cpusMaximum"], 1)  # max across windows
        self.assertEqual(metrics["sampleCount"], 10 * len(windows_seen))

    def test_large_runs_are_sharded_by_task_id(self):
        ids = [str(1000000 + i) for i in range(1000)]

        def handler(expr):
            if expr.startswith("count(count_over_time("):
                return [{"metric": {}, "value": [NOW, "1"]}]
            m = re.search(r'"' + re.escape(T) + r'"=~"([0-9|]+)"', expr)
            if f'"{cw.CPU_USAGE}"' in expr and "max_over_time" in expr and "bool" not in expr:
                self.assertIsNotNone(m)
                return [_sample(1.0, **{T: tid}) for tid in m.group(1).split("|")]
            return []

        client = FakeClient(handler)
        fetcher, warnings = _make_fetcher(client)
        resources = _resources(*ids)
        self.assertTrue(fetcher.attach(resources))
        self.assertTrue(all("cpusMaximum" in r[CW_KEY] for r in resources[1:]))
        self.assertEqual(warnings, [])

    def test_delta_counters_are_summed(self):
        def handler(expr):
            if expr.startswith("count(count_over_time("):
                return [{"metric": {}, "value": [NOW, "1"]}]
            if f'"{cw.NET_IO}"' in expr:
                if expr.startswith("max_over_time"):
                    labels = {T: "111", cw.NET_DIRECTION_LABEL: "receive"}
                    return [_sample(1 * GIB, **labels, __temporality__="delta")]
                if "sum_over_time" in expr:
                    return [_sample(6 * GIB, **{T: "111", cw.NET_DIRECTION_LABEL: "receive"})]
            return []

        fetcher, _ = _make_fetcher(FakeClient(handler))
        resources = _resources("111")
        fetcher.attach(resources)
        self.assertAlmostEqual(resources[1][CW_KEY]["netReceiveGiB"], 6)


class TestUtilization(unittest.TestCase):
    def test_series_are_aligned_with_gaps(self):
        def handler(expr):
            if expr.startswith("count(count_over_time("):
                return [{"metric": {}, "value": [NOW, "1"]}]
            if f'"{cw.CPU_USAGE}"' in expr:
                return [{"metric": {}, "values": [[100, "1.5"], [130, "2"]]}]
            if f'"{cw.CPU_LIMIT}"' in expr:
                return [{"metric": {}, "values": [[130, "4"]]}]
            return []

        client = FakeClient(handler)
        fetcher, _ = _make_fetcher(client)
        result = fetcher.utilization(_resources("111"))
        self.assertEqual(result["time"], [100.0, 130.0])
        self.assertEqual(result["cpuUsage"], [1.5, 2.0])
        self.assertEqual(result["cpuLimit"], [None, 4.0])
        self.assertEqual(result["memoryUsageGiB"], [None, None])
        for expr, _start, _end, step in client.range_queries:
            self.assertIn("last_over_time", expr)
            self.assertIn(f'max by ("{T}")', expr)
            self.assertGreaterEqual(step, cw.SAMPLE_STEP_SECS)

    def test_no_data_returns_none(self):
        fetcher, _ = _make_fetcher(FakeClient(lambda expr: []))
        self.assertIsNone(fetcher.utilization(_resources("111")))

    def test_fatal_error_returns_none(self):
        def handler(expr):
            raise CloudWatchMetricsError("denied", fatal=True)

        fetcher, warnings = _make_fetcher(FakeClient(handler))
        self.assertIsNone(fetcher.utilization(_resources("111")))
        self.assertTrue(fetcher.disabled)
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
