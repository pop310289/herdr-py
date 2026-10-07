"""Team runs (S, N, T) with scripted agents on the fake OpenCode server, plus prompt-and-wait and the guards."""
import os
import shutil
import sys
import tempfile
import time
import unittest

from herdr_py.client import Client, ClientError
from herdr_py.opencode import OpenCode
from herdr_py.policy import Policy
from herdr_py.server import Daemon
from herdr_py.team import TeamRun

from fake_opencode import FakeOpenCode
from test_core import wait_for

CHECK = "import sys\nok = open('ok.txt').read().strip() == 'OK' if __import__('os').path.exists('ok.txt') else False\nprint('PASS' if ok else 'FAIL: ok.txt must contain OK')\nsys.exit(0 if ok else 1)\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenCode().start()
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.work = os.path.join(self.dir, "work")
        os.makedirs(self.work)
        with open(os.path.join(self.work, "check.py"), "w") as handle:
            handle.write(CHECK)
        self.daemon = Daemon(OpenCode(self.fake.url), Policy(default="deny"), os.path.join(self.dir, "state"),
                             socket_path=os.path.join(self.dir, "s.sock"), max_agents=6, max_prompts=8)
        self.daemon.start()
        self.client = Client(self.daemon.socket_path, timeout=30)
        wait_for(lambda: self.fake.stream_count() == 1, what="event stream")
        self.task = {"name": "ok", "prompt": "Write OK into ok.txt.", "check": [sys.executable, "check.py"]}
        self.prompts = []

    def tearDown(self):
        self.daemon.stop()
        self.fake.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text, notes=None):
        with open(os.path.join(self.work, "ok.txt"), "w") as handle:
            handle.write(text)
        if notes:
            with open(os.path.join(self.work, "NOTES.md"), "w") as handle:
                handle.write(notes)

    def script(self, fix_on="Supervisor:", stall=False, val="Looks right. VERDICT: ACCEPT"):
        def on_prompt(sid, text):
            name = self.fake.name_of(sid)
            self.prompts.append((name, text))
            if name.startswith("exec"):
                if stall and name == "exec":
                    self.write("NO", notes="Done: started\\nNext: write OK into ok.txt")
                    self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
                    return  # never finishes: the supervisor has to notice
                fixed = fix_on in text
                self.write("OK" if fixed else "NO", notes="Done: wrote ok.txt\\nNext: nothing")
                self.fake.turn(sid, "done", tools=[("write", "completed", {"filePath": "ok.txt"}, "")])
            elif name == "ver":
                self.fake.turn(sid, "PROBLEM: ok.txt says NO | EVIDENCE: cat ok.txt -> NO")
            elif name == "val":
                self.fake.turn(sid, val)
        self.fake.on_prompt = on_prompt

    def run_team(self, condition, **kw):
        kw.setdefault("poll_s", 0.05)
        kw.setdefault("wall_s", 30)
        return TeamRun(self.client, self.task, condition, self.work, **kw).run()



class TeamTest(Base):
    def test_single_stops_at_the_first_idle(self):
        self.script()
        s = self.run_team("S")
        self.assertEqual((s["outcome"], s["final_check"], len(self.prompts)), ("single-finished", False, 1))
        self.assertIn("NOTES.md", self.prompts[0][1])  # every condition gets the same executor instructions

    def test_generic_nudges_do_not_carry_the_check_output(self):
        self.script()
        s = self.run_team("N", rounds=3)
        self.assertEqual(s["final_check"], False)
        self.assertEqual(len([e for e in s["interventions"] if e["kind"] == "nudge"]), 3)
        self.assertTrue(all("FAIL: ok.txt" not in text for _, text in self.prompts))

    def test_supervisor_rescues_with_the_check_output_and_the_verifier(self):
        self.script()
        s = self.run_team("T", rounds=3)
        self.assertEqual((s["outcome"], s["first_check"], s["final_check"]), ("accepted", False, True))
        feedback = [text for name, text in self.prompts if name == "exec" and "Supervisor:" in text][0]
        for piece in ("FAIL: ok.txt must contain OK", "PROBLEM: ok.txt says NO", "Next: nothing", "write ok.txt"):
            self.assertIn(piece, feedback)
        self.assertEqual([name for name, _ in self.prompts], ["exec", "ver", "exec", "val"])

    def exec_writes_ok(self, val_replies):
        """Executor always writes OK; the validator answers from `val_replies` in order."""
        def on_prompt(sid, text):
            name = self.fake.name_of(sid)
            self.prompts.append((name, text))
            if name.startswith("exec"):
                self.write("OK", notes="Done: ok.txt\nNext: -")
                self.fake.turn(sid, "done")
            elif name == "val":
                self.fake.turn(sid, val_replies.pop(0))
        self.fake.on_prompt = on_prompt

    def test_a_reject_without_evidence_is_only_an_opinion(self):
        self.exec_writes_ok(["I am not sure. VERDICT: REJECT"])
        s = self.run_team("T")
        self.assertEqual((s["outcome"], s["first_check"]), ("accepted", True))
        self.assertEqual([name for name, _ in self.prompts], ["exec", "val"])

    def test_a_reject_with_evidence_sends_the_executor_back(self):
        self.exec_writes_ok(["VERDICT: REJECT\nEVIDENCE: the task also asks for a newline at the end",
                             "VERDICT: ACCEPT"])
        s = self.run_team("T")
        self.assertEqual(s["outcome"], "accepted")
        feedback = [text for name, text in self.prompts if name == "exec" and "Supervisor:" in text]
        self.assertEqual(len(feedback), 1)
        self.assertIn("the task also asks for a newline", feedback[0])
        self.assertIn("passed", feedback[0])

    def test_a_stalled_executor_is_replaced_and_the_notes_carry_over(self):
        self.script(stall=True)
        s = self.run_team("T", stall_s=0.5)
        self.assertEqual(s["executors"], ["exec", "exec2"])
        handoff = [text for name, text in self.prompts if name == "exec2"][0]
        self.assertIn("Next: write OK into ok.txt", handoff)
        self.assertIn("taking over", handoff)
        self.assertEqual(len([e for e in s["interventions"] if e["kind"] == "stall"]), 1)

    def test_a_spinning_executor_is_interrupted_at_the_checkpoint(self):
        def on_prompt(sid, text):
            name = self.fake.name_of(sid)
            self.prompts.append((name, text))
            if name == "exec" and "Supervisor:" not in text:
                self.write("NO", notes="Done: tried\nNext: fix ok.txt")

                def spin():  # keeps producing output without ever stopping
                    for _ in range(400):
                        if sid in self.fake.aborts:
                            self.fake.emit("session.error", sessionID=sid, error={"name": "MessageAbortedError"})
                            self.fake.emit("session.idle", sessionID=sid)
                            return
                        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
                        self.fake.emit("message.part.updated", part={"id": "p", "sessionID": sid, "type": "tool", "callID": str(time.time()),
                                                                       "tool": "bash", "state": {"status": "completed", "input": {"command": "python3 x.py"}}})
                        time.sleep(0.05)
                spin()
            elif name == "exec":
                self.write("OK")
                self.fake.turn(sid, "fixed")
            elif name == "ver":
                self.fake.turn(sid, "PROBLEM: ok.txt says NO | EVIDENCE: cat ok.txt")
            elif name == "val":
                self.fake.turn(sid, "VERDICT: ACCEPT")
        self.fake.on_prompt = on_prompt
        s = self.run_team("T", checkpoint_s=1.0, stall_s=30)
        self.assertEqual((s["outcome"], s["final_check"]), ("accepted", True))
        self.assertIn("checkpoint", [e["kind"] for e in s["interventions"]])
        self.assertEqual(self.fake.aborts[:1], [self.client.call("agent.get", name="exec")["session_id"]])


class PromptWaitTest(Base):
    def test_prompt_and_wait_returns_after_the_new_turn_and_flags_a_stall(self):
        self.script()
        self.client.call("agent.start", name="exec", prompt="first")
        wait_for(lambda: self.client.call("agent.get", name="exec")["state"] == "idle", what="first turn")
        view = self.client.call("agent.prompt", name="exec", text="Supervisor: again", wait=True, timeout_s=10)
        self.assertEqual((view["state"], view["completions"]), ("idle", 2))
        self.fake.on_prompt = None  # nobody answers: OpenCode never reports busy
        with self.assertRaises(ClientError) as ctx:
            self.client.call("agent.prompt", name="exec", text="hello?", wait=True, timeout_s=10)
        self.assertIn("prompt_stalled", str(ctx.exception))

    def test_max_prompts_per_agent(self):
        self.script()
        self.client.call("agent.start", name="busy", prompt="1")  # max_prompts=8 in Base
        for i in range(7):
            self.client.call("agent.prompt", name="busy", text=str(i + 2))
        with self.assertRaises(ClientError) as ctx:
            self.client.call("agent.prompt", name="busy", text="9")
        self.assertIn("max_prompts", str(ctx.exception))

    def test_limits_stop_a_runaway_manager(self):
        self.script()
        for i in range(6):
            self.client.call("agent.start", name=f"a{i}", prompt="x")
        with self.assertRaises(ClientError) as ctx:
            self.client.call("agent.start", name="a6", prompt="x")
        self.assertIn("max_agents", str(ctx.exception))



if __name__ == "__main__":
    unittest.main()
