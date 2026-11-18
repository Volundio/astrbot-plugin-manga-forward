"""指令解析、简繁归一化与跨源章节匹配单元测试。"""

from __future__ import annotations

import pytest

from core.chapter_matcher import (
    classify_chapter_meta,
    parse_chapter_target,
    select_matching_chapter,
)
from core.command_parser import extract_args_after_command, parse_manga_command
from core.models import (
    ChapterInfo,
    ChapterKind,
    ChapterNotFoundError,
    CommandUsageError,
    SourceId,
    TargetMode,
)
from core.text_utils import (
    parse_chinese_integer,
    score_title_match,
    to_simplified,
    to_traditional,
)


def test_command_extraction_and_basic_parsing() -> None:
    args = extract_args_after_command("/manga 电锯人 第1话")
    cmd = parse_manga_command(args)
    assert cmd.manga_query == "电锯人"
    assert cmd.chapter_spec == "第1话"
    assert cmd.forced_source is None
    assert cmd.explicit_manga_id is None


def test_command_with_spaces_in_title_and_quotes() -> None:
    cmd1 = parse_manga_command("辉夜大小姐想让我告白 天才们的恋爱头脑战 第120话")
    assert cmd1.manga_query == "辉夜大小姐想让我告白 天才们的恋爱头脑战"
    assert cmd1.chapter_spec == "第120话"

    cmd2 = parse_manga_command("《电锯人 第二部》 135")
    assert cmd2.manga_query == "电锯人 第二部"
    assert cmd2.chapter_spec == "135"

    cmd3 = parse_manga_command("电锯人第12话")
    assert cmd3.manga_query == "电锯人"
    assert cmd3.chapter_spec == "第12话"


def test_command_source_override_and_explicit_id() -> None:
    cmd = parse_manga_command("葬送的芙莉莲 10 -s 拷贝")
    assert cmd.manga_query == "葬送的芙莉莲"
    assert cmd.chapter_spec == "10"
    assert cmd.forced_source == SourceId.COPYMANGA

    cmd_id = parse_manga_command("zmh:57069 第1话")
    assert cmd_id.forced_source == SourceId.ZAIMANHUA
    assert cmd_id.explicit_manga_id == "57069"
    assert cmd_id.chapter_spec == "第1话"


def test_command_invalid_raises_usage_error() -> None:
    with pytest.raises(CommandUsageError):
        parse_manga_command("")
    with pytest.raises(CommandUsageError):
        parse_manga_command("只有漫画名没有话数")
    with pytest.raises(CommandUsageError):
        parse_manga_command("电锯人 1 -s unknown_site")


def test_sc_tc_and_title_scoring() -> None:
    assert (
        to_simplified("電鋸人 葬送的芙莉蓮 藥屋少女的呢喃") == "电锯人 葬送的芙莉莲 药屋少女的呢喃"
    )
    assert "電鋸" in to_traditional("电锯人")
    assert score_title_match("电锯人", "電鋸人") == 1.0
    # 正标题完全匹配应高于仅别名完全匹配的同人词条
    assert score_title_match("孤独摇滚", "孤独摇滚") > score_title_match(
        "孤独摇滚", "(C108) 同人本", ["孤独摇滚"]
    )
    assert score_title_match("海贼王", "完全无关的恋爱喜剧") < 0.3


def test_chinese_numerals() -> None:
    assert parse_chinese_integer("一") == 1
    assert parse_chinese_integer("十二") == 12
    assert parse_chinese_integer("二十三") == 23
    assert parse_chinese_integer("一百零五") == 105
    assert parse_chinese_integer("二〇三") == 203


def test_chapter_vs_volume_strict_isolation() -> None:
    k_vol, n_vol = classify_chapter_meta("第01卷", "单行本")
    assert k_vol == ChapterKind.VOLUME and n_vol == 1.0

    # 标题带“之卷”副标题但前部明确是“第1话”，必须识别为单话而非单行本卷
    k_chap, n_chap = classify_chapter_meta("第01话 宿命之卷", "连载")
    assert k_chap == ChapterKind.CHAPTER and n_chap == 1.0

    chapters = [
        ChapterInfo(
            chapter_id="v1", title="第1卷", number=1.0, kind=ChapterKind.VOLUME, group_name="单行本"
        ),
        ChapterInfo(
            chapter_id="c1",
            title="第01话 狗与电锯",
            number=1.0,
            kind=ChapterKind.CHAPTER,
            group_name="连载",
        ),
        ChapterInfo(
            chapter_id="c12_5",
            title="第12.5话 附篇",
            number=12.5,
            kind=ChapterKind.CHAPTER,
            group_name="连载",
        ),
        ChapterInfo(
            chapter_id="c120",
            title="第120话",
            number=120.0,
            kind=ChapterKind.CHAPTER,
            group_name="连载",
            updated_at=2000,
        ),
    ]

    # 请求“第1话”或“1”绝不能误命中“第1卷”
    matched_c1 = select_matching_chapter(chapters, parse_chapter_target("1"), "电锯人")
    assert matched_c1.chapter_id == "c1"

    # 请求“第1卷”精准命中整卷
    matched_v1 = select_matching_chapter(chapters, parse_chapter_target("第1卷"), "电锯人")
    assert matched_v1.chapter_id == "v1"

    # 小数话与中文数字话数
    matched_dec = select_matching_chapter(chapters, parse_chapter_target("12.5话"), "电锯人")
    assert matched_dec.chapter_id == "c12_5"

    matched_cn = select_matching_chapter(chapters, parse_chapter_target("第一百二十话"), "电锯人")
    assert matched_cn.chapter_id == "c120"

    # 最新话模式
    t_latest = parse_chapter_target("最新话")
    assert t_latest.mode == TargetMode.LATEST
    assert select_matching_chapter(chapters, t_latest, "电锯人").chapter_id == "c120"

    # 不存在的话数应抛出包含最新话提示的异常
    with pytest.raises(ChapterNotFoundError) as exc_info:
        select_matching_chapter(chapters, parse_chapter_target("999"), "电锯人")
    assert "第120话" in str(exc_info.value)
