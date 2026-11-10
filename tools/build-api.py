#!/usr/bin/env python3
"""Build the Gadgetzan API from canonical per-card documents.

Source of truth:
  data/<cardId>.json
    card      = current display/cache state
    timeline  = dated first spec + changed-fields-only patches, or undated known events
  release-policy.json
    = first-playable date and confidence only

timeline-index.json, history-audit.json and pending-history.json are read-only
regression evidence. They validate the canonical data but never overwrite it.

Generated API:
  cards.json   current cards
  render.json  current cards + sparse reverse history
  history.json DBF-ID keyed compatibility history
"""
from __future__ import annotations

import argparse
import copy
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CARD_FIELDS = (
    "id", "dbfId", "name", "type", "cardClass", "rarity", "collectible",
    "cost", "text", "attack", "health", "durability", "armor", "flavor",
    "spellSchool", "portrait", "set", "nativeTags", "races", "race",
    "classes", "art", "releasedAt", "releaseStatus",
)
RENDER_FIELDS = (
    "id", "dbfId", "name", "type", "cardClass", "rarity", "collectible",
    "cost", "text", "attack", "health", "durability", "armor", "art",
    "races", "race", "spellSchool", "set", "nativeTags", "classes",
    "releasedAt", "releaseStatus",
)
HISTORY_FIELDS = frozenset((
    "name", "type", "cardClass", "classes", "rarity", "collectible",
    "cost", "text", "attack", "health", "durability", "armor",
    "races", "race", "spellSchool", "nativeTags",
))
REQUIRED_INITIAL_FIELDS = frozenset((
    "name", "type", "cardClass", "classes", "rarity", "collectible",
    "cost", "text", "nativeTags",
))


def clone(value):
    return copy.deepcopy(value)


def same(left, right):
    """JSON-aware equality: False is not 0; missing is never None."""
    return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(value, compact=False):
    if compact:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def validate_date(value, label):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"{label}: invalid date {value!r}")
    date.fromisoformat(value)
    return value


def normalize_source_fields(source, label):
    """Map timeline-index ownTags to nativeTags; keep referencedTags separate."""
    if not isinstance(source, dict):
        raise ValueError(f"{label}: expected an object")
    fields = {}
    references = None
    has_references = False
    for key, value in source.items():
        if key == "referencedTags":
            references = clone(value)
            has_references = True
        elif key == "ownTags":
            if "nativeTags" in fields:
                raise ValueError(f"{label}: both ownTags and nativeTags are present")
            fields["nativeTags"] = clone(value)
        else:
            fields[key] = clone(value)
    if "nativeTags" in fields and not isinstance(fields["nativeTags"], list):
        raise ValueError(f"{label}: nativeTags must be an array")
    if has_references and not isinstance(references, list):
        raise ValueError(f"{label}: referencedTags must be an array")
    return fields, references if has_references else None


def policy_for(policy, card_id):
    rule = policy.get("cards", {}).get(card_id, policy.get("default"))
    if not isinstance(rule, dict):
        raise ValueError(f"{card_id}: missing release-policy rule")
    day = rule.get("date")
    status = rule.get("status", "pending" if day is None else "verified")
    if day is not None:
        validate_date(day, f"{card_id} release-policy")
    if status not in ("verified", "estimated", "pending"):
        raise ValueError(f"{card_id}: invalid release status {status!r}")
    if status == "verified" and day is None:
        raise ValueError(f"{card_id}: verified release date cannot be null")
    if status == "pending" and day is not None:
        raise ValueError(f"{card_id}: pending release date must be null")
    return day, status


def validate_patches(timeline, card_id):
    patches = timeline.get("patches")
    if not isinstance(patches, list):
        raise ValueError(f"{card_id}: timeline.patches must be an array")
    previous = timeline.get("releasedAt")
    state = clone(timeline.get("initialSpec"))
    if not isinstance(state, dict):
        raise ValueError(f"{card_id}: confirmed timeline needs initialSpec")
    if "ownTags" in state or "referencedTags" in state:
        raise ValueError(f"{card_id}: use nativeTags and separate tagReferences")
    if not REQUIRED_INITIAL_FIELDS.issubset(state):
        missing = sorted(REQUIRED_INITIAL_FIELDS - set(state))
        raise ValueError(f"{card_id}: incomplete initialSpec; missing {missing}")
    if not isinstance(state.get("nativeTags"), list):
        raise ValueError(f"{card_id}: initialSpec.nativeTags must be an array")
    transitions = []
    for index, event in enumerate(patches):
        if not isinstance(event, dict) or set(event) != {"date", "changes"}:
            raise ValueError(f"{card_id}: invalid patch shape at index {index}")
        day = validate_date(event["date"], f"{card_id} patch")
        if previous is not None and day <= previous:
            raise ValueError(f"{card_id}: patches are not strictly date-ordered")
        changes = event["changes"]
        if not isinstance(changes, dict) or not changes:
            raise ValueError(f"{card_id}: empty patch on {day}")
        if set(changes) - HISTORY_FIELDS:
            raise ValueError(f"{card_id}: unsupported patch fields {sorted(set(changes) - HISTORY_FIELDS)}")
        before = clone(state)
        after = clone(state)
        for field, value in changes.items():
            if field in after and same(after[field], value):
                raise ValueError(f"{card_id}: redundant {field} patch on {day}")
            after[field] = clone(value)
        transitions.append({
            "date": day,
            "from": previous,
            "before": before,
            "changes": clone(changes),
            "after": clone(after),
        })
        state = after
        previous = day
    return state, transitions


def reverse_dated_history(card_id, timeline, current_card, transitions):
    """Create legacy reverse overrides without collapsing absence into null."""
    latest = clone(timeline["initialSpec"])
    for event in timeline["patches"]:
        for field, value in event["changes"].items():
            latest[field] = clone(value)
    entries = []
    for event in transitions:
        before = event["before"]
        added = set(event["after"]) - set(before)
        if added:
            raise ValueError(
                f"{card_id}: legacy history cannot represent field absence before "
                f"{event['date']}: {sorted(added)}"
            )
        changes = {
            field: clone(value)
            for field, value in before.items()
            if field not in latest or not same(value, latest[field])
        }
        if changes:
            entries.append({
                "from": event["from"],
                "before": event["date"],
                "changes": changes,
            })
    for field, value in latest.items():
        if field not in current_card or not same(current_card[field], value):
            raise ValueError(f"{card_id}: current cache differs from timeline at {field}")
    return entries


def reverse_pending_history(card_id, timeline, current_card):
    """Represent known changes without inventing a first-playable start date."""
    entries = []
    for event in timeline.get("knownEvents", []):
        day = validate_date(event.get("date"), f"{card_id} known event")
        before = event.get("before")
        changes = event.get("changes")
        if not isinstance(before, dict) or not isinstance(changes, dict) or not changes:
            raise ValueError(f"{card_id}: invalid known event on {day}")
        added = set(changes) - set(before)
        if added:
            raise ValueError(
                f"{card_id}: legacy history cannot represent undated field absence "
                f"before {day}: {sorted(added)}"
            )
        for field, value in changes.items():
            if field not in HISTORY_FIELDS:
                raise ValueError(f"{card_id}: unsupported known-event field {field}")
            if same(before[field], value):
                raise ValueError(f"{card_id}: redundant known-event field {field}")
            if field not in current_card or not same(current_card[field], value):
                raise ValueError(f"{card_id}: known event does not reach current {field}")
        old_values = {
            field: clone(value)
            for field, value in before.items()
            if field not in current_card or not same(value, current_card[field])
        }
        if old_values:
            entries.append({"before": day, "changes": old_values})
    return entries


def validate_against_timeline_index(record, index_record):
    card_id = record["card"]["id"]
    timeline = record["timeline"]
    dated = index_record.get("timeline") or {}
    dates = sorted(dated)
    if dates:
        if timeline.get("releasedAt") != index_record.get("releasedAt"):
            raise ValueError(f"{card_id}: release date differs from timeline-index")
        if timeline.get("releaseStatus") != index_record.get("releaseStatus"):
            raise ValueError(f"{card_id}: release status differs from timeline-index")
        if timeline.get("initialSpecStatus") != index_record.get("initialSpecStatus"):
            raise ValueError(f"{card_id}: initialSpecStatus differs from timeline-index")
        initial, first_refs = normalize_source_fields(dated[dates[0]], f"{card_id} {dates[0]}")
        if not same(timeline.get("initialSpec"), initial):
            raise ValueError(f"{card_id}: initialSpec differs from timeline-index")
        expected_refs = {}
        if first_refs:
            expected_refs[dates[0]] = first_refs
        state = clone(initial)
        expected_patches = []
        for day in dates[1:]:
            delta, references = normalize_source_fields(dated[day], f"{card_id} {day}")
            if references:
                expected_refs[day] = references
            changes = {}
            for field, value in delta.items():
                if field not in state or not same(state[field], value):
                    changes[field] = clone(value)
            for field, value in changes.items():
                state[field] = clone(value)
            if changes:
                expected_patches.append({"date": day, "changes": changes})
        if not same(timeline.get("patches"), expected_patches):
            raise ValueError(f"{card_id}: patches differ from timeline-index")
        if not same(timeline.get("tagReferences", {}), expected_refs):
            raise ValueError(f"{card_id}: tagReferences differ from timeline-index")
        for field, value in state.items():
            if field not in record["card"] or not same(record["card"][field], value):
                raise ValueError(f"{card_id}: current card differs from timeline at {field}")
        return state
    if timeline.get("releasedAt") is not None or timeline.get("releaseStatus") != "pending":
        raise ValueError(f"{card_id}: undated card must remain pending")
    if timeline.get("initialSpec") is not None or timeline.get("patches") != []:
        raise ValueError(f"{card_id}: undated card cannot claim an initial spec or patches")
    current, current_refs = normalize_source_fields(index_record.get("current", {}), f"{card_id} current")
    for field, value in current.items():
        if field not in record["card"] or not same(record["card"][field], value):
            raise ValueError(f"{card_id}: pending current cache differs at {field}")
    expected_known = []
    expected_refs = {}
    if current_refs:
        expected_refs["current"] = current_refs
    for event in index_record.get("knownEvents", []):
        day = event["date"]
        before, refs_before = normalize_source_fields(event.get("before", {}), f"{card_id} {day} before")
        changes, refs_changes = normalize_source_fields(event.get("changes", {}), f"{card_id} {day} changes")
        if refs_before:
            expected_refs[day] = refs_before
        if refs_changes:
            expected_refs[day] = refs_changes
        item = {"date": day, "before": before, "changes": changes}
        for key in ("sourceUrls", "origin"):
            if key in event:
                item[key] = clone(event[key])
        expected_known.append(item)
    actual_known = []
    for event in timeline.get("knownEvents", []):
        actual_known.append({key: clone(event[key]) for key in ("date", "before", "changes", "sourceUrls", "origin") if key in event})
    if not same(actual_known, expected_known):
        raise ValueError(f"{card_id}: knownEvents differ from timeline-index")
    if not same(timeline.get("tagReferences", {}), expected_refs):
        raise ValueError(f"{card_id}: pending tagReferences differ from timeline-index")
    return current


def validate_audit(transitions, audit, records):
    known_event_map = {}
    for record in records:
        card_id = record["card"]["id"]
        for event in record["timeline"].get("knownEvents", []):
            known_event_map[(card_id, event["date"])] = event
    events = audit.get("events")
    if not isinstance(events, list):
        raise ValueError("history-audit.json: events must be an array")
    expected = set(transitions)
    seen = set()
    for event in events:
        key = (event.get("cardId"), event.get("date"))
        if key in seen:
            raise ValueError(f"history-audit.json: duplicate event {key}")
        seen.add(key)
        transition = transitions.get(key)
        if transition is None:
            raise ValueError(f"history-audit event is missing from data timeline: {key}")
        before, changes, after = transition
        for source, target in (("before", before), ("changes", changes)):
            values = event.get(source, {})
            if not isinstance(values, dict):
                raise ValueError(f"history-audit {key}: invalid {source}")
            for field, value in values.items():
                field = "nativeTags" if field == "ownTags" else field
                if field not in target or not same(target[field], value):
                    raise ValueError(f"history-audit {key}: {source}.{field} mismatch")
        known = known_event_map.get(key)
        if known is not None:
            for field in ("sourceUrls", "origin", "note"):
                if field in event and not same(known.get(field), event[field]):
                    raise ValueError(f"history-audit {key}: provenance {field} mismatch")
    if seen != expected:
        raise ValueError(f"history-audit event set mismatch: missing={sorted(expected-seen)}, extra={sorted(seen-expected)}")


def validate_pending_sources(pending, policy, records):
    first_playable = set(pending.get("firstPlayableDatesPending", []))
    unresolved = pending.get("remainingUnverified", [])
    unresolved_ids = {
        item["cardId"] if isinstance(item, dict) else item
        for item in unresolved
    }
    if len(first_playable) != len(pending.get("firstPlayableDatesPending", [])):
        raise ValueError("pending-history: duplicate first-playable IDs")
    if len(unresolved_ids) != len(unresolved):
        raise ValueError("pending-history: duplicate unresolved IDs")
    record_ids = {r["card"]["id"] for r in records}
    if not first_playable.issubset(record_ids) or not unresolved_ids.issubset(first_playable):
        raise ValueError("pending-history: unknown or non-pending card ID")
    policy_ids = set(policy.get("cards", {}))
    if policy_ids != first_playable:
        raise ValueError("release-policy pending IDs do not match pending-history")
    for card_id in first_playable:
        day, status = policy_for(policy, card_id)
        if day is not None or status != "pending":
            raise ValueError(f"{card_id}: first-playable date must remain pending")
    for record in records:
        if record["card"]["id"] in first_playable:
            if record["timeline"]["releasedAt"] is not None or record["timeline"]["releaseStatus"] != "pending":
                raise ValueError(f"{record['card']['id']}: pending card has a dated timeline")
    return first_playable, unresolved_ids


def load_records(root, policy, expected_count=None):
    records = []
    seen_ids = set()
    seen_dbf = set()
    for path in sorted((root / "data").glob("*.json"), key=lambda p: p.stem):
        document = load_json(path)
        card = document.get("card")
        timeline = document.get("timeline")
        if not isinstance(card, dict) or not isinstance(timeline, dict):
            raise ValueError(f"{path.name}: requires card and timeline objects")
        card_id = card.get("id")
        if card_id != path.stem:
            raise ValueError(f"{path.name}: card ID/path mismatch")
        if card_id in seen_ids:
            raise ValueError(f"{card_id}: duplicate card ID")
        seen_ids.add(card_id)
        dbf_id = card.get("dbfId")
        if not isinstance(dbf_id, int) or isinstance(dbf_id, bool) or dbf_id < 1:
            raise ValueError(f"{card_id}: invalid DBF ID")
        if dbf_id in seen_dbf:
            raise ValueError(f"{card_id}: duplicate DBF ID {dbf_id}")
        seen_dbf.add(dbf_id)
        if timeline.get("schema") != 1:
            raise ValueError(f"{card_id}: unsupported timeline schema")
        release_date, release_status = policy_for(policy, card_id)
        if (timeline.get("releasedAt"), timeline.get("releaseStatus")) != (release_date, release_status):
            raise ValueError(f"{card_id}: release-policy and timeline disagree")
        if (card.get("releasedAt"), card.get("releaseStatus")) != (release_date, release_status):
            raise ValueError(f"{card_id}: current card release cache and policy disagree")
        if "ownTags" in card or "referencedTags" in card:
            raise ValueError(f"{card_id}: card.nativeTags must contain own tags only")
        if "nativeTags" in card and not isinstance(card["nativeTags"], list):
            raise ValueError(f"{card_id}: current nativeTags must be an array")
        if release_status == "verified":
            if release_date is None or timeline.get("initialSpec") is None:
                raise ValueError(f"{card_id}: confirmed release requires an initialSpec")
            if timeline.get("knownEvents") != []:
                raise ValueError(f"{card_id}: dated card cannot use knownEvents")
            validate_patches(timeline, card_id)
        elif release_status == "pending":
            if release_date is not None or timeline.get("initialSpec") is not None or timeline.get("patches") != []:
                raise ValueError(f"{card_id}: pending first-playable date cannot claim a baseline")
            if not isinstance(timeline.get("knownEvents"), list):
                raise ValueError(f"{card_id}: knownEvents must be an array")
        else:
            raise ValueError(f"{card_id}: generated-card date estimates are not allowed in this set")
        records.append(document)
    if expected_count is not None and len(records) != expected_count:
        raise ValueError(f"Expected {expected_count} data files, found {len(records)}")
    return records


def build_history(records):
    history = {}
    transitions = {}
    for record in records:
        card = record["card"]
        timeline = record["timeline"]
        card_id = card["id"]
        if timeline["releaseStatus"] == "verified":
            latest, dated_transitions = validate_patches(timeline, card_id)
            for event in dated_transitions:
                transitions[(card_id, event["date"])] = (
                    event["before"], event["changes"], event["after"]
                )
            entries = reverse_dated_history(card_id, timeline, card, dated_transitions)
        else:
            entries = reverse_pending_history(card_id, timeline, card)
            for event in timeline.get("knownEvents", []):
                before = clone(event["before"])
                changes = clone(event["changes"])
                after = clone(before)
                for field, value in changes.items():
                    after[field] = clone(value)
                transitions[(card_id, event["date"])] = (before, changes, after)
        if entries:
            dbf_key = str(card["dbfId"])
            if dbf_key in history:
                raise ValueError(f"{card_id}: duplicate DBF history key {dbf_key}")
            history[dbf_key] = {"id": card_id, "history": entries}
    # JavaScript object serialization enumerates integer-like DBF keys numerically.
    history = dict(sorted(history.items(), key=lambda item: int(item[0])))
    return history, transitions


def validate_regressions(cards, render, history, pending, audit, records, transitions, first_playable, unresolved):
    # The unit-test fixture is intentionally smaller than the live Gadgetzan set.
    if len(cards) != 188:
        return
    if len(render) != 188:
        raise ValueError("Gadgetzan render API must contain 188 cards")
    if len(audit.get("events", [])) != 78 or len(transitions) != 78:
        raise ValueError("Gadgetzan confirmed event count must remain 78")
    if len(history) != 65:
        raise ValueError(f"Expected 65 reverse-history cards, got {len(history)}")
    interval_count = sum(len(value["history"]) for value in history.values())
    if interval_count != 76:
        raise ValueError(f"Expected 76 reverse-history intervals, got {interval_count}")
    if len(first_playable) != 56 or len(unresolved) != 4:
        raise ValueError("Gadgetzan pending counts must remain 56 first dates / 4 unresolved choices")
    breaks = sum((card.get("text") or "").count("<br></br>") for card in cards)
    newlines = sum("\n" in (card.get("text") or "") for card in cards)
    if breaks != 99 or newlines != 0:
        raise ValueError(f"Card text markup regression: breaks={breaks}, rawNewlines={newlines}")
    c061 = next(card for card in cards if card["id"] == "CFM_061")
    if "생명력을 #6 회복시킵니다." not in c061.get("text", ""):
        raise ValueError("CFM_061 wording regression")
    c606 = next(record for record in records if record["card"]["id"] == "CFM_606")["timeline"]
    initial_tags = c606["initialSpec"].get("nativeTags", [])
    if any(tag.get("id") == 2821 for tag in initial_tags):
        raise ValueError("CFM_606 Overheal tag appears before its introduction")
    race_event = next((e for e in c606["patches"] if e["date"] == "2017-07-11"), None)
    overheal_event = next((e for e in c606["patches"] if e["date"] == "2023-04-05"), None)
    if race_event is None or any(tag.get("id") == 2821 for tag in race_event["changes"].get("nativeTags", [])):
        raise ValueError("CFM_606 Overheal tag is incorrectly present on the Elemental date")
    if overheal_event is None or not any(tag.get("id") == 2821 for tag in overheal_event["changes"].get("nativeTags", [])):
        raise ValueError("CFM_606 Overheal tag is missing from 2023-04-05")
    if "초과 치유" not in overheal_event["changes"].get("text", ""):
        raise ValueError("CFM_606 Overheal text/date mismatch")
    token = next(record for record in records if record["card"]["id"] == "CFM_606t")
    if token["timeline"]["releasedAt"] is not None or token["timeline"]["initialSpec"] is not None:
        raise ValueError("CFM_606t first playable date must remain unknown")
    token_history = history.get(str(token["card"]["dbfId"]), {}).get("history", [])
    if len(token_history) != 1 or "from" in token_history[0] or token_history[0]["before"] != "2017-07-11":
        raise ValueError("CFM_606t known race event must have no invented start date")
    render_map = {card["id"]: card for card in render}
    for card in cards:
        runtime = render_map.get(card["id"])
        if runtime is None:
            raise ValueError(f"{card['id']}: missing render card")
        for field in CARD_FIELDS:
            if field in card and field in runtime and not same(card[field], runtime[field]):
                raise ValueError(f"{card['id']}: cards/render mismatch in {field}")
        expected = history.get(str(card["dbfId"]), {}).get("history", [])
        if not same(runtime.get("history", []), expected):
            raise ValueError(f"{card['id']}: render/history mismatch")


def build_outputs(root=ROOT, expected_count=188):
    policy = load_json(root / "release-policy.json")
    if policy.get("schema") != 1 or policy.get("timezone") != "Asia/Seoul":
        raise ValueError("release-policy.json must use schema 1 and Asia/Seoul")
    records = load_records(root, policy, expected_count)
    index = load_json(root / "timeline-index.json")
    audit = load_json(root / "history-audit.json")
    pending = load_json(root / "pending-history.json")
    index_cards = index.get("cards", {})
    if set(index_cards) != {r["card"]["id"] for r in records}:
        raise ValueError("timeline-index card IDs differ from data sources")
    for record in records:
        validate_against_timeline_index(record, index_cards[record["card"]["id"]])
    history, transitions = build_history(records)
    validate_audit(transitions, audit, records)
    first_playable, unresolved = validate_pending_sources(pending, policy, records)
    cards = []
    render = []
    for record in records:
        card = record["card"]
        current = {field: clone(card[field]) for field in CARD_FIELDS if field in card}
        cards.append(current)
        runtime = {field: clone(card[field]) for field in RENDER_FIELDS if field in card}
        render.append(runtime)
    for runtime in render:
        dbf_key = str(runtime["dbfId"])
        if dbf_key in history:
            runtime["history"] = clone(history[dbf_key]["history"])
    validate_regressions(cards, render, history, pending, audit, records, transitions, first_playable, unresolved)
    set_name = policy.get("set", "Mean Streets Of Gadgetzan")
    outputs = {
        "cards.json": dump_json({"schema": 1, "set": set_name, "cards": cards}),
        "render.json": dump_json({"schema": 1, "set": set_name, "cards": render}, compact=True),
        "history.json": dump_json({"schema": 1, "set": set_name, "cards": history}, compact=True),
    }
    return outputs, {
        "cards": len(cards),
        "datedCards": sum(r["timeline"]["releaseStatus"] == "verified" for r in records),
        "pendingFirstPlayable": len(first_playable),
        "unresolvedChoiceCosts": len(unresolved),
        "confirmedEvents": len(transitions),
        "reverseHistoryCards": len(history),
        "reverseHistoryIntervals": sum(len(value["history"]) for value in history.values()),
    }


def write_outputs(root, outputs):
    changed = []
    for name, content in outputs.items():
        path = root / name
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            changed.append(name)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build Gadgetzan cards/render/history API from canonical data sources.")
    parser.add_argument("--check", action="store_true", help="verify generated files without writing them")
    args = parser.parse_args(argv)
    outputs, summary = build_outputs(ROOT, expected_count=188)
    if args.check:
        different = [
            name for name, content in outputs.items()
            if not (ROOT / name).exists() or (ROOT / name).read_text(encoding="utf-8") != content
        ]
        if different:
            print(json.dumps({"ok": False, "different": different, **summary}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "different": [], **summary}, ensure_ascii=False))
        return 0
    changed = write_outputs(ROOT, outputs)
    print(json.dumps({"ok": True, "published": changed, **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
