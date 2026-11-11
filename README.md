# FHIR R4 Validation Service (pure Python / FastAPI)

A pure-Python re-implementation of [`inferno-framework/fhir-validator-wrapper`](https://github.com/inferno-framework/fhir-validator-wrapper)
(the Java service that wraps the HL7 validator). Same REST API, **no JVM**: roughly 190 MB of RAM and ~3 s
to start, instead of a JVM heap and a minute-long warm-up.

The original Java sources are kept in `fhir-validator-wrapper/` as the reference.

## What it does

- **Instance validation** (`POST /validate`, JSON or XML): unknown properties, JSON/XML shape, primitive formats
  (from the spec's own regexes), cardinality, choice types, nested structures, fixed/pattern values, **slicing**
  (value / pattern / type / exists / profile discriminators), **FHIRPath invariants** (all ~9,000 invariants in
  the R4 core definitions parse; they run through `fhirpathpy` with the missing FHIR functions added),
  **terminology bindings**, reference target types, contained resources, Bundles, narrative XHTML, extensions.
- **Profiles**: validates against the base type *and* every requested profile and `meta.profile`. Profiles that
  only ship a differential get a generated snapshot.
- **Terminology**: local CodeSystems/ValueSets first (R4 core, HL7 v2/v3 tables, any loaded IG), then a
  terminology server (`TX_SERVER_URL`, default `tx.fhir.org`) for SNOMED, LOINC, RxNorm, UCUM etc. Without a
  server those codes get a warning instead of a false error. Wrong display text is an error
  (a warning with `DISPLAY_ISSUES_ARE_WARNINGS=true`).
- **IGs**: load by NPM id/version from `packages.fhir.org` (with dependencies, cached on disk), upload a
  `package.tgz`, or drop `.tgz` files in `igs/` to preload them.
- **FHIRPath** (`POST /evaluate`): typed results (`date`, `Quantity`, `Patient.communication`, ...), choice types,
  bare datatypes and backbone elements via `?type=`, decimals keep their precision.
- Startup behaves like the Java wrapper: the server accepts connections immediately, answers `/version`, and
  returns `503` + an `OperationOutcome` ("Validator still loading...") for everything else until loaded.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/version` | Wrapper and validator-compatibility versions |
| `POST` | `/validate` | Validate a JSON/XML resource; `?profile=url1,url2` (optional) |
| `GET` | `/resources` | Resource types known to the validator (146 for R4) |
| `GET` | `/profiles` | All StructureDefinition URLs loaded |
| `POST` | `/profiles` | Load a StructureDefinition (or ValueSet / CodeSystem), JSON or XML |
| `GET` | `/igs` | IG package id → canonical URL |
| `GET` | `/profiles-by-ig` | IG package id → profile URLs |
| `POST` | `/igs` | Load an uploaded `package.tgz` (a `Content-Encoding: gzip` header is fine) |
| `PUT` | `/igs/{id}?version=` | Load an IG from the package registry |
| `POST` | `/evaluate` | `?path=<FHIRPath>[&type=<datatype or Resource.backbone>]`, body = resource/element |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `VALIDATOR_PORT` / `VALIDATOR_HOST` | `4567` / `0.0.0.0` | Listen address |
| `TX_SERVER_URL` | `http://tx.fhir.org` | Terminology server |
| `DISABLE_TX` | unset | If **set to anything**, no terminology server is used (same rule as the Java wrapper) |
| `DISPLAY_ISSUES_ARE_WARNINGS` | `false` | Wrong code display → warning instead of error |
| `TX_TIMEOUT_SECONDS` | `10` | Terminology request timeout |
| `PACKAGE_CACHE_DIR` | `~/.cache/fhir-r4-validator/packages` | Downloaded IG cache |
| `PACKAGES_REGISTRY_URL` | `https://packages.fhir.org` | NPM-style FHIR registry |
| `PACKAGES_OFFLINE` | `false` | Never touch the network for packages (cache only) |
| `LOG_LEVEL` | `INFO` | |

## Running

```bash
just dev            # auto-reload on :4567
just start          # production, one worker
just test           # pytest
just docker-build && just docker-run
```

or `uv sync && uv run python -m app.main`.

**Use one worker.** The loaded definitions live in RAM, so every extra Uvicorn worker is another full copy.
Requests are served from a thread pool and are safe to run concurrently.

## Layout

```
app/
├── fhir/                 # the validation engine
│   ├── validator.py      #   instance validator (base pass + profile pass, slicing, invariants, bindings)
│   ├── snapshot.py       #   differential -> snapshot generation
│   ├── terminology.py    #   local ValueSet/CodeSystem logic + terminology-server client
│   ├── fhirpath.py       #   strict parser, R4 model, hasValue/htmlChecks/memberOf/resolve/...
│   ├── xml_codec.py      #   model-driven XML -> JSON (line/column tracking)
│   ├── parser.py         #   format detection (JSON / XML)
│   ├── model.py, tree.py #   StructureDefinition navigation
│   └── xhtml.py, jsonio.py
├── services/             # registry, validator, FHIRPath and IG services
├── routers/ schemas/ core/ di/
└── main.py               # app, startup gate (503 while loading), CORS
data/                     # R4 core definitions (resources, types, profiles, extensions, value sets, v2/v3)
igs/                      # IGs preloaded at startup (US Core 3.1.1)
docs/parity-report.md     # the remaining gap to the Java service
tests/                    # 119 tests (incl. Java-confirmed behaviours)
tools/compare_with_java.py  # differential test against the Java service
```

## Parity with the Java service

Checked request-by-request against the running Java wrapper: **98.0%** agreement on whether a resource has errors and
**97.4%** identical error sets (path + message) over 2,793 official R4 examples; **99.9% / 98.1%** over US Core examples
plus corrupted variants. Details, and the short list of known differences (`/profiles-by-ig`, error status codes,
conformance-resource rules), are in `docs/parity-report.md`.

Re-run the comparison against your own Java instance at any time:

```bash
# start this service on another port, ideally both with DISABLE_TX set for a clean comparison
python tools/compare_with_java.py --java http://localhost:4567 --py http://localhost:4568 \
    --dir /path/to/examples-json --limit 500            # add --mutate to also corrupt each resource
```

`--dir` can be the official examples (https://hl7.org/fhir/R4/examples-json.zip) or an IG's `package/example` folder.

## License

Apache License 2.0, see `LICENSE`. This is an independent re-implementation of the API of the Inferno
`fhir-validator-wrapper`; see `NOTICE` for credits and for the origin of the bundled HL7 definitions.
