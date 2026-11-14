# Demo Fixture Mode

**Status:** Implemented and validated.

## Goal

Keep internal fixture projects visibly and structurally distinct from real-business
projects. A fixture must never be presented or counted as an approved real-business
release. This mode is a classification and safety gate; it does not grant permission to
use third-party content or make synthetic facts true.

## Current behavior and implementation points

- `app/pipeline.py` accepts `url`, `business_name`, and `business_id` as arguments and
  derives a project directory from the project ID. It now validates optional
  `projects/<project_id>/input.json` demo metadata before collection or other project
  mutations. `StageInput` remains a generic stage contract, not the business intake schema.
- `app/generator.py` assembles `website/index.html` from editable section files. A banner
  placed inside an ordinary section could be removed by a section revision.
- `app/approval.py` stages candidates and freezes approved files with a release manifest.
  `app/exporter.py` is only a backward-compatible shim; the implementation of
  `export_release` is in `src/engine/exporter.py`.
- Rights confirmation is written by `app/rights.py`. The CLI and Cockpit route through
  this central writer, which now refuses third-party demo confirmation. Pipeline
  collection/resume paths also clear any third-party demo confirmation flags.
- GHL snippet generation has a direct path in `app/ghl_exporter.py`; it now refuses
  third-party demo exports, as does `src/engine/exporter.py`.
- `app/pilot.py` seeds a fixed set of fixture specifications and generated mock images.
  Its `--live` option selects real Playwright QA; it does not turn those specifications
  into real businesses or fetch client websites. The current five-fixture report must not
  be treated as the real-business pilot.

## Input contract

Read optional demo metadata from the approved project-local path
`projects/<project_id>/input.json`:

```json
{
  "demo": {
    "is_fixture": true,
    "kind": "fictional",
    "reason": "Internal workflow demonstration using synthetic business data."
  }
}
```

`kind` is restricted to `fictional` or `third_party_internal_demo`. When
`is_fixture` is true, `reason` must be non-blank. A present demo object must be
schema-validated; malformed JSON, unknown kinds, and invalid values fail before
collection or mutation rather than silently falling back to real-business mode.

An absent `demo` field means real-business mode and must preserve current behavior.
An explicit `is_fixture: false` also follows the real-business path. Existing URL,
business-name, and project-ID CLI arguments remain authoritative; `input.json` supplies
the optional project classification, not a second conflicting source for those values.

The classification is resolved from the target project directory only. It is never
inferred from a URL, business name, another project, or a global cache. Resume and
approval operations use that same project-local state. The first generation/pipeline
operation freezes the classification in `.editvista-demo-mode.json`; changing or
removing it later fails closed and requires a new project identity. Approved releases
remain immutable.

## Mode behavior

| Mode | Preview and HTML | Rights confirmation | Approval and export | Pilot accounting |
|---|---|---|---|---|
| Real business (`demo` absent or `is_fixture: false`) | Preserve existing output | Existing rights workflow | Preserve existing gates | Eligible only under the existing real-pilot criteria |
| Fictional fixture | Visible internal-demo banner on every generated HTML output | Only a genuine documented basis may confirm an asset; no automatic confirmation | Allowed only through existing human review, QA, rights, and immutable-release gates; record `demo: true` | Excluded from real-business totals; may appear in a separate fixture count |
| Third-party internal demo | Same visible banner | System write paths must refuse setting any asset's `rights_confirmed_for_preview` to true | Approval, standalone delivery export, and GHL export are refused | Excluded from real-business totals |

Third-party public content remains unlicensed unless valid permission or another legal
basis is documented. The demo label itself is never evidence of rights.

## Banner and release enforcement

1. Define one exact banner string:
   **`INTERNAL DEMO - NOT A VERIFIED BUSINESS`**.
2. Inject it into the generated document wrapper, outside section HTML that can be
   revised. Rebuilding the index after a targeted revision must reinsert it.
3. Give the banner a stable machine-readable marker. Candidate staging, approval, and
   every export path must verify the marker and visible text before proceeding. Missing
   or altered banner means fail closed. This makes the banner non-removable through the
   supported generation/revision/release workflow; it cannot prevent an operator with
   direct filesystem access from editing a draft file.
4. Every fixture HTML output—including standalone and GHL HTML when fictional export is
   allowed—carries the visible banner and a machine-readable `demo=true` marker.
5. Fixture `release-manifest.json` records `demo: true`, `kind`, and `reason`. Structured
   delivery sidecars, when emitted, carry the same fixture metadata. Real-business
   manifests omit the demo marker so legacy release output remains unchanged.
6. Release checks re-read project-local mode and verify the candidate banner. A
   third-party fixture is rejected regardless of asset-manifest flags, audit entries,
   approval tokens, or the selected exporter.

## Pilot separation

The built-in `app/pilot.py` fixture harness remains useful for deterministic engineering
tests, but it is reported separately from real-business acceptance. Each generated
project is explicitly marked `fictional`; the report shows fixture runs and eligible
real-business runs as separate quantities. `--live` only selects real Playwright QA and
does not change project classification. Until a real-business intake path is supplied
and reviewed, the five-fixture harness cannot produce evidence that the five-business
pilot is complete.

## Tests and validation

`tests/test_demo_mode.py` was added first and initially failed on the missing behavior.
It now covers:

1. Banner appears in generated preview and release outputs; targeted section revision
   cannot remove it, and approval rejects a manually stripped banner.
2. Third-party internal demos cannot confirm rights through the central rights writer
   or CLI, and pipeline manifest/SQLite paths keep rights false.
3. Third-party approval, standalone export, and direct project-based GHL export are refused.
4. Fictional fixtures pass normal human review, QA, and rights gates; export HTML with
   the banner; and record `demo: true`, kind, and reason in release evidence.
5. Pilot reports exclude fixture projects from real-business totals and report fixture
   counts separately, including when live Playwright QA is selected.
6. Two project directories with the same source URL retain independent fixture states.
7. An absent demo field preserves current real-project behavior and release output.
8. Invalid demo metadata fails closed before any network collection or filesystem
   mutation.

Validation completed in a clean Git checkout: `uv run ruff check .` passed; `uv run pyright`
reported 0 errors, 0 warnings, and 0 informations; `uv run pytest` collected 220 tests,
with 218 passed and 2 optional local-reference checks skipped because their Git-ignored
project data was absent. With that local data available, all 220 tests passed.

## Approved scope

The original file allowlist was expanded with approval to cover all required enforcement
paths:

- `app/rights.py` blocks third-party rights confirmations at the central writer.
- `src/engine/exporter.py` blocks direct standalone export and records fixture metadata.
- `app/ghl_exporter.py` blocks direct GHL snippet generation for third-party demos.

The existing Cockpit routes call the central rights and approval functions, so the
central checks cover them without modifying `app/cockpit.py`.

## Approval record

The user approved this design and confirmed `projects/<project_id>/input.json`. The user
also approved adding the three enforcement files listed above. Phase B is complete;
automated validation passed. This does not constitute a real-client pilot or permission
to reuse third-party material.
