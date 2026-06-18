"""Batch aggregation of multiple workflow runs."""

from __future__ import annotations

import statistics
import sys
from typing import IO, Optional, Union

from . import utils
from .metrics import add_metrics
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
    if out is None:
        output = sys.stdout
    elif isinstance(out, str):
        output = open(out, "w")
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

    # print headers
    print(",".join(hdrs), file=output)

    task_names_sorted = sorted(task_names)
    for task_name in task_names_sorted:
        _aggregate_resources(run_resources_list, task_name, engine, output)

    if isinstance(out, str):
        output.close()


def _aggregate_resources(
    run_resources_list: list[list[dict]], task_base_name: str, engine: str, out: IO[str]
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

    print(",".join([str(aggregate.get(h, "")) for h in hdrs]), file=out)


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
