import io

from sqlicrawler.utils import BlackList


def test_blacklist() -> None:
    si = io.StringIO('\t*.facebook.com # ggg\r\n\n')
    blacklist = BlackList.parse(si)
    assert len(blacklist.patterns) == 1
    assert blacklist.patterns[0].pattern == '.*?\\.facebook\\.com$'
    assert blacklist.is_blacklisted('https://www.google.it/') == False
    assert (
        blacklist.is_blacklisted('https://www.facebook.com/plugins/blah/blah')
        == True
    )
