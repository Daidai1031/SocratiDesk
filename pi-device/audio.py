import asyncio
import queue
import threading

import numpy as np
import sounddevice as sd


INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHANNELS = 1
DTYPE = "int16"
CHUNK_FRAMES = 1024


class MicStreamer:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.stream = None
        self.recording = False

    def _callback(self, indata, frames, time, status):
        if status:
            print("[MIC STATUS]", status)

        if not self.recording:
            return

        chunk = indata.copy().tobytes()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, chunk)

    def start(self):
        if self.recording:
            return

        self.recording = True
        self.stream = sd.InputStream(
            samplerate=INPUT_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=CHUNK_FRAMES,
            callback=self._callback,
        )
        self.stream.start()

    def stop(self):
        self.recording = False
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    async def get_chunk(self) -> bytes:
        return await self.queue.get()

    def clear_queue(self):
        try:
            while True:
                self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass


class SpeakerPlayer:
    def __init__(self):
        self.byte_queue = queue.Queue()
        self.stream = None
        self.started = False
        self.lock = threading.Lock()
        self.pending = bytearray()

    def _callback(self, outdata, frames, time, status):
        if status:
            print("[SPK STATUS]", status)

        bytes_needed = frames * 2  # mono int16
        while len(self.pending) < bytes_needed:
            try:
                chunk = self.byte_queue.get_nowait()
                self.pending.extend(chunk)
            except queue.Empty:
                break

        if len(self.pending) >= bytes_needed:
            buffer = self.pending[:bytes_needed]
            del self.pending[:bytes_needed]
        else:
            buffer = self.pending
            self.pending = bytearray()
            buffer += b"\x00" * (bytes_needed - len(buffer))

        audio_np = np.frombuffer(buffer, dtype=np.int16).reshape(-1, 1)
        outdata[:] = audio_np

    def start(self):
        with self.lock:
            if self.started:
                return
            self.stream = sd.OutputStream(
                samplerate=OUTPUT_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                blocksize=CHUNK_FRAMES,
                callback=self._callback,
            )
            self.stream.start()
            self.started = True

    def stop(self):
        with self.lock:
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            self.started = False

    def add_pcm16(self, pcm_bytes: bytes):
        if pcm_bytes:
            self.byte_queue.put(pcm_bytes)

    def clear(self):
        while not self.byte_queue.empty():
            try:
                self.byte_queue.get_nowait()
            except queue.Empty:
                break
        self.pending = bytearray()