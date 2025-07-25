from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Set, cast

import discord
from discord.ui import Button, View, button
from discord.utils import format_dt, utcnow

from ballsdex.core.models import BallInstance, Player, TradeCooldownPolicy
from ballsdex.core.utils import menus
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import Pages
from ballsdex.packages.balls.countryballs_paginator import CountryballsViewer
from ballsdex.packages.bet.bet_user import BettingUser
from ballsdex.packages.bet.display import fill_bet_embed_fields
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import ballsdexBot
    from ballsdex.packages.bet.cog import Bet as BetCog

log = logging.getLogger("ballsdex.packages.bet.menu")


class InvalidBetOperation(Exception):
    pass


class BetView(View):
    def __init__(self, bet: BetMenu):
        super().__init__(timeout=60 * 30)
        self.bet = bet

    async def interaction_check(self, interaction: discord.Interaction["ballsdexBot"], /) -> bool:
        try:
            self.bet._get_bettor(interaction.user)
        except RuntimeError:
            await interaction.response.send_message(
                "You are not allowed to interact with this bet.", ephemeral=True
            )
            return False
        else:
            return True

    @button(label="Lock proposal", emoji="\N{LOCK}", style=discord.ButtonStyle.primary)
    async def lock(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        bettor = self.bet._get_bettor(interaction.user)
        if bettor.locked:
            await interaction.response.send_message(
                "You have already locked your proposal!", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        await self.bet.lock(bettor)
        if self.bet.bettor1.locked and self.bet.bettor2.locked:
            await interaction.followup.send(
                "Your proposal has been locked. Now confirm again to end the bet.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Your proposal has been locked. "
                "You can wait for the other user to lock their proposal.",
                ephemeral=True,
            )

    @button(label="Reset", emoji="\N{DASH SYMBOL}", style=discord.ButtonStyle.secondary)
    async def clear(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        bettor = self.bet._get_bettor(interaction.user)
        await interaction.response.defer(thinking=True, ephemeral=True)

        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return

        view = ConfirmChoiceView(
            interaction,
            accept_message="Clearing your proposal...",
            cancel_message="This request has been cancelled.",
        )
        await interaction.followup.send(
            "Are you sure you want to clear your proposal?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the bet instead.",
                ephemeral=True,
            )
            return

        for countryball in bettor.proposal:
            await countryball.unlock()

        bettor.proposal.clear()
        await interaction.followup.send("Proposal cleared.", ephemeral=True)

    @button(
        label="Cancel bet",
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
        style=discord.ButtonStyle.danger,
    )
    async def cancel(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        await interaction.response.defer(thinking=True, ephemeral=True)

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

        await self.bet.user_cancel(self.bet._get_bettor(interaction.user))
        await interaction.followup.send("Bet has been cancelled.", ephemeral=True)


class ConfirmView(View):
    def __init__(self, bet: BetMenu):
        super().__init__(timeout=90)
        self.bet = bet
        self.cooldown_duration = timedelta(seconds=10)

    async def interaction_check(self, interaction: discord.Interaction["ballsdexBot"], /) -> bool:
        try:
            self.bet._get_bettor(interaction.user)
        except RuntimeError:
            await interaction.response.send_message(
                "You are not allowed to interact with this bet.", ephemeral=True
            )
            return False
        else:
            return True

    @discord.ui.button(
        style=discord.ButtonStyle.success, emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}"
    )
    async def accept_button(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        bettor = self.bet._get_bettor(interaction.user)
        if bettor.player.trade_cooldown_policy == TradeCooldownPolicy.COOLDOWN:
            if self.bet.cooldown_start_time is None:
                return

            elapsed = datetime.now(timezone.utc) - self.bet.cooldown_start_time
            if elapsed < self.cooldown_duration:
                remaining_time = datetime.now(timezone.utc) + (self.cooldown_duration - elapsed)
                remaining = format_dt(remaining_time, style="R")
                await interaction.response.send_message(
                    f"This bet can only be approved {remaining}, please use this "
                    "time to double check the items to prevent any unwanted bets.",
                    ephemeral=True,
                )
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if bettor.accepted:
            await interaction.followup.send(
                "You have already accepted this bet.", ephemeral=True
            )
            return
        result = await self.bet.confirm(bettor)
        if self.bet.bettor1.accepted and self.bet.bettor2.accepted:
            if result:
                await interaction.followup.send("The FootballDex Bet is now concluded.", ephemeral=True)
            else:
                await interaction.followup.send(
                    ":warning: An error occurred while concluding the bet.", ephemeral=True
                )
        else:
            await interaction.followup.send(
                "You have accepted the bet, waiting for the other user...", ephemeral=True
            )

    @discord.ui.button(
        style=discord.ButtonStyle.danger,
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
    )
    async def deny_button(self, interaction: discord.Interaction["ballsdexBot"], button: Button):
        await interaction.response.defer(thinking=True, ephemeral=True)

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

        await self.bet.user_cancel(self.bet._get_bettor(interaction.user))
        await interaction.followup.send("Bet has been cancelled.", ephemeral=True)


class BetMenu:
    def __init__(
        self,
        cog: BetCog,
        interaction: discord.Interaction["ballsdexBot"],
        bettor1: BettingUser,
        bettor2: BettingUser,
    ):
        self.cog = cog
        self.bot = interaction.client
        self.channel: discord.TextChannel = cast(discord.TextChannel, interaction.channel)
        self.bettor1 = bettor1
        self.bettor2 = bettor2
        self.embed = discord.Embed()
        self.task: asyncio.Task | None = None
        self.current_view: BetView | ConfirmView = BetView(self)
        self.message: discord.Message
        self.cooldown_start_time: datetime | None = None

    def _get_bettor(self, user: discord.User | discord.Member) -> BettingUser:
        if user.id == self.bettor1.user.id:
            return self.bettor1
        elif user.id == self.bettor2.user.id:
            return self.bettor2
        raise RuntimeError(f"User with ID {user.id} cannot be found in the bet")

    def _generate_embed(self):
        add_command = self.cog.add.extras.get("mention", "`/bet add`")
        remove_command = self.cog.remove.extras.get("mention", "`/bet remove`")
        view_command = self.cog.view.extras.get("mention", "`/bet view`")

        self.embed.title = f"{settings.plural_collectible_name.title()} FootballDex Betting"
        self.embed.color = discord.Colour.gold()
        self.embed.description = (
            f"Add or remove {settings.plural_collectible_name} you want to bet "
            f"using the {add_command} and {remove_command} commands.\n"
            "Once you're finished, click the lock button below to confirm your proposal.\n"
            "You can also lock with nothing if you're receiving a gift.\n\n"
            "*This bet will timeout "
            f"{format_dt(utcnow() + timedelta(minutes=30), style='R')}.*\n\n"
            f"Use the {view_command} command to see the full"
            f" list of {settings.plural_collectible_name}."
        )
        self.embed.set_footer(
            text="This message is updated every 15 seconds, "
            "but you can keep on editing your proposal."
        )

    async def update_message_loop(self):
        """
        A loop task that updates each 15 second the menu with the new content.
        """

        assert self.task
        start_time = datetime.utcnow()

        while True:
            await asyncio.sleep(15)
            if datetime.utcnow() - start_time > timedelta(minutes=15):
                self.embed.colour = discord.Colour.dark_red()
                await self.cancel("The bet timed out")
                return

            try:
                fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
                await self.message.edit(embed=self.embed)
            except Exception:
                log.exception(
                    "Failed to refresh the bet menu "
                    f"guild={self.message.guild.id} "  # type: ignore
                    f"bettor1={self.bettor1.user.id} bettor2={self.bettor2.user.id}"
                )
                self.embed.colour = discord.Colour.dark_red()
                await self.cancel("The bet timed out")
                return

    async def start(self):
        """
        Start the bet by sending the initial message and opening up the proposals.
        """
        self._generate_embed()
        fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
        self.message = await self.channel.send(
            content=f"Hey {self.bettor2.user.mention}, {self.bettor1.user.name} "
            "is proposing a FootballDex Bet with you!",
            embed=self.embed,
            view=self.current_view,
            allowed_mentions=discord.AllowedMentions(users=self.bettor2.player.can_be_mentioned),
        )
        self.task = self.bot.loop.create_task(self.update_message_loop())

    async def cancel(self, reason: str = "The bet has been cancelled."):
        """
        Cancel the bet immediately.
        """
        if self.task:
            self.task.cancel()

        for countryball in self.bettor1.proposal + self.bettor2.proposal:
            await countryball.unlock()

        self.current_view.stop()
        for item in self.current_view.children:
            item.disabled = True  # type: ignore

        fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
        self.embed.description = f"**{reason}**"
        if getattr(self, "message", None):
            await self.message.edit(content=None, embed=self.embed, view=self.current_view)

    async def lock(self, bettor: BettingUser):
        """
        Lock a bettor's proposal.
        """
        bettor.locked = True

        if self.bettor1.locked and self.bettor2.locked:
            self.current_view.stop()
            self.current_view = ConfirmView(self)
            self.cooldown_start_time = datetime.now(timezone.utc)

            fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
            self.embed.description = (
                "Both users have locked their proposals. "
                "**Please confirm to start the FootballDex Bet!**\n\n"
                "*Winner takes all balls from both players.*"
            )
            await self.message.edit(embed=self.embed, view=self.current_view)

    async def confirm(self, bettor: BettingUser) -> bool:
        """
        Confirm the bet. If both players confirm, execute the FootballDex Bet.
        """
        bettor.accepted = True

        if self.bettor1.accepted and self.bettor2.accepted:
            # Execute the FootballDex Bet
            if self.task:
                self.task.cancel()

            # Random winner selection (50/50)
            winner = random.choice([self.bettor1, self.bettor2])
            loser = self.bettor2 if winner == self.bettor1 else self.bettor1
            
            winner.won = True

            try:
                # Transfer all balls to winner
                all_balls = self.bettor1.proposal + self.bettor2.proposal
                for ball in all_balls:
                    ball.player = winner.player
                    await ball.save()
                    await ball.unlock()

                # Clear proposals
                self.bettor1.proposal.clear()
                self.bettor2.proposal.clear()

                # Update embed
                fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
                self.embed.description = (
                    f"**FootballDex Bet concluded!**\n\n"
                    f"🏆 **{winner.user.name}** won the bet!\n"
                    f"💸 **{loser.user.name}** lost the bet.\n\n"
                    f"**Winner takes all {len(all_balls)} balls!**"
                )
                self.embed.color = discord.Colour.green()

                # Log to channel
                log_channel = self.bot.get_channel(1341228457417248940)
                if log_channel:
                    log_embed = discord.Embed(
                        title="FootballDex Bet Result",
                        color=discord.Colour.gold(),
                        timestamp=datetime.utcnow()
                    )
                    log_embed.add_field(
                        name="Winner", 
                        value=f"{winner.user.mention} ({winner.user.id})", 
                        inline=True
                    )
                    log_embed.add_field(
                        name="Loser", 
                        value=f"{loser.user.mention} ({loser.user.id})", 
                        inline=True
                    )
                    log_embed.add_field(
                        name="Balls Won", 
                        value=str(len(all_balls)), 
                        inline=True
                    )
                    await log_channel.send(embed=log_embed)

                # Disable view
                self.current_view.stop()
                for item in self.current_view.children:
                    item.disabled = True  # type: ignore

                await self.message.edit(embed=self.embed, view=self.current_view)
                return True

            except Exception as e:
                log.exception(f"Error executing FootballDex Bet: {e}")
                # Unlock all balls on error
                for ball in self.bettor1.proposal + self.bettor2.proposal:
                    await ball.unlock()
                return False

        return True

    async def user_cancel(self, bettor: BettingUser):
        """
        Cancel the bet from a user action.
        """
        bettor.cancelled = True
        await self.cancel(f"**{bettor.user.name}** cancelled the bet.")


class BulkAddView(CountryballsViewer):
    def __init__(
        self,
        interaction: discord.Interaction["ballsdexBot"],
        balls: List[BallInstance],
        bet_cog,
    ):
        super().__init__(interaction, balls)
        self.bet_cog = bet_cog
        self.selected_balls: Set[BallInstance] = set()

    async def start(self, *, content: str | None = None):
        await self.create_pages()
        if not self.pages:
            await self.interaction.followup.send(
                f"No {settings.plural_collectible_name} found.", ephemeral=True
            )
            return
        await super().start(content=content)

    def get_ball_mark(self, ball: BallInstance) -> str:
        if ball in self.selected_balls:
            return "✅"
        return ""

    async def update_page_content(self) -> discord.Embed:
        page = await super().update_page_content()
        page.title = f"Select {settings.plural_collectible_name} to add to bet"
        if len(self.selected_balls) > 0:
            page.description = (
                f"{page.description}\n\n"
                f"**{len(self.selected_balls)} {settings.plural_collectible_name} selected**"
            )
        return page

    @discord.ui.button(
        style=discord.ButtonStyle.success,
        emoji="✅",
        label="Toggle Selection",
        row=2,
    )
    async def toggle_selection_button(
        self, interaction: discord.Interaction["ballsdexBot"], button: Button
    ):
        await interaction.response.defer()
        
        balls_on_page = self.pages[self.current_page]
        for ball in balls_on_page:
            if ball in self.selected_balls:
                self.selected_balls.remove(ball)
            else:
                self.selected_balls.add(ball)
        
        await self.update_page()

    @discord.ui.button(
        style=discord.ButtonStyle.primary,
        emoji="➕",
        label="Add Selected",
        row=2,
    )
    async def add_selected_button(
        self, interaction: discord.Interaction["ballsdexBot"], button: Button
    ):
        await interaction.response.defer()
        
        if not self.selected_balls:
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} selected.", ephemeral=True
            )
            return

        bet, bettor = self.bet_cog.get_bet(interaction)
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

        added_balls = []
        favorites_to_confirm = []
        
        for ball in self.selected_balls:
            if ball in bettor.proposal:
                continue
            if await ball.is_locked():
                continue
            if not ball.is_tradeable:
                continue
                
            if ball.favorite:
                favorites_to_confirm.append(ball)
            else:
                added_balls.append(ball)

        # Handle favorites confirmation
        if favorites_to_confirm:
            view = ConfirmChoiceView(
                interaction,
                accept_message=f"{len(favorites_to_confirm)} favorite {settings.plural_collectible_name} added.",
                cancel_message="This request has been cancelled.",
            )
            await interaction.followup.send(
                f"You selected {len(favorites_to_confirm)} favorite {settings.plural_collectible_name}. "
                "Are you sure you want to bet them?",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if view.value:
                added_balls.extend(favorites_to_confirm)

        # Add balls to bet
        if added_balls:
            for ball in added_balls:
                await ball.lock_for_trade()
                bettor.proposal.append(ball)
            
            await interaction.followup.send(
                f"Added {len(added_balls)} {settings.plural_collectible_name} to your bet proposal.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} were added to your proposal.",
                ephemeral=True,
            )

        # Clear selection
        self.selected_balls.clear()
        await self.update_page()

    @discord.ui.button(
        style=discord.ButtonStyle.secondary,
        emoji="🗑️", 
        label="Clear Selection",
        row=2,
    )
    async def clear_selection_button(
        self, interaction: discord.Interaction["ballsdexBot"], button: Button
    ):
        await interaction.response.defer()
        self.selected_balls.clear()
        await self.update_page()