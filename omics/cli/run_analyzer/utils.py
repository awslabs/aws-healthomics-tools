import re

ENGINES = {"WDL", "CWL", "NEXTFLOW"}

_wdl_task_regex = r"^([^-]+)(-\d+-\d+.*)?$"
_nextflow_task_regex = r"^(.+)(\s\(.+\))$"
_cwl_task_regex = r"^(^\D+)(_\d+)?$"


def get_engine(workflow_arn: str, client) -> str:
    """Get the engine name for the workflow_arn using the omics client"""
    id = workflow_arn.split("/")[-1]
    return get_engine_from_id(id, client)


def get_engine_from_id(workflow_id: str, client) -> str:
    """Get the engine name for the workflow_id using the omics client"""
    return client.get_workflow(id=workflow_id)["engine"]


def task_base_name(name: str, engine: str) -> str:
    """Find the base name of the task assuming the naming conventions used by the engine"""
    # WDL
    if engine == "WDL":
        m = re.match(_wdl_task_regex, name)
        if m:
            return m.group(1)
    # Nextflow
    elif engine == "NEXTFLOW":
        m = re.match(_nextflow_task_regex, name)
        if m:
            return m.group(1)
    # CWL
    elif engine == "CWL":
        m = re.match(_cwl_task_regex, name)
        if m:
            return m.group(1)
    else:
        raise ValueError(f"Unsupported engine: {engine}")
    return name


_sizes = {
    "large": 2,
    "xlarge": 4,
    "2xlarge": 8,
    "4xlarge": 16,
    "8xlarge": 32,
    "12xlarge": 48,
    "16xlarge": 64,
    "24xlarge": 96,
    "32xlarge": 128,
    "48xlarge": 192,
}
_families = {"c": 2, "m": 4, "r": 8, "g4dn": 16, "g5": 16, "g6": 16, "g6e": 32}


def omics_instance_weight(instance: str) -> int:
    """Compute a numeric weight for an instance to be used in sorting or finding a max or min"""
    # remove the "omics." from the string
    instance = instance.replace("omics.", "")
    # split the instance into family and size
    parts = instance.split(".")
    fam = parts[0]
    size = parts[1]

    ccount = _sizes[size]
    weight = ccount * _families[fam]
    return weight
