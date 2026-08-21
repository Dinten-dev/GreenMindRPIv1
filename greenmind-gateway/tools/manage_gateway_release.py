#!/usr/bin/env python3
"""Upload, activate, and roll out a signed gateway release safely.

Authentication is read only from ``GREENMIND_TOKEN``. The command never logs
the token or signature and requires an explicit confirmation flag for rollouts.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


def _base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise argparse.ArgumentTypeError("base URL must be credential-free HTTPS")
    return value


def _version(value: str) -> str:
    if not _SEMVER.fullmatch(value):
        raise argparse.ArgumentTypeError("version must be canonical SemVer")
    return value


def _release_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("release ID must be a UUID") from exc


def _artifact(value: str) -> Path:
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if path.is_symlink() or not resolved.is_file() or not resolved.name.endswith(".tar.gz"):
        raise argparse.ArgumentTypeError("artifact must be a regular .tar.gz file")
    if resolved.stat().st_size < 1 or resolved.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise argparse.ArgumentTypeError("artifact size is outside the allowed range")
    return resolved


def _signature(path: Path) -> str:
    data = path.read_bytes().strip()
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise SystemExit("Signature file must contain base64-encoded Ed25519 bytes") from exc
    if len(decoded) != 64:
        raise SystemExit("Signature file does not contain a 64-byte Ed25519 signature")
    return data.decode("ascii")


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    response = client.request(method, url, **kwargs)
    if response.is_error:
        raise SystemExit(f"Admin API request failed with HTTP {response.status_code}")
    return response


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        type=_base_url,
        default="https://green-mind.ch/api/v1/admin",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    upload = subcommands.add_parser("upload", help="upload a mandatory signed artifact")
    upload.add_argument("artifact", type=_artifact)
    upload.add_argument("--version", required=True, type=_version)
    upload.add_argument("--signature-file", required=True, type=Path)
    upload.add_argument("--channel", choices=("stable", "beta", "development"), default="stable")
    upload.add_argument("--mandatory", action="store_true")
    upload.add_argument("--activate", action="store_true")

    activate = subcommands.add_parser("activate", help="activate an uploaded release")
    activate.add_argument("release_id", type=_release_id)

    rollout = subcommands.add_parser("rollout", help="start a rollout")
    rollout.add_argument("--version", required=True, type=_version)
    rollout.add_argument("--target-ring", choices=("canary", "pilot", "all"), default="canary")
    rollout.add_argument("--yes", action="store_true", help="confirm the external rollout action")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    token = os.environ.get("GREENMIND_TOKEN")
    if not token:
        raise SystemExit("Set GREENMIND_TOKEN in the process environment")

    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(60.0, connect=10.0)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=False) as client:
        if args.command == "upload":
            signature = _signature(args.signature_file)
            with args.artifact.open("rb") as artifact:
                response = _request(
                    client,
                    "POST",
                    f"{args.base_url}/gateway-app-releases",
                    data={
                        "version": args.version,
                        "channel": args.channel,
                        "mandatory": str(args.mandatory).lower(),
                        "signature": signature,
                    },
                    files={"file": (args.artifact.name, artifact, "application/gzip")},
                )
            release_id = _release_id(str(response.json().get("id", "")))
            print(f"Uploaded release {args.version} as {release_id}")
            if args.activate:
                _request(
                    client,
                    "PATCH",
                    f"{args.base_url}/gateway-app-releases/{release_id}/status",
                    params={"is_active": "true"},
                )
                print(f"Activated release {release_id}")
        elif args.command == "activate":
            _request(
                client,
                "PATCH",
                f"{args.base_url}/gateway-app-releases/{args.release_id}/status",
                params={"is_active": "true"},
            )
            print(f"Activated release {args.release_id}")
        elif args.command == "rollout":
            if not args.yes:
                raise SystemExit("Rollout requires --yes confirmation")
            _request(
                client,
                "POST",
                f"{args.base_url}/gateway-rollout",
                json={"release_version": args.version, "target_ring": args.target_ring},
            )
            print(f"Started {args.target_ring} rollout for {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
