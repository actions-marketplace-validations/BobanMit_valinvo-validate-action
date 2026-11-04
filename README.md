# Valinvo e-invoice validator

A GitHub Action that checks **XRechnung**, **ZUGFeRD / Factur-X** and **Peppol BIS Billing 3.0** e-invoices in
your CI against the official rules (XML schema and Schematron): XRechnung 3.0.2 (KoSIT), ZUGFeRD 2.5.2 /
Factur-X 1.09.2 (FeRD), the French rules BR-FR 1.4.0.04 (FNFE-MPE) and Peppol BIS Billing 3.0.21 (OpenPeppol).
Send the XML, or a ZUGFeRD / Factur-X PDF: then the embedded XML is checked, and the PDF itself against PDF/A-3.
Every rule error appears on the file, with the rule ID and the XPath, and in the job summary.

Useful when your software *generates* e-invoices: keep a few generated invoices as test fixtures and fail the
build when a change breaks a national rule such as BR-DE-15 (buyer reference) or PEPPOL-EN16931-R010 (buyer
electronic address).

## Usage

```yaml
name: E-invoices
on: [push, pull_request]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      # … generate your test invoices here …
      - uses: BobanMit/valinvo-validate-action@v1
        with:
          files: build/test-invoices/**/*.xml
          accept-terms: "1.0"
```

ZUGFeRD / Factur-X PDFs work the same way; mix both in one step if you like:

```yaml
      - uses: BobanMit/valinvo-validate-action@v1
        with:
          files: |
            build/test-invoices/**/*.xml
            build/test-invoices/**/*.pdf
          accept-terms: "1.0"
```

With an API key, pass it as a secret instead of `accept-terms`:

```yaml
      - uses: BobanMit/valinvo-validate-action@v1
        with:
          files: build/test-invoices/**/*.xml
          api-key: ${{ secrets.VALINVO_API_KEY }}
```

## What it checks

The rule set is detected from each document:

| Format | Documents |
|---|---|
| XRechnung 3.0.2 | UBL Invoice, UBL CreditNote, UN/CEFACT CII |
| ZUGFeRD 2.5.2 / Factur-X 1.09.2 | PDF/A-3 with embedded CII, or the CII XML alone; profiles MINIMUM, BASIC WL, BASIC, EN 16931, EXTENDED |
| France (BR-FR 1.4.0.04) | Factur-X EN 16931, CII or UBL with a French billing frame (`B1`, `S1`, `M1` …) in BT-23 |
| Peppol BIS Billing 3.0.21 | UBL Invoice, UBL CreditNote |

For a PDF, the invoice XML attached to it (`factur-x.xml`, `zugferd-invoice.xml` or `xrechnung.xml`) is checked
against the rules of its format, the PDF against PDF/A-3 (veraPDF; rule IDs like `PDF/A-3 6.2.11.4.1-1`) and its
XMP metadata against the XML (rule IDs `ZF-XMP-…`). The rule set then names both, e.g.
`zugferd-2.5.2-en16931+pdf-a-3u`. Annotations on a PDF aren't placed on a line of the PDF file; the message names the line and
XPath in the embedded XML.

A ZUGFeRD / Factur-X invoice without a delivery date (BT-72) or delivery party gets one warning,
`PEPPOL-EN16931-R008` (empty `ApplicableHeaderTradeDelivery`): the ZUGFeRD schema requires that element even
when there is nothing to put in it, and FeRD's rules warn about any empty element. The invoice is still valid.
If you set `fail-on-warnings: true`, add a delivery date to such invoices.

Files that are not one of these (for example a `pom.xml` caught by a broad pattern, or a PDF without an embedded
invoice) are reported as *not checked* and fail the step, so point `files` at your invoices only. Not supported:
ZUGFeRD 1.0.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `files` | yes | | Glob patterns of the invoice files, one per line or comma-separated. `**` matches any folders. |
| `accept-terms` | without a key | | The version of the [Valinvo Terms of Use](https://valinvo.bmk-hub.com/terms) you accept, currently `1.0`. |
| `api-key` | no | | A Valinvo API key. Replaces `accept-terms`. Always pass it as a secret. |
| `fail-on-warnings` | no | `false` | `true` also fails the step when an invoice has warnings. |
| `api-url` | no | `https://valinvo.bmk-hub.com` | Base URL of the API. |

## Outputs

| Output | Description |
|---|---|
| `checked` | Number of files validated. |
| `failed` | Number of files that are invalid or could not be checked. |
| `report` | Path of a JSON file with every result, error and warning (upload it with `actions/upload-artifact` if you want to keep it). |

## What you see

Each error becomes an annotation on the file:

```
::error file=examples/invalid/xrechnung-missing-fields.xml,line=2,col=324,title=BR-DE-15::Das Element "Buyer reference" (BT-10) muss übermittelt werden.
```

The job summary lists every file with its rule set, result and number of errors and warnings, and the errors
of each failing file with rule, message and location.

## Terms, privacy and limits

- **Terms.** Without an API key, `accept-terms` is how you accept the
  [Terms of Use](https://valinvo.bmk-hub.com/terms) on behalf of your business; there is deliberately no default.
  The Service is for businesses only. Free use is for testing: use **sample or test invoices**, not real invoices
  containing other people's personal data. If the terms change, the API answers `428` until you set the new
  version.
- **Privacy.** The files are sent to the Valinvo API (Oracle Cloud, Frankfurt, Germany), checked in memory and
  not written to disk or kept after the answer; see the [privacy notice](https://valinvo.bmk-hub.com/privacy).
  The action sends nothing else from your repository.
- **Limits.** 20 validations per minute per runner IP address (the action waits as long as the API asks and
  retries), 20 MB and 75,000 XML elements per file (PDF or XML).
- **API keys** are free during the beta: write to hello@bmk-hub.com. With a key, the
  [Data Processing Agreement](https://valinvo.bmk-hub.com/dpa) applies.

The validation rules are published by KoSIT, FeRD, FNFE-MPE, OpenPeppol and CEN and used unmodified. Valinvo is an independent
implementation, not affiliated with, certified or endorsed by them.

## Development

```bash
python3 -m unittest discover -s tests   # no network needed
```

The script uses only the Python standard library (3.8+), which every GitHub-hosted runner has.

## Licence

Apache License 2.0, see [LICENSE](LICENSE). © 2026 BMK HUB DOOEL.
