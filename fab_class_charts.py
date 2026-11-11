# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "beautifulsoup4>=4.12",
#     "matplotlib>=3.8",
#     "requests>=2.31",
# ]
# ///
"""Chart Flesh and Blood adult heroes and Living Legend points per class.

Data sources:

* Hero classes and talents come from the JSON API behind
  https://fabtcg.com/hero-selection/ (``/api/fab/v2/heroes/``). That API also
  lists heroes that are not released yet (``is_released == "no"``), such as
  Azalea, Ace of Spades (Assassin Ranger).
* Living Legend points come from https://fabtcg.com/living-legend/: the
  "Living Legend Leaderboard" table (active heroes) and the "Living Legend
  Heroes" table (heroes that have reached Living Legend status).

A hero with two classes (e.g. Assassin Ranger) counts once towards *each*
class, both for hero counts and for LL points. Talents (Draconic, Chaos,
Elemental, ...) are never treated as classes. Young-only heroes are left out,
as are heroes that are legal in neither Classic Constructed nor Living Legend
(alternate forms such as Mortimer, Living Virus).

Outputs (charts in ``--out-dir``, CSV files in ``--data-dir``):

* ``heroes_per_class.png``: adult heroes per class, split into Living Legend,
  active and not yet released heroes.
* ``ll_points_per_class.png``: Living Legend points per class, split into
  Living Legend and active leaderboard heroes.
* ``heroes.csv``: one row per hero.
* ``class_summary.csv``: one row per class, with the numbers in the charts.
  With ``--readme``, this table is also written into the README.

Usage:
    uv run fab_class_charts.py [--out-dir charts] [--data-dir data] [--readme README.md] [--released-only]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402
import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

DUAL_CLASS_NOTE = "Heroes with two classes count once towards each class."
HEROES_API_URL = "https://fabtcg.com/api/fab/v2/heroes/?lang=en"
LIVING_LEGEND_URL = "https://fabtcg.com/living-legend/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# Mirrors BASE_CLASSES / VALID_TALENT_TOKENS in fablore's
# src/data/card_types_extract.py.
BASE_CLASSES = frozenset(
    {
        "adjudicator", "assassin", "bard", "brute", "generic", "guardian",
        "illusionist", "mechanologist", "merchant", "necromancer", "ninja",
        "pirate", "ranger", "runeblade", "shapeshifter", "thief", "warrior",
        "wizard",
    }
)
VALID_TALENT_TOKENS = frozenset(
    {
        "chaos", "draconic", "earth", "elemental", "ice", "light", "lightning",
        "mystic", "revered", "reviled", "royal", "shadow",
    }
)

# Chart palette (validated categorical slots 1-3) and chrome colours.
BLUE, ORANGE, GREEN = "#2a78d6", "#eb6834", "#1baf7a"
SERIES_COLORS = [BLUE, ORANGE, GREEN]
# Colour per hero group, shared by both charts.
GROUP_COLORS = {"Living Legend": GREEN, "Active": BLUE, "Not yet released": ORANGE}
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"


@dataclass
class Hero:
    slug: str
    name: str
    classes: list[str]
    talents: list[str]
    type_line: str  # talents and classes as printed, e.g. "Draconic Ninja"
    released: bool
    hero_type: str  # "adult", "young" or "both"
    constructed_legal: bool  # legal in Classic Constructed or Living Legend
    ll_points: int | None = None
    ll_status: str = ""  # "Living Legend", "Leaderboard" or ""


def normalise_name(value: str) -> str:
    """Reduce a hero name to lowercase letters and digits for matching."""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]", "", value.lower())


def split_tokens(values: list[str], allowed: frozenset[str]) -> list[str]:
    """Return title-cased tokens from ``values`` that are in ``allowed``."""
    seen: list[str] = []
    for value in values:
        for token in re.split(r"[\s,]+", value.lower()):
            if token in allowed and token.title() not in seen:
                seen.append(token.title())
    return seen


def fetch(url: str) -> requests.Response:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response


def load_heroes() -> list[Hero]:
    """Load every hero (released and unreleased) from the hero-selection API."""
    heroes: list[Hero] = []
    for raw in fetch(HEROES_API_URL).json()["heroes"]:
        # The API's class/talent arrays are sometimes incomplete (e.g. Frankie
        # has no class array but class_string "Necromancer"), so merge both.
        class_string = raw.get("class_string", "")
        # class_string comes first so that classes keep its printed order
        # (e.g. "Pirate Necromancer").
        classes = split_tokens([class_string] + raw.get("class", []), BASE_CLASSES)
        talents = split_tokens(raw.get("talent", []) + [class_string], VALID_TALENT_TOKENS)
        # The type line uses only the talents in class_string: the talent array
        # also lists the elements a hero uses (e.g. Bravo, Star of the Show is an
        # "Elemental Guardian" but has Earth, Ice and Lightning talents too).
        printed = split_tokens([class_string], BASE_CLASSES | VALID_TALENT_TOKENS)
        printed += [cls for cls in classes if cls not in printed]
        name = " ".join(part for part in (raw["title"], raw.get("subtitle", "")) if part.strip())
        heroes.append(
            Hero(
                slug=raw["slug"],
                name=unicodedata.normalize("NFC", name.strip()),
                classes=classes,
                talents=talents,
                type_line=" ".join(printed),
                released=raw.get("is_released") == "yes",
                hero_type=raw.get("type", ""),
                constructed_legal=any(
                    raw.get("format", {}).get(fmt, {}).get("status") == "legal"
                    for fmt in ("cc", "living-legend")
                ),
            )
        )
    return heroes


def find_table_after(soup: BeautifulSoup, heading_text: str):
    """Return the first <table> after the element whose text is ``heading_text``."""
    target = heading_text.casefold()
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "summary"]):
        if tag.get_text(strip=True).casefold() == target:
            table = tag.find_next("table")
            if table is not None:
                return table
    raise RuntimeError(f"No table found under heading {heading_text!r}")


def read_points_table(table) -> dict[str, int]:
    """Map hero name -> LL points from a table with 'Hero' and '...Points' columns."""
    headers = [th.get_text(strip=True).casefold() for th in table.find("thead").find_all("th")]
    hero_col = headers.index("hero")
    points_col = next(i for i, h in enumerate(headers) if "points" in h)
    points: dict[str, int] = {}
    for row in table.find("tbody").find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cells) <= max(hero_col, points_col):
            continue
        digits = re.sub(r"[^\d]", "", cells[points_col])
        if digits:
            points[cells[hero_col]] = int(digits)
    return points


def load_living_legend_points() -> dict[str, tuple[int, str]]:
    """Return {hero name: (points, status)} from the Living Legend page."""
    soup = BeautifulSoup(fetch(LIVING_LEGEND_URL).text, "html.parser")
    result: dict[str, tuple[int, str]] = {}
    for heading, status in (
        ("Living Legend Leaderboard", "Leaderboard"),
        ("Living Legend Heroes", "Living Legend"),
    ):
        for name, points in read_points_table(find_table_after(soup, heading)).items():
            result[name] = (points, status)
    return result


def attach_points(heroes: list[Hero], ll_points: dict[str, tuple[int, str]]) -> list[str]:
    """Set ``ll_points`` on each hero. Return LL names with no matching hero."""
    by_key = {normalise_name(hero.name): hero for hero in heroes}
    unmatched: list[str] = []
    for name, (points, status) in ll_points.items():
        hero = by_key.get(normalise_name(name))
        if hero is None:
            unmatched.append(name)
            continue
        hero.ll_points = points
        hero.ll_status = status
    return unmatched


def style_axes(ax, title: str, subtitle: str, xlabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=14, fontweight="bold", color=INK_PRIMARY, pad=24)
    # Offset in points, so the gap to the title is the same for any chart height.
    ax.annotate(
        subtitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, 6),
        textcoords="offset points", fontsize=9.5, color=INK_SECONDARY,
    )
    ax.set_xlabel(xlabel, color=INK_SECONDARY)
    ax.tick_params(colors=INK_SECONDARY, length=0)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
    ax.xaxis.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(BASELINE)


def stacked_barh(
    series: dict[str, dict[str, int]],
    title: str,
    subtitle: str,
    xlabel: str,
    path: Path,
    colors: list[str] | None = None,
) -> None:
    """Draw a horizontal stacked bar chart, classes sorted by total."""
    totals: Counter[str] = Counter()
    for values in series.values():
        totals.update(values)
    classes = [c for c, _ in sorted(totals.items(), key=lambda kv: (kv[1], kv[0]))]

    fig, ax = plt.subplots(figsize=(10, 0.42 * len(classes) + 1.8))
    left = [0] * len(classes)
    for color, (label, values) in zip(colors or SERIES_COLORS, series.items()):
        widths = [values.get(c, 0) for c in classes]
        ax.barh(
            classes, widths, left=left, height=0.62, label=label, color=color,
            edgecolor=SURFACE, linewidth=1.5,
        )
        left = [a + b for a, b in zip(left, widths)]

    pad = max(totals.values()) * 0.01
    for y, total in enumerate(left):
        ax.text(total + pad, y, f"{total:,}", va="center", fontsize=9, color=INK_PRIMARY)

    style_axes(ax, title, subtitle, xlabel)
    ax.set_xlim(0, max(totals.values()) * 1.08)
    ax.margins(y=0.01)
    if len(series) > 1:
        legend = ax.legend(loc="lower right", frameon=False, fontsize=9)
        for text in legend.get_texts():
            text.set_color(INK_SECONDARY)
    fig.text(
        0.99, 0.01, f"Data as of {date.today().isoformat()} · fabtcg.com",
        ha="right", va="bottom", fontsize=8.5, color=INK_MUTED,
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"Wrote {path}")


HERO_GROUPS = ("Living Legend", "Active", "Not yet released")


def hero_group(hero: Hero) -> str:
    """Return the chart group for ``hero``: one of :data:`HERO_GROUPS`."""
    if hero.ll_status == "Living Legend":
        return "Living Legend"
    return "Active" if hero.released else "Not yet released"


def count_heroes(heroes: list[Hero]) -> dict[str, Counter[str]]:
    """Count heroes per class, split into :data:`HERO_GROUPS`."""
    series: dict[str, Counter[str]] = defaultdict(Counter)
    for hero in heroes:
        for key in hero.classes:
            series[hero_group(hero)][key] += 1
    return series


def plot_heroes(heroes: list[Hero], path: Path) -> None:
    """Chart the number of heroes per class, split into :data:`HERO_GROUPS`."""
    status_series = count_heroes(heroes)
    legends = sum(hero.ll_status == "Living Legend" for hero in heroes)
    upcoming = sum(not hero.released for hero in heroes)
    stacked_barh(
        {
            label: status_series[k]
            for k, label in zip(HERO_GROUPS, ("Living Legend", "Active (released)", "Not yet released"))
            if k in status_series
        },
        "Adult heroes per class",
        f"{len(heroes)} adult heroes: {legends} Living Legends, {upcoming} not yet released. {DUAL_CLASS_NOTE}",
        "Number of heroes",
        path,
        colors=[GROUP_COLORS[k] for k in HERO_GROUPS if k in status_series],
    )


def plot_ll_points(heroes: list[Hero], path: Path) -> None:
    """Chart LL points per class, split into Living Legend and leaderboard heroes."""
    points_series: dict[str, Counter[str]] = defaultdict(Counter)
    for hero in heroes:
        if hero.ll_points:
            for key in hero.classes:
                points_series[hero.ll_status][key] += hero.ll_points
    total_points = sum(hero.ll_points or 0 for hero in heroes)
    groups = [
        (k, label, group)
        for k, label, group in (
            ("Living Legend", "Living Legend heroes", "Living Legend"),
            ("Leaderboard", "Active leaderboard heroes", "Active"),
        )
        if k in points_series
    ]
    stacked_barh(
        {label: points_series[k] for k, label, _ in groups},
        "Living Legend points per class (adult heroes)",
        f"{total_points:,} LL points. {DUAL_CLASS_NOTE}",
        "Living Legend points",
        path,
        colors=[GROUP_COLORS[group] for _, _, group in groups],
    )


def write_csv(heroes: list[Hero], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["slug", "name", "type_line", "classes", "talents", "released", "type", "ll_points", "ll_status"])
        for hero in sorted(heroes, key=lambda h: h.name.casefold()):
            writer.writerow(
                [
                    hero.slug, hero.name, hero.type_line, " ".join(hero.classes), " ".join(hero.talents),
                    "yes" if hero.released else "no", hero.hero_type,
                    "" if hero.ll_points is None else hero.ll_points,
                    hero.ll_status,
                ]
            )
    print(f"Wrote {path}")


def write_summary_csv(heroes: list[Hero], path: Path) -> None:
    """Write one row per class with the hero counts and LL points used in the charts."""
    rows: dict[str, Counter[str]] = defaultdict(Counter)
    for hero in heroes:
        for key in hero.classes:
            row = rows[key]
            group = hero_group(hero)
            row[f"heroes_{group.lower().replace(' ', '_')}"] += 1
            row["ll_points_total"] += hero.ll_points or 0
            if hero.ll_status == "Living Legend":
                row["ll_points_living_legend_heroes"] += hero.ll_points or 0
            elif hero.ll_status == "Leaderboard":
                row["ll_points_leaderboard_heroes"] += hero.ll_points or 0
    columns = [
        "heroes_living_legend", "heroes_active", "heroes_not_yet_released", "heroes_total",
        "ll_points_living_legend_heroes", "ll_points_leaderboard_heroes", "ll_points_total",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["class", *columns])
        for key in sorted(rows):
            row = rows[key]
            row["heroes_total"] = (
                row["heroes_living_legend"] + row["heroes_active"] + row["heroes_not_yet_released"]
            )
            writer.writerow([key, *(row[column] for column in columns)])
    print(f"Wrote {path}")


README_START = "<!-- class-summary:start -->"
README_END = "<!-- class-summary:end -->"
README_HEADERS = {
    "class": "Class",
    "heroes_living_legend": "LL heroes",
    "heroes_active": "Active heroes",
    "heroes_not_yet_released": "Not yet released",
    "heroes_total": "Total heroes",
    "ll_points_living_legend_heroes": "LL points (LL heroes)",
    "ll_points_leaderboard_heroes": "LL points (active heroes)",
    "ll_points_total": "Total LL points",
}


def update_readme(summary_csv: Path, readme: Path) -> None:
    """Replace the table between the README markers with ``summary_csv`` as Markdown."""
    with summary_csv.open(encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    header, body = rows[0], sorted(rows[1:], key=lambda row: -int(row[-1]))
    lines = [
        "| " + " | ".join(README_HEADERS.get(column, column) for column in header) + " |",
        "|" + "---|" + "---:|" * (len(header) - 1),
        *("| " + " | ".join(f"{int(v):,}" if v.isdigit() else v for v in row) + " |" for row in body),
    ]
    table = f"_Data as of {date.today().isoformat()}._\n\n" + "\n".join(lines)
    text = readme.read_text(encoding="utf-8")
    start, end = text.index(README_START) + len(README_START), text.index(README_END)
    readme.write_text(f"{text[:start]}\n{table}\n{text[end:]}", encoding="utf-8")
    print(f"Updated {readme}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out-dir", type=Path, default=Path("charts"), help="chart folder (default: charts)")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="CSV folder (default: data)")
    parser.add_argument("--readme", type=Path, help="README to update with the class summary table")
    parser.add_argument("--released-only", action="store_true", help="leave out heroes that are not released yet")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.data_dir.mkdir(parents=True, exist_ok=True)

    # Adult heroes only: the API marks each hero as "adult", "young" or "both".
    # Also drop alternate forms such as Mortimer, Living Virus, which the API
    # lists as separate heroes but which are legal in neither CC nor LL.
    heroes = [
        hero
        for hero in load_heroes()
        if hero.hero_type in ("adult", "both") and hero.constructed_legal
    ]
    if args.released_only:
        heroes = [hero for hero in heroes if hero.released]
    unmatched = attach_points(heroes, load_living_legend_points())

    for hero in heroes:
        if not hero.classes:
            print(f"Warning: no class found for {hero.name} ({hero.slug}); hero is not charted.", file=sys.stderr)
    for name in unmatched:
        print(f"Warning: LL page hero {name!r} does not match a hero-selection entry.", file=sys.stderr)

    plot_heroes(heroes, args.out_dir / "heroes_per_class.png")
    plot_ll_points(heroes, args.out_dir / "ll_points_per_class.png")
    write_csv(heroes, args.data_dir / "heroes.csv")
    write_summary_csv(heroes, args.data_dir / "class_summary.csv")
    if args.readme:
        update_readme(args.data_dir / "class_summary.csv", args.readme)
    return 0


if __name__ == "__main__":
    sys.exit(main())
