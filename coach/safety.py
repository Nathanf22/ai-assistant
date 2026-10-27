"""Safety guardrail (US20): detects risky messages before any LLM call.

Detection is deliberately broad: a false positive costs one cautious message, a false
negative can cost an injury. The message content itself is never stored.

Patterns match French user input (normalized: lowercase, no accents) and the replies are
user-facing, so both stay in French.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from coach.library import normalize

# Order = priority: the first matching category wins.
PATTERNS: dict[str, list[str]] = {
    "crisis": [
        r"\bsuicid", r"me tuer", r"en finir", r"plus envie de vivre", r"me faire du mal",
        r"\bscarif",
    ],
    "emergency": [
        r"douleur (a la |dans la )?poitrine", r"douleur thoracique", r"oppression",
        r"palpitation", r"malaise", r"evanoui", r"perdu connaissance", r"\bvertige",
        r"du mal a respirer", r"essouffle anormal",
    ],
    "eating": [
        r"ne plus manger", r"arreter de manger", r"sauter (tous )?les repas",
        r"jeun(e|er) (de |pendant )?plusieurs jours", r"\bvomi", r"me faire vomir", r"\bpurg",
        r"\b[1-9]\d{2} ?(k?cal|calories) ?(par|/|a la) ?jour(nee)?\b", r"perdre \d+ ?kg en (une|1|deux|2) semaine",
        r"anorex", r"boulim",
    ],
    "pain": [
        r"\bdouleur", r"\bj'?ai mal\b", r"\bmal (au|aux|a la|a l')\b", r"\bblesse", r"\bblessure",
        r"entorse", r"tendinite", r"foulure", r"claquage", r"dechirure", r"lombalgie",
        r"sciatique", r"\b(ca|genou|dos|epaule|cheville|poignet|hanche) (me )?tire",
        r"\bfracture", r"\bcourbatur(e|es) (insupportable|tres forte)",
    ],
    "medical": [
        r"\bdiabet", r"\benceinte\b", r"\bgrossesse", r"cardiaque", r"hypertension",
        r"\bopere", r"\boperation\b", r"\basthme", r"epilep", r"\bcancer", r"chimio",
    ],
}

_COMPILED = {k: [re.compile(p) for p in v] for k, v in PATTERNS.items()}

MESSAGES: dict[str, str] = {
    "crisis": (
        "Ce que tu écris m'inquiète, et je préfère te le dire directement. "
        "Tu peux appeler le 3114, le numéro national de prévention du suicide, "
        "gratuit et ouvert 24 h/24. En cas de danger immédiat, appelle le 15 ou le 112. "
        "Je reste là pour la suite, mais parle-en aussi à quelqu'un maintenant."
    ),
    "emergency": (
        "Arrête l'effort maintenant. Ce que tu décris peut être sérieux : "
        "si ça persiste ou s'aggrave, appelle le 15 (SAMU) ou le 112. "
        "J'ai mis tes séances en pause ; on reprendra quand un médecin t'aura donné le feu vert."
    ),
    "eating": (
        "Je ne peux pas t'aider à restreindre fortement ton alimentation : c'est risqué pour ta santé. "
        "Si manger devient une source d'angoisse, parles-en à un médecin ou au service de santé "
        "universitaire. Je peux continuer à te proposer des repas simples et équilibrés."
    ),
    "pain": (
        "Merci de me le dire. Je mets la progression en pause : pas de séance plus difficile "
        "tant que la douleur est là, et je te propose seulement de la mobilité douce. "
        "Si la douleur dure plus de quelques jours, est vive ou gonfle, consulte un médecin "
        "ou un kiné. Écris /reprendre quand tu te sens de nouveau bien."
    ),
    "medical": (
        "Avec cette situation, un avis médical est important avant de suivre un programme. "
        "Je ne suis pas un professionnel de santé et je ne peux pas l'adapter à une pathologie. "
        "Je mets la progression en pause ; écris /reprendre quand un médecin t'a donné son accord."
    ),
}

HOLDS_PROGRESSION = {"emergency", "pain", "medical"}


@dataclass(frozen=True)
class SafetyResult:
    category: str
    message: str

    @property
    def holds_progression(self) -> bool:
        return self.category in HOLDS_PROGRESSION

    @property
    def blocks_llm(self) -> bool:
        """Serious cases get a fixed reply and never reach the LLM."""
        return self.category in {"crisis", "emergency", "eating"}


def check(text: str) -> SafetyResult | None:
    norm = normalize(text)
    for category, patterns in _COMPILED.items():
        if any(p.search(norm) for p in patterns):
            return SafetyResult(category, MESSAGES[category])
    return None
