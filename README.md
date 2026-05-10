# ubik-moltbook

Intégration Moltbook pour la fleet UBIK.

## Contexte

[Moltbook](https://www.moltbook.com/) est le réseau social pour agents IA (2.5M agents enregistrés, acquis par Meta en mars 2026).
Chaque agent UBIK fleet peut avoir un profil Moltbook et poster des updates via l'API.

## API Moltbook

- Authentification : API key par agent
- Heartbeat : fetch d'un fichier toutes les 4h avec instructions
- POST `/api/v1/post` pour publier
- Docs : https://www.moltbook.com/

## Mission (pour les agents fleet)

Proposer une intégration qui permet à un agent UBIK de :
1. S'enregistrer sur Moltbook (profil = agent_id + label)
2. Poster des updates depuis le relay UBIK (opt-in, messages marqués `[public]`)
3. Lire le feed Moltbook

## Contraintes à respecter

- Pas de fuite de données privées (handoff, journal, internals LBA)
- Posts opt-in uniquement (jamais auto-mirror de toute la radio)
- API key isolée par agent (pas de credentials partagés)
- Compatible avec le stack existant : Python + FastAPI (ubik-mcp) ou TypeScript

## Structure suggérée

```
src/
  moltbook_client.py   # ou moltbook.ts
  moltbook_meca.py     # méca systemd optionnel (heartbeat loop)
tests/
  test_moltbook.py
```

---

## Vision Felix (branche `felix/integration`) — Audit-first + 2-step approval

Angle distinctif : une publication passe par **trois portes** avant d'atteindre Moltbook. Chaque étape est tracée dans un audit log append-only.

### Pipeline

```
agent ──[public:draft] My update───▶ relay ──▶ moltbook-meca
                                                    │
                                                    ▼  filter_content (regex blacklist)
                                                    │
                                       match → REJECTED (audit + reply au sender)
                                                    │
                                                    ▼  DraftQueue.enqueue (TTL 10min)
                                                    │
                                                    ▼  reply "id=abc123, approve dans 10min"

agent ──[public:approve:abc123]──────────────▶ relay ──▶ moltbook-meca
                                                            │
                                                            ▼  filter re-run (defensive)
                                                            │
                                                            ▼  moltbook_client.post() ──▶ Moltbook
                                                            │
                                                            ▼  audit: action=published
```

### Composants

| Fichier | Rôle |
|---|---|
| `src/moltbook_client.py` | Client HTTP minimal (fourni dans le repo init). |
| `src/moltbook_filter.py` | **Privacy filter** (10 patterns) + **append-only audit log** + **DraftQueue** (in-memory, TTL 10 min, single-use, anti-cross-sender). |
| `src/moltbook_meca.py` | Background méca : poll relay → handle `[public:draft]` / `[public:approve:<id>]` → publish + audit. Heartbeat 4h opportuniste. |
| `tests/test_moltbook_filter.py` | 12 tests stdlib `unittest` (12/12 passing). |

### Pourquoi 2-step approval

Un opt-in marker simple (`[public]` → mirror direct) suffit pour filtrer les acks. Il ne protège pas contre **l'erreur de l'agent** : un agent peut intentionnellement écrire `[public]` puis se rendre compte 30s plus tard qu'il a inclus un détail interne — le post est déjà parti.

Le 2-step approval introduit un délai pendant lequel l'agent peut **annuler implicitement** en ne renvoyant pas `[public:approve:<id>]`. Coût : +1 message de friction. Bénéfice : aucune publication accidentelle. Pour un canal public irrévocable (Moltbook = Meta property), le ratio penche clairement vers la friction.

### Blacklist couverte (10 patterns)

`memory_key` · `journal` · `lba_internal` · `client_code` · `rep_name` · `absolute_path` · `workspace_path` · `token_like` · `ghp_token` · `bridge_internal`.

Surface volontairement large — un faux positif force la reformulation, un faux négatif est un leak irrévocable. Filter re-exécuté au moment de l'approve (defensive : si la blacklist a évolué entre draft et approve, on garde la version la plus restrictive).

### Audit log — observabilité

Chaque action (draft_received, draft_rejected, approved, published, publish_failed, draft_expired) ajoute une ligne JSONL à `~/.ubik-memory/moltbook/audit.jsonl` avec `ts`, `iso_ts`, `agent_id`, `draft_id`, `action`, `content_preview` (200 chars max), `filter_matches`, `extra`.

Lecture : `read_audit(limit=50)` → liste de dicts (most-recent-first). Permet à l'operator d'inspecter ce que la fleet a publié sans toucher Moltbook.

### Comment lancer

```bash
# Configure un agent une fois
python3 -c "from moltbook_client import configure; configure('felix', 'sk-...', 'felix-on-moltbook')"

# Lance le méca
RELAY_URL=http://127.0.0.1:7892 python3 src/moltbook_meca.py

# Publie depuis un agent fleet
relay_send(to="moltbook-meca", message="[public:draft] Just shipped a new module.")
# → "id=abc123ef, approve dans 10min"
relay_send(to="moltbook-meca", message="[public:approve:abc123ef]")
# → published ✓
```

### Trade-offs assumés

- **Friction +1 message** : choix conscient pour un canal public irrévocable.
- **Blacklist over-aggressive** : préfère un rejet d'un post propre à un leak.
- **Draft queue in-memory** : restart méca = drafts perdus. L'audit log retient la trace. Drafts = intentions courtes (10 min), pas des états long-terme.
- **Pas de mini-LLM de reformulation** : V1 publie tel quel après filter. V2 pourrait composer un post Moltbook-friendly via LLM, mais risque d'hallucination > bénéfice V1.
