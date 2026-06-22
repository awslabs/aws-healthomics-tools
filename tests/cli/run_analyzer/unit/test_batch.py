import io
import unittest

import omics.cli.run_analyzer.batch as batch
from omics.cli.run_analyzer.pricing import PricingCache


class TestRunAnalyzerBatch(unittest.TestCase):

    def test_aggregate_and_print_resources(self):
        resources_list = [
            [
                {
                    "metrics": {
                        "runningSeconds": 10.0,
                        "cpuUtilizationRatio": 1.0,
                        "memoryUtilizationRatio": 1.0,
                        "gpusReserved": 0,
                        "recommendedCpus": 4,
                        "recommendedMemoryGiB": 8,
                        "omicsInstanceTypeMinimum": "omics.c.large",
                        "estimatedUSD": 1.00,
                    },
                    "name": "foo-01-000",
                    "arn": "arn:aws:omics:us-east-1:123456789012:task/111113",
                },
                {
                    "metrics": {
                        "runningSeconds": 20.0,
                        "cpuUtilizationRatio": 1.0,
                        "memoryUtilizationRatio": 1.0,
                        "gpusReserved": 0,
                        "recommendedCpus": 4,
                        "recommendedMemoryGiB": 8,
                        "omicsInstanceTypeMinimum": "omics.c.large",
                        "estimatedUSD": 1.00,
                    },
                    "name": "foo-02-000",
                    "arn": "arn:aws:omics:us-east-1:123456789012:task/123458",
                },
                {
                    "metrics": {},
                    "name": "foo-02-000",
                    "arn": "arn:aws:omics:us-east-1:123456789012:run/98765",
                },
            ],
            [
                {
                    "metrics": {
                        "runningSeconds": 30.0,
                        "cpuUtilizationRatio": 0.5,
                        "memoryUtilizationRatio": 0.5,
                        "gpusReserved": 0,
                        "recommendedCpus": 4,
                        "recommendedMemoryGiB": 8,
                        "omicsInstanceTypeMinimum": "omics.c.large",
                        "estimatedUSD": 1.00,
                    },
                    "name": "foo-01-050",
                    "arn": "arn:aws:omics:us-east-1:123456789012:task/111111",
                },
                {
                    "metrics": {
                        "runningSeconds": 20.0,
                        "cpuUtilizationRatio": 0.5,
                        "memoryUtilizationRatio": 0.5,
                        "gpusReserved": 0,
                        "recommendedCpus": 4,
                        "recommendedMemoryGiB": 8,
                        "omicsInstanceTypeMinimum": "omics.c.large",
                        "estimatedUSD": 1.00,
                    },
                    "name": "foo-02-010",
                    "arn": "arn:aws:omics:us-east-1:123456789012:task/123456",
                },
                {
                    "metrics": {},
                    "name": "foo-02-000",
                    "arn": "arn:aws:omics:us-east-1:123456789012:run/87654",
                },
            ],
        ]
        header_string = ",".join(batch.hdrs) + "\n"
        expected = f"{header_string}task,foo,4,20.0,30.0,8.165,1.0,0.75,1.0,0.75,0,0,4,8,omics.c.large,1.0,1.0\n"
        with io.StringIO() as result:
            batch.aggregate_and_print(
                run_resources_list=resources_list,
                pricing_cache=PricingCache(),
                engine="WDL",
                out=result,
            )
            self.assertEqual(result.getvalue(), expected)
