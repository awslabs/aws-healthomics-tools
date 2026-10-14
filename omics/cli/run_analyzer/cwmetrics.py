"""Retrieve HealthOmics run metrics from CloudWatch for the run analyzer.

HealthOmics vends near real-time OpenTelemetry resource-utilization metrics
(``aws.omics.*``) for runs and tasks into the account that ran the workflow, see
https://docs.aws.amazon.com/omics/latest/dev/monitoring-run-metrics.html. They
are queried with PromQL through CloudWatch's Prometheus-compatible HTTP API,
which requires ``cloudwatch:GetMetricData`` and ``cloudwatch:ListMetrics`` and
is billed per million samples scanned.

Metrics are frequently unavailable: runs that predate run metrics, run roles
without ``cloudwatch:PutMetricData``, runs with ``LogLevel=OFF``, runs from
another account, runs older than the 15-month retention, regions without PromQL
and tasks shorter than ~30 seconds. Every one of these cases degrades to the
manifest-only analysis; nothing here is allowed to fail the run analyzer.

Results are stored on each resource under ``cloudwatchMetrics`` (natural units:
vCPUs, GiB, percent, operations) so that ``--show`` output can be reloaded with
``--file`` without querying CloudWatch again.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode

import dateutil.parser  # type: ignore[import-untyped]
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError

logger = logging.getLogger(__name__)

CW_KEY = "cloudwatchMetrics"
# Set on the run resource when some queries failed, so a later --file run re-queries.
CW_INCOMPLETE_KEY = "cloudwatchMetricsIncomplete"

RUN_LABEL = "@resource.aws.omics.run.id"
TASK_LABEL = "@resource.aws.omics.task.id"
GPU_LABEL = "gpu.id"
SCRATCH_MODE_LABEL = "scratch.storage.mode"
FS_DIRECTION_LABEL = "filesystem.io.direction"
NET_DIRECTION_LABEL = "network.io.direction"
TEMPORALITY_LABEL = "__temporality__"

CPU_USAGE = "aws.omics.task.cpu.usage"
CPU_LIMIT = "aws.omics.task.cpu.limit"
MEM_USAGE = "aws.omics.task.memory.usage"
MEM_LIMIT = "aws.omics.task.memory.limit"
SCRATCH_USAGE = "aws.omics.task.filesystem.scratch.storage.usage"
SCRATCH_LIMIT = "aws.omics.task.filesystem.scratch.storage.limit"
GPU_UTIL = "aws.omics.task.gpu.utilization"
GPU_MEM_USAGE = "aws.omics.task.gpu.memory.usage"
GPU_MEM_LIMIT = "aws.omics.task.gpu.memory.limit"
FS_IO = "aws.omics.task.filesystem.io"
FS_OPS = "aws.omics.task.filesystem.operations"
NET_IO = "aws.omics.task.network.io"
RUN_FS_USAGE = "aws.omics.run.filesystem.usage"
RUN_FS_LIMIT = "aws.omics.run.filesystem.limit"

GIB = 1024.0**3
# CloudWatch rejects range selectors longer than 24 hours; stay just under it.
MAX_WINDOW_SECS = 86400 - 60
# Pad the run window so the first and last 30-second samples are included.
WINDOW_PAD_SECS = 60
# CloudWatch returns at most 500 series per query; leave a margin.
MAX_SERIES_PER_QUERY = 450
RETENTION_DAYS = 455  # 15 months
# Metrics for very recent runs may still be arriving (run storage lags > 30 min).
SETTLE_SECS = 3600
# A CPU sample counts as idle below this fraction of the task's vCPU limit.
IDLE_CPU_FRACTION = 0.25
# A GPU whose peak utilization stays below this percentage is considered unused.
IDLE_GPU_PERCENT = 5.0
# Lookback used when sampling gauges so values do not linger after a task stops.
SAMPLE_LOOKBACK = "45s"
SAMPLE_STEP_SECS = 30
MAX_RANGE_POINTS = 1000

_NUMERIC_ID = re.compile(r"^[0-9]+$")
# 422 is also used for deterministic limit errors; only these are worth retrying.
_RETRYABLE_422 = re.compile(
    r"throttl|rate exceeded|tps|concurren|too many requests|timeout", re.IGNORECASE
)


class CloudWatchMetricsError(Exception):
    """A CloudWatch PromQL request failed.

    ``fatal`` errors (access denied, endpoint unavailable, no credentials) mean
    no further queries can succeed in this session/region.
    """

    def __init__(self, msg: str, fatal: bool = False, transient: bool = False):
        """Create an error; ``fatal`` disables further queries.

        ``transient`` errors (timeouts, throttling, server errors) may succeed
        later, so results affected by them are marked for re-querying.
        """
        super().__init__(msg)
        self.fatal = fatal
        self.transient = transient


# --------------------------------------------------------------------------- #
# PromQL HTTP client
# --------------------------------------------------------------------------- #


class PromQLClient:
    """Minimal SigV4-signed client for CloudWatch's Prometheus-compatible API."""

    def __init__(
        self,
        session,
        region: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        http=None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """Create a client for the CloudWatch PromQL endpoint in *region*."""
        self._credentials = session.get_credentials()
        if self._credentials is None:
            raise CloudWatchMetricsError("no AWS credentials available", fatal=True)
        self._region = region
        self._base_url = f"https://monitoring.{region}.amazonaws.com/api/v1/"
        self._timeout = timeout
        self._max_retries = max_retries
        self._http = http or requests.Session()
        self._sleep = sleep

    def query(self, expr: str, at: float) -> list[dict]:
        """Run an instant query; return the result vector."""
        return self._post("query", {"query": expr, "time": f"{at:.3f}"})

    def query_range(self, expr: str, start: float, end: float, step: int) -> list[dict]:
        """Run a range query; return the result matrix."""
        params = {"query": expr, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{step}s"}
        return self._post("query_range", params)

    def _post(self, operation: str, params: dict) -> list[dict]:
        url = self._base_url + operation
        body = urlencode(params)
        for attempt in range(self._max_retries + 1):
            req = AWSRequest(
                method="POST",
                url=url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                # Freeze per request so a refresh cannot mix key and token mid-signing.
                creds = self._credentials.get_frozen_credentials()
                SigV4Auth(creds, "monitoring", self._region).add_auth(req)
            except BotoCoreError as e:
                raise CloudWatchMetricsError(f"cannot obtain AWS credentials: {e}", fatal=True)
            try:
                resp = self._http.post(
                    url, data=body, headers=dict(req.headers.items()), timeout=self._timeout
                )
            except requests.Timeout as e:
                if attempt < self._max_retries:
                    self._backoff(attempt, operation, "timeout")
                    continue
                raise CloudWatchMetricsError(f"CloudWatch query timed out: {e}", transient=True)
            except requests.RequestException as e:
                raise CloudWatchMetricsError(
                    f"cannot reach the CloudWatch PromQL endpoint in {self._region}: {e}",
                    fatal=True,
                )
            status = resp.status_code
            if status == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    raise CloudWatchMetricsError("CloudWatch returned a non-JSON response")
                if not isinstance(payload, dict) or payload.get("status") != "success":
                    error = payload.get("error") if isinstance(payload, dict) else payload
                    raise CloudWatchMetricsError(str(error or "query failed"))
                data = payload.get("data")
                result = data.get("result") if isinstance(data, dict) else None
                if not isinstance(result, list):
                    return []
                return [r for r in result if isinstance(r, dict)]
            message = _error_message(resp)
            if status in (401, 403):
                raise CloudWatchMetricsError(
                    "access denied querying CloudWatch run metrics (requires "
                    f"cloudwatch:GetMetricData and cloudwatch:ListMetrics): {message}",
                    fatal=True,
                )
            if status == 404:
                raise CloudWatchMetricsError(
                    f"CloudWatch PromQL querying is not available in {self._region}",
                    fatal=True,
                )
            retryable = (
                status == 429 or status >= 500 or (status == 422 and _RETRYABLE_422.search(message))
            )
            if retryable and attempt < self._max_retries:
                self._backoff(attempt, operation, f"HTTP {status}")
                continue
            raise CloudWatchMetricsError(
                f"CloudWatch query failed (HTTP {status}): {message}", transient=bool(retryable)
            )
        raise CloudWatchMetricsError("CloudWatch query failed")  # pragma: no cover

    def _backoff(self, attempt: int, operation: str, reason: str) -> None:
        delay = 0.5 * (2**attempt)
        logger.debug("PromQL %s failed (%s), retrying in %.1fs", operation, reason, delay)
        self._sleep(delay)


def _error_message(resp) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:200]
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("message") or payload)
    return str(payload)[:200]


# --------------------------------------------------------------------------- #
# Query specifications
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Spec:
    """One PromQL instant query evaluated per time window and task shard.

    ``expr`` is a template with ``{sel}`` (series selector labels) and ``{rng}``
    (window length in seconds). Results are keyed by the values of ``by``;
    windows are combined with ``combine`` (``max`` or ``sum``).
    """

    name: str
    expr: str
    by: Optional[tuple[str, ...]]  # None keys results by their full label set
    combine: str
    series_per_task: int = 1
    per_task: bool = True


def _sel(metric: str) -> str:
    return '{"' + metric + '",{sel}}'


def _grouped(func: str, metric: str, by: tuple[str, ...], agg: str = "max") -> str:
    labels = ", ".join(f'"{label}"' for label in by)
    return f"{agg} by ({labels}) ({func}({_sel(metric)}[{{rng}}s]))"


_T = (TASK_LABEL,)


def _idle_expr(func: str) -> str:
    """Subquery counting 30s samples where vCPU usage is below the idle threshold."""
    usage = f'max by ("{TASK_LABEL}") (last_over_time({_sel(CPU_USAGE)}[{SAMPLE_LOOKBACK}]))'
    limit = f'max by ("{TASK_LABEL}") (last_over_time({_sel(CPU_LIMIT)}[{SAMPLE_LOOKBACK}]))'
    inner = f"({usage} < bool {IDLE_CPU_FRACTION} * {limit})"
    return f'sum by ("{TASK_LABEL}") ({func}({inner}[{{rng}}s:{SAMPLE_STEP_SECS}s]))'


def _p95(metric: str) -> str:
    return f'max by ("{TASK_LABEL}") (quantile_over_time(0.95, {_sel(metric)}[{{rng}}s]))'


TASK_SPECS: tuple[_Spec, ...] = (
    _Spec("cpuMax", _grouped("max_over_time", CPU_USAGE, _T), _T, "max"),
    _Spec("cpuP95", _p95(CPU_USAGE), _T, "max"),
    _Spec("cpuSum", _grouped("sum_over_time", CPU_USAGE, _T, "sum"), _T, "sum"),
    _Spec("cpuCount", _grouped("count_over_time", CPU_USAGE, _T, "sum"), _T, "sum"),
    _Spec("cpuLimit", _grouped("max_over_time", CPU_LIMIT, _T), _T, "max"),
    _Spec("cpuIdleSum", _idle_expr("sum_over_time"), _T, "sum"),
    _Spec("cpuIdleCount", _idle_expr("count_over_time"), _T, "sum"),
    _Spec("memMax", _grouped("max_over_time", MEM_USAGE, _T), _T, "max"),
    _Spec("memP95", _p95(MEM_USAGE), _T, "max"),
    _Spec("memSum", _grouped("sum_over_time", MEM_USAGE, _T, "sum"), _T, "sum"),
    _Spec("memCount", _grouped("count_over_time", MEM_USAGE, _T, "sum"), _T, "sum"),
    _Spec("memLimit", _grouped("max_over_time", MEM_LIMIT, _T), _T, "max"),
    _Spec(
        "scratchMax",
        _grouped("max_over_time", SCRATCH_USAGE, (TASK_LABEL, SCRATCH_MODE_LABEL)),
        (TASK_LABEL, SCRATCH_MODE_LABEL),
        "max",
        series_per_task=2,
    ),
    _Spec("scratchLimit", _grouped("max_over_time", SCRATCH_LIMIT, _T), _T, "max"),
    _Spec(
        "gpuMax",
        _grouped("max_over_time", GPU_UTIL, (TASK_LABEL, GPU_LABEL)),
        (TASK_LABEL, GPU_LABEL),
        "max",
        series_per_task=8,
    ),
    _Spec("gpuSum", _grouped("sum_over_time", GPU_UTIL, _T, "sum"), _T, "sum"),
    _Spec("gpuCount", _grouped("count_over_time", GPU_UTIL, _T, "sum"), _T, "sum"),
    _Spec("gpuMemMax", _grouped("max_over_time", GPU_MEM_USAGE, _T), _T, "max"),
    _Spec("gpuMemLimit", _grouped("max_over_time", GPU_MEM_LIMIT, _T), _T, "max"),
)

RUN_SPECS: tuple[_Spec, ...] = (
    _Spec(
        "runFsMax", f"max(max_over_time({_sel(RUN_FS_USAGE)}[{{rng}}s]))", (), "max", per_task=False
    ),
    _Spec(
        "runFsLimit",
        f"max(max_over_time({_sel(RUN_FS_LIMIT)}[{{rng}}s]))",
        (),
        "max",
        per_task=False,
    ),
)


@dataclass(frozen=True)
class _CounterSpec:
    """A cumulative-or-delta sum metric split by a direction label."""

    name: str
    metric: str
    direction_label: str


COUNTER_SPECS: tuple[_CounterSpec, ...] = (
    _CounterSpec("fsBytes", FS_IO, FS_DIRECTION_LABEL),
    _CounterSpec("fsOps", FS_OPS, FS_DIRECTION_LABEL),
    _CounterSpec("netBytes", NET_IO, NET_DIRECTION_LABEL),
)

# Output keys for counter totals, by (spec name, direction).
_COUNTER_KEYS = {
    ("fsBytes", "read"): ("fsReadGiB", GIB),
    ("fsBytes", "write"): ("fsWriteGiB", GIB),
    ("fsOps", "read"): ("fsReadOps", 1.0),
    ("fsOps", "write"): ("fsWriteOps", 1.0),
    ("netBytes", "receive"): ("netReceiveGiB", GIB),
    ("netBytes", "transmit"): ("netTransmitGiB", GIB),
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _rtype(res: dict) -> str:
    parts = re.split(r"[:/]", res.get("arn", ""))
    return parts[-2] if len(parts) >= 2 else ""


def _resource_id(res: dict) -> str:
    return re.split(r"[:/]", res.get("arn", ""))[-1]


def _region(res: dict) -> str:
    parts = res.get("arn", "").split(":")
    return parts[3] if len(parts) > 3 else ""


def _epoch(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return dateutil.parser.parse(s).replace(tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None


def _split_windows(start: float, end: float) -> list[tuple[float, float]]:
    """Split [start, end] into consecutive windows no longer than MAX_WINDOW_SECS."""
    windows = []
    t = start
    while t < end:
        t2 = min(end, t + MAX_WINDOW_SECS)
        windows.append((t, t2))
        t = t2
    return windows


def _shards(
    task_ids: list[str], series_per_task: int, run_task_count: Optional[int] = None
) -> list[Optional[list[str]]]:
    """Split task ids so no query can exceed the series limit.

    ``None`` means "no task filter". That is only used when every task in the
    run (``run_task_count``, including tasks not being queried) fits in one
    response, because the run filter alone matches all of them.
    """
    size = max(1, MAX_SERIES_PER_QUERY // series_per_task)
    if max(len(task_ids), run_task_count or 0) <= size:
        return [None]
    return [task_ids[i : i + size] for i in range(0, len(task_ids), size)]


def _selector(run_id: str, task_ids: Optional[list[str]]) -> str:
    sel = f'"{RUN_LABEL}"="{run_id}"'
    if task_ids:
        sel += f', "{TASK_LABEL}"=~"{"|".join(task_ids)}"'
    return sel


def _value(sample: dict) -> Optional[float]:
    try:
        v = float(sample["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _combine(acc: dict, key: tuple, value: float, how: str) -> None:
    if key not in acc:
        acc[key] = value
    elif how == "sum":
        acc[key] += value
    else:
        acc[key] = max(acc[key], value)


# --------------------------------------------------------------------------- #
# Fetcher
# --------------------------------------------------------------------------- #


def _default_warn(msg: str) -> None:
    logger.warning(msg)


class CloudWatchMetricsFetcher:
    """Attach CloudWatch run metrics to run-analyzer resources.

    One fetcher is used per analyzer invocation; after a fatal error (access
    denied, endpoint unavailable) it stops querying and the analyzer continues
    with manifest data only.
    """

    def __init__(
        self,
        session,
        warn: Callable[[str], None] = _default_warn,
        client_factory: Optional[Callable[[str], PromQLClient]] = None,
        now: Callable[[], float] = time.time,
    ):
        """Create a fetcher; *warn* receives user-facing notices."""
        self._session = session
        self._warn = warn
        self._client_factory = client_factory or (lambda region: PromQLClient(session, region))
        self._clients: dict[str, PromQLClient] = {}
        self._now = now
        self.disabled = False
        self._explained_missing = False
        self._incomplete = False
        self._run_task_count = 0

    # ----------------------------------------------------------------- public

    def attach(self, resources: list[dict]) -> bool:
        """Query CloudWatch and store metrics on *resources* in place.

        Returns True when at least one task received CloudWatch metrics. Never
        raises; any problem is reported through *warn* and the analysis falls
        back to manifest values.
        """
        try:
            return self._attach(resources)
        except Exception as e:  # last resort: never fail the analyzer
            logger.debug("unexpected error attaching CloudWatch metrics", exc_info=True)
            self._warn(f"unexpected error reading CloudWatch metrics ({e}); using manifest values")
            return False

    def _attach(self, resources: list[dict]) -> bool:
        run_res = next((r for r in resources if _rtype(r) == "run"), None)
        if any(CW_KEY in res for res in resources):
            if run_res is None or not run_res.get(CW_INCOMPLETE_KEY):
                logger.debug("resources already carry CloudWatch metrics; not querying again")
                return any(res.get(CW_KEY) for res in resources)
            # A previous attempt was incomplete: query again, but keep the old
            # values if the new attempt cannot produce any.
            saved = [(res, res.pop(CW_KEY, None)) for res in resources]
            run_res.pop(CW_INCOMPLETE_KEY, None)
            if self._query(resources):
                return True
            for res, old in saved:
                res.pop(CW_KEY, None)
                if old is not None:
                    res[CW_KEY] = old
            run_res[CW_INCOMPLETE_KEY] = True
            return any(res.get(CW_KEY) for res in resources)
        return self._query(resources)

    def _query(self, resources: list[dict]) -> bool:
        ctx = self._context(resources)
        if ctx is None:
            return False
        run, run_id, tasks, windows, client = ctx
        label = f"run {run_id}"
        self._incomplete = False
        self._run_task_count = sum(1 for r in resources if _rtype(r) == "task")
        recent = windows[-1][1] > self._now() - SETTLE_SECS
        try:
            if not self._has_data(client, run_id, windows):
                self._warn_missing(label, recent)
                if not recent:
                    _mark_queried(run, tasks)
                return False
            task_values = self._fetch_task_values(client, run_id, tasks, windows)
            run_values = self._fetch_run_values(client, run_id, windows)
            counters = self._fetch_counters(client, run_id, tasks, windows)
        except CloudWatchMetricsError as e:
            self._fatal(e)
            return False

        _mark_queried(run, tasks)
        if self._incomplete or recent:
            run[CW_INCOMPLETE_KEY] = True
        run[CW_KEY] = _run_metrics(run_values)
        with_data = 0
        for task in tasks:
            tid = _resource_id(task)
            cw = _task_metrics(tid, task_values)
            cw.update(counters.get(tid, {}))
            task[CW_KEY] = cw
            if cw:
                with_data += 1
        missing = len(tasks) - with_data
        if missing:
            self._warn(
                f"{missing} of {len(tasks)} executed tasks in {label} have no CloudWatch "
                "metrics (tasks that run for less than ~30 seconds may not emit any); "
                "manifest values are used for those tasks"
            )
        return with_data > 0

    def utilization(self, resources: list[dict]) -> Optional[dict]:
        """Return run-wide vCPU and memory usage/limit time series for plotting.

        Result: ``{"time": [epoch secs], "cpuUsage": [...], "cpuLimit": [...],
        "memoryUsageGiB": [...], "memoryLimitGiB": [...]}`` with ``None`` for
        gaps, or None when no data is available. Never raises.
        """
        try:
            return self._utilization(resources)
        except Exception as e:  # last resort: never fail the plot
            logger.debug("unexpected error reading CloudWatch utilization", exc_info=True)
            self._warn(f"unexpected error reading CloudWatch metrics ({e}); plot has no overlay")
            return None

    def _utilization(self, resources: list[dict]) -> Optional[dict]:
        ctx = self._context(resources)
        if ctx is None:
            return None
        _run, run_id, _tasks, windows, client = ctx
        series_defs = {
            "cpuUsage": (CPU_USAGE, 1.0),
            "cpuLimit": (CPU_LIMIT, 1.0),
            "memoryUsageGiB": (MEM_USAGE, GIB),
            "memoryLimitGiB": (MEM_LIMIT, GIB),
        }
        total = windows[-1][1] - windows[0][0]
        step = max(SAMPLE_STEP_SECS, int(math.ceil(total / MAX_RANGE_POINTS)))
        values: dict[str, dict[float, float]] = {k: {} for k in series_defs}
        sel = _selector(run_id, None)
        try:
            if not self._has_data(client, run_id, windows):
                return None
            for name, (metric, scale) in series_defs.items():
                # max per task first so a task with several series counts once
                expr = (
                    f'sum(max by ("{TASK_LABEL}") '
                    f"(last_over_time({_sel(metric)}[{SAMPLE_LOOKBACK}])))"
                ).replace("{sel}", sel)
                for start, end in windows:
                    for series in client.query_range(expr, start, end, step):
                        for t, v in series.get("values", []):
                            try:
                                fv = float(v)
                            except (TypeError, ValueError):
                                continue
                            if math.isfinite(fv):
                                values[name][float(t)] = fv / scale
        except CloudWatchMetricsError as e:
            self._fatal(e)
            return None
        times = sorted(set().union(*[set(v) for v in values.values()]))
        if not times:
            return None
        result: dict = {"time": times}
        for name, by_time in values.items():
            result[name] = [by_time.get(t) for t in times]
        return result

    # ---------------------------------------------------------------- private

    def _context(self, resources: list[dict]):
        if self.disabled:
            return None
        run = next((r for r in resources if _rtype(r) == "run"), None)
        if run is None:
            self._warn("no run resource found; CloudWatch metrics not queried")
            return None
        run_id = _resource_id(run)
        region = _region(run)
        if not _NUMERIC_ID.match(run_id) or not region:
            self._warn(f"unrecognized run ARN {run.get('arn')}; CloudWatch metrics not queried")
            return None
        tasks = [
            t
            for t in resources
            if _rtype(t) == "task"
            and t.get("startTime")
            and t.get("stopTime")
            and _NUMERIC_ID.match(_resource_id(t))
        ]
        now = self._now()
        starts = [_epoch(run.get("startTime"))] + [_epoch(t.get("startTime")) for t in tasks]
        stops = [_epoch(run.get("stopTime"))] + [_epoch(t.get("stopTime")) for t in tasks]
        start_vals = [s for s in starts if s is not None]
        stop_vals = [s for s in stops if s is not None]
        if not start_vals:
            self._warn(f"run {run_id} has no start time; CloudWatch metrics not queried")
            return None
        start = min(start_vals) - WINDOW_PAD_SECS
        end = min(now, (max(stop_vals) if stop_vals else now) + WINDOW_PAD_SECS)
        if end <= start:
            return None
        if end < now - RETENTION_DAYS * 86400:
            self._warn(
                f"run {run_id} is older than the CloudWatch metric retention period "
                "(15 months); using manifest values only"
            )
            return None
        client = self._client(region)
        if client is None:
            return None
        return run, run_id, tasks, _split_windows(start, end), client

    def _client(self, region: str) -> Optional[PromQLClient]:
        if region not in self._clients:
            try:
                self._clients[region] = self._client_factory(region)
            except CloudWatchMetricsError as e:
                self._fatal(e)
                return None
        return self._clients[region]

    def _warn_missing(self, label: str, recent: bool) -> None:
        msg = f"no CloudWatch run metrics found for {label}; using manifest values only."
        if recent:
            msg += " The run finished recently and its metrics may still be arriving."
        if not self._explained_missing:
            self._explained_missing = True
            msg += (
                " Run metrics exist only for runs that started after HealthOmics began "
                "publishing them, whose service role allowed cloudwatch:PutMetricData, "
                "that did not use LogLevel=OFF, and only in the account that started the run."
            )
        self._warn(msg)

    def _fatal(self, e: CloudWatchMetricsError) -> None:
        if e.fatal:
            self.disabled = True
            self._warn(f"{e}; continuing with manifest values only")
        else:
            self._warn(f"CloudWatch metrics unavailable: {e}; using manifest values only")

    def _has_data(self, client: PromQLClient, run_id: str, windows) -> bool:
        sel = _selector(run_id, None)
        for start, end in windows:
            rng = int(math.ceil(end - start))
            expr = f"count(count_over_time({_sel(CPU_USAGE)}[{rng}s]))".replace("{sel}", sel)
            if client.query(expr, end):
                return True
        return False

    def _run_spec(
        self, client: PromQLClient, spec: _Spec, run_id: str, task_ids: list[str], windows
    ) -> dict:
        acc: dict = {}
        shards = (
            _shards(task_ids, spec.series_per_task, self._run_task_count)
            if spec.per_task
            else [None]
        )
        for start, end in windows:
            rng = int(math.ceil(end - start))
            for shard in shards:
                expr = spec.expr.replace("{sel}", _selector(run_id, shard)).replace(
                    "{rng}", str(rng)
                )
                result = client.query(expr, end)
                if len(result) >= 500:
                    raise CloudWatchMetricsError(
                        f"{len(result)} series returned, the response may be truncated"
                    )
                for sample in result:
                    value = _value(sample)
                    labels = sample.get("metric")
                    if value is None or not isinstance(labels, dict):
                        continue
                    key: tuple
                    if spec.by is None:
                        key = tuple(sorted((str(k), str(v)) for k, v in labels.items()))
                    else:
                        key = tuple(str(labels.get(label, "")) for label in spec.by)
                    _combine(acc, key, value, spec.combine)
        return acc

    def _run_specs(self, client, specs, run_id, task_ids, windows) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for spec in specs:
            try:
                out[spec.name] = self._run_spec(client, spec, run_id, task_ids, windows)
            except CloudWatchMetricsError as e:
                if e.fatal:
                    raise
                self._warn(f"CloudWatch query for {spec.name} failed ({e}); skipping it")
                self._incomplete = self._incomplete or e.transient
                out[spec.name] = {}
        return out

    def _fetch_task_values(self, client, run_id, tasks, windows) -> dict[str, dict]:
        task_ids = [_resource_id(t) for t in tasks]
        return self._run_specs(client, TASK_SPECS, run_id, task_ids, windows)

    def _fetch_run_values(self, client, run_id, windows) -> dict[str, dict]:
        return self._run_specs(client, RUN_SPECS, run_id, [], windows)

    def _fetch_counters(self, client, run_id, tasks, windows) -> dict[str, dict]:
        """Total bytes/operations per task and direction.

        The counters are published as cumulative sums (the running total is the
        series maximum). The temporality is read from the returned labels, and
        delta series (per-export totals) are summed instead.
        """
        task_ids = [_resource_id(t) for t in tasks]
        out: dict[str, dict] = {}
        for cspec in COUNTER_SPECS:
            # Unaggregated (temporality cannot be grouped on), keyed by the full
            # label set: max per series across windows, then summed per task and
            # direction so series that differ only in other labels all count.
            raw = _Spec(
                cspec.name,
                f"max_over_time({_sel(cspec.metric)}[{{rng}}s])",
                None,
                "max",
                series_per_task=4,
            )
            try:
                by_series = self._run_spec(client, raw, run_id, task_ids, windows)
                acc: dict[tuple[str, str, str], float] = {}
                for labels, v in by_series.items():
                    d = dict(labels)
                    key3 = (
                        d.get(TASK_LABEL, ""),
                        d.get(cspec.direction_label, ""),
                        d.get(TEMPORALITY_LABEL, ""),
                    )
                    acc[key3] = acc.get(key3, 0.0) + v
                if any(key[2] == "delta" for key in acc):
                    delta = _Spec(
                        cspec.name,
                        f'sum by ("{TASK_LABEL}", "{cspec.direction_label}") '
                        f"(sum_over_time({_sel(cspec.metric)}[{{rng}}s]))",
                        (TASK_LABEL, cspec.direction_label),
                        "sum",
                        series_per_task=4,
                    )
                    sums = self._run_spec(client, delta, run_id, task_ids, windows)
                    acc = {k: v for k, v in acc.items() if k[2] != "delta"}
                    for (tid, direction), v in sums.items():
                        acc[(tid, direction, "delta")] = v
            except CloudWatchMetricsError as e:
                if e.fatal:
                    raise
                self._warn(f"CloudWatch query for {cspec.name} failed ({e}); skipping it")
                self._incomplete = self._incomplete or e.transient
                continue
            totals: dict[tuple[str, str], float] = {}
            for (tid, direction, _temporality), v in acc.items():
                totals[(tid, direction)] = totals.get((tid, direction), 0.0) + v
            for (tid, direction), v in totals.items():
                key_scale = _COUNTER_KEYS.get((cspec.name, direction))
                if key_scale is None or not tid:
                    continue
                key, scale = key_scale
                out.setdefault(tid, {})[key] = v / scale
        return out


def _mark_queried(run: dict, tasks: list[dict]) -> None:
    """Record that CloudWatch was queried so --file reloads do not query again."""
    run[CW_KEY] = {}
    for task in tasks:
        task[CW_KEY] = {}


def _get(values: dict[str, dict], name: str, key: tuple) -> Optional[float]:
    return values.get(name, {}).get(key)


def _ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or not den:
        return None
    return num / den


def _task_metrics(tid: str, v: dict[str, dict]) -> dict:
    """Convert raw query values for one task into ``cloudwatchMetrics``."""
    k = (tid,)
    cw: dict = {}

    def put(name: str, value: Optional[float], scale: float = 1.0) -> None:
        if value is not None:
            cw[name] = value / scale

    put("cpusMaximum", _get(v, "cpuMax", k))
    put("cpusP95", _get(v, "cpuP95", k))
    put("cpusAverage", _ratio(_get(v, "cpuSum", k), _get(v, "cpuCount", k)))
    put("cpusLimit", _get(v, "cpuLimit", k))
    put("cpuIdleFraction", _ratio(_get(v, "cpuIdleSum", k), _get(v, "cpuIdleCount", k)))
    put("memoryMaximumGiB", _get(v, "memMax", k), GIB)
    put("memoryP95GiB", _get(v, "memP95", k), GIB)
    mem_avg = _ratio(_get(v, "memSum", k), _get(v, "memCount", k))
    put("memoryAverageGiB", mem_avg, GIB)
    put("memoryLimitGiB", _get(v, "memLimit", k), GIB)
    count = _get(v, "cpuCount", k)
    if count is not None:
        cw["sampleCount"] = int(count)

    scratch = {key[1]: val for key, val in v.get("scratchMax", {}).items() if key[0] == tid}
    if scratch:
        mode, peak = max(scratch.items(), key=lambda kv: kv[1])
        cw["scratchMaximumGiB"] = peak / GIB
        if mode:
            cw["scratchStorageMode"] = mode
    put("scratchLimitGiB", _get(v, "scratchLimit", k), GIB)

    gpus = {key[1]: val for key, val in v.get("gpuMax", {}).items() if key[0] == tid}
    if gpus:
        cw["gpusObserved"] = len(gpus)
        cw["gpusIdle"] = sum(1 for val in gpus.values() if val < IDLE_GPU_PERCENT)
        cw["gpuUtilizationMaximum"] = max(gpus.values())
        put("gpuUtilizationAverage", _ratio(_get(v, "gpuSum", k), _get(v, "gpuCount", k)))
        put("gpuMemoryMaximumGiB", _get(v, "gpuMemMax", k), GIB)
        put("gpuMemoryLimitGiB", _get(v, "gpuMemLimit", k), GIB)
    return cw


def _run_metrics(v: dict[str, dict]) -> dict:
    cw: dict = {}
    peak = _get(v, "runFsMax", ())
    if peak is not None:
        cw["storageMaximumGiB"] = peak / GIB
    limit = _get(v, "runFsLimit", ())
    if limit is not None:
        cw["storageLimitGiB"] = limit / GIB
    return cw
