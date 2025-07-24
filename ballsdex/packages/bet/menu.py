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
from ballsdex.packages.bet.display import fill_bet_embed_fields
from ballsdex.packages.bet.bet_user import BettingUser
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
    from ballsdex.packages.bet.cog import Bet as BetCog

log = logging.getLogger("ballsdex.packages.bet.menu")


class InvalidBetOperation(Exception):
    pass


class BetView(View):
    def __init__(self, bet: BetMenu):
        super().__init__(timeout=60 * 30)
        self.bet = bet

    async def interaction_check(self, interaction: discord.Interaction["BallsDexBot"], /) -> bool:
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
    async def lock(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
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
                "Your proposal has been locked. Now confirm again to start the coin flip.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Your proposal has been locked. "
                "You can wait for the other user to lock their proposal.",
                ephemeral=True,
            )

    @button(label="Reset", emoji="\N{DASH SYMBOL}", style=discord.ButtonStyle.secondary)
    async def clear(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
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

        for ball in bettor.proposal:
            await ball.unlock()

        bettor.proposal.clear()
        await interaction.followup.send("Proposal cleared.", ephemeral=True)

    @button(
        label="Cancel bet",
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
        style=discord.ButtonStyle.danger,
    )
    async def cancel(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
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

    async def interaction_check(self, interaction: discord.Interaction["BallsDexBot"], /) -> bool:
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
    async def accept_button(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
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
                await interaction.followup.send("The coin flip is now concluded.", ephemeral=True)
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
    async def deny_button(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
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
        interaction: discord.Interaction["BallsDexBot"],
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

        self.embed.title = f"{settings.plural_collectible_name.title()} coin flip betting"
        self.embed.color = discord.Colour.gold()
        self.embed.description = (
            f"Add or remove {settings.plural_collectible_name} you want to bet "
            f"using the {add_command} and {remove_command} commands.\n"
            "Once you're finished, click the lock button below to confirm your proposal.\n"
            "After both players lock, confirm to flip the coin - winner takes all!\n\n"
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
        A loop task that updates every 15 seconds the menu with the new content.
        """

        assert self.task
        start_time = datetime.utcnow()

        while True:
            await asyncio.sleep(15)
            if datetime.utcnow() - start_time > timedelta(minutes=30):
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
            "is challenging you to a coin flip bet!",
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

        for ball in self.bettor1.proposal + self.bettor2.proposal:
            await ball.unlock()

        self.current_view.stop()
        for item in self.current_view.children:
            item.disabled = True  # type: ignore

        fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
        self.embed.description = f"**{reason}**"
        if getattr(self, "message", None):
            try:
                await self.message.edit(embed=self.embed, view=self.current_view)
            except discord.HTTPException:
                pass

    async def user_cancel(self, bettor: BettingUser):
        """
        Cancel the bet from a user action.
        """
        bettor.cancelled = True
        await self.cancel(f"The bet was cancelled by {bettor.user.name}.")

    async def lock(self, bettor: BettingUser):
        """
        Lock a user's proposal.
        """
        if bettor.locked:
            raise InvalidBetOperation("This user's proposal is already locked")

        bettor.locked = True

        if self.bettor1.locked and self.bettor2.locked:
            # Both users locked, switch to confirmation view
            self.current_view.stop()
            self.current_view = ConfirmView(self)
            self.cooldown_start_time = datetime.now(timezone.utc)

            # Update embed for confirmation phase
            self.embed.title = f"{settings.plural_collectible_name.title()} coin flip - Confirmation"
            self.embed.color = discord.Colour.orange()
            self.embed.description = (
                "**Both players have locked their proposals!**\n\n"
                "Click the ✅ button to confirm and flip the coin.\n"
                "The winner takes all the proposed items!\n\n"
                "*This is your last chance to cancel if you changed your mind.*"
            )

            fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)
            try:
                await self.message.edit(embed=self.embed, view=self.current_view)
            except discord.HTTPException:
                pass

    async def confirm(self, bettor: BettingUser) -> bool:
        """
        Confirm the bet and execute the coin flip if both users confirmed.
        """
        if bettor.accepted:
            return True

        bettor.accepted = True

        if self.bettor1.accepted and self.bettor2.accepted:
            return await self._execute_coin_flip()

        return True

    async def _execute_coin_flip(self) -> bool:
        """
        Execute the coin flip and transfer balls to the winner.
        """
        try:
            # Stop the update loop
            if self.task:
                self.task.cancel()

            # Perform the coin flip (50/50 chance)
            winner = random.choice([self.bettor1, self.bettor2])
            loser = self.bettor2 if winner == self.bettor1 else self.bettor1
            
            winner.won = True

            # Transfer all balls to the winner
            all_balls = winner.proposal + loser.proposal
            
            for ball in all_balls:
                ball.player = winner.player
                await ball.save()
                await ball.unlock()

            # Update embed to show results
            coin_emoji = "\N{COIN}" if random.choice([True, False]) else "\N{MONEY WITH WINGS}"
            self.embed.title = f"{settings.plural_collectible_name.title()} coin flip - Results!"
            self.embed.color = discord.Colour.green()
            self.embed.description = (
                f"{coin_emoji} **The coin has been flipped!** {coin_emoji}\n\n"
                f"🎉 **{winner.user.name} wins!** 🎉\n"
                f"💔 {loser.user.name} loses...\n\n"
                f"**{len(all_balls)} {settings.plural_collectible_name}** "
                f"have been transferred to {winner.user.mention}!"
            )

            # Show final proposals
            fill_bet_embed_fields(self.embed, self.bot, self.bettor1, self.bettor2)

            # Disable all buttons
            self.current_view.stop()
            for item in self.current_view.children:
                item.disabled = True  # type: ignore

            try:
                await self.message.edit(embed=self.embed, view=self.current_view)
            except discord.HTTPException:
                pass

            # Send log to specified channel
            await self._send_bet_result_log(winner, loser, all_balls)

            return True

        except Exception as e:
            log.exception(
                f"Failed to execute coin flip: guild={self.message.guild.id} "  # type: ignore
                f"bettor1={self.bettor1.user.id} bettor2={self.bettor2.user.id}"
            )
            
            # Unlock all balls in case of error
            for ball in self.bettor1.proposal + self.bettor2.proposal:
                await ball.unlock()
            
            return False

    async def _send_bet_result_log(self, winner: BettingUser, loser: BettingUser, all_balls: list) -> None:
        """
        Send bet result log to the specified logging channel.
        """
        try:
            # Get the log channel (hardcoded for FootballDx)
            log_channel_id = 1341228457417248940
            log_channel = self.bot.get_channel(log_channel_id)
            
            if not log_channel:
                log.warning(f"Could not find log channel with ID {log_channel_id}")
                return
            
            # Create ball names list
            ball_names = []
            for ball in all_balls:
                ball_names.append(ball.countryball.country)
            
            # Format the names nicely
            if len(ball_names) <= 3:
                balls_text = ", ".join(ball_names)
            else:
                balls_text = f"{', '.join(ball_names[:3])}, and {len(ball_names) - 3} more"
            
            # Create the log message
            log_message = (
                f"🪙 **FootballDx Bet Result**\n"
                f"👑 Winner: {winner.user.mention}\n"
                f"💀 Loser: {loser.user.mention}\n"
                f"🎁 Winner won `{len(all_balls)}` footballers: {balls_text}"
            )
            
            await log_channel.send(log_message)
            
        except Exception as e:
            log.exception(f"Failed to send bet result log: {e}")


class BulkAddView(CountryballsViewer):
    """
    View for bulk adding balls to bet proposals.
    """
    def __init__(self, interaction: discord.Interaction, balls: List[BallInstance], cog: BetCog):
        super().__init__(interaction, balls)
        self.cog = cog
        self.selected_balls: Set[BallInstance] = set()

    @discord.ui.button(
        label="Add selected",
        emoji="\N{HEAVY PLUS SIGN}",
        style=discord.ButtonStyle.success,
        row=2
    )
    async def add_selected(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        bet, bettor = self.cog.get_bet(interaction)
        if not bet or not bettor:
            await interaction.followup.send("You do not have an ongoing bet.", ephemeral=True)
            return
            
        if bettor.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited!", ephemeral=True
            )
            return

        if not self.selected_balls:
            await interaction.followup.send("No balls selected.", ephemeral=True)
            return

        added_count = 0
        errors = []

        for ball in self.selected_balls:
            if ball in bettor.proposal:
                errors.append(f"{ball.countryball.country} already in proposal")
                continue
                
            if await ball.is_locked():
                errors.append(f"{ball.countryball.country} is locked")
                continue
                
            await ball.lock_for_trade()
            bettor.proposal.append(ball)
            added_count += 1

        result_msg = f"Added {added_count} {settings.plural_collectible_name} to your bet."
        if errors:
            result_msg += f"\n\nErrors:\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                result_msg += f"\n... and {len(errors) - 5} more errors."

        await interaction.followup.send(result_msg, ephemeral=True)

    async def on_select(self, interaction: discord.Interaction, ball: BallInstance):
        """Handle ball selection/deselection"""
        if ball in self.selected_balls:
            self.selected_balls.remove(ball)
        else:
            self.selected_balls.add(ball)
        
        await interaction.response.defer()
