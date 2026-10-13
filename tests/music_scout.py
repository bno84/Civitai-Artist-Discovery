"""Music Scout queues request only videos and build lazily from paged feeds.

Covers the per-creator cap, the newest-first merge of several music-model feeds, that an
image row is never served even if a listing returns one, the hidden-content filter, and
that the playback file is the transcode Civitai keeps the audio track on.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.musicscout import MUSIC_FOCUS, MUSIC_MODELS, MusicScout, focus_comment_html

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

# -- the comment that goes with an add ----------------------------------------------
posted = focus_comment_html(MUSIC_FOCUS["commentTemplate"] + "\n\n<b>Nice</b> & loud")
assert posted.startswith('<p>Your track has <a target="_blank" rel="ugc" '
                         'href="https://civitai.red/collections/17980935"><strong>Music Focus'
                         '</strong> 🎸</a> and is added'), posted
assert posted.endswith("<p>&lt;b&gt;Nice&lt;/b&gt; &amp; loud</p>"), posted
assert posted.count("<p>") == 2, posted


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

print("music scout queues: ok")
