"""Native Chromium media with throttled, interrupted, and recoverable local MP4 streams.

The tiny generated test pattern contains no user artwork. No app databases, credentials,
or external requests are involved. Lifecycle timings are accelerated only in this fixture.
"""
import test_safety  # Refuse live application endpoints and isolate all test data.

from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import base64
import json
import re
import threading
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
MEDIA = (ROOT / "tests/fixtures/recovery.mp4").read_bytes()
PREFIX = len(MEDIA) // 2
CHUNK = len(MEDIA) // 25
PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
requests = Counter()
disconnected = []
active_transfers = set()
lock = threading.Lock()
stop = threading.Event()

HTML = """<!doctype html><html><head><link rel="stylesheet" href="/styles/cards.css"><style>
body{margin:0} #host{position:relative;width:320px;height:240px}video{width:320px;height:240px}
.image-stage{position:relative;height:240px}.card-video{position:absolute;inset:0}
#host.grid video{width:100%;height:100%}
dialog{width:340px;height:300px} [hidden]{display:none!important}
</style></head><body><div id="host"></div><dialog id="details"><img id="detailImage"></dialog>
<script type="module">
import {createMediaLifecycle, createVideoRetentionPool, VIDEO_POLICY, attachCardVideo, showDetailVideo, clearDetailVideo, refreshVideoEligibility, suspendCardVideos} from '/ui/video.js';
import {showVideoPoster, showArtwork} from '/ui/artwork.js';
window.states=[];window.mounts=0;window.errors=[];window.waiting=[];window.mountedVideos=[];
const policy={...VIDEO_POLICY,tickMs:100,indicatorMs:200,idleMs:1600,progressLimitMs:4000,backoffMs:[300,500],recoverySpacingMs:0};
window.startFixture=(mode)=>{
 window.player?.release();document.getElementById('host').replaceChildren();window.states=[];window.mounts=0;window.waiting=[];window.mountedVideos=[];
 window.player=createMediaLifecycle({policy,card:true,reserve:at=>at,budget:new Map(),
  createVideo:()=>{const v=document.createElement('video');v.id='native';v.addEventListener('waiting',()=>waiting.push(v.currentTime));return v},
  mount:v=>{window.mounts++;window.mountedVideos.push(v);document.getElementById('host').append(v)},eligible:()=>!document.hidden,
  render:info=>{window.states.push(info.state);window.last=info},diagnose:(event,info)=>window.errors.push({event,...info})});
 window.player.setItem({id:mode,source:location.origin+'/media/'+mode+'.mp4'});window.player.start();
};
window.stopFixture=()=>window.player?.release();
window.makeCard=(mode='card',options={})=>{
 window.stopFixture();window.card?.releaseVideo();document.getElementById('host').replaceChildren();
 const el=document.createElement('article');el.className='creator-card';el.dataset.imagesActive='1';el.setAttribute('aria-busy','false');
 el.innerHTML='<div class="image-stage"><button class="image-button"><img></button></div>';
 document.getElementById('host').append(el);window.card=el;const img=el.querySelector('img');
 const show=attachCardVideo(el,img,options);window.paintVideo=show;
 show({id:mode,type:'video',videoUrl:location.origin+'/media/'+mode+'.mp4'});
 showVideoPoster(img,{id:'card',url:'original',thumbnailCandidates:[location.origin+'/missing.png',location.origin+'/poster.png']});
 el.setVideoVisible(true);
};
window.showPoster=showVideoPoster;window.showImage=showArtwork;window.showDetail=showDetailVideo;window.clearDetail=clearDetailVideo;window.refreshVideos=refreshVideoEligibility;
window.makeRetention=createVideoRetentionPool;window.suspendCards=suspendCardVideos;
window.attachVideo=attachCardVideo;
window.ready=true;
</script></body></html>"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/poster.png"):
            body = HTML.encode() if path == "/" else PIXEL
            self.send_response(200)
            self.send_header("Content-Type", "text/html" if path == "/" else "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not path.startswith("/media/"):
            return super().do_GET()
        match = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(match[1]) if match else 0
        end = min(int(match[2]), len(MEDIA) - 1) if match and match[2] else len(MEDIA) - 1
        with lock:
            requests[path] += 1
            attempt = requests[path]
        self.send_response(206 if match else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(end - start + 1))
        if match:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(MEDIA)}")
        self.end_headers()
        try:
            data = MEDIA[start:end + 1]
            if "pressure-" in path:
                with lock:
                    active_transfers.add(path)
                # Enough initial data to start all players, followed by a long
                # transfer that would monopolize HTTP/1 connections if parked.
                self.wfile.write(data[:131072]); self.wfile.flush()
                for i in range(131072, len(data), 4096):
                    if stop.wait(.1):
                        break
                    self.wfile.write(data[i:i + 4096]); self.wfile.flush()
            elif "stall" in path and attempt == 1:
                self.wfile.write(data[:PREFIX]); self.wfile.flush()
                # Deliberately hang the first connection. A fresh source load succeeds.
                while not stop.wait(.05):
                    with lock:
                        if requests[path] > 1:
                            break
                self.wfile.write(data[PREFIX:]); self.wfile.flush()
            elif "slow" in path:
                for i in range(0, len(data), CHUNK):
                    self.wfile.write(data[i:i + CHUNK]); self.wfile.flush()
                    if stop.wait(.25):
                        break
            elif "brief" in path:
                self.wfile.write(data[:PREFIX]); self.wfile.flush()
                stop.wait(2.2)
                self.wfile.write(data[PREFIX:])
            else:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            disconnected.append(path)
        finally:
            with lock:
                active_transfers.discard(path)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{server.server_port}"
tested = []
try:
    with sync_playwright() as playwright:
        for name, engine, options in [("Chromium", playwright.chromium, {}),
                                      ("Edge", playwright.chromium, {"channel": "msedge"})]:
            try:
                browser = engine.launch(headless=True, **options)
            except Exception:
                if name == "Chromium":
                    raise
                continue
            page = browser.new_page(viewport={"width": 1000, "height": 700})
            page.goto(base)
            page.wait_for_function("window.ready")
            page.evaluate("startFixture('normal-" + name + "')")
            page.wait_for_function("document.querySelector('#native')?.currentTime>1")
            assert page.evaluate("mounts") == 1
            page.evaluate("player.pause()")
            page.wait_for_timeout(1800)
            assert page.evaluate("player.state") == "user-paused"
            assert page.locator("#native").count() == 0

            # A genuinely slow stream with frequent native waiting/buffering remains one load.
            page.evaluate("startFixture('slow-" + name + "')")
            try:
                page.wait_for_function("document.querySelector('#native')?.readyState>=2", timeout=12000)
            except Exception:
                print(page.evaluate("({states,errors,mounts})"), flush=True)
                raise
            page.evaluate("document.querySelector('#native').playbackRate=8")
            page.wait_for_timeout(7200)
            assert page.evaluate("mounts") == 1, page.evaluate("states")
            assert page.evaluate("document.querySelector('#native').buffered.length>0")

            page.evaluate("startFixture('brief-" + name + "')")
            page.wait_for_function("document.querySelector('#native')?.currentTime>0.25", timeout=10000)
            page.evaluate("document.querySelector('#native').playbackRate=8")
            try:
                page.wait_for_function("document.querySelector('#native')?.currentTime>12", timeout=15000)
            except Exception:
                print(page.evaluate("({states,errors,mounts,last,time:player.video?.currentTime,ready:player.video?.readyState})"), dict(requests), flush=True)
                raise
            assert page.evaluate("mounts") == 1
            assert page.evaluate("waiting.some(time=>time>1)"), page.evaluate("waiting")

            # Five seconds of real playback precede the persistent interruption.
            page.evaluate("startFixture('stall-" + name + "')")
            page.wait_for_function("document.querySelector('#native')?.currentTime>4.5", timeout=15000)
            page.wait_for_function("mounts===2", timeout=15000)
            page.wait_for_function("document.querySelector('#native')?.currentTime>5", timeout=10000)
            assert page.evaluate("states.includes('recovering')")
            assert page.evaluate("mounts") == 2
            assert page.evaluate("mountedVideos[0].paused && !mountedVideos[0].isConnected && !mountedVideos[0].getAttribute('src')")
            page.evaluate("stopFixture()")

            # Old image handlers cannot repaint the next artwork or start playback.
            media_before = sum(requests.values())
            page.evaluate("""() => {
                const stage=document.createElement('div');stage.className='image-stage';
                const img=document.createElement('img');stage.append(img);document.querySelector('#host').append(stage);
                window.poster=img;window.oldCallbacks=[];
                const listen=img.addEventListener.bind(img);
                img.addEventListener=(name,fn,options)=>{oldCallbacks.push(fn);listen(name,fn,options)};
                showPoster(img,{id:'old',url:'old',thumbnailCandidates:[location.origin+'/obsolete.png']});
                const stale=[...oldCallbacks];img.addEventListener=listen;
                showPoster(img,{id:'new',url:'new',thumbnailCandidates:[location.origin+'/poster.png']});
                window.stalePosterCallbacks=stale;
                stale.forEach(fn=>fn());
            }""")
            page.wait_for_function("poster.naturalWidth>0")
            page.evaluate("stalePosterCallbacks.forEach(fn=>fn())")
            assert page.evaluate("poster.src.endsWith('/poster.png') && !poster.classList.contains('image-error')")
            page.evaluate("showPoster(poster,{id:'failed',url:'failed',thumbnailCandidates:[location.origin+'/bad1.png',location.origin+'/bad2.png']})")
            page.wait_for_function("poster.closest('.image-stage').classList.contains('video-preview-unavailable')")
            assert not page.evaluate("poster.getAttribute('src')")
            page.evaluate("showImage(poster,location.origin+'/bad3.png',location.origin+'/poster.png')")
            page.wait_for_function("poster.naturalWidth>0 && poster.src.endsWith('/poster.png')")
            assert sum(requests.values()) == media_before
            page.evaluate("poster.cancelArtwork?.();poster.parentElement.remove()")

            # Actual card adapter: poster fallback itself cannot request video in reduced motion.
            page.emulate_media(reduced_motion="reduce")
            page.evaluate("makeCard()")
            page.wait_for_function("document.querySelector('.image-button img').naturalWidth>0")
            page.wait_for_timeout(500)
            assert page.locator(".card-video").count() == 0
            page.locator(".video-toggle").click()
            page.wait_for_function("document.querySelector('.card-video').currentTime>0.25")
            page.locator(".video-toggle").click()
            page.evaluate("refreshVideos()")
            page.wait_for_timeout(500)
            assert page.locator(".card-video").count() == 0
            page.evaluate("card.releaseVideo();card.remove()")

            # A decoded, fully buffered no-store stream survives actual scroll-back.
            # This proves player/buffer reuse without relying on the HTTP cache.
            page.emulate_media(reduced_motion="no-preference")
            path = f'/media/retained-{name}.mp4'
            page.evaluate("""mode => {
                const spacer=document.createElement('div');spacer.id='scroll-space';spacer.style.height='1600px';
                document.body.append(spacer);window.scrollTo(0,0);makeCard(mode);
            }""", f'retained-{name}')
            page.wait_for_function("""() => {
                const v=document.querySelector('.card-video');
                return v?.currentTime>1.2 && v.buffered.length && v.buffered.end(v.buffered.length-1)>=v.duration-0.1;
            }""")
            media_before = requests[path]
            page.evaluate("""() => {
                window.retainedVideo=document.querySelector('.card-video');
                card.querySelector('.image-stage').classList.add('video-preview-unavailable');
                window.scrollTo(0,900);
            }""")
            page.wait_for_function("card.dataset.videoState==='parked'")
            parked_time = page.evaluate('retainedVideo.currentTime')
            page.wait_for_timeout(400)
            assert page.evaluate('retainedVideo.paused && !retainedVideo.hidden && retainedVideo.isConnected')
            assert abs(page.evaluate('retainedVideo.currentTime') - parked_time) < .1
            assert page.evaluate("getComputedStyle(card.querySelector('.image-stage'),'::after').display==='none'")
            page.evaluate('window.scrollTo(0,0)')
            page.wait_for_function('retainedVideo.currentTime>' + str(parked_time + .25))
            assert page.evaluate("document.querySelector('.card-video')===retainedVideo")
            assert requests[path] == media_before, dict(requests)

            # Opening details parks the cards; closing it reuses the same card player.
            page.evaluate("""() => {
                suspendCards(true);document.querySelector('#details').showModal();
                showDetail(document.querySelector('#detailImage'),location.origin+'/media/retention-details.mp4','retention-details');
            }""")
            page.wait_for_function("card.dataset.videoState==='parked' && document.querySelector('#detailVideo').currentTime>.25")
            assert page.evaluate('retainedVideo.paused && retainedVideo.isConnected')
            parked_time = page.evaluate('retainedVideo.currentTime')
            page.evaluate("clearDetail();document.querySelector('#details').close();suspendCards(false)")
            page.wait_for_function('retainedVideo.currentTime>' + str(parked_time + .25))
            assert page.evaluate("document.querySelector('.card-video')===retainedVideo")
            assert requests[path] == media_before, dict(requests)

            # Filter invalidation, hidden views, and a hidden document immediately purge buffers.
            for invalidation in ('controls', 'view', 'document'):
                if invalidation != 'controls':
                    page.evaluate('mode => makeCard(mode)', f'invalidation-{invalidation}-{name}')
                    page.wait_for_function("document.querySelector('.card-video')?.currentTime>1.2")
                page.evaluate('window.oldCardVideo=document.querySelector(".card-video");window.scrollTo(0,900)')
                page.wait_for_function("card.dataset.videoState==='parked'")
                if invalidation == 'controls':
                    page.evaluate("window.dispatchEvent(new Event('content-controls-changing'))")
                elif invalidation == 'view':
                    page.evaluate("card.classList.add('hidden');refreshVideos()")
                else:
                    page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
                assert page.evaluate('!oldCardVideo.isConnected && !oldCardVideo.getAttribute("src")')
                page.evaluate("""() => {
                    card.releaseVideo();card.remove();delete document.hidden;
                    window.dispatchEvent(new Event('community-controls-changed'));window.scrollTo(0,0);
                }""")

            # Expiry permits a fresh request and restores the position on one new player.
            path = f'/media/expiry-{name}.mp4'
            page.evaluate("mode => makeCard(mode,{retention:makeRetention({ttlMs:800})})", f'expiry-{name}')
            page.wait_for_function("document.querySelector('.card-video')?.currentTime>1.2")
            media_before = requests[path]
            page.evaluate('window.expiredVideo=document.querySelector(".card-video");window.scrollTo(0,900)')
            page.wait_for_function("card.dataset.videoState==='parked'")
            page.wait_for_function('!expiredVideo.isConnected')
            page.evaluate('window.scrollTo(0,0)')
            page.wait_for_function("document.querySelector('.card-video')?.currentTime>1.5")
            assert page.evaluate("document.querySelector('.card-video')!==expiredVideo")
            assert requests[path] > media_before, dict(requests)
            page.evaluate('card.releaseVideo();card.remove()')

            # A flick that never satisfies the settle delay creates no media request.
            path = f'/media/flick-{name}.mp4'
            page.evaluate("mode => {makeCard(mode);window.scrollTo(0,900)}", f'flick-{name}')
            page.wait_for_timeout(600)
            assert requests[path] == 0, dict(requests)
            assert page.locator('.card-video').count() == 0
            page.evaluate("card.releaseVideo();card.remove();document.querySelector('#scroll-space').remove();window.scrollTo(0,0)")

            # Played but incomplete streams must free their connections on scroll
            # and details suspension. Six retained downloads previously kept the
            # next player's HTTP/1 request queued, even with preload='none'.
            recovery_page = page
            page = browser.new_page(viewport={"width": 1000, "height": 700})
            page.goto(base)
            page.wait_for_function('window.ready')
            for leave in ('scroll', 'details'):
                prefix = f'pressure-{leave}-{name}'
                page.evaluate("""prefix => {
                    window.scrollTo(0,0);const host=document.querySelector('#host');
                    host.classList.add('grid');host.style.cssText='width:950px;height:440px;display:grid;grid-template-columns:repeat(3,1fr)';
                    window.pressureCards=Array.from({length:6},(_,i)=>{
                        const el=document.createElement('article');el.className='creator-card';
                        el.dataset.imagesActive='1';el.setAttribute('aria-busy','false');
                        el.innerHTML='<div class="image-stage" style="height:180px"><button class="image-button"><img></button></div>';
                        host.append(el);const show=attachVideo(el,el.querySelector('img'));
                        show({id:prefix+i,type:'video',videoUrl:location.origin+'/media/'+prefix+'-'+i+'.mp4'});
                        el.setVideoVisible(true);return el;
                    });
                    const spacer=document.createElement('div');spacer.id='pressure-space';spacer.style.height='1600px';document.body.append(spacer);
                }""", prefix)
                page.wait_for_function("pressureCards.every(el=>el.querySelector('video')?.currentTime>1.2)")
                assert page.evaluate("pressureCards.every(el=>{const v=el.querySelector('video');return v.networkState===2 && v.buffered.end(0)<v.duration-1})")
                page.evaluate("window.partialPlayers=pressureCards.map(el=>el.querySelector('video'))")
                if leave == 'scroll':
                    page.evaluate('window.scrollTo(0,900)')
                else:
                    page.evaluate("suspendCards(true);document.querySelector('#details').showModal()")
                page.wait_for_function("partialPlayers.every(v=>!v.isConnected && !v.getAttribute('src'))")
                # Starting native media here avoids resuming the gallery while we
                # measure whether the six old transfers relinquished their slots.
                foreground = f'foreground-{leave}-{name}'
                page.evaluate("""mode=>{
                    const dialog=document.querySelector('#details');if(!dialog.open)dialog.showModal();
                    showDetail(document.querySelector('#detailImage'),location.origin+'/media/'+mode+'.mp4',mode);
                }""", foreground)
                page.wait_for_function("document.querySelector('#detailVideo')?.currentTime>.25", timeout=5000)
                pressure_paths = [f'/media/{prefix}-{i}.mp4' for i in range(6)]
                assert all(path in disconnected for path in pressure_paths), disconnected
                with lock:
                    assert not any(path in active_transfers for path in pressure_paths), active_transfers
                assert requests[f'/media/{foreground}.mp4'] == 1
                page.evaluate("""() => {
                    pressureCards.forEach(el=>{el.releaseVideo();el.remove()});clearDetail();
                    const dialog=document.querySelector('#details');if(dialog.open)dialog.close();
                    suspendCards(false);document.querySelector('#pressure-space').remove();window.scrollTo(0,0);
                }""")
            page.close()
            page = recovery_page
            page.bring_to_front()

            # Two rows of five videos must all survive a details visit. Media is
            # no-store, so count-based eviction would show up as fresh requests.
            # Give this layout its own page, independent of the stalled/error
            # players and media-cache pressure exercised by the recovery cases.
            recovery_page = page
            page = browser.new_page(viewport={"width": 1000, "height": 700})
            page.goto(base)
            page.wait_for_function('window.ready')
            page.evaluate("""name => {
                const host=document.querySelector('#host');host.replaceChildren();host.classList.add('grid');
                host.style.cssText='width:950px;height:440px;display:grid;grid-template-columns:repeat(5,1fr)';
                window.gridCards=Array.from({length:10},(_,i)=>{
                    const el=document.createElement('article');el.className='creator-card';
                    el.dataset.imagesActive='1';el.setAttribute('aria-busy','false');
                    el.innerHTML='<div class="image-stage" style="height:180px"><button class="image-button"><img></button></div>';
                    host.append(el);const show=attachVideo(el,el.querySelector('img'));
                    show({id:'grid-'+i,type:'video',videoUrl:location.origin+'/media/grid-'+name+'-'+i+'.mp4'});
                    el.setVideoVisible(true);return el;
                });
            }""", name)
            page.wait_for_function("""gridCards.every(el=>{
                const v=el.querySelector('video');return v?.currentTime>1.2 && v.networkState===1 &&
                    v.buffered.length===1 && v.buffered.start(0)===0 && v.buffered.end(0)>=v.duration-.1;
            })""", timeout=20000)
            assert page.evaluate("new Set(gridCards.map(el=>el.offsetLeft)).size===5 && new Set(gridCards.map(el=>el.offsetTop)).size===2")
            grid_paths = [f'/media/grid-{name}-{i}.mp4' for i in range(10)]
            grid_requests_before = {path: requests[path] for path in grid_paths}
            page.evaluate("""() => {
                window.gridPlayers=gridCards.map(el=>el.querySelector('video'));
                suspendCards(true);document.querySelector('#details').showModal();
                showDetail(document.querySelector('#detailImage'),location.origin+'/media/grid-details.mp4','grid-details');
            }""")
            page.wait_for_function("gridCards.every(el=>el.dataset.videoState==='parked') && document.querySelector('#detailVideo').currentTime>.25")
            assert page.evaluate("gridPlayers.every(v=>v.paused && v.isConnected && v.getAttribute('src'))")
            page.evaluate("window.gridTimes=gridPlayers.map(v=>v.currentTime);clearDetail();document.querySelector('#details').close();suspendCards(false)")
            page.wait_for_function("gridPlayers.every((v,i)=>v.currentTime>gridTimes[i]+.25)")
            assert page.evaluate("gridCards.every((el,i)=>el.querySelector('video')===gridPlayers[i])")
            assert all(requests[path] == count for path, count in grid_requests_before.items()), {
                'before': grid_requests_before, 'after': {path: requests[path] for path in grid_paths},
                'players': page.evaluate("gridPlayers.map(v=>({time:v.currentTime,ready:v.readyState,network:v.networkState,buffer:Array.from({length:v.buffered.length},(_,i)=>[v.buffered.start(i),v.buffered.end(i)])}))")}
            page.evaluate("""() => {
                gridCards.forEach(el=>{el.releaseVideo();el.remove()});
                const host=document.querySelector('#host');host.classList.remove('grid');host.removeAttribute('style');
            }""")
            page.close()
            page = recovery_page
            page.bring_to_front()

            # Closing a real native dialog retires its media, including deferred restoration.
            page.evaluate("""() => {
                const d=document.querySelector('#details');d.showModal();
                showDetail(document.querySelector('#detailImage'),location.origin+'/media/details.mp4','details');
            }""")
            page.wait_for_function("document.querySelector('#detailVideo').currentTime>0.25")
            page.evaluate("window.pausedDetail=document.querySelector('#detailVideo');pausedDetail.pause();window.pausedTime=pausedDetail.currentTime")
            page.wait_for_timeout(1800)
            assert page.evaluate("pausedDetail===document.querySelector('#detailVideo') && pausedDetail.paused && Math.abs(pausedDetail.currentTime-pausedTime)<0.1")
            page.evaluate("pausedDetail.play()")
            page.wait_for_function("pausedDetail.currentTime>pausedTime+0.25")
            page.evaluate("clearDetail();document.querySelector('#details').close()")
            assert page.locator("#detailVideo").count() == 0

            # Simulate only the policy denial; the explicit Play action uses native playback.
            page.evaluate("""() => {
                const nativePlay=HTMLMediaElement.prototype.play;let deny=true;
                HTMLMediaElement.prototype.play=function(){
                    if(deny){deny=false;return Promise.reject(new DOMException('Gesture required','NotAllowedError'))}
                    return nativePlay.call(this);
                };
                document.querySelector('#details').showModal();
                showDetail(document.querySelector('#detailImage'),location.origin+'/media/gesture.mp4','gesture');
            }""")
            page.wait_for_function("document.querySelector('#details').dataset.videoState==='awaiting-gesture'")
            assert page.locator("#detailVideo").count() == 0
            page.get_by_role("button", name="Play video", exact=True).click()
            page.wait_for_function("document.querySelector('#detailVideo')?.currentTime>0.25")
            page.evaluate("clearDetail();document.querySelector('#details').close()")
            browser.close()
            tested.append(name)
finally:
    stop.set()
    server.shutdown(); server.server_close()

print(json.dumps({"browsers": tested, "nativePlayback": True, "slowWithoutReload": True,
                  "naturalRecovery": True, "midPlaybackRecovery": True,
                  "scrollAndDetailReuseWithoutNewRequests": True, "retentionExpiry": True,
                  "retentionInvalidation": True, "fastFlickWithoutRequests": True,
                  "wideGridPlayers": 10, "wideGridDetailsWithoutNewRequests": True,
                  "incompleteStreamsReleaseConnectionsOnScrollAndDetails": True,
                  "posterOnlyNeverLoadsVideo": True, "nativeDetailsCleaned": True}))
