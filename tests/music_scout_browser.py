"""The Music Scout tab in the 3.0 shell, against synthetic API answers.

Only static assets are served; every API, video and thumbnail response is a fixture, so
no app server, account or external connection is involved. Playback itself is not
exercised: headless Chromium is given no real video, so this covers which file each
player element holds rather than whether it plays.
"""
import test_safety  # Fail closed before any test network request.

from functools import partial
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright
from v2_visual_baseline import ROOT, StaticHandler, install_fixture

from discovery.musicscout import MusicScout

CATALOG = MusicScout.catalog()
TOTAL = 30
VIEWED = {7001, 7002}
FOCUS_MEMBER = {7004}


def video(index):
    image_id = 7000 + index
    key = f"https://image.civitai.com/scout/u{image_id}"
    return {"id": image_id, "postId": image_id, "username": f"Composer{index % 7}",
            "profileUrl": f"https://civitai.red/user/Composer{index % 7}",
            "createdAt": f"2026-10-28T12:{59 - index:02d}:00.000Z",
            "civitaiUrl": f"https://civitai.red/images/{image_id}",
            "width": 720, "height": 1280, "browsingLevel": 1, "baseModel": "Wan",
            "stats": {"likeCount": 3, "heartCount": 2, "laughCount": 0, "cryCount": 0},
            "stillUrl": f"{key}/still.svg",
            "videoUrl": f"{key}/transcode=true,width=768,optimized=true/u{image_id}.mp4",
            "viewed": image_id in VIEWED}


QUEUE = [video(index) for index in range(1, TOTAL + 1)]
# Every file here fails to load; only this one has a transcode to fall back to.
QUEUE[1]["fallbackUrl"] = QUEUE[1]["videoUrl"].replace("/transcode=", "/fallback,transcode=")

server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticHandler, directory=str(ROOT / "static")))
threading.Thread(target=server.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{server.server_port}"
requests = {"queue": [], "viewed": [], "reaction": [], "focus": []}
reacted = {}


def music_routes(route):
    request = route.request
    parsed = urlparse(request.url)
    if parsed.hostname == "image.civitai.com" and parsed.path.startswith("/scout/"):
        if parsed.path.endswith(".svg"):
            route.fulfill(content_type="image/svg+xml",
                          body='<svg xmlns="http://www.w3.org/2000/svg" width="96" height="64">'
                               '<rect width="96" height="64" fill="#365e4d"/></svg>')
        else:
            route.fulfill(status=404, body="")
        return
    query = parse_qs(parsed.query)
    path = parsed.path
    if path == "/api/music/queues":
        data = CATALOG
    elif path == "/api/music/queue":
        requests["queue"].append(query)
        offset, limit = int(query["offset"][0]), int(query["limit"][0])
        items = QUEUE[offset:offset + limit]
        data = {"queue": query["queue"][0], "offset": offset, "items": items, "total": offset + len(items),
                "exhausted": offset + limit >= TOTAL}
    elif path == "/api/music/viewed":
        body = json.loads(request.post_data)
        requests["viewed"].append(body["imageId"])
        data = {"imageId": body["imageId"], "viewed": True}
    elif path == "/api/music/collection-status":
        ids = query["imageId"]
        data = {"canAdd": True, "collections": {
            "music-focus": {value: int(value) in FOCUS_MEMBER for value in ids},
            "h3-museum": {value: False for value in ids}}}
    elif path == "/api/music/focus":
        body = json.loads(request.post_data)
        requests["focus"].append(body["imageId"])
        FOCUS_MEMBER.add(body["imageId"])
        data = {"imageId": body["imageId"], "inCollection": True, "commented": True, "alreadyIn": False}
    elif path == "/api/reaction-status":
        data = {"images": {value: {"reactions": sorted(reacted.get(int(value), set()))}
                           for value in query["imageId"]}}
    elif path == "/api/reaction":
        body = json.loads(request.post_data)
        requests["reaction"].append(body)
        mine = reacted.setdefault(body["imageId"], set())
        (mine.add if body["active"] else mine.discard)(body["reaction"])
        data = {"imageId": body["imageId"], "reactions": sorted(mine), "changed": True}
    elif path == "/api/oauth/disconnect":
        data = {"disconnected": True}
    else:
        route.fallback()
        return
    route.fulfill(content_type="application/json", body=json.dumps(data))


def counter(page, text):
    page.wait_for_function("text => document.getElementById('msCounter').textContent === text", arg=text)


def playing(page):
    """The item ids held by the visible player and the spare one buffering the next."""
    return page.evaluate("""() => {
        const videos = [...document.querySelectorAll('#msScreen video')];
        const shown = videos.find(video => !video.hidden), spare = videos.find(video => video.hidden);
        return [shown?.getAttribute('src') || '', spare?.getAttribute('src') || ''];
    }""")


try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        install_fixture(page, base_url)
        page.route("**/*", music_routes)
        page.goto(base_url, wait_until="networkidle")
        page.locator(".creator-card").first.wait_for()

        # The tab sits between the Time Machine and Collectalytics, with its icon.
        order = page.eval_on_selector_all(".view-tabs > button", "buttons => buttons.map(button => button.id)")
        assert order.index("tabTimeMachine") + 1 == order.index("tabMusicScout") == order.index("tabCollectalytics") - 1, order
        assert page.locator("#tabMusicScout svg.ui-icon").count() == 1

        page.locator("#tabMusicScout").click()
        page.wait_for_function("() => document.getElementById('tabMusicScout').getAttribute('aria-pressed') === 'true'")
        assert page.locator("#musicScout").is_visible() and page.locator("#gallery").is_hidden()
        assert page.locator(".segment-toolbar").is_hidden(), "gallery-only controls leaked onto Music Scout"
        page.wait_for_function("count => document.querySelectorAll('#msQueues .ms-queue').length === count",
                               arg=len(CATALOG["queues"]))
        assert page.locator("#msPerCreator").input_value() == str(CATALOG["perCreatorDefault"])

        # Opening a queue starts fresh and lands on the first video not viewed yet.
        page.locator('#msQueues .ms-queue[data-queue="latest"]').click()
        page.locator("#msPlayer:not(.hidden)").wait_for()
        counter(page, "3 / 20+")
        assert requests["queue"][0]["refresh"] == ["1"], requests["queue"]
        assert page.locator("#msStrip .ms-thumb").count() == 20
        assert page.locator("#msStrip .ms-thumb.viewed").count() == 2
        assert page.locator("#musicScout").evaluate("node => node.classList.contains('ms-selected')")
        shown, spare = playing(page)
        assert shown == QUEUE[2]["videoUrl"] and spare == QUEUE[3]["videoUrl"], (shown, spare)
        assert page.locator("#msCreator").text_content() == QUEUE[2]["username"]

        # Over half a second on screen marks it viewed, once.
        page.wait_for_function("() => document.querySelectorAll('#msStrip .ms-thumb.viewed').length === 3")
        assert requests["viewed"] == [7003], requests["viewed"]

        # → swaps in the buffered video and starts buffering the one after.
        page.keyboard.press("ArrowRight")
        counter(page, "4 / 20+")
        shown, spare = playing(page)
        assert shown == QUEUE[3]["videoUrl"] and spare == QUEUE[4]["videoUrl"], (shown, spare)

        # Reactions: the account's own state, toggled with the count adjusted locally.
        heart = page.locator('#msReactions button[data-reaction="Heart"]')
        page.wait_for_function("() => !document.querySelector('#msReactions button[data-reaction=Heart]').disabled")
        assert heart.locator("b").text_content() == "2"
        heart.click()
        page.wait_for_function("() => document.querySelector('#msReactions button[data-reaction=Heart]').classList.contains('selected')")
        assert page.locator('#msReactions button[data-reaction="Heart"] b').text_content() == "3"
        assert requests["reaction"] == [{"imageId": 7004, "reaction": "Heart", "active": True}], requests["reaction"]

        # Collection markers: in Music Focus leads to Civitai for removal, read-only museum.
        focus = page.locator('#msReactions .ms-collection[data-collection="music-focus"]')
        museum = page.locator('#msReactions .ms-collection[data-collection="h3-museum"]')
        page.wait_for_function("() => document.querySelector('#msReactions .ms-collection.selected')")
        assert museum.get_attribute("aria-disabled") == "true" and museum.get_attribute("title") == f"Not in {CATALOG['collections'][1]['name']}"
        focus.click()
        assert page.locator("#msRemoveDialog").evaluate("dialog => dialog.open")
        assert page.locator("#msRemoveOpen").get_attribute("href") == QUEUE[3]["civitaiUrl"]
        page.locator("#msRemoveCancel").click()

        # Not in it: adding shows the exact comment, then posts it through the app.
        page.keyboard.press("ArrowRight")
        counter(page, "5 / 20+")
        page.wait_for_function("() => document.querySelector('#msReactions .ms-collection[data-collection=music-focus]')?.title.startsWith('Add to')")
        page.locator('#msReactions .ms-collection[data-collection="music-focus"]').click()
        assert page.locator("#msFocusDialog").evaluate("dialog => dialog.open")
        link = page.locator("#msFocusComment a")
        assert link.get_attribute("href") == "https://civitai.red/collections/17980935?sort=Recently+Added"
        # Arrow keys belong to the dialog while it is open.
        page.keyboard.press("ArrowRight")
        assert page.locator("#msCounter").text_content() == "5 / 20+"
        page.locator("#msFocusConfirm").click()
        page.wait_for_function("() => !document.getElementById('msFocusDialog').open")
        page.wait_for_function("() => document.querySelector('#msReactions .ms-collection[data-collection=music-focus]').classList.contains('selected')")
        assert requests["focus"] == [7005], requests["focus"]

        page.screenshot(path=str(test_safety.REPORTS / "music-scout-desktop.png"))

        # Leaving the tab drops both downloads; returning resumes the same video.
        page.locator("#tabGallery").click()
        page.wait_for_function("() => [...document.querySelectorAll('#msScreen video')].every(video => !video.getAttribute('src'))")
        assert page.locator("#musicScout").is_hidden()
        page.locator("#tabMusicScout").click()
        page.wait_for_function("src => [...document.querySelectorAll('#msScreen video')].some(video => !video.hidden && video.getAttribute('src') === src)",
                               arg=QUEUE[4]["videoUrl"])

        # A reload reopens the tab and the queue at the same video, from the server's copy.
        before = len(requests["queue"])
        page.reload(wait_until="networkidle")
        page.wait_for_function("() => document.getElementById('tabMusicScout').getAttribute('aria-pressed') === 'true'")
        counter(page, "5 / 20+")
        assert "refresh" not in requests["queue"][before], requests["queue"][before]

        page.keyboard.press("Home")
        counter(page, "1 / 20+")

        # An original the browser cannot play is swapped for its transcode, once.
        page.keyboard.press("ArrowRight")
        counter(page, "2 / 20+")
        page.wait_for_function("src => [...document.querySelectorAll('#msScreen video')].some(video => !video.hidden && video.getAttribute('src') === src)",
                               arg=QUEUE[1]["fallbackUrl"])
        page.keyboard.press("ArrowLeft")
        counter(page, "1 / 20+")

        # The narrow layout keeps the player and strip usable.
        page.set_viewport_size({"width": 420, "height": 860})
        page.wait_for_timeout(200)
        assert page.locator("#msScreen").bounding_box()["width"] > 250
        page.screenshot(path=str(test_safety.REPORTS / "music-scout-mobile.png"))

        # Signing out silences it rather than playing on behind the sign-in page.
        page.wait_for_function("() => [...document.querySelectorAll('#msScreen video')].some(video => video.getAttribute('src'))")
        page.locator("#disconnect").click()
        page.locator("#welcome:not(.hidden)").wait_for()
        assert page.locator("#musicScout").is_hidden()
        page.wait_for_function("() => [...document.querySelectorAll('#msScreen video')].every(video => !video.getAttribute('src'))")

        assert not errors, errors
        browser.close()
finally:
    server.shutdown()

print("PASS: Music Scout tab, queue open past viewed videos, buffering, reactions, collection markers, Music Focus add, tab pause, reload restore, transcode fallback and sign-out silence")
