"""Mocked Discord UI & command integration tests for casino, pagination views, exams, and grades."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from icbot.db import Database
from icbot.features.blackjack import BlackjackTerminalView, BlackjackView, _sessions, blackjack
from icbot.features.exams import exams_add_cmd, exams_clear_cmd, exams_remove_cmd
from icbot.features.grades import grades_command
from icbot.features.help import HELP_CATEGORIES, HelpPaginationView, ic_help
from icbot.features.lc_leaderboard import LeaderboardView
from icbot.features.roulette import roulette_cmd
from icbot.features.slots import slots_cmd, slots_payouts_cmd
from icbot.games import blackjack_engine as bj
from icbot.services.fun_market import FunMarketStore
from icbot.services.user_service import UserStore
from tests.helpers import FakeMessage, load_fixture_text

pytestmark = pytest.mark.discord_mock


class _FixedShoe:
    def __init__(self, cards: list[bj.Card]):
        self._cards = list(cards)

    def draw(self) -> bj.Card:
        if not self._cards:
            raise AssertionError("FixedShoe exhausted unexpectedly")
        return self._cards.pop(0)


# ---------------------------------------------------------------------------
# Roulette & Slots commands end-to-end DB accounting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bet", "bet_type", "number", "spun_num", "expected_balance"),
    [
        (20, "straight", "00", -1, 1700),  # +700 net
        (100, "red", None, 1, 1100),  # +100 net
        (100, "red", None, 0, 900),  # green zero loses outside bet
        (50, "dozen2", None, 14, 1100),  # +100 net
        (50, "col3", None, 35, 950),  # 35 % 3 == 2 loses col3
    ],
)
async def test_roulette_command_settles_db_balance_accurately(
    make_interaction,
    make_member,
    tmp_db: Database,
    bet: int,
    bet_type: str,
    number: str | None,
    spun_num: int,
    expected_balance: int,
):
    member = make_member(user_id=800000000000000201, display_name="RoulettePlayer")
    inter = make_interaction(user=member, command_name="ic casino roulette")

    with patch("icbot.games.roulette_engine.random.choice", return_value=spun_num):
        await roulette_cmd.callback(inter, bet=bet, bet_type=bet_type, number=number)

    reply = inter.last_reply
    assert reply.embed is not None
    assert reply.ephemeral is False
    row = await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (member.id,))
    assert row == (expected_balance,)


@pytest.mark.parametrize(
    ("bet", "bet_type", "number"),
    [
        (0, "red", None),
        (-10, "red", None),
        (10, "green", None),
        (10, "straight", None),
        (10, "straight", "37"),
        (10, "straight", "abc"),
        (5000, "red", None),  # exceeds starting 1000 bankroll
    ],
)
async def test_roulette_command_rejects_invalid_bets_ephemerally(
    make_interaction,
    make_member,
    tmp_db: Database,
    bet: int,
    bet_type: str,
    number: str | None,
):
    member = make_member(user_id=800000000000000202, display_name="RouletteInvalid")
    await UserStore(tmp_db).ensure_user(member.id)

    inter = make_interaction(user=member, command_name="ic casino roulette")
    await roulette_cmd.callback(inter, bet=bet, bet_type=bet_type, number=number)

    assert inter.last_reply.ephemeral is True
    assert "❌" in (inter.last_reply.content or "")
    assert await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (member.id,)) == (1000,)


@pytest.mark.parametrize(
    ("bet", "symbols", "expected_balance", "expected_title"),
    [
        (10, [("💎", 50), ("💎", 50), ("💎", 50)], 1490, "JACKPOT"),
        (25, [("⭐", 2), ("⭐", 2), ("🍋", 3)], 1000, "Push"),
        (50, [("💎", 50), ("⭐", 2), ("🍋", 3)], 950, "No Match"),
    ],
)
async def test_slots_command_settles_db_balance_and_payouts_table(
    make_interaction,
    make_member,
    tmp_db: Database,
    bet: int,
    symbols: list[tuple[str, int]],
    expected_balance: int,
    expected_title: str,
):
    member = make_member(user_id=800000000000000203, display_name="SlotsPlayer")
    inter = make_interaction(user=member, command_name="ic casino slots")

    with patch("icbot.games.slots_engine.random.choice", side_effect=symbols):
        await slots_cmd.callback(inter, bet=bet)

    assert expected_title in (inter.last_reply.embed.title or "")
    assert await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (member.id,)) == (
        expected_balance,
    )

    # Payouts help command returns ephemeral table without mutating balance
    pay_inter = make_interaction(user=member, command_name="ic casino payouts")
    await slots_payouts_cmd.callback(pay_inter)
    assert pay_inter.last_reply.ephemeral is True
    assert "50x" in (pay_inter.last_reply.embed.description or "")


# ---------------------------------------------------------------------------
# Blackjack commands, interactive views, timeouts, and double-click protocol
# ---------------------------------------------------------------------------


async def test_blackjack_natural_deal_win_bust_double_split_and_forfeit(
    make_interaction,
    make_member,
    tmp_db: Database,
):
    # 1. Immediate natural blackjack at deal settles directly with BlackjackTerminalView
    p1 = make_member(user_id=800000000000000210, display_name="BJNat")
    nat_shoe = _FixedShoe([bj.Card("A", "♠"), bj.Card("10", "♦"), bj.Card("K", "♥"), bj.Card("9", "♣")])
    with patch("icbot.games.blackjack_engine._get_shoe", return_value=nat_shoe):
        start_inter = make_interaction(user=p1, command_name="ic casino blackjack")
        await blackjack.callback(start_inter, bet=100)

    assert isinstance(start_inter.last_reply.view, BlackjackTerminalView)
    assert _sessions.get(start_inter.guild.id, p1.id) is None
    assert await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (p1.id,)) == (1150,)
    assert await tmp_db.fetchone(
        "SELECT wins, hands_played FROM blackjack_users WHERE user_id=?", (p1.id,)
    ) == (1, 1)

    # 2. Live Hit -> Stand win flow via BlackjackView buttons
    p2 = make_member(user_id=800000000000000211, display_name="BJPlayer")
    live_shoe = _FixedShoe(
        [
            bj.Card("6", "♠"),
            bj.Card("10", "♦"),
            bj.Card("5", "♥"),
            bj.Card("7", "♣"),
            bj.Card("8", "♣"),  # Hit draws 8 -> 19
        ]
    )
    with patch("icbot.games.blackjack_engine._get_shoe", return_value=live_shoe):
        start2 = make_interaction(user=p2, command_name="ic casino blackjack")
        await blackjack.callback(start2, bet=100)

    live_view = start2.last_reply.view
    assert isinstance(live_view, BlackjackView)
    msg = await start2.original_response()

    # Unauthorized user blocked cleanly by interaction_check
    intruder = make_member(user_id=800000000000000299, display_name="Intruder")
    intruder_inter = make_interaction(user=intruder, message=msg, client=start2.client)
    assert await live_view.interaction_check(intruder_inter) is False
    assert intruder_inter.last_reply.ephemeral is True

    # Hit (non-bust 11 -> 19) renews view on msg
    hit_inter = make_interaction(user=p2, message=msg, client=start2.client)
    await live_view.hit_btn.callback(hit_inter)
    assert hit_inter.response.deferred is True
    assert isinstance(msg.view, BlackjackView)
    assert msg.view is not live_view

    # Stand on 19 vs dealer 17 -> settles +100 and swaps to BlackjackTerminalView
    stand_inter = make_interaction(user=p2, message=msg, client=start2.client)
    await msg.view.stand_btn.callback(stand_inter)
    assert isinstance(msg.view, BlackjackTerminalView)
    assert await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (p2.id,)) == (1100,)

    # Double-clicking Stand AFTER hand settled safely rejects without duplicate settlement
    dup_inter = make_interaction(user=p2, message=msg, client=start2.client)
    await live_view.stand_btn.callback(dup_inter)
    assert dup_inter.last_reply.ephemeral is True
    assert "No active hand" in (dup_inter.last_reply.content or "")
    assert await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (p2.id,)) == (1100,)
    assert await tmp_db.fetchone("SELECT hands_played FROM blackjack_users WHERE user_id=?", (p2.id,)) == (1,)

    # 3. Forfeit button debits reserved bet cleanly; timeout with missing bot disables buttons
    p3 = make_member(user_id=800000000000000212, display_name="BJForfeit")
    f_shoe = _FixedShoe([bj.Card("10", "♠"), bj.Card("9", "♦"), bj.Card("6", "♥"), bj.Card("8", "♣")])
    with patch("icbot.games.blackjack_engine._get_shoe", return_value=f_shoe):
        start3 = make_interaction(user=p3, command_name="ic casino blackjack")
        await blackjack.callback(start3, bet=150)

    msg3 = await start3.original_response()
    f_view = start3.last_reply.view
    assert isinstance(f_view, BlackjackView)
    f_inter = make_interaction(user=p3, message=msg3, client=start3.client)
    await f_view.forfeit_btn.callback(f_inter)
    assert await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (p3.id,)) == (850,)
    assert isinstance(msg3.view, BlackjackTerminalView)

    # Timeout with client resolves auto-forfeit; without client disables UI gracefully
    p4 = make_member(user_id=800000000000000213, display_name="BJTimeout")
    t_shoe = _FixedShoe([bj.Card("9", "♠"), bj.Card("10", "♦"), bj.Card("7", "♥"), bj.Card("6", "♣")])
    with patch("icbot.games.blackjack_engine._get_shoe", return_value=t_shoe):
        start4 = make_interaction(user=p4, command_name="ic casino blackjack")
        await blackjack.callback(start4, bet=200)

    t_view = start4.last_reply.view
    assert isinstance(t_view, BlackjackView)
    await t_view.on_timeout()
    assert await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (p4.id,)) == (800,)

    orphan_view = BlackjackView(guild_id=1, user_id=999)
    orphan_msg = FakeMessage(client=None)
    orphan_view.message = orphan_msg  # type: ignore[assignment]
    await orphan_view.on_timeout()
    assert all(child.disabled for child in orphan_view.children)


@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-05: Unsettled blackjack hands live only in RAM (_sessions) with no SQLite escrow; restart drops the active bet without settlement. Planned fix: Phase 4.",
)
async def test_blackjack_unsettled_virtual_bet_survives_restart(
    make_interaction,
    make_member,
    tmp_db: Database,
):
    member = make_member(user_id=800000000000000214, display_name="BJRestart")
    shoe = _FixedShoe([bj.Card("10", "♠"), bj.Card("9", "♦"), bj.Card("5", "♥"), bj.Card("7", "♣")])

    with patch("icbot.games.blackjack_engine._get_shoe", return_value=shoe):
        inter = make_interaction(user=member, command_name="ic casino blackjack")
        await blackjack.callback(inter, bet=400)

    assert _sessions.get(inter.guild.id, member.id) is not None

    # Simulate bot process restart clearing in-memory sessions
    _sessions.sessions.clear()

    # Expected safe economy invariant: either upfront wager escrow held the $400 bet
    # (`balance <= 600`) or persistent session recovery remains in SQLite.
    row = await tmp_db.fetchone("SELECT balance FROM users WHERE user_id=?", (member.id,))
    assert row is not None and row[0] <= 600


@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-06: BlackjackView.hit_btn acquires _lock without checking self.is_finished(), letting stale clicks on a stopped view mutate the live hand twice. Planned fix: Phase 4.",
)
async def test_blackjack_stopped_view_rejects_duplicate_hit_callback(
    make_interaction,
    make_member,
):
    member = make_member(user_id=800000000000000215, display_name="BJDoubleHit")
    shoe = _FixedShoe(
        [
            bj.Card("5", "♠"),
            bj.Card("10", "♦"),
            bj.Card("4", "♥"),
            bj.Card("7", "♣"),
            bj.Card("2", "♣"),  # Hit 1 -> 11
            bj.Card("3", "♦"),  # Should NOT be drawn by stale old_view Click 2
        ]
    )
    with patch("icbot.games.blackjack_engine._get_shoe", return_value=shoe):
        start_inter = make_interaction(user=member, command_name="ic casino blackjack")
        await blackjack.callback(start_inter, bet=50)

    old_view = start_inter.last_reply.view
    assert isinstance(old_view, BlackjackView)
    msg = await start_inter.original_response()

    click1 = make_interaction(user=member, message=msg, client=start_inter.client)
    click2 = make_interaction(user=member, message=msg, client=start_inter.client)

    await old_view.hit_btn.callback(click1)
    assert old_view.is_finished() is True

    # Stale duplicate callback arriving on already-stopped old_view
    await old_view.hit_btn.callback(click2)
    sess = _sessions.get(start_inter.guild.id, member.id)
    assert sess is not None
    assert len(sess.state.hands[0].cards) == 3  # currently mutates to 4 cards!


# ---------------------------------------------------------------------------
# Pagination UI views (LeaderboardView & HelpPaginationView)
# ---------------------------------------------------------------------------


async def test_leaderboard_and_help_pagination_views(
    make_interaction,
    make_member,
    tmp_db: Database,
):
    fm = FunMarketStore(tmp_db, starting_bankroll=1000)
    snaps = []
    for idx in range(12):
        uid = 800000000000000300 + idx
        await UserStore(tmp_db).ensure_user(uid, display_name=f"User{idx}")
        snaps.append(await fm.portfolio(user_id=uid))

    owner = make_member(user_id=800000000000000300, display_name="Owner")
    other = make_member(user_id=800000000000000399, display_name="Other")
    seed_inter = make_interaction(user=owner, command_name="ic leaderboard")

    lb_view = LeaderboardView(owner_id=owner.id, settings=seed_inter.client.settings, snaps=snaps, page=0)
    msg = FakeMessage(embed=lb_view._build_embed(), view=lb_view, client=seed_inter.client)
    lb_view.message = msg  # type: ignore[assignment]

    assert lb_view.prev_btn.disabled is True
    assert lb_view.next_btn.disabled is False

    # Other user blocked by interaction_check
    other_inter = make_interaction(user=other, message=msg, client=seed_inter.client)
    assert await lb_view.interaction_check(other_inter) is False
    assert other_inter.last_reply.ephemeral is True

    # Owner navigates to page 2
    next_inter = make_interaction(user=owner, message=msg, client=seed_inter.client)
    await lb_view.next_btn.callback(next_inter)
    assert isinstance(msg.view, LeaderboardView)
    assert msg.view.state.page == 1
    assert msg.view.prev_btn.disabled is False
    assert msg.view.next_btn.disabled is True

    # Timeout disables children cleanly
    await msg.view.on_timeout()
    assert all(c.disabled for c in msg.view.children)

    # HelpPaginationView navigation
    help_inter = make_interaction(user=owner, command_name="ic help")
    await ic_help.callback(help_inter)
    help_msg = await help_inter.original_response()
    help_view = help_inter.last_reply.view
    assert isinstance(help_view, HelpPaginationView)
    assert help_view.prev_button.disabled is True
    assert HELP_CATEGORIES[0]["title"] in (help_inter.last_reply.embed.title or "")

    h_next = make_interaction(user=owner, message=help_msg, client=help_inter.client)
    await help_view.next_button.callback(h_next)
    assert help_view.current_page == 1
    assert HELP_CATEGORIES[1]["title"] in (help_msg.embed.title or "")


# ---------------------------------------------------------------------------
# Exams & Grades edge cases and defect locks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("date", "start_time", "end_time"),
    [
        ("2026/12/10", "09:00", None),
        ("2026-12-10", "25:00", None),
        ("2026-12-10", "14:00", "12:00"),  # end before start
    ],
)
async def test_exams_reject_invalid_date_or_time_without_inserting(
    make_interaction,
    make_member,
    tmp_db: Database,
    date: str,
    start_time: str,
    end_time: str | None,
):
    admin = make_member(user_id=800000000000000401, administrator=True)
    inter = make_interaction(user=admin, command_name="ic exam add")
    await exams_add_cmd.callback(
        inter,
        course_code="COE817",
        course_name="Applied Cryptography",
        date=date,
        start_time=start_time,
        end_time=end_time,
    )
    assert "invalid date/time" in (inter.last_reply.content or "")
    assert await tmp_db.fetchall("SELECT id FROM exams") == []


async def test_exams_clear_rbac_and_execution(make_interaction, make_member, tmp_db: Database):
    regular = make_member(user_id=800000000000000402, administrator=False, role_ids=[])
    admin = make_member(user_id=800000000000000403, administrator=True)

    await exams_add_cmd.callback(
        make_interaction(user=admin, command_name="ic exam add"),
        course_code="CPS109",
        course_name="Computer Science I",
        date="2026-12-15",
        start_time="10:00",
    )
    unauth_clear = make_interaction(user=regular, command_name="ic exam clear")
    await exams_clear_cmd.callback(unauth_clear)
    assert "do not have permission" in (unauth_clear.last_reply.content or "")
    assert len(await tmp_db.fetchall("SELECT id FROM exams")) == 1

    auth_clear = make_interaction(user=admin, command_name="ic exam clear")
    await exams_clear_cmd.callback(auth_clear)
    assert "Cleared all exams" in (auth_clear.last_reply.content or "")
    assert await tmp_db.fetchall("SELECT id FROM exams") == []


@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-07: exams_add_cmd executes SELECT last_insert_rowid() on a fresh SQLite connection, returning ID 0 so add->remove round-trip fails. Planned fix: Phase 4.",
)
async def test_exams_add_returned_id_round_trips_into_remove(make_interaction, make_member):
    admin = make_member(user_id=800000000000000404, administrator=True)
    add_inter = make_interaction(user=admin, command_name="ic exam add")
    await exams_add_cmd.callback(
        add_inter,
        course_code="ELE888",
        course_name="Intelligent Systems",
        date="2026-12-18",
        start_time="13:00",
    )
    match = re.search(r"- ID:\s*(\d+)", add_inter.last_reply.content or "")
    assert match is not None
    returned_id = int(match.group(1))
    assert returned_id >= 1

    rm_inter = make_interaction(user=admin, command_name="ic exam remove")
    await exams_remove_cmd.callback(rm_inter, exam_id=returned_id)
    assert f"Removed exam ID {returned_id}." in (rm_inter.last_reply.content or "")


async def test_grades_command_success_sends_chart_attachment(
    make_interaction,
    mock_aiohttp,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from icbot.features import grades as grades_mod

    monkeypatch.setattr(grades_mod, "DB_PATH", str(tmp_path / "grades_cmd.db"))
    mock_aiohttp.get(
        "https://tmu.grades.zone/course/ele888",
        text=load_fixture_text("tmu_grades_course.html"),
    )

    inter = make_interaction(command_name="ic grades")
    await grades_command.callback(inter, course="ELE888")

    assert inter.response.deferred is True
    reply = inter.followup.messages[0]
    assert reply.file is not None
    assert reply.embed is not None
    assert "ELE888 - Intelligent Systems" in (reply.embed.title or "")


@pytest.mark.xfail(
    strict=True,
    reason="ISSUE-2B-08: grades_command passes unsupported `is_error=True` kwarg to embeds.system(), raising TypeError on 404 / empty-prof / error branches. Planned fix: Phase 3/4.",
)
async def test_grades_command_not_found_sends_error_embed_cleanly(
    make_interaction,
    mock_aiohttp,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from icbot.features import grades as grades_mod

    monkeypatch.setattr(grades_mod, "DB_PATH", str(tmp_path / "grades_404.db"))
    mock_aiohttp.get("https://tmu.grades.zone/course/nope999", status=404, text="Not Found")

    inter = make_interaction(command_name="ic grades")
    await grades_command.callback(inter, course="NOPE999")

    reply = inter.followup.messages[0]
    assert reply.embed is not None
    assert "Course Not Found" in (reply.embed.title or "")
