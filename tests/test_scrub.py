from googlechat._proto import googlechat_pb2 as gc

from .record_fixtures import find_leaks
from .scrub import Scrubber, strings


def test_same_input_same_fake_and_shape_kept():
    s = Scrubber(salt=b"x")
    a, b = s.text("115566778899"), s.text("115566778899")
    assert a == b and a.isdigit() and len(a) == 12 and a != "115566778899"
    name = s.text("Ann Lee")
    assert name[0].isupper() and name[3] == " " and name != "Ann Lee"


def test_emails_and_links_become_example_com():
    src = "mail ann@corp.io or see https://docs.corp.io/x?y=1 ok"
    out = Scrubber(salt=b"x").text(src)
    assert "corp" not in out
    assert "@example.com" in out and "https://example.com/" in out
    assert out.count(" ") == src.count(" ")  # punctuation and spacing kept


def test_message_scrubs_nested_and_repeated_strings_keeps_numbers_and_mimetypes():
    resp = gc.ListTopicsResponse()
    topic = resp.topics.add()
    topic.id.topic_id = "TOPIC123"
    msg = topic.replies.add(text_body="secret plan", create_time=42)
    msg.creator.email = "ann@corp.io"
    ann = msg.annotations.add(type=gc.DRIVE_DOC)
    ann.drive_metadata.title, ann.drive_metadata.mimetype = "Budget", "application/pdf"

    s = Scrubber(salt=b"x")
    s.message(resp)

    m = resp.topics[0].replies[0]
    assert resp.topics[0].id.topic_id != "TOPIC123"
    assert m.text_body != "secret plan" and m.create_time == 42
    assert m.creator.email.endswith("@example.com")
    assert m.annotations[0].drive_metadata.title != "Budget"
    assert m.annotations[0].drive_metadata.mimetype == "application/pdf"
    assert {"TOPIC123", "secret plan", "ann@corp.io", "Budget"} <= s.originals


def test_unknown_fields_are_dropped():
    # Field 999 is not in the schema: Google may send data we don't model.
    raw = gc.UserId(id="1").SerializeToString() + b"\xba\x3e\x05hello"
    msg = gc.UserId()
    msg.ParseFromString(raw)
    Scrubber(salt=b"x").message(msg)
    assert b"hello" not in msg.SerializeToString()


def test_strings_lists_nested_values():
    resp = gc.ListTopicsResponse()
    topic = resp.topics.add()
    topic.id.topic_id = "T"
    topic.replies.add(text_body="a\nb")
    assert sorted(strings(resp)) == ["T", "a\nb"]


def test_find_leaks_catches_multiline_and_quoted_values():
    # In textproto these are escaped ("line one\\nline two"), so a search of
    # the file text alone would miss them.
    secret = 'line one\nline "two"'
    assert find_leaks([secret], {secret}, set()) == [secret]
    assert find_leaks(["prefix " + secret], {secret}, set()) == [secret]
    assert find_leaks(["scrubbed"], {secret, "ok"}, {"cookie-value"}) == []
    assert find_leaks(["x cookie-value"], set(), {"cookie-value"}) == ["cookie-value"]
