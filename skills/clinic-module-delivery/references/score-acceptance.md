# Clinic score and scenario acceptance

User refinements: 22 October 2026. These apply to this clinic project.

## Test isolation and reproducibility

- Run applicable act/Actions jobs, including administrator scenario edits and registrar proposals. Tests are not patient runtime failures and must never create a real handoff/callback.
- Construct and inspect real MIS request shapes, but route all test writes to an isolated disposable sink/database, removed after the test. No external MIS mutations, including cleanup mutations. Fail closed if live write configuration leaks into a test.
- Generate an audible synthetic dialogue corpus once with different available TTS voices and rates spanning 0.75–2.5. Freeze each fixture's selection, audio, seed, expected text/trace/terminal state and hashes. Actions verifies/replays the existing corpus; it does not rerandomize or regenerate the reference audio.
- Replay actual audio through ASR and the actual FSM, import the recorded dialogue in the registrar client, and compare transcript/trace/target state. A wrong target fails, even if transcription or import alone succeeds. Bound job, subprocess, request, playback and conversation timeouts.
- CI uses the local small encoder for intent/missing fields and deterministic questions; no smart/reasoning external LLM, credentials or paid calls. Current E5 is not a fine-tuned USER-bge model. Claim fine-tuning only with weights, training provenance and independent evaluation.

## Runtime division of work

FSM owns state and guards. A small CPU encoder/parser plus operation schema extracts supplied/missing fields and forms the next question/request. LLM can resolve genuinely ambiguous names/ё or complex context; it cannot invent identity, facts, endpoints or write authorization. Preserve original spelling and evidence, and clarify ambiguity instead of globally replacing ё/е.

Only actual runtime MIS transaction failures trigger «Что-то пошло не так, сейчас переключу на оператора». Missing fields, normal refusals and CI errors are different results. If no operator is available, an optional production reasoning model may inspect a bounded dialogue/error/request snapshot; never blindly retry an uncertain committed write. Explain that AI is investigating while the operator is absent, then report actual availability without a false success.

If callback is needed, offer only 10:00–16:00 within the registrar's working calendar; 08:00–10:00 is excluded. If the patient declines a call, offer a persisted booking request without callback, with SMS only after a confirmed later booking. A request is not a booking. Before another appointment of the same specialty, inspect the patient's nearest not-yet-past appointment and ask explicitly whether to retain, cancel or change it; preserve it on refusal/error.

## Voice and registrar UI

Use silero-stress for Russian context and a validated prerecorded doctor full-name bank, not just a generic vocabulary of specialties. Cache fingerprints include directory revision, model, voice, normalizer and accentor.

Show the actual MIS response to the right of the call, with copy controls for every cell, including empty/NULL/structured values. Clearly identify sandbox versus external MIS, source and verification/result status. Registrar proposals remain provisional and line-scoped; admin edits, proposal validation, night activation boundaries and rollback have separate acceptance checks.
