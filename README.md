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
  `package.tgz`, or drop `.tgz` files in `igs/` to preload them (nothing is preloaded by default).
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
| `DELETE` | `/profiles?url=<canonical url>` | Remove a loaded profile, value set or code system (the HL7 base definitions are protected) |
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
| `DISABLE_TX` | unset | If **set to anything** (even `false`), no terminology server is used, same rule as the Java wrapper |
| `DISPLAY_ISSUES_ARE_WARNINGS` | `false` | Wrong code display → warning instead of error |
| `TX_TIMEOUT_SECONDS` | `10` | Terminology request timeout |
| `PACKAGE_CACHE_DIR` | `~/.cache/fhir-r4-validator/packages` | Downloaded IG cache |
| `PACKAGES_REGISTRY_URL` | `https://packages.fhir.org` | NPM-style FHIR registry |
| `PRELOAD_IGS` | empty | Guides to load at startup, comma separated: `id#version` (or just `id` for the latest) |
| `PRELOAD_IGS_STRICT` | `false` | If `true`, a guide in `PRELOAD_IGS` that fails to load stops startup |
| `PACKAGES_OFFLINE` | `false` | Never touch the network for packages (cache only) |
| `LOG_LEVEL` | `INFO` | |

## Loading profiles and implementation guides

**Nothing is loaded by default** except the HL7 R4 base definitions, so by default you validate against base FHIR R4
only. Add what you need:

| I want to... | How |
|---|---|
| Use a published guide such as **US Core** | `curl -X PUT 'http://localhost:4567/igs/hl7.fhir.us.core?version=3.1.1'`<br>Downloads it from `packages.fhir.org` (with its dependencies) and loads it. Leave out `version` for the latest. |
| Use **my own package** | `curl -X POST --data-binary @my-package.tgz http://localhost:4567/igs`<br>The id and version are read from the package's `package.json`. |
| Load **a single profile**, value set or code system | `curl -X POST --data-binary @profile.json http://localhost:4567/profiles` |
| **Remove** a profile, value set or code system | `curl -X DELETE 'http://localhost:4567/profiles?url=<canonical url>'`<br>Use the `url` from the definition. Returns `204`, or `404` if it isn't loaded. The HL7 base definitions cannot be removed (`403`). |
| **Preload** guides at every start | Put the `.tgz` files in the `igs/` folder (or set `IGS_DIR`), or list them in `PRELOAD_IGS`, for example `PRELOAD_IGS=hl7.fhir.us.core#3.1.1,my.country#1.0.0`. They are loaded at startup, from the package cache or the registry. |

Then validate against a profile with `POST /validate?profile=<canonical url>`, or put the profile in the resource's
`meta.profile`. Loading a guide does not force it on anything: a resource is only checked against profiles it names
or that you pass in the request.

Loaded profiles are held in memory. After a restart only what is in `igs/` or `PRELOAD_IGS` is loaded again, so keep your
packages there, or load them again at startup. A guide in `PRELOAD_IGS` that cannot be loaded is logged and startup
continues; set `PRELOAD_IGS_STRICT=true` to stop startup instead.

Removing a profile also stops every profile derived from it from validating, rather than leaving the removed parent's
rules in place (a derived profile that was loaded with its own full snapshot keeps that snapshot). Downloaded packages are cached on disk (`PACKAGE_CACHE_DIR`), so loading them
again is fast and works offline.

A package is a standard FHIR package: a `.tgz` containing `package/package.json` (`name`, `version`, `canonical`,
`dependencies`) and your StructureDefinitions, ValueSets and CodeSystems as JSON files directly inside `package/`.

## Terminology server

Codes in large code systems (SNOMED CT, LOINC, RxNorm, UCUM) cannot be bundled, so the validator asks a
**terminology server** to check them: is the code real, is it in the value set, is the display text right. This
includes value sets you load yourself: the validator does not match against your list of codes itself, it sends the
code and your value set to the server and trusts its answer. Codes from the HL7 code systems that are bundled in
`data/` (gender, observation-category, HL7 v2/v3 tables, ...) are checked locally.

- **Default:** HL7's public server, `http://tx.fhir.org`, used as `https://tx.fhir.org/r4`.
  Its software is open source: https://github.com/HealthIntersections/fhirserver
- **Your own server:** set `TX_SERVER_URL` to its full **FHIR R4** base URL, for example `https://tx.example.org/fhir`.
  The server must implement the standard FHIR R4 terminology operations `ValueSet/$validate-code` and
  `CodeSystem/$validate-code` (`POST` with a `Parameters` body). Other open-source options include
  [HAPI FHIR JPA server](https://github.com/hapifhir/hapi-fhir-jpaserver-starter) and
  [Snowstorm](https://github.com/IHTSDO/snowstorm) (SNOMED CT); only `tx.fhir.org` has been tested here.
- **What is sent:** the code, its system, the display text and the value set (your own value set's definition when
  you loaded one). No patient data.
- **Off (`DISABLE_TX` set):** nothing is asked, and those codes only produce a warning ("cannot be validated"), never
  a verdict. A wrong SNOMED code, or a code outside your own value set, would pass with just a warning. **Keep it on
  wherever codes matter (billing, claims, clinical data), and prefer your own server in production.** If the server is
  unreachable the validator also falls back to warnings and keeps running.

Copy `.env.example` to `.env` to configure it (every variable is documented there).

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
igs/                      # put .tgz guides here to load them at startup (empty by default)
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
