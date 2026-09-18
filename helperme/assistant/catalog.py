from helperme.runtime.json_values import thaw_value
"""Durable, append-only capability descriptions for the model."""

from dataclasses import asdict
from uuid import uuid4

from helperme.runtime import DomainFactCommitted


CATALOG = "assistant.catalog"


async def sync_catalog(runtime, session_id, surface, skills, clis, management):
    value = {
        "toolsets": sorted(
            (asdict(item) for item in surface.descriptors()), key=lambda x: x["id"]
        ),
        "skills": skills.catalog(),
        "clis": clis.catalog(),
        "management": management.catalog_instruction(session_id),
    }
    events = await runtime.snapshot(session_id)
    previous = [
        e.payload
        for e in events
        if isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == CATALOG
    ]
    if previous and thaw_value(previous[-1].data) == value:
        return
    await runtime.receive_domain_fact(
        session_id,
        CATALOG,
        value,
        source="assistant.catalog",
        delivery_id=uuid4().hex,
        requests_decision=False,
    )
