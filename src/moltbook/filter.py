"""Defence-in-depth content filter — blocks fleet-internal leaks even when
an agent flags `[public]` by mistake.

This is the second line of defence:
  1. Primary : the agent prefixes a message with `[public]` consciously.
  2. Secondary (here) : the filter rejects content matching a blocklist of
     fleet-internal keywords / paths, regardless of the `[public]` flag.

The filter is intentionally **strict and noisy** — it logs the matching
pattern and refuses to post. A false positive ("you blocked my legit public
message") is preferable to leaking client names, internal URLs, or memory
artefact paths to a public social network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class FilterReject(Exception):
    """Raised when content matches a blocklisted pattern."""

    def __init__(self, pattern: str, fragment: str) -> None:
        super().__init__(f"blocked by pattern '{pattern}': '{fragment[:80]}'")
        self.pattern = pattern
        self.fragment = fragment


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    why: str


_DEFAULT_RULES: tuple[_Rule, ...] = (
    _Rule("memory_artifact_path",
          re.compile(r"\.ubik-memory|handoff_[a-zA-Z0-9_\-]+\.md|MEMORY\.md|journal/\d{4}-\d{2}-\d{2}"),
          "Mention d'un artefact ubik-memory (handoff, MEMORY.md, journal)."),
    _Rule("ubik_internal_dir",
          re.compile(r"\.claude-fleet|\.ubik-desktop"),
          "Référence à un répertoire interne fleet."),
    _Rule("internal_url",
          re.compile(r"localhost:\d+|127\.0\.0\.1:\d+|dev-station-02|10\.\d+\.\d+\.\d+|172\.\d+\.\d+\.\d+"),
          "URL ou hôte interne."),
    _Rule("lba_internal",
          re.compile(r"LBA-DESKTOP|PRISMA[_-]?(?:URL|API_KEY)|REP_TO_PRISMA_ID|HATTON_PERIMETRE", re.IGNORECASE),
          "Identifiant interne LBA-DESKTOP / PRISMA."),
    _Rule("lba_client_name",
          re.compile(r"BOULANGERIE LE FOURNIL|RESTAURANT DU PORT|HOTEL DES DUNES|FLEUR DE LYS|FERNANDO PIRES|STAR LCL|DEPANBIERES|AD'BOISSONS", re.IGNORECASE),
          "Nom de client LBA susceptible d'être confidentiel."),
    _Rule("github_token",
          re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
          "Token GitHub détecté."),
    _Rule("api_key_like",
          re.compile(
              r"(?:api[_-]?key|secret|password)\s*[:=]\s*\S{8,}"
              r"|bearer\s+[A-Za-z0-9_\-\.]{8,}",
              re.IGNORECASE,
          ),
          "Forme générique 'api_key=...' / 'secret=...' / 'Bearer <token>'."),
)


class ContentFilter:
    """Stateless validator. Wrap with `try / except FilterReject` at the post site."""

    def __init__(self, *, extra_rules: tuple[tuple[str, str, str], ...] = ()) -> None:
        self._rules: list[_Rule] = list(_DEFAULT_RULES)
        for name, pattern, why in extra_rules:
            self._rules.append(_Rule(name, re.compile(pattern), why))

    def check(self, content: str) -> None:
        """Raises `FilterReject` if any rule matches. Returns `None` on pass."""
        if not isinstance(content, str):
            raise FilterReject("non_string_content", repr(content)[:80])
        for rule in self._rules:
            m = rule.pattern.search(content)
            if m:
                raise FilterReject(rule.name, m.group(0))

    def rule_names(self) -> list[str]:
        return [r.name for r in self._rules]
