import textwrap


def create_config(engine, task_resources, filename):
    """Create a config file based on recommended CPU and Memory values"""
    if engine == "NEXTFLOW":
        task_strings = []
        for task in task_resources:
            task_string = textwrap.dedent(f"""
            withName: {task} {{
                cpus = {task_resources[task]['cpus']}
                memory = {task_resources[task]['mem']}.GB
            }}
            """)  # noqa E202
            task_strings.append(task_string)

        tasks_joined = "".join(task_strings)

        with open(filename, "w") as out:
            out.write("process {")
            out.write(f"{tasks_joined}")
            out.write("}".lstrip())

    elif engine == "CWL":
        raise ValueError("--write-config does not currently support CWL workflows")
    elif engine == "WDL":
        raise ValueError("--write-config does not currently support WDL workflows")
    else:
        raise ValueError("Unknown workflow engine")
