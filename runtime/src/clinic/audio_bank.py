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
        doctors_path = self.folder / 'doctors.json'
        self.doctors = json.loads(doctors_path.read_text()) if doctors_path.exists() else {}

    def refresh_doctors(self, doctors, rate=1.5):
        """Publish a complete active directory only after every name is recorded.

        Called after a successful read/import of the MIS directory. Repeated
        snapshots reuse WAVs; rename, removal, model or pronunciation changes
        invalidate the active index without deleting rollback recordings.
        """
        if not .5 <= rate <= 2.5:
            raise ValueError('Speech rate must be 0.5–2.5')
        if not isinstance(doctors, list) or len(doctors) > 5000:
            raise ValueError('Expected complete doctor directory (max 5000)')
        names = {}
        for item in doctors:
            if not isinstance(item, dict):
                raise ValueError('Invalid doctor')
            if not item.get('is_active', item.get('active', True)):
                continue
            name = ' '.join(str(item.get('full_name', '')).split())
            if not 2 <= len(name.split()) <= 5 or len(name) > 180 or any(c in name for c in '{}<>\n'):
                raise ValueError('Invalid doctor full name')
            names[str(item['id'])] = name
        with self.lock:
            entries = {did: {'full_name': name, 'key': self.record(name, rate, 'doctor:'+did)}
                       for did, name in sorted(names.items())}
            snapshot = {'voice': self.voice, 'rate': float(rate), 'entries': entries}
            snapshot['directory_sha256'] = hashlib.sha256(json.dumps(names, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            temp = self.folder / 'doctors.tmp'
            temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2)+'\n')
            os.replace(temp, self.folder / 'doctors.json')
            self.doctors = snapshot
        return {'doctors': len(entries), 'directory_sha256': snapshot['directory_sha256'], 'generated': self.generated}

    def _doctor_audio(self, text, rate):
        """Compose dynamic prompt around a prerecorded full name, without guessing cases."""
        if self.doctors.get('voice') != self.voice or self.doctors.get('rate') != rate:
            return None
        entries = sorted(self.doctors.get('entries', {}).values(), key=lambda e: -len(e['full_name']))
        for item in entries:
            name = item['full_name']
            position = text.casefold().find(name.casefold())
            if position < 0:
                continue
            before, after = text[:position], text[position+len(name):]
            # Match whole full names only; no slicing a longer surname.
            if (before and before[-1].isalpha()) or (after and after[0].isalpha()):
                continue
            chunks = []
            for part in (before, name, after):
                if part.strip():
                    status, result = self.render(part.strip(), rate, _compose=False)
                    if status != 'ok':
                        return status, result
                    chunks.append(result['pcm'])
            pcm = (b'\x00' * 800).join(chunks)
            return 'ok', {'pcm': pcm, 'bytes': len(pcm), 'sample_rate': SAMPLE_RATE,
                          'format': 'pcm16', 'duration_ms': len(pcm)*1000//16000,
                          'engine': self.synth.name, 'rate': rate, 'audio_source': 'doctor_bank_composite',
                          'doctor_audio_key': item['key'], 'directory_sha256': self.doctors['directory_sha256']}
        return None

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

    def render(self, text, rate, _compose=True):
        key = self.key(text, rate)
        with self.lock:
            entry = self.manifest.get(key)
            if entry and str(entry.get('source', '')).startswith('doctor:'):
                active = {item['key'] for item in self.doctors.get('entries', {}).values()}
                if self.doctors.get('voice') != self.voice or key not in active:
                    entry = None
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
        if _compose:
            composed = self._doctor_audio(text, rate)
            if composed is not None:
                return composed
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
