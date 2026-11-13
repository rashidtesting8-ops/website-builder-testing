# Milestone 1 Run Report — Hachi & Co

**Result: incomplete; do not treat as a verified business pilot or approve a release.**
The project is at `awaiting_approval`, but its business-name claim remains `needs_review`,
all preview-rights flags are false, and no business media was downloaded. No release was
approved or written under `delivery/`.

## Business and scope

- Website: https://www.hachiandco.com.au/
- Supplied Instagram: https://www.instagram.com/hachi.and.co/
- Supplied Facebook: https://www.facebook.com/hachiandcogreensborough/
- Project ID: `hachi-and-co`
- The CLI help was read with `uv run python -m app.pipeline --help`. It provides URL/name,
project/database paths, resume, approval pause, and MVF acknowledgement options, but no
stop-after-curation option and no social URL arguments.
- The site title and extracted candidate say “Hachi & Co”; the extracted business-name facts
remain unverified. The user explicitly authorized `--acknowledge-gaps` to proceed while keeping
that gap open.

## Collection and pipeline run

Initial command:

```text
uv run python -m app.pipeline --url https://www.hachiandco.com.au/ --name "Hachi & Co" --business-id hachi-and-co --projects-dir .\projects --db-path .\projects\editvista.sqlite3 --pause-for-approval
```

The initial run collected pages and facts, then exited with code 1 at the MVF gate because
`business_name` was not verified. The CLI emitted no stdout/stderr; its empty stream was retained
locally as `editvista-m1-hachi-pipeline.log` in the Windows temp directory (not committed).

After the user authorized acknowledging the gap, the run was resumed with
`--resume --acknowledge-gaps --pause-for-approval`. The CLI has no stage limit, and the run
continued beyond curation through art direction, generation, lint, QA, and deterministic visual
critique before pausing at `awaiting_approval`. No release was approved. The resumed command's
stdout/stderr were empty and retained locally as
`editvista-m1-hachi-curation.stdout.log` and `editvista-m1-hachi-curation.stderr.log` in the
Windows temp directory (not committed).

The database shows `collect`, `extract`, `curate`, `art_director`, `generate`, `claims_lint`,
`qa`, `critique`, `revise`, and `ghl_exporter` completed for this acknowledged run. This is an
overrun of the requested “through curation” boundary, not a claim that the pipeline can stop
there. The QA report says five configured viewport checks passed, but the generated site had
no collected photos; this does not prove a genuine, photo-complete business website.

## Counts and provenance

- Pages crawled: **4** — `/`, `/contact`, `/services`, `/location`.
- Media candidates: **16**.
- Collection outcomes: **0 saved**, **0 duplicates**, **9 blocked by robots**, **7 failed with
HTTP 404**.
- Curation: **0 eligible**, **0 selected/kept**, **0 excluded by curator**, **0 gallery photos**;
  no contact sheet was inspected. The curator reported no eligible hero and zero verified
  gallery photos.
- Claims: **14 total** — **8 verified**, **6 needs_review**. Required provenance fields were
  present in these records (the auditor accepts the current collector aliases
  `source_text_snippet` and `is_verified` where used). The auditor validates field presence,
  not the truth of claims; the six `needs_review` records remain unresolved.
- Preview rights: all **16** asset rows explicitly have
  `rights_confirmed_for_preview: false`. No publication permission is inferred.
- Social collection: no social media assets were collected. The website-recorded Facebook URL
  is `https://www.facebook.com/hachiandcomelbourne/`, which differs from the Facebook URL
  supplied for this run. The social manifest remains
  `needs_authorized_collection_or_manual_upload`.

## Gallery pages found vs. missed

The public page sitemap listed 10 URLs. The collector crawled 4; the 6 sitemap URLs not
crawled were:

- `/copy-of-services`
- `/referral-program`
- `/breed-size`
- `/shop`
- `/copy-of-breed-size`
- `/collar-bow`

I manually fetched the 10 sitemap pages (all returned HTTP 200) and checked their titles and
H1–H3 text for gallery/portfolio/photo/before-after labels. **Gallery pages skipped: zero
confirmed gallery-specific routes were identified by that check.** However, gallery coverage
is **not verified**: six sitemap pages were not in the crawl, and `/shop` contained 15 images,
so it may contain relevant imagery even though it was not identified as a grooming gallery.
This was a command-line content check, not a visual browser review.

## Provenance auditor

Command:

```text
uv run python scripts/m1_provenance_audit.py --project-dir .\projects\hachi-and-co
```

Result: exit code **1**; **16 assets checked, 14 claims checked, 48 violations**. The violations
are exactly 16 each for:

- `sha256_missing_or_invalid`
- `asset_file_missing`
- `dimensions_missing`

All 16 rows are failed or robots-blocked candidates, so there are no local bytes to hash and
no downloaded image dimensions. The auditor found no claim-provenance violations. Its complete
JSON output and human-readable violation summary were retained locally as
`editvista-m1-hachi-audit.json` and `editvista-m1-hachi-audit.txt` in the Windows temp directory
(not committed).

The collector's `run-summary.json` has an empty `issues` list even though the asset manifest
records these 16 media failures; this report summarizes them explicitly.

## Resilience results

**4a — mid-collect interruption and resume: not performed.** The CLI has no curation stop
control, and the acknowledged run passed curation before a process could be stopped; no fresh
mid-collect kill was performed and no `skipped_stages` result was captured. Do not treat the
successful final status as proof of the requested mid-collect/resume behavior.

**4b — deliberately broken media URL with other assets intact: not performed.** The run already
had zero saved assets, so it cannot demonstrate that unaffected assets remain intact. The
naturally observed failures below were not deliberately injected and are not a substitute for
the requested resilience test.

No pipeline or collector code was changed.

## Anomalies

The seven natural 404 candidates were:

1. `/quality_auto/Hachi%20and%20Co%20logo%20simple%20final.png`
2. `/quality_auto/e316f544f9094143b9eac01f1f19e697.png`
3. `/quality_auto/8d6893330740455c96d218258a458aa4.png`
4. `/quality_auto/cb175e457890ce6ed05c219ccb08c750.jpg`
5. `/quality_auto/8e16bfada3d848b88b4d0399e0aa27b9.jpg`
6. `/quality_auto/Image%20by%20Karsten%20Winegeart.jpg`
7. `/quality_auto/Image%20by%20Oscar%20Sutton.jpg`

The other nine media candidates were on `static.wixstatic.com` URLs blocked by the site's
robots rules. These were respected; no robots or access restrictions were bypassed. All 16
candidate rows are preserved in `projects/hachi-and-co/research/asset-manifest.json` with
their status; the seven failures include 404 error text.

Six claims remain `needs_review`: five business-name candidates (including page-title variants)
and one incomplete hours claim. `business_name` is the MVF gap acknowledged for this local run;
it has not been independently verified.

## Wall time

Active pipeline wall time was not measured with a process timer. Artifact timestamps span from
collection at `2026-11-13T09:20:22.593891Z` to final QA at
`2026-11-13T09:29:28.583155Z` (about **9 minutes 6 seconds**, including the operator
acknowledgement/wait between runs); that interval is not a valid single-run execution duration.
