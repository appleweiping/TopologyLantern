# Clean-room circuit corpus

These netlists are synthetic fixtures written for TopologyLantern. `ota.sp`
and `blocks.sp` exercise a valid hierarchy, `current_mirror.sp` and
`diff_pair.sp` isolate inferred motifs, and `device_families.sp` covers every
accepted primitive family. Files under `adversarial/` are intentionally
invalid and should fail with the documented typed error; they are never sent
to a simulator.

An included file is expanded only on its first resolved-path occurrence. This
deliberate include-once rule prevents duplicate definitions and keeps resource
accounting deterministic; it is not general SPICE preprocessor behavior.
