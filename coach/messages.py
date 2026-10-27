"""Messages fixes envoyés par le bot (hors LLM) : brief, planning, bilan, consentement."""

from __future__ import annotations

from coach.db import PlannedMeal, PlannedSession, User
from coach.library import get_library
from coach.planner import in_pantry
from coach.timeutil import day_label
from coach.workouts import FOCUS_LABELS, total_minutes

CONSENT_TEXT = (
    "Bonjour{name} ! Je suis ton assistant sport et repas, testé dans le cadre d'un pilote à l'INP.\n\n"
    "Avant de commencer, ce que je fais de tes données :\n"
    "• je garde tes objectifs, créneaux, préférences alimentaires et tes retours de séance, "
    "uniquement pour préparer ton planning ;\n"
    "• tes messages sont traités par un modèle d'IA (Claude, d'Anthropic) ;\n"
    "• je ne te demande aucune donnée médicale : si tu as une pathologie ou une blessure, "
    "demande d'abord l'avis d'un médecin ;\n"
    "• des statistiques anonymes servent à évaluer le pilote ;\n"
    "• /export t'envoie toutes tes données, /supprimer les efface définitivement.\n\n"
    "Je suis un outil de bien-être, pas un professionnel de santé. Tu es d'accord pour continuer ?"
)

WELCOME_AFTER_CONSENT = (
    "Merci ! On fait connaissance en quelques messages, puis tu sauras quoi faire dès aujourd'hui.\n\n"
    "Pour commencer : qu'est-ce que tu aimerais changer ? Par exemple « bouger 3 fois par semaine » "
    "ou « mieux manger le soir »."
)

HELP_TEXT = (
    "Tu peux m'écrire normalement, par exemple :\n"
    "• « réunion jusqu'à 19h, je décale ? »\n"
    "• « ce soir j'ai 20 minutes et des œufs, des épinards »\n"
    "• « je n'aime pas les champignons »\n\n"
    "Commandes :\n"
    "/aujourdhui ce qui est prévu aujourd'hui\n"
    "/semaine le planning de la semaine\n"
    "/courses la liste de courses\n"
    "/planifier refaire le planning de la semaine prochaine\n"
    "/bilan le bilan de la semaine\n"
    "/silence pas de message aujourd'hui\n"
    "/reprendre lever la pause après une douleur\n"
    "/export recevoir mes données\n"
    "/supprimer effacer mes données"
)


def brief_text(user: User, session: PlannedSession | None, meal: PlannedMeal | None) -> str:
    """Brief du matin : 3 lignes maximum (US9)."""
    lib = get_library()
    hello = f"Bonjour {user.first_name} !" if user.first_name else "Bonjour !"
    lines = [hello]
    if session:
        kind = FOCUS_LABELS.get(session.focus, session.focus).lower()
        lines.append(f"Aujourd'hui : {total_minutes(session.exercises)} min de {kind} à "
                     f"{session.time.replace(':', 'h')}.")
    else:
        lines.append("Pas de séance aujourd'hui, profite.")
    if meal and (recipe := lib.recipe(meal.recipe_id)):
        missing = [i.name for i in recipe.ingredients if not i.basic and not in_pantry(i.name, user.pantry)]
        tail = f" Pense à : {', '.join(missing[:3])}." if missing else ""
        lines.append(f"Dîner : {recipe.name.lower()}.{tail}")
    return "\n".join(lines)


def feedback_prompt(session: PlannedSession) -> str:
    return "C'était comment, ta séance ?"


def planning_intro(user: User, summary_line: str | None) -> str:
    parts = []
    if summary_line:
        parts.append(f"Bilan de la semaine : {summary_line}")
    parts.append("Voici ma proposition pour la semaine prochaine, d'après tes créneaux habituels.")
    return "\n\n".join(parts)


def planning_outro() -> str:
    return ("Un soir ne va pas ? Dis-le-moi simplement (« jeudi je rentre tard ») et j'ajuste. "
            "Sinon, appuie sur « Ça me va ».")


def comeback_text() -> str:
    return "Ça fait un moment ! On reprend en douceur : j'ai allégé les premières séances."


def describe_meal_short(meal: PlannedMeal) -> str:
    recipe = get_library().recipe(meal.recipe_id)
    return f"{day_label(meal.day)} : {recipe.name if recipe else meal.recipe_id}"


SURVEY_TEXT = ("Le pilote touche à sa fin, merci d'y avoir participé ! "
               "De 0 à 10, recommanderais-tu cet assistant à un·e collègue ?")
