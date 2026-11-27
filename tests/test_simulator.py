import json

import httpx

from sim.testing import SimulatorFixture


def test_model_wire_examples(simulator: SimulatorFixture) -> None:
    with httpx.Client(base_url=simulator.url, trust_env=False) as client:
        headers = {"Authorization": f"Bearer {simulator.key}"}
        response = client.post(
            "/v1/embeddings",
            headers=headers,
            json={"model": "harbour-embed-1", "input": ["refunds", "settlement"]},
        )
        assert [item["index"] for item in response.json()["data"]] == [1, 0]
        response = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "harbour-answer-1",
                "messages": [
                    {"role": "system", "content": "Quote supplied evidence."},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "question": "When do refunds expire?",
                                "sources": [{"id": "s1", "text": "Refunds expire after 30 days."}],
                            }
                        ),
                    },
                ],
            },
        )
        quote = json.loads(response.json()["choices"][0]["message"]["content"])["quotes"][0]
        assert quote == {"source_id": "s1", "quote": "Refunds expire after 30 days."}


def test_fault_controls_and_identical_retry(simulator: SimulatorFixture) -> None:
    simulator.flags("rate_limit")
    payload = {"model": "harbour-embed-1", "input": ["refunds"]}
    with httpx.Client(base_url=simulator.url, trust_env=False) as client:
        headers = {"Authorization": f"Bearer {simulator.key}"}
        assert client.post("/v1/embeddings", json=payload, headers=headers).status_code == 429
        assert client.post("/v1/embeddings", json=payload, headers=headers).status_code == 200
        simulator.reset()
        assert client.post("/v1/embeddings", json=payload, headers=headers).status_code == 429
    simulator.flags()
    assert simulator.stats()["faults_injected"]["rate_limit"] == 1
