"""Use a Windows-issued ephemeral Realtime session for one Mac-rendered clip."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .client import MimoError, split_text
from .openai_audio import MODEL, synthesize
from .router import openai_key


MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 32 * 1024 * 1024


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def algorithm():
    root = Path(__file__).parent
    return digest(wire([(root / name).read_text(encoding='utf-8')
                        for name in ('mac_render.py', 'openai_audio.py', 'client.py')]))


def client_secret(voice):
    body = {'expires_after': {'anchor': 'created_at', 'seconds': 120},
            'session': {'type': 'realtime', 'model': MODEL, 'output_modalities': ['audio'],
                        'audio': {'output': {'voice': voice, 'format': {'type': 'audio/pcm', 'rate': 24000}}}}}
    request = urllib.request.Request('https://api.openai.com/v1/realtime/client_secrets', method='POST',
                    headers={'Authorization': 'Bearer ' + openai_key(), 'Content-Type': 'application/json'},
                    data=wire(body))
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            value = json.load(response)['value']
    except urllib.error.HTTPError as error:
        raise MimoError(f'OpenAI Realtime client-secret issuance returned HTTP {error.code}.') from None
    except (OSError, KeyError, ValueError, TypeError):
        raise MimoError('Could not obtain a short-lived OpenAI Realtime client secret.') from None
    if not isinstance(value, str) or not value.startswith('ek_'):
        raise MimoError('Expected an ephemeral OpenAI Realtime client secret.')
    return value


def render(text, destination, *, voice, speed, style, settings):
    if len(split_text(text)) != 1:
        raise MimoError('Mac rendering supports one SpeechKit segment per refresh. Clear the Mac option for longer text.')
    suffix = destination.suffix.lower()
    if suffix not in ('.wav', '.mp3') or voice not in ('marin', 'cedar') or not .5 <= speed <= 2.5:
        raise MimoError('Invalid Mac SpeechKit output, voice, or speed.')
    try:
        host = settings['host']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
            raise ValueError
        invoke = ('import runpy,sys; sys.path.insert(0,sys.argv[1]); '
                  'runpy.run_module("mimo_tts.mac_render",run_name="__main__")')
        worker = shlex.join([settings['python'], '-c', invoke, settings['source'], settings['ffmpeg_directory']])
    except (KeyError, TypeError, ValueError):
        raise MimoError('Invalid Mac SpeechKit host configuration.') from None
    request = dict(model=MODEL, text=text, voice=voice, speed=speed, style=style,
                   suffix=suffix, algorithm_sha256=algorithm())
    if len(wire(request)) > MAX_REQUEST - 2048:
        raise MimoError('Mac SpeechKit request is too large.')
    started = time.perf_counter()
    token = client_secret(voice)
    issued = time.perf_counter()
    args = [settings.get('ssh', 'ssh'), '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ClearAllForwardings=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', host, worker]
    try:
        process = subprocess.run(args, input=wire(dict(request=request, client_secret=token)),
                                 capture_output=True, check=True, timeout=240,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if len(process.stdout) > MAX_RESPONSE:
            raise ValueError
        response = json.loads(process.stdout)
        audio = base64.b64decode(response['audio'], validate=True)
        result = response['result']
        if (response['request_sha256'] != digest(wire(request)) or not audio
                or result['sha256'] != digest(audio) or result['bytes'] != len(audio)
                or result['text_sha256'] != digest(text.encode()) or result['speed'] != speed
                or result['provider'] != 'openai' or result['model'] != MODEL or result['voice'] != voice):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise MimoError('Mac SpeechKit audio was not verified. The request was not reissued.') from None
    with destination.open('xb') as stream:
        stream.write(audio)
    result['path'] = str(destination)
    result['mac_render'] = {'host': host, 'issuer_seconds': round(issued-started, 4),
                            'elapsed_seconds': round(time.perf_counter()-started, 4)}
    return result


def serve(ffmpeg_directory):
    data = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(data) > MAX_REQUEST:
        raise MimoError('Mac SpeechKit request is too large.')
    envelope = json.loads(data)
    request = envelope['request']
    token = envelope['client_secret']
    if (not token.startswith('ek_') or request['model'] != MODEL or request['algorithm_sha256'] != algorithm()
            or request['suffix'] not in ('.wav', '.mp3') or request['voice'] not in ('marin', 'cedar')
            or len(split_text(request['text'])) != 1):
        raise MimoError('Mac SpeechKit session or engine mismatch.')
    directory = Path(ffmpeg_directory)
    if not (directory / 'ffmpeg').is_file():
        raise MimoError('Configured Mac FFmpeg is unavailable.')
    os.environ['PATH'] = str(directory) + os.pathsep + os.environ.get('PATH', '')
    root = Path.home() / 'Agent/Caches/MimoTTS'
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as staged:
        target = Path(staged) / ('audio' + request['suffix'])
        result = synthesize(request['text'], target, voice=request['voice'], speed=request['speed'],
                            style=request['style'], cache=False, api_key=token)
        audio = base64.b64encode(target.read_bytes()).decode('ascii')
    sys.stdout.buffer.write(wire(dict(audio=audio, result=result, request_sha256=digest(wire(request)))))


if __name__ == '__main__':
    try:
        serve(sys.argv[2])
    except Exception:
        # Provider exceptions can contain request headers. The transport returns only this safe status.
        print('Mac SpeechKit worker did not produce verified audio.', file=sys.stderr)
        raise SystemExit(2)
