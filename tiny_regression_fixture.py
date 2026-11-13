"""Real controller functions, no project imports; execute only inside admitted Docker."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

P = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("selection_identity", P / "selection_identity.py")
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)
for phase, outcome in (("setup", "passed"), ("call", "passed"), ("teardown", "passed")):
    recorder.pytest_runtest_logreport(SimpleNamespace(nodeid="success", when=phase,
        outcome=outcome, failed=False))
recorder.pytest_runtest_logreport(SimpleNamespace(nodeid="failure", when="call", outcome="failed",
    failed=True, longrepr=SimpleNamespace(reprcrash=SimpleNamespace(message="AssertionError: synthetic"))))
assert recorder.reports["success"] == {"setup": "passed", "call": "passed", "teardown": "passed"}
assert recorder.reports["failure"]["call"] == "failed"
host = ast.parse((P / "host.py").read_text())
loops = [node for node in ast.walk(host) if isinstance(node, ast.For)
         and isinstance(node.iter, ast.Call) and ast.unparse(node.iter).startswith("M['overlay_sha256']")]
assert len(loops) == 1
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder); packet = root / "packet"; runtime = root / "runtime"
    file = packet / "overlay/app/new/path.py"; file.parent.mkdir(parents=True); file.write_bytes(b"synthetic")
    digest = hashlib.sha256(b"synthetic").hexdigest()
    env = {"M": {"overlay_sha256": {"app/new/path.py": digest}}, "P": packet, "R": runtime, "hashlib": hashlib}
    compiled = compile(ast.Module(body=[loops[0]], type_ignores=[]), "actual-overlay-loop", "exec")
    exec(compiled, env)
    assert (runtime / "candidate/app/new/path.py").read_bytes() == b"synthetic"
    env["M"]["overlay_sha256"]["app/new/path.py"] = "0" * 64
    try:
        exec(compiled, env)
    except AssertionError:
        pass
    else:
        raise AssertionError("Bad overlay hash accepted")
import os, re, selectors, signal, subprocess, time

class FakeProcess:
    def __init__(self, stdout=None):
        self.pid, self.returncode, self.stdout, self.waits = 424242, None, stdout, []
    def wait(self, timeout=None):
        self.waits.append(timeout)
        assert timeout is not None, "UNBOUNDED_WAIT"
        raise subprocess.TimeoutExpired("synthetic", timeout)
    def poll(self):
        return None

def extract(name, function):
    found = [n for n in ast.parse((P / name).read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name == function]
    assert len(found) == 1
    return compile(ast.Module(body=found, type_ignores=[]), "actual-" + function, "exec")

for name in ("host.py", "inside.py"):
    for node in ast.walk(ast.parse((P / name).read_text())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "wait":
            assert node.args or any(k.arg == "timeout" for k in node.keywords), f"{name}:{node.lineno}"

signals = []
kill = lambda pid, sig: signals.append(sig)
class LaneFailure(Exception): pass
proc = FakeProcess()
with tempfile.TemporaryDirectory() as folder:
    env = dict(os=SimpleNamespace(environ={"VALIDATION_PHASE_DEADLINE_EPOCH": str(time.time() + 60)}, killpg=kill),
               subprocess=SimpleNamespace(Popen=lambda *a, **k: proc, STDOUT=subprocess.STDOUT,
                                          TimeoutExpired=subprocess.TimeoutExpired),
               signal=signal, time=time, json=json, R=Path(folder), ROOT=Path(folder), ENV={},
               MODE="fixture", STEPS=[], LaneFailure=LaneFailure, HarnessFailure=RuntimeError)
    exec(extract("inside.py", "run"), env)
    try:
        env["run"]("unreaped", ["synthetic"], seconds=0.01)
    except LaneFailure:
        pass
    else:
        raise AssertionError("Unreaped inside timeout accepted")
    row = env["STEPS"][-1]
    assert row["timed_out"] and row["unreaped"] and row["exit"] is None
    assert signals == [signal.SIGTERM, signal.SIGKILL] and None not in proc.waits

signals.clear()
read_end, write_end = os.pipe(); os.close(write_end)
proc = FakeProcess(os.fdopen(read_end, "rb"))
with tempfile.TemporaryDirectory() as folder:
    env = dict(os=SimpleNamespace(environ={}, read=os.read, killpg=kill), selectors=selectors,
               subprocess=SimpleNamespace(Popen=lambda *a, **k: proc, PIPE=subprocess.PIPE,
                                          STDOUT=subprocess.STDOUT, TimeoutExpired=subprocess.TimeoutExpired),
               signal=signal, time=time, re=re, json=json, R=Path(folder), LOGS=[], ACTIVE=None,
               LAST_SAMPLE=0, MISSED_SAMPLES=0, guard=lambda: None,
               StepFailure=type("StepFailure", (Exception,), {}))
    exec(extract("host.py", "command"), env)
    try:
        env["command"]("unreaped", ["synthetic"], 5)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("Unreaped host command accepted")
    row = env["LOGS"][-1]
    assert row["unreaped"] and row["exit"] is None and "UNREAPED_AFTER_SIGKILL" in row["failure"]
    assert signals == [signal.SIGTERM, signal.SIGKILL] and None not in proc.waits
proc.stdout.close()

print(json.dumps({"status": "PASS", "actual_overlay_loop": True, "actual_passive_recorder": True, "bounded_sigkill_reap": True,
    "project_imports": False, "network": "none"}))
