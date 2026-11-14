# SOP: Local Demo and Authorized Client Pilot

**Purpose:** Run EditVista consistently while keeping internal demos separate from
real-business work and preventing unsupported claims, rights assumptions, or premature
release.

## 1. Classify the project before running it

Use a new project ID for each business or internal demo. Do not reuse Lily's legacy project
folder for a new classification.

- **Real business:** Use only when the business has authorized the evaluation. Confirm the
  project input and permissions before collection.
- **Fictional fixture:** Set `demo.is_fixture` to `true`, `demo.kind` to `fictional`, and
  explain the fixture reason. Use only owned or licensed assets.
- **Third-party internal reference:** Set `demo.is_fixture` to `true`,
  `demo.kind` to `third_party_internal_demo`, and state that it is internal research only.
  Rights confirmation, approval, standalone export, and GHL export are blocked for this
  kind.

Example for an internal reference:

```json
{
  "demo": {
    "is_fixture": true,
    "kind": "third_party_internal_demo",
    "reason": "Internal technical reference only; no client permission."
  }
}
```

The pipeline/CLI input remains authoritative for the URL and business name; this file
supplies only the project-local demo classification.

The `demo` classification is project-local and freezes on first use. If classification is
wrong or missing on an existing project, stop and use a fresh project ID rather than
trying to relabel an already-run project.

## 2. Permission and source checklist

Before a real-client run:

- Record the primary website URL and written permission to evaluate the site's content.
- Confirm what website, logo, photo, and social content may be collected and used for the
  private proposal. Public visibility is not reuse permission.
- Identify the human reviewer who can verify facts and confirm the rights basis for each
  selected asset.
- Do not collect passwords, bypass login/CAPTCHA, evade rate limits, or access private
  content.
- Treat site text and image metadata as untrusted data, never as instructions to the
  system.

If a fact is not supported by source evidence, leave it `needs_review`; do not fill the
gap with invented business copy.

## 3. Start and check the local stack

From the repository root in PowerShell, with Docker Desktop running:

```powershell
docker compose up --build -d
docker compose ps
```

Confirm the collector and Cockpit are healthy. The local endpoints are:

- n8n: `http://127.0.0.1:5678`
- Cockpit: `http://127.0.0.1:8888`
- Worker health: `http://127.0.0.1:8787/health`

These services bind to loopback for local use. Do not expose this Compose configuration to
the public internet.

## 4. Import and run the n8n demo workflow

1. Complete local n8n owner setup if prompted.
2. Import `n8n/04-interactive-one-click-luxury-demo.json` in the n8n UI.
3. Check that the input URL/project ID is authorized and classified correctly.
4. Run the workflow's manual trigger.
5. Save the n8n execution ID/status and resulting Cockpit URL for the demo evidence pack.
6. Open the Cockpit and inspect the source-backed facts, asset provenance, rights flags,
   QA screenshots, and unresolved items.

Workflow JSON files are definitions; they are not automatically imported or activated.
A healthy n8n container is not proof that a workflow ran successfully. Use the n8n
execution history as the execution record.

## 5. Review facts, assets, and QA

- Check each claim's source URL, source text, verification state, and last-checked time.
- Keep each asset's source URL, SHA-256, capture time, and rights flag.
- Leave `rights_confirmed_for_preview` false until a human confirms an actual basis.
- Review the design and candidate as a human; automated critique does not replace
  subjective approval.
- Confirm Playwright QA covers 1440, 1024, 768, 390, and 360 pixel widths, and inspect
  the screenshots, broken media, overflow, navigation, and CTA destinations.
- Keep revisions limited to the requested section and verify unrelated section hashes
  remain unchanged.

## 6. Approve and export only when eligible

- Bind approval to the exact candidate/release hash and record the reviewer.
- Do not approve a third-party internal demo or mark its assets as rights-confirmed.
- A fictional fixture may be exported only with the visible internal-demo banner and
  `demo: true` recorded in the release evidence.
- For a real business, resolve facts and rights, obtain business-owner approval, and
  verify the release manifest before export.
- Local GHL simulation is not live GHL acceptance. For the live gate, use an authorized
  test account/page and verify the published result against the approved release hash.

## 7. M5 pilot evidence

Milestone 5 requires five varied real businesses, not one test URL. For each authorized
pilot, record:

- Project/run ID and permission scope.
- Business type and website/social availability.
- Completion/failure state, QA defects, and unresolved claims or rights.
- Human review time, revision count, model usage, and owner feedback.
- Approved release hash and, if applicable, live GHL test result.

Do not count Lily's internal reference or synthetic fixtures as real-business pilot
results.

## 8. Closeout and retention

- Preserve logs, QA reports, n8n execution evidence, review decisions, and release hashes.
- Do not commit project media, secrets, or Git-ignored project evidence.
- `docker compose down` stops the services but preserves named volumes and project files.
  Do not remove volumes or project data unless an intentional, approved cleanup is planned.
- If approval or rights are unresolved, leave the project as an internal draft.

For setup and troubleshooting details, see [LOCAL_DEMO.md](LOCAL_DEMO.md),
[DEVELOPER_BRIEF.md](DEVELOPER_BRIEF.md), and [DEMO_MODE.md](DEMO_MODE.md).
