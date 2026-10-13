"""Rapid-fire video queues for finding work with music.

Music can only ride on a video, so every queue asks Civitai for ``type=video`` and never
requests an image row. Queues are built lazily: a page is fetched only when the reader is
close to the end of what is already loaded, and everything is held in memory, because a
queue is a browsing session rather than something worth keeping.

Measured against civitai.red in Oct 2026:

- ``type=video`` on ``/api/v1/images`` returns only videos; ``types=video`` is ignored.
- ``modelId`` returns work that attributes any version of that model, and combines with
  ``type`` and ``browsingLevel``. It takes one id: ``modelId=a,b`` is HTTP 400, so several
  models are separate feeds merged here, newest first.
- The ``transcode=true,width=N,optimized=true`` playback file keeps the AAC audio track.
- ``anim=false,width=N`` redirects some videos to their original MP4 (30 MB and more),
  while the listing's own ``thumbnail.url`` is a JPEG for the same videos, so it is the
  still used here.
"""

from __future__ import annotations

import threading

from .civitai import normalize
from .history import video_playback_url, video_still_url
from .site import image_url, profile_url


PAGE_SIZE = 100
# Bounds the requests one call may make. A strict per-creator cap over a feed dominated
# by a few prolific uploaders can otherwise page for a long time to fill one screen.
MAX_PAGES_PER_CALL = 4
PLAYBACK_WIDTH = 768
STILL_WIDTH = 450
PER_CREATOR_DEFAULT = 3
PER_CREATOR_MAX = 50

# Audio generation models whose attributed videos are likely to carry music. ``modelId``
# covers every version, so a new release of the same model needs no change here.
MUSIC_MODELS = (
    {"modelId": 2944296, "name": "YuE2 3B", "url": "https://civitai.red/models/2944296"},
    {"modelId": 2549270, "name": "ACE-Step Audio Gen", "url": "https://civitai.red/models/2549270"},
)

QUEUES = (
    {"id": "per-creator", "name": "Latest, a few per creator",
     "description": "Newest videos, capped per creator so one busy uploader cannot fill the queue.",
     "perCreator": True},
    {"id": "music-models", "name": "Music model attributed",
     "description": "Newest videos that credit a music generation model.",
     "models": [model["name"] for model in MUSIC_MODELS]},
    {"id": "latest", "name": "Latest videos",
     "description": "Every new video, newest first."},
)
QUEUE_IDS = {queue["id"] for queue in QUEUES}


def _still(raw: dict, url: str) -> str:
    thumbnail = raw.get("thumbnail") if isinstance(raw.get("thumbnail"), dict) else {}
    return str(thumbnail.get("url") or "") or video_still_url(url, STILL_WIDTH)


def video_entry(raw: dict, source: str | None = None) -> dict | None:
    """The playback view of one listing row, or None for anything that is not a video."""
    if raw.get("type") != "video" or not raw.get("url"):
        return None
    try:
        item = normalize(raw)
    except (TypeError, ValueError, KeyError):
        return None
    entry = {
        "id": item["id"], "postId": item["postId"], "username": item["username"],
        "profileUrl": profile_url(item["username"]),
        "createdAt": item["createdAt"], "civitaiUrl": image_url(item["id"]),
        "width": item["width"], "height": item["height"],
        "browsingLevel": item["browsingLevel"], "baseModel": item["baseModel"],
        "stats": item["stats"], "stillUrl": _still(raw, item["url"]),
        "videoUrl": video_playback_url(item["url"], PLAYBACK_WIDTH),
    }
    if source:
        entry["source"] = source
    return entry


class Feed:
    """One Civitai listing, paged newest first."""

    def __init__(self, request, params: dict, source: str | None = None):
        self.request = request
        self.params = params
        self.source = source
        self.cursor = None
        self.buffer: list[dict] = []
        self.exhausted = False

    def fetch(self) -> None:
        params = {**self.params, "type": "video", "sort": "Newest",
                  "limit": PAGE_SIZE, "withMeta": "false"}
        if self.cursor:
            params["cursor"] = self.cursor
        payload, _ = self.request(params)
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        cursor = (payload.get("metadata") or {}).get("nextCursor")
        self.buffer.extend(entry for entry in (video_entry(raw, self.source) for raw in items)
                           if entry)
        # A page with no rows ends the feed even if a cursor came back, so a misbehaving
        # cursor cannot spin forever.
        self.exhausted = not cursor or not items
        self.cursor = cursor


class Queue:
    """Loaded entries plus the feeds that extend them."""

    def __init__(self, feeds: list[Feed], per_creator: int | None = None,
                 allow=lambda entry: True):
        self.feeds = feeds
        self.per_creator = per_creator
        self.allow = allow
        self.items: list[dict] = []
        self.seen: set[int] = set()
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()

    @property
    def exhausted(self) -> bool:
        return all(feed.exhausted and not feed.buffer for feed in self.feeds)

    def _accept(self, entry: dict) -> None:
        if entry["id"] in self.seen or not self.allow(entry):
            return
        key = entry["username"].casefold()
        if self.per_creator and self.counts.get(key, 0) >= self.per_creator:
            return
        self.seen.add(entry["id"])
        self.counts[key] = self.counts.get(key, 0) + 1
        self.items.append(entry)

    def _next(self, budget: list[int]) -> dict | None:
        """The newest buffered entry across feeds, fetching where a feed has run dry.

        A feed with an empty buffer is refilled before choosing, otherwise an older entry
        from another feed could be emitted ahead of a newer one still unfetched.
        """
        for feed in self.feeds:
            if not feed.buffer and not feed.exhausted:
                if budget[0] <= 0:
                    return None
                budget[0] -= 1
                feed.fetch()
        ready = [feed for feed in self.feeds if feed.buffer]
        if not ready:
            return None
        newest = max(ready, key=lambda feed: str(feed.buffer[0].get("createdAt") or ""))
        return newest.buffer.pop(0)

    def ensure(self, count: int) -> None:
        with self.lock:
            budget = [MAX_PAGES_PER_CALL]
            while len(self.items) < count:
                entry = self._next(budget)
                if entry is None:
                    return
                self._accept(entry)

    def page(self, offset: int, limit: int) -> dict:
        self.ensure(offset + limit)
        with self.lock:
            return {"items": self.items[offset:offset + limit], "total": len(self.items),
                    "exhausted": self.exhausted}


class MusicScout:
    """The queues in use, keyed by everything that changes what they contain."""

    def __init__(self, archive):
        # The archive's request path carries the shared API lane and adaptive pacing, so a
        # queue page waits its turn behind a day build rather than racing it.
        self.archive = archive
        self.queues: dict[tuple, Queue] = {}
        self.lock = threading.Lock()

    def _build(self, queue_id: str, per_creator: int, mask: int, allow) -> Queue:
        request = self.archive._request
        base = {"browsingLevel": mask}
        if queue_id == "music-models":
            return Queue([Feed(request, {**base, "modelId": model["modelId"]}, model["name"])
                          for model in MUSIC_MODELS], allow=allow)
        if queue_id == "per-creator":
            return Queue([Feed(request, base)], per_creator=per_creator, allow=allow)
        return Queue([Feed(request, base)], allow=allow)

    def page(self, queue_id: str, offset: int = 0, limit: int = 20,
             per_creator: int = PER_CREATOR_DEFAULT, levels=(1, 2), allow=lambda entry: True,
             refresh: bool = False) -> dict:
        if queue_id not in QUEUE_IDS:
            raise ValueError("Unknown queue")
        per_creator = min(PER_CREATOR_MAX, max(1, int(per_creator)))
        offset, limit = max(0, int(offset)), min(100, max(1, int(limit)))
        mask = sum(levels)
        key = (queue_id, per_creator if queue_id == "per-creator" else None, mask)
        with self.lock:
            if refresh or key not in self.queues:
                self.queues[key] = self._build(queue_id, per_creator, mask, allow)
            queue = self.queues[key]
        queue.allow = allow
        return {"queue": queue_id, "offset": offset, **queue.page(offset, limit)}

    @staticmethod
    def catalog() -> dict:
        return {"queues": list(QUEUES), "musicModels": list(MUSIC_MODELS),
                "perCreatorDefault": PER_CREATOR_DEFAULT, "perCreatorMax": PER_CREATOR_MAX}
