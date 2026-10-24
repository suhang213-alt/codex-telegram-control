"""Publication scans report secret locations without echoing secret values."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


path = Path(__file__).resolve().parents[1] / "scripts" / "verify_release.py"
spec = importlib.util.spec_from_file_location("publication_scan", path)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class ReleaseSecurityTests(unittest.TestCase):
    def scan_text(self, text):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            body = text.encode()
            (root / "example.txt").write_bytes(body)
            manifest = {"schema": "public-release-manifest/1", "files": [
                {"path": "example.txt", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            ]}
            (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            return scan.verify(root)[1]

    def test_docker_credentials_block_publication_without_echo(self):
        for kind in ("pat", "oat"):
            value = "dckr_" + kind + "_" + "A" * 36
            findings = self.scan_text("example\n" + value)
            self.assertEqual(findings, [{"path": "example.txt", "line": 2, "rule": "docker_secret"}])
            self.assertNotIn(value, json.dumps(findings))

    def test_public_digest_and_placeholder_are_accepted(self):
        self.assertEqual(self.scan_text("image@sha256:" + "a" * 64 + "\nDOCKERHUB_TOKEN=<your-token>"), [])


if __name__ == "__main__":
    unittest.main()
