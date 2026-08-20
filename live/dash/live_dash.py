"""Single-market live execution monitor (:8799).

Watched during ONE supervised live cycle or unattended operation.
Lifts proven UI components from the simulation dashboards:
- Level 1: Run-level Strategy KPI tile grid, tooltips, and bell curves from `server/spread_dash_html.py:1525-1567`
- Level 2: Selection funnel (RAW -> FILTERS -> FINAL -> GRADUATED) & refusal cards from `server/fleet_dash.py:1106-1180`
- Level 2: Market drill-down (quotes vs mid, 4 markout horizons, skip events, settlements) from `server/spread_dash.py:598`
- Level 3: Mechanics & system health (latency, reconcile lag, venue errors, 3-way divergences) from `server/spread_dash_html.py:1572`
- Req 4: Exposure over time (unrealized, committed, naked USD) from `server/spread_dash_html.py:1505-1509`
- Req 5: Run selector with multi-run isolation from `server/spread_dash.py:181`

Telemetry only: reads SQLite orders, fills, and reconcile_lock directly
from `live/run/live.db` via read-only URI mode:
`sqlite3.connect('file:<path>?mode=ro', uri=True)`.

Zero venue network calls. Zero credentials needed.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Generator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware

# live/, one level up from live/dash/. Everything this page reads lives under it.
LIVE_ROOT = Path(__file__).resolve().parent.parent
# Launching this file by path (`python live/dash/live_dash.py`) puts live/dash/ on
# sys.path, not live/, so `import engine.kpi` fails at request time with a 500 that
# the live suite never sees -- it runs with live/ as the working directory.
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))
# The ranker writes run/markets.json at the repo root; live/ reads it as data,
# never as code -- the tree boundary is about imports, not about files on disk.
REPO_ROOT = LIVE_ROOT.parent
DEFAULT_PORT = 8799
# The port actually bound. main() overwrites it when --port is given; the status
# payload must report where the page really is, not where it usually is.
_ACTIVE_PORT = DEFAULT_PORT
POLL_INTERVAL_MS = 2000

CYCLE_RING_NAME = "cycle_events.jsonl"
SSE_REPLAY_LINES = 50
SSE_POLL_SEC = 0.5
SSE_KEEPALIVE_SEC = 15.0
SCAN_STALL_THRESHOLD_SEC = 90.0

_ACTIVE_RING_OVERRIDE: Path | None = None
_ACTIVE_HEARTBEAT_OVERRIDE: Path | None = None
_ACTIVE_GUARDRAIL_HB_OVERRIDE: Path | None = None


def set_ring_override(path: Path | str | None) -> None:
    """Point the cycle-stream/scan-state endpoints at a specific ring file (tests)."""
    global _ACTIVE_RING_OVERRIDE
    _ACTIVE_RING_OVERRIDE = Path(path) if path else None


def set_heartbeat_override(path: Path | str | None) -> None:
    """Point scan-state at a specific heartbeat file (tests)."""
    global _ACTIVE_HEARTBEAT_OVERRIDE
    _ACTIVE_HEARTBEAT_OVERRIDE = Path(path) if path else None


def set_guardrail_heartbeat_override(path: Path | str | None) -> None:
    """Point the guardrail-health endpoint at a specific heartbeat file (tests)."""
    global _ACTIVE_GUARDRAIL_HB_OVERRIDE
    _ACTIVE_GUARDRAIL_HB_OVERRIDE = Path(path) if path else None


def resolve_ring_path() -> Path:
    if _ACTIVE_RING_OVERRIDE is not None:
        return _ACTIVE_RING_OVERRIDE
    return LIVE_ROOT / "run" / CYCLE_RING_NAME


def resolve_heartbeat_path() -> Path:
    if _ACTIVE_HEARTBEAT_OVERRIDE is not None:
        return _ACTIVE_HEARTBEAT_OVERRIDE
    return LIVE_ROOT / "run" / "live_poll_heartbeat.json"


def resolve_guardrail_heartbeat_path() -> Path:
    """The watcher's self-report file (live/scripts/guardrail_watch.py)."""
    if _ACTIVE_GUARDRAIL_HB_OVERRIDE is not None:
        return _ACTIVE_GUARDRAIL_HB_OVERRIDE
    return LIVE_ROOT / "run" / "guardrail_watch_heartbeat.json"


def resolve_db_path(custom_path: str | Path | None = None) -> Path:
    """Find the live registry SQLite database path."""
    if custom_path:
        return Path(custom_path)
    env_path = os.environ.get("LIVE_DB_PATH")
    if env_path:
        return Path(env_path)
    return LIVE_ROOT / "run" / "live.db"


def resolve_sweep_interval() -> float | None:
    """Configured account-sweep cadence in seconds, or None for every tick.

    Read from LIVE_SWEEP_INTERVAL so the operator can throttle the venue
    reads the account card depends on without editing code. An absent,
    invalid, or non-positive value falls back to the every-tick default.
    """
    raw = (os.environ.get("LIVE_SWEEP_INTERVAL") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _env_file() -> Path | None:
    """The .env file engine.live_exec loads, found without importing it.

    Mirrors engine.live_exec._find_env_file: the nearest .env walking up from
    live/engine/, stopping at the AGENTS.md boundary. The dashboard never
    loads the whole file -- only LIVE_SWEEP_INTERVAL is read or written -- so
    the signing key and L2 credentials never enter this process.
    """
    curr = LIVE_ROOT / "engine"
    for _ in range(4):
        if (curr / ".env").is_file():
            return curr / ".env"
        if (curr / "AGENTS.md").is_file():
            break
        if curr.parent == curr:
            break
        curr = curr.parent
    return None


def read_sweep_interval_from_env_file(env_path: Path) -> float | None:
    """Parse LIVE_SWEEP_INTERVAL from a .env file, or None.

    Reads the file as text and looks only at that key, so no credential line
    is ever materialised anywhere it could be logged.
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "LIVE_SWEEP_INTERVAL":
            continue
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = float(raw)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def write_sweep_interval_to_env_file(env_path: Path, value: float | None) -> bool:
    """Set or remove LIVE_SWEEP_INTERVAL in .env without disturbing the rest.

    Atomic temp-file + fsync + os.replace, carrying over the file's permission
    bits, because the file may hold POLY_PRIVATE_KEY and must never be
    truncated in place.
    """
    try:
        text = env_path.read_text(encoding="utf-8")
        try:
            mode = os.stat(env_path).st_mode
        except OSError:
            mode = None
    except OSError:
        return False

    lines = [
        ln for ln in text.splitlines()
        if ln.split("=", 1)[0].strip() != "LIVE_SWEEP_INTERVAL"
    ]
    if value is not None:
        lines += [
            "",
            "# Account-sweep cadence for the live dashboard (seconds).",
            f"LIVE_SWEEP_INTERVAL={value:g}",
        ]
    new_text = "\n".join(lines)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"

    tmp = env_path.with_name(f".env.tmp.{secrets.token_hex(4)}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, env_path)
        return True
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def _bootstrap_sweep_interval() -> None:
    """Seed LIVE_SWEEP_INTERVAL from .env once, without loading credentials.

    An explicit environment variable wins; only when it is absent do we read
    the single key back from the file the engine loads. Everything else in
    .env -- the signing key, L2 credentials -- stays on disk.
    """
    if "LIVE_SWEEP_INTERVAL" in os.environ:
        return
    env_file = _env_file()
    if env_file is None:
        return
    saved = read_sweep_interval_from_env_file(env_file)
    if saved is not None:
        os.environ["LIVE_SWEEP_INTERVAL"] = str(saved)


_bootstrap_sweep_interval()


app = FastAPI(title="Spread Hunter Live Monitor")
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Every /api/system/* route changes machine state: start spawns the live
# execution loop that signs real venue requests, reset-db deletes the registry,
# restart-dash ends this process. Loopback binding is not a defence -- a page
# open in the operator's browser can submit a cross-origin form POST to
# 127.0.0.1:8799 with no CORS preflight, and the side effect lands even though
# the attacker cannot read the reply.
#
# The token is generated per process and handed to the page that this process
# serves, so there is nothing for the operator to configure and no state to
# leak between runs. A simple form POST cannot set a custom header, which is
# what makes the header requirement a complete CSRF defence on its own; the
# Origin check is the second lock.
CONTROL_TOKEN = secrets.token_urlsafe(32)
CONTROL_TOKEN_PLACEHOLDER = "__LIVE_DASH_CONTROL_TOKEN__"


def _authorize_control(request: Request) -> None:
    """Reject cross-origin or untokened attempts to change machine state."""
    origin = request.headers.get("origin")
    if origin is not None:
        allowed = {f"http://{request.url.netloc}", f"https://{request.url.netloc}"}
        if origin not in allowed:
            raise HTTPException(status_code=403, detail="cross-origin control request refused")
    if request.headers.get("x-control-token") != CONTROL_TOKEN:
        raise HTTPException(status_code=403, detail="missing or stale control token")

_ACTIVE_DB_OVERRIDE: Path | None = None


def set_db_override(path: Path | str | None) -> None:
    global _ACTIVE_DB_OVERRIDE
    _ACTIVE_DB_OVERRIDE = Path(path) if path else None


@app.get("/api/state")
def get_state():
    """Return JSON state snapshot for the live execution dashboard."""
    from engine.registry_state import summarize_state
    return JSONResponse(summarize_state(resolve_db_path(_ACTIVE_DB_OVERRIDE)))


# How far a process's real creation time may sit from the time we recorded for
# it. The parent writes started_at immediately after Popen, so a genuine match
# is sub-second; this is slack for clock granularity, not for a different
# process. A recycled PID landing inside this window is not a case worth
# engineering around -- a PID that came back within a minute is still the same
# generation of work.
PID_START_TOLERANCE_S: float = 60.0


def _win_process_times(pid: int) -> tuple[float | None, float | None] | None:
    """(created, exited) as Unix timestamps for a Windows PID.

    `exited` is None while the process is still running. It is not always zero
    for a dead one: a process whose parent still holds an open handle stays
    queryable after exit, and only this field distinguishes it from a live one.

    Returns None when the process cannot be opened at all.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    # argtypes and restype are declared, not left to ctypes' defaults. A HANDLE
    # is pointer-sized, and the default `c_int` restype truncates it on 64-bit
    # Windows -- so a handle above 2**31 would come back as a different value,
    # be passed to GetProcessTimes as garbage, and then be closed as garbage.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not k32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def _unix(ft) -> float | None:
            # FILETIME counts 100ns intervals since 1601-01-01; the Unix epoch
            # is 11644473600 seconds later. Zero means "not set".
            ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
            return None if ticks == 0 else ticks / 1e7 - 11644473600.0

        return _unix(creation), _unix(exited)
    finally:
        k32.CloseHandle(handle)


def _process_start_time(pid: int) -> float | None:
    """Unix timestamp the process was created, or None if it cannot be read."""
    try:
        if sys.platform == "win32":
            times = _win_process_times(int(pid))
            return None if times is None else times[0]
        # Linux: field 22 of /proc/<pid>/stat is starttime in clock ticks since
        # boot. The comm field can contain spaces and parentheses, so the split
        # starts after the last ')'.
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = stat[stat.rindex(")") + 2:].split()
        starttime_ticks = float(fields[19])
        hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/stat", encoding="utf-8") as fh:
            btime = next(float(line.split()[1])
                         for line in fh if line.startswith("btime "))
        return btime + starttime_ticks / hz
    except Exception:
        return None


def _is_pid_alive(pid: int | None, started_at: float | None = None) -> bool:
    """Is the process we recorded still running?

    A bare PID check is not enough. The OS recycles PIDs, and on this project it
    did: the bot exited, Windows handed 13052 to msedge, and the dashboard then
    reported the bot stack RUNNING forever -- which permanently refused every
    `Fresh DB` reset and would have refused a legitimate start. When we know
    when the process was supposed to have started, the creation time must agree.

    If the creation time cannot be read (unsupported platform, denied access),
    this falls back to the bare PID check rather than declaring the process
    dead: a false "stopped" would let a second bot stack launch alongside a
    live one, which AGENTS.md forbids outright.
    """
    if not pid or pid <= 0:
        return False
    created: float | None = None
    try:
        if sys.platform == "win32":
            times = _win_process_times(int(pid))
            if times is None:
                return False
            created, exited = times
            if exited is not None:
                # Queryable but finished -- a handle is still open somewhere.
                return False
        else:
            try:
                os.kill(int(pid), 0)
            except PermissionError:
                # EPERM means the process exists but belongs to another user.
                # Letting the bare `except` below turn that into False is the
                # false "stopped" this function exists to avoid -- it would let
                # a second bot stack launch beside a live one.
                pass
    except Exception:
        return False

    if started_at is None:
        return True
    if created is None:
        created = _process_start_time(int(pid))
    if created is None:
        return True
    return abs(created - float(started_at)) <= PID_START_TOLERANCE_S


def get_system_status() -> dict:
    """Return live running status for Supervisor and 4 sub-services (Screener, Engine, Fleet, Telemetry)."""
    procs_file = LIVE_ROOT / "run" / "live_procs.json"
    saved_procs: dict[str, Any] = {}
    if procs_file.exists():
        try:
            saved_procs = json.loads(procs_file.read_text(encoding="utf-8"))
        except Exception:
            saved_procs = {}

    sup_info = saved_procs.get("supervisor", {})
    sup_pid = sup_info.get("pid")
    sup_running = _is_pid_alive(sup_pid, sup_info.get("started_at"))

    scr_info = saved_procs.get("screener", {})
    scr_pid = scr_info.get("pid")
    scr_running = _is_pid_alive(scr_pid, scr_info.get("started_at"))

    eng_info = saved_procs.get("engine", {})
    eng_pid = eng_info.get("pid")
    eng_running = _is_pid_alive(eng_pid, eng_info.get("started_at"))

    fleet_info = saved_procs.get("fleet", {})
    fleet_pid = fleet_info.get("pid")
    fleet_running = _is_pid_alive(fleet_pid, fleet_info.get("started_at"))

    configured_sweep_interval = resolve_sweep_interval()
    running_sweep_interval = eng_info.get("sweep_interval_sec") if eng_running else None

    dash_running = True
    dash_pid = os.getpid()

    bot_running = bool(sup_running or scr_running or eng_running or fleet_running)

    return {
        "supervisor": {
            "name": "Supervisor",
            "running": sup_running,
            "pid": sup_pid if sup_running else None,
            "started_at": sup_info.get("started_at") if sup_running else None,
        },
        "services": {
            "screener": {
                "name": "Screener (rerank)",
                "running": scr_running,
                "pid": scr_pid if scr_running else None,
            },
            "engine": {
                "name": "Engine (sweep/poll)",
                "running": eng_running,
                "pid": eng_pid if eng_running else None,
                "sweep_interval_sec": configured_sweep_interval,
                "running_sweep_interval_sec": running_sweep_interval,
            },
            "fleet": {
                "name": "Fleet (decide/submit)",
                "running": fleet_running,
                "pid": fleet_pid if fleet_running else None,
            },
            "dash": {
                "name": "Telemetry (dash)",
                "running": dash_running,
                "pid": dash_pid,
                "port": _ACTIVE_PORT,
            },
        },
        "bot_state": "RUNNING" if bot_running else "STOPPED",
        "timestamp": time.time(),
    }


def start_bot() -> dict:
    """Launch background Screener and Reconcile loop."""
    import subprocess
    import tempfile

    # One instance at a time (AGENTS.md): two stacks on one database sum their
    # independent inventories into silently invalid data. The page disables the
    # button while RUNNING, but a double click in the poll gap, a reload, or a
    # direct POST all bypass button state -- and live_procs.json only remembers
    # the newest PIDs, so stop_bot could never reach the first pair.
    # Interprocess lock prevents concurrent start_bot calls from racing.
    lock_file = LIVE_ROOT / "run" / ".bot_start.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # Acquire exclusive lock by atomic file creation.
    lock_fd = None
    try:
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(lock_fd, f"{os.getpid()}\n".encode())
    except FileExistsError:
        # Another start_bot call holds the lock; check if it's stale.
        try:
            if lock_file.exists():
                lock_age = time.time() - lock_file.stat().st_mtime
                if lock_age > 30:  # Stale lock from crashed process
                    lock_file.unlink()
                    lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.write(lock_fd, f"{os.getpid()}\n".encode())
                else:
                    return {
                        "ok": False,
                        "message": "Another start_bot request is in progress; refusing concurrent start.",
                        "status": get_system_status(),
                    }
        except Exception:
            return {
                "ok": False,
                "message": "Failed to acquire startup lock; another start may be running.",
                "status": get_system_status(),
            }
    except Exception as e:
        return {
            "ok": False,
            "message": f"Failed to acquire startup lock: {e}",
            "status": get_system_status(),
        }

    launched_procs = []
    try:
        # Re-check status now that we hold the lock.
        current = get_system_status()
        if current["bot_state"] == "RUNNING":
            return {
                "ok": False,
                "message": "Bot stack is already running; refusing to start a second instance.",
                "status": current,
            }

        procs_file = LIVE_ROOT / "run" / "live_procs.json"
        procs_file.parent.mkdir(parents=True, exist_ok=True)

        # Derive a stable run_id so fleet/exec/dash share one session id.
        # Without this, each process generates its own UUID at import time
        # and fills/orders are tagged to inconsistent run_ids, which makes
        # the dashboard default run selector show a misleading zeros grid.
        from engine.order_registry import get_run_id
        child_env = {**os.environ, "SH_RUN_ID": get_run_id()}

        # Launch Screener (rerank_loop)
        p_scr = subprocess.Popen(
            [sys.executable, "-m", "scripts.rerank_loop"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
        )
        launched_procs.append(p_scr)

        # Launch Engine Poll loop (live_exec poll --interval 5). The account sweep
        # follows LIVE_SWEEP_INTERVAL when set; otherwise it runs every tick. Poll
        # owns reconcile, the account sweep, and the markout sampler, and it keeps
        # the registry's open orders fresh for the fleet loop below.
        sweep_interval = resolve_sweep_interval()
        poll_cmd = [sys.executable, "-m", "engine.live_exec", "poll", "--interval", "5"]
        if sweep_interval is not None:
            poll_cmd += ["--sweep-interval", str(sweep_interval)]
        p_eng = subprocess.Popen(
            poll_cmd,
            cwd=str(LIVE_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
        )
        launched_procs.append(p_eng)

        # Launch the Fleet loop (decide -> submit). It reads open orders from the
        # registry rather than re-reconciling, so it runs with --no-reconcile and
        # --no-sweep: a second reconcile loop would contend on the reconcile lock
        # and double the venue reads poll already makes.
        p_fleet = subprocess.Popen(
            [sys.executable, "-m", "engine.live_fleet", "--live",
             "--no-reconcile", "--no-sweep", "--interval", "5"],
            cwd=str(LIVE_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
        )
        launched_procs.append(p_fleet)

        saved_procs = {
            "supervisor": {"pid": p_eng.pid, "started_at": time.time()},
            "screener": {"pid": p_scr.pid, "started_at": time.time()},
            "engine": {"pid": p_eng.pid, "started_at": time.time(),
                       "sweep_interval_sec": sweep_interval},
            "fleet": {"pid": p_fleet.pid, "started_at": time.time()},
        }
        procs_file.write_text(json.dumps(saved_procs, indent=2), encoding="utf-8")

        return {"ok": True, "message": "Bot stack started", "status": get_system_status()}
    except Exception as e:
        # Cleanup: terminate any children launched before the failure.
        for proc in launched_procs:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return {
            "ok": False,
            "message": f"Failed to start bot stack: {e}",
            "status": get_system_status(),
        }
    finally:
        # Release lock on all exit paths.
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass
        try:
            lock_file.unlink()
        except Exception:
            pass


def stop_bot() -> dict:
    """Terminate background Screener and Reconcile loop."""
    import subprocess
    procs_file = LIVE_ROOT / "run" / "live_procs.json"
    if procs_file.exists():
        try:
            saved_procs = json.loads(procs_file.read_text(encoding="utf-8"))
        except Exception:
            saved_procs = {}

        for name, info in saved_procs.items():
            pid = info.get("pid")
            if pid and _is_pid_alive(pid, info.get("started_at")):
                try:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                    else:
                        os.kill(int(pid), 15)
                except Exception:
                    pass
        try:
            procs_file.unlink()
        except Exception:
            pass

    return {"ok": True, "message": "Bot stack stopped", "status": get_system_status()}


def set_sweep_interval(raw: str | None) -> dict:
    """Apply and persist the account-sweep cadence.

    `raw` is None or empty to clear (revert to every tick), otherwise a
    positive number of seconds. Persists to the .env live_exec loads and
    updates this process's environment so the status payload reflects it
    immediately. A running engine keeps its launch-time cadence until the
    bot stack is restarted.
    """
    value: float | None = None
    if raw is not None and str(raw).strip() != "":
        try:
            value = float(str(raw).strip())
        except ValueError:
            return {"ok": False, "message": "sweep interval must be a number of seconds"}
        if value <= 0:
            return {"ok": False, "message": "sweep interval must be positive seconds"}

    env_file = _env_file()
    if env_file is None:
        return {"ok": False, "message": "no .env found; sweep interval was not persisted"}

    if not write_sweep_interval_to_env_file(env_file, value):
        return {"ok": False, "message": f"failed to write {env_file}"}

    if value is None:
        os.environ.pop("LIVE_SWEEP_INTERVAL", None)
    else:
        os.environ["LIVE_SWEEP_INTERVAL"] = str(value)

    return {
        "ok": True,
        "message": "sweep: every tick" if value is None else f"sweep interval set to {value:g}s",
        "sweep_interval_sec": value,
        "status": get_system_status(),
    }


def reset_database(custom_path: str | Path | None = None) -> dict:
    """Safely archive the existing live.db and initialize a fresh, clean database."""
    from engine.order_registry import OrderRegistry
    import shutil
    import datetime

    target_db = resolve_db_path(custom_path or _ACTIVE_DB_OVERRIDE)
    archived_name = None

    # Unlinking the registry under a live writer loses every subsequent write:
    # the engine and screener keep their handles on the old inode while the page
    # reads a new empty file, so the run's telemetry splits across two files and
    # the dashboard reads empty for a bot that is still trading.
    if get_system_status()["bot_state"] == "RUNNING":
        return {
            "ok": False,
            "message": "Refusing to reset while the bot stack is running. Stop the bot first.",
            "archived_to": None,
            "db_path": str(target_db),
        }

    # This function archives-then-deletes whatever it is pointed at. Launched
    # with --db against an archived cycle for a post-mortem, an unguarded reset
    # would destroy the very record the operator opened the page to read -- and
    # nest a new archive/ inside the archive directory on the way out.
    if any(part.lower() == "archive" for part in target_db.resolve().parts):
        return {
            "ok": False,
            "message": (
                f"Refusing to reset {target_db.name}: it is an archived run, "
                "opened for reading. Archives are history and are never reset."
            ),
            "archived_to": None,
            "db_path": str(target_db),
        }

    if target_db.exists() and target_db.stat().st_size > 0:
        archive_dir = target_db.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"live_{ts_str}.db"
        shutil.copy2(target_db, archive_path)
        archived_name = archive_path.name
        try:
            target_db.unlink()
        except Exception:
            pass
        for extra in (f"{target_db}-wal", f"{target_db}-shm"):
            try:
                Path(extra).unlink(missing_ok=True)
            except Exception:
                pass

    # Initialize fresh database with all tables and schema
    reg = OrderRegistry(target_db)

    return {
        "ok": True,
        "message": f"Created fresh database at {target_db.name}" + (f" (archived previous to {archived_name})" if archived_name else ""),
        "archived_to": archived_name,
        "db_path": str(target_db),
    }


@app.get("/api/system/status")
def api_system_status():
    """Return process states for supervisor and 3 sub-services."""
    return JSONResponse(get_system_status())


@app.post("/api/system/start")
def api_system_start(request: Request):
    """Start background bot stack."""
    _authorize_control(request)
    return JSONResponse(start_bot())


@app.post("/api/system/stop")
def api_system_stop(request: Request):
    """Stop background bot stack."""
    _authorize_control(request)
    return JSONResponse(stop_bot())


@app.post("/api/system/sweep-interval")
def api_system_set_sweep_interval(request: Request, seconds: str | None = None):
    """Set or clear the account-sweep cadence and persist it to .env."""
    _authorize_control(request)
    return JSONResponse(set_sweep_interval(seconds))


@app.post("/api/system/reset-db")
def api_system_reset_db(request: Request):
    """Archive current database and initialize a fresh clean live.db."""
    _authorize_control(request)
    return JSONResponse(reset_database())


@app.post("/api/system/venue-sync")
def api_system_venue_sync(request: Request):
    """Trigger a one-time venue reconciliation.
    Reads the account from Polymarket and backfills closes/float_marks.
    Read-only at the venue; no exposure is opened or increased."""
    _authorize_control(request)
    from engine.live_exec import venue_sync
    db_path = resolve_db_path(_ACTIVE_DB_OVERRIDE)
    return JSONResponse(venue_sync(db_path=db_path, quiet=False))


def relaunch_argv() -> list[str]:
    """Build the command that starts a replacement dashboard process.

    The script path must be absolute. `sys.argv[0]` is whatever the operator
    typed, and .claude/launch.json types it relative ("live/dash/live_dash.py");
    replaying that under cwd=LIVE_ROOT would look for live/live/dash/live_dash.py,
    so the replacement would die on startup -- after the current instance has
    already called os._exit(0), leaving no dashboard at all.

    Everything after argv[0] is carried through, so --port and --db survive a
    restart and the page comes back on the same port against the same database.
    """
    return [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]


@app.post("/api/system/restart-dash")
def api_system_restart_dash(request: Request):
    """Restart only the dashboard web server process without touching engine/screener workers."""
    _authorize_control(request)
    import threading
    import subprocess

    def _do_restart():
        time.sleep(0.8)
        # Launch detached replacement dashboard process.
        subprocess.Popen(relaunch_argv(), cwd=str(LIVE_ROOT))
        # Exit current instance to immediately release port 8799. Engine and
        # screener are separate processes and keep running; they are only
        # orphaned, never signalled.
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Dashboard server restarting..."})


@app.get("/api/kpi")
def get_kpi(run_id: str | None = None):
    """Return live KPI report mirroring strategy/kpi.py with Level 1/2/3 diagnostics."""
    from engine.kpi import report as generate_kpi_report
    db_path = resolve_db_path(_ACTIVE_DB_OVERRIDE)
    try:
        data = generate_kpi_report(db_path=db_path, run_id=run_id)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def compute_scan_state(
    last_event_ts: Optional[float],
    hb_ts: Optional[float],
    now: float,
    active_phases: set[str],
    stall_threshold: float = SCAN_STALL_THRESHOLD_SEC,
) -> tuple[str, Optional[float]]:
    """Classify the fleet as SCANNING, IDLE, or STALLED.

    STALLED  -- the engine heartbeat has not advanced within `stall_threshold`
                (or is absent entirely): a real alarm, not an empty table.
    SCANNING -- heartbeat fresh AND some service did active-phase work
                (scanning/filtering/quoting/settling) in the recent window.
    IDLE     -- heartbeat fresh but no active-phase work in the window.
    """
    age = None
    if hb_ts is not None:
        age = max(0.0, now - hb_ts)
    if hb_ts is None or (age is not None and age > stall_threshold):
        return "STALLED", age
    if active_phases & {"scanning", "filtering", "quoting", "settling"}:
        return "SCANNING", age
    return "IDLE", age


def _parse_event_ts(ts: Any) -> Optional[float]:
    """Parse an ISO-8601 ring timestamp to a Unix timestamp, or None."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _last_per_service(events: list[dict]) -> dict[str, tuple[str, Optional[float]]]:
    """Latest (phase, unix ts) per service from ring events."""
    out: dict[str, tuple[str, Optional[float]]] = {}
    for ev in events:
        svc = str(ev.get("service") or "engine")
        ts = _parse_event_ts(ev.get("ts"))
        if svc not in out or (
            ts is not None and (out[svc][1] is None or ts > out[svc][1])
        ):
            out[svc] = (str(ev.get("phase") or ""), ts)
    return out


def _read_engine_heartbeat() -> dict[str, Any]:
    """Read live/run/live_poll_heartbeat.json, returning {} when absent/invalid."""
    try:
        data = json.loads(resolve_heartbeat_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, list) and data and isinstance(data[-1], dict):
        return data[-1]
    return {}


def _read_guardrail_heartbeat() -> dict[str, Any]:
    """Read the guardrail watcher's self-report, {} when absent/invalid."""
    try:
        data = json.loads(resolve_guardrail_heartbeat_path().read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, list) and data and isinstance(data[-1], dict):
        return data[-1]
    return {}


def _guardrail_health() -> dict[str, Any]:
    """Watcher liveness (heartbeat) + alert totals (ring), one payload.

    Liveness: the watcher writes guardrail_watch_heartbeat.json every check
    (~5s). A heartbeat older than STALE_THRESHOLD_SEC means the watcher is
    down or dead -- the silent failure this endpoint exists to surface.
    Alerts: counted from `guardrail_alert` ring events, so the total survives
    watcher restarts (the heartbeat itself is per-process).
    """
    hb = _read_guardrail_heartbeat()
    ts = hb.get("ts")
    age_s = None
    if isinstance(ts, str):
        try:
            dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            age_s = (datetime.datetime.now(datetime.timezone.utc) - dt.replace(
                tzinfo=datetime.timezone.utc)).total_seconds()
        except ValueError:
            age_s = None

    ring_alerts = []
    try:
        from engine.cycle_stream import read_ring
        for ev in read_ring(resolve_ring_path(), tail=400):
            if ev.get("action") == "guardrail_alert":
                ring_alerts.append(ev)
    except Exception:
        pass
    ring_alerts.sort(key=lambda a: str(a.get("ts") or ""), reverse=True)
    newest = ring_alerts[0] if ring_alerts else {}

    return {
        "pid": hb.get("pid"),
        "started_at": hb.get("started_at"),
        "last_ts": ts,
        "cycle": hb.get("cycle"),
        "running": bool(hb) and age_s is not None and age_s <= STALE_THRESHOLD_SEC,
        "age_s": age_s,
        "alerts_total": len(ring_alerts),
        "last_alert_ts": newest.get("ts"),
        "last_alert_kind": newest.get("reason"),
    }


def _read_cycle_intent_rows(db_path: Path | str, limit: int = 200) -> list[dict]:
    """Last `limit` cycle_intent rows in read-only mode; [] when unavailable."""
    path = Path(db_path)
    if not path.exists():
        return []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT ts, cycle, market_slug, top_skip_reason, top_pass_reason, "
            "intent_count, submitted, cancelled FROM cycle_intent "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@app.get("/api/scan-state")
def get_scan_state():
    """SCANNING / IDLE / STALLED plus per-cycle skip/pass rationale (read-only)."""
    now = time.time()
    events = []
    try:
        from engine.cycle_stream import read_ring
        events = read_ring(resolve_ring_path(), tail=400)
    except Exception:
        events = []

    hb = _read_engine_heartbeat()
    hb_ts = (hb.get("ts") or 0) / 1000.0 if hb.get("ts") else None

    window = now - 60.0
    active_phases: set[str] = set()
    last_event_ts: Optional[float] = None
    last_scan_ts: Optional[float] = None
    for ev in events:
        ts = _parse_event_ts(ev.get("ts"))
        if ts is None:
            continue
        if last_event_ts is None or ts > last_event_ts:
            last_event_ts = ts
        if ts >= window:
            active_phases.add(str(ev.get("phase") or ""))
        if str(ev.get("service") or "") == "screener" and (
            last_scan_ts is None or ts > last_scan_ts
        ):
            last_scan_ts = ts

    state, hb_age = compute_scan_state(last_event_ts, hb_ts, now, active_phases)

    rows = _read_cycle_intent_rows(resolve_db_path(_ACTIVE_DB_OVERRIDE))
    skip_counts: dict[str, int] = {}
    pass_counts: dict[str, int] = {}
    for r in rows:
        sk = r.get("top_skip_reason")
        pk = r.get("top_pass_reason")
        if sk:
            skip_counts[sk] = skip_counts.get(sk, 0) + 1
        if pk:
            pass_counts[pk] = pass_counts.get(pk, 0) + 1

    return JSONResponse({
        "scan_state": state,
        "seconds_since_heartbeat": round(hb_age, 1) if hb_age is not None else None,
        "seconds_since_scan": (
            round(max(0.0, now - last_scan_ts), 1) if last_scan_ts is not None else None
        ),
        "last_scan_ts": last_scan_ts,
        "services": {
            svc: {"phase": phase, "last_ts": ts}
            for svc, (phase, ts) in _last_per_service(events).items()
        },
        "decisions_logged": len(rows),
        "skip_reasons": sorted(
            [{"reason": k, "count": v} for k, v in skip_counts.items()],
            key=lambda x: -x["count"],
        ),
        "pass_reasons": sorted(
            [{"reason": k, "count": v} for k, v in pass_counts.items()],
            key=lambda x: -x["count"],
        ),
    })


def _ring_file_key(st: Any) -> tuple:
    """Identity that changes when the engine rotates the ring via os.replace.

    st_ino distinguishes the replaced inode on POSIX; on Windows st_ino is 0,
    so fall back to creation time (st_ctime_ns), which os.replace changes.
    """
    if getattr(st, "st_ino", 0):
        return (st.st_dev, st.st_ino)
    return (st.st_dev, st.st_ctime_ns)


def _cycle_stream_sse(
    ring_path: Path,
    tail: int = SSE_REPLAY_LINES,
    poll_sec: float = SSE_POLL_SEC,
) -> Generator[str, None, None]:
    """Yield SSE frames for the cycle-telemetry ring: replay tail, then follow appends.

    The engine rotates the ring past 500 lines by atomically replacing the file.
    Replacement is detected by file identity (inode on POSIX, creation time on
    Windows) rather than size alone, so a replacement larger than the current
    read offset is still seen. On rotation we emit an ``event: rotate`` frame
    and re-sync from the new file's start.
    """
    last_keepalive = time.time()

    def _frame(line: str) -> str:
        return f"data: {line.strip()}\n\n"

    offset = 0
    file_key = None
    if ring_path.exists():
        try:
            with open(ring_path, "r", encoding="utf-8", errors="replace") as fh:
                tail_lines = fh.readlines()[-tail:]
                # Position actually consumed, not a later stat: an append in the
                # read-to-stat gap must not be silently skipped.
                offset = fh.tell()
                file_key = _ring_file_key(os.fstat(fh.fileno()))
            for line in tail_lines:
                if line.strip():
                    yield _frame(line)
        except OSError:
            pass

    while True:
        try:
            if not ring_path.exists():
                time.sleep(poll_sec)
                continue
            st = ring_path.stat()
            key = _ring_file_key(st)
            size = st.st_size
            if file_key is not None and (key != file_key or size < offset):
                offset = 0
                yield "event: rotate\ndata: {}\n\n"
            file_key = key
            if size > offset:
                with open(ring_path, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    for line in fh:
                        if line.strip():
                            yield _frame(line)
                    offset = fh.tell()
            if time.time() - last_keepalive >= SSE_KEEPALIVE_SEC:
                yield ": keepalive\n\n"
                last_keepalive = time.time()
            time.sleep(poll_sec)
        except OSError:
            time.sleep(poll_sec)


PAIRS_ACTION_PREFIX = "pairs_"


@app.get("/api/pairs-activity")
def pairs_activity():
    """Aggregate U35 auto-pairs activity from the cycle ring.

    Counts every pairs_* action (completed/exited/would_complete/would_exit/
    hold/balanced/error) overall and per latest cycle, plus each pair's most
    recent action with its timestamp. Read-only; the ring is the source.
    """
    from engine.cycle_stream import read_ring
    events = read_ring(resolve_ring_path(), tail=400)
    totals: dict[str, int] = {}
    per_cycle: dict[int, dict[str, int]] = {}
    per_pair: dict[str, dict] = {}
    for ev in events:
        a = str(ev.get("action") or "")
        if not a.startswith(PAIRS_ACTION_PREFIX):
            continue
        action = a[len(PAIRS_ACTION_PREFIX):]
        totals[action] = totals.get(action, 0) + 1
        cycle = ev.get("cycle")
        if cycle is not None:
            pc = per_cycle.setdefault(int(cycle), {})
            pc[action] = pc.get(action, 0) + 1
        pid = (ev.get("extra") or {}).get("pair_id")
        if pid:
            per_pair[str(pid)] = {
                "action": action,
                "ts": ev.get("ts"),
                "cycle": ev.get("cycle"),
            }
    last_cycle = max(per_cycle) if per_cycle else None
    return {
        "totals": totals,
        "last_cycle": last_cycle,
        "last_cycle_counts": per_cycle.get(last_cycle, {})
        if last_cycle is not None else {},
        "per_pair": [
            {"pair_id": pid, **info}
            for pid, info in sorted(per_pair.items())
        ],
    }


@app.get("/api/guardrail-alerts")
def guardrail_alerts():
    """Active guardrail violations as a visible banner payload.

    Reads the cycle ring for `guardrail_alert` events (emitted by
    live/scripts/guardrail_watch.py on a repeated pair exit or an over-cap
    pair) and returns them newest-first. The dashboard renders the most
    recent one as a red banner so a violation is visible, not just a log
    line. Read-only; the ring is the source.
    """
    from engine.cycle_stream import read_ring
    events = read_ring(resolve_ring_path(), tail=400)
    alerts = []
    for ev in events:
        if ev.get("action") != "guardrail_alert":
            continue
        alerts.append({
            "ts": ev.get("ts"),
            "cycle": ev.get("cycle"),
            "kind": ev.get("reason"),
            "subject": (ev.get("extra") or {}).get("subject"),
            "detail": (ev.get("extra") or {}).get("detail"),
        })
    alerts.sort(key=lambda a: str(a["ts"] or ""), reverse=True)
    return {"alerts": alerts}


@app.get("/api/guardrail-health")
def guardrail_health():
    """Watcher health: running pid, restart time, alert count.

    Reads the watcher's self-report heartbeat (guardrail_watch_heartbeat.json,
    written every check) for liveness and the ring for the cumulative alert
    count, so a dead watcher is visible on the dashboard instead of failing
    silently. Read-only; both files are sources of truth.
    """
    return _guardrail_health()


@app.get("/api/cycle-stream")
def cycle_stream_events():
    """Server-Sent-Events tail of live/run/cycle_events.jsonl."""
    return StreamingResponse(
        _cycle_stream_sse(resolve_ring_path()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Spread Hunter — Live Cycle Monitor</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #080c14;
      --bg-surface: #0f172a;
      --bg-surface-raised: #1e293b;
      --bg-card: rgba(15, 23, 42, 0.65);
      --border-subtle: rgba(255, 255, 255, 0.08);
      --border-strong: rgba(255, 255, 255, 0.16);
      --text-primary: #f8fafc;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --signal: #10b981;
      --loss: #ef4444;
      --open: #38bdf8;
      --warn: #f59e0b;
      --gold: #fbbf24;
      
      --red-alert: #ef4444;
      --red-bg: rgba(127, 29, 29, 0.45);
      --red-border: #dc2626;
      --red-glow: 0 0 40px rgba(239, 68, 68, 0.35);

      --green-ok: #10b981;
      --green-bg: rgba(6, 78, 59, 0.35);
      --green-border: #059669;
      --green-glow: 0 0 30px rgba(16, 185, 129, 0.25);

      --blue-rest: #38bdf8;
      --blue-bg: rgba(12, 74, 110, 0.35);
      --blue-border: #0284c7;

      --amber-warn: #f59e0b;
      --amber-bg: rgba(120, 53, 15, 0.35);
      --amber-border: #d97706;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: linear-gradient(180deg, #080c14 0%, #0b111e 100%) fixed;
      color: var(--text-primary);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      min-height: 100vh;
      padding: 20px 20px 76px;
      line-height: 1.5;
    }

    .font-display { font-family: 'Big Shoulders Display', Impact, sans-serif; letter-spacing: 0.02em; font-weight: 800; }
    .mono { font-family: 'JetBrains Mono', ui-monospace, monospace; font-variant-numeric: tabular-nums; }

    .container {
      max-width: 1380px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }

    /* Top Bar */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 20px;
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      backdrop-filter: blur(8px);
    }

    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-icon { font-size: 24px; }
    .brand-title { font-size: 18px; font-weight: 800; letter-spacing: -0.02em; }
    .brand-sub { font-size: 11px; color: var(--text-secondary); font-family: 'JetBrains Mono', monospace; }

    .top-meta {
      display: flex;
      align-items: center;
      gap: 12px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
    }

    .run-select-wrap {
      display: flex;
      align-items: center;
      gap: 6px;
      background: var(--bg-surface-raised);
      border: 1px solid var(--border-subtle);
      padding: 4px 8px;
      border-radius: 6px;
    }
    .run-select-wrap select {
      background: transparent;
      color: var(--text-primary);
      border: none;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      font-weight: 600;
      outline: none;
      cursor: pointer;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 9999px;
      font-weight: 600;
      font-size: 11px;
    }
    .pill-fresh { background: var(--green-bg); color: #34d399; border: 1px solid var(--green-border); }
    .pill-stale { background: var(--red-bg); color: #fca5a5; border: 1px solid var(--red-border); animation: pulse 1.5s infinite; }
    /* BOT BRAINS (live decision flow via SSE) */
    .bot-brains { border-left: 3px solid rgba(56,189,248,0.4); }
    .bot-brains-grid { display:flex; flex-wrap:wrap; gap:14px; align-items:stretch; }
    .bb-col { background:rgba(15,23,42,0.5); border:1px solid rgba(148,163,184,0.12); border-radius:8px; padding:10px 12px; }
    .bb-pairs-label { font-size:10px; color:#94a3b8; text-transform:uppercase; letter-spacing:.05em; }
    .bb-pairs-counts { font-family:'JetBrains Mono',monospace; font-size:11px; margin-top:6px; line-height:1.6; }
    .bb-pairs-counts .pa-err { color:#f87171; }
    .bb-pairs-last { margin-top:6px; font-family:'JetBrains Mono',monospace; font-size:10px; color:#94a3b8; line-height:1.5; max-height:66px; overflow-y:auto; }
    .bb-pairs-last .pa-ok { color:#10b981; }
    .bb-pairs-last .pa-err { color:#f87171; }
    .bb-pairs-last .pa-mute { color:#64748b; }
    .bb-status { display:flex; align-items:center; gap:10px; min-width:190px; }
    .bb-status-dot { width:14px; height:14px; border-radius:50%; background:#64748b; flex:0 0 auto; }
    .bb-status-dot.scanning { background:#10b981; animation: bbPulse 1.2s infinite; }
    .bb-status-dot.idle { background:#94a3b8; animation: bbBreathe 3.2s ease-in-out infinite; }
    .bb-status-dot.stalled { background:#ef4444; animation: bbPulse 0.8s infinite; }
    .bb-scan-state { font-family:'JetBrains Mono',monospace; font-size:15px; font-weight:700; }
    .bb-scan-state.scanning { color:#10b981; }
    .bb-scan-state.idle { color:#94a3b8; }
    .bb-scan-state.stalled { color:#ef4444; }
    .bb-scan-sub { font-size:11px; color:var(--text-muted); }
    .bb-pills { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .bb-pill { font-family:'JetBrains Mono',monospace; font-size:11px; padding:4px 8px; border-radius:999px; border:1px solid rgba(148,163,184,0.2); color:var(--text-secondary); white-space:nowrap; }
    .bb-pill.busy { border-color:rgba(56,189,248,0.6); color:#7dd3fc; animation: bbPulse 1.1s infinite; }
    .bb-funnel { flex:1 1 220px; }
    .bb-funnel-label { font-size:11px; color:var(--text-muted); margin-bottom:6px; }
    .bb-funnel-bar { height:10px; background:rgba(30,41,59,0.8); border-radius:6px; overflow:hidden; }
    .bb-funnel-fill { height:100%; width:0%; background:linear-gradient(90deg,#0ea5e9,#10b981); transition:width 0.6s ease; }
    .bb-funnel-skips { font-size:10px; color:#fbbf24; margin-top:6px; min-height:14px; }
    .bb-spark { flex:0 1 220px; }
    .bb-spark-label { font-size:11px; color:var(--text-muted); margin-bottom:4px; }
    .bb-log { margin-top:12px; font-family:'JetBrains Mono',monospace; font-size:11px; line-height:1.5; max-height:180px; overflow-y:auto; background:rgba(2,6,23,0.6); border:1px solid rgba(148,163,184,0.1); border-radius:8px; padding:8px 10px; }
    .bb-log-line { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .bb-log-ts { color:#64748b; }
    .bb-log-ms { color:#64748b; }
    .bb-log-line .ph-scanning { color:#38bdf8; }
    .bb-log-line .ph-filtering { color:#a78bfa; }
    .bb-log-line .ph-quoting { color:#10b981; }
    .bb-log-line .ph-settling { color:#f59e0b; }
    .bb-log-line .ph-idle { color:#94a3b8; }
    .bb-log-line .ph-waiting { color:#64748b; }
    .bb-log-line .ph-reconciling { color:#f472b6; }
    .kpi-glow { box-shadow: 0 0 14px rgba(56,189,248,0.55); }
    @keyframes bbPulse { 0%,100% { opacity:1; } 50% { opacity:0.35; } }
    @keyframes bbBreathe { 0%,100% { transform:scale(1); opacity:0.6; } 50% { transform:scale(1.5); opacity:1; } }
    .pill-neutral { background: var(--bg-surface-raised); color: var(--text-secondary); border: 1px solid var(--border-subtle); }

    /* Portfolio Overview (run-level, all markets) */
    .portfolio-card {
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 18px;
      position: relative;
      overflow: hidden;
    }
    .portfolio-card::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
      background: var(--signal);
    }
    .portfolio-tiles {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
    }
    .pf-tile {
      background: var(--bg-base);
      border: 1px solid var(--border-subtle);
      border-radius: 10px;
      padding: 14px;
      display: flex; flex-direction: column; justify-content: space-between; gap: 8px;
    }
    .pf-tile-primary { border-color: rgba(16, 185, 129, 0.35); }
    .pf-label {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase;
      color: var(--text-secondary); font-weight: 600;
    }
    .pf-tile-primary .pf-label { color: var(--signal); }
    .pf-value-huge { font-size: 34px; font-weight: 800; letter-spacing: -0.02em; line-height: 1.1; }
    .pf-value { font-size: 24px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.15; }
    .pf-foot {
      display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
      border-top: 1px solid var(--border-subtle); padding-top: 8px;
    }
    .pf-chip {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px; font-weight: 700;
      padding: 2px 7px; border-radius: 5px; border: 1px solid transparent;
    }
    .pf-foot-note { font-size: 11px; color: var(--text-muted); }
    .pf-src {
      font-size: 9px; letter-spacing: 0.08em; padding: 1px 5px; border-radius: 3px;
      background: rgba(245,158,11,0.15); border: 1px solid rgba(245,158,11,0.35);
      color: var(--warn); font-weight: 700; margin-left: 4px;
    }
    .pf-chart { margin-top: 14px; }
    .pf-chart svg { width: 100%; height: auto; display: block; }
    .pf-empty {
      padding: 26px; text-align: center;
      font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-muted);
      border: 1px dashed var(--border-subtle); border-radius: 10px;
    }
    a.mkt-name-link { color: #38bdf8; text-decoration: none; font-weight: 700; }
    a.mkt-name-link:hover { text-decoration: underline; }
    .cat-tag {
      display: inline-block; font-family: 'JetBrains Mono', monospace;
      font-size: 10px; font-weight: 600; letter-spacing: 0.06em;
      padding: 2px 6px; border-radius: 4px;
      background: var(--bg-surface-raised); border: 1px solid var(--border-subtle);
      color: var(--text-secondary);
    }

    /* Fixed status footer: backend indicators always on screen */
    .footer-bar {
      position: fixed;
      left: 0; right: 0; bottom: 0;
      z-index: 1000;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
      padding: 8px 18px;
      background: rgba(8, 12, 20, 0.92);
      border-top: 1px solid var(--border-subtle);
      backdrop-filter: blur(10px);
      box-shadow: 0 -8px 24px rgba(0, 0, 0, 0.4);
    }
    .footer-bar .sup-card { padding: 4px 12px; }
    .footer-sup-title {
      font-size: 11px; font-weight: 800; letter-spacing: 0.02em; color: var(--text-primary);
      white-space: nowrap;
    }
    .footer-sup-sub { font-size: 10px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; white-space: nowrap; }
    .footer-meta {
      display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
      font-family: 'JetBrains Mono', monospace;
    }
    .footer-lock {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 10px; border-radius: 9999px;
      background: var(--bg-surface-raised); border: 1px solid var(--border-subtle);
      font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 700;
      cursor: help;
    }
    .footer-lock.lock-idle { color: var(--green-ok); }
    .footer-lock.lock-active { color: var(--amber-warn); border-color: rgba(245, 158, 11, 0.5); }
    .dot-amber { background-color: #f59e0b; box-shadow: 0 0 8px #f59e0b; }
    .footer-clock {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px; font-weight: 700; color: var(--text-secondary);
      font-variant-numeric: tabular-nums;
      min-width: 64px; text-align: right;
    }

    @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.9; transform: scale(0.995); } }
    @keyframes flashBg { 0%, 100% { background-color: rgba(127, 29, 29, 0.7); } 50% { background-color: rgba(185, 28, 28, 0.95); } }

    /* HERO SECTION: HEDGE STATE */
    .hero-card {
      border-radius: 14px;
      padding: 24px 28px;
      background: var(--bg-surface);
      border: 2px solid var(--border-strong);
      transition: all 0.3s ease;
    }
    .hero-card.state-naked {
      background: linear-gradient(145deg, rgba(80, 10, 10, 0.95), rgba(30, 5, 5, 0.95));
      border: 3px solid var(--red-alert);
      box-shadow: var(--red-glow);
      animation: flashBg 2s infinite ease-in-out;
    }
    .hero-card.state-balanced {
      background: linear-gradient(145deg, rgba(6, 60, 45, 0.85), rgba(6, 30, 25, 0.85));
      border: 2px solid var(--green-ok);
      box-shadow: var(--green-glow);
    }
    .hero-card.state-resting {
      background: linear-gradient(145deg, rgba(15, 30, 60, 0.85), rgba(10, 20, 40, 0.85));
      border: 2px solid var(--blue-border);
    }
    .hero-card.state-closed { background: var(--bg-surface); border: 1px solid var(--border-subtle); }
    .hero-card.state-empty { background: var(--bg-surface); border: 1px dashed var(--border-strong); text-align: center; padding: 36px 20px; }

    .hero-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
    .hero-badge {
      display: inline-flex; align-items: center; gap: 8px;
      font-size: 13px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase;
      padding: 5px 12px; border-radius: 6px; font-family: 'JetBrains Mono', monospace;
    }
    .hero-badge.naked { background: #ef4444; color: #fff; }
    .hero-badge.balanced { background: #10b981; color: #fff; }
    .hero-badge.resting { background: #0284c7; color: #fff; }
    .hero-badge.closed { background: #475569; color: #f1f5f9; }

    .hero-headline { font-size: 28px; font-weight: 900; letter-spacing: -0.02em; margin-top: 6px; }
    .hero-desc { font-size: 14px; color: var(--text-secondary); margin-top: 4px; }

    .naked-metrics {
      display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 18px;
      padding: 16px; background: rgba(0, 0, 0, 0.4); border-radius: 10px; border: 1px solid rgba(239, 68, 68, 0.4);
    }
    .metric-block { display: flex; flex-direction: column; gap: 4px; }
    .metric-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: #fca5a5; font-family: 'JetBrains Mono', monospace; }
    .metric-val-huge { font-size: 32px; font-weight: 900; color: #fff; font-family: 'JetBrains Mono', monospace; }
    .timer-val { color: #fef08a; }

    /* SECTION CONTAINERS & GRIDS */
    .section-title {
      font-size: 13px; font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase;
      color: var(--gold); display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
    }
    .section-title span.badge {
      font-size: 10px; padding: 2px 6px; border-radius: 4px; background: rgba(251, 191, 36, 0.15);
      border: 1px solid rgba(251, 191, 36, 0.3); color: var(--gold);
    }

    .panel {
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    /* KPI Tiles Grid (Lifted from server/spread_dash_html.py:1525) */
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
    }
    .kpi-tile {
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid var(--border-subtle);
      border-radius: 10px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      transition: border-color 0.15s ease, background-color 0.15s ease;
    }
    .kpi-tile:hover {
      border-color: rgba(255, 255, 255, 0.2);
      background: rgba(20, 30, 50, 0.7);
    }
    .kpi-header {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .kpi-val {
      font-family: 'JetBrains Mono', monospace;
      font-size: 22px;
      font-weight: 800;
      line-height: 1.1;
    }
    .kpi-sub {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: var(--text-muted);
      line-height: 1.4;
    }

    /* TOOLTIP PATTERN (Lifted from server/spread_dash_html.py:99-114) */
    .tip-wrap { position: relative; display: inline-flex; vertical-align: middle; margin-left: 4px; }
    .tip-ico {
      width: 14px; height: 14px; border: 1px solid rgba(148, 163, 184, 0.5); color: #94a3b8;
      background: transparent; border-radius: 9999px; font-size: 9px; font-weight: 700;
      display: inline-flex; align-items: center; justify-content: center; cursor: help; padding: 0;
    }
    .tip-ico:hover, .tip-wrap:focus-within .tip-ico { border-color: var(--gold); color: var(--gold); }
    .tip-pop {
      position: absolute; bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%) translateY(4px);
      width: 290px; max-width: calc(100vw - 32px); padding: 10px 12px; background: #090d16;
      border: 1px solid rgba(255, 255, 255, 0.2); box-shadow: 0 16px 36px rgba(0, 0, 0, 0.95);
      opacity: 0; visibility: hidden; pointer-events: none; transition: opacity 0.15s ease, transform 0.15s ease, visibility 0.15s;
      z-index: 9999; text-align: left; font-family: 'JetBrains Mono', monospace;
    }
    .tip-pop .tip-k { display: block; font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--gold); font-weight: 700; margin-bottom: 4px; }
    .tip-pop .tip-t { display: block; font-size: 11px; line-height: 1.5; color: #94a3b8; font-weight: 400; }
    .tip-pop .tip-g { display: block; margin-top: 6px; padding-top: 6px; border-top: 1px solid rgba(255, 255, 255, 0.1); font-size: 10px; line-height: 1.5; color: #d1d5db; }
    .tip-wrap:hover .tip-pop, .tip-wrap:focus-within .tip-pop { opacity: 1; visibility: visible; transform: translateX(-50%) translateY(0); }

    /* FUNNEL VIEW (Lifted from server/fleet_dash.py:1106-1180) */
    .funnel-board {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
    }
    @media (max-width: 900px) { .funnel-board { grid-template-columns: 1fr; } }
    .funnel-lane {
      background: rgba(15, 23, 42, 0.5);
      border: 1px solid var(--border-subtle);
      border-radius: 10px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .funnel-hdr {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border-subtle);
      padding-bottom: 8px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .funnel-badge {
      padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 800;
    }
    .gate-card {
      background: rgba(8, 12, 20, 0.6);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
    }
    .gate-card .gate-code { font-weight: 700; color: #f59e0b; }
    .gate-card .gate-ex { color: var(--text-muted); font-size: 10px; line-height: 1.4; }

    /* FUNNEL CENSUS STRIP */
    .funnel-census-strip {
      display: flex;
      flex-direction: column;
      gap: 6px;
      padding: 12px 14px;
      background: rgba(15, 23, 42, 0.5);
      border: 1px solid var(--border-subtle);
      border-radius: 10px;
      margin-bottom: 12px;
    }
    .funnel-census-chain {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      color: var(--text-primary);
      display: flex;
      align-items: center;
      gap: 4px;
      flex-wrap: wrap;
    }
    .funnel-census-chain .arrow { color: var(--text-muted); margin: 0 2px; }
    .funnel-census-chain .val { font-weight: 700; }
    .funnel-census-chain .val-up { color: #10b981; }
    .funnel-census-chain .val-down { color: #ef4444; }
    .funnel-census-meta {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .funnel-census-meta .source-badge {
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .funnel-census-details {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      color: var(--text-muted);
    }
    .funnel-census-details summary {
      cursor: pointer;
      color: var(--text-muted);
      font-size: 10.5px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      user-select: none;
    }
    .funnel-census-details[open] summary { margin-bottom: 5px; }
    .funnel-census-gates {
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 5px;
      border-top: 1px dashed var(--border-subtle);
      padding-top: 4px;
    }

    /* TABLES */
    .table-container { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; font-family: 'JetBrains Mono', monospace; }
    th { text-align: left; padding: 10px 12px; color: var(--text-muted); font-weight: 600; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid var(--border-subtle); }
    td { padding: 10px 12px; border-bottom: 1px solid rgba(255, 255, 255, 0.04); color: var(--text-primary); }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }
    tr.clickable-row { cursor: pointer; }
    tr.clickable-row:hover td { background: rgba(56, 189, 248, 0.08); }

    .status-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-weight: 700; font-size: 10px; text-transform: uppercase; }
    .status-tag.open { background: var(--blue-bg); color: var(--blue-rest); border: 1px solid var(--blue-border); }
    .status-tag.filled { background: var(--green-bg); color: #34d399; border: 1px solid var(--green-border); }
    .status-tag.partial { background: var(--amber-bg); color: #fcd34d; border: 1px solid var(--amber-border); }
    .status-tag.cancelled { background: var(--bg-surface-raised); color: var(--text-muted); }
    .status-tag.unattributed { background: #ea580c; color: #ffffff; border: 1px solid #f97316; animation: pulse 1.5s infinite; }

    /* MECHANICS PANEL (Level 3 - Visually Distinct) */
    .mechanics-box {
      border: 1px solid rgba(245, 158, 11, 0.3);
      background: rgba(20, 15, 5, 0.35);
      border-radius: 12px;
      padding: 18px;
    }
    .mech-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
    }
    .mech-card {
      background: rgba(15, 23, 42, 0.7);
      border: 1px solid rgba(255, 255, 255, 0.07);
      border-radius: 8px;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-family: 'JetBrains Mono', monospace;
    }

    /* MODAL */
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.85); backdrop-filter: blur(6px);
      z-index: 10000; display: flex; align-items: center; justify-content: center; padding: 20px;
    }
    .modal-box {
      background: #090d16; border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 12px;
      width: 100%; max-width: 960px; max-height: 90vh; overflow-y: auto; padding: 24px;
      box-shadow: 0 25px 60px rgba(0, 0, 0, 0.95); display: flex; flex-direction: column; gap: 16px;
    }
    .modal-hdr { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-subtle); padding-bottom: 12px; }
    .modal-close { background: transparent; border: 1px solid var(--border-subtle); color: var(--text-secondary); width: 28px; height: 28px; border-radius: 6px; font-weight: 700; cursor: pointer; }
    .modal-close:hover { border-color: #ef4444; color: #ef4444; }

    .grid-2col { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; }
    @media (max-width: 900px) { .grid-2col { grid-template-columns: 1fr; } }

    .stat-list { display: flex; flex-direction: column; gap: 10px; }
    .stat-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: var(--bg-surface-raised); border-radius: 6px; border: 1px solid var(--border-subtle); font-family: 'JetBrains Mono', monospace; font-size: 12px; }
    .lock-idle { color: var(--green-ok); }
    .lock-active { color: var(--amber-warn); }
    .empty-state-text { color: var(--text-muted); font-style: italic; padding: 18px 0; text-align: center; }

    /* SYSTEM STATUS BAR & BOT CONTROLS */
    .status-bar-wrap {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 18px;
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      flex-wrap: wrap;
    }
    .guardrail-banner {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 12px 0 0;
      padding: 12px 18px;
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid var(--red-alert);
      border-radius: 12px;
      color: #fecaca;
      font-weight: 600;
      box-shadow: 0 0 18px rgba(239, 68, 68, 0.25);
    }
    .guardrail-banner .guardrail-kind {
      color: #f87171;
      letter-spacing: 0.04em;
      font-size: 12px;
      text-transform: uppercase;
      margin-right: 8px;
      white-space: nowrap;
    }
    .guardrail-banner .guardrail-detail {
      font-weight: 400;
      color: #fca5a5;
      font-size: 13px;
    }
    .guardrail-banner .guardrail-dismiss {
      margin-left: auto;
      background: transparent;
      border: 1px solid rgba(239, 68, 68, 0.5);
      color: #fecaca;
      border-radius: 6px;
      padding: 2px 10px;
      cursor: pointer;
      font-weight: 600;
    }
    .guardrail-banner .guardrail-dismiss:hover {
      background: rgba(239, 68, 68, 0.2);
    }
    .guardrail-health {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      background: var(--bg-surface-raised);
      border: 1px solid var(--border-subtle);
      border-radius: 10px;
      font-size: 12px;
      white-space: nowrap;
      color: var(--text-secondary);
    }
    .guardrail-health .gh-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      flex: none;
    }
    .guardrail-health .gh-dot.alive {
      background: #34d399;
      box-shadow: 0 0 6px rgba(52, 211, 153, 0.7);
    }
    .guardrail-health .gh-dot.down {
      background: #f87171;
      box-shadow: 0 0 6px rgba(248, 113, 113, 0.7);
    }
    .guardrail-health .gh-dot.unknown {
      background: #fbbf24;
    }
    .guardrail-health .gh-alerts {
      color: var(--text-muted);
      font-weight: 600;
    }
    .sup-card {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 14px;
      background: var(--bg-surface-raised);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
    }
    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
      flex-shrink: 0;
    }
    .status-dot-sm {
      width: 7px;
      height: 7px;
    }
    .dot-online {
      background-color: #10b981;
      box-shadow: 0 0 8px #10b981;
    }
    .dot-offline {
      background-color: #ef4444;
      box-shadow: 0 0 8px #ef4444;
    }
    .sub-services-group {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .sub-service-pill {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      background: var(--bg-surface-raised);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      font-size: 11px;
      font-family: 'JetBrains Mono', monospace;
    }
    .sub-service-name {
      color: var(--text-secondary);
      font-weight: 600;
    }
    .sub-service-status {
      font-weight: 700;
    }
    .bot-controls {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .btn-bot {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      font-weight: 700;
      padding: 7px 16px;
      border-radius: 6px;
      border: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s ease;
    }
    .btn-start {
      background: #059669;
      color: #ffffff;
    }
    .btn-start:hover:not(:disabled) {
      background: #10b981;
      box-shadow: 0 0 12px rgba(16, 185, 129, 0.4);
    }
    .btn-start:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .btn-stop {
      background: #dc2626;
      color: #ffffff;
    }
    .btn-stop:hover:not(:disabled) {
      background: #ef4444;
      box-shadow: 0 0 12px rgba(239, 68, 68, 0.4);
    }
    .btn-stop:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .btn-reset {
      background: #334155;
      color: #cbd5e1;
      border: 1px solid var(--border-strong);
    }
    .btn-reset:hover:not(:disabled) {
      background: #475569;
      color: #ffffff;
      box-shadow: 0 0 10px rgba(148, 163, 184, 0.3);
    }
    .btn-reset:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .btn-restart {
      background: rgba(56, 189, 248, 0.15);
      color: #38bdf8;
      border: 1px solid rgba(56, 189, 248, 0.4);
    }
    .btn-restart:hover:not(:disabled) {
      background: rgba(56, 189, 248, 0.25);
      color: #ffffff;
      box-shadow: 0 0 10px rgba(56, 189, 248, 0.4);
    }
    .btn-restart:disabled {
          opacity: 0.4;
          cursor: not-allowed;
        }
        .btn-sync {
          background: rgba(250, 204, 21, 0.15);
          color: #facc15;
          border: 1px solid rgba(250, 204, 21, 0.4);
        }
        .btn-sync:hover:not(:disabled) {
          background: rgba(250, 204, 21, 0.25);
          color: #ffffff;
          box-shadow: 0 0 10px rgba(250, 204, 21, 0.4);
        }
        .btn-sync:disabled {
          opacity: 0.4;
          cursor: not-allowed;
        }
        .sweep-control {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding-left: 8px;
      border-left: 1px solid var(--border-subtle);
    }
    .sweep-label {
      font-size: 11px;
      font-family: 'JetBrains Mono', monospace;
      color: var(--text-secondary);
      white-space: nowrap;
    }
    .sweep-input {
      width: 64px;
      padding: 5px 8px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px;
      background: var(--bg-surface-raised);
      color: var(--text-primary);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
    }
    .sweep-unit {
      font-size: 11px;
      font-family: 'JetBrains Mono', monospace;
      color: var(--text-muted);
    }
    .btn-sweep {
      background: #334155;
      color: #cbd5e1;
      border: 1px solid var(--border-strong);
    }
    .btn-sweep:hover:not(:disabled) {
      background: #475569;
      color: #ffffff;
      box-shadow: 0 0 10px rgba(148, 163, 184, 0.3);
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <header>
      <div class="brand">
        <span class="brand-icon">🎯</span>
        <div>
          <div class="brand-title">SPREAD HUNTER // LIVE MONITOR</div>
          <div class="brand-sub" id="db-path-display">DB: run/live.db</div>
        </div>
      </div>
      <div class="top-meta">
        <div class="run-select-wrap">
          <label for="run-selector" style="color:var(--text-secondary);font-size:11px;font-weight:700;">RUN:</label>
          <select id="run-selector">
            <option value="">Latest Run</option>
          </select>
        </div>
      </div>
    </header>

    <!-- BOT CONTROL BAR -->
    <div class="status-bar-wrap">
      <div class="bot-controls">
        <button id="btn-start-bot" class="btn-bot btn-start" onclick="startBot()">▶ Start Bot</button>
        <button id="btn-stop-bot" class="btn-bot btn-stop" onclick="stopBot()">⏹ Stop Bot</button>
        <button id="btn-reset-db" class="btn-bot btn-reset" onclick="confirmResetDb()">🔄 Fresh DB</button>
        <button id="btn-restart-dash" class="btn-bot btn-restart" onclick="restartDash()">⚡ Restart Dash</button>
                <button id="btn-venue-sync" class="btn-bot btn-sync" onclick="venueSync()">⟳ Sync</button>
                <div class="sweep-control">
          <label class="sweep-label" for="sweep-interval-input">sweep</label>
          <input id="sweep-interval-input" class="sweep-input" type="number" min="1" step="1" placeholder="every tick" />
          <span class="sweep-unit">s</span>
          <button class="btn-bot btn-sweep" onclick="setSweepInterval()">Set</button>
          <button class="btn-bot btn-sweep" onclick="clearSweepInterval()">Reset</button>
        </div>
      </div>
      <div id="guardrail-health" class="guardrail-health" title="Guardrail watcher health">
        <span class="gh-dot unknown" id="gh-dot"></span>
        <span id="gh-text">guardrail …</span>
      </div>
    </div>

    <!-- GUARDRAIL BANNER: shown only while a guardrail_alert ring event
         is active (repeated pair exit, over-cap pair). Hidden otherwise. -->
    <div id="guardrail-banner" class="guardrail-banner" style="display:none">
      <span class="guardrail-kind" id="guardrail-kind"></span>
      <span id="guardrail-subject"></span>
      <span class="guardrail-detail" id="guardrail-detail"></span>
      <button class="guardrail-dismiss" onclick="dismissGuardrailBanner()">✕ dismiss</button>
    </div>

    <!-- PORTFOLIO OVERVIEW: the whole run, every market, one reading.
         Mirrors the simulation's capital panel (server/spread_dash_html.py:1449). -->
    <section class="portfolio-card">
      <div class="portfolio-tiles">
        <div class="pf-tile pf-tile-primary">
          <div class="pf-label">Account Value <span class="pf-src" id="portfolio-src">venue</span></div>
          <div class="pf-value-huge mono" id="portfolio-total-value">--</div>
          <div class="pf-foot">
            <span class="pf-chip" id="portfolio-pnl">--</span>
            <span class="pf-foot-note mono" id="portfolio-basis">collateral + positions</span>
          </div>
          <div class="pf-foot-note mono" id="portfolio-src-note">&nbsp;</div>
        </div>
        <div class="pf-tile">
          <div class="pf-label">Realized P&amp;L <span class="pf-src" id="portfolio-realized-src">venue</span></div>
          <div class="pf-value mono" id="portfolio-realized">--</div>
          <div class="pf-foot-note mono" id="portfolio-realized-sub">-- closes &middot; -- markets</div>
        </div>
        <div class="pf-tile">
          <div class="pf-label">Unrealized (Open) <span class="pf-src">venue</span></div>
          <div class="pf-value mono" id="portfolio-unrealized">--</div>
          <div class="pf-foot-note mono" id="portfolio-unrealized-sub">marked at last sweep</div>
        </div>
        <div class="pf-tile">
          <div class="pf-label">Capital Committed <span class="pf-src">venue</span></div>
          <div class="pf-value mono" id="portfolio-committed">--</div>
          <div class="pf-foot-note mono" id="portfolio-committed-sub">open cost basis at risk</div>
        </div>
      </div>
      <div class="pf-chart" id="portfolio-equity-curve"></div>
    </section>

    <!-- HERO SECTION: HEDGE STATE -->
    <div id="hero-container">
      <div class="hero-card state-empty">
        <div class="hero-headline">Loading live telemetry...</div>
        <div class="hero-desc">Connecting to local database reader.</div>
      </div>
    </div>

    <!-- BOT BRAINS: LIVE DECISION FLOW (SSE from /api/cycle-stream) -->
    <section class="panel bot-brains">
      <div class="section-title" style="color:#38bdf8;">
        <span>Bot Brains &mdash; Live Decision Flow</span>
        <span class="badge" style="background:rgba(56,189,248,0.15);border-color:rgba(56,189,248,0.3);color:#38bdf8;">Live</span>
      </div>
      <div class="bot-brains-grid">
        <div class="bb-col bb-status">
          <div class="bb-status-dot" id="bb-status-dot"></div>
          <div>
            <div class="bb-scan-state" id="bb-scan-state">CONNECTING</div>
            <div class="bb-scan-sub" id="bb-scan-sub">waiting for cycle stream</div>
          </div>
        </div>
        <div class="bb-col bb-pills" id="bb-active-pills">
          <span class="bb-pill" data-service="engine">engine &middot; &mdash;</span>
          <span class="bb-pill" data-service="fleet">fleet &middot; &mdash;</span>
          <span class="bb-pill" data-service="screener">screener &middot; &mdash;</span>
        </div>
        <div class="bb-col bb-funnel">
          <div class="bb-funnel-label">Filter funnel: <span id="bb-evaluated">&mdash;</span> evaluated &rarr; <span id="bb-passed">&mdash;</span> passed</div>
          <div class="bb-funnel-bar"><div class="bb-funnel-fill" id="bb-funnel-fill"></div></div>
          <div class="bb-funnel-skips" id="bb-funnel-skips">skip reasons appear here</div>
        </div>
        <div class="bb-col bb-spark">
          <div class="bb-spark-label">scan latency (ms)</div>
          <canvas id="bb-sparkline" width="220" height="36"></canvas>
        </div>
        <div class="bb-col bb-pairs">
          <div class="bb-pairs-label">auto pairs (U35) &middot; cycle <span id="bb-pairs-cycle">&mdash;</span></div>
          <div class="bb-pairs-counts" id="bb-pairs-counts">no activity</div>
          <div class="bb-pairs-last" id="bb-pairs-last">last pair actions appear here</div>
        </div>
      </div>
      <div class="bb-log" id="bb-decision-log"><!-- 12-line color-coded tail --></div>
    </section>

    <!-- LEVEL 1: RUN-LEVEL STRATEGY METRICS (Lifted from server/spread_dash_html.py:1525-1567) -->
    <section class="panel">
      <div class="section-title">
        <span>Level 1 &mdash; Strategy Performance</span>
        <span class="badge">Run Evaluation</span>
      </div>
      <div class="kpi-grid" id="sec-run-kpis">
        <!-- Rendered dynamically -->
      </div>
    </section>

    <!-- REQ 4: EXPOSURE OVER TIME (Lifted from server/spread_dash_html.py:1505-1509) -->
    <section class="panel">
      <div class="section-title">
        <span>Portfolio Exposure Over Time</span>
        <span class="badge">Float Marks</span>
      </div>
      <div id="sec-exposure">
        <!-- Exposure Chart SVG -->
      </div>
    </section>

    <!-- LEVEL 2: MARKET-LEVEL DRILL-DOWN & FUNNEL (Lifted from server/fleet_dash.py:1106-1180) -->
    <section class="panel">
      <div class="section-title">
        <span>Level 2 &mdash; Market Selection & Refusals Funnel</span>
        <span class="badge">Market Choice</span>
      </div>
      <div class="funnel-board" id="sec-funnel">
        <!-- 4 Lanes: RAW -> FILTERS -> FINAL -> GRADUATED -->
      </div>

      <div style="margin-top:14px;">
        <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--text-secondary);margin-bottom:8px;">
          MARKETS IN RUN &mdash; CLICK TO DRILL DOWN
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Market</th>
                <th>Category</th>
                <th>Volume (24h)</th>
                <th>Horizon</th>
                <th>Fills / Quotes</th>
                <th>Up / Dn Shares</th>
                <th>Pair Cost</th>
                <th>Balance</th>
                <th>Realized PnL</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="markets-tbody">
              <tr><td colspan="10" class="empty-state-text">No market telemetry available</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- LEVEL 3: MECHANICS & SYSTEM HEALTH (Lifted from server/spread_dash_html.py:1572-1621) -->
    <section class="mechanics-box">
      <div class="section-title" style="color:#f59e0b;">
        <span>Level 3 &mdash; Mechanics & System Health</span>
        <span class="badge" style="background:rgba(245,158,11,0.15);border-color:rgba(245,158,11,0.3);color:#f59e0b;">Machinery Diagnostics</span>
      </div>
      <div class="mech-grid" id="sec-mechanics">
        <!-- Latency, Reconcile Lag, Venue Rejects, Divergence events -->
      </div>
      <div id="venue-rejects-detail" style="margin-top:14px;display:none;">
        <div style="font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:#fca5a5;margin-bottom:6px;">RECENT VENUE REJECTS / ERRORS</div>
        <div class="table-container">
          <table style="font-size:11px;">
            <thead>
              <tr><th>Time</th><th>Code</th><th>Side</th><th>Price</th><th>Size</th><th>Raw Error Message</th></tr>
            </thead>
            <tbody id="venue-rejects-tbody"></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ACTIVE ORDERS + CAPITAL GRID -->
    <div class="grid-2col">
      <!-- Order Matrix -->
      <div class="panel">
        <div class="section-title" style="color:var(--text-secondary);">
          <span>Active Pair Orders</span>
          <span id="order-count-badge" class="pill pill-neutral">0 orders</span>
        </div>
        <div class="table-container">
          <table>
            <thead>
                          <tr>
                            <th>Side / Leg</th>
                            <th>Price</th>
                            <th>Size</th>
                            <th>Matched</th>
                            <th>Fill %</th>
                            <th>Status</th>
                            <th>Age</th>
                            <th style="width:80px;"></th>
                          </tr>
                        </thead>
                        <tbody id="orders-tbody">
                          <tr>
                            <td colspan="8" class="empty-state-text">No orders loaded</td>
                          </tr>
                        </tbody>
          </table>
        </div>
      </div>

      <!-- Capital & Pair Pricing -->
      <div class="panel">
        <div class="section-title" style="color:var(--text-secondary);">
          <span>Capital & Pair Cost</span>
        </div>
        <div class="stat-list">
          <div class="stat-row">
            <span style="color:var(--text-secondary);">Total Capital Committed</span>
            <span id="stat-total-committed" style="font-weight:700;">$0.00</span>
          </div>
          <div class="stat-row">
            <span style="color:var(--text-secondary);">Resting Notional</span>
            <span id="stat-resting-committed" style="font-weight:700;">$0.00</span>
          </div>
          <div class="stat-row">
            <span style="color:var(--text-secondary);">Filled Notional</span>
            <span id="stat-filled-committed" style="font-weight:700;">$0.00</span>
          </div>
          <div class="stat-row">
            <span style="color:var(--text-secondary);">Combined Pair Price</span>
            <span id="stat-pair-price" style="font-weight:700;">--</span>
          </div>
          <div class="stat-row">
            <span style="color:var(--text-secondary);">Max Pair Cost Limit</span>
            <span id="stat-max-pair-cost" style="font-weight:700;">--</span>
          </div>
        </div>
      </div>
    </div>

    <!-- FILLS TIMELINE -->
    <div class="panel">
      <div class="section-title" style="color:var(--text-secondary);">
        <span>Fills Timeline (Newest First)</span>
        <span id="fill-count-badge" class="pill pill-neutral">0 fills</span>
      </div>
      <div class="table-container">
        <table>
          <thead>
                      <tr>
                        <th>Time (Local)</th>
                        <th>Trade ID</th>
                        <th>Side</th>
                        <th>Price</th>
                        <th>Size</th>
                        <th>Notional</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody id="fills-tbody">
                      <tr>
                        <td colspan="7" class="empty-state-text">No fills recorded yet</td>
                      </tr>
                    </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- FIXED STATUS FOOTER: always-visible backend indicators -->
  <div class="footer-bar">
    <div id="supervisor-status" class="sup-card">
      <span class="status-dot dot-offline" id="sup-dot"></span>
      <div>
        <div class="footer-sup-title">SUPERVISOR: <span id="sup-text" style="color:#f87171;">OFFLINE</span></div>
        <div id="sup-sub" class="footer-sup-sub"></div>
      </div>
    </div>

    <div id="sub-services" class="sub-services-group">
      <div class="sub-service-pill" id="pill-screener">
        <span class="status-dot status-dot-sm dot-offline" id="dot-screener"></span>
        <span class="sub-service-name">Screener</span>
        <span class="sub-service-status" id="txt-screener" style="color:#f87171;">offline</span>
      </div>
      <div class="sub-service-pill" id="pill-engine">
        <span class="status-dot status-dot-sm dot-offline" id="dot-engine"></span>
        <span class="sub-service-name">Engine</span>
        <span class="sub-service-status" id="txt-engine" style="color:#f87171;">offline</span>
        <span class="sub-service-status" id="txt-engine-sweep" style="color:var(--text-muted);font-weight:400;">sweep: every tick</span>
      </div>
      <div class="sub-service-pill" id="pill-fleet">
        <span class="status-dot status-dot-sm dot-offline" id="dot-fleet"></span>
        <span class="sub-service-name">Fleet</span>
        <span class="sub-service-status" id="txt-fleet" style="color:#f87171;">offline</span>
      </div>
    </div>

    <div class="footer-meta">
      <span id="poll-pill" class="pill pill-neutral">CONNECTING...</span>
      <span id="lock-container" class="footer-lock lock-idle">
        <span class="status-dot status-dot-sm dot-online" id="lock-dot"></span>
        <span id="lock-status-text">Idle</span>
      </span>
      <span id="port-pill" class="pill pill-neutral"></span>
      <span id="clock-display" class="footer-clock">--:--:--</span>
    </div>
  </div>

  <!-- MARKET DRILL-DOWN MODAL -->
  <div id="modal-drilldown" class="modal-backdrop" style="display:none;">
    <div class="modal-box">
      <div class="modal-hdr">
        <div>
          <div id="modal-mkt-title" style="font-size:18px;font-weight:800;color:#f8fafc;">Market Details</div>
          <div id="modal-mkt-sub" style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text-secondary);"></div>
        </div>
        <button type="button" class="modal-close" onclick="closeDrilldownModal()">✕</button>
      </div>
      <div id="modal-drilldown-body" style="display:flex;flex-direction:column;gap:16px;">
        <!-- Filled dynamically -->
      </div>
    </div>
  </div>

  <!-- DISTRIBUTION ZOOM MODAL (Lifted from server/spread_dash_html.py:1255) -->
  <div id="modal-dist" class="modal-backdrop" style="display:none;">
    <div class="modal-box" style="max-width:700px;">
      <div class="modal-hdr">
        <div id="modal-dist-title" style="font-size:16px;font-weight:800;">Adverse Selection Distribution</div>
        <button type="button" class="modal-close" onclick="closeDistModal()">✕</button>
      </div>
      <div id="modal-dist-body" style="padding:10px 0;"></div>
    </div>
  </div>

  <script>
    let lastState = null;
    let lastKpi = null;
    // Handed to this page by the process that served it. Required on every
    // /api/system/* call: a cross-origin form POST cannot set a custom header,
    // which is what stops another tab from starting the bot on your behalf.
    const CONTROL_TOKEN = "__LIVE_DASH_CONTROL_TOKEN__";

    function controlFetch(path) {
      return fetch(path, { method: 'POST', headers: { 'X-Control-Token': CONTROL_TOKEN } });
    }

    let localNakedSinceMs = null;
    let localLastPollMs = null;
    let selectedRunId = "";

    function esc(v) {
      return String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    const KNOWN_STATUSES = ['pending', 'open', 'partial', 'filled', 'cancelled', 'unattributed'];

    function formatTime(ms) {
      if (!ms) return '--';
      const d = new Date(ms);
      return d.toLocaleTimeString();
    }

    function formatDuration(sec) {
      if (sec === null || sec === undefined || isNaN(sec)) return '--';
      const s = Math.floor(sec);
      const m = Math.floor(s / 60);
      const remS = s % 60;
      if (m > 0) return `${m}m ${remS}s`;
      return `${s}s`;
    }

    function tip(label, body, formula) {
      return `
        <span class="tip-wrap" tabindex="0">
          <span class="tip-ico" aria-label="Info">i</span>
          <span class="tip-pop">
            <span class="tip-k">${esc(label)}</span>
            <span class="tip-t">${body}</span>
            ${formula ? `<span class="tip-g"><strong>Formula:</strong> ${formula}</span>` : ''}
          </span>
        </span>
      `;
    }

    // SVG Bell Curve (Lifted from server/spread_dash_html.py:1217)
    function bellCurveSvg(opts) {
      const {min, max, mean, stdev, zero, color, w, h} = opts;
      const W = w || 180, H = h || 50, pad = 8;
      const x = v => pad + ((v - min) / Math.max(0.001, max - min)) * (W - pad * 2);
      const sd = stdev && stdev > 0 ? stdev : Math.max(0.1, (max - min) / 6);
      const bell = [];
      for (let i = 0; i < 40; i++) {
        const v = min + (i / 39) * (max - min);
        const z = (v - mean) / sd;
        bell.push([v, Math.exp(-0.5 * z * z)]);
      }
      const maxY = Math.max(...bell.map(p => p[1])) || 1;
      const yS = y => (H - 10) - (y / maxY) * (H - 20);
      const path = bell.map((p, i) => `${i === 0 ? "M" : "L"} ${x(p[0]).toFixed(1)} ${yS(p[1]).toFixed(1)}`).join(" ");
      const area = `${path} L ${x(bell[bell.length - 1][0]).toFixed(1)} ${H - 10} L ${x(bell[0][0]).toFixed(1)} ${H - 10} Z`;
      let zeroLine = "";
      if (zero !== undefined && zero >= min && zero <= max) {
        zeroLine = `<line x1="${x(zero).toFixed(1)}" x2="${x(zero).toFixed(1)}" y1="4" y2="${H - 6}" stroke="#EF4444" stroke-width="1.2" stroke-dasharray="3 2"/>`;
      }
      return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:${H}px;display:block;">
        <path d="${area}" fill="${color}" fill-opacity="0.15"/>
        <path d="${path}" fill="none" stroke="${color}" stroke-width="1.5"/>
        ${zeroLine}
        <circle cx="${x(mean).toFixed(1)}" cy="${yS(Math.exp(0)).toFixed(1)}" r="3" fill="#111827" stroke="#F9FAFB" stroke-width="1.5"/>
      </svg>`;
    }

    // Histogram of actual per-trade outcomes (dollars). Green bars = profitable
    // closes, red = losing closes, with a dashed zero line. A real histogram,
    // not a fitted bell: with n=1-2 live trades a normal curve is a drawing,
    // not a measurement.
    function histogramSvg(values, opts) {
      opts = opts || {};
      const W = opts.w || 180, H = opts.h || 46;
      const pad = 4, base = H - 5;
      if (!values || !values.length) return '';
      const minV = Math.min(...values), maxV = Math.max(...values);
      const lo = Math.min(0, minV), hi = Math.max(0, maxV);
      const span = (hi - lo) || 1;
      const nb = Math.max(2, Math.min((opts.bins || 7), Math.max(2, Math.ceil(Math.sqrt(values.length) * 2))));
      const counts = new Array(nb).fill(0);
      for (const v of values) {
        let idx = Math.floor(((v - lo) / span) * nb);
        if (idx >= nb) idx = nb - 1;
        if (idx < 0) idx = 0;
        counts[idx]++;
      }
      const maxC = Math.max(...counts) || 1;
      const barW = (W - pad * 2) / nb;
      const binW = span / nb;
      const zeroX = pad + ((0 - lo) / span) * (W - pad * 2);
      const bars = counts.map((c, i) => {
        const bh = Math.max(0, (c / maxC) * (base - 4));
        const bx = pad + i * barW;
        const center = lo + (i + 0.5) * binW;
        const bcolor = center >= 0 ? (opts.color || '#10b981') : '#ef4444';
        return `<rect x="${bx.toFixed(1)}" y="${(base - bh).toFixed(1)}" width="${Math.max(1, barW - 1).toFixed(1)}" height="${bh.toFixed(1)}" fill="${bcolor}" fill-opacity="0.85"/>`;
      }).join('');
      const zeroLine = (zeroX >= pad && zeroX <= W - pad)
        ? `<line x1="${zeroX.toFixed(1)}" x2="${zeroX.toFixed(1)}" y1="2" y2="${base}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="2 2"/>` : '';
      return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:${H}px;display:block;" role="img" aria-label="Per-trade P&L histogram">
        ${bars}
        ${zeroLine}
      </svg>`;
    }

    function fmtUsdSignedVal(v) {
      const n = Number(v);
      if (v === null || v === undefined || isNaN(n)) return '--';
      return `${n >= 0 ? '+' : '-'}$${Math.abs(n).toFixed(2)}`;
    }

    // ---------- PORTFOLIO OVERVIEW (run level, every market) ----------
    // The run's whole book in one reading. The curve is the same construction
    // the simulation uses (server/spread_dash_html.py:175): realised closes
    // stacked on the starting bankroll, open float folded in at the marks.
    function fmtUsdSigned(v) {
      const n = Number(v || 0);
      return `${n >= 0 ? '+' : '-'}$${Math.abs(n).toFixed(2)}`;
    }

    function equityCurveSvg(series, startingCapital) {
      if (!series || series.length === 0) {
        return `<div class="pf-empty">No closes or marks yet &mdash; the curve starts on the first settled position.</div>`;
      }
      const W = 900, H = 220, padL = 62, padR = 20, padT = 18, padB = 26;
      const pts = series.slice().sort((a, b) => a.ts - b.ts);
      const t0 = pts[0].ts;
      const t1 = pts[pts.length - 1].ts === t0 ? t0 + 3600 : pts[pts.length - 1].ts;
      const span = Math.max(1, t1 - t0);
      const vals = pts.map(p => p.v);
      const lo = Math.min(startingCapital, ...vals);
      const hi = Math.max(startingCapital, ...vals);
      const pad = Math.max((hi - lo) * 0.16, 0.5);
      const minY = lo - pad, maxY = hi + pad;
      const X = t => padL + ((t - t0) / span) * (W - padL - padR);
      const Y = v => padT + (1 - (v - minY) / (maxY - minY)) * (H - padT - padB);

      const last = pts[pts.length - 1].v;
      const up = last >= startingCapital;
      const stroke = up ? '#10b981' : '#ef4444';
      // A single point has no line to draw; anchor it on the bankroll so the
      // reader sees the step, not an empty box.
      const path = [`M ${X(t0)} ${Y(startingCapital)}`]
        .concat(pts.map(p => `L ${X(p.ts)} ${Y(p.v)}`)).join(' ');
      const area = `${path} L ${X(pts[pts.length - 1].ts)} ${Y(minY)} L ${X(t0)} ${Y(minY)} Z`;
      const baseY = Y(startingCapital);

      const dots = pts.filter(p => p.type === 'close').map(p =>
        `<circle cx="${X(p.ts)}" cy="${Y(p.v)}" r="3.5" fill="${stroke}" stroke="#080c14" stroke-width="1.5">`
        + `<title>${esc(String(p.market || 'close'))} — ${fmtUsdSigned(p.pnl)} → $${p.v.toFixed(2)}</title></circle>`
      ).join('');

      return `
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Total equity since inception">
          <defs>
            <linearGradient id="pf-grad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="${stroke}" stop-opacity="0.28"/>
              <stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>
            </linearGradient>
          </defs>
          <line x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}"
                stroke="#64748b" stroke-width="1" stroke-dasharray="4 4"/>
          <text x="${padL - 6}" y="${baseY + 3}" text-anchor="end"
                font-family="JetBrains Mono, monospace" font-size="10" fill="#64748b">$${startingCapital.toFixed(0)}</text>
          <text x="${padL - 6}" y="${Y(maxY) + 10}" text-anchor="end"
                font-family="JetBrains Mono, monospace" font-size="10" fill="#64748b">$${maxY.toFixed(2)}</text>
          <text x="${padL - 6}" y="${Y(minY) - 2}" text-anchor="end"
                font-family="JetBrains Mono, monospace" font-size="10" fill="#64748b">$${minY.toFixed(2)}</text>
          <path d="${area}" fill="url(#pf-grad)"/>
          <path d="${path}" fill="none" stroke="${stroke}" stroke-width="2"
                stroke-linejoin="round" stroke-linecap="round"/>
          ${dots}
        </svg>
      `;
    }

    function renderPortfolio(kpi) {
      const p = (kpi && kpi.portfolio) || null;
      const a = (p && p.account) || null;
      // The account, as the venue reports it. Anything the sweep did not obtain
      // arrives as null and renders "--". A zero here would be a number the
      // venue never gave us, which is the defect this card was built to end.
      const swept = !!(a && a.measured);
      const hasValue = swept && a.account_value_usd !== null && a.account_value_usd !== undefined;
      const accPnl = (swept && a.pnl_usd !== null && a.pnl_usd !== undefined)
        ? Number(a.pnl_usd) : null;
      const up = accPnl === null ? true : accPnl >= 0;
      const color = up ? 'var(--signal)' : 'var(--loss)';

      document.getElementById('portfolio-total-value').textContent = hasValue
        ? `$${Number(a.account_value_usd).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
        : '--';

      const pnlEl = document.getElementById('portfolio-pnl');
      // pnl_pct is NULL when net deposits are zero: the percentage is undefined,
      // and printing 0.00% would read as "flat" instead of "unmeasurable".
      const pctStr = (a && a.pnl_pct !== null && a.pnl_pct !== undefined)
        ? `${a.pnl_pct >= 0 ? '+' : ''}${Number(a.pnl_pct).toFixed(2)}%` : 'n/a';
      pnlEl.textContent = accPnl === null ? '--' : `${fmtUsdSigned(accPnl)} (${pctStr})`;
      pnlEl.style.color = color;
      pnlEl.style.background = up ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)';
      pnlEl.style.borderColor = up ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)';

      // `?? 0` here would print "$0.00 cash" for a collateral read that failed,
      // stating a figure the venue never returned -- the same NULL-versus-zero
      // rule the rest of this card enforces.
      const usdOrDash = (v) =>
        (v === null || v === undefined) ? '--' : `$${Number(v).toFixed(2)}`;
      const basisEl = document.getElementById('portfolio-basis');
      basisEl.textContent = swept
        ? `${usdOrDash(a.collateral_usd)} cash + ${usdOrDash(a.positions_value_usd)} positions`
        : 'collateral + positions';

      // How old the reading is. A balance is only as true as its last sweep,
      // and a stale one is the failure mode worth naming on the tile itself.
      const srcNote = document.getElementById('portfolio-src-note');
      if (!swept) {
        srcNote.textContent = 'no sweep yet — run: python -m engine.live_exec account-sweep';
        srcNote.style.color = 'var(--warn)';
      } else {
        const ageS = Math.max(0, (Date.now() / 1000) - Number(a.ts || 0));
        const ageStr = ageS < 90 ? `${Math.round(ageS)}s`
          : ageS < 5400 ? `${Math.round(ageS / 60)}m`
          : `${Math.round(ageS / 3600)}h`;
        srcNote.textContent = `swept ${ageStr} ago`;
        srcNote.style.color = ageS > 900 ? 'var(--warn)' : 'var(--text-muted)';
      }
      const srcEl = document.getElementById('portfolio-src');
      srcEl.textContent = swept ? (a.source || 'venue') : 'unswept';

      // Realised P&L is the sum of what the venue's own closed positions
      // returned. The registry only knows the closes this bot performed, so it
      // reports $0.00 for a position closed by a merge on Polymarket itself --
      // which is exactly how the White Sox / Cubs pair (-3.10, -1.60, +5.00)
      // vanished from the tile. Registry closes stay as the sub-line, labelled.
      const hasVenueRealized = swept && a.pnl_closed_usd !== null && a.pnl_closed_usd !== undefined;
      const realizedVal = hasVenueRealized ? Number(a.pnl_closed_usd)
                        : (p ? Number(p.realized_pnl) : null);
      const realizedEl = document.getElementById('portfolio-realized');
      realizedEl.textContent = realizedVal === null ? '--' : fmtUsdSigned(realizedVal);
      realizedEl.style.color = (realizedVal !== null && realizedVal < 0) ? 'var(--loss)' : 'var(--signal)';
      document.getElementById('portfolio-realized-src').textContent =
        hasVenueRealized ? 'venue' : 'registry';
      // A null count is "this sweep did not report one", not zero.
      const closedN = (a && a.closed_positions_count !== null && a.closed_positions_count !== undefined)
        ? `${a.closed_positions_count} closed position(s)` : 'closed positions';
      document.getElementById('portfolio-realized-sub').textContent = hasVenueRealized
        ? `${closedN} · ${p ? p.closes_count : 0} registry closes`
        : (p ? `${p.closes_count} closes · ${p.markets_count} markets` : '-- closes · -- markets');

      // Unrealized and committed now come from the venue's open positions, so
      // they no longer depend on a float_marks sweep that nothing ever ran.
      const hasUnreal = swept && a.unrealized_usd !== null && a.unrealized_usd !== undefined;
      const unrealEl = document.getElementById('portfolio-unrealized');
      unrealEl.textContent = hasUnreal ? fmtUsdSigned(a.unrealized_usd) : '--';
      unrealEl.style.color = (hasUnreal && Number(a.unrealized_usd) < 0) ? 'var(--loss)' : 'var(--text-primary)';
      document.getElementById('portfolio-unrealized-sub').textContent = hasUnreal
        ? `${a.open_positions_count ?? 0} open position(s)`
        : 'not marked — no venue sweep';

      const hasCommitted = swept && a.committed_usd !== null && a.committed_usd !== undefined;
      document.getElementById('portfolio-committed').textContent =
        hasCommitted ? `$${Number(a.committed_usd).toFixed(2)}` : '--';
      document.getElementById('portfolio-committed-sub').textContent =
        hasCommitted ? 'open cost basis at risk' : 'not marked — no venue sweep';

      // Plot the venue's account-value curve once sweeps exist; fall back to
      // the registry equity curve until the first one lands.
      const accSeries = (kpi && kpi.account_series) || [];
      if (accSeries.length >= 2) {
        document.getElementById('portfolio-equity-curve').innerHTML =
          equityCurveSvg(accSeries, Number(accSeries[0].v));
      } else {
        document.getElementById('portfolio-equity-curve').innerHTML =
          equityCurveSvg((kpi && kpi.equity_series) || [],
                         p ? Number(p.starting_capital || 0) : 0);
      }
    }

    function renderHero(state) {
      const hero = document.getElementById('hero-container');
      if (state.empty || !state.pairs || state.pairs.length === 0) {
        hero.innerHTML = `
          <div class="hero-card state-empty">
            <div class="hero-badge resting">📡 AWAITING LIVE ORDERS</div>
            <div class="hero-headline">No active orders in registry</div>
            <div class="hero-desc">${esc(state.message || "Orders posted via live_exec quote will appear here automatically.")}</div>
          </div>
        `;
        localNakedSinceMs = null;
        return;
      }

      const pair = state.pairs[0];
      const hedgeState = pair.hedge_state;

      if (hedgeState === 'NAKED' && pair.naked_info) {
        const info = pair.naked_info;
        localNakedSinceMs = info.naked_since_ts;
        const unhedgedUsd = info.unhedged_dollars.toFixed(2);
        const unhedgedShares = info.unhedged_shares.toFixed(2);
        const side = info.unhedged_side || 'LONG';
        const secNaked = Math.floor(info.seconds_naked || 0);

        hero.innerHTML = `
          <div class="hero-card state-naked">
            <div class="hero-header">
              <div>
                <div class="hero-badge naked">🚨 CRITICAL: NAKED LEG DETECTED</div>
                <div class="hero-headline">UNHEDGED POSITION OPEN</div>
                <div class="hero-desc">One leg has filled while the opposite leg remains unmatched. Immediate attention required!</div>
              </div>
            </div>
            <div class="naked-metrics">
              <div class="metric-block">
                <div class="metric-label">Unhedged Dollar Risk</div>
                <div class="metric-val-huge">$${unhedgedUsd} <span style="font-size:18px;font-weight:600;">(${unhedgedShares} sh @ ${side})</span></div>
              </div>
              <div class="metric-block">
                <div class="metric-label">Time Naked (Counting Up)</div>
                <div class="metric-val-huge timer-val" id="live-naked-timer">${formatDuration(secNaked)}</div>
              </div>
            </div>
          </div>
        `;
      } else if (hedgeState === 'SETTLED') {
        localNakedSinceMs = null;
        const st = pair.settlement || {};
        const pnl = Number(st.pnl || 0);
        const sign = pnl >= 0 ? '+' : '-';
        hero.innerHTML = `
          <div class="hero-card state-balanced">
            <div class="hero-header">
              <div>
                <div class="hero-badge balanced">✅ POSITION CLOSED</div>
                <div class="hero-headline">${sign}$${Math.abs(pnl).toFixed(2)} REALISED</div>
                <div class="hero-desc">${esc((st.methods || ['closed']).join(', '))} &mdash; ${Number(st.shares || 0).toFixed(2)} pairs settled. The shares are gone and the money is back; nothing is at risk on this market.</div>
              </div>
            </div>
          </div>
        `;
      } else if (hedgeState === 'BALANCED') {
        localNakedSinceMs = null;
        const totalFills = pair.orders.reduce((acc, o) => acc + (o.size_matched || 0), 0);
        hero.innerHTML = `
          <div class="hero-card state-balanced">
            <div class="hero-header">
              <div>
                <div class="hero-badge balanced">🛡️ INVENTORY BALANCED</div>
                <div class="hero-headline">PERFECTLY HEDGED PAIR</div>
                <div class="hero-desc">Both legs filled in equal size (${(totalFills / 2).toFixed(2)} shares matched). Inventory neutral &mdash; holding to resolution.</div>
              </div>
            </div>
          </div>
        `;
      } else if (hedgeState === 'RESTING') {
        localNakedSinceMs = null;
        hero.innerHTML = `
          <div class="hero-card state-resting">
            <div class="hero-header">
              <div>
                <div class="hero-badge resting">⏳ RESTING BIDS ON CLOB</div>
                <div class="hero-headline">WAITING FOR TAKER FILLS</div>
                <div class="hero-desc">Orders placed and resting on both sides. No fills executed yet.</div>
              </div>
            </div>
          </div>
        `;
      } else if (hedgeState === 'REFUSED') {
        localNakedSinceMs = null;
        hero.innerHTML = `
          <div class="hero-card state-naked">
            <div class="hero-header">
              <div>
                <div class="hero-badge naked">❓ CANNOT CLASSIFY POSITION</div>
                <div class="hero-headline">TREAT AS UNHEDGED</div>
                <div class="hero-desc">${esc(pair.refused_reason || 'Pair shape not recognised.')} Check the position on the venue before acting.</div>
              </div>
            </div>
          </div>
        `;
      } else {
        localNakedSinceMs = null;
        hero.innerHTML = `
          <div class="hero-card state-closed">
            <div class="hero-header">
              <div>
                <div class="hero-badge closed">⏹️ CYCLE CLOSED / CANCELLED</div>
                <div class="hero-headline">NO ACTIVE EXPOSURE</div>
                <div class="hero-desc">Orders in this pair are cancelled or fully closed.</div>
              </div>
            </div>
          </div>
        `;
      }
    }

    // LEVEL 1: Render Strategy KPIs (Lifted from server/spread_dash_html.py:1525)
    function renderRunKpis(kpi) {
      const el = document.getElementById('sec-run-kpis');
      if (!kpi || kpi.error) {
        el.innerHTML = '<div class="empty-state-text">No KPI data loaded</div>';
        return;
      }

      const fillRateVal = kpi.fill_rate !== null ? (kpi.fill_rate * 100).toFixed(1) + '%' : '--';
      const uptimeVal = kpi.quote_uptime !== null ? (kpi.quote_uptime * 100).toFixed(1) + '%' : '--';
      const waitVal = kpi.median_seconds_to_fill !== null ? kpi.median_seconds_to_fill.toFixed(1) + 's' : '--';
      const queueVal = kpi.median_queue_ahead !== null ? kpi.median_queue_ahead.toFixed(0) + ' sh' : '--';
      const capVal = kpi.spread_capture_per_share !== null ? (kpi.spread_capture_per_share * 100).toFixed(2) + '¢' : '--';
      const advVal = kpi.adverse_selection !== null ? (kpi.adverse_selection * 100).toFixed(2) + '¢' : '--';
      const pnlVal = kpi.realized_pnl !== null ? (kpi.realized_pnl >= 0 ? '+' : '') + '$' + kpi.realized_pnl.toFixed(2) : '--';
      const roiVal = kpi.roi_on_cost !== null ? (kpi.roi_on_cost * 100).toFixed(1) + '%' : '--';
      const topSkip = (kpi.top_skip_reasons && kpi.top_skip_reasons[0]) ? `${kpi.top_skip_reasons[0].reason} (${kpi.top_skip_reasons[0].cycles})` : 'None';

      const ta = kpi.trade_analytics || {};
      const winRateVal = ta.win_rate !== null && ta.win_rate !== undefined ? (ta.win_rate * 100).toFixed(1) + '%' : '--';
      const winRateCI = ta.win_rate_ci95
        ? `CI [${(ta.win_rate_ci95.lower * 100).toFixed(0)}%, ${(ta.win_rate_ci95.upper * 100).toFixed(0)}%]`
        : 'CI --';
      const expectancyVal = fmtUsdSignedVal(ta.expectancy_usd);
      const meanRetVal = ta.mean_return_pct !== null && ta.mean_return_pct !== undefined
        ? (ta.mean_return_pct >= 0 ? '+' : '') + ta.mean_return_pct.toFixed(1) + '%' : '--';
      const sharpeVal = ta.sharpe_ratio !== null && ta.sharpe_ratio !== undefined ? ta.sharpe_ratio.toFixed(2) : '--';
      const rrVal = ta.risk_reward_ratio !== null && ta.risk_reward_ratio !== undefined
        ? ta.risk_reward_ratio.toFixed(2) + ':1'
        : ((ta.wins > 0 && ta.losses === 0) ? '&infin;' : '--');
      const pfVal = ta.profit_factor !== null && ta.profit_factor !== undefined
        ? ta.profit_factor.toFixed(2)
        : ((ta.wins > 0 && ta.losses === 0) ? '&infin;' : '--');
      const sortinoVal = ta.sortino_ratio !== null && ta.sortino_ratio !== undefined ? ta.sortino_ratio.toFixed(2) : '--';
      const ddPctVal = ta.max_drawdown_pct !== null && ta.max_drawdown_pct !== undefined
        ? '-' + ta.max_drawdown_pct.toFixed(1) + '%' : '--';
      const ddUsdVal = ta.max_drawdown_usd !== null && ta.max_drawdown_usd !== undefined
        ? '-$' + Math.abs(ta.max_drawdown_usd).toFixed(2) : '--';
      const pnlVals = (ta.pnl_distribution || []).map(d => d.realized_pnl).filter(v => v !== null && v !== undefined);

      const tiles = [
        {
          label: 'Maker Fill Rate',
          val: fillRateVal,
          color: kpi.fill_rate && kpi.fill_rate > 0.5 ? '#10b981' : '#f59e0b',
          sub: `${kpi.filled_shares || 0} filled / ${kpi.posted_shares || 0} posted sh`,
          tipBody: 'Proportion of resting maker bids filled by takers, excluding taker crossing shares.',
          tipFormula: '(filled_shares &minus; crossed_shares) / posted_shares',
        },
        {
          label: 'Quote Uptime & Skips',
          val: uptimeVal,
          color: kpi.quote_uptime && kpi.quote_uptime > 0.8 ? '#10b981' : '#94a3b8',
          sub: `Top Skip: ${esc(topSkip)}`,
          tipBody: 'Fraction of evaluation cycles where quotes were actively posted vs skipped.',
          tipFormula: 'cycles_quoting / total_decision_cycles',
        },
        {
          label: 'Wait to Fill & Queue',
          val: waitVal,
          color: '#38bdf8',
          sub: `Median Queue Ahead: ${queueVal} &middot; n=${kpi.fills || 0}`,
          tipBody: 'Median elapsed seconds from quote placement to venue match timestamp.',
          tipFormula: 'median(venue_ts - quote_posted_ts)',
        },
        {
          label: 'Spread Capture / Share',
          val: capVal,
          color: '#10b981',
          sub: `Total: ${kpi.spread_capture !== null && kpi.spread_capture !== undefined ? '$' + kpi.spread_capture.toFixed(2) : '--'} &middot; avg edge ${kpi.avg_edge_cents !== null && kpi.avg_edge_cents !== undefined ? kpi.avg_edge_cents.toFixed(1) + '¢' : '--'}`,
          tipBody: 'Earned edge vs reference mid-price at time of quote placement per share filled.',
          tipFormula: '&Sigma;(edge_vs_mid &middot; filled_size) / filled_shares',
        },
        {
          label: 'Adverse Selection',
          val: advVal,
          color: kpi.adverse_selection === null || kpi.adverse_selection === undefined ? '#94a3b8' : (kpi.adverse_selection >= 0 ? '#10b981' : '#ef4444'),
          sub: `n=${kpi.markout_samples || 0} markout samples <button onclick="openDistModal('adv')" style="background:none;border:none;color:#38bdf8;cursor:pointer;text-decoration:underline;font:inherit;">chart &nearr;</button>`,
          chart: bellCurveSvg({min: -5, max: 5, mean: (kpi.adverse_selection || 0) * 100, stdev: 1.5, zero: 0, color: (kpi.adverse_selection || 0) >= 0 ? '#10b981' : '#ef4444', w: 180, h: 42}),
          tipBody: 'Size-weighted post-trade drift against us across 4 horizons (5m, 1h, 6h, 15m).',
          tipFormula: '&Sigma;(size &middot; (mid_later &minus; fill_price)) / total_filled',
        },
        {
          label: 'Realized PnL & ROI',
          val: pnlVal,
          color: (kpi.realized_pnl || 0) >= 0 ? '#10b981' : '#ef4444',
          sub: `ROI: ${roiVal} &middot; ${kpi.wins || 0}W / ${kpi.losses || 0}L closes`,
          tipBody: 'Realized trading profit from settled merges and early exits on cost basis.',
          tipFormula: 'realized_pnl / capital_cost',
        },
        {
          label: 'Win Rate & Expectancy',
          val: winRateVal,
          color: ta.win_rate === null || ta.win_rate === undefined ? '#94a3b8' : (ta.win_rate >= 0.5 ? '#10b981' : '#ef4444'),
          sub: `n=${ta.n_closes || 0} closes &middot; exp ${expectancyVal} &middot; ${winRateCI}`,
          tipBody: 'Win rate is profitable closes over all closes, with a Wilson 95% interval. Expectancy is the average realized P&amp;L per closed position &mdash; positive means the strategy earns on average.',
          tipFormula: 'wins / closes; mean(realized_pnl)',
        },
        {
          label: 'Trade PnL Distribution',
          val: expectancyVal,
          color: ta.expectancy_usd === null || ta.expectancy_usd === undefined ? '#94a3b8' : (ta.expectancy_usd >= 0 ? '#10b981' : '#ef4444'),
          sub: `mean return ${meanRetVal} &middot; n=${ta.n_closes || 0} <button onclick="openDistModal('pnl')" style="background:none;border:none;color:#38bdf8;cursor:pointer;text-decoration:underline;font:inherit;">chart &nearr;</button>`,
          chart: histogramSvg(pnlVals, {w: 180, h: 42}),
          tipBody: 'Histogram of the absolute net gain/loss on every closed position ($5 in returning $6.50 is a $1.50 bar). The headline is the average dollar P&amp;L per trade; mean return % is the equal-weighted percentage.',
          tipFormula: 'realized_pnl = proceeds &minus; cost_basis',
        },
        {
          label: 'Sharpe & Risk/Reward',
          val: sharpeVal,
          color: ta.sharpe_ratio === null || ta.sharpe_ratio === undefined ? '#94a3b8' : (ta.sharpe_ratio >= 0 ? '#10b981' : '#ef4444'),
          sub: `R:R ${rrVal} &middot; PF ${pfVal} &middot; Sortino ${sortinoVal}`,
          tipBody: 'Per-trade Sharpe = mean return / &sigma;(returns). Risk:reward = avg win / avg loss. Profit factor = gross wins / gross losses. Sortino penalises only downside deviation.',
          tipFormula: 'mean/&sigma;; &Sigma;wins/|&Sigma;losses|',
        },
        {
          label: 'Drawdown & Inventory',
          val: ddPctVal,
          color: ta.max_drawdown_pct === null || ta.max_drawdown_pct === undefined ? '#94a3b8' : (ta.max_drawdown_pct === 0 ? '#10b981' : '#f59e0b'),
          sub: `Max DD ${ddUsdVal} &middot; peak naked $${ta.max_naked_exposure_usd !== null && ta.max_naked_exposure_usd !== undefined ? ta.max_naked_exposure_usd.toFixed(2) : '--'}`,
          tipBody: 'Max drawdown is the largest peak-to-trough fall of run equity (realised closes plus open float). Inventory risk is the largest naked one-sided exposure ever marked.',
          tipFormula: 'max(peak &minus; trough); max(naked_usd)',
        },
      ];

      el.innerHTML = tiles.map(t => `
        <div class="kpi-tile">
          <div class="kpi-header">
            <span>${esc(t.label)}</span>
            ${tip(t.label, t.tipBody, t.tipFormula)}
          </div>
          <div class="kpi-val" style="color:${t.color};">${esc(t.val)}</div>
          ${t.chart || ''}
          <div class="kpi-sub">${t.sub}</div>
        </div>
      `).join('');
    }

    // REQ 4: Exposure Over Time Chart (Lifted from server/spread_dash_html.py:1505)
    function renderExposureChart(kpi) {
      const el = document.getElementById('sec-exposure');
      const marks = (kpi && kpi.float_marks) ? kpi.float_marks : [];
      if (!marks || marks.length === 0) {
        el.innerHTML = '<div class="empty-state-text">No float marks recorded yet in this run</div>';
        return;
      }

      const W = 900, H = 160, padL = 50, padR = 20, padT = 16, padB = 24;
      const sorted = marks.slice().sort((a, b) => a.ts - b.ts);
      const t0 = sorted[0].ts;
      const t1 = sorted[sorted.length - 1].ts === t0 ? t0 + 60 : sorted[sorted.length - 1].ts;
      const span = Math.max(1, t1 - t0);

      const maxVal = Math.max(10, ...sorted.map(m => Math.max(m.committed_open_usd, m.naked_usd, m.unrealized_usd)));
      const x = t => padL + ((t - t0) / span) * (W - padL - padR);
      const y = v => (H - padB) - (v / maxVal) * (H - padB - padT);

      const pathCommitted = sorted.map((m, i) => `${i === 0 ? 'M' : 'L'} ${x(m.ts).toFixed(1)} ${y(m.committed_open_usd).toFixed(1)}`).join(' ');
      const pathNaked = sorted.map((m, i) => `${i === 0 ? 'M' : 'L'} ${x(m.ts).toFixed(1)} ${y(m.naked_usd).toFixed(1)}`).join(' ');
      const pathUnrealized = sorted.map((m, i) => `${i === 0 ? 'M' : 'L'} ${x(m.ts).toFixed(1)} ${y(m.unrealized_usd).toFixed(1)}`).join(' ');

      const latest = sorted[sorted.length - 1];

      el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font-family:'JetBrains Mono',monospace;font-size:12px;">
          <div style="display:flex;gap:16px;">
            <span style="color:#38bdf8;">● Committed: $${latest.committed_open_usd.toFixed(2)}</span>
            <span style="color:#ef4444;">● Naked Risk: $${latest.naked_usd.toFixed(2)}</span>
            <span style="color:#10b981;">● Unrealised PnL: $${latest.unrealized_usd.toFixed(2)}</span>
          </div>
          <span style="color:var(--text-muted);font-size:11px;">${sorted.length} marks recorded</span>
        </div>
        <div style="overflow-x:auto;">
          <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:${H}px;background:rgba(8,12,20,0.6);border:1px solid var(--border-subtle);border-radius:8px;">
            <!-- Grid lines -->
            <line x1="${padL}" x2="${W - padR}" y1="${y(maxVal * 0.5)}" y2="${y(maxVal * 0.5)}" stroke="rgba(255,255,255,0.05)" stroke-dasharray="3 3"/>
            <line x1="${padL}" x2="${W - padR}" y1="${H - padB}" y2="${H - padB}" stroke="rgba(255,255,255,0.1)"/>
            
            <!-- Series Lines -->
            <path d="${pathCommitted}" fill="none" stroke="#38bdf8" stroke-width="2"/>
            <path d="${pathNaked}" fill="none" stroke="#ef4444" stroke-width="2"/>
            <path d="${pathUnrealized}" fill="none" stroke="#10b981" stroke-width="1.5" stroke-dasharray="4 2"/>

            <!-- Y Axis labels -->
            <text x="${padL - 6}" y="${y(maxVal)}" text-anchor="end" fill="#64748b" font-size="10" font-family="JetBrains Mono">$${maxVal.toFixed(0)}</text>
            <text x="${padL - 6}" y="${H - padB}" text-anchor="end" fill="#64748b" font-size="10" font-family="JetBrains Mono">$0</text>
          </svg>
        </div>
      `;
    }

    // LEVEL 2: Funnel & Market Drill-down (Lifted from server/fleet_dash.py:1106-1180)
    function renderFunnel(kpi) {
      const el = document.getElementById('sec-funnel');
      const funnel = (kpi && kpi.funnel) || {};
      const filters = funnel.filters || [];
      const graduated = funnel.graduated || [];

      const rawCount = funnel.raw_count || 0;
      const filterCount = filters.reduce((acc, f) => acc + (f.n || 0), 0);
      const finalCount = funnel.final_count || graduated.length;
      const gradCount = graduated.length;

      // Census strip: source, snapshot age, gates line -- operator sees
      // which snapshot the lanes came from at a glance.
      const source = funnel.source || 'unknown';
      const age = Math.max(0, funnel.snapshot_age || 0);
      const stale = age > 900;
      const census = (funnel.census || '').trim();
      const gates = (funnel.gates || '').trim();
      const hms = s => { s = Math.max(0, Math.floor(s));
        const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
        const p = n => String(n).padStart(2,'0');
        return h ? `${h}h ${p(m)}m ${p(x)}s` : `${m}m ${p(x)}s`;
      };
      const sourceBadge = source === 'screener'
        ? '<span class="source-badge" style="background:rgba(16,185,129,0.15);border:1px solid rgba(16,185,129,0.4);color:#10b981;">SNAPSHOT</span>'
        : '<span class="source-badge" style="background:rgba(245,158,11,0.15);border:1px solid rgba(245,158,11,0.4);color:#f59e0b;">RUNTIME</span>';
      const ageColor = stale ? '#ef4444' : 'var(--text-muted)';
      const censusStrip = `
        <div class="funnel-census-strip">
          <div class="funnel-census-meta">
            ${sourceBadge}
            <span style="color:${ageColor};">snapshot ${hms(age)} old</span>
          </div>
          ${census ? `<div class="funnel-census-chain">${esc(census)}</div>` : ''}
          ${gates ? `
            <details class="funnel-census-details">
              <summary>what the last rank did · the gates it used</summary>
              <div class="funnel-census-gates">${esc(gates)}</div>
            </details>
          ` : ''}
        </div>
      `;

      el.innerHTML = `
        ${censusStrip}
        <!-- Lane 1: RAW -->
        <div class="funnel-lane">
          <div class="funnel-hdr">
            <span>① RAW CANDIDATES</span>
            <span class="funnel-badge" style="background:#38bdf820;color:#38bdf8;">${rawCount}</span>
          </div>
          <div style="font-size:11px;color:var(--text-muted);font-family:'JetBrains Mono',monospace;">
            Observed in candidate scan & census.
          </div>
        </div>

        <!-- Lane 2: FILTERS -->
        <div class="funnel-lane">
          <div class="funnel-hdr">
            <span>② REFUSALS / GATES</span>
            <span class="funnel-badge" style="background:#f59e0b20;color:#f59e0b;">${filterCount}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px;max-height:180px;overflow-y:auto;">
            ${filters.length ? filters.map(g => `
              <div class="gate-card">
                <div style="display:flex;justify-content:space-between;">
                  <span class="gate-code">${esc(g.cause)}</span>
                  <span style="color:var(--text-secondary);font-weight:700;">${g.n}</span>
                </div>
                ${(g.examples || []).slice(0, 2).map(e => `
                  <div class="gate-ex truncate" title="${esc(e.reason)}">&bull; ${esc(e.title)}: ${esc(e.reason)}</div>
                `).join('')}
              </div>
            `).join('') : '<div class="empty-state-text" style="padding:10px 0;">No refusals logged</div>'}
          </div>
        </div>

        <!-- Lane 3: FINAL -->
        <div class="funnel-lane">
          <div class="funnel-hdr">
            <span>③ FINAL ELIGIBLE</span>
            <span class="funnel-badge" style="background:#a855f720;color:#c084fc;">${finalCount}</span>
          </div>
          <div style="font-size:11px;color:var(--text-muted);font-family:'JetBrains Mono',monospace;">
            Passed spread, inventory, and risk gates.
          </div>
        </div>

        <!-- Lane 4: GRADUATED -->
        <div class="funnel-lane">
          <div class="funnel-hdr">
            <span>④ GRADUATED / LIVE</span>
            <span class="funnel-badge" style="background:#10b98120;color:#10b981;">${gradCount}</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px;max-height:180px;overflow-y:auto;">
            ${graduated.length ? graduated.map(g => `
              <div class="gate-card" style="border-left:3px solid #10b981;">
                <div style="font-weight:700;color:var(--text-primary);">${esc(g.title || g.slug || g.condition_id)}</div>
                <div style="display:flex;justify-content:space-between;color:var(--text-secondary);font-size:10px;">
                  <span>${g.fills || 0} fills</span>
                  <span style="color:${(g.pnl || 0) >= 0 ? '#10b981' : '#ef4444'};">${(g.pnl || 0) >= 0 ? '+' : ''}$${(g.pnl || 0).toFixed(2)}</span>
                </div>
              </div>
            `).join('') : '<div class="empty-state-text" style="padding:10px 0;">No active markets</div>'}
          </div>
        </div>
      `;
    }

    function renderMarketsTable(kpi) {
      const tbody = document.getElementById('markets-tbody');
      const byMkt = (kpi && kpi.by_market) ? kpi.by_market : {};
      const cids = Object.keys(byMkt);

      if (cids.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-state-text">No market telemetry in selected run</td></tr>';
        return;
      }

      tbody.innerHTML = cids.map(cid => {
        const m = byMkt[cid];
        const title = m.title || m.slug || cid.slice(0, 16);
        const pnl = m.realized_pnl || 0;
        const pnlColor = pnl >= 0 ? '#10b981' : '#ef4444';
        const pairCostStr = m.pair_cost !== null ? `$${m.pair_cost.toFixed(3)}` : '--';
        const balanceStr = m.balance !== null ? `${(m.balance * 100).toFixed(0)}%` : '--';
        const volStr = m.volume_24h ? `$${(m.volume_24h / 1000).toFixed(0)}K` : '--';
        const daysStr = m.days_to_resolve !== null ? `${m.days_to_resolve.toFixed(1)}d` : '--';

        // The name is the market's identity and its way out to the venue. The
        // link must not swallow the row click that opens the drill-down.
        const nameHtml = m.url
          ? `<a class="mkt-name-link" href="${esc(m.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation();">${esc(title)} &#8599;</a>`
          : `<strong>${esc(title)}</strong>`;

        return `
          <tr class="clickable-row" onclick="openDrilldownModal('${esc(cid)}')">
            <td>
              ${nameHtml}
              <div style="font-size:10px;color:var(--text-muted);">${esc(cid.slice(0, 10))}...${esc(cid.slice(-6))}</div>
            </td>
            <td><span class="cat-tag">${esc(m.category || 'Uncategorized')}</span></td>
            <td>${volStr}</td>
            <td>${daysStr}</td>
            <td>${m.fills_count} / ${m.quotes_count}</td>
            <td>${m.up_sh.toFixed(1)} UP / ${m.dn_sh.toFixed(1)} DN</td>
            <td><strong>${pairCostStr}</strong></td>
            <td>${balanceStr}</td>
            <td style="color:${pnlColor};font-weight:700;">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</td>
            <td><button style="background:transparent;border:1px solid var(--border-subtle);color:#38bdf8;padding:2px 8px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:11px;">Drill Down &rarr;</button></td>
          </tr>
        `;
      }).join('');
    }

    // LEVEL 3: Mechanics Panel (Lifted from server/spread_dash_html.py:1572)
    function renderMechanics(kpi, state) {
      const el = document.getElementById('sec-mechanics');
      const lat = (kpi && kpi.order_latency_ms) || {};
      const rec = (kpi && kpi.reconcile_lag_ms) || {};
      const rej = (kpi && kpi.venue_rejects) || { total: 0, by_code: {}, events: [] };
      const divs = (kpi && kpi.three_way_divergences) || { total: 0, events: [] };
      const lock = (state && state.reconcile_lock) || {};

      // A backfilled fill can be recorded 44 minutes after the venue timestamp.
      // Printing that as "2663493.0ms" is unreadable, so scale the unit.
      const durMs = (v) => {
        if (v === null || v === undefined) return '--';
        if (v < 1000) return `${v.toFixed(1)}ms`;
        if (v < 60000) return `${(v / 1000).toFixed(1)}s`;
        if (v < 3600000) return `${(v / 60000).toFixed(1)}m`;
        return `${(v / 3600000).toFixed(1)}h`;
      };
      const latMed = durMs(lat.median);
      const latMax = durMs(lat.max);
      const recMed = durMs(rec.median);
      const recMax = durMs(rec.max);

      el.innerHTML = `
        <div class="mech-card">
          <div style="font-size:11px;color:var(--text-secondary);font-weight:700;">ORDER POST LATENCY</div>
          <div style="font-size:20px;font-weight:800;color:#f8fafc;">${latMed}</div>
          <div style="font-size:11px;color:var(--text-muted);">Max: ${latMax} &middot; n=${lat.count || 0}</div>
        </div>

        <div class="mech-card">
          <div style="font-size:11px;color:var(--text-secondary);font-weight:700;">RECONCILE LAG</div>
          <div style="font-size:20px;font-weight:800;color:${(rec.median || 0) > 1000 ? '#f59e0b' : '#f8fafc'};">${recMed}</div>
          <div style="font-size:11px;color:var(--text-muted);">Max: ${recMax} &middot; n=${rec.count || 0}</div>
        </div>

        <div class="mech-card">
          <div style="font-size:11px;color:var(--text-secondary);font-weight:700;">VENUE REJECTS / ERRORS</div>
          <div style="font-size:20px;font-weight:800;color:${rej.total > 0 ? '#ef4444' : '#10b981'};">${rej.total}</div>
          <div style="font-size:11px;color:var(--text-muted);">${Object.keys(rej.by_code || {}).map(c => `${esc(c)}: ${esc(rej.by_code[c])}`).join(', ') || '0 errors'}</div>
        </div>

        <div class="mech-card">
          <div style="font-size:11px;color:var(--text-secondary);font-weight:700;">3-WAY DIVERGENCES</div>
          <div style="font-size:20px;font-weight:800;color:${divs.total > 0 ? '#ef4444' : '#10b981'};">${divs.total}</div>
          <div style="font-size:11px;color:var(--text-muted);">${divs.total === 0 ? 'Clean (Registry=Venue=Chain)' : 'Investigate incidents'}</div>
        </div>
      `;

      // Detail table for venue rejects if any
      const rejTbody = document.getElementById('venue-rejects-tbody');
      const rejBox = document.getElementById('venue-rejects-detail');
      if (rej.events && rej.events.length > 0) {
        rejBox.style.display = 'block';
        rejTbody.innerHTML = rej.events.map(e => `
          <tr>
            <td>${formatTime(e.ts * 1000)}</td>
            <td style="color:#ef4444;font-weight:700;">${esc(e.code)}</td>
            <td>${esc(e.side || '--')}</td>
            <td>${e.price ? '$' + e.price.toFixed(3) : '--'}</td>
            <td>${e.size ? e.size.toFixed(1) : '--'}</td>
            <td style="color:#fca5a5;">${esc(e.message)}</td>
          </tr>
        `).join('');
      } else {
        rejBox.style.display = 'none';
      }
    }

    function openDrilldownModal(cid) {
      const byMkt = (lastKpi && lastKpi.by_market) ? lastKpi.by_market : {};
      const m = byMkt[cid];
      if (!m) return;

      document.getElementById('modal-mkt-title').textContent = m.title || m.slug || cid;
      document.getElementById('modal-mkt-sub').textContent = `Condition ID: ${cid} | ${m.url ? m.url : ''}`;

      const body = document.getElementById('modal-drilldown-body');
      
      // Quotes section
      const quotesHtml = (m.quotes || []).map(q => `
        <tr>
          <td>${formatTime(q.ts * 1000)}</td>
          <td><strong>${esc(q.side)}</strong></td>
          <td>$${parseFloat(q.price).toFixed(3)}</td>
          <td>${parseFloat(q.size).toFixed(1)}</td>
          <td>${q.mid ? '$' + parseFloat(q.mid).toFixed(3) : '--'}</td>
          <td>${q.edge_vs_mid ? (parseFloat(q.edge_vs_mid) * 100).toFixed(2) + '¢' : '--'}</td>
          <td>${q.queue_ahead != null ? parseFloat(q.queue_ahead).toFixed(0) : '--'}</td>
          <td>${q.filled ? parseFloat(q.filled).toFixed(1) : '0.0'}</td>
          <td>${q.latency_ms ? parseFloat(q.latency_ms).toFixed(1) + 'ms' : '--'}</td>
        </tr>
      `).join('') || '<tr><td colspan="9" class="empty-state-text">No quotes logged</td></tr>';

      // 4-Horizon Markouts section
      const markoutsHtml = (m.markouts || []).map(mo => `
        <tr>
          <td>${formatTime(mo.ts * 1000)}</td>
          <td><strong>${esc(mo.side)}</strong></td>
          <td>$${mo.fill_price.toFixed(3)}</td>
          <td>${mo.size.toFixed(1)}</td>
          <td>${mo.ref_mid ? '$' + mo.ref_mid.toFixed(3) : '--'}</td>
          <td>${mo.drift_h0 !== null && mo.drift_h0 !== undefined ? (mo.drift_h0 * 100).toFixed(2) + '¢' : '--'}</td>
          <td>${mo.drift_h3 !== null && mo.drift_h3 !== undefined ? (mo.drift_h3 * 100).toFixed(2) + '¢' : '--'}</td>
          <td>${mo.drift_h1 !== null && mo.drift_h1 !== undefined ? (mo.drift_h1 * 100).toFixed(2) + '¢' : '--'}</td>
          <td>${mo.drift_h2 !== null && mo.drift_h2 !== undefined ? (mo.drift_h2 * 100).toFixed(2) + '¢' : '--'}</td>
        </tr>
      `).join('') || '<tr><td colspan="9" class="empty-state-text">No markout records for this market</td></tr>';

      // Skips section
      const skipsHtml = (m.skip_events || []).map(e => `
        <tr>
          <td>${formatTime(e.ts * 1000)}</td>
          <td style="color:#f59e0b;font-weight:700;">${esc(e.reason_code)}</td>
          <td>${esc(e.kind)}</td>
          <td>${esc(e.reason || '')}</td>
        </tr>
      `).join('') || '<tr><td colspan="4" class="empty-state-text">No skip events logged</td></tr>';

      body.innerHTML = `
        <div class="panel" style="padding:12px;">
          <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--text-secondary);margin-bottom:6px;">QUOTES PLACEMENT VS MID</div>
          <div class="table-container">
            <table>
              <thead><tr><th>Time</th><th>Side</th><th>Price</th><th>Size</th><th>Mid</th><th>Edge</th><th>Queue</th><th>Filled</th><th>Latency</th></tr></thead>
              <tbody>${quotesHtml}</tbody>
            </table>
          </div>
        </div>

        <div class="panel" style="padding:12px;">
          <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--text-secondary);margin-bottom:6px;">FILLS WITH 4 MARKOUT HORIZONS</div>
          <div class="table-container">
            <table>
              <thead><tr><th>Time</th><th>Side</th><th>Fill Px</th><th>Size</th><th>Ref Mid</th><th>h0 (5m)</th><th>h3 (15m)</th><th>h1 (1h)</th><th>h2 (6h)</th></tr></thead>
              <tbody>${markoutsHtml}</tbody>
            </table>
          </div>
        </div>

        <div class="panel" style="padding:12px;">
          <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--text-secondary);margin-bottom:6px;">MARKET EVENT & SKIP TIMELINE</div>
          <div class="table-container">
            <table>
              <thead><tr><th>Time</th><th>Reason Code</th><th>Kind</th><th>Detail</th></tr></thead>
              <tbody>${skipsHtml}</tbody>
            </table>
          </div>
        </div>
      `;

      document.getElementById('modal-drilldown').style.display = 'flex';
    }

    function closeDrilldownModal() {
      document.getElementById('modal-drilldown').style.display = 'none';
    }

    function openDistModal(type) {
      const modal = document.getElementById('modal-dist');
      const body = document.getElementById('modal-dist-body');
      const title = document.getElementById('modal-dist-title');

      if (type === 'pnl') {
        const ta = (lastKpi && lastKpi.trade_analytics) || {};
        const dist = ta.pnl_distribution || [];
        const pnlVals = dist.map(d => d.realized_pnl);
        const winRateStr = ta.win_rate !== null && ta.win_rate !== undefined ? (ta.win_rate * 100).toFixed(1) + '%' : '--';
        const winCI = ta.win_rate_ci95
          ? `${(ta.win_rate_ci95.lower * 100).toFixed(0)}%–${(ta.win_rate_ci95.upper * 100).toFixed(0)}%` : '--';
        const noLosses = ta.wins > 0 && ta.losses === 0;
        const rrStr = ta.risk_reward_ratio !== null && ta.risk_reward_ratio !== undefined
          ? ta.risk_reward_ratio.toFixed(2) + ':1' : (noLosses ? '&infin;' : '--');
        const pfStr = ta.profit_factor !== null && ta.profit_factor !== undefined
          ? ta.profit_factor.toFixed(2) : (noLosses ? '&infin;' : '--');
        const sortinoStr = ta.sortino_ratio !== null && ta.sortino_ratio !== undefined
          ? ta.sortino_ratio.toFixed(2) : '--';
        const statCell = (k, v, color) => `
          <div style="background:rgba(8,12,20,0.6);border:1px solid var(--border-subtle);border-radius:8px;padding:10px 12px;text-align:center;">
            <div style="font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:var(--text-secondary);">${k}</div>
            <div style="font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:800;margin-top:4px;color:${color || '#f8fafc'};">${v}</div>
          </div>`;
        const tradesRows = dist.slice().reverse().map(d => `
          <tr>
            <td>${formatTime(d.ts * 1000)}</td>
            <td>${esc(d.market_slug || d.condition_id || '—')}</td>
            <td><strong>${esc(d.method || '—')}</strong></td>
            <td>${d.cost_basis !== null && d.cost_basis !== undefined ? '$' + d.cost_basis.toFixed(2) : '--'}</td>
            <td style="color:${d.realized_pnl >= 0 ? '#10b981' : '#ef4444'};font-weight:700;">${fmtUsdSignedVal(d.realized_pnl)}</td>
            <td style="color:${d.return_pct >= 0 ? '#10b981' : '#ef4444'};font-weight:700;">${d.return_pct !== null && d.return_pct !== undefined ? (d.return_pct >= 0 ? '+' : '') + d.return_pct.toFixed(1) + '%' : '--'}</td>
          </tr>`).join('');

        title.textContent = "Trade P&L Distribution";
        body.innerHTML = `
          <div style="padding:10px;display:flex;flex-direction:column;gap:14px;">
            <div style="font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--text-secondary);">
              Net gain/loss per closed position: realized_pnl = proceeds &minus; cost_basis. A $5 commit returning $6.50 is a +$1.50 bar (+30%).
            </div>
            ${histogramSvg(pnlVals, {w: 640, h: 180, bins: 9})}
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px;">
              ${statCell('n trades', ta.n_closes || 0)}
              ${statCell('Win rate', winRateStr, (ta.win_rate || 0) >= 0.5 ? '#10b981' : '#ef4444')}
              ${statCell('Win rate CI', winCI)}
              ${statCell('Mean $/trade', fmtUsdSignedVal(ta.expectancy_usd), (ta.expectancy_usd || 0) >= 0 ? '#10b981' : '#ef4444')}
              ${statCell('Mean return', ta.mean_return_pct !== null && ta.mean_return_pct !== undefined ? (ta.mean_return_pct >= 0 ? '+' : '') + ta.mean_return_pct.toFixed(1) + '%' : '--', (ta.mean_return_pct || 0) >= 0 ? '#10b981' : '#ef4444')}
              ${statCell('Sharpe', ta.sharpe_ratio !== null && ta.sharpe_ratio !== undefined ? ta.sharpe_ratio.toFixed(2) : '--', (ta.sharpe_ratio || 0) >= 0 ? '#10b981' : '#ef4444')}
              ${statCell('Sortino', sortinoStr, (ta.sortino_ratio || 0) >= 0 ? '#10b981' : '#ef4444')}
              ${statCell('Profit factor', pfStr, (ta.profit_factor || 0) >= 1 ? '#10b981' : '#ef4444')}
              ${statCell('Risk:reward', rrStr, (ta.risk_reward_ratio || 0) >= 1 ? '#10b981' : '#ef4444')}
              ${statCell('Max DD', ta.max_drawdown_pct !== null && ta.max_drawdown_pct !== undefined ? '-' + ta.max_drawdown_pct.toFixed(1) + '%' : '--', '#f59e0b')}
              ${statCell('Max DD $', ta.max_drawdown_usd !== null && ta.max_drawdown_usd !== undefined ? '-$' + Math.abs(ta.max_drawdown_usd).toFixed(2) : '--', '#f59e0b')}
            </div>
            ${dist.length ? `
              <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--text-secondary);">CLOSED POSITIONS</div>
              <div class="table-container">
                <table style="font-size:11px;">
                  <thead><tr><th>Time</th><th>Market</th><th>Method</th><th>Cost</th><th>Net P&L</th><th>Return</th></tr></thead>
                  <tbody>${tradesRows}</tbody>
                </table>
              </div>` : '<div class="empty-state-text">No closed positions yet — the distribution starts on the first settled trade.</div>'}
          </div>
        `;
        modal.style.display = 'flex';
        return;
      }

      const adv = lastKpi ? (lastKpi.adverse_selection || 0) * 100 : 0;
      title.textContent = "Adverse Selection Markout Distribution";
      body.innerHTML = `
        <div style="padding:10px;">
          <div style="font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--text-secondary);margin-bottom:12px;">
            Size-weighted post-trade drift &Delta; = mid_later &minus; fill_price (in cents). Negative = adverse selection against maker.
          </div>
          ${bellCurveSvg({min: -8, max: 8, mean: adv, stdev: 2.0, zero: 0, color: adv >= 0 ? '#10b981' : '#ef4444', w: 600, h: 160})}
          <div style="display:flex;justify-content:space-between;margin-top:12px;font-family:'JetBrains Mono',monospace;font-size:12px;">
            <span>Sample Mean: <strong style="color:${adv >= 0 ? '#10b981' : '#ef4444'};">${adv.toFixed(2)}¢</strong></span>
            <span>Horizon: 6h/1h/15m/5m</span>
            <span>Samples: ${lastKpi ? lastKpi.markout_samples || 0 : 0}</span>
          </div>
        </div>
      `;
      modal.style.display = 'flex';
    }

    function closeDistModal() {
      document.getElementById('modal-dist').style.display = 'none';
    }

    // Collapsible market aggregation state
    const LIVE_EXPANDED_MARKETS = new Set();
    function toggleLiveMarketExpand(key) {
      if (LIVE_EXPANDED_MARKETS.has(key)) LIVE_EXPANDED_MARKETS.delete(key);
      else LIVE_EXPANDED_MARKETS.add(key);
      // Trigger re-render to show/hide detail rows.
      if (lastState) renderOrders(lastState);
    }

    function renderOrders(state) {
      const tbody = document.getElementById('orders-tbody');
      const badge = document.getElementById('order-count-badge');
      const ACTIVE_STATUSES = new Set(['open', 'pending', 'partial']);
      const all = state.orders || [];
      const orders = all.filter(o => ACTIVE_STATUSES.has(o.status));
      badge.textContent = `${orders.length} of ${all.length} orders`;
      if (orders.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-state-text">No active orders</td></tr>';
        return;
      }
      const groups = new Map();
      for (const o of orders) {
        const cid = o.condition_id || 'unknown';
        if (!groups.has(cid)) groups.set(cid, []);
        groups.get(cid).push(o);
      }
      const sorted = [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
      let html = '';
      for (const [cid, groupOrders] of sorted) {
        groupOrders.sort((a, b) => b.posted_ts - a.posted_ts);
        const isExpanded = LIVE_EXPANDED_MARKETS.has(cid);
        const totalSize = groupOrders.reduce((s, o) => s + parseFloat(o.original_size || 0), 0);
        const totalMatched = groupOrders.reduce((s, o) => s + parseFloat(o.size_matched || 0), 0);
        const statuses = [...new Set(groupOrders.map(o => o.status))].join(', ');
        const cidShort = (cid || '').slice(0, 14) + '..';
        html += `<tr class="market-agg-row" style="cursor:pointer;background:rgba(30,41,59,0.3);" onclick="toggleLiveMarketExpand('${esc(cid)}')">
          <td><strong>${esc(cidShort)}</strong><div style="font-size:10px;color:var(--text-muted);">${groupOrders.length} orders &middot; ${esc(statuses)}</div></td>
          <td>&mdash;</td>
          <td>${totalSize.toFixed(1)}</td>
          <td><strong>${totalMatched.toFixed(1)}</strong></td>
          <td>${totalSize > 0 ? Math.round(totalMatched / totalSize * 100) : 0}%</td>
          <td><span class="status-tag open">${groupOrders.length} open</span></td>
          <td>&mdash;</td>
          <td><button type="button" onclick="event.stopPropagation();toggleLiveMarketExpand('${esc(cid)}')" class="expand-btn mono text-[11px] px-2 py-0.5 bg-[#1F2937] border border-[#374151] text-[#F9FAFB] cursor-pointer">${isExpanded ? '▲' : '▼'}</button></td>
        </tr>`;
        if (isExpanded) {
          for (const o of groupOrders) {
            const size = parseFloat(o.original_size).toFixed(2);
            const matched = parseFloat(o.size_matched).toFixed(2);
            const pct = o.original_size > 0 ? Math.min(100, Math.round((o.size_matched / o.original_size) * 100)) : 0;
            const price = parseFloat(o.price).toFixed(3);
            const age = formatDuration(o.age_sec);
            const tagClass = KNOWN_STATUSES.includes(o.status) ? o.status : 'open';
            html += `<tr class="market-detail-row" style="background:rgba(8,12,20,0.4);">
              <td style="padding-left:24px;"><strong>${esc(o.side || 'BUY')}</strong> <span style="font-size:10px;color:var(--text-muted);">${esc((o.token_id || '').slice(0, 10))}..</span></td>
              <td>$${price}</td>
              <td>${size}</td>
              <td><strong>${matched}</strong></td>
              <td>${pct}%</td>
              <td><span class="status-tag ${tagClass}">${esc(o.status)}</span></td>
              <td>${age}</td>
              <td></td>
            </tr>`;
          }
        }
      }
      tbody.innerHTML = html;
    }

    function renderCapital(state) {
      const cap = state.capital || {};
      document.getElementById('stat-total-committed').textContent = `$${(cap.total_committed || 0).toFixed(2)}`;
      document.getElementById('stat-resting-committed').textContent = `$${(cap.resting_committed || 0).toFixed(2)}`;
      document.getElementById('stat-filled-committed').textContent = `$${(cap.filled_committed || 0).toFixed(2)}`;

      const pairs = state.pairs || [];
      if (pairs.length > 0) {
        const p = pairs[0];
        document.getElementById('stat-pair-price').textContent = p.combined_price ? `$${p.combined_price.toFixed(3)}` : '--';
        document.getElementById('stat-max-pair-cost').textContent = p.max_pair_cost_at_post ? `$${p.max_pair_cost_at_post.toFixed(3)}` : '< $1.00';
      } else {
        document.getElementById('stat-pair-price').textContent = '--';
        document.getElementById('stat-max-pair-cost').textContent = '--';
      }
    }

    function renderFills(state) {
      const tbody = document.getElementById('fills-tbody');
      const badge = document.getElementById('fill-count-badge');
      const fills = state.fills || [];
      badge.textContent = `${fills.length} fill${fills.length === 1 ? '' : 's'}`;
      if (fills.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-state-text">No fills recorded yet</td></tr>';
        return;
      }
      const groups = new Map();
      for (const f of fills) {
        const cid = f.condition_id || 'unknown';
        if (!groups.has(cid)) groups.set(cid, []);
        groups.get(cid).push(f);
      }
      const sorted = [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
      let html = '';
      for (const [cid, groupFills] of sorted) {
        groupFills.sort((a, b) => b.venue_ts - a.venue_ts);
        const isExpanded = LIVE_EXPANDED_MARKETS.has(cid);
        const totalSize = groupFills.reduce((s, f) => s + parseFloat(f.size || 0), 0);
        const totalNotional = groupFills.reduce((s, f) => s + parseFloat(f.size || 0) * parseFloat(f.price || 0), 0);
        const avgPrice = totalSize > 0 ? totalNotional / totalSize : 0;
        const cidShort = (cid || '').slice(0, 14) + '..';
        html += `<tr class="market-agg-row" style="cursor:pointer;background:rgba(30,41,59,0.3);" onclick="toggleLiveMarketExpand('${esc(cid)}')">
          <td><strong>${esc(cidShort)}</strong><div style="font-size:10px;color:var(--text-muted);">${groupFills.length} fills</div></td>
          <td>&mdash;</td>
          <td><strong>${esc(groupFills[0]?.side || 'BUY')}</strong></td>
          <td>$${avgPrice.toFixed(3)}</td>
          <td>${totalSize.toFixed(1)} sh</td>
          <td><strong>$${totalNotional.toFixed(2)}</strong></td>
          <td><button type="button" onclick="event.stopPropagation();toggleLiveMarketExpand('${esc(cid)}')" class="expand-btn mono text-[11px] px-2 py-0.5 bg-[#1F2937] border border-[#374151] text-[#F9FAFB] cursor-pointer">${isExpanded ? '▲' : '▼'}</button></td>
        </tr>`;
        if (isExpanded) {
          for (const f of groupFills) {
            const notional = (f.size * f.price).toFixed(2);
            html += `<tr class="market-detail-row" style="background:rgba(8,12,20,0.4);">
              <td style="padding-left:24px;"><div>${esc(f.venue_time_str || formatTime(f.venue_ts))}</div><div style="font-size:10px;color:var(--text-muted);">${formatDuration(f.age_sec)} ago</div></td>
              <td><span style="font-size:11px;color:var(--text-secondary);">${esc((f.trade_id || '').slice(0, 12))}</span></td>
              <td><strong>${esc(f.side || 'BUY')}</strong></td>
              <td>$${parseFloat(f.price).toFixed(3)}</td>
              <td>${parseFloat(f.size).toFixed(2)}</td>
              <td><strong>$${notional}</strong></td>
              <td></td>
            </tr>`;
          }
        }
      }
      tbody.innerHTML = html;
    }

    function renderFreshnessAndLock(state) {
      const pollPill = document.getElementById('poll-pill');

      localLastPollMs = state.last_polled_ts;

      if (state.empty || !state.last_polled_ts) {
        pollPill.className = 'pill pill-neutral';
        pollPill.textContent = 'NO POLL DATA';
      } else {
        const secSince = state.seconds_since_poll || 0;

        if (state.stale && state.idle) {
          pollPill.className = 'pill pill-neutral';
          pollPill.textContent = 'IDLE — NO CYCLE RUNNING';
        } else if (state.stale) {
          pollPill.className = 'pill pill-stale';
          pollPill.textContent = `STALE (${Math.round(secSince)}s)`;
        } else {
          pollPill.className = 'pill pill-fresh';
          pollPill.textContent = `POLL OK (${Math.round(secSince)}s)`;
        }
      }

      // Reconcile lock
      const lock = state.reconcile_lock || {};
      const lockBox = document.getElementById('lock-container');
      const lockStatus = document.getElementById('lock-status-text');
      const lockDot = document.getElementById('lock-dot');

      if (lock.held) {
        lockBox.className = 'footer-lock lock-active';
        if (lockDot) lockDot.className = 'status-dot status-dot-sm dot-amber';
        lockStatus.textContent = 'HELD';
        lockBox.title = `Reconcile lock held by ${esc(lock.holder)} (acquired ${formatDuration(lock.age_sec)} ago)`;
      } else {
        lockBox.className = 'footer-lock lock-idle';
        if (lockDot) lockDot.className = 'status-dot status-dot-sm dot-online';
        lockStatus.textContent = 'Idle';
        lockBox.title = 'No reconcile pass in flight';
      }
    }

    function renderRunsSelector(kpi) {
      const sel = document.getElementById('run-selector');
      const runs = (kpi && kpi.runs) || [];
      const cur = selectedRunId || (kpi && kpi.active_run_id) || "";

      // Only rebuild if count changed or empty
      if (sel.options.length <= 1 && runs.length > 0) {
        sel.innerHTML = runs.map(r => {
          const pnlStr = (r.realized_pnl >= 0 ? '+' : '') + '$' + r.realized_pnl.toFixed(2);
          const timeStr = r.last_ts ? new Date(r.last_ts * 1000).toLocaleTimeString() : '';
          return `<option value="${esc(r.run_id)}">${esc(r.run_id)} (${pnlStr}, ${r.fills_count}f, ${timeStr})</option>`;
        }).join('') + '<option value="all">All Runs (Pooled)</option>';
        if (cur) sel.value = cur;
      }
    }

    document.getElementById('run-selector').addEventListener('change', (e) => {
      selectedRunId = e.target.value;
      pollState();
    });

    document.getElementById('port-pill').textContent =
      ':' + (window.location.port || (window.location.protocol === 'https:' ? '443' : '80'));

    async function fetchSystemStatus() {
      try {
        const res = await fetch('/api/system/status');
        if (!res.ok) return;
        const data = await res.json();
        renderSystemStatus(data);
      } catch (err) {
        console.error('Failed to fetch system status', err);
      }
    }

    function renderSystemStatus(data) {
      if (!data) return;
      const sup = data.supervisor || {};
      const supDot = document.getElementById('sup-dot');
      const supText = document.getElementById('sup-text');
      const supSub = document.getElementById('sup-sub');

      if (sup.running) {
        if (supDot) { supDot.className = 'status-dot dot-online'; }
        if (supText) { supText.textContent = 'ONLINE'; supText.style.color = '#34d399'; }
        if (supSub) { supSub.style.display = ''; supSub.textContent = `PID ${sup.pid || '--'}`; }
      } else {
        if (supDot) { supDot.className = 'status-dot dot-offline'; }
        if (supText) { supText.textContent = 'OFFLINE'; supText.style.color = '#f87171'; }
        if (supSub) { supSub.textContent = ''; supSub.style.display = 'none'; }
      }

      const s = data.services || {};
      const scr = s.screener || {};
      const eng = s.engine || {};

      const dotScr = document.getElementById('dot-screener');
      const txtScr = document.getElementById('txt-screener');
      if (dotScr) dotScr.className = scr.running ? 'status-dot status-dot-sm dot-online' : 'status-dot status-dot-sm dot-offline';
      if (txtScr) {
        txtScr.textContent = scr.running ? 'online' : 'offline';
        txtScr.style.color = scr.running ? '#34d399' : '#f87171';
      }

      const dotEng = document.getElementById('dot-engine');
      const txtEng = document.getElementById('txt-engine');
      if (dotEng) dotEng.className = eng.running ? 'status-dot status-dot-sm dot-online' : 'status-dot status-dot-sm dot-offline';
      if (txtEng) {
        txtEng.textContent = eng.running ? 'online' : 'offline';
        txtEng.style.color = eng.running ? '#34d399' : '#f87171';
      }

      const fleet = s.fleet || {};
      const dotFleet = document.getElementById('dot-fleet');
      const txtFleet = document.getElementById('txt-fleet');
      if (dotFleet) dotFleet.className = fleet.running ? 'status-dot status-dot-sm dot-online' : 'status-dot status-dot-sm dot-offline';
      if (txtFleet) {
        txtFleet.textContent = fleet.running ? 'online' : 'offline';
        txtFleet.style.color = fleet.running ? '#34d399' : '#f87171';
      }

      const txtEngSweep = document.getElementById('txt-engine-sweep');
      if (txtEngSweep) {
        const cfg = (eng.sweep_interval_sec == null) ? 'every tick' : `${eng.sweep_interval_sec}s`;
        let sweepLabel = `sweep: ${cfg}`;
        if (eng.running && eng.running_sweep_interval_sec !== undefined &&
            eng.running_sweep_interval_sec !== eng.sweep_interval_sec) {
          const running = (eng.running_sweep_interval_sec == null)
            ? 'every tick' : `${eng.running_sweep_interval_sec}s`;
          sweepLabel += ` · running ${running} (restart)`;
        }
        txtEngSweep.textContent = sweepLabel;
      }

      const sweepInput = document.getElementById('sweep-interval-input');
      if (sweepInput && document.activeElement !== sweepInput) {
        sweepInput.value = (eng.sweep_interval_sec == null) ? '' : eng.sweep_interval_sec;
      }

      const btnStart = document.getElementById('btn-start-bot');
      const btnStop = document.getElementById('btn-stop-bot');
      const isRunning = data.bot_state === 'RUNNING';
      if (btnStart) btnStart.disabled = isRunning;
      if (btnStop) btnStop.disabled = !isRunning;
    }

    async function setSweepInterval() {
      const input = document.getElementById('sweep-interval-input');
      const raw = input ? input.value.trim() : '';
      const path = raw
        ? '/api/system/sweep-interval?seconds=' + encodeURIComponent(raw)
        : '/api/system/sweep-interval';
      try {
        const res = await controlFetch(path);
        const data = await res.json();
        if (data.status) renderSystemStatus(data.status);
        if (!data.ok) alert(data.message || 'Failed to set sweep interval');
      } catch (err) {
        alert('Failed to set sweep interval: ' + err);
      }
    }

    function clearSweepInterval() {
      const input = document.getElementById('sweep-interval-input');
      if (input) input.value = '';
      setSweepInterval();
    }

    async function startBot() {
      const btnStart = document.getElementById('btn-start-bot');
      if (btnStart) btnStart.disabled = true;
      try {
        const res = await controlFetch('/api/system/start');
        const data = await res.json();
        if (data.status) renderSystemStatus(data.status);
      } catch (err) {
        alert('Failed to start bot: ' + err);
      } finally {
        fetchSystemStatus();
      }
    }

    async function stopBot() {
      const btnStop = document.getElementById('btn-stop-bot');
      if (btnStop) btnStop.disabled = true;
      try {
        const res = await controlFetch('/api/system/stop');
        const data = await res.json();
        if (data.status) renderSystemStatus(data.status);
      } catch (err) {
        alert('Failed to stop bot: ' + err);
      } finally {
        fetchSystemStatus();
      }
    }

    async function confirmResetDb() {
      if (!confirm("Are you sure you want to archive current live.db and create a FRESH database? All metrics and orders will start clean from 0.")) {
        return;
      }
      const btn = document.getElementById('btn-reset-db');
      if (btn) btn.disabled = true;
      try {
        const res = await controlFetch('/api/system/reset-db');
        const data = await res.json();
        alert(data.message || "Fresh database created successfully.");
        pollState();
      } catch (err) {
        alert("Failed to reset DB: " + err);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function venueSync() {
      const btn = document.getElementById('btn-venue-sync');
              if (btn) btn.disabled = true;
              const orig = btn ? btn.textContent : '';
              if (btn) btn.textContent = '⟳ Syncing...';
              try {
                const res = await controlFetch('/api/system/venue-sync');
                const data = await res.json();
                if (data.ok) {
                  const msg = `Venue sync complete.
Account: ` + (data.account_value_usd != null ? '$' + data.account_value_usd.toFixed(2) : '--') + `
Closed positions: ` + data.closed_positions_count + `
Open positions: ` + data.open_positions_count + `
Closes written: ` + data.closes_written + ` (skipped ` + data.closes_skipped_existing + ` existing)`;
                  alert(msg);
                  pollState();
                } else {
                  alert('Venue sync failed: ' + (data.message || data.error || 'unknown'));
                }
              } catch (err) {
                alert('Venue sync error: ' + err);
              } finally {
                if (btn) {
                  btn.disabled = false;
                  btn.textContent = orig || '⟳ Sync';
                }
              }
            }

            async function restartDash() {
      const btn = document.getElementById('btn-restart-dash');
      if (btn) btn.disabled = true;
      const pollPill = document.getElementById('poll-pill');
      if (pollPill) {
        pollPill.textContent = 'RESTARTING...';
        pollPill.className = 'pill pill-stale';
      }
      try {
        await controlFetch('/api/system/restart-dash');
      } catch (err) {
        // Ignored: server dropping connections on restart is expected
      }
      setTimeout(() => {
        window.location.reload();
      }, 1800);
    }

    // BOT BRAINS: live decision flow via Server-Sent Events.
    const bbSpark = [];
    let bbLastLine = null;
    let bbLogEl, bbDotEl, bbStateEl, bbSubEl, bbPillsEl, bbEvalEl, bbPassEl,
        bbFillEl, bbSkipsEl, bbCanvas;

    function bbSparkPush(v) {
      if (v === null || v === undefined || isNaN(v)) return;
      bbSpark.push(v);
      if (bbSpark.length > 60) bbSpark.shift();
      bbDrawSpark();
    }

    function bbDrawSpark() {
      if (!bbCanvas) return;
      const ctx = bbCanvas.getContext('2d');
      const w = bbCanvas.width, h = bbCanvas.height;
      ctx.clearRect(0, 0, w, h);
      if (!bbSpark.length) return;
      const max = Math.max(...bbSpark, 1);
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      bbSpark.forEach((v, i) => {
        const x = (i / Math.max(1, bbSpark.length - 1)) * (w - 2) + 1;
        const y = h - 2 - (v / max) * (h - 6);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    function bbSetScanState(kind, label, sub) {
      if (!bbDotEl) return;
      bbDotEl.className = 'bb-status-dot ' + kind;
      bbStateEl.className = 'bb-scan-state ' + kind;
      bbStateEl.textContent = label;
      bbSubEl.textContent = sub || '';
    }

    function bbApplyEvent(ev) {
      const line = JSON.stringify(ev);
      if (line === bbLastLine) return;
      bbLastLine = line;

      const service = ev.service || 'engine';
      const phase = ev.phase || 'idle';
      const pill = bbPillsEl.querySelector('.bb-pill[data-service="' + service + '"]');
      if (pill) {
        pill.innerHTML = esc(service) + ' &middot; <b>' + esc(phase) + '</b>';
        pill.classList.toggle('busy', !['idle', 'waiting'].includes(phase));
      }

      const phaseCls = 'ph-' + String(phase).replace(/[^a-z]/gi, '');
      const when = (ev.ts || '').slice(11, 19);
      const who = service.slice(0, 4);
      const market = ev.market_slug || '·';
      const reason = ev.reason ? ' — ' + ev.reason : '';
      const ms = (typeof ev.latency_ms === 'number' && ev.latency_ms > 0)
        ? '(' + ev.latency_ms + 'ms)' : '';
      const div = document.createElement('div');
      div.className = 'bb-log-line';
      div.innerHTML =
        '<span class="bb-log-ts">' + esc(when) + '</span> ' +
        '<span class="' + phaseCls + '">[' + esc(who) + ':' + esc(phase) + ']</span> ' +
        esc(ev.action || '') + ' ' + esc(market) + esc(reason) + ' ' +
        '<span class="bb-log-ms">' + ms + '</span>';
      bbLogEl.appendChild(div);
      while (bbLogEl.children.length > 12) bbLogEl.removeChild(bbLogEl.firstChild);
      bbLogEl.scrollTop = bbLogEl.scrollHeight;

      if (typeof ev.latency_ms === 'number' && ev.latency_ms > 0) bbSparkPush(ev.latency_ms);
      document.querySelectorAll('.kpi-tile').forEach(t => {
        t.classList.add('kpi-glow');
        clearTimeout(t._glowT);
        t._glowT = setTimeout(() => t.classList.remove('kpi-glow'), 700);
      });
    }

    // Guardrail banner: the newest guardrail_alert ring event rendered as a
    // red banner. A dismissed alert stays hidden while it remains the newest;
    // a new alert (new ts) re-shows. No alert in the ring hides the banner.
    let shownGuardrailTs = null;
    let dismissedGuardrailTs = null;
    function renderGuardrailBanner(payload) {
      const banner = document.getElementById('guardrail-banner');
      if (!banner) return;
      const alerts = (payload && payload.alerts) || [];
      const active = alerts.find(a => a && a.subject);
      if (!active) {
        banner.style.display = 'none';
        return;
      }
      shownGuardrailTs = active.ts;
      if (dismissedGuardrailTs === shownGuardrailTs) {
        banner.style.display = 'none';
        return;
      }
      document.getElementById('guardrail-kind').textContent = (active.kind || 'ALERT') + ': ';
      document.getElementById('guardrail-subject').textContent = active.subject;
      const t = (active.ts || '').slice(11, 19);
      const detail = active.detail || '';
      document.getElementById('guardrail-detail').textContent = detail ? '\u2014 ' + t + ' ' + detail : '\u2014 ' + t;
      banner.style.display = 'flex';
    }
    function dismissGuardrailBanner() {
      dismissedGuardrailTs = shownGuardrailTs;
      const banner = document.getElementById('guardrail-banner');
      if (banner) banner.style.display = 'none';
    }

    function renderGuardrailHealth(h) {
      const chip = document.getElementById('guardrail-health');
      const dot = document.getElementById('gh-dot');
      const text = document.getElementById('gh-text');
      if (!chip || !dot || !text) return;
      h = h || {};
      if (!h.running) {
        dot.className = 'gh-dot down';
        const age = (h.age_s != null) ? Math.round(h.age_s / 60) + 'm' : 'never';
        text.textContent = 'guardrail DOWN (last heartbeat ' + age + ' ago)';
        return;
      }
      dot.className = 'gh-dot alive';
      let label = 'guardrail ● pid ' + (h.pid != null ? h.pid : '?');
      if (h.alerts_total > 0) {
        label += ' · ' + h.alerts_total + ' alert' + (h.alerts_total === 1 ? '' : 's');
      }
      text.textContent = label;
    }

    function renderPairsActivity(pa) {
      if (!pa) return;
      const cycleEl = document.getElementById('bb-pairs-cycle');
      const countsEl = document.getElementById('bb-pairs-counts');
      const lastEl = document.getElementById('bb-pairs-last');
      if (!countsEl || !lastEl) return;
      const total = pa.totals || {};
      const cc = pa.last_cycle_counts || {};
      const hasCycle = pa.last_cycle !== null && pa.last_cycle !== undefined;
      if (cycleEl) cycleEl.textContent = hasCycle ? pa.last_cycle : '—';
      const c = (m, k) => (m[k] || 0);
      const errHtml = '<b class="pa-err">' + c(total, 'error') + '</b>';
      if (hasCycle) {
        countsEl.innerHTML =
          'complete <b>' + c(cc, 'completed') + '</b> &middot; exit <b>' + c(cc, 'exited') + '</b>' +
          ' &middot; would-comp <b>' + c(cc, 'would_complete') + '</b>' +
          ' &middot; would-exit <b>' + c(cc, 'would_exit') + '</b>' +
          ' &middot; err ' + errHtml +
          ' <span style="color:#64748b">(life: closed ' + (c(total,'completed') + c(total,'exited')) +
          ', would ' + (c(total,'would_complete') + c(total,'would_exit')) + ', err ' + c(total,'error') + ')</span>';
      } else {
        countsEl.innerHTML = 'closed <b>' + (c(total,'completed') + c(total,'exited')) +
          '</b> &middot; would <b>' + (c(total,'would_complete') + c(total,'would_exit')) +
          '</b> &middot; err ' + errHtml;
      }
      const rows = (pa.per_pair || []).slice()
        .sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')))
        .slice(0, 4).map(p => {
        const t = (p.ts || '').slice(11, 19);
        const cls = p.action === 'error' ? 'pa-err'
          : (p.action === 'completed' || p.action === 'exited') ? 'pa-ok' : 'pa-mute';
        return '<div>' + t + ' <span class="' + cls + '">' + esc(p.action) +
          '</span> ' + esc(String(p.pair_id).slice(0, 8)) + '</div>';
      }).join('');
      lastEl.innerHTML = rows || 'no pair actions yet';
    }

    function renderBotBrainsFunnel(kpi) {
      if (!bbEvalEl) return;
      const f = (kpi && kpi.funnel) || {};
      bbEvalEl.textContent = f.raw_count !== undefined ? f.raw_count : '--';
      bbPassEl.textContent = f.final_count !== undefined ? f.final_count : '--';
      const total = f.raw_count || 0;
      const passed = f.final_count || 0;
      bbFillEl.style.width = (total ? Math.round(100 * passed / total) : 0) + '%';
      const skips = (f.filters || []).map(fl => fl.cause + ' (' + fl.n + ')').slice(0, 3).join(' · ');
      bbSkipsEl.textContent = skips || 'no skips recorded';
    }

    function renderScanState(scanRes) {
      if (!scanRes || !scanRes.ok) {
        bbSetScanState('stalled', 'STALLED', 'scan-state unavailable');
        return;
      }
      scanRes.json().then(scan => {
        const kind = scan.scan_state === 'SCANNING' ? 'scanning'
          : scan.scan_state === 'STALLED' ? 'stalled' : 'idle';
        let sub = '';
        if (scan.seconds_since_scan !== null && scan.seconds_since_scan !== undefined) {
          sub = 'last scan ' + Math.round(scan.seconds_since_scan) + 's ago';
        }
        if (scan.scan_state === 'STALLED') {
          const hb = scan.seconds_since_heartbeat;
          sub = 'STALLED — no heartbeat for ' + (hb !== null && hb !== undefined ? Math.round(hb) + 's' : '?');
        }
        bbSetScanState(kind, scan.scan_state, sub);
        if (bbSkipsEl && scan.skip_reasons && scan.skip_reasons.length) {
          bbSkipsEl.textContent = scan.skip_reasons
            .map(s => s.reason + ' (' + s.count + ')').slice(0, 3).join(' · ');
        }
      }).catch(() => {});
    }

    function bbOpenStream() {
      bbLogEl = document.getElementById('bb-decision-log');
      bbDotEl = document.getElementById('bb-status-dot');
      bbStateEl = document.getElementById('bb-scan-state');
      bbSubEl = document.getElementById('bb-scan-sub');
      bbPillsEl = document.getElementById('bb-active-pills');
      bbEvalEl = document.getElementById('bb-evaluated');
      bbPassEl = document.getElementById('bb-passed');
      bbFillEl = document.getElementById('bb-funnel-fill');
      bbSkipsEl = document.getElementById('bb-funnel-skips');
      bbCanvas = document.getElementById('bb-sparkline');
      bbSetScanState('idle', 'CONNECTING', 'waiting for cycle stream');
      const es = new EventSource('/api/cycle-stream');
      es.onmessage = (m) => {
        try { bbApplyEvent(JSON.parse(m.data)); } catch (e) {}
      };
      es.addEventListener('rotate', () => {
        if (bbLogEl) bbLogEl.innerHTML = '';
        bbLastLine = null;
      });
      es.onerror = () => bbSetScanState('stalled', 'RECONNECTING', 'cycle stream dropped');
      es.onopen = () => bbSetScanState('idle', 'IDLE', 'stream connected');
    }      async function pollState() {
      try {
        const [stateRes, kpiRes, scanRes, pairsRes, guardrailRes, guardrailHealthRes] = await Promise.all([
          fetch('/api/state'),
          fetch(`/api/kpi${selectedRunId ? '?run_id=' + encodeURIComponent(selectedRunId) : ''}`),
          fetch('/api/scan-state'),
          fetch('/api/pairs-activity'),
          fetch('/api/guardrail-alerts'),
          fetch('/api/guardrail-health')
        ]);

        fetchSystemStatus();

        if (!stateRes.ok) throw new Error(`HTTP ${stateRes.status}`);
        const state = await stateRes.json();
        lastState = state;

        let kpi = {};
        if (kpiRes.ok) {
          kpi = await kpiRes.json();
          lastKpi = kpi;
        }

        document.getElementById('db-path-display').textContent = `DB: ${state.db_path || 'run/live.db'}`;
        renderRunsSelector(kpi);
        renderPortfolio(kpi);
        renderHero(state);
        renderRunKpis(kpi);
        renderExposureChart(kpi);
        renderFunnel(kpi);
        renderBotBrainsFunnel(kpi);
        renderScanState(scanRes);
        if (pairsRes.ok) renderPairsActivity(await pairsRes.json());
        if (guardrailRes.ok) renderGuardrailBanner(await guardrailRes.json());
        if (guardrailHealthRes.ok) renderGuardrailHealth(await guardrailHealthRes.json());
        renderMarketsTable(kpi);
        renderMechanics(kpi, state);
        renderOrders(state);
        renderCapital(state);
        renderFills(state);
        renderFreshnessAndLock(state);
      } catch (err) {
        const pollPill = document.getElementById('poll-pill');
        pollPill.className = 'pill pill-stale';
        pollPill.textContent = 'OFFLINE';
      }
    }

    // Local 1-second interval ticker for smooth counting up of timers and clock
    setInterval(() => {
      document.getElementById('clock-display').textContent = new Date().toLocaleTimeString();

      if (localNakedSinceMs) {
        const timerEl = document.getElementById('live-naked-timer');
        if (timerEl) {
          const sec = Math.max(0, Math.floor((Date.now() - localNakedSinceMs) / 1000));
          timerEl.textContent = formatDuration(sec);
        }
      }

      if (localLastPollMs && lastState && !lastState.empty) {
        const secSince = Math.max(0, (Date.now() - localLastPollMs) / 1000);
        if (secSince > 30 && !lastState.stale) {
          lastState.stale = true;
          renderFreshnessAndLock(lastState);
        }
      }
    }, 1000);

    // Initial poll and recurring loop
    bbOpenStream();
    pollState();
    setInterval(pollState, 2000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    """Serve the live execution monitor."""
    # The token is minted per process and only ever reaches the page this
    # process serves. PAGE_HTML keeps the placeholder so the constant itself
    # never carries a live credential.
    return HTMLResponse(
        PAGE_HTML.replace(CONTROL_TOKEN_PLACEHOLDER, CONTROL_TOKEN),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Spread Hunter Live Execution Monitor")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind (default: 8799)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--db", type=str, default=None, help="Path to live.db SQLite file")
    args = parser.parse_args()

    if args.db:
        set_db_override(args.db)

    global _ACTIVE_PORT
    _ACTIVE_PORT = args.port

    print(f"Starting Live Execution Dashboard on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
