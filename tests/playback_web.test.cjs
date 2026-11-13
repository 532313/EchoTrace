const {test} = require('node:test');
const assert = require('node:assert/strict');
const EvidencePlayback = require('../src/echotrace/web/playback.js');

class Media extends EventTarget {
  constructor() { super(); this.src=''; this.paused=true; this.ended=false; this._time=0; this.duration=2; }
  load() { this._time=0; this.ended=false; if(this.src)queueMicrotask(()=>this.dispatchEvent(new Event('loadedmetadata'))); }
  removeAttribute() { this.src=''; }
  get currentTime() { return this._time; }
  set currentTime(value) { this._time=value; this.ended=false; queueMicrotask(()=>this.dispatchEvent(new Event('seeked'))); }
  pause() { this.paused=true; }
  async play() { this.paused=false; }
  finish() { this._time=this.duration; this.ended=true; this.paused=true; this.ontimeupdate?.(); this.onended?.(); }
}

function fixture() {
  const elements=new Map([['video',new Media()],['audio',new Media()]]);
  const root={querySelector(selector){if(!elements.has(selector))elements.set(selector,{textContent:'',value:0,checked:true});return elements.get(selector);}};
  const plan={begin_us:1e6,end_us:7e6,anchor_us:1e6,continuity_tolerance_us:50000,gaps:[],video:[],audio:[]};
  for(const kind of ['video','audio'])for(let index=0;index<3;index++)plan[kind].push({id:`${kind}-${index+1}`,start_us:(1+2*index)*1e6,end_us:(3+2*index)*1e6,available:true,url:`/${kind}-${index+1}`,size_bytes:1});
  const notices=[];
  return {root,plan,notices,create(fetchBlob=async()=>new Blob(['x'])){return new EvidencePlayback(root,plan,fetchBlob,text=>notices.push(text));}};
}

async function until(predicate) {
  for(let attempt=0;attempt<100;attempt++) { if(predicate())return; await new Promise(resolve=>setImmediate(resolve)); }
  assert.fail('asynchronous playback did not settle');
}

test('continues across a file boundary and preserves paused seek and audio offset',async()=>{
  const setup=fixture(), player=setup.create();
  try {
    await player.seek(1e6);
    assert.equal(player.video.paused,true);
    await player.play();
    player.video.finish();
    await until(()=>player.currentVideo.id==='video-2'&&!player.loading&&!player.audioLoading);
    assert.equal(player.wanted,true);
    player.pause();
    await player.seek(5.5e6);
    assert.equal(player.currentVideo.id,'video-3');
    assert.equal(player.video.paused,true);
    assert.equal(player.audio.currentTime,.5);
    assert.equal(player.positionUs,5.5e6);
    await player.play();
    player.video.finish();
    assert.equal(player.wanted,false);
    assert.equal(player.positionUs,7e6);
    assert.equal(player.audio.paused,true);
    assert.deepEqual(setup.notices,[]);
  } finally { player.dispose(); }
});

test('stops at unrecorded time and allows explicit seeking after the gap',async()=>{
  const setup=fixture();
  setup.plan.video[2].start_us+=1e6;
  setup.plan.video[2].end_us+=1e6;
  setup.plan.end_us+=1e6;
  const player=setup.create();
  try {
    await player.seek(3e6); await player.play(); player.video.finish();
    assert.equal(player.wanted,false);
    assert.equal(player.positionUs,5e6);
    assert.match(setup.notices.at(-1),/中断/);
    await player.seek(6.5e6);
    assert.equal(player.currentVideo.id,'video-3');
    assert.equal(player.wanted,false);
  } finally { player.dispose(); }
});

test('an uncached segment never replays the preceding file under a new timestamp',async()=>{
  const setup=fixture(); setup.plan.video[1].available=false;
  const player=setup.create();
  try {
    await player.seek(1e6); await player.play(); player.video.finish();
    await until(()=>!player.loading);
    assert.equal(player.wanted,false);
    assert.equal(player.video.src,'');
    await player.play();
    assert.equal(player.video.src,'');
    assert.equal(player.wanted,false);
    assert.match(setup.notices.at(-1),/尚未缓存/);
  } finally { player.dispose(); }
});

test('late downloads cannot replace a newer selection and dispose releases blob URLs',async()=>{
  const setup=fixture(); let finishFirst;
  const player=setup.create(path=>path==='/video-1'?new Promise(resolve=>finishFirst=resolve):Promise.resolve(new Blob(['x'])));
  const first=player.seek(1e6);
  await player.seek(5e6);
  const currentSource=player.video.src;
  finishFirst(new Blob(['x'])); await first;
  assert.equal(player.currentVideo.id,'video-3');
  assert.equal(player.video.src,currentSource);
  assert.ok(player.urls.size>0);
  player.dispose();
  assert.equal(player.urls.size,0);
  assert.equal(player.audio.paused,true);
});

test('audio-only evidence continues and seeks without requiring a video file',async()=>{
  const setup=fixture(); setup.plan.video=[];
  const player=setup.create();
  try {
    await player.seek(1e6); await player.play();
    player.video.finish();
    await until(()=>player.currentVideo.id==='audio-2'&&!player.loading);
    player.pause(); await player.seek(5.5e6);
    assert.equal(player.currentVideo.id,'audio-3');
    assert.equal(player.video.currentTime,.5);
    assert.equal(player.video.paused,true);
    assert.match(player.output.textContent,/仅声音/);
    assert.equal(setup.root.querySelector('video').hidden,true);
  } finally { player.dispose(); }
});
