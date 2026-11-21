interface ProgressStage {
  name: string;
  label: string;
  status: string;
  plain_english: string;
  updated_at?: string;
  error?: string;
}

interface ProgressPayload {
  project_id: string;
  status: string;
  friendly_message: string;
  action_required: string;
  current_stage: ProgressStage;
  next_stage: ProgressStage | null;
  progress_percent: number;
  total_steps: number;
  completed_steps: number;
  running_steps: number;
  failed_steps: number;
  review_url: string;
  ghl_export_url: string;
}

const statusLabels: Record<string, string> = {
  completed: "Done",
  running: "Happening now",
  failed: "Needs attention",
  interrupted: "Paused",
  queued: "Queued",
  pending: "Waiting",
};

const statusIcon: Record<string, string> = {
  completed: "✓",
  running: "•",
  failed: "!",
  interrupted: "‖",
  queued: "…",
  pending: "•",
};

const progressRoot = document.querySelector<HTMLElement>("#progress-root");

const escapeHtml = (value: string): string => {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
};

const formatStageCard = (stage: ProgressStage): string => {
  const status = statusLabels[stage.status] || stage.status;
  const icon = statusIcon[stage.status] || "•";
  const note = stage.error ? `<p class="stage-note error">${escapeHtml(stage.error)}</p>` : "";
  return `
    <article class="stage-card" data-status="${escapeHtml(stage.status)}">
      <span class="stage-icon" aria-hidden="true">${icon}</span>
      <div>
        <h3>${escapeHtml(stage.label)}</h3>
        <p>${escapeHtml(stage.plain_english)}</p>
        ${note}
        <span class="stage-status">${escapeHtml(status)}</span>
      </div>
    </article>
  `;
};

const copyGhlCode = async (projectId: string, button: HTMLButtonElement): Promise<void> => {
  const originalLabel = button.textContent;
  button.textContent = "Copying…";
  button.disabled = true;
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/ghl-export`);
    if (!response.ok) throw new Error("GHL export request failed");
    const payload = (await response.json()) as { ghl_html?: string };
    const code = payload.ghl_html ?? "";
    if (!code) throw new Error("GHL code is empty");
    await navigator.clipboard.writeText(code);
    button.textContent = "Copied!";
  } catch {
    button.textContent = "Copy failed";
  } finally {
    setTimeout(() => {
      button.textContent = originalLabel;
      button.disabled = false;
    }, 2200);
  }
};

const renderProgress = (payload: ProgressPayload): void => {
  if (!progressRoot) return;
  const percent = Math.max(0, Math.min(100, payload.progress_percent));
  const next =
    payload.next_stage === null
      ? `<p class="next-step muted">This is the final step.</p>`
      : `<p class="next-step"><strong>Next:</strong> ${escapeHtml(payload.next_stage.plain_english)}</p>`;

  progressRoot.innerHTML = `
    <section class="progress-hero" aria-live="polite">
      <p class="hero-kicker">What’s happening now</p>
      <h1>${escapeHtml(payload.friendly_message)}</h1>
      <p class="action-required">${escapeHtml(payload.action_required)}</p>
      <div class="progress-bar" role="progressbar" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100">
        <span style="width:${percent}%"></span>
      </div>
      <p class="progress-meta">${payload.completed_steps} of ${payload.total_steps} steps done · ${percent}%</p>
      ${next}
      <div class="hero-actions">
        <a class="primary-button" href="${escapeHtml(payload.review_url)}">Open review cockpit</a>
        <button id="copy-ghl" class="secondary-button" type="button">Copy GHL code</button>
      </div>
    </section>
    <section class="current-stage">
      <p class="section-kicker">Current step</p>
      ${formatStageCard(payload.current_stage)}
    </section>
  `;

  const copyButton = document.querySelector<HTMLButtonElement>("#copy-ghl");
  if (copyButton) {
    copyButton.addEventListener("click", () => {
      void copyGhlCode(payload.project_id, copyButton);
    });
  }
};

let shouldContinuePolling = true;

const loadProgress = (): void => {
  if (!progressRoot) return;
  const projectId = progressRoot.dataset.projectId || "";
  if (!projectId) return;
  fetch(`/api/projects/${encodeURIComponent(projectId)}/progress`)
    .then((response) => {
      if (!response.ok) throw new Error("Progress request failed");
      return response.json() as Promise<ProgressPayload>;
    })
    .then((payload) => {
      renderProgress(payload);
      if (payload.status === "approved") {
        shouldContinuePolling = false;
      }
    })
    .catch(() => {
      if (progressRoot) {
        progressRoot.innerHTML = `
          <section class="progress-hero error" role="alert">
            <p class="hero-kicker">Status unavailable</p>
            <h1>We could not load the live status right now.</h1>
            <p class="action-required">Please refresh the page in a few seconds.</p>
          </section>
        `;
      }
    });
};

loadProgress();
setInterval(() => {
  if (shouldContinuePolling) loadProgress();
}, 5000);
