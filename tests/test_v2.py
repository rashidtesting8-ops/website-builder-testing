import datetime
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.cleanup import cleanup
from app.social_collect import check_social_url, collect_social, cutoff_year
from PIL import Image


class WorkflowV2Tests(unittest.TestCase):
    def test_social_hosts_no_domain_spoof(self) -> None:
        for bad in [
            "https://instagram.com.evil.example/a",
            "http://www.instagram.com/a",
            "https://example.org/facebook.com",
            "file:///tmp/a",
            "https://www.facebook.com/x",
        ]:
            with self.assertRaises(ValueError):
                check_social_url("instagram", bad)
        self.assertEqual(
            check_social_url("facebook", "https://www.facebook.com/dogs/"),
            "https://www.facebook.com/dogs/",
        )
        self.assertEqual(str(cutoff_year(datetime.date(2024, 2, 29))), "2023-02-28")

    def test_social_collection_runner_date_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            project = Path(t) / "demo"
            (project / "research").mkdir(parents=True)
            saved: list[list[str]] = []

            def fake_runner(command: list[str], stage: Path) -> tuple[int, str]:
                saved.append(command)
                im = Image.new("RGB", (100, 100))
                im.save(stage / "dog.png")
                (stage / "corrupt.jpg").write_bytes(b"not image")
                (stage / "bad.pdf").write_bytes(b"not allowed")
                return 0, ""

            out = collect_social(
                project,
                "instagram",
                "https://www.instagram.com/demo/",
                max_files=4,
                runner=fake_runner,
            )
            self.assertEqual(out["files_added"], 1)
            self.assertEqual(out["status"], "collected_needs_review")
            self.assertNotIn("--date-after", saved[0])
            self.assertEqual(saved[0][saved[0].index("--range") + 1], "1-4")
            mani = json.loads(
                (project / "research/social-media-manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(mani[0]["post_date_verified"])
            self.assertFalse(mani[0]["rights_confirmed_for_preview"])
            self.assertTrue((project / mani[0]["file"]).exists())
            again = collect_social(
                project,
                "instagram",
                "https://www.instagram.com/demo/",
                max_files=4,
                runner=fake_runner,
            )
            self.assertEqual(again["files_added"], 0)

    def test_cleanup_blocks_early_and_retains_used_assets(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "salon"
            for rel in ["assets/website", "assets/instagram", "delivery", "research", "website"]:
                (root / rel).mkdir(parents=True, exist_ok=True)
            used = root / "assets/website/official-logo.png"
            used.write_bytes(b"logo data")
            ignored = root / "assets/instagram/old-reel.mp4"
            ignored.write_bytes(b"old")
            with self.assertRaises(ValueError):
                cleanup(root)
            (root / "delivery/approved-homepage.html").write_text(
                '<img src="assets/website/official-logo.png">',
                encoding="utf-8",
            )
            (root / "delivery/ghl-ready.html").write_text(
                "<section>valid</section>",
                encoding="utf-8",
            )
            (root / "delivery/retained-assets.json").write_text(
                json.dumps({"approved": True, "assets": []}),
                encoding="utf-8",
            )
            plan = cleanup(root)
            self.assertEqual(plan["removable_count"], 1)
            with self.assertRaises(ValueError):
                cleanup(root, "prune", confirm=True)
            result = cleanup(root, "archive")
            self.assertTrue(Path(str(result["archive"])).exists())
            with self.assertRaises(ValueError):
                cleanup(root, "prune", confirm=False)
            result = cleanup(root, "prune", confirm=True)
            self.assertEqual(result["deleted_files"], 1)
            self.assertTrue(used.exists())
            self.assertFalse(ignored.exists())
            with zipfile.ZipFile(str(result["archive"])) as z:
                self.assertIn("assets/website/official-logo.png", z.namelist())
                self.assertIn("delivery/approved-homepage.html", z.namelist())

    def test_no_symlink_retention_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "salon"
            (root / "delivery").mkdir(parents=True)
            (root / "delivery/approved-homepage.html").write_text("ok", encoding="utf-8")
            (root / "delivery/ghl-ready.html").write_text("ok", encoding="utf-8")
            (root / "delivery/retained-assets.json").write_text(
                json.dumps({"approved": True, "assets": ["assets/website/../../../escape.jpg"]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                cleanup(root)


if __name__ == "__main__":
    unittest.main()

