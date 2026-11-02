"""Prints the current stable desktop Chrome major version (e.g. 156) for the
image's link-import User-Agent, from Google's VersionHistory API. CI passes
it to the Docker build as CHROME_MAJOR. On any problem it prints nothing and
a GitHub warning, and the build uses the version written in url_ingest.py:
a Google outage must not stop a release. Standard library only."""
import json
import re
import sys
import urllib.request

URL = ("https://versionhistory.googleapis.com/v1/chrome/platforms/win/channels/stable/versions"
       "?order_by=version%20desc&pageSize=5")
# url_ingest.chrome_major() refuses anything else too.
LOW, HIGH = 100, 999


def major_from(payload: dict) -> int | None:
    """The highest major in the answer, or None. Takes the highest of all
    returned rather than trusting the order."""
    majors = []
    for v in payload.get("versions") or []:
        m = re.fullmatch(r"(\d+)\.\d+\.\d+\.\d+", str((v or {}).get("version", "")))
        if m and LOW <= int(m.group(1)) <= HIGH:
            majors.append(int(m.group(1)))
    return max(majors) if majors else None


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=20) as resp:
            payload = json.load(resp)
        major = major_from(payload)
    except Exception as exc:          # network, HTTP error, bad JSON
        print(f"::warning::Chrome version lookup failed ({type(exc).__name__}: {exc}); "
              "using the version in url_ingest.py", file=sys.stderr)
        return 0
    if major is None:
        print("::warning::Chrome version lookup gave no usable version; using the version in url_ingest.py",
              file=sys.stderr)
        return 0
    print(major)
    return 0


if __name__ == "__main__":
    sys.exit(main())
