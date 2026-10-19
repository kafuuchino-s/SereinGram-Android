"""Check the fork's configuration contract without real credentials or signing keys."""

import base64
import json
import subprocess

import prepare_build
import pytest


def client():
    return {
        "project_info": {"project_id": "fixture-project", "project_number": "123456"},
        "client": [
            {
                "client_info": {
                    "mobilesdk_app_id": "1:123456:android:fixture",
                    "android_client_info": {"package_name": prepare_build.PACKAGE},
                },
                "api_key": [{"current_key": "fixture-client-api-key"}],
            }
        ],
    }


def encode(config):
    return base64.b64encode(json.dumps(config).encode()).decode()


@pytest.fixture
def inputs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "TMessagesProj").mkdir()
    values = {
        "GOOGLE_SERVICES_JSON_BASE64": encode(client()),
        "TELEGRAM_APP_ID": "123456",
        "TELEGRAM_APP_HASH": "abcdef0123456789abcdef0123456789",
        "KEYSTORE_BASE64": base64.b64encode(b"fixture, not a signing key").decode(),
        "KEYSTORE_PASS": "fixture-store-password",
        "ALIAS_NAME": "fixture-alias",
        "ALIAS_PASS": "fixture-key-password",
        "SEREIN_SIGNING_SHA1": ":".join(["AB"] * 20),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    listing = "Owner: CN=fixture\nSHA256: " + "CD" * 32 + "\nSHA1: " + "AB" * 20
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, listing, ""),
    )
    return values


def test_configuration_restores_client_and_accepts_matching_certificate(inputs, capsys):
    prepare_build.check()
    assert json.loads(prepare_build.FIREBASE_FILE.read_text()) == client()
    output = capsys.readouterr().out
    assert "native signing certificate match" in output
    for value in inputs.values():
        assert value not in output


def test_missing_secrets_stop_before_writing_client(inputs, monkeypatch):
    monkeypatch.delenv("ALIAS_PASS")
    with pytest.raises(SystemExit, match="ALIAS_PASS"):
        prepare_build.check()
    assert not prepare_build.FIREBASE_FILE.exists()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("TELEGRAM_APP_ID", "2147483648", "positive 32-bit"),
        ("TELEGRAM_APP_HASH", "bad", "32 lowercase hex"),
        ("SEREIN_SIGNING_SHA1", "bad", "40 hex"),
        ("SEREIN_SIGNING_SHA1", "EF" * 20, "does not match"),
    ],
)
def test_invalid_api_or_native_signature_stops(inputs, monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=message):
        prepare_build.check()


def test_keystore_failure_hides_tool_output(inputs, monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "sensitive", "sensitive"),
    )
    with pytest.raises(SystemExit, match="Cannot open the signing keystore"):
        prepare_build.check()
    captured = capsys.readouterr()
    assert "sensitive" not in captured.out + captured.err


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"type": "service_account", "private_key": "fixture"}, "not a service account"),
        ({"project_info": {}, "client": []}, "project_id and project_number"),
        (
            {
                **client(),
                "client": [
                    {"client_info": {"android_client_info": {"package_name": "org.example.other"}}}
                ],
            },
            "exactly one Android client",
        ),
        ({**client(), "client": [client()["client"][0]] * 2}, "exactly one Android client"),
        (
            {**client(), "client": [{**client()["client"][0], "api_key": []}]},
            "needs mobilesdk_app_id and an API key",
        ),
    ],
)
def test_wrong_firebase_configuration_is_not_written(inputs, monkeypatch, config, message):
    monkeypatch.setenv("GOOGLE_SERVICES_JSON_BASE64", encode(config))
    with pytest.raises(SystemExit, match=message):
        prepare_build.firebase()
    assert not prepare_build.FIREBASE_FILE.exists()


@pytest.mark.parametrize("encoded", ["%%%", base64.b64encode(b"not JSON").decode()])
def test_invalid_firebase_encoding_is_not_written(inputs, monkeypatch, encoded):
    monkeypatch.setenv("GOOGLE_SERVICES_JSON_BASE64", encoded)
    with pytest.raises(SystemExit):
        prepare_build.firebase()
    assert not prepare_build.FIREBASE_FILE.exists()
