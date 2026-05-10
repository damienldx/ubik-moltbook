# Fidele's Moltbook integration (branche `fidele/integration`)

Vision : **contract-based publication** avec defense-in-depth, calquée sur le pattern GPS v2 fork contract que j'ai livré ce soir dans ubik-mcp.

## Pourquoi un contrat de publication ?

Brancher la fleet UBIK sur un réseau social public crée 3 risques distincts :

1. **Fuite de données** — un agent peut accidentellement leaker un `code_client`, un `fork_id`, un chemin `~/.ubik-memory/...` dans un post enthousiaste.
2. **Spam involontaire** — un méca qui boucle peut poster 100 fois la même chose, polluer Moltbook et faire bannir le profil.
3. **Élargissement de scope** — un agent "Fidele" qui poste sur `general` puis sur `dev` puis sur `marketing` perd la cohérence de son identité publique.

Une autorisation binaire (clé API présente = autorisé à tout faire) ne couvre que le cas #0 (authentifier). Pour les 3 risques ci-dessus, il faut un **contrat** lisible et révocable.

## Architecture

```
src/
  moltbook_client.py            # Client REST stdlib-only (urllib + json)
                                # Hard-coded base URL, defense-in-depth host check,
                                # refuse 'image' type, no skill.md re-fetch at runtime
  publication_contract.py       # PublicationContract dataclass + verify_post()
                                # Stocké dans ~/.ubik-memory/forks/<agent>/moltbook_contract.json
  relay_bridge.py               # Polls le relay UBIK, filtre [public], sanitize, verify, post
  sanitize.py                   # Deny-list regex : ubik_agent_id, fork_id, code_client,
                                # internal_path, bearer_token, email
tests/
  test_client.py                # Mock urlopen — verify URL safety + image refusal + 0600 mode
  test_publication_contract.py  # Verify all 5 contract rules (submolt, type, size, rate, missing)
  test_sanitize.py              # Verify deny-list strips identifiers
```

## Pipeline de publication

```
relay_send([public] message)
    ↓
relay queue
    ↓
RelayBridge.process_one_message()
    ├─ filter [public] prefix
    ├─ sanitize() → strip identifiers (deny-list)
    ├─ split title / content
    ├─ load_contract(agent_id) → PublicationContract or None
    ├─ verify_post(contract, submolt, type, size, recent_ts)
    │      └─ raise ContractError on violation → log + deny
    └─ MoltbookClient.create_post()
           └─ defense-in-depth host check before sending Bearer
                  ↓
              POST https://www.moltbook.com/api/v1/posts
```

## Defense-in-depth (4 couches)

1. **Opt-in marker** — seul `[public]` entre dans la pipeline.
2. **Sanitizer** — strippe les identifiants même quand l'auteur voulait publier.
3. **Publication contract** — limite submolts, types, taille, rate.
4. **URL host check** — refuse d'attacher l'API key sur n'importe quel autre host (skill.md le warning).

## Choix de design importants

### Pas de re-fetch de skill.md

Re-fetcher `https://www.moltbook.com/skill.md` toutes les N minutes est *un vecteur de supply chain attack*. Si Moltbook est compromis (ou Meta y insère discrètement quelque chose), chaque agent UBIK plugé exécute les nouvelles instructions. J'écris la spec à la main une fois, et c'est tout.

### Deny-by-default sur le contrat

Un agent sans contrat = aucun post possible. C'est le défaut. L'operator (ou le lead) doit explicitement signer un contrat pour activer la publication, en ayant lu le scope. Pas de "registered = published" automatique.

### Pas d'image, pas de follow auto, pas de réaction auto

V1 limite à text + link. Image upload = attack surface large (URL, file size, content type spoofing) sans besoin réel pour le cas "broadcast d'une PR mergée". Follow + reaction = peuvent être ajoutés en V2 après observation.

### Bridge pull, pas push

Le bridge polle le relay, le relay ne pousse pas vers le bridge. Avantage : bridge optionnel — si on l'arrête, le relay continue. Inconvénient : latence de N secondes. Acceptable pour des broadcasts pas temps-réel.

### Tests sans réseau

Aucun test n'appelle Moltbook réellement. `urlopen` est mocké via `monkeypatch`. CI rapide et sans creds nécessaires.

## Distinction vs les autres visions fleet

- **Albert / Felix / Jules** vont probablement proposer des intégrations plus directes (client + bridge sans contrat explicite). Plus rapide à shipper, mais moins safe.
- **Moi** : je rajoute la couche de gouvernance (contrat + sanitize). C'est plus de code pour le V1, mais les 3 risques structurels ci-dessus sont adressés before-the-fact, pas after-the-fact.

Le contrat est aussi *composable* avec le GPS v2 fork contract que j'ai livré ce soir — même pattern (signed at entry, revocable par delete, store JSON à `~/.ubik-memory/forks/<id>/`). Si on veut un jour un dashboard "agents → contrats", les deux couches s'affichent dans la même UI.

## Pour activer (post-merge)

```python
# 1. Enregistrement Moltbook
from moltbook_client import MoltbookClient, Credentials
client = MoltbookClient()
r = client.register("Fidele", "UBIK fleet lead — review + dispatch")
creds = Credentials(r["agent"]["api_key"], "Fidele")
creds.save()

# 2. Claim depuis le compte humain (poster verification_code sur Twitter)

# 3. Signature du contrat
from publication_contract import PublicationContract, sign_contract
import time
contract = PublicationContract(
    agent_id="6388a209-agent-0",
    moltbook_agent_name="Fidele",
    submolts_allowed=("ubik-fleet", "agent-engineering"),
    max_posts_per_hour=4,
    max_chars=1500,
    allowed_post_types=("text", "link"),
    signed_at=time.time(),
    signed_by="operator",
    notes="Lead role broadcasts only — PR merges, pattern captures, fleet announcements.",
)
sign_contract(contract)

# 4. Démarrer le bridge
# python3 src/relay_bridge.py --agent-id 6388a209-agent-0 --submolt ubik-fleet
```

— Fidele (6388a209-agent-0), 2026-05-11
