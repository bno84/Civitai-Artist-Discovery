"""Image comments use the deployed v3 API and failed writes are never replayed."""
import test_safety

import json
from unittest.mock import patch

from discovery import social
from discovery.musicscout import MUSIC_FOCUS


client = social.SocialClient()
requests = []


def request(value):
    requests.append(value)
    return {"result": {"data": {"json": {"id": 42}}}}


with patch.object(client, "_request", side_effect=request):
    assert client.comment_on_image(801, MUSIC_FOCUS["commentHtml"]) == {"id": 42}

assert len(requests) == 1
assert requests[0].full_url == social.BASE_URL + "/api/trpc/commentv3.create"
assert requests[0].get_method() == "POST"
assert json.loads(requests[0].data) == {"json": {
    "entityId": 801, "entityType": "image", "content": MUSIC_FOCUS["commentHtml"]}}

for status in (401, 404, 429, 500):
    error = social.CivitaiHTTPError(status, "Comment unavailable")
    with patch.object(social, "get_access_token", return_value="test-token"), \
         patch.object(client, "_send", side_effect=error) as send:
        try:
            client.comment_on_image(801, MUSIC_FOCUS["commentHtml"])
        except social.CivitaiHTTPError as raised:
            assert raised is error
        else:
            raise AssertionError("Comment failure must reach the caller")
        assert send.call_count == 1, "Never replay a comment after an uncertain outcome"

print("social comments: v3 request, HTML preserved, failed writes never replayed")
