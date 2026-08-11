from discord.ext import commands
import config


def control_channel_only():
    """限制只能在指定頻道使用"""

    async def predicate(ctx):
        if ctx.channel.id != config.CONTROL_CHANNEL_ID:
            await ctx.send("❌ 此指令只能在指定的控制頻道使用。")
            return False
        return True

    return commands.check(predicate)


def admin_only():
    """限制只有指定身分組可以使用"""

    async def predicate(ctx):

        if any(role.id == config.ADMIN_ROLE_ID for role in ctx.author.roles):
            return True

        await ctx.send("❌ 你沒有權限使用此指令。")
        return False

    return commands.check(predicate)