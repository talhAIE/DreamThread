"""Deterministic source/quote checks. A matching quote does not prove an interpretation."""

import hashlib
import re

from app.schemas.analysis import Extraction, ReflectionOutput


class OutputInvalid(ValueError):
    """Only static categories are returned to a repair prompt, never raw validation input."""


def verify_citation(citation, *, dream_id, revision_id, source_hash, text):
    if (
        citation.dream_id != dream_id
        or citation.revision_id != revision_id
        or citation.source_hash != source_hash
        or hashlib.sha256(text.encode("utf-8")).hexdigest() != source_hash
    ):
        raise OutputInvalid("source_mismatch")
    if (
        0 <= citation.start < citation.end <= len(text)
        and text[citation.start : citation.end] == citation.quote
    ):
        return citation
    # Only a unique occurrence can repair suggested model offsets. Count overlaps as ambiguous.
    first = text.find(citation.quote)
    if first < 0 or text.find(citation.quote, first + 1) >= 0:
        raise OutputInvalid("quote_mismatch_or_ambiguous")
    return citation.model_copy(update={"start": first, "end": first + len(citation.quote)})


def verify_extraction(output: Extraction, source):
    keys, verified = set(), []
    for observation in output.observations:
        if observation.key in keys:
            raise OutputInvalid("duplicate_observation")
        keys.add(observation.key)
        citation = verify_citation(observation.citation, **source)
        label = observation.label.casefold()
        if label not in citation.quote.casefold():
            raise OutputInvalid("label_not_in_source")
        # Conservative English negation check; emotion mentions are not user-reported mood tags.
        position = citation.quote.casefold().find(label)
        before = source["text"][: citation.start + position + len(label)].casefold()[-100:]
        if re.search(
            r"\b(?:not|never|no|without|hardly)\s+(?:\w+\s+){0,2}" + re.escape(label) + r"\b$",
            before,
        ):
            raise OutputInvalid("negated_mention")
        verified.append(observation.model_copy(update={"citation": citation}))
    return output.model_copy(update={"observations": verified})


def verify_reflections(output: ReflectionOutput, keys):
    for reflection in output.possible_reflections:
        if not set(reflection.observation_keys).issubset(keys):
            raise OutputInvalid("unknown_observation")
        text = reflection.text.casefold()
        if not text.startswith(("you might ", "you could ", "one possibility is ", "perhaps ")):
            raise OutputInvalid("tentative_wording_required")
        if forbidden_reflection(text):
            raise OutputInvalid("unsupported_certainty")
    if output.followup_question and (
        output.followup_question.count("?") != 1 or not output.followup_question.endswith("?")
    ):
        raise OutputInvalid("one_question_required")
    if output.followup_question and forbidden_reflection(output.followup_question.casefold()):
        raise OutputInvalid("unsupported_question")
    return output


def forbidden_reflection(text):
    return re.search(
        r"\b(?:diagnos\w*|schizophrenia|bipolar|psychosis|prophe\w*|predict\w*|"
        r"definitely|proves?|must mean|passwords?|credentials?)\b|you (?:have|will)|this means|"
        r"your subconscious (?:says|tells)|api.?key|system prompt|ignore (?:all|previous)|"
        r"\brecurr\w*\b|across (?:your|multiple) dreams|earlier dreams|"
        r"past entries|previous entries",
        text,
    )
