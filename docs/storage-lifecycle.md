# Engine storage lifecycle

The Engine separates durable exhibition evidence from working computation. Run a read-only measurement with:

```bash
"$EXHIBITION_ARCHIVE_ENGINE" storage-report WORKSPACE
```

The report measures regular-file bytes and counts symlinks without following their targets. It separates original image payload, prepared derivatives, backend attempts, caches, workflow audit files, final archive/website deliverables, materialized training datasets, checkpoint candidates, and artifacts referenced by validation or review records. Subset categories identify their overlap and are excluded from the additive unique-byte total. A review-referenced path is protected for preservation; this report does not revalidate the decision’s freshness or scope.

## Retention classes

| Class | Examples | Handling |
| --- | --- | --- |
| Permanent / protected | Original images; source and conversion provenance; accepted reconstruction geometry and cameras; accepted final PLY/model; audit and review records | Keep. The Engine never removes these automatically. |
| Working / retained as needed | Current training checkpoint; active training dataset; current diagnostics and evaluation artifacts | Keep while in use or needed for recovery and review. Training image and COLMAP inputs use symlinks where possible. |
| Regenerable / cleanup candidates | Obsolete intermediate conversions; superseded checkpoints; temporary decoded PNGs; redundant caches; expired or duplicate derivative workspaces | Review references, active processes, plans, and preservation needs first. `storage-report` only lists candidates; it does not delete anything. |

Latest-only checkpoint pruning is owned by Spirula when `--save-only-latest-checkpoint 1` is configured. The Engine preserves backend attempts and never removes the newest recovery checkpoint. If a checkpoint file appears older or redundant, do not remove it while its attempt is active or before the backend’s current recovery point is known.

The archive package is built from an explicit allowlist of current work reports, selected model metadata, navigation evidence, and preparation provenance. It does not copy backend attempt directories, training checkpoints, training datasets, caches, or the complete derived-image set. The archive manifest refers to the original source folder and source index; it is not a self-contained Preservation Package.

Compare the final archive and website deliverables with the measured original photograph payload. Roughly 10× the source image size is an initial optimization target for review, not a hard deletion limit. A larger package requires an explanation and storage review; it never authorizes deleting originals, accepted artifacts, or audit evidence.

## Fresh training disk controls

Every fresh training configuration must provide a maximum iteration count, timeout, positive `minimum_free_disk_bytes_training`, and positive `checkpoint_replacement_reserve_bytes`. Record whether the reserve is `OPERATOR_PROVISIONED` or `MEASURED`; a prior checkpoint measurement is optional. At launch, the Engine requires the runtime floor plus the replacement reserve to account for temporary coexistence of old and new full checkpoints. The reserve is an operator-selected safety budget, not a size forecast. The Engine then monitors free space on the output filesystem and asks Spirula to stop when the runtime floor is crossed. It does not delete checkpoints. After a disk-triggered stop, the attempt record reports whether a new or changed checkpoint candidate was observed; detection uses path, size, and modification time only, so checkpoint validity and resumability remain unverified. A mid-write termination can leave a checkpoint unusable, especially if SIGKILL is required; retain the attempt and inspect it before any retry or resume.
