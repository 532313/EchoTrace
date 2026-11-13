'use strict';

class EvidencePlayback {
  constructor(root, plan, fetchBlob, notify) {
    this.root = root;
    this.plan = plan;
    this.fetchBlob = fetchBlob;
    this.notify = notify;
    this.video = root.querySelector('video');
    this.audio = root.querySelector('audio');
    this.masterKind = plan.video.length ? 'video' : 'audio';
    if (this.masterKind === 'audio') {
      this.video.hidden = true;
      this.video = this.audio;
    }
    this.slider = root.querySelector('input[type=range]');
    this.sound = root.querySelector('input[type=checkbox]');
    this.output = root.querySelector('output');
    this.message = root.querySelector('.playback-message');
    this.button = root.querySelector('[data-play]');
    this.assets = new Map();
    this.urls = new Set();
    this.downloads = new AbortController();
    this.transition = new AbortController();
    this.positionUs = plan.anchor_us;
    this.generation = 0;
    this.audioGeneration = 0;
    this.wanted = false;
    this.disposed = false;
    this.loading = false;
    this.audioLoading = false;
    this.currentVideo = null;
    this.currentAudio = null;
    this.slider.max = Math.max(0, Math.ceil((plan.end_us - plan.begin_us) / 1000));
    this.button.onclick = () => this.wanted ? this.pause() : this.play();
    this.slider.oninput = () => { this.output.textContent = `跳转到 ${(this.slider.value / 1000).toFixed(1)} 秒`; };
    this.slider.onchange = () => this.seek(plan.begin_us + Number(this.slider.value) * 1000);
    root.querySelector('[data-begin]').onclick = () => this.seek(plan.begin_us);
    this.sound.onchange = () => {
      ++this.audioGeneration;
      this.audio.pause();
      this.currentAudio = null;
      this.audioLoading = false;
      this.syncAudio().then(() => this.resume()).catch(error => this.fail(error));
    };
    this.video.ontimeupdate = () => this.tick();
    this.video.onended = () => this.next();
    this.video.onerror = () => { if (!this.disposed && !this.loading && this.currentVideo) this.fail(new Error('原始片段无法解码')); };
    this.renderPosition();
    const gaps = plan.gaps.filter(gap => gap.end_us > gap.start_us);
    root.querySelector('.playback-gaps').textContent = gaps.length
      ? gaps.map(gap => `${gap.kind === 'video' ? '画面' : '声音'} ${(gap.start_us-plan.begin_us)/1e6}–${(gap.end_us-plan.begin_us)/1e6}秒：${gap.reason === 'uncached' ? '尚未缓存' : '未记录'}`).join('；')
      : '全部片段已缓存';
  }

  at(clips, stamp) {
    return clips.filter(clip => clip.start_us <= stamp && stamp < clip.end_us).at(-1);
  }

  async asset(clip) {
    if (!clip.available || !clip.url) throw new Error('该片段尚未缓存，请先获取原件');
    if (!this.assets.has(clip.id)) {
      const pending = this.fetchBlob(clip.url, this.downloads.signal).then(blob => {
        if (blob.size !== clip.size_bytes) throw new Error('原始片段长度校验失败');
        if (this.disposed) throw new DOMException('已离开当前证据', 'AbortError');
        const url = URL.createObjectURL(blob);
        this.urls.add(url);
        return url;
      }).catch(error => { this.assets.delete(clip.id); throw error; });
      this.assets.set(clip.id, pending);
    }
    return this.assets.get(clip.id);
  }

  wait(element, event, action, signal) {
    return new Promise((resolve, reject) => {
      const clean = () => {
        clearTimeout(timer);
        element.removeEventListener(event, ready);
        element.removeEventListener('error', failed);
        signal.removeEventListener('abort', aborted);
      };
      const ready = () => { clean(); resolve(); };
      const failed = () => { clean(); reject(new Error('原始片段无法加载或解码')); };
      const aborted = () => { clean(); reject(new DOMException('已切换播放位置', 'AbortError')); };
      const timer = setTimeout(failed, 15000);
      element.addEventListener(event, ready, {once:true});
      element.addEventListener('error', failed, {once:true});
      signal.addEventListener('abort', aborted, {once:true});
      if (signal.aborted) aborted(); else action();
    });
  }

  async source(element, url, offset, signal) {
    await this.wait(element, 'loadedmetadata', () => { element.src = url; element.load(); }, signal);
    if (Math.abs(element.currentTime-offset) > .001)
      await this.wait(element, 'seeked', () => { element.currentTime = Math.max(0, Math.min(element.duration-.001, offset)); }, signal);
  }

  async seek(stamp) {
    if (this.disposed || this.plan.end_us <= this.plan.begin_us) return;
    this.transition.abort();
    this.transition = new AbortController();
    const signal = this.transition.signal;
    const generation = ++this.generation;
    ++this.audioGeneration;
    this.video.pause();
    this.audio.pause();
    this.loading = true;
    this.video.removeAttribute('src');
    this.video.load();
    this.audioLoading = false;
    this.currentAudio = null;
    this.positionUs = Math.max(this.plan.begin_us, Math.min(this.plan.end_us-1, stamp));
    this.currentVideo = this.at(this.plan[this.masterKind], this.positionUs);
    this.renderPosition();
    if (!this.currentVideo) {
      this.video.removeAttribute('src'); this.video.load();
      this.fail(new Error('该时间没有对应记录，可用进度条选择后续片段'));
      return;
    }
    this.message.textContent = '正在准备原始片段…';
    try {
      const clip = this.currentVideo;
      const url = await this.asset(clip);
      if (generation !== this.generation || this.disposed) return;
      await this.source(this.video, url, (this.positionUs-clip.start_us)/1e6, signal);
      if (Math.abs(this.video.duration*1e6 - (clip.end_us-clip.start_us)) > 200000)
        throw new Error('片段时长与来源时间不一致，已暂停回放');
      if (generation !== this.generation || this.disposed) return;
      this.loading = false;
      this.message.textContent = '';
      await this.syncAudio();
      if (generation !== this.generation || this.disposed) return;
      await this.resume();
      for (const kind of ['video','audio']) {
        const next = this.plan[kind].find(item => item.start_us >= clip.end_us && item.available);
        if (next) this.asset(next).catch(() => {});
      }
    } catch (error) {
      if (generation === this.generation && !this.disposed && error.name !== 'AbortError') this.fail(error);
    }
  }

  async play() {
    if (this.disposed) return;
    this.wanted = true;
    this.button.textContent = '暂停';
    if (this.loading) return;
    if (!this.currentVideo || !this.video.src || this.positionUs >= this.plan.end_us)
      return this.seek(this.positionUs >= this.plan.end_us ? this.plan.begin_us : this.positionUs);
    try { await this.syncAudio(); await this.resume(); } catch (error) { this.fail(error); }
  }

  async resume() {
    if (!this.wanted || this.loading || this.audioLoading || this.disposed || this.video.ended) return;
    if (this.masterKind === 'video' && this.sound.checked && this.currentAudio) await this.audio.play();
    if (this.masterKind === 'audio') this.video.muted = !this.sound.checked;
    if (this.wanted && !this.disposed) await this.video.play();
    this.button.textContent = this.wanted ? '暂停' : '播放';
  }

  pause() {
    this.wanted = false;
    this.video.pause();
    this.audio.pause();
    this.button.textContent = '播放';
  }

  fail(error) {
    if (this.disposed || error.name === 'AbortError') return;
    this.loading = false;
    this.pause();
    const text = error.name === 'NotAllowedError' ? '请再次点击播放以启用浏览器声音' : error.message;
    this.message.textContent = text;
    this.notify(text);
  }

  renderPosition() {
    if (this.disposed) return;
    this.slider.value = Math.max(0, (this.positionUs-this.plan.begin_us)/1000);
    const clips = this.plan[this.masterKind];
    const index = clips.findIndex(item => item.id === this.currentVideo?.id);
    this.output.textContent = `${this.masterKind === 'audio' ? '仅声音 · ' : ''}${((this.positionUs-this.plan.begin_us)/1e6).toFixed(1)} / ${((this.plan.end_us-this.plan.begin_us)/1e6).toFixed(1)} 秒${index >= 0 ? ` · 第 ${index+1}/${clips.length} 段` : ''}`;
  }

  async syncAudio() {
    if (this.masterKind === 'audio') return;
    if (this.disposed || this.loading || this.audioLoading) return;
    const clip = this.sound.checked ? this.at(this.plan.audio, this.positionUs) : null;
    if (!clip?.available) {
      this.audio.pause(); this.currentAudio = null;
      if (this.sound.checked) this.message.textContent = clip ? '此处声音尚未缓存' : '此处没有声音记录';
      return;
    }
    const target = (this.positionUs-clip.start_us)/1e6;
    if (this.currentAudio?.id === clip.id) {
      if (Math.abs(this.audio.currentTime-target) > .15) this.audio.currentTime = target;
      return;
    }
    const generation = this.generation;
    const audioGeneration = ++this.audioGeneration;
    this.audioLoading = true;
    this.video.pause();
    this.audio.pause();
    try {
      const url = await this.asset(clip);
      if (this.disposed || generation !== this.generation || audioGeneration !== this.audioGeneration) return;
      await this.source(this.audio, url, target, this.transition.signal);
      if (this.disposed || generation !== this.generation || audioGeneration !== this.audioGeneration) return;
      this.currentAudio = clip;
      this.audioLoading = false;
    } catch (error) {
      if (generation !== this.generation || audioGeneration !== this.audioGeneration || this.disposed) return;
      this.audioLoading = false;
      this.currentAudio = null;
      this.sound.checked = false;
      this.message.textContent = '声音片段不可用，继续显示原始画面';
      if (error.name !== 'AbortError') this.notify(this.message.textContent);
    }
  }

  tick() {
    if (this.disposed || this.loading || !this.currentVideo) return;
    if (this.video.ended) { this.next(); return; }
    this.positionUs = Math.min(this.plan.end_us, this.currentVideo.start_us+this.video.currentTime*1e6);
    this.renderPosition();
    if (this.positionUs >= this.plan.end_us) { this.pause(); this.message.textContent = '这段证据已播放完毕'; return; }
    this.syncAudio().then(() => this.resume()).catch(error => this.fail(error));
  }

  next() {
    if (this.disposed || this.loading || !this.currentVideo || !this.wanted) return;
    const end = this.currentVideo.end_us;
    this.positionUs = Math.min(end, this.plan.end_us);
    this.renderPosition();
    if (end >= this.plan.end_us) { this.pause(); this.message.textContent = '这段证据已播放完毕'; return; }
    const clips = this.plan[this.masterKind];
    const index = clips.findIndex(clip => clip.id === this.currentVideo.id);
    const next = clips[index+1];
    if (!next || next.start_us-end > this.plan.continuity_tolerance_us) {
      this.fail(new Error('记录在此中断，可用进度条选择后续片段'));
      return;
    }
    this.seek(Math.max(this.plan.begin_us, next.start_us));
  }

  dispose() {
    if (this.disposed) return;
    this.pause();
    this.disposed = true;
    ++this.generation; ++this.audioGeneration;
    this.transition.abort(); this.downloads.abort();
    this.video.removeAttribute('src'); this.audio.removeAttribute('src');
    this.video.load(); this.audio.load();
    for (const url of this.urls) URL.revokeObjectURL(url);
    this.urls.clear();
  }
}

if (typeof module !== 'undefined') module.exports = EvidencePlayback;
else window.EvidencePlayback = EvidencePlayback;
