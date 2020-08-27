import io

from sqlicrawler.utils import BlackList


def test_blacklist() -> None:
    si = io.StringIO('\t*://*.youtube.*/ # ggg\r\n\n')
    blacklist = BlackList.parse(si)
    assert len(blacklist.patterns) == 1
    assert blacklist.patterns[0].pattern == r'.+?://.+?\.youtube\..+?/'
    assert blacklist.is_blacklisted('https://www.google.it/') == False
    assert (
        blacklist.is_blacklisted('https://www.youtube.com/watch?v=BD1G9BNx3q4')
        == True
    )
