import json
from pathlib import Path

from harbourline_search.manifest import load_manifest
from harbourline_search.text import prepare_text

ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_and_question_labels() -> None:
    identities = json.loads((ROOT / "identities.json").read_text())
    documents = {item.row.doc_id: item for item in load_manifest(ROOT / "data/round1")}
    questions = json.loads((ROOT / "questions.json").read_text())
    assert len(documents) == 27
    assert len(questions) == 12
    assert len({item["id"] for item in questions}) == 12
    assert sum(item["kind"] == "retrieval" for item in questions) == 8
    for item in questions:
        assert set(item["expected_doc_ids"]) <= documents.keys()
        token = item.get("positive_token", item["token"])
        for doc_id in item["expected_doc_ids"]:
            groups = documents[doc_id].row.groups
            assert isinstance(groups, list)
            assert "public" in groups or set(groups) & set(identities["tokens"][token])


def test_five_invalid_records_and_duplicate_permissions() -> None:
    identities = json.loads((ROOT / "identities.json").read_text())
    valid = {}
    skipped = []
    for item in load_manifest(ROOT / "data/round1"):
        groups = item.row.groups
        if (
            not isinstance(groups, list)
            or not groups
            or any(
                not isinstance(group, str) or group not in identities["groups"] for group in groups
            )
        ):
            skipped.append(item.row.doc_id)
            continue
        try:
            valid[item.row.doc_id] = prepare_text(item.content or b"").text
        except ValueError:
            skipped.append(item.row.doc_id)
    assert len(skipped) == 5
    assert len(valid) == 22
    assert valid["duplicate-public"] == valid["duplicate-risk"]
    assert len(valid["long-reconciliation"]) > 6000


def test_open_rule_documents_are_public_and_not_scored_as_question_hits() -> None:
    documents = {item.row.doc_id: item for item in load_manifest(ROOT / "data/round1")}
    questions = json.loads((ROOT / "questions.json").read_text())
    policy = documents["room-display-policy"]
    runbook = documents["facilities-display-runbook"]
    assert policy.row.groups == runbook.row.groups == ["public"]
    assert policy.content is not None and b"10 minutes" in policy.content
    assert runbook.content is not None and b"30 minutes" in runbook.content
    assert all(
        {"room-display-policy", "facilities-display-runbook"}.isdisjoint(item["expected_doc_ids"])
        for item in questions
    )
