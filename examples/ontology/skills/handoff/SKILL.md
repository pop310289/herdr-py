---
name: handoff
description: Hand the work from a build container to a session on the workstation through git.
---

# handoff

A person (or an agent session on the workstation) does this; there is no script.

1. Commit the files the chain made so far and push the branch.
2. On the workstation, check the branch out and go on with the next step.

Files travel with git. Values that only exist on the old machine, such as a URL of a server it started, do not.
