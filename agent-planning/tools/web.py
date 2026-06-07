import re
import urllib.request
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def webfetch(url: str) -> str:
    """Fetch a URL and return its full plain-text content (up to 2 MB)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Error fetching {url}: unsupported scheme '{parsed.scheme}'. Only http and https are allowed."
        max_bytes = 2 * 1024 * 1024
        req = urllib.request.Request(url, headers={"User-Agent": "agent/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get_content_type()
            if content_type and content_type not in (
                "text/html",
                "text/plain",
                "application/xhtml+xml",
            ):
                return f"Error fetching {url}: unsupported content type '{content_type}'."
            charset = resp.headers.get_content_charset() or "utf-8"
            raw_chunks = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raw_chunks.append(chunk[: max_bytes - (total - len(chunk))])
                    break
                raw_chunks.append(chunk)
        raw = b"".join(raw_chunks).decode(charset, errors="replace")
        soup = BeautifulSoup(raw, "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    except Exception as e:
        return f"Error fetching {url}: {e}"
