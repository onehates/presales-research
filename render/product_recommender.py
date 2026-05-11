#!/usr/bin/env python3
"""Generates recommended_products section by matching brief signals to product catalog."""

import json
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRODUCTS_PATH = PROJECT_ROOT / "persona" / "verkada-products.yml"


def load_catalog() -> list[dict]:
    """Load the Verkada product catalog."""
    with open(PRODUCTS_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("products", [])


def recommend_products(brief: dict) -> dict:
    """Generate recommended_products section from brief data + product catalog.

    Scoring logic:
    1. Filter products whose target_verticals includes the brief's vertical
    2. Score each product by overlap of pain_solved with brief's pain_hypotheses[].linked_persona_pain
    3. Bonus for compliance match (NDAA if federal funding detected)
    4. Pick top 3 as primary_bundle, next 3 as secondary_bundle
    """
    catalog = load_catalog()
    if not catalog:
        return {"primary_bundle": [], "secondary_bundle": [], "vertical_fit_notes": "Product catalog not available"}

    # Extract brief signals
    vertical = brief.get("snapshot", {}).get("vertical", "")
    if not vertical:
        vertical = brief.get("vertical_match", {}).get("matched_vertical", "")

    # Get pain hypothesis IDs
    pain_ids = set()
    pains = brief.get("pain_hypotheses", [])
    if isinstance(pains, list):
        for p in pains:
            if isinstance(p, dict):
                linked = p.get("linked_persona_pain", "")
                if linked:
                    pain_ids.add(linked)

    # Check for federal funding (boosts NDAA/FIPS products)
    has_federal_funding = bool(brief.get("federal_funding_profile"))

    # Get entity type for context
    entity_type = brief.get("entity_type", "")
    company_name = brief.get("snapshot", {}).get("name", "")
    size_indicator = brief.get("snapshot", {}).get("size_indicator", "")

    # Score each product
    scored = []
    for product in catalog:
        # Vertical filter: product must target this vertical
        target_verticals = product.get("target_verticals", [])
        if vertical and vertical not in target_verticals:
            continue

        # Pain overlap score
        product_pains = set(product.get("pain_solved", []))
        overlap = pain_ids & product_pains
        pain_score = len(overlap) / max(len(product_pains), 1)

        # Compliance bonus
        compliance_bonus = 0
        compliance = product.get("compliance", {})
        if has_federal_funding and compliance.get("ndaa"):
            compliance_bonus += 0.15
        if has_federal_funding and compliance.get("fips"):
            compliance_bonus += 0.05

        # Category diversity bonus (prefer a mix of categories)
        total_score = pain_score + compliance_bonus

        # Skip products with zero relevance
        if total_score <= 0 and not overlap:
            continue

        scored.append({
            "product": product,
            "score": total_score,
            "matched_pains": list(overlap),
            "pain_score": pain_score,
        })

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)

    # Ensure category diversity: pick top across different categories
    primary = []
    secondary = []
    seen_categories = set()

    # First pass: pick best per category for primary
    for item in scored:
        cat = item["product"]["category"]
        if cat not in seen_categories and len(primary) < 3:
            primary.append(item)
            seen_categories.add(cat)

    # If we don't have 3 primary, fill from remaining
    for item in scored:
        if item not in primary and len(primary) < 3:
            primary.append(item)

    # Secondary: next best products not in primary
    for item in scored:
        if item not in primary and len(secondary) < 3:
            secondary.append(item)

    def format_product(item: dict) -> dict:
        p = item["product"]
        matched_triggers = item["matched_pains"]

        # Build reasoning from matched pains and brief context
        pain_descriptions = []
        if isinstance(pains, list):
            for ph in pains:
                if isinstance(ph, dict) and ph.get("linked_persona_pain") in matched_triggers:
                    # Use first ~80 chars of hypothesis
                    hyp = ph.get("hypothesis", "")
                    if hyp:
                        pain_descriptions.append(hyp[:120])

        reasoning_parts = []
        if pain_descriptions:
            reasoning_parts.append(pain_descriptions[0])
        if not reasoning_parts:
            reasoning_parts.append(f"Matches {vertical} vertical requirements")

        # Build scope estimate from size indicator
        scope = ""
        if size_indicator:
            scope = f"Deployment across {size_indicator}"

        return {
            "product_id": p["id"],
            "name": p["name"],
            "category": p["category"],
            "line": p.get("line", ""),
            "tagline": p.get("tagline", ""),
            "description": p.get("description", ""),
            "key_features": p.get("key_features", [])[:4],
            "reasoning": " — ".join(reasoning_parts),
            "confidence": "high" if item["pain_score"] >= 0.3 else "medium",
            "matched_triggers": matched_triggers[:3],
            "estimated_scope": scope,
            "image_url": p.get("image_url", ""),
            "local_image": p.get("local_image", ""),
            "datasheet_url": p.get("datasheet_url", ""),
            "compliance": p.get("compliance", {}),
            "typical_deployment": p.get("typical_deployment", []),
        }

    # Generate vertical fit notes
    vertical_notes_parts = []
    categories_recommended = set()
    for item in primary + secondary:
        categories_recommended.add(item["product"]["category"])

    cat_names = {
        "cameras": "cameras (D-Series)",
        "access_control": "access control",
        "alarms": "alarms (BR-Series)",
        "sensors": "environmental sensors (SV-Series)",
        "intercoms": "intercoms (TD-Series)",
        "software": "software",
    }
    cat_list = ", ".join(cat_names.get(c, c) for c in sorted(categories_recommended) if c != "software")

    if vertical and cat_list:
        notes = f"{vertical} deployments typically include: {cat_list}"
        if has_federal_funding:
            notes += " — all NDAA-compliant for federal funding protection"
        vertical_notes_parts.append(notes)

    return {
        "primary_bundle": [format_product(item) for item in primary],
        "secondary_bundle": [format_product(item) for item in secondary],
        "vertical_fit_notes": ". ".join(vertical_notes_parts) if vertical_notes_parts else "",
    }


def inject_into_brief(brief_path: Path) -> bool:
    """Read a brief JSON, add recommended_products, write back."""
    with open(brief_path) as f:
        brief = json.load(f)

    if not isinstance(brief, dict) or not brief.get("snapshot"):
        return False

    brief["recommended_products"] = recommend_products(brief)

    with open(brief_path, "w") as f:
        json.dump(brief, f, indent=2, default=str)

    return True


def main():
    if len(sys.argv) < 2:
        # Process most recent brief
        briefs_dir = PROJECT_ROOT / "briefs"
        jsons = sorted(
            [p for p in briefs_dir.glob("*.json") if ".failed." not in p.name and ".meta." not in p.name and ".raw." not in p.name],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not jsons:
            print("No brief JSON files found", file=sys.stderr)
            sys.exit(1)
        brief_path = jsons[0]
    else:
        brief_path = Path(sys.argv[1])

    if not brief_path.exists():
        print(f"File not found: {brief_path}", file=sys.stderr)
        sys.exit(1)

    if inject_into_brief(brief_path):
        print(f"Injected recommended_products into {brief_path.name}")
    else:
        print(f"Failed to process {brief_path.name}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
