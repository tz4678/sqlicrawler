import asyncio
import dataclasses
import io
import re
import threading
from collections import Counter
from functools import partial, wraps
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import click
import ujson as json
import yarl

from .types import Function

# unused
echo = partial(click.secho, err=True)


def coro(f: Function) -> Function:
    @wraps(f)
    def wrapper(*args: Tuple[Any, ...], **kwargs: Dict[str, Any]) -> Any:
        return asyncio.run(f(*args, **kwargs))

    return wrapper


def normalize_url(url: str) -> str:
    return url if '://' in url else 'http://' + url


class BlackList:
    def __init__(self, patterns: List[re.Pattern]) -> None:
        self.patterns = patterns

    def is_blacklisted(self, url: str) -> bool:
        regex: re.Pattern  # pylint: disable=unused-variable
        return any(regex.match(url) for regex in self.patterns)

    @classmethod
    def parse(cls, fp: io.TextIOBase) -> 'BlackList':
        patterns: List[re.Pattern] = list(
            map(
                cls._make_pattern,
                filter(None, map(cls._sanitize_line, fp.readlines())),
            )
        )
        return cls(patterns)

    @staticmethod
    def _sanitize_line(line: str) -> str:
        return re.sub('#.*', '', line).strip()

    @staticmethod
    def _make_pattern(pat: str) -> re.Pattern:
        return re.compile(re.escape(pat).replace(r'\*', '.+?'))


class VisitedUrls:
    def __init__(
        self, urls: Optional[Sequence[str]] = [], limit_per_site: int = -1
    ) -> None:
        # set использует хеш таблицы и быстрее списков, где используется линейный поиск при lookup
        # элемента
        self._urls: Set[str] = set()
        self._counter = Counter()
        # threadsafe
        self._lock = threading.RLock()
        self._limit_per_site = limit_per_site
        for url in urls:
            self.add(url)

    def add(self, url: str) -> None:
        with self._lock:
            if url in self._urls:
                return
            host: str = yarl.URL(url).host
            if not self.can_add(url, host):
                raise ValueError('per site limit exceeded')
            self._counter[host] += 1
            self._urls.add(url)

    def can_add(self, url: str, host: Optional[str] = None) -> bool:
        with self._lock:
            if self._limit_per_site == -1:
                return True
            if host is None:
                host = yarl.URL(url).host
            return self._limit_per_site > self._counter[host]

    def __contains__(self, value: str) -> bool:
        # по идее атомарная операция
        return value in self._urls

    def __len__(self) -> int:
        with self._lock:
            return len(self._urls)

    # TODO: add more methods


@dataclasses.dataclass
class ResultEntry:
    status: int
    url: str
    headers: Dict[str, str]
    cookies: Dict[str, str]
    data: Optional[Dict[str, Any]]
    error: Optional[str]


class ResultWriter:
    def __init__(self, output: io.TextIOBase) -> None:
        self.output = output

    def write(self, entry: ResultEntry) -> None:
        dct: Dict[str, Any] = dataclasses.asdict(entry)
        self.output.write(json.dumps(dct, ensure_ascii=False))
        self.output.write('\n')
        self.output.flush()
