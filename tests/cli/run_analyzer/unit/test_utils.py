import unittest

import botocore.session
from botocore.stub import Stubber

import omics.cli.run_analyzer.utils as utils


class TestRunAnalyzerUtils(unittest.TestCase):
    def test_engine_names(self):
        self.assertEqual(utils.ENGINES, set(["CWL", "WDL", "NEXTFLOW"]))

    def test_task_base_name(self):
        # CWL
        self.assertEqual(utils.task_base_name("test", "CWL"), "test")
        self.assertEqual(utils.task_base_name("test_1", "CWL"), "test")
        self.assertEqual(utils.task_base_name("test_again_1", "CWL"), "test_again")
        # WDL
        self.assertEqual(utils.task_base_name("test", "WDL"), "test")
        self.assertEqual(utils.task_base_name("test-01-1234", "WDL"), "test")
        self.assertEqual(utils.task_base_name("test_again-10-2345", "WDL"), "test_again")
        # Nextflow
        self.assertEqual(utils.task_base_name("test", "NEXTFLOW"), "test")
        self.assertEqual(utils.task_base_name("TEST:MODULE:FOO", "NEXTFLOW"), "TEST:MODULE:FOO")
        self.assertEqual(
            utils.task_base_name("TEST:MODULE:FOO (input1)", "NEXTFLOW"), "TEST:MODULE:FOO"
        )

    def test_task_base_name_invalid_engine(self):
        self.assertRaises(ValueError, utils.task_base_name, "test", "INVALID")

    def test_omics_instance_weight(self):
        def _weight(instance):
            return utils.omics_instance_weight(instance)

        self.assertTrue(_weight("omics.c.2xlarge") < _weight("omics.c.4xlarge"))
        self.assertTrue(_weight("omics.c.4xlarge") < _weight("omics.m.4xlarge"))
        self.assertTrue(_weight("omics.m.4xlarge") < _weight("omics.r.4xlarge"))
        self.assertTrue(_weight("omics.r.4xlarge") < _weight("omics.g4dn.4xlarge"))
        self.assertTrue(_weight("omics.r.4xlarge") < _weight("omics.g5.4xlarge"))

    def test_get_engine_with_workflow_arn(self):
        session = botocore.session.get_session()
        region = "us-west-2"
        omics = session.create_client(
            "omics",
            region,
            aws_access_key_id="foo",
            aws_secret_access_key="bar",
        )
        stubber = Stubber(omics)
        workflow_arn = "arn:aws:omics:us-east-1:123456789012:workflow/9876"
        stubber.add_response(
            "get_workflow",
            {
                "arn": workflow_arn,
                "id": "9876",
                "status": "ACTIVE",
                "type": "PRIVATE",
                "name": "hello",
                "engine": "WDL",
                "main": "main.wdl",
                "digest": "sha256:367f76a49c1e6f412a6fb319fcc7061d78ad612d06a9b8ef5b5e5f2e17a32e6f",
                "parameterTemplate": {
                    "param": {"description": "desc"},
                },
                "creationTime": "2024-04-19T14:38:56.492330+00:00",
                "statusMessage": "status",
                "tags": {},
            },
            {"id": "9876"},
        )
        stubber.activate()
        self.assertEqual(utils.get_engine(workflow_arn, client=omics), "WDL")
        stubber.deactivate()

    def test_get_engine_with_workflow_arn_and_owner_id(self):
        session = botocore.session.get_session()
        region = "us-west-2"
        omics = session.create_client(
            "omics",
            region,
            aws_access_key_id="foo",
            aws_secret_access_key="bar",
        )
        stubber = Stubber(omics)
        workflow_arn = "arn:aws:omics:us-east-1:123456789012:workflow/9876"
        stubber.add_response(
            "get_workflow",
            {
                "arn": workflow_arn,
                "id": "9876",
                "status": "ACTIVE",
                "type": "PRIVATE",
                "name": "hello",
                "engine": "WDL",
                "main": "main.wdl",
                "digest": "sha256:367f76a49c1e6f412a6fb319fcc7061d78ad612d06a9b8ef5b5e5f2e17a32e6f",
                "parameterTemplate": {
                    "param": {"description": "desc"},
                },
                "creationTime": "2024-04-19T14:38:56.492330+00:00",
                "statusMessage": "status",
                "tags": {},
            },
            {"id": "9876", "workflowOwnerId": "123456789012"},
        )
        stubber.activate()
        self.assertEqual(
            utils.get_engine(workflow_arn, client=omics, workflow_owner_id="123456789012"), "WDL"
        )
        stubber.deactivate()

    def test_get_instance_for_requirements(self):
        # Test basic functionality
        instance, cpus, mem = utils.get_instance_for_requirements(1, 1)
        self.assertEqual(instance, "omics.c.large")
        self.assertEqual(cpus, 2)
        self.assertEqual(mem, 4)

        # Test memory-optimized selection
        instance, cpus, mem = utils.get_instance_for_requirements(2, 8)
        self.assertEqual(instance, "omics.m.large")
        self.assertEqual(cpus, 2)
        self.assertEqual(mem, 8)

        # Test r-family selection (high memory requirement)
        instance, cpus, mem = utils.get_instance_for_requirements(2, 12)
        self.assertEqual(instance, "omics.r.large")
        self.assertEqual(cpus, 2)
        self.assertEqual(mem, 16)

        # Test larger instance selection
        instance, cpus, mem = utils.get_instance_for_requirements(8, 16)
        self.assertEqual(instance, "omics.c.2xlarge")
        self.assertEqual(cpus, 8)
        self.assertEqual(mem, 16)

        # Test 48xlarge support
        instance, cpus, mem = utils.get_instance_for_requirements(100, 400)
        self.assertEqual(instance, "omics.m.32xlarge")
        self.assertEqual(cpus, 128)
        self.assertEqual(mem, 512)

        # Test actual 48xlarge requirement
        instance, cpus, mem = utils.get_instance_for_requirements(192, 1536)
        self.assertEqual(instance, "omics.r.48xlarge")
        self.assertEqual(cpus, 192)
        self.assertEqual(mem, 1536)

        # Test requirements that exceed largest instance
        result = utils.get_instance_for_requirements(1000, 5000)
        self.assertEqual(result, ())
