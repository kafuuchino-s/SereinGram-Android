"""Exercise sync/retry/versioning with real Git repos and publication with a fake API."""

import json
import re
import subprocess
from pathlib import Path

import pytest
import update


def git_at(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(path, name, content):
    file = path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)
    git_at(path, "add", ".")
    git_at(path, "commit", "-m", f"change {name}")
    return git_at(path, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git_at(upstream, "init", "-b", "main")
    git_at(upstream, "config", "user.name", "fixture")
    git_at(upstream, "config", "user.email", "fixture@example.test")
    commit(upstream, "gradle.properties", "APP_VERSION_NAME=13.0.2\n")
    commit(upstream, "TMessagesProj/build.gradle", "def verCode = 1254\n")
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(origin)], check=True, capture_output=True
    )
    fork = tmp_path / "fork"
    subprocess.run(["git", "clone", str(origin), str(fork)], check=True, capture_output=True)
    git_at(fork, "checkout", "-b", "own-fcm")
    git_at(fork, "config", "user.name", "fixture")
    git_at(fork, "config", "user.email", "fixture@example.test")
    commit(fork, "fork.txt", "fork\n")
    git_at(fork, "push", "origin", "own-fcm")
    monkeypatch.chdir(fork)
    monkeypatch.setattr(update, "UPSTREAM", str(upstream))
    monkeypatch.setattr(update, "api", lambda path: None)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    return upstream, fork, origin


def test_scheduled_no_change_does_not_commit_or_prepare_a_build(repos):
    _, fork, _ = repos
    head = git_at(fork, "rev-parse", "HEAD")
    update.prepare("schedule")
    assert git_at(fork, "rev-parse", "HEAD") == head
    assert not update.STATE.exists()


def test_initial_manual_release_upgrades_1254_and_retry_preserves_commit(repos):
    _, fork, origin = repos
    update.prepare("workflow_dispatch")
    first = git_at(fork, "rev-parse", "HEAD")
    assert update.state()["versionCode"] == "1255"
    assert git_at(origin, "rev-parse", "own-fcm") == first
    update.prepare("workflow_dispatch")
    assert git_at(fork, "rev-parse", "HEAD") == first
    assert update.state()["versionCode"] == "1255"


def test_new_upstream_same_version_is_merged_and_code_increases(repos):
    upstream, fork, _ = repos
    update.prepare("workflow_dispatch")
    new = commit(upstream, "upstream.txt", "new\n")
    update.prepare("schedule")
    assert update.state()["upstreamCommit"] == new
    assert update.state()["versionName"] == "13.0.2"
    assert update.state()["versionCode"] == "1256"
    assert (fork / "upstream.txt").read_text() == "new\n"
    assert (fork / "fork.txt").read_text() == "fork\n"


def test_new_fork_source_manual_build_also_increases_code(repos):
    _, fork, _ = repos
    update.prepare("workflow_dispatch")
    commit(fork, "fork.txt", "new fork\n")
    update.prepare("workflow_dispatch")
    assert update.state()["versionCode"] == "1256"


def test_published_source_is_not_built_again_or_retagged(repos, monkeypatch, tmp_path):
    _, fork, _ = repos
    update.prepare("workflow_dispatch")
    head = git_at(fork, "rev-parse", "HEAD")
    monkeypatch.setattr(
        update,
        "api",
        lambda path: (
            {"object": {"type": "commit", "sha": head}}
            if path.startswith("git/")
            else [{"tag_name": update.tag_for(update.state()), "draft": False, "prerelease": False}]
        ),
    )
    update.prepare("workflow_dispatch")
    assert git_at(fork, "rev-parse", "HEAD") == head
    assert (tmp_path / "outputs").read_text().splitlines()[-3] == "build=false"


def test_conflict_does_not_push_or_prepare_release(repos):
    upstream, fork, origin = repos
    commit(fork, "TMessagesProj/build.gradle", "def verCode = 1254\n// fork\n")
    before = git_at(origin, "rev-parse", "own-fcm")
    commit(upstream, "TMessagesProj/build.gradle", "def verCode = 1254\n// upstream\n")
    with pytest.raises(SystemExit, match="CONFLICT"):
        update.prepare("schedule")
    assert git_at(origin, "rev-parse", "own-fcm") == before
    assert not update.STATE.exists()


def test_android_code_limit_stops_before_push(repos):
    _, fork, origin = repos
    update.prepare("workflow_dispatch")
    before = git_at(origin, "rev-parse", "own-fcm")
    text = update.STATE.read_text().replace("versionCode=1255", f"versionCode={update.MAX_CODE}")
    commit(fork, str(update.STATE), text)
    commit(fork, "fork.txt", "new source\n")
    with pytest.raises(SystemExit, match="Android limit"):
        update.prepare("workflow_dispatch")
    assert git_at(origin, "rev-parse", "own-fcm") == before


@pytest.fixture
def publication(repos, monkeypatch, tmp_path):
    update.prepare("workflow_dispatch")
    directory = tmp_path / "apks"
    directory.mkdir()
    name = "SereinGram-v13.0.2(1255)-arm64-v8a.apk"
    (directory / name).write_bytes(b"fixture APK; signing is verified by the preceding build job")
    metadata = {
        "applicationId": update.PACKAGE,
        "elements": [
            {
                "outputFile": name,
                "versionCode": 1255,
                "versionName": "13.0.2",
                "filters": [{"filterType": "ABI", "value": "arm64-v8a"}],
            }
        ],
    }
    (directory / "output-metadata.json").write_text(json.dumps(metadata))
    stored = {"release": None, "calls": []}

    def api(path):
        if path.startswith("git/"):
            return None
        if stored["release"] is None:
            return []
        return [
            {
                "tag_name": update.tag_for(update.state()),
                "target_commitish": update.git("rev-parse", "HEAD"),
                **stored["release"],
            }
        ]

    def gh(*args):
        stored["calls"].append(args)
        if args[:2] == ("release", "create"):
            stored["release"] = {
                "draft": True,
                "prerelease": False,
                "assets": [
                    {
                        "name": re.sub(r"[^A-Za-z0-9._-]", ".", Path(args[3]).name),
                        "size": Path(args[3]).stat().st_size,
                    }
                ],
            }
        elif args[:2] == ("release", "edit"):
            stored["release"]["draft"] = False
        return ""

    monkeypatch.setattr(update, "api", api)
    monkeypatch.setattr(update, "gh", gh)
    return directory, stored


def test_only_complete_verified_arm64_is_published_and_rerun_changes_nothing(publication):
    directory, stored = publication
    update.publish(directory)
    assert [call[1] for call in stored["calls"]] == ["create", "edit"]
    assert stored["release"]["assets"][0]["name"] == "SereinGram-v13.0.2-b1255-arm64-v8a.apk"
    assert (directory / "SereinGram-v13.0.2-b1255-arm64-v8a.apk").read_bytes() == (
        directory / "SereinGram-v13.0.2(1255)-arm64-v8a.apk"
    ).read_bytes()
    assert "--draft" in stored["calls"][0]
    assert "--latest" in stored["calls"][1]
    assert stored["release"]["draft"] is False
    update.publish(directory)
    assert len(stored["calls"]) == 2


def test_wrong_version_cannot_create_release(publication):
    directory, stored = publication
    path = directory / "output-metadata.json"
    path.write_text(path.read_text().replace('"versionCode": 1255', '"versionCode": 1254'))
    with pytest.raises(SystemExit, match="metadata"):
        update.publish(directory)
    assert not stored["calls"]


def test_incomplete_draft_is_never_published(publication, monkeypatch):
    directory, stored = publication
    stored["release"] = {"draft": True, "assets": []}
    monkeypatch.setattr(update, "gh", lambda *args: stored["calls"].append(args))
    with pytest.raises(SystemExit, match="incomplete"):
        update.publish(directory)
    assert [call[1] for call in stored["calls"]] == ["upload"]
    assert stored["release"]["draft"] is True


def test_existing_assets_are_not_overwritten(publication):
    directory, stored = publication
    stored["release"] = {"draft": True, "assets": [{"name": "wrong.apk", "size": 1}]}
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        update.publish(directory)
    assert not stored["calls"]


def test_draft_with_another_source_is_not_published(publication):
    directory, stored = publication
    stored["release"] = {"draft": True, "target_commitish": "another-commit", "assets": []}
    with pytest.raises(SystemExit, match="another source"):
        update.publish(directory)
    assert not stored["calls"]


def test_existing_tag_is_never_moved(publication, monkeypatch):
    directory, stored = publication
    monkeypatch.setattr(update, "api", lambda path: {"object": {"type": "commit", "sha": "wrong"}})
    with pytest.raises(SystemExit, match="refusing to move"):
        update.publish(directory)
    assert not stored["calls"]
