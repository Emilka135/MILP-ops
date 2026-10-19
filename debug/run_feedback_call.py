"""Audible post-confirmation feedback through the deployed Rust and real ASR."""
import json
from pathlib import Path
import subprocess
from run_assisted_commands import Acceptance

out = Path("debug/runs/feedback-module-review/real-voice")
run = Acceptance("http://127.0.0.1:8080", "http://127.0.0.1:8080", out, "speech-only", 1.5)
call, state = run.start_and_offer("feedback")
state = run.say(call, "confirm-slot", "Да, подходит.")
assert state["state"] == "assisted.success" and state["vars"]["agent_completed"] is True
state = run.say(call, "confirm-details", "Да, подходит.")
assert state["state"] == "feedback.ask", state
state = run.say(call, "free-feedback", "Пожалуйста, говорите немного медленнее.")
assert state["finished"] and state["status"] == "booked" and state["vars"]["feedback_id"] > 0, state
code = "import sqlite3,json; c=sqlite3.connect('/data/clinic.sqlite'); c.row_factory=sqlite3.Row; print(json.dumps(dict(c.execute('SELECT call_id,registrar,text,kind FROM feedback WHERE id=?',(" + str(state["vars"]["feedback_id"]) + ",)).fetchone()),ensure_ascii=False))"
result = subprocess.run(["docker", "compose", "exec", "-T", "crm", "python", "-c", code], text=True, capture_output=True, check=True)
stored = json.loads(result.stdout)
assert stored["call_id"] == call and stored["registrar"] == "100" and "медленнее" in stored["text"].lower(), stored
report = {"status":"passed", "scope":"Actual local Docker deterministic booking and freely spoken feedback; no external LLM", "events":run.events,"bot_audio":run.bot_audio,"stored_feedback":stored,"final_state":state}
(out/"commands.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
print(json.dumps({"status":"passed","spoken_inputs":sum(e.get("input")=="speech" for e in run.events),"feedback_id":state["vars"]["feedback_id"]}))
