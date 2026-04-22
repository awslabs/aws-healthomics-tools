"""Tests for --parameter handling in `aws-healthomics-tools rerun`."""

import copy
import unittest

from omics.cli.rerun.__main__ import _set_nested, start_run_request


def _make_run(parameters=None):
    run = {
        "arn": "arn:aws:omics:us-west-2:1234:run/7777",
        "workflow": "arn:aws:omics:us-west-2:1234:workflow/5678",
    }
    if parameters is not None:
        run["parameters"] = parameters
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


if __name__ == "__main__":
    unittest.main()
