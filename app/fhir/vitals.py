"""Vital-sign profiles HAPI applies automatically, keyed by LOINC code.

HAPI validates an Observation carrying one of these LOINC codes against the matching FHIR vital-signs
profile even when the instance does not claim it. The (name, profile) pairs below were read from the
running Java wrapper, since HAPI hard-codes them.
"""

LOINC = "http://loinc.org"

VITAL_SIGN_PROFILES: dict[str, tuple[str, str]] = {
    "85353-1": ("Vital Signs Panel", "http://hl7.org/fhir/StructureDefinition/vitalspanel"),
    "9279-1": ("Respiratory Rate", "http://hl7.org/fhir/StructureDefinition/resprate"),
    "8867-4": ("Heart rate", "http://hl7.org/fhir/StructureDefinition/heartrate"),
    "2708-6": ("Oxygen saturation", "http://hl7.org/fhir/StructureDefinition/oxygensat"),
    "59408-5": ("Oxygen saturation", "http://hl7.org/fhir/StructureDefinition/oxygensat"),
    "8310-5": ("Body temperature", "http://hl7.org/fhir/StructureDefinition/bodytemp"),
    "8302-2": ("Body height", "http://hl7.org/fhir/StructureDefinition/bodyheight"),
    "8306-3": ("Body height", "http://hl7.org/fhir/StructureDefinition/bodyheight"),
    "8287-5": ("Head circumference", "http://hl7.org/fhir/StructureDefinition/headcircum"),
    "9843-4": ("Head circumference", "http://hl7.org/fhir/StructureDefinition/headcircum"),
    "29463-7": ("Body weight", "http://hl7.org/fhir/StructureDefinition/bodyweight"),
    "3141-9": ("Body weight", "http://hl7.org/fhir/StructureDefinition/bodyweight"),
    "39156-5": ("Body mass index", "http://hl7.org/fhir/StructureDefinition/bmi"),
    "85354-9": ("Blood pressure systolic and diastolic", "http://hl7.org/fhir/StructureDefinition/bp"),
    "8480-6": ("Blood pressure systolic and diastolic", "http://hl7.org/fhir/StructureDefinition/bp"),
    "55284-4": ("Blood pressure systolic and diastolic", "http://hl7.org/fhir/StructureDefinition/bp"),
    "8331-1": ("Body temperature", "http://hl7.org/fhir/StructureDefinition/bodytemp"),
    "8329-5": ("Body temperature", "http://hl7.org/fhir/StructureDefinition/bodytemp"),
}
