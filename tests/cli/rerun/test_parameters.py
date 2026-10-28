"""Tests for request building in `aws-healthomics-tools rerun`."""

import copy
import unittest

from omics.cli.rerun.__main__ import (
    _set_nested,
    output_overwrite_warning,
    start_run_request,
)


def _make_run(parameters=None):
    run = {
        "arn": "arn:aws:omics:us-west-2:1234:run/7777",
        "workflow": "arn:aws:omics:us-west-2:1234:workflow/5678",
    }
    if parameters is not None:
        run["parameters"] = parameters
    return run


def _make_output_run(mode=None):
    run = _make_run()
    run["outputUri"] = "s3://my-bucket/my-path"
    if mode is not None:
        run["outputUriPrefixMode"] = mode
    return run


def _opts(**overrides):
    defaults = {
        "--workflow-id": None,
        "--workflow-version-name": None,
        "--workflow-type": None,
        "--run-id": None,
        "--role-arn": None,
        "--name": None,
        "--cache-id": None,
        "--cache-behavior": None,
        "--run-group-id": None,
        "--priority": None,
        "--parameter": [],
        "--storage-capacity": None,
        "--storage-type": None,
        "--workflow-owner-id": None,
        "--retention-mode": None,
        "--output-uri": None,
        "--output-uri-prefix-mode": None,
        "--log-level": None,
        "--tag": [],
    }
    defaults.update(overrides)
    return defaults


class TestSetNested(unittest.TestCase):
    def test_flat_key(self):
        params = {}
        _set_nested(params, "foo", "bar")
        self.assertEqual(params, {"foo": "bar"})

    def test_nested_creates_intermediates(self):
        params = {}
        _set_nested(params, "a.b.c", "X")
        self.assertEqual(params, {"a": {"b": {"c": "X"}}})

    def test_nested_preserves_siblings(self):
        params = {"a": {"b": {"c": "Y", "d": "Z"}}}
        _set_nested(params, "a.b.c", "X")
        self.assertEqual(params, {"a": {"b": {"c": "X", "d": "Z"}}})

    def test_scalar_conflict_dies(self):
        params = {"a": "scalar"}
        with self.assertRaises(SystemExit):
            _set_nested(params, "a.b", "X")


class TestParameterOverride(unittest.TestCase):
    def test_flat_override_preserves_full_uri(self):
        run = _make_run({"foo": "old"})
        uri = "s3://bucket/path:with/colons-and.dots-and-dashes"
        rqst = start_run_request(run, _opts(**{"--parameter": [f"foo={uri}"]}))
        self.assertEqual(rqst["parameters"]["foo"], uri)

    def test_nested_override_preserves_siblings(self):
        run = _make_run(
            {
                "docker_images": {
                    "aligner": "old:aligner-v1",
                    "caller": "keep:caller-v1",
                }
            }
        )
        new_img = "123456789012.dkr.ecr.us-west-2.amazonaws.com/tools:aligner-v2"
        rqst = start_run_request(
            run,
            _opts(**{"--parameter": [f"docker_images.aligner={new_img}"]}),
        )
        self.assertEqual(
            rqst["parameters"],
            {"docker_images": {"aligner": new_img, "caller": "keep:caller-v1"}},
        )

    def test_nested_override_creates_missing_intermediates(self):
        run = _make_run({"existing": "ok"})
        rqst = start_run_request(
            run,
            _opts(**{"--parameter": ["new.nested.key=val"]}),
        )
        self.assertEqual(
            rqst["parameters"],
            {"existing": "ok", "new": {"nested": {"key": "val"}}},
        )

    def test_does_not_mutate_source_run(self):
        source = {"docker_images": {"aligner": "old:v1"}}
        run = _make_run(copy.deepcopy(source))
        start_run_request(
            run,
            _opts(**{"--parameter": ["docker_images.aligner=new:v2"]}),
        )
        self.assertEqual(run["parameters"], source)

    def test_missing_equals_dies(self):
        run = _make_run({"foo": "old"})
        with self.assertRaises(SystemExit):
            start_run_request(run, _opts(**{"--parameter": ["no_equals_here"]}))

    def test_value_containing_equals_preserved(self):
        run = _make_run({"foo": "old"})
        rqst = start_run_request(
            run,
            _opts(**{"--parameter": ["foo=a=b=c"]}),
        )
        self.assertEqual(rqst["parameters"]["foo"], "a=b=c")


class TestOutputUriPrefixMode(unittest.TestCase):
    def test_manifest_mode_is_forwarded(self):
        rqst = start_run_request(_make_output_run("NONE_WITH_OVERWRITE"), _opts())
        self.assertEqual(rqst["outputUriPrefixMode"], "NONE_WITH_OVERWRITE")

    def test_absent_in_manifest_is_absent_in_request(self):
        rqst = start_run_request(_make_output_run(), _opts())
        self.assertNotIn("outputUriPrefixMode", rqst)

    def test_cli_override_replaces_manifest_mode(self):
        rqst = start_run_request(
            _make_output_run("NONE_WITH_OVERWRITE"),
            _opts(**{"--output-uri-prefix-mode": "SERVICE_DEFINED"}),
        )
        self.assertEqual(rqst["outputUriPrefixMode"], "SERVICE_DEFINED")

    def test_cli_sets_mode_on_run_without_one(self):
        rqst = start_run_request(
            _make_output_run(),
            _opts(**{"--output-uri-prefix-mode": "NONE_WITH_OVERWRITE"}),
        )
        self.assertEqual(rqst["outputUriPrefixMode"], "NONE_WITH_OVERWRITE")

    def test_invalid_mode_dies(self):
        with self.assertRaises(SystemExit):
            start_run_request(_make_output_run(), _opts(**{"--output-uri-prefix-mode": "BOGUS"}))


class TestOutputOverwriteWarning(unittest.TestCase):
    def test_warns_when_overwrite_run_keeps_output_uri(self):
        run = _make_output_run("NONE_WITH_OVERWRITE")
        warning = output_overwrite_warning(run, start_run_request(run, _opts()))
        self.assertIsNotNone(warning)
        self.assertIn("s3://my-bucket/my-path", warning)

    def test_warns_when_output_uri_override_equals_original(self):
        run = _make_output_run("NONE_WITH_OVERWRITE")
        rqst = start_run_request(run, _opts(**{"--output-uri": "s3://my-bucket/my-path"}))
        self.assertIsNotNone(output_overwrite_warning(run, rqst))

    def test_no_warning_when_output_uri_changed(self):
        run = _make_output_run("NONE_WITH_OVERWRITE")
        rqst = start_run_request(run, _opts(**{"--output-uri": "s3://my-bucket/other-path"}))
        self.assertIsNone(output_overwrite_warning(run, rqst))

    def test_no_warning_when_mode_overridden_to_service_defined(self):
        run = _make_output_run("NONE_WITH_OVERWRITE")
        rqst = start_run_request(run, _opts(**{"--output-uri-prefix-mode": "SERVICE_DEFINED"}))
        self.assertIsNone(output_overwrite_warning(run, rqst))

    def test_no_warning_for_run_without_mode(self):
        run = _make_output_run()
        self.assertIsNone(output_overwrite_warning(run, start_run_request(run, _opts())))

    def test_no_warning_when_mode_forced_on_run_without_one(self):
        # the original outputs live under <outputUri>/<runId>/<uuid>/ and are not touched
        run = _make_output_run()
        rqst = start_run_request(run, _opts(**{"--output-uri-prefix-mode": "NONE_WITH_OVERWRITE"}))
        self.assertIsNone(output_overwrite_warning(run, rqst))


if __name__ == "__main__":
    unittest.main()
