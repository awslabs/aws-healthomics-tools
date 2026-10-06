# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
HealthOmics run-metrics dashboard generator.

Builds a CloudWatch dashboard of per-workflow resource utilization from the
HealthOmics vended OpenTelemetry metrics (the ``aws.omics.*`` metrics described
at
https://docs.aws.amazon.com/omics/latest/dev/monitoring-run-metrics.html).

The dashboard has one row per workflow and five plots per row -- CPU
utilization %, memory utilization %, filesystem I/O (read/write), network I/O
(receive/transmit) and run shared-filesystem usage -- each showing the average
and p99 across the workflow's tasks/runs.

The user always supplies the workflow id(s) explicitly; there is no
auto-discovery. Panel titles use the real workflow name (looked up with
``omics:GetWorkflow``), falling back to ``workflow-<n>`` when the name cannot be
resolved.

Run it in the AWS account that owns the run service role and where HealthOmics
vends the metrics. The tool only ever touches the single dashboard it manages:
it creates/updates it (``cloudwatch:PutDashboard``) or, with ``--delete``,
removes it (``cloudwatch:DeleteDashboards``). It never creates, deletes or
modifies workflows or runs.
"""

import argparse
import json
import logging
from typing import Dict, List, Optional, Tuple

import boto3

logger = logging.getLogger(__name__)

# Metric names from the HealthOmics run-metrics documentation.
CPU_USAGE = "aws.omics.task.cpu.usage"
CPU_LIMIT = "aws.omics.task.cpu.limit"
MEM_USAGE = "aws.omics.task.memory.usage"
MEM_LIMIT = "aws.omics.task.memory.limit"
FS_IO = "aws.omics.task.filesystem.io"
NET_IO = "aws.omics.task.network.io"
RUN_FS_USAGE = "aws.omics.run.filesystem.usage"

WORKFLOW_LABEL = "@resource.aws.omics.workflow.id"
TASK_LABEL = "@resource.aws.omics.task.id"

_HEADER_HEIGHT = 2
_ROW_HEIGHT = 6
# Five plots across a 24-wide dashboard: (x, width).
_COLUMN_LAYOUT = [(0, 5), (5, 5), (10, 5), (15, 5), (20, 4)]
_STYLE = {
    "label": {"position": "inside", "show": False},
    "lineOptions": {
        "filled": False,
        "pattern": "solid",
        "spline": False,
        "stacked": False,
        "width": 2,
    },
}


# --------------------------------------------------------------------------- #
# PromQL expression helpers
# --------------------------------------------------------------------------- #


def _utilization_expr(usage: str, limit: str, workflow_id: str) -> str:
    """Average of per-task usage/limit.

    The usage and limit series are each grouped by task id first, so the
    division matches each task's usage with the SAME task's limit, and only
    then are the per-task ratios aggregated. This is correct when tasks have
    different sizes, unlike ``avg(usage) / avg(limit)``.
    """
    per_task_usage = f'avg by ("{TASK_LABEL}") ({{"{usage}", "{WORKFLOW_LABEL}"="{workflow_id}"}})'
    per_task_limit = f'avg by ("{TASK_LABEL}") ({{"{limit}", "{WORKFLOW_LABEL}"="{workflow_id}"}})'
    return f"{per_task_usage} / {per_task_limit}"


def _directional_selector(
    metric: str, workflow_id: str, direction_label: str, direction: str
) -> str:
    return f'{{"{metric}", "{WORKFLOW_LABEL}"="{workflow_id}", "{direction_label}"="{direction}"}}'


def _run_fs_selector(workflow_id: str) -> str:
    return f'{{"{RUN_FS_USAGE}", "{WORKFLOW_LABEL}"="{workflow_id}"}}'


def _promql_query(query_id: str, expr: str, label: str) -> dict:
    return {
        "id": query_id,
        "type": "cloudwatch-metrics",
        "language": "PromQL",
        "query": expr,
        "label": label,
    }


def _chart(
    region: str, x: int, y: int, width: int, title: str, queries: List[dict], y_title: str
) -> dict:
    return {
        "type": "chart",
        "x": x,
        "y": y,
        "width": width,
        "height": _ROW_HEIGHT,
        "properties": {
            "data": {"queries": queries},
            "plotOptions": {
                "legend": {"position": "bottom", "show": True},
                "xAxis": {"type": "datetime"},
                "yAxis": [{"type": "linear", "title": y_title}],
                "style": _STYLE,
            },
            "region": region,
            "title": title,
            "view": "line",
        },
    }


def _avg_p99(prefix: str, expr: str) -> List[dict]:
    return [
        _promql_query(f"{prefix}a", f"avg({expr})", "avg"),
        _promql_query(f"{prefix}p", f"quantile(0.99, {expr})", "p99"),
    ]


def build_dashboard_body(region: str, workflows: List[Tuple[str, str]]) -> dict:
    """Build the full dashboard body in Python (one row per workflow, 5 plots each).

    ``workflows`` is a list of ``(workflow_id, workflow_name)`` tuples. Each plot
    shows the average and p99 across the workflow's tasks/runs. Panel titles use
    the workflow name.
    """
    widgets: List[dict] = [
        {
            "type": "text",
            "x": 0,
            "y": 0,
            "width": 24,
            "height": _HEADER_HEIGHT,
            "properties": {
                "markdown": (
                    f"# HealthOmics Workflow Usage \u2014 {len(workflows)} workflow(s)\n"
                    "**One row per workflow, 5 plots each** (avg + p99 across tasks/runs): "
                    "CPU utilization %, memory utilization %, filesystem I/O (read/write), "
                    "network I/O (receive/transmit), run shared-filesystem usage.\n"
                    "CPU/memory are per-task usage/limit, aggregated with avg / p99. Run "
                    "filesystem usage is DYNAMIC-lagged (>30 min) and only non-zero for "
                    "workflows that use the shared filesystem."
                )
            },
        }
    ]

    y = _HEADER_HEIGHT
    for workflow_id, name in workflows:
        cpu = _utilization_expr(CPU_USAGE, CPU_LIMIT, workflow_id)
        mem = _utilization_expr(MEM_USAGE, MEM_LIMIT, workflow_id)
        fs_read = _directional_selector(FS_IO, workflow_id, "filesystem.io.direction", "read")
        fs_write = _directional_selector(FS_IO, workflow_id, "filesystem.io.direction", "write")
        net_recv = _directional_selector(NET_IO, workflow_id, "network.io.direction", "receive")
        net_xmit = _directional_selector(NET_IO, workflow_id, "network.io.direction", "transmit")
        run_fs = _run_fs_selector(workflow_id)

        widgets.append(
            _chart(
                region,
                _COLUMN_LAYOUT[0][0],
                y,
                _COLUMN_LAYOUT[0][1],
                f"{name} \u2014 CPU utilization %",
                _avg_p99("cpu", cpu),
                "utilization (0-1)",
            )
        )
        widgets.append(
            _chart(
                region,
                _COLUMN_LAYOUT[1][0],
                y,
                _COLUMN_LAYOUT[1][1],
                f"{name} \u2014 Memory utilization %",
                _avg_p99("mem", mem),
                "utilization (0-1)",
            )
        )
        widgets.append(
            _chart(
                region,
                _COLUMN_LAYOUT[2][0],
                y,
                _COLUMN_LAYOUT[2][1],
                f"{name} \u2014 Filesystem I/O (By)",
                [
                    _promql_query("fra", f"avg({fs_read})", "read avg"),
                    _promql_query("frp", f"quantile(0.99, {fs_read})", "read p99"),
                    _promql_query("fwa", f"avg({fs_write})", "write avg"),
                    _promql_query("fwp", f"quantile(0.99, {fs_write})", "write p99"),
                ],
                "bytes",
            )
        )
        widgets.append(
            _chart(
                region,
                _COLUMN_LAYOUT[3][0],
                y,
                _COLUMN_LAYOUT[3][1],
                f"{name} \u2014 Network I/O (By)",
                [
                    _promql_query("nra", f"avg({net_recv})", "recv avg"),
                    _promql_query("nrp", f"quantile(0.99, {net_recv})", "recv p99"),
                    _promql_query("nta", f"avg({net_xmit})", "xmit avg"),
                    _promql_query("ntp", f"quantile(0.99, {net_xmit})", "xmit p99"),
                ],
                "bytes",
            )
        )
        widgets.append(
            _chart(
                region,
                _COLUMN_LAYOUT[4][0],
                y,
                _COLUMN_LAYOUT[4][1],
                f"{name} \u2014 Run FS usage (By)",
                _avg_p99("rfs", run_fs),
                "bytes",
            )
        )
        y += _ROW_HEIGHT

    return {"start": "-P1W", "periodOverride": "auto", "widgets": widgets}


def resolve_workflow_names(
    workflow_ids: List[str], region: str, session: Optional[boto3.Session] = None
) -> Dict[str, str]:
    """Map each workflow id to its real name, falling back to ``workflow-<n>``."""
    omics = (session or boto3.Session()).client("omics", region_name=region)
    names: Dict[str, str] = {}
    for index, wf in enumerate(workflow_ids, start=1):
        fallback = f"workflow-{index}"
        try:
            names[wf] = omics.get_workflow(id=wf).get("name") or fallback
        except Exception as err:  # noqa: BLE001 - name is cosmetic; never fail the build
            logger.warning("Could not resolve name for workflow %s: %s", wf, err)
            names[wf] = fallback
    return names


def put_dashboard(
    name: str, body: dict, region: str, session: Optional[boto3.Session] = None
) -> List[str]:
    """Create or update the dashboard. Returns any validation messages."""
    cw = (session or boto3.Session()).client("cloudwatch", region_name=region)
    resp = cw.put_dashboard(DashboardName=name, DashboardBody=json.dumps(body))
    return [m.get("Message", "") for m in resp.get("DashboardValidationMessages", [])]


def _is_benign_validation(message: str) -> bool:
    """Return True for CloudWatch validation messages that are safe to hide.

    CloudWatch warns that a chart widget's ``x`` property "is not expected to be
    part of a widget definition, will be ignored", but it still stores and honors
    the x/y/width/height coordinates (verified: the dashboard renders as the
    intended 5-across grid). The warning is advisory noise for this widget type,
    so it is suppressed to avoid alarming users; any other validation message is
    still surfaced.
    """
    return "is not expected to be part of a widget definition" in message


def delete_dashboard(name: str, region: str, session: Optional[boto3.Session] = None) -> None:
    """Delete the named dashboard. No error if it does not exist."""
    cw = (session or boto3.Session()).client("cloudwatch", region_name=region)
    cw.delete_dashboards(DashboardNames=[name])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_dashboard",
        description="Generate (or delete) a CloudWatch dashboard of HealthOmics "
        "per-workflow run metrics.",
    )
    parser.add_argument(
        "-i",
        "--workflow-ids",
        default=None,
        help="Comma-separated HealthOmics workflow ids to chart. "
        "Required unless --delete is given.",
    )
    parser.add_argument(
        "-r", "--region", default="us-west-2", help="AWS Region (default: us-west-2)."
    )
    parser.add_argument(
        "-d",
        "--dashboard-name",
        default="omics-workflow-usage",
        help="CloudWatch dashboard name (default: omics-workflow-usage).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the dashboard body instead of creating the dashboard.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the dashboard named by --dashboard-name instead of creating it.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point: create, preview, or delete the dashboard."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)

    session = boto3.Session()

    if args.delete:
        delete_dashboard(args.dashboard_name, args.region, session)
        logger.info("Deleted dashboard %s in %s.", args.dashboard_name, args.region)
        return 0

    if not args.workflow_ids:
        logger.error("No workflow ids provided. Pass -i/--workflow-ids id1,id2,... (or --delete).")
        return 2
    workflow_ids = [w.strip() for w in args.workflow_ids.split(",") if w.strip()]
    if not workflow_ids:
        logger.error("No workflow ids provided. Pass -i/--workflow-ids id1,id2,... (or --delete).")
        return 2
    logger.info("Workflows: %s", ", ".join(workflow_ids))

    names = resolve_workflow_names(workflow_ids, args.region, session)
    for wf in workflow_ids:
        logger.info("  %s -> %s", wf, names[wf])

    body = build_dashboard_body(args.region, [(wf, names[wf]) for wf in workflow_ids])

    if args.dry_run:
        print(json.dumps(body, indent=2))
        return 0

    messages = put_dashboard(args.dashboard_name, body, args.region, session)
    for msg in sorted(set(m for m in messages if m and not _is_benign_validation(m))):
        logger.info("validation: %s", msg)
    logger.info(
        "Done. Console: https://%s.console.aws.amazon.com/cloudwatch/home"
        "?region=%s#dashboards/dashboard/%s",
        args.region,
        args.region,
        args.dashboard_name,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
