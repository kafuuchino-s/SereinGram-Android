#!/usr/bin/env python3
"""Prepare the fork's Firebase client configuration and check release inputs."""

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

PACKAGE = "com.eltavine.sereingram"
FIREBASE_FILE = Path("TMessagesProj/google-services.json")
REQUIRED = (
    "GOOGLE_SERVICES_JSON_BASE64",
    "TELEGRAM_APP_ID",
    "TELEGRAM_APP_HASH",
    "KEYSTORE_BASE64",
    "KEYSTORE_PASS",
    "ALIAS_NAME",
    "ALIAS_PASS",
    "SEREIN_SIGNING_SHA1",
)


def decode(name: str) -> bytes:
    try:
        return base64.b64decode("".join(os.environ.get(name, "").split()), validate=True)
    except (ValueError, binascii.Error):
        raise SystemExit(f"{name} must contain valid base64") from None


def firebase() -> None:
    try:
        config = json.loads(decode("GOOGLE_SERVICES_JSON_BASE64"))
    except (ValueError, UnicodeError):
        raise SystemExit(
            "GOOGLE_SERVICES_JSON_BASE64 must encode a Firebase client JSON file"
        ) from None
    if not isinstance(config, dict) or config.get("type") == "service_account":
        raise SystemExit("Use the Android google-services.json client file, not a service account")
    info = config.get("project_info", {})
    clients = config.get("client", [])
    if not isinstance(info, dict) or not info.get("project_id") or not info.get("project_number"):
        raise SystemExit("Firebase client JSON needs project_id and project_number")
    if not isinstance(clients, list):
        raise SystemExit("Firebase client JSON needs an Android client list")
    matches = [
        client
        for client in clients
        if isinstance(client, dict)
        and client.get("client_info", {}).get("android_client_info", {}).get("package_name")
        == PACKAGE
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"Firebase client JSON must contain exactly one Android client for {PACKAGE}"
        )
    client = matches[0]
    if not client["client_info"].get("mobilesdk_app_id") or not any(
        key.get("current_key") for key in client.get("api_key", []) if isinstance(key, dict)
    ):
        raise SystemExit("Firebase Android client needs mobilesdk_app_id and an API key")
    FIREBASE_FILE.write_text(json.dumps(config) + "\n", encoding="utf-8")
    print("Firebase Android client configuration restored.")


def check() -> None:
    from release import fingerprint, keystore_certificate

    missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit(
            "Configure these repository Secrets/Variables first: " + ", ".join(missing)
        )
    app_id = os.environ["TELEGRAM_APP_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", app_id) or int(app_id) > 2147483647:
        raise SystemExit("TELEGRAM_APP_ID must be a positive 32-bit api_id")
    if not re.fullmatch(r"[0-9a-f]{32}", os.environ["TELEGRAM_APP_HASH"]):
        raise SystemExit(
            "TELEGRAM_APP_HASH must be the 32 lowercase hex digits from my.telegram.org"
        )
    expected = fingerprint(os.environ["SEREIN_SIGNING_SHA1"])
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise SystemExit("SEREIN_SIGNING_SHA1 must be 40 hex digits, with optional colons")
    firebase()
    with tempfile.TemporaryDirectory() as temporary:
        keystore = Path(temporary) / "release.keystore"
        keystore.write_bytes(decode("KEYSTORE_BASE64"))
        result = subprocess.run(
            [
                "keytool",
                "-J-Duser.language=en",
                "-list",
                "-v",
                "-keystore",
                str(keystore),
                "-storepass:env",
                "KEYSTORE_PASS",
                "-alias",
                os.environ["ALIAS_NAME"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode:
        raise SystemExit(
            "Cannot open the signing keystore; check KEYSTORE_BASE64/PASS and ALIAS_NAME"
        )
    if keystore_certificate(result.stdout)[1] != expected:
        raise SystemExit("SEREIN_SIGNING_SHA1 does not match the fixed signing certificate")
    print("Release inputs and native signing certificate match.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "firebase"))
    args = parser.parse_args()
    try:
        {"check": check, "firebase": firebase}[args.command]()
    except (KeyError, TypeError, AttributeError):
        raise SystemExit(
            "Malformed Firebase client JSON; download the Android client file again"
        ) from None


if __name__ == "__main__":
    main()
