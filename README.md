# ClangBuiltArduino Board Manager Files

Arduino board-manager package indexes for the ClangBuiltArduino AVR platform.

## Channels

| Channel | Index URL | Contents |
|---------|-----------|----------|
| **Stable** | `https://raw.githubusercontent.com/ClangBuiltArduino/BoardManagerFiles/main/package_clangbuiltarduino_index.json` | Pinned upstream releases |
| **Nightly** | `https://raw.githubusercontent.com/ClangBuiltArduino/BoardManagerFiles/main/package_clangbuiltarduino_nightly_index.json` | LLVM + avr-libc + core built from git HEAD daily |

Add the URL(s) under *Additional boards manager URLs* in the Arduino IDE, or:

```bash
arduino-cli core update-index --additional-urls "<url>"
arduino-cli core install ClangBuiltArduino:avr --additional-urls "<url>"
```

## How the index stays fresh

`update_index.py` rewrites an index from live GitHub releases. It is driven by
`.github/workflows/update-index.yml`, which runs:

- on `repository_dispatch` sent by `tc-build` and `core_arduino_avr` right after
  they publish a release,
- daily at 06:00 UTC as a safety net,
- on manual dispatch with a channel selector.

No board-manager editing should be needed by hand.

## Manual usage

```bash
# Refresh a channel from the latest releases (downloads archives to hash them).
GH_TOKEN=$(gh auth token) python3 update_index.py --channel stable --auto
GH_TOKEN=$(gh auth token) python3 update_index.py --channel nightly --auto

# Pin specific versions instead (stable channel only).
python3 update_index.py --channel stable \
    --core-tag 1.1.0-01012026 --llvm 22.1.8-01012026 \
    --sysroot 01012026 --bfd 2.47-01012026

# Validate index integrity (platforms reference existing tool versions, etc.).
python3 update_index.py --channel stable --validate-only
```

`GH_TOKEN`/`GITHUB_TOKEN` is optional but recommended; it lifts the GitHub API
rate limit from 60 to 5000 requests/hour (CI provides it automatically).

## Hosts

Tool archives follow `<prefix>-<version>-<arch>-<os>.tar.gz`; the updater maps
the suffix to Arduino host ids and emits one `systems[]` entry per host:

| Archive suffix | Board-manager host |
|---|---|
| `amd64-linux` | `x86_64-linux-gnu` |
| `aarch64-linux` | `aarch64-linux-gnu` |
| `i686-windows` | `i686-mingw32` |
| `x86_64-darwin` | `x86_64-apple-darwin12` |

The sysroot is a single `-any` archive listed for every host. Hosts whose
archive is missing from a release simply get no `systems[]` entry.

## Conventions the updater relies on

- Core release tags: semver (`1.1.0`) for stable, `nightly-<date>` for nightly;
  each release carries a single `cba-avr-<version>.tar.bz2` asset.
- `tc-build` release tags: `llvm-<ver>-<date>` (clang + gold archives),
  `sysroot-avr-<date>`, `bfd-<ver>-<date>` for stable, and the same with a
  `nightly-` prefix for nightly builds.
- Nightly pins the latest **stable** BFD release (binutils is not rebuilt nightly).
