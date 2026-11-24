# flight-deal-scheduler

Find cheap flights to countries you haven't visited and turn them into a dated travel schedule.
It uses **free APIs only**: no paywalled deal newsletters, no paid alert clubs, no scraping behind logins.

| Source | Used for | Cost | Key |
|---|---|---|---|
| [Travelpayouts / Aviasales Data API](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API) | "From my city to anywhere" fare discovery (prices other travellers found in the last ~48h) | Free | `TRAVELPAYOUTS_TOKEN` |
| [Duffel Flights API](https://duffel.com/docs) | Live re-pricing of the scheduled legs (`--verify`) | Free sign-up; 1500 free searches per booked order, then $0.005 per search | `DUFFEL_TOKEN` (optional) |
| [passport-index-dataset](https://github.com/ilyankou/passport-index-dataset) | Visa requirement per passport | Free | none |
| Travelpayouts `cities.json` / `countries.json` | City → country mapping | Free | none |

> Amadeus Self-Service closed on 17 July 2026, and Kiwi Tequila stopped self-serve sign-ups in 2024,
> so neither is used.

## Quick start

```bash
cd flight-deal-scheduler
python3 dealhunter.py --config config.example.json --demo   # try it offline with synthetic fares

cp config.example.json config.json                         # edit: home, passport, visited, dates, budget
export TRAVELPAYOUTS_TOKEN=...                             # travelpayouts.com > Profile > API token
python3 dealhunter.py                                      # separate round trips
python3 dealhunter.py --mode chain                         # one multi-country loop
export DUFFEL_TOKEN=duffel_live_...                        # optional
python3 dealhunter.py --verify                             # re-price scheduled legs live
```

You only need Python 3.9 or later, with no extra packages.

## Output (`out/`)

- `itinerary.md`: the schedule, leg tables with visa status, Google Flights links, runner-up deals, and what the filters removed
- `deals.csv`: cheapest qualifying round trip per new country, for sorting in a spreadsheet
- `trips.ics`: one calendar event per trip, which you can import into Google, Apple or Outlook calendar
- `snapshot.json`: prices from the last run; the next run prints any price drops

## How it decides

1. **Discover:** for every month in your window, pull the cheapest cached fares from each home city to every destination.
2. **Filter:** drops countries you've visited or excluded, countries whose visa type you don't accept, trips with the wrong length, trips outside your dates and fares over your price cap.
3. **Board:** keeps the cheapest fare per new country.
4. **Schedule**
   - `roundtrip`: takes the cheapest new country first and keeps adding trips while they fit your budget, your trip count and your minimum gap between trips.
   - `chain`: goes home → cheapest new country → cheapest new country from there → … → home, staying `nights_per_stop` at each stop.
5. **Verify** (optional): Duffel live offer for each scheduled leg, with the change against the cached price.

## Pair it with the prompt

[`PROMPT.md`](PROMPT.md) is an OODA-loop prompt (Observe, Orient, Decide, Act). It has Claude run this tool, add season, advisory and ground-cost context, choose the plan, re-price it live and set up calendar events and price monitoring.

## Caveats

- Cached fares are what someone else saw recently, not a guaranteed price. Re-price before booking.
- The visa dataset is community-maintained. Confirm on the destination's official immigration site.
- Use IATA **city** codes for home (`LON`, `NYC`, `LOS`), not airport codes.
