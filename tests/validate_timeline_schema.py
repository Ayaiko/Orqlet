"""Additional contract validation using a standard Draft 2020-12 validator.

Run via unittest discover -s tests -p validate_timeline_schema.py after installing
requirements-schema-test.txt into a disposable environment. No runtime dependency.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, ValidationError

from orqlet.timeline import SCHEMA, timeline_file
from test_profile import cx, event, span, window
from test_timeline import wait_metadata, wait_records


class TimelineSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((Path(__file__).resolve().parents[1] /
                                 "schemas/session-timeline-v1.schema.json").read_text())
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def fixture(self, records):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
            return {"schema": SCHEMA, "inputs": [timeline_file(path)]}

    def test_schema_validates_all_evidence_levels_and_timing_failures(self):
        bad_item = wait_metadata()
        bad_item["payload"]["started_at_ms"] = 1e300
        for records in [wait_records(), [span("a", 1, 3, "agent_wait"), window("w", 0, 5)],
                        [event(1, "item_completed", item={"type": "Reasoning"})], [bad_item],
                        [cx(3, "function_call", call_id="a", name="Read"),
                         cx(1, "function_call_output", call_id="a")],
                        [cx(1, "function_call", call_id="a", name="wait_agent")]]:
            self.validator.validate(self.fixture(records))

    def test_schema_rejects_arbitrary_fields_labels_and_source_ids(self):
        original = self.fixture(wait_records())
        for field, value in [("prompt", "private"), ("tool_args", {}), ("path", "private"),
                             ("id", "provider-original-id"), ("category", "private-tool-name"),
                             ("source", "private")]:
            changed = copy.deepcopy(original)
            changed["inputs"][0]["events"][0][field] = value
            with self.assertRaises(ValidationError):
                self.validator.validate(changed)
        for target in [original, original["inputs"][0], original["inputs"][0]["clock"]]:
            changed = copy.deepcopy(target)
            changed["private"] = "secret"
            # Select the matching subschema so every object boundary is checked.
            schema = (self.schema if target is original else self.schema["$defs"]["input"]
                      if target is original["inputs"][0] else
                      self.schema["$defs"]["input"]["properties"]["clock"])
            with self.assertRaises(ValidationError):
                Draft202012Validator({**schema, "$defs": self.schema["$defs"]}).validate(changed)

    def test_schema_rejects_claiming_verified_or_measured_unknown_timing(self):
        original = self.fixture([cx(1, "function_call", call_id="a", name="wait_agent")])
        for field, value in [("evidence", "verified_metadata"), ("duration_ms", 60000)]:
            changed = copy.deepcopy(original)
            changed["inputs"][0]["events"][0][field] = value
            with self.assertRaises(ValidationError):
                self.validator.validate(changed)

    def test_schema_rejects_unknown_versions_and_negative_durations(self):
        original = self.fixture(wait_records())
        changed = copy.deepcopy(original)
        changed["schema"] = "orqlet.timeline.v2"
        with self.assertRaises(ValidationError):
            self.validator.validate(changed)
        original["inputs"][0]["events"][0]["duration_ms"] = -1
        with self.assertRaises(ValidationError):
            self.validator.validate(original)


if __name__ == "__main__":
    unittest.main()
