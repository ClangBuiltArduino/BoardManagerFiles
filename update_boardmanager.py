#!/usr/bin/env python3

import json
import os
import re
import copy
import hashlib
import urllib.request

INDEX_FILE = "package_clangbuiltarduino_index.json"

CORE = {
    "url": "https://github.com/ClangBuiltArduino/core_arduino_avr/releases/download/{tag}/cba-avr-{ver}.tar.bz2",
    "archive": "cba-avr-{ver}.tar.bz2",
    "example": "1.0.0-12022025"
}

TOOLS = {
    "cba-llvm": {
        "url": "https://github.com/ClangBuiltArduino/tc-build/releases/download/llvm-{ver}/cba-llvm-{ver}-amd64-linux.tar.gz",
        "archive": "cba-llvm-{ver}-amd64-linux.tar.gz",
        "example": "20.1.0-06032025",
        "hosts": ["x86_64-linux-gnu"]
    },
    "cba-llvmgold": {
        "url": "https://github.com/ClangBuiltArduino/tc-build/releases/download/llvm-{ver}/cba-llvm-gold-{ver}-amd64-linux.tar.gz",
        "archive": "cba-llvm-gold-{ver}-amd64-linux.tar.gz",
        "example": "20.1.0-06032025",
        "hosts": ["x86_64-linux-gnu"]
    },
    "cba-avr-sysroot": {
        "url": "https://github.com/ClangBuiltArduino/tc-build/releases/download/sysroot-avr-{ver}/cba-sysroot-avr-{ver}-any.tar.gz",
        "archive": "cba-sysroot-avr-{ver}-any.tar.gz",
        "example": "12022025",
        "hosts": ["arm-linux-gnueabihf", "aarch64-linux-gnu", "x86_64-apple-darwin12",
                  "x86_64-linux-gnu", "i686-linux-gnu", "i686-mingw32"]
    },
    "cba-avr-bfd": {
        "url": "https://github.com/ClangBuiltArduino/tc-build/releases/download/bfd-{ver}/bfd-avr-{ver}-amd64-linux.tar.gz",
        "archive": "bfd-avr-{ver}-amd64-linux.tar.gz",
        "example": "2.44-14022025",
        "hosts": ["x86_64-linux-gnu"]
    }
}


def load_index():
    with open(INDEX_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_index(data):
    content = json.dumps(data, indent=2, ensure_ascii=False)

    def reformat_boards(match):
        raw = match.group(1)
        names = re.findall(r'\{\s*"name":\s*"([^"]+)"\s*\}', raw)
        lines = ',\n            '.join(f'{{"name": "{n}"}}' for n in names)
        return f'"boards": [\n            {lines}\n          ]'

    content = re.sub(r'"boards":\s*\[(.*?)\]', reformat_boards, content, flags=re.DOTALL)
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    with open(INDEX_FILE, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)

    print(f"\n✓ Saved {INDEX_FILE}")


def download(url):
    print(f"    Downloading: {url}")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=300) as resp:
            sha = hashlib.sha256()
            size = 0
            while chunk := resp.read(8192):
                sha.update(chunk)
                size += len(chunk)
            checksum = sha.hexdigest()
            print(f"    Size: {size} | SHA-256: {checksum}")
            return str(size), checksum
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        return None, None


def extract_core_tag(url):
    if "/download/" in url:
        return url.split("/download/")[1].split("/")[0]
    return ""


def extract_core_version(archive):
    if archive.startswith("cba-avr-") and archive.endswith(".tar.bz2"):
        return archive[8:-8]
    return ""


def get_latest_platform(data):
    return data["packages"][0]["platforms"][-1]


def get_tool_versions(platform):
    return {
        dep["name"]: dep["version"]
        for dep in platform["toolsDependencies"]
        if dep["packager"] == "ClangBuiltArduino"
    }


def tool_exists(tools, name, version):
    return any(t["name"] == name and t["version"] == version for t in tools)


def build_tool_entry(name, version, size, checksum, spec):
    return {
        "name": name,
        "version": version,
        "systems": [
            {
                "size": size,
                "checksum": f"SHA-256:{checksum}",
                "host": host,
                "archiveFileName": spec["archive"].format(ver=version),
                "url": spec["url"].format(ver=version)
            }
            for host in spec["hosts"]
        ]
    }


def build_platform(base, version, deps, core=None):
    platform = copy.deepcopy(base)
    platform["version"] = version

    for dep in platform["toolsDependencies"]:
        if dep["name"] in deps:
            dep["version"] = deps[dep["name"]]

    if core:
        platform["url"] = core["url"]
        platform["archiveFileName"] = core["archive"]
        platform["checksum"] = core["checksum"]
        platform["size"] = core["size"]

    return platform


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 60)
    print("ClangBuiltArduino Package Index Updater")
    print("=" * 60)

    data = load_index()
    platform = get_latest_platform(data)
    platform_ver = platform["version"]
    tool_vers = get_tool_versions(platform)

    core_tag = extract_core_tag(platform.get("url", ""))
    core_ver = extract_core_version(platform.get("archiveFileName", ""))

    print(f"\nPlatform: {platform_ver}")
    print(f"Core: {core_tag} (v{core_ver})")
    print("\nTools:")
    for name, ver in tool_vers.items():
        print(f"  {name}: {ver}")

    # Core update
    print("\n" + "-" * 60)
    print(f"Core package (current: {core_tag})")
    new_tag = input(f"  New tag [{CORE['example']}] or Enter to skip: ").strip()

    # Tool updates
    print("\n" + "-" * 60)
    print("Tool packages")

    pending = {}
    merged = {}

    for name, spec in TOOLS.items():
        current = tool_vers.get(name, "N/A")
        new_ver = input(f"\n  {name} (current: {current}) [{spec['example']}]: ").strip()

        if new_ver and new_ver != current:
            pending[name] = new_ver
            merged[name] = new_ver
        else:
            merged[name] = current

    if not new_tag and not pending:
        print("\n✓ Nothing to update.")
        return

    # Show pending changes
    print("\n" + "-" * 60)
    print("Pending updates:")
    if new_tag:
        print(f"  Core: {core_tag} → {new_tag}")
    for name, ver in pending.items():
        print(f"  {name}: {tool_vers.get(name)} → {ver}")

    # Download and hash
    print("\n" + "-" * 60)
    print("Fetching packages...")

    core_data = None
    if new_tag:
        new_core_ver = new_tag.split("-")[0]
        url = CORE["url"].format(tag=new_tag, ver=new_core_ver)
        print(f"\nCore ({new_tag}):")
        size, checksum = download(url)
        if size:
            core_data = {
                "url": url,
                "archive": CORE["archive"].format(ver=new_core_ver),
                "checksum": f"SHA-256:{checksum}",
                "size": size
            }

    tool_data = {}
    for name, ver in pending.items():
        spec = TOOLS[name]
        url = spec["url"].format(ver=ver)
        print(f"\n{name} ({ver}):")
        size, checksum = download(url)
        if size:
            tool_data[name] = {"version": ver, "size": size, "checksum": checksum}

    if not core_data and not tool_data:
        print("\n✗ No packages downloaded successfully.")
        return

    # Platform version
    print(f"\nPlatform version: {platform_ver}")
    new_platform_ver = input("  Bump to (Enter to keep): ").strip() or platform_ver

    # Confirm
    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  Platform: {new_platform_ver}")
    if core_data:
        print(f"  Core: {new_tag}")
    for name, info in tool_data.items():
        print(f"  {name}: {info['version']}")

    if input("\nApply? (y/n): ").strip().lower() != 'y':
        print("Aborted.")
        return

    # Apply tool updates
    tools = data["packages"][0]["tools"]
    for name, info in tool_data.items():
        if not tool_exists(tools, name, info["version"]):
            entry = build_tool_entry(name, info["version"], info["size"], info["checksum"], TOOLS[name])
            idx = next((i + 1 for i, t in enumerate(tools) if t["name"] == name), len(tools))
            tools.insert(idx, entry)
            print(f"✓ Added {name} {info['version']}")

    # Apply platform update
    platforms = data["packages"][0]["platforms"]

    if new_platform_ver == platform_ver:
        for dep in platform["toolsDependencies"]:
            if dep["name"] in merged:
                dep["version"] = merged[dep["name"]]
        if core_data:
            platform["url"] = core_data["url"]
            platform["archiveFileName"] = core_data["archive"]
            platform["checksum"] = core_data["checksum"]
            platform["size"] = core_data["size"]
        print(f"✓ Updated platform {platform_ver}")
    else:
        new_plat = build_platform(platform, new_platform_ver, merged, core_data)
        platforms.append(new_plat)
        print(f"✓ Added platform {new_platform_ver}")

    save_index(data)

    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)


if __name__ == "__main__":
    main()
