# FHIR R4 Validator (pure Python): Documentation

## Match with the Java validator

This service is a pure-Python FastAPI replacement for the Java `inferno-framework/fhir-validator-wrapper`
(HAPI 6.5.18, wrapper 2.3.6). The same resources were sent to both services and the answers compared.

| What was compared | Cases | Same "has errors?" answer | Identical errors (same path and message) |
|---|---|---|---|
| All official HL7 R4 example resources | 2,793 | **98.0%** | **97.4%** |
| US Core 3.1.1 examples plus corrupted copies | 877 | **99.9%** | **98.1%** |

| Endpoint | Match with Java |
|---|---|
| `GET /resources` (147 resource types) | 100% identical |
| `GET /profiles` (656 profiles) | 100% identical |
| `GET /igs` (1,491 entries) | 100% identical |
| `GET /version` | 100% identical |
| `POST /evaluate` (49 expressions) | 98% (48 of 49; the other one is a Java 500) |

**In one line: about 97 to 98% of the results are the same as the Java validator.**

Both services were run without a terminology server so the comparison does not depend on server state. How
this was measured, and the remaining differences, are described in the last two sections.

## Resource use

| | Java validator | This service |
|---|---|---|
| Memory | JVM heap (about 1 GB; 929 MB measured here) | about 190 MiB |
| Startup | seconds to a minute | about 3 s |
| Docker image | 755 MB | 431 MB |

## What it does

- **Validates FHIR R4 resources** (`POST /validate`, JSON or XML) against the base definitions and any requested
  profile, plus any profile named in `meta.profile`.
- **Checks**: unknown properties, JSON/XML shape, data type formats, cardinality, choice types, nested and recursive
  structures, fixed and pattern values, slicing, FHIRPath invariants, terminology bindings, reference target types,
  contained resources, Bundles, narrative XHTML, extensions (context and value type).
- **Applies the vital-signs profiles automatically** by LOINC code (for example `bp` for 85354-9), as Java does.
- **Loads Implementation Guides** by NPM id and version from `packages.fhir.org` (with dependencies, cached on
  disk), from an uploaded `package.tgz`, or from `.tgz` files placed in `igs/`.
- **Evaluates FHIRPath** (`POST /evaluate`) with typed results.
- **Behaves like Java at startup**: it answers `/version` straight away and returns `503` with an OperationOutcome
  ("Validator still loading... please wait.") for everything else until loading finishes.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/version` | Wrapper version and the HAPI version it is compatible with |
| `POST` | `/validate` | Validate a JSON/XML resource. Optional `?profile=url1,url2` |
| `GET` | `/resources` | Known resource types |
| `GET` | `/profiles` | Loaded StructureDefinition URLs |
| `POST` | `/profiles` | Load a StructureDefinition (or ValueSet / CodeSystem), JSON or XML |
| `GET` | `/igs` | IG package id to URL |
| `GET` | `/profiles-by-ig` | IG package id to its profile URLs |
| `POST` | `/igs` | Load an uploaded `package.tgz` (a `Content-Encoding: gzip` header is fine) |
| `PUT` | `/igs/{id}?version=` | Load an IG from the package registry |
| `POST` | `/evaluate` | `?path=<FHIRPath>[&type=<datatype or Resource.backbone>]`, body is the resource or element |

Validation always returns an OperationOutcome. Each issue has `severity`, `code`, `details.text`, `expression`
(the element path) and extensions for line, column and source, in the same shape as Java.

Example:

```bash
curl -s -X POST 'http://localhost:4567/validate?profile=http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient' \
     --data-binary @patient.json
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `VALIDATOR_PORT` / `VALIDATOR_HOST` | `4567` / `0.0.0.0` | Listen address |
| `TX_SERVER_URL` | `http://tx.fhir.org` | Terminology server |
| `DISABLE_TX` | unset | If set to any value, no terminology server is used (same rule as Java) |
| `DISPLAY_ISSUES_ARE_WARNINGS` | `false` | A wrong code display text becomes a warning instead of an error |
| `TX_TIMEOUT_SECONDS` | `10` | Terminology request timeout |
| `PACKAGE_CACHE_DIR` | `~/.cache/fhir-r4-validator/packages` | Downloaded IG cache |
| `PACKAGES_REGISTRY_URL` | `https://packages.fhir.org` | FHIR package registry |
| `PACKAGES_OFFLINE` | `false` | Use only cached packages |
| `LOG_LEVEL` | `INFO` | Log level |

## Running it

```bash
just dev            # development, auto-reload on port 4567
just start          # production, one worker
just test           # run the tests
just docker-build && just docker-run
```

Or without `just`: `uv sync && uv run python -m app.main`.

Use **one worker**. The loaded definitions live in memory, so each extra worker is another full copy. Requests are
handled concurrently by threads, so one worker is enough.

## How it works

```
app/
  fhir/             the validation engine
    validator.py      base pass + profile pass, slicing, invariants, bindings, references
    snapshot.py       builds a snapshot for profiles that only have a differential
    terminology.py    local ValueSets/CodeSystems, then the terminology server
    fhirpath.py       FHIRPath (R4 model) with the functions fhirpathpy lacks
    xml_codec.py      XML to JSON, driven by the definitions, with line/column
    vitals.py         vital-signs profiles applied by LOINC code
    parser.py, model.py, tree.py, xhtml.py, jsonio.py
  services/         registry, validator, FHIRPath and IG services
  routers/ schemas/ core/ di/
  main.py           app, startup gate (503 while loading), CORS
data/               R4 core definitions (resources, types, profiles, extensions, value sets, v2/v3)
igs/                IGs loaded at startup (US Core 3.1.1)
tools/compare_with_java.py   comparison against the Java service
tests/              119 tests
```

Terminology is answered in this order: local definitions (R4 core, HL7 v2/v3, loaded IGs), then the terminology
server for systems such as SNOMED, LOINC, RxNorm and UCUM. With no server, those codes produce a warning, as with
Java.

## How the match was measured

The same resources were posted to the Java service and to this one, and each issue was compared on severity, path
and message. Both ran without a terminology server.

- **Official examples**: the HL7 R4 example resources (https://hl7.org/fhir/R4/examples-json.zip).
- **US Core**: the examples inside the US Core 3.1.1 package, each validated against the profile it declares, plus
  corrupted copies (each top-level element removed in turn, an unknown property added, a bad `status`).
- **Endpoints**: the lists and `/evaluate` results were compared directly.

To repeat it against your Java service:

```bash
python tools/compare_with_java.py --java http://localhost:4567 --py http://localhost:4568 \
    --dir /path/to/examples-json --limit 500          # add --mutate to corrupt each resource too
```

## Differences from the Java validator

The full list is in `parity-report.md`. In short:

- **Rules for validating definition files are not built.** The service validates clinical data (Patient,
  Observation, ...) and also definition files (StructureDefinition, ValueSet, ...). Java has extra checks only for the
  definition files, such as HL7 working-group publisher checks and SearchParameter cross-checks. They never apply to
  clinical data, so this only matters if you validate definition files, for example while authoring a profile. It is
  most of the remaining difference on the official examples.
- **HAPI rules written directly in its code were copied where found.** These are not in the FHIR definitions, for
  example the Observation best-practice warnings and the vital-signs profiles applied by LOINC code. I found them by
  testing Java and copied them. A few rare ones may be missing, and the effect is a small difference in a warning or
  hint, almost never in an error. The vital-signs table covers the 18 LOINC codes found by testing Java.
- **Java bugs are not copied.** `GET /profiles-by-ig` returns `{}` on Java even after an IG is loaded, and Java
  returns an HTML 500 for an empty body, an unknown `?profile=`, and some valid FHIRPath expressions. This service
  returns the real lists and an OperationOutcome instead.
- Java attaches some Bundle `fullUrl` errors to a different entry number (same message and count).
- Terminology answers depend on the terminology server when one is configured, for both services.
