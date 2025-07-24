from typing import TYPE_CHECKING, Iterable

import discord

from ballsdex.core.models import Trade as TradeModel
from ballsdex.core.utils import menus
from ballsdex.core.utils.paginator import Pages
from ballsdex.packages.bet.bet_user import BettingUser

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot


class BetViewFormat(menus.ListPageSource):
    def __init__(
        self,
        entries: Iterable[TradeModel],
        header: str,
        bot: "BallsDexBot",
        is_admin: bool = False,
        url: str | None = None,
    ):
        self.header = header
        self.url = url
        self.bot = bot
        self.is_admin = is_admin
        super().__init__(entries, per_page=1)

    async def format_page(self, menu: Pages, bet_data: TradeModel) -> discord.Embed:
        embed = discord.Embed(
            title=f"Bet history for {self.header}",
            description=f"Bet ID: {bet_data.pk:0X}",
            url=self.url if self.is_admin else None,
            timestamp=bet_data.date,
        )
        embed.set_footer(
            text=f"Bet {menu.current_page + 1}/{menu.source.get_max_pages()} | Bet date: "
        )
        # Note: This would need adaptation for bet-specific data if bet history is implemented
        return embed


def _get_prefix_emote(bettor: BettingUser) -> str:
    if bettor.cancelled:
        return "\N{NO ENTRY SIGN}"
    elif bettor.accepted:
        return "\N{WHITE HEAVY CHECK MARK}"
    elif bettor.locked:
        return "\N{LOCK}"
    else:
        return ""


def _get_bettor_name(bettor: BettingUser, is_admin: bool = False) -> str:
    if is_admin:
        blacklisted = "\N{NO MOBILE PHONES} " if bettor.blacklisted else ""
        return f"{blacklisted}{_get_prefix_emote(bettor)} {bettor.user.name} ({bettor.user.id})"
    else:
        return f"{_get_prefix_emote(bettor)} {bettor.user.name}"


def _build_list_of_strings(
    bettor: BettingUser, bot: "BallsDexBot", short: bool = False
) -> list[str]:
    # this builds a list of strings always lower than 1024 characters
    # while not cutting in the middle of a line
    proposal: list[str] = [""]
    i = 0

    for ball in bettor.proposal:
        ball_text = ball.description(short=short, include_emoji=True, bot=bot, is_trade=True)
        if bettor.locked:
            text = f"- *{ball_text}*\n"
        else:
            text = f"- {ball_text}\n"
        if bettor.cancelled:
            text = f"~~{text}~~"

        if len(text) + len(proposal[i]) > 950:
            # move to a new list element
            i += 1
            proposal.append("")
        proposal[i] += text

    if not proposal[0]:
        proposal[0] = "*Empty*"

    return proposal


def fill_bet_embed_fields(
    embed: discord.Embed,
    bot: "BallsDexBot",
    bettor1: BettingUser,
    bettor2: BettingUser,
    compact: bool = False,
    is_admin: bool = False,
):
    """
    Fill the fields of an embed with the items part of a bet.

    This handles embed limits and will shorten the content if needed.

    Parameters
    ----------
    embed: discord.Embed
        The embed being updated. Its fields are cleared.
    bot: BallsDexBot
        The bot object, used for getting emojis.
    bettor1: BettingUser
        The player that initiated the bet, displayed on the left side.
    bettor2: BettingUser
        The player that was invited to bet, displayed on the right side.
    compact: bool
        If `True`, display balls in a compact way. This should not be used directly.
    is_admin: bool
        Whether to show admin-specific information.
    """
    embed.clear_fields()

    # first, build embed strings
    # to play around the limit of 1024 characters per field, we'll be using multiple fields
    # these vars are list of fields, being a list of lines to include
    bettor1_proposal = _build_list_of_strings(bettor1, bot, compact)
    bettor2_proposal = _build_list_of_strings(bettor2, bot, compact)

    # then display the text. first page is easy
    embed.add_field(
        name=_get_bettor_name(bettor1, is_admin),
        value=bettor1_proposal[0],
        inline=True,
    )
    embed.add_field(
        name=_get_bettor_name(bettor2, is_admin),
        value=bettor2_proposal[0],
        inline=True,
    )

    if len(bettor1_proposal) > 1 or len(bettor2_proposal) > 1:
        # we'll have to trick for displaying the other pages
        # fields have to stack themselves vertically
        # to do this, we add a 3rd empty field on each line (since 3 fields per line)
        i = 1
        while i < len(bettor1_proposal) or i < len(bettor2_proposal):
            embed.add_field(name="\u200B", value="\u200B", inline=True)  # empty

            if i < len(bettor1_proposal):
                embed.add_field(name="\u200B", value=bettor1_proposal[i], inline=True)
            else:
                embed.add_field(name="\u200B", value="\u200B", inline=True)

            if i < len(bettor2_proposal):
                embed.add_field(name="\u200B", value=bettor2_proposal[i], inline=True)
            else:
                embed.add_field(name="\u200B", value="\u200B", inline=True)
            i += 1

        # always add an empty field at the end, otherwise the alignment is off
        embed.add_field(name="\u200B", value="\u200B", inline=True)

    if len(embed) > 6000:
        if not compact:
            return fill_bet_embed_fields(
                embed, bot, bettor1, bettor2, compact=True, is_admin=is_admin
            )
        else:
            embed.clear_fields()
            embed.add_field(
                name=_get_bettor_name(bettor1, is_admin),
                value=(
                    f"Bet too long, only showing last page:\n{bettor1_proposal[-1]}"
                    f"\nTotal: {len(bettor1.proposal)}"
                ),
                inline=True,
            )
            embed.add_field(
                name=_get_bettor_name(bettor2, is_admin),
                value=(
                    f"Bet too long, only showing last page:\n{bettor2_proposal[-1]}\n"
                    f"Total: {len(bettor2.proposal)}"
                ),
                inline=True,
            )
