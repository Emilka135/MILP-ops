"""Persistent, content-addressed recordings of static scenario phrases; no patient data."""

import hashlib
import io
import array
import json
import os
from pathlib import Path
import threading
import wave

from calls.scenario.dsl import load_flows
from calls.tts.silero import SAMPLE_RATE


class PromptBank:
    def __init__(self, folder, synth, module):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.synth, self.module = synth, module
        # Invalidate recordings when the actual model bytes change, including v5.4/v5.5.
        with Path(synth.model_path).open('rb') as model:
            self.model_sha256 = hashlib.file_digest(model, 'sha256').hexdigest()
        self.voice = f"{synth.name}:{synth.speaker}:{self.model_sha256}:{synth.pipeline_version}"
        self.lock = threading.RLock()
        path = self.folder / "manifest.json"
        self.manifest = json.loads(path.read_text()) if path.exists() else {}
        self.hits = 0
        self.generated = 0

    def key(self, text, rate):
        return hashlib.sha256(
            json.dumps([self.voice, float(rate), text], ensure_ascii=False).encode()
        ).hexdigest()

    def warm(self, scenarios):
        current_keys=set()
        for flow in load_flows(scenarios).values():
            for state in flow.states.values():
                for s in state.statements:
                    if s.kind == "say" and "{" not in s.text:
                        rate = float(s.args.get("rate", flow.rate))
                        current_keys.add(self.record(s.text, rate, f"{flow.name}.{state.name}"))

        with self.lock:
            active=self.folder/'current.tmp'
            active.write_text(json.dumps({'voice':self.voice,'model_sha256':self.model_sha256,'pipeline_version':self.synth.pipeline_version,'keys':sorted(current_keys)}))
            os.replace(active,self.folder/'current.json')

    def record(self, text, rate, source):
        key = self.key(text, rate)
        path = self.folder / (key + ".wav")
        with self.lock:
            if key in self.manifest and path.exists():
                return key
            pcm, ms = self.module.render(text, rate)
            tmp = path.with_suffix(".wav.tmp")
            with wave.open(str(tmp), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm)
            os.replace(tmp, path)
            self.manifest[key] = {
                "text": text,
                "spoken_text": __import__("calls.tts.normalize", fromlist=["normalize_text"]).normalize_text(text),
                "pipeline_version": self.synth.pipeline_version,
                "rate": rate,
                "voice": self.voice,
                "model_sha256": self.model_sha256,
                "file": path.name,
                "source": source,
                "duration_ms": ms,
            }
            tmp = self.folder / "manifest.tmp"
            tmp.write_text(
                json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n"
            )
            os.replace(tmp, self.folder / "manifest.json")
            self.generated += 1
        return key

    def replace_recording(self, key, raw):
        with self.lock:
            if key not in self.manifest or self.manifest[key].get('voice') != self.voice:
                raise ValueError('Unknown recording for this model')
            with wave.open(io.BytesIO(raw), 'rb') as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
                    raise ValueError('Required WAV: mono, 16-bit, 8000 Hz')
                pcm = wav.readframes(wav.getnframes())
            samples = array.array('h', pcm)
            if len(samples) < 800 or len(samples) > 8000*180 or max(abs(x) for x in samples) < 100:
                raise ValueError('Recording must contain audible speech, 0.1–180 seconds')
            # Preserve previous recording for a deliberate rollback; upload never changes scenario text.
            path = self.folder / (key + '.wav')
            backup = self.folder / 'history'
            backup.mkdir(exist_ok=True)
            if path.exists():
                import time
                (backup / (key + '-' + str(time.time_ns()) + '.wav')).write_bytes(path.read_bytes())
            temp = path.with_suffix('.wav.tmp')
            with wave.open(str(temp), 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(SAMPLE_RATE); wav.writeframes(pcm)
            os.replace(temp, path)
            self.manifest[key].update(recording_type='admin_upload', duration_ms=len(pcm)*1000//16000)
            temp = self.folder / 'manifest.tmp'
            temp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2)+'\n')
            os.replace(temp, self.folder / 'manifest.json')
            return {'key': key, 'duration_ms': self.manifest[key]['duration_ms'], 'recording_type': 'admin_upload'}

    def render(self, text, rate):
        key = self.key(text, rate)
        with self.lock:
            entry = self.manifest.get(key)
            if entry:
                with wave.open(str(self.folder / entry["file"]), "rb") as wav:
                    if (
                        wav.getnchannels() != 1
                        or wav.getsampwidth() != 2
                        or wav.getframerate() != SAMPLE_RATE
                    ):
                        raise ValueError("Invalid recorded prompt format")
                    pcm = wav.readframes(wav.getnframes())
                self.hits += 1
                return "ok", {
                    "pcm": pcm,
                    "bytes": len(pcm),
                    "sample_rate": SAMPLE_RATE,
                    "format": "pcm16",
                    "duration_ms": len(pcm) * 1000 // 16000,
                    "engine": entry.get("recording_type", self.synth.name),
                    "rate": rate,
                    "audio_source": "prerecorded",
                    "audio_key": key,
                    "audio_file": entry["file"],
                    "scenario_source": entry["source"],
                }
        status, result = self.module.handle("synth", {"text": text, "rate": rate})
        result["audio_source"] = "dynamic_tts"
        result["audio_key"] = None
        return status, result

    def audit(self, scenarios, transcribe):
        """Every current static WAV is decoded and recognized. Never trust stale cache metadata."""
        from calls.tts.normalize import normalize_text
        from calls.tts.quality import compare, QUALITY_POLICY_VERSION
        import numpy as np
        self.warm(scenarios)
        report={'status':'running','quality_policy_version':QUALITY_POLICY_VERSION,'model_sha256':self.model_sha256,'voice':self.voice,'phrases':[],
                'method':'real_asr_and_signal_checks','naturalness':'requires_listening'}
        keys=set()
        for flow in load_flows(scenarios).values():
            for state in flow.states.values():
                for s in state.statements:
                    if s.kind=='say' and '{' not in s.text:keys.add(self.key(s.text,float(s.args.get('rate',flow.rate))))
        for key in sorted(keys):
            entry=self.manifest[key];item={'key':key,'text':entry['text'],'rate':entry['rate']}
            try:
                raw=(self.folder/entry['file']).read_bytes()
                with wave.open(io.BytesIO(raw),'rb') as wav:
                    if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate())!=(1,2,SAMPLE_RATE):raise ValueError('Неверный формат WAV')
                    pcm=wav.readframes(wav.getnframes())
                samples=np.frombuffer(pcm,dtype='<i2').astype(float)
                if len(samples)<800 or len(samples)>SAMPLE_RATE*180:raise ValueError('Неверная длительность')
                rms=float(np.sqrt(np.mean(samples*samples)));clipped=float(np.mean(np.abs(samples)>=32760))
                actual=transcribe(pcm);assessment=compare(entry['text'],actual)
                item.update(assessment,actual_asr=actual,spoken_text=normalize_text(entry['text']),rms=round(rms,2),clipped_fraction=clipped,file_sha256=hashlib.sha256(raw).hexdigest(),status='passed' if assessment['passed'] and rms>100 and clipped<.001 else 'needs_review')
            except (ValueError,OSError,KeyError) as e:item.update(status='failed',error=str(e))
            report['phrases'].append(item)
        report.update(status='passed' if report['phrases'] and all(p['status']=='passed' for p in report['phrases']) else 'needs_review',count=len(keys))
        tmp=self.folder/('audit-'+__import__('uuid').uuid4().hex+'.tmp');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,self.folder/'audit.json')
        return report
