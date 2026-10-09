"""Build a notebook of made-up tasks, to try the notebook without spending on models: every member is a program.

    python3 examples/notebook/make_demo.py /tmp/notebook-demo [--lang zh-TW]      # a few seconds
    python3 -m herdr_py.notebook /tmp/notebook-demo/notebook view --out /tmp/notebook-demo/site --runs /tmp/notebook-demo/runs
    python3 -m herdr_py.notebook /tmp/notebook-demo/notebook serve --runs /tmp/notebook-demo/runs   # live, with buttons

The tasks:
- a museum visitors report (report_task.md): run 1 collects data and writes a skill, then its turns run out; a note
  asks for the page; run 2 goes on from run 1 and builds the page from the data and the skill it carried. It waits
  for a person to review run 2.
- 26 circles in a square (examples/coop): run 2 goes on from run 1; a person picked the best packing and accepted it.
- a calculator in five DAG steps (examples/dag): one step fails twice and blocks another; attached, and on hold.
- a library visitors report, twice over: one brings the museum report's skills as reference material (its "from"),
  the other starts from scratch; the first reaches a full page in fewer member turns (definitions P30).
- a draft that waits for approval, and a request for a new task that waits for Claude to draft it.
Every task, file and name in it is made up.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from herdr_py import notebook  # noqa: E402

PY = sys.executable
BY = "demo person"


def make_page(nb, definition):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(definition, handle, ensure_ascii=False, indent=1)
    try:
        return notebook.draft(nb, handle.name, "claude")
    finally:
        os.remove(handle.name)


def run(nb, pid, **kw):
    out = io.StringIO()
    code = notebook.start_run(nb.page(pid), BY, out=out, **kw)
    print(f"  {pid}: run {nb.page(pid).runs()[-1]['n']} ended with exit {code}", flush=True)
    return code


def main(folder, lang):
    if os.path.exists(folder) and os.listdir(folder):
        sys.exit(f"{folder} is not empty")
    nb_dir, runs = os.path.join(folder, "notebook"), os.path.join(folder, "runs")
    os.makedirs(nb_dir)
    os.makedirs(runs)
    with open(os.path.join(nb_dir, "notebook.json"), "w", encoding="utf-8") as handle:
        json.dump({"title": "Demo notebook" if lang == "en" else "示範筆記本", "lang": lang}, handle)
    nb = notebook.Notebook(nb_dir)
    today = time.strftime("%Y-%m-%d")

    print("museum-report", flush=True)
    members = {"collector": "keeps the visitor numbers", "writer": "writes how-to skills", "builder": "builds the report page"}
    make_page(nb, {
        "id": "museum-report", "title": "Museum visitors report", "icon": "M", "day": today,
        "goal": "a one-page report with a bar chart of the monthly visitors of a made-up museum",
        "asked": "Make a one-page report on the museum's visitors, with a chart.",
        "cwd": HERE, "task": "report_task.md", "judge": [PY, "report_judge.py"],
        "team": {"planner": f"plan=command:{PY} report_planner.py",
                 "members": [f"{m}=command:{PY} report_member.py" for m in members], "about": members},
        "budget": {"turns": 3, "planner_wakes": 4}, "carry": ["data", "skill"], "outputs": ["page"]})
    notebook.approve(nb.page("museum-report"), BY)
    run(nb, "museum-report")
    notebook.add_note(nb.page("museum-report"), "Build the page from the whole year, and let a reader read every bar.", BY)
    run(nb, "museum-report")

    print("circles", flush=True)
    make_page(nb, {
        "id": "circles", "title": "26 circles in a square", "icon": "C", "day": today, "goal": "the largest sum of radii",
        "cwd": ROOT, "task": "examples/coop/packing_task.md", "judge": [PY, "examples/coop/packing_judge.py"],
        "team": {"planner": f"plan=command:{PY} examples/engine/planner.py",
                 "members": [f"a=command:{PY} examples/coop/packing_member.py --seed 1 --steps 2000",
                             f"b=command:{PY} examples/coop/packing_member.py --seed 2 --steps 4000"],
                 "about": {"a": "short searches", "b": "longer searches"}},
        "budget": {"turns": 4, "planner_wakes": 5}})
    notebook.approve(nb.page("circles"), BY)
    run(nb, "circles")
    run(nb, "circles")
    page = nb.page("circles")
    best = max((e for e in page.facts(2)["made"] if e["status"] == "valid"), key=lambda e: e["score"])
    notebook.pick(page, best["id"], BY)
    nb.page("circles").append("accept", BY, why="good enough for now")

    print("calculator (DAG)", flush=True)
    dag = os.path.join(folder, "dag")
    subprocess.run([PY, os.path.join(ROOT, "examples", "dag", "make_demo.py"), dag], check=True, stdout=subprocess.DEVNULL)
    env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.run([PY, "-m", "herdr_py.dag", os.path.join(dag, "plan.json"), "--out", os.path.join(runs, "calculator-1")],
                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    make_page(nb, {"id": "calculator", "title": "A calculator in five steps", "icon": "÷", "day": today, "kind": "dag",
                   "goal": "multiply, subtract and divide added to a calculator that only adds, then the docs"})
    notebook.attach(nb.page("calculator"), os.path.join(runs, "calculator-1"), "claude", note="made by examples/dag/make_demo.py")
    nb.page("calculator").append("hold", BY, why="div keeps failing; decide what to do with it later")

    print("library reports: with the museum's skills, and from scratch", flush=True)
    for pid, icon, title, bring in (("library-report", "L", "Library visitors report (with the museum's skills)",
                                     [{"page": "museum-report", "kinds": ["skill"]}]),
                                    ("library-report-plain", "L0", "Library visitors report (from scratch)", None)):
        d = {"id": pid, "icon": icon, "title": title, "day": today, "goal": "a one-page report with a bar chart of a made-up library's visitors",
             "cwd": HERE, "task": "library_task.md", "judge": [PY, "report_judge.py"],
             "team": {"planner": f"plan=command:{PY} report_planner.py",
                      "members": [f"{m}=command:{PY} report_member.py" for m in members], "about": members},
             "budget": {"turns": 5, "planner_wakes": 6}, "carry": ["data", "skill"], "outputs": ["page"]}
        if bring:
            d["from"] = bring
        make_page(nb, d)
        notebook.approve(nb.page(pid), BY)
        run(nb, pid)

    print("a draft and a request", flush=True)
    make_page(nb, {
        "id": "circles-bigger-team", "title": "26 circles, a bigger team", "icon": "C4", "day": today, "goal": "beat the circles task with four members",
        "cwd": ROOT, "task": "examples/coop/packing_task.md", "judge": [PY, "examples/coop/packing_judge.py"],
        "team": {"planner": f"plan=command:{PY} examples/engine/planner.py",
                 "members": [f"{m}=command:{PY} examples/coop/packing_member.py --seed {i} --steps 30000" for i, m in enumerate("abcd", 1)]},
        "budget": {"turns": 8, "planner_wakes": 9}})
    nb.ask("A reading list for a rainy weekend: five short books, a line on why each.", BY, title="Weekend reading list",
           bring=[{"page": "museum-report", "bring": ["skills"]}])
    print(f"\nnotebook: {nb_dir}\n  python3 -m herdr_py.notebook {nb_dir} view --out {os.path.join(folder, 'site')} --runs {runs}"
          f"\n  python3 -m herdr_py.notebook {nb_dir} serve --runs {runs}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--lang", choices=["en", "zh-TW"], default="en")
    a = ap.parse_args()
    main(os.path.abspath(a.folder), a.lang)
