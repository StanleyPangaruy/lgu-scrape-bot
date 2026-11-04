from agent.runner import build_payload
from scraper.interceptor import Comment, SharedPost
from scraper.post_info import PostInfo, canonical_url, comment_post_id, pick_main_story
from server.limits import COMMENT_TEXT_LIMITS
from tests.conftest import comment_id


def story(post_id, author_id, author_url, text="t"):
    return SharedPost(id=f"S{post_id}", post_id=post_id, author_id=author_id, author_url=author_url, text=text)


FEED = story("2132078240789653", "100051283067407", "https://www.facebook.com/jojo.tiu.33")
LGU = story("1557299289776837", "100064903410975", "https://www.facebook.com/municipalityoflopezquezon")


def test_pick_main_story_by_page_slug():
    url = "https://www.facebook.com/municipalityoflopezquezon/posts/pfbid05gG95V"
    assert pick_main_story([FEED, LGU], url) is LGU


def test_pick_main_story_by_numeric_id():
    url = "https://www.facebook.com/groups/itphil/posts/2132078240789653/"
    assert pick_main_story([LGU, FEED], url) is FEED


def test_pick_main_story_by_permalink_owner():
    url = "https://www.facebook.com/permalink.php?story_fbid=pfbid0abc&id=100064903410975"
    assert pick_main_story([FEED, LGU], url) is LGU


def test_pick_main_story_none_when_unsure():
    assert pick_main_story([FEED], "https://www.facebook.com/someone-else/posts/pfbid0x") is None


def test_canonical_url_drops_tracking():
    assert canonical_url("https://www.facebook.com/x/posts/pfbid0?rdid=abc#c") == "https://www.facebook.com/x/posts/pfbid0"


def test_comment_post_id():
    assert comment_post_id(comment_id("1557299289776837", 5)) == "1557299289776837"
    assert comment_post_id("not base64!") is None
    assert comment_post_id("aGVsbG8=") is None  # "hello"


def test_build_payload_trims_long_fields_and_rejects_odd_post_ids():
    long_text = "x" * (COMMENT_TEXT_LIMITS["text"] + 10)
    comments = [Comment(id=comment_id("1", 1), text=long_text), Comment(id="y" * 500)]
    body = build_payload(PostInfo(url="u", post_id="abc"), comments)
    assert body["post"]["post_id"] is None
    assert len(body["comments"]) == 1
    assert len(body["comments"][0]["text"]) == COMMENT_TEXT_LIMITS["text"]
