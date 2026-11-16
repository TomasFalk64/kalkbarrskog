"""Starta med: python -m streamlit run main.py

API-nyckel: ARTPORTALEN_API_KEY i .env, miljön eller sidomenyn.
Valbart årsintervall och arturval.
API-kontrakt: https://github.com/biodiversitydata-se/SOS
"""

from pathlib import Path
from zipfile import ZipFile, BadZipFile
import math
import json
import os
import re

import altair as alt
import geopandas as gpd
import pandas as pd
import pyogrio
import requests
import streamlit as st
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from shapely import make_valid
from shapely.geometry import mapping
from shapely.ops import unary_union


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env", override=False)
DATA_DIR = PROJECT_DIR / "data"
API_URL = "https://api.artdatabanken.se/species-observation-system/v1/observations/SearchByCursor"
SPATIAL_SUFFIXES = {".shp", ".geojson", ".json", ".gpkg"}
PAGE_SIZE = 1000
REDLIST_CATEGORIES = ["DD", "NT", "VU", "EN", "CR"]
REDLIST_COLORS = ["#87949e", "#e7ba52", "#e58a36", "#d45242", "#8f263c"]
DETAIL_COLUMNS = ["Område", "Artnamn", "Vetenskapligt namn", "Rödlistestatus", "Antal områden"]
ALL_SPECIES = "Alla arter"
CONSERVATION_SPECIES = "Naturvårdsarter (RL + signal + utvalda)"
# Samma arturval som Faltkarta
SELECTED_TAXON_IDS = [100001, 100048, 103031, 100109, 100100, 6001993,
                      100049, 2002091, 6006107, 6452, 4000105]
CONSERVATION_FILTERS = [
    ("Signalarter + utvalda taxa", {
        "ids": SELECTED_TAXON_IDS,
        "includeUnderlyingTaxa": True,
        "taxonListIds": [2],
        "taxonListOperator": "Merge",
    }),
    ("Rödlistade arter", {
        "redListCategories": ["DD", "NT", "VU", "EN", "CR", "RE"],
        "includeUnderlyingTaxa": True,
    }),
]


def discover_sources():
    """Lista filer och deras lager, utan att packa upp ZIP på disk."""
    sources, problems = {}, []
    for path in sorted(DATA_DIR.rglob("*")):
        if not path.is_file():
            continue
        label = path.relative_to(DATA_DIR).as_posix()
        candidates = []
        if path.suffix.lower() == ".zip":
            try:
                with ZipFile(path) as archive:
                    members = [n for n in archive.namelist()
                               if Path(n).suffix.lower() in SPATIAL_SUFFIXES
                               and not n.startswith("__MACOSX/")]
                if not members:
                    problems.append(f"{label}: inget spatialt dataset i arkivet.")
                for member in members:
                    candidates.append((f"{label} / {member}",
                                       f"/vsizip/{path.resolve().as_posix()}/{member}"))
            except (BadZipFile, OSError) as exc:
                problems.append(f"{label}: {exc}")
        elif path.suffix.lower() in SPATIAL_SUFFIXES:
            candidates.append((label, str(path.resolve())))
        layers = []
        failed = False
        for name, uri in candidates:
            try:
                for layer, kind in pyogrio.list_layers(uri):
                    if kind is not None:
                        layers.append((uri, str(layer)))
            except Exception as exc:
                failed = True
                problems.append(f"{name}: kunde inte läsa dataset ({exc}).")
        if layers and not failed:
            sources[label] = layers
    return sources, problems


def polygon_parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        return [part for child in geometry.geoms for part in polygon_parts(child)]
    return []


def load_polygons(sources, selected):
    parts = []
    for name in selected:
        file_parts = []
        for uri, layer in sources[name]:
            frame = gpd.read_file(uri, layer=layer)
            if frame.crs is None:
                raise ValueError(f"{name}: koordinatsystem saknas. Ange rätt CRS i GIS och spara filen igen.")
            frame = frame.to_crs(4326)
            for geometry in frame.geometry:
                if geometry is not None and not geometry.is_empty:
                    file_parts.extend(polygon_parts(make_valid(geometry)))
        if not file_parts:
            raise ValueError(f"{name}: innehåller inga polygoner.")
        parts.extend(file_parts)
    merged = unary_union(parts)
    xmin, ymin, xmax, ymax = merged.bounds
    if not all(math.isfinite(v) for v in merged.bounds) or not (
        -180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90
    ):
        raise ValueError("Polygonernas koordinater är ogiltiga efter omprojektering till WGS84.")
    return merged


def lower_keys(value):
    """SOS rekommenderar skiftlägesokänslig läsning av JSON-egenskaper."""
    if isinstance(value, dict):
        return {k.lower(): lower_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [lower_keys(v) for v in value]
    return value


def fetch_observations(geometry, api_key, progress=None, *, start_year=2026, end_year=2026,
                       taxon_filter=None):
    """Hämta varje sida; returnera aldrig ett tyst avklippt delresultat.

    Söker direkt i filens sammanslagna polygoner, inklusive hål. Taxonfilter
    är valfritt; ingen begränsning av koordinatnoggrannhet görs. Årsintervallet
    avser observationens startdatum, inte rapporteringsdatum. SOS söker
    offentliga fynd; skyddade fynd kräver en separat behörighetslösning.
    """
    if not 1 <= start_year <= end_year <= 9999:
        raise ValueError("Från år måste vara mindre än eller lika med Till år (1–9999).")
    search_filter = {
        "dataProvider": {"ids": [1]},
        "date": {
            "startDate": f"{start_year:04d}-01-01T00:00:00",
            "endDate": f"{end_year:04d}-12-31T23:59:59.9999999",
            "dateFilterType": "OnlyStartDate",
        },
        "geographics": {
            "geometries": [mapping(p) for p in polygon_parts(geometry)],
            "considerObservationAccuracy": False,
            "considerDisturbanceRadius": False,
        },
        "output": {"fields": [
            "occurrence.occurrenceId", "location.locationId", "location.locality",
            "location.decimalLatitude", "location.decimalLongitude",
            "taxon.id", "taxon.scientificName", "taxon.vernacularName",
            "taxon.attributes.redlistCategory",
            "occurrence.reportedBy", "occurrence.recordedBy",
        ]},
    }
    if taxon_filter is not None:
        search_filter["taxon"] = taxon_filter
    retry = Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset({"POST"}), respect_retry_after_header=True)
    records, seen_cursors = {}, set()
    cursor, expected_total = None, None
    with requests.Session() as session:
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update({
            "X-Api-Version": "1.5",
            "X-Requesting-System": "Kalkbarrskog-Streamlit",
            "Ocp-Apim-Subscription-Key": api_key,
        })
        while True:
            params = {"take": PAGE_SIZE, "sortBy": "modified", "sortOrder": "Asc",
                      "translationCultureCode": "sv-SE"}
            if cursor:
                params["cursor"] = cursor
            try:
                response = session.post(API_URL, params=params, json=search_filter, timeout=(15, 120))
            except requests.RequestException as exc:
                raise RuntimeError("Nätverksfel eller timeout mot SOS. Försök igen.") from exc
            if response.status_code in (401, 403):
                raise RuntimeError("API-nyckeln saknas, är ogiltig eller saknar SOS-behörighet.")
            if response.status_code == 404:
                raise RuntimeError("SearchByCursor är inte tillgänglig för denna API-prenumeration. Kontakta SLU:s API-support.")
            if not response.ok:
                raise RuntimeError(f"SOS svarade med HTTP {response.status_code}. "
                                   "Kontrollera API-prenumerationen och polygonen, eller försök igen senare.")
            try:
                payload = lower_keys(response.json())
                page = payload["records"]
                total = int(payload["totalcount"])
                next_cursor = payload.get("nextcursor")
                if not isinstance(page, list) or total < 0:
                    raise ValueError()
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError("SOS returnerade ett oväntat svarsformat.") from exc
            if total > 2_000_000:
                raise RuntimeError("Filen överstiger SOS-gränsen på 2 miljoner fynd. Dela upp filens polygoner i mindre områden.")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise RuntimeError("Datamängden ändrades under hämtningen. Kör sökningen igen.")
            before = len(records)
            for record in page:
                occurrence_id = (record.get("occurrence") or {}).get("occurrenceid")
                if not occurrence_id:
                    raise RuntimeError("En observation saknar ID; fullständighet kan inte verifieras.")
                records[str(occurrence_id)] = record
            if progress:
                progress(len(records), total)
            if not next_cursor:
                if len(records) != total:
                    raise RuntimeError(f"Ofullständigt API-svar: {len(records)} av {total} fynd. Försök igen.")
                break
            if next_cursor in seen_cursors or (page and len(records) == before) or not page:
                raise RuntimeError("Pagineringen stannade. Inget ofullständigt resultat visas; försök igen.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    return list(records.values())


def fetch_area_observations(geometry, api_key, species_mode, progress=None, *,
                            start_year=2026, end_year=2026):
    """Hämta hela arturvalet för en fil, utan dubbletter mellan filtren."""
    if species_mode == ALL_SPECIES:
        filters = [(ALL_SPECIES, None)]
    elif species_mode == CONSERVATION_SPECIES:
        filters = CONSERVATION_FILTERS
    else:
        raise ValueError("Okänt arturval.")
    unique = {}
    for label, taxon_filter in filters:
        def report(count, total):
            if progress:
                progress(label, count, total)
        try:
            records = fetch_observations(
                geometry, api_key, report, start_year=start_year,
                end_year=end_year, taxon_filter=taxon_filter,
            )
        except Exception as exc:
            raise RuntimeError(f"{label}: {exc}") from exc
        for record in records:
            unique[str(record["occurrence"]["occurrenceid"])] = record
    return list(unique.values())


def observations_table(records):
    rows = []
    for record in records:
        location, taxon = record.get("location") or {}, record.get("taxon") or {}
        location_id = location.get("locationid")
        name = location.get("locality") or "Namnlös lokal"
        lat, lon = location.get("decimallatitude"), location.get("decimallongitude")
        if location_id is not None:
            place_key = f"id:{location_id}"
        elif lat is not None and lon is not None:
            place_key = f"koordinat:{lat},{lon}"
        else:
            place_key = None  # Saknade lokaler räknas inte som en unik plats.
        taxon_id = taxon.get("id")
        species_key = str(taxon_id) if taxon_id is not None else taxon.get("scientificname")
        attributes = taxon.get("attributes") or {}
        occurrence = record.get("occurrence") or {}
        rows.append({"Område": record["Område"],
                     "Observations-ID": str(record["occurrence"]["occurrenceid"]),
                     "Lokal-ID": place_key, "Plats/lokal": name,
                     "Latitud": lat, "Longitud": lon,
                     "Taxon-ID": species_key,
                     "Artnamn": taxon.get("vernacularname") or "Uppgift saknas",
                     "Vetenskapligt namn": taxon.get("scientificname") or "Uppgift saknas",
                     "Rödlistestatus": attributes.get("redlistcategory") or "Uppgift saknas",
                     "Rapportör": occurrence.get("reportedby"),
                     "Observatörer": occurrence.get("recordedby")})
    return pd.DataFrame(rows, columns=[*DETAIL_COLUMNS[:-1], "Observations-ID", "Lokal-ID", "Plats/lokal", "Taxon-ID",
                                      "Rapportör", "Observatörer", "Latitud", "Longitud"])


def unique_people(values, split_names=False):
    """Räkna namn utan skillnader i skiftläge och extra blanksteg."""
    names = set()
    for value in values.dropna():
        if not isinstance(value, str):
            continue
        parts = re.split(r"[,;|\n]+", value) if split_names else [value]
        for part in parts:
            name = " ".join(part.split()).casefold()
            if name:
                names.add(name)
    return len(names)


def overview_statistics(frame, area_count):
    unique = frame.drop_duplicates("Observations-ID")
    categories = unique["Rödlistestatus"].str.strip().str.upper()
    redlisted = categories.isin(["DD", "NT", "VU", "EN", "CR"])
    threatened = categories.isin(["VU", "EN", "CR"])
    return {
        "Antal områden": area_count,
        "Antal områden med observationer": frame["Område"].nunique(),
        "Antal fynd": len(unique),
        "Antal arter": unique["Taxon-ID"].nunique(),
        "Antal rödlistade arter (DD, NT, VU, EN, CR)": unique.loc[redlisted, "Taxon-ID"].nunique(),
        "Antal hotade arter (VU, EN, CR)": unique.loc[threatened, "Taxon-ID"].nunique(),
        "Antal unika rapportörer": unique_people(unique["Rapportör"]),
        "Antal unika observatörer": unique_people(unique["Observatörer"], split_names=True),
    }


def species_details(frame):
    """Räkna distinkta områden per taxon inom det hämtade urvalet."""
    counts = frame.groupby("Taxon-ID")["Område"].nunique()
    details = frame.copy()
    # Okända taxa får ett tomt antal, inte ett gemensamt påhittat artantal.
    details["Antal områden"] = frame["Taxon-ID"].map(counts).astype("Int64")
    return details[DETAIL_COLUMNS]


def observations_csv(frame):
    """Exportera alla hämtade kolumner och områdestillhörigheter för Excel."""
    export = frame.copy()
    export["Antal områden"] = species_details(frame)["Antal områden"]
    return export.to_csv(index=False, sep=";").encode("utf-8-sig")


def area_names(sources):
    """Använd filstammen; särskilj filer med samma stam med deras sökväg."""
    stems = [Path(name).stem for name in sources]
    return {name: Path(name).stem if stems.count(Path(name).stem) == 1
            else f"{Path(name).stem} ({name})" for name in sources}


def area_statistics(frame, areas):
    # Även valda områden utan fynd ska synas med nollor.
    grouped = frame.groupby("Område").agg(**{
        "Antal observationer": ("Observations-ID", "nunique"),
        "Antal unika arter": ("Taxon-ID", "nunique"),
    })
    redlisted = frame["Rödlistestatus"].str.strip().str.upper().isin(["DD", "NT", "VU", "EN", "CR"])
    grouped["Antal rödlistade arter"] = (
        frame.loc[redlisted].groupby("Område")["Taxon-ID"].nunique()
        .reindex(grouped.index, fill_value=0)
    )
    return grouped.reindex(areas, fill_value=0).astype(int).rename_axis("Område").reset_index()


def observer_statistics(frame):
    pairs, labels = [], {}
    for _, row in frame.drop_duplicates("Observations-ID").dropna(subset=["Taxon-ID", "Observatörer"]).iterrows():
        if not isinstance(row["Observatörer"], str):
            continue
        for part in re.split(r"[,;|\n]+", row["Observatörer"]):
            name = " ".join(part.split())
            if name:
                key = name.casefold()
                labels.setdefault(key, name)
                pairs.append((key, row["Taxon-ID"]))
    pairs = pd.DataFrame(pairs, columns=["Person", "Taxon-ID"])
    counts = pairs.groupby("Person")["Taxon-ID"].nunique().rename("Antal arter").reset_index()
    counts["Observatör"] = counts["Person"].map(labels)
    return counts.sort_values(["Antal arter", "Observatör"], ascending=[False, True])


def redlisted_occurrences(frame):
    records = frame.dropna(subset=["Taxon-ID"]).copy()
    records["Rödlistestatus"] = records["Rödlistestatus"].str.strip().str.upper()
    return records.loc[records["Rödlistestatus"].isin(REDLIST_CATEGORIES)].drop_duplicates(["Område", "Taxon-ID"])


def chart_data(data):
    # Aggregerade tabeller används; stora matriser visas i sidor.
    # InlineData undviker Altairs standardgräns på 5 000 DataFrame-rader.
    return alt.InlineData(values=data.to_dict(orient="records"))


def show_charts(frame, areas, start_year, end_year, species_mode):
    st.subheader("Diagram")
    st.caption(f"År: {start_year}–{end_year} · Valda områden: {len(areas)} · {species_mode}")
    st.caption("Diagrammen visar rapporterade fynd. Skillnader kan också bero på hur mycket områdena har inventerats.")
    stats = area_statistics(frame, areas).sort_values(
        ["Antal unika arter", "Område"], ascending=[False, True])
    order = stats["Område"].tolist()
    height = max(180, len(areas) * 38)
    axis = alt.Axis(tickMinStep=1, format="d")
    red = redlisted_occurrences(frame)

    with st.expander("1. Arter och rödlistade arter per område", expanded=True):
        bars = stats.melt(id_vars="Område", value_vars=["Antal unika arter", "Antal rödlistade arter"],
                          var_name="Arturval", value_name="Antal arter")
        chart = alt.Chart(chart_data(bars)).mark_bar().encode(
            x=alt.X("Antal arter:Q", axis=axis),
            y=alt.Y("Område:N", sort=order, axis=alt.Axis(labelLimit=280)),
            yOffset="Arturval:N",
            color=alt.Color("Arturval:N", scale=alt.Scale(
                domain=["Antal unika arter", "Antal rödlistade arter"], range=["#387d73", "#c44c46"])),
            tooltip=["Område:N", "Arturval:N", "Antal arter:Q"],
        ).properties(height=height)
        st.altair_chart(chart, width="stretch")

    with st.expander("2. Unika arter per observatör", expanded=True):
        observers = observer_statistics(frame)
        st.caption("Varje namngiven observatör får arten tillgodoräknad en gång, även vid gemensamma fynd.")
        if observers.empty:
            st.info("Inga fynd med både taxon och namngiven observatör finns i urvalet.")
        else:
            limit = st.selectbox("Visa observatörer", ["20 främsta", "50 främsta", "Alla"], key="observer_limit")
            shown = observers if limit == "Alla" else observers.head(20 if limit == "20 främsta" else 50)
            st.caption(f"Visar {len(shown)} av {len(observers)} observatörer.")
            chart = alt.Chart(chart_data(shown)).mark_bar(color="#387d73").encode(
                x=alt.X("Antal arter:Q", axis=axis),
                y=alt.Y("Observatör:N", sort=shown["Observatör"].tolist(), axis=alt.Axis(labelLimit=280)),
                tooltip=["Observatör:N", "Antal arter:Q"],
            ).properties(height=max(180, len(shown) * 26))
            st.altair_chart(chart, width="stretch")

    with st.expander("3. Rödlistade arter per kategori och område", expanded=True):
        if red.empty:
            st.info("Inga rödlistade arter i kategorierna DD, NT, VU, EN eller CR finns i urvalet.")
        else:
            categories = red.groupby(["Område", "Rödlistestatus"])["Taxon-ID"].nunique()
            grid = pd.MultiIndex.from_product([areas, REDLIST_CATEGORIES], names=["Område", "Rödlistestatus"])
            counts = categories.reindex(grid, fill_value=0).rename("Antal arter").reset_index()
            counts["Kategoriordning"] = counts["Rödlistestatus"].map(dict(zip(REDLIST_CATEGORIES, range(5))))
            red_order = stats.sort_values(["Antal rödlistade arter", "Område"], ascending=[False, True])["Område"].tolist()
            chart = alt.Chart(chart_data(counts)).mark_bar().encode(
                x=alt.X("Antal arter:Q", stack="zero", axis=axis),
                y=alt.Y("Område:N", sort=red_order, axis=alt.Axis(labelLimit=280)),
                color=alt.Color("Rödlistestatus:N", scale=alt.Scale(domain=REDLIST_CATEGORIES, range=REDLIST_COLORS)),
                order="Kategoriordning:Q",
                tooltip=["Område:N", "Rödlistestatus:N", "Antal arter:Q"],
            ).properties(height=max(180, len(areas) * 28))
            st.altair_chart(chart, width="stretch")

    with st.expander("4. Antal fynd jämfört med antal arter", expanded=True):
        chart = alt.Chart(chart_data(stats)).mark_circle(size=130, opacity=0.8, color="#387d73").encode(
            x=alt.X("Antal observationer:Q", title="Antal fynd", axis=axis, scale=alt.Scale(zero=True)),
            y=alt.Y("Antal unika arter:Q", title="Antal arter", axis=axis, scale=alt.Scale(zero=True)),
            tooltip=["Område:N", "Antal observationer:Q", "Antal unika arter:Q", "Antal rödlistade arter:Q"],
        ).properties(height=360).interactive()
        st.altair_chart(chart, width="stretch")

    with st.expander("5. Färgmatris – rödlistade arter per område", expanded=True):
        if red.empty:
            st.info("Inga rödlistade arter i kategorierna DD, NT, VU, EN eller CR finns i urvalet.")
            return
        taxa = red.drop_duplicates("Taxon-ID").set_index("Taxon-ID")
        taxa["Antal områden"] = red.groupby("Taxon-ID")["Område"].nunique()
        taxa["Art"] = taxa["Artnamn"]
        taxa = taxa.sort_values(["Antal områden", "Art"], ascending=[False, True])
        pages = math.ceil(len(taxa) / 40)
        page = st.selectbox("Sida i färgmatrisen", list(range(1, pages + 1)),
                            format_func=lambda p: f"{p} av {pages}") if pages > 1 else 1
        shown = taxa.iloc[(page - 1) * 40:page * 40]
        st.caption(f"Visar arter {(page - 1) * 40 + 1}–{min(page * 40, len(taxa))} av {len(taxa)}. "
                   "Färg anger rödlistestatus där arten observerats. Grå ruta betyder inget rapporterat fynd i urvalet.")
        grid = pd.MultiIndex.from_product([shown.index, areas], names=["Taxon-ID", "Område"])
        matrix = red.set_index(["Taxon-ID", "Område"])["Rödlistestatus"].reindex(grid).fillna("Inget fynd").reset_index()
        matrix["Art"] = matrix["Taxon-ID"].map(shown["Art"])
        # Behåll separata rader även om olika taxa har samma svenska namn.
        matrix["Rad"] = matrix["Taxon-ID"].map({taxon_id: i for i, taxon_id in enumerate(shown.index)})
        chart = alt.Chart(chart_data(matrix)).mark_rect(stroke="white", strokeWidth=1).encode(
            x=alt.X("Område:N", sort=order, axis=alt.Axis(labelAngle=-45, labelLimit=180)),
            y=alt.Y("Rad:O", title="Art", sort=list(range(len(shown))), axis=alt.Axis(
                labelLimit=450, labelExpr=f"{json.dumps(shown['Art'].tolist())}[datum.value]")),
            color=alt.Color("Rödlistestatus:N", scale=alt.Scale(
                domain=["Inget fynd", *REDLIST_CATEGORIES], range=["#e8ecef", *REDLIST_COLORS])),
            tooltip=["Art:N", "Område:N", "Rödlistestatus:N"],
        ).properties(height=max(140, len(shown) * 26))
        st.altair_chart(chart, width="stretch")


def main():
    st.set_page_config(page_title="Artfynd i polygoner", layout="wide")
    st.title("Artfynd i projektet Upplands kalkbarrskogar")
    if not DATA_DIR.exists():
        st.info(f"Skapa mappen {DATA_DIR} och lägg dina polygonfiler där.")
        return
    sources, problems = discover_sources()
    if problems:
        with st.expander("Filer som inte kunde läsas"):
            for problem in problems:
                st.warning(problem)
    if not sources:
        st.warning("Inga läsbara spatiala filer hittades i data/.")
        return
    names = area_names(sources)
    with st.sidebar:
        st.header("Välj område")
        selected = st.multiselect("Polygonfiler", options=list(sources), format_func=names.get)
        species_mode = st.radio("Arturval", [ALL_SPECIES, CONSERVATION_SPECIES])
        if species_mode == CONSERVATION_SPECIES:
            st.caption("Rödlistade (DD, NT, VU, EN, CR, RE), Skogsstyrelsens signalarter "
                       "och utvalda taxa inklusive underliggande taxa, enligt Fältkarta-backend.")
            with st.expander("Utvalda taxon-ID:n"):
                st.write(", ".join(map(str, SELECTED_TAXON_IDS)))
        start_year = st.number_input("Från år", min_value=1, max_value=9999, value=2026, step=1)
        end_year = st.number_input("Till år", min_value=1, max_value=9999, value=2026, step=1)
        valid_years = start_year <= end_year
        if not valid_years:
            st.error("Från år får inte vara senare än Till år.")
        st.caption("Hela år inkluderas. Urvalet gäller observationens startdatum.")
        api_key = st.text_input("Artportalen / SOS API-nyckel", type="password",
                                value=os.environ.get("ARTPORTALEN_API_KEY", ""))
        st.caption("Nyckeln läses från .env eller miljövariabeln ARTPORTALEN_API_KEY. Du kan också ange den här.")
        run = st.button("Hämta samtliga artfynd", type="primary", disabled=not selected or not valid_years)
        download_slot = st.empty()
    # Förhindra att resultat från ett tidigare urval visas under ett nytt urval.
    file_stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                       for p in sorted(DATA_DIR.rglob("*")) if p.is_file())
    selection_key = ("per-file-v6", tuple(sorted(selected)), file_stamp, start_year, end_year, species_mode)
    if st.session_state.get("selection_key") != selection_key:
        st.session_state.pop("results", None)
        st.session_state["selection_key"] = selection_key
    if run:
        st.session_state.pop("results", None)
        try:
            if not api_key.strip():
                raise ValueError("Ange din SOS API-nyckel i sidomenyn.")
            with st.spinner("Läser polygoner och hämtar samtliga artfynd …"):
                status = st.empty()
                frames = []
                for index, filename in enumerate(selected, start=1):
                    area = names[filename]
                    status.caption(f"Område {index}/{len(selected)}: {area} – läser polygoner …")
                    try:
                        geometry = load_polygons(sources, [filename])
                        records = fetch_area_observations(
                            geometry, api_key.strip(), species_mode,
                            lambda label, count, total: status.caption(
                                f"Område {index}/{len(selected)}: {area} – {label}: hämtat {count:,} av {total:,} fynd"),
                            start_year=start_year, end_year=end_year,
                        )
                        frames.append(observations_table([
                            {**record, "Område": area} for record in records
                        ]))
                    except Exception as exc:
                        raise RuntimeError(f"{area}: {exc}") from exc
                st.session_state["results"] = pd.concat(frames, ignore_index=True)
                status.empty()
        except Exception as exc:
            st.error(f"Hämtningen avbröts: {exc}")
    frame = st.session_state.get("results")
    download_slot.download_button(
        "Ladda ner csv",
        data=observations_csv(frame) if frame is not None else b"",
        file_name=f"artfynd_{start_year}-{end_year}.csv",
        mime="text/csv",
        disabled=frame is None,
        on_click="ignore",
    )
    if frame is None:
        st.info("Välj en eller flera polygonfiler och klicka på Hämta samtliga artfynd.")
        return
    st.caption(f"{species_mode} · Resultat för {start_year}–{end_year} (hela år).")
    a, b, c, d = st.columns(4)
    unique = frame.drop_duplicates("Observations-ID")
    overview = overview_statistics(frame, len(selected))
    a.metric("Antal områden", overview["Antal områden"])
    b.metric("Totalt antal observationer", overview["Antal fynd"])
    c.metric("Antal unika rödlistade arter", overview["Antal rödlistade arter (DD, NT, VU, EN, CR)"])
    d.metric("Antal unika arter", overview["Antal arter"])
    with st.expander("Rapport översikt", expanded=True):
        st.caption(f"År: {start_year}–{end_year} · Valda områden: {len(selected)} · {species_mode}")
        st.dataframe(pd.DataFrame(overview.items(), columns=["Nyckeltal", "Antal"]),
                     hide_index=True, width="stretch")
        st.caption("Arter räknas som unika taxa. Rödlistade: DD, NT, VU, EN och CR. Hotade: VU, EN och CR. "
                   "Varje fynd räknas en gång i totalen. "
                   "Personantal avser unika namn; samma namn räknas som en person. "
                   "Observatörslistor delas vid komma, semikolon, lodstreck eller radbyte. Saknade namn räknas inte.")
        missing_redlist = int(unique["Rödlistestatus"].eq("Uppgift saknas").sum())
        if missing_redlist:
            st.caption(f"{missing_redlist} fynd saknar rödlistestatus i API-svaret och bidrar inte till antalet rödlistade eller hotade arter.")
    st.subheader("Statistik per område")
    st.dataframe(area_statistics(frame, [names[name] for name in selected]),
                 hide_index=True, width="stretch")
    if frame.empty:
        st.info("Inga fynd hittades inom de valda polygonerna.")
        return
    with st.expander("Artfynd"):
        st.caption(f"År: {start_year}–{end_year} · Valda områden: {len(selected)} · {species_mode}")
        st.dataframe(species_details(frame),
                     hide_index=True, width="stretch")
    if frame["Lokal-ID"].isna().any() or frame["Taxon-ID"].isna().any():
        st.warning("Fynd utan lokal- eller taxonuppgift ingår i totalantalet men inte i motsvarande unika nyckeltal.")
    show_charts(frame, [names[name] for name in selected], start_year, end_year, species_mode)


if __name__ == "__main__":
    main()
