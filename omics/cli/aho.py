"""
CLI Tools for working with the AWS HealthOmics Service.

Usage:
  aho run_analyzer [<args>...]
  aho rerun [<args>...]
"""

import sys
from docopt import docopt, DocoptExit


def main():
    try:
        args = docopt(__doc__, argv=sys.argv[1:2])
        command = sys.argv[1] if len(sys.argv) > 1 else None
        sub_args = sys.argv[2:]
        if command == "run_analyzer":
            from omics.cli.run_analyzer.__main__ import main as run_analyzer_main

            run_analyzer_main(sub_args)
        elif command == "rerun":
            from omics.cli.rerun.__main__ import main as rerun_main

            rerun_main(sub_args)
        else:
            print("Unknown or missing command.")
            print(__doc__)
            sys.exit(1)
    except DocoptExit:
        print("Unknown or missing command.")
        print(__doc__)
        sys.exit(1)
