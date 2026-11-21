"""Non-technical, plain-English progress descriptions for the operator frontend."""

from __future__ import annotations

from typing import Any

STAGE_LABELS: dict[str, str] = {
    "collect": "Visit the website & save photos",
    "extract": "Find and verify business facts",
    "social": "Collect authorized social media",
    "curate": "Pick the best genuine photos",
    "art_director": "Decide the look and layout",
    "generate": "Build the web page",
    "claims_lint": "Check every claim is backed by evidence",
    "qa": "Test on screen sizes",
    "critique": "Review how it actually looks",
    "revise": "Fix only the flagged sections",
    "ghl_exporter": "Prepare the GHL snippet",
}

STAGE_ORDER = list(STAGE_LABELS)

STAGE_EXPLANATIONS: dict[str, str] = {
    "collect": "We are reading your website and saving the real photos we find.",
    "extract": "We are checking your business details so nothing is invented.",
    "social": "We are looking at your public social media photos, if you gave permission.",
    "curate": "We are choosing the best real photos for your homepage.",
    "art_director": "We are deciding how your homepage should look and feel.",
    "generate": "We are building your homepage code.",
    "claims_lint": "We are making sure every sentence is backed by real evidence.",
    "qa": "We are testing your homepage on phone, tablet, and computer.",
    "critique": "We are reviewing how your homepage actually looks and reads.",
    "revise": "We are fixing only the parts that need to change.",
    "ghl_exporter": "We are preparing the code you will paste into GoHighLevel.",
}

PROJECT_STATUS_MESSAGES: dict[str, tuple[str, str]] = {
    "running": (
        "We are building your website right now.",
        "You do not need to do anything yet. We will tell you when we need you.",
    ),
    "awaiting_approval": (
        "Your website is ready for your review.",
        "Please open the review cockpit and approve it or tell us what to change.",
    ),
    "approved": (
        "Your website is ready to publish.",
        "You can copy the GHL code and paste it into GoHighLevel.",
    ),
    "needs_review": (
        "We need your help to make a decision.",
        "Please open the review cockpit and tell us what to do next.",
    ),
    "failed": (
        "We ran into a problem and stopped.",
        "Please reopen the project and start the build again.",
    ),
    "interrupted": (
        "Your build is paused.",
        "Please reopen the project and resume the build.",
    ),
}

_DEFAULT_STATUS_MESSAGE = (
    "We are preparing your website.",
    "You do not need to do anything yet. We will tell you when we need you.",
)


def _stage_from_status(project_status: str) -> str:
    if project_status == "approved":
        return "ghl_exporter"
    if project_status in {"awaiting_approval", "needs_review", "failed", "interrupted"}:
        return "critique"
    return "collect"


def _default_stage(stage_name: str, status: str = "pending") -> dict[str, Any]:
    return {
        "name": stage_name,
        "label": STAGE_LABELS.get(stage_name, "Working"),
        "status": status,
        "plain_english": STAGE_EXPLANATIONS.get(stage_name, "We are working on your website."),
        "updated_at": None,
        "error": None,
    }


def _pick_current_stage(stages: list[dict[str, Any]], project_status: str) -> dict[str, Any]:
    if not stages:
        fallback_status = "running" if project_status == "running" else "completed"
        return _default_stage(_stage_from_status(project_status), fallback_status)

    running = next((s for s in stages if s["status"] == "running"), None)
    if running:
        return running

    failed = next(
        (s for s in stages if s["status"] in {"failed", "interrupted"}), None
    )
    if failed:
        return failed

    completed = [s for s in stages if s["status"] == "completed"]
    if completed:
        return completed[-1]

    return stages[0]


def _next_stage(current: dict[str, Any]) -> dict[str, Any] | None:
    names = list(STAGE_ORDER)
    try:
        idx = names.index(current["name"])
    except ValueError:
        return None
    if idx + 1 >= len(names):
        return None
    return _default_stage(names[idx + 1], "pending")


def build_progress_payload(
    project_id: str,
    project_status: str,
    stage_timeline: dict[str, Any],
) -> dict[str, Any]:
    """Return a non-technical progress payload for the frontend."""
    stages = list(stage_timeline.get("stages", []))
    current = _pick_current_stage(stages, project_status)
    next_stage = _next_stage(current)

    friendly_message, action_required = PROJECT_STATUS_MESSAGES.get(
        project_status, _DEFAULT_STATUS_MESSAGE
    )

    total_steps = len(stages) if stages else len(STAGE_ORDER)
    completed_steps = sum(1 for s in stages if s["status"] == "completed")
    running_steps = sum(1 for s in stages if s["status"] == "running")
    failed_steps = sum(1 for s in stages if s["status"] in {"failed", "interrupted"})
    progress_percent = 100 if not stages and project_status == "approved" else 0
    if stages:
        progress_percent = int(round(completed_steps / max(1, len(stages)) * 100))

    return {
        "project_id": project_id,
        "status": project_status,
        "friendly_message": friendly_message,
        "action_required": action_required,
        "current_stage": current,
        "next_stage": next_stage,
        "progress_percent": progress_percent,
        "total_steps": total_steps,
        "completed_steps": completed_steps,
        "running_steps": running_steps,
        "failed_steps": failed_steps,
        "review_url": f"/review/{project_id}",
        "ghl_export_url": f"/api/projects/{project_id}/ghl-export",
    }
