"""Validation de nouveaux relevés de déchets par rapport à un historique validé.

Le script charge deux fichiers JSON (l'historique déjà validé et les nouveaux
relevés), vérifie les sites avec Pydantic, signale les relevés incomplets,
compare les moyennes de déchets et de plastiques pour 100 m et génère un
graphique par site dans ``courbe.png``.

Utilisation :
    python3 Validation.py      (ou simplement ``make``)
"""

import json

import matplotlib.pyplot as plt
from pydantic import BaseModel, Field, ValidationError

# Score de fiabilité : chaque relevé part de 100 et perd des points par problème.
# Modifier ces valeurs suffit pour rendre le contrôle plus ou moins sévère.
PENALTY_MISSING = 60             # une valeur (déchets ou plastiques) manquante
PENALTY_PLASTIC_OVER_TOTAL = 50  # plus de plastiques que de déchets au total : impossible
PENALTY_BIG_DIFF = 40            # écart > MAX_DIFF_PCT avec l'historique du site
PENALTY_SMALL_DIFF = 10          # écart entre MIN_DIFF_PCT et MAX_DIFF_PCT

# Seuils d'écart (en %, en plus ou en moins) avec la moyenne historique du site
MIN_DIFF_PCT = 30
MAX_DIFF_PCT = 50

# Au-dessus de RELIABLE_SCORE : fiable ; au-dessus de DOUBTFUL_SCORE : à vérifier ; sinon douteux
RELIABLE_SCORE = 80
DOUBTFUL_SCORE = 50


class Site(BaseModel):
    """Un site de relevé (plage ou berge), vérifié par Pydantic.

    Les clés du JSON sont en français. Les ``alias`` permettent de les lire
    tout en exposant des attributs en anglais (ex. ``"nom"`` -> ``name``).
    Une ``ValidationError`` est levée si un type est incorrect ou si les
    coordonnées sont hors limites.

    Attributes:
        site_id: Identifiant du site (ex. ``"S1"``).
        name: Nom du site (clé JSON ``nom``).
        town: Commune (clé JSON ``commune``).
        environment: Type de milieu, ex. ``"plage"`` (clé JSON ``milieu``).
        latitude: Latitude en degrés, entre -90 et 90.
        longitude: Longitude en degrés, entre -180 et 180.
    """

    site_id: str
    name: str = Field(alias="nom")
    town: str = Field(alias="commune")
    environment: str = Field(alias="milieu")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class Reading:
    """Un relevé de déchets effectué sur un site à une date donnée.

    Attributes:
        reading_id: Identifiant du relevé (ex. ``"N01"``).
        site_id: Identifiant du site où le relevé a été fait.
        date: Date du relevé au format ``AAAA-MM-JJ``.
        waste_100m: Nombre de déchets pour 100 m, ou ``None`` si manquant.
        plastic_100m: Nombre de plastiques pour 100 m, ou ``None`` si manquant.
        dominant_type: Type de déchet le plus fréquent.
        latitude: Latitude du relevé (absente dans l'historique).
        longitude: Longitude du relevé (absente dans l'historique).
    """

    def __init__(self, releve_id, site_id, date, dechets_100m, plastiques_100m,
                 type_dominant, latitude=None, longitude=None):
        """Crée un relevé à partir des champs d'un relevé JSON.

        Les noms des paramètres sont volontairement ceux du JSON, pour pouvoir
        écrire ``Reading(**releve_json)`` : le ``**`` déballe le dictionnaire
        en arguments nommés.

        Args:
            releve_id: Identifiant du relevé.
            site_id: Identifiant du site.
            date: Date du relevé (``AAAA-MM-JJ``).
            dechets_100m: Nombre de déchets pour 100 m (peut être ``None``).
            plastiques_100m: Nombre de plastiques pour 100 m (peut être ``None``).
            type_dominant: Type de déchet dominant.
            latitude: Latitude du relevé, optionnelle.
            longitude: Longitude du relevé, optionnelle.
        """
        self.reading_id = releve_id
        self.site_id = site_id
        self.date = date
        self.waste_100m = dechets_100m
        self.plastic_100m = plastiques_100m
        self.dominant_type = type_dominant
        self.latitude = latitude
        self.longitude = longitude


class Result:
    """Résultat de la validation : moyennes calculées et erreurs trouvées.

    Attributes:
        hist_waste_100m: Moyenne des déchets / 100 m de l'historique.
        hist_plastic_100m: Moyenne des plastiques / 100 m de l'historique.
        new_waste_100m: Moyenne des déchets / 100 m des nouveaux relevés.
        new_plastic_100m: Moyenne des plastiques / 100 m des nouveaux relevés.
        errors: Problèmes par relevé, sous la forme ``{reading_id: [messages]}``
            (seuls les relevés ayant au moins un problème y figurent).
        scores: Score de fiabilité (0 à 100) de **chaque** nouveau relevé,
            sous la forme ``{reading_id: score}``.
    """

    def __init__(self):
        """Initialise les moyennes à 0 et des dictionnaires d'erreurs et de scores vides."""
        self.hist_waste_100m = 0
        self.hist_plastic_100m = 0
        self.new_waste_100m = 0
        self.new_plastic_100m = 0
        self.errors = {}
        self.scores = {}

    def is_valid(self):
        """Indique si la validation n'a trouvé aucune erreur.

        Returns:
            ``True`` si ``errors`` est vide, ``False`` sinon.
        """
        return not self.errors


def diff_penalty(diff):
    """Donne la pénalité correspondant à un écart avec l'historique du site.

    Args:
        diff: Écart en %, positif ou négatif (voir ``pct_diff()``).

    Returns:
        ``PENALTY_BIG_DIFF`` au-delà de ``MAX_DIFF_PCT`` %,
        ``PENALTY_SMALL_DIFF`` entre ``MIN_DIFF_PCT`` et ``MAX_DIFF_PCT`` %,
        ``0`` en dessous.
    """
    # abs() : un écart de -60 % est aussi suspect qu'un écart de +60 %
    if abs(diff) > MAX_DIFF_PCT:
        return PENALTY_BIG_DIFF
    if abs(diff) >= MIN_DIFF_PCT:
        return PENALTY_SMALL_DIFF
    return 0


def reliability(reading: Reading, hist_readings: list[Reading]):
    """Calcule le score de fiabilité d'un nouveau relevé.

    Le score part de 100 et chaque problème retire des points selon sa
    gravité:

    - valeur de déchets ou de plastiques manquante ;
    - plus de plastiques que de déchets au total ;
    - écart avec la moyenne historique **du même site**, petit (30-50 %)
      ou grand (> 50 %), vérifié séparément pour les déchets et les plastiques.

    Args:
        reading: Nouveau relevé à évaluer.
        hist_readings: Relevés déjà validés, servant de référence.

    Returns:
        Un tuple ``(score, messages)`` : le score entre 0 et 100, et la liste
        des problèmes trouvés avec les points retirés (vide si aucun problème).
    """
    score = 100
    messages = []

    if reading.waste_100m is None:
        score -= PENALTY_MISSING
        messages.append(f"Déchets manquants (-{PENALTY_MISSING})")
    if reading.plastic_100m is None:
        score -= PENALTY_MISSING
        messages.append(f"Plastiques manquants (-{PENALTY_MISSING})")

    # Les plastiques font partie des déchets : ils ne peuvent pas être plus nombreux
    if (reading.waste_100m is not None and reading.plastic_100m is not None
            and reading.plastic_100m > reading.waste_100m):
        score -= PENALTY_PLASTIC_OVER_TOTAL
        messages.append(f"Plastiques ({reading.plastic_100m}) supérieurs au total "
                        f"des déchets ({reading.waste_100m}) (-{PENALTY_PLASTIC_OVER_TOTAL})")

    # Comparaison à l'historique du même site : chaque site a son propre niveau normal
    # (ex. S2 ≈ 86 déchets, S3 ≈ 250), une moyenne tous sites confondus n'aurait pas de sens
    for attribute, label in (("waste_100m", "Déchets"), ("plastic_100m", "Plastiques")):
        value = getattr(reading, attribute)
        if value is None:
            continue
        diff = pct_diff(value, site_average(hist_readings, reading.site_id, attribute))
        penalty = diff_penalty(diff)
        if penalty:
            score -= penalty
            messages.append(f"{label} : {diff:+.0f} % vs historique du site (-{penalty})")

    # Plusieurs gros problèmes pourraient faire passer le score sous 0
    return max(score, 0), messages


def reliability_label(score):
    """Traduit un score de fiabilité en appréciation lisible.

    Args:
        score: Score de fiabilité entre 0 et 100.

    Returns:
        ``"fiable"`` à partir de ``RELIABLE_SCORE``, ``"à vérifier"`` à partir
        de ``DOUBTFUL_SCORE``, ``"douteux"`` en dessous.
    """
    if score >= RELIABLE_SCORE:
        return "fiable"
    if score >= DOUBTFUL_SCORE:
        return "à vérifier"
    return "douteux"


def averages(new_readings: list[Reading], hist_readings: list[Reading]) -> Result:
    """Calcule les moyennes et le score de fiabilité de chaque nouveau relevé.

    Chaque nouveau relevé reçoit un score avec ``reliability()``. Ses
    problèmes éventuels sont enregistrés dans ``Result.errors``.

    Les relevés avec une valeur manquante sont exclus des moyennes. Les
    autres y restent même avec un score bas, car un relevé suspect n'est
    pas forcément faux.

    Args:
        new_readings: Nouveaux relevés à vérifier.
        hist_readings: Relevés déjà validés, servant de référence.

    Returns:
        Un ``Result`` contenant les quatre moyennes, les scores et les erreurs.

    Raises:
        ZeroDivisionError: Si ``hist_readings`` est vide.
    """
    result = Result()

    # Moyennes de l'historique : les données sont déjà validées, donc complètes
    for reading in hist_readings:
        result.hist_waste_100m += reading.waste_100m
        result.hist_plastic_100m += reading.plastic_100m

    result.hist_waste_100m /= len(hist_readings)
    result.hist_plastic_100m /= len(hist_readings)

    valid_count = 0
    for reading in new_readings:
        score, messages = reliability(reading, hist_readings)
        result.scores[reading.reading_id] = score
        if messages:
            result.errors[reading.reading_id] = messages

        # Moyennes des nouveaux relevés : on saute ceux qui ont une valeur manquante
        if reading.waste_100m is None or reading.plastic_100m is None:
            continue
        result.new_waste_100m += reading.waste_100m
        result.new_plastic_100m += reading.plastic_100m
        valid_count += 1

    # On divise par le nombre de relevés réellement additionnés, pas par len(new_readings)
    if valid_count > 0:
        result.new_waste_100m /= valid_count
        result.new_plastic_100m /= valid_count

    return result

def pct_diff(new_value, reference):
    """Calcule l'écart en pourcentage d'une valeur par rapport à une référence.

    Formule : ``(new_value - reference) / reference * 100``.
    Exemple : ``pct_diff(250, 200)`` renvoie ``25.0`` (+25 %).

    Args:
        new_value: Valeur à comparer.
        reference: Valeur de référence (ne doit pas être 0).

    Returns:
        L'écart en %, positif si ``new_value`` est au-dessus de la référence.

    Raises:
        ZeroDivisionError: Si ``reference`` vaut 0.
    """
    return (new_value - reference) / reference * 100


def diffs(result: Result):
    """Calcule les écarts en % des nouveaux relevés par rapport à l'historique.

    Args:
        result: Résultat renvoyé par ``averages()``.

    Returns:
        Un tuple ``(waste_diff, plastic_diff)`` : l'écart en % pour les
        déchets puis pour les plastiques.
    """
    waste_diff = pct_diff(result.new_waste_100m, result.hist_waste_100m)
    plastic_diff = pct_diff(result.new_plastic_100m, result.hist_plastic_100m)
    return waste_diff, plastic_diff


def site_average(readings: list[Reading], site_id, attribute="waste_100m"):
    """Calcule la moyenne d'une mesure (déchets ou plastiques) pour un seul site.

    Les relevés sans valeur pour cette mesure (``None``) sont ignorés.

    Args:
        readings: Relevés parmi lesquels chercher.
        site_id: Identifiant du site (ex. ``"S1"``).
        attribute: Nom de l'attribut à moyenner, ``"waste_100m"`` (par
            défaut) ou ``"plastic_100m"``.

    Returns:
        La moyenne de cette mesure pour les relevés de ce site.

    Raises:
        ZeroDivisionError: Si aucun relevé exploitable n'existe pour ce site.
    """
    total = 0
    count = 0
    for reading in readings:
        if reading.site_id != site_id:
            continue
        value = getattr(reading, attribute)
        if value is None:
            continue
        total += value
        count += 1
    return total / count


def plot(hist_readings: list[Reading], new_readings: list[Reading], sites: list[Site]):
    """Génère ``courbe.png`` : moyenne historique vs nouveaux relevés, par site.

    Diagramme en barres groupées : pour chaque site, une barre bleue pour la
    moyenne historique et une barre orange pour la moyenne des nouveaux
    relevés, avec la valeur affichée au-dessus de chaque barre.

    Args:
        hist_readings: Relevés déjà validés.
        new_readings: Nouveaux relevés.
        sites: Sites à afficher, dans l'ordre de l'axe horizontal.
    """
    names = [site.name for site in sites]
    hist_values = [site_average(hist_readings, site.site_id) for site in sites]
    new_values = [site_average(new_readings, site.site_id) for site in sites]
    positions = range(len(sites))

    figure, axis = plt.subplots(figsize=(9, 5))
    hist_bars = axis.bar([p - 0.2 for p in positions], hist_values, width=0.4,
                         label="Historique", color="#2a78d6")
    new_bars = axis.bar([p + 0.2 for p in positions], new_values, width=0.4,
                        label="Nouveaux relevés", color="#eb6834")
    axis.bar_label(hist_bars, fmt="%.0f")
    axis.bar_label(new_bars, fmt="%.0f")

    axis.set_xticks(positions, names)
    axis.set_title("Déchets moyens pour 100 m par site")
    axis.spines[["top", "right"]].set_visible(False)
    axis.legend(frameon=False)
    figure.tight_layout()
    figure.savefig("courbe.png")


def print_report(result: Result, reading_count):
    """Affiche le rapport de validation dans le terminal.

    Trois parties : le score de fiabilité de chaque relevé (avec le détail
    des points retirés), un tableau des moyennes historique / nouveaux avec
    l'écart en %, puis le nom du graphique généré.

    Args:
        result: Résultat renvoyé par ``averages()``.
        reading_count: Nombre total de nouveaux relevés analysés.
    """
    print("=== Validation des nouveaux relevés ===\n")

    print("Fiabilité des relevés :")
    for reading_id, score in result.scores.items():
        print(f"  {reading_id}  {score:>3} %  {reliability_label(score)}")
        for message in result.errors.get(reading_id, []):
            print(f"        - {message}")

    mean_score = sum(result.scores.values()) / reading_count
    print(f"\n  Fiabilité moyenne : {mean_score:.0f} %"
          f" ({len(result.errors)} relevé(s) sur {reading_count} avec au moins un problème)\n")

    waste_diff, plastic_diff = diffs(result)
    print("Moyennes pour 100 m :")
    print(f"  {'':<12}{'Historique':>12}{'Nouveaux':>12}{'Écart':>10}")
    print(f"  {'Déchets':<12}{result.hist_waste_100m:>12.1f}{result.new_waste_100m:>12.1f}{waste_diff:>+9.1f}%")
    print(f"  {'Plastiques':<12}{result.hist_plastic_100m:>12.1f}{result.new_plastic_100m:>12.1f}{plastic_diff:>+9.1f}%")

    print("\nGraphique enregistré dans courbe.png")


def validate(hist_path, new_path):
    """Lance toute la validation : chargement, calculs, graphique et affichage.

    Les erreurs trouvées dans les relevés sont affichées mais n'empêchent pas
    le calcul ni la génération du graphique.

    Args:
        hist_path: Chemin du fichier JSON de l'historique (sites + relevés).
        new_path: Chemin du fichier JSON des nouveaux relevés.

    Returns:
        ``True`` si le traitement est allé jusqu'au bout, ``False`` si un
        fichier est introuvable, n'est pas un JSON valide, ou si un site ne
        passe pas la vérification Pydantic.
    """
    try:
        # Chargement des nouveaux relevés : chaque dict JSON devient un Reading
        with open(new_path) as file:
            data = json.load(file)
            new_readings = []
            for reading in data["releves"]:
                new_readings.append(Reading(**reading))
        # Chargement de l'historique : les sites sont vérifiés par Pydantic ici
        with open(hist_path) as file:
            data = json.load(file)
            sites = []
            for site in data["sites"]:
                sites.append(Site(**site))
            hist_readings = []
            for reading in data["releves"]:
                hist_readings.append(Reading(**reading))
        result = averages(new_readings, hist_readings)
        plot(hist_readings, new_readings, sites)
        print_report(result, len(new_readings))
        return True
    except FileNotFoundError as error:
        print(f"Fichier introuvable : {error.filename}")
        return False
    except json.JSONDecodeError as error:
        print(f"JSON invalide : {error}")
        return False
    # Levée par Site(**site) si un site a un mauvais type ou des coordonnées hors limites
    except ValidationError as error:
        print(error)
        return False


if __name__ == "__main__":
    validate("valid_history.json", "new_readings.json")
