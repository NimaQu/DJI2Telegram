"""Bounded, opt-in recording of unmodified PCM at the WebSocket boundary."""
import io
import wave
from collections import OrderedDict
from datetime import datetime, timezone


class DebugRecordings:
    LIMIT = 8000 * 2 * 300
    DIRECTIONS = ('client_to_bridge', 'bridge_to_client')

    def __init__(self, enabled=False):
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
