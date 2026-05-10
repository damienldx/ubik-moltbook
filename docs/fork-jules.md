# ubik-moltbook · fork Jules (b5aeb927-agent-0)

> Branche : `jules/integration`
> Date    : 2026-05-11

## Angle distinctif

Trois autres forks (claude-main, Felix, Fidele, Albert) proposent vraisemblablement un client + un méca poll. Mon fork prend un **angle différent** : zéro nouveau service systemd, **intégration via MCP tools dans ubik-mcp**, et **un filtre de contenu defense-in-depth** qui rejette tout ce qui ressemble à du contenu fleet-interne, même si l'agent flag `[public]` par mistake.

## Composants livrés

### `src/moltbook/client.py` — client REST minimal (httpx)

Une seule classe `MoltbookClient`. Stateless, no retry. Méthodes :
- `register(display_name, bio)` — idempotent
- `post(content, tags=)`
- `read_feed(limit, since_id)`
- `heartbeat()` + `heartbeat_overdue()` (fichier local timestamp pour skip si récent)

Pas de FastAPI server, pas de daemon. La cadence heartbeat 4h est laissée à l'appelant (systemd timer ou un méca minimal).

### `src/moltbook/filter.py` — defense-in-depth

7 règles regex blocklist :
- `memory_artifact_path` : `.ubik-memory`, `handoff_<id>.md`, `MEMORY.md`, `journal/YYYY-MM-DD`
- `ubik_internal_dir` : `.claude-fleet`, `.ubik-desktop`
- `internal_url` : `localhost:N`, `127.0.0.1:N`, `dev-station-02`, IP privées
- `lba_internal` : `LBA-DESKTOP`, `PRISMA_*`, `REP_TO_PRISMA_ID`, `HATTON_PERIMETRE`
- `lba_client_name` : noms clients connus (à étendre)
- `github_token` : `ghp_*`, `github_pat_*`
- `api_key_like` : `api_key=...`, `secret=...`, `Bearer <token>`

**Philosophie** : strict + noisy. Un faux positif ("tu as bloqué mon post légit") est préférable à un leak. La règle est extensible (`extra_rules` argument) pour les besoins projet.

Tests dans `tests/test_filter.py` — 9 tests, `python tests/test_filter.py`.

### `src/moltbook/handle.py` — fleet label → Moltbook handle

`resolve_handle(agent_id)` retourne le handle public, ordre de fallback :
1. `override` argument explicite
2. env `MOLTBOOK_HANDLE`
3. fetch du `label` via relay (`GET /agents/<id>` → "Jules" plutôt que `b5aeb927-agent-0`)
4. raw `agent_id` slugifié

Le préfixe `[lead]` etc. est strippé du label. **Le UUID interne n'apparaît jamais sur la timeline publique** sauf si tout le fallback échoue.

### `src/moltbook/cli.py` — smoke test

```
python -m moltbook.cli register --name "Jules" --bio "UBIK fleet, backend"
python -m moltbook.cli post "[public] PR #38 merged"
python -m moltbook.cli feed --limit 10
python -m moltbook.cli heartbeat
```

La CLI strip le `[public]` prefix avant de poster (le flag est routing-layer, pas content).

## Intégration suggérée dans ubik-mcp (out-of-scope ici)

```ts
// dans src/servers/moltbook.ts
server.tool("moltbook_post", "Post to Moltbook from a [public]-tagged message.", {
  agent_id: z.string(),
  content: z.string(),
  tags: z.array(z.string()).optional(),
}, async ({ agent_id, content, tags }) => {
  // shell out to: python -m moltbook.cli post "<content>" --tag X
  // ou import direct via pyodide / pythonjs
});
```

Avec la taxonomy de PR #32 (gps-v2/taxonomic-tools), ces tools sont taggés `social × http_api` — un agent qui ne veut pas poster sur Moltbook ajoute simplement dans son `~/.ubik-desktop/agents/<id>.yaml` :

```yaml
exclude_classes: [social]
```

Et il sort proprement du toolkit. **L'opt-out est explicite, par-agent, gratuit**.

## Bridge `[public]` côté relay (out-of-scope ici)

Le relay (`ubik-fleet/relay/server.py`) watche les messages avec préfixe `[public]` et déclenche `moltbook_post`. Hook 5 lignes :

```python
if message.lstrip().lower().startswith("[public]"):
    fire_and_forget(mcp_call, "moltbook_post", {"agent_id": sender, "content": message})
```

L'agent écrit `[public] J'ai mergé PR #38 ce soir, fix qualité ciblage CRM` — le filtre catch `CRM` ? Non (CRM seul n'est pas dans la blocklist), le post passe. Mais si l'agent écrit `[public] PR #38 sur LBA-DESKTOP mergée` — le filtre rejette `lba_internal`. Et l'agent est warned dans son inbox.

## Pourquoi ce fork

Sans découplage (3 couches MCP + filter + handle), trois failure modes probables :
1. **Leak via négligence** : l'agent flag `[public]` sur un contenu qui mentionne un client. Le filtre catch.
2. **Identité confuse** : posts signés `b5aeb927-agent-0` côté Moltbook. Personne ne sait qui c'est. Le handle resolver donne "jules".
3. **Maintenance** : un nouveau service systemd `moltbook_meca` à maintenir. La taxonomy + manifest gère opt-in/out sans daemon.

## Limites assumées

- Pas de cache : si on poste 100 fois en boucle, on tape Moltbook 100 fois. À ajouter si volume.
- Pas de retry : un échec réseau remonte direct au caller. Convient pour MCP tool (le caller gère).
- Heartbeat dépend du caller : pas de timer interne. Sur systemd, le timer est trivial à écrire.
- La blocklist est statique et incomplète. La maintenir = entrer dans la durée. Acceptable pour un MVP.
