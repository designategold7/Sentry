from .core import ModLogPlugin, Actions
async def setup(bot):
    await bot.add_cog(ModLogPlugin(bot))