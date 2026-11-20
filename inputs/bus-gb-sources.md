# British and Northern Irish bus schedules

The 32 Great Britain city packages use the Department for Transport’s national GTFS feed. It includes services gathered by BODS and Traveline, including Welsh and Scottish services. The Belfast package uses the National Transport Authority’s Northern Ireland feed. A route is included with all its published boarding stops when it serves at least one boarding stop in the city’s search area. These areas are bounding rectangles derived from the ONS May 2026 local authority districts and include a small buffer for boundary roads. Greater London includes all 32 boroughs and the City of London. [BODS implementation and attribution guidance](https://www.gov.uk/government/publications/bus-open-data-implementation-guide/bus-open-data-implementation-guide) · [National GTFS](https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/all/) · [ONS boundaries](https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_May_2026_Boundaries_UK_BUC/FeatureServer) · [NTA schedules and licence](https://www.transportforireland.ie/transitData/PT_Data.html)

The stop coordinates come from the schedule. The arrow uses the active NaPTAN record’s compass bearing where available. For stops without a recorded bearing, the builder checks departures to the next stop and retains a direction only when the observed directions agree. Distinct boarding points keep their own identifiers. Contains public sector information licensed under the Open Government Licence v3.0. London includes TfL Open Data. Northern Ireland schedule data is attributed to the National Transport Authority under CC BY 4.0. The published files are derived from these sources. [NaPTAN](https://naptan.api.dft.gov.uk/) · [Open Government Licence](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

The builder retains the operators’ Monday–Sunday calendars, calendar exceptions, stop-specific destinations, departures after midnight and frequency windows over a rolling 90-day horizon. A service calendar must explicitly authorize a departure. Scheduled and frequency-based calls retain their separate accuracy flags. The SQLite packages contain shared clock offsets to reduce download size.

Run the following with Python 3.9 or later. Use a fresh cache directory when fetching a newer source snapshot.

```sh
python3 -m pip install duckdb==1.4.3
python3 tools/build_british_bus_packs.py --cache /tmp/uk-bus-feed --output /tmp/uk-bus-packages
```
