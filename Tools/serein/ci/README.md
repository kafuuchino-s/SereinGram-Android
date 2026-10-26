# The own-fcm build and release

This fork maintains `own-fcm` in `kafuuchino-s/SereinGram-Android`. The upstream
application ID remains `com.eltavine.sereingram`. Its CI, nightly and stable
workflows retain their `eltavine/SereinGram-Android` repository guards.

## Update and version allocation

`.github/workflows/serein-update.yml` runs daily at 03:17 UTC and accepts a
manual dispatch on `own-fcm`. `update.py prepare` fetches and normally merges
upstream `main`, preserving both histories. A merge conflict stops before any
push or build. Scheduled runs with no new upstream commit skip the build.

The release keeps `APP_VERSION_NAME` from `gradle.properties`. Its versionCode
is the larger of the previous fork code plus one and upstream's `def verCode`
in `TMessagesProj/build.gradle`, within Android's limit of 2,100,000,000.
`.github/serein-release.properties` records that code and the source commits;
the application convention plugin applies the code to the APKs. An unchanged
manual retry keeps the same prepared source and version.

## Gradle build

`.github/workflows/serein-build.yml` first validates the configuration in a
separate job. Only after it succeeds does the APK job restore the Firebase
Android client and call the upstream `.github/actions/build-apks` action.
That action checks out the native submodules and runs
`./gradlew :TMessagesProj:assembleRelease --console=plain`. Gradle compiles the
native code and packages both `armeabi-v7a` and `arm64-v8a`; this workflow sets
no `NATIVE_TARGET` restriction. The obsolete `./run` native build and separate
native library cache are no longer part of the fork workflow. Gradle's own
cache setup stays in the upstream action.

The fork installs SDK 36, build tools 36.0.0, NDK 27.2.12479018 and CMake 3.22.1.
JDK 21 and Gradle setup follow the upstream action. The action uses the source
commit's timestamp for the build timestamp.

The existing repository configuration supplies these names; values belong in
GitHub's configuration, never in source or logs:

| Location | Name | Purpose |
| --- | --- | --- |
| Secret | `GOOGLE_SERVICES_JSON_BASE64` | Firebase Android client for this application ID |
| Secrets | `TELEGRAM_APP_ID`, `TELEGRAM_APP_HASH` | Telegram API credentials passed to Gradle |
| Secrets | `KEYSTORE_BASE64`, `KEYSTORE_PASS`, `ALIAS_NAME`, `ALIAS_PASS` | Existing fixed release key |
| Variable | `SEREIN_SIGNING_SHA1` | Certificate required by the native signature check |

`prepare_build.py check` validates these inputs and matches the native
certificate setting to the keystore before compilation. The APK job restores
Firebase from the same configured input and passes the existing key to the
upstream action. `release.py verify` checks every resulting APK against that
keystore and native certificate setting before uploading the two ABI APKs and
`output-metadata.json` as a 14-day Actions artifact.

## Publication and verification

The updater consumes only that successful build's artifact, checks its package
and version metadata, and publishes the ARM64 APK as a stable GitHub Release.
It creates and checks a draft before making it public, uses tags named
`sereingram-v<versionName>-b<versionCode>`, and refuses to move an existing tag
or overwrite existing assets.

Run the local script checks without production credentials:

```shell
uv run --with pytest==8.4.2 --with httpx==0.28.1 pytest -q Tools/serein/ci
uvx --from actionlint-py==1.7.12.25 actionlint
uvx ruff@0.16.10 check Tools/serein
uvx ruff@0.16.10 format --check Tools/serein
```

The Python tests use temporary Git repositories and fake configuration. A real
Actions build is still required to verify Android compilation, both APKs and
the existing release signature. These local checks do not publish a release.
