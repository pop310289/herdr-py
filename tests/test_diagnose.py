"""herdr_py.diagnose: the flight recorder and its rules, against small hand-written run folders (tests/fixtures/diagnose).

Each pattern folder is an excerpt in the formats the daemon (state/events.jsonl), codex_agents.py (work/codex/events.jsonl)
and the slide teams (work/chat.jsonl) write. Each must give exactly its finding, pointing at the line that proves it;
the clean runs must give none.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from herdr_py import diagnose

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "diagnose")

# fixture -> (code, member, row, turns, text on the first line the finding points at)
EXPECTED = {
    "turn_error": ("turn_error", "art", None, [1], '"exit": 1'),
    "turn_error_opencode": ("turn_error", "drawA", None, [1], '"name": "APIError"'),
    "compacted_reply": ("compacted_reply", "drawA", None, [1], '"type": "session.compacted"'),
    "output_limit": ("output_limit", "art", None, [1], '"reason": "length"'),
    "reasoning_only": ("reasoning_only", "drawA", None, [1], '"reason": "stop"'),
    "time_limit": ("time_limit", "drawB", None, [1], '"reason": "turn time limit"'),
    "time_limit_codex": ("time_limit", "drawA", None, [1], '"exit": -9'),
    "wrong_format": ("wrong_format", "drawA", None, [1], "Reply with ONLY one ```json block"),
    "wrong_format_codex": ("wrong_format", "drawB", None, [1], "1 errors: the reply had no ```json block"),  # no prompt in Codex logs
    "no_change": ("no_change", "drawA", None, [1], "rewrite the WHOLE make_deck.py with the write tool"),
    "claimed_success": ("claimed_success", "drawB", None, [1], "All required labels added"),
    "repeated_tool_error": ("repeated_tool_error", "drawA", None, [1, 1, 1], "Could not find oldString"),
    "repeated_rejection": ("repeated_rejection", "drawA", None, [1, 1, 1], '"reply": "reject"'),
    "revisions_rejected": ("revisions_rejected", "drawB", None, [1, 3], "rejected, kept the better version"),
    "revisions_rejected_row": ("revisions_rejected", None, 1, [None, None], "rejected, kept the better version"),
}


def fixture(name):
    return os.path.join(FIXTURES, name)


def line_of(run, ref):
    path, _, number = ref.rpartition(":")
    with open(os.path.join(run, path), encoding="utf-8") as handle:
        return handle.read().splitlines()[int(number) - 1]


def copy_fixture(test, name):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp)
    run = os.path.join(tmp, name)
    shutil.copytree(fixture(name), run)
    return run


def turn_of(run, member, number):
    return next(t for t in diagnose.load(run)["turns"] if t.member == member and t.number == number)


def rewrite(run, rel, old=None, new="", drop=None):
    """Edit a copied fixture: replace text, or drop the lines that contain `drop`. Returns how many lines were dropped."""
    path = os.path.join(run, rel)
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    kept = [l.replace(old, new) if old else l for l in lines if not (drop and drop in l)]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(kept) + "\n")
    return len(lines) - len(kept)


class FindingsTest(unittest.TestCase):
    def test_each_pattern_gives_exactly_its_finding(self):
        for name, (code, member, row, turns, _) in EXPECTED.items():
            with self.subTest(name):
                found = diagnose.findings(fixture(name))
                self.assertEqual([(f["code"], f["member"], f["row"], f["turns"]) for f in found], [(code, member, row, turns)])
                self.assertTrue(found[0]["suggestion"])

    def test_clean_runs_give_none(self):
        for name in ("clean", "clean_codex"):
            with self.subTest(name):
                self.assertEqual(diagnose.findings(fixture(name)), [])

    def test_evidence_points_at_the_record_that_proves_it(self):
        for name, (_, _, _, _, proof) in EXPECTED.items():
            with self.subTest(name):
                run = fixture(name)
                evidence = diagnose.findings(run)[0]["evidence"]
                self.assertIn(proof, line_of(run, evidence[0]["refs"][0]))
                for item in evidence:
                    self.assertTrue(item["detail"])
                    for ref in item["refs"]:
                        self.assertTrue(line_of(run, ref).startswith("{"), ref)

    def test_a_claim_and_the_check_that_disproves_it_are_both_cited(self):
        evidence = diagnose.findings(fixture("claimed_success"))[0]["evidence"][0]
        self.assertEqual(evidence["refs"], ["state/events.jsonl:11", "work/chat.jsonl:5"])
        self.assertIn("9 missing", evidence["detail"])

    def test_one_outcome_per_turn_the_most_specific_first(self):
        # the compacted turn also hit the time limit and had no JSON block: those rules match it too, but only the
        # compaction (the cause) is reported, so the suggestion is the one that helps
        turn = turn_of(fixture("compacted_reply"), "drawA", 1)
        self.assertTrue(diagnose.time_limit(turn))
        self.assertTrue(diagnose.wrong_format(turn))
        self.assertEqual([f["code"] for f in diagnose.findings(fixture("compacted_reply"))], ["compacted_reply"])
        turn = turn_of(fixture("output_limit"), "art", 1)  # the program also wrote "(no DIFF lines)"
        self.assertTrue(diagnose.wrong_format(turn))
        self.assertIsNone(diagnose.reasoning_only(turn))  # stopped by the limit, not by itself

    def test_findings_are_json_ready(self):
        found = diagnose.findings(fixture("repeated_rejection"))
        self.assertEqual(json.loads(json.dumps(found))[0]["code"], "repeated_rejection")
        self.assertEqual(set(found[0]), {"code", "member", "row", "title", "count", "turns", "first", "evidence", "suggestion"})
        self.assertIn("python3 ... && python3 ...", found[0]["title"])
        self.assertIn("one command per call", found[0]["suggestion"])

    def test_known_tool_errors_get_their_own_suggestion(self):
        found = diagnose.findings(fixture("repeated_tool_error"))[0]
        self.assertTrue(found["suggestion"].startswith("Rewrite whole files with the write tool"))

    def test_the_member_with_accepted_revisions_is_named(self):
        self.assertIn("give the revisions to drawA", diagnose.findings(fixture("revisions_rejected"))[0]["suggestion"])
        self.assertIn("2 of these 2 revisions came with no art notes", diagnose.findings(fixture("revisions_rejected_row"))[0]["suggestion"])

    def test_one_below_each_threshold_is_not_a_pattern(self):
        cases = (("repeated_tool_error", "state/events.jsonl", "call_e3"),            # 2 of the same tool error
                 ("repeated_rejection", "state/events.jsonl", '"hub": "decision", "agent": "drawA", "request": "per_3"'),  # 2 rejections
                 ("revisions_rejected", "work/chat.jsonl", "0.853 -> 0.821"),         # 1 rejected revision of drawB
                 ("revisions_rejected_row", "work/chat.jsonl", "0.763 -> 0.711"))     # 1 rejected revision of row 1
        for name, rel, line in cases:
            with self.subTest(name):
                run = copy_fixture(self, name)
                self.assertEqual(rewrite(run, rel, drop=line), 1)
                self.assertEqual(diagnose.findings(run), [])

    def test_both_records_of_a_time_limit_are_cited(self):
        evidence = diagnose.findings(fixture("time_limit"))[0]["evidence"][0]
        self.assertEqual(evidence["refs"], ["state/events.jsonl:9", "work/chat.jsonl:2"])

    def test_a_compaction_summary_is_a_wrong_reply_even_when_no_format_was_asked(self):
        run = copy_fixture(self, "compacted_reply")
        rewrite(run, "state/events.jsonl", "into a JSON list of components", "into a short description")
        rewrite(run, "state/events.jsonl", "Reply with ONLY one ```json block that contains the list.", "Describe the row in a few lines.")
        self.assertEqual(rewrite(run, "work/chat.jsonl", drop="no ```json block"), 3)
        self.assertEqual(diagnose.asked_format(turn_of(run, "drawA", 1)), (None, None))
        found = diagnose.findings(run)
        self.assertEqual([f["code"] for f in found], ["compacted_reply"])
        self.assertIn("the reply after it is the compaction summary", found[0]["evidence"][0]["detail"])

    def test_a_check_written_as_the_next_turn_starts_belongs_to_the_turn_it_checks(self):
        # chat keeps 2 decimals and the daemon 3: in a real run the picture check and the next prompt had the same time
        run = copy_fixture(self, "clean")
        rewrite(run, "work/chat.jsonl", '"t": 1791400038.1,', '"t": 1791400038.6,')  # the art director's prompt time
        self.assertEqual([m["from"] for m in turn_of(run, "drawA", 1).checks], ["check", "picture"])
        self.assertEqual(turn_of(run, "art", 1).checks, [])

    def test_change_requests_in_the_example_prompts(self):
        asks = ("Edit make_deck.py, then run python3 make_deck.py and python3 -m open_slide_py validate deck.json",
                "Rewrite the WHOLE make_deck.py with the write tool (keep what is already there)",
                "How: write make_deck.py (Python standard library only) that builds the deck as Python dicts",
                "Fix the problems above, run python3 check.py yourself, update NOTES.md, and stop when the check passes.")
        others = ("Do not write code, do not use tools, do not skip lines.",
                  "Fix exactly these problems, keep the rest, and reply with ONLY one ```json block that contains the whole list.",
                  'Keep a file NOTES.md with two short sections, "Done" and "Next", and update it as you go',
                  "You are the verifier. You must not edit files.")
        for text in asks:
            self.assertTrue(diagnose.CHANGE_ASKED.search(text), text)
        for text in others:
            self.assertIsNone(diagnose.CHANGE_ASKED.search(text), text)


class ClaimsTest(unittest.TestCase):
    def turn(self, reply, *checks):
        turn = diagnose.Turn("drawA", "opencode", 1.0, "state/events.jsonl:2")
        turn.reply, turn.reply_ref, turn.end = reply, "state/events.jsonl:9", 2.0
        turn.checks = [{"t": 3.0, "from": sender, "text": text, "ok": ok, "ref": f"work/chat.jsonl:{i + 1}"}
                       for i, (sender, text, ok) in enumerate(checks)]
        return turn

    def test_a_claim_is_judged_only_by_the_checks_that_can_disprove_it(self):
        lint_ok, missing = ("lint", "OK", True), ("content", "21 missing: Prefill, Decode", False)
        self.assertIsNone(diagnose.claimed_success(self.turn("The validation passed with no errors.", lint_ok, missing)))
        self.assertTrue(diagnose.claimed_success(self.turn("All required labels added.", lint_ok, missing)))
        self.assertTrue(diagnose.claimed_success(self.turn("Done.", lint_ok, missing)))
        self.assertIsNone(diagnose.claimed_success(self.turn("Done.", lint_ok)))

    def test_negations_code_blocks_and_headings_are_not_claims(self):
        self.assertEqual(diagnose.claims_of("Not done yet: the labels are still missing."), [])
        self.assertEqual(diagnose.claims_of("```\nCompleted: base layout\n```"), [])
        self.assertEqual(diagnose.claims_of("## Work State\n### Completed\n- Row 1"), [])
        self.assertEqual(diagnose.claims_of("I placed the title.\nValidation passed."),
                         [("Validation passed.", ("check", "build", "lint"))])


class HelpersTest(unittest.TestCase):
    def test_shape_keeps_programs_and_operators(self):
        self.assertEqual(diagnose.shape("python3 make_deck.py && python3 -m open_slide_py validate deck.json"), "python3 ... && python3 ...")
        self.assertEqual(diagnose.shape("/bin/sh -c 'ls'"), "sh ...")
        self.assertEqual(diagnose.shape("ls"), "ls")
        self.assertEqual(diagnose.shape("cat a | grep b; ls"), "cat ... | grep ... ; ls")

    def test_signature_blanks_what_varies(self):
        self.assertEqual(diagnose.signature("No file 'a.py' at /work/x/a.py line 12\nmore"), "No file '_' at PATH line N")

    def test_json_detection_matches_what_the_layout_team_parses(self):
        self.assertTrue(diagnose.has_json('```json\n[{"id": "r1.a"}]\n```'))
        self.assertTrue(diagnose.has_json('Here: [{"id": "r1.a"}] done'))
        self.assertFalse(diagnose.has_json("I placed the title."))
        self.assertFalse(diagnose.has_json('```json\n[{"id": }]\n```'))


class RecorderTest(unittest.TestCase):
    def test_an_opencode_turn(self):
        r = turn_of(fixture("repeated_tool_error"), "drawA", 1).record()
        self.assertTrue(r["prompt_head"].startswith("You are drawA, a slide drawer. drawB made the current version"))
        self.assertEqual(len(r["prompt_head"]), 303)  # the first 300 characters and "..."
        self.assertEqual(r["reply"], "I rewrote make_deck.py with the write tool; the Decode box now sits 40 px further left.")
        self.assertEqual([(c["tool"], c["status"]) for c in r["tools"]], [("edit", "error")] * 3 + [("write", "completed")])
        self.assertEqual(r["tools"][0]["input"], "/work/make_deck.py (oldString 22 chars)")
        self.assertEqual(r["tools"][3]["output"], "Wrote file successfully.")
        self.assertEqual(r["tokens"], {"in": 6725 + 7290 + 7520 + 7700 + 7990, "out": 472 + 190 + 181 + 250 + 50})
        self.assertEqual((r["finish"], r["end_state"], r["duration_s"]), ("stop", "idle", 160.7))
        self.assertEqual([(c["from"], c["ok"]) for c in r["checks"]], [("build", True), ("lint", True), ("content", True)])

    def test_the_prompt_part_is_not_the_reply_and_attachments_are_listed(self):
        run = fixture("clean")
        draw = turn_of(run, "drawA", 1).record()
        self.assertTrue(draw["reply"].startswith("```json"))
        self.assertEqual(draw["reasoning_chars"], len("Two checklist lines: one text component and one tokens component."))
        self.assertEqual(turn_of(run, "art", 1).record()["files"], ["row1-original.png", "row1-ours-01.png"])

    def test_compaction_abort_and_summary(self):
        r = turn_of(fixture("compacted_reply"), "drawA", 1).record()
        self.assertEqual(len(r["compactions"]), 1)
        self.assertTrue(r["reply_is_compaction_summary"])
        self.assertTrue(r["reply"].startswith("## Objective"))
        self.assertEqual((r["end_state"], r["aborts"][0]["reason"], r["errors"]), ("aborted", "turn time limit", []))
        self.assertEqual(r["checks"][0]["text"], "1 errors: the reply had no ```json block with a JSON list")

    def test_a_codex_turn(self):
        run = fixture("clean_codex")
        r = turn_of(run, "drawA", 1).record()
        self.assertEqual((r["backend"], r["session"], r["finish"], r["end_state"]), ("codex", "thread-drawA-1", "completed", "idle"))
        self.assertEqual((r["tokens"], r["reasoning_tokens"]), ({"in": 20537, "out": 1117}, 233))
        self.assertEqual((r["prompt_head"], r["prompt_source"]), ("round 1: row 1 draft from the checklist", "chat (manager)"))
        self.assertTrue(r["reply"].startswith('```json\n[{"id": "r1.longPrompt"'))
        self.assertEqual(turn_of(run, "art", 1).record()["files"], ["row1-original.png", "row1-ours-01.png"])
        self.assertEqual(turn_of(run, "drawB", 1).record()["duration_s"], 117.4)

    def test_a_subagent_works_for_its_member_but_does_not_answer_for_it(self):
        run = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, run)
        os.makedirs(os.path.join(run, "state"))
        part = lambda sid, t, **p: {"t": t, "opencode": {"type": "message.part.updated", "properties": {"sessionID": sid, "part": dict(p, sessionID=sid)}}}
        rows = [{"t": 10.0, "hub": "start", "agent": "drawA", "session": "ses_root"},
                {"t": 10.0, "hub": "prompt", "agent": "drawA", "source": "start", "text": "Edit make_deck.py, then run python3 make_deck.py."},
                {"t": 10.1, "hub": "state", "agent": "drawA", "state": "working", "reason": "busy"},
                {"t": 11.0, "opencode": {"type": "session.created", "properties": {"sessionID": "ses_child", "info": {"id": "ses_child", "parentID": "ses_root"}}}},
                part("ses_child", 12.0, id="prt_c1", messageID="msg_c1", type="tool", tool="edit", callID="call_c1",
                     state={"status": "completed", "input": {"filePath": "/work/make_deck.py"}, "output": "Edit applied successfully."}),
                part("ses_child", 13.0, id="prt_c2", messageID="msg_c1", type="text", text="Subagent: edited make_deck.py."),
                {"t": 14.0, "hub": "state", "agent": "drawA", "state": "idle", "reason": "idle"}]
        with open(os.path.join(run, "state", "events.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(r) + "\n" for r in rows))
        r = turn_of(run, "drawA", 1).record()
        self.assertEqual(r["reply"], "")
        self.assertEqual([(c["tool"], c["status"], c["child"]) for c in r["tools"]], [("edit", "completed", True)])
        self.assertEqual(diagnose.findings(run), [])  # the subagent's edit is a change

    def test_a_failed_codex_turn(self):
        r = turn_of(fixture("turn_error"), "art", 1).record()
        self.assertEqual((r["end_state"], r["errors"][0]["name"], r["errors"][0]["message"]), ("error", "exit 1", "No prompt provided via stdin."))

    def test_members_table(self):
        rows = {m["member"]: m for m in diagnose.diagnose(fixture("revisions_rejected"))["members"]}
        self.assertEqual((rows["drawA"]["accepted"], rows["drawA"]["revisions"], rows["drawB"]["accepted"], rows["drawB"]["revisions"]), (2, 2, 0, 2))
        self.assertEqual((rows["drawB"]["turns"], rows["drawB"]["valid"], rows["drawB"]["judged"]), (3, 3, 3))


class DamagedLogsTest(unittest.TestCase):
    def test_a_half_written_line_is_skipped_and_said(self):
        run = copy_fixture(self, "wrong_format")
        with open(os.path.join(run, "state", "events.jsonl"), "a", encoding="utf-8") as handle:
            handle.write('{"t": 1791402042.0, "hub": "pro')
        result = diagnose.diagnose(run)
        self.assertEqual([f["code"] for f in result["findings"]], ["wrong_format"])
        self.assertIn("state/events.jsonl: 1 unreadable line(s) skipped", result["notes"])

    def test_an_unfinished_turn_is_noted_not_judged(self):
        run = copy_fixture(self, "wrong_format")
        path = os.path.join(run, "state", "events.jsonl")
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines[:-2]) + "\n")  # the log ends before the turn did
        result = diagnose.diagnose(run)
        self.assertEqual(result["findings"], [])
        self.assertIn("drawA turn 1 has no end in the log (still running, or the log was cut)", result["notes"])

    def test_a_restarted_daemon_has_no_start_record_and_state_json_names_the_session(self):
        run = copy_fixture(self, "output_limit")
        path = os.path.join(run, "state", "events.jsonl")
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines[1:]) + "\n")
        blind = diagnose.diagnose(run)
        self.assertNotIn("output_limit", [f["code"] for f in blind["findings"]])
        self.assertTrue(any("left out" in note for note in blind["notes"]), blind["notes"])
        with open(os.path.join(run, "state", "state.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "agents": [{"name": "art", "session_id": "ses_art1", "past_sessions": []}]}, handle)
        self.assertEqual([f["code"] for f in diagnose.findings(run)], ["output_limit"])


class CommandLineTest(unittest.TestCase):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = diagnose.main(list(args), out=out)
        return code, out.getvalue()

    def test_table_and_findings(self):
        code, text = self.run_main(fixture("revisions_rejected"))
        self.assertEqual(code, 0)
        cells = {line.split()[0]: line.split() for line in text.splitlines() if line.startswith(("member ", "drawA ", "drawB "))}
        self.assertEqual(cells["member"], ["member", "backend", "turns", "tokens", "in/out", "valid", "revisions", "accepted"])
        self.assertEqual(cells["drawB"], ["drawB", "codex", "3", "176,812/24,872", "3/3", "0/2"])
        self.assertEqual(cells["drawA"], ["drawA", "codex", "3", "142,629/16,833", "3/3", "2/2"])
        self.assertIn("[1] revisions_rejected  drawB: all 2 of drawB's revisions rejected (no progress)", text)
        self.assertIn("work/chat.jsonl:9", text)
        self.assertIn("    do: Its revisions never beat the kept version", text)

    def test_a_clean_run_says_so(self):
        code, text = self.run_main(fixture("clean"))
        self.assertEqual(code, 0)
        self.assertIn("0 findings (nothing matched the rules)", text)

    def test_json(self):
        code, text = self.run_main(fixture("no_change"), "--json")
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertEqual(data["findings"], json.loads(json.dumps(diagnose.findings(fixture("no_change")))))
        self.assertEqual(data["members"][0]["member"], "drawA")

    def test_one_turn_in_full(self):
        code, text = self.run_main(fixture("repeated_tool_error"), "--member", "drawA", "--turn", "1")
        self.assertEqual(code, 0)
        for part in ("drawA turn 1 (opencode, session ses_drawA1)", "reply (87 chars):", "tools (4):", "1. edit error",
                     "4. write completed", "     -> Wrote file successfully.", "checks:", "build: make_deck.py ran; deck.json rebuilt",
                     "repeated_tool_error: the same edit error 3 times"):
            self.assertIn(part, text)
        code, text = self.run_main(fixture("repeated_tool_error"), "--member", "drawA", "--turn", "1", "--json")
        self.assertEqual((code, json.loads(text)["tools"][3]["tool"], json.loads(text)["findings"][0]["code"]), (0, "write", "repeated_tool_error"))

    def test_one_member(self):
        code, text = self.run_main(fixture("revisions_rejected"), "--member", "drawB")
        self.assertEqual(code, 0)
        self.assertIn("drawB: 3 turns", text)
        self.assertIn("finding revisions_rejected", text)

    def test_output_survives_a_terminal_that_cannot_show_the_reply(self):
        # replies hold characters like "≈" and curly quotes; Python 3.6 under LANG=C can only write ASCII to stdout
        run = copy_fixture(self, "wrong_format")
        rewrite(run, "state/events.jsonl", "in the middle of the row", "at x≈640, “middle”")
        env = dict(os.environ, LANG="C", LC_ALL="C")
        env.pop("PYTHONIOENCODING", None)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run([sys.executable, "-m", "herdr_py.diagnose", run, "--member", "drawA", "--turn", "1"], cwd=root, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"wrong_format: reply without the asked format", p.stdout)

    def test_mistakes_are_reported(self):
        with self.assertRaises(SystemExit):
            self.run_main(fixture("clean"), "--turn", "1")
        self.assertEqual(self.run_main(fixture("clean"), "--member", "nobody")[0], 1)
        self.assertEqual(self.run_main(fixture("clean"), "--member", "drawA", "--turn", "9")[0], 1)
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        code, text = self.run_main(empty)
        self.assertEqual(code, 1)
        self.assertIn("no logs in", text)


if __name__ == "__main__":
    unittest.main()
