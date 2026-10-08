"""Moved to herdr_py/codex_agents.py, with herdr-py's other member backends (herdr_py/members.py); this name stays so the
slide team's imports keep working."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from herdr_py.codex_agents import *  # noqa: E402,F401,F403
