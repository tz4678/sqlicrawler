import asyncio
import cgi
import io
import itertools
import logging
import os
import random
import re
import sys
import weakref
from functools import partial
from os import getenv
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)
from urllib.parse import parse_qsl, quote

import aiohttp
import click
import multidict
import ujson as json
import yarl
from aiohttp_socks import ProxyConnector
from pyppeteer import launch
from pyppeteer.browser import Browser
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
    VisitedUrls,
    coro,
    echo,
    normalize_url,
)

CURRENT_PATH: str = os.path.dirname(__file__)

CONFIG_PATH: str = os.path.expanduser(
    os.path.join(getenv('XDG_CONFIG_DIR', '~/.config'), __package_name__)
)

BLACKLIST_FILENAME: str = 'blacklist.txt'

PROXY_MAPPING: Dict[str, str] = {'tor': 'socks5://localhost:9050'}

FILL_AND_SUBMIT_FORMS: str = """\
() =>
  [...document.forms].forEach(form => {
    for (let input of form) {
      if (!input.name) continue
      if (input.tagName === 'INPUT') {
        switch (input.type) {
          case 'hidden':
            continue
          case 'email':
            input.value = 'anonymous@mail.com'
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
              input.value = 'anonymous'
            } else if (/fisrt_?name/i.test(input.name)) {
              input.value = 'Anonymous'
            } else if (/last_?name/i.test(input.name)) {
              input.value = 'Anonymous'
            } else {
              input.value = 'test'
            }
        }
      } else if (input.tagName == 'TEXTAREA') {
        input.value = 'Test message'
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
  })
"""

EXTRACT_INTRERNAL_LINKS: str = """\
() => [...document.getElementsByTagName('a')].filter(
    a => a.hasAttribute('href') && !a.hasAttribute('download') && a.hostname === location.hostname
  ).map(a => a.href)
"""

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


@click.command()
@click.version_option(__version__)
@click.option(
    '--aiohttp_timeout',
    default=5.0,
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
    '-d',
    '--depth',
    default=3,
    help='crawl depth',
    type=int,
)
@click.option(
    '-i',
    '--input',
    default=sys.stdin,
    help='input file',
    type=click.File('r', encoding='utf-8'),
)
@click.option(
    '-m',
    '--max_pages',
    default=20,
    help='maximum pages to visit per site (-1 is nolimit)',
    type=int,
)
@click.option(
    '--navigation_timeout',
    default=10.0,
    help='page navigation timeout',
    type=float,
)
@click.option(
    '-o',
    '--output',
    default=sys.stdout,
    help='output file',
    type=click.File('w+', encoding='utf-8'),
)
@click.option(
    '-p',
    '--proxy_server',
    help='proxy server address, e.g. `socks5://localhost:9050` or simple `tor`',
)
@click.option(
    '-u',
    '--useragent',
    help='custom user agent',
)
@click.option(
    '--user_data_dir',
    help='user profile directory (for share session between instances)',
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
async def cli(
    checks_num: int,
    depth: int,
    max_pages: int,
    navigation_timeout: float,
    input: io.TextIOBase,
    output: io.TextIOBase,
    proxy_server: Optional[str],
    aiohttp_timeout: float,
    useragent: str,
    user_data_dir: Optional[str],
    verbosity: int,
    workers_num: int,
) -> Optional[int]:
    """\b
      _____  ____  _      _  _____                    _
     / ____|/ __ \| |    (_)/ ____|                  | |
    | (___ | |  | | |     _| |     _ __ __ ___      _| | ___ _ __
     \___ \| |  | | |    | | |    | '__/ _` \ \ /\ / / |/ _ \ '__|
     ____) | |__| | |____| | |____| | | (_| |\ V  V /| |  __/ |
    |_____/ \___\_\______|_|\_____|_|  \__,_| \_/\_/ |_|\___|_|
    """
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    levels: List[int] = [logging.WARNING, logging.INFO, logging.DEBUG]
    level: int = levels[min(verbosity, len(levels) - 1)]
    logger.setLevel(level)
    urls: List[str] = list(
        map(normalize_url, filter(None, map(str.strip, input.readlines())))
    )
    writer = ResultWriter(output)
    proxy_server: str = PROXY_MAPPING.get(proxy_server, proxy_server)
    blacklist_path: str = os.path.join(CONFIG_PATH, BLACKLIST_FILENAME)
    if not os.path.exists(blacklist_path):
        blacklist_path: str = os.path.join(CURRENT_PATH, BLACKLIST_FILENAME)
    with open(blacklist_path, 'r') as f:
        blacklist = BlackList.parse(f)
    crawler = SQLiCrawler(
        aiohttp_timeout=aiohttp_timeout,
        blacklist=blacklist,
        checks_num=checks_num,
        max_pages=max_pages,
        navigation_timeout=navigation_timeout,
        proxy_server=proxy_server,
        user_data_dir=user_data_dir,
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
        max_pages: int,
        navigation_timeout: int,
        proxy_server: Optional[str],
        user_data_dir: Optional[str],
        useragent: Optional[str],
        workers_num: int,
        writer: ResultWriter,
    ) -> None:
        self.aiohttp_timeout = aiohttp_timeout
        self.blacklist = blacklist
        self.checks_num = checks_num
        self.max_pages = max_pages
        self.navigation_timeout = navigation_timeout
        self.proxy_server = proxy_server
        self.user_data_dir = user_data_dir
        self.useragent = useragent
        self.workers_num = workers_num
        self.writer = writer

    async def crawl(self, urls: List[str], depth: int) -> None:
        self.url_queue = asyncio.Queue()
        url: str
        for url in urls:
            self.url_queue.put_nowait((url, depth))
        self.visited = VisitedUrls(limit_per_site=self.max_pages)
        self.checked_requests: Set[int] = set()
        self.browsers = weakref.WeakSet()
        # Запускаем таски в фоновом режиме
        workers: List[asyncio.Task] = [
            asyncio.create_task(self.worker()) for _ in range(self.workers_num)
        ]
        logger.info('wait until url queue becomes empty')
        await self.url_queue.join()
        logger.info('cancel workers')
        worker: asyncio.Task
        for worker in workers:
            worker.cancel()
        logger.info('close browsers')
        browser: Browser
        for browser in self.browsers:
            await browser.close()

    async def get_browser(self) -> Browser:
        logger.info('launch new browser instance')
        args: List[str] = [
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-setuid-sandbox',
            '--no-sandbox',
        ]
        if self.useragent:
            args.append(f'--user-agent={self.useragent}')
        if self.user_data_dir:
            args.append(f'--user-data-dir={self.user_data_dir}')
        if self.proxy_server:
            args.append(f'--proxy-server={self.proxy_server}')
        browser: Browser = await launch(
            headless=True, ignoreHTTPSErrors=True, args=args
        )
        # Инстансы браузера нужно обязательно останавливать чтобы не засирать
        # stdin ошибками
        # Удаленные сборщиком мусора объекты удаляются автоматически
        self.browsers.add(browser)
        return browser

    async def new_page(self, browser: Browser) -> Page:
        logger.info('open new page')
        page: Page = await browser.newPage()
        # page.setDefaultNavigationTimeout()
        await page.setRequestInterception(True)
        page.on('request', self.interception)
        page.on('response', self.on_response)
        return page

    async def worker(self) -> None:
        # Краш страниц часто приводит к падению браузера и зависанию, поэтому
        # вместо создания новых страниц, запускаем дополнительные инстансы
        browser: Browser = await self.get_browser()
        page: Page = await self.new_page(browser)
        task_name: str = asyncio.Task.current_task().get_name()
        while True:
            logger.info('%s is alive', task_name)
            url: str
            depth: int
            url, depth = await self.url_queue.get()
            try:
                if url in self.visited:
                    logger.debug('already visited: %s', url)
                    continue
                if not self.visited.can_add(url):
                    print(
                        'limit:',
                        self.visited._limit_per_site,
                        '\ncounter:',
                        self.visited._counter['bitcoinisscam.com'],
                    )
                    logger.debug('max page limit exceeded, skip %s', url)
                    continue
                logger.debug('goto %s', url)
                response: Response = await asyncio.wait_for(
                    page.goto(url, waitUntil='networkidle2'),
                    self.navigation_timeout,
                )
                logger.debug(
                    'response recieved: %s %s',
                    response.url,
                    response.status,
                )
                if not response:
                    logger.warning('empty response')
                    continue
                self.visited.add(url)
                if not response.ok:
                    continue
                ct: str
                _: Any
                ct, _ = cgi.parse_header(
                    response.headers.get('content-type', '')
                )
                if ct != 'text/html':
                    continue
                await page.evaluate(FILL_AND_SUBMIT_FORMS)
                if depth < 1:
                    continue
                links: List[str] = await page.evaluate(EXTRACT_INTRERNAL_LINKS)
                # перемешиваем ссылки
                random.shuffle(links)
                link: str
                for link in links:
                    await self.url_queue.put((link, depth - 1))
            except Exception as e:
                logger.error(e)
                logger.info('restart browser after page crash')
                await browser.close()
                # await page.close() вешает задание при ошибке Navigation Timeout Exceeded
                browser: Browser = await self.get_browser()
                page: Page = await self.new_page(browser)
            finally:
                self.url_queue.task_done()

    async def interception(self, request: Request) -> None:
        # Ускоряем загрузку страницы, отменяя загрузку стилей, картинок, шрифтов и т.д.
        # https://github.com/puppeteer/puppeteer/blob/f7857d27c4091ebcd219a8180e258f3b61a5de35/new-docs/puppeteer.httprequest.resourcetype.md
        # TODO: элементы могут быть скрыты через CSS
        if (
            request.resourceType
            in [
                'eventsource',
                'font',
                'image',
                'media',
                'stylesheet',
                'websocket',
            ]
            or self.blacklist.is_blacklisted(request.url)
        ):
            await request.abort()
        else:
            await request.continue_()

    async def on_response(self, response: Response) -> None:
        try:
            if response.status < 200 or response.status >= 500:
                logging.warning('bad status: %d', response.status)
                return
            ct: str = cgi.parse_header(
                response.headers.get('content-type', '')
            )[0]
            if ct not in ['text/html', 'application/json']:
                return
            await self.check_sqli(response)
        except Exception as e:
            logger.error(e)

    async def check_sqli(self, response: Response) -> None:
        result: Dict[str, Any] = await response._client.send(
            'Network.getCookies', {'urls': [response.url]}
        )
        cookies: Dict[str, str] = {
            c['name']: c['value'] for c in result['cookies']
        }
        request: Request = response.request
        url: yarl.URL = yarl.URL(request.url)
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
        request_hash: int = self.hash_request(request, data)
        if request_hash in self.checked_requests:
            logging.debug('request already checked')
            return
        if self.proxy_server:
            connector = ProxyConnector.from_url(
                self.proxy_server, verify_ssl=False
            )
        else:
            connector = aiohttp.TCPConnector(verify_ssl=False)
        timeout = aiohttp.ClientTimeout(total=self.aiohttp_timeout)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session:
            logger.info('check sqli: %s', url)
            injected_url: yarl.URL
            injected_data: Payload
            for injected_url, injected_data in itertools.islice(
                self.inject(url, data), self.checks_num
            ):
                try:
                    data_key: str = 'json' if is_json else 'data'
                    client_resp: aiohttp.ClientResponse
                    async with session.request(
                        request.method,
                        injected_url,
                        headers=request.headers,  # содержит content-type
                        cookies=cookies,
                        **{data_key: injected_data},
                    ) as client_resp:
                        content = await client_resp.text()
                        match: Union[None, re.Match]
                        if (match := SQLI_ERROR.search(content)) :
                            error: str = match.group()
                            logger.info('sqli found: %s', error)
                            entry = ResultEntry(
                                status=client_resp.status,
                                url=str(injected_url),
                                headers=request.headers,
                                cookies=cookies,
                                data=injected_data,
                                error=error,
                            )
                            self.writer.write(entry)
                except Exception as e:
                    logging.error(e)
        self.checked_requests.add(request_hash)

    def hash_request(self, request: Request, payload: Payload) -> int:
        url = yarl.URL(request.url)
        hashable: Dict[str, Any] = {
            'method': request.method,
            'url': str(url.with_query('').with_fragment('')),
            'query': sorted(url.query),
            'params': sorted(payload) if payload else None,
        }
        return hash(json.dumps(hashable, sort_keys=True))

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
