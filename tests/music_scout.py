"""Music Scout queues request only videos and build lazily from paged feeds.

Covers the per-creator cap, the newest-first merge of several music-model feeds, that an
image row is never served even if a listing returns one, the hidden-content filter, and
that the playback file is the transcode Civitai keeps the audio track on.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.musicscout import MUSIC_MODELS, MusicScout

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

print("music scout queues: ok")
