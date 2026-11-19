# Contributor identity — the key an authored task is paid to

> **Status**: current · **Updated**: 2026-10-29 · **Audience**: anyone submitting
> a **task** to a task-track season.
> **Scope**: Generating the contributor key, registering it, binding a hotkey,
> and what to do when the backend refuses one of those steps.

## Why this exists, and when it costs you money

An authored task's earnings follow a **persistent `contributor_id`**, not the
hotkey that submitted the package. Hotkeys are replaceable — rotated, sold,
deregistered — and a payout identity that disappears with the key takes an
author's history with it.

On the **task track**, admission resolves the author from the submitting
hotkey's active contributor binding at the moment the package arrives. If the
hotkey is not bound, the submission is rejected with
`ROUND_MISMATCH` / `contributor_unbound`.

> 🔴 **The entry fee is not refunded for that rejection.** Bind first.

`openroboto submit` checks the binding **before the fee** and refuses to pay
when the task track would refuse the submission. The simulation and real tracks
do not use a contributor identity, and their behaviour is unchanged.

## The three commands

```bash
openroboto identity keygen
openroboto identity register --reward-address <your hotkey ss58>
openroboto identity bind
```

**Earnings go to the bound hotkey.** Emissions reach a miner only through
validator weights, which are keyed by the hotkeys registered on the subnet, so
the task track pays the hotkey most recently bound to your identity. The reward
address is recorded with the identity but does not receive payouts. If no
hotkey is bound at settlement time, that day's share for your tasks is not
paid; bind again and the next settlement pays you.

`bind` defaults to `subnet.hotkey_ss58` in `miner.yaml` and signs with the
hotkey in the wallet that config names. It must be the same key: only that
hotkey's private key can produce the second signature.

### Where the key lives

| | |
|---|---|
| Default path | `~/.openroboto/contributor.key` |
| Permissions | `0600` (the directory is created `0700`) |
| Contains | the 32-byte Ed25519 seed, its public key, and the file version |
| Never | inside the workspace, in a repository, in a log, or in a chat window |

`openroboto identity keygen --key-file <path>` writes a second, separate
identity elsewhere. `--force` replaces an existing file and **abandons the
identity it belongs to**; that is why it is not the default.

If an existing registered identity uses another key file, select it explicitly
for both the identity check and submission:

```bash
openroboto identity show --config miner.yaml --key-file /path/to/author.key
openroboto submit --config miner.yaml --key-file /path/to/author.key
```

`--config` does not select a contributor key. Without `--key-file`, both commands
still use `~/.openroboto/contributor.key`. Repeat the selected `--key-file` when
retrying `submit` in the same workspace; the uploaded commit and payment checkpoint
are reused. There is no need to replace the default key or register another identity.

> ⚠️ **Back the file up.** If it is lost, that identity can no longer sign
> anything. Its earnings can only be moved through an operator-arbitrated
> migration (`POST /api/v1/admin/contributors/<id>/migrate`). There is no
> self-service recovery.

## What gets signed

Every mutation covers a canonical text the backend builds from `kind` and a
fixed field order. The CLI fetches that exact text from
`GET /api/v1/contributors/message` and signs those bytes; it never assembles a
second copy. Replaying one kind's signature as another therefore fails: the
text differs.

| Command | `kind` | Signed with | Body sent |
|---|---|---|---|
| `register` | `register` | contributor Ed25519 key | public key, reward address, signature |
| `bind` | `bind` | **both** the contributor key and the hotkey's sr25519 key | hotkey, `signature`, `hotkey_signature` |
| `unbind` | `unbind` | contributor key | signature |
| `migrate` | `migrate` | **old** contributor key | new public key, new reward address, signature |

The `contributor_id` itself is derived by the server from the public key:

```
contributor_id = sha256("openroboto:contributor:v1\0" || public_key)
```

A request body cannot choose it.

## Rotating a key

```bash
openroboto identity keygen --key-file ~/.openroboto/next.key
openroboto identity migrate --new-key-file ~/.openroboto/next.key \
    --reward-address <ss58>
```

`migrate` is signed by the **old** key, and the message names the new public
key *and* the new reward address — one signature authorises both. Filling in a
new address alone is not an action.

Historical tasks stay frozen against the old id; queries aggregate along the
`merged_into` chain. **Existing hotkey bindings are not moved by the
migration**: to submit under the new identity, unbind with the old key and bind
again with the new one.

## Errors and the next command

| Error code | What it means | What to type |
|---|---|---|
| `CONTRIBUTOR_NOT_FOUND` | the identity is not registered on this backend | `openroboto identity register --reward-address <ss58>` |
| `INVALID_CONTRIBUTOR_PROOF` | the signature did not verify | re-run the command: it re-fetches the text and re-signs. Do not retype or reorder the message |
| `HOTKEY_SIGNATURE_INVALID` | the sr25519 signature is missing or wrong | check that the hotkey in `miner.yaml` is the one you meant to bind, then `openroboto identity bind` |
| `CONTRIBUTOR_ALREADY_MIGRATED` | you are using a key that was rotated | use the key that received the migration (`openroboto identity show`) |
| `HOTKEY_ALREADY_BOUND` | another identity owns this hotkey | that identity signs `openroboto identity unbind --hotkey <ss58>` first |
| `HOTKEY_NOT_BOUND` | this identity does not hold the hotkey | `openroboto identity bind --hotkey <ss58>` |
| `REWARD_ADDRESS_MISMATCH` | the key is registered with another address | a signed `migrate` is the only way to change it |

## Checking the current state

```bash
openroboto identity show
```

Prints the local id and public key, the backend's view (status, reward
address, active bindings), and says whether the identity has been migrated.
