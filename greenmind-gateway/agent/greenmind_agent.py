#!/usr/bin/env python3
"""GreenMind Update Agent – Desired-state-based remote management for RPi gateways.

Runs as a separate systemd service (greenmind-agent) under the greenmind-agent user.
Polls the cloud for desired state, applies updates atomically, executes allowlisted
commands, and reports health/status back.

Security model:
- Runs as unprivileged greenmind-agent user
- Only systemctl restart/status and reboot via sudoers
- SHA256 verification of all artifacts
- Optional Ed25519 signature verification
- Download to /tmp, verify, then move to final path
- Global flock prevents concurrent updates
- No shell execution, no arbitrary commands
"""

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Constants ────────────────────────────────────────────────────────

AGENT_VERSION = "1.0.0"

BASE_DIR = Path("/opt/greenmind")
RELEASES_DIR = BASE_DIR / "releases"
CURRENT_LINK = BASE_DIR / "current"
AGENT_DIR = BASE_DIR / "agent"
CONFIG_DIR = BASE_DIR / "config"
CONFIG_VERSIONS_DIR = CONFIG_DIR / "versions"
BACKUPS_DIR = BASE_DIR / "backups"
DATA_DIR = BASE_DIR / "data"
SECRETS_PATH = DATA_DIR / "secrets.json"

STATE_FILE = AGENT_DIR / "agent_state.json"
LOCK_FILE = AGENT_DIR / "update.lock"
SIGNING_KEY_PATH = AGENT_DIR / "signing_key.pub"

POLL_INTERVAL = 30  # seconds
MAX_BACKOFF = 300  # 5 minutes
HEALTHCHECK_TIMEOUT = 15  # seconds after restart
KEEP_RELEASES = 3
MIN_DISK_MARGIN_MB = 100

GATEWAY_SERVICE = "greenmind-gateway"

ALLOWED_COMMANDS = {
    "restart_gateway_service",
    "reload_gateway_config",
    "enable_maintenance_mode",
    "disable_maintenance_mode",
    "controlled_reboot",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("greenmind-agent")


# ── State Persistence ────────────────────────────────────────────────


def load_state() -> dict:
    """Load persistent agent state from disk."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load agent state: %s", exc)
    return {}


def save_state(state: dict) -> None:
    """Persist agent state to disk."""
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except OSError as exc:
        logger.error("Could not save agent state: %s", exc)


def load_secrets() -> dict:
    """Load gateway credentials from secrets.json."""
    try:
        return json.loads(SECRETS_PATH.read_text())
    except (json.JSONDecodeError, FileNotFoundError, OSError) as exc:
        logger.error("Cannot read secrets: %s", exc)
        return {}


# ── Update Window ────────────────────────────────────────────────────


def is_in_update_window(
    window_start: str | None,
    window_end: str | None,
    tz_name: str = "UTC",
) -> bool:
    """Check if the current time falls within the update window.

    Returns True if no window is configured (null = anytime).
    """
    if not window_start or not window_end:
        return True

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        from datetime import timezone as _tz

        tz = _tz.utc

    now = datetime.now(tz)
    current_minutes = now.hour * 60 + now.minute

    start_parts = window_start.split(":")
    end_parts = window_end.split(":")
    start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
    end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])

    if start_minutes <= end_minutes:
        # Normal window: e.g. 02:00–04:00
        return start_minutes <= current_minutes <= end_minutes
    else:
        # Overnight window: e.g. 23:00–03:00
        return current_minutes >= start_minutes or current_minutes <= end_minutes


# ── Signature Verification ───────────────────────────────────────────


def verify_signature(sha256_hex: str, signature_b64: str | None) -> str:
    """Verify Ed25519 signature of the SHA256 hash.

    Returns: 'signed', 'unsigned', or 'invalid'.
    """
    if not signature_b64:
        return "unsigned"

    if not SIGNING_KEY_PATH.exists():
        logger.warning("No signing key found at %s — skipping signature check", SIGNING_KEY_PATH)
        return "unsigned"

    try:
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        key_data = SIGNING_KEY_PATH.read_bytes()
        public_key = load_pem_public_key(key_data)

        if not isinstance(public_key, Ed25519PublicKey):
            logger.error("Signing key is not Ed25519")
            return "invalid"

        signature_bytes = base64.b64decode(signature_b64)
        public_key.verify(signature_bytes, sha256_hex.encode("utf-8"))
        logger.info("Signature verification passed")
        return "signed"
    except ImportError:
        logger.warning("cryptography library not installed — skipping signature check")
        return "unsigned"
    except Exception as exc:
        logger.error("Signature verification FAILED: %s", exc)
        return "invalid"


# ── Disk Check ───────────────────────────────────────────────────────


def get_disk_free_mb() -> int:
    """Get free disk space in MB for the greenmind partition."""
    try:
        stat = os.statvfs(str(BASE_DIR))
        return (stat.f_bavail * stat.f_frsize) // (1024 * 1024)
    except OSError:
        return -1


def check_disk_space(required_bytes: int | None) -> bool:
    """Verify sufficient disk space: required * 2 + margin."""
    free_mb = get_disk_free_mb()
    if free_mb < 0:
        return True  # Can't determine, proceed cautiously

    required_mb = 0
    if required_bytes:
        required_mb = (required_bytes * 2) // (1024 * 1024)

    needed = required_mb + MIN_DISK_MARGIN_MB
    if free_mb < needed:
        logger.error("Insufficient disk space: %d MB free, %d MB needed", free_mb, needed)
        return False
    return True


# ── Lock Manager ─────────────────────────────────────────────────────


class LockManager:
    """Global file lock to prevent concurrent updates/commands."""

    def __init__(self):
        self._fd = None

    def acquire(self) -> bool:
        try:
            LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._fd = open(LOCK_FILE, "w")
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(os.getpid()))
            self._fd.flush()
            return True
        except (OSError, BlockingIOError):
            logger.warning("Could not acquire update lock — another operation in progress")
            return False

    def release(self) -> None:
        if self._fd:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except OSError:
                pass
            self._fd = None


# ── Health Checks ────────────────────────────────────────────────────


def run_healthcheck_suite() -> tuple[bool, str]:
    """Run the 6-point healthcheck suite. Returns (passed, details)."""
    checks = {}

    # 1. Process check
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "is-active", GATEWAY_SERVICE],
            capture_output=True,
            text=True,
            timeout=10,
        )
        checks["process"] = result.stdout.strip() == "active"
    except Exception:
        checks["process"] = False

    # 2. HTTP API check
    try:
        resp = httpx.get("http://localhost:80/api/v1/health", timeout=5.0)
        checks["http_api"] = resp.status_code == 200
    except Exception:
        # Try common alternative port
        try:
            resp = httpx.get("http://localhost:8080/api/v1/health", timeout=5.0)
            checks["http_api"] = resp.status_code == 200
        except Exception:
            checks["http_api"] = False

    # 3. Config valid
    config_link = CONFIG_DIR / "active.json"
    checks["config_valid"] = config_link.exists() and _is_valid_json(config_link)

    # 4. Disk check
    free_mb = get_disk_free_mb()
    checks["disk"] = free_mb > 100 if free_mb >= 0 else True

    # 5. Current symlink valid
    checks["symlink"] = CURRENT_LINK.is_symlink() and CURRENT_LINK.resolve().is_dir()

    passed = all(checks.values())
    details = json.dumps(checks)
    level = logging.INFO if passed else logging.WARNING
    logger.log(level, "Healthcheck: %s → %s", "PASSED" if passed else "FAILED", details)
    return passed, details


def _is_valid_json(path: Path) -> bool:
    try:
        json.loads(path.read_text())
        return True
    except Exception:
        return False


# ── App Updater ──────────────────────────────────────────────────────


def download_release(
    client: httpx.Client,
    base_url: str,
    artifact_url: str,
    expected_sha256: str,
    api_key: str,
) -> Path | None:
    """Download release tarball to a temp directory and verify SHA256.

    Returns the path to the verified tarball, or None on failure.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="greenmind_release_"))
    tmp_file = tmp_dir / "release.tar.gz"

    try:
        download_url = f"{base_url}{artifact_url}"
        logger.info("Downloading release from %s", artifact_url)

        with client.stream("GET", download_url, headers={"X-Api-Key": api_key}) as resp:
            if resp.status_code != 200:
                logger.error("Download failed: HTTP %d", resp.status_code)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return None

            hasher = hashlib.sha256()
            with open(tmp_file, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    hasher.update(chunk)

        actual_sha256 = hasher.hexdigest()
        if actual_sha256 != expected_sha256:
            logger.error(
                "SHA256 MISMATCH: expected %s, got %s", expected_sha256, actual_sha256
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        logger.info("Download verified: SHA256 %s", actual_sha256)
        return tmp_file

    except Exception as exc:
        logger.error("Download failed: %s", exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


def apply_app_update(tarball_path: Path, version: str, state: dict) -> bool:
    """Extract, install wheels, symlink switch, restart, and healthcheck.

    Returns True on success, False triggers rollback.
    """
    release_dir = RELEASES_DIR / version

    try:
        # 1. Extract tarball
        RELEASES_DIR.mkdir(parents=True, exist_ok=True)
        if release_dir.exists():
            shutil.rmtree(release_dir)

        with tarfile.open(tarball_path, "r:gz") as tar:
            # Security: prevent path traversal
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    logger.error("Tarball contains unsafe path: %s", member.name)
                    return False
            tar.extractall(path=str(release_dir))

        logger.info("Extracted release to %s", release_dir)

        # 2. Create venv and install from bundled wheels (no internet)
        venv_dir = release_dir / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True,
            timeout=60,
        )

        pip_path = venv_dir / "bin" / "pip"
        req_file = release_dir / "requirements.lock"
        wheels_dir = release_dir / "wheels"

        if wheels_dir.exists() and req_file.exists():
            # Offline install from bundled wheels
            subprocess.run(
                [
                    str(pip_path),
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheels_dir),
                    "-r",
                    str(req_file),
                ],
                check=True,
                timeout=300,
                capture_output=True,
            )
            logger.info("Installed dependencies from bundled wheels")
        elif req_file.exists():
            # Fallback: online install (legacy tarballs without wheels)
            subprocess.run(
                [str(pip_path), "install", "-r", str(req_file)],
                check=True,
                timeout=300,
                capture_output=True,
            )
            logger.warning("Installed dependencies from PyPI (no wheels bundled)")

        # 3. Save current symlink target for rollback
        previous = None
        if CURRENT_LINK.is_symlink():
            previous = str(CURRENT_LINK.resolve())
            state["previous_release"] = previous

        # 4. Atomic symlink switch
        tmp_link = CURRENT_LINK.parent / f".current_tmp_{os.getpid()}"
        tmp_link.symlink_to(release_dir)
        tmp_link.rename(CURRENT_LINK)
        logger.info("Symlink switched: current → %s", version)

        # 5. Restart gateway service
        subprocess.run(
            ["sudo", "systemctl", "restart", GATEWAY_SERVICE],
            check=True,
            timeout=30,
        )
        logger.info("Gateway service restarted")

        # 6. Wait and run healthcheck
        time.sleep(HEALTHCHECK_TIMEOUT)
        passed, details = run_healthcheck_suite()

        if passed:
            # Write release metadata
            meta = {
                "version": version,
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "previous": previous,
            }
            (release_dir / ".release_meta.json").write_text(json.dumps(meta, indent=2))
            return True

        # 7. Healthcheck failed → rollback
        logger.error("Healthcheck FAILED after update — initiating rollback")
        if previous:
            return _rollback_to(Path(previous), state)
        return False

    except Exception as exc:
        logger.error("App update failed: %s", exc, exc_info=True)
        previous = state.get("previous_release")
        if previous:
            _rollback_to(Path(previous), state)
        return False
    finally:
        # Clean up temp download
        tmp_parent = tarball_path.parent
        if tmp_parent.name.startswith("greenmind_release_"):
            shutil.rmtree(tmp_parent, ignore_errors=True)


def _rollback_to(previous_dir: Path, state: dict) -> bool:
    """Revert the current symlink to a previous release and restart."""
    try:
        if not previous_dir.exists():
            logger.error("Rollback target does not exist: %s", previous_dir)
            return False

        tmp_link = CURRENT_LINK.parent / f".current_rollback_{os.getpid()}"
        tmp_link.symlink_to(previous_dir)
        tmp_link.rename(CURRENT_LINK)

        subprocess.run(
            ["sudo", "systemctl", "restart", GATEWAY_SERVICE],
            check=True,
            timeout=30,
        )
        logger.info("Rolled back to %s and restarted", previous_dir.name)
        state["last_rollback"] = datetime.now(timezone.utc).isoformat()
        return True
    except Exception as exc:
        logger.critical("ROLLBACK FAILED: %s", exc)
        return False


# ── Config Updater ───────────────────────────────────────────────────


def download_config(
    client: httpx.Client,
    base_url: str,
    artifact_url: str,
    expected_sha256: str,
    api_key: str,
) -> dict | None:
    """Download config JSON and verify SHA256."""
    try:
        url = f"{base_url}{artifact_url}"
        resp = client.get(url, headers={"X-Api-Key": api_key})
        if resp.status_code != 200:
            logger.error("Config download failed: HTTP %d", resp.status_code)
            return None

        data = resp.json()
        payload = data.get("config_payload", data)

        # Verify SHA256
        serialised = json.dumps(payload, sort_keys=True)
        actual = hashlib.sha256(serialised.encode()).hexdigest()
        if actual != expected_sha256:
            logger.error("Config SHA256 mismatch: expected %s, got %s", expected_sha256, actual)
            return None

        return payload
    except Exception as exc:
        logger.error("Config download failed: %s", exc)
        return None


def apply_config_update(payload: dict, version: str, app_version: str | None) -> bool:
    """Validate, backup, and atomically apply a config update."""
    try:
        CONFIG_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

        # 1. Validate JSON structure (basic — extend with Pydantic if schema available)
        if not isinstance(payload, dict):
            logger.error("Config payload is not a dict")
            return False

        # 2. Backup current config
        active_link = CONFIG_DIR / "active.json"
        if active_link.exists():
            backup_path = BACKUPS_DIR / "last_good_config.json"
            try:
                resolved = active_link.resolve()
                shutil.copy2(str(resolved), str(backup_path))
                logger.info("Backed up current config to %s", backup_path)
            except Exception as exc:
                logger.warning("Config backup failed: %s", exc)

        # 3. Write new config version
        config_file = CONFIG_VERSIONS_DIR / f"{version}.json"
        config_file.write_text(json.dumps(payload, indent=2))

        # 4. Atomic symlink switch
        tmp_link = CONFIG_DIR / f".active_tmp_{os.getpid()}"
        tmp_link.symlink_to(config_file)
        tmp_link.rename(active_link)
        logger.info("Config switched to version %s", version)

        # 5. Restart gateway to reload config
        subprocess.run(
            ["sudo", "systemctl", "restart", GATEWAY_SERVICE],
            check=True,
            timeout=30,
        )

        # 6. Healthcheck
        time.sleep(10)
        passed, _ = run_healthcheck_suite()

        if passed:
            return True

        # 7. Rollback config
        logger.error("Healthcheck FAILED after config update — rolling back")
        backup = BACKUPS_DIR / "last_good_config.json"
        if backup.exists():
            tmp_link = CONFIG_DIR / f".active_rollback_{os.getpid()}"
            tmp_link.symlink_to(backup)
            tmp_link.rename(active_link)
            subprocess.run(
                ["sudo", "systemctl", "restart", GATEWAY_SERVICE],
                check=True,
                timeout=30,
            )
            logger.info("Config rolled back to last good version")
        return False

    except Exception as exc:
        logger.error("Config update failed: %s", exc)
        return False


# ── Command Executor ─────────────────────────────────────────────────


def execute_command(cmd: dict, state: dict) -> tuple[str, str]:
    """Execute an allowlisted command. Returns (result, message)."""
    cmd_type = cmd.get("command_type", "")
    cmd_id = cmd.get("id", "")

    if cmd_type not in ALLOWED_COMMANDS:
        logger.warning("Rejected unknown command: %s", cmd_type)
        return "rejected", f"Command '{cmd_type}' not in allowlist"

    logger.info("Executing command: %s (id=%s)", cmd_type, cmd_id)

    try:
        if cmd_type == "restart_gateway_service":
            subprocess.run(
                ["sudo", "systemctl", "restart", GATEWAY_SERVICE],
                check=True,
                timeout=30,
            )
            return "executed", "Gateway service restarted"

        elif cmd_type == "reload_gateway_config":
            subprocess.run(
                ["sudo", "systemctl", "restart", GATEWAY_SERVICE],
                check=True,
                timeout=30,
            )
            return "executed", "Config reloaded via service restart"

        elif cmd_type == "enable_maintenance_mode":
            state["maintenance_mode_local"] = True
            save_state(state)
            return "executed", "Maintenance mode enabled locally"

        elif cmd_type == "disable_maintenance_mode":
            state["maintenance_mode_local"] = False
            save_state(state)
            return "executed", "Maintenance mode disabled locally"

        elif cmd_type == "controlled_reboot":
            logger.warning("Controlled reboot requested — rebooting in 5 seconds")
            time.sleep(5)
            subprocess.run(["sudo", "reboot"], check=False, timeout=10)
            return "executed", "Reboot initiated"

        return "rejected", f"No handler for {cmd_type}"

    except subprocess.TimeoutExpired:
        return "failed", f"Command '{cmd_type}' timed out"
    except subprocess.CalledProcessError as exc:
        return "failed", f"Command '{cmd_type}' failed: exit code {exc.returncode}"
    except Exception as exc:
        return "failed", f"Command '{cmd_type}' error: {exc}"


# ── Release Cleanup ──────────────────────────────────────────────────


def cleanup_old_releases() -> None:
    """Keep only KEEP_RELEASES most recent releases."""
    if not RELEASES_DIR.exists():
        return

    current_target = None
    if CURRENT_LINK.is_symlink():
        current_target = CURRENT_LINK.resolve()

    releases = sorted(
        [d for d in RELEASES_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    if len(releases) <= KEEP_RELEASES:
        return

    to_delete = releases[KEEP_RELEASES:]
    for release_dir in to_delete:
        if current_target and release_dir.resolve() == current_target:
            continue  # Never delete the active release
        logger.info("Cleaning up old release: %s", release_dir.name)
        shutil.rmtree(release_dir, ignore_errors=True)


# ── State Reporter ───────────────────────────────────────────────────


def report_state(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    state: dict,
    *,
    status: str = "idle",
    last_error: str | None = None,
) -> None:
    """Report current agent state to the cloud."""
    try:
        # Determine current app version from symlink
        app_version = None
        if CURRENT_LINK.is_symlink():
            app_version = CURRENT_LINK.resolve().name

        config_version = None
        active_config = CONFIG_DIR / "active.json"
        if active_config.is_symlink():
            config_version = active_config.resolve().stem

        payload = {
            "gateway_id": state.get("gateway_id", ""),
            "app_version": app_version,
            "config_version": config_version,
            "agent_version": AGENT_VERSION,
            "status": status,
            "health_status": state.get("health_status", "unknown"),
            "disk_free_mb": get_disk_free_mb(),
            "uptime_seconds": _get_uptime(),
            "last_error": last_error,
            "update_download_status": state.get("update_download_status", "none"),
            "update_apply_status": state.get("update_apply_status", "none"),
            "signature_status": state.get("signature_status"),
        }

        # Add system metrics where available
        try:
            import psutil

            payload["cpu_temp_c"] = _read_cpu_temp()
            payload["ram_usage_pct"] = round(psutil.virtual_memory().percent, 1)
        except ImportError:
            pass

        resp = client.post(
            f"{base_url}/api/v1/gateway/state-report",
            json=payload,
            headers={"X-Api-Key": api_key},
        )
        if resp.status_code == 200:
            logger.debug("State report sent")
        else:
            logger.warning("State report failed: HTTP %d", resp.status_code)
    except Exception as exc:
        logger.debug("State report error: %s", exc)


def report_command_result(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    gateway_id: str,
    command_id: str,
    result: str,
    message: str,
) -> None:
    """Report command execution result to the cloud."""
    try:
        client.post(
            f"{base_url}/api/v1/gateway/command-result",
            json={
                "gateway_id": gateway_id,
                "command_id": command_id,
                "result": result,
                "message": message,
            },
            headers={"X-Api-Key": api_key},
        )
    except Exception as exc:
        logger.debug("Command result report error: %s", exc)


def _get_uptime() -> int | None:
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


def _read_cpu_temp() -> float | None:
    try:
        path = "/sys/class/thermal/thermal_zone0/temp"
        if os.path.exists(path):
            with open(path, "r") as fh:
                return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    return None


# ── Main Agent Loop ──────────────────────────────────────────────────


def main() -> None:
    """Main agent loop: poll, compare, apply, report."""

    logger.info("GreenMind Update Agent v%s starting", AGENT_VERSION)

    # Load credentials
    secrets = load_secrets()
    api_key = secrets.get("api_key")
    gateway_id = secrets.get("gateway_id")
    server_url = secrets.get("server_url", "")

    if not api_key or not gateway_id:
        logger.error("No credentials found in %s — agent cannot start", SECRETS_PATH)
        sys.exit(1)

    # Determine cloud base URL — strip /api/v1 if already present in secrets
    base_url = server_url.rstrip("/") if server_url else "https://green-mind.ch"
    for suffix in ("/api/v1", "/api/v1/"):
        if base_url.endswith(suffix.rstrip("/")):
            base_url = base_url[: -len(suffix.rstrip("/"))]
            break

    state = load_state()
    state["gateway_id"] = gateway_id
    backoff = POLL_INTERVAL
    lock = LockManager()

    # Ensure directories exist
    for d in [RELEASES_DIR, CONFIG_VERSIONS_DIR, BACKUPS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=30.0, verify=True) as client:
        while True:
            try:
                # 1. Poll desired state
                current_app = None
                if CURRENT_LINK.is_symlink():
                    current_app = CURRENT_LINK.resolve().name

                current_config = None
                active_config = CONFIG_DIR / "active.json"
                if active_config.is_symlink():
                    current_config = active_config.resolve().stem

                resp = client.get(
                    f"{base_url}/api/v1/gateway/desired-state",
                    params={
                        "current_app_version": current_app,
                        "current_config_version": current_config,
                        "current_agent_version": AGENT_VERSION,
                    },
                    headers={"X-Api-Key": api_key},
                )

                if resp.status_code != 200:
                    logger.warning("Desired state poll returned %d", resp.status_code)
                    report_state(client, base_url, api_key, state, status="poll_failed")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    continue

                desired = resp.json()
                backoff = POLL_INTERVAL  # Reset backoff on success

                # 2. Check if blocked
                if desired.get("blocked"):
                    logger.info("Gateway is blocked — skipping updates")
                    report_state(client, base_url, api_key, state, status="blocked")
                    time.sleep(POLL_INTERVAL)
                    continue

                # 3. Check maintenance mode
                if desired.get("maintenance_mode"):
                    state["maintenance_mode_local"] = True
                    report_state(client, base_url, api_key, state, status="maintenance")
                    time.sleep(POLL_INTERVAL)
                    continue

                # 4. Handle app update
                if desired.get("app_update_available"):
                    _handle_app_update(client, base_url, api_key, desired, state, lock)

                # 5. Handle config update
                if desired.get("config_update_available"):
                    _handle_config_update(client, base_url, api_key, desired, state, lock)

                # 6. Execute pending commands
                for cmd in desired.get("pending_commands", []):
                    if not lock.acquire():
                        logger.warning("Skipping command — lock held")
                        continue
                    try:
                        # Check reboot restrictions
                        cmd_type = cmd.get("command_type", "")
                        if cmd_type == "controlled_reboot":
                            if not desired.get("reboot_allowed"):
                                report_command_result(
                                    client, base_url, api_key, gateway_id,
                                    str(cmd["id"]), "rejected", "Reboot not allowed",
                                )
                                continue
                            if not desired.get("allow_reboot_outside_window") and not is_in_update_window(
                                desired.get("update_window_start"),
                                desired.get("update_window_end"),
                                desired.get("update_timezone", "UTC"),
                            ):
                                report_command_result(
                                    client, base_url, api_key, gateway_id,
                                    str(cmd["id"]), "rejected", "Reboot outside update window",
                                )
                                continue

                        result, message = execute_command(cmd, state)
                        report_command_result(
                            client, base_url, api_key, gateway_id,
                            str(cmd["id"]), result, message,
                        )
                    finally:
                        lock.release()

                # 7. Report current state
                passed, _ = run_healthcheck_suite()
                state["health_status"] = "healthy" if passed else "degraded"
                report_state(client, base_url, api_key, state, status="idle")

            except httpx.HTTPError as exc:
                logger.warning("Cloud connection failed: %s", exc)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception as exc:
                logger.error("Agent loop error: %s", exc, exc_info=True)
                backoff = min(backoff * 2, MAX_BACKOFF)

            save_state(state)
            time.sleep(backoff)


def _handle_app_update(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    desired: dict,
    state: dict,
    lock: LockManager,
) -> None:
    """Handle the full app update lifecycle: download → verify → window → apply."""
    version = desired.get("desired_app_version", "")
    artifact_url = desired.get("app_artifact_url", "")
    sha256 = desired.get("app_sha256", "")
    signature = desired.get("app_signature")
    file_size = desired.get("app_file_size_bytes")
    mandatory = desired.get("app_mandatory", False)

    gateway_id = state.get("gateway_id", "")

    # Phase 1: Download (allowed outside window if configured)
    can_download = desired.get("allow_download_outside_window", True) or is_in_update_window(
        desired.get("update_window_start"),
        desired.get("update_window_end"),
        desired.get("update_timezone", "UTC"),
    )

    cached_tarball = state.get("cached_tarball")
    cached_version = state.get("cached_version")

    if cached_version == version and cached_tarball and Path(cached_tarball).exists():
        logger.info("Using cached download for version %s", version)
    elif can_download:
        # Disk pre-check
        if not check_disk_space(file_size):
            state["update_download_status"] = "disk_insufficient"
            report_state(client, base_url, api_key, state, status="disk_insufficient")
            return

        tarball = download_release(client, base_url, artifact_url, sha256, api_key)
        if not tarball:
            state["update_download_status"] = "failed"
            report_state(client, base_url, api_key, state, status="download_failed")
            return

        # Verify signature
        sig_status = verify_signature(sha256, signature)
        state["signature_status"] = sig_status
        if sig_status == "invalid":
            logger.error("REJECTING update %s — invalid signature", version)
            shutil.rmtree(tarball.parent, ignore_errors=True)
            state["update_download_status"] = "signature_invalid"
            report_state(
                client, base_url, api_key, state,
                status="signature_invalid", last_error="Ed25519 signature invalid",
            )
            return

        state["cached_tarball"] = str(tarball)
        state["cached_version"] = version
        state["update_download_status"] = "downloaded"
        save_state(state)
        logger.info("Download complete: %s (signature: %s)", version, sig_status)
    else:
        logger.info("Download not allowed outside update window")
        return

    # Phase 2: Apply (only in window unless mandatory)
    in_window = is_in_update_window(
        desired.get("update_window_start"),
        desired.get("update_window_end"),
        desired.get("update_timezone", "UTC"),
    )
    can_apply = desired.get("allow_apply_outside_window", False) or in_window or mandatory

    if not can_apply:
        state["update_apply_status"] = "pending_window"
        report_state(client, base_url, api_key, state, status="pending_window")
        logger.info("Update %s downloaded but waiting for update window", version)
        return

    # Acquire lock for the actual apply
    if not lock.acquire():
        return

    try:
        state["update_apply_status"] = "applying"
        save_state(state)
        report_state(client, base_url, api_key, state, status="apply_started")

        tarball_path = Path(state["cached_tarball"])
        success = apply_app_update(tarball_path, version, state)

        if success:
            state["update_apply_status"] = "applied"
            state["update_download_status"] = "none"
            state.pop("cached_tarball", None)
            state.pop("cached_version", None)
            report_state(client, base_url, api_key, state, status="apply_success")
            cleanup_old_releases()
            logger.info("App update to %s completed successfully", version)
        else:
            state["update_apply_status"] = "failed"
            report_state(
                client, base_url, api_key, state,
                status="apply_failed", last_error="Update failed, rolled back",
            )
    finally:
        lock.release()


def _handle_config_update(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    desired: dict,
    state: dict,
    lock: LockManager,
) -> None:
    """Handle config update: download → validate → apply."""
    version = desired.get("desired_config_version", "")
    artifact_url = desired.get("config_artifact_url", "")
    sha256 = desired.get("config_sha256", "")

    current_app = None
    if CURRENT_LINK.is_symlink():
        current_app = CURRENT_LINK.resolve().name

    payload = download_config(client, base_url, artifact_url, sha256, api_key)
    if not payload:
        return

    if not lock.acquire():
        return

    try:
        success = apply_config_update(payload, version, current_app)
        if success:
            report_state(client, base_url, api_key, state, status="config_applied")
            logger.info("Config update to %s completed", version)
        else:
            report_state(
                client, base_url, api_key, state,
                status="config_failed", last_error="Config update failed",
            )
    finally:
        lock.release()


if __name__ == "__main__":
    main()
