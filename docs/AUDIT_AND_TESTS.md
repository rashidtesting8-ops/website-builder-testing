# Module 01 v2 — audit findings and validation

## Bugs / design gaps from the original package

1. Social profiles were *only discovered*, not downloaded → optional gallery-dl adapter added.
2. A one-year filter was initially attempted at the gallery-dl CLI layer, but the actual installed gallery-dl version rejects that flag. The safe fallback is to apply post-date verification in Python after metadata parsing, and mark `post_date_verified: false` until a real timestamp is available.
3. Too much potential disk usage → reduce default images, per-file and staging caps, short timeouts, selective post-approval cleanup.
4. In-memory-only job status vanished on restart → SQLite persistence and interrupted status.
5. Crawler could fetch more than its final HTML size check and leave streamed connections open → bounded streaming reads and resource cleanup.
6. Robots unavailable/disallow previously could accidentally be treated as allow → fail closed for inaccessible robots / authorization failures; 404 robots allowed.
7. JS-loaded gallery assets missed → optional Playwright rendered-gallery inspector and bounded extra downloads.
8. No automated local page QA → optional Playwright screenshot reporter at 1440/1024/768/390/360px.
9. No safe retention rules → explicit approve + manifest + SHA-verified archive before pruning.
10. Poor research review UX → static low-weight contact sheet from saved media.
11. Floating `n8n:latest` image → configurable version tag default; pull unverified in this environment.
12. Social link hostname substring checks could incorrectly match lookalike domains → strict host checks.

## Actual tests run

- `python -m unittest discover -s tests -v`: 6 synthetic tests passed at build time.
- `python -m compileall -q app`: completed successfully.
- Confirmed source tree and JSON workflow files parse.
- Synthetic local HTTP site test: HTML, linked gallery, manifest, robots and file deduplication.
- Stub social extractor: verifies safe bounded collection defaults, file count, duplicate handling, corrupted-image rejection, and review flags without unsupported gallery-dl CLI flags.
- Cleanup tests: refuses unapproved projects or missing archives; retains referenced images; archive and prune work on a temporary project; blocks unsafe relative paths.

## Not tested / cannot claim production ready

- No actual Meta login or live Instagram/Facebook download, due to access restrictions in this test environment.
- No live business browser-gallery scan and visual check; the Chromium binary is not available here.
- No actual Docker/n8n import or deployment test; Docker is unavailable here.
- No finished AI designer, trained visual critic, polished website builder, or GHL exporter has been created yet.
- This package is the audited **collector foundation**, not a complete complete website builder.

## QA and implementation next gates

1. Run one verified real website with an actual image-heavy gallery and review contact sheet.
2. Run one public IG and one public FB profile, verify real date filter and attribution behavior; handle any blocked profiles manually.
3. Validate Playwright rendered pages and 5-width QA after installing Chromium locally.
4. Connect the verified factual/asset bundle to a strong model-driven art-director and site builder, preserving the reference design bar.
5. Only then add automatic critique/revision, GHL code generation and verified standalone/GHL export packaging.

