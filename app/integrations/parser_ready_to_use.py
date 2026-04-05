import re

from app.integrations.types import ParsedReadyToUseData


def parse_ready_to_use_markdown(markdown: str) -> ParsedReadyToUseData:
    return ParsedReadyToUseData(
        base_urls=_parse_base_urls(markdown),
        supported_api_routes=_parse_supported_routes(markdown),
        individual_clients=_parse_individual_clients(markdown),
    )


def _parse_base_urls(markdown: str) -> list[dict[str, str]]:
    pattern = re.compile(r"\| \[(https?://[^\]]+)\]\([^\)]+\)\s+\|\s+([^|]+?)\s+\|\s+([^|]+?)\|")
    result: list[dict[str, str]] = []
    for base_url, api_key, notes in pattern.findall(markdown):
        result.append(
            {
                "base_url": base_url.strip(),
                "api_key": api_key.strip(),
                "notes": notes.strip(),
            }
        )
    return result


def _parse_supported_routes(markdown: str) -> list[dict[str, str]]:
    section_match = re.search(
        r"### Also Supported API Routes:\s*(.*?)\s*### Individual clients available for:",
        markdown,
        re.S,
    )
    if not section_match:
        return []

    result: list[dict[str, str]] = []
    pattern = re.compile(r"- \*\*(.+?)\*\*: <(https?://[^>]+)>")
    for display_name, base_url in pattern.findall(section_match.group(1)):
        result.append({"display_name": display_name.strip(), "base_url": base_url.strip()})
    return result


def _parse_individual_clients(markdown: str) -> list[dict[str, str]]:
    section_match = re.search(
        r"### Individual clients available for:\s*(.*?)(?:\s*### How to choose a base URL|\Z)",
        markdown,
        re.S,
    )
    if not section_match:
        return []

    result: list[dict[str, str]] = []
    for line in section_match.group(1).splitlines():
        line = line.strip()
        if not line.startswith("- ["):
            continue
        match = re.match(r"- \[(.+?)\]\(([^\)]+)\)", line)
        if not match:
            continue
        display_name, docs_url = match.groups()
        if "Providers Documentation" in display_name:
            continue
        result.append({"display_name": display_name.strip(), "docs_url": docs_url.strip()})
    return result
