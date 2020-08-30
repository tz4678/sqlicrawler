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
