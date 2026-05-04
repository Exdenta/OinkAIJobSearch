#!/usr/bin/env python3
"""Smoke test for forensic.py — rotation, thread-safety, truncation, ctx mgr."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _assert(cond: bool, msg: str) -> None:
    print(("  OK  " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


def test_basic_log_step() -> Path:
    section("1. forensic.log_step writes JSONL")
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)
    os.environ.pop("FORENSIC_FULL", None)
    # Force tight rotation for the rotation test below.
    os.environ["FORENSIC_MAX_BYTES"] = "1024"
    os.environ["FORENSIC_MAX_FIELD_BYTES"] = "256"
    # Reload the module so env takes effect on first import.
    if "forensic" in sys.modules:
        del sys.modules["forensic"]
    import forensic
    forensic.log_step(
        "smoke.basic",
        input={"a": 1, "b": "hello"},
        output={"ok": True},
        chat_id=42,
        run_id=99,
    )
    log_dir = Path(td) / "forensic_logs"
    _assert(log_dir.is_dir(), f"log dir created at {log_dir}")
    files = sorted(log_dir.glob("log.*.jsonl"))
    _assert(len(files) == 1, f"one log file (got {len(files)})")
    line = files[0].read_text().strip()
    _assert(line.endswith("}"), "JSONL line ends with }")
    obj = json.loads(line)
    _assert(obj["op"] == "smoke.basic", "op preserved")
    _assert(obj["chat_id"] == 42, "chat_id preserved")
    _assert(obj["input"] == {"a": 1, "b": "hello"}, "input preserved")
    return log_dir


def test_rotation(log_dir: Path) -> None:
    section("2. rotation when file exceeds max bytes")
    import forensic
    # Tiny max_bytes (1024) — 5 lines with 200-byte payloads should overflow.
    payload = "x" * 150
    for i in range(8):
        forensic.log_step("smoke.rot", input={"i": i, "p": payload})
    files = sorted(log_dir.glob("log.*.jsonl"))
    _assert(len(files) >= 2, f"rotated to ≥2 files (got {len(files)})")
    print(f"    files: {[f.name for f in files]}")


def test_truncation() -> None:
    section("3. truncation respects max-field bytes")
    import forensic
    big = "Y" * 50_000
    forensic.log_step("smoke.trunc", input={"big": big})
    log_dir = Path(os.environ["STATE_DIR"]) / "forensic_logs"
    last_file = max(log_dir.glob("log.*.jsonl"), key=lambda p: int(p.stem.split(".")[1]))
    last_line = last_file.read_text().strip().splitlines()[-1]
    obj = json.loads(last_line)
    inp = obj.get("input")
    # Two acceptable shapes: (a) dict with the big field per-scalar-trimmed,
    # (b) dict-too-big-for-cap fell back to a serialized truncated string.
    if isinstance(inp, dict):
        bigval = inp.get("big") or ""
        _assert(len(bigval) < 50_000, f"dict.big trimmed (got {len(bigval)} chars)")
    elif isinstance(inp, str):
        _assert("truncated" in inp or len(inp) < 50_000,
                f"input fell back to truncated string (len={len(inp)})")
    else:
        _assert(False, f"unexpected input shape {type(inp).__name__}")


def test_step_ctx_happy() -> None:
    section("4. forensic.step ctx writes one line on success")
    import forensic
    log_dir = Path(os.environ["STATE_DIR"]) / "forensic_logs"
    before = sum(1 for f in log_dir.glob("log.*.jsonl")
                 for _ in f.read_text().splitlines())
    with forensic.step("smoke.ctx_ok", input={"x": 1}, chat_id=1) as ctx:
        ctx.set_output({"y": 2})
    after = sum(1 for f in log_dir.glob("log.*.jsonl")
                for _ in f.read_text().splitlines())
    _assert(after == before + 1, f"step wrote +1 line ({before}→{after})")
    last = max(log_dir.glob("log.*.jsonl"), key=lambda p: int(p.stem.split(".")[1])).read_text().splitlines()[-1]
    obj = json.loads(last)
    _assert(obj["op"] == "smoke.ctx_ok", "op preserved")
    _assert(obj["phase"] == "ok", f"phase=ok (got {obj['phase']})")
    _assert(obj["output"] == {"y": 2}, "output preserved")
    _assert("elapsed_ms" in obj, "elapsed_ms present")


def test_step_ctx_error() -> None:
    section("5. forensic.step ctx records error + re-raises")
    # Reset truncation cap to a realistic default — earlier rotation test set
    # it to 256 bytes which compresses the error dict into a string blob.
    os.environ["FORENSIC_MAX_FIELD_BYTES"] = "4096"
    if "forensic" in sys.modules:
        del sys.modules["forensic"]
    import forensic
    log_dir = Path(os.environ["STATE_DIR"]) / "forensic_logs"
    raised = False
    try:
        with forensic.step("smoke.ctx_err", input={"x": 1}):
            raise ValueError("boom")
    except ValueError:
        raised = True
    _assert(raised, "exception re-raised")
    last = max(log_dir.glob("log.*.jsonl"), key=lambda p: int(p.stem.split(".")[1])).read_text().splitlines()[-1]
    obj = json.loads(last)
    _assert(obj["op"] == "smoke.ctx_err", "op preserved on error")
    _assert(obj["phase"] == "error", "phase=error")
    err = obj["error"]
    _assert(isinstance(err, dict), f"error is dict (got {type(err).__name__})")
    _assert(err["class"] == "ValueError", "error class captured")
    _assert("boom" in err["message"], "error message captured")
    _assert(bool(err.get("stack_tail")), "stack_tail captured")


def test_thread_safety() -> None:
    section("6. concurrent appends produce well-formed JSONL")
    import forensic
    N_THREADS = 8
    PER_THREAD = 50

    def worker(tid: int) -> None:
        for i in range(PER_THREAD):
            forensic.log_step("smoke.thread", input={"tid": tid, "i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log_dir = Path(os.environ["STATE_DIR"]) / "forensic_logs"
    rows = 0
    for f in log_dir.glob("log.*.jsonl"):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            json.loads(line)  # raises if malformed
            rows += 1
    print(f"    parsed {rows} lines across {len(list(log_dir.glob('log.*.jsonl')))} files")
    _assert(rows >= N_THREADS * PER_THREAD,
            f"all thread writes captured (≥{N_THREADS*PER_THREAD}, got {rows})")


def test_off_switch() -> None:
    section("7. FORENSIC_OFF=1 disables all writes")
    os.environ["FORENSIC_OFF"] = "1"
    if "forensic" in sys.modules:
        del sys.modules["forensic"]
    import forensic as f2
    log_dir = Path(os.environ["STATE_DIR"]) / "forensic_logs"
    before_files = list(log_dir.glob("log.*.jsonl"))
    sizes_before = sum(p.stat().st_size for p in before_files)
    f2.log_step("smoke.off", input={"silent": True})
    sizes_after = sum(p.stat().st_size for p in log_dir.glob("log.*.jsonl"))
    _assert(sizes_after == sizes_before, "no bytes written when FORENSIC_OFF=1")
    os.environ.pop("FORENSIC_OFF", None)


def main() -> int:
    log_dir = test_basic_log_step()
    test_rotation(log_dir)
    test_truncation()
    test_step_ctx_happy()
    test_step_ctx_error()
    test_thread_safety()
    test_off_switch()
    print("\nPASS — forensic logger smoke green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
