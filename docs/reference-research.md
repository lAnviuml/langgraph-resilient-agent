# Notes de référence — agent résilient FastAPI/LangGraph

Recherche vérifiée le **2026-08-01**. Ce document décrit des patterns d’architecture ; il ne copie aucun code tiers.

## Socle recommandé

- **Python 3.12** comme baseline mature et largement compatible ; LangGraph et FastAPI déclarent Python `>=3.10`.
- **LangGraph 1.2.10**, **langgraph-checkpoint-postgres 3.1.1**, **FastAPI 0.141.1**.
- Versions de support observées : Uvicorn 0.52.1, Pydantic 2.13.4, pytest 9.1.1. Les verrouiller dans `uv.lock` plutôt que copier des plages flottantes dans le déploiement.
- `AsyncPostgresSaver` en production ; `InMemorySaver` uniquement pour tests et développement. La documentation précise que la mémoire disparaît au redémarrage et recommande PostgreSQL pour la persistance durable.

## Architecture retenue

### API et identité d’exécution

FastAPI ne contient pas la logique du graphe. Il valide/authentifie, crée un `thread_id` UUID opaque et un `run_id`, puis appelle un service applicatif :

- `POST /runs` : démarrer, avec `Idempotency-Key` ;
- `GET /runs/{thread_id}` : état courant et éventuelle demande d’approbation ;
- `POST /runs/{thread_id}/resume` : reprendre avec une décision typée ;
- `GET /runs/{thread_id}/audit` : lecture autorisée du journal.

Le propriétaire du thread est vérifié dans la base à chaque opération. Le client ne peut pas choisir l’identité, la politique d’outil ni un `checkpoint_id` arbitraire.

### Graphe déterministe et checkpoints

Construire le `StateGraph` dans une factory injectant modèle, outils, checkpointer, horloge et générateur d’identifiants. L’état reste JSON-sérialisable et minimal ; les secrets et gros objets vivent hors checkpoint.

Chaque invocation et reprise utilise le même `thread_id`. Les checkpoints permettent continuité, reprise après panne et historique. Les écritures intermédiaires de tâches évitent de recalculer les nœuds parallèles déjà terminés après l’échec d’un autre nœud.

En production, appeler `AsyncPostgresSaver.setup()` via une migration/job contrôlé, pas à chaque requête. Définir chiffrement, sauvegardes, rétention et purge : les checkpoints croissent sans limite si aucune politique n’est prévue.

### Approbation humaine

Un `ApprovalPolicy` déterministe classe chaque action `ALLOW`, `REQUIRE_APPROVAL` ou `DENY` selon outil, portée, ressource et principal. Le LLM propose ; il ne décide jamais de l’autorisation.

Pour `REQUIRE_APPROVAL`, un nœud dédié appelle `interrupt()` avec une charge JSON simple et non secrète. La reprise se fait avec `Command(resume=...)`, le même `thread_id`, l’acteur approbateur et une décision validée par schéma.

Règle critique LangGraph : lors de la reprise, le nœud contenant `interrupt()` recommence depuis son début. Donc :

- ne pas envelopper `interrupt()` dans un `try/except` générique ;
- garder l’ordre des interrupts déterministe ;
- placer les effets externes après l’approbation ou dans un nœud séparé ;
- rendre tout effet exécuté avant l’interrupt strictement idempotent.

### Outils idempotents et reprise

Chaque appel d’outil reçoit un `operation_id` calculé/stable (`thread_id + run_id + tool_call_id + version`). Une table `tool_operations` possède une contrainte unique sur cette clé et conserve statut, hash d’entrée, résultat/référence et erreur.

Pattern d’exécution : réserver l’opération transactionnellement → retourner le résultat existant si déjà terminé → exécuter via outbox/worker pour les effets distants → enregistrer le résultat → publier l’événement. L’API distante reçoit aussi l’idempotency key si elle la supporte. Les outils `send`, `create`, `charge` ou `delete` ne reposent jamais sur un simple retry aveugle.

La reprise depuis le dernier checkpoint est le chemin normal. Le time-travel/replay est réservé à l’administration : la documentation précise que les nœuds postérieurs au checkpoint, appels LLM, API et interrupts compris, sont réexécutés. Il faut donc les mêmes garanties d’idempotence et une nouvelle autorisation si la politique l’exige.

### Journal d’audit

Les checkpoints ne remplacent pas un audit métier. Écrire un journal append-only séparé : `event_id`, timestamp UTC, actor/service, tenant, thread/run/checkpoint/node, décision de politique, approbateur, outil, `operation_id`, hashes entrée/sortie, statut et trace ID. Redacter tokens, prompts complets, secrets et PII ; journaliser des références ou hashes. Protéger le journal contre modification et tester sa complétude lors des échecs.

## Tests sans clé API

- Factory du graphe alimentée par `FakeModel`, `FakeTools`, horloge et IDs déterministes ; aucune importation/configuration d’un fournisseur live dans les tests par défaut.
- Nouveau `InMemorySaver` par test, conformément au guide LangGraph ; tester aussi nœuds et chemins partiels indépendamment.
- Scénarios graphe : interruption → approbation/rejet → reprise avec même thread ; mauvais thread refusé ; panne après effet puis retry sans doublon ; reprise après nœud parallèle ; replay/fork ; ordre d’interrupt stable ; sérialisation de l’état.
- FastAPI : `TestClient`/HTTPX et `app.dependency_overrides` pour remplacer authentification, graphe, dépôt et horloge. Tester 401/403, ownership inter-tenant, validation des décisions et `Idempotency-Key` rejoué.
- Intégration PostgreSQL : checkpointer et tables outbox/audit réels dans un conteneur ; redémarrer l’application entre interruption et reprise.
- Marquer les tests fournisseur `@pytest.mark.live_model`, exclus par défaut et exécutés seulement avec secrets éphémères dans un job séparé.

## Arborescence conseillée

```text
pyproject.toml
uv.lock
compose.yaml
src/resilient_agent/
  config.py
  api/                 app.py, routes.py, schemas.py, dependencies.py
  application/         run_service.py, resume_service.py
  graph/               builder.py, state.py, nodes.py, routing.py
  approval/            policy.py, models.py
  tools/               registry.py, contracts.py, executor.py, idempotency.py
  persistence/         checkpointer.py, run_repository.py, operation_repository.py
  audit/               events.py, repository.py, redaction.py
tests/
  unit/                approval/, tools/, audit/
  graph/               test_interrupt_resume.py, test_recovery.py,
                       test_replay.py, test_determinism.py
  api/                 test_runs.py, test_resume.py, test_authorization.py
  integration/         test_postgres_checkpointer.py,
                       test_outbox_idempotency.py, test_restart_recovery.py
  support/             fake_model.py, fake_tools.py, fixed_clock.py
docs/
```

## Risques et retour arrière

- Les checkpoints peuvent contenir des données sensibles et grossir : minimisation, chiffrement, rétention et tests de purge sont requis.
- Un changement de schéma d’état peut rendre les anciens checkpoints illisibles : versionner l’état, fournir une migration et déployer en canary. Le rollback conserve l’ancienne image et l’ancien schéma tant que les nouveaux threads ne sont pas validés.
- Une reprise peut répéter un effet externe : contrainte d’unicité, outbox et tests de crash sont des prérequis, pas des optimisations.
- L’approbation ne vaut que pour l’action exacte présentée ; toute modification d’arguments invalide la décision et redemande une approbation.

## Sources primaires et licences

- [LangGraph — dépôt officiel et exemples](https://github.com/langchain-ai/langgraph)
- [LangGraph — persistance et checkpointers](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph — interrupts et règles de reprise](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph — replay et fork](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [LangGraph — tests de graphes](https://docs.langchain.com/oss/python/langgraph/test)
- [LangGraph 1.2.10 — métadonnées PyPI](https://pypi.org/project/langgraph/1.2.10/)
- [langgraph-checkpoint-postgres 3.1.1 — PyPI](https://pypi.org/project/langgraph-checkpoint-postgres/3.1.1/)
- [FastAPI — tests](https://fastapi.tiangolo.com/tutorial/testing/)
- [FastAPI — remplacement des dépendances en test](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- [FastAPI 0.141.1 — PyPI](https://pypi.org/project/fastapi/0.141.1/)
- [LangGraph, licence MIT](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)
- [FastAPI, licence MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE)

Les exemples du dépôt LangGraph sont couverts par sa licence MIT. Ils ont servi uniquement à confirmer les primitives et patterns ; aucune portion de code n’est reproduite ici.
