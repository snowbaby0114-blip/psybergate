# EMGuidance formulary and dispensing

Needs Docker with Compose v2. Nothing else has to be installed to run, seed or test it.

## Run

```bash
docker compose up --build
```

| | |
|---|---|
| Web app | http://localhost:8080 |
| API and Swagger UI | http://localhost:8000/api/docs |

The first build takes a few minutes. The database starts first and the API applies the Alembic
migrations itself before it starts listening, so there are no manual steps.
`docker compose down` stops everything; add `-v` to delete the database as well.

## Seed

In a second terminal, once the stack is up:

```bash
docker compose exec api python -m app.seed
```

This loads 504 medicines (500 generated and 4 demo ones) with 3 to 5 rule periods each, 6,010
dispenses for 2,501 patient references over two years, and prints a summary and a self-check. It takes
about two seconds. A second run on a database that already holds data changes nothing; to wipe
and reseed use `docker compose exec api python -m app.seed --reset`. Options: `--medicines`,
`--dispenses`, `--patients`, `--years`, `--random-seed`.

Demo data to try in the web app:

| | |
|---|---|
| `DEMO-AUTH` | needs an authorisation reference |
| `DEMO-TIGHT` | 10 per dispense, 20 per 30 days; patient `PT-DEMO` has used 18 of the 20 |
| `DEMO-INACTIVE` | inactive, every dispense is rejected |
| `DEMO-HISTORY` | 5 rule periods: 3 expired, 1 in force, 1 scheduled |

## Test

```bash
docker compose run --rm api pytest
```

Runs the whole backend suite (about 1,400 tests: unit, database and migrations, API, concurrency,
seed, query plans) inside the container against the compose database; it creates and drops its own
`emguidance_test` database. Useful subsets: `pytest tests/concurrency`, `pytest tests/db`.

Frontend unit tests (headless, no browser needed):

```bash
docker build --target build -t emg-web-build ./frontend
docker run --rm emg-web-build npm test
```
