# Recovery integration acceptance

- Combined current speech/recovery:83 host Python tests passed;95 offline Docker tests with actual E5 passed.
-21 actual compiled FSM tests passed; all28 Rust lib tests passed in act, including20 correlated sessions/queue deadline. clippy passed.
- Actual act control:77 evidence,36 speech/identity,62 recovery/CRM/hours,27 editor/ACL tests passed.
- Added independent regression cases for changed visit fingerprint, closed/short registrar calendar and acceptance-model guard. Fixed three DSL entries bypassing identity_start.
- Host callback audio runner failed before scenario execution because sandbox forbids binding HTTP listener. Not counted as accepted audio. Separate isolated Docker audio load is next.
- Production reasoning provider integration remains missing. A timeout-bounded hook is not a configured reasoning service.
- Remote score acceptance25e6da8 successful; DOCX module contracts failed. Other workflows pending at check.
