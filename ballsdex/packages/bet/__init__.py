from typing import TYPE_CHECKING

from ballsdex.packages.bet.cog import Bet

if TYPE_CHECKING:
    from ballsdx.core.bot import BallsDexBot


async def setup(bot: "BallsDexBot"):
    await bot.add_cog(Bet(bot))
  
