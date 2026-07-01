from homefinder.config import RubricCriterion
from homefinder.scoring import build_prompt, build_schema, parse_score
from conftest import make_listing

RUBRIC = [
    RubricCriterion(key="quiet_street", description="quiet street", weight=2.0),
    RubricCriterion(key="value", description="fair price", weight=1.0),
]


def test_schema_shape():
    schema = build_schema(RUBRIC)
    assert schema["additionalProperties"] is False
    keys = schema["properties"]["criteria"]["items"]["properties"]["key"]["enum"]
    assert keys == ["quiet_street", "value"]


def test_parse_score_weighted_overall():
    data = {
        "criteria": [
            {"key": "quiet_street", "score": 8, "rationale": "cul-de-sac"},
            {"key": "value", "score": 5, "rationale": "average $/sqft"},
        ],
        "summary": "nice",
    }
    score = parse_score(data, RUBRIC, "rentcast:x", "claude-haiku-4-5", "t")
    assert score.overall == 7.0  # (8*2 + 5*1) / 3


def test_parse_score_clamps_and_fills_missing():
    data = {
        "criteria": [{"key": "quiet_street", "score": 14, "rationale": "!"}],
        "summary": "",
    }
    score = parse_score(data, RUBRIC, "rentcast:x", "m", "t")
    assert score.criteria[0].score == 10.0
    assert score.criteria[1].score == 5.0
    assert score.criteria[1].rationale == "not scored by model"


def test_prompt_mentions_missing_evidence_rule_and_commute():
    prompt = build_prompt(make_listing(), RUBRIC, 27.5, has_image=True)
    assert "27.5 minutes" in prompt
    assert "score it 5" in prompt
    assert "Street View" in prompt

    prompt = build_prompt(make_listing(), RUBRIC, None, has_image=False)
    assert "could not be measured" in prompt
    assert "Street View" not in prompt
