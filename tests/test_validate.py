"""Unit tests without network: a fake opener stands in for the Valinvo API.

Run from the action's root:  python3 -m unittest discover -s tests
"""
import email.message
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import validate  # noqa: E402

VALID = {"ruleSet": "peppol-bis-3.0.21-ubl-invoice", "valid": True, "errors": [], "warnings": []}
INVALID = {
    "ruleSet": "xrechnung-3.0.2-ubl-invoice", "valid": False,
    "errors": [
        {"rule": "BR-DE-15", "message": "Das Element \"Buyer reference\" (BT-10) muss übermittelt werden.", "location": "/:Invoice[1]", "line": 2, "column": 75},
        {"message": "cvc-complex-type.2.4.a: Invalid content, expected cbc:ID", "location": "invoice.xml", "line": 12, "column": 5},
    ],
    "warnings": [{"rule": "PEPPOL-EN16931-R002", "message": "No more than one note, 100%, a:b", "location": "/:Invoice[1]/cbc:Note[2]"}],
}
WARNING_ONLY = {"ruleSet": "peppol-bis-3.0.21-ubl-invoice", "valid": True, "errors": [],
                "warnings": [{"rule": "PEPPOL-EN16931-R002", "message": "No more than one note", "location": "/:Invoice[1]"}]}


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, payload=None, headers=None):
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    body = io.BytesIO(json.dumps(payload).encode() if payload is not None else b"")
    return urllib.error.HTTPError("https://valinvo.test/v1/validate", status, "error", message, body)


class FakeApi:
    """Answers each request with the next item: a payload dict (200) or an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return FakeResponse(answer)


class ActionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.dir.name)
        self.addCleanup(os.chdir, self.old_cwd)
        os.makedirs("invoices/nested")
        for name in ("invoices/a.xml", "invoices/nested/b.xml", "invoices/readme.txt"):
            with open(name, "w") as f:
                f.write("<Invoice/>")
        self.summary = os.path.join(self.dir.name, "summary.md")
        self.output = os.path.join(self.dir.name, "output.txt")
        self.sleeps = []

    def run_action(self, api, **inputs):
        env = {
            "INPUT_FILES": "invoices/**/*.xml",
            "INPUT_ACCEPT_TERMS": "1.0",
            "GITHUB_STEP_SUMMARY": self.summary,
            "GITHUB_OUTPUT": self.output,
            "RUNNER_TEMP": self.dir.name,
        }
        env.update({"INPUT_" + k.upper(): v for k, v in inputs.items()})
        out = io.StringIO()
        code = validate.main(env=env, opener=api, sleep=self.sleeps.append, out=out)
        return code, out.getvalue()

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_valid_files_pass_with_outputs_summary_and_terms_header(self):
        api = FakeApi(VALID, VALID)
        code, log = self.run_action(api)
        self.assertEqual(0, code)
        self.assertNotIn("::error", log)
        self.assertEqual(["https://valinvo.bmk-hub.com/v1/validate"] * 2, [r.full_url for r in api.requests])
        request = api.requests[0]
        self.assertEqual("1.0", request.get_header("X-accept-terms"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual("application/xml", request.get_header("Content-type"))
        self.assertTrue(request.get_header("User-agent").startswith("valinvo-github-action/"))
        self.assertEqual(b"<Invoice/>", request.data)
        output = self.read(self.output)
        self.assertIn("checked=2\n", output)
        self.assertIn("failed=0\n", output)
        summary = self.read(self.summary)
        self.assertIn("| `invoices/a.xml` | `peppol-bis-3.0.21-ubl-invoice` | ✅ valid | 0 | 0 |", summary)
        self.assertIn("| `invoices/nested/b.xml` |", summary)
        self.assertNotIn("readme.txt", summary)

    def test_errors_become_annotations_and_fail_the_step(self):
        code, log = self.run_action(FakeApi(INVALID, VALID))
        self.assertEqual(1, code)
        self.assertIn("::error file=invoices/a.xml,line=2,col=75,title=BR-DE-15::Das Element \"Buyer reference\" (BT-10) muss übermittelt werden.", log)
        # Schema errors too.
        self.assertIn("::error file=invoices/a.xml,line=12,col=5,title=XML schema::cvc-complex-type.2.4.a: Invalid content, expected cbc:ID", log)
        # Workflow-command escaping: % in data, ':' and ',' in properties.
        self.assertIn("::warning file=invoices/a.xml,title=PEPPOL-EN16931-R002::No more than one note, 100%25, a:b (at /:Invoice[1]/cbc:Note[2])", log)
        self.assertIn("failed=1\n", self.read(self.output))
        summary = self.read(self.summary)
        self.assertIn("❌ invalid | 2 | 1 |", summary)
        self.assertIn("<details><summary><code>invoices/a.xml</code>: 2 error(s), 1 warning(s)</summary>", summary)
        report = json.loads(self.read(os.path.join(self.dir.name, "valinvo-report.json")))
        self.assertEqual(["invalid", "valid"], [f["status"] for f in report["files"]])

    def test_pdfs_are_sent_as_pdf(self):
        with open("invoices/c.pdf", "wb") as f:
            f.write(b"%PDF-1.7\n%binary")
        api = FakeApi(dict(VALID, ruleSet="zugferd-2.5.2-en16931+pdf-a-3b"))
        code, log = self.run_action(api, files="invoices/*.pdf")
        self.assertEqual(0, code)
        self.assertEqual("application/pdf", api.requests[0].get_header("Content-type"))
        self.assertEqual(b"%PDF-1.7\n%binary", api.requests[0].data)
        self.assertIn("| `invoices/c.pdf` | `zugferd-2.5.2-en16931+pdf-a-3b` | ✅ valid | 0 | 0 |", self.read(self.summary))

    def test_warnings_fail_only_when_asked(self):
        self.assertEqual(0, self.run_action(FakeApi(WARNING_ONLY, VALID))[0])
        self.assertEqual(1, self.run_action(FakeApi(WARNING_ONLY, VALID), fail_on_warnings="true")[0])

    def test_rate_limit_and_busy_answers_are_retried_after_the_servers_delay(self):
        api = FakeApi(http_error(429, {"code": "rate_limited", "message": "Too many requests."}, {"Retry-After": "7"}),
                      http_error(503, {"code": "busy"}, {"Retry-After": "500"}), VALID, VALID)
        code, _ = self.run_action(api)
        self.assertEqual(0, code)
        self.assertEqual([7.0, 60], self.sleeps, "waits as long as Retry-After says, at most a minute")

    def test_network_errors_are_retried_then_reported(self):
        api = FakeApi(*[urllib.error.URLError("connection refused")] * validate.MAX_ATTEMPTS, VALID)
        code, log = self.run_action(api)
        self.assertEqual(1, code)
        self.assertIn("::error file=invoices/a.xml,title=Not checked::Could not get a validation result from https://valinvo.bmk-hub.com: connection refused", log)
        self.assertEqual(validate.MAX_ATTEMPTS - 1, len(self.sleeps))

    def test_api_errors_are_reported_with_a_hint(self):
        api = FakeApi(http_error(428, {"code": "terms_not_accepted", "message": "Accept the Terms of Use version 1.1."}),
                      http_error(400, {"code": "unsupported_document", "message": "The document has no specification identifier (BT-24)."}))
        code, log = self.run_action(api)
        self.assertEqual(1, code)
        self.assertIn("HTTP 428 terms_not_accepted: Accept the Terms of Use version 1.1. Set 'accept-terms' to the current version", log)
        self.assertIn("HTTP 400 unsupported_document: The document has no specification identifier (BT-24).", log)
        self.assertIn("⚠️ not checked", self.read(self.summary))

    def test_an_api_key_replaces_the_terms_header(self):
        api = FakeApi(VALID, VALID)
        self.run_action(api, api_key="ei_beta_secret", accept_terms="")
        self.assertEqual("Bearer ei_beta_secret", api.requests[0].get_header("Authorization"))
        self.assertIsNone(api.requests[0].get_header("X-accept-terms"))

    def test_configuration_problems_stop_before_any_request(self):
        for inputs, expected in [
            ({"accept_terms": ""}, "set the 'accept-terms' input"),
            ({"files": ""}, "The 'files' input is empty"),
            ({"files": "nothing/*.xml"}, "No files match nothing/*.xml"),
            ({"api_url": "http://valinvo.example"}, "must be an https:// URL"),
        ]:
            api = FakeApi()
            code, log = self.run_action(api, **inputs)
            self.assertEqual(1, code, inputs)
            self.assertIn(expected, log, inputs)
            self.assertEqual([], api.requests, inputs)

    def test_files_from_several_patterns_are_checked_once_each(self):
        api = FakeApi(VALID, VALID)
        self.run_action(api, files="invoices/a.xml, invoices/**/*.xml\ninvoices/nested/b.xml")
        self.assertEqual(2, len(api.requests))

    def test_local_api_url_may_use_http(self):
        api = FakeApi(VALID, VALID)
        self.assertEqual(0, self.run_action(api, api_url="http://localhost:8080/")[0])
        self.assertEqual("http://localhost:8080/v1/validate", api.requests[0].full_url)

    def test_locations_are_shortened_and_meaningless_ones_dropped(self):
        xpath = "/*:Invoice[namespace-uri()='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'][1]/*:Note[namespace-uri()='urn:x'][2]"
        self.assertEqual("/*:Invoice[1]/*:Note[2]", validate.readable_location({"location": xpath}))
        self.assertEqual("line 3, column 7: /*:Invoice[1]/*:Note[2]", validate.readable_location({"location": xpath, "line": 3, "column": 7}))
        self.assertIsNone(validate.readable_location({"location": "invoice.xml"}), "the API's internal file name means nothing to the user")
        self.assertEqual("line 3, column 7", validate.readable_location({"location": "invoice.xml", "line": 3, "column": 7}))
        issue = {"message": "cvc-complex-type.2.4.a: Invalid content", "location": "invoice.xml"}
        self.assertEqual("::error file=a.xml,title=XML schema::cvc-complex-type.2.4.a: Invalid content", validate.annotation("error", "a.xml", issue))

    def test_a_pdf_gets_no_line_annotation_but_names_the_line_in_its_xml(self):
        issue = {"rule": "BR-1", "message": "Wrong", "location": "/:Invoice[1]", "line": 3, "column": 7}
        self.assertEqual("::error file=a.PDF,title=BR-1::Wrong (at line 3, column 7: /:Invoice[1])", validate.annotation("error", "a.PDF", issue))
        self.assertEqual("::error file=a.xml,line=3,col=7,title=BR-1::Wrong", validate.annotation("error", "a.xml", issue))

    def test_english_explanation_follows_the_official_message(self):
        issue = {"rule": "BR-DE-15", "message": "Muss.", "explanation": "Required.", "line": 3, "column": 7}
        self.assertEqual("::error file=a.xml,line=3,col=7,title=BR-DE-15::Muss. (Required.)", validate.annotation("error", "a.xml", issue))

    def test_summary_cells_cannot_break_the_table_or_inject_html(self):
        self.assertEqual("a \\| b &lt;script&gt; c", validate.cell("a | b <script>\n c"))


if __name__ == "__main__":
    unittest.main()
