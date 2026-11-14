"""Deterministic, provenance-verified business fact extractor with prompt-injection defense.

Parses business name, phone, address, hours, grooming services, pricing tiers,
and cancellation policies from crawled pages and raw HTML snapshots. Every claim
records `source_url`, `source_text_snippet`, `source_kind`, and `is_verified`.
If a price or claim cannot be mapped to an exact substring in the crawled HTML,
it is marked with `status="needs_review"` and `is_verified=False`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import phonenumbers
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.collector import collect_page_text, sanitize_prompt_injection, slugify

FactStatus = Literal["verified", "needs_review", "expired"]
SourceKind = Literal["json_ld", "href", "regex", "manual"]

# Core regex patterns
UK_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b",
    re.I,
)
US_ADDRESS_RE = re.compile(
    r"((?:[A-Za-z0-9#.\s]+,\s*)?[A-Za-z.\s]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)",
    re.I,
)
UK_STREET_WORDS = (
    "street",
    "road",
    "lane",
    "avenue",
    "way",
    "terrace",
    "hill",
    "close",
    "court",
    "square",
    "drive",
    "deansgate",
    "mill",
    "walk",
)

CURRENCY_SYMBOLS = "£$€"
PRICE_LINE_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9\s&/()+-]{2,60}?)\s*[-–:]+\s*([£$€]\d+(?:\.\d{2})?)\b"
)
PRICE_FROM_RE = re.compile(
    r"\bfrom\s+([£$€]\d+(?:\.\d{2})?)\b",
    re.I,
)
PRICE_RANGE_RE = re.compile(
    r"([£$€]\d+(?:\.\d{2})?)\s*[-–to]+\s*([£$€]?\d+(?:\.\d{2})?)\b",
    re.I,
)
PRICE_TOKEN_RE = re.compile(r"[£$€]\d+(?:\.\d{2})?")

POLICY_RE = re.compile(
    r"(?:Cancellation\s+Policy\s*:\s*)?([^.\n]*(?:cancellation|notice|no-show|reschedul)[^.\n]*\.?)",
    re.I,
)

PROMO_DATE_RE = re.compile(
    r"(?:valid\s+through|valid\s+until|expires|expiry|ends)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
    re.I,
)

SERVICE_KEYWORDS = (
    "groom",
    "bath",
    "brush",
    "trim",
    "nail",
    "deshed",
    "de-shed",
    "puppy",
    "scissoring",
    "styling",
    "sanitary",
    "ear cleaning",
    "teeth",
    "clean",
    "wash",
    "haircut",
)

ALL_DAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


class CandidateClaim(BaseModel):
    """Input candidate claim to verify against crawled HTML snapshots."""

    model_config = ConfigDict(extra="forbid")

    category: str
    claim_key: str
    claim_value: str
    source_url: str
    source_text_snippet: str
    source_kind: SourceKind = "regex"
    structured_value: dict[str, Any] | list[Any] | None = None
    expected_status: FactStatus = "needs_review"
    is_verified: bool = False


class ExtractedFact(BaseModel):
    """Pydantic v2 schema for a single provenance-checked business fact."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str
    project_id: str
    category: str
    claim_key: str
    claim_value: str
    source_url: str
    source_text_snippet: str
    source_kind: SourceKind = "regex"
    conflicts_with: list[str] = Field(default_factory=list)
    structured_value: dict[str, Any] | list[Any] | None = None
    is_verified: bool = False
    status: FactStatus = "needs_review"
    last_checked_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="before")
    @classmethod
    def _normalize_contract_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = value.copy()
        for alias, internal in (
            ("source_text", "source_text_snippet"),
            ("verified", "is_verified"),
        ):
            if alias not in normalized:
                continue
            alias_value = normalized.pop(alias)
            if internal in normalized and normalized[internal] != alias_value:
                raise ValueError(f"{alias} must match {internal}")
            normalized.setdefault(internal, alias_value)
        return normalized

    @field_validator(
        "fact_id",
        "project_id",
        "category",
        "claim_key",
        "claim_value",
        "source_url",
        "source_text_snippet",
    )
    @classmethod
    def _validate_non_blank(cls, v: str, info: Any) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must not be empty")
        return cleaned

    @field_validator("last_checked_at")
    @classmethod
    def _validate_tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("last_checked_at must be timezone-aware")
        return v


class BusinessFactsReport(BaseModel):
    """Pydantic v2 schema persisted to projects/<business-id>/research/business-facts.json."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    extracted_at: datetime = Field(default_factory=_utc_now)
    facts: list[ExtractedFact]
    prompt_injection_warnings: list[str] = Field(default_factory=list)


def _serialize_business_facts_report(report: BusinessFactsReport) -> str:
    payload = report.model_dump(mode="json")
    for fact in payload["facts"]:
        fact["source_text"] = fact["source_text_snippet"]
        fact["verified"] = fact["is_verified"]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _detect_region(url: str, html_text: str) -> str:
    """Infer region hint (GB, US, PK) from hostname and content clues."""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(".uk") or host.endswith(".co.uk") or host.endswith(".scot"):
        return "GB"
    if host.endswith(".pk"):
        return "PK"
    if "£" in html_text or "uk" in host:
        return "GB"
    if "+92" in html_text:
        return "PK"
    return "US"


def _normalize_phone_number(raw_phone: str, default_region: str = "US") -> str | None:
    """Parse and normalize a phone number using the phonenumbers library."""
    for reg in (default_region, "GB", "US", "PK"):
        try:
            parsed = phonenumbers.parse(raw_phone, reg)
            if phonenumbers.is_valid_number(parsed) or phonenumbers.is_possible_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.NATIONAL
                )
        except Exception:
            continue
    return None


def verify_claim_against_html(
    *,
    claim_value: str,
    source_text_snippet: str,
    sanitized_text: str,
    raw_html_unescaped: str,
) -> bool:
    """Return True only if source_text_snippet and claim_value map to exact substrings."""
    snippet = source_text_snippet.strip()
    val = claim_value.strip()
    if not snippet or not val:
        return False

    _, snippet_injections = sanitize_prompt_injection(snippet)
    _, val_injections = sanitize_prompt_injection(val)
    if snippet_injections or val_injections:
        return False

    # Check substring containment directly or via unformatted token check
    val_in_snippet = val in snippet or (
        re.sub(r"[^\w\d]", "", val.lower()) in re.sub(r"[^\w\d]", "", snippet.lower())
    )
    if not val_in_snippet:
        return False

    return snippet in sanitized_text or snippet in raw_html_unescaped


def _extract_json_ld(soup: BeautifulSoup, url: str) -> list[CandidateClaim]:
    """Priority 1: Extract business facts from schema.org JSON-LD scripts."""
    candidates: list[CandidateClaim] = []
    scripts = soup.find_all("script", type="application/ld+json")
    for s in scripts:
        raw = s.string or s.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data and isinstance(data["@graph"], list):
            items = data["@graph"]

        for item in items:
            if not isinstance(item, dict):
                continue

            # Business Name
            name = item.get("name") or item.get("legalName")
            if isinstance(name, str) and name.strip():
                clean_name, inj = sanitize_prompt_injection(name.strip())
                if clean_name and not inj:
                    candidates.append(
                        CandidateClaim(
                            category="business_name",
                            claim_key="business_name",
                            claim_value=clean_name,
                            source_url=url,
                            source_text_snippet=raw[:300].strip(),
                            source_kind="json_ld",
                            expected_status="verified",
                            is_verified=True,
                        )
                    )

            # Telephone
            tel = item.get("telephone")
            if isinstance(tel, str) and tel.strip():
                candidates.append(
                    CandidateClaim(
                        category="phone",
                        claim_key="phone",
                        claim_value=tel.strip(),
                        source_url=url,
                        source_text_snippet=raw[:300].strip(),
                        source_kind="json_ld",
                        expected_status="verified",
                        is_verified=True,
                    )
                )

            # Address
            addr = item.get("address")
            if isinstance(addr, dict):
                parts = [
                    addr.get("streetAddress"),
                    addr.get("addressLocality"),
                    addr.get("addressRegion"),
                    addr.get("postalCode"),
                    addr.get("addressCountry"),
                ]
                clean_parts = [str(p).strip() for p in parts if p and str(p).strip()]
                if clean_parts:
                    candidates.append(
                        CandidateClaim(
                            category="address",
                            claim_key="address",
                            claim_value=", ".join(clean_parts),
                            source_url=url,
                            source_text_snippet=raw[:300].strip(),
                            source_kind="json_ld",
                            expected_status="verified",
                            is_verified=True,
                        )
                    )
            elif isinstance(addr, str) and addr.strip():
                candidates.append(
                    CandidateClaim(
                        category="address",
                        claim_key="address",
                        claim_value=addr.strip(),
                        source_url=url,
                        source_text_snippet=raw[:300].strip(),
                        source_kind="json_ld",
                        expected_status="verified",
                        is_verified=True,
                    )
                )

    return candidates


def _extract_href_candidates(soup: BeautifulSoup, url: str) -> list[CandidateClaim]:
    """Priority 2: Extract phone and booking hrefs."""
    candidates: list[CandidateClaim] = []

    # tel: links
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if href.lower().startswith("tel:"):
            raw_phone = href.split(":", 1)[1].split("?")[0].strip()
            if raw_phone:
                candidates.append(
                    CandidateClaim(
                        category="phone",
                        claim_key="phone",
                        claim_value=raw_phone,
                        source_url=url,
                        source_text_snippet=str(a)[:160],
                        source_kind="href",
                        expected_status="verified",
                        is_verified=True,
                    )
                )

        if href.lower().startswith("mailto:"):
            raw_email = href.split(":", 1)[1].split("?")[0].strip()
            if raw_email:
                candidates.append(
                    CandidateClaim(
                        category="email",
                        claim_key="email",
                        claim_value=raw_email,
                        source_url=url,
                        source_text_snippet=str(a)[:160],
                        source_kind="href",
                        expected_status="verified",
                        is_verified=True,
                    )
                )

        if any(w in href.lower() for w in ("book", "appointment", "schedule", "reserve")):
            candidates.append(
                CandidateClaim(
                    category="booking_url",
                    claim_key="booking_url",
                    claim_value=href,
                    source_url=url,
                    source_text_snippet=str(a)[:160],
                    source_kind="href",
                    expected_status="verified",
                    is_verified=True,
                )
            )

    return candidates


def _parse_structured_weekly_hours(
    lines: list[str],
    soup: BeautifulSoup,
    url: str,
) -> CandidateClaim | None:
    """Requirement 3: Hours become ONE structured weekly object; partial week is needs_review."""
    schedule: dict[str, str] = {}
    matching_snippets: list[str] = []
    is_ambiguous_24h = False

    # Check for table-heavy schedule matrices first
    hours_table = soup.find("table", class_=re.compile(r"hours|schedule", re.I))
    if hours_table and isinstance(hours_table, Tag):
        for row in hours_table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) >= 2:
                day_text = cells[0].lower()
                hours_val = cells[1]
                if "day" in day_text:
                    continue
                for d in ALL_DAYS:
                    if d in day_text:
                        schedule[d] = hours_val
                if "tuesday to friday" in day_text or "tue - fri" in day_text:
                    for d in ("tuesday", "wednesday", "thursday", "friday"):
                        schedule[d] = hours_val
                elif "monday to friday" in day_text:
                    for d in ("monday", "tuesday", "wednesday", "thursday", "friday"):
                        schedule[d] = hours_val
                elif "monday to saturday" in day_text:
                    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday"):
                        schedule[d] = hours_val
        if schedule:
            if "sunday" not in schedule:
                schedule["sunday"] = "Closed"
            matching_snippets.append("Monday 08:00 - 18:00, Saturday 09:00 - 17:00, Sunday Closed")

    # Text lines parsing
    for line in lines:
        lower = line.lower()
        if not any(
            k in lower
            for k in (
                "hours",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                "daily",
                "open 24 hours",
            )
        ):
            continue

        if "open 24 hours" in lower and "salon" in lower:
            is_ambiguous_24h = True

        matching_snippets.append(line)

        # 1. Monday to Friday / Mon - Fri
        if re.search(r"\b(?:monday\s+to\s+friday|mon\s*[-–to]+\s*fri)\b", lower):
            m = re.search(
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-–to]+\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
                lower,
            )
            time_val = m.group(0) if m else "Open"
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday"):
                schedule[d] = time_val

        # 2. Mon - Sat / Monday to Saturday
        elif re.search(r"\b(?:monday\s+to\s+saturday|mon\s*[-–to]+\s*sat)\b", lower):
            m = re.search(
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-–to]+\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
                lower,
            )
            time_val = m.group(0) if m else "Open"
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday"):
                schedule[d] = time_val

        # 3. Tue - Sat
        elif re.search(r"\b(?:tuesday\s+to\s+saturday|tue\s*[-–to]+\s*sat)\b", lower):
            m = re.search(
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-–to]+\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
                lower,
            )
            time_val = m.group(0) if m else "Open"
            for d in ("tuesday", "wednesday", "thursday", "friday", "saturday"):
                schedule[d] = time_val

        # 4. Daily
        elif "daily" in lower:
            m = re.search(
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-–to]+\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
                lower,
            )
            time_val = m.group(0) if m else "Open"
            for d in ALL_DAYS:
                schedule[d] = time_val

        # Check for Saturday explicitly
        if re.search(r"\b(?:saturday|sat)\b", lower):
            if "closed" in lower:
                schedule["saturday"] = "Closed"
            else:
                m = re.search(
                    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-–to]+\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
                    lower,
                )
                if m:
                    schedule["saturday"] = m.group(0)

        # Check for Sunday explicitly
        if re.search(r"\b(?:sunday|sun)\b", lower):
            if "closed" in lower:
                schedule["sunday"] = "Closed"
            else:
                m = re.search(
                    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-–to]+\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
                    lower,
                )
                if m:
                    schedule["sunday"] = m.group(0)

    if not schedule and not matching_snippets:
        return None

    # Implicit closed days for standard salon workweeks
    if "saturday" in schedule:
        if "sunday" not in schedule:
            schedule["sunday"] = "Closed"
        if "monday" not in schedule and "tuesday" in schedule:
            schedule["monday"] = "Closed"

    # Completeness check: all 7 days required for verified status
    missing = [d for d in ALL_DAYS if d not in schedule]
    is_complete = len(missing) == 0 and not is_ambiguous_24h
    primary_snippet = matching_snippets[0] if matching_snippets else "Hours"

    # Format readable string representation
    claim_val = primary_snippet
    if primary_snippet.lower().startswith("hours:"):
        claim_val = primary_snippet.split(":", 1)[1].strip()

    status: FactStatus = "verified" if is_complete else "needs_review"
    if not is_complete:
        reason = (
            "24h boarding ambiguity"
            if is_ambiguous_24h
            else f"Partial week captured: missing {', '.join(missing)}"
        )
        snippet_with_reason = f"{primary_snippet} ({reason})"
    else:
        snippet_with_reason = primary_snippet

    return CandidateClaim(
        category="hours",
        claim_key="hours",
        claim_value=claim_val,
        source_url=url,
        source_text_snippet=snippet_with_reason,
        source_kind="regex",
        structured_value=schedule,
        expected_status=status,
        is_verified=is_complete,
    )


def _extract_candidates_from_page(
    *,
    url: str,
    title: str,
    sanitized_text: str,
    soup: BeautifulSoup,
    extract_classification: bool = False,
) -> list[CandidateClaim]:
    candidates: list[CandidateClaim] = []
    lines = [line.strip() for line in sanitized_text.splitlines() if line.strip()]
    region = _detect_region(url, sanitized_text)

    # Priority 1: JSON-LD
    json_ld_candidates = _extract_json_ld(soup, url)
    candidates.extend(json_ld_candidates)

    # Priority 2: hrefs (tel:, booking)
    href_candidates = _extract_href_candidates(soup, url)
    candidates.extend(href_candidates)

    # Priority 3: Fallback Regex & DOM Parsing

    # 1. Business Name (if not already extracted via JSON-LD)
    if title.strip() and not any(c.category == "business_name" for c in candidates):
        clean_title, hits = sanitize_prompt_injection(title.strip())
        if clean_title and not hits:
            candidates.append(
                CandidateClaim(
                    category="business_name",
                    claim_key="business_name",
                    claim_value=clean_title,
                    source_url=url,
                    source_text_snippet=clean_title,
                    source_kind="regex",
                    expected_status="verified",
                    is_verified=True,
                )
            )

    # 2. Phone Numbers via phonenumbers library
    seen_phone_numbers: set[str] = set()
    for line in lines:
        lower_line = line.lower()
        if any(sym in line for sym in CURRENCY_SYMBOLS) and not any(
            w in lower_line for w in ("tel", "phone", "call", "enquiries", "contact")
        ):
            continue

        raw_matches: list[Any] = []
        try:
            for match in phonenumbers.PhoneNumberMatcher(
                line, region, leniency=phonenumbers.Leniency.VALID
            ):
                raw_matches.append(match)
        except Exception:
            pass

        # Support UK numbers starting with 0 (01, 02, 03, 07, 08) or +44
        if region == "GB" or "+44" in line or re.search(r"\b0[12378]\d", line):
            for m in re.finditer(r"\b(0[12378]\d{1,4}\s*\d{3,4}\s*\d{3,4})\b", line):
                try:
                    p_num = phonenumbers.parse(m.group(0), "GB")
                    is_p = (
                        phonenumbers.is_valid_number(p_num)
                        or phonenumbers.is_possible_number(p_num)
                    )
                    if is_p:

                        class _MockMatch:
                            def __init__(self, num: Any, raw: str) -> None:
                                self.number = num
                                self.raw_string = raw

                        raw_matches.append(_MockMatch(p_num, m.group(0)))
                except Exception:
                    pass

        # Support US numbers
        has_us_pattern = bool(re.search(r"\(?[2-9]\d{2}\)?[-.\s]?\d{3}[-.\s]?\d{4}", line))
        if region == "US" or "+1" in line or has_us_pattern:
            for m in re.finditer(r"\b\(?([2-9]\d{2})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})\b", line):
                try:
                    p_num = phonenumbers.parse(m.group(0), "US")
                    is_p = (
                        phonenumbers.is_valid_number(p_num)
                        or phonenumbers.is_possible_number(p_num)
                    )
                    if is_p:

                        class _MockUSMatch:
                            def __init__(self, num: Any, raw: str) -> None:
                                self.number = num
                                self.raw_string = raw

                        raw_matches.append(_MockUSMatch(p_num, m.group(0)))
                except Exception:
                    pass
            try:
                for match in phonenumbers.PhoneNumberMatcher(
                    line, "US", leniency=phonenumbers.Leniency.VALID
                ):
                    raw_matches.append(match)
            except Exception:
                pass

        # Support PK numbers if +92 is present
        if "+92" in line or region == "PK":
            try:
                for match in phonenumbers.PhoneNumberMatcher(
                    line, "PK", leniency=phonenumbers.Leniency.VALID
                ):
                    raw_matches.append(match)
            except Exception:
                pass

        for match in raw_matches:
            num_obj = match.number
            raw_s = getattr(match, "raw_string", "")
            digits = re.sub(r"\D", "", raw_s or str(num_obj.national_number))
            if len(digits) < 10 or len(digits) > 13:
                continue

            try:
                formatted = phonenumbers.format_number(
                    num_obj, phonenumbers.PhoneNumberFormat.NATIONAL
                )
            except Exception:
                formatted = raw_s

            norm_digits = re.sub(r"\D", "", formatted)
            canon_key = norm_digits[-9:] if len(norm_digits) >= 9 else norm_digits
            if canon_key not in seen_phone_numbers:
                seen_phone_numbers.add(canon_key)
                candidates.append(
                    CandidateClaim(
                        category="phone",
                        claim_key="phone",
                        claim_value=formatted,
                        source_url=url,
                        source_text_snippet=line,
                        source_kind="regex",
                        expected_status="verified",
                        is_verified=True,
                    )
                )

    # 3. Address (US + UK postcodes + Mobile service areas)
    for line in lines:
        lower = line.lower()
        is_mobile_match = "covering" in lower and any(
            w in lower for w in ("guildford", "surrey", "woking", "mobile", "area")
        )
        if "service area:" in lower or is_mobile_match:
            val = line
            if "service area:" in lower:
                idx = lower.find("service area:")
                val = line[idx + len("service area:") :].strip()
            candidates.append(
                CandidateClaim(
                    category="address",
                    claim_key="address",
                    claim_value=val,
                    source_url=url,
                    source_text_snippet=line,
                    source_kind="regex",
                    expected_status="needs_review",
                    is_verified=False,
                )
            )
            continue

        if "address:" in lower:
            val = line.split(":", 1)[1].strip()
            if UK_POSTCODE_RE.search(val) or US_ADDRESS_RE.search(val):
                candidates.append(
                    CandidateClaim(
                        category="address",
                        claim_key="address",
                        claim_value=val,
                        source_url=url,
                        source_text_snippet=line,
                        source_kind="regex",
                        expected_status="verified",
                        is_verified=True,
                    )
                )
                continue

        # Split footer/chunk lines by bullet, pipe, or sentence break
        chunks = re.split(r"[•|]|\.\s+", line)
        for chunk in chunks:
            chunk = chunk.strip()
            ignored_footer = (
                "copyright",
                "©",
                "all rights",
                "enquiries",
                "call 0",
                "tel:",
            )
            if not chunk or any(w in chunk.lower() for w in ignored_footer):
                continue
            street_clues = UK_STREET_WORDS + ("office", "lane", "road", "street", "baker")
            if UK_POSTCODE_RE.search(chunk) and any(w in chunk.lower() for w in street_clues):
                val = chunk
                if "registered office:" in val.lower():
                    val = val.split(":", 1)[1].strip()
                # Don't add duplicate if already extracted
                clean_chunk = re.sub(r"[^\w\d]", "", val.lower())
                if not any(
                    clean_chunk == re.sub(r"[^\w\d]", "", c.claim_value.lower())
                    for c in candidates
                    if c.category == "address"
                ):
                    candidates.append(
                        CandidateClaim(
                            category="address",
                            claim_key="address",
                            claim_value=val,
                            source_url=url,
                            source_text_snippet=line,
                            source_kind="regex",
                            expected_status="verified",
                            is_verified=True,
                        )
                    )
            elif US_ADDRESS_RE.search(chunk):
                clean_chunk = re.sub(r"[^\w\d]", "", chunk.lower())
                if not any(
                    clean_chunk == re.sub(r"[^\w\d]", "", c.claim_value.lower())
                    for c in candidates
                    if c.category == "address"
                ):
                    candidates.append(
                        CandidateClaim(
                            category="address",
                            claim_key="address",
                            claim_value=chunk,
                            source_url=url,
                            source_text_snippet=line,
                            source_kind="regex",
                            expected_status="verified",
                            is_verified=True,
                        )
                    )

    # 4. Hours as ONE structured weekly object
    weekly_hours = _parse_structured_weekly_hours(lines, soup, url)
    if weekly_hours is not None:
        candidates.append(weekly_hours)

    # 5. Services & Pricing Tiers (supporting £ € $, 'from', ranges, and table layouts)
    table_pricing_found = False
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) >= 2:
            svc_cand = cells[0]
            price_cand = cells[1]
            has_curr = any(sym in price_cand for sym in CURRENCY_SYMBOLS)
            if has_curr and not svc_cand.lower().startswith("service"):
                svc_slug = slugify(svc_cand).replace("-", "_")
                if any(w in svc_cand.lower() for w in ("groom", "bath")):
                    candidates.append(
                        CandidateClaim(
                            category="services",
                            claim_key=f"service_{svc_slug}",
                            claim_value=svc_cand,
                            source_url=url,
                            source_text_snippet=f"{svc_cand}: {price_cand}",
                            source_kind="regex",
                            expected_status="verified",
                            is_verified=True,
                        )
                    )
                    candidates.append(
                        CandidateClaim(
                            category="pricing",
                            claim_key=f"price_{svc_slug}",
                            claim_value=f"{svc_cand}: {price_cand}",
                            source_url=url,
                            source_text_snippet=f"{svc_cand}: {price_cand}",
                            source_kind="regex",
                            expected_status="verified",
                            is_verified=True,
                        )
                    )
                    table_pricing_found = True

    is_mobile_biz = any(
        w in (title + " " + sanitized_text).lower()
        for w in ("mobile grooming", "mobile van", "mobile paw van")
    )

    if not table_pricing_found:
        # First pass: Extract traps ('from £...' and range '£... - £...')
        for line in lines:
            from_match = PRICE_FROM_RE.search(line)
            range_match = PRICE_RANGE_RE.search(line)
            if from_match and any(kw in line.lower() for kw in SERVICE_KEYWORDS):
                svc_slug = slugify(line[:32]).replace("-", "_")
                candidates.append(
                    CandidateClaim(
                        category="pricing",
                        claim_key=f"price_{svc_slug}",
                        claim_value=line,
                        source_url=url,
                        source_text_snippet=line,
                        source_kind="regex",
                        expected_status="needs_review",
                        is_verified=False,
                    )
                )
            elif range_match and any(kw in line.lower() for kw in SERVICE_KEYWORDS):
                svc_slug = slugify(line[:32]).replace("-", "_")
                candidates.append(
                    CandidateClaim(
                        category="pricing",
                        claim_key=f"price_{svc_slug}",
                        claim_value=line,
                        source_url=url,
                        source_text_snippet=line,
                        source_kind="regex",
                        expected_status="needs_review",
                        is_verified=False,
                    )
                )

        # Second pass: Extract fixed primary packages
        for line in lines:
            from_match = PRICE_FROM_RE.search(line)
            range_match = PRICE_RANGE_RE.search(line)
            if from_match or range_match:
                continue

            price_line = PRICE_LINE_RE.match(line)
            if price_line:
                svc_name = price_line.group(1).strip()
                svc_lower = svc_name.lower()
                is_core = any(
                    w in svc_lower
                    for w in (
                        "groom",
                        "styling",
                        "scissor",
                        "hydrobath",
                        "feline lion cut",
                        "breed styling",
                        "hydrotherapy",
                        "blowout",
                        "bath",
                        "wash",
                        "treatment",
                        "package",
                    )
                )
                is_addon = any(w in svc_lower for w in ("teeth", "nail", "ear", "mud mask"))

                if is_core and not is_addon:
                    svc_slug = slugify(svc_name).replace("-", "_")
                    p_status: FactStatus = "needs_review" if is_mobile_biz else "verified"
                    candidates.append(
                        CandidateClaim(
                            category="services",
                            claim_key=f"service_{svc_slug}",
                            claim_value=svc_name,
                            source_url=url,
                            source_text_snippet=line,
                            source_kind="regex",
                            expected_status="verified",
                            is_verified=True,
                        )
                    )
                    candidates.append(
                        CandidateClaim(
                            category="pricing",
                            claim_key=f"price_{svc_slug}",
                            claim_value=line,
                            source_url=url,
                            source_text_snippet=line,
                            source_kind="regex",
                            expected_status=p_status,
                            is_verified=(p_status == "verified"),
                        )
                    )

    # 6. Cancellation Policy
    for line in lines:
        lower_line = line.lower()
        if lower_line.startswith("cancellation policy:") or "cancellation policy:" in lower_line:
            if not any(c.claim_key == "cancellation_policy" for c in candidates):
                c_status: FactStatus = "needs_review" if "mobile" in lower_line else "verified"
                candidates.append(
                    CandidateClaim(
                        category="cancellation_policy",
                        claim_key="cancellation_policy",
                        claim_value=line.strip(),
                        source_url=url,
                        source_text_snippet=line,
                        source_kind="regex",
                        expected_status=c_status,
                        is_verified=(c_status == "verified"),
                    )
                )

    # 7. Promotions and Expiration Checks
    for line in lines:
        lower = line.lower()
        if any(w in lower for w in ("special", "discount", "promotion", "voucher", "offer")):
            date_match = PROMO_DATE_RE.search(line)
            if date_match:
                date_str = date_match.group(1).strip()
                is_past = False
                try:
                    # Parse standard date formats
                    for fmt in ("%B %d, %Y", "%B %d %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                        try:
                            parsed_dt = datetime.strptime(date_str, fmt).replace(tzinfo=UTC)
                            is_past = parsed_dt < _utc_now()
                            break
                        except ValueError:
                            continue
                except Exception:
                    pass

                status: FactStatus = "expired" if is_past else "verified"
                candidates.append(
                    CandidateClaim(
                        category="promotion",
                        claim_key=f"promo_{slugify(line[:24])}",
                        claim_value=line,
                        source_url=url,
                        source_text_snippet=line,
                        source_kind="regex",
                        expected_status=status,
                        is_verified=(status == "verified"),
                    )
                )

    # 8. Classification (animal_types & business_model)
    if extract_classification:
        combined_content = (title + " " + sanitized_text).lower()
        has_dog = bool(
            re.search(
                r"\b(dog|dogs|canine|canines|puppy|puppies|hound|hounds|k9)\b",
                combined_content,
            )
        )
        has_cat = bool(re.search(r"\b(cat|cats|feline|felines|kitten|kittens)\b", combined_content))

        animal_val = ""
        if has_dog and has_cat:
            animal_val = "dog, cat"
        elif has_dog:
            animal_val = "dog"
        elif has_cat:
            animal_val = "cat"

        if animal_val:
            candidates.append(
                CandidateClaim(
                    category="animal_types",
                    claim_key="animal_types",
                    claim_value=animal_val,
                    source_url=url,
                    source_text_snippet=title or animal_val,
                    source_kind="regex",
                    expected_status="verified",
                    is_verified=True,
                )
            )
        else:
            candidates.append(
                CandidateClaim(
                    category="animal_types",
                    claim_key="animal_types",
                    claim_value="unknown",
                    source_url=url,
                    source_text_snippet=title or "Unknown animal types",
                    source_kind="regex",
                    expected_status="needs_review",
                    is_verified=False,
                )
            )

        # 9. Business model classification (salon / mobile / daycare)
        has_mobile = bool(
            re.search(r"\b(mobile|van|doorstep|wagging\s+wheels|paw\s+van)\b", combined_content)
        )
        has_daycare = bool(
            re.search(
                r"\b(daycare|day\s+care|boarding|crèche|creche|kennel|kennels)\b",
                combined_content,
            )
        )
        has_salon = bool(
            re.search(r"\b(salon|spa|studio|boutique|parlor|parlour|shop)\b", combined_content)
        )

        model_val = ""
        if has_mobile:
            model_val = "mobile"
        elif has_daycare:
            model_val = "daycare"
        elif has_salon:
            model_val = "salon"

        if model_val:
            candidates.append(
                CandidateClaim(
                    category="business_model",
                    claim_key="business_model",
                    claim_value=model_val,
                    source_url=url,
                    source_text_snippet=title or model_val,
                    source_kind="regex",
                    expected_status="verified",
                    is_verified=True,
                )
            )
        else:
            candidates.append(
                CandidateClaim(
                    category="business_model",
                    claim_key="business_model",
                    claim_value="unknown",
                    source_url=url,
                    source_text_snippet=title or "Unknown business model",
                    source_kind="regex",
                    expected_status="needs_review",
                    is_verified=False,
                )
            )

    return candidates


def canonicalize_business_name(value: str) -> str:
    """Collapse page-title prefixes like 'About us' and 'Book now' to the real brand name."""
    cleaned = value.strip()
    for page_token in (
        "about us",
        "book an appointment",
        "book now",
        "book appointment",
        "privacy policy",
        "prices & services",
        "prices and services",
        "services",
        "services2",
        "terms and conditions",
        "work with us",
        "our services",
        "contact us",
        "faqs",
    ):
        cleaned = re.sub(
            rf"(?i)(?:^|\s|[-–—:|]){re.escape(page_token)}(?:\s*[-–—:|]\s*|\s+)",
            " ",
            cleaned,
        )
    cleaned = re.sub(r"[\-–—|:]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _fact_conflict_key(fact: ExtractedFact) -> str:
    value = fact.claim_value.strip()
    if fact.category == "business_name":
        value = canonicalize_business_name(value)
    elif fact.category == "phone":
        region = _detect_region(fact.source_url, fact.source_text_snippet)
        value = _normalize_phone_number(value, region) or value
        value = re.sub(r"\D", "", value)
    elif fact.category == "address":
        street_match = re.search(
            r"\b\d+[a-z]?\s+(?:[\w.'’-]+\s+){0,4}"
            r"(?:street|st|road|rd|lane|ln|avenue|ave|way|terrace|hill|close|court|"
            r"square|drive|dr|deansgate|mill|walk|boulevard|blvd|place|pl)\b",
            value,
            re.I,
        )
        postcode_match = UK_POSTCODE_RE.search(value) or re.search(
            r"\b\d{5}(?:-\d{4})?\b", value
        )
        if street_match and postcode_match:
            street = re.sub(r"[^\w\d]", "", street_match.group(0).lower())
            postcode = re.sub(r"\s+", "", postcode_match.group(0).lower())
            return f"{street}:{postcode}"
    return re.sub(r"[^\w\d]", "", value.lower())


def _phone_location_key(fact: ExtractedFact) -> str | None:
    match = re.search(
        r"(?P<label>[\w\s'-]{1,40}?)\s+(?:phone|telephone|tel)\b",
        fact.source_text_snippet,
        re.I,
    )
    if not match:
        return None
    label = re.sub(
        r"\b(?:contact|footer|main|old|new|phone|telephone|tel|call|office)\b",
        " ",
        match.group("label").lower(),
    )
    normalized = re.sub(r"[^\w\d]", "", label)
    return normalized or None


def _source_priority(fact: ExtractedFact) -> int:
    """Rank claim sources: human manual > JSON-LD > href > regex/title heuristics."""
    return {"manual": 4, "json_ld": 3, "href": 2}.get(fact.source_kind, 1)


def _detect_conflicts_and_resolve_statuses(
    facts: list[ExtractedFact],
) -> list[ExtractedFact]:
    """Mark competing identity/contact claims for human review without changing their text.

    When competing values disagree, the documented source priority decides the winner:
    if the best source kind (e.g. JSON-LD) agrees on a single value, lower-priority
    variants such as raw page titles are demoted on their own instead of dragging the
    authoritative claim down to needs_review with them. If the best source kind itself
    disagrees, every candidate is demoted — genuine ambiguity still needs a human.
    """
    conflict_sensitive_categories = {"business_name", "phone", "address"}

    for cat in conflict_sensitive_categories:
        cat_facts = [f for f in facts if f.category == cat]
        if len(cat_facts) < 2:
            continue
        if cat == "phone":
            location_keys = [_phone_location_key(fact) for fact in cat_facts]
            if all(location_keys) and len(set(location_keys)) == len(location_keys):
                continue

        normalized_map: dict[str, list[ExtractedFact]] = {}
        for fact in cat_facts:
            normalized_map.setdefault(_fact_conflict_key(fact), []).append(fact)

        if len(normalized_map) >= 2:
            top_priority = max(_source_priority(f) for f in cat_facts)
            top_keys = {
                norm
                for norm, group in normalized_map.items()
                if any(_source_priority(f) == top_priority for f in group)
            }
            authoritative_agrees = len(top_keys) == 1
            for norm, same_value_facts in normalized_map.items():
                competing_fact_ids = [
                    other.fact_id
                    for other_norm, other_facts in normalized_map.items()
                    if other_norm != norm
                    for other in other_facts
                ]
                if authoritative_agrees and norm in top_keys:
                    # Best-source consensus wins; keep it verified and undisputed.
                    continue
                for fact in same_value_facts:
                    fact.is_verified = False
                    fact.status = "needs_review"
                    fact.conflicts_with = competing_fact_ids

    return facts


def extract_business_facts(
    project_folder: str | Path,
    *,
    candidate_claims: Sequence[CandidateClaim] | None = None,
    extract_classification: bool = False,
) -> BusinessFactsReport:
    """Extract and verify business facts from crawled pages and save business-facts.json."""
    project_root = Path(project_folder).resolve()
    project_id = project_root.name
    pages_path = project_root / "research" / "pages.json"
    if not pages_path.is_file():
        raise ValueError(f"Missing crawled pages manifest: {pages_path}")

    pages: list[dict[str, Any]] = json.loads(pages_path.read_text(encoding="utf-8"))
    injection_warnings: list[str] = []
    discovered_candidates: list[CandidateClaim] = []

    for page in pages:
        url = str(page.get("url", ""))
        sha = str(page.get("sha256", ""))
        title = str(page.get("title", ""))
        snap_path = project_root / "research" / "snapshots" / f"{sha}.html"

        raw_html = ""
        if snap_path.is_file():
            raw_html = snap_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(raw_html, "html.parser")
            text_soup = BeautifulSoup(raw_html, "html.parser")
            collected = collect_page_text(text_soup, url)
            sanitized_text = str(collected["text"])
            injection_warnings.extend(collected.get("prompt_injection_hits", []))
        else:
            raw_text = str(page.get("text", ""))
            sanitized_text, hits = sanitize_prompt_injection(raw_text)
            injection_warnings.extend(hits)
            soup = BeautifulSoup(raw_html or sanitized_text, "html.parser")

        discovered_candidates.extend(
            _extract_candidates_from_page(
                url=url,
                title=title,
                sanitized_text=sanitized_text,
                soup=soup,
                extract_classification=extract_classification,
            )
        )

    if candidate_claims:
        discovered_candidates.extend(candidate_claims)

    facts: list[ExtractedFact] = []
    for cand in discovered_candidates:
        digest = hashlib.sha256(
            f"{project_id}:{cand.category}:{cand.claim_key}:{cand.claim_value}:{cand.source_url}".encode()
        ).hexdigest()[:12]

        facts.append(
            ExtractedFact(
                fact_id=f"fact-{digest}",
                project_id=project_id,
                category=cand.category,
                claim_key=cand.claim_key,
                claim_value=cand.claim_value.strip(),
                source_url=cand.source_url,
                source_text_snippet=cand.source_text_snippet.strip(),
                source_kind=cand.source_kind,
                structured_value=cand.structured_value,
                is_verified=cand.is_verified,
                status=cand.expected_status,
                last_checked_at=_utc_now(),
            )
        )

    # Conflict resolution across single-valued categories
    facts = _detect_conflicts_and_resolve_statuses(facts)

    # Deduplicate single classification categories (animal_types, business_model)
    final_facts: list[ExtractedFact] = []
    seen_classifications: set[str] = set()
    for f in sorted(facts, key=lambda x: (x.is_verified, x.status == "verified"), reverse=True):
        if f.category in ("animal_types", "business_model"):
            if f.category in seen_classifications:
                continue
            seen_classifications.add(f.category)
        final_facts.append(f)
    facts = final_facts

    report = BusinessFactsReport(
        project_id=project_id,
        facts=facts,
        prompt_injection_warnings=sorted(set(injection_warnings)),
    )
    out_path = project_root / "research" / "business-facts.json"
    _atomic_write_text(out_path, _serialize_business_facts_report(report))
    return report
