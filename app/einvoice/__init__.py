"""E-Rechnung nach EN 16931 (ZUGFeRD/Factur-X, Profil EN 16931).

Aufbau — eine Richtung, keine Rueckwege:

    Invoice (OSS-Modell)
      └─ mapper.build_einvoice()   → model.EInvoice (EN-16931-Semantik)
           ├─ rules.check()         → deutsche Meldungen, leer = gueltig
           └─ cii.serialize()       → CII-XML (UN/CEFACT D16B)
                └─ pdf.write_facturx_pdf() → PDF/A-3b mit factur-x.xml

``service`` verbindet das mit der App: Einstellungen, Einfrieren des XML beim
Versand (``invoices.xml_path``) und die Readiness-Pruefung fuer die
Einstellungsseite. Nur ``mapper`` und ``service`` kennen OSS-Modelle; Modell,
Regeln und Serializer sind reines Python und ohne App-Kontext testbar.
"""
