from subnet_tracker.discovery.extract import Candidate, Occurrence, ScanRole, classify, extract
from subnet_tracker.types import DomainName

PAGE = """\
<!doctype html>
<html>
<head>
<link rel="preconnect" href="https://fonts.example.net">
<link rel="dns-prefetch" href="//dns.example.net">
<link rel="stylesheet" href="https://cdn.example.com/app.css">
<meta http-equiv="refresh" content="0; url=https://refresh.example.com/next">
<style>body { background: url("https://style.example.com/bg.png"); mask: url(https://unquoted.example.com/m.png); }</style>
</head>
<body>
<!-- https://comment.example.com/hidden -->
<a href="/relative">no</a>
<a href="https://same.example.com/x">x</a>
<img src="https://img.example.com/a.png" srcset="https://srcset.example.com/a.png 1x, //srcset2.example.com/b.png 2x" data-src="https://data.example.com/v.mp4">
<video poster="https://poster.example.com/p.jpg"></video>
<form action="https://form.example.com/submit"></form>
<iframe src="//frame.example.com/x"></iframe>
<div style="background: url('https://attrstyle.example.com/a.png')"></div>
<script src="https://cdn.jsdelivr.net/lib.js"></script>
<script>
  const u = "https://inline.example.com/a";
  const v = "//slash.example.com/b";
  const w = "https:\\/\\/escaped.example.com\\/a";
  const skip = "http://not-from-script.example/c";
</script>
<a href="https://пример.рф/путь">idn</a>
<a href="http://plain.example.com/x">http</a>
</body>
</html>
"""

CSP = "default-src 'self'; script-src https://csp.example.com https://*.wild.example.org; img-src data: blob:; font-src fonts.gstatic.com; connect-src *.cdn.example.net"


def occ(name: str, source: str) -> Occurrence:
    return Occurrence(DomainName(name), source)


def test_extract_reads_the_chain_csp_and_html_sources() -> None:
    found = extract(
        ("https://www.example.com/some/page", "https://mid.example.net/r"),
        (("Content-Security-Policy", CSP),),
        PAGE,
    )
    assert found == (
        occ("www.example.com", "page host"),
        occ("mid.example.net", "redirect"),
        occ("csp.example.com", "CSP"),
        occ("wild.example.org", "CSP"),
        occ("fonts.gstatic.com", "CSP"),
        occ("cdn.example.net", "CSP"),
        occ("fonts.example.net", "preconnect"),
        occ("dns.example.net", "dns-prefetch"),
        occ("cdn.example.com", "link href"),
        occ("refresh.example.com", "meta refresh"),
        occ("style.example.com", "style"),
        occ("unquoted.example.com", "style"),
        occ("same.example.com", "a href"),
        occ("img.example.com", "img src"),
        occ("srcset.example.com", "img srcset"),
        occ("srcset2.example.com", "img srcset"),
        occ("data.example.com", "img data-src"),
        occ("poster.example.com", "video poster"),
        occ("form.example.com", "form action"),
        occ("frame.example.com", "iframe src"),
        occ("attrstyle.example.com", "style"),
        occ("cdn.jsdelivr.net", "script src"),
        occ("inline.example.com", "script"),
        occ("slash.example.com", "script"),
        occ("escaped.example.com", "script"),
        occ("xn--e1afmkfd.xn--p1ai", "a href"),
        occ("plain.example.com", "a href"),
    )
    assert occ("comment.example.com", "a href") not in found
    assert occ("not-from-script.example", "script") not in found


def test_meta_refresh_accepts_a_content_before_http_equiv() -> None:
    body = '<meta content="5; URL=\'https://refresh.example.com/next\'" http-equiv="Refresh">'
    assert extract((), (), body) == (occ("refresh.example.com", "meta refresh"),)


def candidate(name: str, role: ScanRole, sources: tuple[str, ...], *, exists: bool = False, registrable: str | None = None) -> Candidate:
    return Candidate(DomainName(name), role, exists, sources, registrable or name)


def test_same_site_is_selected_and_third_parties_are_grouped_by_registrable_domain() -> None:
    found = (
        occ("www.example.com", "page host"),
        occ("static.example.com", "script src"),
        occ("api.example.com", "CSP"),
        occ("fonts.gstatic.com", "link href"),
        occ("cdn.jsdelivr.net", "script src"),
        occ("ssl.gstatic.com", "style"),
    )
    candidates, ignored = classify(found, {"example.com"}, set(), ())
    assert ignored == ()
    assert candidates == (
        candidate("www.example.com", ScanRole.SAME, ("page host",), registrable="example.com"),
        candidate("static.example.com", ScanRole.SAME, ("script src",), registrable="example.com"),
        candidate("api.example.com", ScanRole.SAME, ("CSP",), registrable="example.com"),
        candidate("fonts.gstatic.com", ScanRole.THIRD, ("link href",), registrable="gstatic.com"),
        candidate("ssl.gstatic.com", ScanRole.THIRD, ("style",), registrable="gstatic.com"),
        candidate("cdn.jsdelivr.net", ScanRole.THIRD, ("script src",), registrable="jsdelivr.net"),
    )
    assert [item.selected for item in candidates] == [True, True, True, False, False, False]


def test_www_pair_collapses_by_text_and_stays_two_domains_when_it_is_not_a_pair() -> None:
    found = (
        occ("www.example.com", "page host"),
        occ("example.com", "a href"),
        occ("api.example.com", "script src"),
        occ("www.api.example.com", "CSP"),
    )
    candidates, _ = classify(found, {"example.com"}, set(), ())
    assert [(item.name, item.sources) for item in candidates] == [
        ("www.example.com", ("page host", "a href")),
        ("api.example.com", ("script src", "CSP")),
    ]


def test_www_co_uk_collapses_textually_while_its_registrable_domain_stays_www_co_uk() -> None:
    candidates, _ = classify((occ("www.co.uk", "page host"), occ("co.uk", "a href")), {"www.co.uk"}, set(), ())
    assert len(candidates) == 1
    assert candidates[0].name == "www.co.uk"
    assert candidates[0].sources == ("page host", "a href")
    assert candidates[0].registrable == "www.co.uk"
    assert candidates[0].selected


def test_example_co_uk_pair_is_one_same_site_domain() -> None:
    candidates, _ = classify((occ("example.co.uk", "page host"), occ("www.example.co.uk", "a href")), {"example.co.uk"}, set(), ())
    assert [item.name for item in candidates] == ["example.co.uk"]
    assert candidates[0].registrable == "example.co.uk"
    assert candidates[0].selected


def test_github_io_hosts_are_different_sites() -> None:
    candidates, _ = classify((occ("foo.github.io", "page host"), occ("bar.github.io", "script src")), {"foo.github.io"}, set(), ())
    assert candidates[0] == candidate("foo.github.io", ScanRole.SAME, ("page host",), registrable="foo.github.io")
    assert candidates[1] == candidate("bar.github.io", ScanRole.THIRD, ("script src",), registrable="bar.github.io")


def test_ignore_list_matches_the_name_its_subdomains_and_its_www_pair() -> None:
    found = (
        occ("www.w3.org", "a href"),
        occ("ns.w3.org", "script src"),
        occ("schema.org", "a href"),
        occ("notw3.org", "a href"),
        occ("example.com", "page host"),
    )
    existing = {DomainName("www.w3.org"), DomainName("w3.org")}
    candidates, ignored = classify(found, {"example.com"}, existing, ("w3.org", "schema.org", "ogp.me"))
    assert ignored == ("w3.org", "schema.org")
    assert [item.name for item in candidates] == ["example.com", "notw3.org"]
    pair_ignored, ignored_names = classify((occ("example.com", "page host"),), {"example.com"}, set(), ("www.example.com",))
    assert pair_ignored == ()
    assert ignored_names == ("www.example.com",)


def test_tracked_name_or_its_pair_is_shown_and_not_selected() -> None:
    found = (
        occ("example.com", "page host"),
        occ("www.api.example.com", "script src"),
        occ("cdn.jsdelivr.net", "script src"),
    )
    existing = {DomainName("api.example.com"), DomainName("www.cdn.jsdelivr.net")}
    candidates, _ = classify(found, {"example.com"}, existing, ())
    assert [(item.name, item.role, item.exists, item.selected) for item in candidates] == [
        ("example.com", ScanRole.SAME, False, True),
        ("www.api.example.com", ScanRole.SAME, True, False),
        ("cdn.jsdelivr.net", ScanRole.THIRD, True, False),
    ]
