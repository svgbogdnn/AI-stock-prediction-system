#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent
LOG_DIR = REPO_ROOT / "logs"
PID_DIR = REPO_ROOT / ".pids"
PID_FILE = PID_DIR / "system_pids.json"


def ts() -> str:
    return time.strftime("%H:%M:%S")


def log(level: str, message: str) -> None:
    print(f"{ts()} {level:<8} {message}", flush=True)


def load_dotenv(dotenv_path: Path) -> Dict[str, str]:
    """Minimal .env loader: KEY=VALUE; ignores empty lines and comments."""
    env: Dict[str, str] = {}
    if not dotenv_path.exists():
        return env

    for raw in dotenv_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def which_or_fail(executable: str) -> None:
    if shutil.which(executable) is None:
        raise RuntimeError(f"Not found in PATH: {executable}")


def resolve_npm_executable() -> str:
    if os.name == "nt":
        return shutil.which("npm.cmd") or shutil.which("npm") or "npm.cmd"
    return shutil.which("npm") or "npm"


def open_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a", encoding="utf-8", errors="replace")


def spawn_process(
    name: str,
    cmd: List[str],
    cwd: Path,
    env: Dict[str, str],
    log_path: Path,
) -> subprocess.Popen:
    out_f = open_log(log_path)

    kwargs = dict(cwd=str(cwd), env=env, stdout=out_f, stderr=out_f)

    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        raise RuntimeError(f"[{name}] Failed to start {cmd}: {exc}") from exc

    log("INFO", f"START {name:<15} pid={proc.pid} cmd={' '.join(cmd)}")
    return proc


def stop_pid_windows(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def stop_process(name: str, proc: subprocess.Popen, grace_seconds: float = 6.0) -> None:
    if proc.poll() is not None:
        return

    log("INFO", f"STOP  {name:<15} pid={proc.pid}")

    try:
        if os.name == "nt":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                pass

            deadline = time.time() + grace_seconds
            while time.time() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.25)

            stop_pid_windows(proc.pid)
        else:
            proc.terminate()
            deadline = time.time() + grace_seconds
            while time.time() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.25)
            proc.kill()
    except Exception:
        try:
            if os.name == "nt":
                stop_pid_windows(proc.pid)
            else:
                proc.kill()
        except Exception:
            pass


def http_ok(url: str, timeout_seconds: float = 5.0) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


def wait_http(
    url: str,
    timeout_seconds: float,
    label: str,
    request_timeout_seconds: float = 5.0,
) -> bool:
    log("INFO", f"Waiting for {label} (timeout {int(timeout_seconds)}s): {url}")
    deadline = time.time() + timeout_seconds
    last_log_time = 0.0

    while time.time() < deadline:
        if http_ok(url, timeout_seconds=request_timeout_seconds):
            log("INFO", f"{label} OK")
            return True

        if time.time() - last_log_time >= 3.0:
            elapsed = int(timeout_seconds - (deadline - time.time()))
            log("INFO", f"...waiting for {label} ({elapsed}s)")
            last_log_time = time.time()

        time.sleep(1.0)

    log("WARNING", f"Timed out waiting for {label} ({int(timeout_seconds)}s)")
    return False


def _python_supports_modules(python_exe: str, modules: List[str]) -> bool:
    script = (
        "import importlib.util as u\n"
        + "\n".join([f"assert u.find_spec('{module}') is not None, '{module}'" for module in modules])
    )
    try:
        result = subprocess.run(
            [python_exe, "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode == 0
    except Exception:
        return False


def resolve_service_python(repo_root: Path, fallback_python: str) -> str:
    """Pick a Python interpreter that can run agents/backend reliably."""
    candidates: List[Path] = []
    if os.name == "nt":
        candidates.extend(
            [
                repo_root / "venv" / "Scripts" / "python.exe",
                repo_root / ".venv" / "Scripts" / "python.exe",
            ]
        )
    else:
        candidates.extend(
            [
                repo_root / "venv" / "bin" / "python",
                repo_root / ".venv" / "bin" / "python",
            ]
        )

    must_have = ["google.adk", "fastapi", "uvicorn"]

    for candidate in candidates:
        if candidate.exists() and _python_supports_modules(str(candidate), must_have):
            return str(candidate)

    # Fallback to current interpreter if it has required modules.
    if _python_supports_modules(fallback_python, must_have):
        return fallback_python

    # Last resort: prefer local venv path if present to produce actionable error downstream.
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return fallback_python


def _pids_listening_on_port(port: int) -> List[int]:
    """Return process IDs currently LISTENING on the given TCP port."""
    pids: List[int] = []
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            pattern = re.compile(rf"^\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE)
            for line in proc.stdout.splitlines():
                match = pattern.match(line)
                if match:
                    pid = int(match.group(1))
                    if pid > 0:
                        pids.append(pid)
        else:
            proc = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for token in proc.stdout.splitlines():
                token = token.strip()
                if token.isdigit():
                    pid = int(token)
                    if pid > 0:
                        pids.append(pid)
    except Exception:
        pass
    return sorted(set(pids))


def free_ports(ports: List[int]) -> None:
    """Best-effort cleanup of processes occupying required ports."""
    current_pid = os.getpid()
    for port in ports:
        pids = _pids_listening_on_port(port)
        if not pids:
            continue

        for pid in pids:
            if pid == current_pid:
                continue
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
                else:
                    os.kill(pid, signal.SIGTERM)
                log("INFO", f"Freed port {port} by stopping PID {pid}")
            except Exception:
                log("WARNING", f"Could not stop PID {pid} on port {port}")


def main() -> int:
    try:
        which_or_fail("node")
        npm_exe = resolve_npm_executable()
        if shutil.which(npm_exe) is None:
            which_or_fail("npm.cmd" if os.name == "nt" else "npm")
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 2

    python_exe = sys.executable
    service_python = resolve_service_python(REPO_ROOT, python_exe)

    base_env: Dict[str, str] = dict(os.environ)
    base_env.update(load_dotenv(REPO_ROOT / ".env"))

    base_env["PYTHONUTF8"] = "1"
    base_env["PYTHONIOENCODING"] = "utf-8:replace"
    base_env["PYTHONUNBUFFERED"] = "1"
    base_env.setdefault("MCP_CONTEXT_HUB_PORT", "8010")
    base_env.setdefault("MCP_CONTEXT_HUB_URL", f"http://localhost:{base_env['MCP_CONTEXT_HUB_PORT']}/mcp")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)

    agents: List[Tuple[str, str, int]] = [
        ("fundamental", "agents/fundamental_analyst_server.py", 8001),
        ("technical", "agents/technical_analyst_server.py", 8002),
        ("news_sentiment", "agents/news_sentiment_analyst_server.py", 8003),
        ("macro", "agents/macro_analyst_server.py", 8004),
        ("regulatory", "agents/regulatory_analyst_server.py", 8005),
        ("predictor", "agents/predictor_agent_server.py", 8006),
    ]

    procs: Dict[str, subprocess.Popen] = {}

    def shutdown_all() -> None:
        for proc_name, proc in list(procs.items())[::-1]:
            stop_process(proc_name, proc)
        procs.clear()
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
        except Exception:
            pass

    def handle_signal(_signum, _frame) -> None:
        shutdown_all()
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass

    try:
        log("INFO", f"Repo: {REPO_ROOT}")
        log("INFO", f"Launcher Python: {python_exe}")
        log("INFO", f"Service Python:  {service_python}")

        if not _python_supports_modules(service_python, ["google.adk", "fastapi", "uvicorn"]):
            log("ERROR", f"Service Python is missing required modules: {service_python}")
            log("ERROR", "Install dependencies in project venv: .\\venv\\Scripts\\pip.exe install -r requirements.txt")
            return 8

        # Step 0: frontend deps presence (informational only)
        node_modules = REPO_ROOT / "frontend" / "node_modules"
        if node_modules.exists():
            log("INFO", "STEP 0/4: frontend deps already present (node_modules)")
        else:
            log("INFO", "STEP 0/4: node_modules missing - run npm install in frontend if needed")

        # Pre-cleanup: free known ports to avoid EADDRINUSE / stale processes
        free_ports([8010, 8000, 3001, 8001, 8002, 8003, 8004, 8005, 8006])

        # Step 1: MCP Context Hub
        mcp_port = int(base_env.get("MCP_CONTEXT_HUB_PORT", "8010"))
        mcp_script = REPO_ROOT / "mcp_context_hub" / "server.py"
        log("INFO", "STEP 1/4: start MCP Context Hub")

        if not mcp_script.exists():
            log("ERROR", f"MCP server script not found: {mcp_script}")
            shutdown_all()
            return 3

        procs["mcp_context_hub"] = spawn_process(
            name="mcp_context_hub",
            cmd=[service_python, str(mcp_script)],
            cwd=REPO_ROOT,
            env=base_env,
            log_path=LOG_DIR / "mcp_context_hub.log",
        )

        ok_mcp = wait_http(
            f"http://localhost:{mcp_port}/health",
            timeout_seconds=30.0,
            label="mcp_context_hub",
            request_timeout_seconds=3.0,
        )
        if not ok_mcp:
            log("ERROR", "MCP Context Hub failed to start. Check logs/mcp_context_hub.log")
            shutdown_all()
            return 3

        # Step 2: A2A agents
        log("INFO", "STEP 2/4: start A2A agents")
        for name, rel_path, _port in agents:
            script_path = REPO_ROOT / rel_path
            if not script_path.exists():
                log("WARNING", f"[{name}] file not found: {script_path}")
                continue

            procs[name] = spawn_process(
                name=name,
                cmd=[service_python, str(script_path)],
                cwd=REPO_ROOT,
                env=base_env,
                log_path=LOG_DIR / f"{name}.log",
            )

        # Non-fatal agent wait.
        for name, _rel_path, port in agents:
            ok = wait_http(
                f"http://localhost:{port}/.well-known/agent-card.json",
                timeout_seconds=20.0,
                label=f"agent:{name}:{port}",
                request_timeout_seconds=3.0,
            )
            if not ok:
                log("WARNING", f"Agent {name} ({port}) did not respond in time - continuing")

        # Step 3: backend
        log("INFO", "STEP 3/4: start backend (frontend_api.py -> :8000)")
        backend_path = REPO_ROOT / "frontend_api.py"
        if not backend_path.exists():
            log("ERROR", "frontend_api.py not found in project root")
            shutdown_all()
            return 4

        procs["backend"] = spawn_process(
            name="backend",
            cmd=[service_python, str(backend_path)],
            cwd=REPO_ROOT,
            env=base_env,
            log_path=LOG_DIR / "backend.log",
        )

        # Use root endpoint for readiness to avoid slow first /health initialization.
        if not wait_http(
            "http://localhost:8000/",
            timeout_seconds=120.0,
            label="backend",
            request_timeout_seconds=15.0,
        ):
            log("ERROR", "Backend failed to start. Check logs/backend.log")
            shutdown_all()
            return 5

        # Step 4: frontend
        log("INFO", "STEP 4/4: start frontend (Next.js dev -> :3001)")
        frontend_dir = REPO_ROOT / "frontend"
        if not frontend_dir.exists():
            log("ERROR", "frontend directory not found")
            shutdown_all()
            return 6

        frontend_env = dict(base_env)
        frontend_env.setdefault("PORT", "3001")
        frontend_env.setdefault("NEXT_TELEMETRY_DISABLED", "1")

        procs["frontend"] = spawn_process(
            name="frontend",
            cmd=[resolve_npm_executable(), "run", "dev"],
            cwd=frontend_dir,
            env=frontend_env,
            log_path=LOG_DIR / "frontend.log",
        )

        wait_http(
            "http://localhost:3001/",
            timeout_seconds=120.0,
            label="frontend",
            request_timeout_seconds=5.0,
        )

        try:
            PID_FILE.write_text(json.dumps({name: proc.pid for name, proc in procs.items()}, indent=2), encoding="utf-8")
        except Exception:
            pass

        log("INFO", "Frontend: http://localhost:3001")
        log("INFO", "Backend:  http://localhost:8000")
        log("INFO", f"MCP Hub:  {base_env.get('MCP_CONTEXT_HUB_URL')}")
        log("INFO", "Stop with Ctrl+C")

        while True:
            for critical in ("mcp_context_hub", "backend", "frontend"):
                proc = procs.get(critical)
                if proc and proc.poll() is not None:
                    log("ERROR", f"[{critical}] exited (code={proc.returncode}). Stopping system.")
                    shutdown_all()
                    return 7
            time.sleep(1.0)

    except KeyboardInterrupt:
        shutdown_all()
        return 0
    except SystemExit:
        shutdown_all()
        return 0
    except Exception as exc:
        log("ERROR", f"Unhandled error: {exc}")
        shutdown_all()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
