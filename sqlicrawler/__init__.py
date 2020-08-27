import asyncio
import cgi
import io
import itertools
import logging
import os
import re
import sys
from functools import partial
from os import getenv
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import parse_qsl, quote

import aiohttp
import click
import multidict
import ujson as json
import yarl
from aiohttp_socks import ProxyConnector
from pyppeteer import launch
from pyppeteer.browser import Browser
from pyppeteer.errors import PyppeteerError
from pyppeteer.errors import TimeoutError as PyppeteerTimeoutError
from pyppeteer.network_manager import Request, Response
from pyppeteer.page import Page

from .log import logger
from .meta import (
    __author__,
    __email__,
    __license__,
    __package_name__,
    __version__,
)
from .types import Payload
from .utils import (
    BlackList,
    ResultEntry,
    ResultWriter,
    coro,
    echo,
    normalize_url,
)

CURRENT_PATH: str = os.path.dirname(__file__)

CONFIG_PATH: str = os.path.expanduser(
    os.path.join(getenv('XDG_CONFIG_DIR', '~/.config'), __package_name__)
)

BLACKLIST_FILENAME: str = 'blacklist.txt'
QUOTE_CHARS: str = '\'"'

SQLI_ERROR: re.Pattern = re.compile(
    '|'.join(
        [
            'You have an error in your SQL syntax',
            'Unclosed quotation mark after the character string',
            # Иногда при ошибке выводится полный запрос
            r'SELECT \* FROM',
            # Название PHP функций
            'mysqli?_num_rows',
            'mysqli?_query',
            'mysqli?_fetch_(?:array|assoc|field|object|row)',
            'mysqli?_result',
            # bitrix
            '<b>DB query error.</b>',
            # pg_query
            'Query failed',
            # common PHP errors
            '<b>(?:Fatal error|Warning)</b>:',
        ]
    )
)

PROXY_MAPPING: Dict[str, str] = {'tor': 'socks5://localhost:9050'}


@click.command()
@click.version_option(__version__)
@click.option(
    '--aiohttp_timeout',
    default=10.0,
    help='aiohttp session timeout in seconds',
    type=float,
)
@click.option(
    '--checks',
    'checks_num',
    default=3,
    help='number of sqli checks for each request',
    type=int,
)
@click.option(
    '-d', '--depth', default=3, help='crawl depth', type=int,
)
@click.option(
    '-i',
    '--input',
    default=sys.stdin,
    help='input file',
    type=click.File('r', encoding='utf-8'),
)
@click.option(
    '--nav_timeout',
    default=10000,
    help='page navigation timeout in milliseconds',
    type=int,
)
@click.option(
    '-o',
    '--output',
    default=sys.stdout,
    help='output file',
    type=click.File('w+', encoding='utf-8'),
)
@click.option(
    '--proxy',
    help='proxy server address, e.g. `socks5://localhost:9050` or simple `tor`',
)
@click.option(
    '-u', '--useragent', help='custom user agent',
)
@click.option(
    '-v',
    '--verbosity',
    count=True,
    default=0,
    help='increase output verbosity: 0 - warning, 1 - info, 2 - debug',
)
@click.option(
    '-w',
    '--workers',
    'workers_num',
    default=10,
    help='number of workers',
    type=int,
)
@coro
async def main(
    checks_num: int,
    depth: int,
    input: io.TextIOBase,
    nav_timeout: int,
    output: io.TextIOBase,
    proxy: Optional[str],
    aiohttp_timeout: float,
    useragent: str,
    verbosity: int,
    workers_num: int,
) -> Optional[int]:
    r'''
      _____  ____  _      _  _____                    _
     / ____|/ __ \| |    (_)/ ____|                  | |
    | (___ | |  | | |     _| |     _ __ __ ___      _| | ___ _ __
     \___ \| |  | | |    | | |    | '__/ _` \ \ /\ / / |/ _ \ '__|
     ____) | |__| | |____| | |____| | | (_| |\ V  V /| |  __/ |
    |_____/ \___\_\______|_|\_____|_|  \__,_| \_/\_/ |_|\___|_|
    '''
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    levels: List[int] = [logging.WARNING, logging.INFO, logging.DEBUG]
    level: int = levels[min(verbosity, len(levels) - 1)]
    logger.setLevel(level)
    urls: List[str] = list(
        map(normalize_url, filter(None, map(str.strip, input.readlines())))
    )
    writer = ResultWriter(output)
    proxy = PROXY_MAPPING.get(proxy, proxy)
    blacklist_path: str = os.path.join(CONFIG_PATH, BLACKLIST_FILENAME)
    if not os.path.exists(blacklist_path):
        blacklist_path: str = os.path.join(CURRENT_PATH, BLACKLIST_FILENAME)
    with open(blacklist_path, 'r') as f:
        blacklist = BlackList.parse(f)
    crawler = SQLiCrawler(
        aiohttp_timeout=aiohttp_timeout,
        blacklist=blacklist,
        checks_num=checks_num,
        nav_timeout=nav_timeout,
        proxy=proxy,
        useragent=useragent,
        workers_num=workers_num,
        writer=writer,
    )
    await crawler.crawl(urls, depth)
    logger.info('finished')


class SQLiCrawler(object):
    def __init__(
        self,
        *,
        aiohttp_timeout: float,
        blacklist: BlackList,
        checks_num: int,
        nav_timeout: int,
        pages_num: int,
        proxy: Optional[str],
        useragent: Optional[str],
        writer: ResultWriter,
    ) -> None:
        self.aiohttp_timeout = aiohttp_timeout
        self.blacklist = blacklist
        self.checks_num = checks_num
        self.nav_timeout = nav_timeout
        self.pages_num = pages_num
        self.proxy = proxy
        self.useragent = useragent
        self.writer = writer

    async def crawl(self, urls: List[str], depth: int) -> None:
        self.url_queue = asyncio.Queue()
        url: str
        for url in urls:
            self.url_queue.put_nowait((url, depth))
        self.visited: Tuple[str] = set()
        # Запускаем таски в фоновом режиме
        workers: List[asyncio.Task] = [
            asyncio.create_task(self.worker()) for _ in range(self.workers_num)
        ]
        # Ждем пока очередь станет пустой
        await self.url_queue.join()
        worker: asyncio.Task
        # Останавливаем фоновые задания
        for worker in workers:
            worker.cancel()
        logger.info('finished')

    async def worker(self) -> None:
        task_name: str = asyncio.Task.current_task().get_name()
        logger.info('worker started: %s', task_name)
        # Вся эта затея для перезапуска браузера прикраше
        while True:
            logger.info('create new page')
            args: List[str] = [
                '--disable-accelerated-2d-canvas',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-setuid-sandbox',
                '--ignore-certificate-errors',
                '--lang=en-US',
                '--no-sandbox',
            ]
            if self.proxy:
                args += [f'--proxy-server={self.proxy}']
            browser = await launch(headless=True, args=args)
            page: Page = await self.browser.newPage()
            if self.useragent:
                # Если установить None, то к хуям все повиснет
                await page.setUserAgent(self.useragent)
            page.setDefaultNavigationTimeout(self.nav_timeout)
            await page.setRequestInterception(True)
            page.on('request', self.interception)
            page.on('response', self.check_sqli)
            while True:
                url: str
                depth: int
                # Если внутри try разместить, получим ошибку: task_done() called too many times...
                url, depth = await self.url_queue.get()
                try:
                    if url in self.visited:
                        logger.debug('already visited: %s', url)
                        continue
                    logger.debug('goto %s (%s)', url, task_name)
                    response: Response = await page.goto(
                        url, waitUntil='networkidle2'
                    )
                    if not response:
                        logger.warning('empty response')
                        continue
                    # if not os.path.exists('screenshot.png'):
                    #     await page.screenshot(path='screenshot.png')
                    self.visited.add(url)
                    self.visited.add(response.url)
                    if not response.ok:
                        continue
                    if url == 'http://httpbin.org/ip':
                        logger.info(
                            'chromium ip: %s',
                            (await response.json())['origin'],
                        )
                        continue
                    ct: str
                    _: Any
                    ct, _ = cgi.parse_header(
                        response.headers.get('content-type', '')
                    )
                    if ct != 'text/html':
                        continue
                    # fill and submit all forms
                    await page.evaluate(
                        """\
() =>
  [...document.forms].forEach(form => {
    for (let input of form) {
      if (!input.name) continue
      if (input.tagName === 'INPUT') {
        switch (input.type) {
          case 'hidden':
            continue
          case 'email':
            input.value = 'dummy@example.com'
            break
          case 'password':
            input.value = '123456a'
            break
          case 'tel':
            input.value = '+78005553535'
            break
          case 'url':
            input.value = 'https://example.com'
            break
          case 'number':
            input.value = 42
            break
          default:
            if (/user_?name|login/i.test(input.name)) {
              input.value = 'sema'
            } else if (/fisrt_?name/i.test(input.name)) {
              input.value = 'Semen'
            } else if (/last_?name/i.test(input.name)) {
              input.value = 'Semenov'
            } else {
              input.value = 'test'
            }
        }
      } else if (input.tagName == 'TEXTAREA') {
        input.value = 'Test message.'
      }
    }
    let url = form.action || location.href,
      method = (form.method || 'GET').toUpperCase(),
      params = new URLSearchParams(new FormData(form)),
      body = null
    if (method === 'POST') {
      body = params
    } else {
      url += (url.indexOf('?') === -1 ? '?' : '&') + params.toString()
    }
    fetch(url, { method, body })
  })"""
                    )
                    if depth < 1:
                        continue
                    links = await page.evaluate(
                        """\
() => [...document.getElementsByTagName('a')]
  .filter(a => a.hostname === location.hostname)
  .map(a => a.href)"""
                    )
                    for link in links:
                        await self.url_queue.put((link, depth - 1))
                except (PyppeteerError, PyppeteerTimeoutError) as e:
                    # Страницы часто крашатся и хз что с ними делать
                    logger.error(e)
                    await page.close()
                    break
                except Exception as e:
                    logger.exception(e)
                finally:
                    self.url_queue.task_done()

    async def interception(self, request: Request) -> None:
        # Ускоряем загрузку страницы, отменяя загрузку стилей, картинок, шрифтов и т.д.
        # https://github.com/puppeteer/puppeteer/blob/f7857d27c4091ebcd219a8180e258f3b61a5de35/new-docs/puppeteer.httprequest.resourcetype.md
        # TODO: элементы могут быть скрыты через CSS
        if request.resourceType in [
            'eventsource',
            'font',
            'image',
            'media',
            'stylesheet',
            'websocket',
        ] or self.blacklist.is_blacklisted(request.url):
            await request.abort()
        else:
            await request.continue_()

    async def check_sqli(self, response: Response) -> None:
        if response.status < 200 or response.status >= 500:
            logging.warning('bad status: %d', response.status)
            return
        content_type: str = cgi.parse_header(
            response.headers.get('content-type', '')
        )[0]
        if content_type not in ['text/html', 'application/json']:
            return
        try:
            result: Dict[str, Any] = await response._client.send(
                'Network.getCookies', {'urls': [response.url]}
            )
            # {'cookies': [{'name': 'cf_chl_1', 'value': '2869f5a1529140d', 'domain': 'javascript.ru', 'path': '/', 'expires': 1598448328, 'size': 23, 'httpOnly': False, 'secure': False, 'session': False}, {'name': 'vblastvisit', 'value': '1598443853', 'domain': '.javascript.ru', 'path': '/', 'expires': 1629979853.306669, 'size': 21, 'httpOnly': False, 'secure': False, 'session': False}, {'name': 'cf_chl_prog', 'value': 'b', 'domain': 'javascript.ru', 'path': '/', 'expires': 1598448332, 'size': 12, 'httpOnly': False, 'secure': False, 'session': False}, {'name': '__cfduid', 'value': 'd888c7b031e8db21e8c6d92d7d89991ba1598444719', 'domain': '.javascript.ru', 'path': '/', 'expires': 1601036719.306614, 'size': 51, 'httpOnly': True, 'secure': False, 'session': False, 'sameSite': 'Lax'}]}
            cookies: Dict[str, str] = {
                c['name']: c['value'] for c in result['cookies']
            }
            request: Request = response.request
            url: yarl.URL = yarl.URL(request.url)
            # {'referer': 'https://russiangeeks.com/', 'origin': 'https://russiangeeks.com', 'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/71.0.3542.0 Safari/537.36', 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'}
            data_type: str = cgi.parse_header(
                request.headers.get('content-type', '')
            )[0]
            is_urlencoded: bool = data_type == 'application/x-www-form-urlencoded'
            is_json: bool = data_type == 'application/json'
            data: Payload = None
            if request.postData:
                if is_urlencoded:
                    data = dict(
                        parse_qsl(request.postData, keep_blank_values=True)
                    )
                elif is_json:
                    data = json.loads(request.postData)
                else:
                    logging.warning(f'{data_type!r} is not supported')
                    return
            if self.proxy:
                connector = ProxyConnector.from_url(
                    self.proxy, verify_ssl=False
                )
            else:
                connector = aiohttp.TCPConnector(verify_ssl=False)
            timeout = aiohttp.ClientTimeout(total=self.aiohttp_timeout)
            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout
            ) as session:
                if request.url == 'http://httpbin.org/ip':
                    client_response: aiohttp.ClientResponse = await session.get(
                        request.url
                    )
                    data: Dict[str, Any] = await client_response.json()
                    logger.info('aiohttp.ClientSession ip: %s', data['origin'])
                    return
                logger.info('test sqli: %s', url)
                injected_url: yarl.URL
                injected_data: Payload
                for injected_url, injected_data in itertools.islice(
                    self.inject(url, data), self.checks_num
                ):
                    data_key: str = 'json' if is_json else 'data'
                    client_response: aiohttp.ClientResponse = await session.request(
                        request.method,
                        injected_url,
                        headers=request.headers,
                        cookies=cookies,
                        **{data_key: injected_data},
                    )
                    content = await client_response.text()
                    match: Union[None, re.Match]
                    if (match := SQLI_ERROR.search(content)) is not None:
                        logger.info('match: %s', match.group())
                        entry = ResultEntry(
                            status=client_response.status,
                            url=str(injected_url),
                            headers=request.headers,
                            cookies=cookies,
                            data=injected_data,
                            match=match.group(),
                        )
                        self.writer.write(entry)
        except Exception as e:
            logger.exception(e)

    def inject(
        self, url: yarl.URL, payload: Payload
    ) -> Iterable[Tuple[yarl.URL, Payload]]:
        if payload:
            i: str
            for i in payload:
                # поддержки вложенных массивов и объектов для JSON пока нет
                if not isinstance(payload[i], (bool, float, int, str)):
                    continue
                copy: Dict[str, Any] = payload.copy()
                if isinstance(copy[i], str):
                    copy[i] += QUOTE_CHARS
                else:
                    # None -> null
                    copy[i] = json.dumps(copy[i]) + QUOTE_CHARS
                yield url, copy
            # проводить дальнейшие проверки нет смысла
            return
        # Проверяем параметры URL в обратном порядке
        # TypeError: 'multidict._multidict.MultiDictProxy' object is not reversible
        i: str
        for i in reversed(list(url.query)):
            # как правильно тип описывать?
            copy: multidict.MultiDict[str] = url.query.copy()
            copy[i] += QUOTE_CHARS
            yield url.with_query(copy), payload
        # В последнюю очередь проверяем сегменты пути
        parts: List[str] = url.path.split('/')
        # так же в обратном порядке проверяем
        i: int
        for i in range(len(parts) - 1, -1, -1):
            if not parts[i]:
                continue
            copy: List[str] = parts.copy()
            copy[i] += quote(QUOTE_CHARS)
            yield url.with_path('/'.join(copy)), payload
