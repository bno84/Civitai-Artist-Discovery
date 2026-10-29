"""Music Scout through the real request handler, with Civitai replaced by fakes.

Covers the routes and guards the queues depend on: the catalog, hidden creators and
images left out of a queue, the viewing levels sent upstream, viewed marks, signed-out
answers, reactions on a video only Music Scout has shown, collection markers, and the
owner-only Music Focus add with its comment-first ordering. No request leaves the test.
"""
import test_safety  # Fail closed before any test network request.

from http.server import ThreadingHTTPServer
import http.client
import json
import threading
from unittest.mock import patch

import server
from discovery import musicscout
from discovery.musicscout import H3_MUSEUM, MUSIC_FOCUS, MusicScout

FOCUS, MUSEUM = MUSIC_FOCUS["collectionId"], H3_MUSEUM["collectionId"]


def row(image_id, name, minute):
    key = f"https://image.civitai.com/key/u{image_id}/original=true/u{image_id}.mp4"
    return {"id": image_id, "postId": image_id, "username": name, "type": "video",
            "createdAt": f"2026-10-28T12:{minute:02d}:00.000Z", "url": key,
            "thumbnail": {"url": f"https://image.civitai.com/key/u{image_id}/anim=false/u{image_id}.jpeg"},
            "browsingLevel": 1, "width": 720, "height": 1280, "baseModel": "Wan",
            "stats": {"likeCount": 2, "heartCount": 1}}


class FakeArchive:
    def __init__(self):
        self.requests = []

    def _request(self, params, **_):
        self.requests.append(dict(params))
        items = [row(801, "Alpha", 50), row(802, "Hidden", 49), row(803, "Beta", 48),
                 row(804, "Gamma", 47)]
        return {"items": items, "metadata": {"nextCursor": None}}, 10


class FakeClient:
    """The signed-in Civitai client, recording every write."""
    owner = True
    calls = []
    reactions = {}
    held = {}
    fail_add = False
    fail_readback = False

    def query(self, procedure, payload):
        if procedure == "image.getInfinite":
            ids = {FOCUS: [803], MUSEUM: [801]}[payload["collectionId"]]
            return {"items": [{"id": value, "user": {"username": "Alpha"}} for value in ids],
                    "nextCursor": None}
        if procedure == "image.get":
            return self._image(payload["id"])
        raise AssertionError(procedure)

    def _image(self, image_id):
        return {"id": image_id, "reactions": [{"reaction": name} for name in
                                              sorted(self.reactions.get(image_id, set()))],
                "stats": {"likeCountAllTime": 2, "heartCountAllTime": 1}}

    def batch_query_optional(self, procedure, payloads):
        assert procedure == "image.get", procedure
        return [self._image(payload["id"]) for payload in payloads]

    def mutate(self, procedure, payload):
        assert procedure == "reaction.toggle", procedure
        FakeClient.calls.append(("react", payload["entityId"], payload["reaction"]))
        current = FakeClient.reactions.setdefault(payload["entityId"], set())
        current.symmetric_difference_update({payload["reaction"]})

    def writable_image_collections(self, user_id):
        owner = user_id if FakeClient.owner else user_id + 1
        return [{"id": FOCUS, "userId": owner, "name": "Music Focus", "read": "Public"}]

    def image_collection_ids(self, image_ids):
        if FakeClient.fail_readback and FakeClient.calls and FakeClient.calls[-1][0] == "add":
            raise RuntimeError("read-back failed")
        return {value: set(FakeClient.held.get(value, set())) for value in image_ids}

    def comment_on_image(self, image_id, content):
        FakeClient.calls.append(("comment", image_id, content))

    def add_image_to_collection(self, image_id, collection):
        if FakeClient.fail_add:
            raise RuntimeError("add failed")
        FakeClient.calls.append(("add", image_id, collection["id"]))
        FakeClient.held.setdefault(image_id, set()).add(collection["id"])


signed_in = {"value": False}


def auth_status():
    if not signed_in["value"]:
        raise RuntimeError("Civitai is not connected")
    return {"connected": True, "id": 7, "username": "Owner", "socialWrite": True,
            "collectionsWrite": True}


httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def call(method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
    try:
        data = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, data, {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        raw = response.read()
        # send_error answers with an HTML page rather than JSON.
        is_json = response.getheader("Content-Type", "").startswith("application/json")
        return response.status, json.loads(raw) if is_json else raw.decode(errors="replace")
    finally:
        connection.close()


archive = FakeArchive()
scout = MusicScout(archive, social=lambda: server.music_scout_client())
with patch.object(server, "MUSIC_SCOUT", scout), \
        patch.object(server, "auth_status", auth_status), \
        patch.object(server, "SocialClient", FakeClient), \
        patch.object(server, "hidden_preferences", lambda: ({"hidden"}, {804})), \
        patch.object(musicscout, "public_listing", side_effect=AssertionError("no hub reads here")):
    try:
        # -- catalog and public queues, signed out ------------------------------------
        status, catalog = call("GET", "/api/music/queues")
        assert status == 200, catalog
        assert [queue["id"] for queue in catalog["queues"]] == [
            "per-creator", "music-models", "latest", "music-focus", "mf-hub", "h3-museum",
            "museum-hub"], catalog
        assert [collection["key"] for collection in catalog["collections"]] == ["music-focus", "h3-museum"]

        status, page = call("GET", "/api/music/queue?queue=latest&limit=10")
        assert status == 200, page
        # Hidden creators and hidden images never reach the page.
        assert [item["id"] for item in page["items"]] == [801, 803], page
        assert archive.requests[0]["browsingLevel"] == sum(server.HISTORY.visible_levels), archive.requests
        assert archive.requests[0]["type"] == "video", archive.requests
        assert "/original=true/" in page["items"][0]["videoUrl"], page
        assert "transcode=true,width=768,optimized=true" in page["items"][0]["fallbackUrl"], page

        assert call("GET", "/api/music/queue?queue=images")[0] == 400
        status, answer = call("GET", "/api/music/queue?queue=music-focus")
        assert status == 401 and "Sign in" in answer["error"], answer
        assert call("GET", "/api/music/queue?queue=mf-hub")[0] == 401
        assert call("GET", "/api/music/collection-status?imageId=801")[0] == 401
        assert call("GET", "/api/reaction-status?imageId=801")[0] == 401
        # Signed out, adding is refused cleanly rather than as a server fault.
        status, answer = call("POST", "/api/music/focus", {"imageId": 803})
        assert status == 403 and "owner" in answer["error"], (status, answer)

        # -- viewed marks only for what was shown ---------------------------------------
        assert call("POST", "/api/music/viewed", {"imageId": 802})[0] == 400
        assert call("POST", "/api/music/viewed", {"imageId": 801})[0] == 200
        status, again = call("GET", "/api/music/queue?queue=latest&limit=10")
        assert [item["viewed"] for item in again["items"]] == [True, False], again
        # The usual browser boundary still applies to the new routes.
        assert call("POST", "/api/music/viewed", {"imageId": 801},
                    {"Origin": "http://attacker.invalid"})[0] == 403
        assert call("GET", "/api/music/queues", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403

        # -- signed in: reactions on a Music Scout video --------------------------------
        signed_in["value"] = True
        status, reactions = call("GET", "/api/reaction-status?imageId=801&imageId=803")
        assert status == 200 and set(reactions["images"]) == {"801", "803"}, reactions
        assert call("GET", "/api/reaction-status?imageId=999")[0] == 400
        status, reacted = call("POST", "/api/reaction", {"imageId": 801, "reaction": "Heart", "active": True})
        assert status == 200 and reacted["changed"] and reacted["reactions"] == ["Heart"], reacted
        assert reacted["stats"]["heartCount"] == 2 and reacted["stats"]["reactionCount"] == 4, reacted
        assert scout.served_entry(801)["stats"]["heartCount"] == 2, "a revisit must show the new count"
        assert call("POST", "/api/reaction", {"imageId": 999, "reaction": "Heart", "active": True})[0] == 400

        # -- collection markers and ownership -------------------------------------------
        status, members = call("GET", "/api/music/collection-status?imageId=801&imageId=803")
        assert status == 200, members
        assert members == {"canAdd": True, "collections": {
            "music-focus": {"801": False, "803": True},
            "h3-museum": {"801": True, "803": False}}}, members
        assert call("GET", "/api/music/collection-status?imageId=999")[0] == 400

        # Already in: nothing is posted.
        FakeClient.held = {803: {FOCUS}}
        FakeClient.calls = []
        status, added = call("POST", "/api/music/focus", {"imageId": 803})
        assert status == 200 and added["alreadyIn"] and not added["commented"], added
        assert FakeClient.calls == [], FakeClient.calls

        # A new add posts the default comment first, then adds.
        status, added = call("POST", "/api/music/focus", {"imageId": 801})
        assert status == 200 and added["inCollection"] and added["commented"], added
        assert FakeClient.calls == [("comment", 801, MUSIC_FOCUS["commentHtml"]),
                                    ("add", 801, FOCUS)], FakeClient.calls
        assert FOCUS in FakeClient.held[801]
        assert scout.collection_ids(FOCUS) == {801, 803}, "an add must update the markers at once"

        # Comment posted but the add failed: said plainly, so nobody posts a second one.
        FakeClient.held, FakeClient.calls, FakeClient.fail_add = {}, [], True
        status, answer = call("POST", "/api/music/focus", {"imageId": 801})
        assert status == 502 and "comment was posted" in answer["error"], answer
        FakeClient.fail_add = False

        # Both writes succeeded but the read-back failed: still a success.
        FakeClient.calls, FakeClient.fail_readback = [], True
        status, added = call("POST", "/api/music/focus", {"imageId": 801})
        assert status == 200 and added["inCollection"], added
        FakeClient.fail_readback = False

        assert call("POST", "/api/music/focus", {"imageId": 999})[0] == 400

        # Anyone else sees the markers read-only and cannot add.
        server.MUSIC_FOCUS_OWNER.clear()
        FakeClient.owner = False
        status, members = call("GET", "/api/music/collection-status?imageId=801")
        assert status == 200 and members["canAdd"] is False, members
        status, answer = call("POST", "/api/music/focus", {"imageId": 801})
        assert status == 403, answer
    finally:
        httpd.shutdown()
        server.MUSIC_FOCUS_OWNER.clear()
        scout.viewed_db.close()

print("music scout http: ok")
