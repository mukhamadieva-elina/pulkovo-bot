# Pulkovo board samples

Public JSON rows retrieved on 2026-11-22 from the same feeds used by the website:

- https://pulkovoairport.ru/api/?type=arrival&when=0
- https://pulkovoairport.ru/api/?type=departure&when=0

Selected unchanged rows cover cancellations, codeshares, empty objects instead of
missing values, gates, baggage belts and estimated arrival after midnight.
Tests freeze their clock to the snapshot time. No passenger conversations or
credentials are included. These are test fixtures, never a production fallback.
