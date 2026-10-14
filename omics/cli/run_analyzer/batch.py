"""Batch aggregation of multiple workflow runs."""

from __future__ import annotations

import statistics
import sys
from typing import IO, Optional, Union

from . import utils
from .metrics import CW_KEY, add_metrics
from .pricing import PricingCache

hdrs = [
    "type",
    "name",
    "count",
    "meanRunningSeconds",
    "maximumRunningSeconds",
    "stdDevRunningSeconds",
    "maximumCpuUtilizationRatio",
    "meanCpuUtilizationRatio",
    "maximumMemoryUtilizationRatio",
    "meanMemoryUtilizationRatio",
    "maximumGpusReserved",
    "meanGpusReserved",
    "recommendedCpus",
    "recommendedMemoryGiB",
    "recommendOmicsInstanceType",
    "maximumEstimatedUSD",
    "meanEstimatedUSD",
]

# Appended when any task carries CloudWatch run metrics. Aggregated over the
# tasks that have a value; tasks without CloudWatch data are not counted as 0.
cloudwatch_hdrs = [
    "maximumCwCpusP95",
    "maximumCwCpuUtilizationRatio",
    "meanCwCpuUtilizationRatio",
    "meanCpuIdleFraction",
    "maximumCwMemoryMaximumGiB",
    "maximumCwMemoryUtilizationRatio",
    "meanCwMemoryUtilizationRatio",
    "anyMemoryPressure",
    "maximumScratchMaximumGiB",
    "maximumScratchUtilizationRatio",
    "anyScratchPressure",
    "maximumGpuUtilizationMaximum",
    "maximumRecommendedGpus",
    "maximumFsThroughputMiBps",
    "meanFsThroughputMiBps",
    "maximumNetThroughputMiBps",
    "anyIoBound",
    "cloudwatchCount",
]


def aggregate_and_print(
    run_resources_list: list[list[dict]],
    pricing_cache: PricingCache,
    engine: str,
    headroom: float = 0.0,
    out: Optional[Union[str, IO[str]]] = None,
) -> None:
    """Aggregate resources and print to output."""
    if engine not in utils.ENGINES:
        raise ValueError(
            f"Invalid engine for use in batch aggregation: {engine}. Must be one of {utils.ENGINES}"
        )

    # Resolve output target
    opened_file: Optional[IO[str]] = None
    try:
        if out is None:
            output = sys.stdout
        elif isinstance(out, str):
            opened_file = open(out, "w")
            output = opened_file
        else:
            output = out

        task_names: set[str] = set()
        for run_resources in run_resources_list:
            for res in list(run_resources):
                # skip resources that are not tasks
                if "task" not in res["arn"]:
                    run_resources.remove(res)
                    continue
                add_metrics(res, run_resources, pricing_cache, headroom)
                task_names.add(utils.task_base_name(res["name"], engine))

        use_cw = any(
            res.get(CW_KEY) for run_resources in run_resources_list for res in run_resources
        )
        columns = hdrs + cloudwatch_hdrs if use_cw else hdrs

        # print headers
        print(",".join(columns), file=output)

        task_names_sorted = sorted(task_names)
        for task_name in task_names_sorted:
            _aggregate_resources(run_resources_list, task_name, engine, output, columns)
    finally:
        if opened_file is not None:
            opened_file.close()


def _aggregate_resources(
    run_resources_list: list[list[dict]],
    task_base_name: str,
    engine: str,
    out: IO[str],
    columns: Optional[list[str]] = None,
) -> None:
    """Aggregate resources with the same base name."""
    run_tasks_with_name: list[dict] = []

    for run_resources in run_resources_list:
        for run_task in run_resources:
            run_task_base_name = utils.task_base_name(run_task["name"], engine)
            if run_task_base_name == task_base_name:
                run_tasks_with_name.append(run_task)

    # for each header key, perform the aggregation
    aggregate: dict = {}
    for k in hdrs:
        if k == "type":
            aggregate[k] = "task"
        elif k == "name":
            aggregate[k] = task_base_name
        elif k == "count":
            aggregate[k] = _do_aggregation(run_tasks_with_name, k, "count")
        elif k.startswith("mean"):
            # resource key is k with "mean" removed and the first char to lowercase
            rk = k.replace("mean", "")[0].lower() + k.replace("mean", "")[1:]
            aggregate[k] = _do_aggregation(run_tasks_with_name, rk, "mean")
        elif k.startswith("stdDev"):
            rk = k.replace("stdDev", "")[0].lower() + k.replace("stdDev", "")[1:]
            aggregate[k] = _do_aggregation(run_tasks_with_name, rk, "stdDev")
        elif k.startswith("maximum"):
            rk = k.replace("maximum", "")[0].lower() + k.replace("maximum", "")[1:]
            aggregate[k] = _do_aggregation(run_tasks_with_name, rk, "maximum")
        elif k in ["recommendedCpus", "recommendedMemoryGiB"]:
            aggregate[k] = _do_aggregation(run_tasks_with_name, k, "maximum")
        elif k in ["recommendOmicsInstanceType"]:
            aggregate[k] = _do_aggregation(
                run_tasks_with_name, "omicsInstanceTypeMinimum", "maximum"
            )
        else:
            raise ValueError(f"Unhandled aggregation for key: {k}")

    columns = columns or hdrs
    for k in columns:
        if k in cloudwatch_hdrs:
            aggregate[k] = _aggregate_optional(run_tasks_with_name, k)

    print(
        ",".join(["" if aggregate.get(h) is None else str(aggregate[h]) for h in columns]), file=out
    )


def _aggregate_optional(resources_list: list[dict], column: str):
    """Aggregate a CloudWatch-derived column, ignoring tasks without a value."""
    if column == "cloudwatchCount":
        return sum(1 for r in resources_list if r.get(CW_KEY))
    for prefix, op in (("maximum", "maximum"), ("mean", "mean"), ("any", "any")):
        if column.startswith(prefix):
            rest = column[len(prefix) :]
            key = rest[0].lower() + rest[1:]
            break
    else:  # pragma: no cover - guarded by cloudwatch_hdrs
        raise ValueError(f"Unhandled aggregation for key: {column}")
    values = [
        r["metrics"][key] for r in resources_list if r.get("metrics", {}).get(key) is not None
    ]
    if not values:
        return None
    if op == "any":
        return any(values)
    if op == "maximum":
        return round(max(values), 4)
    return round(statistics.mean(values), 4)


def _do_aggregation(resources_list: list[dict], resource_key: str, operation: str):
    """Perform a single aggregation operation across resources."""
    if operation == "count":
        return len(resources_list)
    elif operation == "sum":
        return sum([r[resource_key] for r in resources_list])
    elif operation == "maximum":
        if resource_key == "omicsInstanceTypeMinimum":
            instances = []
            for r in resources_list:
                if resource_key in r["metrics"]:
                    instances.append(r["metrics"][resource_key])
            return max(instances, key=lambda x: utils.omics_instance_weight(x))
        else:
            return round(max([r["metrics"].get(resource_key, 0.0) for r in resources_list]), 4)
    elif operation == "mean":
        data = [r["metrics"].get(resource_key, 0.0) for r in resources_list]
        return round(statistics.mean(data=data), 4)
    elif operation == "stdDev":
        data = [r["metrics"].get(resource_key, 0.0) for r in resources_list]
        if len(data) > 1:
            return round(statistics.stdev(data=data), 4)
        else:
            return 0.000
    else:
        raise ValueError(f"Invalid aggregation operation: {operation}")
