# Plan: CloudWatch run-metrics intelligence for the run analyzer

## Goal

Let `omics-run-analyzer` enrich its manifest-based analysis with the HealthOmics
vended OpenTelemetry run metrics (`aws.omics.*`, see
https://docs.aws.amazon.com/omics/latest/dev/monitoring-run-metrics.html) and use
them to produce better sizing recommendations, while degrading gracefully to the
existing manifest-only behaviour whenever the metrics are absent.

## Verified facts (probed against a live account, Oct 2026)

| Fact | Consequence |
|---|---|
| Query via Prometheus-compatible HTTP API `https://monitoring.<region>.amazonaws.com/api/v1/{query,query_range}`, SigV4 service `monitoring`; POST form bodies sign cleanly | `botocore` SigV4 + `requests` (both existing deps); always POST |
| Data is only in the run's own region | Endpoint region = region of the run ARN |
| A range selector is limited to **24 h** (`Range selector ... exceeds 86400000ms limit`) | Runs longer than 24 h are queried in ≤24 h windows and combined |
| Max 500 series per response | Large runs are sharded by explicit task-id regex (`"@resource.aws.omics.task.id"=~"id1|id2|..."`) |
| `@resource.aws.omics.task.id` equals the trailing id of the manifest task ARN | Join key |
| `filesystem.io`, `filesystem.operations`, `network.io` are **cumulative**, monotonic sums; `__temporality__` appears in result labels but cannot be used as a matcher | Totals = `max_over_time`; temporality checked from returned labels, `sum_over_time` fallback if delta |
| CloudWatch memory can differ greatly from manifest memory (e.g. 0.30 GiB vs 1.27 GiB) | CloudWatch values are reported separately (`cw*` columns) and preferred for recommendations, with the source reported |
| Runs with no metrics return an empty result, not an error | Cheap probe query per run gates all other queries |
| IAM: `cloudwatch:GetMetricData` + `cloudwatch:ListMetrics`; billed per million samples scanned; 15-month retention | Opt-in `--cloudwatch` flag; skip runs older than retention |

## Graceful handling of missing metrics (cross-cutting requirement)

Metrics can be missing at every level; each is handled without failing the analysis:

| Situation | Behaviour |
|---|---|
| Flag not given | No CloudWatch calls; output identical to today |
| Run predates run metrics / role lacked `cloudwatch:PutMetricData` / `LogLevel=OFF` / other account | Probe query empty → one stderr notice, manifest-only output |
| Run older than 15-month retention | No queries; one notice |
| Region without PromQL (e.g. `il-central-1`), network error | One notice, CloudWatch disabled for the session |
| `AccessDenied` on query | One notice naming the required IAM actions, disabled for the session |
| Throttling (429 / TPS 422) | Bounded exponential-backoff retry, then treat metric as missing |
| A single query fails (bad data / limit exceeded) | That metric is missing; others still used |
| Some tasks have no data (tasks < 30 s, call-cached tasks) | Per-task fallback; summary notice with count |
| Some metrics missing for a task (no GPU, no scratch limit in SHARED mode, DYNAMIC storage delay) | Per-field fallback; blank cells |
| Re-analysis with `--file --cloudwatch` | Embedded values are reused without API calls, unless marked incomplete (failed queries / run finished < 1 h ago), then re-queried. Without the flag embedded values are ignored |
| Unexpected errors (credential refresh, malformed responses) | Caught at the fetcher boundary; notice, manifest-only output |
| Timeouts | Retried; then that metric is missing (not fatal) |

Missing values are never treated as zero (in particular in batch aggregation).

## Phase 1 – sizing intelligence (CPU, memory, scratch, GPU)

1. New module `omics/cli/run_analyzer/cwmetrics.py`
   - `PromQLClient`: SigV4-signed POST to `/api/v1/query` and `/api/v1/query_range`, retries, error classification (`CloudWatchMetricsError` with `fatal` flag for access-denied / endpoint failures).
   - `attach_cloudwatch_metrics(session, resources, warn)`: computes the run window (run start − 60 s → run stop + 60 s, or now if still running), splits it into ≤24 h windows, probes for data, shards tasks (≤400 per query, fewer for multi-series queries), runs one instant query per metric per window/shard and stores results on each resource under `cloudwatchMetrics` (raw, in natural units: vCPUs, GiB, %).
   - Per task: `cpusMaximum`, `cpusP95`, `cpusAverage`, `cpusLimit`, `cpuIdleFraction` (share of 30 s samples below 25 % of the limit), `memoryMaximumGiB`, `memoryP95GiB`, `memoryAverageGiB`, `memoryLimitGiB`, `scratchMaximumGiB`, `scratchLimitGiB`, `scratchStorageMode`, `gpusObserved`, `gpusIdle` (GPUs whose peak utilization < 5 %), `gpuUtilizationAverage`, `gpuUtilizationMaximum`, `gpuMemoryMaximumGiB`, `gpuMemoryLimitGiB`.
   - Per run: `storageMaximumGiB`, `storageLimitGiB` (shared filesystem).
   - Window combination: max→max, averages and fractions via exact sum/count, p95→max of window p95s (conservative; only differs for runs > 24 h).
2. `metrics.add_metrics` derives output columns from `cloudwatchMetrics`:
   `metricsSource` (`cloudwatch` / `mixed` / `manifest`), `cwCpusMaximum`, `cwCpusP95`, `cwCpusAverage`, `cwCpuUtilizationRatio` (p95 / reserved), `cpuIdleFraction`, `cwMemoryMaximumGiB`, `cwMemoryP95GiB`, `cwMemoryAverageGiB`, `cwMemoryUtilizationRatio` (max / reserved), `memoryPressure` (max ≥ 90 % of limit), `scratchStorageMode`, `scratchMaximumGiB`, `scratchLimitGiB`, `scratchUtilizationRatio`, `scratchPressure` (≥ 90 %), `gpuUtilizationAverage`, `gpuUtilizationMaximum`, `gpuMemoryMaximumGiB`, `gpusIdle`, `recommendedGpus`, `cwStorageMaximumGiB` (run row).
   Recommendation rules:
   - CPU basis = CloudWatch p95 vCPUs if present, else manifest `cpusMaximum` (CPU starvation slows a task; it does not kill it).
   - Memory basis = the larger of the CloudWatch and manifest peaks (OOM kills a task, so peak not p95, and never below what the manifest observed). Zero CloudWatch values fall back to the manifest.
   - GPU tasks: `recommendedGpus` = GPUs observed busy. If every GPU was idle the task is sized onto a CPU instance (`omicsInstanceTypeMinimum`, `minimumUSD` updated); otherwise instance type is left as reserved (GPU family mapping is out of scope).
   - Headroom applies to both bases as today.
3. `__main__`: `--cloudwatch` option; attach metrics for stats, `--show`, `--plot` and `--batch`; CloudWatch columns are appended to the CSV only when CloudWatch data exists, so existing output is unchanged otherwise. `--write-config` automatically uses the improved recommendations.
4. `batch.py`: add `maximum/mean` CloudWatch utilization columns (only when present) using missing-aware aggregation.

## Phase 2 – I/O classification

- Per task: `fsReadGiB`, `fsWriteGiB`, `fsReadOps`, `fsWriteOps`, `netReceiveGiB`, `netTransmitGiB` (cumulative → `max_over_time`; delta detected from labels → `sum_over_time`).
- Derived: `fsThroughputMiBps`, `fsOpsPerSecond`, `netThroughputMiBps`, `ioBound` (average CPU utilization < 30 % **and** filesystem or network throughput ≥ 100 MiB/s or ≥ 1000 filesystem ops/s; thresholds are module constants).
- Batch: `maximumFsThroughputMiBps`, `meanFsThroughputMiBps`.

## Phase 3 – timeline utilization overlay

- With `--plot --cloudwatch`, two `query_range` calls per metric (≤ 24 h each, step scaled so ≤ 1000 points) give run-wide `sum` of CPU usage vs limit and memory usage vs limit (`last_over_time(...[45s])` to avoid the 5-minute staleness tail).
- `timeline.plot_timeline` gains an optional `utilization` argument rendering two line charts under the Gantt chart on the same time axis. Without data the plot is unchanged.

## Testing

- Unit tests with a fake PromQL transport covering: query construction, window splitting, sharding, combining, access-denied/region/throttle/bad-data paths, empty probe (older runs), partial-task coverage, `--file` reuse, recommendation rules (CPU p95, memory peak, idle GPUs, pressure flags), CSV column gating, batch missing-aware aggregation, I/O temporality, plot overlay.
- Live smoke test against a real run (read-only queries).

## Out of scope

- Mapping partially idle GPU tasks to a different GPU instance family.
- Default-on CloudWatch querying (cost / permissions).
- Recommending scratch size (not a configurable HealthOmics task setting).
