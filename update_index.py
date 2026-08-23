#!/usr/bin/env python3
DESCRIPTION = "Non-interactive updater for the ClangBuiltArduino board manager indexes."

import argparse
import copy
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request

ORG = "ClangBuiltArduino"
CORE_REPO = "core_arduino_avr"
TC_REPO = "tc-build"
STABLE_INDEX = "package_clangbuiltarduino_index.json"
NIGHTLY_INDEX = "package_clangbuiltarduino_nightly_index.json"

# Hosts listed for each tool in the index.
SYSROOT_HOSTS = [
    "arm-linux-gnueabihf",
    "aarch64-linux-gnu",
    "x86_64-apple-darwin12",
    "x86_64-linux-gnu",
    "i686-linux-gnu",
    "i686-mingw32",
]


def log(msg):
    print(msg, flush=True)


def gh_api(path):
    url = f"https://api.github.com/{path}"
    headers = {
        "User-Agent": "ClangBuiltArduino-index-updater",
        "Accept": "application/vnd.github+json",
    }
    # Optional token lifts the API rate limit (60/h -> 5000/h).
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise SystemExit(f"GitHub API request failed for {path}: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"GitHub API request failed for {path}: {exc}")


_release_cache = {}


def repo_releases(repo):
    if repo not in _release_cache:
        _release_cache[repo] = gh_api(f"repos/{ORG}/{repo}/releases?per_page=100") or []
    return _release_cache[repo]


def rolling_release(repo):
    """The single rolling `nightly` release of a repo, or None."""
    return gh_api(f"repos/{ORG}/{repo}/releases/tags/nightly")


def latest_release(repo, tag_regex, asset_patterns=()):
    """Newest release (incl. prereleases) whose tag matches tag_regex and
    which ships an asset for every pattern in asset_patterns."""
    rx = re.compile(tag_regex)
    axs = [re.compile(p) for p in asset_patterns]
    for rel in repo_releases(repo):
        if rel.get("draft") or not rx.match(rel["tag_name"]):
            continue
        names = [a["name"] for a in rel.get("assets", [])]
        if all(any(ax.match(n) for n in names) for ax in axs):
            return rel
    return None


def asset_name(release, pattern):
    rx = re.compile(pattern)
    for asset in release.get("assets", []):
        if rx.match(asset["name"]):
            return asset["name"]
    return None


def download_meta(url):
    """Return (size, sha256) of url, or (None, None) on failure."""
    log(f"    downloading {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            sha = hashlib.sha256()
            size = 0
            while chunk := resp.read(1 << 16):
                sha.update(chunk)
                size += len(chunk)
        log(f"    size={size} sha256={sha.hexdigest()}")
        return str(size), sha.hexdigest()
    except Exception as exc:  # noqa: BLE001 - report and let caller decide
        log(f"    FAILED: {exc}")
        return None, None


def download_url(repo, tag, name):
    return f"https://github.com/{ORG}/{repo}/releases/download/{tag}/{name}"


# Archive suffix (push-build's <arch>-<os>) -> Arduino board-manager host id.
HOST_SUFFIXES = {
    "amd64-linux": "x86_64-linux-gnu",
    "aarch64-linux": "aarch64-linux-gnu",
    "i686-windows": "i686-mingw32",
    "x86_64-darwin": "x86_64-apple-darwin12",
}


def _host_patterns(prefix):
    """Asset patterns requiring one archive per host, so releases missing a
    host never become index-visible (they stay split until complete)."""
    return [rf"^{prefix}-\d.*-{re.escape(suffix)}\.tar\.gz$"
            for suffix in HOST_SUFFIXES]


class Tool:
    """One board-manager tool dependency and how to resolve its artifacts.

    Stable archives are named <prefix>-<version>-<suffix>.tar.gz with one
    suffix per host (sysroot is a single '-any' archive for all hosts)."""

    def __init__(self, name, tag_regex, version_from_tag, archive_prefix,
                 pair_prefix=None, all_hosts=False, asset_patterns=()):
        self.name = name
        self.tag_regex = tag_regex
        self.version_from_tag = version_from_tag
        self.archive_prefix = archive_prefix
        # Archive prefix of the sibling artifact that must ship alongside this
        # one (clang and gold come from the same LLVM release).
        self.pair_prefix = pair_prefix
        self.all_hosts = all_hosts
        self.asset_patterns = tuple(asset_patterns)

    def _archives(self, version):
        if self.all_hosts:
            return {"any": f"{self.archive_prefix}-{version}-any.tar.gz"}
        return {suffix: f"{self.archive_prefix}-{version}-{suffix}.tar.gz"
                for suffix in HOST_SUFFIXES}

    def _stable_tag(self, version):
        if self.name in ("cba-llvm", "cba-llvmgold"):
            return f"llvm-{version}"
        if self.name == "cba-avr-sysroot":
            return f"sysroot-avr-{version}"
        if self.name == "cba-avr-bfd":
            return f"bfd-{version}"
        raise ValueError(self.name)

    def resolve(self, version=None):
        """Return {name, version, systems: [{host, url, archive, size,
        checksum}]} or None."""
        present = None
        if version is None:
            rel = latest_release(TC_REPO, self.tag_regex, self.asset_patterns)
            if not rel:
                log(f"  {self.name}: no matching tc-build release")
                return None
            tag = rel["tag_name"]
            version = self.version_from_tag(tag)
            present = {a["name"] for a in rel.get("assets", [])}
            if self.pair_prefix and not any(
                    n.startswith(f"{self.pair_prefix}{version}") for n in present):
                log(f"  {self.name}: release {tag} lacks its paired artifact")
                return None
        else:
            # Explicit versions are only supported for the stable channel.
            tag = self._stable_tag(version)

        systems = []
        for key, archive in self._archives(version).items():
            if present is not None and archive not in present:
                continue
            url = download_url(TC_REPO, tag, archive)
            size, checksum = download_meta(url)
            if not size:
                continue
            meta = {"size": size, "checksum": f"SHA-256:{checksum}",
                    "archiveFileName": archive, "url": url}
            if self.all_hosts:
                for host in SYSROOT_HOSTS:
                    systems.append({**meta, "host": host})
            else:
                systems.append({**meta, "host": HOST_SUFFIXES[key]})
        if not systems:
            return None
        return {"name": self.name, "version": version, "systems": systems}


STABLE_TOOLS = {
    "cba-llvm": Tool("cba-llvm", r"^llvm-\d",
                     lambda tag: tag.removeprefix("llvm-"),
                     "cba-llvm", pair_prefix="cba-llvm-gold-",
                     asset_patterns=_host_patterns("cba-llvm")),
    "cba-llvmgold": Tool("cba-llvmgold", r"^llvm-\d",
                         lambda tag: tag.removeprefix("llvm-"),
                         "cba-llvm-gold", pair_prefix="cba-llvm-",
                         asset_patterns=_host_patterns("cba-llvm-gold")),
    "cba-avr-sysroot": Tool("cba-avr-sysroot", r"^sysroot-avr-\d",
                            lambda tag: tag.removeprefix("sysroot-avr-"),
                            "cba-sysroot-avr", all_hosts=True,
                            asset_patterns=(r"^cba-sysroot-avr-.*-any\.tar\.gz$",)),
    "cba-avr-bfd": Tool("cba-avr-bfd", r"^bfd-\d",
                        lambda tag: tag.removeprefix("bfd-"),
                        "bfd-avr",
                        asset_patterns=_host_patterns("bfd-avr")),
}

# Nightly tools live in one rolling `nightly` release of tc-build with fixed
# per-host asset names, refreshed in place daily. The daily tool version is
# derived from the newest asset upload time so arduino-cli sees a new version.
NIGHTLY_HOST_ASSETS = {
    "cba-llvm": ("cba-llvm", False),
    "cba-llvmgold": ("cba-llvm-gold", False),
    "cba-avr-sysroot": ("cba-sysroot-avr", True),
}


def resolve_nightly_tools():
    """Resolve all nightly tools from the rolling release. bfd stays stable."""
    tools = {}
    rel = rolling_release(TC_REPO)
    if rel:
        assets = {a["name"]: a for a in rel.get("assets", [])}
        for name, (prefix, all_hosts) in NIGHTLY_HOST_ASSETS.items():
            candidates = ({"any": f"{prefix}-nightly-any.tar.gz"} if all_hosts
                          else {suffix: f"{prefix}-nightly-{suffix}.tar.gz"
                                for suffix in HOST_SUFFIXES})
            systems, dates = [], []
            for key, archive in candidates.items():
                asset = assets.get(archive)
                if not asset:
                    continue
                url = download_url(TC_REPO, "nightly", archive)
                size, checksum = download_meta(url)
                if not size:
                    continue
                meta = {"size": size, "checksum": f"SHA-256:{checksum}",
                        "archiveFileName": archive, "url": url}
                dates.append(asset["updated_at"])
                if all_hosts:
                    for host in SYSROOT_HOSTS:
                        systems.append({**meta, "host": host})
                else:
                    systems.append({**meta, "host": HOST_SUFFIXES[key]})
            if systems:
                newest = max(time.strptime(d, "%Y-%m-%dT%H:%M:%SZ") for d in dates)
                tools[name] = {"name": name,
                               "version": "nightly-" + time.strftime("%d%m%Y", newest),
                               "systems": systems}
    else:
        log("  no rolling nightly release in tc-build")
    log("resolving nightly cba-avr-bfd (latest stable)...")
    bfd = STABLE_TOOLS["cba-avr-bfd"].resolve()
    if bfd:
        tools["cba-avr-bfd"] = bfd
    return tools


CORE_ASSET = r"^cba-avr-.*\.tar\.bz2$"


def find_core_release(nightly):
    """API-only: newest core release for the channel, or None."""
    if nightly:
        return rolling_release(CORE_REPO)
    return latest_release(CORE_REPO, r"^\d+\.\d+\.\d+", (CORE_ASSET,))


def _platform_version_in_archive(data):
    """Read version= from the platform.txt inside a core archive."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tf:
            for member in tf.getmembers():
                if member.name.endswith("/platform.txt") or member.name == "platform.txt":
                    fp = tf.extractfile(member)
                    if not fp:
                        return None
                    text = fp.read().decode()
                    for line in text.splitlines():
                        if line.startswith("version="):
                            return line.split("=", 1)[1].strip()
    except Exception as exc:  # noqa: BLE001
        log(f"  ! cannot read platform.txt from archive: {exc}")
    return None


def core_meta(rel, nightly=False):
    """Download a core release's archive to compute size/checksum/version."""
    tag = rel["tag_name"]
    archive = asset_name(rel, CORE_ASSET)
    if not archive:
        return None
    url = download_url(CORE_REPO, tag, archive)
    log(f"    downloading {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001
        log(f"    FAILED: {exc}")
        return None
    checksum = hashlib.sha256(data).hexdigest()
    log(f"    size={len(data)} sha256={checksum}")
    if nightly:
        # The rolling archive has a fixed name; the stamped platform.txt is
        # the source of truth for the daily-bumping version.
        version = _platform_version_in_archive(data)
        if not version:
            version = "0." + time.strftime("%Y%m%d") + ".0"
            log(f"  ! falling back to date-derived version {version}")
    else:
        version = archive.removeprefix("cba-avr-").removesuffix(".tar.bz2")
    return {"tag": tag, "version": version, "url": url, "archive": archive,
            "size": str(len(data)), "checksum": f"SHA-256:{checksum}"}


def load_index(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read index {path}: {exc}")


def save_index(path, data):
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError as exc:
        raise SystemExit(f"cannot write index {path}: {exc}")
    log(f"saved {path}")


def add_tool(data, meta):
    tools = data["packages"][0]["tools"]
    if any(t["name"] == meta["name"] and t["version"] == meta["version"]
           for t in tools):
        return
    entry = {"name": meta["name"], "version": meta["version"],
             "systems": meta["systems"]}
    # Keep entries of the same tool adjacent.
    idx = next((i + 1 for i, t in enumerate(tools) if t["name"] == meta["name"]),
               len(tools))
    tools.insert(idx, entry)
    hosts = sorted({sys_["host"] for sys_ in meta["systems"]})
    log(f"  + tool {meta['name']} {meta['version']} ({len(hosts)} host(s))")


def current_tool_versions(platform):
    return {d["name"]: d["version"] for d in platform["toolsDependencies"]
            if d["packager"] == ORG}


def bump_patch(version):
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError as exc:
        raise SystemExit(f"cannot bump non-numeric platform version {version!r}") from exc
    return ".".join(parts)


def new_platform(base, version, deps, core):
    platform = copy.deepcopy(base)
    platform["version"] = version
    for dep in platform["toolsDependencies"]:
        if dep["name"] in deps:
            dep["version"] = deps[dep["name"]]
    platform["url"] = core["url"]
    platform["archiveFileName"] = core["archive"]
    platform["checksum"] = core["checksum"]
    platform["size"] = core["size"]
    return platform


def validate(path):
    """Sanity-check an index file; raises on problems."""
    data = load_index(path)
    pkg = data["packages"][0]
    tool_ids = {(t["name"], t["version"]) for t in pkg["tools"]}
    for platform in pkg["platforms"]:
        for key in ("version", "url", "archiveFileName", "checksum", "size"):
            if not platform.get(key):
                raise SystemExit(f"platform {platform.get('version')} missing {key}")
        for dep in platform["toolsDependencies"]:
            if dep["packager"] != ORG:
                continue
            dep_id = (dep["name"], dep["version"])
            if dep_id not in tool_ids:
                raise SystemExit(
                    f"platform {platform['version']}: no tool "
                    f"{dep['name']} {dep['version']}")
    log(f"validated {path}: {len(pkg['platforms'])} platform(s), "
        f"{len(pkg['tools'])} tool version(s)")


def update_stable(explicit_core=None, explicit_tools=None):
    path = STABLE_INDEX
    data = load_index(path)
    latest = data["packages"][0]["platforms"][-1]
    cur = current_tool_versions(latest)
    cur_core_tag = latest["url"].split("/download/")[1].split("/")[0]

    core = None
    if explicit_core:
        # Manual core tag: reuse archive naming from the tag's semver part.
        version = explicit_core.split("-")[0]
        archive = f"cba-avr-{version}.tar.bz2"
        url = download_url(CORE_REPO, explicit_core, archive)
        size, checksum = download_meta(url)
        if size:
            core = {"tag": explicit_core, "version": version, "url": url,
                    "archive": archive, "size": size,
                    "checksum": f"SHA-256:{checksum}"}
    else:
        log("resolving stable core...")
        rel = find_core_release(nightly=False)
        core_changed_tag = rel and rel["tag_name"] != cur_core_tag
        if rel and core_changed_tag:
            core = core_meta(rel)
        elif rel:
            log(f"  core unchanged ({rel['tag_name']}), skipping download")
    core_changed = bool(core) and core["tag"] != cur_core_tag

    tools = {}
    for name, tool in STABLE_TOOLS.items():
        want = (explicit_tools or {}).get(name)
        if not want and not core_changed and cur.get(name):
            # Only re-resolve tools when something actually moved, otherwise
            # the index would churn on every run.
            latest_rel = latest_release(TC_REPO, tool.tag_regex)
            if not latest_rel:
                continue
            if tool.version_from_tag(latest_rel["tag_name"]) == cur[name]:
                continue
        log(f"resolving stable {name}...")
        meta = tool.resolve(want)
        if meta and meta["version"] != cur.get(name):
            tools[name] = meta

    if not core_changed and not tools:
        log("stable index already up to date")
        return False

    for meta in tools.values():
        add_tool(data, meta)

    deps = {name: meta["version"] for name, meta in tools.items()}
    for name in STABLE_TOOLS:
        deps.setdefault(name, cur.get(name))

    if core_changed and core is not None:
        version = core["version"]
    else:
        version = bump_patch(latest["version"])
        core = {  # keep the existing core artifact
            "url": latest["url"], "archive": latest["archiveFileName"],
            "checksum": latest["checksum"], "size": latest["size"],
        }
    while any(p["version"] == version for p in data["packages"][0]["platforms"]):
        version = bump_patch(version)
    data["packages"][0]["platforms"].append(new_platform(latest, version, deps, core))
    log(f"  + platform {version}")
    save_index(path, data)
    validate(path)
    return True


def update_nightly():
    path = NIGHTLY_INDEX
    data = load_index(path)
    pkg = data["packages"][0]

    log("resolving nightly core...")
    rel = find_core_release(nightly=True)
    core = core_meta(rel, nightly=True) if rel else None
    if not rel:
        log("  core: no rolling nightly release")

    log("resolving nightly tools...")
    tools = resolve_nightly_tools()

    if not core and not tools:
        log("no nightly artifacts found yet; nothing to do")
        return False

    base = (pkg["platforms"][-1] if pkg["platforms"]
            else load_index(STABLE_INDEX)["packages"][0]["platforms"][-1])

    # Roll the tool set (one version per tool).
    for meta in tools.values():
        pkg["tools"] = [t for t in pkg["tools"] if t["name"] != meta["name"]]
        pkg["tools"].insert(0, {"name": meta["name"], "version": meta["version"],
                                "systems": meta["systems"]})

    # The nightly platform is installable only once every ClangBuiltArduino
    # tool dependency has a build in this index, so wait for the full set.
    cba_deps = [d["name"] for d in base["toolsDependencies"]
                if d["packager"] == ORG]
    missing = [n for n in cba_deps if n not in tools]
    if core and missing:
        log(f"nightly platform waiting for tool builds: {', '.join(missing)}")
    elif core:
        deps = {name: tools[name]["version"] for name in cba_deps}
        platform = new_platform(base, core["version"], deps, core)
        pkg["platforms"] = [platform]
        log(f"  nightly platform {core['version']}")

    save_index(path, data)
    validate(path)
    return True


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--channel", choices=["stable", "nightly"], required=True)
    parser.add_argument("--auto", action="store_true",
                        help="resolve the latest releases from the GitHub API")
    parser.add_argument("--core-tag", help="stable only: explicit core release tag")
    parser.add_argument("--llvm", help="stable only: explicit cba-llvm/gold version")
    parser.add_argument("--sysroot", help="stable only: explicit cba-avr-sysroot version")
    parser.add_argument("--bfd", help="stable only: explicit cba-avr-bfd version")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if args.validate_only:
        for path in (STABLE_INDEX, NIGHTLY_INDEX):
            if os.path.exists(path):
                validate(path)
        return

    if args.channel == "nightly":
        update_nightly()
        return

    explicit_tools = {}
    if args.llvm:
        explicit_tools["cba-llvm"] = args.llvm
        explicit_tools["cba-llvmgold"] = args.llvm
    if args.sysroot:
        explicit_tools["cba-avr-sysroot"] = args.sysroot
    if args.bfd:
        explicit_tools["cba-avr-bfd"] = args.bfd

    if not args.auto and not args.core_tag and not explicit_tools:
        parser.error("stable channel needs --auto, --core-tag or tool versions")
    update_stable(explicit_core=args.core_tag, explicit_tools=explicit_tools)


if __name__ == "__main__":
    main()
