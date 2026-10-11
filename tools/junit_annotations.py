"""Turn a pytest JUnit report into GitHub annotations.

A run's logs need a signed-in GitHub account to read; its annotations do
not. Each failing test becomes one `::error` line - test name and the
first lines of the failure - so why a run went red can be seen from the
run page, or the API, without the log.

    python tools/junit_annotations.py test-results.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

MAX_LINES = 12
MAX_REPORTED = 20


def _escape(text: str) -> str:
    # The workflow-command encoding for a message.
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main(path: str) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::warning title=No test report::{_escape(str(exc))}")
        return 0
    reported = 0
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            name = f"{case.get('classname', '')}.{case.get('name', '')}".strip(".")
            body = (node.text or node.get("message") or "").strip().splitlines()
            # The end of a traceback is where the reason is.
            text = "\n".join(body[-MAX_LINES:])
            print(f"::error title={_escape(name)}::{_escape(text)}")
            reported += 1
            if reported >= MAX_REPORTED:
                return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "test-results.xml"))
