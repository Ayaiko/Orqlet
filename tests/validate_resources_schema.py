"""Optional validator-backed integration tests; no host capture or private logs."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, ValidationError

from orqlet.cli import main
from orqlet.correlation import read_metadata
from orqlet.profile import InputError
from test_correlation import alignment_fixture, capture_fixture, timeline_fixture


ROOT = Path(__file__).resolve().parents[1]


class ResourceSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.capture = capture_fixture()
        self.timeline = {"schema": "orqlet.timeline.v1", "inputs": [timeline_fixture()]}
        self.alignment = alignment_fixture()
        self.schemas = {}
        for name in ["resources-v1", "clock-alignment-v1", "correlation-v1"]:
            schema = json.loads((ROOT / "schemas" / (name + ".schema.json")).read_text())
            Draft202012Validator.check_schema(schema)
            self.schemas[name] = Draft202012Validator(schema)

    def write(self):
        capture = self.directory / "PRIVATE_CAPTURE.json"
        timeline = self.directory / "PRIVATE_TIMELINE.json"
        alignment = self.directory / "PRIVATE_ALIGNMENT.json"
        capture.write_text(json.dumps(self.capture))
        timeline.write_text(json.dumps(self.timeline))
        self.alignment["capture_sha256"] = hashlib.sha256(capture.read_bytes()).hexdigest()
        self.alignment["timeline_sha256"] = hashlib.sha256(timeline.read_bytes()).hexdigest()
        alignment.write_text(json.dumps(self.alignment))
        return capture, timeline, alignment

    def cli(self, paths):
        capture, timeline, alignment = paths
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["correlate", str(capture), str(timeline), "--alignment", str(alignment)])
        return status, out.getvalue(), err.getvalue()

    def test_schema_validated_files_cross_real_cli_and_timeline_adapter(self):
        self.schemas["resources-v1"].validate(self.capture)
        self.schemas["clock-alignment-v1"].validate(self.alignment)
        status, output, error = self.cli(self.write())
        self.assertEqual(status, 0, error)
        result = json.loads(output)
        self.schemas["correlation-v1"].validate(result)
        self.assertEqual(result["metadata_coverage_ms"], 4000)
        self.assertEqual(result["alignment_evidence"], "caller_supplied_anchors")
        self.assertNotIn("PRIVATE", output + error)
        self.assertNotIn("sha256", output)

    def test_every_input_boundary_rejects_unapproved_private_fields_without_output(self):
        originals = copy.deepcopy((self.capture, self.timeline, self.alignment))
        for target in ["capture", "cpu", "collector", "timeline", "event", "alignment", "anchor"]:
            self.capture, self.timeline, self.alignment = copy.deepcopy(originals)
            objects = {"capture": self.capture, "cpu": self.capture["samples"][1]["cpu"],
                       "collector": self.capture["collector"], "timeline": self.timeline,
                       "event": self.timeline["inputs"][0]["events"][0], "alignment": self.alignment,
                       "anchor": self.alignment["anchors"][0]}
            objects[target]["prompt"] = "PRIVATE_SECRET"
            status, output, error = self.cli(self.write())
            self.assertEqual(status, 2)
            self.assertEqual(output, "")
            self.assertNotIn("PRIVATE", error)

    def test_finite_numbers_duplicate_keys_and_byte_limits_fail_closed(self):
        capture_path, _, _ = self.write()
        for raw, code in [(b'{"schema":"orqlet.resources.v1","schema":"PRIVATE"}', "duplicate_metadata_key"),
                          (b'{"value":1e999}', "invalid_metadata_number"),
                          (b'{"value":NaN}', "invalid_metadata_number"),
                          (b'{"value":' + b'1' * 80 + b'}', "invalid_metadata_number")]:
            capture_path.write_bytes(raw)
            with self.assertRaisesRegex(InputError, code):
                read_metadata(capture_path, 1024, "resources-v1.schema.json")
        capture_path.write_bytes(b'x' * 1025)
        with self.assertRaisesRegex(InputError, "metadata_limit_exceeded"):
            read_metadata(capture_path, 1024, "resources-v1.schema.json")

    def test_unknown_versions_raw_logs_and_invalid_input_selection_fail(self):
        self.timeline = {"type": "assistant", "message": {"content": "PRIVATE_PROMPT"}}
        status, output, error = self.cli(self.write())
        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        self.assertNotIn("PRIVATE", error)
        self.timeline = {"schema": "orqlet.timeline.v2", "inputs": []}
        self.assertEqual(self.cli(self.write())[0], 2)

    def test_changed_metadata_after_anchor_creation_is_not_accepted(self):
        paths = self.write()
        self.capture["collector"]["python_cpu_ms"] = 2
        paths[0].write_text(json.dumps(self.capture))
        status, output, error = self.cli(paths)
        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        self.assertIn("clock_alignment_identity_mismatch", error)

    def test_schemas_reject_false_normal_pressure_and_numeric_unknown_cpu(self):
        for change in [lambda c: c["samples"][0]["pressure"].update(state="normal"),
                       lambda c: c["samples"][0]["cpu"].update(busy_percent=0),
                       lambda c: c["collector"].update(physical_footprint_bytes=123)]:
            value = copy.deepcopy(self.capture)
            change(value)
            with self.assertRaises(ValidationError):
                self.schemas["resources-v1"].validate(value)


if __name__ == "__main__":
    unittest.main()
