"""herdr_py.teamtools: the members' own MCP tools, probed in the sandbox command before they are offered. The
"sandbox" here is this Python itself (the tests' tools are known code); a real run gives a container command."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from herdr_py import teamtools  # noqa: E402
from herdr_py.teamkb import TeamKB  # noqa: E402

# A tool as a member would write it: an MCP server (revision 2026-07-28) with one tool, exiting at end of input.
TOOL = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "server/discover":
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
    elif msg["method"] == "tools/list":
        result = {"resultType": "complete", "tools": [{"name": "NAME", "description": "says hello",
                                                       "inputSchema": {"type": "object"}}]}
    else:
        result = {"resultType": "complete"}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


def ok(score):
    return lambda path: ("valid", score, "ok")


class TeamToolsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.kb = TeamKB(os.path.join(self.dir, "kb"))
        self.tools = teamtools.TeamTools(self.dir, f"{sys.executable} {{file}} {{kb}}", os.path.join(self.dir, "kb"),
                                         os.path.join(self.dir, "board"))

    def add(self, text, judged=ok(10), parents=(), member="maker"):
        eid = self.kb.propose(member, "result", "a tool: " + text[:30], artifact=text, name="t.txt", parents=list(parents))
        self.kb.judge(eid, judged)
        return eid

    def refresh(self):
        return self.tools.refresh(self.kb.entries(), self.kb.folder)

    def test_a_verified_tool_is_probed_once_and_offered(self):
        tool = self.add("ARTIFACT: mcp\n" + TOOL.replace("NAME", "hello"))
        self.add("ARTIFACT: skill\nnot a tool")
        self.add("ARTIFACT: mcp\n" + TOOL.replace("NAME", "never"), judged=lambda path: ("invalid", 0, "no"))
        new = self.refresh()
        self.assertEqual([(t["id"], t["ok"], [x["name"] for x in t["tools"]]) for t in new], [(tool, True, ["hello"])])
        self.assertEqual(self.refresh(), [])  # probed once
        path, allow, lines = self.tools.config()
        self.assertEqual(allow, [f"mcp__team_{tool}"])
        with open(path) as handle:
            server = json.load(handle)["mcpServers"][f"team_{tool}"]
        self.assertEqual([server["command"]] + server["args"],
                         [sys.executable, os.path.join(self.dir, "tools", tool + ".py"), os.path.join(self.dir, "kb")])
        with open(server["args"][0]) as handle:
            self.assertNotIn("ARTIFACT", handle.read())  # the code, without its first line
        self.assertIn(f"mcp__team_{tool}__hello: says hello (made by maker", lines[0])

    def test_a_tool_that_does_not_answer_is_not_offered_and_says_why(self):
        broken = self.add("ARTIFACT: mcp\nimport sys\nsys.exit(3)\n")
        silent = self.add("ARTIFACT: mcp\n" + TOOL.replace('"tools": [{"name": "NAME", "description": "says hello",\n'
                                                           '                                                       "inputSchema": {"type": "object"}}]', '"tools": []'))
        new = {t["id"]: t for t in self.refresh()}
        self.assertEqual((new[broken]["ok"], new[silent]["ok"]), (False, False))
        self.assertIn("exited", new[broken]["why"])
        self.assertIn("no tools", new[silent]["why"])
        self.assertIsNone(self.tools.config())

    def test_only_the_newest_of_a_line_of_tools_is_offered(self):
        old = self.add("ARTIFACT: mcp\n" + TOOL.replace("NAME", "v1"))
        new = self.add("ARTIFACT: mcp\n" + TOOL.replace("NAME", "v2"), parents=[old])
        other = self.add("ARTIFACT: mcp\n" + TOOL.replace("NAME", "other"), member="someone")
        self.refresh()
        self.assertEqual(sorted(t["id"] for t in self.tools.offered()), sorted([new, other]))

    def test_the_sandbox_template_fills_in_the_folders(self):
        t = teamtools.TeamTools(self.dir, "box --mount {kb}:/kb --mount {board}:/board run {file}", "/the/kb", "/the/board")
        self.assertEqual(t.argv("/x/tool.py"), ["box", "--mount", "/the/kb:/kb", "--mount", "/the/board:/board", "run", "/x/tool.py"])


if __name__ == "__main__":
    unittest.main()
