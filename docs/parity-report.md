# Gap between the Java validator and this service

Current version, compared against the running Java wrapper (HAPI 6.5.18, wrapper 2.3.6), both without a
terminology server.

| Compared | Same "has errors?" answer | Identical errors |
|---|---|---|
| 2,793 official HL7 R4 examples | 98.0% | 97.4% |
| US Core examples plus corrupted copies (877) | 99.9% | 98.1% |

The full documentation is in `validator-documentation.md`. This page lists what the remaining gap is.

## 1. Rules for validating definition files (not built)

The service validates two kinds of resources: clinical data (Patient, Observation, ...) and definition files
(StructureDefinition, ValueSet, CapabilityStatement, ...). Java has extra checks that only apply to definition files:

- HL7 working-group checks (publisher and contact must match the working group named in the file)
- SearchParameter type cross-checks in a CapabilityStatement
- ConceptMap source/target code checks
- narrative hyperlinks that must resolve
- "Profile ... is for type StructureDefinition" on definition resources
- status versus standards-status hints

**Impact:** none for clinical data, these rules never fire there. They only matter if you validate definition files,
for example while authoring a profile. This is most of the remaining difference on the official examples.

## 2. HAPI rules written directly in its code (copied where found, a few may be missing)

HAPI has many special checks that are not in the FHIR definitions. I found the ones that affect clinical data by
testing Java directly and copied them: Observation best-practice warnings, vital-signs profiles applied by LOINC
code, example-URL checks, OID/UUID/id/canonical rules, draft code system notes, Bundle `fullUrl` rules, extension
context checks, and the messages used when no terminology server is available.

**Gap:** rare rules that were not triggered by my test data. The vital-signs table covers the 18 LOINC codes found
by testing Java, other codes are validated normally.

**Impact:** a small difference in a warning or hint, almost never in an error.

## 3. Java bugs and quirks, deliberately not copied

- `GET /profiles-by-ig` returns `{}` on Java even after an IG is loaded. This service returns the real lists.
- Java returns an HTML 500 for an empty body, an unknown `?profile=`, and some valid FHIRPath expressions. This
  service returns an OperationOutcome (`200` for `/validate`, `400` for `/evaluate`).
- Java attaches some Bundle `fullUrl` errors to a different entry number (same message and count).
- Root-level line/column for multi-line JSON depends on HAPI's lexer read-ahead. Property positions and
  single-line documents match.

## 4. Terminology

With a terminology server configured, both services ask it, so the answers depend on the server. The Java instance
used for the comparison had a broken server cache, so terminology-server behaviour (display checks, SNOMED,
RxNorm) was checked against tx.fhir.org directly, not against Java output.

## 5. One engine difference

An invariant that evaluates to empty counts as satisfied. The FHIRPath library returns empty where HAPI returns a
boolean for some `in`/`is` cases, and this reproduces HAPI's result. Extension context invariants are the exception
and treat empty as "not allowed", as HAPI does.
