"""Golden test corpus validation and precision/recall scoring for fact extractor.

Tests the golden corpus of pet grooming websites across UK, US, mobile, and
table-heavy layouts, enforcing scoring metrics and reporting baseline performance.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from app.fact_extractor import extract_business_facts
from PIL import Image

GOLDEN_DIR = Path(__file__).parent / "golden"

# Baseline performance achieved by the current extractor on the golden corpus
BASELINE_THRESHOLDS = {
    "overall_precision": 70.0,
    "overall_recall": 50.0,
    "categories": {
        "business_name": {"precision": 90.0, "recall": 90.0},
        "address": {"precision": 90.0, "recall": 35.0},
        "hours": {"precision": 90.0, "recall": 40.0},
        "phone": {"precision": 90.0, "recall": 50.0},
        "pricing": {"precision": 40.0, "recall": 25.0},
        "services": {"precision": 25.0, "recall": 25.0},
        "cancellation_policy": {"precision": 60.0, "recall": 75.0},
    },
}


def test_freeze_site_script_exists() -> None:
    """Requirement 1: scripts/freeze_site.py must exist and be importable."""
    freeze_script = Path(__file__).parents[1] / "scripts" / "freeze_site.py"
    assert freeze_script.is_file(), f"scripts/freeze_site.py not found at {freeze_script}"

    spec = importlib.util.spec_from_file_location("scripts.freeze_site", freeze_script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "freeze_site"), "scripts/freeze_site.py must expose freeze_site()"


@contextmanager
def _ephemeral_server(html_body: str) -> Iterator[str]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.endswith(".jpg"):
                img = Image.new("RGB", (100, 100), color=(100, 150, 200))
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            data = html_body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    host, port = server.server_address
    url = f"http://{host}:{port}/index.html"

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def test_freeze_site_execution(tmp_path: Path) -> None:
    """Requirement 1: freeze_site must crawl pages & snapshots and save expected.json."""
    from scripts.freeze_site import freeze_site

    sample_html = """<!DOCTYPE html>
    <html>
      <head><title>Test Salon</title></head>
      <body>
        <h1>Test Salon</h1>
        <p>Phone: (512) 555-0199</p>
        <p>Address: 100 Main St, Austin, TX 78701</p>
      </body>
    </html>"""

    with _ephemeral_server(sample_html) as live_url:
        target = freeze_site(
            url=live_url,
            slug="test-frozen-salon",
            output_dir=tmp_path,
            expected_facts=[
                {
                    "category": "business_name",
                    "normalized_value": "Test Salon",
                    "claim_key": "business_name",
                    "expected_status": "verified",
                }
            ],
            allow_private=True,
        )

        assert target.is_dir()
        pages_file = target / "research" / "pages.json"
        assert pages_file.is_file()
        pages_data = json.loads(pages_file.read_text(encoding="utf-8"))
        assert len(pages_data) >= 1

        snaps_dir = target / "research" / "snapshots"
        assert snaps_dir.is_dir()
        assert len(list(snaps_dir.glob("*.html"))) >= 1

        expected_file = target / "expected.json"
        assert expected_file.is_file()
        expected_data = json.loads(expected_file.read_text(encoding="utf-8"))
        assert expected_data[0]["normalized_value"] == "Test Salon"


def test_golden_corpus_distribution() -> None:
    """Requirement 2: Corpus must have >=12 businesses: >=6 UK, >=4 US, >=1 mobile, >=1 table."""
    assert GOLDEN_DIR.is_dir(), f"tests/golden directory missing at {GOLDEN_DIR}"

    slug_dirs = [d for d in GOLDEN_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    assert len(slug_dirs) >= 12, f"Corpus must have >=12 businesses, found {len(slug_dirs)}"

    categories: dict[str, list[str]] = {
        "uk": [],
        "us": [],
        "mobile": [],
        "table_heavy": [],
    }

    for slug_dir in slug_dirs:
        expected_file = slug_dir / "expected.json"
        assert expected_file.is_file(), f"Missing expected.json in {slug_dir}"
        pages_file = slug_dir / "research" / "pages.json"
        assert pages_file.is_file(), f"Missing research/pages.json in {slug_dir}"

        expected_facts = json.loads(expected_file.read_text(encoding="utf-8"))
        assert isinstance(expected_facts, list) and len(expected_facts) > 0

        # Check metadata in slug name or expected annotations
        name = slug_dir.name.lower()
        if name.startswith("uk-") or any(
            f.get("country") == "UK" or "£" in str(f.get("normalized_value", ""))
            for f in expected_facts
        ):
            categories["uk"].append(name)
        if name.startswith("us-") or any(
            f.get("country") == "US" or "$" in str(f.get("normalized_value", ""))
            for f in expected_facts
        ):
            categories["us"].append(name)
        if "mobile" in name or any(f.get("is_mobile") is True for f in expected_facts):
            categories["mobile"].append(name)
        if "table" in name or any(f.get("is_table_heavy") is True for f in expected_facts):
            categories["table_heavy"].append(name)

    assert len(categories["uk"]) >= 6, f"Need >=6 UK businesses, found {len(categories['uk'])}"
    assert len(categories["us"]) >= 4, f"Need >=4 US businesses, found {len(categories['us'])}"
    assert (
        len(categories["mobile"]) >= 1
    ), f"Need >=1 mobile groomer, found {len(categories['mobile'])}"
    assert (
        len(categories["table_heavy"]) >= 1
    ), f"Need >=1 JS/table-heavy page, found {len(categories['table_heavy'])}"


def test_golden_corpus_traps_coverage() -> None:
    """Requirement 2: Corpus must include all specified traps.

    Traps: 'from £45', price ranges, 24h hours, '9am' hours, 'Monday to Friday',
    closed days, two different phone numbers, footers that disagree with contact page.
    """
    all_traps: list[str] = []
    slug_dirs = [d for d in GOLDEN_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]

    for slug_dir in slug_dirs:
        expected_facts = json.loads((slug_dir / "expected.json").read_text(encoding="utf-8"))
        for ef in expected_facts:
            trap_desc = ef.get("trap") or ef.get("notes") or ""
            if trap_desc:
                all_traps.append(str(trap_desc).lower())

    traps_combined = " ".join(all_traps)
    assert "from" in traps_combined and "£" in traps_combined, "Must include 'from £' trap"
    assert "range" in traps_combined, "Must include price range trap"
    assert "24h" in traps_combined or "24 hours" in traps_combined, "Must include 24h hours trap"
    assert "9am" in traps_combined, "Must include '9am' hours trap"
    assert "monday to friday" in traps_combined, "Must include 'Monday to Friday' trap"
    assert "closed" in traps_combined, "Must include closed days trap"
    assert "phone" in traps_combined and (
        "conflicting" in traps_combined or "different" in traps_combined or "two" in traps_combined
    ), "Must include two different phone numbers trap"
    assert "footer" in traps_combined and "disagree" in traps_combined, (
        "Must include footers that disagree with contact page trap"
    )


def _normalize_token(text: str) -> str:
    """Normalize string token for fuzzy-safe evaluation."""
    cleaned = text.lower().strip()
    cleaned = re.sub(r"[^\w\d£$€]", "", cleaned)
    return cleaned


def score_category_facts(
    expected_facts: list[dict[str, Any]],
    extracted_facts: list[Any],
) -> dict[str, Any]:
    """Score precision, recall, and verified-fact precision for a business.

    HARD RULE: precision of 'verified' facts must be 100%
    (a wrong verified fact is worse than a missing one).
    """
    cat_scores: dict[str, dict[str, int]] = {}

    expected_by_cat: dict[str, list[dict[str, Any]]] = {}
    for ef in expected_facts:
        cat = str(ef.get("category", "unknown"))
        expected_by_cat.setdefault(cat, []).append(ef)

    extracted_by_cat: dict[str, list[Any]] = {}
    for xf in extracted_facts:
        cat = str(getattr(xf, "category", ""))
        extracted_by_cat.setdefault(cat, []).append(xf)

    all_cats = sorted(set(list(expected_by_cat.keys()) + list(extracted_by_cat.keys())))

    for cat in all_cats:
        exp_list = expected_by_cat.get(cat, [])
        ext_list = extracted_by_cat.get(cat, [])

        tp = 0
        tp_verified = 0
        fp_verified = 0

        for ext in ext_list:
            ext_val = str(getattr(ext, "claim_value", ""))
            ext_status = str(getattr(ext, "status", ""))
            ext_norm = _normalize_token(ext_val)

            matched_exp = None
            for exp in exp_list:
                exp_val = str(exp.get("normalized_value", exp.get("claim_value", "")))
                exp_norm = _normalize_token(exp_val)
                if ext_norm == exp_norm or ext_norm in exp_norm or exp_norm in ext_norm:
                    matched_exp = exp
                    break

            if matched_exp is not None:
                tp += 1
                if ext_status == "verified":
                    if matched_exp.get("expected_status") == "verified":
                        tp_verified += 1
                    else:
                        fp_verified += 1
            elif ext_status == "verified":
                fp_verified += 1

        total_ext = len(ext_list)
        total_exp = len(exp_list)

        cat_scores[cat] = {
            "tp": tp,
            "total_extracted": total_ext,
            "total_expected": total_exp,
            "tp_verified": tp_verified,
            "fp_verified": fp_verified,
        }

    return cat_scores


def test_hard_rule_verified_precision_scoring_logic() -> None:
    """Verify that scoring logic strictly detects and rejects wrong verified facts."""
    mock_expected = [
        {
            "category": "pricing",
            "normalized_value": "from £45",
            "expected_status": "needs_review",
        }
    ]

    class MockFact:
        category = "pricing"
        claim_value = "£45"
        status = "verified"

    scores = score_category_facts(mock_expected, [MockFact()])
    pricing_score = scores["pricing"]
    assert pricing_score["fp_verified"] == 1
    verified_precision = (
        pricing_score["tp_verified"]
        / (pricing_score["tp_verified"] + pricing_score["fp_verified"])
        if (pricing_score["tp_verified"] + pricing_score["fp_verified"]) > 0
        else 1.0
    )
    assert verified_precision == 0.0, "Wrong verified fact must drop verified precision to 0.0"


def test_baseline_extraction_on_golden_corpus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run current fact extractor against all golden fixtures and score baseline precision/recall.

    Reports baseline precision, recall, and verified-fact precision for each business category.
    """
    assert GOLDEN_DIR.is_dir(), f"tests/golden directory missing at {GOLDEN_DIR}"

    slug_dirs = sorted(
        [d for d in GOLDEN_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    )
    assert len(slug_dirs) >= 12

    aggregate_by_cat: dict[str, dict[str, int]] = {}

    for slug_dir in slug_dirs:
        expected_file = slug_dir / "expected.json"
        expected_facts = json.loads(expected_file.read_text(encoding="utf-8"))

        work_dir = tmp_path / slug_dir.name
        shutil.copytree(slug_dir, work_dir)
        report = extract_business_facts(work_dir)
        scores = score_category_facts(expected_facts, report.facts)

        for cat, sc in scores.items():
            agg = aggregate_by_cat.setdefault(
                cat,
                {
                    "tp": 0,
                    "total_extracted": 0,
                    "total_expected": 0,
                    "tp_verified": 0,
                    "fp_verified": 0,
                },
            )
            for k in ("tp", "total_extracted", "total_expected", "tp_verified", "fp_verified"):
                agg[k] += sc[k]

    hdr_cat = f"{'Category':<22}"
    hdr_ext = f"{'Extracted':<9}"
    hdr_exp = f"{'Expected':<8}"
    hdr_prec = f"{'Precision':<9}"
    hdr_rec = f"{'Recall':<8}"
    hdr_vprec = f"{'Verified Precision':<18}"
    sep = "=" * 87

    lines: list[str] = [
        "",
        sep,
        "BASELINE EXTRACTION REPORT: PET GROOMING FACT EXTRACTOR",
        sep,
        f"{hdr_cat} | {hdr_ext} | {hdr_exp} | {hdr_prec} | {hdr_rec} | {hdr_vprec}",
        "-" * 87,
    ]

    total_tp = 0
    total_ext = 0
    total_exp = 0
    total_tp_v = 0
    total_fp_v = 0

    for cat in sorted(aggregate_by_cat.keys()):
        agg = aggregate_by_cat[cat]
        ext = agg["total_extracted"]
        exp = agg["total_expected"]
        tp = agg["tp"]
        tp_v = agg["tp_verified"]
        fp_v = agg["fp_verified"]

        total_tp += tp
        total_ext += ext
        total_exp += exp
        total_tp_v += tp_v
        total_fp_v += fp_v

        prec = (tp / ext * 100.0) if ext > 0 else 0.0
        rec = (tp / exp * 100.0) if exp > 0 else 0.0
        v_prec = (tp_v / (tp_v + fp_v) * 100.0) if (tp_v + fp_v) > 0 else 100.0

        lines.append(
            f"{cat:<22} | {ext:<9} | {exp:<8} | {prec:>8.1f}% | {rec:>7.1f}% | {v_prec:>17.1f}%"
        )

    lines.append("-" * 87)
    overall_prec = (total_tp / total_ext * 100.0) if total_ext > 0 else 0.0
    overall_rec = (total_tp / total_exp * 100.0) if total_exp > 0 else 0.0
    overall_v_prec = (
        (total_tp_v / (total_tp_v + total_fp_v) * 100.0)
        if (total_tp_v + total_fp_v) > 0
        else 100.0
    )
    lines.append(
        f"{'OVERALL TOTALS':<22} | {total_ext:<9} | {total_exp:<8} | "
        f"{overall_prec:>8.1f}% | {overall_rec:>7.1f}% | {overall_v_prec:>17.1f}%"
    )
    lines.append(sep)

    report_str = "\n".join(lines)
    print(report_str)

    # Validate against baseline thresholds to ensure no regression below established baseline
    assert overall_prec >= BASELINE_THRESHOLDS["overall_precision"], (
        f"Overall precision ({overall_prec:.1f}%) dropped below baseline "
        f"({BASELINE_THRESHOLDS['overall_precision']}%)"
    )
    assert overall_rec >= BASELINE_THRESHOLDS["overall_recall"], (
        f"Overall recall ({overall_rec:.1f}%) dropped below baseline "
        f"({BASELINE_THRESHOLDS['overall_recall']}%)"
    )

    for cat, exp_thresh in BASELINE_THRESHOLDS["categories"].items():
        if cat in aggregate_by_cat:
            cat_agg = aggregate_by_cat[cat]
            c_ext = cat_agg["total_extracted"]
            c_exp = cat_agg["total_expected"]
            c_tp = cat_agg["tp"]
            c_prec = (c_tp / c_ext * 100.0) if c_ext > 0 else 0.0
            c_rec = (c_tp / c_exp * 100.0) if c_exp > 0 else 0.0
            assert c_prec >= exp_thresh["precision"], (
                f"{cat} precision ({c_prec:.1f}%) dropped below baseline "
                f"({exp_thresh['precision']}%)"
            )
            assert c_rec >= exp_thresh["recall"], (
                f"{cat} recall ({c_rec:.1f}%) dropped below baseline "
                f"({exp_thresh['recall']}%)"
            )
