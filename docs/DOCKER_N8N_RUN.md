# Docker + n8n Laptop Run Evidence

**Run date:** 2026-11-13  
**Result:** Docker Compose builds and runs locally; all three container healthchecks pass.
The four n8n workflow definitions were imported and remain inactive. No workflow execution
or mid-run restart is claimed because no preclassified fictional fixture can safely reach the
worker URL gate under the current no-code-change scope.

## Environment

- Host OS: Microsoft Windows 10 Pro, version 10.0.19045, build 19045, 64-bit.
- Docker Desktop: 4.98.0 (245422).
- Docker CLI and engine: 29.9.2; context `desktop-linux`, Linux/amd64 engine.
- n8n image and runtime version: `n8nio/n8n:2.47.3`, runtime reports `2.47.3`.
- The Compose service ports bind to loopback:
  - n8n: `127.0.0.1:5678` -> container `5678`.
  - collector: `127.0.0.1:8787` -> container `8787`.
  - cockpit: `127.0.0.1:8888` -> container `8888`.

The tag, limits, and ports above were read from [docker-compose.yml](../docker-compose.yml)
and checked against the rendered `docker compose config`. The n8n healthcheck calls
`/healthz`; the collector healthcheck calls `/health`; and Cockpit calls `/api/health`.
The image for both Python services is built from [Dockerfile](../Dockerfile).

## Commands and results

All Docker command output files and exit-code files are in
[docs/evidence/docker-n8n/](evidence/docker-n8n/). No `.env` file was copied there.
The evidence text files were checked for common credential patterns; none matched.

| Command | Result |
|---|---|
| `docker version` | Exit 0. CLI and engine are Docker 29.9.2. Raw output: `docker-version.txt`. |
| `docker compose config` | Exit 0. Rendered configuration saved as `docker-compose-config.txt`. |
| `docker compose up -d --build` | Exit 0. Collector and Cockpit images built; all services started and Compose reported the worker and Cockpit healthy. Raw output: `docker-compose-up-build-raw.txt`. |
| `docker compose ps` | Exit 0. n8n, collector, and Cockpit all reported `healthy`. Raw output: `docker-compose-ps.txt` and final snapshot `docker-compose-ps-final.txt`. |
| n8n Compose healthcheck | `docker compose exec -T n8n node -e "require('http').get('http://127.0.0.1:5678/healthz', response => process.exit(response.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(1))"` — exit 0. |
| collector Compose healthcheck | `docker compose exec -T collector python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"` — exit 0. |
| Cockpit Compose healthcheck | `docker compose exec -T cockpit python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/api/health', timeout=3)"` — exit 0. |
| `docker stats --no-stream` | Exit 0. Snapshot below; raw output: `docker-stats-no-stream.txt`. |

The three healthcheck commands returned no response-body text; each exit code was `0`.
Docker Compose independently reported each container as healthy.

## Configured and observed memory

The configured caps below come from [docker-compose.yml](../docker-compose.yml); actual
usage is one `docker stats --no-stream` snapshot after startup.

| Service | Compose cap | Observed use | Brief comparison |
|---|---:|---:|---|
| n8n | `1g` (1 GiB) | 494 MiB / 1 GiB | Matches the stated 1 GB target nominally. Compose's `g` cap is 1 GiB (1024 MiB). |
| collector | `2g` (2 GiB), reservation `1g` | 40.55 MiB / 2 GiB | **Does not match** the stated 512 MB target: the hard cap is 4x 512 MiB (1.5 GiB higher). |
| Cockpit | `512m` (512 MiB) | 43.75 MiB / 512 MiB | No corresponding target was provided. |

No PDF was present in the workspace search. The numeric comparison above therefore uses
the brief values (n8n 1 GB, collector 512 MB); `docs/DEVELOPER_BRIEF.md` §6 does specify
these caps ("Docker caps of 1GB n8n and 512MB collector"), so the compose file's 2 GiB
collector cap is a deliberate deviation from the brief that should be reconciled.

## Workflow import and execution

The workflow JSON files were read before import. Their trigger paths, parsed from those
files and saved in `workflow-webhook-paths.txt`, are:

| Workflow | Trigger in source JSON |
|---|---|
| 01 Client Intake | `POST pet-grooming-research` |
| 02 Telemetry | `GET pet-grooming-research-status` |
| 03 Approval | `POST pet-grooming-approve` |
| 04 One-Click Demo | Manual trigger; no HTTP webhook path. |

Import used the n8n CLI syntax confirmed by `n8n import:workflow --help`:

```text
docker exec website-builder-testing-main-n8n-1 mkdir -p /tmp/editvista-n8n-workflows
docker cp n8n\01-client-intake-and-website-build.json website-builder-testing-main-n8n-1:/tmp/editvista-n8n-workflows/
docker cp n8n\02-live-build-telemetry-and-cockpit-router.json website-builder-testing-main-n8n-1:/tmp/editvista-n8n-workflows/
docker cp n8n\03-approve-and-export-ghl.json website-builder-testing-main-n8n-1:/tmp/editvista-n8n-workflows/
docker cp n8n\04-interactive-one-click-luxury-demo.json website-builder-testing-main-n8n-1:/tmp/editvista-n8n-workflows/
docker compose exec -T n8n n8n import:workflow --separate --input=/tmp/editvista-n8n-workflows
```

Actual import output:

```text
[license SDK] Skipping renewal on init: renewOnInit is disabled in config
[license SDK] Skipping renewal on init: license cert is not initialized
Importing 4 workflows...
Successfully imported 4 workflows.
EXIT=0
```

An n8n CLI export after import confirmed four workflows, all inactive:

| Workflow | Imported workflow ID | Active |
|---|---|---|
| 01 Client Intake | `ugkmTEjQg9Xza54g` | false |
| 02 Telemetry | `cGwA1rJOmvcgAdZY` | false |
| 03 Approval | `6PtAsRoIsqGEnCSu` | false |
| 04 One-Click Demo | `gcQWGIkJPX8mXTqR` | false |

**Workflow 01 was not triggered.** The workspace contains no existing project classified
as a fictional fixture, and no `projects/**/input.json` was found. The workflow's intake
node forwards the supplied URL to the collector. The collector `/jobs` route calls
`safe_url()` with its default `allow_private=False`; a local fixture URL is rejected. The
runtime check produced:

```text
http://127.0.0.1/ -> REJECTED: Non-public address blocked: 127.0.0.1
http://127.0.0.1:8765/ -> REJECTED: Only standard HTTP/HTTPS ports (80/443) are allowed
```

The local rejection is also visible in
[src/engine/service.py](../src/engine/service.py) and
[src/engine/collector.py](../src/engine/collector.py). Using a public business URL would
not be a fictional-fixture run and would not satisfy the requested scope. The four imported
workflows were left inactive; the workflow 04 example URL was not run.

Consequently, there is **no workflow execution ID, no per-node execution status, and no
project folder produced by n8n** in this run. The nodes in workflow 01 are all
`not executed`; this is not reported as a successful workflow test. No n8n UI screenshot
was captured.

## Container interruption / resume

The requested mid-run kill/restart test was **not performed**. Without a safe fictional
workflow job to interrupt, killing a container would not test workflow-job resume behavior
and could disrupt the healthy stack. No resume behavior is verified.

## Not verified / next safe prerequisite

- Workflow 01 end-to-end execution, execution ID, per-node statuses, and output project path.
- Container interruption during that execution and whether the job resumes after restart.
- n8n UI interaction and screenshots.
- Any real-business workflow or data collection.
- Numeric cap values against the source PDF itself, which was not present in the workspace.

To complete the two runtime tests without changing code, provide an already hosted,
authorized fictional fixture reachable on a standard public HTTP(S) port and a fresh project
classified as `demo.kind: "fictional"`, or authorize a separately scoped test-only change
that permits a local fixture. No code was changed for this evidence run. The Docker Compose
stack remains running and healthy.
