"""指令解析、简繁归一化与跨源章节匹配单元测试。"""

from __future__ import annotations

import pytest

from core.chapter_matcher import (
    analyze_chapter_meta,
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


def test_volundio_multi_part_and_chapter_prefix_disambiguation() -> None:
    """回归测试《異劍戰記Völundio》真实章节列表：解决 `第二话` 误命中 `第二章 13话` 问题。"""
    raw_titles = [
        "预告",
        "第00话",
        "第01话",
        "第02话",
        "短篇小剧场2",
        "第13话①",
        "第13话②",
        "第01卷附录",
        "03卷情报&发表",
        "第54话试看",
        "第二部第1话",
        "第二部第2话",
        "第二部 06话",
        "45-54话的附加彩色插画",
        "第二章  12话（前篇）",
        "第二章 12话（后篇）",
        "第二章 13话",
        "第二部 17话",
    ]
    chapters: list[ChapterInfo] = []
    for idx, t in enumerate(raw_titles):
        meta = analyze_chapter_meta(t, "默認")
        chapters.append(
            ChapterInfo(
                chapter_id=f"id_{idx}",
                title=t,
                number=meta.number,
                part_number=meta.part_number,
                sub_order=meta.sub_order,
                is_trial=meta.is_trial,
                kind=meta.kind,
                group_name="默認",
                order_index=idx,
                page_count=33 if t == "第二章 13话" else 28,
            )
        )

    # 1. `/manga 异剑战记 第二话` 必须精准命中第一部 `第02话`，绝不能误发 `第二章 13话` 或 `第二部第2话` 或 `短篇小剧场2`
    cmd_p1_c2 = parse_manga_command("异剑战记 第二话")
    assert cmd_p1_c2.manga_query == "异剑战记"
    assert cmd_p1_c2.chapter_spec == "第二话"
    matched_2 = select_matching_chapter(
        chapters, parse_chapter_target(cmd_p1_c2.chapter_spec), "異劍戰記Völundio"
    )
    assert matched_2.title == "第02话"

    # 2. `/manga 异剑战记 13话` 命中第一部 `第13话①`（而非第二章 13话，且 ① 优先于 ②）
    matched_13 = select_matching_chapter(chapters, parse_chapter_target("13话"), "異劍戰記Völundio")
    assert matched_13.title == "第13话①"

    # 3. 显式指定分部/分章：`/manga 异剑战记 第二章 13话` 与 `/manga 异剑战记 第二部 13话` 均精准命中 `第二章 13话`
    cmd_p2_13 = parse_manga_command("异剑战记 第二章 13话")
    assert cmd_p2_13.manga_query == "异剑战记"
    assert cmd_p2_13.chapter_spec == "第二章 13话"
    assert (
        select_matching_chapter(chapters, parse_chapter_target(cmd_p2_13.chapter_spec)).title
        == "第二章 13话"
    )
    cmd_p2_13_bu = parse_manga_command("异剑战记 第二部 13话")
    assert (
        select_matching_chapter(chapters, parse_chapter_target(cmd_p2_13_bu.chapter_spec)).title
        == "第二章 13话"
    )

    # 4. 指定子篇章后缀：`/manga 异剑战记 第二章 12话 后篇` 精准命中后篇
    cmd_p2_12_hou = parse_manga_command("异剑战记 第二章 12话 后篇")
    assert cmd_p2_12_hou.manga_query == "异剑战记"
    assert (
        select_matching_chapter(chapters, parse_chapter_target(cmd_p2_12_hou.chapter_spec)).title
        == "第二章 12话（后篇）"
    )

    # 5. `45-54话的附加彩色插画` 绝不干扰 `第54话` 匹配
    assert select_matching_chapter(chapters, parse_chapter_target("54话")).title == "第54话试看"

    # 6. `最新话` 应正确识别第二部最新进度 `第二部 17话`（而非第一部的 54 话）
    assert select_matching_chapter(chapters, parse_chapter_target("最新话")).title == "第二部 17话"


def test_kanokari_latest_chapter_ignores_birthday_extras_and_reuploaded_volumes() -> None:
    """回归测试《女朋友、借我一下 / 租借女友》真实场景：
    1. 拷贝漫画 `2026和也生日` 绝不能将年份 2026 误识别为第 2026 话抢占 `最新话`；
    2. 再漫画连载组内的无“第”字单行本 `34卷`（即使补传后 `updated_at` 比 `448话` 更新）必须归为 `VOLUME`，绝不污染 `最新话`。
    """
    raw_items = [
        ("34卷", "连载", 1795001239),
        ("2026和也生日", "默認", 1795002000),
        ("2026和也祭典", "默認", 1795002500),
        ("08卷特典", "其他系列", 1795003000),
        ("406话试看", "其它汉化版", 0),
        ("447话", "连载", 1793937516),
        ("第448话", "默認", 1794492395),
    ]
    chapters: list[ChapterInfo] = []
    for idx, (title, grp, upd) in enumerate(raw_items):
        meta = analyze_chapter_meta(title, grp)
        chapters.append(
            ChapterInfo(
                chapter_id=f"k_{idx}",
                title=title,
                number=meta.number,
                part_number=meta.part_number,
                sub_order=meta.sub_order,
                is_trial=meta.is_trial,
                kind=meta.kind,
                group_name=grp,
                order_index=idx * 10,
                updated_at=upd,
            )
        )

    assert chapters[0].kind == ChapterKind.VOLUME and chapters[0].number == 34.0
    assert chapters[1].kind == ChapterKind.SPECIAL
    assert chapters[2].kind == ChapterKind.SPECIAL and chapters[2].number is None
    assert chapters[3].kind == ChapterKind.SPECIAL

    latest = select_matching_chapter(chapters, parse_chapter_target("最新话"), "女朋友、借我一下")
    assert latest.title == "第448话"

    vol34 = select_matching_chapter(chapters, parse_chapter_target("34卷"), "女朋友、借我一下")
    assert vol34.title == "34卷"
