# Real concurrent call acceptance,22 October2026

Source: integrated speech25e6da8 and recovery6b5a7ca plus the safety-stem correction and runner in this commit. Actual act job used the isolated Docker stack, real CUDA GigaAM and CPU Silero with context silero-stress.

Saved act reports: speech20calls/40turns,max1.29697s;dtmf20/40,max0.18143s;mixed20/40,max1.26938s. Every independent target callback.ask_consent, compiled revision, registrar/call correlation and transcript assertion passed. ASR probe reports actual CUDA kernels verified. No booking/operation/SMS changes. Each measurement begins after full finished user PCM and includes reply PCM preparation, not user speech or real-time playback.

Initial false-emergency failure retained under failures/false-emergency-report.json. Fixed the specific перекос stem collision with переключите;6new cases and58legacy unittest pass. Updated actual act control:77+36+68+27pytest plus58unittest passed.

One unmodified runtime archive from this act run imported into actual registrar HTTP/SQLite and checked by Chromium:7checks passed, exact transcript/trace/final recorded target,admin save/restore and scoped queued proposal. Screenshot inspected; this is the existing accepted root UI, not the new33d41a8 design.

Limits: this is a short two-turn escalation path, not full booking, end-to-end SIP/VAD/noise,20simultaneous external writes or all frozen-case target replays. Hosted CPU preparation path in the new workflow is pending remote execution. DOCX coverage remains open.
