"""Metrics computation for HealthOmics workflow runs and tasks."""

from __future__ import annotations

import datetime
import math
import re
import sys
from typing import Optional

import dateutil.parser  # type: ignore[import-untyped]

from . import utils
from .pricing import PricingCache

SECS_PER_HOUR = 3600.0
STORAGE_TYPE_DYNAMIC_RUN_STORAGE = "DYNAMIC"
STORAGE_TYPE_STATIC_RUN_STORAGE = "STATIC"
PRICE_RESOURCE_TYPE_DYNAMIC_RUN_STORAGE = "Dynamic Run Storage"
PRICE_RESOURCE_TYPE_STATIC_RUN_STORAGE = "Run Storage"


def parse_time_str(s: Optional[str], utc: bool = True):
    """Parse an ISO time string, returning None if input is falsy."""
    tz = datetime.timezone.utc
    return dateutil.parser.parse(s).replace(tzinfo=tz) if s else None


def get_static_storage_gib(capacity: Optional[float] = None) -> int:
    """Return filesystem size in GiB (rounded up to the storage increment)."""
    omics_storage_min = 1200  # Minimum size
    omics_storage_inc = 2400  # Size increment (2400, 4800, 7200, ...)
    if not capacity or capacity <= omics_storage_min:
        return omics_storage_min
    capacity = (capacity + omics_storage_inc - 1) / omics_storage_inc
    return int(capacity) * omics_storage_inc


def add_run_util(run: dict, tasks: list[dict]) -> None:
    """Add run-level metrics computed from task metrics."""
    events: list[dict] = []
    stop1 = None
    stops: list[int] = []
    for idx, task in enumerate(tasks):
        start = parse_time_str(task.get("startTime"))
        if start:
            events.append({"time": start, "event": "start", "index": idx})
        stop = parse_time_str(task.get("stopTime"))
        if stop:
            events.append({"time": stop, "event": "stop", "index": idx})
            if not stop1 or stop > stop1:
                stop1 = stop
        else:
            stops.append(idx)

    # If no tasks have timing data, nothing to compute
    if not events:
        return

    if stop1:
        for idx in stops:
            events.append({"time": stop1, "event": "stop", "index": idx})
    events.sort(key=lambda x: x["time"])

    metric_names = [
        "cpusReserved",
        "cpusMaximum",
        "cpusAverage",
        "gpusReserved",
        "memoryReservedGiB",
        "memoryMaximumGiB",
        "memoryAverageGiB",
    ]
    metrics = run.get("metrics", {})
    run["metrics"] = metrics

    active: list[int] = []
    t0 = None
    time = 0.0
    for evt in events:
        t1 = evt["time"]
        if t0:
            secs = (t1 - t0).total_seconds()
            time += secs
            for name in metric_names:
                mvalues = [tasks[i].get("metrics", {}).get(name) for i in active]
                mvalues = [v for v in mvalues if v is not None]
                if not mvalues:
                    continue
                total = sum(mvalues)
                if "Average" in name:
                    metrics[name] = metrics.get(name, 0) + total * secs
                else:
                    metrics[name] = max(metrics.get(name, total), total)
        t0 = t1
        if evt["event"] == "start":
            active.append(evt["index"])
        elif evt["index"] in active:
            active.remove(evt["index"])

    for name in metric_names:
        if name in metrics and "Average" in name and time > 0:
            metrics[name] /= time


def add_metrics(
    res: dict,
    resources: list[dict],
    pricing_cache: PricingCache,
    headroom: float = 0.0,
    exename: str = "omics-run-analyzer",
) -> None:
    """Add run/task metrics to *res* in-place."""
    arn = re.split(r"[:/]", res["arn"])
    rtype = arn[-2]
    region = arn[3]
    res["type"] = rtype
    headroom_multiplier = 1.0 + float(headroom)

    metrics = res.get("metrics", {})
    # if a resource has no metrics body then we can skip the rest
    if res.get("metrics") is None:
        return

    if rtype == "run":
        add_run_util(res, resources[1:])

    time1 = parse_time_str(res.get("startTime"))
    time2 = parse_time_str(res.get("stopTime"))
    running = 0.0
    if time1 and time2:
        running = (time2 - time1).total_seconds()
        metrics["runningSeconds"] = running

    cpus_res = metrics.get("cpusReserved")
    cpus_max = metrics.get("cpusMaximum")
    if cpus_res and cpus_max:
        metrics["cpuUtilizationRatio"] = float(cpus_max) / float(cpus_res)
    gpus_res = metrics.get("gpusReserved")
    mem_res = metrics.get("memoryReservedGiB")
    mem_max = metrics.get("memoryMaximumGiB")
    if mem_res and mem_max:
        metrics["memoryUtilizationRatio"] = float(mem_max) / float(mem_res)
    store_res = metrics.get("storageReservedGiB", 0.0)
    store_max = metrics.get("storageMaximumGiB", 0.0)
    store_avg = metrics.get("storageAverageGiB", 0.0)
    if store_res and store_max:
        metrics["storageUtilizationRatio"] = float(store_max) / float(store_res)

    storage_type = res.get("storageType", STORAGE_TYPE_STATIC_RUN_STORAGE)

    if rtype == "run":
        # Get capacity requested (static), capacity max. used (dynamic) and
        # charged storage (the requested capacity for static or average used for dynamic)
        if storage_type == STORAGE_TYPE_STATIC_RUN_STORAGE:
            price_resource_type = PRICE_RESOURCE_TYPE_STATIC_RUN_STORAGE
            capacity = get_static_storage_gib(res.get("storageCapacity"))
            charged = capacity
        elif storage_type == STORAGE_TYPE_DYNAMIC_RUN_STORAGE:
            price_resource_type = PRICE_RESOURCE_TYPE_DYNAMIC_RUN_STORAGE
            capacity = store_max
            charged = store_avg

        # Get price for actually used storage (approx. for dynamic storage)
        gib_hrs = charged * running / SECS_PER_HOUR
        price = pricing_cache.get_price(price_resource_type, region, gib_hrs)
        if price:
            metrics["estimatedUSD"] = price

        # Get price for optimal static storage
        if store_max:
            capacity = get_static_storage_gib(store_max * headroom_multiplier)
        gib_hrs = capacity * running / SECS_PER_HOUR
        price = pricing_cache.get_price(PRICE_RESOURCE_TYPE_STATIC_RUN_STORAGE, region, gib_hrs)
        if price:
            metrics["minimumUSD"] = price

    elif "instanceType" in res:
        running_for_instance_cost = max(60, running)
        itype = res["instanceType"]
        metrics["omicsInstanceTypeReserved"] = itype
        price = pricing_cache.get_price(itype, region, running_for_instance_cost / SECS_PER_HOUR)
        if price:
            metrics["estimatedUSD"] = price
        if cpus_max and mem_max and not gpus_res:
            # Get smallest instance type that meets the requirements
            cpus_max_adj = math.ceil(cpus_max * headroom_multiplier)
            mem_max_adj = math.ceil(mem_max * headroom_multiplier)
            instance_result = utils.get_instance_for_requirements(cpus_max_adj, mem_max_adj)
            if instance_result:
                itype, cpus, mem = instance_result
                metrics["omicsInstanceTypeMinimum"] = itype
                metrics["recommendedCpus"] = cpus
                metrics["recommendedMemoryGiB"] = mem
            else:
                # No suitable instance found - requirements exceed largest available instance
                task_name = res.get("name", "unknown")
                task_arn = res.get("arn", "unknown")
                sys.stderr.write(
                    f"{exename}: WARNING - No suitable instance found for task '{task_name}' "
                    f"(ARN: {task_arn}) with requirements: {cpus_max_adj} CPUs, "
                    f"{mem_max_adj} GiB memory. "
                    f"Requirements exceed largest available instance "
                    f"(omics.r.48xlarge: 192 CPUs, 1536 GiB).\n"
                )
                metrics["omicsInstanceTypeMinimum"] = "REQUIREMENTS_EXCEED_LARGEST_INSTANCE"
                metrics["recommendedCpus"] = cpus_res
                metrics["recommendedMemoryGiB"] = mem_res
        else:
            metrics["omicsInstanceTypeMinimum"] = itype
            metrics["recommendedCpus"] = cpus_res
            metrics["recommendedMemoryGiB"] = mem_res
        price = pricing_cache.get_price(itype, region, running_for_instance_cost / SECS_PER_HOUR)
        if price:
            metrics["minimumUSD"] = price
