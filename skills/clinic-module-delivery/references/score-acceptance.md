# Clinic score and scenario acceptance

User refinements: 22 October 2026. These apply to this clinic project.

## Test isolation and reproducibility

- Run applicable act/Actions jobs, including administrator scenario edits and registrar proposals. Tests are not patient runtime failures and must never create a real handoff/callback.
- Construct and inspect real MIS request shapes, but route all test writes to an isolated disposable sink/database, removed after the test. No external MIS mutations, including cleanup mutations. Fail closed if live write configuration leaks into a test.
- Generate an audible synthetic dialogue corpus once with different available TTS voices and rates spanning 0.75–2.5. Freeze each fixture's selection, audio, seed, expected text/trace/terminal state and hashes. Actions verifies/replays the existing corpus; it does not rerandomize or regenerate the reference audio.
- Replay actual audio through ASR and the actual FSM, import the recorded dialogue in the registrar client, and compare transcript/trace/target state. A wrong target fails, even if transcription or import alone succeeds. Bound job, subprocess, request, playback and conversation timeouts.
- CI uses the local small encoder for intent/missing fields and deterministic questions; no smart/reasoning external LLM, credentials or paid calls. Current E5 is not a fine-tuned USER-bge model. Claim fine-tuning only with weights, training provenance and independent evaluation.
- Accept twenty simultaneously active calls, with every response strictly below sixty seconds measured from the end of meaningful user speech, including module queues and audible response preparation. Exclude the user's speaking duration; distinguish noise/silence from real utterances and record endpoint/VAD errors. Check the maximum per turn, call correlation and target state. A queue-only test or twenty sequential HTTP requests does not prove the full audio profile; keep both reports distinct.

## Runtime division of work

For complex spelling, consult phone-owner FIO as a hint first; never fill unsaid fields or verify identity from caller ID. Invoke the small dialogue LLM for letter-by-letter/ambiguous dictation, validate whole source evidence and reconstructed letters, and keep ordinary missing-field questions deterministic. Unresolved time preferences and unparsed replies to a slot offer must reach the time_clarification flow before any MIS slot query. Validate and explicitly confirm the model's interval; invented personal work/lunch boundaries require a question. See scenarios/docs/LLM_CLARIFICATION.md for the actual local API evidence and missing external-key check. A mocked compatible-API contract is not a live external-provider test.

FSM owns state and guards. A small CPU encoder/parser plus operation schema extracts supplied/missing fields and forms the next question/request. LLM can resolve genuinely ambiguous names/ё or complex context; it cannot invent identity, facts, endpoints or write authorization. Preserve original spelling and evidence, and clarify ambiguity instead of globally replacing ё/е.

Use scenarios/ as the canonical pack: executable flows, documentation, source regulation, MIS schema and instructions for staff/AI. Read its knowledge endpoints without depending on CRM or models. The supplied MIS has no referrals/validity endpoints; those belong only to an explicitly configured optional local clinic registry. Do not infer referral status from missing MIS fields.

Only actual runtime MIS transaction failures trigger «Что-то пошло не так, сейчас переключу на оператора». Missing fields, normal refusals and CI errors are different results. If no operator is available, an optional production reasoning model may inspect a bounded dialogue/error/request snapshot; never blindly retry an uncertain committed write. Explain that AI is investigating while the operator is absent, then report actual availability without a false success.

If callback is needed, offer only 10:00–16:00 within the registrar's working calendar; 08:00–10:00 is excluded. If the patient declines a call, offer a persisted booking request without callback, with SMS only after a confirmed later booking. A request is not a booking. Before another appointment of the same specialty, inspect the patient's nearest not-yet-past appointment and ask explicitly whether to retain, cancel or change it; preserve it on refusal/error.

## Voice and registrar UI

Use silero-stress for Russian context and a validated prerecorded doctor full-name bank, not just a generic vocabulary of specialties. Cache fingerprints include directory revision, model, voice, normalizer and accentor.

Show the actual MIS response to the right of the call, with copy controls for every cell, including empty/NULL/structured values. Clearly identify sandbox versus external MIS, source and verification/result status. Registrar proposals remain provisional and line-scoped; admin edits, proposal validation, night activation boundaries and rollback have separate acceptance checks.

Latest UI source: adjust-web-page-styles (1).zip; use its docking/layout design for staff and observers, preserving the existing administrative editor. Do not import the prototype's mock auth, fake patient data, browser speech synthesis or client-side MIS database into the application. Run at most one delegated implementation task at a time until the user changes this instruction; prefer finishing and merging the highest-priority module. Commit and push reviewed parts frequently, with explicit WIP labels for requested backup checkpoints.

## Accepted integration and next evidence

Speech/recovery branches integrated on feature/score-acceptance (25e6da8,6b5a7ca). Use scenarios/docs/SCORE_PLAN.md for the current module matrix; scenarios/docs/CONCURRENT_CALLS.md describes the real20-call workflow and its limits. Local act actually replayed speech,DTMF,mixed through isolated CUDA ASR/CPU context-stress TTS and checked archives/targets. This short operator-to-callback path does not prove full appointments, live MIS throughput or endpoint/noise handling. Preserve the first failed false-emergency report; do not regenerate caller WAVs to pass it. Registrar UI33d41a8 is merged; Chromium and visual-editor act passed. Current follow-up evidence and limits are in scenarios/docs/LLM_CLARIFICATION.md.

## Prototype refactor and acceptance correction —25 October 2026

Use the supplied UI as component source, preserving its theme engine and interaction design, rather than only approximating its appearance. Keep presentation/layout, API transport and configured MIS/SQL/model adapters separate. Default reference services and source exports belong in the repository; frontend components must not embed provider addresses, keys or source file paths. See scenarios/docs/UI_BACKEND_ADAPTERS.md for actual scope, including the separate production CRM configuration.

Browser tests with intercepted HTTP prove presentation and race handling only. Require debug/run_text_call_ci.mjs for actual browser→E5→FSM→SQLite acceptance, including lowercase FIO before purpose, missing fields, explicit readback, actual SQL and a correct no-match response. Its TTS-disabled execution is not speech acceptance. Use the fixed audio corpus for separate speech requirements; never infer an appropriate answer from TTS/ASR text similarity. Verify nested docking, floating, persistence and preservation of live component state independently. A model event must distinguish an actual response, failed request, missing configuration and CI-disabled call; do not present enabled settings as proof of inference.

Keep local LLM endpoint local_base_url separate from compatible-provider base_url. Switching to a local provider must not reuse a previously saved external URL; retain the both-adapter temporal transport contract as regression coverage.
