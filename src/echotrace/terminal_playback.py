"""Small-screen evidence playback: RK decodes; Echo displays bounded RGB565 frames."""
from __future__ import annotations
import asyncio
import base64
import contextlib
import time
from .playback import make_playback_plan
from .store import Conflict, identifier


class ScreenPlayer:
    def __init__(self, settings, store, devices):
        self.settings, self.store, self.devices = settings, store, devices
        self.states, self.tasks, self.locks = {}, {}, {}

    def view(self, state):
        return {key: state.get(key) for key in (
            "id", "anchor_id", "state", "position_ms", "duration_ms", "message")}

    async def publish(self, device_id, state, pixels=None):
        peer = self.devices.connected.get(device_id)
        if not peer:
            raise Conflict("记录终端已离线")
        message = {"type": "screen.frame" if pixels else "screen.state", **self.view(state)}
        if pixels:
            message.update(data=base64.b64encode(pixels).decode(), width=160, height=90,
                           format="rgb565le")
        async with peer["send_lock"]:
            await peer["socket"].send_json(message)

    async def cancel(self, device_id):
        task = self.tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def start(self, device_id, anchor_id, offset_ms=0):
        async with self.locks.setdefault(device_id, asyncio.Lock()):
            return await self._start(device_id, anchor_id, offset_ms)

    async def _start(self, device_id, anchor_id, offset_ms=0):
        peer = self.devices.connected.get(device_id)
        if not peer or peer["capture_state"] not in ("stopped", "unknown"):
            raise Conflict("请先结束录制，再回看原始片段")
        await self.cancel(device_id)
        evidence = self.store.evidence(anchor_id)
        plan = make_playback_plan(evidence)
        duration = max(0, (plan["end_us"] - plan["begin_us"]) // 1000)
        if not duration:
            raise Conflict("这一步没有可播放的时间范围")
        offset_ms = int(offset_ms)
        if not 0 <= offset_ms < duration:
            raise ValueError("跳转位置超出片段范围")
        state = dict(id=identifier(), anchor_id=anchor_id, state="loading",
                     position_ms=offset_ms, duration_ms=duration, message="正在准备原片",
                     begin_us=plan["begin_us"], end_us=plan["end_us"],
                     origin_us=plan["begin_us"] + offset_ms * 1000, clock=None)
        self.states[device_id] = state
        await self.publish(device_id, state)
        self.tasks[device_id] = asyncio.create_task(self.run(device_id, state))
        return self.view(state)

    async def pause(self, device_id):
        async with self.locks.setdefault(device_id, asyncio.Lock()):
            return await self._pause(device_id)

    async def _pause(self, device_id):
        state = self.states.get(device_id)
        if not state:
            return {"state": "stopped"}
        if state.get("clock") is not None:
            cursor = state["origin_us"] + int((time.monotonic() - state["clock"]) * 1e6)
            state["position_ms"] = min(state["duration_ms"] - 1,
                                      max(0, (cursor - state["begin_us"]) // 1000))
        await self.cancel(device_id)
        state.update(state="paused", clock=None, message="已暂停")
        await self.publish(device_id, state)
        return self.view(state)

    async def stop(self, device_id):
        async with self.locks.setdefault(device_id, asyncio.Lock()):
            return await self._stop(device_id)

    async def _stop(self, device_id):
        await self.cancel(device_id)
        state = self.states.get(device_id)
        if state:
            state.update(state="stopped", clock=None, message="已停止")
            with contextlib.suppress(Conflict, RuntimeError):
                await self.publish(device_id, state)
            return self.view(state)
        return {"state": "stopped"}

    async def resume(self, device_id, offset_ms=None):
        state = self.states.get(device_id)
        if not state:
            raise Conflict("请先打开一个步骤")
        return await self.start(device_id, state["anchor_id"],
                                state["position_ms"] if offset_ms is None else offset_ms)

    async def ready_media(self, evidence, timeout=120):
        deadline = time.monotonic() + timeout
        while True:
            missing = [m for m in evidence["media"]
                       if not self.store.verify_media(self.store.media(m["id"]))]
            if not missing:
                return self.store.evidence(evidence["anchor"]["id"])
            for media in missing:
                await self.devices.fetch(media["id"])
            if time.monotonic() >= deadline:
                raise Conflict("原片仍未传输完成，请恢复连接后重试")
            await asyncio.sleep(.5)

    async def audio(self, device_id, state, segments):
        cursor = state["origin_us"]
        for item in segments:
            if item["end_us"] <= cursor:
                continue
            begin = max(cursor, item["start_us"])
            if begin >= state["end_us"]:
                break
            wait = (begin - state["origin_us"]) / 1e6 - (time.monotonic() - state["clock"])
            if wait > 0:
                await asyncio.sleep(wait)
            actual = state["origin_us"] + int((time.monotonic() - state["clock"]) * 1e6)
            offset = max(0., (actual - item["start_us"]) / 1e6)
            if actual < item["end_us"]:
                await self.devices.command(device_id, "playback.start",
                    {"media_id": item["id"], "offset_seconds": offset})

    async def run(self, device_id, state):
        process, audio_task = None, None
        try:
            evidence = await self.ready_media(self.store.evidence(state["anchor_id"]))
            plan = make_playback_plan(evidence)
            cursor = state["origin_us"]
            if not plan["video"] and plan["audio"]:
                state["clock"] = time.monotonic()
                state.update(state="playing", message="此段只有原始声音")
                audio_task = asyncio.create_task(self.audio(device_id, state, plan["audio"]))
                while cursor < state["end_us"]:
                    state["position_ms"] = max(0, (cursor - state["begin_us"]) // 1000)
                    await self.publish(device_id, state)
                    await asyncio.sleep(.2)
                    if audio_task.done() and audio_task.exception(): raise audio_task.exception()
                    cursor = state["origin_us"] + int((time.monotonic() - state["clock"]) * 1e6)
            while cursor < state["end_us"]:
                peer = self.devices.connected.get(device_id)
                if not peer or peer["capture_state"] not in ("stopped", "unknown"):
                    raise Conflict("录制优先，历史播放已停止")
                video = next((v for v in plan["video"]
                              if v["start_us"] <= cursor < v["end_us"]), None)
                if video is None:
                    video = next((v for v in plan["video"] if cursor < v["start_us"] <= cursor + 50000), None)
                    if video is not None: cursor = video["start_us"]
                    else: raise Conflict("此处没有原始画面，播放已停在缺口")
                item = self.store.media(video["id"])
                source = self.store.managed_path(item["path"])
                stop = min(video["end_us"], state["end_us"])
                process = await asyncio.create_subprocess_exec(
                    self.settings.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-ss", str((cursor - video["start_us"]) / 1e6), "-i", str(source),
                    "-t", str((stop - cursor) / 1e6), "-an", "-threads", "2",
                    "-vf", "fps=5,scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2",
                    "-pix_fmt", "rgb565le", "-f", "rawvideo", "pipe:1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                segment_start = cursor
                index = 0
                while cursor < stop:
                    try:
                        pixels = await asyncio.wait_for(process.stdout.readexactly(28800), 8)
                    except asyncio.IncompleteReadError:
                        if index > 0 and stop - cursor <= 250000:
                            cursor = stop
                            break
                        raise Conflict("原视频解码中断，播放已暂停")
                    if state["clock"] is None:
                        state["clock"] = time.monotonic()
                        state.update(state="playing", message="" if plan["audio"] else "此段没有原始声音")
                        audio_task = asyncio.create_task(self.audio(device_id, state, plan["audio"]))
                    deadline = state["clock"] + (cursor - state["origin_us"]) / 1e6
                    delay = deadline - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if audio_task.done() and not audio_task.cancelled() and audio_task.exception():
                        raise audio_task.exception()
                    state["position_ms"] = max(0, (cursor - state["begin_us"]) // 1000)
                    if time.monotonic() - deadline < .5:
                        await self.publish(device_id, state, pixels)
                    index += 1
                    cursor = min(stop, segment_start + index * 200000)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError): process.kill()
                await process.wait()
                process = None
            state.update(state="ended", position_ms=state["duration_ms"], clock=None,
                         message="片段播放结束")
            await self.publish(device_id, state)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.update(state="paused", clock=None, message=str(error)[:180])
            with contextlib.suppress(Exception):
                await self.publish(device_id, state)
        finally:
            if process and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), 2)
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if audio_task:
                audio_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await audio_task
            if device_id in self.devices.connected:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.devices.command(device_id, "playback.stop"), 3)

    async def close(self):
        for device_id in list(self.tasks):
            await self.cancel(device_id)
