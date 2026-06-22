def chunk(text: str, size: int = 1000) -> list[str]:
    text = text or ""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def to_storm(result: dict) -> list[dict]:
    """Map a search_and_fetch() output dict to the STORM/dspy retriever shape."""
    if result.get("status") != "ok":
        return []
    return [
        {
            "url": x["url"],
            "title": x.get("title", ""),
            "description": x.get("snippet", ""),
            "snippets": chunk(x.get("text", "")),
        }
        for x in result.get("results", [])
        if x.get("text")
    ]
