"""Move a tool result out of the host's own session log, byte for byte.

Named failure mode (feedback 2026-08-29 §A). Hosts that inline small MCP
results into the model's context and persist only OVERSIZED ones leave the
small pulls — orders, positions, balances, performance, any quarter window
with few fills — with no file to hand `pull`. The SKILL forbids retyping a
result, so a careful agent reads that as "inline result ⇒ do not archive"
and abandons windows the broker will not serve again.

Claude Code records every `tool_result` verbatim under
`~/.claude/projects/<project>/<session-uuid>.jsonl`. A PROGRAM copying those
bytes preserves them exactly; the model retyping what it read does not, and
only the latter is forbidden. This module is that program, so the extraction
is not authored fresh under time pressure over facts that expire.

Every ambiguity is a refusal. A capture that silently loses a row becomes a
permanent fact in the archive that nothing downstream can detect.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Result bodies Claude Code records. A string is the whole body; a list is
# text blocks to be joined IN ORDER. Anything else (an image block) cannot be
# rendered as the tool's bytes, and dropping it would yield a shorter but
# still-valid capture — so it is refused, not skipped.
_TEXT_BLOCK = "text"


class TranscriptRefusal(Exception):
    """A condition under which extracting would risk a wrong archive."""


# A result too large to inline is NOT recorded verbatim: the host writes the
# payload to its own file and records a stand-in NAMING it. There are two
# observed families and the preamble wording varies within them ("N characters",
# "N characters across 1 line", "Output too large (40KB)"), so this anchors on
# the one stable fact — the stand-in names a file — instead of the wording.
# Measured 2026-08-30 over two real corpora: this project's 768 transcripts
# (78 refused: 74 genuine stand-ins + 4 Bash results that QUOTE the wording in
# non-JSON prose) and all 1,453 on the box (306 refused). Zero genuine
# stand-ins missed, and zero of 67 real Interactive Brokers payloads refused —
# those parse as JSON, which is what the gate below keys on. The 4 are the
# accepted cost: a non-JSON body quoting the phrase is refused conservatively,
# and the refusal names a path the operator can check.
_SAVED_ELSEWHERE = re.compile(
    r"(?:Output has been saved to|Full output saved to:)\s*(?P<path>\S+?)\.?(?=\s|$)")
# The `\.?(?=\s|$)` is load-bearing: paths contain dots (`/home/u/.claude/…`,
# `…/b27nj47zb.txt`), so a plain `[.\s]` terminator truncates the path at the
# FIRST one and the refusal then names a file that does not exist. Only a
# period followed by whitespace or end-of-string ends the sentence.


def iter_transcripts(path: Path) -> list[Path]:
    """Every session log under `path`, RECURSIVELY.

    A tool called inside a dispatched subagent is recorded under
    `<session>/subagents/agent-*.jsonl`, which a flat glob never sees — the
    capture then reports "id not found" for a result that is right there.
    Measured cost on this box: 768 files / 547MB in 0.54s, so correctness wins.
    """
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise TranscriptRefusal(f"no such transcript: {path}")
    found = sorted(path.rglob("*.jsonl"))
    if not found:
        raise TranscriptRefusal(f"no *.jsonl transcript under {path}")
    return found


def _entries(path: Path) -> tuple[list[dict], int, list[str]]:
    """Every JSON object on its own line, plus the count that would not parse.

    Unparseable lines are COUNTED and reported rather than dropped quietly: a
    corrupt line may be the target, and silence would render that as "id not
    found", which an operator reads as "I used the wrong id". They do not
    abort — an active session's last line is routinely half-written, and
    refusing on that would block every capture made while the session runs.

    The caller gets the skipped lines back, because a skipped line that
    CONTAINS the requested id is a competing body, not noise — that is the
    narrow case where proceeding really would be a coin flip.

    A file that is not valid UTF-8 DOES abort, because it could hold the
    target or a competing body and nothing can rule that out. It has to abort
    as a controlled refusal: the decode happens while the line iterator
    advances, outside any per-line guard, so it surfaced as a traceback.
    """
    rows, bad, unreadable = [], 0, []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise TranscriptRefusal(
            f"{path} is not valid UTF-8 ({exc}); refusing rather than "
            f"searching a transcript that cannot be read in full") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            bad += 1
            unreadable.append(line)
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
            unreadable.append(line)
    return rows, bad, unreadable


def _content_blocks(entry: dict) -> list:
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def _tool_names(entries: list[dict]) -> dict[str, str]:
    """tool_use_id → tool name, read from the assistant's `tool_use` blocks."""
    names = {}
    for e in entries:
        for b in _content_blocks(e):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                names[str(b["id"])] = str(b.get("name") or "")
    return names


def _results(entries: list[dict]) -> list[dict]:
    out = []
    for e in entries:
        for b in _content_blocks(e):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append(b)
    return out


def render_body(block: dict) -> str:
    """The tool's own bytes, or a refusal naming what stopped it."""
    if block.get("is_error"):
        raise TranscriptRefusal(
            "the recorded result has is_error=true — archiving it would "
            "certify the window as covered by an error string")
    content = block.get("content")
    if isinstance(content, str):
        _refuse_if_oversized_pointer(content)
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict) or b.get("type") != _TEXT_BLOCK:
                kind = b.get("type") if isinstance(b, dict) else type(b).__name__
                raise TranscriptRefusal(
                    f"result contains a non-text block ({kind!r}); joining the "
                    f"text alone would write a SHORTER capture that still parses")
            text = b.get("text")
            if not isinstance(text, str):
                raise TranscriptRefusal(
                    f"a text block carries {type(text).__name__} instead of a "
                    f"string; `str(...)` on it would write a SHORTER or altered "
                    f"capture that still parses as JSON")
            parts.append(text)
        joined = "".join(parts)
        _refuse_if_oversized_pointer(joined)
        return joined
    raise TranscriptRefusal(
        f"unrecognised tool_result content type: {type(content).__name__}")


def _refuse_if_oversized_pointer(body: str) -> None:
    """The recorded body is a stand-in naming the real file — not the payload.

    This is the case the whole feature exists for (an oversized trades pull),
    and it is the one that looked most like success: 1.5KB written, exit 0.
    The named file IS the faithful payload the host wrote, so the refusal
    hands the operator the answer instead of a dead end.
    """
    m = _SAVED_ELSEWHERE.search(body)
    if not m:
        return
    # A body that parses as a JSON CONTAINER is a usable capture whatever it
    # says — this is what stops a legitimate payload quoting the phrase from
    # being rejected. A JSON *scalar* is not: `"Full output saved to: /x.txt"`
    # is valid JSON and was written as though it were the broker payload.
    try:
        if isinstance(json.loads(body), (dict, list)):
            return
    except ValueError:
        pass
    if m:
        # A path quoted in prose keeps its closing punctuation; a real one
        # never ends in it. Strip it, or the refusal names a file that is off
        # by one character.
        saved = m.group("path").rstrip("\"'),;")
        raise TranscriptRefusal(
            f"this result was too large to inline, so the transcript holds a "
            f"POINTER, not the payload (is_error is NOT set, so nothing else "
            f"catches it). The host already wrote the real bytes to:\n"
            f"    {saved}\n"
            f"Archive THAT file directly: `pull --response {saved}`")


def list_results(path: Path, tool_filter: str | None = None) -> tuple[list[dict], int]:
    """[{tool_use_id, tool, args, bytes, file, note}], unparseable-line count.

    Searches the SAME set `extract` does — every session under the directory,
    subagent transcripts included. They were split once (list took the newest
    session, extract took all of them), which meant a result `list` could not
    show was one `extract` would happily write: the operator sees an empty
    listing and concludes the capture is impossible.

    The tool-name map is built across ALL files BEFORE filtering, for the same
    reason: a subagent's `tool_use` and its `tool_result` can land in
    different transcripts, and a per-file map left such a row nameless — so
    `--tool get_account_trades` hid a row `extract` would write.

    `args` is in the listing because the archive derives the window it will
    forever claim to have covered from the args the operator passes to `pull`.
    Two `get_account_trades` rows differing only by `period` are otherwise
    indistinguishable, and picking the wrong one certifies a window nothing
    observed.
    """
    files = iter_transcripts(path)
    scanned, bad = [], 0
    names: dict[str, str] = {}
    args: dict[str, str] = {}
    for f in files:
        entries, n, _ = _entries(f)
        bad += n
        for e in entries:
            for blk in _content_blocks(e):
                if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("id"):
                    names[str(blk["id"])] = str(blk.get("name") or "")
                    inp = blk.get("input")
                    args[str(blk["id"])] = (
                        json.dumps(inp, sort_keys=True, separators=(",", ":"))
                        if isinstance(inp, dict) else "")
        scanned.append((f, _results(entries)))

    rows = []
    for f, blocks in scanned:
        for block in blocks:
            tuid = str(block.get("tool_use_id") or "")
            tool = names.get(tuid, "")
            if tool_filter and tool_filter.lower() not in tool.lower():
                continue
            try:
                size, note = len(render_body(block).encode("utf-8")), ""
            except TranscriptRefusal as exc:
                size, note = 0, str(exc).replace("\n", " ")[:120]
            except UnicodeEncodeError as exc:
                size, note = 0, f"body is not encodable UTF-8: {exc}"
            rows.append({"tool_use_id": tuid, "tool": tool, "args": args.get(tuid, ""),
                         "bytes": size, "file": f.name, "note": note})
    return rows, bad


def extract(path: Path, tool_use_id: str, output: Path) -> dict:
    """Write one recorded result verbatim.

    Refuses whenever which-body-to-write is genuinely in doubt: two different
    bodies under the id, or an UNPARSEABLE line that carries the id. It does
    NOT refuse on unparseable lines in general — an active session's last line
    is routinely half-written, and refusing on that would block every capture
    made while the session runs.
    """
    files = iter_transcripts(path)
    bodies: dict[str, list[str]] = {}
    tool, bad, carriers = "", 0, []
    for f in files:
        entries, n, unreadable = _entries(f)
        bad += n
        # A line that would not parse but CONTAINS the id is a competing body,
        # not noise. Writing the parseable one is then a coin flip over which
        # broker snapshot is archived, which the NOTE does not cure.
        carriers += [f.name for line in unreadable if tool_use_id in line]
        hits = [b for b in _results(entries)
                if str(b.get("tool_use_id") or "") == tool_use_id]
        if not hits:
            continue
        tool = tool or _tool_names(entries).get(tool_use_id, "")
        for h in hits:
            bodies.setdefault(render_body(h), []).append(f.name)
    if carriers:
        raise TranscriptRefusal(
            f"an unparseable line in {sorted(set(carriers))} carries "
            f"{tool_use_id!r} — it may hold a competing body for this id, and "
            f"nothing can rule that out. Most often the session is still "
            f"being written and that line is its half-finished tail — list "
            f"again once it settles. Measured on 1,453 real transcripts this "
            f"fires for 35 of ~33,000 results, so it is not a routine block")
    if not bodies:
        raise TranscriptRefusal(
            f"no tool_result with tool_use_id {tool_use_id!r} in "
            f"{len(files)} transcript(s) ({bad} unparseable line(s) were "
            f"skipped — one of them may be it)")
    if len(bodies) > 1:
        where = "; ".join(f"{sorted(set(v))} ({len(k)} bytes)"
                          for k, v in bodies.items())
        raise TranscriptRefusal(
            f"tool_use_id {tool_use_id!r} is recorded with "
            f"{len(bodies)} DIFFERENT bodies — {where}. Picking one is a coin "
            f"flip over which broker snapshot is archived; pass the exact "
            f"session file with --transcript to disambiguate")
    # Identical copies are NOT an ambiguity: Claude Code replays a result
    # verbatim just before a compact boundary, so a long session legitimately
    # records one id twice. Refusing those blocked captures whose bytes were
    # never in doubt.
    body = next(iter(bodies))
    if output.exists():
        raise TranscriptRefusal(
            f"{output} already exists — refusing to overwrite an earlier capture")
    try:
        data = body.encode("utf-8")
    except UnicodeEncodeError as exc:
        # `json.loads` accepts an escaped unpaired surrogate (\ud800); encoding
        # it does not. Without this it was an uncaught traceback.
        raise TranscriptRefusal(
            f"the recorded body is not encodable as UTF-8 ({exc}); it cannot "
            f"be written faithfully") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    # Read the bytes BACK and compare them. The earlier version described this
    # but called `stat()`, so same-length corruption passed the check that was
    # the module's whole fidelity claim.
    if output.read_bytes() != data:
        raise TranscriptRefusal(
            f"{output} does not match the recorded body after writing — the "
            f"capture is NOT faithful; delete it and retry")
    return {"bytes": len(data), "unparseable": bad, "tool": tool,
            "copies": len(next(iter(bodies.values()))),
            "searched": len(files)}
