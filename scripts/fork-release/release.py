#!/usr/bin/env python3
"""Build a stable upstream tag plus explicit patches; publish only verified artifacts."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile


UPSTREAM = "Mygod/VPNHotspot"
ABIS = ("arm64-v8a", "armeabi-v7a", "x86", "x86_64")
OVERLAY = (".github/workflows", "scripts/fork-release", "patches",
           "scripts/verify_legacy_firewall_cleanup.py", "FORK.md")


def run(*args, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def api(path, missing=False):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{path}",
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json", "User-Agent": "VPNHotspot-patch-release"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            return None
        raise


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def tagged_commit(repo, tag):
    ref = api(f"{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='')}", missing=True)
    if ref is None:
        return None
    obj = ref["object"]
    while obj["type"] == "tag":
        obj = api(f"{repo}/git/tags/{obj['sha']}")["object"]
    if obj["type"] != "commit":
        raise RuntimeError("The release tag does not point to a commit")
    return api(f"{repo}/git/commits/{obj['sha']}")


def same_source(commit, info):
    return (commit["tree"]["sha"] == info["source_tree_sha"]
            and [parent["sha"] for parent in commit["parents"]] == [info["upstream_sha"]])


def prepare(args):
    maintenance = args.maintenance.resolve()
    source = args.source.resolve()
    output = args.output.resolve()
    repo = os.environ["GITHUB_REPOSITORY"]
    if repo.lower() == UPSTREAM.lower():
        raise RuntimeError("Run this workflow in your fork, not in the upstream repository")
    maintenance_sha = os.environ["GITHUB_SHA"]
    if run("git", "rev-parse", "HEAD", cwd=maintenance) != maintenance_sha:
        raise RuntimeError("Maintenance checkout does not match the triggering workflow commit")
    entries = subprocess.check_output(
        ["git", "ls-tree", "-rz", maintenance_sha, "--", *OVERLAY], cwd=maintenance)
    overlay = {}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, filename = entry.split(b"\t", 1)
        mode, kind, sha = metadata.decode().split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise RuntimeError("Maintenance overlay must contain ordinary tracked files")
        overlay[filename.decode()] = (int(mode, 8), subprocess.check_output(
            ["git", "cat-file", "blob", sha], cwd=maintenance))
    required = {".github/workflows/patched-release.yml", "scripts/fork-release/release.py",
                "patches/revision", "scripts/verify_legacy_firewall_cleanup.py", "FORK.md"}
    if not required.issubset(overlay):
        raise RuntimeError("Required maintenance overlay files are not committed")
    revision_text = overlay["patches/revision"][1].decode().strip()
    if not re.fullmatch(r"[1-9][0-9]{0,2}", revision_text):
        raise RuntimeError("patches/revision must be an integer between 1 and 999")
    revision = int(revision_text)
    release = api(f"{UPSTREAM}/releases/latest")
    upstream_tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", upstream_tag):
        raise RuntimeError("Expected an upstream stable vMAJOR.MINOR.PATCH release")
    if source.exists():
        raise RuntimeError(f"Refusing to replace existing checkout: {source}")
    output.mkdir(parents=True, exist_ok=True)
    run("git", "init", "--quiet", str(source))
    run("git", "fetch", "--no-tags", f"https://github.com/{UPSTREAM}.git",
        f"refs/tags/{upstream_tag}", cwd=source)
    upstream_sha = run("git", "rev-parse", "FETCH_HEAD^{commit}", cwd=source)
    run("git", "checkout", "--quiet", "--detach", upstream_sha, cwd=source)
    patches = sorted(path for path in overlay if re.fullmatch(r"patches/[^/]+\.patch", path))
    if not patches:
        raise RuntimeError("No patches/*.patch files found")
    for patch in patches:
        subprocess.run(["git", "apply", "--check", "-"], input=overlay[patch][1], cwd=source, check=True)
        subprocess.run(["git", "apply", "--index", "-"], input=overlay[patch][1], cwd=source, check=True)
    gradle = source / "mobile/build.gradle.kts"
    original = gradle.read_text()
    codes = re.findall(r"(?m)^        versionCode = ([0-9]+)$", original)
    names = re.findall(r'(?m)^        versionName = "([^"\n]+)"$', original)
    if len(codes) != 1 or names != [upstream_tag[1:]]:
        raise RuntimeError("Upstream version declarations changed; review the release script")
    version_code = int(codes[0]) * 1000 + revision
    if version_code > 2_100_000_000:
        raise RuntimeError("Patched versionCode exceeds Android's limit")
    version_name = f"{names[0]}-p{revision}"
    gradle.write_text(original.replace(f"        versionCode = {codes[0]}\n",
                                       f"        versionCode = {version_code}\n")
                     .replace(f'        versionName = "{names[0]}"\n',
                              f'        versionName = "{version_name}"\n'))
    # Preserve only trusted maintenance files, including the complete workflow tree.
    # GitHub can reject tags introducing workflows absent from an authorized branch.
    for directory in OVERLAY[:3]:
        if (source / directory).exists():
            shutil.rmtree(source / directory)
    for filename, (mode, content) in overlay.items():
        destination = source / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(mode & 0o777)
    run("git", "add", "--all", "--", "mobile/build.gradle.kts", *OVERLAY, cwd=source)
    source_tree = run("git", "write-tree", cwd=source)
    if run("git", "rev-parse", f"{source_tree}:.github/workflows", cwd=source) != run(
            "git", "rev-parse", f"{maintenance_sha}:.github/workflows", cwd=maintenance):
        raise RuntimeError("Release workflows differ from the authorized maintenance commit")
    tag = f"{upstream_tag}-p{revision}"
    info = {
        "upstream_repository": UPSTREAM, "upstream_tag": upstream_tag, "upstream_sha": upstream_sha,
        "patch_revision": revision, "release_tag": tag, "version_name": version_name,
        "version_code": version_code, "package_name": "be.mygod.vpnhotspot",
        "patches": {Path(patch).name: hashlib.sha256(overlay[patch][1]).hexdigest() for patch in patches},
        "maintenance_overlay": {"commit": maintenance_sha, "files_sha256": {
            path: hashlib.sha256(content).hexdigest() for path, (_, content) in sorted(overlay.items())}},
        "source_tree_sha": source_tree,
        "workflow_sha": maintenance_sha,
        "workflow_run": f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    }
    existing = tagged_commit(repo, tag)
    published = api(f"{repo}/releases/tags/{tag}", missing=True)
    if existing and not same_source(existing, info):
        raise RuntimeError(f"{tag} already refers to different source; increment patches/revision")
    if published and not existing:
        raise RuntimeError(f"{tag} release exists without a source tag; inspect it before retrying")
    skip = bool(published and not published["draft"])
    if existing:
        run("git", "fetch", "--no-tags", f"https://github.com/{repo}.git", f"refs/tags/{tag}", cwd=source)
        run("git", "reset", "--hard", existing["sha"], cwd=source)
    else:
        timestamp = run("git", "show", "-s", "--format=%cI", upstream_sha, cwd=source)
        environment = os.environ | {
            "GIT_AUTHOR_NAME": "VPNHotspot patch builder", "GIT_COMMITTER_NAME": "VPNHotspot patch builder",
            "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp,
        }
        message = (f"Apply VPNHotspot patch revision {revision}\n\nUpstream: {UPSTREAM}@{upstream_sha}\n"
                   f"Maintenance-Overlay: {maintenance_sha}\nPatch-Revision: {revision}")
        run("git", "commit", "--quiet", "--no-gpg-sign", "-m", message, cwd=source, env=environment)
    info["source_sha"] = run("git", "rev-parse", "HEAD", cwd=source)
    write_json(output / "build-info.json", info)
    if not skip:
        run("git", "bundle", "create", str(output / "source.bundle"), "HEAD", cwd=source)
    with open(os.environ["GITHUB_OUTPUT"], "a") as outputs:
        outputs.write(f"skip={str(skip).lower()}\ntag={tag}\n")
    print(f"{'Already published' if skip else 'Prepared'} {tag}: {info['source_sha']}")


def restore(args):
    info = json.loads((args.prepared / "build-info.json").read_text())
    run("git", "clone", "--quiet", "--no-checkout", str(args.prepared / "source.bundle"), str(args.source))
    run("git", "checkout", "--quiet", "--detach", info["source_sha"], cwd=args.source)
    if run("git", "rev-parse", "HEAD^{tree}", cwd=args.source) != info["source_tree_sha"]:
        raise RuntimeError("Prepared source tree does not match its provenance")


def signing_certificate(verification):
    if not re.search(r"(?m)^Number of signers: 1\r?$", verification):
        raise RuntimeError("Expected exactly one APK signer")
    # Build Tools 37 labels certificates by scheme; older versions use Signer #1.
    certificates = {value.lower() for value in re.findall(
        r"(?m)^(?:Signer #1|V[0-9]+(?:\.[0-9]+)* Signer:) certificate SHA-256 digest: "
        r"([0-9a-fA-F]{64})\r?$", verification)}
    if len(certificates) != 1:
        raise RuntimeError(f"APK signing certificates differ or could not be verified:\n{verification}")
    return certificates.pop()


def sign(args):
    info = json.loads((args.prepared / "build-info.json").read_text())
    for key in ("APK_SIGNING_KEY", "APK_SIGNING_PASSWORD", "APK_SIGNING_ALIAS"):
        if not os.environ.get(key):
            raise RuntimeError(f"Missing repository secret {key}; there is no debug-key fallback")
    tools = Path(os.environ["ANDROID_HOME"]) / "build-tools/37.0.0"
    expected = {f"mobile-{abi}-release-unsigned.apk" for abi in ABIS}
    if {path.name for path in args.apks.glob("*.apk")} != expected:
        raise RuntimeError("Expected exactly four unsigned APKs, one per supported ABI")
    args.output.mkdir(parents=True, exist_ok=True)
    certificate = None
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP")) as temp:
        key = Path(temp) / "release.p12"
        key.write_bytes(base64.b64decode("".join(os.environ["APK_SIGNING_KEY"].split()), validate=True))
        key.chmod(0o600)
        for abi in ABIS:
            apk = args.apks / f"mobile-{abi}-release-unsigned.apk"
            with zipfile.ZipFile(apk) as archive:
                if f"lib/{abi}/libvpnhotspotd.so" not in archive.namelist():
                    raise RuntimeError(f"Missing {abi} daemon in {apk.name}")
            badging = run(str(tools / "aapt2"), "dump", "badging", str(apk))
            expected_package = (f"package: name='{info['package_name']}' versionCode='{info['version_code']}' "
                                f"versionName='{info['version_name']}'")
            if not badging.startswith(expected_package):
                raise RuntimeError(f"Wrong package or version in {apk.name}")
            aligned = Path(temp) / "aligned.apk"
            signed = args.output / f"mobile-{abi}-release.apk"
            run(str(tools / "zipalign"), "-f", "-P", "16", "4", str(apk), str(aligned))
            run(str(tools / "apksigner"), "sign", "--ks", str(key), "--ks-type", "PKCS12",
                "--ks-key-alias", os.environ["APK_SIGNING_ALIAS"], "--ks-pass", "env:APK_SIGNING_PASSWORD",
                "--key-pass", "env:APK_SIGNING_PASSWORD", "--out", str(signed), str(aligned))
            verification = run(str(tools / "apksigner"), "verify", "--verbose", "--print-certs", str(signed))
            verified_certificate = signing_certificate(verification)
            if certificate and verified_certificate != certificate:
                raise RuntimeError("APK signing certificates differ across ABIs")
            certificate = verified_certificate
            run(str(tools / "zipalign"), "-c", "-P", "16", "4", str(signed))
    info["signing_certificate_sha256"] = certificate
    info["apk_sha256"] = {path.name: digest(path) for path in sorted(args.output.glob("*.apk"))}
    write_json(args.output / "build-info.json", info)
    paths = sorted(args.output.glob("*.apk")) + [args.output / "build-info.json"]
    (args.output / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in paths))
    print(f"Verified four APKs; signing certificate SHA-256: {certificate}")


def publish(args):
    repo = os.environ["GITHUB_REPOSITORY"]
    info = json.loads((args.output / "build-info.json").read_text())
    tag = info["release_tag"]
    if run("git", "rev-parse", "HEAD", cwd=args.source) != info["source_sha"]:
        raise RuntimeError("Publication source differs from the verified build")
    existing = tagged_commit(repo, tag)
    release = api(f"{repo}/releases/tags/{tag}", missing=True)
    if existing and (existing["sha"] != info["source_sha"] or not same_source(existing, info)):
        raise RuntimeError("Release tag changed while validation was running")
    if release and not release["draft"]:
        raise RuntimeError("Release is already published; refusing to overwrite its assets")
    if release and not existing:
        raise RuntimeError("An existing draft has no corresponding source tag")
    if release:
        manifests = [asset for asset in release["assets"] if asset["name"] == "build-info.json"]
        if manifests:
            if len(manifests) != 1:
                raise RuntimeError("Draft contains duplicate build manifests")
            with tempfile.TemporaryDirectory() as temp:
                run("gh", "release", "download", tag, "--repo", repo,
                    "--pattern", "build-info.json", "--dir", temp)
                manifest = Path(temp) / "build-info.json"
                if manifests[0].get("digest") != f"sha256:{digest(manifest)}":
                    raise RuntimeError("Draft build manifest digest could not be verified")
                original = json.loads(manifest.read_text())
                # A fresh workflow has a new run URL, even for identical source and APKs.
                # Retain the first verified build's provenance without replacing remote assets.
                if ({key: value for key, value in original.items() if key != "workflow_run"}
                        != {key: value for key, value in info.items() if key != "workflow_run"}):
                    raise RuntimeError("Draft build identity or APKs differ; inspect the draft before retrying")
                (args.output / "build-info.json").write_bytes(manifest.read_bytes())
                info = original
            paths = sorted(args.output.glob("*.apk")) + [args.output / "build-info.json"]
            (args.output / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in paths))
        expected_assets = {path.name: f"sha256:{digest(path)}"
                           for path in sorted(args.output.glob("*.apk"))
                           + [args.output / "build-info.json", args.output / "SHA256SUMS"]}
        seen = set()
        for asset in release["assets"]:
            name = asset["name"]
            if name in seen or name not in expected_assets or asset.get("digest") != expected_assets[name]:
                raise RuntimeError(f"Draft asset {name} differs; inspect the draft before retrying")
            seen.add(name)
    previous = api(f"{repo}/releases/latest", missing=True)
    if previous:
        with tempfile.TemporaryDirectory() as temp:
            run("gh", "release", "download", previous["tag_name"], "--repo", repo,
                "--pattern", "build-info.json", "--dir", temp)
            old = json.loads((Path(temp) / "build-info.json").read_text())
            if old["signing_certificate_sha256"] != info["signing_certificate_sha256"]:
                raise RuntimeError("Signing key changed; refusing an APK that cannot update the previous release")
            if old["version_code"] >= info["version_code"]:
                raise RuntimeError("New release must increase versionCode")
    if not existing:
        auth = base64.b64encode(f"x-access-token:{os.environ['GH_TOKEN']}".encode()).decode()
        environment = os.environ | {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}"}
        run("git", "push", f"https://github.com/{repo}.git", f"HEAD:refs/tags/{tag}",
            cwd=args.source, env=environment)
    notes = (f"基于 [{info['upstream_tag']}](https://github.com/{UPSTREAM}/releases/tag/{info['upstream_tag']})，"
             f"补丁修订 {info['patch_revision']}。\n\n"
             f"- 上游 commit：`{info['upstream_sha']}`\n"
             f"- 实际编译源码 commit：`{info['source_sha']}`\n"
             f"- 维护 workflow commit：`{info['workflow_sha']}`\n"
             f"- [GitHub Actions 验证记录]({info['workflow_run']})\n"
             f"- 签名证书 SHA-256：`{info['signing_certificate_sha256']}`\n\n"
             "已通过隔离网络命名空间的防火墙回归、Rust 检查和测试、Android 构建与检查、R8 检查及 APK 验签。"
             "这些检查不能代替用户设备上的 ROM/内核和实际热点连接测试。\n\n"
             "请下载与设备 ABI 对应的 APK（多数新手机是 arm64-v8a）。"
             "本 fork 使用独立签名，首次不能直接覆盖官方版；请先备份设置、关闭 VPN 共享再处理原版安装。"
             "后续本 fork 的版本使用同一签名，可以正常更新。"
             "详见 build-info.json 和 SHA256SUMS。\n")
    notes_path = args.output / "release-notes.md"
    notes_path.write_text(notes)
    assets = sorted(args.output.glob("*.apk")) + [args.output / "build-info.json", args.output / "SHA256SUMS"]
    if not release:
        run("gh", "release", "create", tag, "--repo", repo, "--verify-tag", "--draft", "--title", tag,
            "--notes-file", str(notes_path))
    for path in assets:
        current = api(f"{repo}/releases/tags/{tag}")
        if not current["draft"]:
            raise RuntimeError("Release was published concurrently; refusing to change its assets")
        matching = [asset for asset in current["assets"] if asset["name"] == path.name]
        if matching:
            if len(matching) != 1 or matching[0].get("digest") != f"sha256:{digest(path)}":
                raise RuntimeError(f"Draft asset {path.name} differs; inspect the draft before retrying")
        else:
            run("gh", "release", "upload", tag, "--repo", repo, str(path))
    uploaded = api(f"{repo}/releases/tags/{tag}")
    if not uploaded["draft"]:
        raise RuntimeError("Draft was published concurrently; stop before changing its metadata")
    if {asset["name"] for asset in uploaded["assets"]} != {path.name for path in assets}:
        raise RuntimeError("Draft release assets are incomplete or contain unexpected files")
    run("gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--latest", "--notes-file", str(notes_path))
    print(f"Published https://github.com/{repo}/releases/tag/{tag}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "restore", "sign", "publish"))
    parser.add_argument("--maintenance", type=Path, default=Path("maintenance"))
    parser.add_argument("--source", type=Path, default=Path("source"))
    parser.add_argument("--prepared", type=Path, default=Path("prepared"))
    parser.add_argument("--apks", type=Path, default=Path("unsigned"))
    parser.add_argument("--output", type=Path, default=Path("signed"))
    arguments = parser.parse_args()
    {"prepare": prepare, "restore": restore, "sign": sign, "publish": publish}[arguments.command](arguments)
