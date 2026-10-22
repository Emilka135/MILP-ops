"""Actual organizer gateway call using phone account, leaving vm registration intact.

Uses the organizer's installed SIP transaction implementation, not a fake call.
Only an informational request is sent; no appointments are changed.
"""
import argparse
import audioop
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import urllib.request
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime/src')]
from calls.dtmf.synth import dtmf_case


def save(path, pcm):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        output.writeframes(pcm)


def main(mode):
    spec = importlib.util.spec_from_file_location('organizer_sip', '/organizer/check.py')
    sip = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sip)
    env = sip.load_env('/organizer-env/sip.env')
    env['SIP_VM_USER'] = env['SIP_PHONE_USER']
    env['SIP_VM_PASSWORD'] = env['SIP_PHONE_PASSWORD']
    folder = ROOT / 'debug/runs/vm-deployment/sip' / mode
    folder.mkdir(parents=True, exist_ok=True)
    pcm = bytearray(8000 * 2 * 30)
    if mode == 'speech':
        request = urllib.request.Request('http://127.0.0.1:8080/v1/speech.synth',
            data=json.dumps({'text':'Как работает поликлиника?', 'rate':1.3}).encode(),
            headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['API_TOKEN']})
        clip = json.load(urllib.request.urlopen(request, timeout=30))['payload']
        utterance = base64.b64decode(clip['pcm_b64'])
        pcm[9*16000:9*16000+len(utterance)] = utterance
    else:
        for second, digit in ((9,'3'),(13,'1'),(26,'0')):
            samples, _, _ = dtmf_case(digit, 150, codec='lin', trail_ms=0)
            utterance = samples.tobytes()
            pcm[second*16000:second*16000+len(utterance)] = utterance
    save(folder/'caller.wav', pcm)

    def frames(payload, seconds):
        convert = audioop.lin2alaw if payload == 8 else audioop.lin2ulaw
        return [convert(bytes(pcm[offset:offset+320]),2) for offset in range(0,len(pcm),320)]

    sip.tone_frames = frames
    agent = sip.Agent(env, 15060, 15062)
    received = bytearray()

    class Tap:
        def __init__(self, original): self.original = original
        def __getattr__(self, name): return getattr(self.original, name)
        def fileno(self): return self.original.fileno()
        def recvfrom(self, size):
            packet, source = self.original.recvfrom(size)
            if len(packet)>12:
                payload=packet[1]&127
                if payload in (0,8):
                    received.extend((audioop.alaw2lin if payload==8 else audioop.ulaw2lin)(packet[12:],2))
            return packet, source

    agent.rtp = Tap(agent.rtp)
    registration = agent.register()
    if registration != 200: raise RuntimeError('Phone registration failed: '+str(registration))
    try:
        result = agent.call(env['SIP_NUMBER'], tone_seconds=30)
    finally:
        agent.register(expires=0)
        agent.sip.close()
        agent.rtp.close()
    save(folder/'assistant.wav', received)
    result.update({'mode':mode,'registration':registration,'received_pcm_bytes':len(received),
                   'scope':'actual phone account -> organizer SIP gateway -> VM Asterisk -> ARI/AudioSocket',
                   'passed':result['status']==200 and result['rtp_in']>100 and audioop.rms(received,2)>100})
    (folder/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
    if not result['passed']: raise SystemExit(1)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['speech','tones'],required=True)
    main(parser.parse_args().mode)
