"""Check the RUNNING container against the isolation rules, and scan for leaked secrets.

    .venv/bin/python scripts/security_audit.py

Checks: the port is published on 127.0.0.1 only (and refused on the LAN address), the container has exactly one
mount (./data -> /data), runs read-only as a non-root user with every capability dropped and no-new-privileges,
and none of the secret values from .env appears in data/ or in the container logs. Secret VALUES are never
printed; only the names of any that were found.
"""
import json
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "forge"
SECRET_NAMES = ("GROQ_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "RAPIDAPI_KEY")


def run(*command: str) -> str:
    """Output of a command, stderr included (container logs are written to stderr)."""
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False).stdout


def lan_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
            return str(sock.getsockname()[0])
    except OSError:
        return None


def secret_values() -> dict[str, str]:
    env = ROOT / ".env"
    values: dict[str, str] = {}
    if env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() in SECRET_NAMES and len(value.strip()) >= 8:
                values[name.strip()] = value.strip().strip("\"'")
    return values


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []
    info = json.loads(run("podman", "inspect", NAME) or "[]")
    if not info:
        print(f"FAIL: container '{NAME}' is not running (start it with ./run.sh)")
        return 1
    c = info[0]
    host = c["HostConfig"]

    mounts = [(m["Type"], m["Destination"]) for m in c["Mounts"]]
    if mounts != [("bind", "/data")]:
        problems.append(f"mounts should be exactly one bind mount at /data, found {mounts}")
    ports = host["PortBindings"]
    bound = {(port, b["HostIp"], b["HostPort"]) for port, binds in ports.items() for b in binds}
    if bound != {("8000/tcp", "127.0.0.1", "8000")}:
        problems.append(f"the port must be published only as 127.0.0.1:8000, found {sorted(bound)}")
    if not host["ReadonlyRootfs"]:
        problems.append("the root filesystem is not read-only")
    if "no-new-privileges" not in " ".join(host.get("SecurityOpt") or []):
        problems.append("no-new-privileges is not set")
    if host.get("Privileged") or host.get("CapAdd"):
        problems.append("the container is privileged or adds capabilities")
    if len(host.get("CapDrop") or []) < 10 and "ALL" not in (host.get("CapDrop") or []):
        problems.append("capabilities are not all dropped")
    if run("podman", "exec", NAME, "id", "-u").strip() == "0":
        problems.append("the app runs as root")
    if "Read-only" not in subprocess.run(["podman", "exec", NAME, "sh", "-c", "touch /srv/forge/x 2>&1"], capture_output=True, text=True).stdout:
        problems.append("the install directory is writable")

    listening = [line for line in run("ss", "-ltn").splitlines() if ":8000" in line]
    if not listening or any(("127.0.0.1:8000" not in line) for line in listening):
        problems.append(f"port 8000 must listen on 127.0.0.1 only, found: {listening}")
    lan = lan_address()
    if lan and not lan.startswith("127."):
        try:
            socket.create_connection((lan, 8000), timeout=3).close()
            problems.append(f"port 8000 answers on the LAN address {lan}")
        except OSError:
            notes.append(f"port 8000 is refused on the LAN address {lan}")
    else:
        notes.append("no LAN address found to test")

    secrets = secret_values()
    haystacks = {"container logs": run("podman", "logs", NAME).encode()}
    for path in (ROOT / "data").rglob("*"):
        if path.is_file():
            haystacks[str(path.relative_to(ROOT))] = path.read_bytes()
    leaked = sorted({f"{name} (in {where})" for name, value in secrets.items() for where, data in haystacks.items() if value.encode() in data})
    if leaked:
        problems.append(f"secret values found: {leaked}")
    notes.append(f"scanned {len(haystacks)} places for {len(secrets)} secret value(s) from .env: none found" if not leaked else "")

    for note in filter(None, notes):
        print(f"ok   {note}")
    print(f"ok   mounts {mounts}; ports {sorted(bound)}; read-only root; non-root user" if not problems else "")
    for problem in problems:
        print(f"FAIL {problem}")
    print("PASS" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
