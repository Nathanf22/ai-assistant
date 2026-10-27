# Assistant sport & repas

Assistant IA sur Telegram qui transforme des objectifs de sport et d'alimentation en un plan quotidien réaliste : séances guidées, dîners, liste de courses, et ajustement après chaque retour. MVP du pilote de 8 semaines à l'INP.

La boucle couverte : **planifier → faire → donner un retour → ajuster**.

## Principe

Claude comprend les messages et formule les réponses, mais ne décide de rien d'important :

- les **séances** viennent d'une bibliothèque d'exercices contrôlée (`coach/library/exercises.yaml`) ;
- les **dîners** viennent d'une bibliothèque de recettes contrôlée (`coach/library/recipes.yaml`) ;
- la **difficulté** est calculée par des règles déterministes (`coach/progression.py`) ;
- un **garde-fou** (`coach/safety.py`) intercepte douleur, urgence, troubles alimentaires et situations médicales *avant* tout appel au LLM, met la progression en pause et renvoie vers un professionnel.

```
Telegram ──► bot.py ──► safety.py ──► agent.py (Claude + outils) ──► services.py
                │                                                     │
             jobs.py (brief, retour, planning du dimanche)          planner.py · workouts.py · progression.py
                                                                      │
                                                              db.py (SQLite / PostgreSQL)
```

| Module | Rôle |
| --- | --- |
| `bot.py` | Commandes, messages libres, boutons en un tap |
| `jobs.py` | Tâche chaque minute : brief du matin, demande de retour, planning et bilan du dimanche, questionnaire de fin de pilote |
| `agent.py` | Boucle tool-use avec Claude, prompt système mis en cache |
| `services.py` | Opérations métier en base, journalisées dans `event_log` |
| `planner.py` | Choix des créneaux, des dîners, liste de courses (fonctions pures) |
| `workouts.py` | Construction d'une séance adaptée à la durée, au matériel et à la charge |
| `progression.py` | Règles de charge (1 à 10) |
| `safety.py` | Détection des messages à risque |
| `metrics.py` | Tableau de bord du pilote (`/stats`, `coach-metrics`) |

## Démarrage rapide

1. Créer un bot avec [@BotFather](https://t.me/BotFather) et récupérer son jeton.
2. Créer une clé API sur [console.anthropic.com](https://console.anthropic.com).
3. Configurer :

```bash
cp .env.example .env   # puis renseigner TELEGRAM_BOT_TOKEN et ANTHROPIC_API_KEY
```

### En local (SQLite)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
coach-bot
```

### Avec Docker (PostgreSQL)

```bash
docker compose up -d --build
docker compose logs -f bot
```

Le bot fonctionne en *polling* : aucune URL publique n'est nécessaire, une petite machine ou un conteneur suffit.

### Tests

```bash
pytest -q        # 136 tests : règles, planning, agent simulé, planificateur, handlers Telegram
ruff check .
```

## Utilisation

L'utilisateur écrit normalement (« réunion jusqu'à 19h », « ce soir j'ai 20 minutes et des œufs », « je n'aime pas les champignons »). Commandes disponibles :

| Commande | Effet |
| --- | --- |
| `/start [CODE]` | Consentement puis onboarding conversationnel |
| `/aujourdhui` | Séance et dîner du jour |
| `/semaine` | Planning de la semaine |
| `/courses` | Liste de courses par rayon |
| `/planifier` | Refaire le planning |
| `/bilan` | Bilan de la semaine |
| `/silence` | Aucun message automatique aujourd'hui |
| `/reprendre` | Lever la pause après une douleur |
| `/export` | Recevoir toutes ses données en JSON |
| `/supprimer` | Effacer définitivement ses données |
| `/stats` | Métriques du pilote (administrateurs uniquement) |

Pour le pilote, renseigner `PILOT_INVITE_CODES` et distribuer le lien `https://t.me/<nom_du_bot>?start=<CODE>`.

## Règles de progression

| Situation | Effet |
| --- | --- |
| 2 retours « dur » d'affilée à la même charge | charge − 1 |
| 2 retours « facile » d'affilée à la même charge | charge + 1 |
| Séance sautée | aucun effet |
| 10 jours ou plus sans séance | charge − 2 au prochain planning |
| Douleur ou avis médical signalé | pas de hausse, séances de mobilité douce uniquement |

## Couverture des user stories

| Épopée | Stories | État |
| --- | --- | --- |
| E1 Onboarding | US1–US4 | Fait : conversation guidée, créneaux, matériel, exclusions (test automatisé), consentement |
| E2 Planning hebdomadaire | US5–US8 | Fait : planning du dimanche, contraintes, 4 à 5 dîners remplaçables, liste par rayon |
| E3 Quotidien et imprévus | US9–US12 | Fait : brief en un tap, raccourcir / décaler / annuler, « 20 minutes et ça dans le frigo », mode silence |
| E4 Séance et retour | US13–US15 | Fait : séance détaillée, retour en un tap, séance sautée sans culpabilité |
| E5 Progression et mémoire | US16–US18 | Fait : règles déterministes, préférences, reprise après pause |
| E6 Bilan | US19 | Fait : bilan enchaîné avec le planning du dimanche |
| E7 Confiance et pilote | US20–US24 | Fait : garde-fou, export et suppression immédiate, `/stats`, relecture des plans (`coach-metrics --plans 50`), questionnaire en semaine 8 |

## Données et RGPD

- Aucune donnée médicale n'est stockée : une mention de douleur pose un drapeau de pause, sans le texte du message.
- Les messages sont envoyés à l'API Anthropic pour être traités.
- `/supprimer` efface immédiatement toutes les données ; seul un compteur anonyme subsiste dans le journal.
- Avant le pilote : héberger la base dans l'UE et valider la qualification des données (et un éventuel besoin d'hébergement HDS) avec le DPO de l'INP.

## Avant le pilote

- [ ] Faire valider les bibliothèques d'exercices (coach, STAPS) et de recettes (diététicien·ne) ; elles comptent 43 exercices et 25 recettes, l'objectif de départ était 60 et 40.
- [ ] Faire relire les messages du garde-fou (`coach/safety.py`).
- [ ] Renseigner les tarifs `LLM_*_USD_PER_MTOK` pour suivre le coût par utilisateur.
- [ ] Ajouter des migrations Alembic avant toute évolution du schéma en production.
- [ ] Tester le parcours complet avec l'équipe pendant une semaine.
