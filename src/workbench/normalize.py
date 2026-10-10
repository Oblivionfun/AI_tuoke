"""域名、公司名、国家的标准化。去重靠域名；制裁比对靠去掉法律后缀的公司名。"""

import re
import unicodedata
from functools import lru_cache
from urllib.parse import urlsplit

import pycountry
import tldextract

_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

_LEGAL_SUFFIXES: list[tuple[str, ...]] = sorted(
    (
        tuple(s.split())
        for s in [
            "co ltd",
            "co limited",
            "company limited",
            "pty ltd",
            "pvt ltd",
            "private limited",
            "l l c",
            "llc spc",
            "llc fz",
            "trading est",
            "est",
            "inc",
            "incorporated",
            "llc",
            "llp",
            "ltd",
            "limited",
            "corp",
            "corporation",
            "co",
            "company",
            "gmbh",
            "sa",
            "srl",
            "bv",
            "plc",
            "pty",
            "pte",
            "fze",
            "fzco",
            "fzc",
            "fz",
            "wll",
            "cjsc",
            "jsc",
        ]
    ),
    key=len,
    reverse=True,
)


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_company_name(name: str) -> str:
    """'Zujer Trading Company L.L.C.' → 'zujer trading'：小写、去标点、去掉末尾的法律后缀。"""
    s = _strip_accents(unicodedata.normalize("NFKC", name)).casefold()
    s = s.replace("&", " and ")
    tokens = re.sub(r"[^\w\s]", " ", s).split()
    changed = True
    while changed and len(tokens) > 1:
        changed = False
        for seq in _LEGAL_SUFFIXES:
            if len(tokens) > len(seq) and tuple(tokens[-len(seq) :]) == seq:
                tokens = tokens[: -len(seq)]
                changed = True
                break
    if tokens and tokens[0] == "the" and len(tokens) > 1:
        tokens = tokens[1:]
    return " ".join(tokens)


def display_company_name(name: str) -> str:
    """'PEAKFLOW YOGA, LLC.' → 'Peakflow Yoga'：去掉法律后缀，全大写的名字转成首字母大写。"""
    base = name.strip()
    if base.isupper():
        base = base.title()
    keep = len(normalize_company_name(base).split())
    words = base.replace(",", " ").split()
    while len(words) > keep and len(words) > 1:
        words.pop()
    return " ".join(words).strip(" .,") or name.strip()


def host_of(value: str) -> str | None:
    """从网址或邮箱里取出主机名。"""
    v = value.strip().lower()
    if not v:
        return None
    if "@" in v and "://" not in v:
        v = v.rsplit("@", 1)[1]
    if "://" not in v:
        v = "http://" + v
    try:
        host = urlsplit(v).hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def normalize_domain(value: str | None) -> str | None:
    """返回可注册域名（去掉 www 和子域名），无法识别时返回 None。"""
    if not value:
        return None
    host = host_of(value)
    if not host or "." not in host or re.fullmatch(r"[\d.]+", host):
        return None
    ext = _TLD(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return ".".join(host.split(".")[-2:])


_COUNTRY_ALIASES: dict[str, str] = {
    "uae": "AE",
    "u a e": "AE",
    "emirates": "AE",
    "dubai": "AE",
    "阿联酋": "AE",
    "ksa": "SA",
    "saudi": "SA",
    "saudi arabia": "SA",
    "沙特": "SA",
    "沙特阿拉伯": "SA",
    "south africa": "ZA",
    "南非": "ZA",
    "usa": "US",
    "uk": "GB",
    "美国": "US",
    "英国": "GB",
    "中国": "CN",
    "卡塔尔": "QA",
    "科威特": "KW",
    "阿曼": "OM",
    "巴林": "BH",
    "埃及": "EG",
    "尼日利亚": "NG",
    "肯尼亚": "KE",
}


@lru_cache(maxsize=1024)
def normalize_country(value: str | None) -> str | None:
    """'阿联酋' / 'UAE' / 'ARE' / 'United Arab Emirates' → 'AE'。"""
    if not value or not value.strip():
        return None
    raw = value.strip()
    key = " ".join(re.sub(r"[^\w\s]", " ", _strip_accents(raw).casefold()).split())
    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    if raw in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[raw]
    if len(raw) == 2 and raw.isalpha():
        return raw.upper() if pycountry.countries.get(alpha_2=raw.upper()) else None
    if len(raw) == 3 and raw.isalpha():
        country = pycountry.countries.get(alpha_3=raw.upper())
        if country is not None:
            return country.alpha_2
    try:
        return pycountry.countries.lookup(raw).alpha_2
    except LookupError:
        return None
