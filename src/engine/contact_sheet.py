"""Make a small local image-review sheet from verified manifest file paths.

No AI and no external network access. Preserves source filenames for a human to
check logo and real pet photos before any AI design work.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError


def make_sheet(
    project: str | Path,
    *,
    limit: int = 80,
    columns: int = 5,
) -> dict[str, Any]:
    project_path = Path(project).resolve()
    if not 1 <= limit <= 160 or not 2 <= columns <= 8:
        raise ValueError("invalid limits")
    records: list[dict[str, Any]] = []
    for f in [
        project_path / "research/asset-manifest.json",
        project_path / "research/social-media-manifest.json",
    ]:
        if f.exists():
            obj = json.loads(f.read_text(encoding="utf8"))
            records.extend(r for r in obj if r.get("file"))
    # If manifest paths were pruned after approval (cleanup), fall back to the
    # retained optimized assets that still exist on disk (brief §C contact sheet).
    if not any(
        (project_path / str(rec.get("file", ""))).is_file()
        for rec in records
        if rec.get("file")
    ):
        roots = [project_path / "assets" / "optimized"]
        # Approved + cleaned-up projects keep retained assets inside each frozen version.
        versions_dir = project_path / "versions"
        if versions_dir.is_dir():
            roots.extend(
                v / "assets" / "optimized"
                for v in sorted(versions_dir.iterdir())
                if v.is_dir()
            )
        found: list[dict[str, Any]] = []
        for root in roots:
            if not root.is_dir():
                continue
            found.extend(
                {
                    "file": p.relative_to(project_path).as_posix(),
                    "source": "retained_optimized",
                }
                for p in sorted(root.iterdir())
                if p.is_file()
                and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif"}
            )
            if found:
                break
        records = found

    items: list[tuple[Path, dict[str, Any]]] = []
    for rec in records:
        if len(items) >= limit:
            break
        path = (project_path / str(rec["file"])).resolve()
        if (
            not path.is_relative_to(project_path)
            or not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif"}
        ):
            continue
        items.append((path, rec))
    width = 220
    height = 220
    sheet = Image.new(
        "RGB",
        (columns * width, math.ceil(max(1, len(items)) / columns) * height),
        "#f6f6f5",
    )
    draw = ImageDraw.Draw(sheet)
    for i, (path, rec) in enumerate(items):
        x = i % columns * width
        y = i // columns * height
        try:
            with Image.open(path) as im:
                im.thumbnail((204, 165))
                frame = ImageOps.contain(im.convert("RGB"), (204, 165))
                sheet.paste(
                    frame,
                    (x + (width - frame.width) // 2, y + (165 - frame.height) // 2 + 6),
                )
        except (UnidentifiedImageError, ValueError, OSError):
            continue
        label = path.name[:28]
        draw.text((x + 7, y + 176), label, fill="#252525")
        draw.text(
            (x + 7, y + 194),
            str(rec.get("platform", "website")) + " | " + str(rec.get("dimensions", ""))[:16],
            fill="#666666",
        )
    out = project_path / "qa/contact-sheet.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=78, optimize=True)
    return {"output": str(out), "images_shown": len(items), "size_bytes": out.stat().st_size}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    args = ap.parse_args()
    print(json.dumps(make_sheet(args.project), indent=2))


if __name__ == "__main__":
    main()

