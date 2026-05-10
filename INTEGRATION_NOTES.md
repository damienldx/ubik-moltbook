# ubik-moltbook — Albert's integration

> Branch: `albert/integration`
> Author: b5aeb927-agent-1 (Albert)
> Distinctif: **Egress firewall**, not just an API client.

## What's in this PR

Three layers, all in `src/`:

| File | Purpose | LOC |
|---|---|---|
| `moltbook_egress_firewall.py` | Content firewall: regex bank, sanitize / block verdicts | ~180 |
| `moltbook_client.py` | Thin HTTP client (stdlib only) + per-agent API key store | ~180 |
| `moltbook_meca.py` | Optional systemd daemon mirroring `[public]` relay messages | ~210 |
| `moltbook_register.py` | One-shot registration script | ~40 |
| `tests/test_egress_firewall.py` | 17 golden tests covering allow/sanitize/block + strict mode | ~115 |

Total: ~725 LOC, no external dependencies (`urllib`, `json`, `signal` only).

## Why a firewall, not just opt-in

The brief says "posts opt-in uniquement". I added a second layer because **opt-in alone is not enough**:

- An agent who copy-pastes `[public]` then types "Today I edited `/home/damienldx/.ubik-memory/journal/2026-05-11.md`" has technically opted-in but is now leaking a private filename. No `[public]` discipline rescues that.
- Tokens get pasted in by accident. Once the relay forwards the message, it's external. Audit-after is too late.

The firewall sits **between** the relay and `client.post()` and is non-bypassable from the public surface. It emits one of three verdicts:

| Verdict | Behavior |
|---|---|
| `ALLOW` | publish text as-is |
| `SANITIZE` | publish with risky spans masked (paths → `<…>/tail`, emails → `[REDACTED:email]`) |
| `BLOCK` | refuse to publish, append a structured entry to the audit log |

### Pattern bank

Four bands, ordered by priority:

1. **Secrets** (block on match): Anthropic keys, OpenAI keys, GitHub PATs, AWS access keys, SSH private keys, JWTs. Plus high-entropy hex blobs (sanitize-only).
2. **Internals** (mostly block): handoff/journal/identity filenames, "ma tension portée", `bridge:*`, `*-meca`, fleet UUIDs (`b5aeb927-agent-1`), PRISMA endpoints, LBA rep codes.
3. **Paths** (sanitize): `/home/<user>/...`, `/Users/...`, `~/.ubik-memory/...`, `~/.ubik-desktop/...`, `~/.claude/...`.
4. **PII** (sanitize): emails, French phone numbers.

### Strict mode

Setting `MOLTBOOK_FW_LEVEL=strict` upgrades every `SANITIZE` to `BLOCK`. Use this when you want pristine pass-through only — typically on the first 24h of a new agent's profile, before you trust its writing habits.

## Wiring

### One-shot register

```bash
python -m src.moltbook_register b5aeb927-agent-1 "Albert — Reviewer Backend"
# writes ~/.ubik-memory/moltbook/b5aeb927-agent-1.key (mode 0600)
```

### Run the méca (systemd)

```ini
[Unit]
Description=ubik moltbook méca for b5aeb927-agent-1
After=network.target

[Service]
Environment=MOLTBOOK_AGENT_ID=b5aeb927-agent-1
Environment=MOLTBOOK_RELAY_URL=http://localhost:7894
Environment=MOLTBOOK_FW_LEVEL=normal
ExecStart=/usr/bin/python3 -m src.moltbook_meca
WorkingDirectory=/home/damienldx/workspace/ubik-moltbook
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
```

### Audit log

Every publication attempt (success or block) appends a JSONL line to `~/.ubik-memory/moltbook/egress.log`:

```jsonl
{"ts":"2026-05-11T01:14:32+00:00","event":"published","agent_id":"b5aeb927-agent-1","post_id":"42","sanitized":false,"text_published":"Today I shipped..."}
{"ts":"2026-05-11T01:14:55+00:00","event":"blocked","agent_id":"b5aeb927-agent-1","from":"operator","reasons":["matched_block:rep_code_literal"],"matched":["rep_code_literal"],"preview":"DIRIL ANDRE has 14 visits this quarter"}
```

This makes post-incident review trivial: grep `event=blocked` to see what the firewall caught, grep `event=published` to see what actually went out.

## Tests

```bash
python3 -m unittest tests.test_egress_firewall -v
# Ran 17 tests in 0.002s — OK
```

17 golden cases cover: clean allow, secrets block, internals block, paths sanitize, emails sanitize, fleet UUID sanitize, PRISMA endpoint sanitize, strict mode promotion, priority (secret beats sanitize).

## What I deliberately did *not* do

- **No SDK abstraction** (zero external deps). `urllib.request` is enough.
- **No auto-mirror.** Opt-in via `[public]` prefix is the only entry path. No "smart inference" of what could be published.
- **No retry on POST failure.** Failed posts hit the audit log; humans investigate. Retrying blind could publish stale messages.
- **No multi-agent shared key.** Each `~/.ubik-memory/moltbook/<agent_id>.key` is mode 0600 and per-slot. Lateral compromise is contained.
- **No Bearer auth on the heartbeat path** if Moltbook's API uses it differently. I kept the default `Authorization: Bearer <key>`; if the real spec uses an `X-Moltbook-Token`, swap one line in `MoltbookClient._request`.

## Comparison with the brief

| Brief requirement | This PR |
|---|---|
| Client Python or TypeScript | Python, stdlib only |
| Posts opt-in (`[public]` marker) | Enforced in `_is_public()` + `_strip_marker()` |
| No leak of private data | Egress firewall (180 LOC) + 17 golden tests |
| API key per agent | `~/.ubik-memory/moltbook/<agent_id>.key`, 0600 perms |
| Heartbeat every 4h | `MoltbookMeca._tick_heartbeat` with state file |
| Compatible with stack existing | Méca pattern matches `console_meca.py`, `memory_meca.py`, `ledger_meca.py` |

## Open questions for the merge

1. Should the firewall's verdict be **echoed back to the original sender** (via relay `ack`) when blocked? Today we only audit-log. Echo would help authors learn the rules.
2. Should `[public:channel]` route to multiple Moltbook accounts (e.g. `[public:tech]` posts under a specialized agent)? The méca already strips that prefix variant; routing logic isn't wired.
3. `egress.log` rotation — today append-only. Add `logrotate` config in a follow-up or use a built-in size cap?

---

— Albert, 2026-05-11
