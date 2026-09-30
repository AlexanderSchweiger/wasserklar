"""CII-XML als ZUGFeRD/Factur-X-Anhang in ein WeasyPrint-Dokument einbetten.

Ergebnis ist ein PDF/A-3b mit ``factur-x.xml`` (AFRelationship ``Data``) und
der Factur-X-XMP-Erweiterung. Verifiziert mit dem Mustang-Validator gegen alle
Rechnungsdesigns (E_RECHNUNG_PLAN.md, Phase 0). Braucht WeasyPrint >= 69
(eigene XMP-Bloecke, MIME-Typ ``text/xml`` fuer den Anhang).
"""

FACTURX_FILENAME = "factur-x.xml"
CONFORMANCE_EN16931 = "EN 16931"

_FX_NS = "urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#"

# fx-Eigenschaften + das PDF/A-Erweiterungsschema, das sie deklariert (ohne
# Schema-Block meldet jeder PDF/A-Validator die fx-Eigenschaften als Fehler).
_FX_XMP = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:fx="{ns}">
    <fx:DocumentType>INVOICE</fx:DocumentType>
    <fx:DocumentFileName>{filename}</fx:DocumentFileName>
    <fx:Version>1.0</fx:Version>
    <fx:ConformanceLevel>{level}</fx:ConformanceLevel>
  </rdf:Description>
  <rdf:Description rdf:about=""
      xmlns:pdfaExtension="http://www.aiim.org/pdfa/ns/extension/"
      xmlns:pdfaSchema="http://www.aiim.org/pdfa/ns/schema#"
      xmlns:pdfaProperty="http://www.aiim.org/pdfa/ns/property#">
    <pdfaExtension:schemas>
      <rdf:Bag>
        <rdf:li rdf:parseType="Resource">
          <pdfaSchema:schema>Factur-X PDFA Extension Schema</pdfaSchema:schema>
          <pdfaSchema:namespaceURI>{ns}</pdfaSchema:namespaceURI>
          <pdfaSchema:prefix>fx</pdfaSchema:prefix>
          <pdfaSchema:property>
            <rdf:Seq>
              <rdf:li rdf:parseType="Resource">
                <pdfaProperty:name>DocumentFileName</pdfaProperty:name>
                <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                <pdfaProperty:category>external</pdfaProperty:category>
                <pdfaProperty:description>The name of the embedded XML document</pdfaProperty:description>
              </rdf:li>
              <rdf:li rdf:parseType="Resource">
                <pdfaProperty:name>DocumentType</pdfaProperty:name>
                <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                <pdfaProperty:category>external</pdfaProperty:category>
                <pdfaProperty:description>The type of the hybrid document in capital letters, e.g. INVOICE or ORDER</pdfaProperty:description>
              </rdf:li>
              <rdf:li rdf:parseType="Resource">
                <pdfaProperty:name>Version</pdfaProperty:name>
                <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                <pdfaProperty:category>external</pdfaProperty:category>
                <pdfaProperty:description>The actual version of the standard applying to the embedded XML document</pdfaProperty:description>
              </rdf:li>
              <rdf:li rdf:parseType="Resource">
                <pdfaProperty:name>ConformanceLevel</pdfaProperty:name>
                <pdfaProperty:valueType>Text</pdfaProperty:valueType>
                <pdfaProperty:category>external</pdfaProperty:category>
                <pdfaProperty:description>The conformance level of the embedded XML document</pdfaProperty:description>
              </rdf:li>
            </rdf:Seq>
          </pdfaSchema:property>
        </rdf:li>
      </rdf:Bag>
    </pdfaExtension:schemas>
  </rdf:Description>
</rdf:RDF>"""


def facturx_xmp(level=CONFORMANCE_EN16931):
    return _FX_XMP.format(ns=_FX_NS, filename=FACTURX_FILENAME, level=level).encode("utf-8")


def write_facturx_pdf(document, xml_bytes, level=CONFORMANCE_EN16931):
    """Schreibt das gerenderte WeasyPrint-``document`` als PDF/A-3b mit Factur-X-Anhang."""
    from weasyprint import Attachment

    attachment = Attachment(
        string=xml_bytes.decode("utf-8"), name=FACTURX_FILENAME, relationship="Data",
        description="Factur-X/ZUGFeRD-Rechnung (EN 16931)",
    )
    document.metadata.attachments = [*document.metadata.attachments, attachment]
    document.metadata.xmp_metadata = [*document.metadata.xmp_metadata, facturx_xmp(level)]
    return document.write_pdf(pdf_variant="pdf/a-3b")
