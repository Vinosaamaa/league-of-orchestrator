"""Synthetic contract checks for League's shared Engineering gate."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

GATE = module("validate-engineering-impact")
SCAFFOLD = module("new-engineering-receipt")
TITLE = "Require Engineering receipts"

class EngineeringGateTests(unittest.TestCase):
    def receipt(self, classification="none", refs=None):
        return SCAFFOLD.render(218, TITLE, "Require exact Engineering evidence before merging a pull request.", classification, refs or [])

    def validate(self, markdown, classification="none", index=None, **overrides):
        args = dict(repository="league-of-orchestrator", pr_number=218, pr_title=TITLE, classification=classification, record_index=index or {}, pr_url="https://github.com/Vinosaamaa/league-of-orchestrator/pull/218")
        args.update(overrides)
        return GATE.validate_receipt(markdown, "docs/engineering/changes/pr-218.md", **args)

    def test_forward_receipt_uses_league_identity(self):
        self.assertEqual(self.validate(self.receipt())["repository"], "league-of-orchestrator")

    def test_wrong_repository_number_title_and_type_fail(self):
        for override in ({"repository":"interview-arc"}, {"pr_number":219}, {"pr_title":"Different title"}, {"classification":"change-note"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.validate(self.receipt(), **override)

    def test_material_receipt_requires_exact_matching_revision(self):
        markdown = self.receipt("change-note", ["change-note-engineering-gate@1"])
        self.validate(markdown, "change-note", {"change-note-engineering-gate@1":"change-note"})
        for index in ({}, {"change-note-engineering-gate@2":"change-note"}, {"change-note-engineering-gate@1":"adr"}):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.validate(markdown, "change-note", index)

    def test_forward_receipt_cannot_claim_reconstruction(self):
        with self.assertRaises(ValueError):
            self.validate(self.receipt().replace("reconstructed: false", "reconstructed: true"))

    def test_missing_or_duplicate_classification_fails(self):
        for body in ("No classification", "- [x] Change Note\n- [x] ADR", "- [x] None — reason: TODO replace this"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                GATE.validate(body, [])
        GATE.validate("- [x] None — reason: Only a documentation typo changed.", [])

    def test_material_classification_matches_changed_record(self):
        GATE.validate("- [x] Change Note", ["change-note"])
        with self.assertRaises(ValueError):
            GATE.validate("- [x] ADR", ["change-note"])

    def test_scaffold_writes_once_in_synthetic_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "AGENTS.md").write_text("# Synthetic repository")
            (root / "docs/contracts").mkdir(parents=True)
            (root / "docs/contracts/engineering-pull-request-receipt.schema.json").write_text("{}")
            args = SCAFFOLD.parser().parse_args(["--pr","218","--title",TITLE,"--summary","Require exact Engineering evidence before merging.","--classification","none"])
            path = SCAFFOLD.run(args, root)
            self.validate(path.read_text())
            with self.assertRaises(SCAFFOLD.ScaffoldError):
                SCAFFOLD.run(args, root)

if __name__ == "__main__":
    unittest.main()
