"""docpolicy core tests — unittest, stdlib only. Run: python3 -m unittest discover tests -v"""

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from docpolicy import checks, classify
from docpolicy import frontmatter as fmod
from docpolicy.miniyaml import loads as yloads
from docpolicy.policy import PolicyError, load

POLICY = """\
# test policy
version: 1
sources:
  issues: {issues}
classes:
  - id: docs
    mode: canonical
    paths: ["docs/**/*.md"]
  - id: archive
    mode: archive
    paths: ["**/archive/**"]
  - id: scratch
    mode: ephemeral
    paths: [".hermes/plans/**"]
    max_age_days: 2
    require_pointer: true
"""


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class MiniYamlTest(unittest.TestCase):
    def test_mapping_list_scalars(self):
        doc = yloads("""
version: 1
name: "hello world"
n: 42
flag: true
# comment
classes:
  - id: docs
    mode: canonical
    paths: ["docs/**/*.md"]
    max_age_days: 2
""")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["name"], "hello world")
        self.assertEqual(doc["n"], 42)
        self.assertIs(doc["flag"], True)
        c = doc["classes"][0]
        self.assertEqual(c["id"], "docs")
        self.assertEqual(c["paths"], ["docs/**/*.md"])
        self.assertEqual(c["max_age_days"], 2)

    def test_block_scalars(self):
        doc = yloads(
            "type: Research Topic\n"
            "description: >\n"
            "  line one of the summary\n"
            "  continues here\n"
            "tags: [a, b]\n"
            "body: |\n"
            "  literal line 1\n"
            "  literal line 2\n"
        )
        self.assertEqual(doc["type"], "Research Topic")
        self.assertIn("line one", doc["description"])
        self.assertIn("continues", doc["description"])
        self.assertIn("\n", doc["body"])
        self.assertEqual(doc["tags"], ["a", "b"])

    def test_nested_block(self):
        doc = yloads("rules:\n  severity:\n    C5: error\n    C8: off\n")
        self.assertEqual(doc["rules"]["severity"]["C5"], "error")
        self.assertEqual(doc["rules"]["severity"]["C8"], "off")


class FrontmatterTest(unittest.TestCase):
    def test_ok(self):
        fmd, body, had = fmod.parse("---\nsubject: x\nstatus: active\ntags: [a, b]\n---\n\nbody\n")
        self.assertTrue(had)
        self.assertEqual(fmd["subject"], "x")
        self.assertEqual(fmd["status"], "active")
        self.assertIn("body", body)

    def test_missing(self):
        fmd, _, had = fmod.parse("just text")
        self.assertFalse(had)
        self.assertIsNone(fmd)

    def test_malformed(self):
        fmd, _, had = fmod.parse("---\nsubject: [unclosed\n")
        self.assertTrue(had)
        self.assertIsNone(fmd)


class ClassifyTest(unittest.TestCase):
    def test_globs(self):
        self.assertTrue(classify.match("docs/**/*.md", "docs/specs/x.md"))
        self.assertTrue(classify.match("docs/**/*.md", "docs/a/b/c.md"))
        self.assertFalse(classify.match("docs/*.md", "docs/specs/x.md"))
        self.assertTrue(classify.match("docs/*.md", "docs/x.md"))
        self.assertTrue(classify.match("**/archive/**", "docs/archive/old.md"))
        self.assertFalse(classify.match("**/archive/**", "docs/old.md"))
        self.assertTrue(classify.match(".hermes/plans/**", ".hermes/plans/2026-09-02_x.md"))


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docpolicy-test-"))
        self.repo = self.tmp / "repo"
        issues = self.tmp / "queue.db"
        import sqlite3
        con = sqlite3.connect(issues)
        con.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
        con.execute("INSERT INTO tasks (id, title) VALUES (7, 'real task')")
        con.commit()
        con.close()
        write(self.repo / ".docpolicy.yaml", POLICY.format(issues=issues))

    def policy(self):
        return load(self.repo)

    def test_non_dict_severity_warns_and_keeps_defaults(self):
        # inline maps parse as a plain string: overrides must not be silently dropped
        write(self.repo / ".docpolicy.yaml",
              POLICY.format(issues=self.tmp / "queue.db")
              + "rules:\n  severity: { C5: error, C10: off }\n")
        err = io.StringIO()
        import sys
        old = sys.stderr
        sys.stderr = err
        try:
            pol = self.policy()
        finally:
            sys.stderr = old
        self.assertIn("rules.severity is not a mapping", err.getvalue())
        self.assertEqual(pol.severity["C5"], "warn")
        self.assertEqual(pol.severity["C10"], "warn")

    def codes(self, findings):
        return [f.code for f in findings]

    def test_first_match_wins(self):
        write(self.repo / "docs/a.md", "---\nsubject: a\nstatus: active\n---\n")
        write(self.repo / "docs/archive/a-old.md", "---\nsubject: a-old\nstatus: archived\n---\n")
        write(self.repo / "misc/orphan.md", "# stray")           # no class
        pol = self.policy()
        # archive class is declared before docs class -> first match wins
        findings = checks.check(self.repo, pol)
        msgs = [f.message for f in findings if f.code == "C1"]
        self.assertTrue(any("unclassified" in m for m in msgs), msgs)
        self.assertFalse(any("ambiguous" in m for m in msgs), msgs)
        # archive doc is NOT flagged: archive class owns it
        self.assertFalse(any(f.path == "docs/archive/a-old.md" for f in findings), msgs)

    def test_subject_collision(self):
        write(self.repo / "docs/one.md", "---\nsubject: ladder\nstatus: active\n---\n")
        write(self.repo / "docs/two.md", "---\nsubject: ladder\nstatus: active\n---\n")
        findings = checks.check(self.repo, self.policy())
        self.assertIn("C2", self.codes(findings))

    def test_superseded_chain_ok(self):
        write(self.repo / "docs/new.md", "---\nsubject: ladder\nstatus: active\n---\n")
        write(self.repo / "docs/old.md",
              "---\nsubject: ladder\nstatus: superseded\nsuperseded_by: docs/new.md\n---\n")
        findings = checks.check(self.repo, self.policy())
        self.assertEqual([f for f in findings if f.severity == "error"], [],
                         str([f.as_dict() for f in findings]))

    def test_superseded_missing_target(self):
        write(self.repo / "docs/old.md",
              "---\nsubject: ladder\nstatus: superseded\nsuperseded_by: docs/gone.md\n---\n")
        self.assertIn("C3", self.codes(checks.check(self.repo, self.policy())))

    def test_superseded_target_not_active(self):
        write(self.repo / "docs/new.md", "---\nsubject: ladder\nstatus: superseded\n"
              "superseded_by: docs/newer.md\n---\n")
        write(self.repo / "docs/newer.md", "---\nsubject: ladder\nstatus: superseded\n"
              "superseded_by: docs/oldest.md\n---\n")
        write(self.repo / "docs/oldest.md", "---\nsubject: ladder\nstatus: superseded\n"
              "superseded_by: docs/never.md\n---\n")
        self.assertIn("C3", self.codes(checks.check(self.repo, self.policy())))

    def test_missing_frontmatter_on_canonical(self):
        write(self.repo / "docs/bare.md", "# no frontmatter")
        codes = self.codes(checks.check(self.repo, self.policy()))
        self.assertIn("FM", codes)

    def test_archive_hygiene(self):
        write(self.repo / "archive/junk.md", "---\nsubject: junk\nstatus: active\n---\n")
        self.assertIn("C9", self.codes(checks.check(self.repo, self.policy())))

    def test_ephemeral_stale_and_pointer(self):
        p = self.repo / ".hermes/plans/old-plan.md"
        write(p, "# old plan, no links")
        old = time.time() - 10 * 86400
        import os
        os.utime(p, (old, old))
        findings = checks.check(self.repo, self.policy())
        self.assertIn("C7", self.codes(findings))
        msgs = [f.message for f in findings if f.code == "C7"]
        self.assertTrue(any("old" in m for m in msgs))
        self.assertTrue(any("pointer" in m for m in msgs))

    def test_links_and_issue_refs_audit(self):
        write(self.repo / "docs/a.md",
              "---\nsubject: a\nstatus: active\n---\nsee [x](missing.md) and NAT-7 and NAT-999\n")
        findings = checks.audit(self.repo, self.policy())
        codes = self.codes(findings)
        self.assertIn("C5", codes)
        msgs = [f.message for f in findings if f.code == "C6"]
        self.assertTrue(any("NAT-999" in m for m in msgs), msgs)
        self.assertFalse(any("NAT-7" in m and "not found" in m for m in msgs))

    def test_only_filter(self):
        write(self.repo / "docs/a.md", "# no fm")
        write(self.repo / "docs/b.md", "# no fm")
        findings = checks.check(self.repo, self.policy(), only={"docs/a.md"})
        paths = {f.path for f in findings}
        self.assertIn("docs/a.md", paths)
        self.assertNotIn("docs/b.md", paths)

    def test_exempt_content_never_read(self):
        # secret-class docs must be path-classified only: body stays empty
        pol_yaml = POLICY.format(issues=self.tmp / "queue.db") + (
            "  - id: exempt\n    mode: exempt\n    paths: [\"secret.md\"]\n")
        # insert exempt BEFORE docs so first-match-wins applies
        pol_yaml = pol_yaml.replace(
            "  - id: docs\n    mode: canonical\n    paths: [\"docs/**/*.md\"]\n",
            "  - id: docs\n    mode: canonical\n    paths: [\"docs/**/*.md\"]\n")
        pol_yaml = pol_yaml.replace("classes:\n",
            "classes:\n  - id: secret\n    mode: exempt\n    paths: [\"secret.md\"]\n", 1)
        write(self.repo / ".docpolicy.yaml", pol_yaml)
        write(self.repo / "secret.md", "TOPSECRET-MARKER should-not-appear")
        docs = checks.collect(self.repo, load(self.repo))
        secret = [d for d in docs if d.rel == "secret.md"][0]
        self.assertEqual(secret.body, "")
        self.assertEqual(secret.fm, {})

    def test_advisory_downgrades(self):
        write(self.repo / "docs/one.md", "---\nsubject: s\nstatus: active\n---\n")
        write(self.repo / "docs/two.md", "---\nsubject: s\nstatus: active\n---\n")
        findings = checks.check(self.repo, self.policy(), advisory=True)
        self.assertTrue(all(f.severity == "warn" for f in findings))

    def test_fm_is_error_by_default(self):
        write(self.repo / "docs/bare.md", "# no fm")
        fm_f = [f for f in checks.check(self.repo, self.policy()) if f.code == "FM"]
        self.assertTrue(fm_f)
        self.assertTrue(all(f.severity == "error" for f in fm_f))

    def test_stdin_file_colliding_subject_blocks(self):
        write(self.repo / "docs/one.md", "---\nsubject: ladder\nstatus: active\n---\n")
        pol = self.policy()
        # new doc re-using an active subject -> C2 on the virtual doc
        findings = checks.check(self.repo, pol, stdin_file="docs/two.md",
                                stdin_text="---\nsubject: ladder\nstatus: active\n---\n")
        self.assertIn("C2", self.codes(findings))
        # proper supersession write -> no error findings
        clean = checks.check(self.repo, pol, stdin_file="docs/two.md",
                             stdin_text="---\nsubject: ladder\nstatus: superseded\n"
                                        "superseded_by: docs/one.md\n---\n")
        self.assertEqual([f for f in clean if f.severity == "error"], [], str(clean))
        # new content without frontmatter -> FM error on the virtual doc
        bare = checks.check(self.repo, pol, stdin_file="docs/two.md", stdin_text="# x\n")
        self.assertTrue(any(f.code == "FM" and f.severity == "error"
                            and f.path == "docs/two.md" for f in bare))

    def test_restrict_ignores_preexisting_collision(self):
        write(self.repo / "docs/a.md", "---\nsubject: s\nstatus: active\n---\n")
        write(self.repo / "docs/b.md", "---\nsubject: s\nstatus: active\n---\n")
        write(self.repo / "docs/clean.md", "---\nsubject: other\nstatus: active\n---\n")
        per_call = checks.check(self.repo, self.policy(), only={"docs/clean.md"})
        self.assertFalse(any(f.code == "C2" for f in per_call))
        self.assertTrue(any(f.code == "C2" for f in checks.check(self.repo, self.policy())))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docpolicy-cli-"))
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        issues = self.tmp / "queue.db"
        import sqlite3
        con = sqlite3.connect(issues)
        con.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
        con.commit()
        con.close()
        write(self.repo / ".docpolicy.yaml", POLICY.format(issues=issues))
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def run_cli(self, *argv):
        r = subprocess.run(
            [__import__("sys").executable, "-m", "docpolicy", *argv],
            cwd=self.repo, capture_output=True, text=True, check=False,
            env={"PYTHONPATH": str(Path(__file__).resolve().parent.parent), "PATH": "/usr/bin:/bin"},
        )
        return r

    def test_check_blocks_and_advisory(self):
        write(self.repo / "docs/one.md", "---\nsubject: s\nstatus: active\n---\n")
        write(self.repo / "docs/two.md", "---\nsubject: s\nstatus: active\n---\n")
        self.assertEqual(self.run_cli("check", "--repo", ".").returncode, 1)
        self.assertEqual(self.run_cli("check", "--repo", ".", "--advisory").returncode, 0)

    def test_json_output(self):
        write(self.repo / "docs/one.md", "---\nsubject: s\nstatus: active\n---\n")
        write(self.repo / "docs/two.md", "---\nsubject: s\nstatus: active\n---\n")
        r = self.run_cli("check", "--repo", ".", "--json")
        data = json.loads(r.stdout)
        self.assertEqual(data["summary"]["errors"], 1)
        self.assertEqual(data["findings"][0]["code"], "C2")

    def test_multi_repo_check(self):
        repo2 = self.tmp / "repo2"
        (repo2 / ".git").mkdir(parents=True)
        write(repo2 / ".docpolicy.yaml", POLICY.format(issues=self.tmp / "queue.db"))
        write(repo2 / "docs/a.md", "---\nsubject: s\nstatus: active\n---\n")
        write(repo2 / "docs/b.md", "---\nsubject: s\nstatus: active\n---\n")
        write(self.repo / "docs/one.md", "---\nsubject: s\nstatus: active\n---\n")
        r = self.run_cli("check", "--repo", ".", str(repo2))
        self.assertEqual(r.returncode, 1)
        self.assertIn("== ", r.stdout)
        self.assertIn(str(repo2), r.stdout)
        # single-repo JSON keeps its shape; multi-repo wraps in {"repos": [...]}
        single = json.loads(self.run_cli("check", "--repo", ".", "--json").stdout)
        self.assertIn("findings", single)
        multi = json.loads(self.run_cli("check", "--repo", ".", str(repo2), "--json").stdout)
        self.assertEqual([p["repo"] for p in multi["repos"]], [str(self.repo), str(repo2)])
        self.assertEqual(multi["repos"][1]["summary"]["errors"], 1)

    def test_multi_repo_rejects_single_repo_flags(self):
        repo2 = self.tmp / "repo2"
        (repo2 / ".git").mkdir(parents=True)
        write(repo2 / ".docpolicy.yaml", POLICY.format(issues=self.tmp / "queue.db"))
        write(self.repo / "docs/one.md", "---\nsubject: s\nstatus: active\n---\n")
        r = self.run_cli("check", "--repo", ".", str(repo2), "--files", "docs/one.md")
        self.assertEqual(r.returncode, 2)

    def test_fix_dry_run_then_apply(self):
        write(self.repo / "docs/bare.md", "# bare")
        r = self.run_cli("fix", "--repo", ".")
        self.assertIn("would fix", r.stdout)
        self.assertIn("subject: bare", r.stdout)
        self.assertNotIn("subject:", (self.repo / "docs/bare.md").read_text())
        self.assertEqual(self.run_cli("fix", "--repo", ".", "--apply").returncode, 0)
        text = (self.repo / "docs/bare.md").read_text()
        self.assertIn("subject: bare", text)
        self.assertIn("status: active", text)
        self.assertIn("# bare", text)

    def test_init_installs_hook(self):
        repo2 = self.tmp / "repo2"
        repo2.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo2, check=True)
        r = self.run_cli("init", "--repo", str(repo2))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((repo2 / ".docpolicy.yaml").exists())
        self.assertTrue((repo2 / ".githooks" / "pre-commit").exists())
        hooks = subprocess.run(["git", "config", "core.hooksPath"], cwd=repo2,
                               capture_output=True, text=True, check=False).stdout.strip()
        self.assertEqual(hooks, ".githooks")


INBOX_POLICY = """\
# inbox test policy
version: 1
project: TST
sources:
  issues: {issues}
classes:
  - id: inbox
    mode: inbox
    paths: ["inbox/**"]
    dest: "concepts"
    max_age_days: 14
  - id: concepts
    mode: canonical
    paths: ["concepts/*.md"]
"""


class InboxTriageTest(unittest.TestCase):
    """Inbox captures: zero friction on write, surfaced cost on read."""

    def setUp(self):
        from docpolicy import triage  # noqa: F401  (import check)
        self.triage = __import__("docpolicy.triage", fromlist=["triage"])
        self.tmp = Path(tempfile.mkdtemp(prefix="docpolicy-inbox-"))
        self.repo = self.tmp / "repo"
        issues = self.tmp / "queue.db"
        import sqlite3
        con = sqlite3.connect(issues)
        con.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT,"
                    " title TEXT, description TEXT, status TEXT, priority INT, notes TEXT)")
        con.commit()
        con.close()
        write(self.repo / ".docpolicy.yaml", INBOX_POLICY.format(issues=issues))
        self.issues = issues
        write(self.repo / "concepts/chess_endgames.md",
              "---\nsubject: chess-endgames\nstatus: active\n"
              "title: \"Chess endgames\"\ntags: [chess, endgames, rook-endings]\n---\n\n"
              "# Chess endgames\n\nThe Lucena position wins; Philidor draws. Rook endings.\n")

    def policy(self):
        return load(self.repo)

    def test_inbox_doc_needs_no_frontmatter(self):
        write(self.repo / "inbox/rough_note.md", "# rough\n\nNo frontmatter at all.\n")
        findings = checks.check(self.repo, self.policy())
        self.assertEqual([f.code for f in findings], [], [f.message for f in findings])

    def test_inbox_age_is_surfaced(self):
        p = self.repo / "inbox/old_note.md"
        write(p, "# old\n\nDropped long ago, never triaged.\n")
        old = time.time() - 20 * 86400
        import os
        os.utime(p, (old, old))
        codes = [f.code for f in checks.check(self.repo, self.policy())]
        self.assertIn("C11", codes)

    def test_tag_collision_needs_two_shared_tags(self):
        write(self.repo / "concepts/chess_openings.md",
              "---\nsubject: chess-openings\nstatus: active\ntags: [chess, openings]\n---\n\n# Openings\n")
        write(self.repo / "concepts/chess_tactics.md",
              "---\nsubject: chess-tactics\nstatus: active\ntags: [chess, tactics]\n---\n\n# Tactics\n")
        codes = [f.code for f in checks.audit(self.repo, self.policy())]
        self.assertNotIn("C10", codes)  # one shared tag = adjacency, not collision
        write(self.repo / "concepts/chess_middlegame.md",
              "---\nsubject: chess-middlegame\nstatus: active\ntags: [chess, openings]\n---\n\n# Middle\n")
        findings = checks.audit(self.repo, self.policy())
        hits = [f for f in findings if f.code == "C10"]
        self.assertEqual(len(hits), 1, [f.message for f in hits])
        self.assertIn("openings", hits[0].message)

    def test_propose_links_related_docs(self):
        write(self.repo / "inbox/rook_endgame_notes.md",
              "# Rook endgame notes\n\nLucena and Philidor practice, rook endings in chess.\n")
        items = self.triage.scan(self.repo, self.policy())
        self.assertEqual(len(items), 1)
        prop = self.triage.propose(items[0], self.triage.corpus(self.repo, self.policy()),
                                   self.policy())
        self.assertEqual(prop.subject, "rook-endgame-notes")
        self.assertEqual(prop.item.dest, "concepts")
        self.assertTrue(any(r == "concepts/chess_endgames.md" for r, _ in prop.related))
        self.assertIn("chess", prop.tags)

    def test_promote_writes_one_frontmatter_block_and_moves(self):
        write(self.repo / "inbox/algo_idea.txt", "Algo trading: mean reversion on gold futures.\n")
        items = self.triage.scan(self.repo, self.policy())
        prop = self.triage.propose(items[0], self.triage.corpus(self.repo, self.policy()),
                                   self.policy())
        res = self.triage.promote(self.repo, prop, apply=True)
        self.assertEqual(res["to"], "concepts/algo_idea.md")
        self.assertFalse((self.repo / "inbox/algo_idea.txt").exists())
        text = (self.repo / "concepts/algo_idea.md").read_text()
        self.assertTrue(text.startswith("---\nsubject: algo-idea\nstatus: active\n"))
        self.assertEqual(text.count("\n---\n"), 1)      # ONE block: index must see it
        self.assertIn("title:", text)
        self.assertIn("tags:", text)
        self.assertIn("source: inbox/algo_idea.txt", text)
        self.assertEqual(checks.check(self.repo, self.policy()), [])

    def test_llm_layer_is_optional_and_fails_open(self):
        write(self.repo / "inbox/note.md", "# Note\n\nSome captured text about chess.\n")
        prop = self.triage.propose(self.triage.scan(self.repo, self.policy())[0],
                                   self.triage.corpus(self.repo, self.policy()), self.policy())
        self.triage.llm_refine(prop, [], {"key": ""})            # no key configured
        self.assertEqual(prop.llm, "no-key")
        self.triage.llm_refine(prop, [], {"key": "x", "model": "m",
                                          "base": "http://127.0.0.1:9", "timeout": 1.0})
        self.assertTrue(prop.llm.startswith("failed"), prop.llm)  # deterministic stands

    def test_flagged_items_need_force_and_can_be_queued(self):
        write(self.repo / "inbox/paper.pdf", "%PDF-1.4 not text\n")
        r = self.run_cli("triage", "--repo", ".", "--json", "--no-llm")
        data = json.loads(r.stdout)
        self.assertEqual(data["needs_judgement"], 1)
        self.assertEqual(data["promoted"], [])
        self.assertTrue((self.repo / "inbox/paper.pdf").exists())
        r = self.run_cli("triage", "--repo", ".", "--apply", "--queue", "--no-llm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("queued", r.stdout)
        import sqlite3
        con = sqlite3.connect(self.issues)
        rows = con.execute("SELECT project, title FROM tasks").fetchall()
        con.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "TST")
        self.assertIn("paper.pdf", rows[0][1])

    def run_cli(self, *argv):
        import os
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        return subprocess.run(["python3", "-m", "docpolicy", *argv],
                              cwd=self.repo if argv[0] != "init" else self.tmp,
                              capture_output=True, text=True, env=env, check=False)


class LlmConfigTest(unittest.TestCase):
    """Resolution order: environment > config file > defaults. No network."""

    def setUp(self):
        from docpolicy import triage
        self.triage = triage
        self.tmp = Path(tempfile.mkdtemp(prefix="docpolicy-llm-"))
        self.cfg = self.tmp / "llm.json"
        os.environ["DOCPOLICY_LLM_CONFIG"] = str(self.cfg)
        for var in ("DOCPOLICY_LLM_KEY", "DOCPOLICY_LLM_MODEL", "DOCPOLICY_LLM_BASE",
                    "DOCPOLICY_LLM_TIMEOUT", "OPENROUTER_API_KEY"):
            os.environ.pop(var, None)

    def tearDown(self):
        os.environ.pop("DOCPOLICY_LLM_CONFIG", None)
        for var in ("DOCPOLICY_LLM_KEY", "DOCPOLICY_LLM_MODEL", "DOCPOLICY_LLM_BASE",
                    "DOCPOLICY_LLM_TIMEOUT"):
            os.environ.pop(var, None)

    def test_defaults_without_key(self):
        cfg = self.triage.llm_config()
        self.assertEqual(cfg["key"], "")
        self.assertEqual(cfg["model"], self.triage.DEFAULT_LLM_MODEL)
        self.assertIn("unset", self.triage.llm_config_source())

    def test_config_file_is_used_and_private(self):
        path = self.triage.llm_config_save({"key": "sk-test", "model": "some/model"})
        self.assertEqual(path, self.cfg)
        self.assertEqual(oct(self.cfg.stat().st_mode)[-3:], "600")
        cfg = self.triage.llm_config()
        self.assertEqual(cfg["key"], "sk-test")
        self.assertEqual(cfg["model"], "some/model")
        self.assertEqual(self.triage.llm_config_source(), "config file")

    def test_environment_overrides_file(self):
        self.triage.llm_config_save({"key": "from-file", "model": "file-model"})
        os.environ["DOCPOLICY_LLM_KEY"] = "from-env"
        os.environ["DOCPOLICY_LLM_MODEL"] = "env-model"
        cfg = self.triage.llm_config()
        self.assertEqual(cfg["key"], "from-env")
        self.assertEqual(cfg["model"], "env-model")
        self.assertEqual(self.triage.llm_config_source(), "environment")

    def test_save_merges_and_ignores_empty(self):
        self.triage.llm_config_save({"key": "keep-me"})
        self.triage.llm_config_save({"model": "m2", "base": ""})
        cfg = self.triage.llm_config_load()
        self.assertEqual(cfg["key"], "keep-me")
        self.assertEqual(cfg["model"], "m2")
        self.assertNotIn("base", cfg)

    def test_broken_config_file_degrades_to_defaults(self):
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.write_text("{not json", encoding="utf-8")
        cfg = self.triage.llm_config()
        self.assertEqual(cfg["key"], "")
        self.assertEqual(cfg["model"], self.triage.DEFAULT_LLM_MODEL)


class AssertionsTest(unittest.TestCase):
    """C12: assert: frontmatter vs machine facts (generated class `facts:`)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docpolicy-assert-"))
        self.repo = self.tmp / "repo"
        (self.repo / "docs").mkdir(parents=True)
        (self.repo / "docs" / "generated").mkdir()
        write(self.repo / ".docpolicy.yaml", """\
version: 1
classes:
  - id: reality
    mode: generated
    paths: ["docs/generated/*.md"]
    generator: "python3 scripts/gen.py"
    facts: docs/generated/facts.json
  - id: docs
    mode: canonical
    paths: ["docs/*.md"]
""")

    def _facts(self, data):
        write(self.repo / "docs" / "generated" / "facts.json",
              json.dumps(data, indent=2) + "\n")

    def _spec(self, assert_block=None):
        fm_text = "---\nsubject: plan\nstatus: active\n"
        if assert_block:
            fm_text += assert_block
        write(self.repo / "docs" / "plan.md", fm_text + "---\n\n# Plan\n")

    def test_matching_assertion_is_clean(self):
        self._facts({"price": 0.0002, "endpoints": ["/v1/compress", "/health"]})
        self._spec("assert:\n  price: 0.0002\n  endpoints: [\"/v1/compress\"]\n")
        findings = [f for f in checks.audit(self.repo, load(self.repo)) if f.code == "C12"]
        self.assertEqual(findings, [])

    def test_mismatching_assertion_is_error(self):
        self._facts({"price": 0.0005})
        self._spec("assert:\n  price: 0.0002\n")
        findings = [f for f in checks.audit(self.repo, load(self.repo)) if f.code == "C12"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "error")
        self.assertIn("0.0002", findings[0].message)
        self.assertIn("0.0005", findings[0].message)

    def test_numeric_cross_type_equality(self):
        self._facts({"free_per_day": 50})
        self._spec("assert:\n  free_per_day: \"50\"\n")
        findings = [f for f in checks.audit(self.repo, load(self.repo)) if f.code == "C12"]
        self.assertEqual(findings, [])

    def test_assertion_key_not_in_facts_is_finding(self):
        self._facts({"price": 0.0002})
        self._spec("assert:\n  mystery: 1\n")
        findings = [f for f in checks.audit(self.repo, load(self.repo)) if f.code == "C12"]
        self.assertEqual(len(findings), 1)
        self.assertIn("does not extract", findings[0].message)

    def test_missing_facts_file_is_finding(self):
        self._spec("assert:\n  price: 0.0002\n")
        findings = [f for f in checks.audit(self.repo, load(self.repo)) if f.code == "C12"]
        self.assertEqual(len(findings), 1)
        self.assertIn("facts file missing", findings[0].message)

    def test_no_generated_class_means_no_c12(self):
        write(self.repo / ".docpolicy.yaml",
              "version: 1\nclasses:\n  - id: docs\n    mode: canonical\n"
              "    paths: [\"docs/*.md\"]\n")
        self._spec("assert:\n  price: 0.0002\n")
        findings = [f for f in checks.audit(self.repo, load(self.repo)) if f.code == "C12"]
        self.assertEqual(findings, [])

    def test_docs_without_assert_are_ignored(self):
        self._facts({"price": 0.0002})
        self._spec()
        findings = [f for f in checks.audit(self.repo, load(self.repo)) if f.code == "C12"]
        self.assertEqual(findings, [])


class GeneratedGlobDirectoryTest(unittest.TestCase):
    """C4 must skip directories that a `dir/**` pattern matches (regression: IsADirectoryError)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docpolicy-generated-"))
        self.repo = self.tmp / "repo"
        write(self.repo / "out" / "notes.md", "generated content\n")
        write(self.repo / "out" / "nested" / "deep.md", "nested generated content\n")
        write(self.repo / ".docpolicy.yaml", """\
version: 1
classes:
  - id: generated
    mode: generated
    paths: ["out/**"]
    generator: "true"
""")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_directory_matches_are_skipped(self):
        policy = load(self.repo)
        findings: list = []
        checks.check_generated(self.repo, policy, findings, advisory=False)
        self.assertEqual(findings, [], "unchanged files plus skipped directories must be clean")

    def test_audit_does_not_raise_on_dir_glob(self):
        policy = load(self.repo)
        findings = [f for f in checks.audit(self.repo, policy) if f.code == "C4"]
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
