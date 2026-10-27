"""Loading of the controlled exercise and recipe library.

The assistant never suggests an exercise or a recipe that is not in these files.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

LIBRARY_DIR = Path(__file__).parent

EQUIPMENT = ("none", "band", "dumbbells", "bench")
LEVELS = {"beginner": 1, "intermediate": 2, "advanced": 3}
# Aisle id -> label shown to users (French).
AISLES = {
    "produce": "Fruits et légumes",
    "dairy_eggs": "Frais (crèmerie, œufs)",
    "meat_fish": "Boucherie et poissonnerie",
    "frozen": "Surgelés",
    "bakery": "Boulangerie",
    "grocery": "Épicerie",
}
ANIMAL_TAGS = {"meat", "pork", "poultry", "beef", "fish", "shellfish"}


def normalize(text: str) -> str:
    """Lowercase, without accents or ligatures, to compare free-form user input."""
    text = text.lower().replace("œ", "oe").replace("æ", "ae").replace("’", "'").replace("‘", "'")
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
        return self.is_vegetarian and not (self.tags & {"egg", "lactose"})


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
            raise ValueError(f"Duplicate exercise id: {ex.id}")
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
            raise ValueError(f"Duplicate recipe id: {rec.id}")
        out[rec.id] = rec
    return out


@lru_cache
def get_library(directory: str | None = None) -> Library:
    base = Path(directory) if directory else LIBRARY_DIR
    return Library(
        exercises=_load_exercises(base / "exercises.yaml"),
        recipes=_load_recipes(base / "recipes.yaml"),
    )


# --- Food exclusions -----------------------------------------------------------

_NO_ANIMAL = {"meat", "pork", "poultry", "beef", "fish", "shellfish"}
_NO_MEAT = {"meat", "pork", "poultry", "beef"}

# Free-form user terms (normalized) -> excluded ingredient tags.
# Keys are mostly French because users write in French; English synonyms are accepted too.
EXCLUSION_ALIASES: dict[str, set[str]] = {
    "vegetarien": _NO_ANIMAL,
    "vegetarienne": _NO_ANIMAL,
    "vegetarian": _NO_ANIMAL,
    "vegan": _NO_ANIMAL | {"egg", "lactose"},
    "vegane": _NO_ANIMAL | {"egg", "lactose"},
    "viande": _NO_MEAT,
    "meat": _NO_MEAT,
    "porc": {"pork"},
    "pork": {"pork"},
    "boeuf": {"beef"},
    "beef": {"beef"},
    "volaille": {"poultry"},
    "poulet": {"poultry"},
    "chicken": {"poultry"},
    "poisson": {"fish"},
    "fish": {"fish"},
    "fruits de mer": {"shellfish"},
    "crustaces": {"shellfish"},
    "shellfish": {"shellfish"},
    "gluten": {"gluten"},
    "sans gluten": {"gluten"},
    "lactose": {"lactose"},
    "sans lactose": {"lactose"},
    "produits laitiers": {"lactose"},
    "dairy": {"lactose"},
    "oeuf": {"egg"},
    "oeufs": {"egg"},
    "egg": {"egg"},
    "eggs": {"egg"},
    "fruits a coque": {"tree_nuts"},
    "noix": {"tree_nuts"},
    "nuts": {"tree_nuts"},
    "arachide": {"peanut"},
    "arachides": {"peanut"},
    "cacahuete": {"peanut"},
    "cacahuetes": {"peanut"},
    "peanut": {"peanut"},
    "peanuts": {"peanut"},
    "soja": {"soy"},
    "soy": {"soy"},
    "champignon": {"mushroom"},
    "champignons": {"mushroom"},
    "mushroom": {"mushroom"},
    "mushrooms": {"mushroom"},
}


def exclusion_tags_and_terms(exclusions: list[str]) -> tuple[set[str], set[str]]:
    """Split exclusions into known tags and free-form terms (matched against ingredient names)."""
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
    """True if the recipe contains an excluded food (strict check, US3)."""
    tags, terms = exclusion_tags_and_terms(exclusions)
    if recipe.tags & tags:
        return True
    for ing in recipe.ingredients:
        name = normalize(ing.name)
        if any(t in name for t in terms):
            return True
    return False
