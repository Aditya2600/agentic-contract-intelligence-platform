"""Who is allowed to decide.

The model and the automation around it propose: facts, derivations, findings, review
items. All of it lands as `pending`, and the only writes that move an item out of
`pending` are the ones this module lets through.

Identity here is not a claim the caller makes. Until this existed, a decision carried
whatever `actor_id` was in the request body, so `decided_by` recorded an assertion rather
than an authenticated party -- anything that could reach the API could approve its own
proposals under any name it liked. Now the actor is resolved from the presented
credential and stamped onto the resume payload, and the graph gates refuse a payload that
was never stamped. The body cannot name a reviewer it did not authenticate as.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from doctask.config import settings

REVIEWER = "reviewer"  # an authenticated human
SERVICE = "service"  # the model, the ingestion automation, anything that is not a person


class AuthenticationError(Exception):
    """No usable credential was presented."""


class AuthorizationError(Exception):
    """The credential is valid, but this role may not do this."""


@dataclass(frozen=True, slots=True)
class Principal:
    actor_id: str
    role: str

    @property
    def is_reviewer(self) -> bool:
        return self.role == REVIEWER

    def as_payload(self) -> dict[str, str]:
        """The identity a graph gate reads back out of a resume payload."""
        return {"actor_id": self.actor_id, "actor_role": self.role}


def _credentials(raw: str, role: str) -> list[tuple[str, Principal]]:
    """Parse `token:actor_id, token:actor_id` into credentials for one role."""
    parsed: list[tuple[str, Principal]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        token, _, actor_id = entry.partition(":")
        if not token or not actor_id:
            raise RuntimeError("credentials must be written as token:actor_id, comma separated")
        parsed.append((token, Principal(actor_id=actor_id, role=role)))
    return parsed


def authenticate(token: str | None) -> Principal:
    """Resolve a bearer token to the party presenting it.

    Fails closed. A deployment with no tokens configured authenticates nobody rather
    than everybody, so forgetting to set them cannot quietly leave the gate open.
    """
    known = _credentials(settings.reviewer_tokens, REVIEWER) + _credentials(
        settings.service_tokens, SERVICE
    )
    match: Principal | None = None
    for candidate, principal in known:
        # compare_digest so a wrong token cannot be recovered from how long the
        # comparison ran. Every credential is checked even after a match, so the
        # work done does not reveal the position of the matching entry either.
        if token is not None and secrets.compare_digest(token.encode(), candidate.encode()):
            match = principal
    if match is None:
        raise AuthenticationError("missing or unknown credential")
    return match


def require_reviewer(principal: Principal) -> Principal:
    """Approving, rejecting, and overriding a blocker are human acts, by definition."""
    if not principal.is_reviewer:
        raise AuthorizationError(
            f"{principal.actor_id} is a {principal.role}: only an authenticated reviewer "
            "can approve, reject, or override a blocker"
        )
    return principal


def require_service(principal: Principal) -> Principal:
    """The other half of the separation, enforced from the automation's side.

    Ingestion automation -- the watcher -- must run as something that *cannot* decide.
    Handing it a reviewer credential would give a background process the standing of a
    human, and every run it started would be attributable to a person who never saw the
    document. So a reviewer token is refused here rather than quietly accepted as "more
    than enough permission".
    """
    if principal.is_reviewer:
        raise AuthorizationError(
            f"{principal.actor_id} is a {principal.role}: automation must present a "
            "service credential, so nothing it starts can be recorded as a human act"
        )
    return principal


def reviewer_from_payload(payload: dict[str, Any]) -> Principal:
    """The reviewer a resume payload was stamped with, or an error.

    The graph gates call this instead of reading `actor_id` out of the payload. A
    payload that reached the graph without passing through `authenticate` carries no
    role and is refused, so no path -- including one the model could influence -- can
    record a decision as an anonymous human.
    """
    actor_id = payload.get("actor_id")
    role = payload.get("actor_role")
    if not actor_id or not role:
        raise AuthorizationError("decision payload carries no authenticated actor")
    return require_reviewer(Principal(actor_id=str(actor_id), role=str(role)))
