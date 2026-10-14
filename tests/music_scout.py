"""Music Scout queues request only videos and build lazily from paged feeds.

Covers the per-creator cap, the newest-first merge of several music-model feeds, that an
image row is never served even if a listing returns one, the hidden-content filter, and
that the playback file is the transcode Civitai keeps the audio track on.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.musicscout import HubBuilding, MUSIC_FOCUS, MUSIC_MODELS, MusicScout

CDN = "https://image.civitai.com/key"


def row(image_id, name, minute, kind="video"):
    extension = "mp4" if kind == "video" else "jpeg"
    return {"id": image_id, "postId": image_id, "username": name,
            "createdAt": f"2026-10-13T12:{minute:02d}:00.000Z",
            "url": f"{CDN}/u{image_id}/original=true/u{image_id}.{extension}",
            "thumbnail": {"url": f"{CDN}/u{image_id}/anim=false,skip=4/u{image_id}.jpeg"},
            "browsingLevel": 1, "width": 720, "height": 1280, "type": kind,
            "stats": {"likeCount": 2}, "meta": None, "baseModel": "Wan"}


class FakeArchive:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def _request(self, params, **_):
        self.requests.append(dict(params))
        assert params["type"] == "video", params
        assert params["sort"] == "Newest", params
        feed = params.get("modelId", "all")
        items, cursor = self.pages.get((feed, params.get("cursor")), ([], None))
        return {"items": items, "metadata": {"nextCursor": cursor}}, 100


# -- per creator: a prolific uploader is capped, the rest still appear ----------------
pages = {
    ("all", None): ([row(10 + i, "Busy", 59 - i) for i in range(6)] + [row(30, "Quiet", 40),
                     row(31, "Pic", 39, kind="image")], "c2"),
    ("all", "c2"): ([row(40, "Busy", 30), row(41, "Other", 29), row(42, "Hidden", 28)], None),
}
archive = FakeArchive(pages)
scout = MusicScout(archive)
allow = lambda entry: entry["username"] != "Hidden"
page = scout.page("per-creator", 0, 20, per_creator=2, levels=(1, 2), allow=allow)
names = [item["username"] for item in page["items"]]
assert names == ["Busy", "Busy", "Quiet", "Other"], names
assert page["exhausted"] is True, page
assert all(request["browsingLevel"] == 3 for request in archive.requests), archive.requests
first = page["items"][0]
assert "/transcode=true,width=768,optimized=true/" in first["videoUrl"], first
assert first["stillUrl"].endswith(".jpeg") and "anim=false" in first["stillUrl"], first
assert first["civitaiUrl"].endswith("/images/10"), first

# Only videos actually handed to the page may be reacted to.
assert scout.served_entry(10)["username"] == "Busy"
assert scout.served_entry(42) is None and scout.served_entry(31) is None

# A later page reuses the same queue rather than starting again.
requests = len(archive.requests)
again = scout.page("per-creator", 2, 2, per_creator=2, levels=(1, 2), allow=allow)
assert [item["id"] for item in again["items"]] == [30, 41], again
assert len(archive.requests) == requests, "a loaded queue must not refetch"

# -- music models: separate feeds, merged newest first, duplicates once ---------------
yue, ace = (model["modelId"] for model in MUSIC_MODELS)
pages = {
    (yue, None): ([row(1, "A", 50), row(2, "B", 20)], None),
    (ace, None): ([row(3, "C", 40), row(1, "A", 50), row(4, "D", 10)], None),
}
archive = FakeArchive(pages)
page = MusicScout(archive).page("music-models", 0, 10, levels=(1, 2, 4))
assert [item["id"] for item in page["items"]] == [1, 3, 2, 4], page["items"]
assert {request["modelId"] for request in archive.requests} == {yue, ace}, archive.requests
assert page["items"][1]["source"] == "ACE-Step Audio Gen", page["items"][1]

# -- lazy: one screen costs one page --------------------------------------------------
pages = {("all", None): ([row(100 + i, f"U{i}", 59 - i) for i in range(50)], "next"),
         ("all", "next"): ([row(200, "Late", 1)], None)}
archive = FakeArchive(pages)
page = MusicScout(archive).page("latest", 0, 20)
assert len(page["items"]) == 20 and not page["exhausted"], page
assert len(archive.requests) == 1, archive.requests

try:
    MusicScout(archive).page("images", 0, 10)
except ValueError:
    pass
else:
    raise AssertionError("unknown queues must be rejected")

# -- Music Focus: the signed-in collection feed, in the order Civitai returns -------
def trpc_row(image_id, name, kind="video"):
    return {"id": image_id, "postId": image_id, "type": kind, "url": f"key-{image_id}",
            "user": {"username": name}, "createdAt": f"2025-0{image_id % 9 + 1}-01T00:00:00.000Z",
            "width": 1920, "height": 1080, "nsfwLevel": 1, "baseModel": None,
            "stats": {"likeCountAllTime": 3, "heartCountAllTime": 2}}


class FakeSocial:
    def __init__(self):
        self.calls = []

    def query(self, procedure, payload):
        self.calls.append((procedure, dict(payload)))
        assert procedure == "image.getInfinite", procedure
        assert payload["collectionId"] == MUSIC_FOCUS["collectionId"], payload
        assert payload["sort"] == "Recently Added" and payload["types"] == ["video"], payload
        if payload.get("cursor") is None:
            return {"items": [trpc_row(7, "Old"), trpc_row(3, "Older"), trpc_row(5, "Pic", "image")],
                    "nextCursor": 900}
        return {"items": [trpc_row(9, "Newer")], "nextCursor": None}


social = FakeSocial()
page = MusicScout(FakeArchive({}), social=lambda: social).page("music-focus", 0, 10)
# Added order, not upload date: the merge must not re-sort a single feed.
assert [item["id"] for item in page["items"]] == [7, 3, 9], page["items"]
assert page["exhausted"], page
entry = page["items"][0]
assert entry["username"] == "Old" and entry["stats"]["reactionCount"] == 5, entry
assert entry["videoUrl"] == ("https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/key-7/"
                             "transcode=true,width=768,optimized=true/key-7.mp4"), entry
assert entry["stillUrl"].endswith("/key-7.jpeg") and "anim=false" in entry["stillUrl"], entry


def signed_out():
    raise PermissionError("Sign in")


try:
    MusicScout(FakeArchive({}), social=signed_out).page("music-focus", 0, 10)
except PermissionError:
    pass
else:
    raise AssertionError("Music Focus must require sign-in")

# -- the default comment links to the collection, as the owner's own comments do -----
assert ('<a target="_blank" rel="ugc" href="https://civitai.red/collections/17980935">'
        '<strong>Music Focus</strong> 🎸</a>') in MUSIC_FOCUS["commentHtml"], MUSIC_FOCUS["commentHtml"]


# -- membership comes from the collection itself, cached between pages -----------------
class MemberSocial:
    def __init__(self):
        self.calls = 0

    def query(self, procedure, payload):
        self.calls += 1
        if payload.get("cursor") is None:
            return {"items": [{"id": 1}, {"id": 2}], "nextCursor": 5}
        return {"items": [{"id": 3}], "nextCursor": None}


members = MemberSocial()
scout = MusicScout(FakeArchive({}), social=lambda: members)
assert scout.focus_ids() == {1, 2, 3} and members.calls == 2
scout.mark_focus(4)
assert scout.focus_ids() == {1, 2, 3, 4} and members.calls == 2, "a fresh cache must not refetch"

# -- viewed videos persist and are flagged on every later answer ----------------------
import tempfile
with tempfile.TemporaryDirectory() as folder:
    pages = {("all", None): ([row(500 + i, f"V{i}", 59 - i) for i in range(4)], None)}
    first = MusicScout(FakeArchive(pages), root=Path(folder))
    page = first.page("latest", 0, 10)
    assert not any(item["viewed"] for item in page["items"]), page
    first.mark_viewed(501)
    assert "viewed" not in first.served_entry(501), "the shared entry must not carry the flag"
    again = MusicScout(FakeArchive(pages), root=Path(folder)).page("latest", 0, 10)
    assert [item["viewed"] for item in again["items"]] == [False, True, False, False], again
    first.viewed_db.close()

# -- MF hub: newest videos from Music Focus creators, capped, kept between sessions ----
class HubSocial:
    def query(self, procedure, payload):
        return {"items": [{"id": 1, "user": {"username": "Ana"}}, {"id": 2, "user": {"username": "Bo"}},
                          {"id": 3, "user": {"username": "ana"}}], "nextCursor": None}


class HubArchive:
    def __init__(self):
        self.users = []

    def _request(self, params, **_):
        assert params["type"] == "video" and params["sort"] == "Newest", params
        self.users.append(params["username"])
        videos = {"Ana": [row(901, "Ana", 50), row(902, "Ana", 40), row(903, "Ana", 10)],
                  "Bo": [row(911, "Bo", 45)]}[params["username"]]
        return {"items": videos[:params["limit"]], "metadata": {}}, 100


import time as _time
import discovery.musicscout as musicscout
with tempfile.TemporaryDirectory() as folder:
    archive = HubArchive()
    # The first build reads in parallel outside the lane; route it to the same fake.
    musicscout.public_listing = lambda params: archive._request(params)[0]
    hub = MusicScout(archive, social=lambda: HubSocial(), root=Path(folder))
    try:
        hub.page("mf-hub", 0, 10, per_creator=2)
    except HubBuilding as building:
        assert building.total == 2, building.total  # "Ana" and "ana" are one creator
    else:
        raise AssertionError("the first open must report that creators are being read")
    for _ in range(100):
        if not hub.hub_job["running"]:
            break
        _time.sleep(0.02)
    page = hub.page("mf-hub", 0, 10, per_creator=2, refresh=True)
    assert [item["id"] for item in page["items"]] == [901, 911, 902], page["items"]
    assert sorted(archive.users) == ["Ana", "Bo"], archive.users
    # Saved: a new session serves straight away, with no further requests.
    later = MusicScout(HubArchive(), social=lambda: HubSocial(), root=Path(folder))
    assert [item["id"] for item in later.page("mf-hub", 0, 10, per_creator=1)["items"]] == [901, 911]
    assert later.archive.users == [], later.archive.users
    # Older than the freshness window: caught up from the global feed before serving.
    # Only the pages since the last read are asked for, and only Music Focus creators kept.
    with later.viewed_lock, later.viewed_db:
        later.viewed_db.execute("UPDATE hub_latest SET fetched_at = ?", (_time.time() - 3600,))
    feed_calls = []

    def feed(params):
        assert "username" not in params and params["type"] == "video", params
        feed_calls.append(params.get("cursor"))
        fresh = dict(row(950, "Ana", 59)); fresh["createdAt"] = "2099-01-01T00:00:00.000Z"
        stranger = dict(row(951, "Zed", 58)); stranger["createdAt"] = "2099-01-01T00:00:00.000Z"
        # The cursor's ordering time is two hours ago: older than the last read, so done.
        cursor = f"feed:{int((_time.time() - 7200) * 1000)}:1"
        return {"items": [fresh, stranger], "metadata": {"nextCursor": cursor}}

    musicscout.public_listing = feed
    caught = later.page("mf-hub", 0, 10, per_creator=1, refresh=True)
    assert [item["id"] for item in caught["items"]] == [950, 911], caught["items"]
    assert feed_calls == [None], feed_calls
    assert later.archive.users == [], "catching up must not reread every creator"
    # Just caught up: the next open reads nothing at all.
    later.page("mf-hub", 0, 10, per_creator=1, refresh=True)
    assert feed_calls == [None], feed_calls

    # A gap the feed cannot cover in its page budget falls back to rereading everyone.
    with later.viewed_lock, later.viewed_db:
        later.viewed_db.execute("UPDATE hub_latest SET fetched_at = ?", (_time.time() - 30 * 86400,))
    feed_calls.clear()

    def endless(params):
        if "username" in params:
            return archive._request(params)[0]
        feed_calls.append(params.get("cursor"))
        return {"items": [row(960 + len(feed_calls), "Zed", 1)],
                "metadata": {"nextCursor": f"feed:{int(_time.time() * 1000)}:{len(feed_calls)}"}}

    musicscout.public_listing = endless
    try:
        later.page("mf-hub", 0, 10, per_creator=1, refresh=True)
    except HubBuilding:
        pass
    else:
        raise AssertionError("a gap too long for the feed must reread every creator")
    assert len(feed_calls) == musicscout.HUB_CATCHUP_PAGES, len(feed_calls)
    for _ in range(100):
        if not later.hub_job["running"]:
            break
        _time.sleep(0.02)
    hub.viewed_db.close(); later.viewed_db.close()

print("music scout queues: ok")
