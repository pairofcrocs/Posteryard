from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from posteryard import apple, overrides, pipeline
from posteryard.artwork import MemoryChoices, rejection
from posteryard.fanart import is_fanart
from posteryard.server import Item, labels
from posteryard.store import Status, Store
from posteryard.tmdb import ImageRef, Kind


class ReadOnlyChoices:
    """Reads the service's cached choices. New ones stay in memory."""

    def __init__(self, store: Store) -> None:
        self.store, self.memory = store, MemoryChoices()

    def get_choice(self, key: str) -> Mapping[str, Any] | None:
        return self.memory.get_choice(key) or self.store.get_choice(key)

    def put_choice(self, key: str, value: Mapping[str, Any]) -> None:
        self.memory.put_choice(key, value)


@dataclass(frozen=True)
class Candidate:
    ref: ImageRef
    reason: str | None


@dataclass
class Report:
    art: str
    note: str
    logo: str | None
    skipped: frozenset[str] = frozenset()
    notices: list[str] = field(default_factory=list)
    groups: list[tuple[str, list[Candidate]]] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)


def report(ctx: pipeline.Context, item: Item, store: Store) -> Report:
    key = str(item["ratingKey"])
    poster = next((p for p in pipeline.plan_item(ctx, item) if p.target == "poster"), None)
    if poster is None:
        raise pipeline.NotFoundError(f"{item.get('title')} has no poster")
    result = Report(str(poster.inputs["art"]), poster.notes[0], poster.inputs.get("logo"))
    if overrides.IGNORE_LABEL in labels(item):
        result.notices.append(f"{overrides.IGNORE_LABEL} is set: Posteryard leaves this poster alone.")
    record = store.get(key, "poster")
    if record is not None and record.status == Status.MANUAL:
        result.notices.append("The poster was changed by hand. Posteryard leaves it alone until `forget`.")
    override = ctx.sources.overrides(key)
    if override and override.custom:
        result.notices.append(
            f"Custom art from {override.source or 'a command'}. `art reset` returns to automatic art."
        )
        return result
    if override and override.skip:
        result.skipped = override.skip
        result.notices.append(f"`art next` skipped {len(override.skip)} image(s). `art reset` returns to the first.")
    if not ctx.sources.text_check:
        result.notices.append("TEXT_CHECK is off: art is not checked for printed titles.")
    _add_candidates(ctx, item, result)
    return result


def _add_candidates(ctx: pipeline.Context, item: Item, result: Report) -> None:
    titles = _titles(ctx, item)
    read = ctx.sources.picker.read
    checked: set[str] = set()
    found = False
    for name, refs in pipeline.art_candidates(ctx, item, next_art=bool(result.skipped)):
        fresh = [ref for ref in refs if ref.path not in checked]
        if found:
            if fresh:
                result.unchecked.append(name)
            continue
        candidates = [Candidate(ref, rejection(read(ctx.sources.fetch(ref.path)), titles)) for ref in fresh]
        checked.update(ref.path for ref in fresh)
        if candidates:
            result.groups.append((name, candidates))
        found = result.art in checked


def _titles(ctx: pipeline.Context, item: Item) -> list[str]:
    show = ctx.server.item(str(item["parentRatingKey"])) if item.get("type") == "season" and ctx.server else None
    source = show or item
    kind: Kind = "movie" if source.get("type") == "movie" else "tv"
    tid = pipeline.resolve_tmdb(ctx, source, kind)
    if tid is None:
        return [str(source.get("title", ""))]
    return ctx.sources.title(kind, tid, str(source.get("title", ""))).all_titles


def lines(name: str, result: Report) -> list[str]:
    out = [name, f"  Uses: {result.note}"]
    out.append(f"  Logo: {pipeline.art_source(result.logo)}" if result.logo else "  Logo: the name in text")
    out += [f"  {notice}" for notice in result.notices]
    for group, candidates in result.groups:
        out += ["", f"  {group}, best voted first:"]
        for number, candidate in enumerate(candidates, 1):
            ref, said = candidate.ref, verdict(result, candidate)
            out.append(f"  {number:>3}. {_name(ref.path)}  {ref.width}x{ref.height}  {votes(ref)}  {said}")
    if result.unchecked:
        out += ["", f"  Not checked, art was found earlier: {', '.join(result.unchecked)}."]
    return out


def verdict(result: Report, candidate: Candidate) -> str:
    """Why a candidate was used or not: "used", "skipped by art next", the reason it was rejected, or "clean"."""
    if candidate.ref.path in result.skipped:
        return "skipped by art next"
    return "used" if candidate.ref.path == result.art else candidate.reason or "clean"


def votes(ref: ImageRef) -> str:
    if apple.is_apple(ref.path):
        return "Apple TV"
    if is_fanart(ref.path):
        return f"{ref.vote_count} like{'' if ref.vote_count == 1 else 's'}"
    return (
        f"rated {ref.votes:.1f} ({ref.vote_count} vote{'' if ref.vote_count == 1 else 's'})"
        if ref.vote_count
        else "no votes"
    )


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path.startswith(("http://", "https://")) else path
