# Local Docker Demo

This is a loopback-only prototype demo. It does not publish services to the LAN or internet.

## Start the stack

From the repository root, with Docker Desktop running:

```powershell
docker compose up --build -d
docker compose ps
```

Wait until `collector` and `cockpit` report healthy. Open:

- n8n: <http://127.0.0.1:5678>
- Operator Cockpit: <http://127.0.0.1:8888>
- Worker health: <http://127.0.0.1:8787/health>

On the first n8n visit, complete its local owner-account setup. Import
`n8n/04-interactive-one-click-luxury-demo.json` using n8n's workflow import UI, then run its
manual trigger. The workflow submits a site build, polls the worker, and returns a Cockpit URL.
The example workflow targets a public demo business; replace its URL/name/ID with a business
you are authorized to evaluate before using it as a customer demo.

In the Cockpit, inspect factual claims and asset provenance. A rights confirmation requires a
basis, reviewer name, and evidence. Confirm rights only when you have actual permission or
another valid basis; do not treat successful downloading or public visibility as permission.
Approval remains blocked until required checks and rights evidence pass.

## Demo boundaries

- Playwright and its Chromium browser are installed in the collector image at the same pinned
  Playwright version.
- `docker compose down` stops the services but preserves the named n8n and worker-state volumes and
  project files. Do not remove volumes or project data unless you intend to delete that state.
- The Compose ports bind to `127.0.0.1`; `N8N_SECURE_COOKIE=false` is only for this local HTTP
  setup. Do not expose this configuration outside the local machine.
- n8n task runners are disabled because the included workflows use no Code nodes. Enable and
  configure task runners separately before adding workflows that require them.
- The n8n workflow JSON is imported manually and is not activated automatically.
- Instagram/Facebook collection is not part of this Docker workflow. Use client-authorized exports
  or media for the demo. The optional `gallery-dl` adapter may return `review_needed` when access,
  permissions, or platform behavior prevent compliant collection. Do not bypass login, CAPTCHA,
  rate limits, or access controls.
- This prototype has not been validated as a production deployment or against a live GoHighLevel
  account.

## Troubleshooting

```powershell
docker compose logs --tail 100 collector
docker compose logs --tail 100 cockpit
docker compose logs --tail 100 n8n
```

If Docker commands cannot reach the engine, start Docker Desktop and wait for its engine to become
ready before retrying. Browser QA errors should remain visible as failed jobs; do not mark a run
approved based on a missing browser or missing screenshots.
