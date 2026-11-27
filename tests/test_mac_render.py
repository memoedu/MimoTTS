import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from mimo_tts import mac_render
from mimo_tts.client import MimoError


class MacRenderTests(unittest.TestCase):
    settings = {'host': 'fixture', 'python': '/bin/python3', 'source': '/source', 'ffmpeg_directory': '/ffmpeg'}

    def test_mac_result_is_bound_to_requested_text_and_audio(self):
        text = 'A public fixture.'
        audio = b'verified audio fixture'

        def worker(args, *, input, **kwargs):
            self.assertNotIn('ek_fixture', ' '.join(args))
            request = json.loads(input)
            self.assertEqual(request['client_secret'], 'ek_fixture')
            self.assertEqual(request['request']['text'], text)
            result = dict(bytes=len(audio), sha256=mac_render.digest(audio), text_sha256=mac_render.digest(text.encode()),
                          provider='openai', model=mac_render.MODEL, voice='marin', speed=1)
            return subprocess.CompletedProcess(args, 0, stdout=mac_render.wire({
                'request_sha256': mac_render.digest(mac_render.wire(request['request'])),
                'result': result, 'audio': base64.b64encode(audio).decode(),
            }))

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'output.mp3'
            with (patch.object(mac_render, 'client_secret', return_value='ek_fixture'),
                  patch.object(mac_render.subprocess, 'run', side_effect=worker)):
                saved = mac_render.render(text, destination, voice='marin', speed=1, style='', settings=self.settings)
            self.assertEqual(destination.read_bytes(), audio)
            self.assertEqual(saved['mac_render']['host'], 'fixture')

    def test_interrupted_audio_is_neither_published_nor_reissued(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'output.wav'
            with (patch.object(mac_render, 'client_secret', return_value='ek_fixture') as issue,
                  patch.object(mac_render.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 1)) as run):
                with self.assertRaisesRegex(MimoError, 'not reissued'):
                    mac_render.render('Fixture.', destination, voice='marin', speed=1, style='', settings=self.settings)
            self.assertFalse(destination.exists())
            issue.assert_called_once()
            run.assert_called_once()
