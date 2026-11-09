import tempfile
import unittest
from pathlib import Path

import server


class StaticAssetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "assets").mkdir()
        (root / "assets" / "index-abc.js").write_text("x")
        (root / "index.html").write_text("<html>")
        (root / "secret.js").write_text("no")
        self.old = server.DIST
        server.DIST = root

    def tearDown(self):
        server.DIST = self.old
        self.tmp.cleanup()

    def test_serves_assets(self):
        f, ctype = server.static_asset(server.DIST, "/assets/index-abc.js")
        self.assertEqual((f.name, ctype), ("index-abc.js", "text/javascript"))

    def test_rejects_traversal_and_unknown(self):
        for p in ["/assets/../secret.js", "/assets/%2e%2e/secret.js", "/assets/..%2fsecret.js",
                  "/assets/", "/assets//etc/passwd", "/assets/missing.js", "/assets/a\\..\\secret.js",
                  "/assets/../index.html", "/index.html", "/server.py", "/assets/index-abc.js%00.css"]:
            self.assertIsNone(server.static_asset(server.DIST, p), p)

    def test_no_ui_serves_nothing(self):
        self.assertIsNone(server.static_asset(None, "/assets/index-abc.js"))


if __name__ == "__main__":
    unittest.main()
