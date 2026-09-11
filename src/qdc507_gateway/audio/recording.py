"""Bounded, opt-in recording of unmodified PCM at the WebSocket boundary."""
import asyncio
import json
import logging
import uuid
from pathlib import Path

import io
import wave
from collections import OrderedDict
from datetime import datetime, timezone


class DebugRecordings:
    LIMIT = 8000 * 2 * 300
    DIRECTIONS = ('client_to_bridge', 'bridge_to_client')

    def __init__(self, enabled=False, directory=None):
        self.directory = Path(directory) if directory is not None else None
        self.enabled = enabled
        self.records = OrderedDict()

    def start(self, call_id):
        if not self.enabled:
            return None
        if call_id in self.records:
            return self.status(call_id)
        while len(self.records) >= 3:
            self.records.popitem(last=False)
        self.records[call_id] = {
            'call_id': call_id, 'started_at': datetime.now(timezone.utc).isoformat(),
            'stopped_at': None, 'active': True,
            'tracks': {name: bytearray() for name in self.DIRECTIONS},
            'truncated': False,
        }
        return self.status(call_id)

    def append(self, call_id, direction, data):
        record = self.records.get(call_id)
        if record is None or not record['active']:
            return
        track = record['tracks'][direction]
        available = self.LIMIT - len(track)
        track.extend(data[:available])
        if len(track) >= self.LIMIT:
            record['truncated'] = True
            self.stop(call_id)

    def stop(self, call_id):
        record = self.records[call_id]
        if record['active']:
            record['active'] = False
            record['stopped_at'] = datetime.now(timezone.utc).isoformat()
        return self.status(call_id)

    def status(self, call_id):
        record = self.records[call_id]
        return {key: value for key, value in record.items() if key != 'tracks'} | {
            'sample_rate': 8000, 'channels': 1, 'encoding': 'pcm_s16le',
            'tracks': {name: {'bytes': len(data), 'duration_seconds': len(data) / 16000}
                       for name, data in record['tracks'].items()},
        }

    def snapshot(self, call_id, direction):
        return bytes(self.records[call_id]['tracks'][direction])

    @staticmethod
    def wav(data):
        output = io.BytesIO()
        with wave.open(output, 'wb') as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(data)
        return output.getvalue()

    async def finish(self, call_id):
        if call_id not in self.records:
            return
        metadata = self.stop(call_id)
        record = self.records.pop(call_id)
        if self.directory is None:
            return
        task = asyncio.create_task(asyncio.to_thread(self._save, record, metadata))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _save(self, record, metadata):
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex
            directory = self.directory / name
            directory.mkdir(mode=0o700)
            for direction, data in record['tracks'].items():
                path = directory / (direction + '.wav')
                with path.open('xb') as output:
                    path.chmod(0o600)
                    output.write(self.wav(data))
            path = directory / 'metadata.json'
            with path.open('x', encoding='utf-8') as output:
                path.chmod(0o600)
                json.dump(metadata, output, ensure_ascii=False, indent=2)
        except Exception as exc:
            # Debug storage failures must not break call cleanup or expose audio.
            logging.getLogger(__name__).error('debug recording save failed: %s', type(exc).__name__)
