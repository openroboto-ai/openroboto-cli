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
