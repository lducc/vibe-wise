"""End-to-end checks of the VibeWise Pi port over pi --mode rpc.

Calls a real model, so it costs tokens and takes a few minutes.
"""

import json
import queue
import subprocess
import sys
import threading
import time

ASK = ("List every message in your context that mentions VibeWise, quoting its "
       "first sentence. If none, reply NONE. Do not call tools.")
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(name)


class Pi:
    def __init__(self, cwd, *extra):
        self.proc = subprocess.Popen(
            ["pi", "--mode", "rpc", "--approve", *extra], cwd=cwd, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.events = queue.Queue()
        self.log = []
        self.n = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            self.log.append(record)
            self.events.put(record)

    def wait(self, predicate, timeout=240):
        end = time.time() + timeout
        while time.time() < end:
            try:
                record = self.events.get(timeout=1)
            except queue.Empty:
                continue
            if predicate(record):
                return record
        raise TimeoutError("timed out")

    def call(self, kind, **fields):
        self.n += 1
        rid = f"r{self.n}"
        self.proc.stdin.write(json.dumps({"id": rid, "type": kind, **fields}) + "\n")
        self.proc.stdin.flush()
        return self.wait(lambda r: r.get("type") == "response" and r.get("id") == rid)

    def prompt(self, message):
        self.call("prompt", message=message)
        self.wait(lambda r: r.get("type") == "agent_settled")

    def restores(self):
        messages = self.call("get_messages")["data"]["messages"]
        return sum(m.get("customType") == "vibe-wise-restore" for m in messages)

    def reply(self):
        return self.call("get_last_assistant_text")["data"]["text"] or ""

    def sees_restore(self):
        self.prompt(ASK)
        return "VibeWise is active for this project" in self.reply()

    def close(self):
        self.proc.terminate()


# Usage: python3 e2e_rpc.py ACTIVE_PROJECT PAUSED_PROJECT AUTO_COMPACT_PROJECT
# Each project is a git repo with .vibe-wise/profile.md (paused: "Learning mode: paused").
# AUTO_COMPACT_PROJECT also has .pi/settings.json with
# {"compaction": {"keepRecentTokens": 50, "reserveTokens": 100000000}}.
active, paused, auto = sys.argv[1:4]
sessions = "/tmp/vw-suite-sessions"

# Startup in an active project, then commands.
pi = Pi(active, "--session-dir", sessions)
names = [c["name"] for c in pi.call("get_commands")["data"]["commands"]]
check("commands registered", {"vibe-wise:learn", "vibe-wise:reset"} <= set(names))
check("startup restore visible to model", pi.sees_restore())
check("startup restore stored once", pi.restores() == 1, str(pi.restores()))
session_file = pi.call("get_state")["data"].get("sessionFile")

pi.prompt("/vibe-wise:reset Do not follow the skill or call tools. Reply only with "
          "the name attribute of the skill tag you received, or NONE.")
check("/vibe-wise:reset loads skill", pi.reply().strip() == "vibe-wise:reset", pi.reply())
pi.prompt("/vibe-wise:reset Do not follow the skill or call tools. Reply only with the exact "
          "path of reset.py that the skill tells you to run.")
path = pi.reply().strip().strip("`\"")
check("reset path substituted", path.endswith("/skills/reset/reset.py") and "$" not in path, path)

# New session (Claude's clear).
pi.call("new_session")
check("new session restore visible", pi.sees_restore())

# Fork from the first user message of this branch.
forkable = pi.call("get_fork_messages")["data"]["messages"]
pi.call("fork", entryId=forkable[0]["entryId"])
check("fork restore visible", pi.sees_restore())

# Resume the first session from disk.
pi.call("switch_session", sessionPath=session_file)
before = pi.restores()
check("resume restore visible", pi.sees_restore())
check("resume adds exactly one restore", pi.restores() == before + 1, f"{before}->{pi.restores()}")
pi.close()

# Paused project: nothing restored.
pi = Pi(paused, "--no-session")
pi.prompt(ASK)
check("paused project not restored", pi.restores() == 0 and "VibeWise is active" not in pi.reply())
pi.close()

# Auto (threshold) compaction after every run: no extra turn, restore visible after.
# --no-tools keeps the run to one turn; with tools the model re-reads the guides,
# and compaction after every turn would loop exactly as it would in Claude Code.
pi = Pi(auto, "--no-session", "--no-tools")
mark = len(pi.log)
pi.prompt("Reply OK. Do not call tools.")
time.sleep(20)  # any extra, unrequested turn would start within this window
window = [r for r in pi.log[mark:] if r.get("type") not in ("message_update", "message_start", "extension_ui_request")]
runs = sum(r.get("type") == "agent_start" for r in window)
compactions = [i for i, r in enumerate(window) if r.get("type") == "compaction_end" and r.get("result")]
check("auto-compaction ran", bool(compactions), str(len(compactions)))
check("no extra run after auto-compaction", runs == 1, f"agent_start x{runs}")
# Every compaction inside the run must be followed by a restore before the next model turn.
mid_run = []
for i in compactions:
    rest = window[i + 1:]
    next_turn = next((j for j, r in enumerate(rest) if r.get("type") == "turn_start"), None)
    if next_turn is None:
        continue  # post-run compaction: restore rides with the next prompt
    before_turn_end = next((j for j, r in enumerate(rest) if r.get("type") == "turn_end"), len(rest))
    seen = any(r.get("type") == "message_end" and r.get("message", {}).get("customType") == "vibe-wise-restore"
               for r in rest[:before_turn_end])
    mid_run.append(seen)
check("mid-run compactions restored in same run", all(mid_run), f"{sum(mid_run)}/{len(mid_run)} mid-run")
check("restore visible after auto-compaction", pi.sees_restore())
pi.close()

print("FAILURES:", failures or "none")
