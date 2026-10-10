"""制裁名单名称比对（只做提示，不拦截）。

数据源是美国综合筛查清单 CSL（公共领域，合并 OFAC SDN、BIS Entity List 等名单，免 key 下载）。
按 OFAC 50% 规则，只比对名称不够；命中只在卡片上提示，由人判断。
"""

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rapidfuzz import fuzz, process

from workbench.normalize import normalize_company_name, normalize_country

CSL_CSV_URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv"
_COUNTRY_TAIL = re.compile(r",\s*([A-Z]{2})\s*$")


@dataclass
class SanctionEntry:
    name: str
    source: str = ""
    programs: str = ""
    alt_names: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)

    @property
    def normalized(self) -> str:
        return normalize_company_name(self.name)


@dataclass
class SanctionMatch:
    listed_name: str
    score: float
    source: str
    programs: str
    countries: list[str]


def download_csl(dest: Path, client: httpx.Client | None = None) -> Path:
    client = client or httpx.Client(timeout=120, follow_redirects=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", CSL_CSV_URL) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    return dest


def _countries(addresses: str) -> list[str]:
    found: set[str] = set()
    for address in addresses.split(";"):
        match = _COUNTRY_TAIL.search(address.strip())
        code = normalize_country(match.group(1)) if match else None
        if code:
            found.add(code)
    return sorted(found)


def read_csl(path: Path) -> list[SanctionEntry]:
    """读取 CSL 格式的 CSV（name、alt_names、addresses、source、programs、type），只保留实体。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    entries: list[SanctionEntry] = []
    for row in csv.DictReader(io.StringIO(text)):
        lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = lowered.get("name") or lowered.get("sdn_name") or ""
        if not name or lowered.get("type", "").lower() == "individual":
            continue
        entries.append(
            SanctionEntry(
                name=name[:500],
                source=lowered.get("source", ""),
                programs=lowered.get("programs", "")[:500],
                alt_names=[a.strip() for a in lowered.get("alt_names", "").split(";") if a.strip()][:20],
                countries=_countries(lowered.get("addresses", "")),
            )
        )
    return entries


class SanctionScreener:
    """把名单一次性读进内存，用 RapidFuzz 做与词序无关的名称比对。"""

    def __init__(self, entries: Iterable[SanctionEntry], threshold: float = 92.0):
        self.threshold = threshold
        self._choices: list[str] = []
        self._owners: list[SanctionEntry] = []
        for entry in entries:
            for candidate in [entry.normalized, *(normalize_company_name(a) for a in entry.alt_names)]:
                if len(candidate) >= 4:
                    self._choices.append(candidate)
                    self._owners.append(entry)

    def match(self, name: str, country: str | None) -> SanctionMatch | None:
        normalized = normalize_company_name(name)
        if not self._choices or len(normalized) < 4:
            return None
        best = process.extractOne(normalized, self._choices, scorer=fuzz.token_sort_ratio, score_cutoff=self.threshold)
        if best is None:
            return None
        _, score, index = best
        entry = self._owners[index]
        if entry.countries and country and country not in entry.countries and score < 97:
            return None
        return SanctionMatch(entry.name, float(score), entry.source, entry.programs, entry.countries)
