"""Regression tests for draft recovery; never contact GitHub or sign an APK."""

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("fork_release", Path(__file__).with_name("release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class DraftRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "signed"
        self.output.mkdir()
        self.original = self.root / "original"
        self.original.mkdir()
        apks = {}
        for abi in release.ABIS:
            name = f"mobile-{abi}-release.apk"
            (self.output / name).write_bytes(f"signed test fixture: {abi}".encode())
            (self.original / name).write_bytes((self.output / name).read_bytes())
            apks[name] = release.digest(self.output / name)
        self.info = {
            "release_tag": "v3.0.8-p1", "source_sha": "source", "source_tree_sha": "tree",
            "upstream_sha": "upstream", "upstream_tag": "v3.0.8", "patch_revision": 1,
            "version_code": 2011001, "version_name": "3.0.8-p1",
            "package_name": "be.mygod.vpnhotspot", "workflow_sha": "maintenance",
            "workflow_run": "https://github.com/example/fork/actions/runs/222",
            "signing_certificate_sha256": "certificate", "apk_sha256": apks,
        }
        self.old_info = self.info | {"workflow_run": "https://github.com/example/fork/actions/runs/111"}
        release.write_json(self.output / "build-info.json", self.info)
        release.write_json(self.original / "build-info.json", self.old_info)
        for directory in (self.output, self.original):
            paths = sorted(directory.glob("*.apk")) + [directory / "build-info.json"]
            (directory / "SHA256SUMS").write_text("".join(
                f"{release.digest(path)}  {path.name}\n" for path in paths))
        self.draft = {"draft": True, "assets": [self.asset(path) for path in sorted(self.original.iterdir())]}
        self.uploads = []
        self.published = False
        self.mutations = []
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/fork"}).start()
        patch.object(release, "tagged_commit", return_value={
            "sha": "source", "tree": {"sha": "tree"}, "parents": [{"sha": "upstream"}],
        }).start()
        patch.object(release, "api", side_effect=self.api).start()
        patch.object(release, "run", side_effect=self.run_command).start()

    @staticmethod
    def asset(path):
        return {"name": path.name, "digest": f"sha256:{release.digest(path)}"}

    def api(self, path, missing=False):
        return None if path.endswith("/latest") else copy.deepcopy(self.draft)

    def run_command(self, *args, **kwargs):
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "source"
        if args[:3] == ("gh", "release", "download"):
            target = Path(args[args.index("--dir") + 1]) / "build-info.json"
            target.write_bytes((self.original / "build-info.json").read_bytes())
            return ""
        if args[:3] == ("gh", "release", "upload"):
            path = Path(args[-1])
            self.uploads.append(path.name)
            self.mutations.append(args)
            self.draft["assets"].append(self.asset(path))
            return ""
        if args[:3] == ("gh", "release", "edit"):
            self.published = True
            self.mutations.append(args)
            return ""
        raise AssertionError(f"Unexpected external command: {args}")

    def publish(self):
        release.publish(argparse.Namespace(output=self.output, source=self.root / "source"))

    def test_new_run_preserves_original_manifest_and_checksums(self):
        self.publish()
        self.assertTrue(self.published)
        self.assertEqual(self.uploads, [])
        for name in ("build-info.json", "SHA256SUMS"):
            self.assertEqual((self.output / name).read_bytes(), (self.original / name).read_bytes())
        self.assertIn(self.old_info["workflow_run"], (self.output / "release-notes.md").read_text())

    def test_partial_draft_uploads_only_missing_assets(self):
        self.draft["assets"] = [asset for asset in self.draft["assets"]
                                if asset["name"] == "build-info.json"]
        self.publish()
        self.assertTrue(self.published)
        self.assertEqual(set(self.uploads), set(self.info["apk_sha256"]) | {"SHA256SUMS"})

    def test_changed_build_identity_never_mutates_remote(self):
        for key, changed in (("signing_certificate_sha256", "other"),
                             ("source_sha", "other"), ("version_code", 2011002),
                             ("apk_sha256", {"changed.apk": "other"})):
            with self.subTest(key=key):
                release.write_json(self.output / "build-info.json", self.info | {key: changed})
                with self.assertRaises(RuntimeError):
                    self.publish()
                self.assertEqual(self.mutations, [])

    def test_manifest_digest_mismatch_never_mutates_remote(self):
        for asset in self.draft["assets"]:
            if asset["name"] == "build-info.json":
                asset["digest"] = "sha256:wrong"
        with self.assertRaisesRegex(RuntimeError, "manifest digest"):
            self.publish()
        self.assertEqual(self.mutations, [])

    def test_existing_apk_mismatch_never_mutates_remote(self):
        for asset in self.draft["assets"]:
            if asset["name"].endswith(".apk"):
                asset["digest"] = "sha256:wrong"
                break
        with self.assertRaisesRegex(RuntimeError, "Draft asset"):
            self.publish()
        self.assertEqual(self.mutations, [])

    def test_unexpected_asset_never_mutates_remote(self):
        self.draft["assets"].append({"name": "unexpected.txt", "digest": "sha256:other"})
        with self.assertRaisesRegex(RuntimeError, "unexpected.txt"):
            self.publish()
        self.assertEqual(self.mutations, [])

    def test_published_release_is_immutable(self):
        self.draft["draft"] = False
        with self.assertRaisesRegex(RuntimeError, "already published"):
            self.publish()
        self.assertEqual(self.mutations, [])


class SigningCertificateTest(unittest.TestCase):
    certificate = "37990b9e22dd4ee8cf77d78e922af16f7ef329107f15062ecc8e2886e2d3438c"

    def test_build_tools_37_scheme_label(self):
        output = ("Verifies\nNumber of signers: 1\nV3.0 Signer: certificate DN: CN=test\n"
                  f"V3.0 Signer: certificate SHA-256 digest: {self.certificate}\n")
        self.assertEqual(release.signing_certificate(output), self.certificate)

    def test_legacy_label_uppercase_and_windows_newlines(self):
        output = ("Number of signers: 1\r\n"
                  f"Signer #1 certificate SHA-256 digest: {self.certificate.upper()}\r\n")
        self.assertEqual(release.signing_certificate(output), self.certificate)

    def test_multiple_schemes_must_have_same_certificate(self):
        output = ("Number of signers: 1\n"
                  f"V3.0 Signer: certificate SHA-256 digest: {self.certificate}\n"
                  f"V3.1 Signer: certificate SHA-256 digest: {self.certificate}\n")
        self.assertEqual(release.signing_certificate(output), self.certificate)
        with self.assertRaises(RuntimeError):
            release.signing_certificate(output.replace(
                f"V3.1 Signer: certificate SHA-256 digest: {self.certificate}",
                f"V3.1 Signer: certificate SHA-256 digest: {'0' * 64}"))

    def test_multiple_signers_are_rejected(self):
        with self.assertRaises(RuntimeError):
            release.signing_certificate("Number of signers: 2\n"
                                        f"Signer #1 certificate SHA-256 digest: {self.certificate}\n")

    def test_missing_or_malformed_certificate_is_rejected(self):
        for output in ("", "Number of signers: 1\n",
                       "Number of signers: 1\nV3.0 Signer: certificate SHA-256 digest: abc\n"):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                release.signing_certificate(output)


if __name__ == "__main__":
    unittest.main()
