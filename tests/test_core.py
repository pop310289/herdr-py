"""Hub and policy tests against the fake OpenCode server (real HTTP client, real event stream)."""
import json
import os
import tempfile
import time
import unittest

from herdr_py.core import Hub, HubError
from herdr_py.opencode import OpenCode, start_stream_thread
from herdr_py.policy import Policy, PolicyError, describe, target_of

from fake_opencode import FakeOpenCode

RULES = [{"permission": "bash", "match": r"ls( -\w+)*", "action": "allow"},
         {"permission": "bash", "match": r"rm .*", "action": "deny", "message": "no deleting"},
         {"permission": "external_directory", "action": "deny"}]


def wait_for(cond, timeout=6.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class HubTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenCode(password="pw").start()
        self.client = OpenCode(self.fake.url, password="pw")
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.hub = self.make_hub()
        self.stop, _ = start_stream_thread(self.client, self.hub.on_event, self.hub.on_stream_state)
        wait_for(lambda: self.fake.stream_count() == 1, what="event stream")

    def make_hub(self, **kw):
        return Hub(self.client, Policy(RULES, default="ask"), log_path=os.path.join(self.tmp.name, "events.jsonl"),
                   state_path=os.path.join(self.tmp.name, "state.json"), clock=self.clock, run=lambda fn: fn(), **kw)

    def tearDown(self):
        self.stop.set()
        self.fake.stop()
        self.hub.close()
        self.tmp.cleanup()

    def state(self, name="a"):
        return self.hub.get(name)["state"]

    def start(self, name="a", **kw):
        self.hub.start(name, "do it", **kw)
        return self.hub.agents[name].session_id

    def test_a_turn_goes_starting_working_idle_and_ignores_a_stale_idle(self):
        sid = self.start()
        self.assertEqual(self.state(), "starting")
        self.assertEqual(self.fake.prompts[0][1]["parts"][0]["text"], "do it")
        self.fake.emit("session.idle", sessionID=sid)  # left over from before the prompt reached OpenCode
        time.sleep(0.2)
        self.assertEqual(self.state(), "starting")
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        wait_for(lambda: self.state() == "working")
        self.fake.emit("session.status", sessionID=sid, status={"type": "idle"})
        wait_for(lambda: self.state() == "idle")
        view = self.hub.get("a")
        self.assertEqual((view["turns"], self.hub.agents["a"].idles), (1, 1))

    def test_policy_allows_denies_and_leaves_ask_for_a_human(self):
        sid = self.start()
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        ok = self.fake.ask_permission(sid, "bash", "ls -la")
        bad = self.fake.ask_permission(sid, "bash", "rm -rf /")
        wait_for(lambda: len(self.fake.replies) == 2, what="two policy replies")
        replies = {r[0]: r for r in self.fake.replies}
        self.assertEqual(replies[ok][1], "once")
        self.assertEqual(replies[bad][1:], ("reject", "no deleting"))
        human = self.fake.ask_permission(sid, "bash", "curl -sI https://example.com")
        wait_for(lambda: self.state() == "blocked", what="blocked")
        self.assertEqual([p["id"] for p in self.hub.pending()], [human])
        time.sleep(0.2)
        self.assertNotIn(human, {r[0] for r in self.fake.replies})  # nobody answered for the human
        self.hub.reply(human, "reject", "not now")
        wait_for(lambda: self.state() == "working", what="back to working")
        self.assertIn((human, "reject", "not now"), self.fake.replies)

    def test_requests_from_sessions_we_did_not_create_are_never_answered(self):
        self.start()
        self.fake.sessions["ses_other"] = {"id": "ses_other"}
        foreign = self.fake.ask_permission("ses_other", "bash", "ls")
        time.sleep(0.4)
        self.assertNotIn(foreign, {r[0] for r in self.fake.replies})
        self.assertEqual(self.hub.pending(), [])

    def test_subagent_requests_are_mapped_to_the_root_agent(self):
        sid = self.start()
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        wait_for(lambda: self.state() == "working")
        child = self.fake.child(sid)
        ok = self.fake.ask_permission(child, "bash", "ls")
        wait_for(lambda: ok in {r[0] for r in self.fake.replies}, what="child request answered")
        self.assertIn(child, self.hub.get("a")["children"])
        self.fake.emit("session.idle", sessionID=child)
        time.sleep(0.2)
        self.assertEqual(self.state(), "working")  # a subagent finishing does not finish its parent
        early = self.fake.child(sid, announce=False)  # its request arrives before session.created
        far = self.fake.ask_permission(early, "external_directory", patterns=["/think/*"])
        wait_for(lambda: far in {r[0] for r in self.fake.replies}, what="early child request")
        self.assertEqual({r[0]: r[1] for r in self.fake.replies}[far], "reject")
        middle = self.fake.child(sid, announce=False)
        grandchild = self.fake.child(middle, announce=False)  # two unknown levels
        deep = self.fake.ask_permission(grandchild, "bash", "ls -1")
        wait_for(lambda: deep in {r[0] for r in self.fake.replies}, what="grandchild request")
        self.assertEqual(self.hub.root_of(grandchild), sid)

    def test_followups_then_time_budget_abort(self):
        sid = self.start(followups=["second"], budget_s=10)
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        wait_for(lambda: self.state() == "working")
        self.fake.emit("session.status", sessionID=sid, status={"type": "idle"})
        wait_for(lambda: self.state() == "idle")
        self.hub.tick()
        self.assertEqual([p[1]["parts"][0]["text"] for p in self.fake.prompts], ["do it", "second"])
        self.assertEqual(self.state(), "starting")
        self.hub.tick()
        self.assertEqual(len(self.fake.prompts), 2)  # a follow-up is sent once
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        wait_for(lambda: self.state() == "working")
        self.clock.now += 9
        self.hub.tick()
        self.assertEqual(self.fake.aborts, [])  # within budget
        self.clock.now += 2
        self.hub.tick()
        self.assertEqual(self.fake.aborts, [sid])
        self.assertEqual(self.state(), "aborted")
        self.fake.emit("session.error", sessionID=sid, error={"name": "MessageAbortedError"})
        self.fake.emit("session.idle", sessionID=sid)
        time.sleep(0.2)
        self.assertEqual(self.state(), "aborted")  # the abort's own error does not turn it into "error"

    def test_an_error_sticks_until_the_next_prompt(self):
        sid = self.start()
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        self.fake.emit("session.error", sessionID=sid, error={"name": "ProviderError", "data": {"message": "boom"}})
        wait_for(lambda: self.state() == "error")
        self.hub.prompt("a", "again")
        self.assertEqual(self.state(), "starting")

    def test_resync_after_a_dropped_stream(self):
        sid = self.start()
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        wait_for(lambda: self.state() == "working")
        human = self.fake.ask_permission(sid, "bash", "curl x")
        wait_for(lambda: self.state() == "blocked")
        self.fake.drop_streams()
        wait_for(lambda: self.fake.stream_count() == 0, what="stream dropped")
        self.fake.pending.pop(human)  # answered elsewhere while we were away
        missed = self.fake.ask_permission(sid, "bash", "ls", announce=False)  # asked while we were away
        wait_for(lambda: missed in {r[0] for r in self.fake.replies}, timeout=8, what="missed request answered after resync")
        wait_for(lambda: self.hub.pending() == [], what="stale request dropped")

    def test_state_file_reattaches_agents_and_children(self):
        sid = self.start()
        child = self.fake.child(sid)
        self.fake.ask_permission(child, "bash", "ls")
        wait_for(lambda: child in self.hub.get("a")["children"])
        self.hub.save()
        again = self.make_hub()
        self.assertEqual(again.load(), 1)
        self.assertEqual(again.agents["a"].session_id, sid)
        self.assertEqual(again.root_of(child), sid)
        again.close()

    def test_fresh_start_gives_a_finished_agent_a_new_session_and_ignores_the_old_one(self):
        old = self.start()
        self.fake.emit("session.status", sessionID=old, status={"type": "busy"})
        wait_for(lambda: self.state() == "working")
        with self.assertRaises(HubError):  # still working: a new session would orphan the running turn
            self.hub.start("a", "again", fresh=True)
        self.fake.emit("session.status", sessionID=old, status={"type": "idle"})
        wait_for(lambda: self.state() == "idle")
        with self.assertRaises(HubError):  # without fresh the name is still taken
            self.hub.start("a", "again")
        self.hub.max_agents = 1  # a new session for the same name is not a new agent
        self.hub.start("a", "again", fresh=True)
        agent = self.hub.agents["a"]
        self.assertNotEqual(agent.session_id, old)
        self.assertEqual((agent.past_sessions, agent.turns, len(self.hub.agents)), ([old], 2, 1))
        self.assertEqual(self.fake.prompts[-1][0], agent.session_id)
        self.assertEqual(self.state(), "starting")
        self.fake.emit("session.status", sessionID=old, status={"type": "busy"})  # a late event from the old session
        time.sleep(0.2)
        self.assertEqual(self.state(), "starting")
        self.fake.emit("session.status", sessionID=agent.session_id, status={"type": "busy"})
        wait_for(lambda: self.state() == "working")
        self.hub.save()
        again = self.make_hub()
        again.load()
        self.assertEqual(again.agents["a"].past_sessions, [old])
        again.close()

    def test_a_slow_subscriber_is_told_events_were_lost(self):
        hub = self.make_hub(max_queue=3)
        q = hub.subscribe()
        with hub.lock:
            for i in range(5):
                hub.emit({"type": "x", "i": i})
        items = []
        while not q.empty():
            items.append(q.get())
        self.assertEqual(items[-1]["type"], "events_lost")
        self.assertNotIn(q, hub.subscribers)
        hub.close()

    def test_wait_returns_on_the_state_and_times_out_otherwise(self):
        sid = self.start()
        with self.assertRaises(HubError):
            self.hub.wait("a", until=["idle"], timeout=0.3)
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        self.fake.emit("session.idle", sessionID=sid)
        self.assertEqual(self.hub.wait("a", until=["idle"], timeout=5)["state"], "idle")

    def test_the_log_is_written_line_by_line(self):
        self.start()
        lines = open(os.path.join(self.tmp.name, "events.jsonl")).read().splitlines()  # not closed yet: must be flushed
        self.assertTrue(any(json.loads(l).get("hub") == "start" for l in lines))


class PolicyTest(unittest.TestCase):
    def test_first_matching_rule_wins_and_regex_must_match_the_whole_target(self):
        p = Policy(RULES, default="ask")
        self.assertEqual(p.decide({"permission": "bash", "metadata": {"command": "ls -la"}})[0], "allow")
        self.assertEqual(p.decide({"permission": "bash", "metadata": {"command": "ls -la; rm -rf /"}})[0], "ask")
        self.assertEqual(p.decide({"permission": "bash", "metadata": {"command": "rm x"}})[:2], ("deny", "no deleting"))
        self.assertEqual(p.decide({"permission": "external_directory", "patterns": ["/tmp/*"], "metadata": {"command": "ls"}})[0], "deny")
        self.assertEqual(p.decide({"permission": "edit", "patterns": ["a.py"]})[0], "ask")

    def test_agent_globs_and_bad_rules(self):
        p = Policy([{"permission": "*", "agent": "trusted-*", "action": "allow"}], default="deny")
        self.assertEqual(p.decide({"permission": "bash", "metadata": {"command": "x"}}, agent="trusted-1")[0], "allow")
        self.assertEqual(p.decide({"permission": "bash", "metadata": {"command": "x"}}, agent="other")[0], "deny")
        with self.assertRaises(PolicyError):
            Policy([{"permission": "bash", "action": "maybe"}])
        with self.assertRaises(PolicyError):
            Policy([{"permission": "bash", "match": "(", "action": "allow"}])

    def test_descriptions_show_the_type_not_just_the_command(self):
        req = {"permission": "external_directory", "patterns": ["/think/*"], "metadata": {"command": "ls"}}
        self.assertEqual(target_of(req), "/think/*")
        self.assertEqual(describe(req), "access outside the project: /think/* (via ls)")
        self.assertEqual(describe({"permission": "bash", "metadata": {"command": "ls"}, "patterns": ["ls"]}), "run ls")


if __name__ == "__main__":
    unittest.main()
