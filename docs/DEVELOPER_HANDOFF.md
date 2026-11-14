# Developer Handoff

**Project:** EditVista Pet Grooming Website Builder Engine  
**Handoff date:** 12 November 2026  
**Delivery status:** Local-first prototype; not production-ready

## Repository and current code

The current demo-fixture work is on GitHub branch
[`feat/demo-fixture-mode-20261112`](https://github.com/rashidtesting8-ops/website-builder-testing/tree/feat/demo-fixture-mode-20261112).
The branch contains the demo-mode implementation, focused regression tests, and updated
project documentation. It has been pushed but is not a merged `main` release.

Read these first:

1. [DEVELOPER_BRIEF.md](DEVELOPER_BRIEF.md) — project scope and acceptance criteria.
2. [README.md](../README.md) — setup, architecture, tests, and milestone status.
3. [LOCAL_DEMO.md](LOCAL_DEMO.md) — local Docker/n8n setup and boundaries.
4. [DEMO_MODE.md](DEMO_MODE.md) — fixture classification and enforcement rules.
5. [SOP_LOCAL_DEMO_AND_CLIENT_PILOT.md](SOP_LOCAL_DEMO_AND_CLIENT_PILOT.md) — operational procedure.

## Environment and validation

- Python 3.12 and `uv`.
- Pydantic v2 contracts under `src/engine/contracts/`.
- Optional browser extra for Playwright QA.
- Docker Desktop for the local Compose stack.

Validation commands:

```powershell
uv sync --extra browser --group dev
uv run ruff check .
uv run pyright
uv run pytest
```

Last clean-checkout result: Ruff passed; Pyright reported 0 errors, warnings, or
informations; pytest collected 220 tests, with 218 passed and 2 optional Lily reference
tests skipped because their Git-ignored local project data was absent. With that local
data present, all 220 tests passed.

## Runtime and workflow handoff

The Compose stack is local and loopback-bound. At handoff time, Docker showed n8n,
collector, and Cockpit running; collector and Cockpit were healthy. The following
endpoints returned HTTP 200 during the check:

- `http://127.0.0.1:5678/`
- `http://127.0.0.1:8787/health`
- `http://127.0.0.1:8888/`

The repository includes four n8n workflow JSON exports. The documented local demo imports
`n8n/04-interactive-one-click-luxury-demo.json` manually and runs its manual trigger.
Workflow execution should be evidenced by the n8n execution history; service health alone
does not prove a successful workflow run.

## Lily reference project: handle as legacy internal data

The local `projects/lilys-club-live-pilot/` directory is Git-ignored and must remain
internal. The following checks were observed:

- `research/asset-manifest.json` contains 40 assets; all 40 rights flags are false.
- `qa/qa-report.json` is marked passed at 1440, 1024, 768, 390, and 360 pixels.
- Candidate files exist in `release-candidate/`, including `ghl-ready.html`.
- `delivery/release-manifest.json` is absent; the candidate is not an approved release.
- The directory currently has neither `input.json` nor `.editvista-demo-mode.json`.

Therefore do not describe this earlier project run as protected by the new demo
classification. Do not publish its assets or treat its candidate GHL file as deployed.
For another third-party internal test, use a fresh project ID with
`demo.is_fixture: true` and `demo.kind: "third_party_internal_demo"` before the first
pipeline run. The third-party path must keep rights unconfirmed and must refuse approval
and export.

## Milestone and acceptance status

- **M1–M4:** Engineering paths and automated/internal fixture evidence exist. The evidence
  does not substitute for business-owner fact review, rights clearance, or real-client
  acceptance.
- **M5:** Open. The Developer Brief requires five varied real-business pilots and recorded
  operational results. One client URL is a starting input, not completion of the
  milestone.
- **GHL:** Exporter and simulation paths exist; no actual published GHL page was validated
  in this handoff. Use an authorized test account and verify the published page against
  the approved release hash.
- **Production:** Not a production deployment. Keep services local; do not expose the
  Compose stack publicly.

## Immediate next actions

1. Obtain the first authorized client URL and written permission for the permitted
   collection/review scope.
2. Identify the client reviewer who can confirm claims and asset rights.
3. Create a fresh project directory and input before the first run; do not reuse or relabel
   the legacy Lily project.
4. Run the SOP, retain the n8n execution ID and five-width QA evidence, and obtain
   human approval before release.
5. Arrange an authorized GHL test page and separately plan the remaining four real
   businesses for M5.

Never put credentials in the repository or ask a client to send passwords in chat. The
development work used VS Code, Antigravity, and Claude; Codex was unavailable because of
usage limits. Tool choice is not acceptance evidence—the repository history, test output,
and review artifacts are.
