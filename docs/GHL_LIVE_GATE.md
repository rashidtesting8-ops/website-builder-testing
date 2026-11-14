# Human GHL Live Gate Checklist

This gate is for a human who can access the authorized GoHighLevel account and paste the
approved snippet. The automated harness checks a published URL; it cannot log in, publish,
or certify account-specific behavior. **Do not describe a result as a GHL pass until the
human checklist and the actual published-page checks are complete.**

## Checklist

1. **Create the page.** In the authorized GHL account, create the intended funnel or website
   page. Confirm the account, location, page, and publication permissions with the account
   owner. Exact navigation and page-creation labels can vary; **verify in account**.
2. **Add a Custom HTML/JS element.** Add the account's custom-code element to the intended
   page section. The exact element name, placement, and script/style restrictions are
   account/UI-dependent; **verify in account**.
3. **Paste the approved snippet.** Use only
   `projects/<project-id>/delivery/ghl-ready.html` from the approved release. Paste the
   complete contents without removing or rewriting the root, `<style>`, or `<script>` blocks.
   Whether the editor normalizes or sanitizes markup on paste/save is **verify in account**.
4. **Save and publish.** Save the page and publish it using the account's normal workflow.
   Confirm that the published page is accessible without editor preview mode. The publication
   controls, propagation delay, redirects, and any password/access settings are **verify in
   account**.
5. **Record the published URL.** Copy the public HTTPS URL after publication. If the account
   displays a preview URL and a published URL, use the published URL; which URL is final is
   **verify in account**.
6. **Record the exact release.** From `delivery/release-manifest.json`, record
   `project_id` and `version_hash`. Record the manifest's
   `checksum_tree["delivery/ghl-ready.html"]` value and the tested snippet SHA-256 printed
   in `qa/ghl-live/ghl-live-report.json`. They must match.
7. **Run the browser harness** from the repository root in PowerShell:

   ```powershell
   uv run python scripts/ghl_live_gate.py "https://published-page.example/" ".\projects\<project-id>" --out qa/ghl-live
   ```

   Replace the example URL and project path with the actual published page and approved
   project. The live CLI requires HTTPS and rejects local/private host URLs. It writes the
   report and viewport screenshots below the selected output directory. It does not publish
   or alter the page.
8. **Review the report and screenshots.** Open
   `projects/<project-id>/qa/ghl-live/ghl-live-report.json` and all five screenshots. Review
   failed checks, console errors, request failures, asset status codes, computed-style deltas,
   host-element deltas, mobile interactions, and overflow. Do not ignore a failure or change
   the approved delivery file in place; fix the source, create a newly approved release, and
   rerun against that exact release.
9. **Record the account-side evidence privately.** Record the account/location and page
   identifier, publisher/reviewer, publication time, final URL, project ID, release hash,
   snippet hash, report path, outcome, and any manual observations. Keep screenshots of the
   account UI in the private evidence location, not in Git. Redact access tokens and other
   credentials from notes and evidence.
10. **Resolve account-specific uncertainty.** Whether GHL preserves the snippet DOM, executes
    inline JavaScript, allows inline styles, delays mounting, changes markup, or applies
    different desktop/mobile wrappers is **verify in account**. The report's DOM and
    interaction checks provide evidence for the observed published URL only; they do not
    establish that every account, editor, or future publish behaves identically.

## Pass criteria

The automated report may have `overall_pass: true` only when all of its checks pass:

- The release manifest's GHL snippet hash matches the exact `delivery/ghl-ready.html` bytes
  tested.
- The expected `#ghl-ev-engine-root-<project-id>` is present at all five widths.
- Delivered inline scripts and styles survive unchanged; the mobile menu opens and closes
  at 768, 390, and 360 pixels; any included slider changes its value.
- Ten selected element styles match the local approved render at all five tested widths, and
  host elements outside the root show no computed CSS-property changes against the no-snippet
  baseline at those widths, except flow-dependent height/origin values that naturally change
  when the embed adds document content.
- All snippet asset references are permitted HTTPS URLs and return HTTP 200. No local paths,
  localhost/private hosts, or mixed-content asset URLs are accepted by the live CLI.
- No console errors, failed requests, broken images, or horizontal overflow are reported.
- Screenshots exist for 1440, 1024, 768, 390, and 360 pixels.

`overall_pass` is a browser-check result, **not an account approval or a claim that GHL
passes**. A human must also confirm the correct account/page was published, review the
rendered page, and resolve any account-specific behavior marked **verify in account**.
