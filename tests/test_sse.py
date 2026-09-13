from wikilag.sse import parse_sse


def test_parses_a_single_event_block():
    lines = ["event: message", "id: abc", 'data: {"wiki":"enwiki"}', ""]
    (message,) = list(parse_sse(lines))
    assert message.event == "message"
    assert message.id == "abc"
    assert message.data == '{"wiki":"enwiki"}'


def test_ignores_keepalive_comments():
    lines = [":ok", "event: message", "data: one", "", ":ok"]
    assert len(list(parse_sse(lines))) == 1


def test_joins_multiline_data():
    lines = ["event: message", "data: {", 'data: "a": 1', "data: }", ""]
    (message,) = list(parse_sse(lines))
    assert message.data == '{\n"a": 1\n}'


def test_drops_incomplete_trailing_block():
    # A half-received event on a dropped connection is not an event.
    lines = ["event: message", "data: complete", "", "event: message", "data: partial"]
    (message,) = list(parse_sse(lines))
    assert message.data == "complete"


def test_id_persists_until_overwritten():
    lines = ["id: 1", "data: a", "", "data: b", ""]
    first, second = list(parse_sse(lines))
    assert first.id == "1"
    assert second.id == "1"
