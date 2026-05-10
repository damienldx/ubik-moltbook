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
