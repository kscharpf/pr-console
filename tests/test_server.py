import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import server


class StaticAssetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "assets").mkdir()
        (root / "assets" / "index-abc.js").write_text("x")
        (root / "index.html").write_text("<html>")
        (root / "secret.js").write_text("no")
        self.old = server.DIST
        server.DIST = root

    def tearDown(self) -> None:
        server.DIST = self.old
        self.tmp.cleanup()

    def test_serves_assets(self) -> None:
        asset = server.static_asset(server.DIST, "/assets/index-abc.js")
        assert asset is not None  # noqa: S101  narrows the type for mypy; assertEqual below checks the value
        f, ctype = asset
        self.assertEqual((f.name, ctype), ("index-abc.js", "text/javascript"))

    def test_rejects_traversal_and_unknown(self) -> None:
        for p in [
            "/assets/../secret.js",
            "/assets/%2e%2e/secret.js",
            "/assets/..%2fsecret.js",
            "/assets/",
            "/assets//etc/passwd",
            "/assets/missing.js",
            "/assets/a\\..\\secret.js",
            "/assets/../index.html",
            "/index.html",
            "/server.py",
            "/assets/index-abc.js%00.css",
        ]:
            self.assertIsNone(server.static_asset(server.DIST, p), p)

    def test_no_ui_serves_nothing(self) -> None:
        self.assertIsNone(server.static_asset(None, "/assets/index-abc.js"))


if __name__ == "__main__":
    unittest.main()


class ForcedRefresh(unittest.TestCase):
    def test_force_clears_verdicts_and_queues_behind_a_running_refresh(self) -> None:
        cache = server.Cache()
        calls: list[bool] = []
        cache.status[1] = {"head": "a", "at": time.time(), "verdict": None}  # type: ignore[typeddict-item]  # minimal entry

        def fake_list(_args: list[str], timeout: int = 120) -> list[object]:  # noqa: ARG001
            calls.append(cache.data["refreshing"])
            if len(calls) == 1:
                cache.refresh(force=True)  # a click while this refresh runs: queued, not dropped
            return []

        with mock.patch.object(server, "gh_json", fake_list):
            cache.refresh(force=True)
        self.assertEqual(calls, [True, True])  # ran twice: the queued forced refresh ran after
        self.assertEqual(cache.status, {})
        self.assertFalse(cache.data["refreshing"])
        self.assertFalse(cache.rerun)
