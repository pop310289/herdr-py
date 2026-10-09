"""The event-driven team (engine.py): the planner is woken by events and keeps a shared todo list, members take todos
as soon as they are free (never two on one), every answer is judged by a program, the board file shows the latest
state while members work, and the run stops for the stated reasons. The planner and the members are programs here,
so no model is called."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from herdr_py import engine, engineview  # noqa: E402
from herdr_py.teamkb import TeamKB, TeamKBError  # noqa: E402

# The planner: its n-th reply is ENGINE_TEST_PLANNER[n] (the last one repeats); "$best" becomes the id of the best
# verified result in the prompt, "$open" the ids of the open todos. Every prompt is kept in ENGINE_TEST_LOG.
PLANNER = r'''
import json, os, re, sys
prompt = sys.stdin.read()
log = os.environ["ENGINE_TEST_LOG"]
os.makedirs(log, exist_ok=True)
n = len([f for f in os.listdir(log) if f.startswith("planner-")])
open(os.path.join(log, "planner-%02d.txt" % (n + 1)), "w").write(prompt)
replies = json.load(open(os.environ["ENGINE_TEST_PLANNER"]))
reply = json.dumps(replies[min(n, len(replies) - 1)])
best = re.search(r"^- (k[0-9a-f]{12}) by ", prompt, re.M)
reply = reply.replace("$best", best.group(1) if best else "none")
reply = reply.replace('"$open"', ", ".join('"%s"' % t for t in re.findall(r"^- (t[0-9a-f]{12}) \[open\]", prompt, re.M)))
reply = reply.replace('"$taken"', ", ".join('"%s"' % t for t in re.findall(r"^- (t[0-9a-f]{12}) \[taken by", prompt, re.M)))
print("Planning.\n```json\n" + reply + "\n```")
'''

# A member: what it does on its k-th turn is ENGINE_TEST_MEMBERS[name][k] (the last one repeats): sleep, read the
# board (and keep what it saw), answer a number, report a failure, crash, or say nothing usable.
MEMBER = r'''
import json, os, re, sys, time
prompt = sys.stdin.read()
name = os.environ["HERDR_MEMBER"]
log = os.environ["ENGINE_TEST_LOG"]
os.makedirs(log, exist_ok=True)
k = len([f for f in os.listdir(log) if f.startswith(name + "-") and f.endswith(".prompt")])
open(os.path.join(log, "%s-%02d.prompt" % (name, k + 1)), "w").write(prompt)
open(os.path.join(log, "%s-%02d.where" % (name, k + 1)), "w").write(os.getcwd() + "\n" + os.environ.get("HERDR_ACCESS", ""))
acts = json.load(open(os.environ["ENGINE_TEST_MEMBERS"])).get(name, [{"answer": "1"}])
act = acts[min(k, len(acts) - 1)]
if act.get("board_first"):
    open(os.path.join(log, "%s-%02d.board1" % (name, k + 1)), "w").write(open("TEAM_BOARD.md").read())
until = act.get("until_file")
deadline = time.time() + 10
while until and not os.path.exists(until) and time.time() < deadline:
    time.sleep(0.05)
time.sleep(act.get("sleep", 0))
if act.get("board_after"):
    open(os.path.join(log, "%s-%02d.board2" % (name, k + 1)), "w").write(open("TEAM_BOARD.md").read())
if act.get("crash"):
    sys.exit(3)
if "fail" in act:
    print("FAILED: " + act["fail"])
elif "garbage" in act:
    print("I am not sure what to do.")
else:
    parents = act.get("parents", "none")
    print("SUMMARY: answered %s\nPARENTS: %s\n```\n%s\n```" % (act["answer"], parents, act["answer"]))
'''

# The judge: an answer that is a number is valid with that score; anything else is invalid.
JUDGE = r'''
import json, sys
text = open(sys.argv[-1]).read().strip()
try:
    score = float(text)
    print(json.dumps({"status": "valid", "score": score, "detail": "a number"}))
except ValueError:
    print(json.dumps({"status": "invalid", "score": 0, "detail": "not a number: %r" % text[:40]}))
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, body in (("planner.py", PLANNER), ("member.py", MEMBER), ("judge.py", JUDGE)):
            with open(os.path.join(self.dir, name), "w") as handle:
                handle.write(textwrap.dedent(body))
        with open(os.path.join(self.dir, "task.md"), "w") as handle:
            handle.write("Give a number as high as you can.\n")
        self.log = os.path.join(self.dir, "log")
        saved = {k: os.environ.get(k) for k in ("ENGINE_TEST_LOG", "ENGINE_TEST_PLANNER", "ENGINE_TEST_MEMBERS")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ.update(ENGINE_TEST_LOG=self.log, ENGINE_TEST_PLANNER=os.path.join(self.dir, "planner.json"),
                          ENGINE_TEST_MEMBERS=os.path.join(self.dir, "members.json"))
        self.out = os.path.join(self.dir, "run")
        cwd = os.getcwd()
        os.chdir(self.dir)  # the command members' files are named from here, as a person would run it
        self.addCleanup(os.chdir, cwd)

    def script(self, planner, members):
        with open(os.path.join(self.dir, "planner.json"), "w") as handle:
            json.dump(planner, handle)
        with open(os.path.join(self.dir, "members.json"), "w") as handle:
            json.dump(members, handle)

    def run_main(self, *extra, members=("a", "b")):
        argv = ["--task", "task.md", "--judge", "%s -B judge.py" % sys.executable,
                "--planner", "plan=command:%s -B planner.py" % sys.executable, "--out", self.out]
        for m in members:
            argv += ["--member", "%s=command:%s -B member.py" % (m, sys.executable)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = engine.main(argv + list(extra))
        return code, out.getvalue(), err.getvalue()

    def summary(self):
        with open(os.path.join(self.out, "summary.json")) as handle:
            return json.load(handle)

    def records(self, name):
        with open(os.path.join(self.out, name)) as handle:
            return [json.loads(line) for line in handle]

    def logged(self, suffix):
        return sorted(f for f in os.listdir(self.log) if f.endswith(suffix))

    def read_log(self, name):
        with open(os.path.join(self.log, name)) as handle:
            return handle.read()


def add(*todos, done=False):
    return {"add": [dict(t) if isinstance(t, dict) else {"text": t, "for": None, "parents": []} for t in todos],
            "drop": [], "done": done, "why": "test"}


class EngineTest(Base):
    def test_events_wake_the_planner_and_members_build_on_judged_results(self):
        self.script(planner=[add("try a small number", "try a medium number"),       # wake 1: the start
                             add(), add({"text": "beat the best", "for": "a", "parents": ["$best"]}),  # after each result
                             add(done=True)],
                    members={"a": [{"answer": "40"}, {"answer": "90"}], "b": [{"answer": "60", "sleep": 0.3}]})
        code, said, err = self.run_main("--turns", "4")
        self.assertEqual(code, 0, said + err)
        s = self.summary()
        self.assertEqual((s["best"], s["best_member"], s["stopped"]), (90.0, "a", "the planner says the task is done"))
        self.assertEqual((s["double_takes"], s["traceable"], s["todos"]), (0, 1.0, {"done": 3}))
        wakes = [w for w in self.records("engine.jsonl") if w["kind"] == "wake"]
        self.assertEqual(wakes[0]["reason"], "the run started")
        self.assertTrue(all("ended todo" in w["reason"] for w in wakes[1:]))  # every later wake was an event
        # the todo that builds on the best result shows that result's answer, and the new entry names it as parent
        kb = TeamKB(os.path.join(self.out, "kb"))
        entries = {e["id"]: e for e in kb.entries()}
        best60 = next(e for e in entries.values() if e["score"] == 60.0)
        last = next(e for e in entries.values() if e["score"] == 90.0)
        self.assertEqual(last["parents"], [best60["id"]])
        prompt = self.read_log("a-02.prompt")
        self.assertIn("It builds on %s by b (score 60)" % best60["id"], prompt)
        self.assertIn("```\n60\n```", prompt)
        # the planner saw the judged results on its board, built by the program
        self.assertIn("%s by b: score 60" % best60["id"], self.read_log("planner-03.txt"))
        # members worked in the board folder, read-only
        where = self.read_log("a-01.where").split("\n")
        self.assertEqual((where[0], where[1]), (os.path.realpath(os.path.join(self.out, "board")), "read"))

    def test_a_fast_member_does_not_wait_for_a_slow_one(self):
        self.script(planner=[add("t1", "t2", "t3", "t4"), add()],
                    members={"a": [{"answer": "1", "sleep": 1.5}], "b": [{"answer": "2", "sleep": 0.1}]})
        started = time.time()
        code, said, err = self.run_main("--turns", "4")
        self.assertEqual(code, 0, said + err)
        by = [t["member"] for t in self.records("run.jsonl")]
        self.assertEqual((by.count("a"), by.count("b")), (1, 3))  # no rounds: b went on while a worked
        self.assertLess(time.time() - started, 4 * 1.5)
        idle = self.summary()["idle_seconds"]
        self.assertGreater(idle["b"], 0.5)  # b ran out of todos while a still worked
        self.assertLess(idle["a"], 0.5)

    def test_the_planners_reply_is_checked_and_sent_back_with_every_reason(self):
        bad = {"add": [{"text": "x", "for": "zed", "parents": ["k000000000000"]}, {"text": ""}], "drop": ["t000000000000"],
               "done": False}
        self.script(planner=[bad, add("fine"), add(done=True)], members={"a": [{"answer": "5"}]})
        code, said, err = self.run_main("--turns", "2", members=("a",))
        self.assertEqual(code, 0, said + err)
        wakes = [w for w in self.records("engine.jsonl") if w["kind"] == "wake"]
        problems = " | ".join(wakes[0]["problems"])
        for expected in ("no member is called 'zed'", "no entry k000000000000", "add 2: \"text\" says what to do",
                         "drop: no todo t000000000000"):
            self.assertIn(expected, problems)
        self.assertEqual((wakes[1]["wake"], wakes[1]["attempt"], len(wakes[1]["added"])), (1, 2, 1))
        self.assertIn("Your last reply was sent back", self.read_log("planner-02.txt"))
        self.assertEqual(self.summary()["planner_refused"], 1)

    def test_too_many_open_todos_are_refused(self):
        self.script(planner=[add(*["t%d" % i for i in range(5)]), add(done=True)], members={"a": [{"answer": "5"}]})
        code, said, err = self.run_main("--turns", "1", "--max-open", "2", "--planner-wakes", "1", members=("a",))
        wakes = [w for w in self.records("engine.jsonl") if w["kind"] == "wake"]
        self.assertIn("this would leave 5 todos open; at most 2", " ".join(wakes[0]["problems"]))
        self.assertEqual(self.summary()["todos_total"], 0)  # both attempts broke the rule: nothing was added
        self.assertEqual(code, 1)  # no valid answer

    def test_the_board_shows_teammates_results_while_a_member_works(self):
        flag = os.path.join(self.dir, "b-judged")
        self.script(planner=[add({"text": "slow one", "for": "a"}, {"text": "fast one", "for": "b"}), add(), add(done=True)],
                    members={"a": [{"answer": "10", "board_first": True, "until_file": flag, "sleep": 0.5, "board_after": True}],
                             "b": [{"answer": "77"}]})
        # b's judged result is on the board while a still works: a waits for a file the test makes after b's turn
        import threading

        def mark():
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    if any(t.get("member") == "b" for t in self.records("run.jsonl")):
                        open(flag, "w").close()
                        return
                except OSError:
                    pass
                time.sleep(0.05)
        threading.Thread(target=mark, daemon=True).start()
        code, said, err = self.run_main("--turns", "2", "--planner-wakes", "1")  # only member turns rewrite the board here
        self.assertEqual(code, 0, said + err)
        first, second = self.read_log("a-01.board1"), self.read_log("a-01.board2")
        self.assertNotIn("score 77", first)
        self.assertIn("score 77", second)  # rewritten by the program during a's turn
        self.assertIn("[taken by a]", second)

    def test_the_run_stops_when_turns_target_or_patience_say_so(self):
        self.script(planner=[add("x", "y")], members={"a": [{"answer": "1"}], "b": [{"answer": "2"}]})
        self.run_main("--turns", "3")
        s = self.summary()
        self.assertEqual((s["turns"], s["stopped"]), (3, "the member turns are used up"))

        shutil.rmtree(self.out)
        shutil.rmtree(self.log)
        self.script(planner=[add("x", "y")], members={"a": [{"answer": "10"}, {"answer": "95"}], "b": [{"answer": "20", "sleep": 0.2}]})
        self.run_main("--turns", "9", "--target", "90")
        s = self.summary()
        self.assertEqual((s["best"], s["stopped"]), (95.0, "the target 90 was reached"))

        shutil.rmtree(self.out)
        shutil.rmtree(self.log)
        self.script(planner=[add("x")], members={"a": [{"answer": "50"}, {"answer": "40"}, {"answer": "30"}, {"answer": "20"}]})
        self.run_main("--turns", "9", "--patience", "2", members=("a",))
        s = self.summary()
        self.assertEqual((s["turns"], s["stopped"]), (3, "2 judged answers in a row did not beat the best"))

    def test_the_planner_is_woken_when_nothing_is_left_and_its_wakes_are_bounded(self):
        self.script(planner=[add("one"), add()], members={"a": [{"answer": "3"}]})
        code, said, err = self.run_main("--turns", "5", "--planner-wakes", "3", members=("a",))
        self.assertEqual(code, 0, said + err)
        s = self.summary()
        self.assertEqual((s["turns"], s["planner_wakes"], s["stopped"]),
                         (1, 3, "the planner's wakes are used up and no todo is left"))

    def test_a_broken_backend_fails_its_todo_or_stops_the_run(self):
        self.script(planner=[add("x", "y")], members={"a": [{"crash": True}], "b": [{"answer": "8", "sleep": 0.3}]})
        code, said, err = self.run_main("--turns", "2")
        self.assertEqual(code, 0, said + err)  # b's valid answer stands; a's crash failed only its todo
        s = self.summary()
        self.assertEqual((s["todos"], s["turn_states"]), ({"failed": 1, "done": 1}, {"error": 1, "idle": 1}))
        self.assertIn("a turn 1: the backend ended error", s["broken"][0])

        shutil.rmtree(self.out)
        shutil.rmtree(self.log)
        self.script(planner=[add("x", "y", "z")], members={"a": [{"crash": True}], "b": [{"answer": "8", "sleep": 1.0}]})
        code, said, err = self.run_main("--turns", "6", "--stop-on-infra-error")
        self.assertEqual(code, 3, said + err)
        s = self.summary()
        self.assertTrue(s["stopped"].startswith("the setup broke"))
        self.assertEqual(s["turns"], 2)  # no new todo was taken after the crash; b's running turn finished

    def test_failures_and_unusable_replies_end_the_todo_as_failed(self):
        self.script(planner=[add("x", "y", "z"), add()], members={"a": [{"fail": "could not do it"}, {"garbage": True}],
                                                                  "b": [{"answer": "not a number", "sleep": 0.2}]})
        self.run_main("--turns", "3")
        todos = TeamKB(os.path.join(self.out, "kb")).todo_list()
        self.assertEqual(sorted(t["state"] for t in todos), ["failed", "failed", "failed"])
        details = " | ".join(t["detail"] or "" for t in todos)
        for expected in ("could not do it", "no fenced answer and no FAILED line", "not a number"):
            self.assertIn(expected, details)

    def test_many_members_never_take_one_todo_twice(self):
        self.script(planner=[add(*["t%d" % i for i in range(8)]), add()],
                    members={m: [{"answer": str(i), "sleep": 0.05 * i}] for i, m in enumerate("abcd", 1)})
        code, said, err = self.run_main("--turns", "8", "--max-open", "8", members=tuple("abcd"))
        self.assertEqual(code, 0, said + err)
        turns = self.records("run.jsonl")
        self.assertEqual(len({t["todo"] for t in turns}), 8)
        kb = TeamKB(os.path.join(self.out, "kb"))
        self.assertEqual((kb.double_takes, sorted({t["takes"] for t in kb.todo_list()})), (0, [1]))

    def test_a_todo_meant_for_one_member_waits_for_that_member(self):
        self.script(planner=[add({"text": "b's job", "for": "b"}, {"text": "b's other job", "for": "b"}), add()],
                    members={"a": [{"answer": "1"}], "b": [{"answer": "2", "sleep": 0.4}]})
        code, said, err = self.run_main("--turns", "2")
        self.assertEqual(code, 0, said + err)
        self.assertEqual([t["member"] for t in self.records("run.jsonl")], ["b", "b"])  # a was free, and took neither

    def test_a_take_of_a_todo_that_is_not_open_is_counted(self):
        folder = os.path.join(self.dir, "kb2")
        kb = TeamKB(folder)
        tid = kb.add_todo("one")
        kb.take_todo("a")
        kb._write({"type": "todo", "op": "take", "id": tid, "t": 1, "member": "b"})  # a writer that skipped the lock
        self.assertEqual((kb.double_takes, TeamKB(folder).double_takes), (1, 1))

    def test_the_open_todo_limit_holds_at_its_boundary(self):
        self.script(planner=[add("one", "two"), add(done=True)], members={"a": [{"answer": "5"}]})
        self.run_main("--turns", "1", "--max-open", "2", "--planner-wakes", "1", members=("a",))
        wakes = [w for w in self.records("engine.jsonl") if w["kind"] == "wake"]
        self.assertEqual((wakes[0].get("problems"), len(wakes[0]["added"])), (None, 2))  # exactly the limit: accepted
        shutil.rmtree(self.out)
        shutil.rmtree(self.log)
        self.script(planner=[add("one", "two", "three"), add(done=True)], members={"a": [{"answer": "5"}]})
        self.run_main("--turns", "1", "--max-open", "2", "--planner-wakes", "1", members=("a",))
        wakes = [w for w in self.records("engine.jsonl") if w["kind"] == "wake"]
        self.assertIn("this would leave 3 todos open; at most 2", " ".join(wakes[0]["problems"]))  # one over: sent back

    def test_a_todo_being_worked_on_cannot_be_dropped(self):
        drop_taken = {"add": [], "drop": ["$taken"], "done": False}
        self.script(planner=[add({"text": "slow", "for": "a"}, {"text": "fast", "for": "b"}), drop_taken, add(), add(done=True)],
                    members={"a": [{"answer": "1", "sleep": 1.0}], "b": [{"answer": "2"}]})
        self.run_main("--turns", "3")  # turns left after b's result: the planner is woken while a works
        wakes = [w for w in self.records("engine.jsonl") if w["kind"] == "wake"]
        self.assertIn("is taken, only open todos can be dropped", " ".join(wakes[1]["problems"]))
        self.assertEqual(sorted(t["state"] for t in TeamKB(os.path.join(self.out, "kb")).todo_list()), ["done", "done"])

    def test_reaching_the_target_exactly_stops_the_run(self):
        self.script(planner=[add("x")], members={"a": [{"answer": "95"}, {"answer": "99"}]})
        self.run_main("--turns", "5", "--target", "95", members=("a",))
        s = self.summary()
        self.assertEqual((s["turns"], s["best"], s["stopped"]), (1, 95.0, "the target 95 was reached"))

    def test_time_a_member_waits_in_the_middle_of_a_run_is_counted(self):
        self.script(planner=[add({"text": "slow", "for": "a"}, {"text": "fast", "for": "b"}), add(),
                             add({"text": "b again", "for": "b"}), add(done=True)],
                    members={"a": [{"answer": "1", "sleep": 1.2}], "b": [{"answer": "2"}, {"answer": "3"}]})
        code, said, err = self.run_main("--turns", "3")
        self.assertEqual(code, 0, said + err)
        self.assertEqual([t["member"] for t in self.records("run.jsonl")], ["b", "a", "b"])
        self.assertGreater(self.summary()["idle_seconds"]["b"], 0.8)  # b waited for a's result before its second todo

    def test_research_access_and_verified_results_as_files_on_the_board(self):
        self.script(planner=[add("one"), add(done=True)], members={"a": [{"answer": "42", "board_after": True}]})
        code, said, err = self.run_main("--turns", "1", "--member-access", "research", members=("a",))
        self.assertEqual(code, 0, said + err)
        self.assertEqual(self.read_log("a-01.where").split("\n")[1], "research")
        entry = next(e for e in TeamKB(os.path.join(self.out, "kb")).entries() if e["status"] == "valid")
        with open(os.path.join(self.out, "board", "artifacts", entry["id"] + ".txt")) as handle:
            self.assertEqual(handle.read().strip(), "42")
        with open(os.path.join(self.out, "board", "TEAM_BOARD.md")) as handle:
            self.assertIn("artifacts/%s.txt: %s by a, score 42" % (entry["id"], entry["id"]), handle.read())

    def test_a_todo_waits_for_the_todos_it_comes_after(self):
        self.script(planner=[add("first", {"text": "second", "for": None, "parents": [], "after": ["#1"]}), add()],
                    members={"a": [{"answer": "1", "sleep": 0.6}], "b": [{"answer": "2"}]})
        code, said, err = self.run_main("--turns", "2")
        self.assertEqual(code, 0, said + err)
        todos = {t["text"]: t for t in TeamKB(os.path.join(self.out, "kb")).todo_list()}
        first, second = todos["first"], todos["second"]
        self.assertEqual(second["after"], [first["id"]])
        self.assertGreaterEqual(second["taken_t"], first["ended_t"])  # b was free, but second waited for first to end
        with open(os.path.join(self.out, "board", "TEAM_BOARD.md")) as handle:
            self.assertIn("(after %s)" % first["id"], handle.read())

    def test_after_must_name_an_existing_or_earlier_todo(self):
        bad = {"add": [{"text": "x", "after": ["t000000000000"]}, {"text": "y", "after": ["#2"]}], "drop": [], "done": False}
        self.script(planner=[bad, add(done=True)], members={"a": [{"answer": "5"}]})
        self.run_main("--turns", "1", "--planner-wakes", "1", members=("a",))
        problems = " | ".join([w for w in self.records("engine.jsonl") if w["kind"] == "wake"][0]["problems"])
        self.assertIn('add 1: "after" names no todo t000000000000', problems)
        self.assertIn('add 2: "after" #2 must point to an earlier todo in this reply (#1 to #1)', problems)

    def test_a_busy_team_does_not_wake_the_planner_after_every_result(self):
        self.script(planner=[add(*["t%d" % i for i in range(6)]), add()],
                    members={m: [{"answer": str(i), "sleep": 0.3}] for i, m in enumerate("abc", 1)})
        code, said, err = self.run_main("--turns", "6", "--max-open", "6", members=("a", "b", "c"))
        self.assertEqual(code, 0, said + err)
        s = self.summary()
        self.assertEqual(s["turns"], 6)
        self.assertLessEqual(s["planner_wakes"], 3)  # the start and a lap of 3 results, not one wake per result (7)

    def test_a_member_free_with_nothing_to_take_wakes_the_planner_at_once(self):
        self.script(planner=[add({"text": "quick", "for": "a"}, {"text": "slow", "for": "b"}), add({"text": "more", "for": "a"}), add()],
                    members={"a": [{"answer": "1"}, {"answer": "3"}], "b": [{"answer": "2", "sleep": 1.2}]})
        code, said, err = self.run_main("--turns", "3")
        self.assertEqual(code, 0, said + err)
        wakes = [w for w in self.records("engine.jsonl") if w["kind"] == "wake"]
        self.assertIn("free with nothing to take: a", wakes[1]["reason"])
        b_end = [t for t in self.records("run.jsonl") if t["member"] == "b"][0]["end"]
        self.assertLess(wakes[1]["start"], b_end)  # a did not wait for b
        prompt = self.read_log("planner-02.txt")
        self.assertIn("free and waiting for a todo: a", prompt)
        self.assertIn("b works on", prompt)

    def test_the_time_limit_stops_new_work(self):
        self.script(planner=[add(*["t%d" % i for i in range(6)]), add()],
                    members={"a": [{"answer": "1", "sleep": 1.5}], "b": [{"answer": "2", "sleep": 1.5}]})
        started = time.time()
        code, said, err = self.run_main("--turns", "10", "--max-open", "6", "--time-limit", "1")
        s = self.summary()
        self.assertEqual((s["turns"], s["stopped"]), (2, "the time limit of 1 s was reached"))  # running turns finished
        self.assertLess(time.time() - started, 4)

    def test_after_points_to_the_right_todo_of_the_same_reply(self):
        third = {"text": "third", "for": None, "parents": [], "after": ["#1"]}
        self.script(planner=[add("first", "second", third), add()], members={"a": [{"answer": "1"}]})
        self.run_main("--turns", "1", "--planner-wakes", "1", "--max-open", "3", members=("a",))
        todos = {t["text"]: t for t in TeamKB(os.path.join(self.out, "kb")).todo_list()}
        self.assertEqual(todos["third"]["after"], [todos["first"]["id"]])

    def test_results_that_arrive_one_by_one_are_gathered_into_a_lap(self):
        self.script(planner=[add(*["t%d" % i for i in range(8)]), add()],
                    members={"a": [{"answer": "1", "sleep": 0.2}], "b": [{"answer": "2", "sleep": 0.45}], "c": [{"answer": "3", "sleep": 0.9}]})
        code, said, err = self.run_main("--turns", "7", "--max-open", "8", members=("a", "b", "c"))
        self.assertEqual(code, 0, said + err)
        s = self.summary()
        self.assertEqual(s["turns"], 7)
        self.assertLessEqual(s["planner_wakes"], 3)  # one wake per result would be about 7

    def test_no_planner_wake_once_the_turns_are_used_up(self):
        self.script(planner=[add("x", "y"), add()], members={"a": [{"answer": "1"}], "b": [{"answer": "2", "sleep": 0.8}]})
        code, said, err = self.run_main("--turns", "2")
        self.assertEqual(code, 0, said + err)
        self.assertEqual(self.summary()["planner_wakes"], 1)  # a was free after its turn, but no turns were left to give it

    def test_a_wall_clock_that_jumps_back_does_not_bend_the_durations(self):
        # seen on RHEL in a VM: the guest's clock was stepped back mid-run and b's waiting time came out as -1.0 s
        import threading
        import types
        real, shift = time.time, {"by": 0.0}
        engine.time = types.SimpleNamespace(time=lambda: real() - shift["by"], monotonic=time.monotonic, sleep=time.sleep,
                                            strftime=time.strftime, localtime=time.localtime)
        self.addCleanup(setattr, engine, "time", time)
        threading.Timer(0.5, lambda: shift.__setitem__("by", 5.0)).start()  # the wall clock jumps 5 s back
        self.script(planner=[add("t1", "t2"), add()], members={"a": [{"answer": "1", "sleep": 1.0}], "b": [{"answer": "2"}]})
        code, said, err = self.run_main("--turns", "2")
        self.assertEqual(code, 0, said + err)
        s = self.summary()
        self.assertTrue(all(v >= 0 for v in s["idle_seconds"].values()), s["idle_seconds"])
        self.assertGreater(s["idle_seconds"]["b"], 0.5)  # b waited for a about 1 s
        self.assertTrue(s["seconds"] > 0.9 and all(t["seconds"] >= 0 for t in self.records("run.jsonl")))


class PartsTest(Base):
    def test_arguments_are_checked(self):
        self.script(planner=[add()], members={})
        code, _, err = self.run_main("--about", "zed=x")
        self.assertEqual(code, 2)
        self.assertIn("--about: no member called zed", err)
        argv = ["--task", "task.md", "--judge", "j", "--planner", "a=claude", "--member", "a=claude", "--out", self.out]
        with contextlib.redirect_stderr(io.StringIO()) as e:
            self.assertEqual(engine.main(argv), 2)
        self.assertIn("a is also a member", e.getvalue())

    def test_board_reads_are_counted_from_member_logs(self):
        self.script(planner=[add(done=True)], members={})
        self.run_main("--turns", "1")
        os.makedirs(os.path.join(self.out, "members", "claude"), exist_ok=True)
        with open(os.path.join(self.out, "members", "claude", "events.jsonl"), "w") as handle:
            for agent, path in (("a", "TEAM_BOARD.md"), ("a", "other.md"), ("b", "/x/board/TEAM_BOARD.md"), ("zed", "TEAM_BOARD.md")):
                handle.write(json.dumps({"agent": agent, "t": 1, "event": {"tool": "Read", "input": {"file_path": path}}}) + "\n")
        run = engine.EngineRun("t", None, None, ["a", "b"], "plan", self.out + "2", 1)
        run.out = self.out
        self.assertEqual(run.board_reads(), {"by_member": {"a": 1, "b": 1}, "measured": ["claude"]})

    def test_a_take_is_atomic_across_kb_objects(self):
        folder = os.path.join(self.dir, "kb")
        one, two = TeamKB(folder), TeamKB(folder)
        tid = one.add_todo("only one may take this")
        self.assertEqual(one.take_todo("a")["id"], tid)
        self.assertIsNone(two.take_todo("b"))  # the second object reads the first's take before choosing
        self.assertEqual(two.double_takes, 0)
        self.assertFalse(two.drop_todo(tid))  # taken: it runs to its end
        with self.assertRaises(TeamKBError):
            one.add_todo("after nothing", after=["tnothing00000"])
        one.end_todo(tid, "a", "done", entry=None, status="valid", score=1)
        self.assertEqual(TeamKB(folder).todo_list()[0]["state"], "done")


class ViewTest(Base):
    def test_the_page_shows_the_loop_the_timeline_and_every_todo_without_javascript(self):
        nasty = "make it </script><b>bold</b> & quick"
        self.script(planner=[add(nasty, "two"), add(), add(done=True)],
                    members={"a": [{"answer": "40"}], "b": [{"answer": "x", "sleep": 0.2}]})
        code, said, err = self.run_main("--turns", "2")
        with open(os.path.join(self.out, "view.html")) as handle:
            page = handle.read()
        for marker in ('id="n-planner"', 'id="a-wake"', 'id="timeline"', 'class="ln">planner<', 'class="ln">a<', 'class="ln">b<',
                       'class="bar pass"', 'class="bar fail"', 'class="event timed"', "stopped: the member turns are used up"):
            self.assertIn(marker, page)
        todos = TeamKB(os.path.join(self.out, "kb")).todo_list()
        for t in todos:
            self.assertIn(t["id"], page)
        self.assertIn("make it &lt;/script&gt;&lt;b&gt;bold&lt;/b&gt; &amp; quick", page)  # escaped as text
        self.assertEqual(page.count("</script>"), 2)  # only the page's own two script elements end
        blob = page.split('<script type="application/json" id="run-data">')[1].split("</script>")[0]
        data = json.loads(blob)
        self.assertEqual((len(data["turns"]), len(data["todos"]), data["finished"]), (2, 2, True))
        self.assertTrue(all(t["s"] <= t["e"] for t in data["turns"] + data["wakes"]))
        self.assertIn('class="player" id="player"></div>', page)  # empty without JavaScript: hidden by .player:empty

    def test_the_page_shows_the_tools_each_turn_used_and_what_built_on_what(self):
        os.makedirs(os.path.join(self.out, "kb", "artifacts"))
        os.makedirs(os.path.join(self.out, "members", "claude"))
        rows = {"engine.jsonl": [{"t": 100.0, "kind": "start", "members": ["a", "b"], "planner": "plan", "turns": 4},
                                 {"t": 100.5, "start": 100.1, "end": 100.5, "kind": "wake", "wake": 1, "attempt": 1,
                                  "reason": "the run started", "state": "idle", "added": ["t1"], "dropped": []}],
                "run.jsonl": [{"t": 101.0, "start": 101.0, "end": 110.0, "turn": 1, "member": "a", "todo": "t1", "state": "idle",
                               "kind": "result", "status": "valid", "score": 6, "entry": "kskill"},
                              {"t": 111.0, "start": 111.0, "end": 120.0, "turn": 2, "member": "b", "todo": "t2", "state": "idle",
                               "kind": "result", "status": "valid", "score": 90, "entry": "kanim"}]}
        for name, items in rows.items():
            with open(os.path.join(self.out, name), "w") as handle:
                handle.write("".join(json.dumps(r) + "\n" for r in items))
        for rel, text in (("artifacts/s.txt", "ARTIFACT: skill\n---"), ("artifacts/a.txt", "ARTIFACT: animation\n<html>")):
            with open(os.path.join(self.out, "kb", rel), "w") as handle:
                handle.write(text)
        kb = [{"type": "propose", "id": "kskill", "t": 109.0, "member": "a", "summary": "how to draw a walking couple",
               "parents": [], "kind": "result", "artifact": "artifacts/s.txt"},
              {"type": "verdict", "id": "kskill", "status": "valid", "score": 6},
              {"type": "propose", "id": "kanim", "t": 119.0, "member": "b", "summary": "the couple walks through day 1",
               "parents": ["kskill"], "kind": "result", "artifact": "artifacts/a.txt"},
              {"type": "verdict", "id": "kanim", "status": "valid", "score": 90}]
        with open(os.path.join(self.out, "kb", "events.jsonl"), "w") as handle:
            handle.write("".join(json.dumps(r) + "\n" for r in kb))
        def use(t, agent, name, inp):
            return {"t": t, "agent": agent, "event": {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": name, "input": inp}]}}}
        log = [use(102.0, "a", "WebSearch", {"query": "新竹 約會 <景點>"}), use(103.0, "a", "WebFetch", {"url": "https://example.org/x"}),
               use(104.0, "a", "Read", {"file_path": "/r/board/TEAM_BOARD.md"}), use(112.0, "b", "Read", {"file_path": "/r/board/artifacts/kskill.txt"}),
               use(150.0, "a", "WebSearch", {"query": "outside any turn"})]
        with open(os.path.join(self.out, "members", "claude", "events.jsonl"), "w") as handle:
            handle.write("".join(json.dumps(r) + "\n" for r in log))
        page = engineview.render(self.out)
        for marker in ('class="tool web timed"', 'class="tool fetch timed"', 'class="tool read timed"', 'class="lin timed"',
                       'class="kh">skill<', 'class="kh">animation<', "Tools the team wrote for itself",
                       "built on by 1: kanim (b, animation)", "searched 1 time: 新竹 約會 &lt;景點&gt;", "fetched 1 sites: example.org",
                       "read 1 files: TEAM_BOARD.md", "read 1 files: kskill.txt"):
            self.assertIn(marker, page)
        self.assertNotIn("outside any turn", page.split('id="gather"')[1].split("</ul>")[0])  # only calls inside a turn's time
        data = json.loads(page.split('<script type="application/json" id="run-data">')[1].split("</script>")[0])
        self.assertEqual(data["turns"][0]["tools"][0], ["WebSearch", "新竹 約會 <景點>"])

    def test_a_run_still_going_is_drawn_from_what_is_there(self):
        os.makedirs(os.path.join(self.out, "kb"))
        with open(os.path.join(self.out, "engine.jsonl"), "w") as handle:
            handle.write(json.dumps({"t": 100.0, "kind": "start", "members": ["a"], "planner": "plan", "turns": 3}) + "\n")
            handle.write(json.dumps({"t": 101.0, "start": 100.2, "end": 101.0, "kind": "wake", "wake": 1, "attempt": 1,
                                     "reason": "the run started", "state": "idle", "added": ["t1"], "dropped": []}) + "\n")
            handle.write('{"t": 102.0, "kind": "wa')  # a line still being written
        page = engineview.render(self.out)
        self.assertIn("still running", page)
        self.assertIn("0 of 3 member turns", page)
        self.assertIn('"finished": false', page)


if __name__ == "__main__":
    unittest.main()
