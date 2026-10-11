# Team mode example

One task for `herdr-py team` (README: Team mode), with the visible check the supervisor runs and a policy for the
three roles: `exec*` (the executor: edits files, runs `python3 FILE.py`, `ls`, `cat`, `wc`, `mkdir -p`), `ver` (the
verifier: runs the same read-only commands and the tests) and `val` (the validator: reads only). Everything else is
denied.

```sh
cp -r examples/team/count/work ~/team-count        # a fresh copy: the agents edit it
herdr-py serve --opencode http://127.0.0.1:4096 --password-file PASSFILE --policy examples/team/policy.json   # OpenCode serving ~/team-count
herdr-py team examples/team/count/task.json --condition T --workdir ~/team-count --summary ~/team-count.json
```

`task.json` holds `name`, `prompt` and `check` (the command the supervisor runs in the work folder after each stop:
exit code 0 passes). `--condition S` (one agent) and `N` (the same reminder after each stop) run the same task for
comparison.
