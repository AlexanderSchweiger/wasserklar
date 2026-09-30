# E-Rechnung-Fixtures

`kosit-*.xml` stammen aus der offiziellen **XRechnung-Testsuite** der KoSIT
(https://github.com/itplr-kosit/xrechnung-testsuite, `src/test/business-cases/standard`,
Apache-2.0). Sie dienen als Fremdwerk-Gegenprobe für den Parser
`app/einvoice/incoming.py`: dieselbe Rechnung liegt jeweils als UBL und als CII vor —
beide müssen dieselben Daten ergeben.
