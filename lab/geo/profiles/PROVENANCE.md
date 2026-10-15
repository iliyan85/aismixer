# Illustrative Black Sea domain provenance

`black_sea_demo.yaml` uses representative settlement coordinates from Wikidata
property P625, read through the public Wikidata entity-data API on **2026-10-14**.
The selected entities are the requested villages/cities/towns, rather than
similarly named municipalities, lakes, airports or vessels. Every selected
coordinate identifies Earth (`Q2`). The table links pinned entity revisions and
preserves the original decimal values returned by the API.

This is a **coarse illustrative domain**, not a surveyed coast, water-only mask,
nautical chart or geographic accuracy qualification. A settlement's representative
point may be inland. Straight longitude/latitude edges can include land and
exclude actual water. Community records have varying precision and provenance;
reading a record verifies the source value, not its physical accuracy. In
particular, the Odesa claim records approximately 0.13-degree precision and has
no claim reference; Constanta's selected claim also has no reference. These
limitations are independent of the geometry engine's computational tests.

## Selection and conversion policy

For each pinned entity, discard deprecated P625 claims and claims without a
coordinate data value. If preferred-ranked claims exist, choose the first such
claim; otherwise choose the first remaining normal-ranked claim in entity JSON
order. Shabla, Kavarna and Mangalia have two eligible normal-ranked claims in
the retrieved records. The table records the selected values explicitly, so
later claim ordering or source edits cannot silently change this profile.

Both axes are rounded from decimal source values to **0.01 degree**, using
nearest rounding with exact half cases away from zero. They are then multiplied
by 1,000,000,000 and stored as integer nanodegrees. For example, Trabzon's
latitude 41.005 becomes 41.01. This rounding and integer representation do not
improve source accuracy. The source precision column is P625 metadata, not a
measured error bound or assurance that the point is on the shore.

All coordinate pairs below are explicitly **longitude, latitude**. The public
API uses named axes and the YAML declares `coordinate_order: longitude_latitude`;
it does not assume the native EPSG:4326 axis ordering. Durankulak is repeated
exactly as the final closing point after Mangalia. The ordered ring was checked
independently using integer arithmetic for distinct vertices, closure,
nondegenerate adjacent turns and nonadjacent intersections/touches.

| Landmark | Pinned source revision | Original longitude, latitude (degrees) | Profile longitude, latitude (degrees) | Source precision (degrees) |
| --- | --- | --- | --- | --- |
| Durankulak | [Q1266785, revision 2551815326](https://www.wikidata.org/w/index.php?title=Q1266785&oldid=2551815326) | 28.526237, 43.692929 | 28.53, 43.69 | 0.0000010 |
| Shabla | [Q406639, revision 2551891633](https://www.wikidata.org/w/index.php?title=Q406639&oldid=2551891633) | 28.53112, 43.535504 | 28.53, 43.54 | 0.0000010 |
| Kavarna | [Q405120, revision 2556601408](https://www.wikidata.org/w/index.php?title=Q405120&oldid=2556601408) | 28.339218, 43.435977 | 28.34, 43.44 | 0.0000010 |
| Balchik | [Q329437, revision 2556842875](https://www.wikidata.org/w/index.php?title=Q329437&oldid=2556842875) | 28.166152, 43.410166 | 28.17, 43.41 | 0.0000010 |
| Varna | [Q6506, revision 2551792827](https://www.wikidata.org/w/index.php?title=Q6506&oldid=2551792827) | 27.91108, 43.211375 | 27.91, 43.21 | 0.0000010 |
| Burgas | [Q6509, revision 2551788858](https://www.wikidata.org/w/index.php?title=Q6509&oldid=2551788858) | 27.470179, 42.502965 | 27.47, 42.50 | 0.0000010 |
| Istanbul | [Q406, revision 2556807961](https://www.wikidata.org/w/index.php?title=Q406&oldid=2556807961) | 28.960277777778, 41.01 | 28.96, 41.01 | 0.00027777777777778 |
| Trabzon | [Q45301, revision 2555595754](https://www.wikidata.org/w/index.php?title=Q45301&oldid=2555595754) | 39.7225, 41.005 | 39.72, 41.01 | 0.0002777777777777778 |
| Batumi | [Q25475, revision 2555696962](https://www.wikidata.org/w/index.php?title=Q25475&oldid=2555696962) | 41.641666666667, 41.645833333333 | 41.64, 41.65 | 0.00027777777777778 |
| Sochi | [Q39420, revision 2555039610](https://www.wikidata.org/w/index.php?title=Q39420&oldid=2555039610) | 39.720277777778, 43.585277777778 | 39.72, 43.59 | 0.00027777777777778 |
| Novorossiysk | [Q15760, revision 2535744954](https://www.wikidata.org/w/index.php?title=Q15760&oldid=2535744954) | 37.766666666667, 44.716666666667 | 37.77, 44.72 | 0.016666666666667 |
| Sevastopol | [Q7525, revision 2538649757](https://www.wikidata.org/w/index.php?title=Q7525&oldid=2538649757) | 33.5225, 44.605 | 33.52, 44.61 | 0.0002777777777777778 |
| Odesa | [Q1874, revision 2541860471](https://www.wikidata.org/w/index.php?title=Q1874&oldid=2541860471) | 30.73262, 46.47747 | 30.73, 46.48 | 0.13044778146083 |
| Sulina | [Q328647, revision 2530149311](https://www.wikidata.org/w/index.php?title=Q328647&oldid=2530149311) | 29.652777777778, 45.159444444444 | 29.65, 45.16 | 0.00027777777777778 |
| Constanta | [Q79808, revision 2554633083](https://www.wikidata.org/w/index.php?title=Q79808&oldid=2554633083) | 28.6507598, 44.1767161 | 28.65, 44.18 | 1.0E-7 |
| Mangalia | [Q467498, revision 2535847645](https://www.wikidata.org/w/index.php?title=Q467498&oldid=2535847645) | 28.582777777778, 43.817222222222 | 28.58, 43.82 | 0.00027777777777778 |

Entity JSON was read at `https://www.wikidata.org/wiki/Special:EntityData/QID.json`
with the QID substituted from the table. Source structured data is available
under [CC0, as described by Wikidata's licensing policy](https://www.wikidata.org/wiki/Wikidata:Licensing).
No geographic software, decoder code, dependency package or large capture was
downloaded. API responses were inspected in memory; this document records only
the selected facts, identifiers and provenance needed to reproduce the profile.

`scene_demo_overlay.yaml` and `receivers_demo.yaml` remain **synthetic** test
fixtures. Their anchors/passive antenna sites are not sourced real stations,
identity assignments or measured ellipsoidal heights. The receiver catalog
computes no RF coverage, propagation, reception or duplicate traffic.
