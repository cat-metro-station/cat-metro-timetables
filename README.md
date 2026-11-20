# cat metro station timetables

The iOS app downloads one package for its default city. This repository contains public transit data, source references and the scripts used to prepare the packages.

## Offline bus maps and schedules

[City coverage, operator credits and known gaps](packages/bus/COVERAGE.md) list the downloadable bus networks. The [bus manifest](packages/bus/manifest.json) gives each city's expiry dates, compressed size, SHA-256 digest and immutable GitHub Release asset URL.

The bus workflow retrieves operator GTFS daily. It preserves local service dates, additions, cancellations and after-midnight stop times. A route and its stop coordinates must come from a published feed. The arrow at a boarding point follows reported adjacent stops or an official stop bearing. Frequency-based arrivals use the published interval. If a refresh fails for one city, its prior verified download remains accessible.

## Sources

- The 272 London Underground station boards and their scheduled stop times are powered by TfL Open Data. Services come from the directed station timetable API. Each departure carries the stop sequence for its particular journey. Bank holidays in England and Wales come from GOV.UK. [TfL Open Data](https://tfl.gov.uk/info-for/open-data-users/) · [TfL data terms](https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service) · [UK bank holidays](https://www.gov.uk/bank-holidays.json)
- Kyoto City Subway schedules use the municipal transport bureau’s weekday, weekend/holiday and applicable special timetables. The package records its sources. [Kyoto City open data](https://www2.city.kyoto.lg.jp/kotsu/webguide/ja/comm/opendata.html)
- Tokyo’s four Toei Subway line schedules are adapted from Mini Tokyo 3D train data. The original data providers are the Bureau of Transportation, Tokyo Metropolitan Government and the Association for Open Data of Public Transportation. Across both operators, 198 of the app’s 216 Tokyo stations have an offline board. All nine Tokyo Metro lines have daytime estimates for the sections, directions and stops listed in the package. These estimates use reviewed service intervals. The F Liner includes only its documented express stops. The daily job refreshes Toei runs and holidays. Changes to the summarized Metro frequency bands require a review of the linked operator sources. [Mini Tokyo 3D](https://github.com/nagix/mini-tokyo-3d) · [Toei timetable data](https://ckan.odpt.org/dataset/r_train_timetable-toei) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- New York City Subway and Los Angeles Metro Rail use the operators’ GTFS schedules, stop sequences, boarding restrictions, service dates and dated exceptions. [MTA schedules](https://www.mta.info/developers) · [LA Metro Rail GTFS](https://gitlab.com/LACMTA/gtfs_rail)
- Beijing departures are estimates adapted from BoyInTheSun’s Beijing subway schedule and Mick235711’s Beijing Subway Tools. This package adapts station identifiers, train stop patterns and working-day/weekend departures. The Beijing package is distributed under CC BY-SA 4.0. [Beijing subway schedule](https://github.com/BoyInTheSun/beijing-subway-schedule) · [Beijing Subway Tools](https://github.com/Mick235711/Beijing-Subway-Tools) · [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
- Shanghai includes first and last trains at 412 of the app’s 413 stations, using station and terminal facts compiled in OpenMetro. On Line 2 the app also estimates daytime departures from 11:00 to 15:00 using the operator’s weekday and weekend intervals. The package also includes the published Line 2 late train from Hongqiao Railway Station and the Line 16 weekday express, with their confirmed stopping patterns. Other station records list the first and last train to its published terminus. No published record is available for Longju Road. The source revision is recorded in the city package. [Shanghai Metro](https://m.shmetro.com/workspace/shmetrotest/view_lnfltime.aspx) · [OpenMetro](https://github.com/Naptie/openmetro) · [OpenMetro license](https://github.com/Naptie/openmetro/blob/main/LICENSE)
- The Beijing and Shanghai station matching tables include adapted names and identifiers from the app’s network data, which incorporates © OpenStreetMap contributors. [OpenStreetMap attribution](https://www.openstreetmap.org/copyright) · [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)
- Japan’s national holiday calendar comes from the Cabinet Office. [National holidays](https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html)

## Regional Japanese rail

The app downloads verified operator revisions for eight Japanese city networks. The daily workflow checks GTFS for Kumamoto City Tram, Okayama Electric Tramway and Naha Yui Rail. Sendai, Kobe, Fukuoka, Sapporo and Nagoya use operator timetable editions reviewed before publication. A replacement keeps its published service period and calendars. Each timetable includes the original provider, source and attribution. [Japanese timetable index](packages/japan-regional-index.json) · [Station data credits](inputs/japan-rail-stations.json)

## Regional UK and US rail

The regional index lists the source date, last service date and SHA-256 digest for each of 32 UK and 46 US city packages. The app fetches the index, then downloads only a changed default-city package. The digest in its URL identifies the exact bytes. A previously downloaded package stays usable offline if a source is unavailable. [Regional timetable index](packages/regional-index.json)

The daily regional workflow rebuilds schedules from the sources listed in each package. The publisher validates weekday calendars and exceptions, station identifiers, lines, boarding restrictions and chronological stop times against the released maps. A failed city keeps its previous package. The workflow reports failures while publishing other cities that passed. Source agencies set the service dates. The Las Vegas Monorail uses estimated departures derived from published headways. [Regional workflow](.github/workflows/regional.yml) · [UK timetable sources and conversions](inputs/regional-uk-sources.md)

The US generator reads official agency GTFS feeds and current public mirrors using the station and route IDs in the regional catalog. Each derived package credits its providers and carries the service dates used. When a ZIP contains expired rail service, the generator tries the configured alternative sources. A multi-operator city can have different expiry dates for each provider. Las Vegas estimates retain a short review window for the operator’s hours and dated closure notices. [US timetable sources and station mappings](inputs/us-regional-rail.json) · [US National Transit Map](https://www.bts.gov/national-transit-map)

To rebuild and check locally, use a scratch directory for input archives and generated candidates.

```sh
python3 tools/refresh_regional_uk.py --cache /tmp/cat-metro/uk --output /tmp/cat-metro/candidates
python3 tools/refresh_regional_us.py --cache /tmp/cat-metro/us --output /tmp/cat-metro/candidates
python3 tools/publish_regional.py --candidates /tmp/cat-metro/candidates
python3 tools/verify_regional.py
python3 -m unittest discover -s tests
```

## Regenerating the packages

The GitHub Actions workflow refreshes sources daily. A failed city update preserves the previous published file. Station maps in `inputs/` contain the identifiers needed to match the providers’ stops. Each package carries the attribution and service calendars for that dataset.

```sh
python3 tools/build_city_timetables.py --city newYork --network-dir inputs --output-dir packages
python3 tools/refresh_japan_packages.py --packages packages
python3 tools/generate_london_timetable.py --network inputs/london-routes.json --output packages/offline-london.json
```
