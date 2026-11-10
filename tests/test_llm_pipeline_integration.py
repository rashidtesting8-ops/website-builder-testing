"""Tests for integrating the hardened LLM adapter into art direction and visual critique stages."""

from __future__ import annotations

import json
from pathlib import Path

from app.art_director import generate_art_direction
from app.visual_critic import run_visual_critic
from PIL import Image
from src.engine.llm.adapter import (
    AnthropicProvider,
    BudgetConfig,
    FakeProvider,
    GeminiProvider,
    LLMAdapter,
    LLMResponse,
    OpenAIProvider,
    get_default_adapter,
)


def _scaffold_minimal_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "pup-luxury-spa"
    (project_root / "research").mkdir(parents=True, exist_ok=True)
    (project_root / "design").mkdir(parents=True, exist_ok=True)
    (project_root / "website").mkdir(parents=True, exist_ok=True)
    (project_root / "qa" / "screenshots").mkdir(parents=True, exist_ok=True)

    facts = {
        "project_id": "pup-luxury-spa",
        "facts": [
            {
                "fact_id": "f1",
                "category": "business_name",
                "claim_key": "business_name",
                "claim_value": "Pup Luxury Spa",
                "source_text_snippet": "Pup Luxury Spa",
                "is_verified": True,
            },
            {
                "fact_id": "f2",
                "category": "pricing",
                "claim_key": "bath_and_brush",
                "claim_value": "$65.00",
                "source_text_snippet": "Bath & Brush - $65.00",
                "is_verified": True,
            },
            {
                "fact_id": "f3",
                "category": "phone",
                "claim_key": "phone",
                "claim_value": "(512) 555-9876",
                "source_text_snippet": "(512) 555-9876",
                "is_verified": True,
            },
        ],
    }
    (project_root / "research" / "business-facts.json").write_text(
        json.dumps(facts, indent=2), encoding="utf-8"
    )

    selection = {
        "project_id": "pup-luxury-spa",
        "hero_image": {"filename": "hero_dog.jpg", "object_position": "50% 25%"},
        "grooming_gallery": [{"filename": "gal1.jpg", "object_position": "50% 30%"}],
    }
    (project_root / "design" / "image-selection.json").write_text(
        json.dumps(selection, indent=2), encoding="utf-8"
    )

    # Synthetic 1440px screenshot fixture
    img = Image.new("RGB", (1440, 900), color=(240, 240, 245))
    img.save(project_root / "qa" / "screenshots" / "1440px.png")

    html = (
        '<!DOCTYPE html><html><body>'
        '<header data-section="header"><h1>Pup Luxury Spa</h1></header>'
        '<main>'
        '<section id="hero" data-section="hero">'
        '<h1>Pup Luxury Spa</h1>'
        '<p>Gentle pet grooming.</p>'
        '<a class="cta btn" href="#contact">Book Now</a>'
        '</section>'
        '<section id="services" data-section="services">'
        '<h2>Services</h2>'
        '<div style="background-color: #f5f5dc;">Bath & Brush $65</div>'
        '</section>'
        '</main>'
        '<footer data-section="footer"><p>Copyright Pup Luxury Spa</p></footer>'
        '</body></html>'
    )
    (project_root / "website" / "index.html").write_text(html, encoding="utf-8")
    return project_root


def test_art_director_with_llm_adapter_generates_editorial_brief_and_records_ledger(
    tmp_path: Path,
) -> None:
    project_root = _scaffold_minimal_project(tmp_path)

    llm_payload = {
        "headline": "Bespoke Canine Wellness & Gentle Spa Care",
        "tagline": "Stress-free grooming tailored to your companion's coat.",
        "value_proposition": "Master stylists using organic shampoos in a serene private setting.",
        "suggested_section_order": ["hero", "services", "gallery", "contact"],
    }
    canned = LLMResponse(
        content=json.dumps(llm_payload),
        prompt_tokens=120,
        completion_tokens=65,
        total_tokens=185,
        model="fake-llm-v1",
    )
    provider = FakeProvider(canned_responses=[canned])
    config = BudgetConfig(cache_dir=tmp_path / ".cache" / "llm")
    adapter = LLMAdapter(provider, config=config)

    brand, brief = generate_art_direction(project_root, llm_adapter=adapter)

    # 1. Brief must contain editorial copy from LLM
    assert brief.editorial_headline == "Bespoke Canine Wellness & Gentle Spa Care"
    assert brief.editorial_tagline == "Stress-free grooming tailored to your companion's coat."
    assert "Master stylists" in str(brief.editorial_value_prop)

    # 2. Persisted JSON on disk must include editorial fields
    brief_disk = json.loads(
        (project_root / "design" / "creative-brief.json").read_text(encoding="utf-8")
    )
    assert brief_disk["editorial_headline"] == "Bespoke Canine Wellness & Gentle Spa Care"

    # 3. Ledger must record the call under project_id and stage='art_direction'
    ledger_file = config.ledger_path
    assert ledger_file.is_file()
    ledger_lines = [
        json.loads(line)
        for line in ledger_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        entry["project_id"] == "pup-luxury-spa" and entry["stage"] == "art_direction"
        for entry in ledger_lines
    )


def test_visual_critic_with_multimodal_llm_adapter_detects_defects_from_screenshots(
    tmp_path: Path,
) -> None:
    project_root = _scaffold_minimal_project(tmp_path)

    llm_critique_payload = {
        "status": "defects_found",
        "defects": [
            {
                "target_section_id": "services",
                "screenshot_path": "qa/screenshots/1440px.png",
                "defect_type": "repetitive_beige_boxes",
                "actionable_instruction": (
                    "Replace flat beige #f5f5dc card backgrounds with clean surface contrast "
                    "and subtle borders."
                ),
            }
        ],
    }
    canned = LLMResponse(
        content=json.dumps(llm_critique_payload),
        prompt_tokens=250,
        completion_tokens=90,
        total_tokens=340,
        model="fake-vision-v1",
    )
    provider = FakeProvider(canned_responses=[canned])
    config = BudgetConfig(cache_dir=tmp_path / ".cache" / "llm")
    adapter = LLMAdapter(provider, config=config)

    report = run_visual_critic(project_root, llm_adapter=adapter)

    assert report.status == "defects_found"
    assert any(d.defect_type == "repetitive_beige_boxes" for d in report.defects)
    assert any("Replace flat beige" in d.actionable_instruction for d in report.defects)

    # Verify call passed the 1440px screenshot as image input
    assert provider.call_count == 1
    assert provider.recorded_calls[0]["images_count"] >= 1
    assert provider.recorded_calls[0]["extra"].get("stage") == "visual_critic"


def test_concrete_providers_build_payloads_and_headers_without_network(
    monkeypatch,
) -> None:
    # 1. Anthropic Provider formatting
    anthropic = AnthropicProvider(api_key="sk-ant-test-dummy-key")
    payload = anthropic._build_request_payload(
        prompt="Critique this layout",
        system_prompt="You are a design critic",
        images=[],
        model="claude-3-5-sonnet-20241022",
        temperature=0.2,
        max_tokens=1000,
    )
    assert payload["model"] == "claude-3-5-sonnet-20241022"
    assert payload["system"] == "You are a design critic"
    headers = anthropic._build_headers()
    assert headers["x-api-key"] == "sk-ant-test-dummy-key"
    assert headers["anthropic-version"] == "2023-06-01"

    # 2. OpenAI Provider formatting
    openai = OpenAIProvider(api_key="sk-openai-test-dummy-key")
    oai_payload = openai._build_request_payload(
        prompt="Hello world",
        system_prompt="System instructions",
        images=[],
        model="gpt-4o",
        temperature=0.3,
        max_tokens=800,
    )
    assert oai_payload["model"] == "gpt-4o"
    assert len(oai_payload["messages"]) == 2
    oai_headers = openai._build_headers()
    assert oai_headers["Authorization"] == "Bearer sk-openai-test-dummy-key"

    # 3. Gemini Provider formatting
    gemini = GeminiProvider(api_key="gemini-test-key")
    gem_payload = gemini._build_request_payload(
        prompt="Analyze facts",
        system_prompt="Fact checker",
        images=[],
        temperature=0.0,
        max_tokens=500,
    )
    assert "contents" in gem_payload
    assert "systemInstruction" in gem_payload
    url = gemini._build_url("gemini-1.5-flash")
    assert "gemini-1.5-flash:generateContent" in url
    assert "key=gemini-test-key" in url


def test_get_default_adapter_resolves_provider_from_env(monkeypatch, tmp_path: Path) -> None:
    # When no key is set, returns adapter with FakeProvider
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    adapter = get_default_adapter(config=BudgetConfig(cache_dir=tmp_path / ".cache" / "llm"))
    assert isinstance(adapter.provider, FakeProvider)

    # When ANTHROPIC_API_KEY is set, returns AnthropicProvider
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-12345")
    adapter_ant = get_default_adapter(config=BudgetConfig(cache_dir=tmp_path / ".cache" / "llm"))
    assert isinstance(adapter_ant.provider, AnthropicProvider)


def test_pipeline_e2e_with_llm_adapter_executes_orchestration(tmp_path: Path) -> None:
    from app.pipeline import run_pipeline
    from tests.test_pipeline_e2e import _ephemeral_grooming_salon_server
    from tests.test_pipeline_lifecycle import _fast_qa_runner

    editorial_payload = {
        "editorial_headline": "Holistic Spa & Gentle Grooming For Every Pup",
        "editorial_tagline": "Stress-free pampering in downtown Brooklyn",
        "editorial_value_prop": "Certified master groomers dedicated to patient, loving pet care.",
        "suggested_sections": ["hero", "services", "trust", "contact"],
    }
    critique_payload = {
        "status": "passed",
        "defects": [],
    }
    canned_responses = [
        LLMResponse(
            content=json.dumps(editorial_payload),
            prompt_tokens=180,
            completion_tokens=65,
            total_tokens=245,
            model="fake-art-director",
        ),
        LLMResponse(
            content=json.dumps(critique_payload),
            prompt_tokens=220,
            completion_tokens=40,
            total_tokens=260,
            model="fake-critic",
        ),
    ]
    provider = FakeProvider(canned_responses=canned_responses)
    config = BudgetConfig(
        cache_dir=tmp_path / ".cache" / "llm",
        ledger_path=tmp_path / ".cache" / "llm-ledger.jsonl",
        db_path=tmp_path / ".cache" / "budget.sqlite3",
    )
    adapter = LLMAdapter(provider, config=config)

    with _ephemeral_grooming_salon_server() as base_url:
        result = run_pipeline(
            url=base_url,
            business_name="Furry Tail Spa",
            projects_dir=tmp_path / "projects",
            db_path=tmp_path / "editvista.sqlite3",
            allow_private=True,
            qa_runner=_fast_qa_runner,
            llm_adapter=adapter,
        )

    assert result.status == "awaiting_approval"
    assert result.qa_passed is True

    # Approve release through human approval gate
    from app.rights import confirm_asset_rights

    confirm_asset_rights(
        result.project_folder,
        all_assets=True,
        basis="client_provided",
        operator="Test Rights Reviewer",
        evidence="Synthetic asset used by the LLM pipeline integration test.",
        projects_dir=tmp_path / "projects",
        db_path=tmp_path / "editvista.sqlite3",
    )
    from app.approval import approve_release

    manifest_sha = approve_release(result.project_folder, operator_name="Test QA Dev")
    assert len(manifest_sha) == 64
    assert (result.project_folder / "delivery" / "release-manifest.json").is_file()

    # Verify editorial brief was persisted with LLM copy
    brief_file = tmp_path / "projects" / "furry-tail-spa" / "design" / "creative-brief.json"
    assert brief_file.is_file()
    brief_data = json.loads(brief_file.read_text(encoding="utf-8"))
    assert brief_data["editorial_headline"] == "Holistic Spa & Gentle Grooming For Every Pup"
    assert len(brief_data["suggested_sections"]) == 4

    # Verify provider recorded calls
    assert provider.call_count >= 1
