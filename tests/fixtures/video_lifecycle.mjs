import assert from 'node:assert/strict';
globalThis.matchMedia = () => ({matches:false, addEventListener(){}});
globalThis.document = new EventTarget();
globalThis.window = new EventTarget();
globalThis.IntersectionObserver = class { observe(){} unobserve(){} };
const {createMediaLifecycle, createVideoRetentionPool, VIDEO_POLICY} = await import('../../static/ui/video.js');

class Clock {
  time=0; next=0; tasks=new Map();
  now=()=>this.time;
  setTimeout=(fn,ms)=>{const id=++this.next;this.tasks.set(id,{at:this.time+ms,fn});return id};
  clearTimeout=id=>this.tasks.delete(id);
  async advance(ms){
    const end=this.time+ms;
    for(;;){
      const found=[...this.tasks].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!found)break;
      this.tasks.delete(found[0]);this.time=found[1].at;found[1].fn();await Promise.resolve();await Promise.resolve();
    }
    this.time=end;await Promise.resolve();await Promise.resolve();
  }
}
class Video extends EventTarget {
  currentTime=0; playbackRate=1; volume=1; muted=true; paused=true; readyState=0; duration=60; seeking=false;
  buffer=0; src=''; removed=false; error=null; playError=null; pending=null;
  get buffered(){return {length:this.buffer>0?1:0,end:()=>this.buffer,start:()=>0}}
  get seekable(){return {length:this.readyState?1:0,end:()=>this.duration,start:()=>0}}
  play(){this.plays=(this.plays||0)+1;this.paused=false;this.dispatchEvent(new Event('play'));return this.pending || (this.playError?Promise.reject(this.playError):Promise.resolve())}
  pause(){this.paused=true;this.dispatchEvent(new Event('pause'))}
  removeAttribute(name){if(name==='src')this.src=''}
  load(){this.loads=(this.loads||0)+1}
  remove(){this.removed=true}
}
function fixture({card=true,muted,budget=new Map(),make=()=>new Video(),clock=new Clock(),reserve=at=>at,retention=null}={}){
  const videos=[],renders=[],events=[];
  let eligible=true,online=true;
  const controller=createMediaLifecycle({card,retention,...(muted===undefined ? {} : {muted}),clock,budget,policy:VIDEO_POLICY,...(reserve ? {reserve} : {}),
    createVideo:()=>{const v=make();videos.push(v);return v},mount(){},
    eligible:()=>eligible,online:()=>online,render:v=>renders.push(v),diagnose:(...args)=>events.push(args)});
  controller.setItem({id:'1',source:'https://image.civitai.com/fixture.mp4'});
  return {controller,clock,budget,videos,renders,events,setEligible:v=>eligible=v,setOnline:v=>online=v};
}

// Initial timeout: one card reload, then a manual action. Never overlapping old sources.
{
 const f=fixture();f.controller.start();await f.clock.advance(15000);
 assert.equal(f.controller.state,'recovering');assert.equal(f.videos[0].src,'');
 await f.clock.advance(3000);assert.equal(f.videos.length,2);assert(f.videos[0].removed);
 await f.clock.advance(15000);assert.equal(f.controller.state,'failed');
 await f.clock.advance(60000);assert.equal(f.videos.length,2);
 f.controller.retry();assert.equal(f.videos.length,3);f.controller.release();
}
// A slow functioning load, including beyond the soft 45s boundary, must not redownload.
{
 const f=fixture();f.controller.start();const v=f.videos[0];
 for(let i=0;i<60;i++){v.buffer+=0.25;await f.clock.advance(1000)}
 assert.equal(f.videos.length,1);assert.notEqual(f.controller.state,'recovering');
 v.currentTime=1;await f.clock.advance(1000);assert.equal(f.controller.state,'playing');f.controller.release();
}
// Playback really advances for five seconds; buffering grows, recovers, then genuinely stalls.
{
 const f=fixture();f.controller.start();const v=f.videos[0];v.networkState=2;
 for(let i=0;i<60;i++){v.dispatchEvent(new Event('progress'));await f.clock.advance(1000)}
 assert.equal(f.videos.length,1);assert.equal(v.buffered.length,0);f.controller.release();
}
// A canceled seek preserves the original resume target; an unseekable reload offers an explicit restart.
{
 const f=fixture();f.controller.start();const first=f.videos[0];first.currentTime=8;
 await f.clock.advance(1000);await f.clock.advance(18000);
 assert.equal(f.videos.length,2);f.controller.cancel('hidden');f.controller.start();
 const resumed=f.videos[2];resumed.readyState=1;resumed.dispatchEvent(new Event('loadedmetadata'));
 assert.equal(resumed.currentTime,8);f.controller.release();
}
{
 const f=fixture();f.controller.start();f.videos[0].currentTime=8;
 await f.clock.advance(19000);await f.clock.advance(15000);
 assert.equal(f.controller.state,'failed');f.controller.retry();
 assert.equal(f.videos.at(-1).currentTime,0);f.controller.release();
}
// Playback really advances for five seconds; buffering grows, recovers, then genuinely stalls.
{
 const f=fixture();f.controller.start();const v=f.videos[0];v.readyState=3;
 for(let i=1;i<=5;i++){v.currentTime=i;await f.clock.advance(1000)}
 for(let i=0;i<10;i++){v.buffer+=1;await f.clock.advance(1000)}
 assert.equal(f.videos.length,1);v.currentTime=6;await f.clock.advance(1000);
 assert.equal(f.controller.state,'playing');v.volume=.3;v.playbackRate=1.5;
 await f.clock.advance(15000);assert.equal(f.controller.state,'recovering');
 await f.clock.advance(3000);const restored=f.videos[1];restored.readyState=1;
 restored.dispatchEvent(new Event('loadedmetadata'));await Promise.resolve();
 assert.equal(restored.currentTime,6);assert.equal(restored.volume,.3);assert.equal(restored.playbackRate,1.5);assert.equal(restored.muted,true);
 f.controller.release();
}
// Every invalidating action defeats pending backoff, and late old element events.
for(const action of ['pause','offscreen','carousel','close','hidden','replace']){
 const f=fixture();f.controller.start();await f.clock.advance(15000);const old=f.videos[0];
 if(action==='pause')f.controller.pause();
 else if(action==='carousel')f.controller.setItem({id:'2',source:'other'});
 else if(action==='close'||action==='replace')f.controller.release();
 else {f.setEligible(false);f.controller.cancel(action)}
 old.dispatchEvent(new Event('playing'));old.dispatchEvent(new Event('error'));
 await f.clock.advance(60000);assert.equal(f.videos.length,1,action);assert.equal(old.src,'');
}
// Pending play promises cannot revive canceled players.
{
 let resolve;const f=fixture({make:()=>{const v=new Video();v.pending=new Promise(r=>resolve=r);return v}});
 f.controller.start();f.controller.pause();resolve();await Promise.resolve();await Promise.resolve();
 assert.equal(f.controller.state,'user-paused');assert.equal(f.videos[0].src,'');
}
// Details honor the sound preference; cards always start muted.
{
 const detail=fixture({card:false,muted:false});detail.controller.start();
 assert.equal(detail.videos[0].muted,false);detail.controller.release();
 const card=fixture({card:true,muted:false});card.controller.start();
 assert.equal(card.videos[0].muted,true);card.controller.release();
 const quiet=fixture({card:false,muted:true});quiet.controller.start();
 assert.equal(quiet.videos[0].muted,true);quiet.controller.release();
}
// A browser that refuses audible playback gets muted playback, not a stop.
{
 const f=fixture({card:false,muted:false,make:()=>{const v=new Video();v.play=function(){this.plays=(this.plays||0)+1;
  if(!this.muted)return Promise.reject({name:'NotAllowedError'});this.paused=false;return Promise.resolve()};return v}});
 f.controller.start();f.videos[0].dispatchEvent(new Event('loadedmetadata'));await f.clock.advance(1000);
 assert.equal(f.videos.length,1);assert.equal(f.videos[0].muted,true);assert.equal(f.videos[0].plays,2);
 assert.notEqual(f.controller.state,'awaiting-gesture');f.controller.release();
}
// Browser policy denial isn't a network retry; unsupported media isn't blindly reloaded.
for(const name of ['NotAllowedError','NotSupportedError']){
 const f=fixture({make:()=>{const v=new Video();v.playError={name};return v}});
 f.controller.start();await f.clock.advance(60000);assert.equal(f.videos.length,1);
 assert.equal(f.controller.state,name==='NotAllowedError'?'awaiting-gesture':'failed');f.controller.release();
}
// Shared budget survives separate card/detail controllers, visibility, and brief playback.
{
 const budget=new Map();const a=fixture({budget});a.controller.start();await a.clock.advance(18000);a.controller.release();
 const b=fixture({budget,card:false});b.controller.start();await b.clock.advance(25000);
 assert.equal(b.videos.length,2);await b.clock.advance(60000);assert.equal(b.videos.length,2);assert.equal(b.controller.state,'failed');
}
// Details get two automatic reloads, then a manual action.
{
 const f=fixture({card:false});f.controller.start();await f.clock.advance(18000);
 assert.equal(f.videos.length,2);await f.clock.advance(25000);assert.equal(f.videos.length,3);
 await f.clock.advance(15000);assert.equal(f.controller.state,'failed');f.controller.release();
}
// The production recovery scheduler separates simultaneous attempts by one second.
{
 const clock=new Clock();const a=fixture({clock,reserve:null}),b=fixture({clock,reserve:null});
 a.controller.start();b.controller.start();await clock.advance(18000);
 assert.equal(a.videos.length,2);assert.equal(b.videos.length,1);
 await clock.advance(1000);assert.equal(b.videos.length,2);a.controller.release();b.controller.release();
}
// Offline events do not consume a reload; return online respects eligibility.
{
 const f=fixture();f.setOnline(false);f.controller.start();await f.clock.advance(15000);
 assert.equal(f.controller.state,'offline');f.setOnline(true);f.controller.online();await f.clock.advance(3000);assert.equal(f.videos.length,2);f.controller.release();
}
// Native detail pause keeps its element and intent; resume uses the same source.
{
 const f=fixture({card:false});f.controller.start();await Promise.resolve();const v=f.videos[0];v.pause();
 await f.clock.advance(60000);assert.equal(f.controller.state,'user-paused');assert.equal(f.videos.length,1);
 v.play();await f.clock.advance(1000);assert.equal(f.videos.length,1);f.controller.release();
}
console.log('video lifecycle: slow loads, natural recovery, true stalls, budgets, restoration, cancellation and native controls passed');

async function played(f) {
 f.controller.start();const v=f.videos.at(-1);v.readyState=3;v.buffer=v.duration;v.networkState=1;v.currentTime=8;
 await f.clock.advance(1000);return v;
}
// Brief absence retains the decoded frame and source, ignoring queued media events.
{
 const clock=new Clock(),retention=createVideoRetentionPool({clock});
 const f=fixture({clock,retention});const v=await played(f),source=v.src;
 f.setEligible(false);assert(f.controller.park());assert(v.paused);
 for(const event of ['progress','canplay','playing','play','error'])v.dispatchEvent(new Event(event));
 await clock.advance(5000);assert.equal(f.controller.state,'parked');assert.equal(v.src,source);
 f.setEligible(true);f.controller.start();await clock.advance(0);
 assert.equal(f.controller.video,v);assert.equal(f.videos.length,1);assert.equal(v.loads||0,0);
 assert.equal(f.controller.state,'playing');assert.equal(retention.size,0);assert.equal(f.budget.size,0);
 assert.equal(clock.tasks.size,1); // Exactly one watchdog, with no parked expiry left.
 await clock.advance(15000);assert.equal(f.controller.state,'recovering');
 await clock.advance(3000);assert.equal(f.videos.length,2);f.controller.release();
}
// Repeated refreshes do not extend the deadline; eviction preserves resume position.
{
 const clock=new Clock(),retention=createVideoRetentionPool({clock});
 const f=fixture({clock,retention});const v=await played(f);
 f.setEligible(false);f.controller.park();await clock.advance(VIDEO_POLICY.parkedMs-1);
 f.controller.park();v.currentTime=0;await clock.advance(1);
 assert.equal(v.src,'');assert(v.removed);assert.equal(retention.size,0);
 f.setEligible(true);f.controller.start();const next=f.videos.at(-1);
 next.readyState=3;next.dispatchEvent(new Event('loadedmetadata'));
 assert.equal(next.currentTime,8);assert.equal(f.budget.size,0);f.controller.release();
}
// Retain the whole recently viewed grid; there is no player-count eviction.
{
 const clock=new Clock(),retention=createVideoRetentionPool({clock});
 const fixtures=[];
 for(let i=0;i<20;i++){
  const f=fixture({clock,retention});await played(f);f.setEligible(false);f.controller.park();fixtures.push(f);
 }
 assert.equal(retention.size,20);
 for(const f of fixtures){
  const v=f.videos[0];assert(v.src);assert(!v.removed);f.setEligible(true);f.controller.start();
  assert.equal(f.controller.video,v);assert.equal(f.videos.length,1);assert.equal(f.budget.size,0);
 }
 assert.equal(retention.size,0);
 for(const f of fixtures){f.setEligible(false);f.controller.park()}
 retention.clear();assert.equal(retention.size,0);
 for(const f of fixtures){assert.equal(f.videos[0].src,'');f.controller.release()}
}
// Starting downloads and detail players cannot be retained by the card-only path.
for(const card of [true,false]){
 const clock=new Clock(),retention=createVideoRetentionPool({clock});const f=fixture({clock,retention,card});
 f.controller.start();assert.equal(f.controller.park(),false);f.controller.cancel('offscreen');
 assert.equal(retention.size,0);assert.equal(f.videos[0].src,'');f.controller.release();
}
// Playback alone is insufficient: incomplete or still-fetching streams must
// release their connection rather than blocking newly visible videos.
for(const failure of ['fetching','partial','missing-start','gapped','unknown-duration','empty']){
 const clock=new Clock(),retention=createVideoRetentionPool({clock}),f=fixture({clock,retention});
 const v=await played(f);
 if(failure==='fetching')v.networkState=2;
 if(failure==='partial')v.buffer=v.duration/2;
 if(failure==='missing-start')Object.defineProperty(v,'buffered',{value:{length:1,start:()=>8,end:()=>v.duration}});
 if(failure==='gapped')Object.defineProperty(v,'buffered',{value:{length:2,start:i=>i?20:0,end:i=>i?v.duration:10}});
 if(failure==='unknown-duration')v.duration=Infinity;
 if(failure==='empty')v.buffer=0;
 f.setEligible(false);assert.equal(f.controller.park(),false,failure);f.controller.cancel('offscreen');
 assert.equal(retention.size,0);assert.equal(v.src,'');assert(v.removed);assert.equal(f.budget.size,0);
 f.controller.release();
}
// An errored/discarded parked source starts fresh without using recovery attempts.
for(const failure of ['error','discarded']){
 const clock=new Clock(),retention=createVideoRetentionPool({clock});const f=fixture({clock,retention});
 const v=await played(f);f.setEligible(false);f.controller.park();
 if(failure==='error')v.error={code:2};else v.networkState=3;
 v.currentTime=0;
 v.dispatchEvent(new Event('error'));assert.equal(f.controller.state,'parked');
 f.setEligible(true);f.controller.start();assert.equal(f.videos.length,2);assert.equal(v.src,'');
 assert.equal(f.budget.size,0);assert.equal(f.events.length,0);
 const next=f.videos.at(-1);next.readyState=3;next.dispatchEvent(new Event('loadedmetadata'));
 assert.equal(next.currentTime,8);f.controller.release();
}
// Parking neither spends nor resets a recovery budget already used by this item.
{
 const clock=new Clock(),retention=createVideoRetentionPool({clock});const f=fixture({clock,retention});
 f.controller.start();await clock.advance(18000);assert.equal(f.videos.length,2);
 const v=f.videos[1];v.readyState=3;v.buffer=v.duration;v.networkState=1;v.currentTime=8;await clock.advance(1000);
 f.setEligible(false);f.controller.park();f.setEligible(true);f.controller.start();await clock.advance(15000);
 assert.equal(f.controller.state,'failed');assert.equal(f.videos.length,2);assert.equal(f.budget.get('1'),1);
 f.controller.release();
}
// Every hard invalidation still releases retained scopes and their expiry timers.
for(const action of ['navigation','hidden','removed','source-change','item-change']){
 const clock=new Clock(),retention=createVideoRetentionPool({clock});const f=fixture({clock,retention});
 const v=await played(f);f.setEligible(false);f.controller.park();
 if(action==='removed')f.controller.release();
 else if(action==='source-change')f.controller.setItem({id:'1',source:'different.mp4'});
 else if(action==='item-change')f.controller.setItem({id:'2',source:v.src});
 else f.controller.cancel(action);
 assert.equal(v.src,'');assert.equal(retention.size,0);
 for(const event of ['playing','play','canplay','error'])v.dispatchEvent(new Event(event));
 await clock.advance(60000);assert.equal(f.videos.length,1);assert(v.paused);f.controller.release();
}
// A pending play promise cannot restart playback or render 'playing' after parking.
{
 let resolve;const clock=new Clock(),retention=createVideoRetentionPool({clock});
 const f=fixture({clock,retention,make:()=>{const v=new Video();v.pending=new Promise(r=>resolve=r);return v}});
 const v=await played(f);f.setEligible(false);f.controller.park();resolve();await clock.advance(0);
 assert.equal(f.controller.state,'parked');assert(v.paused);assert(v.src);f.controller.release();
}
console.log('video retention: whole-grid reuse, expiry, queued events, budgets and hard invalidation passed');
