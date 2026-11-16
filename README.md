# cat metro station timetables

The iOS app downloads one package for its default city. This repository contains public transit data, source references and the scripts used to prepare the packages.

## Sources

- London Underground stations and scheduled stop times are powered by TfL Open Data. Services come from the directed station timetable API. Each departure carries the stop sequence for its particular journey. Bank holidays in England and Wales come from GOV.UK. [TfL Open Data](https://tfl.gov.uk/info-for/open-data-users/) · [TfL data terms](https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service) · [UK bank holidays](https://www.gov.uk/bank-holidays.json)
- Kyoto City Subway schedules use the municipal transport bureau’s weekday, weekend/holiday and applicable special timetables. The package records its sources. [Kyoto City open data](https://www2.city.kyoto.lg.jp/kotsu/webguide/ja/comm/opendata.html)
- Tokyo’s four Toei Subway line schedules are adapted from Mini Tokyo 3D train data. The original data providers are the Bureau of Transportation, Tokyo Metropolitan Government and the Association for Open Data of Public Transportation. Tokyo Metro frequency estimates are identified separately from scheduled departures and use published service intervals. [Mini Tokyo 3D](https://github.com/nagix/mini-tokyo-3d) · [Toei timetable data](https://ckan.odpt.org/dataset/r_train_timetable-toei) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
