"""Smoke-check the configured model server: list models, classify, extract one block.

    DOCTASK_LLM=gateway DOCTASK_LLM_API_KEY=... DOCTASK_LLM_MODEL=... \
        python scripts/check_gateway.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx

from doctask.config import settings
from doctask.domain import Block
from doctask.runtime import build_model
from doctask.services.citations import validate_citation
from doctask.services.hashing import sha256_text


async def main() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{settings.llm_base_url.rstrip('/')}/v1/models",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        response.raise_for_status()
        print("models:", [entry["id"] for entry in response.json().get("data", [])])

    model = build_model()
    text = Path("data/sample_data/vendor_msa.txt").read_text()
    print("classify:", await model.classify(text))

    paragraph = next(p.strip() for p in text.split("\n\n") if "Payment" in p)
    block = Block(
        document_id=uuid4(),
        index=0,
        text=paragraph,
        text_sha256=sha256_text(paragraph),
        char_start=0,
        char_end=len(paragraph),
    )
    for candidate in await model.extract(block):
        check = validate_citation(block, candidate)
        print(
            f"{candidate.key}: {candidate.value} "
            f"grounded={check.valid} quote={candidate.quote!r}"
        )
    print("usage:", getattr(model, "last_usage", {}))

    aclose = getattr(model, "aclose", None)
    if aclose is not None:
        await aclose()


if __name__ == "__main__":
    asyncio.run(main())
