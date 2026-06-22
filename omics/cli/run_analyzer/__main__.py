"""
Generate statistics for a completed HealthOmics workflow run

Usage: omics-run-analyzer [<runId>...]
                          [--profile=<profile>]
                          [--region=<region>]
                          [--show]
                          [--file=<path>]
                          [--out=<path>]
                          [--plot=<directory>]
                          [--headroom=<float>]
                          [--write-config=<path>]
                          [--workflow-owner-id=<value>]
                          [--verbose]
       omics-run-analyzer --timeline <runId> [--profile=<profile>] [--region=<region>] [--vebose]
       omics-run-analyzer --time <interval>  [--profile=<profile>] [--region=<region>] [--vebose]
       omics-run-analyzer --batch <runId>... [--profile=<profile>] [--region=<region>] [--headroom=<float>]
                                             [--out=<path>] [--workflow-owner-id=<value>] [--verbose]
       omics-run-analyzer (-h --help)
       omics-run-analyzer --version

Arguments:
 <interval>               Select runs over a time interval [default: 1day]
 <runId>...               One or more workflow run IDs
 <path>                   Path to a file or directory


Options:
 -b, --batch                    Analyze one or more runs and generate aggregate stastics on repeated or scattered tasks
 -c, --write-config=<path>      Output a config file with recommended resources (Nextflow only)
 -f, --file=<path>              Load input from file
 -H, --headroom=<float>         Adds a fractional buffer to the size of recommended memory and CPU. Values must be between 0.0 and 1.0.
 -o, --out=<path>               Write output to file
 -p, --profile=<profile>        AWS profile
 -P, --plot=<directory>         Plot a run timeline to a directory
 -r, --region=<region>          AWS region
 -t, --time=<interval>          Select runs over a time interval [default: 1day]
 -s, --show                     Show run resources with no post-processing (JSON)
 -T, --timeline                 Show workflow run timeline
 -V, --verbose                  Verbose output
 -w, --workflow-owner-id=<value> Workflow owner account ID, required for shared workflows

 -h, --help                     Show help text
 --version                      Show the version of this application

Examples:
 # Show workflow runs that were running in the last 5 days
 # (supported time units include minutes, hours, days, weeks, or years)
 omics-run-analyzer --time=5days
 # Retrieve and analyze a specific workflow run by ID writing output to ./run-1234567.csv
 omics-run-analyzer 1234567 -o run-1234567.csv
 # Show the completion time and UUID (only) of multiple runs
 omics-run-analyzer 1234567 2345678
 # Retrieve and analyze a specific workflow run by ID and UUID
 omics-run-analyzer 2345678:12345678-1234-5678-9012-123456789012
 # Output workflow run and tasks in JSON format
 omics-run-analyzer 1234567 -s -o run-1234567.json
 # Plot a timeline of a workflow run and write the plot the HTML to "out/"
 omics-run-analyzer 1234567 -P out
 # Output a workflow run analysis with 10% headroom added to recommended CPU and memory
 omics-run-analyzer 1234567 -P timeline -H 0.1
 # Analyze multiple runs and output aggregate statistics to a file
 omics-run-analyzer -b 1234567 2345678 3456789 -o out.csv
"""

from __future__ import annotations

import csv
import datetime
import importlib.metadata
import json
import logging
import os
import re
import sys
from typing import IO, NoReturn, Optional

import boto3
import docopt
from bokeh.plotting import output_file

from . import batch  # type: ignore
from . import timeline  # type: ignore
from . import utils, writeconfig
from .exceptions import RunAnalyzerError
from .metrics import add_metrics, parse_time_str
from .pricing import PRICING_AWS_REGION, PricingCache

EXENAME = os.path.basename(sys.argv[0])
logging.basicConfig(
    format="%(asctime)s run_analyzer:%(levelname)s - %(message)s", level=logging.WARNING
)
logger = logging.getLogger(EXENAME)

OMICS_LOG_GROUP = "/aws/omics/WorkflowLog"
OMICS_SERVICE_CODE = "AmazonOmics"


def die(msg: object) -> NoReturn:
    """Show error message and terminate."""
    raise RunAnalyzerError(str(msg))


def parse_time_delta(s: str) -> datetime.timedelta:
    """Parse time delta string."""
    m = re.match(r"(\d+)\s*(m|min|minutes?|h|hours?|d|days?|w|weeks?|y|years?)$", s)
    if not m:
        die("unrecognized time interval format '{}'".format(s))
    secs = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 220752000}
    delta = int(m.group(1)) * secs[m.group(2)[0]]
    return datetime.timedelta(seconds=delta)


# ---------------------------------------------------------------------------
# AWS data retrieval helpers
# ---------------------------------------------------------------------------


def stream_to_run(strm: dict) -> Optional[dict]:
    """Convert CloudWatch Log stream to workflow run details."""
    m = re.match(r"^manifest/run/(\d+)/([a-f0-9-]+)$", strm["logStreamName"])
    if not m:
        return None
    strm["id"] = m.group(1)
    strm["uuid"] = m.group(2)
    return strm


def get_streams(logs, rqst: dict, start_time: Optional[float] = None) -> list[dict]:
    """Get matching CloudWatch Log streams."""
    streams: list[dict] = []
    for page in logs.get_paginator("describe_log_streams").paginate(**rqst):
        done = False
        for strm in page["logStreams"]:
            if start_time and strm["lastEventTimestamp"] < start_time:
                done = True
            elif stream_to_run(strm):
                streams.append(strm)
                if (len(streams) % 100) == 0:
                    sys.stderr.write(f"{EXENAME}: found {len(streams)} workflow runs\n")
                if not start_time:
                    done = True
        if done:
            break
    return streams


def get_runs(logs, runs: list[str], opts: dict) -> list[dict]:
    """Get matching workflow runs."""
    streams: list[dict] = []
    if runs:
        for run in runs:
            run_parts = re.split(r"[:/]", run)
            if re.match(r"[a-f\d]{8}(-[a-f\d]{4}){3}-[a-f\d]{12}$", run_parts[-1]):
                prefix = f"manifest/run/{run_parts[-2]}/{run_parts[-1]}"
            else:
                prefix = f"manifest/run/{run_parts[-1]}/"
            rqst = {
                "logGroupName": OMICS_LOG_GROUP,
                "logStreamNamePrefix": prefix,
            }
            returned_streams = get_streams(logs, rqst)
            if returned_streams:
                streams.extend(returned_streams)
            else:
                die(f"run {run_parts[-1]} not found")
    else:
        start_time = datetime.datetime.now() - parse_time_delta(opts["--time"])
        start_time_ms = start_time.timestamp() * 1000.0
        rqst_desc: dict[str, object] = {
            "logGroupName": OMICS_LOG_GROUP,
            "orderBy": "LastEventTime",
            "descending": True,
        }
        streams.extend(get_streams(logs, rqst_desc, start_time_ms))
    result = [stream_to_run(s) for s in streams]
    return sorted(result, key=lambda x: x["creationTime"])


def get_run_resources(logs, run: dict) -> list[dict]:
    """Get workflow run/task details."""
    rqst = {
        "logGroupName": OMICS_LOG_GROUP,
        "logStreamName": run["logStreamName"],
        "startFromHead": True,
        "endTime": run["lastEventTimestamp"] + 1,
    }
    resources: list[dict] = []
    done = False
    while not done:
        resp = logs.get_log_events(**rqst)
        for evt in resp.get("events", []):
            resources.append(json.loads(evt["message"]))
        token = resp.get("nextForwardToken")
        if not token or token == rqst.get("nextToken"):
            done = True
        rqst["nextToken"] = token
    return sorted(resources, key=lambda x: x.get("creationTime", "1970-01-01"))


# ---------------------------------------------------------------------------
# Call-cache detection
# ---------------------------------------------------------------------------


def is_task_cached(res: dict) -> bool:
    """Check if a task resource was served from call cache (missing timing data)."""
    return not (res.get("creationTime") and res.get("startTime") and res.get("stopTime"))


# ---------------------------------------------------------------------------
# Output handlers
# ---------------------------------------------------------------------------


def _handle_show(resources: list[dict], out: IO[str]) -> None:
    """Write resources as JSON."""
    out.write(json.dumps(resources, indent=2) + "\n")


def _handle_timeline(resources: list[dict], out: IO[str]) -> None:
    """Write a CSV timeline, skipping cached tasks gracefully."""
    hdrs = ["resource", "pending", "starting", "running"]
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(hdrs)

    # Separate the run resource from task resources (mirrors _handle_plot logic)
    tasks = [res for res in resources if re.split(r"[:/]", res["arn"])[-2] != "run"]

    cached_tasks: list[str] = []
    executed_tasks = 0
    for res in tasks:
        event = _get_timeline_event(res, resources)
        if event is None:
            cached_tasks.append(res.get("name", res.get("arn", "unknown")))
            continue
        row = [event.get(h, "") for h in hdrs]
        writer.writerow(row)
        executed_tasks += 1
    if cached_tasks:
        total = executed_tasks + len(cached_tasks)
        sys.stderr.write(
            f"{EXENAME}: {len(cached_tasks)}/{total} tasks were served from "
            f"call cache and are not shown in the timeline: "
            f"{', '.join(cached_tasks)}\n"
        )
    if executed_tasks == 0:
        die(
            "all tasks in this run were served from call cache. "
            "No timing data is available to build a timeline."
        )


def _handle_stats(
    resources: list[dict],
    session,
    pricing_cache: PricingCache,
    opts: dict,
    out: IO[str],
) -> None:
    """Write CSV run statistics and optionally write a recommended config."""
    headroom = 0.0
    if opts["--headroom"]:
        try:
            headroom = float(opts["--headroom"])
        except Exception:
            die(f'the --headroom argument {opts["--headroom"]} is not a valid float value')
        if headroom > 1.0 or headroom < 0.0:
            die(f"the --headroom argument {headroom} must be between 0.0 and 1.0")

    def tocsv(val):
        if val is None:
            return ""
        return f"{val:f}" if type(val) is float else str(val)

    hdrs = [
        "uuid",
        "arn",
        "type",
        "name",
        "startTime",
        "stopTime",
        "runningSeconds",
        "cpus",
        "gpus",
        "memory",
        "omicsInstanceTypeReserved",
        "omicsInstanceTypeMinimum",
        "recommendedCpus",
        "recommendedMemoryGiB",
        "estimatedUSD",
        "minimumUSD",
        "cpuUtilizationRatio",
        "memoryUtilizationRatio",
        "storageUtilizationRatio",
        "cpusReserved",
        "cpusMaximum",
        "cpusAverage",
        "gpusReserved",
        "memoryReservedGiB",
        "memoryMaximumGiB",
        "memoryAverageGiB",
        "storageReservedGiB",
        "storageMaximumGiB",
        "storageAverageGiB",
    ]

    hrdrs_map = {
        "cpus": "cpusRequested",
        "gpus": "gpusRequested",
        "memory": "memoryRequestedGiB",
    }
    formatted_headers = [hrdrs_map.get(h, h) for h in hdrs]

    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(formatted_headers)
    config: dict = {}
    omics = session.client("omics")
    engine = ""
    for res in resources:
        add_metrics(res, resources, pricing_cache, headroom, exename=EXENAME)
        metrics = res.get("metrics", {})
        if opts["--write-config"]:
            if res["type"] == "run":
                wfid = res["workflow"].split("/")[-1]
                engine = utils.get_engine_from_id(wfid, omics, opts["--workflow-owner-id"])
            if res["type"] == "task":
                task_name = utils.task_base_name(res["name"], engine)
                if task_name not in config:
                    config[task_name] = {
                        "cpus": metrics["recommendedCpus"],
                        "mem": metrics["recommendedMemoryGiB"],
                    }
                else:
                    config[task_name] = {
                        "cpus": max(metrics["recommendedCpus"], config[task_name]["cpus"]),
                        "mem": max(metrics["recommendedMemoryGiB"], config[task_name]["mem"]),
                    }
        row = [tocsv(metrics.get(h, res.get(h))) for h in hdrs]
        writer.writerow(row)

    if opts["--write-config"]:
        filename = opts["--write-config"]
        writeconfig.create_config(engine, config, filename)


def _handle_plot(resources: list[dict], opts: dict) -> None:
    """Generate a Bokeh HTML timeline plot."""
    if len(resources) < 1:
        die("no resources to plot")

    run: dict = {}
    plot_resources = list(resources)
    for res in plot_resources:
        rtype = re.split(r"[:/]", res["arn"])[-2]
        if rtype == "run":
            run = res
            plot_resources.remove(res)
            break

    if not run:
        die("no run resource found in workflow run data")

    # Identify cached tasks before plotting
    cached_tasks = [res for res in plot_resources if is_task_cached(res)]
    executed_tasks = [res for res in plot_resources if not is_task_cached(res)]

    if cached_tasks:
        total = len(cached_tasks) + len(executed_tasks)
        cached_names = [t.get("name", t.get("arn", "unknown")) for t in cached_tasks]
        sys.stderr.write(
            f"{EXENAME}: {len(cached_tasks)}/{total} tasks were served from "
            f"call cache and are not shown in the plot: "
            f"{', '.join(cached_names)}\n"
        )

    if not executed_tasks:
        die(
            "all tasks in this run were served from call cache. "
            "No timing data is available to build a timeline plot."
        )

    start = datetime.datetime.strptime(run["startTime"], "%Y-%m-%dT%H:%M:%S.%fZ")
    stop = datetime.datetime.strptime(run["stopTime"], "%Y-%m-%dT%H:%M:%S.%fZ")
    run_duration_hrs = (stop - start).total_seconds() / 3600

    runid = run["arn"].split("/")[-1]
    output_file_basename = f"{runid}_timeline"

    plot_dir = opts["--plot"]
    if not os.path.isdir(plot_dir):
        os.makedirs(plot_dir)
    output_file(
        filename=os.path.join(plot_dir, f"{output_file_basename}.html"),
        title=runid,
        mode="cdn",
    )
    title = f"arn: {run['arn']}, name: {run.get('name')}"
    if cached_tasks:
        title += f" ({len(cached_tasks)} cached tasks not shown)"

    timeline.plot_timeline(executed_tasks, title=title, max_duration_hrs=run_duration_hrs)


def _handle_list_runs(runs: list[dict], out: IO[str]) -> None:
    """Show available runs when no specific resources loaded."""
    out.write("Workflow run IDs (<completionTime> <UUID>):\n")
    for r in runs:
        time0 = r["creationTime"] / 1000.0
        time0 = datetime.datetime.fromtimestamp(time0)
        time0 = time0.isoformat(timespec="seconds")
        out.write(f"{r['id']} ({time0} {r['uuid']})\n")


# ---------------------------------------------------------------------------
# Timeline event helper
# ---------------------------------------------------------------------------


def _get_timeline_event(res: dict, resources: list[dict]) -> Optional[dict]:
    """Convert resource to timeline event. Returns None for cached tasks."""
    if is_task_cached(res):
        return None
    arn = re.split(r"[:/]", res["arn"])
    time0 = parse_time_str(resources[0].get("creationTime"))
    time1 = parse_time_str(res.get("creationTime"))
    time2 = parse_time_str(res.get("startTime"))
    time3 = parse_time_str(res.get("stopTime"))
    attrs = ["name", "cpus", "gpus", "memory"]
    attrs = [f"{a}={res[a]}" for a in attrs if res.get(a)]
    resource = f"{arn[-2]}/{arn[-1]}"
    if attrs:
        resource += f" ({','.join(attrs)})"
    return {
        "resource": resource,
        "pending": (time1 - time0).total_seconds(),
        "starting": (time2 - time1).total_seconds(),
        "running": (time3 - time2).total_seconds(),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    """Run the analyzer CLI with the given arguments."""
    opts = docopt.docopt(
        __doc__, version=f"v{importlib.metadata.version('aws-healthomics-tools')}", argv=argv
    )
    if opts["--verbose"]:
        logger.setLevel(logging.DEBUG)

    logger.debug("command line options: %s", opts)

    try:
        _run(opts)
    except RunAnalyzerError as e:
        exit(f"{EXENAME}: {e}")


def _run(opts: dict) -> None:
    """Core logic, separated from main() so errors propagate as exceptions."""
    try:
        session = boto3.Session(profile_name=opts["--profile"], region_name=opts["--region"])
        pricing_client = session.client("pricing", region_name=PRICING_AWS_REGION)
        pricing_client.describe_services(ServiceCode=OMICS_SERVICE_CODE)
    except Exception as e:
        die(e)

    pricing_cache = PricingCache(pricing_client)

    # Retrieve workflow runs & tasks
    runs: list[dict] = []
    resources: list[dict] = []
    if opts["--file"]:
        with open(opts["--file"]) as f:
            resources = json.load(f)
    else:
        try:
            logs = session.client("logs")
            runs = get_runs(logs, opts["<runId>"], opts)
        except Exception as e:
            die(e)
        if not runs:
            die("no matching workflow runs")

        elif len(runs) == 1 and opts["<runId>"]:
            resources = get_run_resources(logs, runs[0])
            if not resources:
                die("no workflow run resources")
        if len(runs) >= 1 and opts["--batch"]:
            list_of_resources: list[list[dict]] = []
            engine = ""
            for run in runs:
                resources = get_run_resources(logs, run)
                run_engine = utils.get_engine(
                    workflow_arn=resources[0]["workflow"],
                    client=session.client("omics"),
                    workflow_owner_id=opts["--workflow-owner-id"],
                )
                if not engine:
                    engine = run_engine
                elif engine != run_engine:
                    die("aggregated runs must be from the same engine")
                if resources:
                    list_of_resources.append(resources)
            batch.aggregate_and_print(
                run_resources_list=list_of_resources,
                pricing_cache=pricing_cache,
                engine=engine,
                headroom=opts["--headroom"] or 0.0,
                out=opts["--out"],
            )
            return

    # Display output
    with open(opts["--out"] or sys.stdout.fileno(), "w") as out:
        if not resources:
            _handle_list_runs(runs, out)
        elif opts["--show"]:
            _handle_show(resources, out)
        elif opts["--timeline"]:
            _handle_timeline(resources, out)
        else:
            _handle_stats(resources, session, pricing_cache, opts, out)
        if opts["--out"]:
            sys.stderr.write(f"{EXENAME}: wrote {opts['--out']}\n")

    if opts["--plot"]:
        _handle_plot(resources, opts)


if __name__ == "__main__":
    main()
