from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from doctask.domain import OBLIGATION_KEYS, FindingCitation, Rule, Ruleset
from doctask.llm.base import RuleExcerpt, RuleVerdict
from doctask.services.hashing import canonical_json, sha256_text

SEVERITIES = {"blocker", "major", "minor", "info"}
SCOPES = {"source", "deliverable", "both"}

# Bumped whenever anything that could change a source verdict changes: the prompt, the
# grounding checks, the excerpt selection. A cached verdict may only be reused while this
# matches, because the cache key has to name everything the verdict depended on -- a
# reuse across an evaluator change is a stale judgement presented as a fresh one.
SOURCE_RULE_EVALUATOR_VERSION = "1"


def source_rule_cache_key(
    *,
    collection_id: UUID,
    document_sha256: str,
    ruleset_hash: str | None,
    evaluator_version: str = SOURCE_RULE_EVALUATOR_VERSION,
) -> str:
    """Identity of one source-rule evaluation: these bytes, this playbook, this evaluator.

    Collection-scoped because a document is deduplicated per collection: the same bytes in
    two collections are two document rows with two sets of blocks, and findings cite
    blocks. Reusing across that boundary would hand a run citations into another
    collection's document.
    """
    return sha256_text(
        "|".join(
            [
                str(collection_id),
                document_sha256,
                ruleset_hash or "",
                evaluator_version,
            ]
        )
    )


def ruleset_hash(ruleset: Ruleset) -> str:
    """Identity of the exact playbook a run was judged against.

    Pinned at run start and recorded, so a finding stays explicable after the playbook is
    edited: `name v2` is a moving target, this is not.
    """
    return sha256_text(ruleset.source_text)


def ruleset_content_hash(ruleset: Ruleset) -> str:
    """Identity of what a playbook *says*, independent of its version or how it was
    typed. `ruleset_hash` above hashes the raw uploaded payload -- right for pinning a
    run to the exact bytes it was judged against, wrong for deciding whether a new
    upload changed anything, since a re-upload that only omits or repeats a version
    number would hash differently by that measure despite meaning the same thing. This
    hashes the parsed, validated content instead: same name and same rules is the same
    playbook, whatever the raw JSON looked like."""
    return sha256_text(
        canonical_json(
            {
                "name": ruleset.name,
                "rules": [
                    {
                        "code": rule.code,
                        "text": rule.text,
                        "severity": rule.severity,
                        "scope": rule.scope,
                        "keys": sorted(rule.target_keys),
                    }
                    for rule in sorted(ruleset.rules, key=lambda rule: rule.code)
                ],
            }
        )
    )


def parse_ruleset(payload: dict[str, Any], collection_id: UUID) -> Ruleset:
    """Parse an uploaded playbook. This is a trust boundary: the payload comes from a
    user, so every field is checked here rather than at the database constraint."""
    if not isinstance(payload, dict):
        raise ValueError("ruleset must be a JSON object")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("ruleset needs a name")

    try:
        version = int(payload.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("ruleset version must be an integer") from exc
    if version < 1:
        raise ValueError("ruleset version must be 1 or greater")

    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("ruleset needs a non-empty rules array")

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise ValueError(f"rule {index} must be an object")
        code = str(raw.get("code") or "").strip()
        text = str(raw.get("text") or "").strip()
        severity = str(raw.get("severity") or "").strip().lower()
        scope = str(raw.get("scope") or "").strip().lower()
        if not code:
            raise ValueError(f"rule {index} needs a code")
        if code in seen:
            raise ValueError(f"duplicate rule code: {code}")
        if not text:
            raise ValueError(f"rule {code} needs text")
        if severity not in SEVERITIES:
            raise ValueError(f"rule {code} has severity {severity!r}, expected one of {SEVERITIES}")
        if scope not in SCOPES:
            raise ValueError(f"rule {code} has scope {scope!r}, expected one of {SCOPES}")
        raw_keys = raw.get("keys", [])
        if not isinstance(raw_keys, list):
            raise ValueError(f"rule {code} keys must be an array of register keys")
        keys = [str(key).strip() for key in raw_keys if str(key).strip()]
        if len(keys) != len(set(keys)):
            raise ValueError(f"rule {code} lists a duplicate key")
        # A rule aimed at a key the register cannot hold is never evaluated at the
        # deliverable stage, and a rule that never runs produces no finding -- which
        # reads exactly like a rule that ran and found nothing wrong. One typo in an
        # uploaded playbook silently switches a control off, so it is refused here.
        unknown = [key for key in keys if key not in OBLIGATION_KEYS]
        if unknown:
            raise ValueError(
                f"rule {code} targets unknown register key(s) {unknown}; "
                f"known keys are {sorted(OBLIGATION_KEYS)}"
            )
        seen.add(code)
        rules.append(
            Rule(code=code, text=text, severity=severity, scope=scope, target_keys=keys)
        )

    return Ruleset(
        collection_id=collection_id,
        name=name,
        version=version,
        source_text=json.dumps(payload, sort_keys=True),
        rules=rules,
    )


def rules_for(ruleset: Ruleset, stage: str) -> list[Rule]:
    """Rules that apply to `source` or `deliverable`; `both` applies to either."""
    if stage not in {"source", "deliverable"}:
        raise ValueError(f"unknown rule stage: {stage}")
    return [rule for rule in ruleset.rules if rule.scope in {stage, "both"}]


def target_keys_for(rule: Rule, available: list[str]) -> list[str]:
    """Which register keys this rule is evaluated against in the deliverable stage.

    A rule that names its keys is only evaluated against the ones this run actually
    touched - naming a key the run never affected would mean re-judging state nobody
    changed. A rule that names none applies to every affected key.
    """
    if not rule.target_keys:
        return list(available)
    return [key for key in available if key in set(rule.target_keys)]


def ground_verdict(
    verdict: RuleVerdict, excerpts: list[RuleExcerpt]
) -> tuple[str, str, list[FindingCitation]]:
    """Turn a model opinion into a recordable verdict.

    Two things are checked, in order, and both are the model's claims rather than facts:
    the excerpt it says it read has to be one it was actually given, and the quote has to
    be verbatim *inside that excerpt*. Searching every excerpt for the quote instead would
    quietly repair a model that cited the wrong location, and location is half of what a
    citation is for.

    A violation that survives neither check is downgraded to `insufficient_evidence`: an
    accusation without evidence is not a finding. Returns (verdict, rationale, citations).
    """
    citations: list[FindingCitation] = []
    quote = (verdict.quote or "").strip()
    by_index = {excerpt.index: excerpt for excerpt in excerpts}
    excerpt = by_index.get(verdict.excerpt_index) if verdict.excerpt_index is not None else None
    failure: str | None = None

    if not quote:
        failure = "no quote was offered"
    elif excerpt is None:
        failure = f"excerpt {verdict.excerpt_index} was never offered to the model"
    elif quote not in excerpt.text:
        failure = f"the quote is not verbatim in {excerpt.label}"
    elif excerpt.block_id is None:
        # A register row is a rendering, not source text. Quoting it grounds nothing.
        failure = f"{excerpt.label} is derived state, not a source block"
    else:
        start = excerpt.text.find(quote)
        citations.append(
            FindingCitation(
                block_id=excerpt.block_id,
                quote=quote,
                quote_start=start,
                quote_end=start + len(quote),
            )
        )

    if verdict.verdict == "violation" and not citations:
        return (
            "insufficient_evidence",
            f"{verdict.rationale} (downgraded: {failure})",
            [],
        )
    return verdict.verdict, verdict.rationale, citations
