# Budget and quality policy — Pet Grooming Engine

**Non-negotiable:** design quality is not traded for throughput. Each stage has a pass/fail gate; rejected work is revised, not automatically published.

| Stage | Default tool | Implementation notes |
| --- | --- | --- |
| Workflow orchestration | Self-hosted n8n Community | No n8n license fee for permitted internal agency use; local laptop/server must run |
| Crawl website + download site media | Python requests + BeautifulSoup | Open source; this package implements the first pass |
| JavaScript-heavy sites + mobile QA | Playwright/Chromium | Open source; browser fallback to be implemented |
| Local storage | Project directories and JSON | Existing disk; back up regularly |
| Brand/asset factual validation | Deterministic rules + human check | Zero model calls when possible |
| Image curator + art direction | Strong vision-capable model | Metered API; only after asset collection; no cheap-model substitution if quality falls |
| Website design/code | Strong coding/design model | Metered API; expensive step gated by research completeness |
| Visual critique | Strong vision model + screenshots | Metered API; hard cap on revisions with human escalation |
| Local preview | Double-click standalone HTML + optional Playwright | No external hosting required for local review |

## Spend control

Per-job spend is already enforced by `src/engine/llm/adapter.py`: default
`max_calls_per_job=30`, `max_tokens_per_job=150_000`, `max_cost_usd=10.0`, a pre-spend
estimate that blocks calls over budget, retries counting toward the cap, and usage logged
to `.cache/llm_usage.jsonl`. The items below are the remaining policy layer.

1. Don't call a design model if fewer than the minimum verified assets/facts exist.
2. Cache all collected content and images by hash and source URL.
3. Build only *new* or *modified* pages. Never rerun a complete build after a small content change.
4. Use low-cost methods for sorting, file conversion, dimensions, duplicates and link checks.
5. Allocate AI calls to individual art direction, actual website creation and visual review.
6. Configure a per-project token/cost ceiling and stop for approval when reached (do not degrade creative quality silently).
7. Separate failed asset collection from design rejection; do not waste model calls fixing missing real photos.
8. Log exact API usage, time, QA outcome, and build completion status per project.

## Quality release gates

- [ ] All actual brand/logo and business claims have source URLs or a missing-input flag.
- [ ] Business site Gallery / Portfolio pages inspected; original photography preferred.
- [ ] Hero photo is genuine and compositionally sound; no baked-in text/ads.
- [ ] No generated owner/staff presented as real, fabricated review, pricing or promotion.
- [ ] All displayed photos have known provenance, justified placement, and no obvious repetition.
- [ ] Exactly approved/latest design used for any revisions and for GHL conversion.
- [ ] Screenshots manually checked at 1440 / 1024 / 768 / 390 / 360 px.
- [ ] Mobile nav, CTAs, file loading, responsiveness, before/after interaction checked.
- [ ] Human reviewed and approved before release archive creation.

## Measuring the 5-business pilot

Record: source pages reached; website images saved; social assets obtained; missing/blocked sources; research time; AI spend; code generation time; critique rounds; QA defects; human time; approved/rejected; export validity; archive and cleanup status.

**Do not promise zero total cost:** electricity, local machine resources and optional model API usage can incur costs. Building/testing Module 01 locally requires no paid vendor API.

## Laptop resource policy (v2)

- Prefer the lightweight Python CLI first; do not require n8n or Docker for an initial quality test.
- 25 pages and 80 website media candidates by default; 40 recent social media files per platform when source access is available.
- n8n/collector Docker services have memory caps; stop services when idle. Browser scans are on-demand.
- No auto-publish or auto-prune. Cleanup only after human design approval + retained asset list + successful ZIP verification.
- Local HTML preview runs without a public host.
- Social media published dates and rights must be independently checked; successful download does not grant reuse permission.

