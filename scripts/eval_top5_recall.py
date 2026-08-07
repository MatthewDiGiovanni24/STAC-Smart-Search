"""Measure top-5 recall of the RemoteCLIP semantic ranking vs. a no-ranking baseline.

This is a controlled, reproducible retrieval benchmark. We label a pool of
Earth-observation collections by topic, then for each natural-language query
compare two rankings:

  * semantic — collections ordered by RemoteCLIP cosine similarity (the same
    ordering pgvector's `<=>` cosine operator produces in production);
  * baseline — arbitrary catalog order (what a federated fan-out returns without
    the semantic pre-filter/rerank), estimated as the expected recall over many
    random orderings.

recall@5 for a query = (# of that topic's collections appearing in the top 5) /
(total collections of that topic). We report the mean across queries, the
absolute gain (percentage points), and the relative improvement.

    python scripts/eval_top5_recall.py

Honesty notes: the absolute number depends on the benchmark composition (pool
size, relevants-per-query, how confusable the distractors are) — all printed
below so the result is inspectable. It measures ranking quality on labeled data,
not real user-query relevance.
"""

import random

import numpy as np

from app.services.embeddings import embed_query, embed_texts

K = 5  # top-K
RANDOM_TRIALS = 20000
SEED = 12345

# Labeled corpus: (id, topic, "title. description"). This is a DELIBERATELY HARD
# benchmark: (1) several topics have MORE than K=5 relevant collections, so a
# perfect recall@5 is impossible (ceiling = 5/R < 1); (2) confusable distractors
# share vocabulary with a target topic (precipitation~flood, glacier~sea_ice,
# urban land cover~nightlights, ocean color~water, drought~soil_moisture) to
# create genuine ranking errors. Topic "distractor" is never a query answer.
CORPUS: list[tuple[str, str, str]] = [
    # --- flood (7 relevant > K) ---
    ("flood_a", "flood", "Flood Inundation Extent. Surface water and flood inundation mapping from optical and SAR imagery after storms and hurricanes."),
    ("flood_b", "flood", "Global Surface Water. Seasonal and permanent surface water occurrence, change, and flood extent from Landsat."),
    ("flood_c", "flood", "Sentinel-1 Flood Mapping. Radar-based detection of standing floodwater and inundated land for disaster response."),
    ("flood_d", "flood", "Riverine Flood Hazard. Modeled river flooding depth and inundation footprints across watersheds."),
    ("flood_e", "flood", "Coastal Storm Surge. Storm surge flooding and coastal inundation depth from tropical cyclones."),
    ("flood_f", "flood", "Flash Flood Guidance. Rainfall-driven flash flood potential and inundated area estimates."),
    ("flood_g", "flood", "Wetland Water Extent. Open water and flooded vegetation extent in wetlands and floodplains."),
    # --- wildfire (6 relevant > K) ---
    ("fire_a", "wildfire", "Active Fire Detections. Thermal anomalies and active fire hotspots detected from MODIS and VIIRS."),
    ("fire_b", "wildfire", "Burned Area Product. Monthly burned area and fire scar extent from satellite surface reflectance."),
    ("fire_c", "wildfire", "Fire Radiative Power. Fire intensity and radiative power for wildfire monitoring and smoke emissions."),
    ("fire_d", "wildfire", "Post-fire Burn Severity. Normalized burn ratio and burn severity classification after wildfires."),
    ("fire_e", "wildfire", "Wildfire Smoke Plumes. Smoke aerosol dispersion and plume extent from active wildfires."),
    ("fire_f", "wildfire", "Fuel Moisture Content. Live and dead vegetation fuel moisture for wildfire danger rating."),
    # --- sea ice (6 relevant > K) ---
    ("ice_a", "sea_ice", "Arctic Sea Ice Concentration. Daily polar sea ice concentration and extent from passive microwave sensors."),
    ("ice_b", "sea_ice", "Sea Ice Thickness. Satellite altimetry estimates of Arctic and Antarctic sea ice thickness and freeboard."),
    ("ice_c", "sea_ice", "Sea Ice Drift. Motion and drift vectors of polar pack ice from scatterometer and radiometer data."),
    ("ice_d", "sea_ice", "Sea Ice Age. Multi-year versus first-year sea ice age classification for polar oceans."),
    ("ice_e", "sea_ice", "Sea Ice Edge. Marginal ice zone and sea ice edge location from microwave brightness temperature."),
    ("ice_f", "sea_ice", "Polynya Detection. Open water polynyas within polar sea ice cover."),
    # --- vegetation (6 relevant > K) ---
    ("veg_a", "vegetation", "Vegetation Index NDVI. Normalized difference vegetation index for greenness and canopy health monitoring."),
    ("veg_b", "vegetation", "Leaf Area Index. Canopy leaf area index and fraction of absorbed photosynthetically active radiation."),
    ("veg_c", "vegetation", "Gross Primary Productivity. Terrestrial vegetation productivity and carbon uptake from reflectance."),
    ("veg_d", "vegetation", "Crop Phenology. Agricultural crop growth stages and seasonal vegetation dynamics."),
    ("veg_e", "vegetation", "Enhanced Vegetation Index. EVI greenness for dense canopy vegetation monitoring."),
    ("veg_f", "vegetation", "Chlorophyll Fluorescence. Solar-induced chlorophyll fluorescence of terrestrial vegetation."),
    # --- elevation (4 relevant) ---
    ("dem_a", "elevation", "Digital Elevation Model. Global terrain elevation and topography from radar interferometry."),
    ("dem_b", "elevation", "Copernicus DEM. High-resolution digital surface model of ground and terrain heights worldwide."),
    ("dem_c", "elevation", "Slope and Aspect. Derived terrain slope, aspect, and hillshade from elevation data."),
    ("dem_d", "elevation", "Bathymetry and Topography. Combined land elevation and seafloor bathymetric relief."),
    # --- nighttime lights (4 relevant) ---
    ("ntl_a", "nightlights", "Nighttime Lights. VIIRS day/night band radiance of city lights and human settlement activity."),
    ("ntl_b", "nightlights", "Electrification Mapping. Nighttime luminosity as a proxy for electricity access and urban growth."),
    ("ntl_c", "nightlights", "Light Pollution. Artificial sky brightness and nocturnal light emissions from urban areas."),
    ("ntl_d", "nightlights", "Gas Flaring Detection. Nighttime infrared detection of industrial gas flares."),
    # --- soil moisture (4 relevant) ---
    ("sm_a", "soil_moisture", "Soil Moisture Active Passive. Surface and root-zone soil moisture and land surface wetness."),
    ("sm_b", "soil_moisture", "Root Zone Soil Moisture. Modeled subsurface soil water content for drought and agriculture."),
    ("sm_c", "soil_moisture", "Land Surface Wetness. Microwave retrievals of near-surface soil water and freeze/thaw state."),
    ("sm_d", "soil_moisture", "Surface Soil Moisture Anomaly. Anomalies of topsoil water content relative to climatology."),
    # --- air quality (4 relevant) ---
    ("aq_a", "air_quality", "Aerosol Optical Depth. Atmospheric aerosol loading and air quality from satellite radiometry."),
    ("aq_b", "air_quality", "Nitrogen Dioxide Column. Tropospheric NO2 concentrations for air pollution monitoring."),
    ("aq_c", "air_quality", "Particulate Matter. Fine particulate matter PM2.5 estimates for air quality assessment."),
    ("aq_d", "air_quality", "Carbon Monoxide. Tropospheric carbon monoxide columns for air pollution and combustion."),
    # --- distractors (never a query answer; share vocabulary with a target topic) ---
    ("dist_a", "distractor", "Precipitation Rate. Rainfall and precipitation accumulation from satellite and gauge data."),
    ("dist_b", "distractor", "Snow Cover Extent. Terrestrial snow cover fraction and snow water equivalent on land."),
    ("dist_c", "distractor", "Glacier Ice Velocity. Land ice glacier flow speed and mass balance in mountain regions."),
    ("dist_d", "distractor", "Ocean Color Chlorophyll. Ocean surface chlorophyll concentration and water clarity."),
    ("dist_e", "distractor", "Urban Land Cover. Impervious surfaces and built-up urban land cover classification."),
    ("dist_f", "distractor", "Drought Index. Agricultural and meteorological drought severity indices."),
    ("dist_g", "distractor", "Land Surface Temperature. Daytime and nighttime land surface skin temperature."),
    ("dist_h", "distractor", "Sea Surface Temperature. Ocean surface water temperature from thermal infrared."),
]

QUERIES: dict[str, str] = {
    "flood": "flooding, inundation, and surface water extent after storms",
    "wildfire": "active wildfire detection and burned area mapping",
    "sea_ice": "polar sea ice concentration and extent",
    "vegetation": "vegetation health and greenness NDVI monitoring",
    "elevation": "digital elevation models and terrain topography",
    "nightlights": "nighttime city lights and human settlement activity",
    "soil_moisture": "soil moisture and land surface wetness",
    "air_quality": "atmospheric aerosols and air quality",
}


def recall_at_k(top_ids: list[str], relevant: set[str]) -> float:
    hits = sum(1 for i in top_ids if i in relevant)
    return hits / len(relevant)


def main() -> None:
    rng = random.Random(SEED)
    ids = [c[0] for c in CORPUS]
    topics = [c[1] for c in CORPUS]
    texts = [c[2] for c in CORPUS]
    n = len(CORPUS)

    print(f"Corpus: {n} collections across {len(QUERIES)} topics; top-K = {K}\n")

    doc_vecs = np.asarray(embed_texts(texts), dtype=float)  # (n, 512), L2-normalized

    sem_recalls, base_recalls = [], []
    print(f"{'query topic':<15}{'relevant':>9}{'recall@5 semantic':>20}{'recall@5 baseline':>20}")
    print("-" * 64)
    for topic, query in QUERIES.items():
        relevant = {ids[i] for i in range(n) if topics[i] == topic}
        qv = np.asarray(embed_query(query), dtype=float)

        # Semantic ranking: cosine == dot (normalized). Same order as pgvector <=>.
        order = np.argsort(-(doc_vecs @ qv))
        top_ids = [ids[i] for i in order[:K]]
        sem = recall_at_k(top_ids, relevant)

        # Baseline: expected recall@5 over random orderings.
        base_trials = []
        for _ in range(RANDOM_TRIALS):
            picks = rng.sample(ids, K)
            base_trials.append(recall_at_k(picks, relevant))
        base = float(np.mean(base_trials))

        sem_recalls.append(sem)
        base_recalls.append(base)
        print(f"{topic:<15}{len(relevant):>9}{sem:>20.3f}{base:>20.3f}")

    sem_mean = float(np.mean(sem_recalls))
    base_mean = float(np.mean(base_recalls))
    abs_gain = (sem_mean - base_mean) * 100
    rel_gain = (sem_mean - base_mean) / base_mean * 100

    print("-" * 64)
    print(f"{'MEAN':<15}{'':>9}{sem_mean:>20.3f}{base_mean:>20.3f}")
    print()
    print(f"Mean recall@5:  semantic = {sem_mean:.1%}   baseline = {base_mean:.1%}")
    print(f"Absolute gain:  +{abs_gain:.1f} percentage points")
    print(f"Relative improvement:  {rel_gain:.0f}%  ({sem_mean / base_mean:.1f}x)")


if __name__ == "__main__":
    main()
