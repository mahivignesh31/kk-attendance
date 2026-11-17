"""Match portal rows to the master candidate list.

Candidate Id is the key. The candidate name is used only when it is an exact,
unique match. Attendance Id and similar names are not used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat", "null"}:
        return ""
    if text.endswith(".0"):
        head = text[:-2]
        if head.replace(".", "", 1).isdigit():
            text = head
    return text


def clean_id(value: object) -> str:
    return re.sub(r"\s+", "", clean_text(value))


def normalize_name(value: object) -> str:
    text = clean_text(value).casefold().replace(".", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _digits_equal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if left.isdigit() and right.isdigit():
        return int(left) == int(right) and (left.lstrip("0") == right.lstrip("0"))
    return False


@dataclass
class Match:
    candidate: Optional[dict]
    method: Optional[str]
    warning: Optional[str] = None


def match_candidate(portal_row: dict, candidates: list[dict]) -> Match:
    candidate_id = clean_id(portal_row.get("candidate_id"))
    portal_name = clean_text(portal_row.get("name"))

    if candidate_id:
        by_id = [item for item in candidates if _digits_equal(clean_id(item.get("candidate_id")), candidate_id)]
        if len(by_id) > 1:
            return Match(None, None, f"Candidate Id {candidate_id} matches more than one master record.")
        if len(by_id) == 1:
            warning = _name_warning(portal_name, by_id[0].get("name"), "Candidate Id")
            return Match(by_id[0], "candidate_id", warning)

    folded = normalize_name(portal_name)
    if folded:
        by_name = [item for item in candidates if normalize_name(item.get("name")) == folded]
        if len(by_name) == 1:
            return Match(
                by_name[0],
                "name_fallback",
                "Matched by exact candidate name because no id matched. Confirm this row before relying on it.",
            )
        if len(by_name) > 1:
            return Match(None, None, f"Name '{portal_name}' matches more than one master candidate.")

    if not candidate_id:
        return Match(None, None, "Portal row has no Candidate Id and the name is not an exact unique match.")
    return Match(None, None, "Portal candidate does not match the master list.")


def _name_warning(portal_name: str, master_name: object, method: str) -> Optional[str]:
    if not portal_name or not master_name:
        return None
    if normalize_name(portal_name) == normalize_name(master_name):
        return None
    return (
        f"{method} matched, but the portal name '{portal_name}' differs from the master name '{master_name}'."
    )
