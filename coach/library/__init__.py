"""Chargement de la bibliothèque contrôlée d'exercices et de recettes.

L'assistant ne propose jamais d'exercice ni de recette en dehors de ces fichiers.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

LIBRARY_DIR = Path(__file__).parent

EQUIPMENT = ("aucun", "elastique", "halteres", "banc")
LEVELS = {"debutant": 1, "intermediaire": 2, "avance": 3}
AISLES = {
    "fruits_legumes": "Fruits et légumes",
    "frais": "Frais (crèmerie, œufs)",
    "boucherie_poisson": "Boucherie et poissonnerie",
    "surgeles": "Surgelés",
    "boulangerie": "Boulangerie",
    "epicerie": "Épicerie",
}
ANIMAL_TAGS = {"viande", "porc", "volaille", "boeuf", "poisson", "crustaces"}


def normalize(text: str) -> str:
    """Minuscules sans accents ni ligatures, pour comparer des termes saisis librement."""
    text = text.lower().replace("œ", "oe").replace("æ", "ae").replace("\u2019", "'").replace("\u2018", "'")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


@dataclass(frozen=True)
class Exercise:
    id: str
    name: str
    role: str
    focus: tuple[str, ...]
    equipment: str
    level: int
    mode: str
    base: int
    impact: str
    cue: str


@dataclass(frozen=True)
class Ingredient:
    name: str
    qty: float
    unit: str
    aisle: str
    tags: tuple[str, ...] = ()
    basic: bool = False


@dataclass(frozen=True)
class Recipe:
    id: str
    name: str
    minutes: int
    ingredients: tuple[Ingredient, ...]
    steps: tuple[str, ...]
    tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_vegetarian(self) -> bool:
        return not (self.tags & ANIMAL_TAGS)

    @property
    def is_vegan(self) -> bool:
        return self.is_vegetarian and not (self.tags & {"oeuf", "lactose"})


@dataclass(frozen=True)
class Library:
    exercises: dict[str, Exercise]
    recipes: dict[str, Recipe]

    def exercise(self, exercise_id: str) -> Exercise | None:
        return self.exercises.get(exercise_id)

    def recipe(self, recipe_id: str) -> Recipe | None:
        return self.recipes.get(recipe_id)


def _load_exercises(path: Path) -> dict[str, Exercise]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, Exercise] = {}
    for item in raw:
        ex = Exercise(
            id=item["id"],
            name=item["name"],
            role=item["role"],
            focus=tuple(item["focus"]),
            equipment=item["equipment"],
            level=int(item["level"]),
            mode=item["mode"],
            base=int(item["base"]),
            impact=item["impact"],
            cue=item["cue"],
        )
        if ex.id in out:
            raise ValueError(f"Exercice en double : {ex.id}")
        out[ex.id] = ex
    return out


def _load_recipes(path: Path) -> dict[str, Recipe]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, Recipe] = {}
    for item in raw:
        ingredients = tuple(
            Ingredient(
                name=i["name"],
                qty=float(i["qty"]),
                unit=i["unit"],
                aisle=i["aisle"],
                tags=tuple(i.get("tags", ())),
                basic=bool(i.get("basic", False)),
            )
            for i in item["ingredients"]
        )
        tags = frozenset(t for i in ingredients for t in i.tags)
        rec = Recipe(
            id=item["id"],
            name=item["name"],
            minutes=int(item["minutes"]),
            ingredients=ingredients,
            steps=tuple(item["steps"]),
            tags=tags,
        )
        if rec.id in out:
            raise ValueError(f"Recette en double : {rec.id}")
        out[rec.id] = rec
    return out


@lru_cache
def get_library(directory: str | None = None) -> Library:
    base = Path(directory) if directory else LIBRARY_DIR
    return Library(
        exercises=_load_exercises(base / "exercises.yaml"),
        recipes=_load_recipes(base / "recipes.yaml"),
    )


# --- Exclusions alimentaires -------------------------------------------------

# Termes libres de l'utilisateur → tags d'ingrédients exclus.
EXCLUSION_ALIASES: dict[str, set[str]] = {
    "vegetarien": {"viande", "porc", "volaille", "boeuf", "poisson", "crustaces"},
    "vegetarienne": {"viande", "porc", "volaille", "boeuf", "poisson", "crustaces"},
    "vegan": {"viande", "porc", "volaille", "boeuf", "poisson", "crustaces", "oeuf", "lactose"},
    "vegane": {"viande", "porc", "volaille", "boeuf", "poisson", "crustaces", "oeuf", "lactose"},
    "viande": {"viande", "porc", "volaille", "boeuf"},
    "porc": {"porc"},
    "boeuf": {"boeuf"},
    "volaille": {"volaille"},
    "poulet": {"volaille"},
    "poisson": {"poisson"},
    "fruits de mer": {"crustaces"},
    "crustaces": {"crustaces"},
    "gluten": {"gluten"},
    "sans gluten": {"gluten"},
    "lactose": {"lactose"},
    "sans lactose": {"lactose"},
    "produits laitiers": {"lactose"},
    "oeuf": {"oeuf"},
    "oeufs": {"oeuf"},
    "fruits a coque": {"fruits_a_coque"},
    "noix": {"fruits_a_coque"},
    "arachide": {"arachide"},
    "arachides": {"arachide"},
    "cacahuete": {"arachide"},
    "cacahuetes": {"arachide"},
    "soja": {"soja"},
    "champignon": {"champignons"},
    "champignons": {"champignons"},
}


def exclusion_tags_and_terms(exclusions: list[str]) -> tuple[set[str], set[str]]:
    """Sépare les exclusions en tags connus et en termes libres (comparés aux noms)."""
    tags: set[str] = set()
    terms: set[str] = set()
    for raw in exclusions:
        term = normalize(raw)
        if not term:
            continue
        if term in EXCLUSION_ALIASES:
            tags |= EXCLUSION_ALIASES[term]
        else:
            terms.add(term.rstrip("s") if len(term) > 3 else term)
    return tags, terms


def recipe_violates(recipe: Recipe, exclusions: list[str]) -> bool:
    """Vrai si la recette contient un aliment exclu (contrôle strict, US3)."""
    tags, terms = exclusion_tags_and_terms(exclusions)
    if recipe.tags & tags:
        return True
    for ing in recipe.ingredients:
        name = normalize(ing.name)
        if any(t in name for t in terms):
            return True
    return False
