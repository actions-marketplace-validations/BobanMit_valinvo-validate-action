#!/usr/bin/env python3
"""Valinvo e-invoice validator for GitHub Actions.

Sends each matched invoice file to the Valinvo API (POST /v1/validate) and reports the result as
workflow annotations, a job summary and step outputs. Standard library only, Python 3.8+.
"""
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

VERSION = "1.0.0"
USER_AGENT = "valinvo-github-action/" + VERSION
MAX_ATTEMPTS = 5
MAX_WAIT_SECONDS = 60
MAX_FILE_BYTES = 20 * 1024 * 1024
ISSUES_PER_FILE_IN_SUMMARY = 20
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Settings:
    def __init__(self, patterns: List[str], api_url: str, api_key: str, accept_terms: str, fail_on_warnings: bool):
        self.patterns = patterns
        self.api_url = api_url
        self.api_key = api_key
        self.accept_terms = accept_terms
        self.fail_on_warnings = fail_on_warnings


class Outcome:
    """The result for one file: a validation report from the API, or why there is none."""

    def __init__(self, path: str, report: Optional[dict] = None, failure: Optional[str] = None):
        self.path = path
        self.report = report
        self.failure = failure

    @property
    def errors(self) -> List[dict]:
        return (self.report or {}).get("errors", [])

    @property
    def warnings(self) -> List[dict]:
        return (self.report or {}).get("warnings", [])

    @property
    def status(self) -> str:
        if self.failure is not None:
            return "failed"
        return "valid" if self.report.get("valid") else "invalid"


def settings_from_env(env: Dict[str, str]) -> Settings:
    patterns = [p.strip() for p in re.split(r"[\n,]", env.get("INPUT_FILES", "")) if p.strip()]
    return Settings(
        patterns=patterns,
        api_url=(env.get("INPUT_API_URL", "").strip() or "https://valinvo.bmk-hub.com").rstrip("/"),
        api_key=env.get("INPUT_API_KEY", "").strip(),
        accept_terms=env.get("INPUT_ACCEPT_TERMS", "").strip(),
        fail_on_warnings=env.get("INPUT_FAIL_ON_WARNINGS", "false").strip().lower() == "true",
    )


def configuration_problem(settings: Settings) -> Optional[str]:
    if not settings.patterns:
        return "The 'files' input is empty. Give one or more glob patterns, e.g. 'invoices/**/*.xml' or 'invoices/**/*.pdf'."
    if not settings.api_key and not settings.accept_terms:
        return ("Without an API key, set the 'accept-terms' input to the version of the Valinvo Terms of Use you accept "
                "(currently '1.0', see https://valinvo.bmk-hub.com/terms), or pass an API key in 'api-key'.")
    url = urllib.parse.urlsplit(settings.api_url)
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in LOCAL_HOSTS):
        return "The 'api-url' input must be an https:// URL (http:// only for localhost)."
    return None


def find_files(patterns: List[str]) -> List[str]:
    """Files matching any pattern, relative to the working directory, '/'-separated, sorted, without duplicates."""
    found = set()
    for pattern in patterns:
        for match in glob.glob(pattern, recursive=True):
            if os.path.isfile(match):
                found.add(os.path.normpath(match).replace(os.sep, "/"))
    return sorted(found)


def retry_after(headers) -> float:
    try:
        seconds = float(headers.get("Retry-After", "5"))
    except (TypeError, ValueError):
        seconds = 5
    return max(1.0, min(seconds, MAX_WAIT_SECONDS))


def describe_http_error(status: int, body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
        code, message = payload.get("code", ""), payload.get("message", "")
    except (ValueError, AttributeError, UnicodeDecodeError):
        code, message = "", ""
    text = "HTTP {}{}{}".format(status, " " + code if code else "", ": " + message if message else "")
    hints = {
        401: " Check the API key in the 'api-key' input (usually a repository secret).",
        428: " Set 'accept-terms' to the current version shown at https://valinvo.bmk-hub.com/terms.",
    }
    return text + hints.get(status, "")


def validate_file(path: str, settings: Settings, opener: Callable, sleep: Callable[[float], None]) -> Outcome:
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        return Outcome(path, failure="The file has {:,} bytes; the validator accepts up to 20 MB.".format(size))
    with open(path, "rb") as f:
        body = f.read()
    content_type = "application/pdf" if body.startswith(b"%PDF-") else "application/xml"
    headers = {"Content-Type": content_type, "Accept": "application/json", "User-Agent": USER_AGENT}
    if settings.api_key:
        headers["Authorization"] = "Bearer " + settings.api_key
    else:
        headers["X-Accept-Terms"] = settings.accept_terms
    url = settings.api_url + "/v1/validate"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener(request, timeout=120) as response:
                return Outcome(path, report=json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            with e:
                if e.code in (429, 503) and attempt < MAX_ATTEMPTS:
                    sleep(retry_after(e.headers))
                    continue
                return Outcome(path, failure=describe_http_error(e.code, e.read()))
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt < MAX_ATTEMPTS and not isinstance(e, ValueError):
                sleep(min(2 ** attempt, MAX_WAIT_SECONDS))
                continue
            reason = getattr(e, "reason", e)
            return Outcome(path, failure="Could not get a validation result from {}: {}".format(settings.api_url, reason))
    raise AssertionError("unreachable")


# --- GitHub workflow commands, summary and outputs ---------------------------------------------

def escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def xpath(issue: dict) -> Optional[str]:
    """The XPath without its namespace filters; None for anything else (e.g. the API's own file name)."""
    location = issue.get("location")
    if not location or not location.startswith("/"):
        return None
    return re.sub(r"\[namespace-uri\(\)='[^']*'\]", "", location)


def readable_location(issue: dict) -> Optional[str]:
    line = issue.get("line")
    position = "line {}, column {}".format(line, issue.get("column")) if line else None
    return ": ".join(part for part in (position, xpath(issue)) if part) or None


def with_explanation(issue: dict) -> str:
    """The official message, plus our English when it exists only in German."""
    explanation = issue.get("explanation")
    message = issue.get("message", "")
    return "{} ({})".format(message, explanation) if explanation else message


def annotation(kind: str, path: str, issue: dict) -> str:
    rule = issue.get("rule") or "XML schema"
    location = xpath(issue)
    # The line of a PDF's embedded XML means nothing in the PDF file itself, so it stays in the message.
    line = None if path.lower().endswith(".pdf") else issue.get("line")
    properties = "file=" + escape_property(path)
    if line:
        properties += ",line={},col={}".format(line, issue.get("column"))
    elif issue.get("line"):
        location = readable_location(issue)
    properties += ",title=" + escape_property(rule)
    message = with_explanation(issue)
    if location and line is None:
        message += " (at " + location + ")"
    return "::{} {}::{}".format(kind, properties, escape_data(message))


def cell(text: str) -> str:
    text = " ".join(str(text).split())
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")


RESULT_LABELS = {"valid": "✅ valid", "invalid": "❌ invalid", "failed": "⚠️ not checked"}


def summary(outcomes: List[Outcome]) -> str:
    lines = [
        "## Valinvo e-invoice validation",
        "",
        "| File | Rule set | Result | Errors | Warnings |",
        "|---|---|---|---:|---:|",
    ]
    for o in outcomes:
        rule_set = (o.report or {}).get("ruleSet", "")
        lines.append("| `{}` | {} | {} | {} | {} |".format(
            cell(o.path), "`" + cell(rule_set) + "`" if rule_set else "–", RESULT_LABELS[o.status],
            len(o.errors) if o.report else "–", len(o.warnings) if o.report else "–"))
    for o in outcomes:
        if o.failure:
            lines += ["", "**`{}`**: {}".format(cell(o.path), cell(o.failure))]
        issues = [("error", i) for i in o.errors] + [("warning", i) for i in o.warnings]
        if o.report and issues:
            lines += ["", "<details><summary><code>{}</code>: {} error(s), {} warning(s)</summary>".format(cell(o.path), len(o.errors), len(o.warnings)), "",
                      "| | Rule | Message | Location |", "|---|---|---|---|"]
            for kind, issue in issues[:ISSUES_PER_FILE_IN_SUMMARY]:
                lines.append("| {} | `{}` | {} | {} |".format(kind, cell(issue.get("rule") or "XML schema"), cell(with_explanation(issue)), cell(readable_location(issue) or "")))
            if len(issues) > ISSUES_PER_FILE_IN_SUMMARY:
                lines.append("")
                lines.append("…and {} more in the step log.".format(len(issues) - ISSUES_PER_FILE_IN_SUMMARY))
            lines += ["", "</details>"]
    lines += ["", "Checked against the official rules by [Valinvo](https://valinvo.bmk-hub.com)."]
    return "\n".join(lines) + "\n"


def append(path: Optional[str], text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def report_json(outcomes: List[Outcome]) -> str:
    return json.dumps({"files": [
        {"path": o.path, "status": o.status, "ruleSet": (o.report or {}).get("ruleSet"),
         "errors": o.errors, "warnings": o.warnings, "failure": o.failure}
        for o in outcomes
    ]}, indent=2, ensure_ascii=False)


def main(env: Dict[str, str] = os.environ, opener: Callable = urllib.request.urlopen,
         sleep: Callable[[float], None] = time.sleep, out=sys.stdout) -> int:
    def say(text: str) -> None:
        print(text, file=out, flush=True)

    settings = settings_from_env(env)
    problem = configuration_problem(settings)
    if problem:
        say("::error title=Valinvo configuration::" + escape_data(problem))
        return 1
    files = find_files(settings.patterns)
    if not files:
        say("::error title=Valinvo::" + escape_data("No files match " + ", ".join(settings.patterns) + " (relative to " + os.getcwd() + ")."))
        return 1

    outcomes = []
    for path in files:
        outcome = validate_file(path, settings, opener, sleep)
        outcomes.append(outcome)
        if outcome.failure:
            say("::error file={},title=Not checked::{}".format(escape_property(path), escape_data(outcome.failure)))
            continue
        say("{}: {} ({}), {} error(s), {} warning(s)".format(path, outcome.status, outcome.report.get("ruleSet"), len(outcome.errors), len(outcome.warnings)))
        for issue in outcome.errors:
            say(annotation("error", path, issue))
        for issue in outcome.warnings:
            say(annotation("warning", path, issue))

    report_path = os.path.join(env.get("RUNNER_TEMP") or os.getcwd(), "valinvo-report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_json(outcomes))
    append(env.get("GITHUB_STEP_SUMMARY"), summary(outcomes))
    failed = [o for o in outcomes if o.status != "valid" or (settings.fail_on_warnings and o.warnings)]
    append(env.get("GITHUB_OUTPUT"), "checked={}\nfailed={}\nreport={}\n".format(len(outcomes), len(failed), report_path))
    say("Checked {} file(s): {} failed.".format(len(outcomes), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows runners default to a legacy code page
    sys.exit(main())
