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
- A collection in "Recently Added" order is only available from the signed-in tRPC
  ``image.getInfinite``. The public API ignores ``collectionId``: without a sort it serves
  the global most-reacted feed, and with ``sort=Newest`` the global newest feed.
- tRPC rows carry ``metadata.audio``, but it is not reliable: a video marked
  ``audio: false`` had an AAC track in both its original and transcoded files.
"""

from __future__ import annotations

import threading

from pathlib import Path
import sqlite3
import time
import urllib.parse

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

# The account's own curated collection. Its owner can add a video from any queue.
MUSIC_FOCUS = {"collectionId": 17980935, "name": "Music Focus 🎸",
               "url": "https://civitai.red/collections/17980935",
               # The comment the owner leaves on every video they add, in the markup
               # Civitai's own editor produced for it. The link to the collection is the
               # point of the comment. It is posted exactly as is; anything different is
               # written on Civitai itself.
               "commentHtml": '<p>Your track has <a target="_blank" rel="ugc" '
                              'href="https://civitai.red/collections/17980935"><strong>Music '
                              'Focus</strong> 🎸</a> and is added to the collection! 🎧 Give it '
                              'a listen and share more jams if you like.</p>'}
# Membership is read from the collection itself, so anyone signed in sees the same
# answer the owner does. Reread at most this often; an add made here updates it at once.
FOCUS_TTL_SECONDS = 600
FOCUS_PAGE_SIZE = 200
FOCUS_MAX_PAGES = 50
CDN_ROOT = "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA"

QUEUES = (
    {"id": "per-creator", "name": "Latest, a few per creator",
     "description": "Newest videos, capped per creator so one busy uploader cannot fill the queue.",
     "perCreator": True},
    {"id": "music-models", "name": "Music model attributed",
     "description": "Newest videos that credit a music generation model.",
     "models": [model["name"] for model in MUSIC_MODELS]},
    {"id": "latest", "name": "Latest videos",
     "description": "Every new video, newest first."},
    {"id": "music-focus", "name": MUSIC_FOCUS["name"],
     "description": "Your Music Focus collection, most recently added first. Needs sign-in.",
     "signIn": True},
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


def collection_entry(raw: dict) -> dict | None:
    """The playback view of one signed-in ``image.getInfinite`` row.

    These rows name the file by its CDN key rather than a URL, nest the creator under
    ``user`` and report stats as ``*AllTime``, so they are mapped onto the shape the
    public listing produces before the shared conversion runs.
    """
    key = str(raw.get("url") or "")
    if raw.get("type") != "video" or not key or "/" in key:
        return None
    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    quoted = urllib.parse.quote(key)
    listing = {
        "id": raw.get("id"), "postId": raw.get("postId"), "type": "video",
        "username": (raw.get("user") or {}).get("username") or "Unknown",
        "createdAt": raw.get("createdAt"), "width": raw.get("width"), "height": raw.get("height"),
        "browsingLevel": raw.get("nsfwLevel"), "baseModel": raw.get("baseModel"),
        "url": f"{CDN_ROOT}/{quoted}/original=true/{quoted}.mp4",
        "thumbnail": {"url": f"{CDN_ROOT}/{quoted}/anim=false,transcode=true,optimized=true,"
                             f"width={STILL_WIDTH}/{quoted}.jpeg"},
        "stats": {name: stats.get(f"{name}AllTime") or 0 for name in
                  ("likeCount", "heartCount", "laughCount", "cryCount", "commentCount")},
    }
    return video_entry(listing)


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


class CollectionFeed(Feed):
    """One collection, most recently added first, read through the signed-in client."""

    def __init__(self, social, collection_id: int, mask: int):
        super().__init__(None, {})
        self.social = social
        self.collection_id = collection_id
        self.mask = mask

    def fetch(self) -> None:
        payload = {"collectionId": self.collection_id, "sort": "Recently Added",
                   "period": "AllTime", "types": ["video"], "limit": PAGE_SIZE,
                   "browsingLevel": self.mask}
        if self.cursor:
            payload["cursor"] = self.cursor
        value = self.social().query("image.getInfinite", payload)
        value = value if isinstance(value, dict) else {}
        items = value.get("items") if isinstance(value.get("items"), list) else []
        self.buffer.extend(entry for entry in (collection_entry(raw) for raw in items) if entry)
        self.cursor = value.get("nextCursor")
        self.exhausted = not self.cursor or not items


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

    def __init__(self, archive, social=None, root: Path | None = None):
        # The archive's request path carries the shared API lane and adaptive pacing, so a
        # queue page waits its turn behind a day build rather than racing it.
        self.archive = archive
        # Returns a signed-in client, or raises PermissionError when there is none.
        self.social = social
        self.queues: dict[tuple, Queue] = {}
        # Every video handed to the page, by id. Reacting is limited to these, the same
        # way it is limited to collected images elsewhere, and a reaction updates the
        # counts here so a revisit shows them.
        self.served: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.focus_ids_cache: set[int] | None = None
        self.focus_loaded_at = 0.0
        self.focus_lock = threading.Lock()
        # Which videos the reader has already watched, kept across sessions so a queue
        # can open on the first one not seen yet. Its own small database: it is personal
        # history, unrelated to the daily archive or the taste profile.
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        self.viewed_db = sqlite3.connect(":memory:" if root is None else root / "musicscout.sqlite3",
                                         check_same_thread=False)
        self.viewed_lock = threading.Lock()
        with self.viewed_lock, self.viewed_db:
            self.viewed_db.execute("CREATE TABLE IF NOT EXISTS viewed("
                                   "image_id INTEGER PRIMARY KEY, viewed_at TEXT NOT NULL)")

    def viewed_ids(self, image_ids) -> set[int]:
        ids = [int(value) for value in image_ids]
        if not ids:
            return set()
        with self.viewed_lock:
            marks = ",".join("?" * len(ids))
            return {row[0] for row in self.viewed_db.execute(
                f"SELECT image_id FROM viewed WHERE image_id IN ({marks})", ids)}

    def mark_viewed(self, image_id: int) -> None:
        with self.viewed_lock, self.viewed_db:
            self.viewed_db.execute("INSERT OR IGNORE INTO viewed(image_id, viewed_at) VALUES(?, ?)",
                                   (int(image_id), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))

    def _build(self, queue_id: str, per_creator: int, mask: int, allow) -> Queue:
        request = self.archive._request
        base = {"browsingLevel": mask}
        if queue_id == "music-models":
            return Queue([Feed(request, {**base, "modelId": model["modelId"]}, model["name"])
                          for model in MUSIC_MODELS], allow=allow)
        if queue_id == "music-focus":
            if self.social is None:
                raise PermissionError("Sign in to Civitai to load the Music Focus collection")
            self.social()  # Fail now, not on the first page, when nobody is signed in.
            return Queue([CollectionFeed(self.social, MUSIC_FOCUS["collectionId"], mask)],
                         allow=allow)
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
        result = queue.page(offset, limit)
        with self.lock:
            self.served.update((entry["id"], entry) for entry in result["items"])
        viewed = self.viewed_ids(entry["id"] for entry in result["items"])
        # A copy per answer: the flag belongs to this moment, not to the shared entry.
        result["items"] = [{**entry, "viewed": entry["id"] in viewed} for entry in result["items"]]
        return {"queue": queue_id, "offset": offset, **result}

    def focus_ids(self) -> set[int]:
        """Every video id in Music Focus. Needs a signed-in client, but not the owner's."""
        with self.focus_lock:
            if self.focus_ids_cache is not None and \
                    time.monotonic() - self.focus_loaded_at < FOCUS_TTL_SECONDS:
                return set(self.focus_ids_cache)
            client = self.social()
            ids: set[int] = set()
            cursor = None
            for _ in range(FOCUS_MAX_PAGES):
                payload = {"collectionId": MUSIC_FOCUS["collectionId"], "sort": "Recently Added",
                           "period": "AllTime", "types": ["video"], "limit": FOCUS_PAGE_SIZE,
                           "browsingLevel": 31}
                if cursor:
                    payload["cursor"] = cursor
                value = client.query("image.getInfinite", payload)
                value = value if isinstance(value, dict) else {}
                items = value.get("items") if isinstance(value.get("items"), list) else []
                ids.update(int(row["id"]) for row in items if isinstance(row, dict) and row.get("id"))
                cursor = value.get("nextCursor")
                if not cursor or not items:
                    break
            self.focus_ids_cache, self.focus_loaded_at = ids, time.monotonic()
            return set(ids)

    def mark_focus(self, image_id: int) -> None:
        with self.focus_lock:
            if self.focus_ids_cache is not None:
                self.focus_ids_cache.add(int(image_id))

    def served_entry(self, image_id: int) -> dict | None:
        with self.lock:
            return self.served.get(int(image_id))

    @staticmethod
    def catalog() -> dict:
        return {"queues": list(QUEUES), "musicModels": list(MUSIC_MODELS),
                "musicFocus": MUSIC_FOCUS,
                "perCreatorDefault": PER_CREATOR_DEFAULT, "perCreatorMax": PER_CREATOR_MAX}
