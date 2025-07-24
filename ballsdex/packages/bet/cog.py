import datetime
from collections import defaultdict
from typing import TYPE_CHECKING, Optional, cast

import discord
from cachetools import TTLCache
from discord import app_commands
from discord.ext import commands
from discord.utils import MISSING
from tortoise.expressions import Q

from ballsdex.core.models import BallInstance, Player
from ballsdex.core.models import Trade as TradeModel
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import Pages
from ballsdex.core.utils.sorting import FilteringChoices, SortingChoices, filter_balls, sort_balls
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)
from ballsdex.packages.bet.display import BetViewFormat
from ballsdex.packages.bet.menu import BulkAddView, BetMenu
from ballsdex.packages.bet.bet_user import BettingUser
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot


@app_commands.guild_only()
class Bet(commands.GroupCog):
    """
    Bet countryballs with other players in coin flip games.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.bets: TTLCache[int, dict[int, list[BetMenu]]] = TTLCache(maxsize=999999, ttl=1800)
        self.log_channel_id = 1341228457417248940  # Channel for bet result logging

    bulk = app_commands.Group(name="bulk", description="Bulk Commands")

    def get_bet(
        self,
        interaction: discord.Interaction["BallsDexBot"] | None = None,
        *,
        channel: discord.TextChannel | None = None,
        user: discord.User | discord.Member = MISSING,
    ) -> tuple[BetMenu, BettingUser] | tuple[None, None]:
        """
        Find an ongoing bet for the given interaction.

        Parameters
        ----------
        interaction: discord.Interaction["BallsDexBot"]
            The current interaction, used for getting the guild, channel and author.

        Returns
        -------
        tuple[BetMenu, BettingUser] | tuple[None, None]
            A tuple with the `BetMenu` and `BettingUser` if found, else `None`.
        """
        guild: discord.Guild
        if interaction:
            guild = cast(discord.Guild, interaction.guild)
            channel = cast(discord.TextChannel, interaction.channel)
            user = interaction.user
        elif channel:
            guild = channel.guild
        else:
            raise TypeError("Missing interaction or channel")

        if guild.id not in self.bets:
            self.bets[guild.id] = defaultdict(list)
        if channel.id not in self.bets[guild.id]:
            return (None, None)
        to_remove: list[BetMenu] = []
        for bet in self.bets[guild.id][channel.id]:
            if (
                bet.current_view.is_finished()
                or bet.bettor1.cancelled
                or bet.bettor2.cancelled
            ):
                # remove what was supposed to have been removed
                to_remove.append(bet)
                continue
            try:
                bettor = bet._get_bettor(user)
            except RuntimeError:
                continue
            else:
                break
        else:
            for bet in to_remove:
                self.bets[guild.id][channel.id].remove(bet)
            return (None, None)

        for bet in to_remove:
            self.bets[guild.id][channel.id].remove(bet)
        return (bet, bettor)

    @app_commands.command()
    async def begin(self, interaction: discord.Interaction["BallsDexBot"], user: discord.User):
        """
        Begin a coin flip bet with the chosen user.

        Parameters
        ----------
        user: discord.User
            The user you want to bet with
        """
        if user.bot:
            await interaction.response.send_message("You cannot bet with bots.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot bet with yourself.", ephemeral=True
            )
            return
        player1, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.get_or_create(discord_id=user.id)
        blocked = await player1.is_blocked(player2)
        if blocked:
            await interaction.response.send_message(
                "You cannot begin a bet with a user that you have blocked.", ephemeral=True
            )
            return
        blocked2 = await player2.is_blocked(player1)
        if blocked2:
            await interaction.response.send_message(
                "You cannot begin a bet with a user that has blocked you.", ephemeral=True
            )
            return

        bet1, bettor1 = self.get_bet(interaction)
        bet2, bettor2 = self.get_bet(channel=interaction.channel, user=user)  # type: ignore
        if bet1 or bettor1:
            await interaction.response.send_message(
                "You already have an ongoing bet.", ephemeral=True
            )
            return
        if bet2 or bettor2:
            await interaction.response.send_message(
                "The user you are trying to bet with is already in a bet.", ephemeral=True
            )
            return

        player1, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.get_or_create(discord_id=user.id)
        if player2.discord_id in self.bot.blacklist:
            await interaction.response.send_message(
                "You cannot bet with a blacklisted user.", ephemeral=True
            )
            return

        menu = BetMenu(
            self, interaction, BettingUser(interaction.user, player1), BettingUser(user, player2)
        )
        self.bets[interaction.guild.id][interaction.channel.id].append(menu)  # type: ignore
        await menu.start()
        await interaction.response.send_message("Bet started!", ephemeral=True)

    @app_commands.command(extras={"trade": TradeCommandType.PICK})
    async def add(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Add a countryball to your bet proposal.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to add to your bet
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return
        if not countryball.is_tradeable:
            await interaction.response.send_message(
                f"You cannot bet this {settings.collectible_name}.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if countryball.favorite:
            view = ConfirmChoiceView(
                interaction,
                accept_message=f"{settings.collectible_name.title()} added to bet.",
                cancel_message="This request has been cancelled.",
            )
            await interaction.followup.send(
                f"This {settings.collectible_name} is a favorite, "
                "are you sure you want to bet it?",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.value:
                return

        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.followup.send("You do not have an ongoing bet.", ephemeral=True)
            return
        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return
        if countryball in bettor.proposal:
            await interaction.followup.send(
                f"You already have this {settings.collectible_name} in your proposal.",
                ephemeral=True,
            )
            return
        if await countryball.is_locked():
            await interaction.followup.send(
                f"This {settings.collectible_name} is currently in an active trade or bet, "
                "please try again later.",
                ephemeral=True,
            )
            return

        await countryball.lock_for_trade()
        bettor.proposal.append(countryball)
        await interaction.followup.send(
            f"{countryball.countryball.country} added to bet.", ephemeral=True
        )

    @bulk.command(name="add", extras={"trade": TradeCommandType.PICK})
    async def bulk_add(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallEnabledTransform | None = None,
        sort: SortingChoices | None = None,
        special: SpecialEnabledTransform | None = None,
        filter: FilteringChoices | None = None,
    ):
        """
        Bulk add countryballs to your bet proposal, with parameters to aid with searching.

        Parameters
        ----------
        countryball: Ball
            The countryball you would like to filter the results to
        sort: SortingChoices
            Choose how countryballs are sorted. Can be used to show duplicates.
        special: Special
            Filter the results to a special event
        filter: FilteringChoices
            Filter the results to a specific filter
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.followup.send("You do not have an ongoing bet.", ephemeral=True)
            return
        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return
        query = BallInstance.filter(player__discord_id=interaction.user.id)
        if countryball:
            query = query.filter(ball=countryball)
        if special:
            query = query.filter(special=special)
        if sort:
            query = sort_balls(sort, query)
        if filter:
            query = filter_balls(filter, query, interaction.guild_id)
        balls = await query
        if not balls:
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} found.", ephemeral=True
            )
            return
        balls = [x for x in balls if x.is_tradeable]

        view = BulkAddView(interaction, balls, self)  # type: ignore
        await view.start(
            content=f"Select the {settings.plural_collectible_name} you want to add "
            "to your bet proposal, note that the display will wipe on pagination however "
            f"the selected {settings.plural_collectible_name} will remain."
        )

    @app_commands.command(extras={"trade": TradeCommandType.REMOVE})
    async def remove(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Remove a countryball from your bet proposal.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to remove from your bet
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return

        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return
        if bettor.locked:
            await interaction.response.send_message(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return
        if countryball not in bettor.proposal:
            await interaction.response.send_message(
                f"That {settings.collectible_name} is not in your proposal.", ephemeral=True
            )
            return
        bettor.proposal.remove(countryball)
        await countryball.unlock()
        await interaction.response.send_message(
            f"{countryball.countryball.country} removed from bet.", ephemeral=True
        )

    @app_commands.command()
    async def view(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
    ):
        """
        View your current bet proposal or another user's if specified.

        Parameters
        ----------
        user: discord.User
            The user whose bet proposal you want to view
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return

        target_bettor = bettor
        if user:
            try:
                target_bettor = bet._get_bettor(user)
            except RuntimeError:
                await interaction.response.send_message(
                    "That user is not part of this bet.", ephemeral=True
                )
                return

        if not target_bettor.proposal:
            owner_text = "Your" if target_bettor == bettor else f"{target_bettor.user.name}'s"
            await interaction.response.send_message(
                f"{owner_text} bet proposal is empty.",
                ephemeral=True,
            )
            return

        owner_text = "Your" if target_bettor == bettor else f"{target_bettor.user.name}'s"
        embed = discord.Embed(
            title=f"{owner_text} bet proposal",
            color=discord.Colour.gold(),
        )

        proposal_text = ""
        for i, ball in enumerate(target_bettor.proposal, 1):
            ball_desc = ball.description(short=True, include_emoji=True, bot=self.bot)
            proposal_text += f"{i}. {ball_desc}\n"

        if len(proposal_text) > 4000:
            proposal_text = proposal_text[:4000] + "...\n*(List truncated)*"

        embed.description = proposal_text or "*Empty*"
        embed.set_footer(text=f"Total: {len(target_bettor.proposal)} {settings.plural_collectible_name}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command()
    async def reset(self, interaction: discord.Interaction["BallsDexBot"]):
        """
        Clear your bet proposal.
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return
        if bettor.locked:
            await interaction.response.send_message(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        view = ConfirmChoiceView(
            interaction,
            accept_message="Clearing your proposal...",
            cancel_message="This request has been cancelled.",
        )
        await interaction.followup.send(
            "Are you sure you want to clear your bet proposal?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        for ball in bettor.proposal:
            await ball.unlock()

        bettor.proposal.clear()
        await interaction.followup.send("Bet proposal cleared.", ephemeral=True)

    @app_commands.command()
    async def cancel(self, interaction: discord.Interaction["BallsDexBot"]):
        """
        Cancel your ongoing bet.
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        view = ConfirmChoiceView(
            interaction,
            accept_message="Cancelling the bet...",
            cancel_message="This request has been cancelled.",
        )
        await interaction.followup.send(
            "Are you sure you want to cancel this bet?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        await bet.user_cancel(bettor)
        await interaction.followup.send("Bet has been cancelled.", ephemeral=True)

    @app_commands.command()
    async def lock(self, interaction: discord.Interaction["BallsDexBot"]):
        """
        Lock your bet proposal to proceed to confirmation.
        """
        bet, bettor = self.get_bet(interaction)
        if not bet or not bettor:
            await interaction.response.send_message(
                "You do not have an ongoing bet.", ephemeral=True
            )
            return
        if bettor.locked:
            await interaction.response.send_message(
                "You have already locked your proposal!", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await bet.lock(bettor)

        if bet.bettor1.locked and bet.bettor2.locked:
            await interaction.followup.send(
                "Your proposal has been locked. The bet is now ready for confirmation!",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Your proposal has been locked. Waiting for the other player to lock theirs.",
                ephemeral=True,
            )
