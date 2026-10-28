"""Real Community markup: artwork-only autoplay, live tuning and details bursts.

Synthetic local artwork/API transport; native Chromium and Firefox video playback.
No account, hosted API or live profile writes. Threshold saves use fixture storage.
"""
import test_safety
from functools import partial
from http.server import ThreadingHTTPServer
import json
import re
import threading
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright
from v2_visual_baseline import ROOT, StaticHandler, install_fixture

media = (ROOT / 'tests/fixtures/recovery.mp4').read_bytes()
class MediaHandler(StaticHandler):
    def do_GET(self):
        if not urlsplit(self.path).path.startswith('/visibility-'):
            return super().do_GET()
        match = re.match(r'bytes=(\d+)-(\d*)', self.headers.get('Range', ''))
        start = int(match[1]) if match else 0
        end = min(int(match[2]), len(media)-1) if match and match[2] else len(media)-1
        self.send_response(206 if match else 200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(end-start+1))
        if match:
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(media)}')
        self.end_headers()
        try:
            self.wfile.write(media[start:end+1])
        except (BrokenPipeError, ConnectionResetError):
            pass
server = ThreadingHTTPServer(('127.0.0.1', 0), partial(MediaHandler, directory=str(ROOT / 'static')))
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}'
results = []
try:
    with sync_playwright() as pw:
        for name in ('chromium', 'firefox'):
            browser = getattr(pw, name).launch(headless=True)
            context = browser.new_context(viewport={'width': 2000, 'height': 1100})
            page = context.new_page()
            page.emulate_media(reduced_motion='reduce')
            install_fixture(page, base, cached_tags=True)
            settings = {'browsingLevels': [1, 2], 'contentRating': 'Soft', 'checkForUpdates': False,
                        'videoVisibilityPercent': 40, 'hideHighVolumeCreators': False}
            state = {'failSave': False}
            failures, requests, saves = [], [], []
            page.on('pageerror', lambda e: failures.append(str(e)))
            page.on('request', lambda r: requests.append(r.url))
            arts = [{'id': 7000+i, 'creatorId': 9000+i, 'username': f'VisibilityArtist{i}',
                     'type': 'video', 'browsingLevel': 1, 'url': f'{base}/community-fixture.svg',
                     'thumbnailUrl': f'{base}/community-fixture.svg', 'videoUrl': f'{base}/visibility-{i}.mp4',
                     'detailVideoUrl': f'{base}/visibility-detail-{i}.mp4',
                     'imageUrl': f'https://civitai.red/images/{7000+i}',
                     'stats': {'likeCount': 1700+i, 'heartCount': 600, 'laughCount': 20,
                               'cryCount': 5, 'reactionCount': 2325+i},
                     'tagState': {'known': True, 'tags': []}} for i in range(15)]
            def reply(route, value, status=200):
                route.fulfill(status=status, content_type='application/json', body=json.dumps(value))
            def save_settings(route):
                if route.request.method == 'POST':
                    patch = json.loads(route.request.post_data)
                    saves.append(patch)
                    if state['failSave']:
                        reply(route, {'error': 'Synthetic save failure'}, 500)
                        return
                    settings.update(patch)
                reply(route, settings)
            page.route('**/api/settings', save_settings)
            page.route('**/api/community/beta', lambda r: reply(r, {'production': True, 'serverPolicy': True}))
            page.route('**/api/community/config', lambda r: reply(r, {'development': False}))
            page.route('**/api/community/status*', lambda r: reply(r, {'member': {'id': 7}, 'quota': {'remaining': 3}, 'beta': {'available': True}}))
            page.route('**/api/community/gallery*', lambda r: reply(r, {'items': [
                {'artwork': art, 'recommender': 'SyntheticMemberWithALongName', 'submittedAt': '2026-10-24T12:00:00Z'}
                for art in arts], 'nextCursor': None}))
            page.route('**/api/community/detail*', lambda r: reply(r, {**arts[int(parse_qs(urlsplit(r.request.url).query)['id'][0])-7000],
                'known': True, 'tags': [], 'civitaiUrl': 'https://civitai.red/images/7005', 'prompt': ''}))
            page.goto(base, wait_until='domcontentloaded')
            page.locator('#tabCommunity').click()
            page.wait_for_function("document.querySelectorAll('#communityGrid .creator-card').length===15")
            page.add_style_tag(content='''#communityGrid{grid-template-columns:repeat(5,minmax(0,1fr))!important;max-width:1500px}
                #communityGrid .image-stage{height:480px!important}
                #communityGrid .creator-footer{min-height:60px}
                #communityGrid .community-credit{min-height:70px;max-width:200px}
                #communityGrid .creator-card:nth-child(3n) .creator-footer{min-height:95px}''')
            # Native app cards, including separate reactions/footer/credit. Align the
            # upper artwork just behind the sticky bar; its metadata remains visible.
            page.evaluate('''() => {
                window.cards=[...document.querySelectorAll('#communityGrid .creator-card')];
                const top=document.querySelector('.view-tabs').getBoundingClientRect().bottom;
                window.scrollBy(0,cards[0].querySelector('.image-stage').getBoundingClientRect().bottom-top+1);
            }''')
            height = page.evaluate("Math.ceil(cards[10].querySelector('.image-stage').getBoundingClientRect().top+480*.45)")
            page.set_viewport_size({'width': 2000, 'height': height})
            page.wait_for_timeout(400)
            page.emulate_media(reduced_motion='no-preference')
            page.wait_for_function("cards.slice(5).every(c=>c.querySelector('video')?.currentTime>.25)")
            assert page.evaluate("cards.slice(0,5).every(c=>!c.querySelector('video'))")
            assert page.evaluate("cards.slice(0,5).some(c=>c.getBoundingClientRect().bottom>document.querySelector('.view-tabs').getBoundingClientRect().bottom)")
            assert not any('/visibility-'+str(i)+'.mp4' in url for i in range(5) for url in requests)
            page.evaluate("window.controlRefreshes=0;window.addEventListener('community-controls-changed',()=>controlRefreshes++)")
            def gallery_reads():
                return sum('/api/community/gallery' in url or '/api/history/artists' in url for url in requests)
            reads = gallery_reads()
            page.locator('#galleryPreferences').click()
            slider = page.locator('#prefVideoVisibility')
            assert slider.input_value() == '40'
            def tune(percent):
                slider.evaluate('''(el,p)=>{el.value=String(p);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))}''', percent)
                page.wait_for_function("!document.querySelector('#prefVideoVisibility').disabled")
            # Dragging updates the displayed value; player changes and persistence
            # happen once on release, avoiding repeated restarts along the drag.
            slider.evaluate("el=>{el.value='80';el.dispatchEvent(new Event('input',{bubbles:true}))}")
            assert page.locator('#prefVideoVisibilityValue').text_content() == '80%'
            assert not saves
            assert page.locator('#communityGrid video').count() == 10
            middle_players = page.evaluate("window.middlePlayers=cards.slice(5,10).map(c=>c.querySelector('video'));true")
            tune(60)
            page.wait_for_function("cards.slice(10).every(c=>!c.querySelector('video'))")
            assert page.evaluate("cards.slice(5,10).every((c,i)=>c.querySelector('video')===middlePlayers[i])")
            assert settings['videoVisibilityPercent'] == 60
            tune(1)
            page.wait_for_function("cards.slice(5).every(c=>c.querySelector('video')?.currentTime>.25)")
            assert page.evaluate("cards.slice(0,5).every(c=>!c.querySelector('video'))")
            tune(40)
            try:
                page.wait_for_function("cards.slice(5).every(c=>c.querySelector('video')?.currentTime>.25)")
            except Exception:
                print(json.dumps({'browser':name,'failures':failures,'saves':saves,'state':page.evaluate("""cards.map(c=>{
                    const s=c.querySelector('.image-stage').getBoundingClientRect(),v=c.querySelector('video');
                    return {state:c.dataset.videoState,top:s.top,bottom:s.bottom,viewport:innerHeight,
                        nav:document.querySelector('.view-tabs').getBoundingClientRect().bottom,
                        feedback:c.querySelector('.video-feedback')?.textContent,
                        video:v?{time:v.currentTime,ready:v.readyState,network:v.networkState,paused:v.paused,seeking:v.seeking,
                        buffer:Array.from({length:v.buffered.length},(_,i)=>[v.buffered.start(i),v.buffered.end(i)])}:null};})""")}),flush=True)
                raise
            state['failSave'] = True
            tune(80)
            page.wait_for_function("document.querySelector('#prefVideoVisibility').value==='40'")
            page.wait_for_function("cards.slice(5).every(c=>c.querySelector('video')?.currentTime>.25)")
            state['failSave'] = False
            assert gallery_reads() == reads
            assert page.evaluate('controlRefreshes') == 0
            assert all(list(patch) == ['videoVisibilityPercent'] for patch in saves)
            page.locator('#closePreferences').click()
            # Details close must restart exactly the artwork-visible rows, never
            # the upper row whose reactions and recommendation note remain visible.
            page.locator('#communityGrid .creator-card').nth(5).locator('.info-button').click()
            page.wait_for_function("document.querySelector('#detailVideo')?.currentTime>.25")
            assert page.locator('#communityGrid video').count() == 0
            page.locator('#close').click()
            page.wait_for_function("cards.slice(5).every(c=>c.querySelector('video')?.currentTime>.25)")
            assert page.locator('#communityGrid video').count() == 10
            assert page.evaluate("cards.slice(0,5).every(c=>!c.querySelector('video'))")
            # Another tab can tune playback without triggering content-control
            # broadcasts or feed refreshes in this tab.
            peer = page.context.new_page()
            peer.route('**/api/settings', save_settings)
            peer.goto(base+'/ui/api.js')
            peer.evaluate("""async()=>{const {api}=await import('/ui/api.js');await api('/api/settings',{
                method:'POST',body:JSON.stringify({videoVisibilityPercent:72})});}""")
            page.wait_for_function("document.querySelector('#prefVideoVisibility').value==='72'")
            assert gallery_reads() == reads
            assert page.evaluate('controlRefreshes') == 0
            peer.evaluate("""()=>{const channel=new BroadcastChannel('discovery-content-controls-v1');
                channel.postMessage({phase:'video-visibility',settings:{videoVisibilityPercent:'invalid'}});channel.close();}""")
            page.wait_for_timeout(100)
            assert slider.input_value() == '72'
            peer.close()
            page.bring_to_front()
            # Saved setting is restored through the normal startup GET, without a rebuild.
            page.locator('#galleryPreferences').click()
            tune(63)
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function("document.querySelector('#prefVideoVisibility')?.value==='63'")
            assert not failures, failures
            page.locator('#galleryPreferences').click()
            page.screenshot(path=str(test_safety.REPORTS / f'video-visibility-{name}.png'))
            # Traverse fresh copies of real card markup faster than the settle
            # delay. None of their unique media sources may be requested.
            page.evaluate("""async name=>{
                const {attachCardVideo,setCardVideoVisibilityPercent}=await import('/ui/video.js');
                setCardVideoVisibilityPercent(40);
                const host=document.createElement('div');host.style.cssText='display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:14px';
                const originals=[...document.querySelectorAll('#communityGrid .creator-card')];
                originals.forEach(el=>el.releaseVideo());
                document.body.append(host);
                for(let i=0;i<originals.length;i++){
                    const el=originals[i].cloneNode(true);
                    el.querySelectorAll('video,.video-toggle,.video-feedback').forEach(v=>v.remove());
                    el.querySelector('.image-stage').style.height='480px';el.dataset.imagesActive='1';host.append(el);
                    attachCardVideo(el,el.querySelector('.image-button img'))({id:'flick-'+i,type:'video',videoUrl:location.origin+'/visibility-flick-'+name+'-'+i+'.mp4'});
                }
                const spacer=document.createElement('div');spacer.style.height='2000px';document.body.append(spacer);
                const start=host.getBoundingClientRect().top+scrollY;
                for(let i=0;i<7;i++){scrollTo(0,start+i*400);await new Promise(requestAnimationFrame)}
                scrollTo(0,host.getBoundingClientRect().bottom+scrollY+50);
            }""", name)
            page.wait_for_timeout(500)
            assert not any('/visibility-flick-' in url for url in requests)
            results.append({'browser': name, 'artworkOnly': True, 'footerOnlyPlayers': 0,
                            'visiblePlayersAt40': 10, 'liveTuningWithoutFeedReload': True,
                            'failedSaveRestored': True, 'detailsClosePlayers': 10,
                            'restoredAfterReload': 63, 'crossTabWithoutFeedReload': True, 'fastFlickRequests': 0})
            browser.close()
finally:
    server.shutdown(); server.server_close()
print(json.dumps(results))
