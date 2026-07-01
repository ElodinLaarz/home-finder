"""Preference scoring — ranks and annotates, NEVER rejects.

Each new listing is scored against the personal rubric by a vision-capable
Claude model using structured outputs (output_config.format json_schema), so
the response is guaranteed-valid JSON. A scoring failure or a low score sinks
a listing to the bottom of the digest; it never removes it.

Input discipline: RentCast provides no photos or description, so the model
sees the structured listing facts, the measured commute, price history, and
(optionally) one small Street View image for street context. Cost on
claude-haiku-4-5 is well under a cent per listing.
"""

from __future__ import annotations

import base64
import json
import logging

import anthropic

from .config import RubricCriterion, ScoringConfig
from .models import CriterionScore, Listing, ListingScore

log = logging.getLogger(__name__)

MAX_TOKENS = 2000


class ScoringError(RuntimeError):
    pass


def build_schema(rubric: list[RubricCriterion]) -> dict:
    # Structured-output schemas can't use numeric min/max constraints;
    # scores are clamped to 0-10 in parse_score below instead.
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["criteria", "summary"],
        "properties": {
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "score", "rationale"],
                    "properties": {
                        "key": {"type": "string", "enum": [c.key for c in rubric]},
                        "score": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                },
            },
            "summary": {"type": "string"},
        },
    }


def build_prompt(
    listing: Listing,
    rubric: list[RubricCriterion],
    commute_minutes: float | None,
    has_image: bool,
) -> str:
    facts = {
        "address": listing.address,
        "price": listing.price,
        "beds": listing.beds,
        "baths": listing.baths,
        "sqft": listing.sqft,
        "lot_sqft": listing.lot_sqft,
        "year_built": listing.year_built,
        "hoa_monthly_fee": listing.hoa_fee,
        "days_on_market": listing.days_on_market,
        "listed_date": listing.listed_date,
        "listing_type": listing.raw.get("listingType"),
        "price_and_listing_history": listing.raw.get("history"),
    }
    rubric_text = "\n".join(
        f"- {c.key} (weight {c.weight}): {c.description.strip()}" for c in rubric
    )
    commute_text = (
        f"{commute_minutes} minutes (traffic-aware prediction at the buyer's departure time)"
        if commute_minutes is not None
        else "could not be measured"
    )
    image_note = (
        "Image 1 is a Google Street View photo of the address. Use it only for "
        "street context (road type, what the house faces, power lines, curb "
        "appeal). It may be outdated or misaimed — never let it dominate a "
        "criterion the data also speaks to.\n\n"
        if has_image
        else ""
    )
    return (
        "You are ranking a for-sale single-family house against a buyer's "
        "personal rubric. Your scores only ORDER listings for a human who will "
        "review every one of them — you are not filtering.\n\n"
        f"{image_note}"
        "Listing data (from an aggregator; no MLS photos or description exist):\n"
        f"{json.dumps(facts, indent=2, default=str)}\n\n"
        f"Commute to the buyer's fixed destination: {commute_text}\n\n"
        "Rubric — score EVERY criterion from 0 (bad) to 10 (ideal), with a "
        "one-line rationale grounded in the evidence above. When the evidence "
        "doesn't speak to a criterion, score it 5 and say the evidence is "
        "missing rather than guessing.\n"
        f"{rubric_text}\n\n"
        "Also write a two-sentence overall summary a buyer can skim."
    )


def parse_score(
    data: dict,
    rubric: list[RubricCriterion],
    listing_id: str,
    model: str,
    scored_at: str,
) -> ListingScore:
    """Turn the (schema-validated) model response into a ListingScore.

    Clamps scores to 0-10, keeps the first entry per criterion, and fills any
    criterion the model skipped with a neutral 5 so weighting stays honest.
    """
    by_key: dict[str, CriterionScore] = {}
    for entry in data.get("criteria", []):
        key = entry["key"]
        if key in by_key:
            continue
        by_key[key] = CriterionScore(
            key=key,
            score=max(0.0, min(10.0, float(entry["score"]))),
            rationale=str(entry["rationale"]),
        )
    criteria = [
        by_key.get(
            c.key,
            CriterionScore(key=c.key, score=5.0, rationale="not scored by model"),
        )
        for c in rubric
    ]
    total_weight = sum(c.weight for c in rubric) or 1.0
    overall = sum(
        crit.score * spec.weight for crit, spec in zip(criteria, rubric)
    ) / total_weight
    return ListingScore(
        listing_id=listing_id,
        overall=round(overall, 2),
        criteria=criteria,
        summary=str(data.get("summary", "")),
        model=model,
        scored_at=scored_at,
    )


def score_listing(
    llm: anthropic.Anthropic,
    cfg: ScoringConfig,
    listing: Listing,
    commute_minutes: float | None,
    image_jpeg: bytes | None,
    scored_at: str,
) -> ListingScore:
    content: list[dict] = []
    if image_jpeg:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(image_jpeg).decode("ascii"),
                },
            }
        )
    content.append(
        {
            "type": "text",
            "text": build_prompt(listing, cfg.rubric, commute_minutes, bool(image_jpeg)),
        }
    )

    response = llm.messages.create(
        model=cfg.model,
        max_tokens=MAX_TOKENS,
        output_config={"format": {"type": "json_schema", "schema": build_schema(cfg.rubric)}},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason not in ("end_turn", "stop_sequence"):
        raise ScoringError(
            f"scoring stopped with stop_reason={response.stop_reason} for {listing.id}"
        )
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ScoringError(f"no text block in scoring response for {listing.id}")
    return parse_score(json.loads(text), cfg.rubric, listing.id, cfg.model, scored_at)
