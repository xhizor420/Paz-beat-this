"""Which category a clip belongs in, from its e621 tags.

The categories, in priority order: MLP Futa, MLP, Furry Futa, Furry.

- Futa: gynomorph, herm, or any other intersex tag. Femboy and
  crossgender without an intersex tag are male, not futa.
- MLP: a My Little Pony copyright.
- Everything else is Furry - human-only clips included (a missed tag).

Grabber saves a post under whichever of its searches found it, so a post
two searches find lands in two folders, and a post a search finds for
the wrong reason lands in the wrong one. Convert sorts by these rules
instead (route).
"""

from __future__ import annotations

FUTA_TAGS = {"gynomorph", "herm", "maleherm", "andromorph", "intersex"}
MLP_COPYRIGHTS = {"my_little_pony", "friendship_is_magic", "mlp_g5",
                  "equestria_girls", "my_little_pony:_a_new_generation"}

CATEGORIES = ("MLP Futa", "MLP", "Furry Futa", "Furry")


def is_futa(tags) -> bool:
    return bool(set(tags) & FUTA_TAGS)


def is_mlp(record: dict) -> bool:
    """A My Little Pony copyright. Not "and a pony species": most MLP clips
    are original characters e621 tags equid or equine, Equestria Girls
    characters end "(eg)", and zebras, deer-ponies and goats live in that
    world too - a species test sent 26 real MLP clips to Furry."""
    tags = set((record.get("tags") or "").split())
    copyrights = set(record.get("copyright") or []) | tags
    return bool(copyrights & MLP_COPYRIGHTS)


def category(record: dict | None) -> str:
    """The folder a clip with this e621 record belongs in; "" without one."""
    if not record or record.get("missing"):
        return ""
    tags = set((record.get("tags") or "").split())
    mlp, futa = is_mlp(record), is_futa(tags)
    if mlp:
        return "MLP Futa" if futa else "MLP"
    return "Furry Futa" if futa else "Furry"


def wanted(pid: str, record: dict | None, overrides: dict | None = None) -> str:
    """category(), unless this one is set to stay where it is
    (config category_overrides)."""
    return (overrides or {}).get(pid or "") or category(record)


def route(folder: str, pid: str, record: dict | None, folders,
          overrides: dict | None = None) -> str:
    """The folder Convert should put a clip Grabber saved in `folder`:
    the one its tags say, when that is one of your categories - else
    `folder` as it is. A clip with no tags yet stays where Grabber put it."""
    if folder not in CATEGORIES:
        return folder
    want = wanted(pid, record, overrides)
    return want if want and want in folders else folder
