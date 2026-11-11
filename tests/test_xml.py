from conftest import errors_of, messages_of

PATIENT = """<Patient xmlns="http://hl7.org/fhir">
  <id value="a"/>
  <text><status value="generated"/><div xmlns="http://www.w3.org/1999/xhtml">hi</div></text>
  <extension url="http://fhir.test.local/x"><valueString value="v"/></extension>
  <active value="true"/>
  <name id="n1"><family value="Doe"/><given value="J"/></name>
  <telecom><system value="phone"/><value value="1"/></telecom>
  <birthDate value="2000-01-01"/>
  <multipleBirthInteger value="2"/>
  <contained><Organization><id value="o"/><name value="Org"/></Organization></contained>
  <managingOrganization><reference value="#o"/></managingOrganization>
</Patient>"""


def validate(client, xml, profile=None):
    url = "/validate" + (f"?profile={profile}" if profile else "")
    r = client.post(url, content=xml.encode(), headers={"Content-Type": "application/fhir+xml"})
    assert r.status_code == 200
    return r.json()


def test_valid_xml_with_single_repeating_elements(client):
    # a single <name>/<given>/<telecom>/<extension> must still be treated as arrays
    out = validate(client, PATIENT)
    assert errors_of(out) == [], messages_of(out)


def test_xml_primitive_types_come_from_the_model(client):
    out = validate(client, PATIENT.replace('<active value="true"/>', '<active value="yes"/>'))
    assert "must be a boolean" in messages_of(out) or "not a valid boolean" in messages_of(out)


def test_xml_wrong_namespace(client):
    out = validate(client, PATIENT.replace('xmlns="http://hl7.org/fhir"', 'xmlns="http://example.org"', 1))
    assert "Wrong namespace" in messages_of(out)


def test_xml_unknown_element(client):
    out = validate(client, PATIENT.replace("<active", "<bogus value='1'/><active", 1))
    assert "Unrecognized property 'bogus'" in messages_of(out)


def test_xml_doctype_is_rejected(client):
    out = validate(client, '<!DOCTYPE x [<!ENTITY e "boom">]>' + PATIENT)
    assert out["issue"][0]["severity"] == "fatal"
    assert "DTD" in messages_of(out)


def test_xml_not_well_formed(client):
    out = validate(client, "<Patient xmlns='http://hl7.org/fhir'><id value='a'></Patient>")
    assert out["issue"][0]["severity"] == "fatal"


def test_xml_error_positions(client):
    xml = '<Patient xmlns="http://hl7.org/fhir">\n  <gender value="banana"/>\n</Patient>'
    out = validate(client, xml)
    err = errors_of(out)[0]
    line = [e for e in err["extension"] if e["url"].endswith("issue-line")][0]["valueInteger"]
    assert line == 2


def test_xml_primitive_extension_survives(client):
    xml = PATIENT.replace('<given value="J"/>', '<given value="J"><extension url="http://e"><valueCode value="x"/></extension></given>')
    assert errors_of(validate(client, xml)) == []


def test_xml_us_core_example(client):
    xml = """<Patient xmlns="http://hl7.org/fhir">
      <text><status value="generated"/><div xmlns="http://www.w3.org/1999/xhtml">x</div></text>
      <identifier><system value="urn:oid:2.16.840.1.113883.4.1"/><value value="1"/></identifier>
      <name><family value="A"/></name><gender value="male"/></Patient>"""
    out = validate(client, xml, "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient")
    assert errors_of(out) == [], messages_of(out)
