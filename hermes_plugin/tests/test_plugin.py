"""Pure-logic tests for the docpolicy Hermes plugin. Stdlib only, no hermes.

Run from the repo root: python3 -m unittest discover hermes_plugin/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import hermes_plugin as hp  # noqa: E402

MIN_POLICY = """\
version: 1

rules:
  severity:
    FM: error   # default FM severity is warn; enforcing repos promote it

classes:
  - id: archive
    mode: archive
    paths: ["**/archive/**"]
  - id: scratch
    mode: ephemeral
    paths: [".hermes/plans/**"]
    max_age_days: 2
    require_pointer: true
  - id: docs
    mode: canonical
    paths: ["**/*.md"]
"""

GOOD_FM = "---\nsubject: plan-a\nstatus: active\n---\n\n# Plan A\n"


def make_repo(tmp: Path, with_policy: bool = True) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    if with_policy:
        (tmp / ".docpolicy.yaml").write_text(MIN_POLICY, encoding="utf-8")
    return tmp


class ToolAndPathExtractionTest(unittest.TestCase):
    def test_watched_tools(self):
        for t in ("write_file", "patch", "create_file"):
            self.assertTrue(hp.is_watched_tool(t))
        for t in ("read_file", "terminal", "web_search", "", None):
            self.assertFalse(hp.is_watched_tool(t))

    def test_extract_file_path_keys(self):
        self.assertEqual(hp.extract_file_path({"path": "a.md"}), "a.md")
        self.assertEqual(hp.extract_file_path({"file_path": "b.md"}), "b.md")
        self.assertEqual(hp.extract_file_path({"filepath": "c.md"}), "c.md")
        self.assertEqual(hp.extract_file_path({"target_path": "d.md"}), "d.md")

    def test_extract_file_path_missing(self):
        self.assertIsNone(hp.extract_file_path({"content": "x"}))
        self.assertIsNone(hp.extract_file_path({}))
        self.assertIsNone(hp.extract_file_path(None))
        self.assertIsNone(hp.extract_file_path({"path": ""}))
        self.assertIsNone(hp.extract_file_path({"path": 42}))

    def test_markdown_filter(self):
        self.assertTrue(hp.is_markdown("docs/x.md"))
        self.assertTrue(hp.is_markdown("docs/x.MD"))
        self.assertFalse(hp.is_markdown("docs/x.txt"))
        self.assertFalse(hp.is_markdown("docs/x.md.bak"))


class RepoRootWalkerTest(unittest.TestCase):
    def test_walks_up_to_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_repo(Path(td) / "repo")
            nested = root / "docs" / "deep"
            nested.mkdir(parents=True)
            f = nested / "note.md"
            f.write_text("hi", encoding="utf-8")
            self.assertEqual(hp.find_repo_root(str(f)), root)

    def test_file_at_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_repo(Path(td) / "repo")
            f = root / "a.md"
            f.write_text("x", encoding="utf-8")
            self.assertEqual(hp.find_repo_root(str(f)), root)

    def test_no_policy_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = make_repo(Path(td) / "repo", with_policy=False)
            f = root / "a.md"
            f.write_text("x", encoding="utf-8")
            self.assertIsNone(hp.find_repo_root(str(f)))


class BlockDecisionTest(unittest.TestCase):
    def setUp(self):
        self.findings = [
            {"code": "FM", "severity": "error", "path": "docs/a.md",
             "message": "missing frontmatter"},
            {"code": "C5", "severity": "warn", "path": "docs/a.md",
             "message": "broken link"},
        ]

    def test_fm_error_blocks(self):
        action, msg = hp.decide(self.findings, "docs/a.md", advisory=False)
        self.assertEqual(action, "block")
        self.assertIn("FM", msg)
        self.assertIn("docs/a.md", msg)

    def test_warn_only_allows(self):
        warnings = [self.findings[1]]
        action, msg = hp.decide(warnings, "docs/a.md", advisory=False)
        self.assertEqual(action, "allow")
        self.assertIsNone(msg)

    def test_other_path_error_does_not_block(self):
        other = [{"code": "C2", "severity": "error", "path": "docs/other.md",
                  "message": "collision"}]
        action, _ = hp.decide(other, "docs/a.md", advisory=False)
        self.assertEqual(action, "allow")

    def test_advisory_downgrades_block(self):
        action, _ = hp.decide(self.findings, "docs/a.md", advisory=True)
        self.assertEqual(action, "allow")

    def test_all_block_codes(self):
        for code in ("FM", "C2", "C7", "C9"):
            f = [{"code": code, "severity": "error", "path": "docs/a.md", "message": "x"}]
            action, _ = hp.decide(f, "docs/a.md", advisory=False)
            self.assertEqual(action, "block", code)

    def test_non_block_codes_do_not_block(self):
        for code in ("C1", "C3", "C4", "C5", "C6", "C8"):
            f = [{"code": code, "severity": "error", "path": "docs/a.md", "message": "x"}]
            action, _ = hp.decide(f, "docs/a.md", advisory=False)
            self.assertEqual(action, "allow", code)

    def test_warn_severity_of_block_code_does_not_block(self):
        f = [{"code": "C7", "severity": "warn", "path": "docs/a.md", "message": "x"}]
        action, _ = hp.decide(f, "docs/a.md", advisory=False)
        self.assertEqual(action, "allow")

    def test_non_blocking_split(self):
        blocked = hp.blocking_findings(self.findings, "docs/a.md")
        rest = hp.non_blocking_findings(self.findings, "docs/a.md")
        self.assertEqual([f["code"] for f in blocked], ["FM"])
        self.assertEqual([f["code"] for f in rest], ["C5"])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DOCPOLICY_PLUGIN_HOME"] = self.tmp.name

    def tearDown(self):
        del os.environ["DOCPOLICY_PLUGIN_HOME"]
        self.tmp.cleanup()

    def test_creates_default_config(self):
        cfg = hp.load_config()
        self.assertFalse(cfg["advisory"])
        self.assertEqual(cfg["repos"], [])
        self.assertTrue((Path(self.tmp.name) / "config.json").is_file())

    def test_loads_existing_config(self):
        p = Path(self.tmp.name) / "config.json"
        p.write_text(json.dumps({"advisory": True, "repos": ["/a"]}), encoding="utf-8")
        cfg = hp.load_config()
        self.assertTrue(cfg["advisory"])
        self.assertEqual(cfg["repos"], ["/a"])

    def test_corrupt_config_falls_back(self):
        p = Path(self.tmp.name) / "config.json"
        p.write_text("{not json", encoding="utf-8")
        cfg = hp.load_config()
        self.assertFalse(cfg["advisory"])

    def test_repos_allowlist(self):
        self.assertTrue(hp._repos_allowed(Path("/any/repo"), {"repos": []}))
        self.assertTrue(hp._repos_allowed(Path("/a/b"), {"repos": ["/a/b"]}))
        self.assertFalse(hp._repos_allowed(Path("/a/c"), {"repos": ["/a/b"]}))


class HookFailOpenTest(unittest.TestCase):
    def test_hook_never_raises(self):
        # garbage args, unknown tool, missing paths — must all return None
        self.assertIsNone(hp._on_pre_tool_call(tool_name="terminal", args={"command": "ls"}))
        self.assertIsNone(hp._on_pre_tool_call(tool_name="write_file", args=None))
        self.assertIsNone(hp._on_pre_tool_call(tool_name="write_file",
                                               args={"path": 12345}))
        self.assertIsNone(hp._on_pre_tool_call(tool_name="write_file",
                                               args={"path": "/nonexistent/../x.md"}))

    def test_observers_never_raise(self):
        self.assertIsNone(hp._on_session_start(session_id="t1"))
        self.assertIsNone(hp._on_session_end(session_id="t1"))


class CheckerIntegrationTest(unittest.TestCase):
    """End-to-end: real docpolicy checker subprocess + plugin decision logic."""

    def run_checker(self, repo, path):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            ["python3", "-m", "docpolicy", "check", "--repo", str(repo),
             "--files", str(path), "--json"],
            cwd=str(repo), env=env, capture_output=True, text=True, timeout=30,
            check=False)
        return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else None

    def test_missing_frontmatter_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td) / "repo")
            doc = repo / "docs"
            doc.mkdir()
            f = doc / "a.md"
            f.write_text("# No frontmatter\n", encoding="utf-8")
            rc, payload = self.run_checker(repo, f)
            findings = payload["findings"]
            action, msg = hp.decide(findings, "docs/a.md", advisory=False)
            self.assertEqual(rc, 1)
            self.assertEqual(action, "block")
            self.assertIn("FM", msg)

    def test_clean_doc_allows(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td) / "repo")
            doc = repo / "docs"
            doc.mkdir()
            f = doc / "a.md"
            f.write_text(GOOD_FM, encoding="utf-8")
            rc, payload = self.run_checker(repo, f)
            self.assertEqual(rc, 0)
            self.assertEqual(payload["findings"], [])
            action, _ = hp.decide(payload["findings"], "docs/a.md", advisory=False)
            self.assertEqual(action, "allow")

    def test_superseded_without_target_does_not_block(self):
        # C3 is NOT in BLOCK_CODES — must allow (advisory-only path)
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td) / "repo")
            doc = repo / "docs"
            doc.mkdir()
            f = doc / "old.md"
            f.write_text("---\nsubject: plan-b\nstatus: superseded\n---\n\n# Old\n",
                         encoding="utf-8")
            rc, payload = self.run_checker(repo, f)
            action, _ = hp.decide(payload["findings"], "docs/old.md", advisory=False)
            self.assertEqual(action, "allow")

    def test_session_end_summary(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td) / "repo")
            os.environ["DOCPOLICY_PLUGIN_HOME"] = td  # reuse tmp as plugin home
            try:
                cwd = os.getcwd()
                os.chdir(repo)
                try:
                    hp._on_session_end(session_id="it")
                finally:
                    os.chdir(cwd)
                log = (Path(td) / "advisory.log").read_text(encoding="utf-8")
                self.assertIn("session_end check", log)
                self.assertIn("total=", log)
            finally:
                del os.environ["DOCPOLICY_PLUGIN_HOME"]


class CheckerArgsTest(unittest.TestCase):
    """Pre-write evaluation: write_file content goes via stdin, patch falls back."""

    def test_write_file_uses_stdin(self):
        args = {"path": "docs/x.md", "content": "---\nsubject: s\nstatus: active\n---\n"}
        extra, stdin = hp._checker_args(Path("/r"), "write_file", args, "docs/x.md",
                                        "/r/docs/x.md")
        self.assertIn("--stdin-file", extra)
        self.assertNotIn("--files", extra)
        self.assertEqual(stdin, args["content"])

    def test_patch_falls_back_to_disk(self):
        extra, stdin = hp._checker_args(Path("/r"), "patch", {"path": "docs/x.md"},
                                        "docs/x.md", "/r/docs/x.md")
        self.assertIn("--files", extra)
        self.assertNotIn("--stdin-file", extra)
        self.assertIsNone(stdin)

    def test_extract_content_non_string(self):
        self.assertIsNone(hp.extract_content({"content": 42}))
        self.assertIsNone(hp.extract_content(None))
        self.assertEqual(hp.extract_content({"content": "x"}), "x")


INBOX_MIN_POLICY = """\
version: 1
classes:
  - id: inbox
    mode: inbox
    paths: ["inbox/**"]
    dest: "docs"
    max_age_days: 14
  - id: docs
    mode: canonical
    paths: ["docs/**/*.md"]
"""


class InboxPickupTest(unittest.TestCase):
    """pre_llm_call is the pickup path: it is the only hook whose return value
    is injected into the turn (on_session_start is observer-only)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "inbox").mkdir(parents=True)
        (self.repo / "docs").mkdir()
        (self.repo / ".docpolicy.yaml").write_text(INBOX_MIN_POLICY, encoding="utf-8")
        os.environ["DOCPOLICY_PLUGIN_HOME"] = self.tmp.name
        # Hermeticity: point the model-layer config at a missing file so triage
        # stays deterministic even when this machine has an operator LLM key
        # configured (~/.hermes/docpolicy/llm.json) — otherwise the subprocess
        # applies the LLM layer and makes real network calls during tests.
        self._llm_cfg = os.environ.get("DOCPOLICY_LLM_CONFIG")
        os.environ["DOCPOLICY_LLM_CONFIG"] = str(
            Path(self.tmp.name) / "no-llm-config.json")
        self._cwd = os.getcwd()
        os.chdir(self.repo)
        hp._inbox_cache.update({"repo": "", "at": 0.0, "text": None})

    def tearDown(self):
        os.chdir(self._cwd)
        os.environ.pop("DOCPOLICY_PLUGIN_HOME", None)
        if self._llm_cfg is None:
            os.environ.pop("DOCPOLICY_LLM_CONFIG", None)
        else:
            os.environ["DOCPOLICY_LLM_CONFIG"] = self._llm_cfg
        hp._inbox_cache.update({"repo": "", "at": 0.0, "text": None})
        self.tmp.cleanup()

    def test_inbox_class_detection(self):
        self.assertTrue(hp.has_inbox_class(self.repo))
        (self.repo / ".docpolicy.yaml").write_text(MIN_POLICY, encoding="utf-8")
        self.assertFalse(hp.has_inbox_class(self.repo))

    def test_no_captures_injects_nothing(self):
        self.assertIsNone(hp.inbox_notice(self.repo))
        self.assertIsNone(hp._on_pre_llm_call(session_id="t"))

    def test_capture_is_surfaced_and_promotable(self):
        (self.repo / "inbox/rough.md").write_text("# Rough note\n\nCaptured text.\n",
                                                  encoding="utf-8")
        notice = hp.inbox_notice(self.repo)
        self.assertIsNotNone(notice)
        self.assertIn("1 untriaged capture", notice)
        self.assertIn("docpolicy_triage", notice)
        result = hp._on_pre_llm_call(session_id="t")
        self.assertIsInstance(result, dict)
        self.assertIn("untriaged capture", result["context"])

    def test_triage_tool_returns_json_proposals(self):
        (self.repo / "inbox/rough.md").write_text("# Rough note\n\nCaptured text.\n",
                                                  encoding="utf-8")
        payload = json.loads(hp.tool_docpolicy_triage({"repo": str(self.repo)}))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["subject"], "rough")
        self.assertEqual(payload["items"][0]["dest"], "docs")

    def test_check_tool_returns_findings(self):
        # regression: run_checker does NOT prepend a subcommand — the handlers must
        (self.repo / "docs/bare.md").write_text("# bare\n", encoding="utf-8")
        payload = json.loads(hp.tool_docpolicy_check({"repo": str(self.repo)}))
        self.assertIn("summary", payload, payload)
        self.assertGreaterEqual(payload["summary"]["total"], 1)
        self.assertTrue(any(f["code"] == "FM" for f in payload["findings"]))

    def test_triage_tool_on_repo_without_policy(self):
        payload = json.loads(hp.tool_docpolicy_triage({"repo": self.tmp.name}))
        self.assertIn("error", payload)

    def test_hook_survives_broken_policy(self):
        (self.repo / ".docpolicy.yaml").write_text("classes: [",
                                                   encoding="utf-8")
        self.assertIsNone(hp._on_pre_llm_call(session_id="t"))  # fail open


class CliPassthroughTest(unittest.TestCase):
    """`hermes docpolicy <cmd> ...` forwards verbatim to the checker."""

    def _parse(self, argv):
        import argparse
        import io
        import contextlib
        register_cli, docpolicy_command = hp._cli_hooks()
        self.assertIsNotNone(register_cli, "CLI hooks must resolve")
        parser = argparse.ArgumentParser(prog="hermes docpolicy")
        register_cli(parser)
        args = parser.parse_args(argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = docpolicy_command(args)
        return rc, buf.getvalue()

    def test_cli_module_resolves_lazily(self):
        """The loader execs this package under a name of its choosing, so the
        CLI import must be relative (an absolute self-import broke doctor)."""
        register_cli, docpolicy_command = hp._cli_hooks()
        self.assertTrue(callable(register_cli))
        self.assertTrue(callable(docpolicy_command))

    def test_no_subcommand_prints_usage(self):
        rc, out = self._parse([])
        self.assertEqual(rc, 2)
        self.assertIn("hermes docpolicy", out)

    def test_llm_subcommand_reports_config(self):
        # the checker's own output goes to the inherited stdout, so assert on the
        # exit code here; the config resolution itself is tested in the core suite
        with tempfile.TemporaryDirectory() as td:
            os.environ["DOCPOLICY_LLM_CONFIG"] = str(Path(td) / "llm.json")
            try:
                rc, _ = self._parse(["llm"])
                self.assertEqual(rc, 0)
            finally:
                del os.environ["DOCPOLICY_LLM_CONFIG"]

    def test_check_subcommand_forwards_flags(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td) / "repo")
            rc, out = self._parse(["check", "--repo", str(repo), "--json"])
            self.assertEqual(rc, 0, out)

    def test_unknown_repo_path_is_reported(self):
        rc, out = self._parse(["check", "--repo", "/nonexistent/repo/xyz"])
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
