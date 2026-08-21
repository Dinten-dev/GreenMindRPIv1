#!/usr/bin/env python3
"""GreenMind Update Agent – Desired-state-based remote management for RPi gateways.

Runs as a separate systemd service (greenmind-agent) under the greenmind-agent user.
Polls the cloud for desired state, applies updates atomically, executes allowlisted
commands, and reports health/status back.

Security model:
- Runs as unprivileged greenmind-agent user
- Only systemctl restart/status and reboot via sudoers
- SHA256 verification of all artifacts
- Mandatory Ed25519 signature verification (fail closed)
- Download to /tmp, verify, then move to final path
- Global flock prevents concurrent updates
- No shell execution, no arbitrary commands
"""

import base64
import fcntl
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

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
MAX_RELEASE_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_FILE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200
ALLOW_LEGACY_ONLINE_PIP = os.environ.get("GREENMIND_ALLOW_LEGACY_ONLINE_PIP", "false").lower() in {
    "1",
    "true",
    "yes",
}
ALLOW_INSECURE_CLOUD_HTTP = os.environ.get("ALLOW_INSECURE_CLOUD_HTTP", "false").lower() in {
    "1",
    "true",
    "yes",
}
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_CONFIG_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_CONFIG_BYTES = 1024 * 1024

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


def _validate_cloud_url(value: str, *, allow_insecure_loopback: bool = False) -> str:
    """Validate the standalone agent's cloud base URL without logging it."""
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError("invalid cloud URL")

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid cloud URL") from exc

    if not parsed.netloc or not hostname:
        raise ValueError("invalid cloud URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid cloud URL")
    if parsed.query or parsed.fragment:
        raise ValueError("invalid cloud URL")

    if parsed.scheme == "https":
        return value.rstrip("/")
    if parsed.scheme == "http" and allow_insecure_loopback:
        if hostname.lower() == "localhost":
            return value.rstrip("/")
        try:
            if ipaddress.ip_address(hostname).is_loopback:
                return value.rstrip("/")
        except ValueError:
            pass
    raise ValueError("invalid cloud URL")


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

    Returns ``signed`` only when every verification prerequisite succeeds.
    Missing signatures, keys, crypto support, and malformed inputs are invalid.
    """
    if not signature_b64:
        logger.error("Release is unsigned")
        return "invalid"

    if not re.fullmatch(r"[0-9a-f]{64}", sha256_hex):
        logger.error("Release SHA256 is malformed")
        return "invalid"

    if not SIGNING_KEY_PATH.exists():
        logger.error("No release signing key found at %s", SIGNING_KEY_PATH)
        return "invalid"

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        key_data = SIGNING_KEY_PATH.read_bytes()
        public_key = load_pem_public_key(key_data)

        if not isinstance(public_key, Ed25519PublicKey):
            logger.error("Signing key is not Ed25519")
            return "invalid"

        signature_bytes = base64.b64decode(signature_b64, validate=True)
        if len(signature_bytes) != 64:
            logger.error("Ed25519 signature has an invalid length")
            return "invalid"
        public_key.verify(signature_bytes, sha256_hex.encode("utf-8"))
        logger.info("Signature verification passed")
        return "signed"
    except ImportError:
        logger.error("cryptography is unavailable; refusing release update")
        return "invalid"
    except Exception as exc:
        logger.error("Signature verification FAILED: %s", exc)
        return "invalid"


def is_valid_semver(version: str) -> bool:
    """Accept only canonical SemVer 2.0.0 strings safe as path components."""
    return isinstance(version, str) and bool(_SEMVER_RE.fullmatch(version))


def _contained_path(root: Path, child: str) -> Path:
    """Resolve a direct child and reject escapes through separators/symlinks."""
    if not child or child in {".", ".."} or "/" in child or "\\" in child:
        raise ValueError("unsafe path component")
    resolved_root = root.resolve()
    candidate = (resolved_root / child).resolve(strict=False)
    if candidate.parent != resolved_root:
        raise ValueError("path escapes configured root")
    return candidate


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


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
        logger.error("Cannot determine free disk space; refusing update")
        return False

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

    # 3. Config valid (optional; if exists, must be valid JSON)
    config_link = CONFIG_DIR / "active.json"
    checks["config_valid"] = not config_link.exists() or _is_valid_json(config_link)

    # 4. Disk check
    free_mb = get_disk_free_mb()
    checks["disk"] = free_mb > 100 if free_mb >= 0 else False

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
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            logger.error("Refusing release with malformed SHA256")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None
        if (
            not artifact_url.startswith("/")
            or artifact_url.startswith("//")
            or "://" in artifact_url
            or any(ord(char) < 32 for char in artifact_url)
        ):
            logger.error("Refusing unsafe artifact URL")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None
        download_url = f"{base_url}{artifact_url}"
        logger.info("Downloading release from %s", artifact_url)

        with client.stream("GET", download_url, headers={"X-Api-Key": api_key}) as resp:
            if resp.status_code != 200:
                logger.error("Download failed: HTTP %d", resp.status_code)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return None

            content_length = resp.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    logger.error("Release has invalid Content-Length")
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return None
                if declared_size < 0 or declared_size > MAX_RELEASE_ARCHIVE_BYTES:
                    logger.error("Release exceeds maximum archive size")
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return None

            hasher = hashlib.sha256()
            downloaded = 0
            with open(tmp_file, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    downloaded += len(chunk)
                    if downloaded > MAX_RELEASE_ARCHIVE_BYTES:
                        raise ValueError("release exceeds maximum archive size")
                    f.write(chunk)
                    hasher.update(chunk)

        actual_sha256 = hasher.hexdigest()
        if actual_sha256 != expected_sha256:
            logger.error("SHA256 MISMATCH: expected %s, got %s", expected_sha256, actual_sha256)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        logger.info("Download verified: SHA256 %s", actual_sha256)
        return tmp_file

    except Exception as exc:
        logger.error("Download failed: %s", exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


def _extract_release_archive(tarball_path: Path, destination: Path) -> None:
    """Extract only bounded regular files/directories into a fresh directory."""
    archive_size = tarball_path.stat().st_size
    if archive_size <= 0 or archive_size > MAX_RELEASE_ARCHIVE_BYTES:
        raise ValueError("release archive size is invalid")

    members: list[tuple[tarfile.TarInfo, Path]] = []
    seen: set[Path] = set()
    total_size = 0
    destination_root = destination.resolve()

    with tarfile.open(tarball_path, "r:gz") as archive:
        for count, member in enumerate(archive, start=1):
            if count > MAX_ARCHIVE_MEMBERS:
                raise ValueError("release archive contains too many entries")
            if (
                not member.name
                or "\\" in member.name
                or "\x00" in member.name
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or getattr(member, "sparse", None)
                or not (member.isdir() or member.isreg())
            ):
                raise ValueError(f"unsupported archive entry: {member.name!r}")

            relative = PurePosixPath(member.name)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError(f"unsafe archive path: {member.name!r}")
            target = destination.joinpath(*relative.parts)
            resolved = target.resolve(strict=False)
            try:
                resolved.relative_to(destination_root)
            except ValueError as exc:
                raise ValueError(f"archive path escapes destination: {member.name!r}") from exc
            if resolved in seen:
                raise ValueError(f"duplicate archive path: {member.name!r}")
            seen.add(resolved)

            if member.size < 0 or member.size > MAX_ARCHIVE_FILE_BYTES:
                raise ValueError(f"archive entry is too large: {member.name!r}")
            total_size += member.size
            if total_size > MAX_ARCHIVE_UNPACKED_BYTES:
                raise ValueError("release archive expands beyond configured limit")
            members.append((member, resolved))

        if not members:
            raise ValueError("release archive is empty")
        if total_size > archive_size * MAX_ARCHIVE_COMPRESSION_RATIO:
            raise ValueError("release archive compression ratio is unsafe")

        for member, target in members:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"cannot read archive entry: {member.name!r}")
            remaining = member.size
            with target.open("xb") as output:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError(f"truncated archive entry: {member.name!r}")
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def apply_app_update(
    tarball_path: Path,
    version: str,
    state: dict,
    expected_sha256: str | None = None,
    signature_b64: str | None = None,
) -> bool:
    """Extract, install wheels, symlink switch, restart, and healthcheck.

    Returns True on success, False triggers rollback.
    """
    release_dir: Path | None = None
    staging_dir: Path | None = None
    created_release = False

    try:
        if not is_valid_semver(version):
            raise ValueError("release version is not canonical SemVer")
        if expected_sha256 is None or _sha256_file(tarball_path) != expected_sha256:
            raise ValueError("release digest verification failed before apply")
        if verify_signature(expected_sha256, signature_b64) != "signed":
            raise ValueError("release signature verification failed before apply")

        RELEASES_DIR.mkdir(parents=True, exist_ok=True)
        release_dir = _contained_path(RELEASES_DIR, version)
        if release_dir.exists():
            raise FileExistsError(f"release {version} already exists")

        staging_dir = Path(tempfile.mkdtemp(prefix=".release-staging-", dir=RELEASES_DIR))
        _extract_release_archive(tarball_path, staging_dir)
        if not (staging_dir / "src" / "main.py").is_file():
            raise ValueError("release is missing src/main.py")
        if not (staging_dir / "requirements.lock").is_file():
            raise ValueError("release is missing requirements.lock")
        os.replace(staging_dir, release_dir)
        staging_dir = None
        created_release = True

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

        if wheels_dir.is_dir() and req_file.is_file():
            # Offline install from bundled wheels
            subprocess.run(
                [
                    str(pip_path),
                    "install",
                    "--no-index",
                    "--require-hashes",
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
        elif req_file.is_file() and ALLOW_LEGACY_ONLINE_PIP:
            # Explicit break-glass compatibility for old releases only.
            subprocess.run(
                [str(pip_path), "install", "--require-hashes", "-r", str(req_file)],
                check=True,
                timeout=300,
                capture_output=True,
            )
            logger.warning(
                "INSECURE LEGACY MODE: installed dependencies from the public package index"
            )
        else:
            raise ValueError(
                "release must bundle wheels; online pip is disabled "
                "(set GREENMIND_ALLOW_LEGACY_ONLINE_PIP=true only for emergency migration)"
            )

        # 3. Save current symlink target for rollback
        previous = None
        if CURRENT_LINK.is_symlink():
            previous = str(CURRENT_LINK.resolve())
            state["previous_release"] = previous

        # 4. Atomic symlink switch
        tmp_link = CURRENT_LINK.parent / f".current_tmp_{os.getpid()}"
        tmp_link.symlink_to(release_dir)
        os.replace(tmp_link, CURRENT_LINK)
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
            _rollback_to(Path(previous), state)
        current_target = CURRENT_LINK.resolve() if CURRENT_LINK.is_symlink() else None
        if current_target != release_dir:
            shutil.rmtree(release_dir, ignore_errors=True)
        return False

    except Exception as exc:
        logger.error("App update failed: %s", exc, exc_info=True)
        previous = state.get("previous_release")
        if previous:
            _rollback_to(Path(previous), state)
        if created_release and release_dir is not None:
            current_target = CURRENT_LINK.resolve() if CURRENT_LINK.is_symlink() else None
            if current_target != release_dir:
                shutil.rmtree(release_dir, ignore_errors=True)
        return False
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        # Clean up temp download
        tmp_parent = tarball_path.parent
        if tmp_parent.name.startswith("greenmind_release_"):
            shutil.rmtree(tmp_parent, ignore_errors=True)


def _rollback_to(previous_dir: Path, state: dict) -> bool:
    """Revert the current symlink to a previous release and restart."""
    try:
        releases_root = RELEASES_DIR.resolve()
        resolved_previous = previous_dir.resolve(strict=True)
        if (
            resolved_previous.parent != releases_root
            or not is_valid_semver(resolved_previous.name)
            or not resolved_previous.is_dir()
        ):
            logger.error("Rollback target is outside the validated release directory")
            return False
        if not previous_dir.exists():
            logger.error("Rollback target does not exist: %s", previous_dir)
            return False

        tmp_link = CURRENT_LINK.parent / f".current_rollback_{os.getpid()}"
        tmp_link.symlink_to(resolved_previous)
        os.replace(tmp_link, CURRENT_LINK)

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
        if (
            not isinstance(artifact_url, str)
            or not artifact_url.startswith("/")
            or artifact_url.startswith("//")
            or "://" in artifact_url
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        ):
            logger.error("Refusing malformed config metadata")
            return None
        url = f"{base_url}{artifact_url}"
        resp = client.get(url, headers={"X-Api-Key": api_key})
        if resp.status_code != 200:
            logger.error("Config download failed: HTTP %d", resp.status_code)
            return None

        if len(resp.content) > MAX_CONFIG_BYTES:
            logger.error("Config response exceeds maximum size")
            return None

        data = resp.json()
        payload = data.get("config_payload", data)

        # Verify SHA256
        # The backend signs the canonical JSON representation. Whitespace,
        # insertion order, and non-finite numbers must not create a second
        # representation of the same config at the verification boundary.
        serialised = json.dumps(
            payload,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        actual = hashlib.sha256(serialised.encode("utf-8")).hexdigest()
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

        # 1. Validate the bounded structure and path component.
        if not isinstance(payload, dict) or not _CONFIG_VERSION_RE.fullmatch(version):
            logger.error("Config payload is not a dict")
            return False
        serialised = json.dumps(payload, indent=2)
        if len(serialised.encode("utf-8")) > MAX_CONFIG_BYTES:
            logger.error("Config payload exceeds maximum size")
            return False

        # 2. Backup current config
        active_link = CONFIG_DIR / "active.json"
        if active_link.exists():
            backup_path = BACKUPS_DIR / "last_good_config.json"
            try:
                resolved = active_link.resolve(strict=True)
                resolved.relative_to(CONFIG_VERSIONS_DIR.resolve())
                shutil.copy2(str(resolved), str(backup_path))
                logger.info("Backed up current config to %s", backup_path)
            except Exception as exc:
                logger.error("Config backup validation failed: %s", exc)
                return False

        # 3. Write new config version
        config_file = _contained_path(CONFIG_VERSIONS_DIR, f"{version}.json")
        temp_config = CONFIG_VERSIONS_DIR / f".config-{os.getpid()}.tmp"
        with temp_config.open("x", encoding="utf-8") as output:
            output.write(serialised)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_config, config_file)

        # 4. Atomic symlink switch
        tmp_link = CONFIG_DIR / f".active_tmp_{os.getpid()}"
        tmp_link.symlink_to(config_file)
        os.replace(tmp_link, active_link)
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
            os.replace(tmp_link, active_link)
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
        [
            directory
            for directory in RELEASES_DIR.iterdir()
            if not directory.is_symlink() and directory.is_dir() and is_valid_semver(directory.name)
        ],
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

    # Determine cloud base URL — strip /api/v1 if already present in secrets.
    # Validation happens before any request and errors never echo the URL, which
    # may contain accidentally embedded credentials.
    try:
        base_url = _validate_cloud_url(
            server_url or "https://green-mind.ch",
            allow_insecure_loopback=ALLOW_INSECURE_CLOUD_HTTP,
        )
    except ValueError:
        logger.error("Configured cloud URL is invalid; HTTPS is required")
        sys.exit(1)
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
                                    client,
                                    base_url,
                                    api_key,
                                    gateway_id,
                                    str(cmd["id"]),
                                    "rejected",
                                    "Reboot not allowed",
                                )
                                continue
                            if not desired.get(
                                "allow_reboot_outside_window"
                            ) and not is_in_update_window(
                                desired.get("update_window_start"),
                                desired.get("update_window_end"),
                                desired.get("update_timezone", "UTC"),
                            ):
                                report_command_result(
                                    client,
                                    base_url,
                                    api_key,
                                    gateway_id,
                                    str(cmd["id"]),
                                    "rejected",
                                    "Reboot outside update window",
                                )
                                continue

                        result, message = execute_command(cmd, state)
                        report_command_result(
                            client,
                            base_url,
                            api_key,
                            gateway_id,
                            str(cmd["id"]),
                            result,
                            message,
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

    if (
        not is_valid_semver(version)
        or not isinstance(artifact_url, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or not isinstance(signature, str)
        or not signature
        or isinstance(file_size, bool)
        or not isinstance(file_size, int)
        or file_size < 1
        or file_size > MAX_RELEASE_ARCHIVE_BYTES
    ):
        logger.error("REJECTING malformed or unsigned release metadata")
        state["signature_status"] = "invalid"
        state["update_download_status"] = "metadata_invalid"
        report_state(
            client,
            base_url,
            api_key,
            state,
            status="metadata_invalid",
            last_error="Release metadata/signature is missing or invalid",
        )
        return

    # Phase 1: Download (allowed outside window if configured)
    can_download = desired.get("allow_download_outside_window", True) or is_in_update_window(
        desired.get("update_window_start"),
        desired.get("update_window_end"),
        desired.get("update_timezone", "UTC"),
    )

    cached_tarball = state.get("cached_tarball")
    cached_version = state.get("cached_version")
    cached_path: Path | None = None
    if cached_version == version and isinstance(cached_tarball, str):
        candidate = Path(cached_tarball)
        try:
            resolved = candidate.resolve(strict=True)
            temp_root = Path(tempfile.gettempdir()).resolve()
            if (
                candidate.is_symlink()
                or not resolved.is_file()
                or resolved.parent.parent != temp_root
                or not resolved.parent.name.startswith("greenmind_release_")
                or resolved.stat().st_size > MAX_RELEASE_ARCHIVE_BYTES
            ):
                raise ValueError("unsafe cached release path")
            if _sha256_file(resolved) != sha256:
                raise ValueError("cached release digest mismatch")
            if verify_signature(sha256, signature) != "signed":
                raise ValueError("cached release signature invalid")
            cached_path = resolved
        except (OSError, ValueError):
            logger.error("Discarding invalid cached release metadata")
            state.pop("cached_tarball", None)
            state.pop("cached_version", None)

    if cached_path is not None:
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
        if sig_status != "signed":
            logger.error("REJECTING update %s — invalid signature", version)
            shutil.rmtree(tarball.parent, ignore_errors=True)
            state["update_download_status"] = "signature_invalid"
            report_state(
                client,
                base_url,
                api_key,
                state,
                status="signature_invalid",
                last_error="Ed25519 signature invalid",
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
        success = apply_app_update(
            tarball_path,
            version,
            state,
            expected_sha256=sha256,
            signature_b64=signature,
        )

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
                client,
                base_url,
                api_key,
                state,
                status="apply_failed",
                last_error="Update failed, rolled back",
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
                client,
                base_url,
                api_key,
                state,
                status="config_failed",
                last_error="Config update failed",
            )
    finally:
        lock.release()


if __name__ == "__main__":
    main()
