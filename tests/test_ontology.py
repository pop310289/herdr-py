"""Skill ontology (ontology.py): writes go only through actions, in one transaction with an action log that replays to
the current state; a rejected submission writes nothing but itself; declarations made against an older skill version
are refused and a changed skill marks its operations and chains for a recheck; check_chain refuses every broken chain
(wrong order, missing producer, missing condition, a URL carried across a handoff, a report about the wrong start, a
last step that is not a check, unknown operations and parameters) and passes the right ones; find_chains returns only
shortest chains, hands off as late as possible and ranks steps that need a person last; compile_script binds paths
between steps and keeps parameters from becoming options; the example chain runs for real (Python only).
No model is called."""
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from herdr_py import ontology as o  # noqa: E402

PROBED = ["git", "loopback", "python3"]          # what the workstation has in these tests, whatever the machine has
BUILD, LINT, SERVE, PROBE = "notes2site.build", "site-lint.check", "builtin.serve-local", "url-probe.check"
HANDOFF, PUBLISH = "handoff.handoff", "site-publish.publish"
GOOD = [  # chains that must pass
    ("notes-site-workstation", [BUILD, LINT, SERVE, PROBE]),
    ("notes-site-builder", [BUILD, LINT, HANDOFF, SERVE, PROBE]),
    ("notes-site-workstation", [BUILD, LINT, {"op": PUBLISH, "params": {"target": "notes"}}, PROBE]),
]


def steps_of(ops):
    return [s if isinstance(s, dict) else {"op": s} for s in ops]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lib = os.path.join(self.tmp, "skills")
        shutil.copytree(o.SKILL_LIB, self.lib, ignore=shutil.ignore_patterns("__pycache__"))
        self.conn = o.open_db(":memory:")
        self.addCleanup(self.conn.close)
        self.assertEqual(o.build(self.conn, lib=self.lib, probed=PROBED), [])
        self.cat = o.Catalog(o.View(self.conn))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, task, ops):
        t = self.cat.tasks[task]
        return o.check_chain(self.cat, steps_of(ops), t["start"], t["goal"], t["env"])

    def count(self, table):
        return self.conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0]

    def with_action(self, rule):
        o.ACTIONS["testOnly"] = (1, ("owner",), lambda v, p, a: [], rule)
        self.addCleanup(o.ACTIONS.pop, "testOnly")

    def task_with_two_sites(self):
        """A task whose start already has a site, so the chain holds two sites."""
        r = o.act(self.conn, "registerTask", {"id": "two-sites", "start": ["notes-md", "site"], "env": "workstation",
                                              "goal": [{"type": "lint-report", "of": "notes-md"},
                                                       {"type": "probe-report", "of": "notes-md"}],
                                              "description": "a site exists already"}, "owner")
        self.assertTrue(r["ok"], r)
        self.cat = o.Catalog(o.View(self.conn))


class Engine(Base):
    def test_an_action_commits_the_object_and_its_log_entry_together(self):
        logs = self.count("action_log")
        r = o.act(self.conn, "registerCondition", {"name": "docker", "description": "a docker daemon"}, "owner")
        self.assertTrue(r["ok"])
        self.assertEqual(self.count("action_log"), logs + 1)
        self.assertEqual(o.View(self.conn).get("Condition", "docker"), {"description": "a docker daemon"})
        touched = self.conn.execute("SELECT type, pk FROM log_touches WHERE log_id=?", (r["log_id"],)).fetchall()
        self.assertEqual(touched, [("Condition", "docker")])

    def test_a_rejected_submission_writes_only_the_rejection(self):
        objs, logs, rej = self.count("objects"), self.count("action_log"), self.count("rejected")
        r = o.act(self.conn, "registerCondition", {"name": "Bad Name", "description": "x"}, "owner")
        self.assertFalse(r["ok"])
        self.assertEqual((self.count("objects"), self.count("action_log"), self.count("rejected")), (objs, logs, rej + 1))

    def test_a_bad_edit_rolls_back_every_edit_of_the_action(self):
        self.with_action(lambda v, p, a: [{"op": "put", "type": "Condition", "pk": "x", "props": {"description": "ok"}},
                                          {"op": "put", "type": "Condition", "pk": "y", "props": {}}])
        r = o.act(self.conn, "testOnly", {}, "owner")
        self.assertFalse(r["ok"])
        self.assertIn("edits do not fit the types", r["reasons"][0])
        self.assertIsNone(o.View(self.conn).get("Condition", "x"))

    def test_an_undefined_property_is_rejected(self):
        self.with_action(lambda v, p, a: [{"op": "put", "type": "Condition", "pk": "x",
                                           "props": {"description": "ok", "color": "red"}}])
        r = o.act(self.conn, "testOnly", {}, "owner")
        self.assertIn("undefined properties ['color']", r["reasons"][0])

    def test_an_unexpected_error_rolls_back_and_is_raised(self):
        logs, rej = self.count("action_log"), self.count("rejected")
        self.with_action(lambda v, p, a: [{"op": "put", "type": "Condition", "pk": "x", "props": {"description": "ok"}},
                                          {"op": "put", "type": "Condition", "pk": "y", "props": {"description": {1, 2}}}])
        with self.assertRaises(TypeError):
            o.act(self.conn, "testOnly", {}, "owner")
        self.assertIsNone(o.View(self.conn).get("Condition", "x"))
        self.assertEqual((self.count("action_log"), self.count("rejected")), (logs, rej))

    def test_a_foreign_key_must_point_to_an_existing_object(self):
        self.with_action(lambda v, p, a: [{"op": "put", "type": "Chain", "pk": "c",
                                           "props": {"task": "nope", "steps": [], "status": "accepted"}}])
        r = o.act(self.conn, "testOnly", {}, "owner")
        self.assertFalse(r["ok"])
        self.assertIn("points to a Task that does not exist (nope)", r["reasons"][0])

    def test_nothing_changed_is_not_logged_and_building_again_adds_nothing(self):
        logs = self.count("action_log")
        self.assertEqual(o.build(self.conn, lib=self.lib, probed=PROBED), [])
        self.assertEqual(self.count("action_log"), logs)
        r = o.act(self.conn, "registerCondition", {"name": "git", "description": "git, to hand files to another machine"}, "owner")
        self.assertEqual((r["ok"], r["log_id"]), (True, None))

    def test_verify_log_finds_writes_that_skip_the_actions(self):
        self.assertEqual(o.verify_log(self.conn), [])
        before = self.conn.execute("SELECT props FROM objects WHERE type='Condition' AND pk='git'").fetchone()[0]
        self.conn.execute("UPDATE objects SET props=? WHERE type='Condition' AND pk='git'", ('{"description": "x"}',))
        self.assertEqual(len(o.verify_log(self.conn)), 1)
        self.conn.execute("UPDATE objects SET props=? WHERE type='Condition' AND pk='git'", (before,))
        self.assertEqual(o.verify_log(self.conn), [])
        self.conn.execute("INSERT INTO links VALUES('provides', 'builder', 'loopback')")
        self.assertEqual(len(o.verify_log(self.conn)), 1)

    def test_agents_cannot_record_runs_or_register_operations(self):
        r = o.act(self.conn, "recordChainRun", {"chain": "x", "status": "succeeded", "results": []}, "agent:planner")
        self.assertIn("can only be done by", r["reasons"][0])
        op = o.read_json(o.CATALOG)["operations"][0]
        self.assertFalse(o.act(self.conn, "registerOperation", op, "agent:planner")["ok"])


class CatalogMatchesSkills(Base):
    def edit_skill(self, lib, skill):
        with open(os.path.join(lib, skill, "SKILL.md"), "a", encoding="utf-8") as f:
            f.write("\nA new line.\n")

    def test_the_example_catalog_matches_the_example_skills(self):
        """Every declaration matches its skill as shipped. Edit a skill, check its declarations, then run pin."""
        conn = o.open_db(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(o.build(conn, probed=PROBED), [])

    def test_a_declaration_checked_against_an_older_skill_is_refused(self):
        lib = os.path.join(self.tmp, "changed")
        shutil.copytree(o.SKILL_LIB, lib, ignore=shutil.ignore_patterns("__pycache__"))
        self.edit_skill(lib, "site-lint")
        conn = o.open_db(":memory:")
        self.addCleanup(conn.close)
        rejected = o.build(conn, lib=lib, probed=PROBED)
        self.assertEqual([r[1] for r in rejected], [LINT])
        self.assertIn("check the declaration again", rejected[0][2][0])

    def test_a_changed_skill_marks_its_operations_and_chains_for_a_recheck(self):
        steps = steps_of(GOOD[0][1])
        self.assertTrue(o.act(self.conn, "proposeChain", {"task": "notes-site-workstation", "steps": steps}, "agent:p")["ok"])
        self.edit_skill(self.lib, "site-lint")
        new = o.skill_md5(os.path.join(self.lib, "site-lint"))
        r = o.act(self.conn, "syncSkill", {"name": "site-lint", "md5": new, "dir": os.path.join(self.lib, "site-lint")},
                  "program:sync")
        self.assertTrue(r["ok"])
        v = o.View(self.conn)
        self.assertEqual(v.get("Operation", LINT)["status"], "stale")
        self.assertEqual(v.get("Operation", BUILD)["status"], "current")
        self.assertEqual([c["status"] for c in v.all("Chain").values()], ["needs-recheck"])
        cat = o.Catalog(v)
        t = cat.tasks["notes-site-workstation"]
        self.assertEqual(o.find_chains(cat, t["start"], t["goal"], t["env"]), [])
        self.assertIn("needs a recheck", " ".join(o.check_chain(cat, steps, t["start"], t["goal"], t["env"])))

    def test_a_wrong_skill_md5_is_refused(self):
        r = o.act(self.conn, "syncSkill", {"name": "site-lint", "md5": "a" * 32,
                                           "dir": os.path.join(self.lib, "site-lint")}, "program:sync")
        self.assertFalse(r["ok"])

    def test_compiled_python_files_do_not_change_a_skill_md5(self):
        skill = os.path.join(self.lib, "site-lint")
        before = o.skill_md5(skill)
        with open(os.path.join(skill, "lint.cpython-36.pyc"), "wb") as f:
            f.write(b"bytecode")
        os.makedirs(os.path.join(skill, "__pycache__"))
        with open(os.path.join(skill, "__pycache__", "lint.pyc"), "wb") as f:
            f.write(b"bytecode")
        self.assertEqual(o.skill_md5(skill), before)

    def test_a_probed_environment_gets_only_the_conditions_this_machine_has(self):
        conn = o.open_db(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(o.build(conn, lib=self.lib, probed=["python3"]), [])
        cat = o.Catalog(o.View(conn))
        self.assertEqual(cat.envs["workstation"], {"python3"})
        self.assertEqual(cat.envs["builder"], {"git", "python3"})        # declared, not probed

    def test_the_md5_comes_from_a_manifest_when_the_library_has_one(self):
        with open(os.path.join(self.lib, "MANIFEST.json"), "w") as f:
            json.dump({"skills": {"site-lint": {"md5": "b" * 32}}}, f)
        self.assertEqual(o.skill_md5(os.path.join(self.lib, "site-lint")), "b" * 32)
        self.assertIsNone(o.skill_md5(os.path.join(self.lib, "notes2site")))


class CheckChain(Base):
    def test_chains_that_should_pass_pass(self):
        for task, ops in GOOD:
            self.assertEqual(self.check(task, ops), [], (task, ops))
        self.task_with_two_sites()
        self.assertEqual(self.check("two-sites", [BUILD, SERVE, PROBE, LINT]), [])     # the site made last is served

    def assertCaught(self, task, ops, fragment):
        bad = self.check(task, ops)
        self.assertTrue(any(fragment in b for b in bad), (fragment, bad))

    def test_wrong_order(self):
        self.assertCaught("notes-site-workstation", [LINT, BUILD, SERVE, PROBE], "%s needs static-site" % LINT)

    def test_missing_producer(self):
        self.assertCaught("notes-site-workstation", [SERVE, PROBE], "%s needs static-site" % SERVE)

    def test_an_environment_without_the_condition(self):
        self.assertCaught("notes-site-builder", [BUILD, LINT, SERVE, PROBE], "%s needs loopback, which builder does not have" % SERVE)

    def test_a_url_does_not_survive_the_handoff(self):
        steps = [BUILD, LINT, {"op": PUBLISH, "params": {"target": "notes"}}, HANDOFF, PROBE]
        self.assertCaught("notes-site-builder", steps, "%s needs web-url" % PROBE)

    def test_a_report_about_the_wrong_start(self):
        self.task_with_two_sites()
        self.assertCaught("two-sites", [SERVE, PROBE, BUILD, LINT], "no goal probe-report (from notes-md) is made")

    def test_the_last_step_must_be_a_check(self):
        self.assertCaught("notes-site-workstation", [BUILD, LINT, SERVE, PROBE, BUILD], "the last step is not a check")

    def test_unknown_operation_and_parameter(self):
        self.assertCaught("notes-site-workstation", ["notes2site.export-pdf"] + GOOD[0][1], "no operation notes2site.export-pdf")
        self.assertCaught("notes-site-workstation", [{"op": BUILD, "params": {"x": "1"}}] + GOOD[0][1][1:], "has no parameter x")

    def test_a_proposal_needs_every_parameter(self):
        steps = steps_of([BUILD, LINT, PUBLISH, PROBE])
        r = o.act(self.conn, "proposeChain", {"task": "notes-site-workstation", "steps": steps}, "agent:p")
        self.assertIn("is missing parameter target", " ".join(r["reasons"]))
        steps[2]["params"] = {"target": "notes"}
        self.assertTrue(o.act(self.conn, "proposeChain", {"task": "notes-site-workstation", "steps": steps}, "agent:p")["ok"])


class FindChains(Base):
    def plans(self, task):
        t = self.cat.tasks[task]
        return o.find_chains(self.cat, t["start"], t["goal"], t["env"])

    def test_the_builder_does_what_it_can_then_hands_off(self):
        self.assertEqual(self.plans("notes-site-builder")[0], [BUILD, LINT, HANDOFF, SERVE, PROBE])

    def test_steps_that_need_no_person_come_first(self):
        plans = self.plans("notes-site-workstation")
        self.assertEqual(plans[0], [BUILD, LINT, SERVE, PROBE])
        self.assertEqual(plans[1], [BUILD, LINT, PUBLISH, PROBE])

    def test_every_chain_found_passes_and_no_step_can_be_dropped(self):
        for task in self.cat.tasks:
            plans = self.plans(task)
            self.assertTrue(plans, task)
            for p in plans:
                self.assertEqual(self.check(task, p), [], (task, p))
                for i in range(len(p)):
                    self.assertTrue(self.check(task, p[:i] + p[i + 1:]), (task, p, i))

    def test_a_step_that_needs_a_person_ranks_last_even_when_its_name_sorts_first(self):
        r = o.act(self.conn, "registerOperation", {"id": "builtin.a-manual-serve", "skill": "builtin", "argv": [],
                                                   "inputs": ["static-site"], "outputs": ["local-url"], "requires": ["python3"],
                                                   "check": False, "success": [0], "manual": "a person serves it", "doc": "test"}, "owner")
        self.assertTrue(r["ok"], r)
        self.cat = o.Catalog(o.View(self.conn))
        plans = self.plans("notes-site-workstation")
        self.assertEqual(plans[0], [BUILD, LINT, SERVE, PROBE])      # the manual way to the same state is dominated
        self.assertNotIn([BUILD, LINT, "builtin.a-manual-serve", PROBE], plans)

    def test_chains_with_steps_that_need_a_person_rank_after_those_without(self):
        r = o.act(self.conn, "registerOperation", {"id": "builtin.a-manual-publish", "skill": "builtin", "argv": [],
                                                   "inputs": ["static-site"], "outputs": ["public-url"], "requires": ["python3"],
                                                   "check": False, "success": [0], "manual": "a person publishes it", "doc": "test"}, "owner")
        self.assertTrue(r["ok"], r)
        self.cat = o.Catalog(o.View(self.conn))
        plans = self.plans("notes-site-workstation")
        self.assertEqual(plans[0], [BUILD, LINT, SERVE, PROBE])      # sorts after builtin.a-manual-publish by name
        self.assertEqual(plans[1], [BUILD, LINT, "builtin.a-manual-publish", PROBE])

    def test_an_impossible_goal_finds_nothing(self):
        self.assertEqual(o.find_chains(self.cat, [], [{"type": "lint-report", "of": "notes-md"}], "workstation"), [])


class Compile(Base):
    def compile(self, task, steps, start=None):
        if start is None:
            start = {"notes-md": os.path.join(self.tmp, "notes.md")}
        return o.compile_script(self.cat, task, steps_of(steps), start, os.path.join(self.tmp, "work"))

    def test_paths_flow_between_steps_and_no_shell_is_used(self):
        s = self.compile("notes-site-workstation", GOOD[0][1])
        self.assertTrue(all(isinstance(a, str) for step in s for a in step["argv"]))
        site = os.path.join(self.tmp, "work", "01-site")
        self.assertEqual(s[0]["argv"][-1], site)
        self.assertEqual(s[1]["argv"][-1], site)
        self.assertEqual(s[2]["cwd"], site)
        self.assertEqual(s[3]["argv"][-1], "http://127.0.0.1:%d/" % s[2]["port"])
        self.assertEqual(s[0]["argv"][0], sys.executable)

    def test_a_parameter_cannot_become_an_option_or_a_second_line(self):
        for bad in ("-rf", "a\nb"):
            with self.assertRaises(o.CompileError):
                self.compile("notes-site-workstation", [BUILD, LINT, {"op": PUBLISH, "params": {"target": bad}}, PROBE])

    def test_a_chain_that_fails_the_check_is_not_compiled(self):
        with self.assertRaises(o.CompileError) as cm:
            self.compile("notes-site-workstation", [LINT, BUILD, SERVE, PROBE])
        self.assertIn("does not pass the check", str(cm.exception))

    def test_the_start_must_match_the_task(self):
        with self.assertRaises(o.CompileError) as cm:
            self.compile("notes-site-workstation", GOOD[0][1], start={"site": self.tmp})
        self.assertIn("give paths for the start", str(cm.exception))

    def test_a_list_parameter_becomes_separate_arguments(self):
        r = o.act(self.conn, "registerOperation", {"id": "builtin.echo", "skill": "builtin", "argv": ["{python}", "-c", "pass", "{param:words}"],
                                                   "params": ["words"], "inputs": ["lint-report"], "outputs": [], "requires": ["python3"],
                                                   "check": True, "success": [0], "doc": "test"}, "owner")
        self.assertTrue(r["ok"], r)
        self.cat = o.Catalog(o.View(self.conn))
        s = self.compile("notes-site-workstation", [BUILD, LINT, SERVE, PROBE, {"op": "builtin.echo", "params": {"words": ["a", "b"]}}])
        self.assertEqual(s[4]["argv"][-2:], ["a", "b"])


class RunScript(Base):
    def step(self, code, **kw):
        s = {"op": "x", "argv": [sys.executable, "-c", "raise SystemExit(%d)" % code], "cwd": self.tmp, "mkdirs": [],
             "stdout_to": None, "service": False, "port": None, "manual": None, "success": [0], "check": True, "value": None}
        s.update(kw)
        return s

    def test_the_first_failure_stops_the_chain(self):
        status, res = o.run_script([self.step(0), self.step(3), self.step(0)])
        self.assertEqual((status, [r["exit"] for r in res]), ("failed", [0, 3]))

    def test_a_step_that_needs_a_person_stops_the_chain(self):
        status, res = o.run_script([self.step(0), self.step(0, manual="the owner does it")])
        self.assertEqual((status, len(res)), ("stopped-at-manual", 2))

    def test_a_service_runs_during_the_chain_and_stops_after(self):
        port = o._free_port()
        svc = self.step(0, argv=[sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                        service=True, port=port, value="http://127.0.0.1:%d/" % port)
        status, res = o.run_script([svc, self.step(0)])
        self.assertEqual(status, "succeeded")
        self.assertIn("service started", res[0]["note"])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()

    def test_a_success_with_a_failing_exit_code_is_refused(self):
        steps = [{"op": s, "params": {}} for s in GOOD[0][1]]
        self.assertTrue(o.act(self.conn, "proposeChain", {"task": "notes-site-workstation", "steps": steps}, "agent:p")["ok"])
        cid = o.chain_id("notes-site-workstation", steps)
        res = [{"op": s["op"], "exit": 0} for s in steps]
        res[1]["exit"] = 1
        r = o.act(self.conn, "recordChainRun", {"chain": cid, "status": "succeeded", "results": res}, "program:run")
        self.assertIn("exit code 1 of %s is not a success code" % LINT, r["reasons"])
        res[1]["exit"] = 0
        self.assertTrue(o.act(self.conn, "recordChainRun", {"chain": cid, "status": "succeeded", "results": res}, "program:run")["ok"])

    def test_the_example_chain_runs_for_real(self):
        out = o.run_chain(self.conn, "notes-site-workstation", steps_of(GOOD[0][1]),
                          {"notes-md": os.path.join(os.path.dirname(o.CATALOG), "notes.md")}, os.path.join(self.tmp, "run"),
                          actor="agent:demo")
        self.assertEqual(out["status"], "succeeded", out["results"])
        self.assertEqual([r["exit"] for r in out["results"]], [0, 0, None, 0])
        self.assertTrue(out["recorded"]["ok"])
        with open(os.path.join(self.tmp, "run", "04-probe-report.out")) as f:
            self.assertEqual(json.loads(f.read())["status"], 200)
        self.assertEqual(o.verify_log(self.conn), [])


if __name__ == "__main__":
    unittest.main()
