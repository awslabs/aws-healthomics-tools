#!/usr/bin/env python3
"""
Command-line tool to create a sample sheet from active readsets

Usage: omics-samples [<sequenceStoreId>]
                   [--start=<date>]
                   [--end=<date>]
                   [--out=<path>]
                   [--profile=<profile>]
                   [--region=<region>]
                   [--sampleId=<sample>]
                   [--subjectId=<subject>]
                   [--help]

Options:
 -s, --start=<date>            Show runs completed after specified date/time (UTC)
 -e, --end=<date>              Show runs completed before specified date/time (UTC)
 --sampleId=<sampleId>         Select the sampleId
 --subjectId=<subjectId>       Select the subjectId
 -o, --out=<path>              Write output to file
 -p, --profile=<profile>       AWS profile
 -r, --region=<region>         AWS region
 -h, --help                    Show help text

Examples:
 # Create a sample sheet with samples created after the specified date
 omics-samples 1234567890 -s 2023-07-01
"""
import boto3
from botocore.config import Config
import docopt
from dateutil import parser
import logging
import sys 

opts = docopt.docopt(__doc__)
config = Config(retries={"max_attempts": 10, "mode": "standard"})
session = boto3.session.Session(profile_name=opts["--profile"], region_name=opts["--region"])
omics_client = session.client("omics", config=config)

logging.basicConfig(level=logging.INFO)


def get_samples(sqnid, filter):
    samples = []
    paginator = omics_client.get_paginator("list_read_sets")

    params = {"sequenceStoreId": sqnid, "filter": filter}

    for page in paginator.paginate(**params):
        read_sets = page.get("readSets", [])
        for read_set in read_sets:
            logging.info(f"Processing read set {read_set['id']}")
            data = omics_client.get_read_set_metadata(id=read_set["id"], sequenceStoreId=sqnid)
            sample = data["sampleId"]
            uri = data["files"]["source1"]["s3Access"]["s3Uri"]
            samples.append(f"{sqnid},{sample},{uri}")
    return samples


def get_filter(cli_opts) -> dict:
    filter = {"status": "ACTIVE"}
    if opts["--start"]:
        filter["createdAfter"] = parser.parse(opts["--start"])
    if opts["--end"]:
        filter["createdBefore"] = parser.parse(opts["--end"])
    if opts["--sampleId"]:
        filter["sampleId"] = opts["--sampleId"]
    if opts["--subjectId"]:
        filter["subjectId"] = opts["--subjectId"]
    return filter


def write_samples(samples, outpath):
    headers = ["sequenceStoreId", "sampleId", "s3Uri"]
    with open(outpath, "w") as out:
        out.write(",".join(headers) + "\n")
        for sample in samples:
            out.write(sample + "\n")


def main():
    sequence_store = opts["<sequenceStoreId>"]
    filter = get_filter(opts)
    samples = get_samples(sequence_store, filter)
    if opts["--out"]:
        write_samples(samples, opts["--out"])
    else:
        for sample in samples:
            print(sample)


if __name__ == "__main__":
    if opts["--sampleId"]:
        if not opts["--subjectId"]:
            logging.error("If using --sampleId you must also specify --sampleId")
            sys.exit(1)
    main()
