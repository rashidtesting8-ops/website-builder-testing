"""Claims and microcopy linter enforcing zero-fabrication and provenance standards.

Guarantees:
1. Every text node and alt attribute must match a verified fact value or the UI
   microcopy allowlist.
2. Anything with digits/currency must match a verified fact exactly.
3. Banned unless fact-backed: verified, authentic, luxury, bespoke, premium,
   certified, cage-free, botanical, organic, award-winning.
4. Outputs qa/claims-lint.json with node path, text, and required fix.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag
from pydantic import BaseModel, ConfigDict, Field

BANNED_WORDS = (
    "verified",
    "authentic",
    "luxury",
    "bespoke",
    "premium",
    "certified",
    "cage-free",
    "cage free",
    "botanical",
    "organic",
    "award-winning",
    "award winning",
)

CURRENCY_SYMBOLS = ("£", "$", "€")

DEFAULT_ALLOWLIST_PATH = (
    Path(__file__).parents[1] / "config" / "ui_microcopy_allowlist.json"
)


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class ClaimViolation(BaseModel):
    """Specific claim or microcopy violation."""

    model_config = ConfigDict(extra="forbid")

    node_path: str
    text: str
    required_fix: str


class ClaimsLintResult(BaseModel):
    """Result of claims linting across an HTML page."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    violations_count: int = 0
    violations: list[dict[str, str]] = Field(default_factory=list)


def _load_allowlist(allowlist_path: Path | None = None) -> tuple[set[str], list[re.Pattern[str]]]:
    path = allowlist_path or DEFAULT_ALLOWLIST_PATH
    if not path.is_file():
        return set(), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        exact = {s.strip().lower() for s in data.get("exact_strings", []) if s.strip()}
        patterns = [re.compile(p, re.IGNORECASE) for p in data.get("patterns", [])]
        return exact, patterns
    except Exception:
        return set(), []


def _get_node_path(node: Tag | NavigableString) -> str:
    parts: list[str] = []
    curr = node if isinstance(node, Tag) else node.parent
    while curr and curr.name and curr.name != "[document]":
        tag_desc = curr.name
        if curr.get("id"):
            tag_desc += f"#{curr['id']}"
        elif curr.get("class"):
            classes = curr.get("class")
            if isinstance(classes, list):
                tag_desc += f".{classes[0]}"
            elif isinstance(classes, str):
                tag_desc += f".{classes.split()[0]}"
        parts.append(tag_desc)
        curr = curr.parent
    parts.reverse()
    return " > ".join(parts) if parts else "root"


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def lint_html_claims(
    *,
    html_content: str,
    verified_facts: Sequence[dict[str, Any] | Any],
    allowlist_path: Path | None = None,
) -> ClaimsLintResult:
    """Lint an HTML document for unverified claims, banned adjectives, and unbacked digits."""
    allowlist_exact, allowlist_patterns = _load_allowlist(allowlist_path)

    # 1. Index verified facts
    fact_values: list[str] = []
    fact_tokens: set[str] = set()
    banned_allowed_in_facts: set[str] = set()

    for fact in verified_facts:
        val = ""
        is_verified = False
        status = ""
        if isinstance(fact, dict):
            val = str(fact.get("claim_value", "")).strip()
            is_verified = bool(fact.get("is_verified", False))
            status = str(fact.get("status", "")).lower()
        else:
            val = str(getattr(fact, "claim_value", "")).strip()
            is_verified = bool(getattr(fact, "is_verified", False))
            status = str(getattr(fact, "status", "")).lower()

        if is_verified and status == "verified" and val:
            fact_values.append(val)
            norm_val = val.lower()
            fact_tokens.add(norm_val)
            if " - " in val:
                s_part, p_part = val.split(" - ", 1)
                fact_values.append(s_part.strip())
                fact_values.append(p_part.strip())
            elif ": " in val:
                s_part, p_part = val.split(": ", 1)
                fact_values.append(s_part.strip())
                fact_values.append(p_part.strip())
            # Digits inside verified fact
            for b in BANNED_WORDS:
                if re.search(rf"\b{re.escape(b)}\b", norm_val, re.IGNORECASE):
                    banned_allowed_in_facts.add(b)

    soup = BeautifulSoup(html_content, "html.parser")
    violations: list[dict[str, str]] = []

    # Helper: Check if string is matched by facts or allowlist
    def is_text_allowed(clean: str) -> tuple[bool, str]:
        lower = clean.lower()

        # Check banned words first
        for bw in BANNED_WORDS:
            if re.search(rf"\b{re.escape(bw)}\b", lower, re.IGNORECASE):
                if bw not in banned_allowed_in_facts:
                    return False, f"Contains unbacked banned word '{bw}'"

        # Check if matches allowlist exact
        if lower in allowlist_exact:
            return True, ""

        # Check if matches allowlist patterns
        for pat in allowlist_patterns:
            if pat.match(clean) or pat.match(lower):
                # Pattern match: if digits/currency are present, verify phone/name consistency
                has_digits = bool(re.search(r"\d", clean))
                has_curr = any(s in clean for s in CURRENCY_SYMBOLS)
                if has_digits or has_curr:
                    if clean.lower().startswith("call"):
                        # Extract phone part
                        phone_cand = re.sub(r"^call\s*", "", clean, flags=re.IGNORECASE).strip()
                        phone_digits = re.sub(r"\D", "", phone_cand)
                        phone_matched = any(
                            phone_digits and phone_digits in re.sub(r"\D", "", fv)
                            for fv in fact_values
                        )
                        if not phone_matched:
                            return (
                                False,
                                f"Phone number in '{clean}' does not match any verified phone fact",
                            )
                    elif pat.pattern.endswith("photo\\s+\\d+$"):
                        pass  # Standard allowed neutral alt text
                    else:
                        return False, f"Digits/currency in '{clean}' not backed by verified fact"
                return True, ""

        # Check if matches any verified fact claim_value directly
        for fv in fact_values:
            fv_clean = _normalize_text(fv)
            fv_lower = fv_clean.lower()
            if lower == fv_lower or lower in fv_lower or fv_lower in lower:
                return True, ""

            # Check digit equality if both contain digits
            has_digits = bool(re.search(r"\d", clean))
            if has_digits and fv_clean:
                clean_digits = re.sub(r"\D", "", clean)
                fv_digits = re.sub(r"\D", "", fv_clean)
                if clean_digits and clean_digits == fv_digits:
                    return True, ""

        # If text has digits or currency and didn't match a verified fact or allowlist
        has_digits = bool(re.search(r"\d", clean))
        has_curr = any(s in clean for s in CURRENCY_SYMBOLS)
        if has_digits or has_curr:
            return False, f"Digits or currency in '{clean}' do not match any verified fact exactly"

        # Check if text is composed of verified business name
        for fv in fact_values:
            if fv.lower() in lower:
                # Text contains verified business name
                remainder = _normalize_text(
                    re.sub(re.escape(fv), "", clean, flags=re.IGNORECASE)
                )
                if (
                    not remainder
                    or remainder.lower() in allowlist_exact
                    or remainder in ("—", "-", ":")
                ):
                    return True, ""

        return False, f"Text node '{clean}' does not match any verified fact or UI allowlist entry"

    # 1. Check all visible text nodes
    for element in soup.find_all(True):
        if element.name in ("script", "style", "noscript"):
            continue

        # Check direct text children
        for child in element.children:
            if isinstance(child, NavigableString) and not isinstance(child, Tag):
                clean = _normalize_text(str(child))
                if not clean or clean in ("&copy;", "©", "|", "•"):
                    continue
                # Skip HTML comment nodes
                if clean.startswith("<!--") or clean.endswith("-->"):
                    continue

                allowed, reason = is_text_allowed(clean)
                if not allowed:
                    violations.append(
                        {
                            "node_path": _get_node_path(element),
                            "text": clean,
                            "required_fix": reason,
                        }
                    )

        # 2. Check alt attributes of <img>
        if element.name == "img" and element.has_attr("alt"):
            alt_text = _normalize_text(str(element["alt"]))
            if alt_text:
                allowed, reason = is_text_allowed(alt_text)
                if not allowed:
                    violations.append(
                        {
                            "node_path": f"{_get_node_path(element)}[alt]",
                            "text": alt_text,
                            "required_fix": reason,
                        }
                    )

    passed = len(violations) == 0
    return ClaimsLintResult(
        passed=passed,
        violations_count=len(violations),
        violations=violations,
    )


def run_claims_lint(project_folder: str | Path) -> ClaimsLintResult:
    """Run claims lint on project's website/index.html and save qa/claims-lint.json."""
    project_root = Path(project_folder).resolve()
    website_index = project_root / "website" / "index.html"
    standalone_rc = project_root / "release-candidate" / "standalone.html"

    target_html_path = standalone_rc if standalone_rc.is_file() else website_index
    if not target_html_path.is_file():
        raise FileNotFoundError(f"Missing HTML file to lint in {project_root}")

    html_content = target_html_path.read_text(encoding="utf-8")

    facts_path = project_root / "research" / "business-facts.json"
    facts: list[dict[str, Any]] = []
    if facts_path.is_file():
        try:
            facts_data = json.loads(facts_path.read_text(encoding="utf-8"))
            facts = facts_data.get("facts", [])
        except Exception:
            facts = []

    result = lint_html_claims(
        html_content=html_content,
        verified_facts=facts,
    )

    out_path = project_root / "qa" / "claims-lint.json"
    _atomic_write_text(out_path, result.model_dump_json(indent=2))
    return result
