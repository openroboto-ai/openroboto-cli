> # ⚠️ Archived — the content has been merged into `../control_json.md`
>
> **Status**: superseded · **Archived on**: 2026-08-19
> **Superseded by**: `docs/control_json.md` (contract description + field table +
> sample, all in one)
>
> **Why**: the single topic of control.json used to be spread across three files —
> the contract description (`control_json.md`), the sample walkthrough (this file),
> and the sample itself (`control_json_example.json`). This file is only 15 lines
> long in total, and all it says is "the sample is in that json next door".

---

# control.json Sample

The canonical placeholder sample is `control_json_example.json`.

It contains only miner-visible and validator-visible fields:

- round number, status, and public message;
- evaluation-fee parameters;
- public training and validation resource URLs;
- public model and training parameters;
- an optional public read credential.

The sample intentionally omits scoring-service settings, held-out task data, emission controls, owner process controls, internal URLs, wallet identifiers, and write credentials.

Clients should treat the fetched document as untrusted input: require expected types, reject invalid fee or round values, use HTTPS for remote resources, and ignore unknown fields.
