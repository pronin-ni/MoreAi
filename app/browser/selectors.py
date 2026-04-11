"""Selector strategy abstraction for browser providers.

Replaces ad-hoc lists of lambda selectors scattered across provider
implementations with a structured, data-driven model.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from playwright.async_api import Locator, Page


class SelectorKind(Enum):
    """How a single selector should be resolved."""

    ROLE = "role"
    CSS = "css"
    TEXT = "text"
    PLACEHOLDER = "placeholder"
    LABEL = "label"
    RAW = "raw"  # already a full locator string


@dataclass(frozen=True, slots=True)
class SelectorDef:
    """A single selector definition.

    Parameters
    ----------
    kind : SelectorKind
        Strategy used to build the Playwright locator.
    value : str
        The selector value (role name, CSS selector, text, …).
    extra : str | None
        Optional second argument (e.g. role options dict rendered as string
        that the helper will ``eval`` — kept simple to avoid runtime eval;
        callers should use ``Raw`` for complex cases).
    first : bool
        If ``True``, append ``.first`` to the resulting locator.
    last : bool
        If ``True``, append ``.last`` to the resulting locator.
    nth : int | None
        If set, call ``.nth(nth)`` on the resulting locator.
    description : str | None
        Human-readable label used in logging / telemetry.
    """

    kind: SelectorKind
    value: str
    extra: str | None = None
    first: bool = False
    last: bool = False
    nth: int | None = None
    description: str | None = None

    def resolve(self, page: Page) -> Locator:
        """Build a Playwright ``Locator`` from this definition."""
        loc: Locator
        if self.kind == SelectorKind.ROLE:
            loc = page.get_by_role(self.value)  # type: ignore[arg-type]
        elif self.kind == SelectorKind.CSS:
            loc = page.locator(self.value)
        elif self.kind == SelectorKind.TEXT:
            loc = page.get_by_text(self.value, exact=False)
        elif self.kind == SelectorKind.PLACEHOLDER:
            loc = page.get_by_placeholder(self.value)
        elif self.kind == SelectorKind.LABEL:
            loc = page.get_by_label(self.value)
        elif self.kind == SelectorKind.RAW:
            loc = page.locator(self.value)
        else:
            raise ValueError(f"Unknown SelectorKind: {self.kind}")

        if self.first:
            loc = loc.first
        if self.last:
            loc = loc.last
        if self.nth is not None:
            loc = loc.nth(self.nth)
        return loc

    @classmethod
    def role(cls, role: str, *, name: str | None = None, first: bool = False, last: bool = False,
             nth: int | None = None, description: str | None = None) -> SelectorDef:
        """Convenience constructor for role-based selectors."""
        if name:
            return cls(
                kind=SelectorKind.ROLE, value=role,
                first=first, last=last, nth=nth, description=description,
            )
        return cls(
            kind=SelectorKind.ROLE, value=role,
            first=first, last=last, nth=nth, description=description,
        )

    @classmethod
    def css(cls, selector: str, *, first: bool = False, last: bool = False,
            nth: int | None = None, description: str | None = None) -> SelectorDef:
        return cls(
            kind=SelectorKind.CSS, value=selector,
            first=first, last=last, nth=nth, description=description,
        )

    @classmethod
    def text(cls, text: str, *, first: bool = False, description: str | None = None) -> SelectorDef:
        return cls(kind=SelectorKind.TEXT, value=text, first=first, description=description)

    @classmethod
    def placeholder(cls, text: str, *, first: bool = False, description: str | None = None) -> SelectorDef:
        return cls(kind=SelectorKind.PLACEHOLDER, value=text, first=first, description=description)

    @classmethod
    def label(cls, text: str, *, first: bool = False, description: str | None = None) -> SelectorDef:
        return cls(kind=SelectorKind.LABEL, value=text, first=first, description=description)

    @classmethod
    def raw(cls, selector: str, *, first: bool = False, last: bool = False,
            nth: int | None = None, description: str | None = None) -> SelectorDef:
        return cls(
            kind=SelectorKind.RAW, value=selector,
            first=first, last=last, nth=nth, description=description,
        )


@dataclass(frozen=True, slots=True)
class SelectorStrategy:
    """An ordered list of fallback selectors for a specific UI element.

    The provider tries each selector in order until one is visible.
    """

    name: str
    selectors: tuple[SelectorDef, ...]

    @classmethod
    def build(cls, name: str, selectors: list[SelectorDef]) -> SelectorStrategy:
        return cls(name=name, selectors=tuple(selectors))


# ---------------------------------------------------------------------------
# Convenience: legacy-style factory that converts old lambda lists into
# SelectorStrategy instances.  Providers can migrate incrementally.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SelectorBuilder:
    """Builds a SelectorStrategy from declarative dicts (legacy-friendly).

    Accepts the same shape many providers already use internally::

        {"role": "textbox", "name": "Chat with ChatGPT", "first": True}
        {"css": "textarea", "first": True}
        {"text": "Continue with Google"}
    """

    name: str
    definitions: list[dict]

    def build(self) -> SelectorStrategy:
        selectors: list[SelectorDef] = []
        for d in self.definitions:
            sel = self._one(d)
            if sel:
                selectors.append(sel)
        return SelectorStrategy(name=self.name, selectors=tuple(selectors))

    @staticmethod
    def _one(d: dict) -> SelectorDef | None:
        if "role" in d:
            return SelectorDef.role(
                d["role"],
                name=d.get("name"),
                first=d.get("first", False),
                last=d.get("last", False),
                nth=d.get("nth"),
                description=d.get("description"),
            )
        if "css" in d:
            return SelectorDef.css(
                d["css"],
                first=d.get("first", False),
                last=d.get("last", False),
                nth=d.get("nth"),
                description=d.get("description"),
            )
        if "text" in d:
            return SelectorDef.text(
                d["text"],
                first=d.get("first", False),
                description=d.get("description"),
            )
        if "placeholder" in d:
            return SelectorDef.placeholder(
                d["placeholder"],
                first=d.get("first", False),
                description=d.get("description"),
            )
        if "label" in d:
            return SelectorDef.label(
                d["label"],
                first=d.get("first", False),
                description=d.get("description"),
            )
        if "raw" in d:
            return SelectorDef.raw(
                d["raw"],
                first=d.get("first", False),
                last=d.get("last", False),
                nth=d.get("nth"),
                description=d.get("description"),
            )
        return None
