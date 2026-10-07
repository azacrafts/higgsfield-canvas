import threading
from pinscrape import Pinterest
from typing import TypedDict


class PinImage(TypedDict):
    url: str
    title: str


def fetch_pinterest_images(query: str, max_results: int = 5, timeout: int = 15) -> list[PinImage]:
    print(f"[pinterest] Fetching images for: {query!r}")
    result: list[PinImage] = []

    def _fetch():
        try:
            p = Pinterest(proxies={}, sleep_time=0)
            urls = p.search(query, max_results)
            print(f"[pinterest] Got {len(urls)} URLs")
            result.extend(
                {"url": str(url), "title": query}
                for url in urls[:max_results]
                if url
            )
        except Exception as e:
            print(f"[pinterest] pinscrape failed: {e}")

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        print(f"[pinterest] Timed out after {timeout}s")

    return result
