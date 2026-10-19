"""Merge upstream and prepare/publish immutable, monotonically numbered fork releases."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPOSITORY = "kafuuchino-s/SereinGram-Android"
UPSTREAM = "https://github.com/eltavine/SereinGram-Android.git"
UPSTREAM_REF = "refs/remotes/upstream/main"
STATE = Path(".github/serein-release.properties")
MAX_CODE = 2_100_000_000
PACKAGE = "com.eltavine.sereingram"


def run(*args: str) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"{args[0]} failed: {result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def git(*args: str) -> str:
    return run("git", *args)


def gh(*args: str) -> str:
    return run("gh", *args)


def api(path: str):
    result = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/{path}"], check=False, capture_output=True, text=True
    )
    if result.returncode:
        if "(HTTP 404)" in result.stderr:
            return None
        raise SystemExit(f"GitHub API failed for {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def release_for(tag: str):
    # The tag endpoint only promises published releases; this also finds drafts.
    page = 1
    while True:
        releases = api(f"releases?per_page=100&page={page}") or []
        for release in releases:
            if release["tag_name"] == tag:
                return release
        if len(releases) < 100:
            return None
        page += 1


def state() -> dict[str, str]:
    if not STATE.exists():
        return {}
    return dict(line.split("=", 1) for line in STATE.read_text().splitlines() if line)


def tag_for(info: dict[str, str]) -> str:
    return f"sereingram-v{info['versionName']}-b{info['versionCode']}"


def output(**values) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        handle.writelines(f"{name}={value}\n" for name, value in values.items())


def check_tag(tag: str, commit: str) -> None:
    reference = api(f"git/ref/tags/{tag}")
    if reference and (
        reference["object"]["type"] != "commit" or reference["object"]["sha"] != commit
    ):
        raise SystemExit(f"Existing tag {tag} does not point to {commit}; refusing to move it")


def prepare(event: str) -> None:
    git("fetch", "--no-tags", UPSTREAM, f"main:{UPSTREAM_REF}")
    upstream = git("rev-parse", UPSTREAM_REF)
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", upstream, "HEAD"], check=False)
    if ancestor.returncode not in (0, 1):
        raise SystemExit("Cannot determine the upstream merge base")
    if event == "schedule" and ancestor.returncode == 0:
        print("Upstream main is already merged; no build.")
        output(build="false")
        return
    # A conflict fails this disposable checkout before a push, build or publication.
    git("merge", "--no-edit", UPSTREAM_REF)
    previous = state()
    same_source = False
    if previous and previous["upstreamCommit"] == upstream:
        difference = subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                previous["baseCommit"],
                "HEAD",
                "--",
                ".",
                f":(exclude){STATE}",
            ],
            check=False,
        ).returncode
        if difference not in (0, 1):
            raise SystemExit("Cannot compare this source with the previous prepared release")
        same_source = difference == 0
    if not same_source:
        properties = Path("gradle.properties").read_text()
        version = re.search(r"^APP_VERSION_NAME=(\S+)$", properties, re.MULTILINE)
        upstream_code = re.search(
            r"^def verCode = (\d+)\s*$",
            Path("TMessagesProj/build.gradle").read_text(),
            re.MULTILINE,
        )
        if (
            not version
            or not upstream_code
            or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]*", version[1])
        ):
            raise SystemExit("Upstream version format changed; review it before releasing")
        code = max(int(previous.get("versionCode", "1254")) + 1, int(upstream_code[1]))
        if not 1254 < code <= MAX_CODE:
            raise SystemExit("Release versionCode exceeds the Android limit")
        previous = {
            "versionCode": str(code),
            "versionName": version[1],
            "upstreamCommit": upstream,
            "baseCommit": git("rev-parse", "HEAD"),
        }
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text("".join(f"{key}={value}\n" for key, value in previous.items()))
        git("add", str(STATE))
        git("commit", "-m", f"ci: prepare SereinGram {version[1]} ({code})")
    commit = git("rev-parse", "HEAD")
    tag = tag_for(previous)
    check_tag(tag, commit)
    release = release_for(tag)
    if release and not release["draft"]:
        if release["prerelease"]:
            raise SystemExit("Existing release is not stable; refusing to alter it")
        print(f"{tag} is already published; no build and no asset changes.")
        output(build="false", tag=tag, commit=commit)
        return
    # A rejected push aborts before the reusable build starts.
    git("push", "origin", "HEAD:refs/heads/own-fcm")
    print(f"Prepared {tag} at {commit}, upstream {upstream}.")
    output(build="true", tag=tag, commit=commit)


def publish(directory: Path) -> None:
    from release import apks

    info = state()
    commit = git("rev-parse", "HEAD")
    tag = tag_for(info)
    check_tag(tag, commit)
    built = apks(directory)
    metadata = json.loads((directory / "output-metadata.json").read_text())
    if metadata.get("applicationId") != PACKAGE or any(
        apk.version_name != info["versionName"] or apk.version_code != int(info["versionCode"])
        for apk in built
    ):
        raise SystemExit("Built APK metadata does not match the prepared release")
    arm64 = [apk for apk in built if apk.abi == "arm64-v8a"]
    if len(arm64) != 1:
        raise SystemExit("Expected exactly one ARM64 APK")
    # GitHub normalizes parentheses and other punctuation in uploaded filenames.
    # Stage an unchanged copy under a safe name; retain upstream Artifact filenames.
    name = re.sub(
        r"[^A-Za-z0-9._-]",
        "-",
        f"SereinGram-v{info['versionName']}-b{info['versionCode']}-arm64-v8a.apk",
    )
    asset = directory / name
    if arm64[0].path != asset:
        shutil.copyfile(arm64[0].path, asset)
    expected_assets = [(asset.name, asset.stat().st_size)]
    release = release_for(tag)
    if release and not release["draft"]:
        if (
            release["prerelease"]
            or [(a["name"], a["size"]) for a in release["assets"]] != expected_assets
        ):
            raise SystemExit("Published release differs; refusing to overwrite any asset")
        print(f"{tag} is already published; left unchanged.")
        return
    if release:
        if release["target_commitish"] != commit:
            raise SystemExit("Draft points to another source; refusing to alter it")
        assets = [(a["name"], a["size"]) for a in release["assets"]]
        if assets and assets != expected_assets:
            raise SystemExit("Draft has unexpected assets; refusing to overwrite them")
        if not assets:
            gh("release", "upload", tag, str(asset), "--repo", REPOSITORY)
    else:
        notes = (
            f"SereinGram {info['versionName']} ({info['versionCode']})\n\n"
            f"Source: https://github.com/{REPOSITORY}/commit/{commit}\n"
            f"Upstream: https://github.com/eltavine/SereinGram-Android/commit/{info['upstreamCommit']}\n"
            f"Build: https://github.com/{REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}\n\n"
            "ARM64 APK verified with the existing fixed signing key.\n"
            "The upstream versionName is preserved; versionCode increases for each prepared source.\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            body = Path(temporary) / "notes.md"
            body.write_text(notes)
            gh(
                "release",
                "create",
                tag,
                str(asset),
                "--repo",
                REPOSITORY,
                "--target",
                commit,
                "--title",
                f"SereinGram {info['versionName']} ({info['versionCode']})",
                "--notes-file",
                str(body),
                "--draft",
            )
    ready = release_for(tag)
    if (
        not ready
        or not ready["draft"]
        or [(a["name"], a["size"]) for a in ready["assets"]] != expected_assets
    ):
        raise SystemExit("Draft upload is incomplete; release remains unpublished")
    check_tag(tag, commit)
    gh(
        "release",
        "edit",
        tag,
        "--repo",
        REPOSITORY,
        "--draft=false",
        "--prerelease=false",
        "--latest",
    )
    print(f"Published https://github.com/{REPOSITORY}/releases/tag/{tag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument(
        "--event", choices=("schedule", "workflow_dispatch"), required=True
    )
    commands.add_parser("publish").add_argument("--apk-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.event)
    else:
        publish(args.apk_dir)


if __name__ == "__main__":
    main()
