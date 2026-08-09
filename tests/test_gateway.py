"""Offline tests for the production gateway: no network, transport is stubbed.

The point of these is the trust boundary — the model supplies a quote, this code
supplies the offsets, and a quote that is not verbatim must fail validation.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from doctask.domain import Block
from doctask.llm.gateway import ModelGatewayError, OpenAICompatibleGateway
from doctask.services.citations import validate_citation
from doctask.services.hashing import sha256_text

BLOCK_TEXT = "Payment is due within 30 calendar days of receipt."


def _block() -> Block:
    return Block(
        document_id=uuid4(),
        index=0,
        text=BLOCK_TEXT,
        text_sha256=sha256_text(BLOCK_TEXT),
        char_start=0,
        char_end=len(BLOCK_TEXT),
    )


def _gateway(handler) -> OpenAICompatibleGateway:
    gateway = OpenAICompatibleGateway(
        base_url="http://model.invalid:8001", api_key="test-key", model="test-model"
    )
    gateway._client = httpx.AsyncClient(
        base_url="http://model.invalid:8001",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-key"},
    )
    return gateway


def _completion(content: dict, *, tokens_in: int = 11, tokens_out: int = 7) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
        },
    )


async def test_extraction_offsets_come_from_the_source_not_the_model() -> None:
    quote = "Payment is due within 30 calendar days"
    gateway = _gateway(
        lambda request: _completion(
            {"facts": [{"key": "payment_due_days", "value": {"days": 30}, "quote": quote}]}
        )
    )
    block = _block()

    candidates = await gateway.extract(block)

    assert len(candidates) == 1
    candidate = candidates[0]
    # The offsets are the source's, and the span reaches the anchor the model stopped
    # short of: "30 calendar days" of what is the part that makes it an obligation.
    assert candidate.quote.startswith(quote)
    assert candidate.quote.endswith("of receipt.")
    assert candidate.quote_start == 0
    assert block.text[candidate.quote_start : candidate.quote_end] == candidate.quote
    assert validate_citation(block, candidate).ok
    assert gateway.last_usage == {"tokens_in": 11, "tokens_out": 7}


async def test_invented_quote_cannot_pass_validation() -> None:
    gateway = _gateway(
        lambda request: _completion(
            {
                "facts": [
                    {
                        "key": "payment_due_days",
                        "value": {"days": 90},
                        "quote": "Payment is due within 90 calendar days",
                    }
                ]
            }
        )
    )
    block = _block()

    candidate = (await gateway.extract(block))[0]

    assert (candidate.quote_start, candidate.quote_end) == (0, 0)
    assert validate_citation(block, candidate).ok is False


async def test_off_ontology_and_valueless_facts_are_dropped() -> None:
    gateway = _gateway(
        lambda request: _completion(
            {
                "facts": [
                    {"key": "not_in_ontology", "value": {"x": 1}, "quote": BLOCK_TEXT},
                    {"key": "payment_due_days", "value": None, "quote": BLOCK_TEXT},
                    {"key": "payment_due_days", "value": {"days": 30}, "quote": 42},
                ]
            }
        )
    )
    assert await gateway.extract(_block()) == []


async def test_scalar_value_is_kept_without_inventing_field_names() -> None:
    gateway = _gateway(
        lambda request: _completion(
            {"facts": [{"key": "payment_due_days", "value": 30, "quote": BLOCK_TEXT}]}
        )
    )
    assert (await gateway.extract(_block()))[0].value == {"value": 30}


async def test_fenced_json_is_still_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"doc_type": "invoice", '
                            '"confidence": 0.8, "reason": "amount due"}\n```'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )

    doc_type, confidence, _ = await _gateway(handler).classify("INVOICE")
    assert (doc_type, confidence) == ("invoice", 0.8)


async def test_document_text_is_sent_as_delimited_data() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _completion({"doc_type": "master_agreement", "confidence": 0.9, "reason": "title"})

    gateway = _gateway(handler)
    doc_type, confidence, _ = await gateway.classify("MASTER SERVICES AGREEMENT")

    assert (doc_type, confidence) == ("master_agreement", 0.9)
    assert seen["messages"][0]["role"] == "system"
    assert "untrusted source data" in seen["messages"][0]["content"]
    assert "<document>\nMASTER SERVICES AGREEMENT\n</document>" in seen["messages"][1]["content"]
    assert seen["response_format"]["type"] == "json_object"
    assert '"doc_type"' in seen["messages"][1]["content"]  # schema travels in the prompt


async def test_out_of_range_confidence_is_clamped() -> None:
    gateway = _gateway(
        lambda request: _completion({"doc_type": "made_up", "confidence": 7.5, "reason": "x"})
    )
    doc_type, confidence, _ = await gateway.classify("anything")
    assert (doc_type, confidence) == ("unknown", 1.0)


async def test_server_error_is_a_gateway_error() -> None:
    gateway = _gateway(lambda request: httpx.Response(401, json={"error": "Unauthorized"}))
    with pytest.raises(ModelGatewayError):
        await gateway.classify("anything")
