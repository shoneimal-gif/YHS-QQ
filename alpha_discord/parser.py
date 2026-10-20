"""Strict, dependency-free parsing of the observed AlphaAgent embed format.

Public API: ``parse_message(message: Mapping[str, Any]) -> dict[str, Any]``.
The returned dictionary is JSON serializable. It contains only normalized
fields and fixed diagnostics, never arbitrary descriptions or credentials.
The collector must authenticate the Discord source before using the result.

Supported titles are an optional T<number> followed by OPEN, CLOSE,
BUY_TO_OPEN, or SELL_TO_CLOSE. OPEN/CLOSE alone do not imply a buy or sell.
The description must have exactly one contract line in this form
(synthetic examples; not actual signals)::

    XYZ P 100 2030-06-21
    Entry 4.00 | 1x
    GEX ...

The observed CLOSE description is also accepted::

    ABC P 50 2030-06-21
    Exit 2.00 | -20.0% | -$50.00 | hard_stop

The exit price, return, PnL and reason describe the source message, never
our actual fills or performance. OPEN/CLOSE remain UNKNOWN in this raw
parser; an explicitly authorized execution policy may supply their meaning.

``P100`` is also accepted. ``1x`` is a multiplier with unknown units, never
one contract. Future explicit templates may use ``Contracts: 1`` or
``Qty: 1 contracts`` (as a line or the portion after the Entry separator).
A SELL_TO_CLOSE additionally needs an ``Opening message: <Discord ID>``
line for syntactic completeness. No provided screenshot establishes that
the vendor actually uses those explicit templates.

``parsed`` means the supported embed syntax was parsed, not that it is a
trade. ``syntax_complete`` only means explicit fields exist for later
validation. ``eligible_for_orders`` is ALWAYS False: this module does not
verify current contract metadata, multipliers, quotes, signal age, source
authorization, positions, budget, or execution policy. It does not read a
clock, guess a date from a display time, infer language, or send requests.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any


PARSER_VERSION = "alpha_embed_v2"
_SNOWFLAKE = re.compile(r"[1-9][0-9]{0,19}\Z")
_TITLE = re.compile(r"(?:(T[1-9][0-9]{0,2})\s+)?(OPEN|CLOSE|BUY_TO_OPEN|SELL_TO_CLOSE)\Z")
_DECIMAL_TEXT = r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?"
_CONTRACT_LINE = re.compile(
    r"(?P<symbol>[A-Z]{1,6})\s+(?P<right>[CP])\s*"
    r"(?P<strike>" + _DECIMAL_TEXT + r")\s+"
    r"(?P<expiration>[0-9]{4}-[0-9]{2}-[0-9]{2})\Z"
)
_CONTRACT_ANYWHERE = re.compile(
    r"\b[A-Z]{1,6}\s+[CP]\s*[0-9]+(?:\.[0-9]+)?\s+[0-9]{4}-[0-9]{2}-[0-9]{2}\b"
)
_OCC_ANYWHERE = re.compile(r"\b[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}\b")
_ENTRY_LINE = re.compile(r"Entry\s*(?P<price>" + _DECIMAL_TEXT + r")(?:\s*\|\s*(?P<size>.+))?\Z")
_EXIT_LINE = re.compile(
    r"Exit\s*(?P<price>" + _DECIMAL_TEXT + r")\s*\|\s*"
    r"(?P<return>[+-]?" + _DECIMAL_TEXT + r")%\s*\|\s*"
    r"(?P<pnl_sign>[+-])\$(?P<pnl_absolute>" + _DECIMAL_TEXT + r")\s*\|\s*"
    r"(?P<reason>[a-z][a-z0-9_]{0,63})\Z"
)
_MULTIPLIER = re.compile(r"(?P<size>" + _DECIMAL_TEXT + r")\s*x\Z")
_CONTRACTS = re.compile(r"Contracts\s*:\s*(?P<qty>[1-9][0-9]{0,5})\Z")
_QTY_CONTRACTS = re.compile(r"Qty\s*:\s*(?P<qty>[1-9][0-9]{0,5})\s+contracts\Z")
_OPENING_MESSAGE = re.compile(r"Opening message\s*:\s*(?P<id>[1-9][0-9]{0,19})\Z")


def _id(value: Any) -> str | None:
    if isinstance(value, str) and _SNOWFLAKE.fullmatch(value) and int(value) < 2**64:
        return value
    return None


def _timestamp(value: Any) -> tuple[datetime | None, str | None]:
    if not isinstance(value, str) or len(value) > 64:
        return None, None
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None, None
        stamp = stamp.astimezone(timezone.utc)
        return stamp, stamp.isoformat()
    except (ValueError, OverflowError):
        return None, None


def _positive_decimal(text: str, maximum: str) -> Decimal | None:
    try:
        value = Decimal(text)
        if not value.is_finite() or value <= 0 or value > Decimal(maximum):
            return None
        return value
    except (InvalidOperation, ValueError):
        return None


def _revision(message: Mapping[str, Any], message_key: str | None) -> tuple[str | None, str | None]:
    # Hash the full embed dictionaries, not only parsed title/description.
    # Unsupported content/fields/footer edits must also get a new revision.
    payload = {"parser_version": PARSER_VERSION, "message_key": message_key,
               "content": message.get("content", ""), "embeds": message.get("embeds", [])}
    try:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        return None, "invalid_revision_content"
    if len(raw) > 131072:
        return None, "revision_content_too_large"
    return hashlib.sha256(raw).hexdigest(), None


def _quantity(text: str) -> dict[str, Any] | None:
    found = _CONTRACTS.fullmatch(text) or _QTY_CONTRACTS.fullmatch(text)
    if found:
        return {"quantity_mode": "explicit_contracts", "requested_contracts": int(found.group("qty")), "size_multiplier": None}
    found = _MULTIPLIER.fullmatch(text)
    if not found:
        return None
    value = _positive_decimal(found.group("size"), "1000")
    if value is None:
        return None
    return {"quantity_mode": "unknown_size_multiplier", "requested_contracts": None, "size_multiplier": float(value)}


def _source(message: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    author = message.get("author")
    author = author if isinstance(author, Mapping) else {}
    reference = message.get("message_reference")
    reference = reference if isinstance(reference, Mapping) else {}
    stamp, stamp_text = _timestamp(message.get("timestamp"))
    edited_stamp, edited_text = _timestamp(message.get("edited_timestamp"))
    ids = {
        "message_id": _id(message.get("id")),
        "channel_id": _id(message.get("channel_id")),
        "guild_id": _id(message.get("guild_id")),
        "author_id": _id(author.get("id")),
        "webhook_id": _id(message.get("webhook_id")),
    }
    for key in ("message_id", "channel_id", "author_id"):
        if ids[key] is None:
            errors.append("invalid_" + key)
    for input_key in ("guild_id", "webhook_id"):
        if message.get(input_key) is not None and ids[input_key] is None:
            errors.append("invalid_" + input_key)
    if stamp is None:
        errors.append("invalid_message_timestamp")
    if message.get("edited_timestamp") is not None and edited_stamp is None:
        errors.append("invalid_edited_timestamp")
    if stamp is not None and edited_stamp is not None and edited_stamp < stamp:
        errors.append("edited_before_created")
    flags = message.get("flags", 0)
    if type(flags) is not int or not 0 <= flags <= 2**31 - 1:
        errors.append("invalid_message_flags")
        flags = None
    ref_type = reference.get("type", 0) if reference else None
    if ref_type is not None and (type(ref_type) is not int or ref_type not in (0, 1)):
        errors.append("invalid_reference_type")
        ref_type = None
    normalized_reference = {key: _id(reference.get(key)) for key in ("message_id", "channel_id", "guild_id")}
    for key, value in normalized_reference.items():
        if reference.get(key) is not None and value is None:
            errors.append("invalid_reference_" + key)
    normalized_reference["type"] = ref_type
    if message.get("message_reference") is not None and not isinstance(message.get("message_reference"), Mapping):
        errors.append("invalid_message_reference")
    if ref_type == 1 or message.get("message_snapshots"):
        errors.append("unsupported_forward_structure")
    # These are delivery metadata. A webhook author's ID/name is not proof
    # of the original signal author, and a local display time is never used.
    source = {
        **ids,
        "author_is_bot": author.get("bot") if type(author.get("bot")) is bool else None,
        "message_timestamp_utc": stamp_text,
        "edited_timestamp_utc": edited_text,
        "flags": flags,
        "message_reference": normalized_reference,
        "source_authenticated": False,
        "original_author_id": None,
        "original_timestamp_utc": None,
    }
    if flags is not None and flags & 2 and all(normalized_reference[key] for key in ("guild_id", "channel_id", "message_id")):
        event_ids = normalized_reference
        source["message_key"] = "discord:" + ":".join(event_ids[key] for key in ("guild_id", "channel_id", "message_id"))
        source["message_key_scope"] = "crosspost_reference"
    elif ids["message_id"] and ids["channel_id"]:
        source["message_key"] = "discord:" + ":".join((ids["guild_id"] or "unknown", ids["channel_id"], ids["message_id"]))
        source["message_key_scope"] = "delivery"
    else:
        source["message_key"] = None
        source["message_key_scope"] = None
    return source, errors


def _contract(line: str) -> tuple[dict[str, Any] | None, str | None]:
    match = _CONTRACT_LINE.fullmatch(line)
    if not match:
        return None, "unsupported_contract_format"
    try:
        expiration = date.fromisoformat(match.group("expiration"))
    except ValueError:
        return None, "invalid_expiration"
    # YY in an OCC code cannot distinguish centuries; do not silently wrap.
    if not 2000 <= expiration.year <= 2099:
        return None, "unsupported_expiration_year"
    strike = _positive_decimal(match.group("strike"), "99999.999")
    if strike is None or strike * 1000 != (strike * 1000).to_integral_value():
        return None, "invalid_occ_strike"
    symbol, right = match.group("symbol"), match.group("right")
    encoded_strike = int(strike * 1000)
    occ = symbol + expiration.strftime("%y%m%d") + right + f"{encoded_strike:08d}"
    return {
        "asset_class": "us_option",
        "underlying": symbol,
        "right": right,
        "strike_usd": float(strike),
        "strike_decimal": format(strike, ".3f"),
        "expiration": expiration.isoformat(),
        "occ_symbol": occ,
        "contract_verified": False,
        "multiplier": None,
        "standard_multiplier_assumption": 100,
    }, None


def parse_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one message; reject ambiguous syntax without inferring trades.

    This is a total diagnostic function for ordinary JSON-like inputs. There
    are no broker, Discord, clock, filesystem, or notification side effects.
    Callers must not interpret ``parsed``/``syntax_complete`` as permission
    to trade. ``eligible_for_orders`` remains False on every result.
    """
    result: dict[str, Any] = {
        "parser_version": PARSER_VERSION,
        "parsed": False,
        "metadata_valid": False,
        "syntax_complete": False,
        "eligible_for_orders": False,
        "action_kind": "unknown",
        "explicit_side": "UNKNOWN",
        "broker_side": None,
        "signal_tier": None,
        "quantity_mode": "missing",
        "size_multiplier": None,
        "requested_contracts": None,
        "entry_reference_price_usd": None,
        "entry_reference_price_decimal": None,
        "entry_reference_is_executable_quote": False,
        "exit_reference_price_usd": None,
        "exit_reference_price_decimal": None,
        "exit_reference_is_executable_quote": False,
        "source_return_pct": None,
        "source_pnl_usd": None,
        "source_exit_reason": None,
        "source_performance_is_our_performance": False,
        "opening_message_id": None,
        "contract": None,
        "gex_commentary_present": False,
        "source": {},
        "revision_sha256": None,
        "errors": [],
        "warnings": [],
        "required_verifications": ["authenticated_source", "signal_freshness", "contract_metadata", "broker_quotes", "position_ownership", "execution_policy"],
    }
    if not isinstance(message, Mapping):
        result["errors"] = ["invalid_message_object"]
        return result
    result["source"], metadata_errors = _source(message)
    result["metadata_valid"] = not metadata_errors
    result["errors"].extend(metadata_errors)
    result["revision_sha256"], revision_error = _revision(message, result["source"].get("message_key"))
    if revision_error:
        result["errors"].append(revision_error)
    if message.get("content") not in (None, ""):
        result["errors"].append("unsupported_message_content")
    embeds = message.get("embeds")
    if not isinstance(embeds, list) or not 1 <= len(embeds) <= 10 or any(not isinstance(embed, Mapping) for embed in embeds):
        result["errors"].append("invalid_embeds")
        return result
    normalized: list[tuple[str, str, Mapping[str, Any]]] = []
    total_length = 0
    for embed in embeds:
        title, description = embed.get("title", ""), embed.get("description", "")
        if not isinstance(title, str) or not isinstance(description, str):
            result["errors"].append("invalid_embed_text")
            return result
        try:
            title.encode("utf-8")
            description.encode("utf-8")
        except UnicodeEncodeError:
            result["errors"].append("invalid_embed_unicode")
            return result
        total_length += len(title) + len(description)
        normalized.append((title.strip(), description, embed))
    if total_length > 6000:
        result["errors"].append("embed_text_too_long")
        return result
    signal_embeds = [embed for embed in normalized if _TITLE.fullmatch(embed[0])]
    all_text = "\n".join(title + "\n" + description for title, description, _ in normalized)
    if len(_CONTRACT_ANYWHERE.findall(all_text)) + len(_OCC_ANYWHERE.findall(all_text)) > 1:
        result["errors"].append("multiple_contract_text")
    if len(signal_embeds) != 1:
        result["errors"].append("ambiguous_signal_embeds" if len(signal_embeds) > 1 else "unsupported_signal_title")
        return result
    if len(normalized) != 1:
        # Additional embeds might contain direction/quantity overrides, so
        # the observed one-embed template is deliberately the only grammar.
        result["errors"].append("unsupported_additional_embeds")
    title, description, embed = signal_embeds[0]
    if embed.get("fields") or ("fields" in embed and not isinstance(embed["fields"], list)):
        result["errors"].append("unsupported_embed_fields")
    title_match = _TITLE.fullmatch(title)
    assert title_match is not None
    tier, action = title_match.groups()
    result["signal_tier"] = tier
    result["action_kind"] = "open" if action in ("OPEN", "BUY_TO_OPEN") else "close"
    if action in ("BUY_TO_OPEN", "SELL_TO_CLOSE"):
        result["explicit_side"] = action
        result["broker_side"] = "buy" if action == "BUY_TO_OPEN" else "sell"
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    if not lines:
        result["errors"].append("missing_contract_line")
        return result
    result["contract"], contract_error = _contract(lines[0])
    if contract_error:
        result["errors"].append(contract_error)
    entry_seen = exit_seen = size_seen = opening_seen = gex_seen = False
    for line in lines[1:]:
        entry = _ENTRY_LINE.fullmatch(line)
        if entry:
            if result["action_kind"] != "open":
                result["errors"].append("entry_price_on_close_signal")
            if entry_seen:
                result["errors"].append("multiple_entry_prices")
                continue
            entry_seen = True
            price = _positive_decimal(entry.group("price"), "999999.999999")
            if price is None:
                result["errors"].append("invalid_entry_price")
            else:
                result["entry_reference_price_usd"] = float(price)
                result["entry_reference_price_decimal"] = format(price, "f")
            if entry.group("size") is not None:
                size = _quantity(entry.group("size").strip())
                if size_seen:
                    result["errors"].append("multiple_quantity_fields")
                elif size is None:
                    result["errors"].append("unsupported_quantity_format")
                else:
                    result.update(size)
                size_seen = True
            continue
        exit_summary = _EXIT_LINE.fullmatch(line)
        if exit_summary:
            if result["action_kind"] != "close":
                result["errors"].append("exit_summary_on_open_signal")
            if exit_seen:
                result["errors"].append("multiple_exit_summaries")
                continue
            exit_seen = True
            price = _positive_decimal(exit_summary.group("price"), "999999.999999")
            if price is None:
                result["errors"].append("invalid_exit_price")
            else:
                result["exit_reference_price_usd"] = float(price)
                result["exit_reference_price_decimal"] = format(price, "f")
            source_return = Decimal(exit_summary.group("return"))
            source_pnl = Decimal(exit_summary.group("pnl_sign") + exit_summary.group("pnl_absolute"))
            result["source_return_pct"] = float(source_return)
            result["source_pnl_usd"] = float(source_pnl)
            result["source_exit_reason"] = exit_summary.group("reason")
            continue
        size = _quantity(line)
        if size:
            if size_seen:
                result["errors"].append("multiple_quantity_fields")
            else:
                result.update(size)
            size_seen = True
            continue
        opening = _OPENING_MESSAGE.fullmatch(line)
        if opening:
            if _id(opening.group("id")) is None:
                result["errors"].append("invalid_opening_message_id")
            elif opening_seen:
                result["errors"].append("multiple_opening_references")
            else:
                result["opening_message_id"] = opening.group("id")
            opening_seen = True
            continue
        if line == "GEX" or line.startswith("GEX ") or line.startswith("GEX：") or line.startswith("GEX:"):
            if gex_seen:
                result["errors"].append("multiple_gex_commentary_lines")
            gex_seen = True
            result["gex_commentary_present"] = True
            continue
        result["errors"].append("unsupported_description_line")
    if result["explicit_side"] == "UNKNOWN":
        result["warnings"].append("open_close_does_not_establish_buy_sell_side")
    if result["quantity_mode"] == "unknown_size_multiplier":
        result["warnings"].append("multiplier_units_unknown_not_contract_count")
    elif result["quantity_mode"] == "missing":
        result["warnings"].append("contract_quantity_missing")
    if result["action_kind"] == "open" and result["entry_reference_price_usd"] is None:
        result["warnings"].append("entry_reference_price_missing")
    if result["action_kind"] == "close" and result["opening_message_id"] is None:
        result["warnings"].append("opening_position_reference_missing")
    if result["action_kind"] == "close" and not exit_seen:
        result["errors"].append("missing_exit_summary")
    if exit_seen:
        result["warnings"].append("source_exit_statistics_are_not_our_fills_or_performance")
    if result["gex_commentary_present"]:
        result["warnings"].append("gex_commentary_is_not_strategy_confirmation")
    result["warnings"].append("source_and_execution_policy_not_verified_by_parser")
    result["errors"] = list(dict.fromkeys(result["errors"]))
    result["parsed"] = not result["errors"] and result["contract"] is not None
    result["syntax_complete"] = bool(
        result["parsed"] and result["metadata_valid"]
        and result["explicit_side"] in ("BUY_TO_OPEN", "SELL_TO_CLOSE")
        and result["quantity_mode"] == "explicit_contracts"
        and (result["action_kind"] != "open" or result["entry_reference_price_usd"] is not None)
        and (result["action_kind"] != "close" or result["opening_message_id"] is not None)
    )
    return result
